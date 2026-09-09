#!/usr/bin/env bash
# scripts/test_lib_verdict.sh
# scripts/lib_verdict.py の回帰テスト (t010, mission 20260909-dead-config-sweep,
# QA t008 FINDING-1/2/3)。
#
# 背景: plan_review.md の verdict 抽出は wait_for_plan_review.sh /
# normalize_plan_review_verdict.py / plan.sh cmd_review の3層がそれぞれ独自の
# 正規表現を持っていたため、コードフェンスを除去しない・複数判定の混在を
# 検出しない、という共通の穴があった (QA t008 が実 plan.sh review を
# エンドツーエンドで走らせて実測)。scripts/lib_verdict.py にロジックを
# 一本化した本体 (extract_canonical_verdict) を直接検証する。
#
# 検証内容:
#   1. FINDING-1: コードフェンス内にだけ判定がある → 判定不能 (None)
#   2. FINDING-2: フェンス内の書式例 approve の後に本物の revise がある
#      → フェンスは無視され、revise が採用される (approve に上書きされない)
#   3. FINDING-3: `**Verdict:** approve | revise | reject` の
#      テンプレート行そのまま → 判定不能 (None)
#   4. 正常系 (canon approve/revise/reject) に regression がないこと
#   5. 判定不能の一般化: 判定語なし / 空ファイル / 未知語 / 複数の異なる
#      判定が (フェンスの外で) 混在 → いずれも None
#   6. 同じ判定が複数行にわたって重複しているだけなら approve として確定
#      すること (「複数判定 = 常に判定不能」という過剰な安全側倒れをしない)
#
# 実行: bash scripts/test_lib_verdict.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${SCRIPT_DIR}/lib_verdict.py"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST="/tmp/crewvia-test-lib-verdict-$$"
mkdir -p "$TMPDIR_TEST"
trap 'python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$TMPDIR_TEST"' EXIT

echo "== test_lib_verdict.sh (t010) =="

# $1 = ファイル内容, $2 = 期待する標準出力 ("" なら判定不能を期待),
# $3 = ラベル
_check() {
  local content="$1" expected="$2" label="$3"
  local f="$TMPDIR_TEST/case.md"
  printf '%s' "$content" > "$f"
  local out rc
  out="$(python3 "$LIB" "$f" 2>/dev/null)"
  rc=$?
  if [[ -z "$expected" ]]; then
    if [[ "$rc" -ne 0 && -z "$out" ]]; then
      pass "$label → 判定不能 (期待どおり)"
    else
      fail "$label → 判定不能であるべきなのに rc=$rc out='$out'"
    fi
  else
    if [[ "$rc" -eq 0 && "$out" == "$expected" ]]; then
      pass "$label → '$expected' (期待どおり)"
    else
      fail "$label → '$expected' を期待したが rc=$rc out='$out'"
    fi
  fi
}

echo ""
echo "--- 正常系 (regression) ---"
_check '**Verdict:** approve' "approve" "canon approve"
_check '**Verdict:** revise' "revise" "canon revise"
_check '**Verdict:** reject' "reject" "canon reject"
_check $'# Plan Review\n\n**Verdict:** approve\n\n## Summary\n問題なし。' "approve" "canon approve (前後に本文あり)"

echo ""
echo "--- FINDING-1: コードフェンス内にだけ判定がある → 判定不能 ---"
_check $'レビュー結果は以下の形式で書きます:\n\n```\n**Verdict:** approve\n```\n\n実際の判定はまだ書いていません。' "" "fence-only verdict (FINDING-1)"

echo ""
echo "--- FINDING-2: フェンス内 approve の後に本物の revise → revise が採用される (approve に上書きされない) ---"
_check $'書式例:\n\n```\n**Verdict:** approve\n```\n\n実際の判定は以下です。\n\n**Verdict:** revise' "revise" "fenced approve + real revise (FINDING-2)"

echo ""
echo "--- FINDING-3: テンプレート行 (approve | revise | reject) をそのまま貼る → 判定不能 ---"
_check $'**Verdict:** approve | revise | reject\n\n# Plan Review: <slug>' "" "template line verbatim (FINDING-3)"

echo ""
echo "--- 判定不能の一般化 ---"
_check $'# Plan Review\n\nまだ検査中です。' "" "判定語なし"
_check "" "" "空ファイル"
_check '**Verdict:** STOP' "" "未知の判定語"
_check $'**Verdict:** approve\n\nsome note\n\n**Verdict:** revise' "" "フェンス外で異なる判定が混在 (approve と revise)"

echo ""
echo "--- 同一判定の重複は許容 (過剰な安全側倒れをしない) ---"
_check $'**Verdict:** approve\n\nnote\n\n**Verdict:** approve' "approve" "同じ approve が複数行 → approve のまま"

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
