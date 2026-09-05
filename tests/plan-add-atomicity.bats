#!/usr/bin/env bats
# tests/plan-add-atomicity.bats
#
# Regression tests for plan.sh add atomicity:
#   - --skills 未指定の場合、非ゼロ終了 + task ファイル未作成 + next_task_id 変化なし
#   - --skills 指定の場合、ゼロ終了 + task ファイル作成 + next_task_id インクリメント
#
# BL-1: plan.sh add atomicity バグ修正
#
# Run: npx bats tests/plan-add-atomicity.bats
#      (requires Node.js / npx; bats 1.13+ recommended)

PLAN_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/plan.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Create a minimal queue fixture with one active mission.
setup_queue() {
  TEST_QUEUE="$(mktemp -d)"
  TEST_MISSION="${1:-test-add-atomicity}"
  MISSION_DIR="$TEST_QUEUE/missions/$TEST_MISSION"
  TASKS_DIR="$MISSION_DIR/tasks"
  mkdir -p "$TASKS_DIR" "$TEST_QUEUE/archive" "$TEST_QUEUE/assignments"

  cat >"$MISSION_DIR/mission.yaml" <<YAML
title: "Test add atomicity mission"
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

# Read next_task_id from mission.yaml
get_next_task_id() {
  grep "^next_task_id:" "$TEST_QUEUE/missions/$TEST_MISSION/mission.yaml" \
    | awk '{print $2}'
}

# Count task files in missions tasks dir
count_task_files() {
  find "$TEST_QUEUE/missions/$TEST_MISSION/tasks" -name "*.md" 2>/dev/null | wc -l | tr -d ' '
}

plan_add() {
  CREWVIA_QUEUE="$TEST_QUEUE" \
    bash "$PLAN_SH" add "$@" 2>&1
}

cleanup_queue() {
  [[ -n "${TEST_QUEUE:-}" && -d "$TEST_QUEUE" ]] && rm -rf "$TEST_QUEUE"
}

# ---------------------------------------------------------------------------
# Tests: --skills 未指定（エラーケース）
# ---------------------------------------------------------------------------

@test "add without --skills: exits non-zero" {
  setup_queue "no-skills-exit"

  run plan_add "Test task title"
  cleanup_queue

  [ "$status" -ne 0 ]
}

@test "add without --skills: stderr contains error message" {
  setup_queue "no-skills-stderr"

  run plan_add "Test task title"
  local out="$output"
  cleanup_queue

  [[ "$out" == *"--skills"* ]] || [[ "$out" == *"skills"* ]]
}

@test "add without --skills: no task file created" {
  setup_queue "no-skills-no-file"
  local before
  before=$(count_task_files)

  plan_add "Test task title" >/dev/null 2>&1 || true
  local after
  after=$(count_task_files)
  cleanup_queue

  [ "$after" -eq "$before" ]
}

@test "add without --skills: next_task_id not incremented" {
  setup_queue "no-skills-no-increment"
  local before
  before=$(get_next_task_id)

  plan_add "Test task title" >/dev/null 2>&1 || true
  local after
  after=$(get_next_task_id)
  cleanup_queue

  [ "$after" -eq "$before" ]
}

# ---------------------------------------------------------------------------
# Tests: --skills 指定（正常ケース）
# ---------------------------------------------------------------------------

@test "add with --skills: exits zero" {
  setup_queue "with-skills-exit"

  run plan_add "Test task title" --skills bash,code
  cleanup_queue

  [ "$status" -eq 0 ]
}

@test "add with --skills: task file created" {
  setup_queue "with-skills-file"

  plan_add "Test task title" --skills bash,code >/dev/null 2>&1
  local count
  count=$(count_task_files)
  cleanup_queue

  [ "$count" -eq 1 ]
}

@test "add with --skills: next_task_id incremented" {
  setup_queue "with-skills-increment"
  local before
  before=$(get_next_task_id)

  plan_add "Test task title" --skills bash,code >/dev/null 2>&1
  local after
  after=$(get_next_task_id)
  cleanup_queue

  [ "$after" -eq $(( before + 1 )) ]
}

@test "add with --skills: task file contains correct skills" {
  setup_queue "with-skills-content"

  plan_add "Test task title" --skills bash,code >/dev/null 2>&1
  local task_file
  task_file=$(find "$TEST_QUEUE/missions/$TEST_MISSION/tasks" -name "*.md" | head -1)
  local content
  content=$(cat "$task_file")
  cleanup_queue

  [[ "$content" == *"bash"* ]]
  [[ "$content" == *"code"* ]]
}

# ---------------------------------------------------------------------------
# Regression: 複数回 add 後のアトミシティ確認
# ---------------------------------------------------------------------------

@test "add: no-skills error between two success adds leaves count at 1" {
  setup_queue "atomicity-middle-fail"

  # 1回目: 正常
  plan_add "First task" --skills bash >/dev/null 2>&1
  local after_first
  after_first=$(count_task_files)

  # 2回目: エラー（--skills 未指定）
  plan_add "Second task" >/dev/null 2>&1 || true
  local after_fail
  after_fail=$(count_task_files)

  # 3回目: 正常
  plan_add "Third task" --skills code >/dev/null 2>&1
  local after_third
  after_third=$(count_task_files)
  local final_id
  final_id=$(get_next_task_id)
  cleanup_queue

  [ "$after_first" -eq 1 ]
  [ "$after_fail" -eq 1 ]   # fail は task ファイルを追加しない
  [ "$after_third" -eq 2 ]  # 正常は追加される
  [ "$final_id" -eq 3 ]     # 2回成功 → next_task_id は 1+2=3
}
