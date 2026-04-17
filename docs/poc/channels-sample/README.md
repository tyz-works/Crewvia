# Claude Code Channels × Crewvia — M7 調査メモ

作成日: 2026-04-18  
作成者: Data (task_081_data)

---

## 1. Claude Code Channels 仕様要点

### 概要
Claude Code Channels は MCP を双方向メッセージング基盤に拡張する仕組み。
外部システム（Discord / Telegram / GitHub webhook 等）から Claude Code セッションへイベントを push し、Claude がリアクティブに応答できる。

### 主要仕様

| 項目 | 内容 |
|------|------|
| 必要バージョン | Claude Code v2.1.81 以降（permission relay は v2.1.81+） |
| CLI フラグ | `--channels <tag>` |
| タグ形式 | `plugin:<name>@<marketplace>` または `server:<name>` |
| transport | stdio（標準的な MCP transport） |
| capability キー | `"claude/channel": { "permission": true }` |
| プロトコル | JSON-RPC 2.0 over stdio |

### --channels フラグ

```bash
# 承認済みプラグイン（アローリスト強制）
claude --channels plugin:telegram@claude-plugins-official

# 手動設定 MCP サーバ（--mcp-config または .mcp.json）
claude --channels server:fakechat

# 開発中サーバのサイドロード（dev のみ）
claude --channels server:mydev --dangerously-load-development-channels
```

### permission relay フロー（4ステップ）

```
1. Claude Code → 5文字 request_id 生成
2. Claude Code → notifications/claude/channel/permission_request 送信
   params: { request_id, tool_name, tool_input, reason }
3. channel server → ユーザー（Discord/Telegram/ターミナル等）へ転送
4. ユーザー → verdict (y/n + request_id) 返信
   channel server → notifications/claude/channel/permission_response 送信
   params: { request_id, verdict: "approved"|"denied" }
```

### sender allowlist 設計

- 各 channel plugin は **送信者ホワイトリスト**を管理
- 未ペアリングの送信者メッセージは**サイレント破棄**（no error, no log）
- ペアリング: 初回メッセージ → pairing code 返却 → Claude Code セッションで入力
- 粒度: メール＝ドメイン単位 / GitHub・Slack = ユーザーID / Telegram・Discord = ユーザーID

---

## 2. Taskvia との機能マッピング

### 現在の Crewvia 承認フロー（Taskvia）

```
Claude Code (Worker)
  → hooks/pre-tool-use.sh
    → POST /api/request (Taskvia SaaS)
    → polling /api/status/{id} (1秒間隔・最大600秒)
  ← approved / denied
```

### Channels permission relay フロー

```
Claude Code (Worker)
  → notifications/claude/channel/permission_request (MCP stdio)
  → channel server (fakechat-server.js 等)
    → forward to user (ターミナル表示 / Discord / Telegram)
  ← notifications/claude/channel/permission_response
  ← verdict 即時適用
```

### 機能マッピング表

| 機能 | Taskvia (現在) | Channels relay (新) | 備考 |
|------|--------------|---------------------|------|
| 承認リクエスト送信 | POST /api/request | `permission_request` notification | MCP統合で外部HTTP不要 |
| 承認待ちポーリング | GET /api/status (1s) | event-driven (即時) | latency 大幅削減 |
| ユーザー通知先 | Taskvia UI (Web) | Discord/Telegram/ターミナル | 柔軟な通知先 |
| sender 認証 | Bearer token | allowlist + pairing code | Channelsは事前ペアリング必要 |
| タスク ID / コンテキスト | task_id, task_title | tool_name, tool_input (全量) | Channelsはより詳細 |
| 承認 UI | Kanban card | モバイルチャット / ターミナル | チャットアプリで即承認 |
| timeout | 600s で自動拒否 | N/A (request_id で管理) | TTL実装は channel server 側 |
| skill-based auto-allow | lib_skill_perms.py | 不変（Channels非関係） | 引き続き有効 |

### 統合ポイント

Channels は Taskvia の**代替**ではなく**補完**として機能する:

- **Taskvia**: Web UI Kanban で visual management、複数エージェント一元管理
- **Channels relay**: モバイルチャット（外出中）、低レイテンシ、Taskvia 利用不可時のフォールバック

`pre-tool-use.sh` への統合案:
1. `CREWVIA_TASKVIA=disabled` かつ Channels 接続済みの場合 → `permission_request` 経由で relay
2. Taskvia 応答タイムアウト時 → Channels relay へフォールバック

---

## 3. PoC で確認できた動作

| 確認事項 | 結果 |
|---------|------|
| `claude/channel` capability 宣言 | ✅ 動作 |
| `--channels server:<name>` 接続 | ✅ 動作 |
| MCP initialize ハンドシェイク | ✅ 動作 |
| `permission_request` 受信（インタラクティブ） | ⚠️ 未確認（非インタラクティブでは発火しない） |
| `permission_response` 送信後 verdict 適用 | ⚠️ 未確認 |
| sender allowlist 実装 | ⚠️ PoC省略（TODO） |

## 4. PoC で確認できなかった動作

### permission relay エンドツーエンド

`claude -p`（非インタラクティブ）モードでは tool 承認プロンプトが生成されないため、
`notifications/claude/channel/permission_request` が Claude Code から送信されなかった。

**再現手順（今後）:**
```bash
# インタラクティブセッションで実行
claude --mcp-config /tmp/fakechat-mcp.json --channels server:fakechat
# セッション内で Write / Bash を呼ぶタスクを与える
# → permission_request が fakechat-server に届くはず
```

---

## 5. 実装ファイル

```
docs/poc/channels-sample/
├── README.md              ← このファイル
├── fakechat-server.js     ← 最小 MCP サーバ (claude/channel 宣言 + relay)
└── poc-log.md             ← 動作確認ログ
```

### fakechat-server.js の起動方法

```bash
# MCP config を用意
cat > /tmp/fakechat-mcp.json << 'EOF'
{
  "mcpServers": {
    "fakechat": {
      "type": "stdio",
      "command": "node",
      "args": ["/path/to/fakechat-server.js"]
    }
  }
}
EOF

# Claude Code に接続
claude --mcp-config /tmp/fakechat-mcp.json --channels server:fakechat
```
