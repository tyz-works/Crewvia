# デーモン権限境界の設計 (dispatcher / watchdog)

> 作成: 2026-09-21
> タスク: 20260921-daemon-authority-and-mutual-watch/t001
> 関連: `knowledge/worker-shutdown-rules.md` (Rule 1-5)、`knowledge/worker-vanish-detection.md`、`knowledge/dispatcher-restart-after-merge.md`
> 種別: 設計メモ。**このタスクでは実装しない**。後続タスクはこの文書の決定に従う

## 1. 背景と目的

crewvia には Worker を終了させうる主体が 2 つある。`scripts/dispatcher.sh` と
`scripts/watchdog.py` だ。両者は**互いの存在を一切知らない**。コード上の相互参照は
ゼロで (`grep -i watchdog scripts/dispatcher.sh` → 0 件、逆も同じ)、`start.sh` が
mux 窓で並べて起動するだけで supervisor は無い。

(N2: 厳密には第 3 の主体がある。`scripts/benchmark-ctx.sh` が :199 / :312 で
`<agent>-worker` を直接 `mux_kill` する。dispatcher 側は `bench_gate_active()` /
`bench_worker_restarting()` (:1160-1172) で協調しているが、**watchdog にはこの
ゲートが無い** (`grep -ic bench scripts/watchdog.py` → 0)。`CREWVIA_BENCH_MODE=1`
のときだけ効く経路なので本文書のスコープ外として扱うが、「kill を一元化する」と
述べる以上、協調していない kill 主体が 1 つ残ることをここに明記しておく。)

この構造から 2 つの実害が出ている。

1. **後始末の穴** — watchdog は Worker プロセスを殺すが、task を pending に戻さず
   `queue/assignments/<agent>` も消さない。後始末は dispatcher の vanished worker
   検知が Director に `--reset` を促すことに依存している。dispatcher が死んでいる間に
   watchdog が Worker を殺すと、誰も気付かない幽霊 task が残る。
2. **判定の重複** — 「この Worker はもう要らない」の判定が dispatcher に 3 経路、
   watchdog に 1 経路あり、それぞれ別の閾値・別の dedup・別の grace を持つ。

本文書は (2) の全数を洗い出し、責務境界を確定し、権限移譲の方法・中断耐性の要件・
移行の原子性を決める。

---

## 2. 現状の全数洗い出し

「Worker を終了させうる経路」と、比較のために「終了させないが同じ事象を観測している
経路」を併記する。行番号は本 PR の base (`cccddb7`) 時点。

### 2-1. dispatcher.sh (5 秒ポーリング)

| # | 経路 | 場所 | 発火条件 | 実際に行う操作 | task / assignments の後始末 |
|---|---|---|---|---|---|
| **D1** | idle Worker shutdown | `shutdown_idle_workers()` :998-1015 | `*-worker` 窓が存在 **かつ** `queue/assignments/<agent>` が無い **かつ** spawn grace (90s) 外 **かつ** notify dedup (300s) 通過。**呼ばれるのは `active_missions` が空 (:1024) か全 mission done (:1044) のときだけ** | `タスクなし、shutdown` を send → 1s → `tmux_kill_window()` = `_mux.kill(窓)` | **しない**。assignment は元々無く、task には触れない |
| **D2** | no-task shutdown | `dispatch()` :1210-1238 | idle **かつ** skill 一致する pending task が blocked 含めゼロ **かつ** その Worker 名義の in_progress task が無い **かつ** spawn grace 外 **かつ** dedup 通過 | D1 と同一 (send → 1s → kill) | **しない** |
| **D3** | Rule 2 blocked-stuck | `dispatch()` :1239-1308 | idle **かつ** 一致 task はあるが全部 blocked **かつ** 直接 blocker に in_progress が無い **かつ** chain の最新 mtime が 600s (`BLOCKED_STUCK_THRESHOLD`) 以上古い **かつ** spawn grace 外 **かつ** dedup 通過 | D1 と同一 | **しない** |
| **D4** | vanished worker 検知 | `dispatch()` :1351-1379 | `status==in_progress` **かつ** worker 記録あり **かつ** 窓が無い **かつ** heartbeat が 600s (`AGENT_PRESENCE_TTL`) より古い / 無い | Director に `plan.sh update <id> --status pending --reset --mission <slug>` の手順を send **するだけ** | **しない**。Director の手動 `--reset` に依存 |
| **D5** | Rule 5 | `check_rule5()` :819-912 | mux state が `blocked`、または `idle`/`done` **かつ** assignment 有り、が `STATE_GRACE` (60s) 継続 | Director に画面末尾付きで通知 **するだけ** | **しない** |

D1 / D2 / D3 はいずれも `tmux_kill_window()` (:523) という単一の choke point を通る。
この関数は破壊直前に `repo_identity_ok()` を再チェックし、失敗時は kill せず戻る
(fail closed)。kill 成功時に spawn grace マーカー `registry/mux/<target>.firstseen`
を削除する (:561-566)。この unlink の意味は §3-4 で扱う。

(N4: 同関数の docstring :543-544 は call site として "vanished-worker cleanup" を
挙げているが、**これは stale**。D4 は通知のみで kill しない。`tmux_kill_window()` の
呼び出しは :1015 / :1238 / :1308 の 3 箇所だけで、いずれも D1 / D2 / D3 である。
上表の D4 行の方が正しい。コード側のコメント修正は t002 に申し送る → §6。)

### 2-2. watchdog.py (30 秒ポーリング)

| # | 経路 | 場所 | 発火条件 | 実際に行う操作 | task / assignments の後始末 |
|---|---|---|---|---|---|
| **W1** | warn | `run()` :746-753 | 窓あり **かつ** 子プロセス無し **かつ** `idle_seconds > idle_threshold` (既定 300s) | Taskvia に `type=alert` を POST + ログ | **しない** (そもそも殺さない) |
| **W2** | terminate | `check()` :295 → `graceful_terminate()` :401-474 (呼び出し :755-766) | (a) **monitor 生成**からの経過 > `max_threshold` (既定 3600s)、または (b) `idle_seconds > idle_threshold * 2` | shutdown メッセージ send → **60s 待機** → SIGTERM → **10s 待機** → SIGKILL。各破壊ステップ直前に `repo_identity_ok()` を再チェック (fail closed) | **しない**。task は `in_progress` のまま、assignment も残る。`del monitors[...]` はプロセスメモリ上の辞書から消すだけ |
| **W3** | kill | `run()` :768-778 | `_mux_window_name()` が `None` (窓が既に消滅) | ログ + Taskvia alert + `del monitors[...]`。**プロセスへの操作は無し** | **しない** |
| **W4** | mass-kill guard | `_is_mass_kill()` :335 / `run()` :693 | 全 monitor が `kill` **かつ** (mux 使用不可 or `mux.list()` が空) | 何もしない (cycle 丸ごと skip)。設定ミスを N 体の死と誤認しないための安全弁 | — |

**Worker プロセスを実際に殺すのは W2 だけ**である。W3 は名前に反して監視対象からの
除外のみで、`os.kill` は `graceful_terminate()` の中にしか存在しない (:456, :469)。

### 2-3. 現状から直接導かれる欠陥

- **E1: 後始末の担い手が居ない。** watchdog は `registry/` へのログ追記以外にファイルを
  書かない (`registry_dir.mkdir` が唯一の例外)。W2 が Worker を殺すと task は
  `in_progress`、assignment は残存し、それを掃除するのは D4 → Director の手動
  `--reset` だけ。dispatcher が停止していれば掃除は永久に来ない。
- **E2: W2 の再発火が遅延する。** `graceful_terminate()` 後に `del monitors[...]` する
  が、task は `in_progress` のままなので次 cycle で monitor が `started_at = now` で
  再生成される。つまり殺し損ねた Worker に対する **max ベースの terminate は改めて
  3600s 後にしか来ない**。idle ベース (b) は mtime 基準なので即再発火しうる。
- **E3: 判定が四重化している。** D1/D2/D3 と W2(b) は「この Worker は働いていない」と
  いう同一の事実を、別々の入力 (assignment ファイルの有無 / pending task の有無 /
  activity mtime) と別々の閾値 (90s grace / 600s / 600s) で判定している。

---

## 3. 責務境界の決定

### 3-1. 方針

> **仕事の有無の判定は dispatcher。プロセスの生死に対する強制終了は watchdog。**

理由は入力の所在である。dispatcher は `queue/` (task frontmatter、assignments、
mission state) を読む唯一のデーモンであり、「この Worker に割り当てる仕事があるか」は
そこにしか無い。watchdog は `registry/` の activity / heartbeat / notification と
mux のペイン PID を見るデーモンであり、「このプロセスは生きているか・応答するか」は
そこにしか無い。現状は dispatcher が後者の領域 (kill) に踏み込んでいる。

### 3-2. 制約: watchdog の monitor 母集団は in_progress task 由来しかない

これが設計上いちばん効く制約である。watchdog の監視対象は
`load_active_tasks()` (:509-536) が返す **`status == in_progress` の task** からのみ
生成される (`run()` :664-673)。`WorkerMonitor` は `task_id` と task frontmatter の
`timeout` / `worker_profile` を必須の構成要素として持つ (:144-152)。

ところが dispatcher が引き渡す「用済み Worker」(D1/D2/D3 の対象) は、**定義上
assignment も in_progress task も持たない**。したがって現在の watchdog には対応する
monitor が存在せず、既存の monitor ループでは 1 件も処理できない。

**帰結: 引き渡された Worker は、monitors とは独立した専用ループで処理する。**
同じ `while True:` の中に `process_retirements()` を 1 パス追加し、
`registry/retirements/*.json` を直接走査する。`monitors` 辞書には一切入れない。

**母集団を広げる案 (`load_active_tasks()` に idle Worker も含める) を採らない理由:**
`monitors` の要素は `check()` を通じて idle / max の **タイムアウト判定の対象**でもある。
そこに task を持たない Worker を入れると、

- `idle_threshold` / `max_threshold` は task frontmatter の `timeout` から来るが、
  引き渡された Worker には frontmatter が無いので `DEFAULT_PROFILE` (idle 300s /
  max 3600s) が一律に適用される。
- `started_at` は monitor 生成時刻なので、**引き渡しとは無関係に 3600s で terminate
  が発火する**新たな経路が生まれる。既に「健全な Worker を max で殺す」事故の前例が
  ある (memory: `watchdog-max-threshold-kills-long-tasks`)。
- `_last_activity_mtime()` は `{task_id}.activity` を見るが task_id が無い。

つまり母集団の拡大は、引き渡し処理を得る代わりに**新しい誤 terminate 経路を 2 本
増やす**。専用ループなら `check()` の判定条件は一切変わらない。

### 3-3. 引き渡し方法の比較

dispatcher が「この Worker は用済み」と判断してから watchdog が実際に終了させる
までの受け渡し方法を 3 案比較する。

| | 案A: task frontmatter | 案B: マーカーファイル | 案C: assignment ファイル拡張 |
|---|---|---|---|
| 置き場 | `queue/missions/<slug>/tasks/tNNN.md` に `retire_worker:` を追加 | `registry/retirements/<agent>.json` を新設 | `queue/assignments/<agent>` に tombstone を書く |
| 表現対象 | task | **Worker** | Worker |
| 既存の所有権との衝突 | **あり** (frontmatter は `plan.sh` が排他ロック付きで所有) | 無し | **あり** (5 箇所が不変条件を共有) |
| 致命的な問題 | **対象 Worker には書き込む先の task が存在しない** | — | **「存在 = busy」の不変条件が壊れる** |

**案A を否決する理由 (決定的)。** 引き渡しの対象は Worker であって task ではない。
D1 / D2 の対象 Worker は assignment も in_progress task も持たないので、フラグを
書き込むべき task ファイルが**そもそも無い**。D3 は「skill 一致する blocked な pending
task」を持つが、それは将来誰かがやる task であって当該 Worker 固有ではない — 同 skill の
Worker が複数居れば、誰の retire なのかを task 側では表現できない。加えて frontmatter は
`plan.sh` が `with_lock` 付きで所有しており、デーモンが直接書き手として加わるのは、
複数行 reason で frontmatter を壊した前例 (PR #181) と同じ経路に書き手をもう 1 人
増やすことを意味する。

**案C を否決する理由。** `queue/assignments/<agent>` は「**存在 = busy / 不在 = idle**」
という不変条件を、dispatcher の idle 判定 (D1 :1005、D2/D3 :1150-1151) と `plan.sh`
側の公開・撤去が共有している (plan.sh 側は t021 で `publish_assignment()` /
`retire_assignment()` の 2 関数に集約済み — §3-5)。tombstone を置くと「存在するが
idle」という第 4 の状態が生まれ、**Rule 5 の条件 B (`idle`/`done` かつ assignment
有り) が即座に誤発火する** (:854)。

**案B を採用する。** `registry/` は既にデーモン間共有状態の置き場である
(`heartbeats/`、`mux/`、`activity/`、`notifications/`、`watchdog-observations.jsonl`)。
Worker 名をファイル名にできるので対象を自然に表現でき、`plan.sh` の所有物に触れない。

**案B の書式と同時書き込み規律:**

```
registry/retirements/<agent>.json          ← dispatcher のみが書き、dispatcher のみが消す
  {"agent","window_target","reason":"no-task|blocked-stuck|all-done",
   "requested_at":<epoch>,"mission":<slug|null>,"task_id":<id|null>,
   "spawn_identity":{"pane_pid":<int|null>,"created_at":<epoch|null>}}

registry/retirements/<agent>.progress.json ← watchdog のみが書く
  {"phase":"notified|sigterm_sent|terminated|discarded","deadline":<epoch>,
   "pane_pid":<int|null>,"updated_at":<epoch>,"window_gone":<bool>,
   "discard_reason":<str|null>}
```

**1 ファイル 1 書き手**を規律にする。両デーモンが同一ファイルを更新する設計にすると
lost update のリスクを排他制御で潰す必要が出るが、ファイルを分ければロックが要らない。
書き込みは temp + `os.replace` の atomic rename に統一し、読み手が破損 JSON を見ない
ようにする。削除は dispatcher が両ファイルまとめて行う (R4)。

**`spawn_identity` が必須である理由 (F2)。** `{"agent","window_target",...}` だけでは
「**どの Worker インスタンスか**」を特定できない。watchdog 側で窓を引くのは
`_mux_window_name()` (:265-271) で、これは `<agent>-worker` → `<agent>` の順に名前解決
するだけである。crewvia は Worker 名を再利用する (Haruto / Seo / Arjun ...) ので、
marker が元の Worker より長生きすると**同名の別 Worker に着弾する**。発火列:

1. T0 に dispatcher が `retirements/Seo.json` を書く
2. watchdog が落ちる / 遅れる (ポーリング 30s、状態機械は R2 で最大 70s)
3. 元の Seo が自力終了し、Director が**同名**の Seo を再起動
4. watchdog 復帰 → R3 が `phase != terminated` を走査 → **窓が生きている**ので
   deadline を張り直す → 新しい Seo に SIGTERM

これは §5-2 が「先に watchdog に kill を足した場合」の最大の危険として挙げている事象
そのもので、marker 方式でも識別子が無ければ同じ穴が残る。R3 の「窓が生きていれば
deadline を張り直す (冪等)」は、**同名別インスタンスに対しては冪等ではない**。

識別子の取り方は backend 非依存にする。`pane_pid` は両 backend で `_mux.pid(name)`
(`lib_mux.py` の抽象メソッド :185) が返す。`created_at` は herdr なら
`_mux_created_at()` (dispatcher.sh :570) の `registry/mux/<target>.json`、tmux なら
`_spawn_time_fallback()` (:590) の `.firstseen` 値を使う。両方 null になる場合は
**marker を書かない** (fail closed。居座り = §5-1 の方に倒す)。ただし
**黙って捨てないこと** — dispatcher のログに理由付きで 1 行出す。出さないと
「用済み Worker が閉じられない」が dedup (TTL 300s) の陰に隠れて無症状になり、
§5-1 の発見が遅れる。再照合の規律は R7。

`.firstseen` を tmux 側の identity トークンに使う点は、§3-4 の sweep と噛み合っている
必要がある。sweep は**窓が `_mux.list()` に居ないときだけ** unlink するので、窓が
生きている限りトークンは安定して残る。窓が消えていれば R7 以前に R3 の (1)
(`window_gone`) で決着するため、トークンが消えていても困らない。`_mux.list()` の
一時的な失敗で unlink → 再出現時に新しい値が書かれた場合は、R7 が不一致と判定して
marker を捨てる = **殺さない**方向に倒れる。

リポジトリには既に同型の防御規約がある — `plan.sh:2960-2984` は assignment を消す前に
内容が `<slug>:<task_id>` と一致するかを確認する (コメント: "guard against accidentally
removing the assignment of a Worker who was already reused for a different task")。
R7 はこれと同じ考え方を破壊ステップに適用するものである。

### 3-4. 各経路の割り当て

| 経路 | 移行後の担当 | 変更内容 |
|---|---|---|
| **D1** idle shutdown | 判定 = dispatcher / 実行 = watchdog | `tmux_send` + `tmux_kill_window` を `retirements/<agent>.json` の書き込みに置換 |
| **D2** no-task shutdown | 同上 | 同上 (`reason: no-task`) |
| **D3** Rule 2 blocked-stuck | 同上 | 同上 (`reason: blocked-stuck`) |
| **D4** vanished 検知 | **dispatcher のまま** | queue の簿記なので移さない。加えて retirement の終端処理の受け口になる (R4) |
| **D5** Rule 5 | **dispatcher のまま** | 通知のみ。変更なし |
| **W1** warn | **watchdog のまま** | 変更なし |
| **W2** terminate | **watchdog のまま** | idle / max はプロセスの応答性の判定。ただし後始末の要件 (R4) を満たすよう改修 |
| **W3** kill (窓消滅) | **watchdog のまま** | monitor の除外は watchdog の内部管理。ただし queue 側の帰結 (幽霊 task) は D4 が拾う — この二重観測は意図的に残す |
| **新設** retirement 消費 | **watchdog** | `process_retirements()`。monitors とは独立したループ |
| **新設** `.firstseen` 掃除 | **dispatcher** | 窓が消えた target の spawn-grace マーカーを毎 cycle 掃除する (下記 F1) |

`graceful_terminate()` は現在 `WorkerMonitor` を引数に取るが、retirement には task が
無い。`(agent_name, window_target, repo_root)` を取る形に切り出し、W2 と retirement の
両方から使えるようにする。**monitor を捏造して渡す実装にしないこと** — `task_id` や
`started_at` が偽値になり、E2 の再発防止を難しくする。

**F1: `.firstseen` の後始末を落とさないこと。** D1/D2/D3 が通る
`tmux_kill_window()` は kill だけの関数ではない。kill 成功時に
`registry/mux/<target>.firstseen` を unlink する副作用を持つ (:561-566)。これは
t015 (PR #190 の QA FAIL) の修正本体で、docstring :525-536 が理由を明記している —
crewvia は Worker 名を再利用するため、tmux backend (`created_at` キャッシュが無い)
では stale な `.firstseen` が残ると**次に同名で起動した Worker が spawn grace ゼロに
なり 1 dispatch cycle で殺される (実測: spawn から 33 秒)**。

一方 `graceful_terminate()` に unlink は無い (watchdog のファイル書き込みは
`_LOG_FILE.open("a")` :554 と `_OBSERVATION_LOG_FILE.open("a")` :570、および
`registry_dir.mkdir` :610 だけである)。したがって kill 権限をそのまま watchdog に
移すと、**誰も `.firstseen` を消さなくなり、ゼロ猶予バグが再発する**。

**決定: `.firstseen` の所有者は dispatcher のままとし、kill 経路から切り離して
「窓が消えたら掃除する」sweep にする。**

- dispatcher は毎 cycle、`registry/mux/*.firstseen` のうち対応する target が
  `_mux.list()` に居ないものを unlink する。`_spawn_time_fallback()` が書き、
  dispatcher が消す — 書き手と消し手が同じで、§3-3 の「1 ファイル 1 書き手」と揃う。
- **kill の成否ではなく窓の不在をトリガにする**点が要点である。これにより D1/D2/D3
  経由・W2 経由・W3 (窓消滅) 経由・BENCH_MODE の `mux_kill` 経由 (N2) のどれで窓が
  消えても、掃除は同じ 1 箇所で閉じる。現状は W2 と BENCH_MODE で窓が消えた場合に
  誰も unlink しておらず、**これは移行前から存在する穴でもある**。
- `_mux.list()` が一時的に空を返した場合 (mux 不達・設定ミス) は全 `.firstseen` が
  消えるが、結果は「次の spawn grace が満額に戻る」= 殺されにくくなる方向なので
  fail-safe である。watchdog の `_is_mass_kill()` (:335) と同じ向きに倒れる。
- 逆方向の案 (watchdog の `graceful_terminate()` に unlink を足す) は採らない。
  watchdog が `registry/mux/` という dispatcher の状態ディレクトリに書き手として
  加わることになり、しかも W3 / BENCH_MODE の経路は依然として拾えない。
- **ロールバック経路 (`CREWVIA_KILL_AUTHORITY=dispatcher`) では `tmux_kill_window()`
  が復活する。その中の unlink を「新経路があるから不要」として削除しないこと** (R5 と
  同じ理由)。sweep との二重実行は unlink が冪等 (`missing_ok=True`) なので無害。

影響範囲は tmux backend のみである。herdr は `created_at` を `<target>.json` に
spawn ごとに書き直すため `.firstseen` に依存しない。ただし `mode: tmux` はサポート
対象であり、落として良い穴ではない。

---

### 3-5. assignment の所有権 — 実行アイデンティティでの束縛 (t021 で実装)

案B が `registry/` に retirement marker を置くのとは別に、**`queue/assignments/<agent>`
側にも不変条件が要る**。案B のマーカーは「誰を終了させるか」を表すが、終了させたあとに
誰の assignment を消すかは依然 `plan.sh` の仕事であり、ここが実行で束縛されていないと
後始末が**稼働中の後任を巻き込む**。

**問題。** `<mission>:<task>` は *実行* を指していない。crewvia は Worker 名を
ポジションとして使い回すので、同じ card を同じ名前が pull し直すと後任の assignment は
先任のものとバイト単位で同一になる。加えて `cmd_update()` は status の書き換えを
`with_lock()` の中で、assignment の削除を**ロックの外**で行っていた。ロック解放から
削除までの間に同名 Worker が pending になった card を pull すると、内容一致という
理由だけで**後任の assignment が消える**。assignment を失った Worker は dispatcher から
idle に見えるので kill される。`cmd_done()` / `cmd_fail()` に至っては内容すら見ずに
無条件で削除していた。

**解決 (t021)。**

1. **単一トランザクション** — assignment の公開・撤去はすべて card の書き換えと同じ
   `with_lock()` の中で行う。`pull` の公開も同様。ロックの外に判定と書き込みが
   分かれる隙間そのものを無くした。構造は `tests/test_plan_assignment_transaction.py`
   が AST で固定している (ヘルパー以外がパスを組み立てない / 変更がロック外に出ない)。
2. **実行アイデンティティ** — `queue/assignments/<agent>.identity` に
   `{mission, task, worker, started_at}` を並べて置く。本体の 1 行フォーマットは
   変えないので hooks / dispatcher / verifier-dispatcher の読み手は無改修
   (いずれも名前で引くだけでディレクトリを列挙しない)。世代には `pull` が毎回
   書き換える `started_at` を使い、同一秒内の差し戻し → 再 pull で衝突しないよう
   小数秒まで刻む。
3. **判定の集約** — 「公開中の assignment はこの実行のものか」の判定は
   `classify_assignment()` 1 箇所だけにあり、撤去する唯一の入口 `retire_assignment()`
   は判定が `mine` のときだけ消す。判定不能 (旧形式で世代が読めない・破損) は
   `unverifiable` であって `mine` ではない。

**`plan.sh retire` — 後始末の 1 本の API。**

```
plan.sh retire <task_id> --agent <name> --started-at <generation>
               [--mission <slug>] [--outcome reset|needs-director] [--reason "<1 行>"]
```

「この実行 (mission, task, worker, 世代) を終了扱いにして後始末する」を 1 操作で行う。
呼び出し側が status / worker / 世代を個別に組み立てる形だと、どれを渡すか・省くかの
判断が呼び出し側ごとに分かれ、1 つ緩めた場所から同じ型の事故が再発する。

- `--started-at` は**必須**。省略を許すと名前だけで束縛された後始末に戻る。世代を
  読めなかった呼び出し側は retire を呼ばず Director に上げること。
- 前提 (status が未終了 / worker 一致 / 世代一致 / assignment が自分のものか不在) が
  1 つでも外れたら **1 バイトも書かずに exit 3** (`PRECONDITION_UNMET`)。0 (書いた)
  とも 1 (plan.sh 側の異常) とも区別できるので、呼び出し側は保留に倒せる。
- 前提を弱めて実行する経路は用意しない。

watchdog 側の retirement (§4) が「終了させたあとの queue の後始末」で呼ぶのは
この API である。`plan.sh update --reset` を直接叩く経路は、名前だけで束縛された
後始末になるため使わない。


## 4. 中断耐性の要件

### 4-1. 現状のどこが宙に浮くか

`graceful_terminate()` は「send → 60s 待機 → SIGTERM → 10s 待機 → SIGKILL」の間、
**main loop 全体を最大 70 秒ブロックする**。この 70 秒の状態はプロセスメモリ
(`monitors` 辞書とローカル変数) にしか存在せず、どのファイルにも残らない。

この途中でデーモンが再起動されると:

- **SIGTERM 前で中断** → Worker は「タイムアウトのため中断します」を受け取っただけで
  生きている。再起動後の watchdog はそれを知らず、E2 により max ベースなら更に
  3600s 生き延びる。
- **SIGTERM と SIGKILL の間で中断** → Worker は SIGTERM を受けて死にかけている。
  SIGKILL は永久に来ない。`del monitors` も実行されていない。
- **いずれの場合も** task は `in_progress`、assignment は残存。dispatcher が生きていれば
  D4 が heartbeat 600s 経過後に Director へ通知するが、dispatcher も再起動中なら
  それすら来ない → **幽霊 task**。

さらに `graceful_terminate()` は直列実行なので、N 件の terminate は 70N 秒 main loop を
止める。retirement をこの形のまま足すと待ち行列が伸びるだけ悪化する。

### 4-2. 要件

- **R1 (意図の先行耐久化).** 破壊的ステップに入る**前**に意図をディスクに書く。
  `progress.json` に `phase` / `deadline` / `pane_pid` / `mission` / `task_id` を
  書いてから、初めてメッセージを送る。「書く前に送る」は禁止。
- **R2 (ブロッキング待機の廃止).** 70 秒の `time.sleep` を、cycle 駆動の状態機械に
  置き換える。各 cycle は deadline を過ぎた phase だけを 1 段進めて即座に戻る:
  `requested → notified (deadline = now+60) → sigterm_sent (deadline = now+10) → terminated`。
  **これで中断耐性と「1 cycle の所要時間が有界」が同時に満たされる** — 状態が毎 phase
  ディスクにあるので、どこで落ちても次の起動が続きを引き取れる。
- **R3 (再起動時の回収).** watchdog は起動時に `registry/retirements/*.progress.json` を
  走査し、`phase != terminated` のものを引き継ぐ。判定はこの順に行う:
  1. 窓が既に無ければ `phase=terminated, window_gone=true` に進めて終端処理へ。
  2. 窓が生きていて **R7 の identity 再照合が不一致**なら `phase=discarded` にする
     (同名の別 Worker に着弾させない)。
  3. 一致した場合のみ、当該 phase の deadline を now 起点で張り直す。
  **「窓が生きていれば張り直す」だけでは冪等にならない。** 同じ名前の窓が同じ
  インスタンスである保証は無く、(2) を省くと復帰した watchdog が新しい同名 Worker を
  殺す (§3-3 F2 の発火列そのもの)。冪等なのは「同一インスタンスに対して同じ
  メッセージや SIGTERM が二度届くこと」までである。
- **R4 (終端を dispatcher が読める形で残す).** watchdog は **`queue/` を書かない原則を
  維持する**。終端時は `progress.json` に `phase=terminated` と、殺したときに紐づいていた
  `mission` / `task_id` を残すだけにする。dispatcher は次 cycle でこれを読み、task が
  まだ `in_progress` なら D4 と同一の復旧レシピを Director に通知し、**その後に
  `<agent>.json` と `<agent>.progress.json` を削除する**。これにより E1 の穴が、
  heartbeat の 600s を待たずに塞がる。
- **R5 (既存 backstop を消さない).** R4 の経路が壊れても、既存の D4 は task ファイルと
  heartbeat だけを見ており `progress.json` に依存しないので、600s 遅れで同じ通知を出す。
  **この二重化は意図的に残す。** 「足した機構が偶然の backstop を消す」のは過去に実際に
  起きた失敗パターンである (memory: `session-20260912-verdict-ci-launcher`)。D4 を
  「新経路があるから不要」として削らないこと。
- **R6 (fail closed の維持).** `repo_identity_ok()` の再チェックは各破壊ステップの直前に
  残す。状態機械化すると phase 間に cycle 境界が挟まるため、**チェック回数は減らさず
  増える**方向になる。これは正しい方向。
- **R7 (Worker インスタンスの再照合).** `repo_identity_ok()` が「自分は正しいリポジトリか」
  を確かめるのに対し、R7 は「**相手は marker を書いた時と同じ Worker インスタンスか**」を
  確かめる。**R6 と同じ位置 — 各破壊ステップ (メッセージ送信 / SIGTERM / SIGKILL) の
  直前 — に置く。**
  - 照合対象は marker の `spawn_identity` (§3-3)。現在値は `pane_pid` = `_mux.pid(target)`、
    `created_at` = `_mux_created_at()` → `_spawn_time_fallback()` の順で取る。
  - **記録が非 null の項目が 1 つでも不一致なら不一致**と判定する。
  - 不一致なら破壊ステップを実行せず `phase=discarded` + `discard_reason` を書いて終える。
    dispatcher は `discarded` を読んだら **D4 の復旧レシピを送らずに** marker 2 本を
    削除する (別インスタンスが元気に動いているのだから幽霊 task ではない)。
  - 現在値が両方とも取れない (mux 不達等) 場合も **不一致扱い = 殺さない**。
    fail closed の向きは §5-1 (居座り) であって §5-2 (誤 kill) ではない。
  - **W2 (既存の idle / max terminate) にも同じ再照合を入れる。** W2 は monitor を
    毎 cycle 作り直すので今は問題が表面化しにくいが、`graceful_terminate()` を状態機械に
    するなら phase 間に cycle 境界が入るため、retirement と同じ穴がそのまま開く。

### 4-3. スコープ外として backlog に出す

E2 の根本 (`started_at` が monitor 生成時刻であり、watchdog 再起動で max タイマーが
リセットされる) は、本ミッションの権限移譲とは独立した欠陥である。task frontmatter の
`started_at` を起点にすべきかは別途検討する。retirement 経路は task に紐づかないので
この影響を受けない。

---

## 5. 移行の原子性

**権限の移譲は 1 つの PR で原子的に行う必要がある。** 片側だけ先に入れると、どちらの
順序でも実害が出る。

### 5-1. 先に dispatcher から kill を外した場合 → 居座り

D1 / D2 / D3 が「marker を書くだけ」になるが、watchdog はまだそれを消費しない。
**誰も窓を閉じない。** 用済みの Worker が WIP 枠を占有し続け、WSL の 8GB メモリを
食い続ける (memory: `wsl-memory-editorial-room-contention`)。しかも dispatcher の
dedup (`shutdown_<agent>` / TTL 300s) が効くので、ログ上も 5 分に 1 回しか痕跡が
出ず、気付くのが遅れる。

### 5-2. 先に watchdog に kill を足した場合 → 二重 kill (より危険)

dispatcher と watchdog が同一 Worker に独立して kill を打つ。単に二重なだけではない:

- **crewvia は Worker 名を再利用する** (Haruto / Seo / Arjun ...)。これは
  `tmux_kill_window()` の `.firstseen` に関するコメント (:525-534) が明示している既知の
  前提である。dispatcher が窓を閉じた直後に Director が**同名**で新しい Worker を
  起動すると、遅れて到達した watchdog の SIGTERM / SIGKILL が**後続の無関係な Worker を
  殺す**。
- watchdog の kill はメッセージ送信を伴うので、後続 Worker の入力欄に
  「タイムアウトのため中断します。現在の状況を 1-2 行で記載して終了してください。」が
  刺さる。Worker はそれを指示として解釈しうる。
- **dedup が両者で別管理**である。dispatcher は `/tmp/dispatcher-notify-cache.json`
  (TTL 300s)、watchdog は dedup を持たない。二重期間中は相互の抑止が一切効かない。

この方向の事故は「間違った Worker を殺す」ため、5-1 より回復が難しい。

### 5-3. 結論と運用上の注意

- 判定 → marker 書き込みへの差し替え (dispatcher 側) と、marker 消費 + kill 実行
  (watchdog 側) は**同一 PR**にする。
- ロールバックは部分 revert ではなく、環境変数 1 個 (例:
  `CREWVIA_KILL_AUTHORITY=watchdog|dispatcher`、既定 `watchdog`) で旧経路に一括で
  戻せるようにする。部分 revert は 5-1 / 5-2 のどちらかを必ず再現するため禁止。
- **デプロイも原子的でなければならない。** `dispatcher.sh` は Python を heredoc で
  埋め込んでおり、起動時に一度コンパイルしてプロセスの寿命の間メモリに保持する
  (ファイル冒頭 :22-32)。watchdog も同じく常駐である。**merge しただけでは稼働中の
  タブには反映されない** (`knowledge/dispatcher-restart-after-merge.md`)。片方だけを
  kill + respawn すると、まさに 5-1 または 5-2 の状態が本番で発生する。**両デーモンを
  同時に停止 → 両方 respawn** の手順を PR の説明に明記すること。
- 検証は隔離環境で行う。本番の dispatcher / watchdog / Worker を巻き込まないこと
  (memory: `dispatcher-isolated-qa-harness`)。
- **N3: t002 が merge されてから t005 (相互監視) が入るまでの間、Worker の通常
  shutdown は watchdog の生存に完全に依存する。** 現状は D1/D2/D3 が dispatcher 側の
  backstop になっているが、t002 はそれを watchdog へ一本化するため、watchdog が黙って
  止まれば用済み Worker が誰にも閉じられなくなる (= §5-1 の常態化)。watchdog が実際に
  一度も起動していなかった前例がある (`start.sh:778-779` の "F6是正: tmuxモードでは
  従来watchdogが一度も起動していなかった")。`CREWVIA_KILL_AUTHORITY=dispatcher` への
  一括切替が緩和策になるので blocking にはしないが、**t002 の実装者は t005 までの期間を
  「watchdog 単一障害点」として認識し、PR 説明に明記すること**。

---

## 6. 後続タスク向けチェックリスト

- [ ] `registry/retirements/` を `.gitignore` に追加する (`registry/mux/` と同じ扱い)
- [ ] `process_retirements()` は `monitors` に触れない。`load_active_tasks()` の戻り値を
      変更しない
- [ ] `graceful_terminate()` を `(agent_name, window_target, repo_root)` 版に切り出す。
      monitor の捏造をしない
- [ ] 70 秒ブロッキングを phase + deadline の状態機械に置換する (R2)
- [ ] 破壊ステップ直前の `repo_identity_ok()` 再チェックを全 phase で維持する (R6)
- [ ] D4 (vanished 検知) を削除しない (R5)
- [ ] **F1**: `.firstseen` 掃除を dispatcher 側の sweep として実装する (窓の不在が
      トリガ、kill の成否ではない)。`tmux_kill_window()` 内の unlink は
      `CREWVIA_KILL_AUTHORITY=dispatcher` 経路のために**残す**
- [ ] **F2/R7**: marker に `spawn_identity` を持たせ、各破壊ステップ直前 (R6 と同位置) と
      R3 の回収時に再照合する。不一致 / 取得不能はどちらも「殺さない」に倒す。W2 にも同じ
      再照合を入れる
- [ ] **N4**: `tmux_kill_window()` の docstring :543-544 から "vanished-worker cleanup" を
      外す (D4 は kill しない)
- [ ] `CREWVIA_KILL_AUTHORITY` による一括ロールバック経路
- [ ] PR 説明に「両デーモンの同時 respawn 手順」と、t005 までの watchdog 単一障害点
      (§5-3 N3) を書く

## 6-2. 実装時に設計から変えた点 (t002, 2026-09-21)

実装は `scripts/lib_retirement.py` + `scripts/watchdog.py` + `scripts/dispatcher.sh`。
本文の決定に従ったが、3 点だけ設計と違う形になった。理由込みで残す。

### (1) R4 — 後始末の担い手を watchdog にした

本文 §4-2 R4 は「watchdog は `queue/` を書かない。`phase=terminated` を残し、
dispatcher が読んで Director に復旧レシピを送り、marker を消す」だった。
**タスク指示 (実装項目 3) とプランレビューがこれを上書きした** —
「後始末を watchdog 自身が完結させる。Director への通知は結果の事後報告にする」。

実装はプランレビューの指定どおり `plan.sh update <task> --status pending --reset
--mission <slug>` を subprocess で呼ぶ。frontmatter を自前で書かないので
R4 の本来の趣旨 (`queue/` の所有権を侵さない) は保たれている — 書き手は
依然として plan.sh 1 つで、ロックも assignment のインスタンス照合も
plan.sh の既存実装がそのまま効く。

結果として **request 以降のライフサイクルは全部 watchdog が持つ**
(marker 2 本の削除も含む)。dispatcher が落ちていても後始末が完結するので、
E1 の穴が「dispatcher の生存」に依存しなくなった。D4 は R5 どおり残してある。

### (2) 後始末の順序 — kill が先、reset が後

プランレビューの「plan.sh の呼び出しに失敗した場合は kill せず通知に倒す」を
**2 つに分けて**実装した。素直に「reset してから kill」にすると、kill が失敗
したときに *pending に戻った task を持つ生きた Worker* ができ、dispatcher が
同じ task を別の Worker に割り当てる (二重作業 + worktree 競合)。

- **開始前のプリフライト**: task を持つ retirement は、`plan.sh` が使えなければ
  **そもそも終了処理を始めない** (`_start()` の `REFUSING to start`)。
  「殺してから plan.sh が無いと気付く」を封じる。これが「kill せず」の実装。
- **実行時の失敗**: kill 後に plan.sh が落ちた場合は `phase=cleanup_failed` を
  残し、毎 cycle 再試行しつつ Director に 1 度だけ手動レシピを送る。
  marker を消さないので状態は残り、D4 も 600s 後に独立して同じ task を拾う。

### (3) 「窓が消えた」の判定に pane_pid を使う (実装中に見つけた欠陥)

本文は各ステップで「窓が生きているか」を `_mux.list()` で見る前提だった。
実装してテストしたところ、**SIGTERM 直後に Worker が終了した場合**に
以下が起きた:

1. cycle 冒頭の窓スナップショットにはまだ窓が居る
2. 直後の R7 再照合で `_mux.pid()` が None を返す
3. `identity_matches()` は「記録した pane_pid があるのに現在読めない」を
   **不一致**と判定する (これ自体は正しい — mux 不達で kill を許さないため)
4. → `phase=discarded` になり、**後始末を飛ばして marker が消える**

つまり R7 のガードが、まさに直そうとしている幽霊 task を再生産していた。
修正は `process_alive()` — 記録済み pane_pid が生きているかを `/proc` で
直接見る。mux を介さないので「インスタンスが終わった」と「backend が
答えられない」を混同しない。各ステップはこの判定を R7 より**先に**行う。

この欠陥は `tests/test_retirement.py::test_cleanup_failure_keeps_marker_and_notifies_once`
が最初に捕まえた (marker が消えていた)。

### (4) cycle 全体を `mux.available()` でゲートした

`_mux.list()` が一時的に空を返すと全 marker が「窓消滅 → terminated →
後始末」に進み、**生きている Worker の task を pending に戻す**。
`_is_mass_kill()` と同じ考え方で、backend が答えられない cycle は
1 件も処理しない (`test_mux_unavailable_processes_nothing`)。

> **このゲートだけでは閉じなかった (t019 で判明)。** `available()` は tmux
> では `shutil.which("tmux")` しか見ないので、サーバーが死んでいても True。
> herdr では ping するが `pane_list` とは別呼び出しで、ping が通ったまま
> `pane_list` だけが 10s タイムアウトし得る。詳細は §6-3。

---

## 6-3. QA FAIL-1 / Codex P1 の修正 (t019, 2026-09-21)

t003 (QA) の FAIL-1 と t012 (Codex) の P1 3 件は、**別々の症状だが根は 1 つ**
だった: *Worker がまだ生きている / task がもう別の状態になっているのに、
後始末が無条件に走る*。後始末 (`plan.sh update --status pending --reset`) は
「task を他人に配り直してよい」という宣言なので、根拠が揃う前に到達しては
いけない。3 件はその根拠が抜けていた 3 箇所である。

### (1) 空の window list を死亡証明に使わない

`_start()` だけが `mux.list()` の結果 (`_window_alive()`) で終端へ飛び、
後続ステップが使っている `pane_pid` の `/proc` 確認 ((3) の解法) を通って
いなかった。`list()` は**両 backend とも失敗・タイムアウトを黙って `[]` に
潰す**ので、WSL のメモリ逼迫で 1 回詰まれば本番でも成立する。QA の実機ログ
では同一 run の 3 秒差で、`_is_mass_kill()` が同じ `mux.list()=[]` を理由に
cleanup を拒否している横で、新経路がその `[]` を鵜呑みにしていた。

判定を `_exit_evidence()` に一本化した。証拠の強い順に:

1. 記録済み `pane_pid` が `/proc` に居ない → **消滅確定**
2. `pane_pid` が生きている → **生存確定** (backend の答えは要らない)
3. `pane_pid` が未記録のときだけ window list に投票権がある。ただし
   *裏の取れた* list に限る (`_listing_authority_for_cycle()`):
   - 空でない list → 問い合わせが成功した証拠
   - 空 + backend 自体が落ちている (`server_running()` が False) → 全滅の証拠
     — **t020 で撤回。§6-4 (2) を参照**
   - 空 + backend は生きている → **outage と全滅が区別できない。保留**

「空 + backend 生存」を無条件に保留に倒すと、最後の Worker が死んだ瞬間に
marker が永久に残り、このモジュールが潰そうとしている幽霊 task が再発する。
t019 はその出口を `server_running()` の例外に求めたが、**probe は死亡証明に
ならない** (§6-4 (2))。懸念自体は正しかったので、t020 で出口だけを
「自動 cleanup」から「Director への 1 度きりの報告」に差し替えた。保留中も
dispatcher の D4 検知 (R5) は同じ task を独立に見つけるので、沈黙にはならない。

### (2) 後始末を「元の割り当てがまだ有効か」に条件付けた

`_settle_terminated()` は無条件に `--reset` を撃っていた。猶予期間 + 後始末
リトライの間に task は動く — Worker が最後の `plan.sh done` を済ませて
抜けた、人間が reset して後任が pull した。どちらでも「完了済みの成果を
pending に戻す」「後任の assignment を消す」が起きる。plan.sh 既存の
assignment 内容一致チェックは *別 task の* assignment しか守らず、
*同じ task の別の実行* は素通りする。

`plan.sh update` に前提条件フラグ (`--expect-status` / `--expect-worker`) を
足した。判定は **queue ロックの内側**、最初の書き換えの直前で行う (読んで
決めて書く、の間に他の writer が入れない)。前提が外れたら 1 バイトも書かずに
**exit 3** — 実エラーの exit 1 と区別できるので、呼び出し側は「世の中が
変わった」と「plan.sh が壊れた」を取り違えない。

`expect-status` を `in_progress` だけに絞ったのは意図的:
`needs_director` などは Director が握っている状態で、retirement が横から
戻してよい対象ではない。外れた場合は Director に「queue は変更していない」
と報告して settle する。

> **t024 で撤去。** このフラグ群は `plan.sh retire` (§3-5) に吸収した。
> 前提を「どれを渡すか」という呼び出し側の選択として残したことが、次の 2 巡で
> P1 を生む形そのものになったため (§6-5)。`in_progress` 限定も retire の
> 内側に移してある。

### (3) SIGKILL の「送信成功」を「死亡」と読まない

`_step_sigterm()` は `kill_process()` の戻り値を捨てて即 `PHASE_TERMINATED`
を永続化していた。`os.kill` が失敗しても次 cycle で task が pending に戻り
marker も消えるが、Worker は生きたまま。送信が成功しても即死は意味しない。

`_await_exit()` で死を確認してから終端に進む。確認は
`KILL_CONFIRM_WINDOW` (1.0s) で頭打ちにしてあり、R2 (cycle は詰まらない)
を壊さない — 確認できなければ `sigterm_sent` のまま次 cycle で撃ち直す。
`SIGKILL_REPORT_AFTER` (3 回) を超えても死なない場合は Director に 1 度
だけ報告する。**黙って再試行し続けると「殺さない側に倒した」が「誰も
気付かない居座り」になる** ので、保留は必ず声に出す。

### 回帰テストの形

3 件とも「ガードが誤発火したとき何が失われるか」を assert する
(`tests/test_retirement.py` の t019 節。plan.sh 側は t024 で
`tests/plan-assignment-identity.bats` の retire 節に統合した)。
phase 名やフラグではなく、生きた Worker の task が `in_progress` のまま
であること、完了済みの成果が pending に巻き戻らないこと、後任の
assignment が残っていることを見る。逆向き (保留に倒しすぎて永久に
終わらない) も `test_red_sigkill_waits_for_the_process_to_actually_exit` の
後半と、t020 で入れ替えた
`test_unresolvable_retirement_is_escalated_to_the_director_once` で押さえた。

---

## 6-4. Codex P1 3 件 — identity の束縛と証拠の強度 (t020, 2026-09-22)

t012 (Codex) の 2 巡目。§6-3 が「根拠が揃う前に後始末に到達しない」を入れた
のに対し、こちらは**その根拠の貼り方**が甘かった 3 箇所。3 件とも
「名前は同じでも中身が別物になりうる」「観測できないことは証拠にならない」
という 1 つの弱点の別の顔である。

### (1) 後始末を assignment インスタンスに束縛する (P1-1)

§6-3 (2) の `--expect-status in_progress` + `--expect-worker <agent>` は、
**同名の後任が同じ task を実行している場合にも一致する**。crewvia は Worker
名を意図的に使い回すので、人間が `--reset` して同じ名前の Worker が pull し
直すと、status も worker も元と寸分違わない状態に戻る。cleanup が遅れていれば
後任の task が pending に戻り assignment も消える — §6-3 (2) が塞いだつもりの
穴が、`worker` 欄では見えない形で残っていた。

区別が付くのは `started_at` だけ。`plan.sh pull` が実行のたびに書き換える
ので、これが **割り当ての世代** になる。t020 ではこれを
`--expect-started-at` として足し、t024 で `plan.sh retire --started-at`
(§3-5) に吸収した。

`started_at` は retirement request を書く時点で `read_task_started_at()` が
task カードから読み、request → progress と引き継ぐ。**呼び出し側に渡させない**
のは、忘れた 1 箇所が黙って名前ベースの判定に落ちるため (memory:
`approve-judgment-needs-allowlist-and-scope` — 判定 unit は 1 つに絞る)。
カードが読めなかった場合はキーごと省略し、`null` を捏造しない: 捏造した
`null` は「まだ実行が始まっていない task」に一致してしまう。

> **t020 の積み残し。** 省略した場合に「status と worker だけで reset する」
> フォールバックを残したのが、3 巡目の P1-2 になった (§6-5 (2))。

### (2) probe の失敗を死亡の証拠にしない (P1-2)

§6-3 (1) の 3 番目の箇条書き — `server_running() == False` を全滅の証拠と
読む — を撤回した。`TmuxBackend.server_running()` は**タイムアウトでも例外でも
False を返す** 5 秒の subprocess であり、HerdrBackend のそれは socket への
ping でプロセスの終了証明ではない。つまり list を空にするほどの outage は
probe も同時に黙らせる。**互いに裏を取り合っているつもりの 2 つの失敗**が
「生きた Worker の task を pending に戻してよい」という結論を出す構図で、
list ゲートが塞いだ欠陥が 1 段下で再発していた (memory:
`fail-closed-guard-can-recreate-the-defect`)。

`_listing_authority_for_cycle()` は `True` か `None` (unknown) しか返さない。
cleanup には積極的な終了の証拠 (`/proc` で消えた pane_pid、または**何かを
返した** list からの欠落) を要求する。

**保留に出口を付ける。** t019 の懸念 (曖昧なら保留を無条件にすると最後の
Worker が死んだ瞬間に marker が永久に残る) は正当なので捨てない。ただし出口は
自動 cleanup ではなく `_check_stall()`: `STALL_REPORT_AFTER` (1800s) 動けない
marker を Director に **1 度だけ** 上げ、queue には触れない。受領証は
`registry/retirements/<agent>.stalled` に残すので、watchdog が再起動しても
2 通目は出ない。人間が marker を手で片付けたら次の cycle が受領証も掃除する。

> 自動で倒す先は常に「殺さない・書き換えない」側。曖昧さを queue の書き換えで
> 解決しようとしたのが、このモジュールで 3 回続けて欠陥になった形である。

### (3) identity check を通した PID にシグナルを送る (P1-3)

`_step_notified()` は `_guard()` で現在の PID を検証したあと、**別途**
`mux.pid(target)` を呼び直してその結果に SIGTERM を送っていた (`_start()` も
同型で `current_spawn_identity()` を 2 度呼んでいた)。元の Worker が抜けて
同名の後任がこの 2 呼び出しの間に現れると、**request と一度も照合されていない
PID** が marker に永続化され、そのまま殺される。identity check は、その結果を
使う対象が同じインスタンスでなければ意味がない。

`_guard()` は判定に使った snapshot ごと返し、各ステップは
`_verified_pid()` — request に記録された PID、無ければ検証済み snapshot の
PID — にだけシグナルを送る。**窓名の再解決は禁止**。

### 回帰テストの形

P1-1 と P1-3 は TOCTOU なので、テストは「検査と使用の間に後任が現れる」状況を
実際に作る (`SuccessorRaceMux` は `pid()` の 2 回目で後任に入れ替わる)。
assert するのは「後任のプロセスが生きていること」「後任が作業中の task が
`in_progress` のままであること」— 誤発火したときに実際に失われるもの。

---

## 6-5. 3 巡分を 1 本の API に畳む (t024, 2026-09-22)

t012 (Codex) の 3 巡目。残った P1 は 2 件で、3 件目の
「assignment の削除がロックの外」は t021 (§3-5) が先に潰していた。
この節は**その 2 件の修正**と、**なぜ 9 件が同じ形だったのか**の記録である。

### 9 件が同じ 1 つの形だった

| 巡 | 指摘 | 弱めていた前提 | 閉じ方 |
|---|---|---|---|
| 1 (t019) | 空の window list を死亡証明にした | 「観測できない」=「死んだ」 | 積極的な終了の証拠を要求 (§6-3 (1)) |
| 1 | `--reset` が無条件 | 前提を 1 つも置かなかった | ロック内の前提チェック (§6-3 (2)) |
| 1 | SIGKILL の送信成功を死亡と読んだ | 「送った」=「死んだ」 | `_await_exit()` で確認 (§6-3 (3)) |
| 2 (t020) | 後始末が名前にしか束縛されていない | 「同じ名前」=「同じ実行」 | 世代 (`started_at`) で束縛 (§6-4 (1)) |
| 2 | probe の失敗を死亡証明にした | 「backend が黙った」=「死んだ」 | authority は `True` / `None` だけ (§6-4 (2)) |
| 2 | 検証した PID と殺す PID が別 | 「同じ窓名」=「同じインスタンス」 | `_verified_pid()`、窓名の再解決を禁止 (§6-4 (3)) |
| 3 (t021) | assignment の削除がロックの外 | 判定と書き込みが割れてよい | 単一トランザクション (§3-5) |
| 3 (t024) | PID 不在を死亡の証拠にした | 「PID が無い」=「死んだ」 | 記録済み PID を要求、無ければ保留 (下 (1)) |
| 3 (t024) | 世代不明なら前提を弱めて実行 | 証拠が無いときに実行してよい | 自動 cleanup を見送り escalate (下 (2)) |

同じ文が 9 回書ける: **証拠が足りない状態を、足りているほうに読み替えて
破壊的な一手に進んでいた。** 個別にガードを足し続けたから 9 回出た —
ガードを足せる場所がそのたびに 1 つ増え、呼び出し側は「どれを渡すか」を
選べてしまう。選べる以上、どこかは緩む。

### (1) 記録済み PID の不在は死亡の証拠ではない (P1-1)

`_orphaned()` は `process_alive(prog["pane_pid"])` だけを見ていた。
`build_progress()` は `pane_pid` を既定値 `None` で必ず埋めるので、
**PID を答えられない backend** で始まった retirement — herdr の
`pane_process_info` が失敗し identity が `created_at` だけになった状態、
§6-4 (2) で保留が増えたぶん現実に起きうる — は `process_alive(None) ==
False` により即 `terminated` に飛ぶ。猶予期間中に request が消えるだけで、
Worker が生きていても次 cycle が task を pending に戻す。

`_orphaned()` の分岐を 3 つにした。記録済み PID が**死んでいる**なら後始末を
完了し、**生きている**なら marker を discard し、**そもそも記録が無い**なら
何も書かずに保留する。3 つ目を discard ではなく保留にしたのは、discard が
「この task はまだ修復が要るかもしれない」という唯一の記録を捨てるため。
出口は `_check_stall()` (§6-4 (2) と同じ形)。

同型の穴が 1 つ隣にあったので同時に塞いだ: `process_alive()` は**解釈できない
値にも False を返す**。「そのプロセスは動いているか」への答えとしては正しいが、
「我々の Worker は終了したか」への答えとしては PID 不在と同じ間違いになる
(marker は JSON なので途中で切れた書き込みや手編集で壊れうる)。破壊的な一手の
直前に PID を問う側は `recorded_pid()` を通し、「読めない」を `None` で受け
取って保留に倒す — `_orphaned()` と `_exit_evidence()` の両方。

### (2) 世代が不明なら、前提を弱めずに Director へ上げる (P1-2)

§6-4 (1) はカードを読めなかった場合に `--expect-started-at` を省き、status と
worker だけで reset するフォールバックを残していた。その 2 つは人間が差し戻して
同名 Worker が pull し直すと元の値に完全に戻る — つまり**世代を読めなかった
ときだけ、世代チェックが防ぐはずだった後任レースが復活する**。証拠が無いときに
だけガードが外れるのだから、外れる条件は「守りたい状況」と一致している。

`_bound_generation()` が `None` を返したら `_cleanup_deferred()` に倒す。
自動の後始末は**一切しない**。Director に 1 度だけ — 何を確認し、どう修復し、
marker をどう片付けるかを添えて — 報告し、marker は残す (この task が幽霊で
ある可能性の唯一の記録なので)。後から card を読み直して埋めることはできない:
そのとき読めるのは後任の世代だからである。

### (3) 後始末を 1 呼び出しに集約し、サイトのガードを消した

`_settle_terminated()` は `plan.sh retire` (§3-5) を 1 回呼ぶだけになった。
渡すのは証拠 (mission / task / agent / 世代) で、判定は API の内側・単一
ロックの中にある。これに伴い **`plan.sh update` の `--expect-*` 3 つは撤去**
した (`tests/plan-update-expect.bats` も削除)。二重に残すとどちらが効いて
いるか分からなくなり、片方だけ緩んだときに気付けない。

吸収の際、`--expect-status in_progress` が持っていた意味は
`RETIRE_RETIRABLE_STATUS` として retire の内側に移した。「終了している状態を
列挙して弾く」ではなく「`in_progress` だけ通す」にしてある: 列挙は必ず漏れ、
`needs_director` / `ready_for_verification` はどれも worker と `started_at` を
残したままなので、漏れた瞬間に世代まで一致する reset が成立して、Worker 自身が
書いた結末が消える。

`update --reset` はガード無しのまま残した。**人間が card を見て打つコマンド**
だからで、デーモンからは呼ばない。

### 回帰テストの形

- `test_red_orphan_recovery_without_a_recorded_pid_does_not_assume_a_death`
  — PID を答えない backend で始めた retirement の request を消し、Worker が
  生きたまま・task が `in_progress` のままであることを見る。保留の出口
  (`STALL_REPORT_AFTER` 後に 1 通) まで同じテストで押さえる。
- `test_red_cleanup_without_a_recorded_generation_is_escalated_not_guessed`
  — request の瞬間だけ card を読めなくし、後から同名の後任を立てる。
  後任の `started_at` が残ること、報告が 1 通で止まることを見る。
- `test_unparseable_recorded_pid_is_not_evidence_of_a_death`
  — marker の `pane_pid` を壊し、生きた Worker の task が巻き戻らないことを見る。
- `test_cleanup_is_bound_to_the_execution_by_a_single_plan_sh_call`
  — 逆向きの担保。後始末が `retire` 1 呼び出しで、渡すのが証拠だけであることを
  引数で固定する (`--expect` で始まる引数が 1 つも無いこと)。
- `tests/plan-assignment-identity.bats` の
  "retire writes nothing once the execution recorded its own outcome"
  — `needs-director` を通した card が retire で巻き戻らないこと。

RED であることは、同じテストファイルを修正前の `scripts/` に当てて確認した
(4 本とも fail → 修正後 41 passed)。`scripts/test_retirement_authority.sh`
(実 tmux + 実 watchdog) の fixture も本番に合わせた: `plan.sh pull` は
assignment 本体と `.identity` サイドカー、card の `started_at` を必ず書くので、
本体だけ置いた fixture は本番より弱い状態を試していた。

## 6-6. 猶予期間のあいだに世界が動く (t025, 2026-09-22)

t012 (Codex) の 4 巡目。**これまでの 9 件とは型が違う。** 1〜3 巡目は
「誰を終わらせるのか」の束縛 (identity) と「何を証拠とするか」の強度で、
どちらも t021 / t024 で構造的に閉じた。4 巡目に出た 3 件はそのどちらも通る。
壊れるのは、**退役が一瞬の出来事ではなくなった**からである — t002 が判定と
着弾のあいだに猶予期間を入れ、その 60 秒のあいだに queue も他のデーモンも
動き続ける。

| 指摘 | 前提が崩れる経路 | 閉じ方 |
|---|---|---|
| P1-1 | 猶予期間中に Worker が次の task を pull する | 割り当てを止める (dispatcher) + assignment の世代で再照合 (下 (1)) |
| P1-2 | 2 つのデーモンが同時に request を書く | create-if-absent の原子操作 + `request_id` 束縛 (下 (2)) |
| P2  | plan.sh のブロッキング flock を cycle 内で待つ | `--no-wait` で諦めて次 cycle (下 (3)) |

### (1) Worker はプロセスではなく「その実行」である (P1-1)

`_guard()` は task_id を持つ request について assignment チェックを免除し、
pane identity だけを見ていた。timeout retirement は「assignment を持っている
Worker」が対象なので、存在することを理由に免除するのは正しい。**どの実行の
assignment かを問わなかったのが誤り**である。

猶予期間中に Worker が生き返って `plan.sh done` を通し、次の task を pull
すると:

- pane の PID も created_at も**変わらない**ので R7 は素通りする
- watchdog は**新しい task を実行中の Worker に SIGTERM を撃つ**
- 後始末は元の task しか見ないので、**新しい task は in_progress のまま
  宙に浮く** — 誰も証拠を持っていない幽霊 task が 1 枚増える

閉じ方は 2 段。**本命は dispatcher 側**で、`has_marker()` が立っている Worker
を割り当て対象から外す (`dispatch()` のループ冒頭)。t002 以前は判定と kill が
1 秒差だったので割り込む窓が無かった — 窓を作ったのは t002 自身である。

**backstop は watchdog 側**の `assignment_execution_verdict()`。
`queue/assignments/<agent>` の本体 (`<mission>:<task>`) と `.identity`
サイドカーの `started_at` を、request に記録した実行と突き合わせる。
`plan.sh pull` が queue ロックの中で card と一緒に公開するものなので、
「いまこの Worker が握っている実行」の唯一の一次情報である。

一致以外はすべて discard に倒す (絶対 / 別の task / 別の世代 / 読めない)。
これは保留ではなく**決着**であることが重要で、保留にすると `has_marker()` が
立ったままになり、誰も再要求できない Worker が残る。discard なら marker が
消え、本当に止まっているなら次の cycle で card から timeout が再判定される。

> **t026 で訂正。** 「読めない」をここに混ぜたのが 5 巡目 P1-1 だった。
> 読めないケースは discard でも決着でもなく、次の cycle が同じ結論に辿り着く
> だけの**無限ループ**になる。§6-7 (1) を参照 — 読めないときだけ hold + 報告
> に分けた。

**既に別 task を pull されていた場合、退役の側を取り消す**ことにした。新しい
task を pending に戻す選択肢もあるが、それは「生きている Worker が作業中の
card を、曖昧さを理由に巻き戻す」ことであり、このモジュールが 9 件かけて
やめたことそのものである。加えて退役要求の前提 (この Worker はこの task で
止まっている) は、Worker が自分で `done` を書いた時点で**反証されている** —
取り消すのに推測は要らない。

### (2) request の生成そのものが競合である (P1-2)

`has_marker()` の確認と `request()` の書き込みが分かれており、`request()` は
無条件に `os.replace` していた。dispatcher と watchdog は**どちらも**書き手
なので、両方が「marker 無し」を観測してから両方が書ける。後から書いた方が
先の request を黙って差し替える。

watchdog が最初の request に対して progress を作った後でも起きる。progress は
最初の request の PID と task 証拠を持つのに、後続ステップは差し替わった
request を読み、`_settle_terminated()` は差し替え側の mission/task/世代を
優先する。結果は「timeout の後始末義務が捨てられる」か、より悪く
**「別の退役の証拠が、いま動いている実行に適用される」**。

- 生成は `write_json_exclusive()` — temp に完成品を書いてから `os.link` で
  publish する。EEXIST なら負け。`O_EXCL` + 後から書き込みにしなかったのは、
  負けた側が**空または書きかけの request を観測しうる**ため:
  `read_json()` は壊れた文書に `None` を返し、`_advance()` はその `None` を
  「request が無い」= 別の分岐として読む。リトライではない。
- request には `request_id` (uuid) を持たせ、progress が複写する。両者の id が
  食い違う組は**別の退役**であり、証拠を混ぜない (`_read_pair()`)。混ぜない
  とは、progress を自分の証拠だけで完走させ (`_orphaned()` と同じ経路)、
  request の方は**消さずに**置いておく — それは誰かの生きた依頼なので、
  progress が片付いてから `_start()` で自分の番を取る。

### (3) cycle の中で queue ロックを待たない (P2)

`_settle_terminated()` は `plan.sh retire` を同期で呼ぶ。plan.sh の
`with_lock()` は**ブロッキングの排他 flock** なので、queue が混んでいると
marker 1 件につき subprocess timeout (旧 120 秒) まで**全 Worker の監視と
退役処理が止まる**。リトライのたびに繰り返される。R2 が消したはずの 70 秒
ストールより悪い。

`plan.sh retire --no-wait` を足し、`with_lock(nonblocking=True)` が
`LOCK_NB` で取れなければ **1 バイトも書かずに exit 4 (`LOCK_BUSY`)** で返る。
呼び出し側はこれを「次の cycle で聞き直す」と読む。

終了コードを 3 (`PRECONDITION_UNMET`) と分けたのが肝で、3 は「もう何も owed
でない」を意味し marker を settle させる。ロック待ちを 3 と混ぜると、混んで
いるだけの queue に対して**後始末の義務が捨てられる**。1 (plan.sh の異常) と
も分ける — 1 は `_cleanup_failed()` に落ちて attempt を消費し Director を
起こすので、じきに解放されるロックに対してそれをやるのは過剰である。

保留の出口は他と同じ `_check_stall()`: 1800 秒動かなければ Director に 1 通。

subprocess timeout 自体も 120 → 30 秒 (`CLEANUP_COMMAND_TIMEOUT`) に下げた。
ロック待ちが無くなった以上、これは「インタプリタが固まった」等の残りの
病態に対するベルトであり、120 秒は cycle の有界性としては大きすぎる。

### 回帰テストの形

並行性の 2 件は**実際に競わせる**。再現を仕込みで代用すると、直したのが
競合なのか仕込みなのか区別できない。

- `test_red_worker_that_pulled_another_task_during_the_grace_period_is_spared`
  — 猶予期間中に**本物の plan.sh で** `done` → `pull` を通し、Worker が生きて
  いること・新しい task が in_progress のままであること・その assignment が
  残っていることを見る。
- `test_red_worker_that_finished_its_task_during_the_grace_period_is_spared` /
  `test_red_same_card_pulled_again_by_a_successor_is_a_different_execution`
  — assignment が消えた場合と、同じ card の別世代を後任が握った場合。
- `test_red_concurrent_requests_do_not_overwrite_each_other`
  — 8 プロセスから同時に `request()` を叩き、「書いた」と答えるのが 1 本だけで、
  ディスクに残るのがその 1 本であることを見る。
- `test_red_progress_is_bound_to_the_request_that_created_it`
  — progress を作った後に request を別物 (別 task・別世代) へ差し替え、
  **その別の実行が巻き戻らない**ことを見る。
- `test_red_plan_sh_retire_can_refuse_to_wait_for_the_queue_lock` /
  `test_red_cleanup_does_not_block_the_cycle_on_the_queue_lock`
  — 別プロセスでロックを握ったまま、plan.sh が即 exit 4 で返ること、
  1 cycle が有界であること、ロックが空けば後始末が再開することを見る。
- `test_timeout_retirement_on_the_same_execution_still_terminates`
  — 逆向きの担保。上の 3 本は全部「前提が外れたら殺さない」なので、これが
  無いと「何も殺さない」に倒しただけで全部緑になる。
- `tests/test_dispatcher_retirement_exclusion.py`
  — dispatcher 側。`dispatcher.sh` の heredoc に埋め込まれた**本物の python を
  exec() して `dispatch()` を回す** (`tests/test_orphan_daemon_guard.py` と
  同じ方式)。ロジックを複製したテストは dispatcher.sh を直したことを一切
  証明しないため使わない。marker 有りで割り当てないこと、marker が片付けば
  復帰すること、marker 無しなら従来どおり割り当てることを対で押さえる。

RED であることは 7 本 + dispatcher 3 本を修正前の `scripts/` に対して流して
確認した (全 fail → 修正後 53 passed)。

## 6-7. 証拠が無いときの倒し方と、予約の置き場所 (t026, 2026-09-22)

t012 (Codex) の 5 巡目、P1 2 件。どちらも §6-6 で入れた対策の**当てが甘かった
場所**であり、新しい型ではない。

| 指摘 | 何が甘かったか | 閉じ方 |
|---|---|---|
| P1-1 | 「世代を読めない」を EXEC_SAME (続行可) に倒していた | 積極的に一致したときだけ続行。読めないときは hold + 報告 (下 (1)) |
| P1-2 | 退役予約が dispatcher の割り当て経路にしか無い | `plan.sh pull` のロックの中で予約を効かせる (下 (2)) |

### (1) 「比べる相手が無い」は「一致した」ではない

`assignment_execution_verdict()` は、記録側に世代が無い場合も `.identity`
サイドカーを読めない場合も `EXEC_SAME` を返していた。理由づけは「assignment
の本文が同じ card を指しているのだから」だったが、**その本文こそ、同じ card
を取り直した後任のものとバイト単位で同一になる**。crewvia は Worker 名を
ポジションとして使い回すので pane の pid も created_at も変わらない。つまり
この fallback は、猶予期間中に reset → 再 pull が起きたときに**新しい実行への
SIGTERM/SIGKILL を許可する**。しかも後始末は世代が違うことを理由にその実行の
reset を拒むので、task は誰の管轄でもないまま in_progress で残る — §6-6 (1)
が消したはずの幽霊 task が、証拠が無いときだけ復活していた。

積極的に一致したときだけ `EXEC_SAME` を返すように直した。**倒す先は discard
ではなく hold** で、ここが §6-6 (1) の書き方との違いである:

- assignment が「Worker はもう別のことをしている」と**言っている** (別の
  task / 別の世代 / 不在) → discard。前提が反証されたので決着してよく、
  本当に止まっているなら次の cycle で card から再判定される。
- 世代を**読めない** → hold (`PHASE_UNPROVABLE`)。discard にすると、次の
  cycle も card は同じことを言うので再要求され、また読めずに discard される。
  Worker は殺されも解放もされないまま、誰も気付かない無限ループになる。

marker を残すこと自体が保護になっている: `has_marker()` が立っている間
watchdog は timeout を再発火せず、dispatcher は割り当てず、下 (2) により本人も
pull できない。つまり kill ではなく**隔離**である。報告に `--reset` の手順は
**入れない** — 何も殺していない (あるいは SIGTERM までしか撃っていない) ので、
それに従うと作業中の card を巻き戻させてしまう。

hold の出口は 2 つある。**人間**が `registry/retirements/<agent>.*` を消すか、
**assignment が言い切る**か。後者が `_recheck_unprovable()` で、assignment が
不在になる / 別の task を指すようになったら — つまり「この Worker はもう別の
ことをしている」が世代抜きで確定したら — discard に落として marker を消す。
自力で完了しただけの健全な Worker を人間待ちで止めないための出口であり、
「読めるようになって、しかも一致した」場合は**解かない** (止めたと報告済みの
退役を黙って再開することになる)。

### (2) 予約は assignment を公開する場所に置く

§6-6 (1) の本命は dispatcher 側の除外だったが、dispatcher にできるのは
**割り当てメッセージを送らないこと**だけである。Worker 自身が `plan.sh pull`
を叩く経路も、退役が決まる前に既に届いていた指示も止まらない。実際
`_guard()` は assignment を確認したあとに `current_spawn_identity()` を呼んで
おり、これは mux の subprocess でブロックしうる。その隙に Worker が done →
pull を通せば、pane の pid は変わらないので検証を通過し、**新しい task の実行
中にシグナルが飛ぶ**。

assignment を公開するのは `cmd_pull()` の 1 箇所だけで、しかも card の書き換え
と同じキューロックの中である (t021)。予約を効かせられる直列化点はそこしか
ない。`retirement_reservation()` が marker の**存在だけ**を stat で見て、
立っていれば 1 バイトも書かずに `exit 2` (`retirement_reserved`) で返る。

- 中身は読まない。全 Worker が叩く pull のロックの中なので、パースや列挙で
  ロック保持時間を伸ばしてはいけない。
- `exit 2` = 「タスクなし」に載せたのは、退役中の Worker に対しては**それが
  本当のこと**だからである。Worker は既定の idle 動作 (待って再試行 → やがて
  shutdown) に素直に落ちる。理由は stderr の `retirement_reserved` で残る。
- registry の場所は `CREWVIA_REPO_ROOT` を優先する。worktree 側の plan.sh を
  叩かれると `REPO_ROOT` は worktree を指し、本体の marker を見逃す
  (memory: crewvia-worktree-repo-root-pitfall)。

そのうえで `_guard()` の順序を入れ替え、**ブロックしうる mux 呼び出しを先頭に、
assignment の確認を最後に**した。確認から着弾までの間に残るのはファイル読み
数回だけになる。予約と順序は**どちらか一方では閉じない**: 予約は「新しい task
を握らせない」ことを保証し、順序は予約が届かない経路 (既に届いていた指示、手で
置かれた assignment、別 checkout の古い plan.sh) に対して窓を狭める。

### 回帰テストの形

- `test_red_missing_assignment_identity_sidecar_does_not_authorise_a_kill` /
  `test_red_missing_recorded_generation_does_not_authorise_a_kill`
  — 証拠の 2 通りの欠け方それぞれで、Worker が生きたまま card も assignment も
  変わらないこと。
- `test_unprovable_retirement_is_escalated_once_and_can_be_released` /
  `test_unprovable_hold_releases_itself_when_the_worker_lets_go_of_the_card`
  — 逆向きの担保。hold は黙って居座らせることではないので、報告が 1 通である
  こと・`--reset` を勧めていないこと・**marker を消せば次の退役は普通に成立
  すること**・**Worker が自分で assignment を手放したら人間を呼ばずに解ける
  こと** (= 二度と終了できない Worker を作っていないこと) を押さえる。
- `test_red_pull_is_refused_while_a_retirement_is_in_flight`
  — 本物の plan.sh で。`exit 2` を返し、card を書き換えず、assignment を
  公開しないこと。
- `test_red_worker_that_pulls_inside_the_guard_is_not_signalled_on_the_new_task`
  — 並行性。実時間では再現できない幅なので、guard が必ず通る `mux.pid()` を
  割り込み点として固定し、そこで本物の `done` → `pull` を走らせる
  (memory: microsecond-race-fix-needs-structural-test)。
- `test_pull_is_allowed_again_once_the_retirement_marker_is_cleared`
  — 逆向きの担保。これが無いと「常に断る」に倒しただけで緑になる。

§6-6 の 3 本 (`..._during_the_grace_period_is_spared` 他) は、(2) の予約で
**正面からは再現できなくなった**。backstop の試験としては残す必要があるので、
`_reservation_lifted` で予約を一時的に外して同じ状態を作っている。予約が届か
ない経路が現実にありうる以上、guard 単体の担保を消してはいけない。

---

## 6-8. 予約を「読む側」だけロックに入れても閉じない (t033, 2026-09-22)

Codex 6 巡目。§6-7 (2) で入れた予約と、§6-4 (2) 以来増え続けた「保留」の出口。
2 件とも **対策を半分だけ適用していた** 箇所である。

### (1) 予約の作成を pull のトランザクションと直列化する (P1)

§6-7 (2) は `plan.sh pull` に marker を見させた。見る側はキューロックの中に
入ったが、**書く側 — `RetirementExecutor.request()` — はロックを取らないまま
だった**。判定と書き込みが別のクリティカルセクションにある以上、それは決定に
なっていない:

```
pull:  retirement_reservation() を通過 (marker はまだ無い)
pull:  … card 走査中 …
here:  marker 作成 → _guard() は assignment を見ない → idle 退役と判断
here:  shutdown 送信
pull:  assignment を公開
```

公開された assignment はどの退役の管轄でもない。`_guard()` の中で mux 参照を
前倒ししても (§6-7 (2) の後半) 閉じない: 読む順番の問題ではなく、**決定を
書き込む瞬間が、その決定が覆すはずの決定と直列化されていない**ことが原因
だからである。

`queue_transaction()` — plan.sh と同じ `queue/.lock` を `LOCK_NB` で取る
contextmanager — を追加し、marker の作成をその中に入れた。

- **待たない。** 呼び手は両方デーモンで、dispatcher は割り当てサイクルを、
  watchdog は監視サイクルを止められない (`plan.sh retire --no-wait` と同じ
  理由 / §6-6 (3))。取れなければ「今サイクルは退役しない」で、倒れる先は
  §5-1 (Worker が居残る) 側であって §5-2 (別の実行を殺す) 側ではない。
- **ロックの中で subprocess を呼ばない。** `current_spawn_identity()` は mux
  の subprocess なので**ロックの外**に残した。中でやるのはカードの読み取りと
  小さな JSON の `os.replace` だけである。
- **世代 (`started_at`) の読み取りはロックの中へ移した。** `cmd_pull()` は
  `started_at` の書き換えと assignment の公開を 1 トランザクションで行うので、
  外で読んだ世代は公開済みの後任のものと食い違いうる。
- `queue_dir` が無い executor は marker を書かない。直列化する相手が分から
  ないうえ、その executor は idle 前提の確認も後始末もできない (設定ミス)。

`write_json_exclusive()` は残す。キューロックは plan.sh との直列化であって、
**2 つのデーモン同士**は順番にロックを取れてしまうので、§6-6 (2) の
create-if-absent は依然として必要である。

### (2) 届かなかった報告は、届くまで再試行する (P2)

`_unprovable()` は `_report()` が False を返しても (= 通知が届かなくても)
`PHASE_UNPROVABLE` を永続化していた。以降のサイクルは `_recheck_unprovable()`
しか通らず report を再試行せず、`_check_stall()` はこの phase の報告を明示的に
抑制する。つまり **通知 1 回の失敗で「Director に伝えて保留」が「保留」だけに
なる**。marker があるあいだ Worker は pull も割り当ても止まる (§6-7 (2)) ので、
外からは何も起きず、復旧に必要な情報はどこにも届かない — この module が消す
ために書かれた幽霊 task と同じ形で、デーモンが黙っている分だけ悪い。

`_report_fields()` が報告の結果を progress のフィールドに畳み、**届かなかった
message をそのまま `pending_report` に保存する**。`_retry_pending_report()` が
毎サイクル再送し、**配信に成功してから** `director_notified` を立てて重複抑制を
始める。message を作り直さず保存するのは、作成に使った入力 (request、guard の
理由) が再送時には消えていることがあり、作り直すと決定時の文面から乖離する
ためである。

同じ形が `_cleanup_deferred()` にもあった (世代が無く後始末を保留する側。
Worker は**既に死んでいる**ので、届かなければ task が in_progress のまま誰にも
知られない)。片方だけ直すと同型が残るので、両方を `_report_fields()` /
`_retry_pending_report()` に載せ替えた
(memory: crewvia-recurring-defect-patterns)。

`_check_stall()` がこの 2 phase を抑制し続けてよいのは、この再送があるから
である — 抑制と再送はセットで読むこと。

### 回帰テストの形

- `test_red_marker_is_not_created_while_a_pull_transaction_is_open`
  — 並行性を実際に作る。**名前付きパイプ**で本物の `plan.sh pull` を
  「予約チェックの後・assignment 公開の前」で止める: `list_tasks()` は
  `tNNN.md` を番号順に全部 open して read するので、`t000.md` を FIFO に
  しておけばそこで止まる。止まったことは sleep で当て込まず、書き手側の
  `O_WRONLY|O_NONBLOCK` open が ENXIO を返さなくなったことで確定する
  (memory: microsecond-race-fix-needs-structural-test)。解放後に pull が
  実際に assignment を公開していることまで assert する — ここが空だと
  「pull が何もしなかったから marker も立たなかった」をテストが見逃す。
- `test_request_is_only_delayed_by_a_live_pull_not_refused_forever`
  — 逆向きの担保。これが無いと「ロックを見たら常に諦める」に倒しただけで
  緑になり、退役が二度と始まらない Worker ができる。
- `test_red_unprovable_hold_retries_its_report_until_the_director_hears_it` /
  `test_red_deferred_cleanup_retries_its_report_until_the_director_hears_it`
  — 通知先を落としたまま hold に入れ、復旧後に **1 通だけ** 届くこと、
  届いたあとは送り続けないこと、報告のついでに kill も queue 書き換えも
  していないことを押さえる。

---

## 7. 参照

- `scripts/dispatcher.sh` — D1 :998、D2 :1210、D3 :1239、D4 :1351、D5 :819、
  `tmux_kill_window()` :523 (`.firstseen` unlink :561-566)、`_mux_created_at()` :570、
  `_spawn_time_fallback()` :590、BENCH_MODE ゲート :1160-1172
- `scripts/watchdog.py` — `check()` :295、`_mux_window_name()` :265、
  `graceful_terminate()` :401、`load_active_tasks()` :509、`run()` :607
- `scripts/lib_mux.py` — `pid()` (backend 抽象メソッド) :185
- `scripts/benchmark-ctx.sh` — 直接 `mux_kill` :199 / :312 (N2)
- `scripts/start.sh` — dispatcher / watchdog の起動 :771 / :780、F6是正コメント :778-779
- `scripts/plan.sh` — `publish_assignment()` / `classify_assignment()` /
  `retire_assignment()` / `cmd_retire()` (§3-5)、
  `retirement_reservation()` + `cmd_pull()` 冒頭のゲート (§6-7 (2))、
  `with_lock()` = `queue/.lock` (§6-8 (1) が marker 作成で共有する)
- `scripts/lib_retirement.py` — `queue_transaction()` / `request()` (§6-8 (1))、
  `_report_fields()` / `_retry_pending_report()` (§6-8 (2))
- `knowledge/worker-shutdown-rules.md` — Rule 1-5 の確定仕様
- `knowledge/worker-vanish-detection.md` — D4 の背景
- `knowledge/dispatcher-restart-after-merge.md` — 常駐デーモンの反映手順
