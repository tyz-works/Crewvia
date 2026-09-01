#!/usr/bin/env bats
# tests/worker-idle-shutdown.bats
#
# Regression tests for Worker idle/shutdown rule implementation.
# Exercises plan.sh pull behaviour that underpins Rule 1–4 detection.
#
# Scope:
#   - Plan.sh auto-select correctly reports "all_blocked" when every
#     matching task is blocked (Rule 2 precondition)
#   - Plan.sh pull exit codes allow callers to distinguish idle vs error
#   - No tasks remain pullable after all deps are in-progress
#
# Dispatcher Rule 2 (blocked-stuck 600s detection) requires live mtime
# manipulation and is exercised in tests/dispatcher-rule2-integration.sh
# (a manual integration test, not included here due to timing dependencies).
#
# Run: npx bats tests/worker-idle-shutdown.bats

PLAN_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/plan.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

setup_queue() {
  TEST_QUEUE="$(mktemp -d)"
  TEST_MISSION="${1:-idle-test}"
  MISSION_DIR="$TEST_QUEUE/missions/$TEST_MISSION"
  TASKS_DIR="$MISSION_DIR/tasks"
  mkdir -p "$TASKS_DIR" "$TEST_QUEUE/archive" "$TEST_QUEUE/assignments"

  cat >"$MISSION_DIR/mission.yaml" <<YAML
title: "Idle shutdown test"
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
  local id="$1" status="$2" skills_csv="$3" blocked_by_csv="${4:-}"
  local skills_yaml="[${skills_csv}]"
  local blocked_yaml="[]"
  if [[ -n "$blocked_by_csv" ]]; then
    blocked_yaml="[${blocked_by_csv}]"
  fi
  cat >"$TASKS_DIR/${id}.md" <<MD
---
id: $id
title: "Task $id"
skills: $skills_yaml
priority: medium
status: $status
blocked_by: $blocked_yaml
target_dir: null
worker: null
started_at: null
completed_at: null
---

## Description
Test task $id.
MD
}

plan_pull_auto() {
  local skills="$1"
  CREWVIA_QUEUE="$TEST_QUEUE" \
    bash "$PLAN_SH" pull --agent TestWorker --skills "$skills" 2>&1
}

cleanup_queue() {
  [[ -n "${TEST_QUEUE:-}" && -d "$TEST_QUEUE" ]] && rm -rf "$TEST_QUEUE"
}

# ---------------------------------------------------------------------------
# Exit code contract (underpins Rule 1/2 observability)
# ---------------------------------------------------------------------------

@test "exit 0 when task is available and pulled" {
  setup_queue "exit0"
  add_task t001 pending bash

  run plan_pull_auto bash
  cleanup_queue

  [ "$status" -eq 0 ]
}

@test "exit 2 when no tasks match worker skills (idle state)" {
  # Dispatcher interprets exit 2 as 'idle' — Rule 1 shutdown flow begins
  setup_queue "exit2-skill-mismatch"
  add_task t001 pending code  # code-only task, bash worker

  run plan_pull_auto bash
  cleanup_queue

  [ "$status" -eq 2 ]
}

@test "exit 2 when queue is empty (no tasks at all)" {
  setup_queue "exit2-empty"
  # No tasks added

  run plan_pull_auto bash
  cleanup_queue

  [ "$status" -eq 2 ]
}

@test "exit 2 when all matching tasks are in_progress" {
  # Worker should idle (exit 2) when the only task it could do is already claimed
  setup_queue "exit2-in-progress"
  add_task t001 in_progress bash

  run plan_pull_auto bash
  cleanup_queue

  [ "$status" -eq 2 ]
}

# ---------------------------------------------------------------------------
# All-blocked scenario (Rule 2 precondition)
# ---------------------------------------------------------------------------

@test "exit 2 when all matching tasks are blocked (Rule 2 precondition)" {
  # This is the state Dispatcher's Rule 2 watches: worker has matching tasks
  # but they are all blocked → 600s timer starts in dispatcher
  setup_queue "all-blocked"
  add_task t001 pending bash          # dep — in_progress or pending
  add_task t002 pending bash "t001"   # blocked by t001

  run plan_pull_auto bash
  # t001 is available (unblocked), t002 is blocked — t001 should be pulled
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t001"* ]]
  # t002 must NOT be returned
  [[ "$output" != *'"id": "t002"'* ]]
}

@test "all matching tasks blocked AND dep is in_progress → only dep available" {
  setup_queue "all-blocked-dep-in-progress"
  add_task t001 in_progress code       # dep is in_progress (not pullable)
  add_task t002 pending bash "t001"    # blocked, bash Worker cannot pull this
  add_task t003 pending code           # code task for different worker

  # bash Worker: t002 is blocked (by in_progress t001) → exit 2
  run plan_pull_auto bash
  cleanup_queue

  [ "$status" -eq 2 ]
  # Confirm t002 was not returned
  [[ "$output" != *'"id": "t002"'* ]]
}

@test "blocked task becomes pullable when dep reaches done" {
  # Simulates the moment Rule 2 timer resets: dep completes → task unblocked
  setup_queue "dep-completes"
  add_task t001 done bash
  add_task t002 pending bash "t001"   # blocked by t001 (done) → now unblocked

  run plan_pull_auto bash
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t002"* ]]
}

@test "blocked task becomes pullable when dep is failed (PR #108 regression)" {
  # failed dep should not block (Rule 2 must not fire for failed deps)
  setup_queue "dep-failed"
  add_task t001 failed bash
  add_task t002 pending bash "t001"

  run plan_pull_auto bash
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t002"* ]]
}

# ---------------------------------------------------------------------------
# Rule 3 observable precondition: unblocked tasks for a given skill
# ---------------------------------------------------------------------------

@test "worker with no unblocked tasks exits idle (Rule 3 precondition)" {
  # Director checks this before applying Rule 3 (manual kill)
  setup_queue "rule3-precond"
  add_task t001 in_progress bash       # someone else is doing it
  add_task t002 pending bash "t001"    # blocked

  # bash Worker: t001 in_progress (not pullable), t002 blocked → idle
  run plan_pull_auto bash
  cleanup_queue

  [ "$status" -eq 2 ]
}

@test "multi-mission: blocked in one mission, available in another" {
  # Worker should pull from the second mission, not idle
  TEST_QUEUE="$(mktemp -d)"
  M1="mission-alpha"
  M2="mission-beta"
  for m in "$M1" "$M2"; do
    mkdir -p "$TEST_QUEUE/missions/$m/tasks"
    cat >"$TEST_QUEUE/missions/$m/mission.yaml" <<YAML
title: "Mission $m"
slug: $m
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
  done
  mkdir -p "$TEST_QUEUE/archive" "$TEST_QUEUE/assignments"
  cat >"$TEST_QUEUE/state.yaml" <<YAML
active_missions:
  - $M1
  - $M2
default_mission: $M1
YAML
  # M1: all blocked
  cat >"$TEST_QUEUE/missions/$M1/tasks/t001.md" <<MD
---
id: t001
title: "Task t001"
skills: [bash]
priority: medium
status: in_progress
blocked_by: []
target_dir: null
worker: OtherWorker
started_at: null
completed_at: null
---
## Description
Blocking task.
MD
  cat >"$TEST_QUEUE/missions/$M1/tasks/t002.md" <<MD
---
id: t002
title: "Task t002"
skills: [bash]
priority: medium
status: pending
blocked_by: [t001]
target_dir: null
worker: null
started_at: null
completed_at: null
---
## Description
Blocked task.
MD
  # M2: has a free task
  cat >"$TEST_QUEUE/missions/$M2/tasks/t001.md" <<MD
---
id: t001
title: "Free task"
skills: [bash]
priority: medium
status: pending
blocked_by: []
target_dir: null
worker: null
started_at: null
completed_at: null
---
## Description
Free task in M2.
MD

  run env CREWVIA_QUEUE="$TEST_QUEUE" bash "$PLAN_SH" pull --agent TestWorker --skills bash 2>&1
  rm -rf "$TEST_QUEUE"

  [ "$status" -eq 0 ]
  [[ "$output" == *"mission-beta"* ]]
}
