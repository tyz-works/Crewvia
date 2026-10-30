#!/usr/bin/env bash
# scripts/e2e_harness.sh
# task_089 Phase D — iPhone 実機 E2E テストハーネス
#
# 使い方:
#   ./scripts/e2e_harness.sh
#
# 必須環境変数:
#   TASKVIA_TOKEN  — Taskvia Bearer トークン
#
# オプション環境変数:
#   TASKVIA_URL    — Taskvia ベース URL (default: https://taskvia.vercel.app)
#   E2E_TIMEOUT    — ポーリングタイムアウト秒数 (default: 300)
#
# 終了コード:
#   0 — approved
#   1 — denied / timeout / error

set -euo pipefail

TASKVIA_DIR="${TASKVIA_DIR:-$HOME/workspace/Taskvia}"
TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
E2E_TIMEOUT="${E2E_TIMEOUT:-300}"

# Taskvia .env.local を自動ロード (test_phase_c.sh と同パターン、値は表示しない)
if [[ -f "$TASKVIA_DIR/.env.local" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$TASKVIA_DIR/.env.local"
  set +a
fi

# TASKVIA_TOKEN が未設定の場合、config/.taskvia-token から読み込む
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_TOKEN_FILE="${_SCRIPT_DIR}/../config/.taskvia-token"
if [[ -z "${TASKVIA_TOKEN:-}" ]] && [[ -f "$_TOKEN_FILE" ]]; then
  TASKVIA_TOKEN="$(tr -d '[:space:]' < "$_TOKEN_FILE")"
fi

# ---- 前提確認 ----
if [[ -z "${TASKVIA_TOKEN:-}" ]]; then
  echo "[harness] ❌ TASKVIA_TOKEN が未設定です。終了します。" >&2
  echo "[harness]    config/.taskvia-token にトークンを記載するか、export TASKVIA_TOKEN=... で設定してください。" >&2
  exit 1
fi

command -v curl >/dev/null || { echo "[harness] ❌ curl が見つかりません。" >&2; exit 1; }
command -v jq   >/dev/null || { echo "[harness] ❌ jq が見つかりません。" >&2; exit 1; }

echo "[harness] Taskvia E2E ハーネス開始"
echo "[harness] URL: ${TASKVIA_URL}"
echo ""

# ---- Taskvia health 確認 ----
HEALTH="$(curl -sf --connect-timeout 5 --max-time 10 "${TASKVIA_URL}/api/health" 2>/dev/null || echo '{}')"
HEALTH_STATUS="$(echo "$HEALTH" | jq -r '.status // "unknown"')"
if [[ "$HEALTH_STATUS" != "ok" ]]; then
  echo "[harness] ❌ Taskvia health チェック失敗: status=${HEALTH_STATUS}" >&2
  echo "[harness]    レスポンス: ${HEALTH}" >&2
  exit 1
fi
echo "[harness] ✅ Taskvia health: ok"

# ---- 承認カード登録 ----
SEND_TIME="$(date +%H:%M:%S)"
RESPONSE="$(curl -sf --connect-timeout 5 --max-time 15 \
  -X POST "${TASKVIA_URL}/api/request" \
  -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "Bash",
    "agent": "beverly-e2e-harness",
    "task_title": "[E2E テスト] iPhone 実機 approve/deny 検証",
    "priority": "medium",
    "notify": true
  }' 2>/dev/null || echo '{}')"

CARD_ID="$(echo "$RESPONSE" | jq -r '.id // ""')"
if [[ -z "$CARD_ID" ]]; then
  echo "[harness] ❌ 承認カード登録失敗: レスポンス=${RESPONSE}" >&2
  exit 1
fi

echo "[harness] 承認カード登録: id=${CARD_ID}"
echo "[harness] 送信時刻: ${SEND_TIME}"
echo ""
echo "[harness] ⏳ iPhone に ntfy 通知が届くのを待っています..."
echo "[harness]    Admiral: ntfy アプリで通知を確認し、✓承認 または ✗却下 をタップしてください。"
echo "[harness]    ポーリング中 (Taskvia: ${TASKVIA_URL}) — タイムアウト: ${E2E_TIMEOUT}秒"
echo ""

# ---- ポーリング ----
DECISION=""
for i in $(seq 1 "$E2E_TIMEOUT"); do
  sleep 1
  STATUS_RESP="$(curl -sf --connect-timeout 5 --max-time 10 \
    "${TASKVIA_URL}/api/status/${CARD_ID}" \
    -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
    2>/dev/null || echo '{}')"
  STATUS="$(echo "$STATUS_RESP" | jq -r '.status // "error"')"

  if [[ "$STATUS" == "approved" ]]; then
    DECISION="approved"
    DETECT_TIME="$(date +%H:%M:%S)"
    break
  elif [[ "$STATUS" == "denied" ]]; then
    DECISION="denied"
    DETECT_TIME="$(date +%H:%M:%S)"
    break
  elif [[ "$STATUS" == "error" ]]; then
    echo "[harness] ⚠️  ポーリングエラー (${i}s): レスポンス=${STATUS_RESP}" >&2
  fi

  # 10秒ごとに経過表示
  if (( i % 10 == 0 )); then
    echo "[harness] ... ${i}秒経過 (status=${STATUS})"
  fi
done

echo ""
if [[ "$DECISION" == "approved" ]]; then
  echo "[harness] ✅ 決定: approved"
  echo "[harness] 検知時刻: ${DETECT_TIME}"
  echo "[harness] カード ID: ${CARD_ID}"
  exit 0
elif [[ "$DECISION" == "denied" ]]; then
  echo "[harness] ❌ 決定: denied"
  echo "[harness] 検知時刻: ${DETECT_TIME}"
  echo "[harness] カード ID: ${CARD_ID}"
  exit 1
else
  echo "[harness] ⏱️  タイムアウト: ${E2E_TIMEOUT}秒以内に決定が得られませんでした" >&2
  echo "[harness] カード ID: ${CARD_ID}" >&2
  exit 1
fi
