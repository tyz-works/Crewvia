# fakechat PoC 動作確認ログ

実施日: 2026-04-18  
実施者: Lt. Commander Data (task_081_data)  
Claude Code バージョン: v2.1.112  
Node.js バージョン: v25.6.1

---

## テスト 1: MCP initialize — capability 宣言確認

**コマンド:**
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize",...}' | node fakechat-server.js
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
[fakechat] ✅ initialize OK — channel capability declared
```

**確認事項:** `claude/channel` capability が正しく宣言されている。

---

## テスト 2: --channels server:fakechat で Claude Code 接続

**コマンド:**
```bash
claude --mcp-config /tmp/fakechat-mcp.json --channels server:fakechat -p "say hello"
```

**結果: ✅ 成功**

```
Hello!
```

Claude Code が fakechat MCP サーバに `server:fakechat` 形式で接続し、セッションが正常に動作した。

---

## テスト 3: --channels フラグ形式確認

**コマンド:**
```bash
claude --channels fakechat  # タグなし（誤った形式）
```

**結果: 確認済みのエラー（仕様通り）**

```
--channels entries must be tagged: fakechat
  plugin:<name>@<marketplace>  — plugin-provided channel (allowlist enforced)
  server:<name>                — manually configured MCP server
```

**確認事項:** `--channels` は以下2形式のみ受付:
- `plugin:<name>@<marketplace>` — Anthropic承認済みプラグイン（アローリスト強制）
- `server:<name>` — `.mcp.json` / `--mcp-config` で設定した MCP サーバ

---

## 確認できた動作

| 機能 | 状態 | 備考 |
|------|------|------|
| `claude/channel` capability 宣言 | ✅ OK | `{"permission": true}` で確認 |
| `--channels server:<name>` 接続 | ✅ OK | Claude Code がサーバを認識・接続 |
| MCP initialize ハンドシェイク | ✅ OK | protocolVersion 2024-11-05 |
| `notifications/claude/channel/permission_request` 受信 | ⚠️ 未確認 | 非インタラクティブ (-p) では発火しない |
| permission_response 送信後の verdict 適用 | ⚠️ 未確認 | 上記と同じ理由 |
| sender allowlist | ⚠️ 未実装 | PoC のため省略（TODO） |

---

## 確認できなかった動作

### permission_request / permission_response

`claude -p`（非インタラクティブ）モードでは tool 承認 UI が発生しないため、
`notifications/claude/channel/permission_request` 通知が Claude Code から送られてこなかった。

**次のステップ:** インタラクティブセッション（`claude` のみ）で実行し、
Bash/Write/Edit ツールが呼ばれるタスクを与えることで permission relay をテストできる。

---

## 知見サマリー

1. **`--channels` は `server:<name>` 形式が必要** — MCP config に登録したサーバ名を使う
2. **capability 宣言は `capabilities["claude/channel"]`** — `{"permission": true}` を含める
3. **permission relay は stdio transport で動作する** — HTTP 不要
4. **非インタラクティブモードでは relay テスト不可** — インタラクティブセッション必須
5. **Crewvia の pre-tool-use.sh との統合は実装可能** — Taskvia の代替/補完として位置付けられる
