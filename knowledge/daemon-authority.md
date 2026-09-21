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
を削除する。

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
| 既存の所有権との衝突 | **あり** (frontmatter は `plan.sh` が排他ロック付きで所有) | 無し | **あり** (4 箇所が不変条件を共有) |
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
の書き込み (:1514-1519)、`plan.sh done` の削除 (:1794-1797)、`plan.sh update --reset`
の削除 (:2966-2984) の 4 箇所が共有している。tombstone を置くと「存在するが idle」と
いう第 4 の状態が生まれ、**Rule 5 の条件 B (`idle`/`done` かつ assignment 有り) が
即座に誤発火する** (:854)。

**案B を採用する。** `registry/` は既にデーモン間共有状態の置き場である
(`heartbeats/`、`mux/`、`activity/`、`notifications/`、`watchdog-observations.jsonl`)。
Worker 名をファイル名にできるので対象を自然に表現でき、`plan.sh` の所有物に触れない。

**案B の書式と同時書き込み規律:**

```
registry/retirements/<agent>.json          ← dispatcher のみが書き、dispatcher のみが消す
  {"agent","window_target","reason":"no-task|blocked-stuck|all-done",
   "requested_at":<epoch>,"mission":<slug|null>,"task_id":<id|null>}

registry/retirements/<agent>.progress.json ← watchdog のみが書く
  {"phase":"notified|sigterm_sent|terminated","deadline":<epoch>,
   "pane_pid":<int|null>,"updated_at":<epoch>,"window_gone":<bool>}
```

**1 ファイル 1 書き手**を規律にする。両デーモンが同一ファイルを更新する設計にすると
lost update のリスクを排他制御で潰す必要が出るが、ファイルを分ければロックが要らない。
書き込みは temp + `os.replace` の atomic rename に統一し、読み手が破損 JSON を見ない
ようにする。削除は dispatcher が両ファイルまとめて行う (R4)。

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

`graceful_terminate()` は現在 `WorkerMonitor` を引数に取るが、retirement には task が
無い。`(agent_name, window_target, repo_root)` を取る形に切り出し、W2 と retirement の
両方から使えるようにする。**monitor を捏造して渡す実装にしないこと** — `task_id` や
`started_at` が偽値になり、E2 の再発防止を難しくする。

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
  走査し、`phase != terminated` のものを引き継ぐ。窓が既に無ければ
  `phase=terminated, window_gone=true` に進めて終端処理へ。窓が生きていれば当該 phase の
  deadline を now 起点で張り直す (冪等 — 同じメッセージや SIGTERM が二度届いても害は無い)。
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
- [ ] `CREWVIA_KILL_AUTHORITY` による一括ロールバック経路
- [ ] PR 説明に「両デーモンの同時 respawn 手順」を書く

## 7. 参照

- `scripts/dispatcher.sh` — D1 :998、D2 :1210、D3 :1239、D4 :1351、D5 :819、
  `tmux_kill_window()` :523
- `scripts/watchdog.py` — `check()` :295、`graceful_terminate()` :401、
  `load_active_tasks()` :509、`run()` :607
- `scripts/plan.sh` — assignment 書き込み :1514、削除 (done) :1794、削除 (--reset) :2966
- `knowledge/worker-shutdown-rules.md` — Rule 1-5 の確定仕様
- `knowledge/worker-vanish-detection.md` — D4 の背景
- `knowledge/dispatcher-restart-after-merge.md` — 常駐デーモンの反映手順
