#!/usr/bin/env bats
# tests/plan-update-worker-null.bats
#
# Regression tests for plan.sh update --worker null bug.
#
# Bug: `plan.sh update tXXX --worker null` wrote `worker: "null"` (quoted string)
# to the task file instead of `worker: null` (YAML null).  The string "null" is
# truthy in Python so `"null" or None` kept the string; _dump_scalar then quoted
# it to avoid YAML misparse.
#
# Fix: treat "null" / "none" / "" as Python None before handing to the serialiser.
#
# Run: npx bats tests/plan-update-worker-null.bats
#      (requires Node.js / npx; bats 1.13+ recommended)

PLAN_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/plan.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

setup_queue() {
  TEST_QUEUE="$(mktemp -d)"
  TEST_MISSION="${1:-test-worker-null}"
  MISSION_DIR="$TEST_QUEUE/missions/$TEST_MISSION"
  TASKS_DIR="$MISSION_DIR/tasks"
  mkdir -p "$TASKS_DIR" "$TEST_QUEUE/archive" "$TEST_QUEUE/assignments"

  cat >"$MISSION_DIR/mission.yaml" <<YAML
title: "Test mission"
slug: $TEST_MISSION
status: active
created_at: "2026-01-01T00:00:00Z"
completed_at: null
next_task_id: 2
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
  local status="${2:-pending}"
  local worker="${3:-null}"

  cat >"$TASKS_DIR/${id}.md" <<MD
---
id: $id
title: "Task $id"
skills: [bash]
priority: medium
status: $status
blocked_by: []
target_dir: null
worker: $worker
started_at: null
completed_at: null
---

## Description
Test task $id.
MD
}

plan_update() {
  CREWVIA_QUEUE="$TEST_QUEUE" bash "$PLAN_SH" update "$@" 2>&1
}

cleanup_queue() {
  [[ -n "${TEST_QUEUE:-}" && -d "$TEST_QUEUE" ]] && rm -rf "$TEST_QUEUE"
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@test "--worker null writes 'worker: null' (unquoted) to task file" {
  setup_queue "wn-null-unquoted"
  add_task t001 in_progress Alice

  run plan_update t001 --worker null --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  # The file must contain unquoted null
  grep -q '^worker: null$' "$TASKS_DIR/t001.md"

  # Must NOT contain quoted "null"
  ! grep -q '^worker: "null"' "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "--worker Alice writes 'worker: Alice' (string unchanged)" {
  setup_queue "wn-alice"
  add_task t001 pending

  run plan_update t001 --worker Alice --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  grep -q '^worker: Alice$' "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "--worker null: file has unquoted null and grep confirms no quoted string" {
  setup_queue "wn-yaml-parse"
  add_task t001 in_progress Alice

  # Reset worker to null
  run plan_update t001 --worker null --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  # File must have unquoted null
  grep -q '^worker: null$' "$TASKS_DIR/t001.md"

  # Must NOT contain quoted "null" (PyYAML would parse quoted as string)
  ! grep -q '^worker: "null"' "$TASKS_DIR/t001.md"
  ! grep -q "^worker: 'null'" "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "--worker none also writes 'worker: null' (alias)" {
  setup_queue "wn-none-alias"
  add_task t001 in_progress Bob

  run plan_update t001 --worker none --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  grep -q '^worker: null$' "$TASKS_DIR/t001.md"
  ! grep -q '^worker: "null"' "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "--reset also produces unquoted worker: null (baseline check)" {
  setup_queue "wn-reset-baseline"
  add_task t001 in_progress Carol

  run plan_update t001 --reset --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  grep -q '^worker: null$' "$TASKS_DIR/t001.md"

  cleanup_queue
}

# ---------------------------------------------------------------------------
# F1: case-insensitive sentinel matching
# ---------------------------------------------------------------------------

@test "--worker Null (capital N) writes 'worker: null' unquoted" {
  setup_queue "wn-capital-null"
  add_task t001 in_progress Alice

  run plan_update t001 --worker Null --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  grep -q '^worker: null$' "$TASKS_DIR/t001.md"
  ! grep -q '^worker: "Null"' "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "--worker None (capital N) writes 'worker: null' unquoted" {
  setup_queue "wn-capital-none"
  add_task t001 in_progress Alice

  run plan_update t001 --worker None --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  grep -q '^worker: null$' "$TASKS_DIR/t001.md"
  ! grep -q '^worker: "None"' "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "--worker NONE (all caps) writes 'worker: null' unquoted" {
  setup_queue "wn-allcaps-none"
  add_task t001 in_progress Alice

  run plan_update t001 --worker NONE --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  grep -q '^worker: null$' "$TASKS_DIR/t001.md"
  ! grep -q '^worker: "NONE"' "$TASKS_DIR/t001.md"

  cleanup_queue
}

# ---------------------------------------------------------------------------
# F4: stdout displays 'worker=null' (not Python repr 'worker=None')
# ---------------------------------------------------------------------------

@test "--worker null: stdout shows 'worker=null' not 'worker=None'" {
  setup_queue "wn-stdout-null"
  add_task t001 in_progress Alice

  run plan_update t001 --worker null --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  [[ "$output" == *"worker=null"* ]]
  [[ "$output" != *"worker=None"* ]]

  cleanup_queue
}

@test "--worker Null: stdout shows 'worker=null' not 'worker=None'" {
  setup_queue "wn-stdout-capital-null"
  add_task t001 in_progress Alice

  run plan_update t001 --worker Null --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  [[ "$output" == *"worker=null"* ]]
  [[ "$output" != *"worker=None"* ]]

  cleanup_queue
}
