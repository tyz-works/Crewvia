#!/usr/bin/env bash
# scripts/test_review_plan_json_rescue.sh
# t004 (mission 20260909-dead-config-sweep) の回帰テスト。
#
# 背景: agents/plan_reviewer.md は plan_review.md の1行目に規定形式
# `**Verdict:** approve|revise|reject` を書くようプロンプトで指示していたが、
# plan-reviewer (Opus) がその指示を外し ## 総合判定: **GO** のような別表記で
# 出力する事故が繰り返し発生していた (scripts/normalize_plan_review_verdict.py
# のヒューリスティックで拾えない場合、600s タイムアウト → Director の手動介入)。
#
# 修正: scripts/review-plan.sh は plan-reviewer 起動時に `claude --json-schema`
# (config/plan-review-verdict.schema.json) で最終応答を強制する。既存の
# polling/normalize 経路 (プローズ解析) が判定不能だった場合のみ、この
# 構造化出力から verdict を機械的に回収し、plan_review.md の冒頭に規定形式の
# 行を書き込む rescue 経路として動く (既存経路を置き換えない、追加の保険)。
#
# テスト方法: scripts/test_review_plan_pane_leak.sh と同じ手法 —
# scripts/review-plan.sh 本体を scratch ディレクトリにコピーし、
# lib_mux.sh (mux_spawn を no-op 成功にしつつ、$$ — review-plan.sh 自身の
# PID、source されたシェル関数なので $$ は呼び出し元と同じ — を使って
# PLAN_REVIEWER_LOG に claude --output-format json 相当の出力を模したログを
# 書く) と wait_for_plan_review.sh (プローズ解析の結果を模して即座に返す)
# をスタブに差し替える。claude CLI は一切起動しない。
#
# 検証内容:
#   1. WAIT_STATUS=TIMEOUT_FRESH + ログに有効な structured_output.verdict
#      → rescue が発火し、plan_review.md 冒頭に規定形式の行が書かれ、
#      review-plan.sh は exit 0 (Director の手動介入を回避できる本体)
#   2. WAIT_STATUS=TIMEOUT_NONE + ログに有効な structured_output.verdict
#      → 同上 (plan_review.md が一度も書かれなかった場合でも rescue で
#      新規作成できる)
#   3. WAIT_STATUS=TIMEOUT_FRESH + ログが空/JSON として壊れている
#      → rescue は発火せず、既存どおり exit 1 (当て推量で verdict を
#      捏造しない、既存のフェイルクローズ挙動に回帰がないことの確認)
#   4. WAIT_STATUS=OK (プローズ解析が既に成功) → rescue を経由せず
#      そのまま exit 0 (rescue が正常系に干渉しないことの確認)
#
# 実行: bash scripts/test_review_plan_json_rescue.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_REVIEW_PLAN="${SCRIPT_DIR}/review-plan.sh"
REAL_SCHEMA="${SCRIPT_DIR}/../config/plan-review-verdict.schema.json"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

echo "== test_review_plan_json_rescue.sh (t004) =="

TMPDIR_TEST=""
cleanup() {
  if [[ -n "$TMPDIR_TEST" && -d "$TMPDIR_TEST" ]]; then
    rm -rf "$TMPDIR_TEST"
  fi
}
trap cleanup EXIT

# $1 = wait_status, $2 = ログ内容 ("" なら書かない), $3 = expect_rc,
# $4 = expect_verdict_line_present (true/false), $5 = label
_run_case() {
  local wait_status="$1" log_body="$2" expect_rc="$3" expect_line="$4" label="$5"

  TMPDIR_TEST="/tmp/crewvia-test-review-plan-json-rescue-$$-${wait_status}-$(date +%s%N)"
  rm -rf "$TMPDIR_TEST"
  mkdir -p "$TMPDIR_TEST/scripts" "$TMPDIR_TEST/config" "$TMPDIR_TEST/queue/missions/testmission"

  cp "$REAL_REVIEW_PLAN" "$TMPDIR_TEST/scripts/review-plan.sh"
  cp "$REAL_SCHEMA" "$TMPDIR_TEST/config/plan-review-verdict.schema.json"

  # スタブ lib_mux.sh: mux_available/mux_spawn は常に成功 (no-op)。mux_spawn は
  # sourced function として review-plan.sh 自身のプロセスで実行されるため、
  # 中で使う $$ は review-plan.sh の PID と一致する — これを使って本物の
  # PLAN_REVIEWER_LOG (/tmp/plan_reviewer_$$.log) にログを書き込める。
  cat > "$TMPDIR_TEST/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() {
  if [[ -n "${log_body}" ]]; then
    printf '%s\n' '${log_body}' > "/tmp/plan_reviewer_\$\$.log"
  else
    : > "/tmp/plan_reviewer_\$\$.log"
  fi
  return 0
}
mux_kill() { return 0; }
EOF

  local wait_rc=0
  [[ "$wait_status" != "OK" ]] && wait_rc=1
  cat > "$TMPDIR_TEST/scripts/wait_for_plan_review.sh" << EOF
#!/usr/bin/env bash
echo "$wait_status"
exit $wait_rc
EOF
  chmod +x "$TMPDIR_TEST/scripts/wait_for_plan_review.sh"

  set +e
  bash "$TMPDIR_TEST/scripts/review-plan.sh" testmission >/tmp/test_review_plan_json_rescue_stdout 2>&1
  local rc=$?
  set -e

  if [[ "$rc" -ne "$expect_rc" ]]; then
    fail "$label — expected review-plan.sh exit $expect_rc, got $rc (log: $(cat /tmp/test_review_plan_json_rescue_stdout))"
    return
  fi

  local review_output="$TMPDIR_TEST/queue/missions/testmission/plan_review.md"
  local has_line="false"
  if [[ -f "$review_output" ]] && grep -Eq '^\*\*Verdict:\*\*[[:space:]]*(approve|revise|reject)\b' "$review_output"; then
    has_line="true"
  fi

  if [[ "$has_line" == "$expect_line" ]]; then
    pass "$label — exit=$rc, canonical verdict line present=$has_line (as expected)"
  else
    fail "$label — exit=$rc but canonical verdict line present=$has_line (expected $expect_line). plan_review.md: $(cat "$review_output" 2>/dev/null || echo '<missing>')"
  fi
}

RESULT_JSON_REVISE='{"type":"result","subtype":"success","is_error":false,"result":"{\"verdict\":\"revise\"}","structured_output":{"verdict":"revise"}}'

echo ""
echo "--- Case 1: TIMEOUT_FRESH + 有効な structured_output.verdict → rescue が発火し OK 扱い ---"
_run_case "TIMEOUT_FRESH" "$RESULT_JSON_REVISE" 0 "true" "TIMEOUT_FRESH + valid structured output"

echo ""
echo "--- Case 2: TIMEOUT_NONE (plan_review.md 自体が無い) + 有効な structured_output.verdict → rescue が plan_review.md を新規作成し OK 扱い ---"
_run_case "TIMEOUT_NONE" "$RESULT_JSON_REVISE" 0 "true" "TIMEOUT_NONE + valid structured output"

echo ""
echo "--- Case 3 (fail-closed 確認): TIMEOUT_FRESH + ログが空/壊れている → rescue は発火せず既存どおり exit 1 ---"
_run_case "TIMEOUT_FRESH" "not valid json at all" 1 "false" "TIMEOUT_FRESH + broken log (no rescue, no fabricated verdict)"

echo ""
echo "--- Case 4 (正常系への非干渉確認): WAIT_STATUS=OK → rescue を経由せず exit 0 ---"
_run_case "OK" "" 0 "false" "OK (prose already succeeded, rescue untouched — this stub never writes plan_review.md itself, only Case 1/2's own mechanism does)"

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
