#!/usr/bin/env bash
# scripts/test_plan_review_write_guard.sh
# hooks/pre-tool-use.sh の "Plan-review write scope guard" (t002, mission
# 20260908-launch-reliability) の回帰テスト。
#
# 背景: plan-reviewer (SKILLS=plan_review) は config/skill-permissions.yaml で
# Write を許可されているが、hook は非 Bash ツールに bare な tool 名 ("Write" 等、
# file_path を含まない) を signature として渡すため、その allow はパスを一切
# 区別できていなかった。実際に mission.yaml が規格外の値 (status: active /
# last_verdict: GO) に書き換えられ、plan.sh launch まで壊れる事故が発生した
# (20260908-codex-reviewer-phase3)。
#
# 修正: SKILLS に plan_review が含まれるセッションの Write/Edit/MultiEdit/
# NotebookEdit を、書き込み先が queue/missions/<slug>/plan_review.md である
# 場合のみ許可し、それ以外は deny する構造的ガードを追加した。
#
# このテストで検証:
#   1. plan_review スキルが mission.yaml に Write → deny
#   2. plan_review スキルが task ファイルに Write → deny
#   3. plan_review スキルが plan_review.md に Write → ブロックされない
#   4. plan_review スキルが plan_review.md 以外に Edit → deny
#   5. plan_review スキルが plan_review.md 以外に MultiEdit → deny (Edit だけを
#      見るガードは MultiEdit という同格ツールで素通りする control-bypass の
#      再発防止。t011/t014 と同じ観点)
#   6. plan_review スキルが plan_review.md 相対パスに Write → ブロックされない
#      (cwd 起点の相対パス解決の検証)
#   7. SKILLS に plan_review を含まないセッション (通常 Worker) は mission.yaml
#      への Write をこのガードではブロックされない (誤爆防止。他の skill の
#      許可判定はこのガードのスコープ外)
#   8. SKILLS が plan_review を含む複合値 (例: "plan_review,bash") でも
#      ガードが発火すること (カンマ区切り判定の検証)
#
# 実行: bash scripts/test_plan_review_write_guard.sh
# 副作用: /tmp 配下に合成の「main repo」ディレクトリを作成し終了時に削除する
#         (crewvia の実 repo / queue には一切触れない)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="${OWN_CHECKOUT_ROOT}/hooks/pre-tool-use.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST=""
cleanup() {
  if [[ -n "$TMPDIR_TEST" && -d "$TMPDIR_TEST" ]]; then
    rm -rf "$TMPDIR_TEST"
  fi
}
trap cleanup EXIT

echo "== test_plan_review_write_guard.sh (t002: plan_review write scope guard) =="

TMPDIR_TEST="/tmp/crewvia-test-plan-review-guard-$$"
FAKE_REPO="$TMPDIR_TEST/mainrepo"
mkdir -p "$FAKE_REPO/queue/missions/test-mission/tasks"
echo "status: reviewing" > "$FAKE_REPO/queue/missions/test-mission/mission.yaml"
echo "id: t001" > "$FAKE_REPO/queue/missions/test-mission/tasks/t001.md"

_run_hook() {
  local input="$1"
  shift
  env -i \
    HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CREWVIA_REPO="$FAKE_REPO" \
    "$@" \
    bash "$HOOK" <<< "$input" 2>/tmp/test_plan_review_guard_stderr
}

_write_payload() {
  printf '{"tool_name":"Write","tool_input":{"file_path":"%s"}}' "$1"
}
_edit_payload() {
  printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$1"
}
_multiedit_payload() {
  printf '{"tool_name":"MultiEdit","tool_input":{"file_path":"%s","edits":[{"old_string":"a","new_string":"b"}]}}' "$1"
}

_decision_of() {
  echo "$1" | jq -r '.hookSpecificOutput.permissionDecision // "none"' 2>/dev/null || echo "none"
}

_assert_blocked() {
  local name="$1" exit_code="$2" stdout="$3"
  local decision
  decision="$(_decision_of "$stdout")"
  if [[ "$exit_code" -eq 0 ]] && [[ "$decision" == "deny" ]]; then
    pass "$name → deny"
  else
    fail "$name should be denied — exit=$exit_code decision=$decision stdout=$stdout"
  fi
}

_assert_not_blocked() {
  local name="$1" exit_code="$2" stdout="$3"
  local decision
  decision="$(_decision_of "$stdout")"
  if [[ "$exit_code" -eq 0 ]] && [[ "$decision" != "deny" ]]; then
    pass "$name → not blocked (exit=0, decision=$decision)"
  else
    fail "$name should not be blocked by this guard — exit=$exit_code decision=$decision stdout=$stdout"
  fi
}

echo ""
echo "--- Test 1: plan_review skill writes mission.yaml → deny ---"
STDOUT=$(_run_hook "$(_write_payload "${FAKE_REPO}/queue/missions/test-mission/mission.yaml")" \
  AGENT_NAME=PlanReviewer SKILLS=plan_review); EXIT=$?
_assert_blocked "mission.yaml Write" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 2: plan_review skill writes a task file → deny ---"
STDOUT=$(_run_hook "$(_write_payload "${FAKE_REPO}/queue/missions/test-mission/tasks/t001.md")" \
  AGENT_NAME=PlanReviewer SKILLS=plan_review); EXIT=$?
_assert_blocked "task file Write" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 3: plan_review skill writes plan_review.md → not blocked ---"
STDOUT=$(_run_hook "$(_write_payload "${FAKE_REPO}/queue/missions/test-mission/plan_review.md")" \
  AGENT_NAME=PlanReviewer SKILLS=plan_review); EXIT=$?
_assert_not_blocked "plan_review.md Write" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 4: plan_review skill Edits mission.yaml → deny ---"
STDOUT=$(_run_hook "$(_edit_payload "${FAKE_REPO}/queue/missions/test-mission/mission.yaml")" \
  AGENT_NAME=PlanReviewer SKILLS=plan_review); EXIT=$?
_assert_blocked "mission.yaml Edit" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 5: plan_review skill MultiEdits mission.yaml → deny (control-bypass regression) ---"
STDOUT=$(_run_hook "$(_multiedit_payload "${FAKE_REPO}/queue/missions/test-mission/mission.yaml")" \
  AGENT_NAME=PlanReviewer SKILLS=plan_review); EXIT=$?
_assert_blocked "mission.yaml MultiEdit" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 6: plan_review skill writes plan_review.md via relative path → not blocked ---"
STDOUT=$(cd "${FAKE_REPO}/queue/missions/test-mission" && env -i \
  HOME=/tmp \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  CREWVIA_REPO="$FAKE_REPO" \
  AGENT_NAME=PlanReviewer SKILLS=plan_review \
  bash "$HOOK" <<< "$(_write_payload "plan_review.md")" 2>/tmp/test_plan_review_guard_stderr); EXIT=$?
_assert_not_blocked "relative plan_review.md Write" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 7: non-plan_review skill writes mission.yaml → not blocked by THIS guard ---"
STDOUT=$(_run_hook "$(_write_payload "${FAKE_REPO}/queue/missions/test-mission/mission.yaml")" \
  AGENT_NAME=Haruto SKILLS=bash); EXIT=$?
_assert_not_blocked "non-plan_review mission.yaml Write" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 8: SKILLS='plan_review,bash' (複合値) でもガードが発火する → deny ---"
STDOUT=$(_run_hook "$(_write_payload "${FAKE_REPO}/queue/missions/test-mission/mission.yaml")" \
  AGENT_NAME=PlanReviewer SKILLS=plan_review,bash); EXIT=$?
_assert_blocked "combined SKILLS mission.yaml Write" "$EXIT" "$STDOUT"

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
