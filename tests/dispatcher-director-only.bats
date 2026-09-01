#!/usr/bin/env bats
# tests/dispatcher-director-only.bats
#
# Regression tests for dispatcher.sh DIRECTOR_ONLY_SKILLS filter,
# exercised through plan.sh pull (the observable CLI boundary).
#
# Tests cover:
#   1. Skills scalar-string normalization (Fix B) — `skills: bash` must behave
#      identically to `skills: [bash]` after parse_frontmatter normalizes it.
#   2. director-only skill task is excluded from auto-select for non-director workers.
#   3. director-only skill task CAN be pulled with --task (explicit assignment).
#   4. planning skill task requires a worker that declares planning skill.
#   5. Mixed skills: task with [director-only, bash] blocked for bash-only worker.
#
# pull-notify integration (requires live tmux + dispatcher process) is NOT tested
# here; run dispatcher-director-only.e2e.sh against a live crewvia environment.
#
# Run: npx bats tests/dispatcher-director-only.bats

PLAN_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/plan.sh"

# ---------------------------------------------------------------------------
# Helpers (shared with dispatcher-blocked-by.bats pattern)
# ---------------------------------------------------------------------------

setup_queue() {
  TEST_QUEUE="$(mktemp -d)"
  TEST_MISSION="${1:-test-director-only}"
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

# Add a task using YAML inline-list skills: `skills: [bash]`
add_task_list() {
  local id="$1" status="$2" skills_csv="$3"
  # Convert "bash,code" → "[bash, code]"
  local skills_yaml
  IFS=',' read -ra arr <<<"$skills_csv"
  skills_yaml="[$(IFS=', '; echo "${arr[*]}")]"
  cat >"$TASKS_DIR/${id}.md" <<MD
---
id: $id
title: "Task $id"
skills: $skills_yaml
priority: medium
status: $status
blocked_by: []
target_dir: null
worker: null
started_at: null
completed_at: null
---

## Description
Test task $id (list-style skills).
MD
}

# Add a task using YAML scalar skills: `skills: bash`  (the bug path)
add_task_scalar() {
  local id="$1" status="$2" skill="$3"
  cat >"$TASKS_DIR/${id}.md" <<MD
---
id: $id
title: "Task $id"
skills: $skill
priority: medium
status: $status
blocked_by: []
target_dir: null
worker: null
started_at: null
completed_at: null
---

## Description
Test task $id (scalar skills — Fix B regression).
MD
}

plan_pull() {
  CREWVIA_QUEUE="$TEST_QUEUE" \
    bash "$PLAN_SH" pull --agent TestWorker --skills "$@" 2>&1
}

plan_pull_task() {
  local task_id="$1" skills="$2"
  CREWVIA_QUEUE="$TEST_QUEUE" \
    bash "$PLAN_SH" pull --agent TestWorker --skills "$skills" \
      --task "$task_id" --mission "$TEST_MISSION" 2>&1
}

cleanup_queue() {
  [[ -n "${TEST_QUEUE:-}" && -d "$TEST_QUEUE" ]] && rm -rf "$TEST_QUEUE"
}

# ---------------------------------------------------------------------------
# Fix B: Skills scalar normalization
# ---------------------------------------------------------------------------

@test "scalar skills: 'skills: bash' task is pulled by bash worker" {
  setup_queue "scalar-match"
  add_task_scalar t001 pending bash

  run plan_pull bash
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t001"* ]]
}

@test "scalar skills: 'skills: bash' task is NOT pulled by code-only worker" {
  setup_queue "scalar-mismatch"
  add_task_scalar t001 pending bash

  run plan_pull code
  cleanup_queue

  [ "$status" -ne 0 ]
  # No task available for this worker (skill mismatch)
  [[ "$output" != *'"id": "t001"'* ]]
}

@test "scalar skills: 'skills: director-only' treated as list [director-only]" {
  # Before Fix B: set("director-only") gives individual chars → no intersection
  # with {'director-only'}, so the director-only filter would silently fail.
  # After Fix B: ['director-only'] → task_skills = {'director-only'} → filtered.
  setup_queue "scalar-director-only"
  add_task_scalar t001 pending director-only

  # A bash worker should NOT auto-select a director-only task (skill mismatch)
  run plan_pull bash
  cleanup_queue

  [ "$status" -ne 0 ]
  [[ "$output" != *'"id": "t001"'* ]]
}

# ---------------------------------------------------------------------------
# director-only skill — auto-select exclusion
# ---------------------------------------------------------------------------

@test "director-only list: auto-select excludes task for bash worker" {
  setup_queue "do-autoselect"
  add_task_list t001 pending director-only

  run plan_pull bash
  cleanup_queue

  [ "$status" -ne 0 ]
  [[ "$output" != *'"id": "t001"'* ]]
}

@test "director-only list: auto-select works for director-only worker" {
  setup_queue "do-match"
  add_task_list t001 pending director-only

  run plan_pull director-only
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t001"* ]]
}

@test "director-only: --task pull succeeds (bypasses skill filter)" {
  # --task bypasses skill filtering; any worker can pull an explicit task
  setup_queue "do-explicit"
  add_task_list t001 pending director-only

  run plan_pull_task t001 bash
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t001"* ]]
}

@test "mixed skills [director-only,bash]: bash-only worker cannot auto-select" {
  # Task requires BOTH director-only and bash; bash-only worker lacks director-only
  setup_queue "do-mixed"
  add_task_list t001 pending director-only,bash

  run plan_pull bash
  cleanup_queue

  [ "$status" -ne 0 ]
  [[ "$output" != *'"id": "t001"'* ]]
}

# ---------------------------------------------------------------------------
# planning skill — worker skill matching
# ---------------------------------------------------------------------------

@test "planning skill: planning-only worker can auto-select planning task" {
  setup_queue "planning-match"
  add_task_list t001 pending planning

  run plan_pull planning
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t001"* ]]
}

@test "planning skill: bash-only worker cannot auto-select planning task" {
  setup_queue "planning-mismatch"
  add_task_list t001 pending planning

  run plan_pull bash
  cleanup_queue

  [ "$status" -ne 0 ]
  [[ "$output" != *'"id": "t001"'* ]]
}

@test "planning + bash worker auto-selects planning task" {
  # Worker with [planning, bash] can pull a [planning] task
  setup_queue "planning-superset"
  add_task_list t001 pending planning

  run plan_pull planning,bash
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t001"* ]]
}

# ---------------------------------------------------------------------------
# No regression: normal skill matching still works after Fix B
# ---------------------------------------------------------------------------

@test "list skills [bash,code]: bash-code worker auto-selects" {
  setup_queue "normal-match"
  add_task_list t001 pending bash,code

  run plan_pull bash,code
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"t001"* ]]
}

@test "list skills [bash,code]: bash-only worker cannot auto-select" {
  setup_queue "normal-mismatch"
  add_task_list t001 pending bash,code

  run plan_pull bash
  cleanup_queue

  [ "$status" -ne 0 ]
  [[ "$output" != *'"id": "t001"'* ]]
}
