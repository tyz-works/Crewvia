#!/usr/bin/env bash
# scripts/test_task_file_write_guard.sh
# hooks/pre-tool-use.sh の task file direct-write guard (t015) の回帰テスト
#
# 背景: t004 で review skill の Worker が Result を
#   `cat >> queue/missions/<slug>/tasks/t004.md <<'EOF' ... EOF` で書き込もうと
#   してハングした (42 分間無応答)。過去にも research skill Worker が同じ
#   パターンで 9 分以上ハングしている (再発)。根本原因は
#   queue/missions/**/tasks/*.md への直接シェル書き込みが正規の経路として
#   選ばれてしまうこと自体にあるため、hooks/pre-tool-use.sh に構造的ガードを
#   追加した (t011/t014 の worktree edit guard と同じ配置・crash guard 作法)。
#
# 検証内容:
#   1-4. task ファイルへの `>` / `>>` (heredoc 併用含む) / `sed -i` / `tee` が
#        deny されること
#   5-9. 誤爆しないこと: 他ファイルへの heredoc / リダイレクト、task ファイルの
#        単純な読み取り、plan.sh 自身の呼び出し、絶対パスでの読み取り
#   10.  絶対パスでの task ファイル書き込みも deny されること (相対パス限定の
#        抜け穴になっていないこと)
#   11.  Director はこのガードをバイパスすること (t011/t014 と同じ前提)
#
# 使い方: bash scripts/test_task_file_write_guard.sh
# 終了コード: 0 = 全パス, 1 = 1件以上失敗

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="${REPO_ROOT}/hooks/pre-tool-use.sh"

PASS=0
FAIL=0

_run_hook() {
  local input="$1"
  shift
  env -i \
    HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CREWVIA_REPO="$REPO_ROOT" \
    "$@" \
    bash "$HOOK" <<< "$input" 2>/tmp/test_task_file_write_guard_stderr
}

_assert_decision() {
  local test_name="$1" expected_decision="$2" actual_stdout="$3" actual_exit="$4"

  if [ "$actual_exit" -ne 0 ]; then
    echo "FAIL [$test_name]: exit code $actual_exit (expected 0)"
    echo "  stdout: $actual_stdout"
    echo "  stderr: $(cat /tmp/test_task_file_write_guard_stderr)"
    FAIL=$((FAIL + 1))
    return
  fi

  if ! echo "$actual_stdout" | jq -e . >/dev/null 2>&1; then
    echo "FAIL [$test_name]: stdout is not valid JSON"
    echo "  stdout: '$actual_stdout'"
    echo "  stderr: $(cat /tmp/test_task_file_write_guard_stderr)"
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
    echo "  stderr: $(cat /tmp/test_task_file_write_guard_stderr)"
    FAIL=$((FAIL + 1))
  fi
}

# JSON 文字列を安全に組み立てるヘルパー (改行を含む heredoc コマンドを扱うため)
_bash_input_json() {
  local command="$1"
  jq -nc --arg cmd "$command" '{tool_name: "Bash", tool_input: {command: $cmd}}'
}

echo "=== scripts/test_task_file_write_guard.sh (t015 task file direct-write guard) ==="
echo ""

# ---------------------------------------------------------------------------
# 1-4: 実際にハングを引き起こしたパターン群 → deny
# ---------------------------------------------------------------------------

INPUT="$(_bash_input_json "cat >> queue/missions/20260908-codex-reviewer-phase3/tasks/t004.md <<'EOF'
## Result
done
EOF")"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=review AGENT_NAME=Seo || true)
EXIT=$?
_assert_decision "1. cat >> heredoc で task ファイルに追記 → deny (t004 の実際の事故を再現)" "deny" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json 'echo "## Result" > queue/missions/foo/tasks/t001.md')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "2. echo > で task ファイルに書き込み → deny" "deny" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json "sed -i 's/status: pending/status: done/' queue/missions/foo/tasks/t001.md")"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "3. sed -i で task ファイルを書き換え → deny" "deny" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json 'printf "x" | tee -a queue/missions/foo/tasks/t001.md')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "4. tee -a で task ファイルに追記 → deny" "deny" "$STDOUT" "$EXIT"

# ---------------------------------------------------------------------------
# 5-9: 誤爆検証 (heredoc / リダイレクト自体は正当な用途で使われる)
# ---------------------------------------------------------------------------

INPUT="$(_bash_input_json "cat > /tmp/test_script.sh <<'EOF'
#!/usr/bin/env bash
echo hello
EOF")"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "5. 無関係なファイルへの heredoc (テストスクリプト作成) → allow (誤爆しない)" "allow" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json 'echo hello > /tmp/out.txt')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "6. 無関係なファイルへの通常リダイレクト → allow (誤爆しない)" "allow" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json 'cat queue/missions/foo/tasks/t001.md')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "7. task ファイルの単純な読み取り (cat) → allow (誤爆しない)" "allow" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json 'cat queue/missions/foo/tasks/t001.md > /tmp/copy.txt')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "8. task ファイルを読んで別ファイルにコピー (リダイレクト先は task ファイルでない) → allow (誤爆しない)" "allow" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json './scripts/plan.sh done t001 "Result: 全部やりました" --mission foo')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "9. plan.sh done 経由の正規記録 → allow (plan.sh 自身の書き込みは対象外)" "allow" "$STDOUT" "$EXIT"

# ---------------------------------------------------------------------------
# 10: 絶対パスでの書き込みも deny されること (相対パス限定の抜け穴が無いこと)
# ---------------------------------------------------------------------------

INPUT="$(_bash_input_json 'echo "x" >> /home/tkadmin/workspace/crewvia/queue/missions/foo/tasks/t001.md')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "10. 絶対パスでの task ファイル書き込みも deny される" "deny" "$STDOUT" "$EXIT"

# ---------------------------------------------------------------------------
# 11: Director はガードをバイパスする (t011/t014 と同じ前提)
# ---------------------------------------------------------------------------

INPUT="$(_bash_input_json "cat >> queue/missions/foo/tasks/t001.md <<'EOF'
x
EOF")"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" AGENT_NAME=Sora || true)
EXIT=$?
if [ "$EXIT" -eq 0 ]; then
  echo "PASS [11. Director (Sora) はガードをバイパスする → exit 0]"
  PASS=$((PASS + 1))
else
  echo "FAIL [11. Director bypass]: exit $EXIT"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "================================"
echo "Results: ${PASS} passed, ${FAIL} failed"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
