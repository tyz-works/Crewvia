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
# t019 (Seo/Opus 5 最終レビュー [P2] の修正):
#   12-13. 報告コマンドの引数として該当パスを**引用しただけ**の
#          plan.sh done / gh pr comment は deny されないこと (本インシデントを
#          報告しようとする行為自体が deny される自己矛盾の回帰テスト)
#   14.    上記の「クォート内は無視する」対策を悪用した compound command での
#          迂回 (`; ./scripts/plan.sh --help; cat >> ... <<EOF` を 1 コマンドに
#          混ぜる) が deny されたままであること — Director 指摘のバイパス懸念
#   15.    コマンド置換 `$(...)` の中に実際の書き込みを隠しても deny された
#          ままであること (クォート除去を意図的に無効化しているため)
#
# t023 (Seo/Opus 5 再レビュー [P2] の修正): t019 のクォート一括除去は、
# パス自体をクォートで囲んだ**本物の書き込み**まで判定対象から消してしまう
# 検出力の退行を生んでいた (`cat >> "queue/missions/.../t004.md" <<EOF` 等)。
#   16-20. パスをクォートで囲んだ 5 パターンの本物の書き込みが deny に戻る
#          こと (退行の回帰テスト。修正前 (t019 時点) はいずれも allow だった)
#   21.    16-20 の修正 (クォート内の中身が丸ごとパスの場合だけクォートを
#          剥がす) を入れても、12-13 の allow ケースは引き続き allow のまま
#          であること (t019 の誤 deny 修正が壊れていないことの回帰)
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

# ---------------------------------------------------------------------------
# 12-13 (t019 fix): 報告コマンドの引用テキストは deny されない
# ---------------------------------------------------------------------------

INPUT="$(_bash_input_json './scripts/plan.sh done t017 "原因は cat >> queue/missions/m1/tasks/t004.md の heredoc"')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "12. plan.sh done の引数に該当パスを引用 → allow (t019: Seo 実測の回帰テスト)" "allow" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json 'gh pr comment 183 --body "guard blocks: cat >> queue/missions/m1/tasks/t004.md"')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "13. gh pr comment --body に該当パスを引用 → allow (t019: Seo 実測の回帰テスト)" "allow" "$STDOUT" "$EXIT"

# ---------------------------------------------------------------------------
# 14 (t019 fix): 12-13 の「クォート内は無視」対策を compound command で悪用した
# 迂回が引き続き deny されること (Director 指摘のバイパス懸念への回答)
# ---------------------------------------------------------------------------

INPUT="$(_bash_input_json "X; ./scripts/plan.sh --help; cat >> queue/missions/foo/tasks/t001.md <<'EOF'
malicious
EOF")"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "14. plan.sh を騙った compound command での偽装 → deny (クォート除去を使わせない)" "deny" "$STDOUT" "$EXIT"

# ---------------------------------------------------------------------------
# 15 (t019 fix): コマンド置換 \$(...) の中に実際の書き込みを隠しても deny
# されること (\$( を含む場合はクォート除去自体を行わない設計の検証)
# ---------------------------------------------------------------------------

INPUT="$(_bash_input_json './scripts/plan.sh done t001 "$(cat >> queue/missions/foo/tasks/t001.md <<EOF
malicious
EOF
)"')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "15. \$(...) コマンド置換の中に実際の書き込みを隠す → deny (クォート除去を無効化)" "deny" "$STDOUT" "$EXIT"

# ---------------------------------------------------------------------------
# 16-20 (t023 fix): パスをクォートで囲んだ本物の書き込みが deny に戻ること
# (Seo 実測による退行の回帰テスト。t019 時点ではいずれも allow に退行していた)
# ---------------------------------------------------------------------------

INPUT="$(_bash_input_json 'cat >> "queue/missions/m1/tasks/t004.md" <<EOF
x
EOF')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "16. cat >> \"...\" <<EOF (ダブルクォートで囲んだパスへの実書き込み) → deny (t023 退行修正)" "deny" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json "cat >> 'queue/missions/m1/tasks/t004.md' <<EOF
x
EOF")"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "17. cat >> '...' <<EOF (シングルクォートで囲んだパスへの実書き込み) → deny (t023 退行修正)" "deny" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json 'cat >> "/home/x/crewvia/queue/missions/m1/tasks/t004.md"')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "18. 絶対パスをダブルクォートで囲んだ実書き込み → deny (t023 退行修正)" "deny" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json "sed -i 's/a/b/' 'queue/missions/m1/tasks/t004.md'")"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "19. sed -i '...' 'クォートで囲んだパス' → deny (t023 退行修正)" "deny" "$STDOUT" "$EXIT"

INPUT="$(_bash_input_json 'tee -a "queue/missions/m1/tasks/t004.md"')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "20. tee -a \"クォートで囲んだパス\" → deny (t023 退行修正)" "deny" "$STDOUT" "$EXIT"

# ---------------------------------------------------------------------------
# 21 (t023 fix): 16-20 の修正後も t019 の allow ケースが壊れていないこと
# ---------------------------------------------------------------------------

INPUT="$(_bash_input_json './scripts/plan.sh done t021 "#183 は cat >> queue/missions/m1/tasks/t004.md を deny する"')"
STDOUT=$(_run_hook "$INPUT" CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash AGENT_NAME=Haruto || true)
EXIT=$?
_assert_decision "21. plan.sh done への引用テキストは引き続き allow (t023 修正後も t019 の修正が壊れていない)" "allow" "$STDOUT" "$EXIT"

echo ""
echo "================================"
echo "Results: ${PASS} passed, ${FAIL} failed"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
