# task_089 Phase D — iPhone 実機 E2E テスト シナリオ手順書

作成者: Dr. Beverly Crusher (Beverly / Chief Medical Officer)
作成日: 2026-04-22
対象: ntfy 承認通知 Phase 2 — iPhone 実機 approve/deny E2E

---

## Phase D 開始前チェックリスト

Admiral に確認を取ってから E2E を開始すること。すべての項目が ✅ になるまで開始禁止。

| # | 確認項目 | 確認方法 | 状態 |
|---|---------|---------|------|
| 1 | **[1-b 対応] GCP VM の ntfy anonymous publish が 401 を返すこと** | GCP VM で `docker exec ntfy ntfy access` 実行 → `auth-default-access: deny-all` であること。または `curl -s https://ntfy.elni.net/<TOPIC>` が 401 を返すこと | ⬜ 要確認 |
| 2 | Admiral iPhone に ntfy アプリがインストール済みで `https://ntfy.elni.net` の topic を subscribe 済みであること | iPhone の ntfy アプリで `ntfy.elni.net` トピック一覧を確認 | ⬜ 要確認 |
| 3 | Taskvia 本番 URL (`https://taskvia.vercel.app`) が生きていること | `curl -s https://taskvia.vercel.app/api/health` → `{"status":"ok"}` | ✅ 確認済み (2026-04-22) |
| 4 | crewvia 側の環境変数が設定されていること: `TASKVIA_TOKEN`, `NTFY_TOPIC`, `NTFY_USER`, `NTFY_PASS` | `echo $TASKVIA_TOKEN` などで確認 | ⬜ 要確認 |
| 5 | crewvia `config/crewvia.yaml` の `taskvia_url` が `https://taskvia.vercel.app` であること | `grep taskvia_url ~/workspace/crewvia/config/crewvia.yaml` | ⬜ 要確認 |

> **[1-b 補足]** Phase C (Worf) で `1-b ntfy anonymous publish` が FAIL (got 200, expected 401) だった。
> これは GCP VM の ntfy サーバー設定の問題。Beverly による E2E 開始前に Admiral が以下を完了させること:
> ```bash
> # GCP VM で実施
> docker exec ntfy ntfy access          # auth-default-access: deny-all を確認
> # もし deny-all でなければ server.yml を修正後:
> docker compose restart ntfy
> # 確認
> curl -s https://ntfy.elni.net/<TOPIC>  # → HTTP 401 が返れば OK
> ```

---

## 所要時間・タイムライン

- **全体目安**: 10〜15 分
- 事前確認 (上記チェックリスト): 3〜5 分
- approve シナリオ: 3〜5 分
- deny シナリオ: 3〜5 分

---

## 環境変数・設定確認

E2E 開始前に Beverly (実施者) が以下を確認する:

```bash
# crewvia リポジトリ内で実行
cd ~/workspace/crewvia

# 必須環境変数の確認 (値は表示しない)
echo "TASKVIA_TOKEN  : ${TASKVIA_TOKEN:+[SET]}"
echo "NTFY_TOPIC     : ${NTFY_TOPIC:+[SET]}"
echo "NTFY_USER      : ${NTFY_USER:+[SET]}"
echo "NTFY_PASS      : ${NTFY_PASS:+[SET]}"
echo "TASKVIA_URL    : ${TASKVIA_URL:-https://taskvia.vercel.app (default)}"

# Taskvia 疎通確認
curl -s https://taskvia.vercel.app/api/health
# 期待: {"status":"ok"}

# ntfy 認証あり疎通確認
curl -s -o /dev/null -w "%{http_code}" \
  -u "${NTFY_USER}:${NTFY_PASS}" \
  "https://ntfy.elni.net/${NTFY_TOPIC}"
# 期待: 200
```

---

## テストハーネスの使い方

E2E テストには `scripts/e2e_harness.sh` を使用する。
このスクリプトは Taskvia `/api/request` に直接 `notify: true` でリクエストを送り、
ポーリングして決定結果を返す。

```bash
cd ~/workspace/crewvia
# 基本実行 (デフォルト: TASKVIA_URL 参照)
./scripts/e2e_harness.sh

# 明示的に本番 URL を指定する場合
TASKVIA_URL=https://taskvia.vercel.app ./scripts/e2e_harness.sh
```

スクリプトは以下を行う:
1. Taskvia に承認カードを登録 (`POST /api/request` with `notify: true`)
2. ntfy 通知が iPhone に届くのを待つ (Admiral の操作を待機)
3. `/api/status/<card_id>` を1秒間隔でポーリング
4. `approved` / `denied` になったら結果を出力し終了
5. 600 秒でタイムアウト → `deny` として扱う

---

## シナリオ 1: approve パス

### 手順

| ステップ | 実施者 | 操作 | 期待結果 |
|---------|-------|-----|---------|
| 1 | Beverly | `./scripts/e2e_harness.sh` を実行 | `[harness] 承認カード登録: id=XXXXXXX` が表示される |
| 2 | Beverly | ターミナルに `[harness] ⏳ ポーリング中... (Taskvia: https://taskvia.vercel.app)` が表示されることを確認 | — |
| 3 | Admiral (iPhone) | ntfy アプリに通知が届くことを確認 | 通知タイトル: `Approval Required` (または類似)、アクションボタン: `✓承認` / `✗却下` が表示される |
| 4 | Beverly | 通知到達までの時間を計測: `time_notified - time_sent` | 目標: 10秒以内 |
| 5 | Admiral (iPhone) | `✓承認` ボタンをタップ | ntfy アプリが Taskvia エンドポイントに POST を送信 |
| 6 | Beverly | ターミナルに `[harness] ✅ 決定: approved` が表示されることを確認 | `approved` と表示される |
| 7 | Beverly | ポーリング検知時刻と承認時刻の差 (レイテンシ) を記録 | 目標: 5秒以内 |

### 期待される通知内容

| 項目 | 期待値 |
|-----|-------|
| 通知サーバー | https://ntfy.elni.net |
| subscribe トピック | `$NTFY_TOPIC` (crewvia.yaml または env で設定済みの値) |
| アクションボタン 1 | ✓承認 → `POST https://taskvia.vercel.app/api/approve-token/<token>` |
| アクションボタン 2 | ✗却下 → `POST https://taskvia.vercel.app/api/deny-token/<token>` |

### 成功判定

- [ ] ntfy 通知が 10 秒以内に届く
- [ ] `✓承認` タップ後 Taskvia が `approved` を返す
- [ ] harness が `[harness] ✅ 決定: approved` を表示する

---

## シナリオ 2: deny パス

### 手順

シナリオ 1 と同様の手順で、ステップ 5 のみ異なる:

| ステップ | 実施者 | 操作 | 期待結果 |
|---------|-------|-----|---------|
| 1〜4 | (シナリオ 1 と同じ) | — | — |
| 5 | Admiral (iPhone) | `✗却下` ボタンをタップ | Taskvia エンドポイントに POST を送信 |
| 6 | Beverly | ターミナルに `[harness] ❌ 決定: denied` が表示されることを確認 | `denied` と表示される |
| 7 | Beverly | ポーリング検知時刻と却下時刻の差 (レイテンシ) を記録 | 目標: 5秒以内 |

### 成功判定

- [ ] ntfy 通知が 10 秒以内に届く
- [ ] `✗却下` タップ後 Taskvia が `denied` を返す
- [ ] harness が `[harness] ❌ 決定: denied` を表示する

---

## 計測指標

E2E ログ (`20260422_task089_e2e_log.md`) に以下を記録すること:

```
# Scenario 1 (approve)
send_time:    HH:MM:SS
notify_time:  HH:MM:SS (Admiral iPhone 目視確認)
tap_time:     HH:MM:SS (Admiral 操作)
detect_time:  HH:MM:SS (harness が approved を検知)

notify_latency: (notify_time - send_time) 秒
poll_latency:   (detect_time - tap_time) 秒

# Scenario 2 (deny)
(同様)
```

---

## 失敗時のロールバック手順

### FAIL A: ntfy 通知が届かない

1. `curl -s -o /dev/null -w "%{http_code}" -u "${NTFY_USER}:${NTFY_PASS}" https://ntfy.elni.net/${NTFY_TOPIC}` で疎通確認
2. 200 以外 → 環境変数 `NTFY_USER` / `NTFY_PASS` / `NTFY_TOPIC` を再確認
3. `CREWVIA_APPROVAL_CHANNEL` が `taskvia` または `both` であることを確認
4. Taskvia ログ (`https://taskvia.vercel.app/api/health`) で ntfy 送信エラーがないか確認
5. → 解決しない場合: Geordi (Phase B) に差し戻し

### FAIL B: アクションボタンをタップしても `pending` のまま

1. Taskvia 本番 URL `https://taskvia.vercel.app/api/approve-token/<token>` に手動で curl:
   ```bash
   curl -s -X POST https://taskvia.vercel.app/api/approve-token/<token>
   # 期待: {"ok":true}
   ```
2. 404 → token が期限切れ (TTL 900s)。harness を再実行してトークンを取得し直す
3. 500 → Taskvia 本番サーバーの問題 → Geordi に差し戻し
4. ntfy アクションボタンの URL が `taskvia.vercel.app` を向いていない → Taskvia 側 notify 実装の問題 → Geordi に差し戻し

### FAIL C: Taskvia `health` が落ちている

1. `curl -s https://taskvia.vercel.app/api/health` が `{"status":"ok"}` 以外
2. Vercel ダッシュボードで deployment status を確認
3. 必要であれば再デプロイ (Admiral 承認が必要)
4. E2E を一時中断し、Riker 経由で状況を Picard に報告

### FAIL D: harness がタイムアウト (600s)

1. harness を Ctrl+C で終了
2. 原因を特定 (通知届かない / ボタン動作しない / ポーリング失敗)
3. 上記 FAIL A/B/C を順に確認

---

## 成功基準（task_089.md §2 Item 6 準拠）

| 基準 | 判定条件 |
|-----|---------|
| approve シナリオ通過 | harness が `approved` を出力し、裏で crewvia Worker が継続実行できる状態 |
| deny シナリオ通過 | harness が `denied` を出力し、Worker が中断する状態 |
| 通知レイテンシ許容 | ntfy 通知が 10 秒以内に届く (Admiral が許容することを確認) |
| ntfy anonymous 禁止 (1-b) | GCP VM 側の設定が完了し `curl` (認証なし) が 401 を返す |

---

## Phase E (Wesley + Troi) への引き継ぎメモ

E2E 完了後、以下を `e2e_log.md` に追記して Wesley・Troi に渡すこと:

1. **approve / deny 各シナリオ pass/fail** — 本番 URL での実績
2. **計測レイテンシ** — ntfy 通知到達時間・ポーリング検知時間
3. **Admiral フィードバック** — UX 観点 (通知がわかりやすいか、ボタンが押しやすいか など)
4. **未解決事項** — Geordi/Worf 差し戻し案件があれば記載
5. **[スキル候補]** `iphone-e2e-ntfy`: ntfy アクションボタンを使った実機 E2E 手順は再利用可能性が高い。手順を skills/ に切り出すことを推奨。

---

## 関連ファイル

| ファイル | 用途 |
|---------|-----|
| `docs/20260422_task089_integration_log.md` | Phase C (Worf) 統合テストログ — FAIL 2 件の詳細はここ |
| `docs/20260422_task089_e2e_log.md` | Phase D 実行ログ (Beverly が作成) |
| `hooks/pre-tool-use.sh` | crewvia Worker の承認ゲート実装 |
| `hooks/lib_approval_channel.sh` | 承認チャネル共通ライブラリ |
| `config/crewvia.yaml` | CREWVIA_APPROVAL_CHANNEL / ntfy 設定 |
| `scripts/e2e_harness.sh` | E2E テストハーネス (承認カード発行 + ポーリング) |
