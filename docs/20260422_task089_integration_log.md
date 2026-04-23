# task_089 Phase C — 統合疎通テストログ

実施日時: 2026-04-22 23:01:05
実施者: Worf (Lt. Commander, Tactical)
対象ブランチ:
  - Taskvia: feat/task089-ntfy-phase2-alignment @ e52348e
  - crewvia:  feat/task089-ntfy-phase2-alignment @ 39e81c4
方針: α方針 (Taskvia ntfy統一)

---

## カテゴリ 1: ntfy 直接疎通

### 1-a: 認証あり → 200 期待
  curl -u taskvia:**** -d 'Phase C 1-a' https://ntfy.elni.net/<TOPIC>
  期待: 200 | 実際: 200
  ✅ PASS: 1-a ntfy 認証あり 200

### 1-b: 認証なし → 401 期待
  curl (no auth) https://ntfy.elni.net/<TOPIC>
  期待: 401 | 実際: 200
  ❌ FAIL: 1-b ntfy 認証なし: got 200

### 1-c: 誤パスワード → 401 期待
  curl -u taskvia:wrongpassword https://ntfy.elni.net/<TOPIC>
  期待: 401 | 実際: 401
  ✅ PASS: 1-c ntfy 誤パスワード 401

## カテゴリ 2: Taskvia ローカル起動


## dev server 起動中...
  PID: 49564
  → サーバー起動確認 (2s)

### 2-a: notify:true → {id} + ntfy 通知
  curl POST /api/request notify:true
  レスポンス: {"id":"XxZxeVT7Qa0XncXrMCZg7"}
  ✅ PASS: 2-a notify:true → {id} = XxZxeVT7Qa0XncXrMCZg7
  ✅ PASS: 2-a α方針: レスポンスは {id} のみ (approve_url/deny_url なし)

### 2-b: notify 未指定 → {id} 返却 (ntfy は届かないはず)
  curl POST /api/request (notify なし)
  レスポンス: {"id":"LN_XPh2Fyzf54Vao3sUJl"}
  ✅ PASS: 2-b notify 未指定 → {id} 返却確認
  ※ ntfy 未着信は iPhone 側で目視確認が必要 (Phase C では自動判定不可)

### 2-c: Redis 格納確認 — approval_token:<token> TTL ~900s
  Upstash GET approval:XxZxeVT7Qa0XncXrMCZg7
  レスポンス型: str
  card status: pending
  ✅ PASS: 2-c approval:XxZxeVT7Qa0XncXrMCZg7 Redis 格納確認
  TTL: 599s (期待: ~600s)
  ✅ PASS: 2-c TTL > 0 確認 (599s)

## カテゴリ 3: approve-token / deny-token

### 3-x: テスト用 approval_token を取得
  POST /api/request → id: YxQNwHLl7GnSwemfiErHt
  approval_token 取得成功 (長さ: 32)

### 3-a: approve-token 正常 → 200 {ok:true}
  POST /api/approve-token/<token>
  status: 200 | body: {"ok":true}
  ✅ PASS: 3-a approve-token 200 {ok:true}

### 3-b: approve-token 再送 → 409
  POST /api/approve-token/<same_token>
  status: 409 | body: {"error":"token_already_used"}
  ✅ PASS: 3-b approve-token 再送 409

### 3-e: 消費後 token TTL 短縮確認 (期待: ~60s)
  approval_token TTL: 59s (期待: ≤60s)
  ✅ PASS: 3-e 消費後 TTL 短縮: 59s (≤60)

### 3-c: 不在 token → 404
  POST /api/approve-token/nonexistent_token_worf_test
  status: 404 | body: {"error":"invalid_or_expired_token"}
  ✅ PASS: 3-c 不在 token 404

### 3-d: deny-token 正常 → 200
  POST /api/deny-token/<token>
  status: 200 | body: {"ok":true}
  ✅ PASS: 3-d deny-token 200 {ok:true}
  再送: status: 409
  ✅ PASS: 3-d deny-token 再送 409

## カテゴリ 4: Status ポーリング

### 4-a: approved card のステータス
  GET /api/status/YxQNwHLl7GnSwemfiErHt
  status: approved
  ✅ PASS: 4-a approved card status=approved

### 4-b: denied card のステータス
  GET /api/status/ZwhgwSQutAo5GZLVUtLcz
  status: pending
  ❌ FAIL: 4-b status: got 'pending' (expected 'denied')

### 4-c: pending card のステータス
  GET /api/status/novL5REosVXo4hZNq-tNL
  status: pending
  ✅ PASS: 4-c pending card status=pending

## カテゴリ 5: crewvia hook 統合

### 5-1: bash -n 構文チェック
  ✅ PASS: 5-1 lib_approval_channel.sh bash -n
  ✅ PASS: 5-1 pre-tool-use.sh bash -n

### 5-2: α方針残骸チェック
  ✅ PASS: 5-2 ntfy_publish/parse_token_urls 実コード残骸なし

### 5-3: CREWVIA_APPROVAL_CHANNEL=taskvia モード — /api/request 呼び出し確認
  (Taskvia dev server が既に起動中)
  pre-tool-use.sh 実行ログ (抜粋):
  ✅ PASS: 5-3 dev server ログで /api/request 呼び出し確認

### 5-4: CREWVIA_APPROVAL_CHANNEL=ntfy — ntfy 直叩き除去確認
  get_approval_channel_mode with CREWVIA_APPROVAL_CHANNEL=ntfy: ntfy
  ✅ PASS: 5-4 ntfy チャネルモード返却正常

### 5-5: CREWVIA_APPROVAL_CHANNEL=both — 既存動作確認
  get_approval_channel_mode with CREWVIA_APPROVAL_CHANNEL=both: both
  ✅ PASS: 5-5 both チャネルモード返却正常

---

## テスト結果サマリー

- 合計: 23 項目
- ✅ PASS: 21
- ❌ FAIL: 2

**判定: FAIL あり — 差し戻し要確認**

## Phase D (Beverly) 引き継ぎメモ

1. iPhone ntfy subscribe 確認: https://ntfy.elni.net/<TOPIC> (認証: taskvia/****)
   → NTFY_USER/NTFY_PASS が正しく設定され、通知が届くことを目視確認
2. TASKVIA_BASE_URL が本番 URL (https://taskvia.vercel.app) に設定されていること
   → ntfy アクションボタンの approve/deny URL が本番を向くため
3. APPROVAL_TOKEN_TTL_SECONDS のデフォルトは 900s — Phase D E2E では時間余裕あり
4. /api/approve-token と /api/deny-token は Bearer 認証不要 (token 自体が秘密)
5. 本番では Redis の approval:* キーが TTL 600s で自動消滅する点を考慮
