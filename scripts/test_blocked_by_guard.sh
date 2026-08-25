#!/usr/bin/env bash
# test_blocked_by_guard.sh — blocked_by guard 回帰テスト
#
# 問題: plan.sh pull --task で blocked_by チェックがスキップされ、
#       依存タスク未完了のまま blocked task が実行される事象が発生。
#       (mission 20260824-macos-wsl, t005 が t003 未完了で Haruto に割当)
#
# 修正: plan.sh pull --task にも blocked_by ガードを追加 (defense-in-depth)。
#       dispatcher.sh の TERMINAL_STATUSES に 'verified' を追加 (plan.sh と一致)。
#
# このテストで検証:
#   1. plan.sh pull --task: blocked 状態のタスクは exit 1 で拒否される
#   2. plan.sh pull --task: 依存が全 done なら正常に pull できる
#   3. plan.sh pull --task: 依存が 'verified' の場合も正常に pull できる
#   4. plan.sh pull (通常パス): blocked task はスキップされ自由タスクが選ばれる
#   5. dispatcher.sh: TERMINAL_STATUSES に 'verified' が含まれる
#   6. エラーメッセージに未完了依存タスク名が含まれる
#
# 実行: bash scripts/test_blocked_by_guard.sh
# 副作用: /tmp 配下に一時ディレクトリを作成し終了時に削除する

# NOTE: set -e は使わない。run_plan が非ゼロを返す際の pipe 早期終了を避けるため。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Top level of THIS checkout
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLAN_SH="$OWN_CHECKOUT_ROOT/scripts/plan.sh"
DISPATCHER_SH="$OWN_CHECKOUT_ROOT/scripts/dispatcher.sh"

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

echo "== test_blocked_by_guard.sh (blocked_by defense-in-depth) =="

# ---------------------------------------------------------------------------
# Setup: 一時 queue ディレクトリを構築
# ---------------------------------------------------------------------------
TMPDIR_TEST="/tmp/crewvia-test-blocked-$$"
QUEUE="$TMPDIR_TEST/queue"
MISSION_SLUG="test-mission"
TASKS_DIR="$QUEUE/missions/$MISSION_SLUG/tasks"
mkdir -p "$TASKS_DIR" "$QUEUE/archive"

# state.yaml
printf 'active_missions:\n  - %s\ndefault_mission: %s\n' \
  "$MISSION_SLUG" "$MISSION_SLUG" > "$QUEUE/state.yaml"

# mission.yaml
printf 'title: Test Mission\nslug: %s\nstatus: in_progress\ncreated_at: 2026-08-25T00:00:00Z\ncompleted_at: null\nnext_task_id: 7\n' \
  "$MISSION_SLUG" > "$QUEUE/missions/$MISSION_SLUG/mission.yaml"

# t001: done (dependency)
printf -- '---\nid: t001\ntitle: First task (done)\nskills: [bash]\npriority: medium\nstatus: done\nblocked_by: []\nworker: null\nstarted_at: null\ncompleted_at: 2026-08-25T01:00:00Z\n---\n\n## Description\nDone dep.\n\n## Result\nDone.\n' \
  > "$TASKS_DIR/t001.md"

# t002: pending, no blocked_by
printf -- '---\nid: t002\ntitle: Second task (pending, no deps)\nskills: [bash]\npriority: medium\nstatus: pending\nblocked_by: []\nworker: null\nstarted_at: null\ncompleted_at: null\n---\n\n## Description\nIndependent.\n\n## Result\n' \
  > "$TASKS_DIR/t002.md"

# t003: pending, blocked by t001 (done) → unblocked
printf -- '---\nid: t003\ntitle: Third task (blocked_by t001=done)\nskills: [bash]\npriority: medium\nstatus: pending\nblocked_by: [t001]\nworker: null\nstarted_at: null\ncompleted_at: null\n---\n\n## Description\nBlocked by t001.\n\n## Result\n' \
  > "$TASKS_DIR/t003.md"

# t004: pending, blocked by t002 (pending) → still blocked
printf -- '---\nid: t004\ntitle: Fourth task (blocked_by t002=pending)\nskills: [bash]\npriority: medium\nstatus: pending\nblocked_by: [t002]\nworker: null\nstarted_at: null\ncompleted_at: null\n---\n\n## Description\nBlocked by t002 (not done).\n\n## Result\n' \
  > "$TASKS_DIR/t004.md"

# t005: pending, blocked by t006 (verified)
printf -- '---\nid: t005\ntitle: Task blocked by verified dep\nskills: [bash]\npriority: medium\nstatus: pending\nblocked_by: [t006]\nworker: null\nstarted_at: null\ncompleted_at: null\n---\n\n## Description\nBlocked by t006 (verified).\n\n## Result\n' \
  > "$TASKS_DIR/t005.md"

# t006: verified
printf -- '---\nid: t006\ntitle: Verified dep\nskills: [bash]\npriority: medium\nstatus: verified\nblocked_by: []\nworker: null\nstarted_at: null\ncompleted_at: 2026-08-25T02:00:00Z\n---\n\n## Description\nAlready verified.\n\n## Result\nVerified.\n' \
  > "$TASKS_DIR/t006.md"

# Helper: run plan.sh with test QUEUE (stdout only; discard stderr warnings)
run_plan_stdout() {
  CREWVIA_QUEUE="$QUEUE" \
  CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
  bash "$PLAN_SH" "$@" 2>/dev/null
}

# Helper: run plan.sh combining stdout+stderr (for error message inspection)
run_plan_all() {
  CREWVIA_QUEUE="$QUEUE" \
  CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
  bash "$PLAN_SH" "$@" 2>&1 || true
}

echo ""
echo "--- Test 1: pull --task blocked task exits non-zero ---"
# NOTE: use direct invocation (not run_plan_all) to capture real exit code
CREWVIA_QUEUE="$QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
  bash "$PLAN_SH" pull --task t004 --mission "$MISSION_SLUG" --skills bash \
  > /dev/null 2>&1 && rc1=0 || rc1=$?
if [[ "$rc1" -ne 0 ]]; then
  pass "pull --task blocked task exits non-zero (rc=$rc1)"
else
  fail "pull --task blocked task should have exited non-zero"
fi

echo ""
echo "--- Test 2: pull --task blocked task shows 'blocked by unfinished' error ---"
msg2=$(run_plan_all pull --task t004 --mission "$MISSION_SLUG" --skills bash)
if echo "$msg2" | grep -q "blocked by unfinished"; then
  pass "error message contains 'blocked by unfinished dependencies'"
else
  fail "error should mention 'blocked by unfinished' — got: $msg2"
fi

echo ""
echo "--- Test 3: pull --task unblocked task (dep=done) succeeds ---"
out3=$(run_plan_stdout pull --task t003 --mission "$MISSION_SLUG" --skills bash) && rc3=0 || rc3=$?
if [[ "$rc3" -eq 0 ]] && echo "$out3" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('id')=='t003' else 1)" 2>/dev/null; then
  pass "pull --task unblocked task (dep done) → t003 pulled"
else
  fail "pull --task unblocked task should succeed — rc=$rc3 out=$out3"
fi

# Reset t003 to pending for clean state
printf -- '---\nid: t003\ntitle: Third task (blocked_by t001=done)\nskills: [bash]\npriority: medium\nstatus: pending\nblocked_by: [t001]\nworker: null\nstarted_at: null\ncompleted_at: null\n---\n\n## Description\nBlocked by t001.\n\n## Result\n' \
  > "$TASKS_DIR/t003.md"

echo ""
echo "--- Test 4: pull --task with 'verified' dependency → unblocked ---"
out4=$(run_plan_stdout pull --task t005 --mission "$MISSION_SLUG" --skills bash) && rc4=0 || rc4=$?
if [[ "$rc4" -eq 0 ]] && echo "$out4" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('id')=='t005' else 1)" 2>/dev/null; then
  pass "pull --task: 'verified' dep counts as done → t005 pulled"
else
  fail "pull --task: 'verified' dep should be treated as done — rc=$rc4 out=$out4"
fi

# Reset t005 to pending
printf -- '---\nid: t005\ntitle: Task blocked by verified dep\nskills: [bash]\npriority: medium\nstatus: pending\nblocked_by: [t006]\nworker: null\nstarted_at: null\ncompleted_at: null\n---\n\n## Description\nBlocked by t006 (verified).\n\n## Result\n' \
  > "$TASKS_DIR/t005.md"

echo ""
echo "--- Test 5: normal pull (no --task) skips blocked t004, picks free task ---"
out5=$(run_plan_stdout pull --skills bash --mission "$MISSION_SLUG") && rc5=0 || rc5=$?
picked=$(echo "$out5" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','?'))" 2>/dev/null || echo "?")
if [[ "$rc5" -eq 0 ]] && [[ "$picked" != "t004" ]]; then
  pass "normal pull picks '$picked', not blocked t004"
else
  fail "normal pull should skip t004 — rc=$rc5 picked=$picked out=$out5"
fi

echo ""
echo "--- Test 6: dispatcher.sh TERMINAL_STATUSES includes 'verified' ---"
if grep -q "TERMINAL_STATUSES.*verified" "$DISPATCHER_SH"; then
  pass "dispatcher.sh TERMINAL_STATUSES contains 'verified'"
else
  fail "dispatcher.sh TERMINAL_STATUSES should include 'verified' to match plan.sh"
fi

echo ""
echo "--- Test 7: error message names unmet dependency ---"
msg7=$(run_plan_all pull --task t004 --mission "$MISSION_SLUG" --skills bash)
if echo "$msg7" | grep -q "t002"; then
  pass "error message names the unmet dependency 't002'"
else
  fail "error message should name t002 — got: $msg7"
fi

echo ""
echo "================================"
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
