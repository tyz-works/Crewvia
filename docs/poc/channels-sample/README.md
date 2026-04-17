# Claude Code Channels × Crewvia — M7 調査メモ

作成日: 2026-04-18  
作成者: Data (task_081_data)

---

## 1. Channels 仕様要点

### 概要
Claude Code Channels は MCP を双方向メッセージング基盤に拡張する機能。
外部システムから Claude Code セッションへイベントを push し、Claude がリアクティブに応答できる。

### 主要仕様

| 項目 | 内容 |
|------|------|
| 必要バージョン | Claude Code v2.1.81 以降 |
| CLI フラグ | `--channels <tag>` |
| タグ形式 | `plugin:<name>@<marketplace>` または `server:<name>` |
| transport | stdio（標準 MCP transport） |
| capability キー | `"claude/channel": { "permission": true }` |
| プロトコル | JSON-RPC 2.0 over stdio |

### --channels フラグ

```bash
# 承認済みプラグイン（アローリスト強制）
claude --channels plugin:telegram@claude-plugins-official

# 手動設定 MCP サーバ（--mcp-config または .mcp.json）
claude --channels server:fakechat

# 開発中サーバのサイドロード
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

- 各 channel plugin は送信者ホワイトリストを管理
- 未ペアリングの送信者メッセージはサイレント破棄
- ペアリング: 初回メッセージ → pairing code → Claude Code セッションで入力
- 粒度: メール＝ドメイン / GitHub・Slack＝ユーザーID / Telegram・Discord＝ユーザーID

---

## 2. Taskvia との機能マッピング

### 現在の Crewvia 承認フロー（Taskvia）

```
Claude Code → hooks/pre-tool-use.sh → POST /api/request (Taskvia)
           → polling /api/status/{id} (1秒間隔・最大600秒)
           ← approved / denied
```

### Channels permission relay フロー

```
Claude Code → notifications/claude/channel/permission_request (MCP stdio)
           → channel server → ユーザー（ターミナル / Discord / Telegram）
           ← notifications/claude/channel/permission_response (即時)
```

### 機能マッピング表

| 機能 | Taskvia (現在) | Channels relay (新) | 備考 |
|------|--------------|---------------------|------|
| 承認リクエスト | POST /api/request | `permission_request` notification | 外部HTTP不要 |
| 承認待ち | GET /api/status (1s polling) | event-driven (即時) | latency 大幅削減 |
| 通知先 | Taskvia Web UI | Discord/Telegram/ターミナル | 柔軟 |
| 認証 | Bearer token | allowlist + pairing code | 事前ペアリング必要 |
| コンテキスト | task_id, task_title | tool_name, tool_input (全量) | Channels がより詳細 |
| timeout | 600s 自動拒否 | N/A (server側実装) | TTL は channel server 担当 |
| skill auto-allow | lib_skill_perms.py | 変更なし | 引き続き有効 |

### 統合ポジション

Channels は Taskvia の**代替でなく補完**:

- **Taskvia**: Web Kanban による visual management、複数エージェント一元管理
- **Channels relay**: モバイルチャット承認、低レイテンシ、Taskvia 不可時フォールバック

`pre-tool-use.sh` への統合案:
1. `CREWVIA_TASKVIA=disabled` かつ Channels 接続済み → `permission_request` で relay
2. Taskvia タイムアウト時 → Channels relay へフォールバック

---

## 3. PoC で確認できた動作

| 確認事項 | 結果 |
|---------|------|
| `claude/channel` capability 宣言 | ✅ 動作 |
| `--channels server:<name>` 接続 | ✅ 動作 |
| MCP initialize ハンドシェイク | ✅ 動作 |
| `permission_request` 受信（インタラクティブ） | ⚠️ 未確認（-p では発火しない） |
| `permission_response` 後の verdict 適用 | ⚠️ 未確認 |
| sender allowlist | ⚠️ PoC省略（TODO） |

---

## 4. 実装ファイル

```
docs/poc/channels-sample/
├── README.md              ← このファイル（仕様・マッピング・結果）
├── fakechat-server.js     ← 最小 MCP サーバ（claude/channel 宣言 + relay）
└── poc-log.md             ← 動作確認ログ（テスト結果詳細）
```

### 起動方法

```bash
cat > /tmp/fakechat-mcp.json << 'EOF'
{
  "mcpServers": {
    "fakechat": {
      "type": "stdio",
      "command": "node",
      "args": ["<absolute-path-to>/fakechat-server.js"]
    }
  }
}
EOF

claude --mcp-config /tmp/fakechat-mcp.json --channels server:fakechat
```
