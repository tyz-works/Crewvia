# ntfy Phase 2 仕様乖離ギャップ分析

作成: 2026-04-22 / Data (task_089_data)

参照元:
- 提案書: `~/obsidian/proposals/crewvia/20260420_ntfy-server-handoff.md`
- 設計書: `docs/ntfy-approval-design.md` (Worker Lin / t001、2026-04-21)
- 現状コード: Taskvia 5ファイル + crewvia 2ファイル

---

## セクション 1: Taskvia 側ギャップ

| # | 項目 | 提案書の要件 | 現状コード | 必要な修正 | 担当 |
|---|---|---|---|---|---|
| T1 | POST /api/request レスポンス | `{ id, approve_url, deny_url }` を返す (提案書§4.1, 設計書§4) | `{ id }` のみ返す | approve_url / deny_url をレスポンスに追加 | Geordi |
| T2 | publishApprovalRequest 戻り値 | approve_url / deny_url を呼び出し元に渡す必要あり | `void` — トークン・URL を返さない | 関数がトークンを返すか、route.ts 側でトークン生成して URL を構築 | Geordi |
| T3 | notify フラグ | `notify: true` フラグで ntfy を制御 (提案書§4.1) | フラグなし。NTFY_URL が設定されていれば常時送信 | `ntfy: true` パラメータ追加、またはNTFY_URL設定有無で暗黙判定に統一 | Geordi |
| T4 | カード TTL vs トークン TTL | 提案書§5: card=600s, token=900s (設計書§6 で整合性問題を指摘) | card=600s, token=900s (数値は一致、ただし card が先に失効) | トークン使用後に card TTL を 900s 以上に延長するか、TTL を 600s に揃える | Geordi (要判断) |
| T5 | エンドポイント名 | 提案書§4.2/4.3: `/api/approve/[token]` `/api/deny/[token]` | `/api/approve-token/[token]` `/api/deny-token/[token]` | **設計書§2.3 が既に `-token` 形式に更新済み** → 設計書を正として現状コードが正しい。提案書は旧仕様。乖離は設計書で解消済み |  -(対応不要) |

---

## セクション 2: Crewvia 側ギャップ

| # | 項目 | 提案書/設計書の要件 | 現状コード/設定 | 必要な修正 | 担当 |
|---|---|---|---|---|---|
| C1 | ntfy Basic 認証 | NTFY_USER/NTFY_PASS を設定 (提案書§5, 設計書§5) | crewvia.yaml: `user: ""` `pass: ""` | ntfy サーバーは `auth-default-access: deny-all` のため認証必須。NTFY_USER=taskvia / NTFY_PASS を設定 | Geordi (crewvia.yaml / env) |
| C2 | トピック名規則 | `taskvia-approval-{ランダム}` 形式 (提案書§5) | crewvia.yaml: `PhCcCUMzsE0J11pWGZT0aomA5mQewIOv` (プレフィックスなし) | ntfy `iphone` ユーザーの ACL は `taskvia-approval-*` のみ ro 許可。現トピックでは iphone が subscribe 不可 | Geordi (crewvia.yaml) |
| C3 | crewvia.yaml 重複キー | `taskvia:` は一度だけ定義 | `taskvia: ask` が2箇所 (L48 / L66) に重複定義 — YAML パーサが後者で上書き | 重複エントリを削除して一本化 | Geordi |
| C4 | pre-tool-use.sh の lib 統合 | 設計書§8: pre-tool-use.sh が lib_approval_channel.sh を source し、approve_url/deny_url をパースして ntfy_publish() を呼ぶ | pre-tool-use.sh の確認未実施 (タスク対象外) | Geordi が pre-tool-use.sh の実装状況を確認・補完 | Geordi |
| C5 | approve_url/deny_url パース | 設計書§7: `parse_token_urls()` で /api/request レスポンスから URL を抽出 | `parse_token_urls()` 実装済み (lib_approval_channel.sh L151-157) | Taskvia 側 T1 修正が前提。T1 完了後に動作確認 | - (T1 依存) |

---

## セクション 3: 提案書 vs 設計書(ntfy-approval-design.md)の差分

| # | 項目 | 提案書 | 設計書 | 結論 |
|---|---|---|---|---|
| D1 | ntfy Actions タイプ | `view` + `shortcuts://` URL (iOS Shortcut 起動) | `http` + 直接 HTTP POST | 設計書が Phase 2 の正仕様。提案書の `view+shortcuts` は Phase 3 (手動)。コードは設計書通りで正しい |
| D2 | エンドポイント名 | `/api/approve/[token]` | `/api/approve-token/[token]` | 設計書が改訂版。現コードは設計書通りで正しい |
| D3 | mode=both の crewvia 設定 | 提案書では Taskvia 経由のみ言及 | 設計書§3 で `mode=both` フロー追加 | crewvia.yaml は `mode: both` 設定済み。設計書通り |

---

## Geordi Phase B で特に注意すべきポイント

1. **T1 + T2 が連動**: `/api/request` レスポンスに `approve_url`/`deny_url` を返すには、`publishApprovalRequest()` が void のまま使えない。route.ts 内でトークンを先に生成→URL構築→ntfy に渡す→レスポンスに URL を含める、という順序リファクタが必要。既存の `publishApprovalRequest()` シグネチャ変更が波及する。

2. **C1 + C2 が E2E 動作の前提**: crewvia が `mode=ntfy` または `mode=both` で直接 ntfy 送信する際、認証情報とトピック名が正しくないと iPhone に通知が届かない。Beverly の E2E テスト (Phase D) 前に必ず修正すること。

---

## スキル候補

- `claude-api`: ntfy.ts の `publishApprovalRequest()` リファクタ時に Upstash Redis / nanoid 使用パターン確認
- `vercel-functions`: route.ts の Next.js App Router `params: Promise<{...}>` 形式は最新仕様 — 変更時は慎重に
