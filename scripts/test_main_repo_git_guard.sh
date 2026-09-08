#!/usr/bin/env bash
# test_main_repo_git_guard.sh — t001 回帰テスト (mission 20260908-main-repo-protection)
#
# 不具合: 2026-09-08、docs Worker (worktree モードで作業中) が以下を実行し、
# 主リポジトリ (main checkout) のブランチを main から切り替える事故が発生した:
#
#   cd $CREWVIA_REPO_ROOT && git fetch origin main && git checkout -b docs/...
#
# Director が検知して復旧済み (コミットは積まれておらず実害なし)。Sofia でも
# 同種の事故があり2回目。PR#182 (t011/t014) の worktree scope guard は
# Edit/Write/MultiEdit/NotebookEdit のみが対象で、Bash 経由の git 操作は対象外
# だった。
#
# 修正 (hooks/pre-tool-use.sh + hooks/lib_main_repo_git_guard.py):
#   worktree を持つ Worker (TASK_ID が解決済みで TARGET_DIR が未設定) の Bash
#   コマンドに「主リポジトリへの参照」と「破壊的 git 動詞」が同居していたら
#   emit_decision "deny" で拒否する。主リポジトリへの参照が `.claude/worktrees/`
#   へ続く場合は対象外 (別の独立した worktree checkout への正当な参照)。
#
# このテストで検証:
#   1. worktree Worker が `cd $CREWVIA_REPO_ROOT && ... git checkout -b ...`
#      (変数表記) を実行 → deny
#   2. 同じ Worker が主リポジトリの絶対パスを直接使って同様の操作 → deny
#   3. `git branch -D` / `git worktree add` も同様に deny される
#   4. 同じ Worker が `$CREWVIA_REPO_ROOT` を参照しつつ読み取り系 git
#      (status/log/diff/show) を実行 → ブロックされない
#   5. 同じ Worker が `$CREWVIA_REPO_ROOT/scripts/plan.sh` を呼ぶ (正当な用途) →
#      ブロックされない
#   6. 同じ Worker が worktree 内で通常の git 操作 (checkout -b / commit / merge、
#      主リポジトリへの参照なし) を実行 → ブロックされない (最重要: これが
#      止まると Worker が一切作業できなくなる)
#   7. 同じ Worker が worktree の絶対パス (主リポジトリパスを prefix に含む) に
#      cd してから git checkout -b → ブロックされない (.claude/worktrees/ 除外)
#   8. Director (role: director) が主リポジトリで危険な git 操作を実行 →
#      ブロックされない (role チェックでこのガードより前に bypass 済み)
#   9. TARGET_DIR モードの Worker が同様の危険なコマンドを実行 → このガードでは
#      ブロックされない (対象外)
#   10. TASK_ID 未解決 (ad-hoc セッション) → ブロックされない
#   11. 事故を報告する plan.sh done の Result 文字列がクォート内で
#       "$CREWVIA_REPO_ROOT" や "git checkout" を引用しているだけ → ブロックされ
#       ない (このタスク自身の Result 記述が誤検知されないことの検証)
#   12. コマンド置換 ($(...)) でクォート内に隠された危険な git 操作 →
#       フェイルセーフでブロックされる (クォートマスクをスキップする分岐の検証)
#   13. anchor ($CREWVIA_REPO 相当) が main checkout ではなく worktree を指す
#       場合、ガードそのものが安全に無効化される (t014 と同じ P2 対策の踏襲)
#
# t005 (Seo/Opus 5 レビュー [P1][P2] への対応。mission 20260908-main-repo-protection):
#   14. [P1] Seo が本番同形 env で実測した誤爆 5 例 (参照と破壊的動詞が別コマンド
#       /別行にあるだけで同居判定されていたケース) → not blocked に修正
#   15. [P1] QA (t003) が確認した検出 14 ケースの回帰 (検出力の退行がないこと)
#   16. [P2] 事故コマンドをシングルクォートで引用しただけの報告コマンド →
#       not blocked。ダブルクォート内 / 裸のバッククォートは引き続き deny
#   17. 複数行スクリプトのケース (修正前はゼロだった)
#
# 実行: bash scripts/test_main_repo_git_guard.sh
# 副作用: /tmp 配下に合成の「main repo」ディレクトリを作成し終了時に削除する
#         (crewvia の実 repo / registry / queue には一切触れない)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="${OWN_CHECKOUT_ROOT}/hooks/pre-tool-use.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST=""
cleanup() {
  if [[ -n "$TMPDIR_TEST" && -d "$TMPDIR_TEST" ]]; then
    rm -rf "$TMPDIR_TEST"
  fi
}
trap cleanup EXIT

echo "== test_main_repo_git_guard.sh (t001: Bash 経由の主リポジトリ git 操作ガード) =="

# ---------------------------------------------------------------------------
# Setup: 合成の main repo ディレクトリ (実 crewvia repo には一切触れない)
# ---------------------------------------------------------------------------
TMPDIR_TEST="/tmp/crewvia-test-mrg-guard-$$"
FAKE_REPO="$TMPDIR_TEST/mainrepo"
FAKE_WORKTREE="$FAKE_REPO/.claude/worktrees/test-mission/t001-slug"
mkdir -p "$FAKE_REPO/scripts" "$FAKE_REPO/queue/missions/test-mission/tasks" \
  "$FAKE_REPO/queue/assignments" "$FAKE_REPO/registry" "$FAKE_WORKTREE/scripts" \
  "$FAKE_REPO/hooks"
# anchor が main checkout であることの自己確認 (.git がディレクトリ) を満たす
mkdir -p "$FAKE_REPO/.git"
echo "#!/usr/bin/env bash" > "$FAKE_REPO/scripts/plan.sh"
cat > "$FAKE_REPO/registry/workers.yaml" << 'EOF'
workers:
  - name: Haruto
    role: worker
  - name: SomeDirector
    role: director
EOF

# 実際の lib_main_repo_git_guard.py をコピー (hook が $_CREWVIA_REPO/hooks/... を
# 参照するため、合成 repo 側にも配置する必要がある)
cp "${OWN_CHECKOUT_ROOT}/hooks/lib_main_repo_git_guard.py" "$FAKE_REPO/hooks/lib_main_repo_git_guard.py"

# 実運用の TASK_ID 解決経路を模擬する assignments ファイル
printf 'test-mission:t001' > "$FAKE_REPO/queue/assignments/Haruto"

# _run_hook: env をクリアして CREWVIA_REPO / 追加 env を渡し、hook にペイロードを流す
_run_hook() {
  local input="$1"
  shift
  env -i \
    HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CREWVIA_REPO="$FAKE_REPO" \
    "$@" \
    bash "$HOOK" <<< "$input" 2>/tmp/test_mrg_guard_stderr
}

_bash_payload() {
  local cmd="$1"
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$cmd"
}

_decision_of() {
  echo "$1" | jq -r '.hookSpecificOutput.permissionDecision // "none"' 2>/dev/null || echo "none"
}

_assert_blocked() {
  local name="$1" exit_code="$2" stdout="$3"
  local decision
  decision="$(_decision_of "$stdout")"
  if [[ "$exit_code" -eq 0 ]] && [[ "$decision" == "deny" ]]; then
    pass "$name → deny"
  else
    fail "$name should be denied — exit=$exit_code decision=$decision stdout=$stdout"
  fi
}

# t011 のテストと同じ理由: CREWVIA_REPO が合成ディレクトリのため
# config/skill-permissions.yaml 等は存在せず、以降の skill/Taskvia チェックは
# 動作しない。「ブロックされない」は「このガードが deny を出していない」ことで
# 判定すれば十分。
_assert_not_blocked() {
  local name="$1" exit_code="$2" stdout="$3"
  local decision
  decision="$(_decision_of "$stdout")"
  if [[ "$exit_code" -eq 0 ]] && [[ "$decision" != "deny" ]]; then
    pass "$name → not blocked (exit=0, decision=$decision)"
  else
    fail "$name should not be blocked by the guard — exit=$exit_code decision=$decision stdout=$stdout"
  fi
}

_worker_env=(AGENT_NAME=Haruto CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash)

# ---------------------------------------------------------------------------
# Test 1-3: 実際の事故パターン → deny
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 1: cd \$CREWVIA_REPO_ROOT && git checkout -b (実事故の再現) ---"
STDOUT=$(_run_hook "$(_bash_payload 'cd $CREWVIA_REPO_ROOT && git fetch origin main && git checkout -b docs/x')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_blocked "cd \$CREWVIA_REPO_ROOT && git checkout -b" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 2: 主リポジトリの絶対パスを直接使った同様の操作 → deny ---"
STDOUT=$(_run_hook "$(_bash_payload "cd ${FAKE_REPO} && git checkout -b docs/x")" \
  "${_worker_env[@]}"); EXIT=$?
_assert_blocked "cd <main repo abs path> && git checkout -b" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 3a: git branch -D (\$CREWVIA_REPO_ROOT 参照) → deny ---"
STDOUT=$(_run_hook "$(_bash_payload 'cd $CREWVIA_REPO_ROOT && git branch -D old-branch')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_blocked "git branch -D on main repo" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 3b: git worktree add (\${CREWVIA_REPO_ROOT} braced 参照) → deny ---"
STDOUT=$(_run_hook "$(_bash_payload 'git -C ${CREWVIA_REPO_ROOT} worktree add /tmp/evil')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_blocked "git worktree add on main repo" "$EXIT" "$STDOUT"

# ---------------------------------------------------------------------------
# Test 4-7: 正当な経路はブロックされない
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 4: \$CREWVIA_REPO_ROOT 参照 + 読み取り系 git (status) → not blocked ---"
STDOUT=$(_run_hook "$(_bash_payload 'cd $CREWVIA_REPO_ROOT && git status')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "read-only git status on main repo ref" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 5: \$CREWVIA_REPO_ROOT/scripts/plan.sh 呼び出し (正当な用途) → not blocked ---"
STDOUT=$(_run_hook "$(_bash_payload '$CREWVIA_REPO_ROOT/scripts/plan.sh done t001 "result" --mission test-mission')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "plan.sh done call" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 6: worktree 内での通常の git 操作 (主リポジトリ参照なし) → not blocked (最重要) ---"
STDOUT=$(_run_hook "$(_bash_payload 'git checkout -b task/my-fix && git add -A && git commit -m "wip"')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "plain worktree git checkout -b / commit" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 7: worktree 絶対パス (主リポジトリパスを prefix に含む) へ cd してから checkout -b → not blocked ---"
STDOUT=$(_run_hook "$(_bash_payload "cd ${FAKE_WORKTREE} && git checkout -b task/my-fix")" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "cd into .claude/worktrees/... then checkout -b" "$EXIT" "$STDOUT"

# ---------------------------------------------------------------------------
# Test 8-10: 誤爆しないことの検証 (Director / TARGET_DIR / task 未解決)
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 8: Director が主リポジトリで危険な git 操作 (role bypass) → not blocked ---"
STDOUT=$(_run_hook "$(_bash_payload 'cd $CREWVIA_REPO_ROOT && git checkout -b docs/x')" \
  AGENT_NAME=SomeDirector); EXIT=$?
_assert_not_blocked "Director main repo git checkout -b" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 9: TARGET_DIR モードの Worker が同様の危険なコマンド → このガードでは not blocked ---"
STDOUT=$(_run_hook "$(_bash_payload 'cd $CREWVIA_REPO_ROOT && git checkout -b docs/x')" \
  TARGET_DIR=/tmp/some-target-project "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "TARGET_DIR mode main repo git checkout -b (guard is worktree-mode only)" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 10: TASK_ID 未解決 (ad-hoc セッション) → not blocked ---"
STDOUT=$(_run_hook "$(_bash_payload 'cd $CREWVIA_REPO_ROOT && git checkout -b docs/x')" \
  AGENT_NAME=AdHocDebugger CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash); EXIT=$?
_assert_not_blocked "no TASK_ID resolvable" "$EXIT" "$STDOUT"

# ---------------------------------------------------------------------------
# Test 11-12: クォートマスクの正当性 (自己言及の誤検知防止 + フェイルセーフ)
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 11: 事故を報告する plan.sh done の Result 文字列 (クォート内引用) → not blocked ---"
STDOUT=$(_run_hook "$(_bash_payload '$CREWVIA_REPO_ROOT/scripts/plan.sh done t001 "説明: cd $CREWVIA_REPO_ROOT && git checkout -b docs/x という事故がありました" --mission test-mission')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "quoted incident description mentioning the dangerous pattern" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 12: コマンド置換でクォート内に隠された危険操作 → フェイルセーフで deny ---"
STDOUT=$(_run_hook "$(_bash_payload 'echo "$(cd $CREWVIA_REPO_ROOT && git checkout -b evil)"')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_blocked "command substitution hiding the dangerous git op inside quotes" "$EXIT" "$STDOUT"

# ---------------------------------------------------------------------------
# Test 13 (t014 P2 と同じ踏襲): anchor が main checkout ではなく worktree の
# 場合、ガードそのものが安全に無効化される。
# ---------------------------------------------------------------------------
echo ""
if [[ -d "${OWN_CHECKOUT_ROOT}/.git" ]]; then
  echo "--- Test 13: skipped (このスクリプトは main checkout 上で実行されている — anchor が worktree に化けるケースを再現できない) ---"
else
  echo "--- Test 13: anchor が worktree ('${OWN_CHECKOUT_ROOT}') に化けてもガードが無効化される ---"
  STDOUT=$(env -i \
    HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CREWVIA_TASK_ID=t001 AGENT_NAME=NobodyWithNoAssignment CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
    bash "$HOOK" <<< "$(_bash_payload "cd ${OWN_CHECKOUT_ROOT} && git checkout -b docs/x")" 2>/tmp/test_mrg_guard_stderr
  ); EXIT=$?
  _assert_not_blocked "anchor-is-worktree Bash git op (guard self-disables via .git type check)" "$EXIT" "$STDOUT"
fi

# ---------------------------------------------------------------------------
# Test 14 (t005 [P1]): Seo (Opus 5) が本番同形 env で実測した誤爆 5 例 →
# not blocked。「参照」と「破壊的動詞」が同居していても、参照が main repo を
# 操作対象にしていなければブロックしない (worktree 内の正常作業 + 主リポジトリ
# の読み取り/呼び出しを 1 コマンドにまとめただけのケース)。
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 14: t005 [P1] Seo 実測の誤爆 5 例 → not blocked ---"

STDOUT=$(_run_hook "$(_bash_payload 'git add -A && git commit -m wip && $CREWVIA_REPO_ROOT/scripts/plan.sh done t004 "ok"')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "P1-1: commit (worktree) && plan.sh done (main repo call)" "$EXIT" "$STDOUT"

STDOUT=$(_run_hook "$(_bash_payload 'git commit -m "fix" && cat $CREWVIA_REPO_ROOT/knowledge/bash.md')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "P1-2: commit (worktree) && cat (main repo read-only)" "$EXIT" "$STDOUT"

STDOUT=$(_run_hook "$(_bash_payload $'git fetch origin main\ngit merge origin/main\ncat $CREWVIA_REPO_ROOT/knowledge/code.md')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "P1-3: multi-line fetch/merge (worktree) + cat (main repo read-only)" "$EXIT" "$STDOUT"

STDOUT=$(_run_hook "$(_bash_payload $'git status\ngrep -rn checkout $CREWVIA_REPO_ROOT/hooks')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "P1-4: multi-line status + grep (main repo, 完全に読み取り専用)" "$EXIT" "$STDOUT"

STDOUT=$(_run_hook "$(_bash_payload $'git status\nsed -n 1,50p $CREWVIA_REPO_ROOT/hooks/pre-tool-use.sh\ngit stash list')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "P1-5: multi-line status/sed(main repo 読み取り)/stash list(worktree)" "$EXIT" "$STDOUT"

# ---------------------------------------------------------------------------
# Test 15 (t005 [P1] 回帰): QA (t003) が確認した検出 14 ケースがすべて deny の
# ままであること (検出力の退行がないこと)。すべて `cd <ref> &&` 形か
# `git -C <ref>` 形 (Test 1-3b で既に一部カバー済みだが、ここで 14 件全量を
# 通しで再確認する)。
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 15: QA 検出 14 ケースの回帰 (すべて deny) ---"

_DETECTION_14=(
  'cd $CREWVIA_REPO_ROOT && git fetch origin main && git checkout -b docs/codex-reviewer-phase3-docs'
  'cd $CREWVIA_REPO_ROOT && git checkout main'
  'cd $CREWVIA_REPO_ROOT && git switch main'
  'cd $CREWVIA_REPO_ROOT && git reset --hard origin/main'
  'cd ${CREWVIA_REPO_ROOT} && git rebase origin/main'
  'cd $CREWVIA_REPO && git merge origin/main'
  'cd $CREWVIA_REPO_ROOT && git commit -am "oops"'
  'cd $CREWVIA_REPO_ROOT && git clean -fd'
  'cd $CREWVIA_REPO_ROOT && git stash push -u'
  'git -C $CREWVIA_REPO_ROOT branch -D task/foo'
  'git -C $CREWVIA_REPO_ROOT worktree add /tmp/x main'
)
for cmd in "${_DETECTION_14[@]}"; do
  STDOUT=$(_run_hook "$(_bash_payload "$cmd")" "${_worker_env[@]}"); EXIT=$?
  _assert_blocked "detection-14 (\$CREWVIA_REPO_ROOT form): $cmd" "$EXIT" "$STDOUT"
done

_DETECTION_14_ABS=(
  "cd ${FAKE_REPO} && git checkout -b docs/xxx"
  "git -C ${FAKE_REPO} switch main"
  "cd ${FAKE_REPO} && git reset --hard HEAD~3"
)
for cmd in "${_DETECTION_14_ABS[@]}"; do
  STDOUT=$(_run_hook "$(_bash_payload "$cmd")" "${_worker_env[@]}"); EXIT=$?
  _assert_blocked "detection-14 (絶対パス形): $cmd" "$EXIT" "$STDOUT"
done

# ---------------------------------------------------------------------------
# Test 16 (t005 [P2]): 事故コマンドをシングルクォートで引用しただけの報告
# コマンドは not blocked。一方、クォート外 (裸の位置 / ダブルクォート内) の
# 本物のコマンド置換は引き続き deny (フェイルセーフ維持)。
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 16: t005 [P2] シングルクォート引用の報告コマンド → not blocked ---"

STDOUT=$(_run_hook "$(_bash_payload "\$CREWVIA_REPO_ROOT/scripts/plan.sh done t003 'バッククォート引用: \`cd \$CREWVIA_REPO_ROOT && git checkout -b docs/x\` が deny されることを確認した'")" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "P2: single-quoted incident report containing backticks" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 16b: ダブルクォート内のバッククォート (実際に展開される) → 引き続き deny ---"
STDOUT=$(_run_hook "$(_bash_payload 'echo "$(cd $CREWVIA_REPO_ROOT && git checkout -b evil)"')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_blocked "P2 regression: command substitution inside double quotes still executes" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 16c: 裸のバッククォート (シングルクォート外) → 引き続き deny ---"
STDOUT=$(_run_hook "$(_bash_payload 'echo `cd $CREWVIA_REPO_ROOT && git checkout -b evil`')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_blocked "P2 regression: bare backtick command substitution outside quotes" "$EXIT" "$STDOUT"

# ---------------------------------------------------------------------------
# Test 17 (t005): 複数行スクリプトのケース (修正前はゼロだった)。正当な
# 複数行操作 (worktree 内のみ) はブロックせず、複数行にまたがる本物の事故
# パターン (同一行内に参照+動詞が同居) は引き続き検出する。
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 17a: 複数行の正当な worktree 操作 (主リポジトリ参照なし) → not blocked ---"
STDOUT=$(_run_hook "$(_bash_payload $'git fetch origin main\ngit merge origin/main\ngit push')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_not_blocked "multi-line worktree fetch/merge/push (no main repo reference)" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 17b: 複数行で、危険な行そのものは単独でも main repo 参照+動詞が同居 → deny ---"
STDOUT=$(_run_hook "$(_bash_payload $'git status\ncd $CREWVIA_REPO_ROOT && git checkout -b docs/x\ngit log')" \
  "${_worker_env[@]}"); EXIT=$?
_assert_blocked "multi-line: one line itself has reference+verb co-located" "$EXIT" "$STDOUT"

echo ""
echo "================================"
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
