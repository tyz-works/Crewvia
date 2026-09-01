#!/usr/bin/env bats
# tests/dispatcher-blocked-by.bats
#
# Regression tests for plan.sh pull blocked_by enforcement.
# Covers both --task (dispatcher→Worker path) and auto-selection path.
#
# Related: PR #108 (failed/cancelled dep regression), dispatcher-blocked-by-analysis.md
#
# Run: npx bats tests/dispatcher-blocked-by.bats
#      (requires Node.js / npx; bats 1.13+ recommended)

PLAN_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/plan.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Create a minimal queue fixture in TMPDIR with one mission and N tasks.
# Usage:
#   setup_queue <mission_slug>
#   add_task    <task_id> <status> [blocked_by_csv]  (e.g. add_task t001 pending "")
#   The fixture sets CREWVIA_QUEUE and is cleaned up automatically.
setup_queue() {
  TEST_QUEUE="$(mktemp -d)"
  TEST_MISSION="${1:-test-mission}"
  MISSION_DIR="$TEST_QUEUE/missions/$TEST_MISSION"
  TASKS_DIR="$MISSION_DIR/tasks"
  mkdir -p "$TASKS_DIR" "$TEST_QUEUE/archive" "$TEST_QUEUE/assignments"

  cat >"$MISSION_DIR/mission.yaml" <<YAML
title: "Test mission"
slug: $TEST_MISSION
status: active
created_at: "2026-01-01T00:00:00Z"
completed_at: null
next_task_id: 1
max_review_cycles: 3
review:
  last_verdict: null
  cycle_count: 0
  reviewed_at: null
  reviewer: null
YAML

  cat >"$TEST_QUEUE/state.yaml" <<YAML
active_missions:
  - $TEST_MISSION
default_mission: $TEST_MISSION
YAML
}

add_task() {
  local id="$1"
  local status="$2"
  local blocked_by_csv="${3:-}"

  # Build blocked_by list: "t001,t002" → "- t001\n- t002"
  local blocked_by_yaml="[]"
  if [[ -n "$blocked_by_csv" ]]; then
    blocked_by_yaml="["
    IFS=',' read -ra deps <<<"$blocked_by_csv"
    for dep in "${deps[@]}"; do
      blocked_by_yaml+="${dep},"
    done
    blocked_by_yaml="${blocked_by_yaml%,}]"
  fi

  cat >"$TASKS_DIR/${id}.md" <<MD
---
id: $id
title: "Task $id"
skills: [bash]
priority: medium
status: $status
blocked_by: $blocked_by_yaml
target_dir: null
worker: null
started_at: null
completed_at: null
---

## Description
Test task $id.
MD
}

plan_pull() {
  CREWVIA_QUEUE="$TEST_QUEUE" \
    bash "$PLAN_SH" pull --agent TestWorker --skills bash "$@" 2>&1
}

plan_pull_task() {
  local task_id="$1"
  shift
  CREWVIA_QUEUE="$TEST_QUEUE" \
    bash "$PLAN_SH" pull --agent TestWorker --skills bash \
      --task "$task_id" --mission "$TEST_MISSION" "$@" 2>&1
}

cleanup_queue() {
  [[ -n "${TEST_QUEUE:-}" && -d "$TEST_QUEUE" ]] && rm -rf "$TEST_QUEUE"
}

# ---------------------------------------------------------------------------
# Test: --task path (dispatcher → Worker assignment)
# ---------------------------------------------------------------------------

@test "--task: blocked_by pending dep → pull rejected" {
  setup_queue "bbt-blocked-pending"
  add_task t001 pending ""
  add_task t002 pending "t001"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -ne 0 ]
  [[ "$output" == *"blocked"* ]] || [[ "$output" == *"blocked_by"* ]]
}

@test "--task: blocked_by in_progress dep → pull rejected" {
  setup_queue "bbt-blocked-inprog"
  add_task t001 in_progress ""
  add_task t002 pending "t001"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -ne 0 ]
  [[ "$output" == *"blocked"* ]] || [[ "$output" == *"blocked_by"* ]]
}

@test "--task: blocked_by done dep → pull succeeds" {
  setup_queue "bbt-done-dep"
  add_task t001 done ""
  add_task t002 pending "t001"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -eq 0 ]
  # JSON output should contain id=t002
  [[ "$output" == *'"id": "t002"'* ]] || [[ "$output" == *"t002"* ]]
}

@test "--task: blocked_by verified dep → pull succeeds" {
  setup_queue "bbt-verified-dep"
  add_task t001 verified ""
  add_task t002 pending "t001"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -eq 0 ]
}

@test "--task: blocked_by skipped dep → pull succeeds" {
  setup_queue "bbt-skipped-dep"
  add_task t001 skipped ""
  add_task t002 pending "t001"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -eq 0 ]
}

@test "--task: blocked_by failed dep → pull succeeds (PR #108 regression)" {
  # failed dep should NOT block (dep will never complete; downstream should proceed)
  setup_queue "bbt-failed-dep"
  add_task t001 failed ""
  add_task t002 pending "t001"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -eq 0 ]
}

@test "--task: blocked_by cancelled dep → pull succeeds" {
  setup_queue "bbt-cancelled-dep"
  add_task t001 cancelled ""
  add_task t002 pending "t001"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -eq 0 ]
}

@test "--task: blocked_by non-existent dep (typo) → pull rejected" {
  setup_queue "bbt-nonexistent-dep"
  add_task t002 pending "t999"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -ne 0 ]
  [[ "$output" == *"blocked"* ]]
}

@test "--task: all deps done, one was failed → pull succeeds" {
  # t002 blocked_by [t001, t003]; t001=done, t003=failed → both are "terminal" → unblocked
  setup_queue "bbt-mixed-deps-done"
  add_task t001 done ""
  add_task t003 failed ""
  add_task t002 pending "t001,t003"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -eq 0 ]
}

@test "--task: one dep done, one dep pending → pull rejected" {
  setup_queue "bbt-partial-done"
  add_task t001 done ""
  add_task t003 pending ""
  add_task t002 pending "t001,t003"

  run plan_pull_task t002
  cleanup_queue

  [ "$status" -ne 0 ]
  [[ "$output" == *"blocked"* ]]
}

@test "--task: no blocked_by (empty list) → pull succeeds" {
  setup_queue "bbt-no-deps"
  add_task t001 pending ""

  run plan_pull_task t001
  cleanup_queue

  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test: auto-selection path (Worker pulls without --task)
# ---------------------------------------------------------------------------

@test "auto-select: blocked task excluded from candidates" {
  setup_queue "bbt-auto-blocked"
  add_task t001 pending ""
  add_task t002 pending "t001"
  # t001 is pending → t002 blocked; only t001 should be returned

  run plan_pull
  cleanup_queue

  [ "$status" -eq 0 ]
  # Should return t001, not t002
  [[ "$output" == *'"id": "t001"'* ]] || [[ "$output" == *'"id":"t001"'* ]]
  [[ "$output" != *'"id": "t002"'* ]] && [[ "$output" != *'"id":"t002"'* ]]
}

@test "auto-select: all tasks blocked → all_blocked diag" {
  setup_queue "bbt-auto-all-blocked"
  add_task t001 in_progress ""   # in_progress → not pullable
  add_task t002 pending "t001"   # blocked by t001

  run plan_pull
  cleanup_queue

  [ "$status" -ne 0 ]
  # diag should report all_blocked or no_tasks
  [[ "$output" == *"blocked"* ]] || [[ "$output" == *"no task"* ]] || [[ "$output" == *"no_task"* ]]
}

@test "auto-select: dep becomes done → blocked task now pulled" {
  setup_queue "bbt-auto-dep-done"
  add_task t001 done ""
  add_task t002 pending "t001"

  run plan_pull
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t002"* ]]
}
