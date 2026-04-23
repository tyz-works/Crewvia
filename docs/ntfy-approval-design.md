# ntfy 承認通知 — Crewvia 側設計書

作成: 2026-04-21 / Worker: Lin (t001)

---

## 1. 調査対象と概要

Taskvia (`tyz-works/taskvia`) の以下を調査し、Crewvia へのntfy組み込み設計を確定する。

| ファイル | 役割 |
|---|---|
| `src/lib/ntfy.ts` | ntfy publish クライアント + 承認トークン生成 |
| `src/app/api/request/route.ts` | 承認リクエスト投入 → ntfy publish 呼び出し |
| `src/app/api/approve-token/[token]/route.ts` | ntfy アクションボタン "✓承認" 受け口 |
| `src/app/api/deny-token/[token]/route.ts` | ntfy アクションボタン "✗却下" 受け口 |
| `src/app/api/status/[id]/route.ts` | Crewvia pre-tool-use.sh がポーリングするエンドポイント |

---

## 2. Taskvia ntfy 実装詳細

### 2.1 ntfy publish リクエスト形式

```
POST ${NTFY_URL}/${NTFY_TOPIC}
Content-Type: text/plain       ← body がテキスト
Authorization: Basic base64(user:pass)   ← NTFY_USER/NTFY_PASS 設定時のみ

Title:    [${agent}] ${tool} 承認要求
Priority: high
Tags:     lock
Click:    ${TASKVIA_BASE_URL}/requests/${requestId}
Actions:  http, ✓承認, ${approveUrl}, method=POST, clear=true; http, ✗却下, ${denyUrl}, method=POST, clear=true

Body: 承認待ち: ${tool}
```

- `approveUrl` = `${TASKVIA_BASE_URL}/api/approve-token/${token}`
- `denyUrl`    = `${TASKVIA_BASE_URL}/api/deny-token/${token}`
- `token` = `nanoid(32)` (32文字のランダムID)

### 2.2 承認トークンの生成・保存

```
Redis key:  approval_token:{token}
Value (JSON):
  {
    request_id: string,   ← approval:{id} カードのID
    decision: null | "approved" | "denied",
    expires_at: ISO8601,
    consumed_at: null | ISO8601
  }
TTL: APPROVAL_TOKEN_TTL_SECONDS (デフォルト 900秒)
```

### 2.3 approve-token / deny-token エンドポイント

`POST /api/approve-token/{token}` / `POST /api/deny-token/{token}`

| ケース | HTTP status | レスポンス |
|---|---|---|
| 正常処理 | 200 | `{ ok: true }` |
| 存在しない / TTL切れ | 404 | `{ error: "invalid_or_expired_token" }` |
| 消費済み | 409 | `{ error: "token_already_used" }` |

**認証不要** — トークン自体が秘密。

処理内容:
1. `approval_token:{token}` の `decision` と `consumed_at` を更新
2. `approval:{request_id}` カードの `status` を `approved` / `denied` に更新
3. トークンの TTL を 60 秒に短縮 (消費後の残存)

### 2.4 POST /api/request — 現状の動作

```json
Request:  { tool, agent, task_title, task_id, priority }
Response: { id }
```

カード `approval:{id}` 作成 (TTL 600s) → `publishApprovalRequest()` 呼び出し (ntfy送信) → `{ id }` 返却。

> **現状の問題点**: レスポンスにトークン URL が含まれない。
> Crewvia が直接 ntfy を送りたい場合、approve/deny URL を知る手段がない。

---

## 3. 承認フロー図

### 現行フロー (mode=taskvia)

```
Crewvia pre-tool-use.sh
  │
  ├─ POST /api/request  ────────────────────────────→  Taskvia
  │    { tool, agent, ... }                               │
  │                                                       ├─ Redis: approval:{id} 作成 (TTL 600s)
  │                                                       ├─ Redis: approval_token:{t} 作成 (TTL 900s)
  │                                                       └─ ntfy publish (NTFY_URL 設定時)
  │                                                             ↓
  │                                                    [スマホ ntfy通知]
  │                                                    [アクションボタン]
  │                                                      ✓承認 / ✗却下
  │                                                             ↓
  │                                              POST /api/approve-token/{t}
  │                                                 or /api/deny-token/{t}
  │                                                             ↓
  │                                              Redis: approval:{id}.status 更新
  │
  ├─ GET /api/status/{id}  [1秒毎, 最大600秒]
  │    status=pending → 続行
  │    status=approved → emit_decision allow; exit 0
  │    status=denied   → emit_decision deny;  exit 0
  │    not_found       → emit_decision deny;  exit 0
```

### ntfy 直接送信フロー (mode=ntfy / mode=both) — 要 API 変更

```
Crewvia pre-tool-use.sh
  │
  ├─ POST /api/request  ────────────────────────────→  Taskvia
  │    { tool, agent, ..., ntfy: true }                  │
  │                                                       ├─ Redis: approval:{id} 作成
  │                                                       ├─ Redis: approval_token:{t} 作成
  │    ← { id, approve_url, deny_url } ──────────────────┤
  │                                                       └─ (mode=both: Taskvia も ntfy 送信)
  │
  ├─ ntfy publish  [Crewvia 側から直接]
  │    POST ${NTFY_URL}/${NTFY_TOPIC}
  │    Actions: http,✓承認,${approve_url},...; http,✗却下,${deny_url},...
  │
  ├─ GET /api/status/{id}  [1秒毎, 最大600秒]  ← ポーリングは既存フローと同じ
```

> 既存のポーリング先 `/api/status/{id}` は変更不要。
> トークンベースの承認も `approval:{id}.status` を更新するため。

---

## 4. Taskvia 側に必要な API 変更 (最小限)

`POST /api/request` のレスポンスを拡張する:

```json
{
  "id": "...",
  "approve_url": "https://taskvia.vercel.app/api/approve-token/abc...",
  "deny_url":    "https://taskvia.vercel.app/api/deny-token/abc..."
}
```

- トークンが生成されない場合 (NTFY_URL 未設定かつ Crewvia が ntfy を使わない場合) は `null`
- `ntfy: true` フラグをリクエストに含めることでトークン生成を強制できる

---

## 5. Crewvia config 設定項目

`config/crewvia.yaml` に `approval_channel` セクションを追加:

```yaml
# ===== 承認チャネル設定 =====
# mode:
#   taskvia  — Taskvia WebUI + Taskvia側ntfy（デフォルト）
#   ntfy     — Crewvia から直接 ntfy を送信、WebUI は使わない
#   both     — Crewvia から ntfy + Taskvia WebUI の両方
#
# 環境変数での上書き: CREWVIA_APPROVAL_CHANNEL=taskvia|ntfy|both
approval_channel:
  mode: taskvia

  ntfy:
    # ntfy サーバーの URL (例: https://ntfy.example.com)
    # 環境変数: NTFY_URL
    url: ""

    # 通知トピック名
    # 環境変数: NTFY_TOPIC
    topic: ""

    # Basic 認証 (任意)
    # 環境変数: NTFY_USER / NTFY_PASS
    user: ""
    pass: ""

    # ntfy アクションボタン用トークンの TTL (秒)
    # Taskvia 側の APPROVAL_TOKEN_TTL_SECONDS と合わせること
    # 環境変数: APPROVAL_TOKEN_TTL_SECONDS
    token_ttl_seconds: 900
```

### 5.1 設定優先順位

```
env var > CLI フラグ > config/crewvia.yaml > デフォルト値
```

| 設定項目 | 環境変数 | デフォルト |
|---|---|---|
| approval_channel.mode | `CREWVIA_APPROVAL_CHANNEL` | `taskvia` |
| approval_channel.ntfy.url | `NTFY_URL` | `""` |
| approval_channel.ntfy.topic | `NTFY_TOPIC` | `""` |
| approval_channel.ntfy.user | `NTFY_USER` | `""` |
| approval_channel.ntfy.pass | `NTFY_PASS` | `""` |
| approval_channel.ntfy.token_ttl_seconds | `APPROVAL_TOKEN_TTL_SECONDS` | `900` |

---

## 6. TTL・タイムアウト設計

| 対象 | TTL | 備考 |
|---|---|---|
| カード `approval:{id}` | 600s | /api/request 作成時。approve/deny後も600s維持 |
| トークン `approval_token:{t}` | 900s (デフォルト) | 消費後は60sに短縮 |
| pre-tool-use.sh ポーリング上限 | 600s | `TIMEOUT` 変数 |
| pre-tool-use.sh ポーリング間隔 | 1s | 現行固定値 |

**TTL整合性**:
- カード(600s) < トークン(900s): ユーザーがトークンで承認後、カードが先に消えるリスクあり
- 対策: カードの TTL を 900s に伸ばすか、トークン TTL を 600s に揃えることを推奨
- 現状は許容範囲 (ntfy ボタンタップは通常 1〜2 分以内)

---

## 7. lib_approval_channel.sh の責務

新規ファイル `hooks/lib_approval_channel.sh` が実装すべき関数:

```bash
# モードを返す (taskvia|ntfy|both)
get_approval_channel_mode()

# ntfy 設定を env から/config から読み込む
load_ntfy_config()

# ntfy 通知を送信する
# 引数: agent tool summary approve_url deny_url
ntfy_publish()

# approve_url / deny_url を取得 (POST /api/request のレスポンスから)
parse_token_urls()  # { id, approve_url, deny_url }
```

---

## 8. pre-tool-use.sh の変更点サマリー

`hooks/pre-tool-use.sh` で変更が必要な箇所:

1. `lib_approval_channel.sh` を source する (冒頭)
2. `POST /api/request` のレスポンスから `approve_url`, `deny_url` をパースする
3. `mode=ntfy or both` の場合: `ntfy_publish()` を呼ぶ
4. ポーリングロジックは **変更不要** (`/api/status/{id}` をそのまま使う)

---

## 9. フォールバック設計

| 状態 | 動作 |
|---|---|
| `NTFY_URL` 未設定 + mode=ntfy | エラーログ出力 → taskvia フォールバック |
| ntfy publish 失敗 (タイムアウト/接続エラー) | ログ出力 → 継続 (ポーリングは続く) |
| `/api/request` が `approve_url` を返さない | ntfy 送信スキップ → taskvia フォールバック |
| mode=both + Taskvia ntfy 設定済み | 通知が二重送信される (設計上許容) |

---

## 10. 実装チェックリスト (t002〜t005 向け)

- [ ] **t002**: `config/crewvia.yaml` に `approval_channel` セクション追加
- [ ] **t002**: `hooks/lib_approval_channel.sh` 新規作成
- [ ] **t002**: `scripts/start.sh` が ntfy 設定を env に展開する部分を追加
- [ ] **t003**: `src/app/api/request/route.ts` に `approve_url`/`deny_url` を返す変更 (Taskvia側)
- [ ] **t003**: `hooks/pre-tool-use.sh` に ntfy publish ロジック追加
- [ ] **t004**: フォールバック・エラーハンドリング実装
- [ ] **t005**: CLAUDE.md 更新 + PR作成
