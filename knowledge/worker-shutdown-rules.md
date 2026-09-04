# Worker Shutdown ルール仕様書

> 作成: 2026-09-02
> タスク: 20260902-crewvia-improvements/t004
> 関連 memory: crewvia-idle-worker-shutdown-inconsistency

## 1. 背景・目的

Worker の自主 shutdown 判断が LLM 個体差によりブレる問題がある。
特に user 手作業 phase（Director がユーザーに確認を求めている間）で、
Worker が idle のまま WIP 枠を食い潰し続けるケースが発生している。

本文書の目的:
1. 現状の shutdown メカニズムを 3 層で整理する
2. 失敗パターンを分類する
3. Rule 1-4 の確定仕様を策定する
4. director.md / worker.md への統合案を示す

実装は別タスク（t008）が担当する。

---

## 2. 現状の Shutdown メカニズム（3 層）

### 2-A. Dispatcher 層（scripts/dispatcher.sh）

5 秒ごとにポーリング。以下の条件でシャットダウン通知を送る。

条件: Worker のスキルに合う pending タスク（blocked 含む）がゼロ
  処理: tmux send-keys タスクなし、shutdown → tmux kill-window

条件: 全 active mission が done
  処理: idle Worker を全員 shutdown → Director に全ミッション完了通知

重要仕様: Dispatcher は shutdown_idle_workers() を呼んだ後に
tmux kill-window も実行する。つまりタスクなし、shutdown メッセージは
Worker が受け取れない場合もある（ウィンドウが即 kill される）。

NOTIFY_TTL = 300秒（5 分）により同じ Worker への shutdown 通知の重複は抑制される。

### 2-B. Watchdog 層（scripts/watchdog.py v2）

30 秒ごとにポーリング。Worker プロファイルに基づいて判定。

状態 | 条件 | 処理
warn       | idle > idle_threshold            | Taskvia に alert 投稿
terminate  | idle > idle_threshold*2 or max超 | graceful_terminate() → SIGTERM → SIGKILL
kill       | tmux window が消失               | cleanup のみ

プロファイル別しきい値:
  feature_impl（デフォルト）: idle=300秒, max=3600秒
  research                  : idle=600秒, max=7200秒
  quick                     : idle=120秒, max=600秒

graceful_terminate() の流れ:
  1. tmux でメッセージ送信
  2. 60 秒待機（Worker が自発的に終了する猶予）
  3. 自発終了しなければ SIGTERM → 10 秒後 SIGKILL

### 2-C. Worker 自身（agents/worker.md）

Worker は原則としてポーリングしない。Dispatcher からの通知を受けて動作する。

受信メッセージ          | 動作
タスク {id} ... を実行して | pull → 実行
タスクなし、shutdown       | セッション終了

現状のルール（worker.md）: 「自発的にタスクがない判断で終了しないこと —
Dispatcher が適切なタイミングで通知する」

---

## 3. 失敗パターンの分類

### パターン A: shutdown 通知を Worker が無視する

発生状況:
- Dispatcher が「タスクなし、shutdown」を送信
- Worker が「次のタスクを待機します」と応答してセッションを継続
- Dispatcher は NOTIFY_TTL（5 分）内に同じ通知を再送しない

根本原因: LLM が「shutdown = 強制終了」ではなく「状況次第の判断」と解釈する。
影響: Worker が idle のままで WIP 枠を占有し続ける。

### パターン B: user 手作業 phase での WIP 占有

発生状況:
1. Director がユーザーに確認を求め、処理を停止
2. Worker はタスクを完了し、次の pull を試みるが blocked タスクしかない
3. Dispatcher は「スキルに合う blocked タスクが存在する」と判断し、shutdown を送らない
4. Worker は idle のまま WIP 枠を食い潰し続ける

根本原因: Dispatcher は「blocked タスクがある = Worker はいずれ必要」と判断するが、
user 手作業 phase では「誰も解除できない blocked タスク」が長時間残る。
影響: 他の mission のタスクが WIP 制限で起動できない。

### パターン C: Description 空タスクを pull した Worker の即 shutdown

対処（既知）: Priya（planning スキル）に Description 補強を依頼する。
Rule 1-4 の対象外（実装側の問題）。

### パターン D: shutdown 受信後に graceful handoff が長引く

発生状況: watchdog.py が terminate 判定 → graceful handoff 実行中に 60 秒超過 → SIGKILL
影響: HANDOFF.md が不完全な状態で引き継ぎが発生する。

---

## 4. Rule 1-4 確定仕様

### Rule 1: shutdown 通知受信後の即時終了（Worker 側）

トリガー: Dispatcher から「タスクなし、shutdown」を受信

Worker の義務的動作:
1. 受信した瞬間に Pre-Done チェックリストを実行
2. 即座にセッション終了

以下の判断は禁止:
  ❌ 「もう少し待てば次のタスクが来るかもしれない」→ 待機継続
  ❌ 「Director に確認してから終了する」→ 遅延
  ❌ 「念のため plan.sh status を確認してから判断する」→ 独自判断

根拠: Dispatcher はすべてのタスク状況を把握した上で shutdown を送信する。
Worker が独自に「まだ必要かもしれない」と判断する情報は Dispatcher より劣る。

### Rule 2: Dispatcher の blocked-stuck 判定（Dispatcher 側）

トリガー: Worker のスキルに合う blocked タスクが 600 秒（10 分）以上変化なし

Dispatcher の動作:
1. pending blocked タスクの last_update タイムスタンプを確認
2. 600 秒以上変化なし（= ブロッカーが解除される見込みなし）と判定
3. idle Worker に「タスクなし、shutdown」を送信 + tmux kill-window

設定値:
  blocked 継続時間しきい値 : 600 秒（research プロファイル idle_threshold と揃える）
  チェック間隔              : 5 秒（現状維持）
  NOTIFY_TTL               : 300 秒（現状維持）

実装メモ: task frontmatter に last_status_change フィールドを追加するか、
plan.sh done/fail 実行時のタイムスタンプをファイル mtime で代用する。

### Rule 3: Director による能動 kill（user 手作業 phase）

トリガー: Director がユーザーに確認を求める phase に入り、
かつ idle Worker が存在し、当該 Worker のスキルに合う unblocked タスクがゼロ

判断基準（Rule 3 を適用するタイミング）:
1. ユーザーへの確認完了まで 30 分以上かかる見込み
2. plan.sh status で idle（in_progress マーカなし）の Worker が存在
3. 当該 Worker のスキルに合う unblocked pending タスクがゼロ

実行手順:
  AGENT_NAME=<対象 Worker 名>
  tmux send-keys -t crewvia:Tariq-worker タスクなし、shutdown Enter
  sleep 2
  tmux kill-window -t crewvia:Tariq-worker 2>/dev/null || true
  rm -f registry/heartbeats/Tariq

将来の自動化案: queue/missions/<slug>/user_wait.flag ファイルを Director が作成し、
Dispatcher がそれを検知して自動的に Rule 3 を実行する仕組みを追加（t008 以降）。

### Rule 5: herdr agent state による blocked / idle-with-task 検知（Dispatcher 側、通知のみ）

トリガー: herdr モードで Worker の `agent_status` が以下のいずれかを **grace 秒連続** で満たす

| 条件名 | 条件 | 意味 |
|---|---|---|
| A: blocked | `agent_status == blocked` | 承認ダイアログ等で止まっている |
| B: idle-with-task | `agent_status ∈ {idle, done}` かつ `queue/assignments/<name>` が存在 | task を持ったまま手が止まっている |

Dispatcher の動作:
1. 各 Worker の `agent_status` を 5 秒ごとに `lib_mux.py state <name>` で取得
2. 状態を `registry/mux/<name>.state.json` に `{state, since}` として保存
3. A or B が grace 秒 (デフォルト 60、`config mux.state_grace_seconds`、env `CREWVIA_STATE_GRACE`) 以上連続 → Director に通知
4. `working` に戻ったら通知 dedup key をクリアし、再発時に再通知可能にする

通知文面:
```
[Rule 5] Worker <name> が <blocked|idle-with-task> です (task <id>, mission=<slug>, <n>秒継続)。
画面末尾:
<pane の末尾 5 行>
```

Director の対応 (参照: `agents/director.md §17`):
1. 画面末尾で状況を判断
2. 質問待ち → `python3 scripts/lib_mux.py send {name}-worker "<回答>"`
3. 承認ダイアログ → ユーザーへエスカレーション
4. 回復不能 → kill + `plan.sh update --reset`

設定値:
  grace 連続時間しきい値: 60 秒 (config `mux.state_grace_seconds`)
  NOTIFY_TTL           : 300 秒 (共通、重複通知抑制)
  チェック間隔          : 5 秒 (通常ポーリングに統合)

注意: **自動対処はしない**。通知 + Director 判断のみ。tmux モードでは state が unknown 固定なので発火しない。

### Rule 4: Graceful Handoff タイムアウト制約（Worker 側）

トリガー: watchdog.py から「タイムアウトのため中断します」を受信

現状の制約:
  graceful period = 60 秒（TERMINATE_GRACE_PERIOD）
  60 秒内に終了しなければ SIGTERM → 10 秒後 SIGKILL

Worker の義務的動作:
  目標: 30 秒以内に HANDOFF.md 作成 + plan.sh fail 完了
  「完璧な HANDOFF.md」より「30 秒以内に終了」を優先
  最低限 progress/remaining/注意点の 3 点のみ記載して完了させる

watchdog.py のメッセージ変更案（t008）:
  「タイムアウトのため中断します。HANDOFF.md を作成して
  plan.sh fail を実行し、30 秒以内に終了してください。」

---

## 5. 実装優先度と担当（t008 scope）

Rule 1: 高 | worker.md へのルール追記          | docs スキル
Rule 2: 中 | dispatcher.sh に blocked-stuck 追加 | bash,code スキル
Rule 3: 中 | director.md に能動 kill 手順追記   | docs スキル
Rule 4: 低 | watchdog.py + worker.md 追記        | bash,code + docs スキル

---

## 6. director.md への統合案（§14 末尾への追記）

### User 手作業 phase での能動 kill（Rule 3）

ユーザーへの確認が長時間（目安 30 分以上）かかる場合、
idle Worker は WIP 枠を消費し続けるため、Director が能動的に終了させる。

適用条件の確認:
1. plan.sh status で in_progress マーカのない Worker が存在するか確認
2. 当該 Worker のスキルに合う unblocked pending タスクが 0 件か確認
3. ユーザー確認完了まで 30 分以上かかる見込みか判断

実行手順（bash）:
  AGENT_NAME=<agent_name>
  tmux send-keys -t crewvia:Tariq-worker タスクなし、shutdown Enter
  sleep 2
  tmux kill-window -t crewvia:Tariq-worker 2>/dev/null || true
  rm -f registry/heartbeats/Tariq

ユーザー確認完了後、必要であれば同スキルで新 Worker を起動する。

---

## 7. 数値設定一覧

パラメータ                    | 現状値    | 推奨値      | 変更理由
Dispatcher NOTIFY_TTL        | 300 秒   | 300 秒     | 変更なし
Dispatcher ポーリング間隔     | 5 秒     | 5 秒       | 変更なし
Watchdog TERMINATE_GRACE_PERIOD | 60 秒 | 60 秒      | 変更なし
Worker idle 待機時間（exit 2後）| 30 秒  | 30 秒      | 変更なし
Rule 2: blocked 継続判定しきい値 | なし  | 600 秒     | 新規追加
Rule 3: Director 介入判断しきい値 | なし | 30 分（目安）| 新規追加
Rule 4: Worker handoff 目標時間 | なし  | 30 秒      | 新規追加
Rule 5: herdr state grace 秒数 | なし   | 60 秒      | 新規追加 (herdr モードのみ)

---

## 8. 未解決課題・次 session 申し送り

1. Rule 2 実装の前提条件: task frontmatter に last_status_change タイムスタンプが必要。
   plan.sh の done/fail 時に自動更新する仕組みを追加する必要がある。

2. Rule 1 LLM 個体差の定量測定: e2e_harness.sh でシミュレーションして
   shutdown 通知受信後も待機継続する Worker の発生率を測定することを推奨。

3. Rule 4 HANDOFF.md 品質保証: Priya による HANDOFF.md レビュータスクの追加を検討。
