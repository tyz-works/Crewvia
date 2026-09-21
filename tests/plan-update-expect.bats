#!/usr/bin/env bats
# tests/plan-update-expect.bats
#
# `plan.sh update --expect-status / --expect-worker` (t019)
# `plan.sh update --expect-started-at` (t020, Codex P1-1).
#
# Why these exist: watchdog's retirement cleanup ran `update --status pending
# --reset` unconditionally.  Between the moment a Worker is told to shut down
# and the moment the cleanup runs, the task can move — the Worker may finish
# it, or a human may reset it and a successor may pull it.  An unconditional
# reset then reopens finished work or clears the successor's assignment;
# plan.sh's assignment-content check cannot see either case, because the
# content still reads "<slug>:<task_id>".
#
# What is asserted is therefore not "the flag parses" but **what survives a
# precondition that does not hold**: the frontmatter untouched, the assignment
# file still on disk, and an exit status the caller can distinguish from a
# broken plan.sh.
#
# Run: npx bats tests/plan-update-expect.bats

PLAN_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/plan.sh"

setup_queue() {
  TEST_QUEUE="$(mktemp -d)"
  TEST_MISSION="${1:-test-expect}"
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
YAML

  cat >"$TEST_QUEUE/state.yaml" <<YAML
active_missions:
  - $TEST_MISSION
default_mission: $TEST_MISSION
YAML
}

add_task() {
  local id="$1"
  local status="${2:-in_progress}"
  local worker="${3:-Alice}"
  local started_at="${4:-null}"

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
started_at: $started_at
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
  [[ -n "${TEST_QUEUE:-}" && -d "$TEST_QUEUE" ]] && rm -r "$TEST_QUEUE"
}

# ---------------------------------------------------------------------------
# 前提が外れたとき、何も失われないこと
# ---------------------------------------------------------------------------

@test "--expect-worker mismatch leaves the task and the assignment untouched" {
  setup_queue "expect-worker-mismatch"
  add_task t001 in_progress Successor
  printf '%s:t001\n' "$TEST_MISSION" >"$TEST_QUEUE/assignments/Successor"

  run plan_update t001 --status pending --reset --mission "$TEST_MISSION" \
      --expect-status in_progress --expect-worker Alice
  [ "$status" -eq 3 ]
  [[ "$output" == *"precondition not met"* ]]

  grep -q '^status: in_progress$' "$TASKS_DIR/t001.md"
  grep -q '^worker: Successor$' "$TASKS_DIR/t001.md"
  [ -f "$TEST_QUEUE/assignments/Successor" ]

  cleanup_queue
}

@test "--expect-status mismatch does not reopen finished work" {
  setup_queue "expect-status-mismatch"
  add_task t001 done Alice

  run plan_update t001 --status pending --reset --mission "$TEST_MISSION" \
      --expect-status in_progress --expect-worker Alice
  [ "$status" -eq 3 ]

  grep -q '^status: done$' "$TASKS_DIR/t001.md"
  grep -q '^worker: Alice$' "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "exit 3 is distinct from a real error (exit 1)" {
  setup_queue "expect-exit-codes"
  add_task t001 done Alice

  run plan_update t001 --reset --mission "$TEST_MISSION" --expect-status in_progress
  [ "$status" -eq 3 ]

  run plan_update t001 --reset --mission no-such-mission --expect-status in_progress
  [ "$status" -eq 1 ]

  cleanup_queue
}

# ---------------------------------------------------------------------------
# 前提が成り立つとき、従来どおり動くこと
# ---------------------------------------------------------------------------

@test "matching preconditions still perform the reset and remove the assignment" {
  setup_queue "expect-match"
  add_task t001 in_progress Alice
  printf '%s:t001\n' "$TEST_MISSION" >"$TEST_QUEUE/assignments/Alice"

  run plan_update t001 --status pending --reset --mission "$TEST_MISSION" \
      --expect-status in_progress --expect-worker Alice
  [ "$status" -eq 0 ]

  grep -q '^status: pending$' "$TASKS_DIR/t001.md"
  grep -q '^worker: null$' "$TASKS_DIR/t001.md"
  [ ! -f "$TEST_QUEUE/assignments/Alice" ]

  cleanup_queue
}

@test "--expect-worker null matches an unassigned task" {
  setup_queue "expect-worker-null"
  add_task t001 pending null

  run plan_update t001 --priority high --mission "$TEST_MISSION" --expect-worker null
  [ "$status" -eq 0 ]
  grep -q '^priority: high$' "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "a csv of expected statuses accepts any of them" {
  setup_queue "expect-status-csv"
  add_task t001 verifying Alice

  run plan_update t001 --priority high --mission "$TEST_MISSION" \
      --expect-status "in_progress,verifying"
  [ "$status" -eq 0 ]
  grep -q '^priority: high$' "$TASKS_DIR/t001.md"

  cleanup_queue
}

# ---------------------------------------------------------------------------
# 前提そのものが壊れているとき
# ---------------------------------------------------------------------------

@test "a misspelled --expect-status is an error, not a silent permanent no-op" {
  setup_queue "expect-status-typo"
  add_task t001 in_progress Alice

  # 黙って 3 を返すと、呼び出し側は「世の中が変わった」と読んで永久に
  # 何もしなくなる。typo は 1 で落とす。
  run plan_update t001 --reset --mission "$TEST_MISSION" --expect-status in_progres
  [ "$status" -eq 1 ]
  [[ "$output" == *"invalid --expect-status"* ]]

  grep -q '^status: in_progress$' "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "an empty --expect-status is an error" {
  setup_queue "expect-status-empty"
  add_task t001 in_progress Alice

  run plan_update t001 --reset --mission "$TEST_MISSION" --expect-status ","
  [ "$status" -eq 1 ]

  cleanup_queue
}

# ---------------------------------------------------------------------------
# t020 / Codex P1-1 — 名前ではなく「その割り当て」に束縛する
#
# status と worker は reset → 同名 Worker が再 pull した後にちょうど元の値に
# 戻る。crewvia は Worker 名を意図的に使い回すので、その 2 つだけでは「この
# 呼び出しが話している割り当て」と「同じ task の別の実行」が区別できない。
# started_at は pull のたびに書き換わるので、そこを見て初めて区別が付く。
# ---------------------------------------------------------------------------

@test "--expect-started-at rejects a second execution wearing the same worker name" {
  setup_queue "expect-started-at-successor"
  # 後任: status も worker も「元の割り当て」と同一。違うのは started_at だけ。
  add_task t001 in_progress Alice '"2026-09-21T12:00:00Z"'
  printf '%s:t001\n' "$TEST_MISSION" >"$TEST_QUEUE/assignments/Alice"

  run plan_update t001 --status pending --reset --mission "$TEST_MISSION" \
      --expect-status in_progress --expect-worker Alice \
      --expect-started-at "2026-09-21T09:00:00Z"
  [ "$status" -eq 3 ]
  [[ "$output" == *"different execution"* ]]

  # 後任の作業が 1 バイトも巻き戻っていないこと
  grep -q '^status: in_progress$' "$TASKS_DIR/t001.md"
  grep -q '^worker: Alice$' "$TASKS_DIR/t001.md"
  grep -q '^started_at: "2026-09-21T12:00:00Z"$' "$TASKS_DIR/t001.md"
  [ -f "$TEST_QUEUE/assignments/Alice" ]

  cleanup_queue
}

@test "--expect-started-at lets the original execution through" {
  setup_queue "expect-started-at-match"
  add_task t001 in_progress Alice '"2026-09-21T09:00:00Z"'
  printf '%s:t001\n' "$TEST_MISSION" >"$TEST_QUEUE/assignments/Alice"

  run plan_update t001 --status pending --reset --mission "$TEST_MISSION" \
      --expect-status in_progress --expect-worker Alice \
      --expect-started-at "2026-09-21T09:00:00Z"
  [ "$status" -eq 0 ]

  grep -q '^status: pending$' "$TASKS_DIR/t001.md"
  grep -q '^started_at: null$' "$TASKS_DIR/t001.md"
  [ ! -f "$TEST_QUEUE/assignments/Alice" ]

  cleanup_queue
}

@test "--expect-started-at null matches a task that never recorded one" {
  setup_queue "expect-started-at-null"
  add_task t001 in_progress Alice

  run plan_update t001 --status pending --reset --mission "$TEST_MISSION" \
      --expect-started-at null
  [ "$status" -eq 0 ]
  grep -q '^status: pending$' "$TASKS_DIR/t001.md"

  cleanup_queue
}

@test "--expect-started-at null refuses once an execution has started" {
  setup_queue "expect-started-at-null-mismatch"
  add_task t001 in_progress Alice '"2026-09-21T12:00:00Z"'

  run plan_update t001 --status pending --reset --mission "$TEST_MISSION" \
      --expect-started-at null
  [ "$status" -eq 3 ]
  grep -q '^status: in_progress$' "$TASKS_DIR/t001.md"

  cleanup_queue
}
