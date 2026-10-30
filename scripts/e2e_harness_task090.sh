#!/usr/bin/env bash
# scripts/e2e_harness_task090.sh
# task_090 Phase E — Taskvia UI 可視化 E2E テストハーネス
#
# 使い方:
#   ./scripts/e2e_harness_task090.sh <command> [args...]
#
# コマンド:
#   inject <task_id> <verdict> [mission_slug] [rework_count]
#       — /api/verification に任意 verdict でデータ投入
#   scenario-i  — 正常系: pending → verifying → verified (Admiral 目視用)
#   scenario-ii <mission_slug> — Queue タブ用: verifying を複数件投入
#   scenario-iii — rework: failed → rework:1 → verified (rework_count=1 維持)
#   scan-redis  — verification:* キーを Upstash SCAN で確認
#
# 必須環境変数:
#   TASKVIA_TOKEN  — Taskvia Bearer トークン
#
# オプション環境変数:
#   TASKVIA_URL    — Taskvia ベース URL (default: https://taskvia.vercel.app)
#   UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN — Redis SCAN 用

set -euo pipefail

TASKVIA_DIR="${TASKVIA_DIR:-$HOME/workspace/Taskvia}"
TASKVIA_URL="${TASKVIA_URL:-http://localhost:3000}"

# Taskvia .env.local を自動ロード
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

command -v curl >/dev/null || { echo "[harness] ❌ curl が見つかりません。" >&2; exit 1; }
command -v jq   >/dev/null || { echo "[harness] ❌ jq が見つかりません。" >&2; exit 1; }

CMD="${1:-help}"

# ---- 共通: approval card 作成 (Board UI 表示に必須) ----
# /api/request → approval:{nanoid_id} + approval:index を書く
# task_id を card.task_id として保存し verification との紐付けに使う
create_card() {
  local task_id="$1"
  local task_title="${2:-[E2E] verification バッジテスト}"

  local auth_header=""
  [[ -n "${TASKVIA_TOKEN:-}" ]] && auth_header="Authorization: Bearer ${TASKVIA_TOKEN}"

  local payload
  payload="$(jq -n \
    --arg task_id "$task_id" \
    --arg task_title "$task_title" \
    '{
      tool: "E2E-harness",
      agent: "beverly-e2e",
      task_title: $task_title,
      task_id: $task_id,
      priority: "medium",
      notify: false
    }')"

  local resp
  if [[ -n "$auth_header" ]]; then
    resp="$(curl -sf --connect-timeout 5 --max-time 15 \
      -X POST "${TASKVIA_URL}/api/request" \
      -H "$auth_header" \
      -H "Content-Type: application/json" \
      -d "$payload" 2>/dev/null || echo '{}')"
  else
    resp="$(curl -sf --connect-timeout 5 --max-time 15 \
      -X POST "${TASKVIA_URL}/api/request" \
      -H "Content-Type: application/json" \
      -d "$payload" 2>/dev/null || echo '{}')"
  fi

  local card_id
  card_id="$(echo "$resp" | jq -r '.id // ""')"

  if [[ -z "$card_id" ]]; then
    echo "[harness] ❌ approval card 作成失敗: resp=${resp}" >&2
    return 1
  fi

  echo "[$(date +%H:%M:%S)] POST /api/request task_id=${task_id} → card_id=${card_id}"
  echo "$card_id"
}

# ---- 共通: verification POST ----
post_verification() {
  local task_id="$1"
  local verdict="$2"
  local mission_slug="${3:-e2e-test-mission}"
  local rework_count="${4:-0}"
  local verifier="${5:-beverly-e2e-harness}"

  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  local payload
  payload="$(jq -n \
    --arg task_id "$task_id" \
    --arg verdict "$verdict" \
    --arg mission_slug "$mission_slug" \
    --argjson rework_count "$rework_count" \
    --arg verifier "$verifier" \
    --arg verified_at "$ts" \
    '{
      task_id: $task_id,
      mission_slug: $mission_slug,
      verdict: $verdict,
      rework_count: $rework_count,
      verifier: $verifier,
      verified_at: $verified_at,
      checks: [
        {name: "output_exists", passed: true, note: "E2E test mock"},
        {name: "no_errors", passed: ($verdict != "failed"), note: "E2E test mock"}
      ]
    }')"

  local resp
  resp="$(curl -sf --connect-timeout 5 --max-time 15 \
    -X POST "${TASKVIA_URL}/api/verification" \
    -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$payload" 2>/dev/null || echo '{}')"

  local ok
  ok="$(echo "$resp" | jq -r '.ok // false')"

  echo "[$(date +%H:%M:%S)] POST /api/verification task_id=${task_id} verdict=${verdict} rework_count=${rework_count} → ok=${ok}"
  if [[ "$ok" != "true" ]]; then
    echo "[harness] ⚠️  レスポンス: $resp" >&2
    return 1
  fi
}

# ---- 共通: card 作成 + verification を一括で行うヘルパー ----
create_card_and_verify() {
  local task_id="$1"
  local verdict="$2"
  local mission_slug="${3:-e2e-test-mission}"
  local rework_count="${4:-0}"
  local task_title="${5:-[E2E] verification バッジテスト}"

  create_card "$task_id" "$task_title" > /dev/null
  post_verification "$task_id" "$verdict" "$mission_slug" "$rework_count"
}

# ---- コマンド: inject ----
if [[ "$CMD" == "inject" ]]; then
  TASK_ID="${2:-task_e2e_$(date +%s)}"
  VERDICT="${3:-pending}"
  MISSION="${4:-e2e-test-mission}"
  REWORK="${5:-0}"
  # approval card を先に作成してから verification を書く
  CARD_ID="$(create_card "$TASK_ID" "[E2E] ${TASK_ID}")"
  echo "[harness] card_id=${CARD_ID}"
  post_verification "$TASK_ID" "$VERDICT" "$MISSION" "$REWORK"
  exit 0
fi

# ---- コマンド: scan-redis ----
if [[ "$CMD" == "scan-redis" ]]; then
  if [[ -z "${UPSTASH_REDIS_REST_URL:-}" ]] || [[ -z "${UPSTASH_REDIS_REST_TOKEN:-}" ]]; then
    echo "[harness] ❌ UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN が未設定です。" >&2
    exit 1
  fi
  echo "[$(date +%H:%M:%S)] Upstash SCAN verification:* ..."
  curl -s "${UPSTASH_REDIS_REST_URL}/scan/0/match/verification:*" \
    -H "Authorization: Bearer ${UPSTASH_REDIS_REST_TOKEN}" | jq .
  exit 0
fi

# ---- コマンド: scenario-i (正常系) ----
if [[ "$CMD" == "scenario-i" ]]; then
  TASK_ID="task_e2e_normal_$(date +%s)"
  MISSION="e2e-test-mission"
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " シナリオ (i): 正常系 バッジ遷移"
  echo " task_id: ${TASK_ID}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  echo "▶ Step 0: approval card 作成 (Board 表示に必須)"
  CARD_ID="$(create_card "$TASK_ID" "[E2E-i] 正常系 バッジ遷移テスト")"
  echo "   card_id=${CARD_ID}"

  echo ""
  echo "▶ Step 1: verdict=pending を投入"
  post_verification "$TASK_ID" "pending" "$MISSION" 0
  echo "   → Admiral: Taskvia Board で対象カードに '○ pending' バッジが表示されることを確認"
  echo "   → 確認できたら Enter を押してください..."
  read -r

  echo ""
  echo "▶ Step 2: verdict=verifying を投入"
  post_verification "$TASK_ID" "verifying" "$MISSION" 0
  echo "   → Admiral: 5秒以内に '🔍 verifying' (sky色) に自動遷移することを目視"
  echo "   → 確認できたら Enter を押してください..."
  read -r

  echo ""
  echo "▶ Step 3: verdict=passed を投入"
  post_verification "$TASK_ID" "passed" "$MISSION" 0
  echo "   → Admiral: 5秒以内に '✓ verified' (emerald色) に自動遷移することを目視"
  echo "   → rework_count=0 のままであることを確認"
  echo "   → 確認できたら Enter を押してください..."
  read -r

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " シナリオ (i) 完了。task_id=${TASK_ID}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  exit 0
fi

# ---- コマンド: scenario-ii (Queue タブ) ----
if [[ "$CMD" == "scenario-ii" ]]; then
  MISSION="${2:-e2e-queue-mission}"
  TS="$(date +%s)"
  TASK_IDS=("task_e2e_q1_${TS}" "task_e2e_q2_${TS}" "task_e2e_q3_${TS}")

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " シナリオ (ii): Verification Queue タブ"
  echo " mission: ${MISSION}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  echo "▶ Step 0: approval card 3件作成"
  for tid in "${TASK_IDS[@]}"; do
    CARD_ID="$(create_card "$tid" "[E2E-ii] Queue タブテスト ${tid##*_}")"
    echo "   card_id=${CARD_ID} task_id=${tid}"
  done

  echo ""
  echo "▶ Step 1: verifying を 3件投入"
  for tid in "${TASK_IDS[@]}"; do
    post_verification "$tid" "verifying" "$MISSION" 0
  done
  echo "   → Admiral: Header nav の 'Verification (3)' タブをクリック"
  echo "   → mission 別グルーピングで task 一覧が表示されることを確認"
  echo "   → 確認できたら Enter を押してください..."
  read -r

  echo ""
  echo "▶ Step 2: 全件を passed に更新"
  for tid in "${TASK_IDS[@]}"; do
    post_verification "$tid" "passed" "$MISSION" 0
  done
  echo "   → Admiral: タブのカウントが 0 になりバッジが消えることを確認 (5s polling)"
  echo "   → 確認できたら Enter を押してください..."
  read -r

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " シナリオ (ii) 完了。"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  exit 0
fi

# ---- コマンド: scenario-iii (rework) ----
if [[ "$CMD" == "scenario-iii" ]]; then
  TASK_ID="task_e2e_rework_$(date +%s)"
  MISSION="e2e-test-mission"

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " シナリオ (iii): rework 強制 fail → 再 verify"
  echo " task_id: ${TASK_ID}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  echo "▶ Step 0: approval card 作成"
  CARD_ID="$(create_card "$TASK_ID" "[E2E-iii] rework シナリオテスト")"
  echo "   card_id=${CARD_ID}"

  echo ""
  echo "▶ Step 1: verdict=failed, rework_count=0 を投入"
  post_verification "$TASK_ID" "failed" "$MISSION" 0
  echo "   → Admiral: カードバッジが '✕ failed' (red) で表示されることを確認"
  echo "   → 確認できたら Enter を押してください..."
  read -r

  echo ""
  echo "▶ Step 2: verdict=rework, rework_count=1 を投入"
  post_verification "$TASK_ID" "rework" "$MISSION" 1
  echo "   → Admiral: バッジが '↩ rework: 1/3' (orange) に遷移することを確認"
  echo "   → カードを展開して rework 履歴が表示されることを確認"
  echo "   → 確認できたら Enter を押してください..."
  read -r

  echo ""
  echo "▶ Step 3: verdict=passed, rework_count=1 (再 verify) を投入"
  post_verification "$TASK_ID" "passed" "$MISSION" 1
  echo "   → Admiral: バッジが '✓ verified' かつ 'rework: 1/3' 表示が維持されることを確認"
  echo "   → 確認できたら Enter を押してください..."
  read -r

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " シナリオ (iii) 完了。task_id=${TASK_ID}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  exit 0
fi

# ---- help ----
cat <<EOF
使い方: ./scripts/e2e_harness_task090.sh <command> [args...]

コマンド:
  inject <task_id> <verdict> [mission_slug] [rework_count]
      任意 verdict でデータ投入 (単発)
      例: ./scripts/e2e_harness_task090.sh inject task_001 pending my-mission

  scenario-i
      正常系: pending → verifying → verified (対話形式、Admiral 目視)

  scenario-ii [mission_slug]
      Verification Queue タブ確認: verifying 3件投入 → passed 更新

  scenario-iii
      rework: failed → rework:1 → verified 維持

  scan-redis
      Upstash SCAN で verification:* キー一覧を確認

EOF
exit 0
