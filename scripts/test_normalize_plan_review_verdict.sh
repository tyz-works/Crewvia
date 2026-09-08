#!/usr/bin/env bash
# scripts/test_normalize_plan_review_verdict.sh
# scripts/normalize_plan_review_verdict.py の回帰テスト (t002, mission
# 20260908-launch-reliability)。
#
# 背景: plan-reviewer (Opus) が規定形式 `**Verdict:** approve|revise|reject` を
# 守らず、`## 総合判定: **GO**` のような別表記で判定を書いてしまうことがある
# (20260908-codex-reviewer-phase3 で実際に観測)。scripts/wait_for_plan_review.sh
# はこのスクリプトで別表記を正規形式に正規化してから受理する (方針2)。
#
# このテストで検証:
#   1. 既に規定形式 (`**Verdict:** approve`) → 何も変更せず exit 0 (idempotent)
#   2. `## 総合判定: **GO**` → approve に正規化されて exit 0
#   3. `## 総合判定: **NO-GO**` → reject に正規化されて exit 0
#   4. `## 総合判定: 要修正`  → revise に正規化されて exit 0
#   5. 判定語が一切無い (総合判定見出しすら無い) → 変更されず exit 1
#      (倒れる方向: 判定不能なら当て推量しない)
#   6. 本文中に無関係な "go" という単語があるだけ (総合判定見出しの外) →
#      誤検知せず exit 1 のまま (総合判定見出し付近だけを見るスコープ制限の検証)
#   7. 正規化後、元の内容 (総合判定行含む) が保持されていること
#   8. 2回連続で実行しても2回目は何も壊さないこと (idempotency の直接検証)
#
# 実行: bash scripts/test_normalize_plan_review_verdict.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NORMALIZE="${SCRIPT_DIR}/normalize_plan_review_verdict.py"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST="/tmp/crewvia-test-normalize-verdict-$$"
mkdir -p "$TMPDIR_TEST"
trap 'rm -rf "$TMPDIR_TEST"' EXIT

echo "== test_normalize_plan_review_verdict.sh (t002) =="

echo ""
echo "--- Test 1: already-conformant file → unchanged, exit 0 ---"
F1="$TMPDIR_TEST/t1.md"
cat > "$F1" << 'EOF'
**Verdict:** approve

# Plan Review: test-mission
EOF
ORIG="$(cat "$F1")"
python3 "$NORMALIZE" "$F1" >/tmp/test_normalize_stdout 2>&1
RC=$?
NEW="$(cat "$F1")"
if [[ "$RC" -eq 0 && "$NEW" == "$ORIG" ]]; then
  pass "conformant file left unchanged"
else
  fail "conformant file should be left unchanged — rc=$RC"
fi

echo ""
echo "--- Test 2: '## 総合判定: **GO**' → normalized to approve ---"
F2="$TMPDIR_TEST/t2.md"
cat > "$F2" << 'EOF'
# Plan Review: test-mission

## 総合判定: **GO**

## Summary
問題なし。
EOF
python3 "$NORMALIZE" "$F2" >/tmp/test_normalize_stdout 2>&1
RC=$?
if [[ "$RC" -eq 0 ]] && grep -q '^\*\*Verdict:\*\* approve' "$F2"; then
  pass "GO → approve normalized"
else
  fail "GO should normalize to approve — rc=$RC content=$(cat "$F2")"
fi

echo ""
echo "--- Test 3: '## 総合判定: **NO-GO**' → normalized to reject ---"
F3="$TMPDIR_TEST/t3.md"
cat > "$F3" << 'EOF'
# Plan Review: test-mission

## 総合判定: **NO-GO**
EOF
python3 "$NORMALIZE" "$F3" >/tmp/test_normalize_stdout 2>&1
RC=$?
if [[ "$RC" -eq 0 ]] && grep -q '^\*\*Verdict:\*\* reject' "$F3"; then
  pass "NO-GO → reject normalized"
else
  fail "NO-GO should normalize to reject — rc=$RC content=$(cat "$F3")"
fi

echo ""
echo "--- Test 4: '## 総合判定: 要修正' → normalized to revise ---"
F4="$TMPDIR_TEST/t4.md"
cat > "$F4" << 'EOF'
# Plan Review: test-mission

## 総合判定: 要修正
EOF
python3 "$NORMALIZE" "$F4" >/tmp/test_normalize_stdout 2>&1
RC=$?
if [[ "$RC" -eq 0 ]] && grep -q '^\*\*Verdict:\*\* revise' "$F4"; then
  pass "要修正 → revise normalized"
else
  fail "要修正 should normalize to revise — rc=$RC content=$(cat "$F4")"
fi

echo ""
echo "--- Test 5: no verdict language at all → exit 1, file unchanged ---"
F5="$TMPDIR_TEST/t5.md"
cat > "$F5" << 'EOF'
# Plan Review: test-mission

## Summary
まだ検査中です。
EOF
ORIG5="$(cat "$F5")"
python3 "$NORMALIZE" "$F5" >/tmp/test_normalize_stdout 2>&1
RC=$?
NEW5="$(cat "$F5")"
if [[ "$RC" -eq 1 && "$NEW5" == "$ORIG5" ]]; then
  pass "no verdict → exit 1, file untouched (fail safe)"
else
  fail "no verdict should exit 1 and leave file untouched — rc=$RC"
fi

echo ""
echo "--- Test 6: unrelated 'go' outside the 総合判定 heading → not misdetected, exit 1 ---"
F6="$TMPDIR_TEST/t6.md"
cat > "$F6" << 'EOF'
# Plan Review: test-mission

## Summary
このタスクは go 言語で書かれている。判定はまだ書いていない。
EOF
python3 "$NORMALIZE" "$F6" >/tmp/test_normalize_stdout 2>&1
RC=$?
if [[ "$RC" -eq 1 ]] && ! grep -q '^\*\*Verdict:\*\*' "$F6"; then
  pass "unrelated 'go' mention not misdetected as verdict"
else
  fail "unrelated 'go' should not be misdetected — rc=$RC content=$(cat "$F6")"
fi

echo ""
echo "--- Test 7: original content preserved after normalization ---"
if grep -q '総合判定: \*\*GO\*\*' "$F2"; then
  pass "original 総合判定 line preserved after normalization"
else
  fail "original content should be preserved, not replaced"
fi

echo ""
echo "--- Test 8: running normalize twice is idempotent ---"
BEFORE="$(cat "$F2")"
python3 "$NORMALIZE" "$F2" >/tmp/test_normalize_stdout 2>&1
RC=$?
AFTER="$(cat "$F2")"
if [[ "$RC" -eq 0 && "$BEFORE" == "$AFTER" ]]; then
  pass "second run is a no-op (idempotent)"
else
  fail "second run should be a no-op — rc=$RC"
fi

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
