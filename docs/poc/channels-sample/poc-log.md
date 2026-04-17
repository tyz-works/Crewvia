# fakechat PoC 動作確認ログ

実施日: 2026-04-18  
実施者: Lt. Commander Data (task_081_data)  
Claude Code バージョン: v2.1.112  
Node.js バージョン: v25.6.1

---

## テスト 1: MCP initialize — capability 宣言確認

**コマンド:**
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' \
  | node fakechat-server.js
```

**結果: ✅ 成功**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "serverInfo": { "name": "fakechat", "version": "0.1.0" },
    "capabilities": {
      "claude/channel": { "permission": true }
    }
  }
}
```

stderr:
```
[fakechat] MCP server started (stdio). Declaring claude/channel/permission.
[fakechat] ✅ initialize — claude/channel/permission declared
```

---

## テスト 2: --channels server:fakechat で Claude Code 接続

**コマンド:**
```bash
claude --mcp-config /tmp/fakechat-mcp.json --channels server:fakechat -p "say hello"
```

**結果: ✅ 成功**

出力: `Hello!`

Claude Code が `server:fakechat` 形式で接続し、セッションが正常動作した。

---

## テスト 3: --channels フラグ形式確認（エラーケース）

**コマンド:**
```bash
claude --channels fakechat  # タグなし
```

**結果: 仕様通りのエラー（確認済み）**

```
--channels entries must be tagged: fakechat
  plugin:<name>@<marketplace>  — plugin-provided channel (allowlist enforced)
  server:<name>                — manually configured MCP server
```

---

## 確認できた動作

| 機能 | 状態 | 備考 |
|------|------|------|
| `claude/channel` capability 宣言 | ✅ OK | `{"permission": true}` で確認 |
| `--channels server:<name>` 接続 | ✅ OK | Claude Code がサーバを認識・接続 |
| MCP initialize ハンドシェイク | ✅ OK | protocolVersion 2024-11-05 |
| `notifications/claude/channel/permission_request` 受信 | ⚠️ 未確認 | 非インタラクティブ (-p) では発火しない |
| permission_response 送信後 verdict 適用 | ⚠️ 未確認 | 上記と同じ理由 |
| sender allowlist | ⚠️ 未実装 | PoC のため省略 |

---

## 確認できなかった動作

`claude -p`（非インタラクティブ）モードでは tool 承認 UI が発生しないため、
`notifications/claude/channel/permission_request` が送信されなかった。

**次ステップ:** インタラクティブセッション（`claude` のみ）で Bash/Write を呼ぶタスクを与えて再テスト。

---

## 知見サマリー

1. `--channels` は `server:<name>` 形式が必須（タグなしはエラー）
2. capability キーは `"claude/channel": { "permission": true }`
3. stdio transport で動作、HTTP 不要
4. permission relay テストにはインタラクティブセッションが必要
5. Crewvia `pre-tool-use.sh` への統合は実装可能
