#!/usr/bin/env bash
# scripts/test_wait_for_plan_review.sh
# scripts/wait_for_plan_review.sh の回帰テスト (t002, mission
# 20260908-launch-reliability)。claude CLI を一切起動せず、review-plan.sh から
# 切り出したポーリング判定だけを検証する。
#
# 検証内容 (t002 で実際に観測した3パターン + 正常系):
#   1. 規定形式の verdict が既にある新しいファイル → 即 OK
#   2. 別表記 (`## 総合判定: **GO**`) の新しいファイル → TIMEOUT_FRESH
#      (F1, mission 20260912-verdict-ci-launcher で挙動変更。以前はここで
#      normalize_plan_review_verdict.py がファイル全体走査で別表記を救済して
#      いたが、その走査が判定を1点だけ完全一致で読むという不変条件を迂回する
#      唯一の穴だったため削除した。別表記の救済は scripts/review-plan.sh の
#      構造化出力経路 (`claude --json-schema`) に一本化されている — このテスト
#      は wait_for_plan_review.sh 単体を見るため、その rescue は経由しない)
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
echo "--- Test 2 (F1, mission 20260912-verdict-ci-launcher: 挙動変更): alt-wording fresh file → TIMEOUT_FRESH (もう正規化されない) ---"
# 旧挙動 (削除前): normalize_plan_review_verdict.py がファイル全体を走査して
# 別表記を救済し OK を返していた。その走査が「判定は1点だけを完全一致で
# 読む」という lib_verdict.py の不変条件を丸ごと迂回する唯一の経路だった
# ため削除した (F1)。別表記の救済は scripts/review-plan.sh の構造化出力
# 経路 (`claude --json-schema`) に一本化されており、wait_for_plan_review.sh
# 単体では別表記は判定不能のまま — 危険側 (OK) ではなく安全側
# (TIMEOUT_FRESH) に倒れることを確認する。
F2="$TMPDIR_TEST/t2.md"
cat > "$F2" << 'EOF'
# Plan Review: test-mission

## 総合判定: **GO**
EOF
START=$(date +%s)
OUT="$(bash "$WAIT_SCRIPT" "$F2" "$START" 1 1)"; RC=$?
STATUS="$(echo "$OUT" | head -1)"
if [[ "$RC" -eq 1 && "$STATUS" == "TIMEOUT_FRESH" ]]; then
  pass "alt-wording fresh file → TIMEOUT_FRESH (F1: no longer silently normalized to approve)"
else
  fail "expected TIMEOUT_FRESH (safe side, F1 regression check), got rc=$RC status=$STATUS content=$(cat "$F2")"
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
echo "--- Test 7 (F2, PR#188 t012 Seo 指摘): fresh file already has a literal '**Verdict:**' line but with an unrecognized word (STOP) → NOT accepted as OK (grep must also match a known verdict word) ---"
F7="$TMPDIR_TEST/t7.md"
cat > "$F7" << 'EOF'
**Verdict:** STOP
EOF
START=$(date +%s)
OUT="$(bash "$WAIT_SCRIPT" "$F7" "$START" 1 1)"; RC=$?
STATUS="$(echo "$OUT" | head -1)"
# 以前は `^\*\*Verdict:\*\*` の存在だけで OK にしていたため、下流の
# plan.sh cmd_review (approve|revise|reject を要求) と不整合になり、
# 書式ミスなのに review cycle を消費していた (F2)。ここでは OK にならず
# TIMEOUT_FRESH (判定語不明の別経路) に落ちることを検証する。
if [[ "$RC" -eq 1 && "$STATUS" == "TIMEOUT_FRESH" ]]; then
  pass "'**Verdict:** STOP' (未知の判定語) は OK にならず TIMEOUT_FRESH (F2 fix)"
else
  fail "未知の判定語が OK として受理されている (F2 regression) — rc=$RC status=$STATUS"
fi

echo ""
echo "--- Test 8 (t018, Director 設計判断4): 書式違反・自己矛盾 (1行目 revise + 本文に approve) → OK にしない ---"
F8="$TMPDIR_TEST/t8.md"
START=$(date +%s)
printf '%s\n' '**Verdict:** revise' '' '書式例:' '' '**Verdict:** approve' > "$F8"
OUT="$(bash "$WAIT_SCRIPT" "$F8" "$START" 1 1)"; RC=$?
STATUS="$(echo "$OUT" | head -1)"
if [[ "$RC" -eq 1 && "$STATUS" == "TIMEOUT_FRESH" ]]; then
  pass "書式違反 (lib_verdict rc=20) は OK にならず TIMEOUT_FRESH"
else
  fail "書式違反が OK として受理されている — rc=$RC status=$STATUS"
fi

# Test 9-11: lib_verdict.py の終了コード/出力を allowlist で解釈していることの確認。
# wait_for_plan_review.sh は自分の隣の lib_verdict.py を呼ぶため、コピーした
# ディレクトリにスタブを置く。
_run_with_stub_lib() {
  local dir="$1" stub_body="$2"
  mkdir -p "$dir"
  cp "$WAIT_SCRIPT" "$dir/wait_for_plan_review.sh"
  printf '%s\n' "$stub_body" > "$dir/lib_verdict.py"
  local start
  start=$(date +%s)
  printf '%s\n' '**Verdict:** approve' > "$dir/plan_review.md"
  OUT="$(bash "$dir/wait_for_plan_review.sh" "$dir/plan_review.md" "$start" 1 1)"; RC=$?
  STATUS="$(echo "$OUT" | head -1)"
}

echo ""
echo "--- Test 9 (t018): lib_verdict.py が落ちた (未捕捉例外 = rc 1) → OK にしない ---"
_run_with_stub_lib "$TMPDIR_TEST/w9" $'import sys\nsys.exit(1)'
if [[ "$RC" -eq 1 && "$STATUS" == "TIMEOUT_FRESH" ]]; then
  pass "lib_verdict rc=1 は OK にならない"
else
  fail "lib_verdict rc=1 で rc=$RC status=$STATUS"
fi

echo ""
echo "--- Test 10 (t018): lib_verdict.py が rc 0 でも出力が正規の 1 語でない ('APPROVE') → OK にしない ---"
_run_with_stub_lib "$TMPDIR_TEST/w10" 'print("APPROVE")'
if [[ "$RC" -eq 1 && "$STATUS" == "TIMEOUT_FRESH" ]]; then
  pass "rc 0 + 'APPROVE' は OK にならない"
else
  fail "rc 0 + 'APPROVE' で rc=$RC status=$STATUS"
fi

echo ""
echo "--- Test 11 (対照): スタブが rc 0 + 'approve' を返す → OK (Test 9/10 のスタブ差し替えが効いていることの確認) ---"
_run_with_stub_lib "$TMPDIR_TEST/w11" 'print("approve")'
if [[ "$RC" -eq 0 && "$STATUS" == "OK" ]]; then
  pass "rc 0 + 'approve' は OK"
else
  fail "対照が OK にならない (スタブが呼ばれていない可能性) — rc=$RC status=$STATUS"
fi

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
