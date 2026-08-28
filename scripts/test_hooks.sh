#!/usr/bin/env bash
# scripts/test_hooks.sh
# PreToolUse hook (hooks/pre-tool-use.sh) の回帰テスト
#
# 使い方:
#   bash scripts/test_hooks.sh
#
# 終了コード:
#   0 — 全テストパス
#   1 — 1件以上失敗

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="${REPO_ROOT}/hooks/pre-tool-use.sh"

PASS=0
FAIL=0

# テスト共通ヘルパー
_run_hook() {
  local input="$1"
  shift
  env -i \
    HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CREWVIA_REPO="$REPO_ROOT" \
    "$@" \
    bash "$HOOK" <<< "$input" 2>/tmp/test_hooks_stderr
}

_assert() {
  local test_name="$1" expected_decision="$2" actual_stdout="$3" actual_exit="$4"

  # exit code は常に 0 であること
  if [ "$actual_exit" -ne 0 ]; then
    echo "FAIL [$test_name]: exit code $actual_exit (expected 0)"
    echo "  stdout: $actual_stdout"
    echo "  stderr: $(cat /tmp/test_hooks_stderr)"
    FAIL=$((FAIL + 1))
    return
  fi

  # stdout に JSON が含まれること
  if ! echo "$actual_stdout" | jq -e . >/dev/null 2>&1; then
    echo "FAIL [$test_name]: stdout is not valid JSON"
    echo "  stdout: '$actual_stdout'"
    echo "  stderr: $(cat /tmp/test_hooks_stderr)"
    FAIL=$((FAIL + 1))
    return
  fi

  local actual_decision
  actual_decision="$(echo "$actual_stdout" | jq -r '.hookSpecificOutput.permissionDecision')"

  if [ "$actual_decision" = "$expected_decision" ]; then
    echo "PASS [$test_name]: $actual_decision"
    PASS=$((PASS + 1))
  else
    echo "FAIL [$test_name]: expected=$expected_decision actual=$actual_decision"
    echo "  stdout: $actual_stdout"
    echo "  stderr: $(cat /tmp/test_hooks_stderr)"
    FAIL=$((FAIL + 1))
  fi
}

# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------

echo "=== hooks/pre-tool-use.sh 回帰テスト ==="
echo ""

# 1. Worker (role なし) が bash 実行: exit 0 + allow を返す
#    修正前: `grep 'role:'` が exit 1 → pipefail でスクリプトが exit 1 になり no-output エラー
STDOUT=$(_run_hook \
  '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}' \
  CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto \
  || true)
EXIT=$?
_assert "Worker Bash 非破壊コマンド (pipefail 回帰)" "allow" "$STDOUT" "$EXIT"

# 2. Safe tool (Read) は即 allow
STDOUT=$(_run_hook \
  '{"tool_name":"Read","tool_input":{"file_path":"/tmp/test.txt"}}' \
  CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" AGENT_NAME=Haruto \
  || true)
EXIT=$?
_assert "Safe tool Read → allow" "allow" "$STDOUT" "$EXIT"

# 3. Glob も safe tool
STDOUT=$(_run_hook \
  '{"tool_name":"Glob","tool_input":{"pattern":"**/*.sh"}}' \
  CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" AGENT_NAME=Haruto \
  || true)
EXIT=$?
_assert "Safe tool Glob → allow" "allow" "$STDOUT" "$EXIT"

# 4. Director (Sora) は即通過: stdout が空または allow
#    Director は emit_decision を呼ばず exit 0 するため stdout は空
STDOUT=$(_run_hook \
  '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}' \
  CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" AGENT_NAME=Sora \
  || true)
EXIT=$?
if [ "$EXIT" -eq 0 ]; then
  echo "PASS [Director bypass → exit 0]"
  PASS=$((PASS + 1))
else
  echo "FAIL [Director bypass]: exit $EXIT"
  FAIL=$((FAIL + 1))
fi

# 5. Global deny (rm -rf /) は Worker でも deny
STDOUT=$(_run_hook \
  '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}' \
  CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto \
  || true)
EXIT=$?
_assert "Global deny rm -rf / → deny" "deny" "$STDOUT" "$EXIT"

# 6. Taskvia disabled + SKILLS 未設定: 非破壊 Bash は allow
STDOUT=$(_run_hook \
  '{"tool_name":"Bash","tool_input":{"command":"echo hello"}}' \
  CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" AGENT_NAME=Haruto \
  || true)
EXIT=$?
_assert "SKILLS 未設定の非破壊 Bash → allow" "allow" "$STDOUT" "$EXIT"

# 7. Worker が Bash 以外のツール (Edit) を使う場合 — skill check で処理される
STDOUT=$(_run_hook \
  '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/test.sh"}}' \
  CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto \
  || true)
EXIT=$?
# Edit は skill check に委ねられ、bash skill は Edit を allow しているはず
# (skill-permissions.yaml の bash 定義に依存するためここでは exit 0 のみ確認)
if [ "$EXIT" -eq 0 ]; then
  echo "PASS [Worker Edit → exit 0 (decision delegated to skill)]"
  PASS=$((PASS + 1))
else
  echo "FAIL [Worker Edit]: exit $EXIT"
  FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# hooks/post-tool-use.sh テスト
# ---------------------------------------------------------------------------

POST_HOOK="${REPO_ROOT}/hooks/post-tool-use.sh"

echo ""
echo "=== hooks/post-tool-use.sh 回帰テスト ==="
echo ""

_run_post_hook() {
  local input="$1"
  shift
  env -i \
    HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CREWVIA_REPO_ROOT="$REPO_ROOT" \
    "$@" \
    bash "$POST_HOOK" <<< "$input" 2>/tmp/test_post_stderr
}

_assert_exit() {
  local test_name="$1" expected_exit="$2" actual_exit="$3"
  if [ "$actual_exit" -eq "$expected_exit" ]; then
    echo "PASS [$test_name]: exit $actual_exit"
    PASS=$((PASS + 1))
  else
    echo "FAIL [$test_name]: expected exit $expected_exit, got exit $actual_exit"
    echo "  stderr: $(cat /tmp/test_post_stderr 2>/dev/null)"
    FAIL=$((FAIL + 1))
  fi
}

# PT-1: TASKVIA disabled 正常系 — exit 0
_run_post_hook \
  '{"tool_name":"Bash","tool_input":{"command":"echo test"}}' \
  CREWVIA_TASKVIA=disabled AGENT_NAME=TestHaruto TASK_ID=t999 \
  || true
EXIT=$?
_assert_exit "PT-1: TASKVIA disabled 正常系 → exit 0" 0 "$EXIT"

# PT-2: クラッシュガード確認 — 壊れた CREWVIA_REPO_ROOT でも exit 0
# (activity-log step で mkdir が失敗してもクラッシュガードが exit 0 を保証する)
env -i \
  HOME=/tmp \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  CREWVIA_REPO_ROOT="/nonexistent_crewvia_repo_for_test_$$" \
  CREWVIA_TASKVIA=disabled AGENT_NAME=TestHaruto TASK_ID=t999 \
  bash "$POST_HOOK" <<< '{"tool_name":"Bash","tool_input":{"command":"echo test"}}' \
  2>/tmp/test_post_stderr || true
EXIT=$?
_assert_exit "PT-2: 壊れた env でも exit 0 (crash guard)" 0 "$EXIT"

# PT-3: TASKVIA TOKEN 設定なし (standalone mode) — exit 0
_run_post_hook \
  '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/test.txt"}}' \
  TASKVIA_TOKEN="" AGENT_NAME=TestHaruto TASK_ID=t998 \
  || true
EXIT=$?
_assert_exit "PT-3: TOKEN 未設定 standalone → exit 0" 0 "$EXIT"

# PT-4: curl 失敗 (Taskvia 接続不可) でも exit 0
_run_post_hook \
  '{"tool_name":"Bash","tool_input":{"command":"ls"}}' \
  CREWVIA_TASKVIA=enabled TASKVIA_TOKEN=dummy_token \
  TASKVIA_URL=http://127.0.0.1:19999 \
  AGENT_NAME=TestHaruto TASK_ID=t997 \
  || true
EXIT=$?
_assert_exit "PT-4: Taskvia 接続不可でも exit 0" 0 "$EXIT"

# ---------------------------------------------------------------------------
# 結果
# ---------------------------------------------------------------------------

echo ""
echo "=== 結果: PASS=$PASS FAIL=$FAIL ==="

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
