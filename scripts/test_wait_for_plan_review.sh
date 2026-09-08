#!/usr/bin/env bash
# scripts/test_wait_for_plan_review.sh
# scripts/wait_for_plan_review.sh の回帰テスト (t002, mission
# 20260908-launch-reliability)。claude CLI を一切起動せず、review-plan.sh から
# 切り出したポーリング判定だけを検証する。
#
# 検証内容 (t002 で実際に観測した3パターン + 正常系):
#   1. 規定形式の verdict が既にある新しいファイル → 即 OK
#   2. 別表記 (`## 総合判定: **GO**`) の新しいファイル → 正規化されて OK
#      (side effect: ファイルに規定形式の Verdict 行が追記されること)
#   3. start_epoch より古い plan_review.md (前 cycle の残骸、規定形式の verdict
#      入り) だけが存在し、新しいファイルが来ない → 古い判定を採用せず
#      TIMEOUT_NONE で終わること (t002 の必須パターン3の直接再現)
#   4. plan_review.md が一度も作られない → TIMEOUT_NONE
#   5. 新しいファイルはあるが判定語が一切無い → TIMEOUT_FRESH
#      (Director に「中身は活かせるかも」と伝えるための区別)
#
# 実行時間を抑えるため MAX_WAIT=1 POLL_INTERVAL=1 で実行する (タイムアウト系は
# 最大でも数秒で終わる)。
#
# 実行: bash scripts/test_wait_for_plan_review.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WAIT_SCRIPT="${SCRIPT_DIR}/wait_for_plan_review.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST="/tmp/crewvia-test-wait-plan-review-$$"
mkdir -p "$TMPDIR_TEST"
trap 'rm -rf "$TMPDIR_TEST"' EXIT

echo "== test_wait_for_plan_review.sh (t002) =="

echo ""
echo "--- Test 1: conformant fresh file → OK immediately ---"
F1="$TMPDIR_TEST/t1.md"
cat > "$F1" << 'EOF'
**Verdict:** approve
EOF
START=$(date +%s)
OUT="$(bash "$WAIT_SCRIPT" "$F1" "$START" 1 1)"; RC=$?
STATUS="$(echo "$OUT" | head -1)"
if [[ "$RC" -eq 0 && "$STATUS" == "OK" ]]; then
  pass "conformant fresh file → OK"
else
  fail "expected OK, got rc=$RC status=$STATUS"
fi

echo ""
echo "--- Test 2: alt-wording fresh file → normalized to OK ---"
F2="$TMPDIR_TEST/t2.md"
cat > "$F2" << 'EOF'
# Plan Review: test-mission

## 総合判定: **GO**
EOF
START=$(date +%s)
OUT="$(bash "$WAIT_SCRIPT" "$F2" "$START" 1 1)"; RC=$?
STATUS="$(echo "$OUT" | head -1)"
if [[ "$RC" -eq 0 && "$STATUS" == "OK" ]] && grep -q '^\*\*Verdict:\*\* approve' "$F2"; then
  pass "alt-wording fresh file → normalized to OK"
else
  fail "expected normalized OK, got rc=$RC status=$STATUS content=$(cat "$F2")"
fi

echo ""
echo "--- Test 3 (t002 必須パターン3): stale file with a valid verdict → NOT accepted, times out ---"
F3="$TMPDIR_TEST/t3.md"
cat > "$F3" << 'EOF'
**Verdict:** approve
EOF
# ファイルを「レビュー開始より前」に作られたことにする — mtime を過去にずらす
touch -d '@1000000000' "$F3" 2>/dev/null || touch -t 200109090000 "$F3"
START=$(date +%s)  # 現在時刻 — F3 の mtime より確実に新しい
OUT="$(bash "$WAIT_SCRIPT" "$F3" "$START" 1 1)"; RC=$?
STATUS="$(echo "$OUT" | head -1)"
if [[ "$RC" -eq 1 && "$STATUS" == "TIMEOUT_NONE" ]]; then
  pass "stale file with old verdict is ignored → TIMEOUT_NONE (not silently accepted)"
else
  fail "stale file should be ignored and time out as TIMEOUT_NONE — got rc=$RC status=$STATUS"
fi

echo ""
echo "--- Test 4: plan_review.md never created → TIMEOUT_NONE ---"
F4="$TMPDIR_TEST/never-created.md"
START=$(date +%s)
OUT="$(bash "$WAIT_SCRIPT" "$F4" "$START" 1 1)"; RC=$?
STATUS="$(echo "$OUT" | head -1)"
if [[ "$RC" -eq 1 && "$STATUS" == "TIMEOUT_NONE" ]]; then
  pass "file never created → TIMEOUT_NONE"
else
  fail "expected TIMEOUT_NONE, got rc=$RC status=$STATUS"
fi

echo ""
echo "--- Test 5: fresh file but no recognizable verdict at all → TIMEOUT_FRESH ---"
F5="$TMPDIR_TEST/t5.md"
cat > "$F5" << 'EOF'
# Plan Review: test-mission

## Summary
まだ検査中です。
EOF
START=$(date +%s)
OUT="$(bash "$WAIT_SCRIPT" "$F5" "$START" 1 1)"; RC=$?
STATUS="$(echo "$OUT" | head -1)"
if [[ "$RC" -eq 1 && "$STATUS" == "TIMEOUT_FRESH" ]]; then
  pass "fresh file with unparseable verdict → TIMEOUT_FRESH (distinguished from TIMEOUT_NONE)"
else
  fail "expected TIMEOUT_FRESH, got rc=$RC status=$STATUS"
fi

echo ""
echo "--- Test 6 (t011, QA t009 FINDING-B): unknown vocabulary ('**STOP**') with a large max_wait → breaks early instead of waiting the full timeout ---"
F6="$TMPDIR_TEST/t6.md"
cat > "$F6" << 'EOF'
# Plan Review: test-mission

## 総合判定

**STOP**
EOF
START=$(date +%s)
T0=$(date +%s)
# MAX_WAIT=600 (実運用と同じ) だが、mtime が動かず判定不能が続くため
# 早期打ち切りされるはず — 実測の経過時間が MAX_WAIT よりずっと短いことを
# 検証する (POLL_INTERVAL=1 でテストを高速化)。
OUT="$(bash "$WAIT_SCRIPT" "$F6" "$START" 600 1)"; RC=$?
T1=$(date +%s)
ELAPSED=$((T1 - T0))
STATUS="$(echo "$OUT" | head -1)"
if [[ "$RC" -eq 1 && "$STATUS" == "TIMEOUT_FRESH" && "$ELAPSED" -lt 30 ]]; then
  pass "unknown vocabulary breaks early (elapsed ${ELAPSED}s << 600s max_wait), status=TIMEOUT_FRESH"
else
  fail "expected early TIMEOUT_FRESH well under 600s, got rc=$RC status=$STATUS elapsed=${ELAPSED}s"
fi

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
