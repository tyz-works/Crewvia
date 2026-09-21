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
という不変条件を、dispatcher の idle 判定 (D1 :1005、D2/D3 :1150-1151)、`plan.sh pull`
の書き込み (:1514-1519)、`plan.sh done` の削除 (:1794-1797)、`plan.sh fail` の削除
(:1901-1906、コメントに "same as done")、`plan.sh update --reset` の削除 (:2966-2984)
の **5 箇所**が共有している。tombstone を置くと「存在するが idle」と
いう第 4 の状態が生まれ、**Rule 5 の条件 B (`idle`/`done` かつ assignment 有り) が
即座に誤発火する** (:854)。

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
   *裏の取れた* list に限る (`_listing_is_authoritative()`):
   - 空でない list → 問い合わせが成功した証拠
   - 空 + backend 自体が落ちている (`server_running()` が False) → 全滅の証拠
   - 空 + backend は生きている → **outage と全滅が区別できない。保留**

「空 + backend 生存」を無条件に保留に倒すと、最後の Worker が死んだ瞬間に
marker が永久に残り、このモジュールが潰そうとしている幽霊 task が再発する。
`server_running()` の例外がそれを塞いでいる (memory:
`fail-closed-guard-can-recreate-the-defect` — 曖昧さの原因を分けて独立の
証拠を取る)。保留中も dispatcher の D4 検知 (R5) は同じ task を独立に
見つけるので、沈黙にはならない。

### (2) 後始末を「元の割り当てがまだ有効か」に条件付けた

`_settle_terminated()` は無条件に `--reset` を撃っていた。猶予期間 + 後始末
リトライの間に task は動く — Worker が最後の `plan.sh done` を済ませて
抜けた、人間が reset して後任が pull した。どちらでも「完了済みの成果を
pending に戻す」「後任の assignment を消す」が起きる。plan.sh 既存の
assignment 内容一致チェックは *別 task の* assignment しか守らず、
*同じ task の別の実行* は素通りする。

`plan.sh update` に前提条件フラグを足した:

```
plan.sh update <id> --status pending --reset --mission <slug> \
    --expect-status in_progress --expect-worker <agent>
```

判定は **queue ロックの内側**、最初の書き換えの直前で行う (読んで決めて
書く、の間に他の writer が入れない)。前提が外れたら 1 バイトも書かずに
**exit 3** — 実エラーの exit 1 と区別できるので、呼び出し側は「世の中が
変わった」と「plan.sh が壊れた」を取り違えない。リトライのたびに再評価
される。`--expect-status` の typo は exit 1 (黙って永久 no-op にしない)。

`expect-status` を `in_progress` だけに絞ったのは意図的:
`needs_director` などは Director が握っている状態で、retirement が横から
戻してよい対象ではない。外れた場合は Director に「queue は変更していない」
と報告して settle する (assignment ファイルはそのまま残るが、dispatcher の
D4 が独立に見つける)。

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
(`tests/test_retirement.py` の t019 節 / `tests/plan-update-expect.bats`)。
phase 名やフラグではなく、生きた Worker の task が `in_progress` のまま
であること、完了済みの成果が pending に巻き戻らないこと、後任の
assignment が残っていることを見る。逆向き (保留に倒しすぎて永久に
終わらない) も
`test_window_gone_is_concluded_when_the_backend_itself_is_down` と
`test_red_sigkill_waits_for_the_process_to_actually_exit` の後半で押さえた。

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
- `scripts/plan.sh` — assignment 書き込み :1514、削除 (done) :1794、削除 (fail) :1904、
  削除 (--reset) :2966、インスタンス一致確認の先例 :2960-2984
- `knowledge/worker-shutdown-rules.md` — Rule 1-5 の確定仕様
- `knowledge/worker-vanish-detection.md` — D4 の背景
- `knowledge/dispatcher-restart-after-merge.md` — 常駐デーモンの反映手順
