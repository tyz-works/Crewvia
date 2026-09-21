#!/usr/bin/env bats
# tests/plan-assignment-identity.bats
#
# queue/assignments/<agent> の書き込み・削除が「実行アイデンティティ」で
# 束縛されていることの回帰テスト。
#
# 背景 (Codex P1 / main の既存欠陥):
#   cmd_update() は with_lock() の *外* で assignment を削除していた。
#   ロックを離した直後に同名 Worker が pending になった task を pull すると、
#   新しい assignment の内容は <mission>:<task> で一致してしまうため、
#   後任の assignment が消える。cmd_done() / cmd_fail() に至っては内容すら
#   見ずに無条件で削除していた。
#
#   assignment が消えた Worker は dispatcher から idle に見えるため kill される。
#   つまりこれは「稼働中の Worker を殺す」経路である。
#
# ここで固定する性質:
#   1. pull は assignment と *同じロックの中で* 実行世代を記録する
#   2. done / fail は自分の task を指していない assignment を消さない
#   3. retire は世代が一致しない限り 1 バイトも書かない (exit 3)
#   4. retire は世代を証明できないとき「実行しない」に倒す (exit 3)
#
# Run: bats tests/plan-assignment-identity.bats

PLAN_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/plan.sh"

# plan.sh が「前提が外れたので何も書いていない」を表す終了コード。
PRECONDITION_UNMET=3

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

setup_queue() {
  TEST_QUEUE="$(mktemp -d)"
  TEST_TARGET="$(mktemp -d)"
  TEST_MISSION="${1:-test-assignment-identity}"
  MISSION_DIR="$TEST_QUEUE/missions/$TEST_MISSION"
  TASKS_DIR="$MISSION_DIR/tasks"
  ASSIGN_DIR="$TEST_QUEUE/assignments"
  mkdir -p "$TASKS_DIR" "$TEST_QUEUE/archive" "$ASSIGN_DIR"

  cat >"$MISSION_DIR/mission.yaml" <<YAML
title: "Assignment identity test mission"
slug: $TEST_MISSION
status: active
created_at: "2026-01-01T00:00:00Z"
completed_at: null
next_task_id: 9
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

cleanup_queue() {
  [[ -n "${TEST_QUEUE:-}" && -d "$TEST_QUEUE" ]] && rm -rf "$TEST_QUEUE"
  [[ -n "${TEST_TARGET:-}" && -d "$TEST_TARGET" ]] && rm -rf "$TEST_TARGET"
  return 0
}

# add_task <id> [status] [worker] [started_at]
#
# target_dir を埋めておくのは worktree 作成 (git subprocess) を抑止するため。
add_task() {
  local id="$1" status="${2:-pending}" worker="${3:-null}" started="${4:-null}"

  cat >"$TASKS_DIR/${id}.md" <<MD
---
id: $id
title: "Task $id"
skills: [bash]
priority: medium
status: $status
blocked_by: []
target_dir: $TEST_TARGET
worker: $worker
started_at: $started
completed_at: null
---

## Description
Task $id.

## Result
MD
}

plan() {
  CREWVIA_QUEUE="$TEST_QUEUE" TASKVIA_URL= TASKVIA_TOKEN= TARGET_DIR= \
    bash "$PLAN_SH" "$@" 2>&1
}

plan_as() {
  local agent="$1"; shift
  CREWVIA_QUEUE="$TEST_QUEUE" TASKVIA_URL= TASKVIA_TOKEN= TARGET_DIR= \
    AGENT_NAME="$agent" bash "$PLAN_SH" "$@" 2>&1
}

# card_field <task_id> <key> — frontmatter の値を引用符を外して返す。
card_field() {
  sed -n "s/^$2: *//p" "$TASKS_DIR/$1.md" | head -1 | sed 's/^"\(.*\)"$/\1/'
}

# ---------------------------------------------------------------------------
# 1. pull が実行世代を記録する
# ---------------------------------------------------------------------------

@test "pull publishes the assignment together with the execution identity" {
  setup_queue "ai-pull-publishes"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  [ -f "$ASSIGN_DIR/Ren" ]
  [ "$(cat "$ASSIGN_DIR/Ren")" = "$TEST_MISSION:t001" ]

  # 世代を記録したサイドカーが無ければ、後から来た後始末は「この実行の
  # assignment か、後任の assignment か」を区別できない。
  [ -f "$ASSIGN_DIR/Ren.identity" ]

  local started
  started="$(card_field t001 started_at)"
  [ -n "$started" ]
  [ "$started" != "null" ]

  run python3 -c "
import json, sys
d = json.load(open('$ASSIGN_DIR/Ren.identity'))
assert d['mission'] == '$TEST_MISSION', d
assert d['task'] == 't001', d
assert d['worker'] == 'Ren', d
assert d['started_at'] == '''$started''', d
"
  [ "$status" -eq 0 ]

  cleanup_queue
}

@test "two executions of one task get distinguishable generations" {
  setup_queue "ai-generation-unique"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local first
  first="$(card_field t001 started_at)"

  run plan update t001 --reset --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local second
  second="$(card_field t001 started_at)"

  # 秒精度だと同一秒内の reset → 再 pull で世代が衝突し、「別の実行」を
  # 区別できなくなる。世代として使う以上、衝突してはならない。
  [ "$first" != "$second" ]

  cleanup_queue
}

# ---------------------------------------------------------------------------
# 2. done / fail が他人の assignment を消さない
# ---------------------------------------------------------------------------

@test "done does not remove an assignment that points at another task" {
  setup_queue "ai-done-foreign"
  add_task t001 in_progress Ren '"2026-01-01T00:00:00Z"'
  add_task t002

  # Ren は既に t002 に移っている (t001 の card だけが取り残されている)。
  run plan pull --agent Ren --skills bash --task t002 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  [ "$(cat "$ASSIGN_DIR/Ren")" = "$TEST_MISSION:t002" ]

  run plan_as Ren done t001 "result" --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  # t002 の assignment は Ren が現在稼働中である証拠。消すと dispatcher が
  # Ren を idle とみなして kill する。
  [ -f "$ASSIGN_DIR/Ren" ]
  [ "$(cat "$ASSIGN_DIR/Ren")" = "$TEST_MISSION:t002" ]
  [ -f "$ASSIGN_DIR/Ren.identity" ]

  cleanup_queue
}

@test "fail does not remove an assignment that points at another task" {
  setup_queue "ai-fail-foreign"
  add_task t001 in_progress Ren '"2026-01-01T00:00:00Z"'
  add_task t002

  run plan pull --agent Ren --skills bash --task t002 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  run plan_as Ren fail t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  [ -f "$ASSIGN_DIR/Ren" ]
  [ "$(cat "$ASSIGN_DIR/Ren")" = "$TEST_MISSION:t002" ]

  cleanup_queue
}

@test "done removes its own assignment (identity and sidecar both)" {
  setup_queue "ai-done-own"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  run plan_as Ren done t001 "result" --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  [ ! -e "$ASSIGN_DIR/Ren" ]
  [ ! -e "$ASSIGN_DIR/Ren.identity" ]

  cleanup_queue
}

# ---------------------------------------------------------------------------
# 3. retire — 実行アイデンティティで束縛した単一トランザクション
# ---------------------------------------------------------------------------

@test "retire retires the bound execution atomically" {
  setup_queue "ai-retire-ok"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local gen
  gen="$(card_field t001 started_at)"

  run plan retire t001 --agent Ren --started-at "$gen" --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  [ "$(card_field t001 status)" = "pending" ]
  [ "$(card_field t001 worker)" = "null" ]
  [ "$(card_field t001 started_at)" = "null" ]
  [ ! -e "$ASSIGN_DIR/Ren" ]
  [ ! -e "$ASSIGN_DIR/Ren.identity" ]

  cleanup_queue
}

@test "retire writes nothing when a successor already took the task over" {
  setup_queue "ai-retire-successor"
  add_task t001

  # 世代 1: これが retire の対象。
  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local gen1
  gen1="$(card_field t001 started_at)"

  # 人間が差し戻し、同名 Worker が pull し直す (crewvia は名前を使い回す)。
  run plan update t001 --reset --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local gen2
  gen2="$(card_field t001 started_at)"
  [ "$gen1" != "$gen2" ]

  # 世代 1 に向けて出された後始末が、いま動いている世代 2 に着弾する状況。
  run plan retire t001 --agent Ren --started-at "$gen1" --mission "$TEST_MISSION"
  [ "$status" -eq "$PRECONDITION_UNMET" ]

  # 1 バイトも変わっていないこと。
  [ "$(card_field t001 status)" = "in_progress" ]
  [ "$(card_field t001 worker)" = "Ren" ]
  [ "$(card_field t001 started_at)" = "$gen2" ]
  [ -f "$ASSIGN_DIR/Ren" ]
  [ "$(cat "$ASSIGN_DIR/Ren")" = "$TEST_MISSION:t001" ]
  [ -f "$ASSIGN_DIR/Ren.identity" ]

  cleanup_queue
}

@test "retire writes nothing when the worker name does not match" {
  setup_queue "ai-retire-worker"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local gen
  gen="$(card_field t001 started_at)"

  run plan retire t001 --agent Omar --started-at "$gen" --mission "$TEST_MISSION"
  [ "$status" -eq "$PRECONDITION_UNMET" ]

  [ "$(card_field t001 status)" = "in_progress" ]
  [ "$(card_field t001 worker)" = "Ren" ]
  [ -f "$ASSIGN_DIR/Ren" ]

  cleanup_queue
}

@test "retire writes nothing when the task already finished" {
  setup_queue "ai-retire-done"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local gen
  gen="$(card_field t001 started_at)"

  run plan_as Ren done t001 "result" --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  run plan retire t001 --agent Ren --started-at "$gen" --mission "$TEST_MISSION"
  [ "$status" -eq "$PRECONDITION_UNMET" ]

  [ "$(card_field t001 status)" = "done" ]

  cleanup_queue
}

@test "retire refuses when the published assignment cannot be proved to be this execution" {
  setup_queue "ai-retire-unprovable"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local gen
  gen="$(card_field t001 started_at)"

  # 旧 plan.sh が書いた assignment (世代の記録がない) を模す。
  rm -f "$ASSIGN_DIR/Ren.identity"

  run plan retire t001 --agent Ren --started-at "$gen" --mission "$TEST_MISSION"
  [ "$status" -eq "$PRECONDITION_UNMET" ]

  # 証拠が足りないときは前提を弱めず「実行しない」に倒す。
  [ "$(card_field t001 status)" = "in_progress" ]
  [ "$(card_field t001 worker)" = "Ren" ]
  [ -f "$ASSIGN_DIR/Ren" ]

  cleanup_queue
}

@test "retire proceeds when the worker holds no assignment at all" {
  setup_queue "ai-retire-absent"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local gen
  gen="$(card_field t001 started_at)"

  # assignment だけ先に消えている (クラッシュ後の取り残し card)。
  rm -f "$ASSIGN_DIR/Ren" "$ASSIGN_DIR/Ren.identity"

  run plan retire t001 --agent Ren --started-at "$gen" --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  [ "$(card_field t001 status)" = "pending" ]

  cleanup_queue
}

@test "retire requires an explicit generation" {
  setup_queue "ai-retire-needs-gen"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]

  run plan retire t001 --agent Ren --mission "$TEST_MISSION"
  [ "$status" -eq 1 ]

  # 世代を省略しても「とりあえず実行」にならないこと。
  [ "$(card_field t001 status)" = "in_progress" ]
  [ -f "$ASSIGN_DIR/Ren" ]

  cleanup_queue
}

@test "retire --outcome needs-director hands the card to the Director" {
  setup_queue "ai-retire-needs-director"
  add_task t001

  run plan pull --agent Ren --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -eq 0 ]
  local gen
  gen="$(card_field t001 started_at)"

  run plan retire t001 --agent Ren --started-at "$gen" --mission "$TEST_MISSION" \
    --outcome needs-director --reason "watchdog terminated the worker"
  [ "$status" -eq 0 ]

  [ "$(card_field t001 status)" = "needs_director" ]
  grep -q 'needs_director_reason:' "$TASKS_DIR/t001.md"
  [ ! -e "$ASSIGN_DIR/Ren" ]

  cleanup_queue
}

# ---------------------------------------------------------------------------
# 4. Worker 名が assignments ディレクトリから出られないこと
# ---------------------------------------------------------------------------

@test "an agent name cannot escape the assignments directory" {
  setup_queue "ai-agent-name-guard"

  # card 側の worker も同じ名前にしておく。そうしないと worker 不一致で先に
  # 弾かれてしまい、パスを組み立てる側のガードを通らない。
  add_task t001 in_progress '"../../victim"' '"2026-01-01T00:00:00Z"'

  local victim="$TEST_QUEUE/victim"
  echo "keep me" > "$victim"

  run plan retire t001 --agent "../../victim" --started-at "2026-01-01T00:00:00Z" \
    --mission "$TEST_MISSION"
  [ "$status" -ne 0 ]
  [ "$status" -ne "$PRECONDITION_UNMET" ]   # 前提ではなく名前が不正、で落ちること
  [ -f "$victim" ]
  [ "$(card_field t001 status)" = "in_progress" ]

  cleanup_queue
}

@test "an agent name cannot collide with the identity sidecar" {
  setup_queue "ai-agent-name-reserved"
  add_task t001

  run plan pull --agent "Ren.identity" --skills bash --task t001 --mission "$TEST_MISSION"
  [ "$status" -ne 0 ]

  cleanup_queue
}
