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
echo "--- Test 9 (t008 F5): '## 総合判定: 承認しない' → NOT approve, exit 1, file unchanged ---"
F9="$TMPDIR_TEST/t9.md"
cat > "$F9" << 'EOF'
# Plan Review: test-mission

## 総合判定: 承認しない

## Summary
致命的な問題があるため承認しない。
EOF
ORIG9="$(cat "$F9")"
python3 "$NORMALIZE" "$F9" >/tmp/test_normalize_stdout 2>&1
RC=$?
NEW9="$(cat "$F9")"
if [[ "$RC" -eq 1 && "$NEW9" == "$ORIG9" ]]; then
  pass "承認しない → not misdetected as approve, exit 1, file untouched"
else
  fail "承認しない should NOT normalize to approve — rc=$RC content=$(cat "$F9")"
fi

echo ""
echo "--- Test 10 (t008 F5): '## 総合判定: 承認できない' → NOT approve, exit 1 ---"
F10="$TMPDIR_TEST/t10.md"
cat > "$F10" << 'EOF'
# Plan Review: test-mission

## 総合判定: 承認できない
EOF
ORIG10="$(cat "$F10")"
python3 "$NORMALIZE" "$F10" >/tmp/test_normalize_stdout 2>&1
RC=$?
NEW10="$(cat "$F10")"
if [[ "$RC" -eq 1 && "$NEW10" == "$ORIG10" ]]; then
  pass "承認できない → not misdetected as approve, exit 1, file untouched"
else
  fail "承認できない should NOT normalize to approve — rc=$RC content=$(cat "$F10")"
fi

echo ""
echo "--- Test 11 (t008 F5): '## 総合判定: LGTM とは言えない' → NOT approve, exit 1 ---"
F11="$TMPDIR_TEST/t11.md"
cat > "$F11" << 'EOF'
# Plan Review: test-mission

## 総合判定: LGTM とは言えない
EOF
ORIG11="$(cat "$F11")"
python3 "$NORMALIZE" "$F11" >/tmp/test_normalize_stdout 2>&1
RC=$?
NEW11="$(cat "$F11")"
if [[ "$RC" -eq 1 && "$NEW11" == "$ORIG11" ]]; then
  pass "LGTM とは言えない → not misdetected as approve, exit 1, file untouched"
else
  fail "LGTM とは言えない should NOT normalize to approve — rc=$RC content=$(cat "$F11")"
fi

echo ""
echo "--- Test 12 (t008 F5 guard-rail): positive context containing negation word ('問題はないため承認') → still normalizes to approve ---"
F12="$TMPDIR_TEST/t12.md"
cat > "$F12" << 'EOF'
# Plan Review: test-mission

## 総合判定: 重大な問題はないため承認

## Summary
問題なし。
EOF
python3 "$NORMALIZE" "$F12" >/tmp/test_normalize_stdout 2>&1
RC=$?
if [[ "$RC" -eq 0 ]] && grep -q '^\*\*Verdict:\*\* approve' "$F12"; then
  pass "positive context with negation word still normalizes to approve (NEG_RE not over-broad)"
else
  fail "positive '問題はないため承認' should still normalize to approve — rc=$RC content=$(cat "$F12")"
fi

echo ""
echo "--- Test 13 (t008 追加, QA t003 FINDING-2, 実運用で観測): 実際に plan-reviewer が書いた「修正後 GO」→ NOT approve, exit 1, file unchanged ---"
F13="$TMPDIR_TEST/t13.md"
cat > "$F13" << 'EOF'
# Plan Review: test-mission

## 総合判定

**修正後 GO**

理由:
- タスク構成自体は正しい（実装 → review の流れ）
- スキル割り当ても妥当

Director が上記の Description 補記を行えば即実行可。
EOF
ORIG13="$(cat "$F13")"
python3 "$NORMALIZE" "$F13" >/tmp/test_normalize_stdout 2>&1
RC=$?
NEW13="$(cat "$F13")"
if [[ "$RC" -eq 1 && "$NEW13" == "$ORIG13" ]]; then
  pass "修正後 GO (実運用の実例) → not misdetected as approve, exit 1, file untouched"
else
  fail "修正後 GO should NOT normalize to approve — rc=$RC content=$(cat "$F13")"
fi

echo ""
echo "--- Test 14 (t008 追加): '## 総合判定: 条件付き GO' → NOT approve, exit 1 ---"
F14="$TMPDIR_TEST/t14.md"
cat > "$F14" << 'EOF'
# Plan Review: test-mission

## 総合判定: 条件付き GO
EOF
ORIG14="$(cat "$F14")"
python3 "$NORMALIZE" "$F14" >/tmp/test_normalize_stdout 2>&1
RC=$?
NEW14="$(cat "$F14")"
if [[ "$RC" -eq 1 && "$NEW14" == "$ORIG14" ]]; then
  pass "条件付き GO → not misdetected as approve, exit 1, file untouched"
else
  fail "条件付き GO should NOT normalize to approve — rc=$RC content=$(cat "$F14")"
fi

echo ""
echo "--- Test 15 (t008 追加): '## 総合判定: GO（ただし t002 の Description 追記が前提）' → NOT approve, exit 1 ---"
F15="$TMPDIR_TEST/t15.md"
cat > "$F15" << 'EOF'
# Plan Review: test-mission

## 総合判定: GO（ただし t002 の Description 追記が前提）
EOF
ORIG15="$(cat "$F15")"
python3 "$NORMALIZE" "$F15" >/tmp/test_normalize_stdout 2>&1
RC=$?
NEW15="$(cat "$F15")"
if [[ "$RC" -eq 1 && "$NEW15" == "$ORIG15" ]]; then
  pass "GO（ただし...が前提） → not misdetected as approve, exit 1, file untouched"
else
  fail "GO（ただし...が前提） should NOT normalize to approve — rc=$RC content=$(cat "$F15")"
fi

echo ""
echo "--- Test 16 (t008 guard-rail, Director 指摘): '## 総合判定: **GO**。ただし Codex review は Director 判断' → 留保が付随情報のみ、approve のまま ---"
F16="$TMPDIR_TEST/t16.md"
cat > "$F16" << 'EOF'
# Plan Review: test-mission

## 総合判定: **GO**。ただし Codex review は Director 判断とする。
EOF
python3 "$NORMALIZE" "$F16" >/tmp/test_normalize_stdout 2>&1
RC=$?
if [[ "$RC" -eq 0 ]] && grep -q '^\*\*Verdict:\*\* approve' "$F16"; then
  pass "GO。ただし(付随情報) → HEDGE_RE が文をまたいで誤爆せず approve のまま (over-broad guard)"
else
  fail "GO。ただし(付随情報のみ) は approve のままであるべき — rc=$RC content=$(cat "$F16")"
fi

echo ""
echo "--- Test 17: 既存の NO-GO / 却下 / 要修正 / LGTM が引き続き正しく判定される (regression) ---"
declare -A T17_CASES=(
  ["**NO-GO**"]="reject"
  ["**却下**"]="reject"
  ["**要修正**"]="revise"
  ["**LGTM**"]="approve"
)
T17_OK=1
for content17 in "${!T17_CASES[@]}"; do
  expected="${T17_CASES[$content17]}"
  F17="$TMPDIR_TEST/t17-$(echo "$expected" | tr -d '*').md"
  printf '# Plan Review: test-mission\n\n## 総合判定\n%s\n' "$content17" > "$F17"
  python3 "$NORMALIZE" "$F17" >/tmp/test_normalize_stdout 2>&1
  if ! grep -q "^\*\*Verdict:\*\* ${expected}" "$F17"; then
    T17_OK=0
    echo "    (unexpected) '$content17' did not normalize to $expected — content=$(cat "$F17")"
  fi
done
if [[ "$T17_OK" -eq 1 ]]; then
  pass "NO-GO / 却下 / 要修正 / LGTM は引き続き正しく判定される (regression)"
else
  fail "正当な NO-GO / 却下 / 要修正 / LGTM の判定に regression がある"
fi

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
