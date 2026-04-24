# task_090 Phase E — Taskvia UI 可視化 E2E テスト シナリオ手順書

作成者: Dr. Beverly Crusher (Beverly / Chief Medical Officer)
作成日: 2026-04-23
参照: docs/plans/20260423_task090.md §2 Item 10

---

## Phase E 開始前チェックリスト

Admiral の GREEN LIGHT を受けてから E2E を開始すること。すべて ✅ になるまで開始禁止。

| # | 確認項目 | 確認方法 | 状態 |
|---|---------|---------|------|
| 1 | **Worf integration_log 全 PASS** (または Admiral 判定) | `docs/20260423_task090_worf_integration_log.md` を確認。FAIL があれば Admiral 判断を取る | ⬜ Worf 完了待ち |
| 2 | **Taskvia 公開方式の確認** (local/本番どちらか) | Admiral に確認。本番なら `vercel ls --prod` で最新 deploy を確認 | ⬜ Admiral 確認待ち |
| 3 | **Vercel 本番 env の空非空確認** (L1 教訓適用) | `cd ~/workspace/Taskvia && vercel env pull --environment=production .env.production.local` で実値確認 | ⬜ 要確認 |
| 4 | **crewvia 側 env (TASKVIA_TOKEN/NTFY_*) ロード確認** | `echo "TASKVIA_TOKEN: ${TASKVIA_TOKEN:+[SET]}"` | ⬜ 要確認 |
| 5 | **feature flag のデフォルト値確認** | Taskvia UI を開いてバッジ・Verification タブが表示されることを確認 | ⬜ 要確認 |
| 6 | **verify-task.sh + taskvia-verification-sync.sh の疎通** | `bash -n scripts/verify-task.sh && bash -n scripts/taskvia-verification-sync.sh` | ⬜ 要確認 |

> **[task_089 教訓 L1 適用]** Vercel 本番 env は「存在する (Encrypted)」でなく「値の非空」まで確認する。
> `vercel env ls` だけでは不十分。`vercel env pull` で実値を pull し、該当変数が空でないことを確認すること。
>
> **[task_089 教訓 L2 適用]** ntfy.ts や verification-sync.sh に silent failure がある場合、
> Redis への書き込み有無を Upstash SCAN で確認することで問題を切り分けできる。

---

## 所要時間・タイムライン

- **全体目安**: 20〜30 分
- 事前確認 (チェックリスト): 5 分
- シナリオ (i) 正常系: 5〜8 分
- シナリオ (ii) Verification Queue タブ: 3〜5 分
- シナリオ (iii) rework: 5〜8 分
- シナリオ (iv) feature flag: 2〜3 分

---

## 環境変数・起動確認

```bash
cd ~/workspace/crewvia

# 必須変数確認 (値は表示しない)
echo "TASKVIA_TOKEN : ${TASKVIA_TOKEN:+[SET]}"
echo "TASKVIA_URL   : ${TASKVIA_URL:-https://taskvia.vercel.app (default)}"

# verify-task.sh 構文チェック
bash -n scripts/verify-task.sh && echo "verify-task.sh OK"
bash -n scripts/taskvia-verification-sync.sh && echo "taskvia-verification-sync.sh OK"

# Taskvia health (本番使用時)
curl -s https://taskvia.vercel.app/api/health
# 期待: {"status":"ok"}
```

---

## テストハーネス概要

Beverly が用意する test harness (`scripts/e2e_harness_task090.sh`) は以下を担当:

1. **verification データ投入**: `POST /api/verification` に mock データを送信して各状態を再現
2. **rework 強制**: `verdict=failed` で投入し rework_count をインクリメントして再 verify を再現
3. **feature flag 切替**: `CREWVIA_VERIFICATION_UI=disabled` で Taskvia を再起動

> `e2e_harness.sh` (task_089) の承認カード発行フローは今回は不使用。
> 今回のハーネスは verification データ投入に特化。

---

## シナリオ (i): 正常系 — verification バッジ遷移の目視確認

### 目的

Task カードに verification バッジが `pending → verifying → verified` と遷移し、
5 秒 polling で自動更新されることを Admiral が目視確認する。

### 手順

| ステップ | 実施者 | 操作 | 期待結果 |
|---------|-------|-----|---------|
| 1 | Beverly | `POST /api/verification` に `verdict=pending` を送信 | バッジが `○ pending` (zinc) で表示 |
| 2 | Admiral | Taskvia Board を開き、対象 task カードを確認 | `○ pending` バッジが右上に表示されている |
| 3 | Beverly | `POST /api/verification` に `verdict=verifying` を送信 | — |
| 4 | Admiral | Taskvia Board 上で 5 秒以内に自動更新されることを目視 | `🔍 verifying` (sky) に自動遷移。リロード不要 |
| 5 | Beverly | `POST /api/verification` に `verdict=passed` を送信 | — |
| 6 | Admiral | 再び 5 秒以内に自動更新を目視 | `✓ verified` (emerald) に自動遷移 |
| 7 | Admiral | `rework_count=0` のままであることを確認 | rework 表示なし |

### 成功判定

- [ ] バッジが 3 状態すべて表示される
- [ ] 手動リロードなしに 5 秒以内で遷移する
- [ ] `aria-label` が状態に応じて変化する (アクセシビリティ)

---

## シナリオ (ii): Verification Queue タブ確認

### 目的

Header nav の「Verification Queue」タブが正しく表示・機能することを確認。

### 手順

| ステップ | 実施者 | 操作 | 期待結果 |
|---------|-------|-----|---------|
| 1 | Beverly | `POST /api/verification` に `verdict=verifying` の task を 2〜3 件投入 | — |
| 2 | Admiral | Header nav の「Verification (N)」タブをクリック | タブに件数バッジ (N) が表示され、Verification Queue ビューに切替わる |
| 3 | Admiral | 一覧に task が mission 別グルーピングで表示されることを確認 | ready_for_verification / verifying の task 一覧が表示される |
| 4 | Beverly | すべての task を `verdict=passed` に更新 | — |
| 5 | Admiral | タブのカウントが 0 になりバッジが消えることを確認 (5s polling) | 件数が消える |

### 成功判定

- [ ] Verification タブが nav に表示される
- [ ] mission 別グルーピングが表示される
- [ ] カウントが動的に更新される (0 のとき非表示)

---

## シナリオ (iii): rework シナリオ — 強制 fail → 再 verify

### 目的

`verdict=failed` → `rework_count` 増加 → rework 履歴表示 → 再 verify で `rework:1/3` 維持を確認。

### 手順

| ステップ | 実施者 | 操作 | 期待結果 |
|---------|-------|-----|---------|
| 1 | Beverly | `POST /api/verification` に `verdict=failed, rework_count=0` を送信 | — |
| 2 | Admiral | カードバッジが `✕ failed` (red) で表示されることを確認 | — |
| 3 | Beverly | `POST /api/verification` に `verdict=rework, rework_count=1` を送信 | — |
| 4 | Admiral | バッジが `↩ rework: 1/3` (orange) に遷移することを確認 | — |
| 5 | Admiral | カードを展開してrework 履歴が表示されることを確認 | cycle 1 の verdict + 失敗原因サマリが見える |
| 6 | Beverly | `POST /api/verification` に `verdict=passed, rework_count=1` を送信 | — |
| 7 | Admiral | バッジが `✓ verified` かつ `rework: 1/3` の表示が維持されることを確認 | rework 回数が消えない |

### 成功判定

- [ ] `failed` バッジ表示
- [ ] `rework:N/3` 形式のバッジ表示
- [ ] rework 履歴がカード展開で表示される
- [ ] 再 verify 後も rework_count が維持される

---

## シナリオ (iv): feature flag — `CREWVIA_VERIFICATION_UI=disabled`

### 目的

`CREWVIA_VERIFICATION_UI=disabled` 設定時に verification UI が非表示になり、
既存機能 (Board/Logs タブ、承認フロー) が正常動作することを確認。

### 手順

| ステップ | 実施者 | 操作 | 期待結果 |
|---------|-------|-----|---------|
| 1 | Beverly | Taskvia を `CREWVIA_VERIFICATION_UI=disabled` で再起動 (local 使用時) | — |
| 2 | Admiral | Board タブを開いて verification バッジが非表示であることを確認 | バッジなし |
| 3 | Admiral | Header nav に「Verification」タブが存在しないことを確認 | タブなし |
| 4 | Admiral | Board/Logs タブ・承認フローが従来通り動作することを確認 | 既存機能に回帰なし |
| 5 | Beverly | env を戻して再起動 | バッジ・タブが再び表示される |

> **注意**: 本番 Vercel の場合は env var を変更して `vercel --prod` deploy が必要 → Wesley (Phase F) の範疇。
> Beverly は local dev での確認に留める。

### 成功判定

- [ ] flag=disabled でバッジ・タブが非表示
- [ ] Board/Logs タブが正常動作 (回帰なし)
- [ ] flag を戻すとバッジ・タブが再表示

---

## 計測指標

E2E ログ (`20260423_task090_e2e_log.md`) に以下を記録:

```
# Scenario (i) 正常系
pending → verifying 遷移時刻:   HH:MM:SS
verifying → verified 遷移時刻:  HH:MM:SS
polling 実測レイテンシ (Admiral 目視): Xs以内

# Scenario (ii) Queue タブ
カウント表示確認:   HH:MM:SS
カウント消滅確認:   HH:MM:SS

# Scenario (iii) rework
failed バッジ確認:   HH:MM:SS
rework:1/3 確認:     HH:MM:SS
再verify後 rework 維持確認: HH:MM:SS

# Scenario (iv) feature flag
disabled 確認:      HH:MM:SS
復帰確認:           HH:MM:SS
```

---

## 失敗時のロールバック手順

### FAIL A: verification バッジが Board 上に表示されない

1. Taskvia が最新ブランチ `feat/task090-qa-ui-viz` を参照しているか確認
2. `CREWVIA_VERIFICATION_UI` が `disabled` でないか確認
3. Redis に verification データが存在するか Upstash SCAN で確認:
   ```bash
   curl -s "${UPSTASH_REDIS_REST_URL}/scan/0/match/verification:*" \
     -H "Authorization: Bearer ${UPSTASH_REDIS_REST_TOKEN}"
   ```
4. → 存在しない場合: harness の送信 HTTP ステータスを確認 (401/400/500)
5. → Geordi (Phase D) に差し戻し

### FAIL B: polling で自動更新されない (5s 超過)

1. Taskvia の polling 実装 (`useEffect` + `setInterval(5000)`) を確認
2. ブラウザ devtools の Network タブで `/api/cards` などのポーリングリクエストを確認
3. → Geordi に差し戻し

### FAIL C: rework 履歴が表示されない

1. `GET /api/cards/[id]/rework-history` が正しいデータを返すか確認:
   ```bash
   TOKEN="$(tr -d '[:space:]' < ~/workspace/crewvia/config/.taskvia-token)"
   curl -s "https://taskvia.vercel.app/api/cards/${CARD_ID}/rework-history" \
     -H "Authorization: Bearer $TOKEN"
   ```
2. → データなし: verification 送信側の `rework_count` 値確認
3. → データあり but 非表示: UI 実装 (Geordi) に差し戻し

---

## Admiral への事前通告文案

```
task_090 Phase E (Taskvia UI 可視化 E2E) の準備が整いました。
以下の確認後に実施をお願いします（所要: 20〜30 分）。

【事前確認事項】
1. Taskvia の使用方式: local pnpm dev / 本番 vercel.app のどちらですか？
2. 本番使用の場合: Phase D Geordi の最新コード (commit ae970d6 以降) がデプロイ済みですか？

【テスト内容】
(i) 正常系: バッジ pending → verifying → verified の自動遷移目視 (5s polling)
(ii) Verification Queue タブの表示・カウント確認
(iii) rework シナリオ: failed → rework:1/3 → 再 verify で rework 維持
(iv) feature flag: CREWVIA_VERIFICATION_UI=disabled でバッジ・タブが非表示になること

Beverly がデータ投入ハーネスを用意します。Admiral は Taskvia UI を開いて目視確認するだけです。
準備完了をお知らせください。
```

---

## 関連ファイル

| ファイル | 用途 |
|---------|-----|
| `docs/20260423_task090_ui_design.md` | Troi UI 設計書 (バッジ仕様・タブ設計) |
| `docs/20260423_task090_taskvia_ui_analysis.md` | Data UI 分析 |
| `docs/20260423_task090_worf_integration_log.md` | Worf API 疎通テストログ (Phase E 並列) |
| `docs/20260423_task090_e2e_log.md` | Phase E 実行ログ (Beverly が作成) |
| `scripts/e2e_harness_task090.sh` | E2E テストハーネス (verification データ投入) |
| `scripts/verify-task.sh` | crewvia 側 verification 実行スクリプト |
| `scripts/taskvia-verification-sync.sh` | verification → Taskvia POST 同期スクリプト |
