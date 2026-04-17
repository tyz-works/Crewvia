# Claude Code Channels × Crewvia 移行調査

作成日: 2026-04-18  
作成者: Counselor Troi (task_081_troi)  
調査ベース: Data の PoC 調査メモ（docs/poc/channels-sample/README.md）

---

## 1. Channels 仕様まとめ

Claude Code Channels は MCP を双方向メッセージング基盤に拡張する機能。外部システムから Claude Code セッションへイベントを push し、Claude がリアクティブに応答できる。

### 主要仕様

| 項目 | 内容 |
|------|------|
| 必要バージョン | Claude Code v2.1.81 以降 |
| CLI フラグ | `--channels <tag>` |
| タグ形式 | `plugin:<name>@<marketplace>` または `server:<name>` |
| transport | stdio（標準 MCP transport） |
| capability キー | `"claude/channel": { "permission": true }` |
| プロトコル | JSON-RPC 2.0 over stdio |

### --channels フラグ形式

```bash
# 承認済みプラグイン（allowlist 強制）
claude --channels plugin:telegram@claude-plugins-official

# 手動設定 MCP サーバ
claude --channels server:fakechat

# 開発中サーバのサイドロード
claude --channels server:mydev --dangerously-load-development-channels
```

> ⚠️ タグなし形式（`--channels fakechat`）はエラー。`server:` または `plugin:` プレフィックスが必須。

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

### sender allowlist

- 各 channel plugin は送信者ホワイトリストを管理
- 未ペアリングの送信者メッセージはサイレント破棄
- ペアリング: 初回メッセージ → pairing code → Claude Code セッションで入力
- 粒度: メール＝ドメイン / GitHub・Slack・Telegram・Discord＝ユーザーID

---

## 2. Taskvia 承認 UI 現状機能整理

現在の Crewvia 承認フローは `hooks/pre-tool-use.sh` が担う:

```
Claude Code → hooks/pre-tool-use.sh
  → POST /api/request (Taskvia)
  → GET /api/status/{id} ポーリング（1秒間隔・最大600秒）
  ← approved / denied
```

### 現行機能の強み

- **Web Kanban UI**: ブラウザから複数エージェントの承認を一元管理
- **task_id / task_title コンテキスト**: どのタスクの何の操作かが可視化
- **skill auto-allow**: `lib_skill_perms.py` により繰り返し承認を自動化
- **実績あり**: 本番稼働済み・Crewvia 全 Worker が利用中

### 現行機能の弱点

- **ポーリング遅延**: 最大 1 秒の応答遅延が累積する
- **Taskvia 依存**: Taskvia がダウンすると承認が通らない（600秒後に自動拒否）
- **モバイル不便**: Taskvia Web UI はスマートフォンに最適化されていない

---

## 3. 機能マッピング（詳細版）

| 機能 | Taskvia (現在) | Channels relay (新) | 評価 |
|------|--------------|---------------------|------|
| 承認リクエスト | POST /api/request | `permission_request` notification | Channels が軽量 |
| 承認待ち | GET /api/status (1s ポーリング) | event-driven（即時） | Channels が高速 |
| 通知先 | Taskvia Web UI | Discord/Telegram/ターミナル | Channels が柔軟 |
| 認証 | Bearer token | allowlist + pairing code | 同等（方式が異なる） |
| コンテキスト | task_id, task_title | tool_name, tool_input（全量） | Channels がより詳細 |
| TTL/timeout | 600s 自動拒否（hook 実装） | サーバ側実装 | 同等（実装場所が異なる） |
| skill auto-allow | `lib_skill_perms.py`（有効） | 変更なし（引き続き有効） | 変更不要 |
| 複数 Worker 一元管理 | ✅ Kanban で一覧表示 | ❌ Worker ごとに channel session | Taskvia が優位 |
| オフライン動作 | ❌ Taskvia 依存 | ✅ stdio local（サーバ不要） | Channels が優位 |
| モバイル承認 | ❌ Web UI（スマホ不便） | ✅ Discord/Telegram app | Channels が優位 |

### 統合ポジション

Channels は Taskvia の**代替ではなく補完**として位置付ける:

- **Taskvia**: 複数エージェントの visual management・監査ログ・Kanban
- **Channels relay**: モバイル承認・低レイテンシ・Taskvia 不可時フォールバック

---

## 4. 3シナリオ評価

### シナリオA: 全面置換（Taskvia → Channels）

**概要**: `pre-tool-use.sh` の Taskvia 呼び出しを Channels permission relay に全面置き換え。

| 項目 | 内容 |
|------|------|
| **Benefit** | ポーリング遅延解消・HTTP 依存排除・モバイル承認対応 |
| **Cost** | 工数大（pre-tool-use.sh 全面改修・Taskvia 撤去・channel server 実装）。Kanban 一元管理が失われる |
| **リスク** | permission_request の動作が **PoC 未確認**（インタラクティブセッション必須・`-p` 非対応）。Crewvia は `-p` モードで稼働しており Channels が機能するか不明 |
| **判定** | ❌ 現時点では推奨しない |

### シナリオB: 部分採用（承認 UI のみ Channels を追加）

**概要**: Taskvia を維持しつつ、`pre-tool-use.sh` に Channels relay オプションを追加。`CREWVIA_TASKVIA=disabled` 時または Taskvia タイムアウト時にフォールバックとして使用。

| 項目 | 内容 |
|------|------|
| **Benefit** | Taskvia の Kanban 管理を維持しながらモバイル承認・フォールバック経路を追加。リスク低。 |
| **Cost** | 工数中（channel server 実装・pre-tool-use.sh に分岐追加・ペアリング手順整備） |
| **リスク** | permission_request の実動作がインタラクティブセッション限定である点（PoC 未確認）はシナリオA と同様。ただしフォールバックとして導入するため影響は限定的 |
| **判定** | ⚠️ PoC Phase 2（インタラクティブ確認）完了後に判断推奨 |

### シナリオC: 見送り

**概要**: Channels 統合を現時点では実施しない。Taskvia 運用を継続。

| 項目 | 内容 |
|------|------|
| **Benefit** | 追加工数ゼロ。現行運用に支障なし |
| **Cost** | ポーリング遅延・Taskvia 単一障害点は解消されない |
| **リスク** | なし |
| **判定** | ✅ permission_request 未確認問題が解決するまでの現実的選択 |

---

## 5. Wei 事案（TUI 漏れ）との関係

PoC 調査で判明した制約: **`claude -p`（非インタラクティブ）モードでは `permission_request` が発火しない**。

Crewvia の Worker は Dispatcher から tmux send-keys でタスクを受信し、`claude -p` または bare モードで稼働している。この場合 Channels の permission relay は**機能しない可能性が高い**。

これは TUI（ターミナル UI）が必要な機能がバックグラウンド実行で使えない「TUI 漏れ」問題の一種として捉えられる。

### 影響範囲

| 稼働モード | permission_request 発火 | 備考 |
|-----------|------------------------|------|
| `claude`（インタラクティブ） | ✅ 発火する見込み | 未確認・PoC Phase 2 で要検証 |
| `claude -p`（非インタラクティブ） | ❌ 発火しない（PoC 確認済み） | Crewvia Worker の現行モード |
| `claude --bare`（bare モード） | ❓ 不明 | 追加調査が必要 |

この制約が解消されない限り、シナリオA・Bともに Crewvia への本格統合は困難。

> 詳細なテスト実施記録: [fakechat PoC 動作確認ログ](docs/poc/channels-sample/poc-log.md)

---

## 6. セキュリティ論点

### allowlist ペアリング管理

- Channels はペアリングコードで送信者を認証する。ペアリング情報の保管先（crewvia repo 内か外部か）を決める必要がある
- ペアリングコードをリポジトリに誤 commit するリスクがある → `.gitignore` 対象に追加が必須

### stdin injection リスク

- channel server は外部から stdin 経由でメッセージを Claude Code に注入できる
- `sender_allowlist` の実装が不完全な場合、悪意ある送信者が任意のツール実行を要求できる
- **対策**: allowlist の厳密な実装（PoC では省略済み）。本番導入前に必ず実装すること

### Taskvia と Channels の並存時の二重承認リスク

- シナリオB（部分採用）では Taskvia と Channels が同時に動作する瞬間がある
- 同一リクエストを両方で承認/拒否した場合の動作を明確に定義しておく必要がある

---

## 7. 推奨アクション

**現時点の推奨: シナリオC（見送り）＋ PoC Phase 2 実施**

### 根拠

PoC で確認できた事項:
- ✅ `claude/channel` capability 宣言・接続・ハンドシェイクは動作する
- ❌ `permission_request` の発火は `-p` モードで未発火（PoC 確認済み）

Crewvia の Worker は `-p` モードで稼働しており、Channels の核心機能（permission relay）が使えるかどうかが未確定のまま統合を進めるのはリスクが高い。

### 推奨ロードマップ

1. **今すぐ**: 現状維持（シナリオC）
2. **PoC Phase 2**: インタラクティブセッション（`claude` のみ）で Bash/Write を含むタスクを実行し、`permission_request` の発火・verdict 適用を確認する
3. **Phase 2 成功時**: シナリオB（部分採用）の詳細設計に進む
4. **Phase 2 失敗時**: Channels は `-p` モードとの非互換として記録し、中期は Taskvia 継続・長期は Claude Code のアップデートを待つ

---

## 8. 後続ミッション概要（シナリオB 採用時）

PoC Phase 2 で permission_request の動作が確認できた場合の後続ミッション案:

### M8: Channels channel server 実装

- `docs/poc/channels-sample/fakechat-server.js` を本番用 channel server に昇格
- sender allowlist の実装（PoC で省略した部分）
- Discord または Telegram への転送ロジック実装

### M9: pre-tool-use.sh Channels フォールバック統合

- `CREWVIA_TASKVIA=disabled` フラグ検知ロジック追加
- Taskvia タイムアウト時の Channels relay へのフォールバック実装
- 統合テスト（Worker 実行 + Channels 承認の end-to-end 確認）

### M10: ドキュメント・運用手順整備

- ペアリング手順書（worker.md §9 相当）
- Taskvia/Channels 切り替えガイド
- セキュリティチェックリスト（allowlist 設定・.gitignore 確認）
