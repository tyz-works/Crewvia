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

## 7. 相互監視 — heartbeat / respawn / 自己申告 (t005, 2026-09-22)

§1 で述べた「両者は互いの存在を一切知らない」を、ここで閉じる。
外部 supervisor (pm2 等) は使わない (ユーザー決定 2026-09-21。node 依存を増やさず、
設計原則の *mux 非依存 / Taskvia 非依存* と揃えるため)。残る手段は互いを見ることだけで、
dispatcher と watchdog が 2 サイクルの輪をなす。どちらも特権を持たないので、
「監視役が死んだら仕組み全体が止まる」という単一障害点ができない。

実装は `scripts/lib_daemon_watch.py` (判定・respawn・報告) と
`scripts/lib_daemon_watch.sh` (dispatcher の heartbeat 書き出し) の 2 本。

### 7-1. 何が一番まずいのかを先に決める

相互監視が救う障害は「デーモンが止まって誰も気付かない」であり、
相互監視が新しく作りうる障害は「**生きているデーモンの隣にもう 1 つ起動する**」である。
後者の方がはるかに重い: dispatcher が 2 つになると同じ task を 2 人の Worker に配り、
watchdog が 2 つになると同じ retirement を 2 系統が進める (§5-2 の同名別インスタンス
誤 kill が、今度はデーモン側で起きる)。

したがって判定は常に **fail closed = 確証が無ければ respawn しない**。倒れる方向は
「起こし損ねる」側であって「起こしすぎる」側ではない。§5-1 (居座り) と §5-2 (誤 kill)
の選択と同じ向きである。

### 7-2. 生存の書き手を、生きているプロセスに合わせる

| デーモン | 実際に生き続けるプロセス | heartbeat の書き手 | 周期 |
|---|---|---|---|
| dispatcher | main loop を回す **bash** (python3 は 1 サイクルごとに使い捨て) | `lib_daemon_watch.sh` の `daemon_beat` (bash) | 5s |
| watchdog | `watchdog.py` そのもの | `DaemonWatch.beat()` (python) | 30s |

dispatcher の heartbeat を bash から書くのには 2 つ理由がある。

1. **相手が probe すべき PID は bash の `$$` である。** python は毎サイクル別 PID に
   なるので、記録しても次の cycle には存在しない。
2. **dispatch サイクルの成否から独立させる必要がある。** python 側が毎回例外で落ちる
   状態 (壊れた card、中途半端な deploy) では bash ループは元気に回り続けているのに
   heartbeat だけが止まる。相手から見ると「生きているのに死んで見える」＝ respawn ＝
   dispatcher が 2 つ、という 7-1 の最悪ケースそのものになる。そのため
   `daemon_beat` は main loop の **先頭で無条件に** 呼び、`run_dispatch` と
   `&&` で繋がない (回帰: `test_dispatcher_beats_independently_of_the_dispatch_cycle`)。

watchdog 側の穴は `graceful_terminate()` である。ここは send → **60s 待機** →
SIGTERM → **10s 待機** → SIGKILL の間、main loop を最大 70 秒ブロックする (§4-1)。
1 cycle に 1 回しか書かない実装だと、**Worker を終了させるたびに watchdog 自身が
死んで見える**。よって両方の待機ループの中でも `_beat()` を呼ぶ
(回帰: `test_watchdog_beats_inside_the_blocking_terminate_wait`)。
`watchdog_stale_seconds` の既定 240s も、この 70s を跨いでなお余裕が残る値として選んだ。

置き場は `registry/daemons/` で、Worker 用の `registry/heartbeats/` とは分ける。
後者は **エージェント名** をキーに D4 (vanished worker 検知) が走査するので、
そこに `watchdog` というファイルを置くと「watchdog という名前の Worker」に見える。

### 7-3. 死亡と判定する条件 — 証拠の強度で並べる

以下を **すべて** 満たしたときだけ respawn する。1 つでも欠ければ保留 (hold)。

| # | 条件 | 満たせない時の読み |
|---|---|---|
| 0 | 相互監視が有効 / 自分の checkout が本物 (`repo_identity_ok`) | `refused` |
| 1 | 停止マーカー `<name>.paused` が無い | `paused` (§7-5) |
| 2 | 直前の respawn から `respawn_grace_seconds` 経過 | `grace` |
| 3 | heartbeat が stale、または最初から無い | `healthy` |
| 4 | **プロセスが存在しない** | `hold` |
| 5 | flap しきい値未満 | `flapping` (§7-6) |

**生死の証拠は 4 だけ**である。タブの有無は条件に入らない (理由は §7-3-1)。

**4 — プロセス (唯一の証拠).** 2 つの独立した probe を使い、どちらか一方でも「生きている」と
答えたら respawn しない。

- 記録 PID を **世代ごと** 照合する (`instance_alive()`)。PID は再利用されるので、
  番号が存在することは同じデーモンである証拠にならない。`/proc/<pid>/stat` の
  22 番目 (starttime) を `generation` として heartbeat に書き、両方一致したときだけ
  「生きている」と読む。これは §5-2 の「名前は同じでも中身は別物」を PID の層で
  繰り返さないためで、bash 側と python 側が同じ数え方をしていることは実プロセスに対して
  検証してある (`test_bash_written_heartbeat_is_readable_by_python` + 隔離 smoke)。
- `/proc` を絶対パスで走査する (`scan_daemon_pids()`)。needle は
  `<repo_root>/scripts/dispatcher.sh` のような**絶対パス**で、`dispatcher.sh` という
  名前ではない。名前で照合すると、隔離 QA 用の worktree で動かしたデーモンが本番の
  デーモンに見えてしまう (逆も同じ)。走査そのものが失敗したら `None` を返し、
  **`[]` (見たが居なかった) と区別する**。

この probe を主証拠に置くのは、**カーネルが mux backend を介さずに答える**からである。
バックエンドの不調から独立している唯一の信号がこれしかない。

### 7-3-1. タブの名前を生存の証拠に使わない (t035 / t006 QA FAIL-1 の差し戻し)

当初は 4 に加えて「**権威ある窓一覧に名前が無い**」を必要条件にしていた
(`mux.list()` は両 backend とも失敗を黙って `[]` に変換するので、空でない一覧だけを
権威として扱う、という §6-3 (1) と同じ形)。**この条件が本番では永久に満たされない。**

`start.sh` も `TmuxBackend.spawn` も `HerdrBackend.spawn` も、**pane にシェルを置いて
そこへコマンドを流し込む**。だからデーモンは pane シェルの子であり、デーモンが落ちても
シェルは生き残り、pane は label を保ったまま残る (= husk)。本番実測:

    pid 1770898 = dispatcher → PPid 1769235 = /bin/bash → PPPid 1769218 = herdr server
    pid  719042 = watchdog   → PPid  718789 = /bin/bash → PPPid 1769218 = herdr server

`HerdrBackend.list()` は pane の label を**生死を見ずに**返す。つまり名前は常に在り、
判定は必ず husk 分岐へ落ちる。t006 QA は隔離環境でこれを実測した: プロセスだけを
kill した 4 分間、respawn は **0 回**、Director に届くのも既定 1800 秒後の
「自動 respawn は安全側で止めています」= 人を呼ぶ 1 通だけだった。
タブごと消した場合だけ設計どおり動く。

husk を「herdr サーバー再起動時に残る稀な状態」と見積もっていたのが誤りで、
**husk はあらゆるクラッシュの通常形**である。fail closed の向き自体は正しいが、
この条件では相互監視の主目的 (片系が落ちたら起こし直す) が一度も発火しない。
「起こしすぎない」ために「一度も起こさない」になっていた。

**決定: 窓一覧を判定から外し、プロセス層を唯一の生死の証拠にする。**
窓一覧は respawn の宛先を決めるためだけに使う。

二重起動への最後の防壁は、名前の有無ではなく **その pane の中身の生死** に移した。
`mux.spawn()` が既存 pane を見つけたとき:

| backend | husk の判別 | husk だった場合 |
|---|---|---|
| herdr | `pane process-info` の foreground_processes が idle shell だけか (`_is_idle_shell_process`) | その pane で `pane run` して True |
| tmux | pane シェルの pid (`#{pane_pid}`) から `/proc` を読み、**シェルであり、生きた子を持たない**か (`_pane_shell_is_idle`) | その窓へ send-keys して True |

どちらも読めなければ「使用中」に倒す (= spawn は False)。名前より強い証拠であり、
backend 間で強さが揃う。tmux 側は t035 で足した — それまでは tmux の spawn が
「窓が在る」だけで無条件に False を返しており、husk を認めても tmux では何も
起きなかった。

`available()` / `server_running()` による救済は **あえて入れていない**。どちらも
タイムアウトや例外で False を返すので、1 つの障害が両方を倒し、2 つの失敗が互いを
補強して「相手は死んだ」の権威になってしまう。それは §6-3 (1) で塞いだ欠陥を
1 層下で再生産することである (memory: `fail-closed-guard-can-recreate-the-defect`)。

回帰は**実プロセスでしか置けない**。偽の mux は spawn 後に窓を消すか残すかを
テスト側が決められるので、この欠陥をテスト自身が隠せる。
`tests/test_daemon_husk_respawn.py` は実 tmux 窓で stub デーモンを起こし、
**プロセスだけ** を SIGKILL して窓が残っていることを確認したうえで 1 cycle を回す。
判定表側の双子は `test_a_husk_tab_does_not_veto_the_respawn` と
`test_a_refused_spawn_holds_instead_of_claiming_a_respawn`。

#### 7-3-2. 「子が無いシェル」は「プロンプトに居るシェル」ではない (t036)

t035 の husk 判別は **comm がシェルで、生きた子が居ない** の 2 つだけを見ていた。
これは緩すぎる。次はどれもシェルで、どれも子を持たず、どれも仕事中である:

    bash script.sh          builtin だけで完結するスクリプト
    bash -c 'while :; …'    同じものの短い形
    bash < pipe             端末ではない入力を読んでいる非対話シェル

ここへ launch コマンドを送り込んでも デーモンは起動しない。最悪の場合、走っている
処理が**それを入力として食う**。よって idle は「仮定する」ものではなく「示す」もの
とし、`_pane_shell_is_idle()` は 6 点すべてを要求する: シェルであること / **引数
無しで起動された** こと (`bash script.sh` も `bash -c` もここで落ちる) / 制御端末が
あること / **自分のプロセスグループがその端末の前景** であること / state が S
(builtin ループは R で回る) / 生きた子が 1 つも無いこと。

それでも残る穴が 1 つある。**対話シェルが `read` で止まっている状態は、プロンプトで
止まっている状態と /proc 上で完全に同一** である (state も argv も tpgid も同じ)。
区別が付かない以上、判定層では閉じられない。閉じるのは 1 層上で、
`spawn()` が husk へ相乗りしたときだけ**送った後に「本当に何か走り出したか」を
pane に問い直す** (`_wait_until_launched`, 既定 10 秒)。走っていなければ spawn は
False を返し、相互監視は respawn を記録せず hold する。これが無いと、コマンドを
食われた 1 回が「成功した respawn」として grace と flap 枠を消費する。

回帰は実プロセス:
`tests/test_daemon_husk_respawn.py::test_a_shell_running_a_script_is_not_idle` ほか
と、実 tmux で `read` 中のペインへ spawn する
`test_spawn_does_not_claim_success_when_the_pane_swallows_the_command`。
逆側 (本物の husk は idle のままで respawn できる) は
`test_a_real_interactive_shell_at_its_prompt_is_idle` が固定している。

#### 7-3-3. 読めなかった /proc エントリは「不在」ではない (t036)

`scan_daemon_pids()` は `cmdline` の OSError をすべて「終了した」として skip して
いた。権限エラーや読み取り失敗は不在の証明ではないのに、**不完全な走査が
「何も走っていない」として respawn の条件を満たしてしまう**。`_live_children()` も
同じ前提で、読めない stat があると「子が居ない」= 空いている pane と答えていた。

分けるのは errno で、**ENOENT / ESRCH だけが「本当に居ない」**。走査中にプロセスが
終わるのは日常なのでこれを不明扱いにすると respawn が二度と起きない (過剰修正)。
それ以外は 1 件でも出たら走査全体を `None` (= 保留) にする。`hidepid` のマシンでは
毎回 hold になるが、それが正しい倒れ方であり、出口は §7-4 の 30 分報告である。

### 7-4. 保留には必ず出口を付ける

上の表の `hold` はどれも「次の cycle でもう一度見る」であり、一過性の原因
(バックエンドが復帰する、詰まったデーモンが死ぬ) には正しく効く。効かないのは
永久に解けない組み合わせで、そこで黙ると**無音の故障**になる。

そこで `hold_report_after_seconds` (既定 1800) を超えた保留は Director に
**1 度だけ** 報告する。自動で解消はしない — ここで採れる自動の解消手段は respawn しか
なく、それが 7-1 の危険な向きだからである (memory: `fail-closed-discard-vs-hold`)。

報告は**届くまで再試行する**。`mux.send()` の戻り値を見ないと「Director に伝えた」が
偽のまま真に見え、それは障害が残す唯一の痕跡を消すことになる。未達の報告は
`registry/daemons/<self>.reports.json` に積み、毎 cycle の先頭で再送する
(§6-8 (2) と同じ形)。届いた報告は `delivered` 台帳に移り、二度と送られない。

### 7-5. 意図的な停止 — maintenance マーカー

運用で実際に打つ復旧手順は「kill してから spawn」である。この**隙間でデーモンは
本当に死んでいる**ので、そこを見た相手が respawn するのは正しく、その 1 秒後に手で
打った spawn が上に乗る = 二重起動になる。

`registry/daemons/<name>.paused` がある間は respawn しない。順序が効くので、
`restart()` は **lock → pause → kill → spawn → resume** で実行する (kill を先に
すると上記の隙間が開いたままになる。回帰:
`test_restart_helper_locks_then_pauses_then_kills`)。先頭のロックが §7-10、
その次の pause が**成功したときだけ**先へ進むのが次の段落である。

**マーカーが disk に残っていなければ、破壊的ステップに進んではいけない (t036)。**
`pause()` は `write_json_atomic()` の戻り値を見たうえで**読み直して token が
一致すること**まで確かめ、駄目なら `None` を返す。書けなかったのに token を返すのは
「保護がある」という嘘で、その嘘の直後に kill が走る。`restart()` は `None` を見たら
**何も kill せずに** False を返し、CLI の `pause` も非ゼロで終わる (回帰:
`test_restart_does_not_kill_when_the_pause_marker_cannot_be_persisted` /
`test_pause_cli_exits_nonzero_when_the_marker_cannot_be_persisted`)。

マーカーは `token` で **その restart 実行に束縛** する。名前だけで束縛すると、
重なった 2 回の restart のうち先に終わった方の `resume` が、まだ作業中の方の保護を
外してしまう — これも「名前は識別子ではない」の一例である。

マーカーは**古くなっても自動では外さない**。「古い」は「作業が終わった」の証拠では
なく、作業途中の相手を起こすのは二重起動だからである。代わりに
`pause_report_after_seconds` を超えたら Director に 1 度だけ報告する。

    # 手動の restart (推奨)
    python3 scripts/lib_daemon_watch.py restart dispatcher

    # 状態確認
    python3 scripts/lib_daemon_watch.py status

    # 手で pause/resume する場合 (token を控えること)
    python3 scripts/lib_daemon_watch.py pause watchdog --reason "PR #NNN 反映"
    python3 scripts/lib_daemon_watch.py resume watchdog --token <token>

§5-3 の「両デーモンを同時に停止 → 両方 respawn」を手で行う場合は、**先に両方を
pause してから** kill すること。片方だけ pause して kill すると、生きている側が
死んだ側を起こす。

### 7-6. flap ガード

起動直後に落ちるデーモンを延々と起こし続けても復旧はしないし、ログだけが流れる。
`flap_window_seconds` (既定 900) の中で `flap_threshold` (既定 3) 回に達したら
自動 respawn をやめ、Director に**対応を求める**通知に格上げする (他の報告が
「対応は不要です」で終わるのと対照的に、これは人を呼ぶ)。

カウンタは名前ではなく**どのインスタンスを置き換えたか** (`replaced_generation`) を
記録する。単なる回数は「同じ死体を 3 回起こした」(本物の flap) と「3 世代が健全に
入れ替わった」を区別できない。窓はローリングで、ラッチではない — 1 時間前に荒れた
デーモンが今日も起こせないのは行き過ぎである。

### 7-7. 起動コマンドの単一の出どころ

respawn のコマンドは `spawn_command()` が唯一の出どころで、`start.sh` も
`lib_daemon_watch.py spawn <name>` (= ロックを取ってから `spawn_command()` で
起動する経路) を呼んでこれを使う (回帰: `test_respawn_command_matches_start_sh` /
`test_start_sh_launches_the_daemons_through_the_locked_path`)。

同じ文字列を 2 箇所に置くと、**どちらの backend と話すかを決める変数だけが片方に無い**
という形でずれる。`Mux.spawn()` の `env=` 引数は **両 backend とも無視する** ので、
env はコマンド文字列に埋め込むしかなく (memory: `lib-mux-spawn-env-arg-ignored`)、
herdr はさらにサーバー起動時の env を全ペインに継承するため、「./crewvia で起動した
デーモン」と「相手に起こされたデーモン」が別の backend を向く事故が現実に起こりうる。

運ぶのは `CREWVIA_MUX` だけでは足りない。**backend (どう話すか) と宛先 (どこと話すか)
は別**で、`CREWVIA_TMUX_SESSION` / `CREWVIA_HERDR_WORKSPACE` が落ちると、起こされた
デーモンは既定のセッション名にフォールバックし、`list()` が存在しないセッションを
引いて空で返り、**相互監視が無言で恒久 hold に縮退する**。t006 QA が隔離環境で実際に
踏んだ (`CREWVIA_MUX=tmux` だけが env に入り `tmux list-windows -t crewvia` が失敗)。
本番は既定名なので露見しない = 既定以外を使い始めた瞬間に静かに壊れる形だった。
`_SPAWN_ENV_VARS` に並べ、値は必ず単一引用で囲って **データとして** 渡す
(回帰: `test_spawn_command_carries_the_mux_destination_too` /
`test_spawn_command_quotes_a_hostile_session_name`)。

#### 何を引き継ぎ、何を引き継がないか (t036)

mux の 3 変数だけでは足りない。起こし直されたデーモンが**別の設定で動く**なら、
それは復旧ではなく「同じ名前の別のデーモン」を起動したことになる。運ぶのは
**非機密の運用上書き** に限った allowlist:

| 群 | 変数 | 落としたときに起きること |
|---|---|---|
| mux | `CREWVIA_MUX` / `CREWVIA_MUX_ENABLED` / `CREWVIA_TMUX_SESSION` / `CREWVIA_HERDR_WORKSPACE` / `CREWVIA_HERDR_SOCK` | 別の backend・別のセッションを向き、相互監視が無言で hold に縮退する |
| 運用 | `CREWVIA_QUEUE` / `CREWVIA_KILL_AUTHORITY` / `CREWVIA_TASKVIA` / `CREWVIA_PROJECT` / `CREWVIA_NOTIFY_CACHE` / `CREWVIA_SPAWN_GRACE` / `CREWVIA_STATE_GRACE` / `CREWVIA_BENCH_MODE` | **別の queue を割り当て始める**。kill 権限が dispatcher と watchdog で食い違う (§5 が「どちらに倒しても必ず壊れる」と書いている状態)。隔離 QA のデーモンが本番の notify cache に戻る |
| 相互監視 | `CREWVIA_DAEMON_*` (§7-8 の全キー) | 起こされた側だけ既定しきい値に戻り、緩めた側を stale と読んで起こし返す = 相互監視が自分で flap を作る |

**運ばないもの** — `TASKVIA_TOKEN` / `NTFY_PASS` / `NTFY_USER` などの機密、
`AGENT_NAME` / `TASK_ID` などの Worker 固有値、`HERDR_ENV` / `TMUX` などの mux 内部値。
コマンド文字列は `ps` にもペインの履歴にも mux のログにも残るので、**機密を埋め込む
のは漏洩**である。機密は今まで通りペインが作られる env 経由で渡り、無ければ
Taskvia 同期が落ちるだけ (= 安全側) に縮退する。`os.environ` を丸ごと運ぶ「素朴な
修正」は、この漏洩と、herdr server の古い env を引きずる既知の罠
(memory: `herdr-server-stale-env-inheritance`) を同時に踏む。

allowlist は**推移的**に効く: これらはデーモン自身の env に export されるので、
そのデーモンが後で相手を起こすときにも同じ値が乗る。
(回帰: `test_spawn_command_carries_the_operational_overrides` /
`test_spawn_command_carries_the_mutual_watch_settings` /
`test_spawn_command_does_not_carry_secrets`)

### 7-8. しきい値 (`config/crewvia.yaml` の `daemons:`)

| キー | 既定 | 意味 |
|---|---|---|
| `mutual_watch` | `true` | 相互監視そのものの ON/OFF |
| `dispatcher_stale_seconds` | 60 | 5s 周期の 12 サイクル分 |
| `watchdog_stale_seconds` | 240 | 30s 周期の 8 サイクル分、かつ 70s ブロックを跨げる値 |
| `respawn_grace_seconds` | 120 | respawn 直後、まだ heartbeat を書いていない間は判定しない |
| `flap_window_seconds` / `flap_threshold` | 900 / 3 | §7-6 |
| `hold_report_after_seconds` | 1800 | §7-4 |
| `pause_report_after_seconds` | 1800 | §7-5 |
| `watch_lock_timeout_seconds` | 2 | §7-10。watch 側は短く — 取れなければ保留するだけ |
| `maintenance_lock_timeout_seconds` | 60 | §7-10。人が待っている操作なので進行中の判定に並ぶ |

env での上書きは `CREWVIA_DAEMON_<KEY 大文字>` (例: `CREWVIA_DAEMON_MUTUAL_WATCH=0`)。

### 7-10. 決定を直列化する — マーカーだけでは閉じない (t036, 2026-09-23)

§7-5 のマーカーは「意図的な停止」を**伝える**手段であって、判定と maintenance を
**排他にする**手段ではなかった。実際の壊れ方:

    watcher  : マーカーを読む → 無い
    operator : マーカーを書く → peer を kill
    watcher  : そのまま spawn            ← 手動 spawn と並んで 2 つ起動する

読んだ瞬間と spawn する瞬間は別の瞬間で、その間に何でも起こる。両 backend の
「既にあるか見てから作る」も 2 段階なので排他にはならない (spawn の拒否は最後の
防壁であって相互排除ではない)。PR #205 が 6 巡かけて学んだ
**「決定は、書き込む瞬間が直列化されていて初めて決定になる」** と同型である。

したがって **デーモン 1 つにつき 1 つのロック** (`registry/daemons/<name>.lock`、
`flock`) を置き、次の 3 つを同じロックで囲う:

1. watch の判定全体 — マーカーの読み → 生死の判定 → spawn → 記録 (`DaemonWatch._decide`)
2. maintenance — `pause` → kill → spawn → `resume` (`restart()`, `pause()`)
3. 起動そのもの — `./crewvia` (`spawn_daemon()` / CLI `spawn`)

3 を入れるのが肝心で、**起動主体は 3 つある**。2 つだけ囲っても、相手が respawn を
決めた直後に人が `./crewvia` を叩けば同じ二重起動になる。

マーカーファイルではなく `flock` なのは、**保持者が死ねばカーネルが外す**からである。
クラッシュした restart が残したロックが居座ると相互監視が恒久的に止まり、それは
まさにマーカーの stale 報告が拾おうとしている故障そのものになる。

ロックが取れなかった側は**必ず何もしない**: watch は 1 サイクル hold (次の周期で
また見る)、maintenance と launcher は非ゼロで終わる。ロックファイルすら作れない
環境も同じ扱い — 直列化できないなら破壊的なことをする資格が無い。

回帰: `tests/test_daemon_watch_hardening.py` の §1
(`test_a_restart_in_progress_stops_the_watcher_from_spawning` /
`test_the_decision_and_the_spawn_happen_under_one_lock` /
`test_a_pause_cannot_slip_in_while_a_decision_is_in_flight` /
`test_the_launcher_also_starts_daemons_under_the_lock`)。

**しきい値を詰めすぎないこと。** 1 回遅いサイクルが「死亡」に見えた瞬間、それは
7-1 の二重起動である。stale 判定は respawn の入口にすぎず、そこから 4 の実在確認に
進むのだから、余裕を取っても検知が遅れるだけで見落としにはならない。

### 7-11. テストが本番の mux を掴む (t037, 2026-09-23 の本番障害)

**約 4 時間半、本番の dispatcher が止まった。原因は t036 のテストである。**

`restart` の CLI を実プロセスで検証する赤いテストが、本物の herdr・既定ワーク
スペース `crewvia`・既定ペイン名 `dispatcher` を対象に走り、本番のペインを
乗っ取った。ペインは pytest の一時ディレクトリを指すコマンドで置き換えられ、
テスト終了後にそのディレクトリが消えて死亡。タブだけが残り (§7-3-1 の husk)、
相互監視はまだ merge されていなかったので誰も気付かなかった。

```
cd /tmp/pytest-of-tkadmin/pytest-199/test_restart_cli_exits_nonzero0/crewvia \
  && ... bash .../scripts/dispatcher.sh
bash: /tmp/pytest-of-tkadmin/.../scripts/dispatcher.sh: No such file or directory
```

`--repo-root` は隔離されていた。隔離されていなかったのは**宛先**で、CLI は
`Mux()` を周囲の環境変数から組み立てる。そして — ここが肝心だが —
**赤いテストは定義上、欠陥のある破壊的経路を必ず通る**。「そのテストを直す」は
対策にならない。隔離をテスト作者の記憶に預けてはいけない。

防壁は 2 本立て、**別々の証拠**に立たせた。片方を破っても片方が残る
(memory: fail-closed-guard-can-recreate-the-defect)。

**(a) mux 層のテスト隔離** — テスト中かどうかを知っている側。

| 変数 | 役割 |
|---|---|
| `CREWVIA_MUX_TEST_ISOLATION` | 「今はテスト中」の印。`tests/conftest.py` が `os.environ` に置くので **subprocess にも継承される** (事故で唯一欠けていたもの) |
| `CREWVIA_MUX_PANE_PREFIX` | ペイン名の名前空間。**本番は空で完全な no-op**。テスト中は `spawn("dispatcher")` が `<prefix>dispatcher` に解決されるので、本番のペイン名そのものがテストから言えない |

テスト中に、宛先が既定の `crewvia` のまま／接頭辞が空のまま、ペインを名指しする
verb (`spawn` / `send` / `kill` / `pid` / `capture`) を呼ぶと
`MuxTestIsolationError`。**実行前に**投げるので「何も起きなかった」まで保証する。
CLI は traceback ではなく 1 行で断り exit 3。

読み取り (`pid` / `capture`) も塞いでいるのは、それが破壊を**認可する**ステップ
だからである (`restart()` はペインの pid を見て kill してよいかを決める)。規則は
単純に「テストは本番のペインを**名指しできない**」。`list()` は名前を取らないので
開けてあり、代わりに結果から名前空間を剥がす。

**(b) repo identity ガード** — テストかどうかを知らない側。§7-1 の `repo_identity_ok()`
が「自分に checkout があるか」を問うのに対し、`pane_daemon_owner()` は反対側、
**そのペインで走っているのは誰のデーモンか**を `/proc` で問う。

| 答え | 意味 | kill |
|---|---|---|
| `mine` | ペインの配下で `<repo_root>/scripts/<script>` が走っている | 通す |
| `none` | それらしいものが居ない (husk / 新しいペイン) | 通す — §7-3-1 の復旧経路 |
| `foreign` | **別チェックアウト**の同じスクリプトが走っている | 断る |
| `unknown` | ペインの pid か `/proc` が読めない | 断る |

`unknown` を通すと、このガードは「読めるときだけ効く」ものになる。本番が壊れるのは
たいてい読めないときなので、それでは意味が無い。ただし fail-closed に出口を付ける
(memory: fail-closed-discard-vs-hold): `restart --force` が操作者の逃げ道である。

一台のマシンに crewvia の checkout が 2 つある (QA 用 worktree、2 つ目の WSL、
herdr の古い env) のはこの repo の**普通の状態**なので、(b) は誰も何も宣言して
いない本番同士の誤射にも効く。

回帰: `tests/test_mux_production_safety.py`。事故の形そのもの
(pytest → subprocess → CLI → 本番を指す env) を記録専用の tmux スタブで再現し、
欠陥を戻すと `kill-window -t crewvia:dispatcher` が記録されて赤くなる。
bats は python の import 層を通らずガードが効かないので、**PATH に置いた偽
tmux / herdr** が唯一の隔離になる。その対応関係も
`test_every_bats_suite_that_can_reach_the_mux_installs_a_path_stub` で固定した。

### 7-11-1. 境界を「呼ぶ側の env」から「対象の identity」へ移す (t038, Codex 3 巡目)

§7-11 (a) の隔離は **env に依存している**。Codex 3 巡目はそこを突いた:

```
env を消して TmuxBackend().kill("dispatcher") を呼ぶと
tmux kill-window -t crewvia:dispatcher が発行された
```

`CREWVIA_MUX_TEST_ISOLATION` は**呼ぶ側の性質**なので、きれいな env で
サブプロセスを起動すれば消える。そして `pane_daemon_owner()` は `restart()` の
中にしか無く、**直接の mux 呼び出しは守っていなかった**。

**本当の防壁は「対象が誰のものか」でなければならない。** 2026-09-23 の事故も、
identity ガードが正しく働いていれば env に関係なく防げた (本番ペインでは別
checkout のデーモンが動いていた = foreign)。そこで (b) を本線にし、(a) は
「テストが本番のペイン名を**名乗れない**」ための利便性の層として残す。

**2 つの層は形が違う。混ぜてはいけない。**

| 層 | 形 | 曖昧なとき | 適用範囲 |
|---|---|---|---|
| `lib_mux.kill()` の backstop | **allowlist** (`mine` / `none` のみ) | 断る | `DAEMON_PANE_NAMES` のペインだけ |
| `pane_daemon_owner()` (デーモン層) | **allowlist** (`mine` / `none` のみ) | 断る | `restart()` の判定 |

**2 つの層は同じ形にした (t039)。** ここには元々「mux 層を allowlist に
できないのは、そこが Worker のペインも通る道だから」と書いてあり、曖昧なときは
通す設計だった。Codex の 4 巡目がその理屈の誤りを指摘した — **適用範囲が既に
`DAEMON_PANE_NAMES` に絞られている以上、Worker のペインはこの分岐に来ない**。
Worker の可用性は、判定を緩めることではなく**スコープ**が守っている。

緩めた代償として、`unknown` が kill の許可に変換されていた。そこを通れたものは:

- `/proc` 走査中に読めない `cmdline` が 1 つでもある
- バックエンドがペインの pid を答えない
- `python3.12` のような表に無いインタプリタで起動された別 checkout の watchdog

いずれも「判断できなかった」であって「空だった」ではない。きれいな env からの
`mux.kill("watchdog")` が本番を閉じられる、という形で残っていた。

適用範囲をデーモンのペイン名に絞ってあるのは今まで通り — crewvia の QA Worker は
自分のペインで `bash scripts/dispatcher.sh` を走らせることが実際にあり、そこまで
広げるとその Worker が二度と retire できない。**判定は allowlist、守る unit は
1 つに絞る** (memory: approve-judgment-needs-allowlist-and-scope)。

出口は `restart --force` (`kill(allow_foreign=True)`) の 1 本だけ。出口の無い
fail closed は「判断できない」を「何も二度と動かない」に化けさせる
(memory: fail-closed-discard-vs-hold)。

**破壊は覗いた対象そのものに束縛する。** 所有権の判定が真偽値しか持ち帰らないと、
kill は対象を名前から**引き直す**ことになる。tmux の window 名も herdr の
ラベルも可変で、あいだに挟まる `/proc` の全走査には実時間がかかる。覗いたタブが
消えて別 checkout が同じ名前で作り直せば、**一度も覗いていない後継**を閉じる。
`_inspect_pane()` が「不変の id + そのときの pane pid」を 1 回の問い合わせで
返し、kill はその id (tmux は `@window_id`、herdr は `tab_id`) を宛先にする。
後継は別の id を持つので、届かずに失敗する — それが欲しい答え
(PR #205 の `_verified_pid()` と同じ形)。

**起動形態を同定する。** 修正前の `pane_daemon_owner()` は「`/scripts/<script>`
で終わる引数」を探していた。つまり:

- `bash scripts/dispatcher.sh` (**人が実際に打つ形**) は何にも一致せず、その
  ペインは `none` = **husk = 破壊してよい**と分類されていた
- 逆に、引数のどこかに自分のパスがあれば即 `mine` — `tail -f …/dispatcher.sh`
  でも kill を認可した

`lib_mux.script_owner()` は代わりに **実行しているスクリプト**を argv から取り出し
(`_executed_script_arg()`: argv[0] がインタプリタなら最初の非オプション引数、
`-c` / `-m` の後ろはスクリプトではない)、プロセス自身の cwd に対して解決する。
名前は出てくるのに実行位置を特定できない形 (`env FOO=1 bash scripts/…`) は
`none` ではなく **`unknown`**。認識できない起動形態を「空のペイン」と書くと、
知らない形が全部**破壊側**に落ちる。

**スクリプトの同定は「字面」ではなく「届くファイル」で行う (t039)。** 同定を
argv の綴りに任せると、同じ欠陥が 2 つの向きから戻ってくる:

- **別名の symlink** — `python3 /theirs/monitor` (`monitor` は
  `/theirs/scripts/watchdog.py` を指す)。basename で先に絞っていたので
  `foreign` でも `unknown` でもなく **`none`** = 空のペイン。生きた他人の
  デーモンが husk として潰せた
- **`..` の字面での畳み込み** — `/ours/link` が `/theirs/subdir` を指すとき、
  `/ours/link/../scripts/watchdog.py` が実際に動かすのは
  `/theirs/scripts/watchdog.py`。`normpath()` はこれを
  `/ours/scripts/watchdog.py` に畳むので **`mine`** = 自分のデーモン

`normpath()` は symlink を知らないまま `..` を消すので、**パスが指すファイルが
変わる**。`_resolved()` (= `realpath()`) で **symlink を解決してから**認識し、
`_same_script_file()` も両辺を解決して比べる。字面の一致はスクリプト同一性の
証明にならない。

**respawn に隔離を引き継ぐ。** mux 経由で起動されるプロセスは呼び出し側ではなく
**mux サーバーの env** を継承する (herdr は server 起動時の env を全ペインに
複製する。memory: herdr-server-stale-env-inheritance)。したがって
`CREWVIA_MUX_TEST_ISOLATION` / `CREWVIA_MUX_PANE_PREFIX` は `spawn_command()` の
`export` 一覧に載せるしかない。載っていないと、名前空間付きのテストペインに
起動されたデーモンが素のペイン名を名指しし、保護を両方とも失う。

**消せなかったら成功と言わない。** `resume()` は `unlink_quiet()` を呼んで無条件に
true を返していた。marker がディレクトリだったり親が書き込み不可だと、**保護が
残ったまま「解除しました」と答える** — 相互監視は永久に hold し、出口は 30 分後の
stale-pause 報告だけになる。`_remove_marker()` が削除の成否を確かめ、失敗なら
診断付きで false を返す。`--force` の枝と普通の枝の両方に要る (片方だけ直すと
同じ欠陥が残る)。

回帰: `tests/test_pane_identity_guard.py`。欠陥を 9 通り戻して、それぞれ対応する
テストが赤くなることを確認済み。

> **赤の証明そのものが罠だった。** 同じファイルへの 2 つの注入が pristine と
> **同じバイト数だけ**違い、しかも 1 秒以内に書かれると、`.pyc` のキーが
> (mtime の**秒**, size) なので **1 つ目のバイトコードが再利用される**。
> 2 つ目は「緑のまま」に見えるが、走っていたのは 1 つ目のコードだった。
> 欠陥注入のハーネスでは `PYTHONDONTWRITEBYTECODE=1` と `__pycache__` の削除を
> 必ず入れること (memory: red-proof-catches-tests-green-for-the-wrong-reason)。

### 7-11-2. 破壊の根拠を「中の人の同定」から「自分が作った記録」に変える (t040, Codex 5 巡目)

§7-11-1 で境界を「対象の identity」に移し、t039 で判定表を allowlist に反転した。
それでも Codex 5 巡目の P1 は同じ場所から 3 件出た。

| # | 形 | 出る答え |
|---|---|---|
| 1 | 相対パスの別名 (`./monitor`) で cwd が読めない | `NONE` |
| 2 | `python3.12 /theirs/monitor` (未知のインタプリタ + 別名 symlink) | `NONE` |
| 3 | 起動後に `/ours/link` が消え、`..` が字面で畳まれる | `MINE` |

4 巡かけて表を反転しても同じ場所から出続けるなら、直すべきは表ではない。
**`NONE` も `MINE` も破壊を許す結論**であり、そのどちらもが「外の世界についての
推論」で出ている。分類器が知らない起動形態は今後も必ず現れる。

そこで**土台を変えた**。

> **破壊してよいのは「自分が作ったと記録に残っているペイン」だけ。**

`registry/mux/<name>.json` は `spawn()` が pane を作った瞬間に書く。中身は
バックエンド自身の不変 id (tmux `@window_id` / herdr `tab_id`)。これは**観測して
推論する事実ではなく、自分が書いた事実**なので、symlink も相対パスも未知の
インタプリタも関係ない。記録は「これを作ったのは私か」に答える — 破壊が本当に
懸かっている問いはこれである。

判定は `lib_mux.may_destroy_pane()` 1 箇所。積極的な証明が 2 つ、拒否が 1 つ:

- **provenance** — 自分の spawn 記録がこのペインを名指ししている。強い方の証明。
- **emptiness** — シェル以外に生きたプロセスが 1 つも無いことを確認できた。
  provenance の *代わり* ではなく *並び*: herdr を再起動するとタブは復元されるが
  id は新しくなり、記録は全部古くなる。この出口が無いと husk を二度と掃除できない
  (memory: fail-closed-discard-vs-hold)。
- **veto** — 別チェックアウトのデーモンを積極的に同定できたら、記録があっても断る。
  記録は「ペインを作った」ことしか証明しない。

**`MINE` は根拠から外した。** それが 3 の欠陥そのものだったから。

分類器は backstop として残るが、失敗はすべて `UNKNOWN` に倒れる:

- `NONE` を返してよいのは「シェル以外に生きたプロセスが無い」を確認できたときだけ。
  「デーモンだと認識できるものが無かった」は別の答えで、これを `NONE` にしていた
  から、分類器が取り逃がすたびにペインが「空」に化けていた。
- `..` は、そこから登るディレクトリが実在するときにしか辿らない。実在しなければ
  `UNDECIDED`。`..` を含まないパスは従来どおり解決するので、消えた worktree の
  同定 (retirement が要る) は壊れない。
- versioned interpreter (`python3.12`, `perl5.36`) を認識し、mention 走査は argv の
  パスを**解決してから**照合する。

付随して要ったもの:

- **tmux も spawn 記録を書く。** 従来は herdr だけだったので、tmux モードでは
  provenance が常に不在になり、新しい判定が常に拒否に倒れてしまう。
- **herdr に訊けなかっただけでは記録を捨てない。** 一過性の失敗が永久の拒否に
  化ける。「消えた」と**答えられた**ときだけ畳む。
- **`restart()` は mux が断った kill を「済んだこと」にしない。** 握り潰すと、
  生きているデーモンの上に spawn が乗る — この関数が防ぐはずの二重起動に、
  反対側から到達する。
- **`--force` は残すが黙って通らない。** 見えない出口は、安全判定を儀式に変える。

既存の `registry/mux/*.json` は `tab_id` を持つので、`pane_record_status()` は
`handle` が無ければ `tab_id` を読む。**merge 後も本番は自分を操作できる** —
稼働中の herdr デーモンで実機確認済み (本番チェックアウトからは `match`/`mine` で
破壊可、worktree からは `foreign` で拒否)。tmux モードで既に走っているデーモンには
記録が無いので、一度だけ `--force` が要る。

> 既定を拒否に反転する変更は、**本番が自分を操作できることを実機で確認する**まで
> 終わりではない (memory: two-guard-layers-need-one-notion-of-self)。

回帰: `tests/test_pane_provenance_guard.py`。欠陥を 8 通り戻して赤を確認済み。
うち 1 件 (P1-1) は最初**緑のままだった** — ペイン単位で訊いていたため、同じ変更に
含まれる別の修正が先に `UNKNOWN` を返して欠陥を隠していた。確かめたい層に直接
当て直して赤にした (memory: red-proof-catches-tests-green-for-the-wrong-reason)。

### 7-11-3. 記録が「自分のもの」であることまで証明する (t041, Codex 6 巡目)

§7-11-2 で土台を「自分が作った記録」に移した。6 巡目の P1 4 件は、**その土台を
否定していない** — 4 件とも「**その記録が本当に自分のものだと証明できていない**」
という詰めである。記録という考え方は正しく、記録の*束縛*が足りていなかった。

| # | 記録が自分のものだと言えない理由 | 倒れる方向 |
|---|---|---|
| 1 | ペインの root プロセスを無条件に「自分のシェル」とみなしていた | 空 → 破壊可 |
| 2 | 記録の中に**どのチェックアウトが書いたか**が無い | 他人の記録 → 破壊可 |
| 3 | 記録の中に**どの mux サーバーの何世代目か**が無い | 古い id → 破壊可 |
| 4 | 記録する id を、作成後に**名前で引き直して**いた | 出自の偽造 |

**1. 空だと宣言する前に、root が idle なシェルであることを確認する。**
`_pane_recognition()` は `others` から `pane_pid` を無条件に引いていた。root を
除いてよいのは root が本当にそのペインのシェルのときだけで、シェルが解決できない
別名経由でデーモンを `exec` していれば **root こそが中の人**である。分類器がその
argv を認識できなければ `others` は空、結論は「確実に空」— 記録が 1 つも無くても
破壊が認可される。root がシェルかどうかは argv を認識できるかとは**別の事実**
なので、別に、積極的に確かめる。`spawn()` が husk 判定に使うのと同じ述語
(`_pane_shell_state`) を使うので、「再利用してよい husk」の定義が 2 つに割れない。

**2. 記録を、書いたチェックアウトに束縛する。** ファイルを `_own_repo_root()` の
下に置くのは**置き場所**の隔離であって**中身の出自**ではない。2 つの worktree が
`registry/mux` を symlink で共有すれば置き場所の隔離はそもそも成立しないし、記録を
コピーすれば付いて回る。`checkout` をファイルの中に書いて照合する。

**3. 記録を mux サーバーのエンドポイントと世代に束縛する。** tmux の `@window_id`
は**1 つのサーバーの生存期間内でしか一意でない**。記録は window の外部破壊もサーバー
停止も越えて残り、新しいサーバーは同じ `@7` を別の window に配れる。別ソケットにも
同じ id はある。tmux は `#{socket_path}` と `#{pid}` (サーバー pid = 世代)、herdr は
API ソケットのパスと `SO_PEERCRED` が名指すサーバープロセスの `pid:starttime`。
いずれもペインの pid と**同じ 1 回の問い合わせ**から取る。

**4. window id は作成コマンドの出力から取る。** `new-window -P -F '#{window_id}'`
/ `new-session -P -F '#{window_id}'`。作成と `send-keys` のあとに名前で引き直すと、
その間に別チェックアウトが同名の window を作ったり rename したりでき、**他人の
window を「自分が作った」と記録**してしまう。起動コマンドの送信先も、記録する id も、
以後すべて**作成が返した id** を使い通す。id が取れなければ**名前に戻らず失敗する** —
戻る先が、まさに閉じた穴だから。husk への再投入も同じで、`_inspect_pane_full()` が
返した id をそのまま持ち回る。

#### 可用性 — 永久に kill できない状態を黙って作らない

出自もサーバーも持たない**旧形式の記録**は、この変更をまたいで稼働している
デーモンが全部持っている。一律に拒否すれば、そのペインは二度と終われない。
永久の拒否は安全な既定ではなく、別の障害である (memory: fail-closed-discard-vs-hold)。

- 記録の置き場所が**共有されていない**とき (`registry/mux` が自分のルートの
  直下に解決する) だけ、旧形式を受け入れる。置き場所がまだ弱い証拠として効く。
  **必ず stderr に警告を出す。** 次の spawn で新形式に置き換わる。
- 共有ストレージ (symlink 等) では拒否する。どちらが書いたか決めようがない。
- `server` を持つ記録に対して現在のサーバーが不明なら拒否する。`--force` が出口。

herdr サーバー再起動後の husk は、id が変わって記録が合わなくなるが、root が素の
idle シェルなので「確実に空」の側で片付く — t035 の復活経路はそのまま。

回帰: `tests/test_spawn_record_binding.py` (20 本)。4 件それぞれについて**欠陥を
戻すと赤くなることを確認済み**。加えて実機 tmux 3.4 で spawn → 記録 → 世代改竄で
拒否 → 出自改竄で拒否 → 正規の記録で kill → husk 再投入まで通してある (15/15)。

> 偽の tmux が「聞かれた書式」ではなく「決め打ちの並び」を返していたせいで、
> 赤いはずのテストが 1 本**緑のまま**だった。書式駆動に直して赤を取り直した
> (memory: red-proof-catches-tests-green-for-the-wrong-reason)。

### 7-12. 「わからない」を Yes/No に潰さない (t037, Codex 2 巡目)

§7-3 で `/proc` 走査について書いた「見られなかったは居なかったではない」は、
同じ形の欠陥がこの PR の中にあと 3 つあった。3 つとも**同じ述語に正反対の
安全側を求めている**のが正体である。

1. **`read_pause()`** — 不在・EISDIR・壊れた JSON を全部 `None` にしていた。
   `_decide()` はそれを「停止マーカーは無い」と読み、**maintenance の真っ最中に
   respawn** できた。`read_pause_state()` が `absent` / `active` / `unreadable`
   を返し、`FileNotFoundError` だけが不在の証拠。読めなければ hold。

2. **`_pane_has_live_process()`** — 読めなければ `True`。「ここに起こしていいか」
   (占有判定) には正しいが、`_wait_until_launched()` の「起動したか」には正反対で、
   コマンドが飲まれたうえにペイン照会も失敗すると **spawn が即座に成功を報告**し、
   猶予と flap カウントを消費して検証していない復旧を宣言していた。
   `PANE_IDLE` / `PANE_LIVE` / `PANE_UNKNOWN` の 3 値にし、占有判定は unknown を
   busy に、起動確認は unknown を not-started に倒す。`_pane_shell_is_idle()` は
   `state == PANE_IDLE` の wrapper なので、答えは 3 値化の前後で完全に一致する。

3. **`resume()`** — ロックの外で「読む → token 照合 → unlink」をやっていた。
   1 と 3 の間に別の maintenance がマーカーを差し替えると、**照合した token と
   消したファイルが別物**になり、実行中の maintenance の保護が外れる。§7-10 の
   ロックに入れた。取れなければ `False` — 直列化できないなら保護は外さない。

おまけで **`spawn_command()` のクォート漏れ**。env の値だけクォートして
`repo_root` とスクリプトパスを生で `'...'` に埋めていたので、`/home/o'brien/`
の checkout でクォートが閉じ、**続くメタ文字がコマンドとして走る**。危険なのが
「悪意ある入力」ではなく**ただの人名**だったのが教訓で、全ての補間を
`_sh_single_quote()` に通した。

回帰: `tests/test_daemon_watch_failclosed.py`。4 番の赤は
`cd: .../obrien; touch PWNED; /crewvi...: No such file or directory` と出て、
パスがクォートを破ったことがそのまま読める。

### 7-9. 回帰テストの形

`tests/test_daemon_mutual_watch.py`。観測の口 (`mux` / `/proc` 走査 / 時計 /
自己同一性) をすべて注入可能にしてあるので、本番のデーモン・mux・registry を一切
巻き込まずに判定表を全通りたどれる。タスク要件の 3 本柱は:

- 片方を落としたら相手が起こす — `test_dead_peer_is_respawned`
- 生きている相手は起こさない — `test_stuck_but_alive_peer_is_not_respawned` ほか
- flap で止まる — `test_flap_guard_stops_respawning`

加えて、合成 `/proc` では証明できない部分 (実プロセスの argv 配置、bash と python の
starttime の数え方の一致) は、使い捨てディレクトリに sleep するだけの
`scripts/dispatcher.sh` を置いて実プロセスを起動・kill する隔離 smoke で確認した。

---

### 7-13. 相互監視が機能しない瞬間の backstop — PostToolUse hook (t008, 2026-09-23)

§7 の相互監視は「相手を見る」仕組みなので、**両方が同時に死ぬ**(herdr 再起動、OOM 等) ケースは
原理的に救えない — 見る側も死んでいるから。これを補うのが Director 自身のセッションで動く
PostToolUse hook (`hooks/post-tool-use.sh`) の役目である。

**方針。** 相互監視の判定 (`DaemonWatch._decide()`) を再実装・再利用しない。hook は `.claude/settings.json`
の `PostToolUse` matcher (`Bash|Write|Edit|MultiEdit`) の経路にあり、失敗やハングが全体に波及するため、
判定は「heartbeat ファイルの mtime を見るだけ」に絞る (`instance_alive()` の /proc 照合や
`scan_daemon_pids()` の走査はしない — それは respawn する側の相互監視の仕事であり、この hook は
respawn しない・報告するだけ)。

- しきい値は §7-8 の既定値 (60 / 240) をハードコードし、`lib_daemon_watch.py` と同じ env var 名
  (`CREWVIA_DAEMON_DISPATCHER_STALE_SECONDS` / `CREWVIA_DAEMON_WATCHDOG_STALE_SECONDS`) でだけ
  上書きを許す。`config/crewvia.yaml` の YAML 解析はこの hook の目的には重すぎるため行わない —
  独立した簡易チェックであり、`lib_daemon_watch.py` の判定とバイト単位で一致する必要はない。
- `registry/daemons/` ディレクトリが無い (= どちらのデーモンも一度も `beat()` していない) 場合は
  判定に入らずスキップする。このディレクトリは `DaemonWatch.__post_init__` が最初の beat 時に
  作るものなので、無いことは「デーモンが動いていない (standalone/inline 運用)」の証拠であり、
  「両方死んでいる」の証拠ではない。ここをスキップしないと、mutual watch を使わない運用で常時
  誤検知することになる。
- **判定順 (t049)。** 安いものから順に並べ、状態を消費するもの (throttle) を最後にする:
  1. `registry/daemons/` の存在確認 (最も安い。現在の本番では常にここで抜ける)
  2. heartbeat の mtime 判定 (`stat` のみ。両方 stale でなければここで抜ける)
  3. role の解決 (`registry/workers.yaml` を読む python3 サブプロセス。**両方 stale のときだけ**
     走るので、通常運用 (両方健全) では 1 度も走らない)
  4. throttle の判定と消費 (role が director と判明した呼び出しだけが行う。Worker は
     一切消費しない)
- 対象は role が director のセッションのみ。全 Worker のツール呼び出しにも同じ hook が刺さるが、
  Worker には respawn も報告もできないので実際の通知は行わない。ただし role 自体の解決は、
  **両方の heartbeat が stale と分かった呼び出しでは Worker であっても実行される** — 「誰が
  呼んだか」は throttle より先に確定させる必要があるため。この分岐に入るのは mutual watch を
  使っていて、かつ両デーモンが実際に stale な (= backstop が意味を持つ) 稀な状況に限られる。
  **t008 原案 (2026-09-23) は role 解決を daemons/ の存在や throttle と無関係に毎ツール呼び出しで
  走らせており、Director だけでなく全 Worker の全呼び出しが python3 起動コストを払っていた
  (t047 で O-1 是正のため grep から python3 ヒアドキュメントに変わったのが引き金。実測
  12ms→38ms、3.2倍。t009 2巡目 QA の F-3)。t048 で daemons/ の存在確認と throttle 判定を role 解決
  より前に出したことで F-3 は閉じたが、その並べ替えは throttle マーカーの消費まで role 解決の
  手前に動かしてしまい、新しい欠陥 (下記 F-4) を生んだ。t049 で throttle だけを role 解決の後段
  (director のときだけ触る場所) に戻し、F-3 の是正 (daemons/ 不在なら python3 ゼロ) を保ったまま
  F-4 を閉じた。**
- throttle はマーカーファイル (`registry/daemons/backstop-notify.throttle`) の mtime で 60 秒に
  1 回に抑える。マーカーの更新は「daemons/ あり かつ 両方 stale かつ director かつ 窓が開いている」を
  全て通った後にしか起きない (t049。判定より先にマーカーを更新すると F-4 を再発させる)。
  通知を出す呼び出しがマーカーを更新するので、同時に複数の PostToolUse が走っても
  直後の呼び出しは早期リターンする (完全な排他ではないが、この hook にロックを持ち込むほどの
  重さではない — 最悪でも throttle 窓の中で数回検知メッセージが重複するだけで、実害は無い)。
  **このマーカーは全エージェント共有 (agent 別ではない) だが、role が director の呼び出ししか
  触らない (t049)。** Worker のツール呼び出しは role 解決までは行うが、throttle の判定・消費には
  一切踏み込まないので、Worker が並行して動いていても Director の通知窓を奪わない。

  **t048 での事故 (F-4, t009 3巡目 QA, 修正済み)。** throttle 消費を role 解決の**前**に置いた
  結果、マーカーが全エージェント共有のまま「最初にこの窓を触った呼び出し」が誰であるかに
  関わらず消費されるようになり、Worker のツール呼び出し 1 回で Director の窓が丸ごと潰れていた。
  実測では「次の窓まで遅れる」ではなく、Worker が先に呼ぶ限り**恒久的に**Director へ届かない
  (連続 5 窓で到達 0/5)。backstop が意味を持つのは Worker が動いている並列モードだけなので、
  この欠陥は実運用条件下で要件 1 (両デーモン停止時に Director に通知が届くこと) を満たさなかった。
  複雑な per-agent throttle (`backstop-notify.<AGENT_NAME>.throttle`) を導入する案もあったが、
  上記の判定順の並べ替え (throttle を director 専用の最終ゲートにする) だけで十分に閉じたため
  採用しなかった。

**Director への伝え方 — exit code 2 を使う。** Claude Code の PostToolUse hook は exit code 2 で
終わると、ツールは既に実行済みのままブロックはせず、**stderr をそのまま呼び出し元 (Director) の
文脈に見せる**。これはポーリングさせずに「Director が何か操作した拍子に勝手に届く」を実現する
標準的な方法である。

既存の crash guard (`trap '_crash_guard' EXIT`) は非ゼロ終了を全部 0 に握り潰す設計だったので、
そのままでは意図した exit 2 も握り潰されてしまう。`_INTENTIONAL_EXIT_CODE` という変数を挟み、
crash guard は「予期しないクラッシュ (`_INTENTIONAL_EXIT_CODE` と食い違う非ゼロ終了)」だけを
警告付きで 0 に収束させ、`_INTENTIONAL_EXIT_CODE=2` をセットした意図的な経路はそのまま通す形に
した。crash guard 自体の「失敗しても Worker/Director の動作を止めない」という不変条件は変えて
いない — 意図的な exit 2 はツール実行を止めない (PostToolUse は事後フックなので、そもそもブロック
する権限が無い)。

**respawn はしない。** この hook が見つけたら唯一やることは「1 行出す」だけで、`mux.spawn()` は
一切呼ばない。理由は 2 つ: (1) Claude Code の hook はタイムアウトに敏感で、mux の subprocess 呼び
出しを混ぜると全ツール呼び出しの体感速度が悪化する、(2) respawn の判断 (flap ガード・pause
マーカー確認・pane owner 確認) を省略した簡易実装で行うと、§7 が積み上げた fail-closed の設計を
迂回する非公式な第二の respawn 経路になってしまう。Director が `status` で裏を取ってから
`lib_daemon_watch.py restart` を手で打つ、という一段人間を挟む設計にした
(`agents/director.md` §14「両デーモンの同時死 backstop」)。

#### 回帰テストの形

`tests/test_daemon_backstop_hook.py`。**本物の `hooks/post-tool-use.sh` を subprocess で実行**する
— ロジックを Python で再実装したテストは hook を直したことを一切証明しないため使わない (§6-5 の
教訓と同じ形)。両方 stale で exit 2 になること、片方だけでは発火しないこと、director 以外の
role では発火しないこと、throttle が効くこと・窓が空けば再発火すること、`registry/daemons/` が
無い (mutual watch 未使用) 環境で誤検知しないこと、しきい値が env var で上書きできることを
それぞれ担保する。RED は、fix 前の `hooks/post-tool-use.sh` (git HEAD) に対して同じテストを
流し、4 本が意図通り fail することで確認した。

**t049 で追加した 4 本。** F-4 (Worker が窓を消費する) の回帰防止として、Worker が 1 回呼んだ
直後に Director が呼んでも通知が届くこと、連続 3 窓すべてで Worker が先に呼んでも Director が
毎回届くことを固定した。O-9 (t048 の並べ替えを守る回帰テストが無かった) の是正として、
`registry/daemons/` 不在時と、両デーモンが健全 (fresh) な時に role 解決の python3 が
1 回も起動しないことを、PATH に計数スタブを挿して固定した。4 本とも t048 (`d1bcead`) に対して
3 本が意図通り fail する (`test_healthy_daemons_never_invoke_python3` は daemons/ 不在の分岐が
t048 の時点で既に成立していたため元々 green) ことを確認済み。

### 7-14. pane が消えても記録が残る — 失効記録の掃除 (t001, 2026-09-25)

`registry/mux/<name>.json` は「この checkout が pane を作った、その id」の記録で、§7-11-2 以降は
**kill の認可の唯一の証拠**である。書くのは `spawn()`、消すのは `kill()` だけだった。だから
`kill()` を通らずに pane が消えると、記録だけが残る。

**発生源 (実測)**: Worker の retirement は pane の shell pid に SIGTERM → SIGKILL を送るだけで
`mux.kill()` を通らない (`lib_retirement.py` `_step_sigterm` / `_step_sigkill`)。mux は 0.2 秒以内に
pane を自分で閉じるが、記録を消す者がいない。隔離 herdr で `spawn` → shell pid を `kill -9` して
再現した (pane は消え、記録は残る)。着手前に本番で見つかった 5 件の失効記録
(Ren / Haruto / Wei / Seo / Arjun) は、いずれも retire 済みの Worker だった。

**症状 (a)「already running と言われるのにペインが無い」の真因は特定できていない。**
記録が原因ではないことは確かめた — `spawn()` も `list()` も記録を読まない (herdr は
`pane list` をラベルで、tmux は `list-windows` を名前で引く)。失効記録を置いたまま
`spawn` すると新しい pane が作られ、記録は上書きされる。隔離 herdr で、正常な close /
server 再起動 (husk として復元) / retirement 相当の SIGKILL の 3 経路を試したが、どれでも
拒否は再現しなかった。本番の `logs/{dispatcher,watchdog,kai-spawn}` と `registry/daemons/` にも
該当のイベントは無い (`start.sh` の「already running」は端末に出るだけでログに残らない)。
**実際に再現できたのは別の誤報**である: 空の pane への再起動が定着しなかった
(`_wait_until_launched` が偽) とき `spawn` は False を返し、名前は `list` に出続けるので、
`start.sh` は「already running」と言っていた。これは (a) の一因になりうるが、7 回の発生が
これだったとは確認できていない。

**掃除の契約** (`lib_mux.reap_stale_pane_records()` — 判定はここ 1 箇所):

- **消してよいのは、mux が「その id は無い」と明確に答えたときだけ。** herdr は失敗を全部
  `{"error": ...}` で返し、server 不達も `server_not_running` としてその形で来る。
  `pane_not_found` 以外 (不達・timeout・未知のエラー・空応答) は「観測できなかった」で、
  記録を残す。消すと次の kill が恒久拒否になる (`knowledge/empty-vs-unobservable.md`)。
  従来の `_resolve_pane_id()` / `_resolve_ids()` は「error がある = 消えた」と読んでいて、
  **herdr の停止中に呼ばれただけで記録が消えた**。`HerdrBackend._pane_existence()` に集約して直した。
- **名前ではなく、記録が指す id を、記録の server の世代に束ねて問い合わせる。**
  - **束縛 (Kai 2巡目 P2-2)**: herdr の `pane.get` は、`SO_PEERCRED` で相手の世代を確かめた
    **その接続の上に** 流す (`_herdr_pane_get_bound()`。`_herdr_close_tab_bound()` と同じ形)。
    初版は `server_identity()` を sweep で 1 回取って接続を閉じ、`herdr pane get` (CLI) が
    別の接続を張っていた。**検証と問い合わせが別の呼び出しなら、別の server でありうる**
    (§7-11-1、memory `verify-and-destroy-must-share-one-connection`) — herdr が sweep の途中で
    再起動すると、後継 server は旧 id すべてに `pane_not_found` を返し、契約に反して記録が消えた。
    いまは記録の endpoint + generation と違う世代には**何も尋ねず** (`None` = 観測できず)、
    記録は残る。識別子は **記録ごとに取り直す** (全記録で使い回すキャッシュは窓を広げるだけ)。
    「無い」の判定は `_pane_existence()` 1 か所、束縛付きの `pane.get` を送るのは
    `_herdr_pane_get_bound()` 1 か所 (構造テストが数える)。
  - **rename (QA F1 → Kai 2巡目 P2-1)**: id が引けて、ラベルが期待と違う pane は「消えた」
    ではない。**同じ server の世代・同じ pane id・記録が持つ tab id が一致すれば `PANE_RENAMED`**
    (場所で同定する。名前ではない)、tab が違う / 記録に無いなら `PANE_UNOBSERVED`。
    どちらも記録を残す。初版 (t022) は UNOBSERVED にして記録を残したが、解決経路
    (`_resolve_ids`) は UNOBSERVED を「label で引き直す」と読み、**rename された pane は label では
    もう見つからない**ので capture / send / pid / `kill --force` のどれも届かず、孤児化は
    解決していなかった (記録を残すだけでは足りない)。
  - **解決の順序**: ① 記録 (server が「その label で居る」と答えたとき) → ② label (live の
    `pane list`) → ③ 記録 (`PANE_RENAMED` のとき)。**③ は label で何も見つからないときだけ**。
    label が別の pane を指すなら、従来どおりその pane が答えで、kill の認可は id を記録と突き合わせて
    **handle の食い違いを拒否する**。名前 → pane の意味は変えない。
  - **kill の認可は緩めていない**: 記録の id で pane に届くようになっても、別 checkout の記録 /
    別世代 / handle 食い違いは `pane_record_status()` が拒否する。別世代の記録は、そもそも束縛付きの
    問い合わせが尋ねないので解決にも使われない。空のラベルは不一致と見ない (tab は作成後に
    ラベルが付く)。
  - tmux は `list-windows -a -F '#{pid} #{window_id}'` の 1 回の問い合わせで、id を発行した世代の
    一覧であることまで確かめる (問い合わせ自身が世代を運ぶので、別の接続の問題が無い)。
- **迷ったら残す**: 自分の checkout の記録でない / server 束縛が無い / 別の endpoint・世代 /
  120 秒より若い / 年齢が読めない / 判定と削除の間に書き直された / **ラベルが違う** /
  **問い合わせの接続が、記録の世代でなかった** / **書き手を排除するロックが取れない**。
- **比較と削除は書き手と同じロックの一区間** (Kai P1 / PR #215 の追加コミット)。初版は
  「判定した記録を読み直して同じか確かめ、そのあと unlink」を 3 つの別操作で行っていて、
  比較の後・unlink の前に `spawn()` が新しい記録を書くと、**掃除が生きた pane の記録 (=kill の
  唯一の認可) を消せた**。120 秒の猶予は古い記録を見るだけで、差し替えを防がない。いまは
  `registry/mux/.records.lock` (記録と同じディレクトリなので、symlink で `registry/mux` を共有する
  checkout 同士でも共有される) を **`write_pane_record()` と `drop_pane_record()` が取り**、
  掃除は `drop_pane_record(expect=<判定した記録>)` で「ロックの中で、ディスク上の記録が判定した
  ものと同じときだけ」消す。ロックの中で mux を呼ばない (書き手を待たせるので、問い合わせは
  ロックの前に済ませる)。取れなければ、掃除は消さず、書き手は「記録できなかった」と返す
  (どちらも既存の安全側の分岐)。待ちは有限 (`PANE_RECORD_LOCK_TIMEOUT_SECONDS` = 5 秒)。
  同じ先例は `registry/task-graph` の pending ロックと `retire_assignment` の card 書き換え。
- **記録を消す判断は、すべて「判定した記録」を渡して消す** (Kai 3巡目 P2-1)。ロックは unlink を
  直列化するだけで、**古い判断を新しくはできない**。上の掃除は `expect=` にしたが、解決経路
  (`_resolve_ids`) が素の `_delete_cache(name)` のまま残っていて、「無い」の答えが返る途中で
  spawn が書いた置き換えの記録を消せた (1 巡目 P1 と同じ構造の 2 か所目)。いまは観測に基づく
  削除がすべて `_forget_record(name, judged)` (= `drop_pane_record(name, expect=judged)`) を通る。
  全経路の棚卸し (`drop_pane_record` の呼び出し元。構造テストが表で固定する):

  | 経路 | 消す理由 | 形 |
  |---|---|---|
  | `reap_stale_pane_records` | mux が「その id は無い」と答えた | `expect=` (判定した記録) |
  | `_resolve_ids` | 解決の途中で「無い」と答えが返った | `_forget_record(name, cached)` |
  | `TmuxBackend.kill` (2 か所) / `HerdrBackend.kill` (2 か所) | pane を壊した | `_forget_record(name, judged)`。kill の判断時点の記録 |
  | `write_pane_record` (サーバー特定不能の spawn) | 直前の記録は、この spawn が置き換えた pane のもの | **無条件のまま**。古い観測に基づく判断ではなく、spawn 自身がこの名前の持ち主になった事実による |

  `judged` が無い (判断時点で記録が無かった) なら何も消さない — その後にあるのは、判断のあとに書かれた
  別の spawn の記録である。`_delete_cache` は廃止した (名前だけで消す入口を残さない)。無条件に消せる
  呼び出しが 1 か所を越えて増えたら、構造テスト (`test_every_path_that_drops_a_record_goes_through_expect`)
  が落ちる。
- **掃除には時間予算がある** (Kai 3巡目 P2-2)。sweep は dispatcher の cycle の中で同期に走り
  (相互監視は 60 秒の heartbeat で見る)、`start.sh` は spawn の前にその完了を待つ。初版は
  eligible な記録を 1 件ずつ、1 件 5 秒の timeout で全部問い合わせ、全体の上限が無かった。
  **接続は受けるが返事をしない herdr** だと 5 秒 x 記録数を毎 cycle 待ち (記録は残るので毎 cycle
  繰り返す)、本番の 13 件で 65 秒 — **掃除のせいで相互監視が dispatcher を「死んだ」と判断して
  respawn し、Worker の起動も遅れる**。付加機能の失敗が、倒してはいけない側 (dispatch) に倒れて
  いた。倒す先は「失効記録が数秒長く残る」でなければならない (`registry/task-graph` の publish が
  待ちを有限にして、待てなければ手を引くのと同じ判断)。両方入れた:
  - **全体の予算** `STALE_SWEEP_BUDGET_SECONDS` (5 秒)。使い切ったら以降は何も問い合わせない。
    1 件ごとの問い合わせも「残り」に切り詰める (`min(STALE_SWEEP_QUERY_TIMEOUT_SECONDS, 残り)`)。
  - **`PANE_UNANSWERED` で打ち切る**。問い合わせたが答えが返らなかった (herdr は `no_answer`、
    tmux は `TimeoutExpired`) 記録が 1 件でも出たら、その周は止める。残りも同じ理由で同じだけ待つので、
    予算だけでは「答えは返すが遅い server」しか止められない一方、応答しない server では 1 件目で
    止めるほうが安い (2 秒 1 回)。予算だけだと、応答しない server で毎 cycle 予算いっぱい使う。
  記録はどちらでも残り (`UNANSWERED` は UNOBSERVED の一種)、次の周が続きを拾う。上限は
  「予算 + `server_identity()` 1 回」(問い合わせの前の絞り込みで、自前の allowance 引数を持たない。
  herdr は接続 3 秒、tmux は 5 秒)。**`start.sh` の `mux_reap_records` も dispatcher も同じ
  `Mux.reap_stale_records()` -> `reap_stale_pane_records()` なので、同じ上限に従う** (入口の
  wiring もテストで固定)。
- **kill の認可とは別経路**: 掃除は `may_destroy_pane()` を呼ばず、逆も呼ばない。掃除が消せるのは
  「消えた pane の記録」だけで、他人の pane を殺す認可には化けない。
- **書き手は増やさない**: `list()` には混ぜない (watchdog も `list()` を呼ぶ。§3 / F1)。
  呼ぶのは dispatcher (`sweep_stale_pane_records()`、`.firstseen` 掃除の隣) と `start.sh`
  (`mux_reap_records`、spawn の前) だけ。テストは watchdog 側のファイルに `reap_stale` が
  現れたら落ちる。
- 記録の削除は `drop_pane_record()` 1 箇所。テストは AST で **削除系の呼び出し**
  (`unlink` / `os.remove` / `os.rename` / `os.replace` / `shutil.move` / `shutil.rmtree` /
  `Path.replace` 等と `unlink_quiet` 系の helper) を、`lib_mux.py` だけでなく
  **`dispatcher.sh` / `watchdog.py` の埋め込み python、`lib_retirement.py` /
  `lib_daemon_watch.py` まで**全部拾い、理由付きの表 (`ALLOWED_DELETIONS`) に無いものがあれば
  落とす (QA F2)。初版は `lib_mux.py` 内の `unlink/remove/rmdir` と `"pane_get"` リテラルにしか
  効かず、`dispatcher.sh` に足した `os.unlink(registry/mux/*-worker.json)` は全 942 件が緑だった。
  **塞げないもの (AST の限界)**: 変数・連結を経由した名前 (`op = "pane" + "_get"`)、
  `getattr(os, "unl" + "ink")`、外部コマンド (`rm`)、`open(path, "w")` での上書き。
  red proof は連結の注入 (I2) が**緑のまま**であることをそのまま記録している。
- 別 PR の走査と互いの新コードを拾い合うことがある (t031): `dispatcher.sh:save_told` の
  `os.replace(tmp, TOLD_FILE)` (notified-state 台帳の原子的書き込み) は `ALLOWED_DELETIONS` に
  理由付きで 1 行足して閉じた。逆向きの `lib_mux.py:_herdr_pane_get_bound` の `json.loads` は
  `knowledge/notify-once.md` §3 の `ALLOWED_JSON_PARSES` 側 (E 区分)。走査の範囲は緩めていない。

**`spawn` の終了コード**: 0 = 起動した / 1 = 起動しなかった / **10 = pane に live なプロセスが居る /
11 = pane の中身が読めず busy 扱い**。`start.sh` が「already running」と言うのは 10 だけ
(11 は「読めなかった」、1 は「起動が定着しなかった」または純粋な失敗)。dispatcher / watchdog の
起動 (`lib_daemon_watch.py spawn`) は別経路で、ここでは変えていない。

**ロールバック**: 停止スイッチは `CREWVIA_MUX_RECORD_SWEEP=0` (掃除が何も見ず何も消さない)。
**止まるのは掃除だけ**で、記録の書き手側のロック (`.records.lock`) は常に働く。掃除は
「消す側」の 1 者 (dispatcher と `start.sh` が同じ関数を呼ぶ) で、片方だけ止めても、答えが割れるのは
「どれだけ掃除されるか」であって「どの記録が正しいか」ではない — 共有規則 (`lib_dep_rules` 等) と
違い、食い違っても危険な側には倒れない。dispatcher は cycle ごとに新しい python なので、
`dispatcher.sh` の env に足して再起動すれば効く。`start.sh` は env をそのまま読む。
掃除が誤って記録を消したときの復旧は、その pane を respawn する (記録が書き直される) か、kill に
`--force`。mux の停止中は掃除が何も消さないので、停止中に呼ばれること自体は安全。
revert するなら `dispatcher.sh` の `sweep_stale_pane_records()` 呼び出しと `start.sh` の
`mux_reap_records` を外せば掃除は止まる。**ロックだけを戻したい場合**は `_pane_record_lock()` を
外すのではなく PR ごと revert する (書き手と掃除のどちらか一方だけがロックを取る状態は、
元の競合より悪い — 掃除だけが取れば書き手を止められず、書き手だけが取れば何も排除しない)。
ロックが取れない環境 (記録のディレクトリに `.records.lock` を作れない) では、書き手は
「記録できなかった」と警告して False を返し、掃除は何も消さない — 記録が書かれないと後の kill は
`--force` が要るが、これは §7-11-2 の既存の安全側 (記録が無ければ壊さない) と同じ向き。
ロックファイル自体は残っていても害は無い (flock はプロセス終了で解放される)。
**merge 後、dispatcher の再起動が要る** (`knowledge/dispatcher-restart-after-merge.md`)。

**残した穴**: 別世代の server に対する記録 (server が再起動した後の記録) は掃除しない。
その id は別のものかもしれず、判定の根拠が無い。同名の pane が spawn されれば上書きされる。
`<name>.state.json` / `.firstseen` は dispatcher 自身の別の印で、この掃除の対象外。

**見送ったもの (QA F3 / P3)**: herdr の server 停止中に `pid` / `capture` 等が `pane not found` と
表示する。記録は正しく残る (動作は正しい) が、利用者には「pane が無い」と「server に問い合わせ
できなかった」が同じ文言に見える。直すには `_resolve_ids()` が「尋ねられなかった」を返り値で
運び、6 か所の警告文 (send / capture / kill / pid / attach / state) を分ける必要がある。表示だけの
変更に対して PR が大きく、レビュー機構の上限に近いため、別 PR に回した。

**start.sh の bats は使い捨て checkout で走る** (Kai P1-2): `start.sh` は自分の位置から REPO_ROOT を
決めて `.claude/settings.local.json` と `registry/` を書くので、実 checkout で走らせて teardown で
無条件に消すと開発者の設定を壊す。setup が作業ツリー (未コミット変更を含む) を複製し、teardown が
消すのはその複製だけ。teardown は実 checkout の `settings.local.json` / `workers.yaml` /
`registry/mux` が変わっていないことも assert する。

回帰テスト: `tests/test_stale_pane_record_sweep.py`、`tests/start-sh-spawn-refusal.bats`。
欠陥を戻すと赤くなることは `tests/red_proof_t001_stale_records.sh` (35 種 + 既知の限界 1) で実証した。
3 巡目 (t030) の分は `tests/test_sweep_budget_and_conditional_drop.py`: 「無い」と答えが返る途中の置き換え
(解決経路・両 backend の kill)、記録を消す全経路の構造テスト、応答しない / 遅い server に対する
sweep の上限 (偽の clock で時間を進める。実時間に頼らない。dispatcher の埋め込み関数と
`Mux.reap_stale_records()` を実コードのまま動かす)、本物の unix socket の「応答しない server」。
2 巡目の分は `tests/test_renamed_pane_and_bound_existence.py` (実プロセスの 2 世代 + 実 unix socket。
世代の違いは SO_PEERCRED が答える本物) と、隔離 herdr での実機確認 (rename 後の
capture / pid / send / `kill --force` の到達、sweep 中の server 再起動で記録が残ること)。
「最終比較の後に spawn が書き直す」窓は実時間では再現できないので、比較の直後に別スレッドで
`write_pane_record()` を走らせて窓を作る (ロックが無ければ書き手はすぐ書いて掃除に消され、
あれば掃除が終わるまで待つ)。

### 7-15. pytest が作った宛先を終わるときに片付ける (mission 20260925-pytest-workspace-leak / t001 / backlog #15)

§7-11 の隔離は、pytest セッションごとに `crewvia-pytest-<pid>-<8 桁 hex>` という宛先を作って
`CREWVIA_HERDR_WORKSPACE` / `CREWVIA_TMUX_SESSION` をそこへ向ける。**この隔離は正しい**。
足りなかったのは後始末だけで、herdr は初回アクセスで workspace を自動作成するのに、conftest が
終了時に閉じなかった。pytest を 1 回まわすたびに空の workspace が本番 herdr に 1 つ残り、
2026-09-25 には 50 個になって `herdr pane list` が 50KB を超えた。

**実測 (修正前 = origin/main、label 集合の差分)**: 漏れのあるテスト 1 本でも、`tests/` 全体
(1648 件) でも、実行後に自分の `crewvia-pytest-<pid>-<hex>` が **1 個** 残る (pane は idle な素の
シェル 1 つ)。**tmux は漏れない**: 同じ全体実行の前後で `tmux ls` に差が無く、tmux の実行層
(`new-session`) を呼ぶ経路は `lib_mux.TmuxBackend` だけで、テストの偽 tmux はそこへ届かない。
ただし判定ロジックは共有し、実行層だけ差し替える形で tmux 用の後始末も入れてある (将来漏れたときのため)。

**直し方** (`tests/pytest_workspace_sweep.py`、conftest の `pytest_unconfigure` から 1 回呼ぶ):

| 対象 | 何を閉じるか | 根拠 |
|---|---|---|
| 自分の宛先 | label が **完全一致** する workspace / session だけ | 自分が作った名前。中身も自分のテストが作ったもの。空かどうかは問わない |
| 残骸 | `crewvia-pytest-<pid>-<hex>` の形 **かつ** pid が死んでいる **かつ** 中に live な pane が 1 つも無い | 3 つの AND。どれか 1 つでも欠ければ残す |

`pytest_sessionfinish` でなく `pytest_unconfigure` なのは、collection エラーや中断でも走るため。
残骸掃除を**開始時でなく終了時**に置いたのは、開始時に置くと最初のテストが掃除のぶん遅れるため。
**このフックは `--collect-only` / `--help` でも走る** (QA が subprocess を記録して確認: herdr への
一覧の取得 2 回だけで書き込みは無い。自分の宛先は作られていないので閉じるものも無い)。
SIGKILL された回 (watchdog の timeout 等) は終了フックが走らないので、次に正常終了した回が拾う。

**倒す向き** (`knowledge/empty-vs-unobservable.md` の作法): 破壊の根拠は「観測できたこと」だけ。
label の形も pid の生死も対象そのものの観測ではなく分類なので、単独では閉じる根拠にしない。

- **pid**: `ESRCH` だけが「死んでいる」。`EPERM` (別ユーザーの生きたプロセス)・その他の `OSError`・
  `OverflowError` は「生きている」= 残す。pid 0 は `os.kill(0, 0)` が自分のプロセスグループへの
  シグナルで常に成功するので、形の段階で対象外にする。**別 worktree で同時に走っている pytest の
  workspace は pid が生きているので残る** (空かどうかを尋ねる前に対象外)。
- **live な pane が無い**: herdr は `pane list --workspace` + 各 pane の `process-info` の `shell_pid` を
  `lib_mux._pane_shell_state()` (idle な素のシェルだと**示せた**ときだけ `idle`) に通す。tmux は
  `list-panes -s` の `pane_pid` を同じ関数に通す。pane 一覧が読めない・pane が 0 個と答えた・
  process-info が読めない・idle でない (live / 判定不能) pane が 1 つでもある → 閉じない。
- **label**: 完全一致だけ。前方一致・部分一致で閉じない。閉じる前にガード (`guard_refusal`) が、
  `crewvia-pytest-` で始まらない / 本番の宛先 (`crewvia`) と等しい label を拒否して警告 1 行を出す。
  tmux の `kill-session` は `-t =<名前>` (完全一致。`=` が無いと前方一致で別のセッションを撃つ)。
- **失敗はテスト結果に影響しない**: herdr が居ない (`herdr` が PATH に無い / socket が無い — socket は
  `CREWVIA_HERDR_SOCK` か既定の `~/.config/herdr/herdr.sock`。server を起こさないよう CLI を呼ばない)・timeout・
  CLI エラー・読めない答えは、警告 1 行で握りつぶして先へ進む。`run_cleanup()` は例外を出さない。
  tmux の「no server running」は正常な状態なので警告も出さない。
- **時間上限は 1 つの締切 (`Deadline`) で、backend の subprocess 呼び出しまで届く**: 後始末**全体**で
  30 秒 (`BUDGET_SECONDS`)、CLI 1 回は 5 秒 (`CLI_TIMEOUT_SECONDS`)。`run_cleanup` が締切を 1 つだけ作り、
  全 backend で共有する (backend ごとに作ると herdr と tmux で 60 秒になる)。各 subprocess の timeout は
  `min(5 秒, 残り時間)` で、**残りが尽きたら呼ばずに「観測できなかった」に倒す**ので、閉じずに打ち切る
  (記録は無いので次回が拾う)。以前は宛先の境目でしか締切を見ておらず、`HerdrBackend.is_empty` が
  pane ごとの `process-info` を (それぞれ最大 5 秒で) 何回でも走らせたうえ、その後の `close` も締切を
  見なかった: pane 10 個 × 応答 4 秒の偽 herdr が、30 秒の予算に対して **52 秒**かかった (Codex 指摘)。
  直したあとは同じ構成が 30 秒に収まり、workspace は閉じない (空だと示し切れていない)。
  空だと示せた直後に尽きたときも `close` は走らせない。**herdr の残骸掃除で予算を使い切ると tmux は
  何も始めない** (tmux は漏れないことを実測済みで、次に正常終了した回が拾う)。
  テストは時計を差し替えた偽 herdr で構成する (実時間に頼らない)。

**暴走時の止め方: `CREWVIA_PYTEST_WORKSPACE_SWEEP=0`** — 残骸掃除は何も見ず何も消さない。
**自分の宛先の後始末は止まらない** (自分の label だけを消すので危険が無く、止めると元の漏れに戻る)。
`CREWVIA_MUX_RECORD_SWEEP=0` (§7-14) と同じ考え方で、消す側が 1 者なので食い違っても危険な側には
倒れない。値は `0` のときだけ効く (`false` や空では止まらない)。掃除は pytest が終わるときに走るので、
止めたいシェルの env に足して pytest を起動し直すだけで効く (常駐デーモンの再起動は要らない)。
完全に外したければ conftest の `pytest_unconfigure` を revert する。

**残した穴**: SIGKILL された回の残骸は、次に**正常終了した** pytest まで残る (数が増え続けることは
ない — 次の 1 回で全部消える)。pid が再利用されて別のプロセスが同じ番号を持っていると、その残骸は
「生きている」として残る (安全側。手で `herdr workspace close` する)。

回帰テスト: `tests/test_pytest_workspace_sweep.py` (偽の herdr / tmux。本物の pytest セッションを
偽 herdr を PATH に置いて走らせる入口から出口までのテストを含む)。欠陥を戻すと赤くなることは
`tests/red_proof_t001_pytest_workspace.sh` (15 種: 12 種 + 締切が subprocess に届かない / backend ごとに
予算が戻る / 実 subprocess に timeout を渡さない) で実証した。実機の label 集合差分は PR 本文と
Result にある。

**赤の実証自身も本物の tmux / herdr に触れない** (Codex 指摘): 欠陥を注入した版の
`pytest_unconfigure` は、複製の pytest が終わるたびに**その版の後始末**を走らせる。`is_empty` の
確認や pid の生死の確認を外した版 (case E / G) が本物の tmux server (このマシンには `main` が居る)
に届けば、別 worktree で同時に走っている pytest の `crewvia-pytest-*` を kill しうる。
そこでスクリプトは、外側の変異実行にも (内側の統合テストが元々そうしていたのと同じく)
PATH の先頭に tmux / herdr のスタブ (呼び出しを記録して失敗を返す) を置き、`TMUX_TMPDIR` を空の
ディレクトリに向けて `TMUX` を外し、`CREWVIA_HERDR_SOCK` を存在しないパスにする。そのうえで
**実行の前後で本物の `tmux list-sessions` が同じ**こと・スタブに tmux が届いたこと (隔離が効いていた
証拠) ・kill-session / kill-server / herdr がどこにも届いていないことを assert する。スタブが
PATH の先頭に無ければ、注入版を走らせる前に中止する。**赤の実証を書き足すときは、注入した版が走る
場所が本番から届かないことを先に確かめる**こと。

### 7-16. needs-director は assignment を外す — 退役判定は「仕事」を card で数える (mission 20260926-mechanize-guards-a / t001 / backlog #13)

`plan.sh needs-director` は assignment を外さなかった (`cmd_done` / `cmd_fail` は外す)。dispatcher の
codex-review spawn は `queue/assignments/Kai-codex` の**有無**だけで「同時 1 実行」を判定するので、
findings を出して needs_director で止まった run の assignment が残ると、以後の codex-review が
恒久的に spawn されなかった (1 ミッションで Director が手で 15 回消した)。

単純に外すと別の事故になる: dispatcher の Rule 2 (§2-1 の D2 no-task / D3 blocked-stuck) は
`status == 'in_progress'` の card しか「仕事」と数えないので、assignment を失った Worker は idle・タスク
なしと読まれ、Director の判断を待っているだけの Worker が退役の対象になる。そこで 3 つを同時に入れた:

1. **`cmd_needs_director` が `retire_assignment(AGENT_NAME, slug, task, None)` を呼ぶ** — `cmd_fail` と同じ形
   (キューロックの中・card の書き換えと同じトランザクション・別 task を指す assignment は消さない)。
2. **Rule 2 の「仕事を持っている」を card で数える** — `worker_holds_work()`: card の `worker` が自分で、
   `RELEASED_WORK_STATUSES` (done / verified / skipped / cancelled / failed。「もう完了しない」status の名前は
   `lib_dep_rules` から取り、ここに並べ直さない) でも `pending` でもない card が
   1 枚でもあれば持っている (needs_director / needs_human_review / blocked / verifying / in_progress / 未知の
   status)。倒す先は「殺さない」(除外側を数える)。card は `load_all_tasks()` の戻り (= `lib_task_cards` の入口) を
   使い、ここで読み直さない。`TERMINAL_STATUSES` (依存が満たされた) とは問いが違うので別の集合にしてある —
   `failed` は依存を満たさないが Worker は手放している。
3. **判断待ちの Worker は busy のまま** — `worker_waits_on_director()`: assignment を外す前は「assignment がある = busy」
   が判断待ちの Worker を守っていた (新しい task を渡さない)。外した後は card がそれを言う。Rule 5-A の
   t032 F4 (needs_director の Worker に重ねて通知しない) も、根拠を assignment の中身から card に移した
   (assignment が指す task の判定も、旧版が残した assignment のために残してある)。

codex-review 側は、Kai-codex の assignment が**終わった task (`RELEASED_WORK_STATUSES`) か needs_director を指す孤児**なら
spawn を塞がない (`codex_review_slot_busy()`)。dispatcher は**読むだけ**で、assignment を消すのは plan.sh の役目
(次の pull が上書きする)。塞ぐ側に倒すもの: assignment が読めない / `<mission>:<task>` の形でない / 指す task が
見つからない (archive 済みなど) / 進行中の status — 孤児と**証明できない**ものは孤児と扱わない (誤って 2 つ目の
run を走らせるより、Director に見える停止のほうが安い)。plan.sh が外す (1) と dispatcher が塞がない (孤児判定) は
二重の防御で、片方だけでも codex-review は止まらない (旧版の plan.sh が残した assignment・archive 前に取り残された
assignment にも効く)。

**task-graph への影響 (同じ PR で塞いだ)**: pane_match / pane_id は「card の status の allowlist」と「assignment が
この task を指している」の AND だった。needs_director は assignment を外すので、そのままだと Director がいちばん
ペインに飛びたい node だけが実運用で pane_match を失う (fixture は assignment を置くので既存テストでは見えない)。
`task_graph_assignment_holds()` は `needs_director` に限り assignment が**本当に無い** (ENOENT) ときも許す。読めない
assignment は「無い」ではないので許さない。代償: 名前を使い回した別の Worker が idle で居ると、その pane を指しうる
(別の task に就いていれば assignment の指す先が違うので出ない)。

**残る穴**:
- 判断待ちのあいだ、hooks (`pre-tool-use.sh` / `post-tool-use.sh`) が assignment から補完する `TASK_ID` は解決できない
  (env に `TASK_ID` が無い場合)。main checkout 編集ガード (worktree ガード) は `TASK_ID` 解決済みが発火条件なので、
  判断待ちの Worker が pull し直さずに作業を再開するとガードが効かない。再開は `update --reset` → pull し直す前提
  (`agents/director.md`)。`done` / `fail` の後と同じ状態で、この PR で新しく生まれた種類の穴ではないが、needs_director は
  Worker が生きたまま待つので露出が長い。
- 指す task が archive 済み mission にある assignment は「指す task が見つからない」なので孤児と
扱わず塞ぐ (証明できない)。その場合は従来どおり `ls queue/assignments/` を見て手で消す。

**戻し方**: 共有規則 (plan.sh と dispatcher が同じ「仕事」の定義を読む) なので env の停止スイッチは付けない
(片方だけ戻すと、assignment を外す plan.sh と card を見ない Rule 2 の組が残り、判断待ちの Worker が殺される)。
PR revert → 主 checkout を `git merge --ff-only origin/main` → `lib_daemon_watch.py restart` (dispatcher は
常駐で、merge しただけでは動かない: `knowledge/dispatcher-restart-after-merge.md`)。

回帰テスト: `tests/test_needs_director_releases_assignment.py` (本物の plan.sh と dispatcher の python を回す)、
赤の実証は `tests/red_proof_t001_needs_director.sh`。

---

## 8. 参照

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
- `scripts/lib_daemon_watch.py` — 相互監視の判定・respawn・報告 (§7)。
  `instance_alive()` / `scan_daemon_pids()` (§7-3)、`_list_windows()` (§7-3 の従証拠)、
  `_hold()` (§7-4)、`pause()` / `resume()` / `restart()` (§7-5)、
  `_flap_entries()` (§7-6)、`spawn_command()` (§7-7)
- `scripts/lib_daemon_watch.sh` — dispatcher の heartbeat を bash から書く (§7-2)
- `hooks/post-tool-use.sh` — 同時死の backstop (§7-13)。`_INTENTIONAL_EXIT_CODE` /
  `daemon-backstop` セクション。`tests/test_daemon_backstop_hook.py` が回帰テスト
- `scripts/lib_mux.py` — `reap_stale_pane_records()` / `HerdrBackend._pane_existence()` /
  `TmuxBackend.record_existence()` (§7-14)、`spawn` の終了コード 10 / 11
- `tests/pytest_workspace_sweep.py` + `tests/conftest.py` の `pytest_unconfigure` — pytest が作った
  宛先の後始末 (§7-15)。`CREWVIA_PYTEST_WORKSPACE_SWEEP=0` で残骸掃除だけが止まる
- `scripts/plan.sh` `cmd_needs_director()` / `scripts/dispatcher.sh` `worker_holds_work()` /
  `worker_waits_on_director()` / `codex_review_slot_busy()` / `RELEASED_WORK_STATUSES` (§7-16)
- `knowledge/dispatcher-restart-after-merge.md` — 常駐デーモンの反映手順。
  **§7-5 の pause を挟む手順が追加された**ので、kill → spawn を素で打たないこと
