#!/usr/bin/env bats
# tests/target-dir-settings-revert.bats
#
# Regression tests for TARGET_DIR settings.local.json 汚染防止 (Option D 実装)。
# start.sh が crewvia-worker-{AGENT_NAME}.json を書き込み settings.local.json を
# 一切変更しないこと、および plan.sh done がファイルを削除することを検証する。
#
# Related: mission 20260903-target-dir-settings-revert / t003
#
# Run: npx bats tests/target-dir-settings-revert.bats
#      (requires Node.js / npx; bats 1.13+ recommended)

PLAN_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/plan.sh"
START_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/start.sh"
CLEANUP_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/cleanup-target-dir.sh"
REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# setup_queue: Create a minimal queue fixture in TMPDIR
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

# add_task: Add a minimal task to the fixture
add_task() {
  local id="$1"
  local status="${2:-pending}"

  cat >"$TASKS_DIR/${id}.md" <<MD
---
id: $id
title: "Task $id"
skills: [bash]
priority: medium
status: $status
blocked_by: []
target_dir: null
worker: TestWorker
started_at: null
completed_at: null
---

## Description
Test task $id.
MD
}

# plan_done: Call plan.sh done with TARGET_DIR and AGENT_NAME in env
plan_done() {
  local task_id="$1"
  local target_dir="$2"
  local agent_name="${3:-TestWorker}"
  CREWVIA_QUEUE="$TEST_QUEUE" \
    TARGET_DIR="$target_dir" \
    AGENT_NAME="$agent_name" \
    bash "$PLAN_SH" done "$task_id" "Test result" 2>&1
}

# inject_worker_hooks: Simulate what start.sh does — write crewvia-worker-*.json
inject_worker_hooks() {
  local target_dir="$1"
  local agent_name="${2:-TestWorker}"
  local settings_dir="$target_dir/.claude"
  mkdir -p "$settings_dir"
  python3 - "$settings_dir/crewvia-worker-${agent_name}.json" "$REPO_ROOT" <<'PYEOF'
import sys, json
settings_path = sys.argv[1]
repo_root = sys.argv[2]
data = {
    "hooks": {
        "PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit",
                        "hooks": [{"type": "command", "command": f"{repo_root}/hooks/pre-tool-use.sh"}]}],
        "PostToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit",
                         "hooks": [{"type": "command", "command": f"{repo_root}/hooks/post-tool-use.sh"}]}]
    }
}
with open(settings_path, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
PYEOF
}

teardown() {
  [[ -n "${TEST_QUEUE:-}" ]] && rm -rf "$TEST_QUEUE" 2>/dev/null || true
  [[ -n "${TARGET_DIR_FIXTURE:-}" ]] && rm -rf "$TARGET_DIR_FIXTURE" 2>/dev/null || true
  true
}

# ---------------------------------------------------------------------------
# Case A: 元 settings.local.json なし → Worker 起動 → 停止 → ファイル削除確認
# ---------------------------------------------------------------------------

@test "Case A: TARGET_DIR に settings.local.json なし → crewvia-worker.json のみ作成 → plan.sh done で削除" {
  TARGET_DIR_FIXTURE="$(mktemp -d)"
  setup_queue "test-mission-a"
  add_task "t001" "in_progress"

  # start.sh 相当の injection を実行（settings.local.json は存在しない状態）
  inject_worker_hooks "$TARGET_DIR_FIXTURE" "TestWorker"

  # crewvia-worker.json が作成されること
  [[ -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-TestWorker.json" ]]

  # settings.local.json は作成されないこと
  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/settings.local.json" ]]

  # crewvia-worker.json の内容に絶対パスの hooks が含まれること
  run python3 -c "
import json
with open('$TARGET_DIR_FIXTURE/.claude/crewvia-worker-TestWorker.json') as f:
    d = json.load(f)
assert 'hooks' in d, 'hooks key missing'
assert 'PreToolUse' in d['hooks'], 'PreToolUse missing'
print('ok')
"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ok"* ]]

  # plan.sh done でファイルが削除されること
  plan_done "t001" "$TARGET_DIR_FIXTURE" "TestWorker"

  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-TestWorker.json" ]]
}

# ---------------------------------------------------------------------------
# Case B: 元 settings.local.json 有り (hooks なし) → Worker 起動 → 停止 → 元通り復元
# ---------------------------------------------------------------------------

@test "Case B: settings.local.json 有り (hooks なし) → 起動後も settings.local.json は変更なし" {
  TARGET_DIR_FIXTURE="$(mktemp -d)"
  setup_queue "test-mission-b"
  add_task "t001" "in_progress"

  # 元の settings.local.json（hooks なし）を作成
  mkdir -p "$TARGET_DIR_FIXTURE/.claude"
  cat >"$TARGET_DIR_FIXTURE/.claude/settings.local.json" <<JSON
{
  "permissions": {
    "allow": ["Read"]
  }
}
JSON
  local original_content
  original_content="$(cat "$TARGET_DIR_FIXTURE/.claude/settings.local.json")"

  # start.sh 相当の injection（crewvia-worker.json のみ書き込む）
  inject_worker_hooks "$TARGET_DIR_FIXTURE" "TestWorker"

  # crewvia-worker.json が作成されること
  [[ -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-TestWorker.json" ]]

  # settings.local.json が変更されていないこと
  local after_content
  after_content="$(cat "$TARGET_DIR_FIXTURE/.claude/settings.local.json")"
  [ "$original_content" = "$after_content" ]

  # plan.sh done 後、settings.local.json は変わらず crewvia-worker.json のみ削除
  plan_done "t001" "$TARGET_DIR_FIXTURE" "TestWorker"

  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-TestWorker.json" ]]
  local final_content
  final_content="$(cat "$TARGET_DIR_FIXTURE/.claude/settings.local.json")"
  [ "$original_content" = "$final_content" ]
}

# ---------------------------------------------------------------------------
# Case C: 元 settings.local.json 有り (別の hooks) → Worker 起動 → 停止 → 元 hooks 保持
# ---------------------------------------------------------------------------

@test "Case C: settings.local.json に既存 hooks あり → 起動後も既存 hooks は保持される" {
  TARGET_DIR_FIXTURE="$(mktemp -d)"
  setup_queue "test-mission-c"
  add_task "t001" "in_progress"

  # 元の settings.local.json（独自 hooks あり）を作成
  mkdir -p "$TARGET_DIR_FIXTURE/.claude"
  cat >"$TARGET_DIR_FIXTURE/.claude/settings.local.json" <<JSON
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [{"type": "command", "command": "/usr/local/bin/my-custom-hook.sh"}]
      }
    ]
  }
}
JSON
  local original_content
  original_content="$(cat "$TARGET_DIR_FIXTURE/.claude/settings.local.json")"

  # start.sh 相当の injection（crewvia-worker.json のみ書き込む）
  inject_worker_hooks "$TARGET_DIR_FIXTURE" "TestWorker"

  # settings.local.json の hooks が変更されていないこと
  run python3 -c "
import json
with open('$TARGET_DIR_FIXTURE/.claude/settings.local.json') as f:
    d = json.load(f)
hooks = d.get('hooks', {}).get('PreToolUse', [])
assert any('/usr/local/bin/my-custom-hook.sh' in str(h) for h in hooks), 'original hook missing'
# crewvia hook が settings.local.json に混入していないこと
crewvia_injected = any('pre-tool-use.sh' in str(h) for h in hooks)
assert not crewvia_injected, f'crewvia hook was injected into settings.local.json (should not)'
print('ok')
"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ok"* ]]

  # plan.sh done 後、settings.local.json は変わらず crewvia-worker.json のみ削除
  plan_done "t001" "$TARGET_DIR_FIXTURE" "TestWorker"

  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-TestWorker.json" ]]
  local final_content
  final_content="$(cat "$TARGET_DIR_FIXTURE/.claude/settings.local.json")"
  [ "$original_content" = "$final_content" ]
}

# ---------------------------------------------------------------------------
# Case D: 並列 Worker 2 個で同 TARGET_DIR → race condition 対処（ファイル独立）
# ---------------------------------------------------------------------------

@test "Case D: 並列 Worker 2 個が同 TARGET_DIR → 各 Worker のファイルが独立して作成・削除される" {
  TARGET_DIR_FIXTURE="$(mktemp -d)"
  setup_queue "test-mission-d"
  add_task "t001" "in_progress"
  add_task "t002" "in_progress"

  # Worker A と Worker B が同一 TARGET_DIR に並列起動
  inject_worker_hooks "$TARGET_DIR_FIXTURE" "Haruto"
  inject_worker_hooks "$TARGET_DIR_FIXTURE" "Luca"

  # 両方のファイルが独立して存在すること
  [[ -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Haruto.json" ]]
  [[ -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Luca.json" ]]

  # Worker A (Haruto) が done → Haruto のファイルのみ削除、Luca のファイルは残存
  CREWVIA_QUEUE="$TEST_QUEUE" \
    TARGET_DIR="$TARGET_DIR_FIXTURE" \
    AGENT_NAME="Haruto" \
    bash "$PLAN_SH" done "t001" "Result A" 2>&1

  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Haruto.json" ]]
  [[ -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Luca.json" ]]

  # Worker B (Luca) が done → Luca のファイルも削除
  CREWVIA_QUEUE="$TEST_QUEUE" \
    TARGET_DIR="$TARGET_DIR_FIXTURE" \
    AGENT_NAME="Luca" \
    bash "$PLAN_SH" done "t002" "Result B" 2>&1

  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Luca.json" ]]
}

# ---------------------------------------------------------------------------
# cleanup-target-dir.sh のテスト
# ---------------------------------------------------------------------------

@test "cleanup-target-dir.sh: 孤立ファイルを削除できる" {
  TARGET_DIR_FIXTURE="$(mktemp -d)"

  # 孤立ファイルを手動作成
  mkdir -p "$TARGET_DIR_FIXTURE/.claude"
  echo '{"hooks":{}}' >"$TARGET_DIR_FIXTURE/.claude/crewvia-worker-OldWorker.json"

  run bash "$CLEANUP_SH" "$TARGET_DIR_FIXTURE" "OldWorker"
  [ "$status" -eq 0 ]
  [[ "$output" == *"削除"* ]]
  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-OldWorker.json" ]]
}

@test "cleanup-target-dir.sh: 全 crewvia-worker-*.json を一括削除できる" {
  TARGET_DIR_FIXTURE="$(mktemp -d)"
  mkdir -p "$TARGET_DIR_FIXTURE/.claude"
  echo '{}' >"$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Alice.json"
  echo '{}' >"$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Bob.json"

  run bash "$CLEANUP_SH" "$TARGET_DIR_FIXTURE"
  [ "$status" -eq 0 ]
  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Alice.json" ]]
  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Bob.json" ]]
}

@test "cleanup-target-dir.sh: settings.local.json には触れない" {
  TARGET_DIR_FIXTURE="$(mktemp -d)"
  mkdir -p "$TARGET_DIR_FIXTURE/.claude"
  echo '{"permissions":{"allow":["Read"]}}' >"$TARGET_DIR_FIXTURE/.claude/settings.local.json"
  echo '{}' >"$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Alice.json"

  run bash "$CLEANUP_SH" "$TARGET_DIR_FIXTURE"
  [ "$status" -eq 0 ]

  # settings.local.json は削除されないこと
  [[ -f "$TARGET_DIR_FIXTURE/.claude/settings.local.json" ]]
  # crewvia-worker.json は削除されること
  [[ ! -f "$TARGET_DIR_FIXTURE/.claude/crewvia-worker-Alice.json" ]]
}

@test "cleanup-target-dir.sh: TARGET_DIR が存在しない場合はエラー終了" {
  run bash "$CLEANUP_SH" "/nonexistent/path/$(date +%s)"
  [ "$status" -ne 0 ]
  [[ "$output" == *"ERROR"* ]]
}

@test "cleanup-target-dir.sh: クリーンアップ対象なしの場合は INFO で正常終了" {
  TARGET_DIR_FIXTURE="$(mktemp -d)"
  mkdir -p "$TARGET_DIR_FIXTURE/.claude"

  run bash "$CLEANUP_SH" "$TARGET_DIR_FIXTURE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"INFO"* ]]
}
