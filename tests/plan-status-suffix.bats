#!/usr/bin/env bats
# tests/plan-status-suffix.bats
#
# Regression tests for plan.sh status --mission suffix display.
# Covers: skipped, needs_director (with/without reason), pending, in_progress.
#
# Bug: skipped / needs_director タスクが (pending) と表示される
# Fix: _print_mission_detail に elif ブランチを追加 (PR #xxx)
#
# Run: npx bats tests/plan-status-suffix.bats
#      (requires Node.js / npx; bats 1.13+ recommended)

PLAN_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/plan.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

setup_queue() {
  TEST_QUEUE="$(mktemp -d)"
  TEST_MISSION="${1:-suffix-test-mission}"
  MISSION_DIR="$TEST_QUEUE/missions/$TEST_MISSION"
  TASKS_DIR="$MISSION_DIR/tasks"
  mkdir -p "$TASKS_DIR" "$TEST_QUEUE/archive" "$TEST_QUEUE/assignments"

  cat >"$MISSION_DIR/mission.yaml" <<YAML
title: "Suffix Test Mission"
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
  local worker="${3:-}"
  local extra_yaml="${4:-}"

  cat >"$TASKS_DIR/${id}.md" <<MD
---
id: $id
title: "Task $id"
skills: [bash]
priority: medium
status: $status
blocked_by: []
target_dir: null
worker: ${worker:-null}
started_at: null
completed_at: null
${extra_yaml}
---

## Description
Test task $id with status $status.
MD
}

plan_status() {
  CREWVIA_QUEUE="$TEST_QUEUE" \
    bash "$PLAN_SH" status --mission "$TEST_MISSION" 2>&1
}

cleanup_queue() {
  [[ -n "${TEST_QUEUE:-}" && -d "$TEST_QUEUE" ]] && rm -rf "$TEST_QUEUE"
}

# ---------------------------------------------------------------------------
# Tests: skipped
# ---------------------------------------------------------------------------

@test "skipped task shows (スキップ) suffix" {
  setup_queue "suffix-skipped"
  add_task t001 skipped ""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"(スキップ)"* ]]
}

@test "skipped task does NOT show (pending) suffix" {
  setup_queue "suffix-skipped-not-pending"
  add_task t001 skipped ""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" != *"t001"*"(pending)"* ]]
}

# ---------------------------------------------------------------------------
# Tests: needs_director (worker なし)
# ---------------------------------------------------------------------------

@test "needs_director task without reason shows (要Director) suffix" {
  setup_queue "suffix-nd-no-reason"
  add_task t001 needs_director ""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"(要Director)"* ]]
}

@test "needs_director task without reason does NOT show (pending) suffix" {
  setup_queue "suffix-nd-not-pending"
  add_task t001 needs_director ""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" != *"t001"*"(pending)"* ]]
}

# ---------------------------------------------------------------------------
# Tests: needs_director (reason あり)
# ---------------------------------------------------------------------------

@test "needs_director task with reason includes reason in suffix" {
  setup_queue "suffix-nd-with-reason"
  add_task t001 needs_director "" "needs_director_reason: \"依存関係が複雑\""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"要Director"* ]]
  [[ "$output" == *"依存関係が複雑"* ]]
}

# ---------------------------------------------------------------------------
# Tests: needs_director (worker あり)
# ---------------------------------------------------------------------------

@test "needs_director task with worker shows worker name in suffix" {
  setup_queue "suffix-nd-with-worker"
  add_task t001 needs_director "Astrid"

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"Astrid"* ]]
  [[ "$output" == *"要Director"* ]]
}

@test "needs_director task with worker and reason shows both" {
  setup_queue "suffix-nd-worker-reason"
  add_task t001 needs_director "Astrid" "needs_director_reason: \"実装方針確認が必要\""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"Astrid"* ]]
  [[ "$output" == *"要Director"* ]]
  [[ "$output" == *"実装方針確認が必要"* ]]
}

# ---------------------------------------------------------------------------
# Tests: failed
# ---------------------------------------------------------------------------

@test "failed task shows (失敗) suffix" {
  setup_queue "suffix-failed"
  add_task t001 failed ""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"(失敗)"* ]]
}

@test "failed task does NOT show (pending) suffix" {
  setup_queue "suffix-failed-not-pending"
  add_task t001 failed ""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" != *"t001"*"(pending)"* ]]
}

@test "failed task with worker shows worker name in suffix" {
  setup_queue "suffix-failed-with-worker"
  add_task t001 failed "Astrid"

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"Astrid"* ]]
  [[ "$output" == *"失敗"* ]]
}

# ---------------------------------------------------------------------------
# Tests: blocked (explicit status, not blocked_by dependency)
# ---------------------------------------------------------------------------

@test "blocked task shows (ブロック中) suffix" {
  setup_queue "suffix-blocked"
  add_task t001 blocked ""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"(ブロック中)"* ]]
}

@test "blocked task does NOT show (pending) suffix" {
  setup_queue "suffix-blocked-not-pending"
  add_task t001 blocked ""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" != *"t001"*"(pending)"* ]]
}

# ---------------------------------------------------------------------------
# Regression: pending / in_progress は壊れていない
# ---------------------------------------------------------------------------

@test "pending task still shows (pending) suffix" {
  setup_queue "suffix-pending-regression"
  add_task t001 pending ""

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"(pending)"* ]]
}

@test "in_progress task shows (進行中) suffix" {
  setup_queue "suffix-inprogress-regression"
  add_task t001 in_progress "Astrid"

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"進行中"* ]]
}

# ---------------------------------------------------------------------------
# Integration: 複数ステータスが混在しても正しく表示される
# ---------------------------------------------------------------------------

@test "mixed statuses all display correctly" {
  setup_queue "suffix-mixed"
  add_task t001 pending ""
  add_task t002 skipped ""
  add_task t003 needs_director "" "needs_director_reason: \"方針確認\""
  add_task t004 in_progress "Astrid"

  run plan_status
  cleanup_queue

  [ "$status" -eq 0 ]
  [[ "$output" == *"(pending)"* ]]
  [[ "$output" == *"(スキップ)"* ]]
  [[ "$output" == *"要Director"* ]]
  [[ "$output" == *"方針確認"* ]]
  [[ "$output" == *"進行中"* ]]
}
