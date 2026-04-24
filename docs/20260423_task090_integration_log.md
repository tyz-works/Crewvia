# task_090 Phase E — 統合疎通テストログ

実施日時: 2026-04-23 22:58:28
実施者: Worf (Lt. Commander, Tactical)
対象コミット:
  - Taskvia Phase B: 8f47163 (POST /api/verification)
  - Taskvia Phase C: 838b842 (GET 3本実装)
  - Taskvia Phase D: b8b4bce + ae970d6 (UI + hotfix)
方針: task_089 教訓 L1/L2/L3 適用 (env 確認 + POST/GET 明記)
TASKVIA_TOKEN: EMPTY → open mode

---


## dev server 起動中...
  PID: 23556
  → サーバー起動確認 (2s)

## カテゴリ 1: POST /api/verification
  TEST_SLUG: worf-test-mission-1776952711
  TEST_TASK_ID: worf_task_1776952712

### 1-a: POST /api/verification 正常 → 200 {ok:true}
  POST /api/verification
  status: 200 | body: {"ok":true,"task_id":"worf_task_1776952712"}
  ✅ PASS: 1-a POST /api/verification 200 {ok:true, task_id}

### 1-b: Bearer なし → 401 期待 (open mode では 200)
  POST /api/verification (no auth)
  status: 200
  ⚠️ WARN: 1-b open mode: got 200 (TASKVIA_TOKEN 未設定のため、401 未テスト)

### 1-c: Bearer wrong → 401 期待 (open mode では 200)
  POST /api/verification (wrong Bearer)
  status: 200
  ⚠️ WARN: 1-c open mode: got 200 (TASKVIA_TOKEN 未設定のため、401 未テスト)

### 1-d: schema 不正 (task_id 欠落) → 400
  POST /api/verification (task_id 欠落)
  status: 400 | body: {"error":"missing_required_fields"}
  ✅ PASS: 1-d schema 不正 400

### 1-e: Redis 3key 確認 (TTL/値)
  verification:worf_task_1776952712: val=EXISTS(     351bytes) | TTL=604799s
  ✅ PASS: 1-e verification:worf_task_1776952712 存在 + TTL 7d (604799s)
  verification:index:worf-test-mission-1776952711: len=3 | TTL=-1s
  ✅ PASS: 1-e verification:index:worf-test-mission-1776952711 len=3
  verification:history:worf_task_1776952712: len=3 | TTL=604798s
  ✅ PASS: 1-e verification:history:worf_task_1776952712 len=3

## カテゴリ 2: GET /api/verification-queue

### 2-a: GET /api/verification-queue?mission=worf-test-mission-1776952711 → 200 {queue:[...]}
  GET /api/verification-queue?mission=worf-test-mission-1776952711
  status: 200 | body: {"queue":[{"task_id":"worf_task_1776952712","mission_slug":"worf-test-mission-1776952711","mode":"standard","verdict":"pass","rework_count":0,"verified_at":"2026-04-23T13:58:32Z","verifier":"worf"},{"task_id":"worf_task_1776952712","mission_slug":"worf-test-mission-1776952711","mode":"standard","verdict":"pass","rework_count":0,"verified_at":"2026-04-23T13:58:32Z","verifier":"worf"},{"task_id":"worf_task_1776952712","mission_slug":"worf-test-mission-1776952711","mode":"standard","verdict":"pass","rework_count":0,"verified_at":"2026-04-23T13:58:32Z","verifier":"worf"}]}
  ✅ PASS: 2-a GET verification-queue 200 {queue: 3 items}

### 2-b: GET /api/verification-queue?mission=nonexistent → 200 {queue:[]}
  GET /api/verification-queue?mission=nonexistent
  status: 200 | body: {"queue":[]}
  ✅ PASS: 2-b 存在しない mission → 200 {queue:[]}

### 2-c: GET /api/verification-queue?mission=empty_slug → 200 {queue:[]}
  GET /api/verification-queue?mission=worf_empty_*
  status: 200 | body: {"queue":[]}
  ✅ PASS: 2-c 空 queue → 200 {queue:[]}

### 2-d: GET /api/verification-queue 認証なし → 401 期待 (open mode では 200)
  GET /api/verification-queue (no auth)
  status: 200
  ⚠️ WARN: 2-d open mode: got 200 (TASKVIA_TOKEN 未設定、401 未テスト)

### 2-e: GET /api/verification-queue (mission= なし) → 400
  GET /api/verification-queue (no mission param)
  status: 400 | body: {"error":"mission parameter required"}
  ✅ PASS: 2-e mission= なし → 400

## カテゴリ 3: GET /api/cards/:id/verification

### 3-a: GET /api/cards/worf_task_1776952712/verification → 200 {verification:{...}}
  GET /api/cards/worf_task_1776952712/verification
  status: 200 | body: {"verification":{"task_id":"worf_task_1776952712","mission_slug":"worf-test-mission-1776952711","mode":"standard","verdict":"pass","checks":[{"name":"bash-n","status":"pass","duration_s":0.1},{"name":"alpha-residual","status":"pass","duration_s":0.05}],"rework_count":0,"verified_at":"2026-04-23T13:58:32Z","verifier":"worf","received_at":"2026-04-23T13:58:32.713Z"}}
  ✅ PASS: 3-a GET verification 200 {verification: {...}}

### 3-b: GET /api/cards/nonexistent/verification → 仕様:404 / 実装:200+null
  GET /api/cards/nonexistent/verification
  status: 200 | body: {"verification":null}
  ❌ FAIL: 3-b 不存在 task_id: got 200 (verification=None) — 仕様は 404、実装は 200+null (差し戻し候補)

### 3-c: GET /api/cards/:id/verification 認証なし → 401 (open mode では 200)
  GET /api/cards/worf_task_1776952712/verification (no auth)
  status: 200
  ⚠️ WARN: 3-c open mode: got 200 (TASKVIA_TOKEN 未設定、401 未テスト)

### 3-d: schema 全フィールド確認
  schema チェック: OK
  ✅ PASS: 3-d schema 全フィールド揃い

### 3-e: TTL 7日確認 (期待: ~604800s)
  verification:worf_task_1776952712 TTL: 604794s (期待: ~604800s)
  ✅ PASS: 3-e TTL 7日: 604794s

## カテゴリ 4: GET /api/cards/:id/rework-history

### 4-a: GET /api/cards/worf_task_1776952712/rework-history → 200 cycles:[2+]
  GET /api/cards/worf_task_1776952712/rework-history
  status: 200 | body: {"cycles":[{"cycle":0,"verdict":"pass","failed_checks":[],"verified_at":"2026-04-23T13:58:32Z"},{"cycle":0,"verdict":"pass","failed_checks":[],"verified_at":"2026-04-23T13:58:32Z"},{"cycle":0,"verdict":"pass","failed_checks":[],"verified_at":"2026-04-23T13:58:32Z"},{"cycle":1,"verdict":"fail","failed_checks":[{"name":"bash-n","status":"fail","duration_s":0.2}],"verified_at":"2026-04-23T13:58:38Z"}]}
  ✅ PASS: 4-a rework-history 200 {cycles: 4 items}

### 4-b: 1 cycle のみ → 200 {cycles:[1要素]}
  GET /api/cards/worf_1cycle_1776952719/rework-history
  status: 200 | body: {"cycles":[{"cycle":0,"verdict":"pass","failed_checks":[],"verified_at":"2026-04-23T13:58:39.862Z"}]}
  ✅ PASS: 4-b 1 cycle → {cycles:[1]}

### 4-c: 履歴なし task_id → 200 {cycles:[]}
  GET /api/cards/worf_no_history_task/rework-history
  status: 200 | body: {"cycles":[]}
  ✅ PASS: 4-c 履歴なし → 200 {cycles:[]}

### 4-d: 不存在 task_id → 仕様:404 / 実装:200+[] (仕様乖離)
  GET /api/cards/truly_nonexistent_xyz_worf/rework-history
  status: 200 | body: {"cycles":[]}
  ❌ FAIL: 4-d 不存在 task_id: got 200 (仕様は 404、実装は 200+[] — 差し戻し候補)

## カテゴリ 5: 既存動作回帰

### 5-a: POST /api/request notify:true → 200 {id}
  POST /api/request notify:true
  status: 200 | body: {"id":"f5h2gM0kJ_Tk1BFHGDt13"}
  ✅ PASS: 5-a POST /api/request notify:true → 200 {id}

### 5-b: POST /api/request (notify なし) → 200 {id}
  POST /api/request (notify なし)
  status: 200 | body: {"id":"wppE5flsf9XqVJUPdIgR8"}
  ✅ PASS: 5-b POST /api/request no-notify → 200 {id}

### 5-c: POST /api/log → 200 (既存エンドポイント回帰)
  POST /api/log
  status: 200
  ✅ PASS: 5-c POST /api/log 200

### 5-d: CREWVIA_VERIFICATION_UI=disabled feature flag
  Taskvia 側に feature flag 参照確認:
  ✅ PASS: 5-d CREWVIA_VERIFICATION_UI feature flag が Taskvia src に存在

---

## テスト結果サマリー

- 合計: 25 項目
- ✅ PASS: 19
- ❌ FAIL: 2
- ⚠️ WARN: 4 (open mode による 401 未テスト)

### FAIL 詳細
  3-b: GET /api/cards/:id/verification — 不存在 id が 404 でなく 200+null を返す (仕様乖離)
  4-d: GET /api/cards/:id/rework-history — 不存在 id が 404 でなく 200+[] を返す (仕様乖離)

**判定: WARN — 仕様乖離 2件あり (差し戻し要検討)、それ以外は PASS**

## Beverly (Phase E E2E) 引き継ぎメモ

1. **pnpm dev 事前起動必須**: Board UI は localhost:3000 で確認。polling 5s は dev server 起動後に自動開始。
2. **TASKVIA_TOKEN 未設定 = open mode**: 認証なしで全 API にアクセス可能。E2E では実認証をテストする場合は .env.local に追記。
3. **verification-queue は ?mission= 必須**: mission slug なしの全件取得 API は存在しない。crewvia からは slug を渡すこと。
4. **3-b/4-d 仕様乖離**: GET verification/rework-history で不存在 id が 404 でなく 200+null/[] を返す。E2E では UI が null/[] を正しく表示するか確認。
5. **CREWVIA_VERIFICATION_UI feature flag**: Taskvia Board UI 側の表示制御。disabled 時にバッジ・Verification Queue タブが非表示になるか UI で確認。
6. **rework cycle sort**: rework-history は rework_count 昇順ソート。E2E では複数 cycle の表示順を確認。
7. **Redis TTL**: verification:* は 7d (604800s)、index は TTL なし (lazy cleanup のみ)。

---

## Phase F 補足 — WARN 項目の今後の改善メモ (Troi, task_090_pf_troi)

WARN 1-b / 1-c / 2-d / 3-c は `TASKVIA_TOKEN` 未設定 (open mode) のため Bearer 認証が未テストの項目です。
次回テスト機会に以下を追加してください:

| WARN | API | 推奨追加テスト |
|------|-----|--------------|
| 1-b | POST /api/verification (no auth) | `TASKVIA_TOKEN` 設定時に `Authorization: Bearer <wrong>` で 401 を確認 |
| 1-c | POST /api/verification (wrong Bearer) | 上記と同じ設定で token 不一致時 401 を確認 |
| 2-d | GET /api/verification-queue (no auth) | `TASKVIA_TOKEN` 設定時に Bearer なし GET で 401 を確認 |
| 3-c | GET /api/cards/:id/verification (no auth) | `TASKVIA_TOKEN` 設定時に Bearer なし GET で 401 を確認 |

**実施タイミング**: 本番 `TASKVIA_TOKEN` を設定して Taskvia を起動できる環境で実施すること。
open mode (token 未設定) では auth bypass のため 401 テストは構造上不可能。
