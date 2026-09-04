# herdr Spike 結果レポート (Phase 0)

## 環境

| 項目 | 値 |
|------|-----|
| herdr version | 0.8.2 (stable) |
| WSL kernel | 6.18.33.2-microsoft-standard-WSL2 |
| 実施日 | 2026-09-04 |
| 参照設計 | `docs/herdr-migration-design.md` §7 |

---

## 5 項目の検証結果

### 1. server の daemon 化

**結論: Yes（完全自律 daemon 化）**

`herdr server` は起動直後に自動的に fork して背景化する（`detached_server_daemon: true`）。
`setsid nohup` は不要。

#### 実行コマンドと出力抜粋

```bash
# 起動（setsid nohup は参考として使用したが不要）
setsid nohup herdr server > /tmp/herdr-server-stdout.txt 2>&1 &
# → 親プロセスは即座に exit。実際のサーバーは別PIDで起動

# stdout に出力されたメッセージ:
# herdr server running; you can use any herdr CLI command in another terminal.
# api socket: /home/tkadmin/.config/herdr/herdr.sock
# logs: /home/tkadmin/.config/herdr/herdr-server.log

# socket 出現時間: 約 510ms

# socket ping:
echo '{"id":"1","method":"ping","params":{}}' | nc -U ~/.config/herdr/herdr.sock
# → {"id":"1","result":{"type":"pong","version":"0.8.2","protocol":20,
#     "capabilities":{"live_handoff":true,"detached_server_daemon":true}}}

# 起動済みの状態で herdr server を再実行すると:
herdr server  # → error: herdr server is already running (exit code 1)
```

#### spec §4.2 への影響

設計変更不要。`herdr server` の一度だけの呼び出しで OK。
`pgrep herdr` でサーバー存在確認 → 存在しなければ `herdr server` を呼ぶパターンで実装可能。
socket ping でリトライすれば起動完了判定も安定する（約 500ms 程度）。

---

### 2. label の伝播

**結論: 条件付き（tab label → pane 自動伝播なし。`herdr pane rename` が必要）**

`herdr tab create --label spike-a` で作成した tab の label は `spike-a` になるが、
その tab 内の root_pane の label は `null` のまま。
`herdr pane rename <pane_id> <name>` で明示的に設定する必要がある。

#### 実行コマンドと出力抜粋

```bash
# tab create
herdr tab create --workspace w1 --label spike-a --cwd /tmp/spike --no-focus
# result.tab.label = "spike-a"  ← tab は OK

# pane list で確認
herdr pane list --workspace w1 | jq '.result.panes[] | {pane_id, label}'
# → {"pane_id": "w1:p2", "label": null}  ← pane は null

# pane rename で明示設定
herdr pane rename w1:p2 spike-a
# → result.pane.label = "spike-a"  ← 設定成功
```

#### spec §4.2 への影響

**設計変更あり**。Worker 起動ワークフローに `pane rename` を追加：

```bash
# 変更後（正）
tab_out=$(herdr tab create --workspace "$WID" --label "$WORKER_NAME" --no-focus ...)
pane_id=$(echo "$tab_out" | jq -r '.result.root_pane.pane_id')
herdr pane rename "$pane_id" "$WORKER_NAME"  # 追加必要
```

---

### 3. pane run の Enter 送信

**結論: 条件付き（❯ 後は正常 submit、❯ 前はテキスト挿入のみで Enter が落ちる）**

#### ❯ が出た後の送信（正常ケース）

```bash
herdr pane run w1:p3 'こんにちは、1行で返事して'
# agent_status: working（即座に遷移）← submit 成功

# 応答後の pane read:
# ❯ こんにちは、1行で返事して
# ● こんにちは！今日はどんなお手伝いをしましょうか？
# ✻ Baked for 4s · done 12:04 PM
```

→ **Yes**。`herdr pane run` は ❯ 出現後であれば確実に submit される。

#### ❯ が出る前の送信（初期化途中ケース）

```bash
# claude 起動直後（200ms後）に即座に送信
herdr pane run w1:p3 'claude --dangerously-skip-permissions'
sleep 0.2
herdr pane run w1:p3 'early_message_before_prompt'  # EXIT:0 を返すがSubmitされない

# claude ready 後に pane read すると:
# ❯ early_message_before_prompt  ← テキストは入力済みだが Enter は落ちている
```

→ **Enter が落ちる**（テキストは input field に残るが未 submit）。
競合状態でメッセージが消えるのではなく、入力フィールドに蓄積される。

#### `agent wait` の注意点

```bash
# claude 応答完了後のステータスは "done"（headless では idle にならない）
herdr agent wait w1:p3 --until idle --timeout 60000  # → timeout

# 正しい待機:
herdr agent wait w1:p3 --until done idle --timeout 60000
# または pane wait-output で ✻ マークを待つ
```

#### spec §4.2 への影響

**設計変更あり**。最初のプロンプト送信前に ready 確認が必須：

```bash
# Worker 起動後の送信パターン
herdr agent start "$AGENT_NAME" --kind claude --pane "$PANE_ID" -- --dangerously-skip-permissions
# または: agent wait で ready 確認してから pane run
herdr agent wait "$PANE_ID" --until idle done --timeout 30000
herdr pane run "$PANE_ID" "$FIRST_PROMPT"
```

---

### 4. tab close の挙動

**結論: Yes（tab close で claude および子プロセスが 2 秒以内に完全終了、孤児プロセスなし）**

#### 実行コマンドと出力抜粋

```bash
# close 前のプロセス確認
herdr pane process-info --pane w1:p3 | jq '.result.process_info | {shell_pid, fg_processes: [.foreground_processes[] | {pid, name}]}'
# shell_pid: 199426
# fg_processes: [{pid:200403, name:"claude"}, {pid:201521, name:"npm exec @playwright/mcp"}, ...]

# tab close
herdr tab close w1:t3
# → {"result": {"type": "ok"}}

# 2秒後の確認
pgrep -a -f "claude --dangerously-skip-permissions" || echo "terminated"
# → "terminated" (PID 200403 消滅)
ps -p 199426 || echo "shell terminated"
# → "shell terminated"
```

- claude プロセスと子プロセス（npm/playwright/chrome-devtools）がすべて消滅
- SIGHUP 経由の PTY close による終了（graceful、2 秒以内）
- `herdr workspace close <wid>` でも同様の動作を確認

#### spec §4.2 への影響

設計変更不要。`herdr tab close` / `herdr workspace close` で Worker の claude プロセスを確実に終了できる。追加の `pane send-keys ctrl+c` は不要。

---

### 5. WSL2 での安定性

**結論: Yes（spike 全体を通してエラー・異常なし）**

#### 観察結果

| チェック項目 | 結果 |
|------------|------|
| socket エラー | なし |
| ConPTY 起因の描画異常 | なし |
| コマンドハング | なし（全 CLI が正常終了） |
| server socket 応答時間 | ≤ 510ms |

```bash
herdr --version   # herdr 0.8.2
uname -r          # 6.18.33.2-microsoft-standard-WSL2
herdr status server
# status: running, version: 0.8.2, protocol: 20, compatible: yes
```

- server log は security ルール（`~/.config/**` 禁止）により直接アクセス不可
  → `herdr status server` + socket ping で異常なし確認
- trust dialog / bypass permission dialog 対応時に `send-keys down/enter` を複数回送信したが問題なし
- `workspace create → tab create → pane run → pane read → tab close → workspace close` の
  ライフサイクル全体が正常動作

#### spec §4.2 への影響

設計変更不要。WSL2 は実用上安定している。

---

## 追加で気づいた点

1. **`herdr server` の出力**: 起動に成功すると stdout に `"herdr server running; you can use any herdr CLI command in another terminal."` を表示してから daemon 化する。CI/スクリプトでは stdout をキャプチャして起動完了を確認できる。

2. **`herdr api snapshot` の不整合**: `herdr workspace list` でワークスペースが見えているのに `herdr api snapshot` が `workspace_count: 0` を返すケースがあった。`workspace list` コマンドを信頼すること。

3. **`--dangerously-skip-permissions` の確認ダイアログ**: 空ディレクトリでも 2 段階確認が出る:
   - 「このフォルダを信頼しますか？」（Down + Enter で "Yes"）
   - 「Bypass Permissions モードで続行しますか？」（Down + Enter で "Yes, I accept"）
   `herdr agent start` を使えばこの wait-for-ready が自動化できる。

4. **agent status `done` vs `idle`**: claude 応答完了直後は `done`（タブが未表示）。headless 環境では `focus` を呼ばない限り `idle` に遷移しない。`herdr agent wait --until done idle` と両方指定すること。

5. **CLI 名の実態**（調査レポートとの差分）:
   - `herdr workspace/tab/pane/agent` サブコマンド → 一致（v0.8.2 安定）
   - `herdr worktree` サブコマンドが存在（調査レポート未記載、crewvia との連携に有用かも）
   - `herdr agent prompt` は `herdr pane run` の agent-aware wrapper（`--wait` オプションで send+wait が atomic）

---

## 後片付け

spike 中に作成した workspace `crewvia-spike`（w1）は `herdr workspace close w1` で削除済み。
herdr server は起動したままで可（本番 crewvia tmux セッションには一切触れていない）。

---

## 5 項目の結論サマリ

| # | 項目 | 結論 |
|---|------|------|
| 1 | server daemon 化 | **Yes** — 自律 daemon 化。`setsid nohup` 不要。socket 約 500ms |
| 2 | label 伝播 | **条件付き** — tab label は pane に自動伝播しない。`pane rename` が必要 |
| 3 | pane run + Enter | **条件付き** — ❯ 後は OK、❯ 前は Enter drop。`agent wait` で ready 確認必須 |
| 4 | tab close 挙動 | **Yes** — claude および子プロセスが 2s 以内に完全終了、孤児なし |
| 5 | WSL2 安定性 | **Yes** — socket/ConPTY/hang エラーなし、全操作が正常動作 |
