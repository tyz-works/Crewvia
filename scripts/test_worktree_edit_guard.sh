#!/usr/bin/env bash
# test_worktree_edit_guard.sh — t011 回帰テスト (t014 で組み替え)
#
# 不具合: t009 で、worktree モードで作業中の Worker (Sofia) が Edit ツールに
# main repo ($CREWVIA_REPO, branch=main) の絶対パスを渡してしまい、専用 worktree
# ではなく main checkout を直接編集する事故が発生した。commit 前に git status で
# 気づき自己復旧したため実害は無かったが、気づかなければ main に混入していた。
#
# 根本原因: agents/worker.md には TARGET_DIR モード用の「作業スコープの制約」しか
# 無く、worktree モード（デフォルト）には同等の制約が無かった。ドキュメントだけでは
# 再発を防げないため、hooks/pre-tool-use.sh に構造的なガードを追加した (t011)。
#
# 修正 (hooks/pre-tool-use.sh):
#   worktree を持つ Worker (TASK_ID が解決済みで、TARGET_DIR が未設定) の
#   Edit/Write/MultiEdit/NotebookEdit が $CREWVIA_REPO 配下 (queue/ registry/
#   .claude/worktrees/ を除く) を対象にしたら emit_decision "deny" で拒否する。
#   skill 設定や urgent 例外の前段（_global.deny よりも前）に置き、バイパスできない
#   絶対安全弁にしてある。
#
# t014 での組み替え (Seo/Opus 5 レビュー指摘):
#   [P1] 旧テストの deny 系 (Test 1/2/2b) は CREWVIA_TASK_ID を明示的に env で渡して
#   いたが、実運用でその env が hook プロセスに届くことはない (Bash tool subshell の
#   export は hook の親プロセスに継承されない)。実運用で TASK_ID が解決される唯一の
#   経路は queue/assignments/<agent> ファイルの fallback であり、CREWVIA_TASK_ID を
#   明示付与する旧テストは「テストと同じ env でしか発火しない dead code」を検出でき
#   ていなかった。deny 系は CREWVIA_TASK_ID を渡さず assignments ファイルだけを置いた
#   状態に組み替えた。旧 Test 8 (CREWVIA_TASK_ID 未設定 → not blocked) はこの組み替え
#   後は「env も assignments ファイルも無い純粋な ad-hoc セッション」の意味になる
#   (Test 8 として維持)。
#   [P2] anchor ($CREWVIA_REPO) が worktree を指した場合にガードが安全に無効化される
#   ことを検証する Test 10 を追加した。実際の t011 worktree (linked worktree なので
#   .git はファイル) を anchor として使い、修正前なら誤って deny されていたはずの
#   ケースを再現する。
#
# このテストで検証:
#   1. worktree Worker (assignments ファイル経由で TASK_ID 解決、CREWVIA_TASK_ID は
#      未設定 = 実運用と同じ env) が main checkout 直下のファイルを Edit → deny
#   2. 同じ状況で Write → deny
#   2b. 同じ状況で MultiEdit → deny (commit security review で検出した
#       control-bypass の回帰テスト。Edit/Write だけを見るガードは MultiEdit
#       という別名の同格ツール経由で素通りしていた)
#   3. 同じ Worker が自分の worktree 内のファイルを Edit → ブロックされない
#   4. 同じ Worker が queue/ 配下を Write → ブロックされない (plan.sh 経由の正当な書き込みを模擬)
#   5. 同じ Worker が registry/ 配下を Write → ブロックされない
#   6. Director (role: director) は main checkout 直下を Edit してもブロックされない
#   7. TARGET_DIR モードの Worker は main checkout 直下を Edit してもこのガードでは
#      ブロックされない (誤爆しないことの検証 — TARGET_DIR モードは対象外)
#   8. TASK_ID 未解決 (env も assignments ファイルも無い、純粋な ad-hoc セッション) の
#      場合はブロックされない
#   9. $CREWVIA_REPO の外にあるファイルへの Edit はブロックされない
#   10. anchor ($CREWVIA_REPO 相当) が main checkout ではなく worktree を指す場合、
#       ガードそのものが安全に無効化される (誤爆しない) — 実際の t011 worktree
#       (linked worktree, .git はファイル) を anchor に使い、その worktree 内の
#       通常ファイルへの Edit がこのガードで deny されないことを検証する
#   11. NotebookEdit も Edit/Write/MultiEdit と同様に main checkout 直下を
#       notebook_path で対象にしたら deny される (file_path ではなく
#       notebook_path を持つツールが二重に漏れていた control-bypass の回帰テスト)
#
# 「ブロックされない」判定について: CREWVIA_REPO をテスト用の合成ディレクトリに
# 差し替えているため、そこには実 config/skill-permissions.yaml や
# hooks/lib_skill_perms.py が存在せず、skill ベースの許可判定 (実運用では
# "allow" を明示的に返す) は本テストでは動作しない。そのため「ブロックされない」
# ケースは decision=="allow" ではなく exit=0 かつ decision!="deny" で判定する
# (このガード自身が deny を出していないことだけを検証すればよく、その先の
# skill-permissions / Taskvia 判定は本テストのスコープ外)。
#
# 実行: bash scripts/test_worktree_edit_guard.sh
# 副作用: /tmp 配下に合成の「main repo」ディレクトリを作成し終了時に削除する
#         (crewvia の実 repo / registry / queue には一切触れない。git worktree も
#         実際には作らず、ディレクトリ構造だけで worktree パスを模擬する — ガードの
#         実装は $CREWVIA_REPO からの相対パス判定のみで動作し git には依存しないため。
#         ただし Test 10 だけは P2 (anchor が worktree の場合の自己無効化) を検証する
#         ため、このスクリプト自身が置かれている実際の checkout を anchor に使う —
#         それが main checkout なら .git はディレクトリなので Test 10 は無意味になる
#         ため、その場合は skip する。この checkout が worktree の場合、Test 10 は
#         emit_decision の副作用として実際の registry/approvals/approvals.tsv に
#         1行追記する — これは gitignore 対象であり、通常の Worker 稼働時に毎回
#         書き込まれるのと同じ経路なので無害)

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

echo "== test_worktree_edit_guard.sh (t011: main checkout 直接編集ガード) =="

# ---------------------------------------------------------------------------
# Setup: 合成の main repo ディレクトリ (実 crewvia repo には一切触れない)
# ---------------------------------------------------------------------------
TMPDIR_TEST="/tmp/crewvia-test-guard-$$"
FAKE_REPO="$TMPDIR_TEST/mainrepo"
FAKE_WORKTREE="$FAKE_REPO/.claude/worktrees/test-mission/t001-slug"
mkdir -p "$FAKE_REPO/scripts" "$FAKE_REPO/queue/missions/test-mission/tasks" \
  "$FAKE_REPO/queue/assignments" "$FAKE_REPO/registry" "$FAKE_WORKTREE/scripts"
# t014 (P2 fix): ガードは anchor の .git がディレクトリであることを main checkout の
# 証拠として要求する。FAKE_REPO は「main checkout」を模擬しているので、実際の git
# init はせず (git worktree も実際には作らない、というこのテストの方針を踏襲)、
# ディレクトリだけを合成して .git = ディレクトリという条件を満たす。
mkdir -p "$FAKE_REPO/.git"
echo "#!/usr/bin/env bash" > "$FAKE_REPO/scripts/plan.sh"
echo "#!/usr/bin/env bash" > "$FAKE_WORKTREE/scripts/plan.sh"
cat > "$FAKE_REPO/registry/workers.yaml" << 'EOF'
workers:
  - name: Sofia
    role: worker
  - name: SomeDirector
    role: director
EOF

# 実運用の TASK_ID 解決経路を模擬する assignments ファイル。
# hooks/pre-tool-use.sh:127-140 はここから "<mission_slug>:<task_id>" を読み取り、
# env の CREWVIA_TASK_ID/TASK_ID が無くても TASK_ID を解決する。
printf 'test-mission:t001' > "$FAKE_REPO/queue/assignments/Sofia"

# _run_hook: env をクリアして CREWVIA_REPO / 追加 env を渡し、hook にペイロードを流す
# (scripts/test_hooks.sh の _run_hook と同じ流儀)
_run_hook() {
  local input="$1"
  shift
  env -i \
    HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CREWVIA_REPO="$FAKE_REPO" \
    "$@" \
    bash "$HOOK" <<< "$input" 2>/tmp/test_worktree_guard_stderr
}

_edit_payload() {
  local path="$1"
  printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$path"
}
_write_payload() {
  local path="$1"
  printf '{"tool_name":"Write","tool_input":{"file_path":"%s"}}' "$path"
}
_multiedit_payload() {
  local path="$1"
  printf '{"tool_name":"MultiEdit","tool_input":{"file_path":"%s","edits":[{"old_string":"a","new_string":"b"}]}}' "$path"
}
_notebookedit_payload() {
  local path="$1"
  printf '{"tool_name":"NotebookEdit","tool_input":{"notebook_path":"%s","new_source":"x"}}' "$path"
}

_decision_of() {
  echo "$1" | jq -r '.hookSpecificOutput.permissionDecision // "none"' 2>/dev/null || echo "none"
}

# ブロックされる (deny) ことを検証する
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

# ブロックされない (このガードが deny を出さない) ことを検証する。
# 上のコメント参照: CREWVIA_REPO が合成ディレクトリのため skill-permissions は
# 動作せず "allow" 明示は出ない。exit=0 かつ decision!=deny であれば
# 「このガードには捕まっていない」ことの証明として十分。
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

# ---------------------------------------------------------------------------
# Test 1-2: worktree Worker が main checkout 直下を Edit/Write → deny
#
# t014: CREWVIA_TASK_ID は意図的に渡さない。実運用ではこの env は hook プロセスに
# 届かないため、それでも deny されることを示すのが本テストの核心。TASK_ID は
# AGENT_NAME=Sofia + queue/assignments/Sofia ファイル (setup 済み) からのみ解決される。
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 1: worktree Worker edits main checkout scripts/plan.sh (実運用 env: assignments ファイルのみ) ---"
STDOUT=$(_run_hook "$(_edit_payload "${FAKE_REPO}/scripts/plan.sh")" \
  AGENT_NAME=Sofia CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_blocked "main checkout Edit" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 2: worktree Worker writes main checkout scripts/plan.sh (実運用 env: assignments ファイルのみ) ---"
STDOUT=$(_run_hook "$(_write_payload "${FAKE_REPO}/scripts/plan.sh")" \
  AGENT_NAME=Sofia CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_blocked "main checkout Write" "$EXIT" "$STDOUT"

# commit security review で検出された control-bypass の回帰テスト:
# Edit/Write だけを見るガードは MultiEdit 経由で素通しになっていた。
# config/skill-permissions.yaml は全 skill で Edit/Write/MultiEdit を常に
# 三点セットで許可しており、ファイル書き込みという意味では Edit と全く同じ
# 権限を持つ別名ツールのため、同様に deny されなければならない。
echo ""
echo "--- Test 2b (security review fix): worktree Worker MultiEdits main checkout scripts/plan.sh (実運用 env) ---"
STDOUT=$(_run_hook "$(_multiedit_payload "${FAKE_REPO}/scripts/plan.sh")" \
  AGENT_NAME=Sofia CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_blocked "main checkout MultiEdit" "$EXIT" "$STDOUT"

# t014 (Seo/Opus 5 レビュー指摘): NotebookEdit は file_path ではなく notebook_path を
# 持つため、MultiEdit と全く同じ理由で二重に漏れていた control-bypass。
echo ""
echo "--- Test 11 (t014 fix): worktree Worker NotebookEdits main checkout notebook.ipynb (実運用 env) ---"
STDOUT=$(_run_hook "$(_notebookedit_payload "${FAKE_REPO}/scripts/notebook.ipynb")" \
  AGENT_NAME=Sofia CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_blocked "main checkout NotebookEdit" "$EXIT" "$STDOUT"

# ---------------------------------------------------------------------------
# Test 3-5: 正当な経路はブロックされない
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 3: same Worker edits own worktree file ---"
STDOUT=$(_run_hook "$(_edit_payload "${FAKE_WORKTREE}/scripts/plan.sh")" \
  CREWVIA_TASK_ID=t001 AGENT_NAME=Sofia CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_not_blocked "own worktree Edit" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 4: same Worker writes queue/ (plan.sh 経由を模擬) ---"
STDOUT=$(_run_hook "$(_write_payload "${FAKE_REPO}/queue/missions/test-mission/tasks/t001.md")" \
  CREWVIA_TASK_ID=t001 AGENT_NAME=Sofia CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_not_blocked "queue/ Write" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 5: same Worker writes registry/ ---"
STDOUT=$(_run_hook "$(_write_payload "${FAKE_REPO}/registry/workers.yaml")" \
  CREWVIA_TASK_ID=t001 AGENT_NAME=Sofia CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_not_blocked "registry/ Write" "$EXIT" "$STDOUT"

# ---------------------------------------------------------------------------
# Test 6-9: 誤爆しないことの検証 (Director / TARGET_DIR / task 未設定 / repo 外)
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 6: Director editing main checkout directly (role bypass) ---"
STDOUT=$(_run_hook "$(_edit_payload "${FAKE_REPO}/scripts/plan.sh")" \
  CREWVIA_TASK_ID=t001 AGENT_NAME=SomeDirector \
  ); EXIT=$?
_assert_not_blocked "Director main checkout Edit" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 7: TARGET_DIR mode Worker editing main checkout ---"
STDOUT=$(_run_hook "$(_edit_payload "${FAKE_REPO}/scripts/plan.sh")" \
  CREWVIA_TASK_ID=t002 TARGET_DIR="/tmp/some-target-project" AGENT_NAME=Sofia \
  CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_not_blocked "TARGET_DIR mode Edit of main checkout (guard is worktree-mode only)" "$EXIT" "$STDOUT"

# t014: AGENT_NAME をわざと Sofia 以外にする — Sofia には queue/assignments/Sofia
# (setup 済み) があるため、Sofia のままだと TASK_ID が assignments 経由で解決されて
# しまい「TASK_ID が本当に未解決」というこのテストの前提が壊れる。
echo ""
echo "--- Test 8: no TASK_ID resolvable at all (env も assignments ファイルも無い純粋な ad-hoc セッション) ---"
STDOUT=$(_run_hook "$(_edit_payload "${FAKE_REPO}/scripts/plan.sh")" \
  AGENT_NAME=AdHocDebugger CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_not_blocked "no TASK_ID resolvable Edit (guard requires an active worktree task)" "$EXIT" "$STDOUT"

echo ""
echo "--- Test 9: file outside \$CREWVIA_REPO entirely ---"
STDOUT=$(_run_hook "$(_edit_payload "/tmp/totally-unrelated-file.txt")" \
  CREWVIA_TASK_ID=t001 AGENT_NAME=Sofia CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
  ); EXIT=$?
_assert_not_blocked "file outside \$CREWVIA_REPO Edit" "$EXIT" "$STDOUT"

# ---------------------------------------------------------------------------
# Test 10 (t014 P2 fix): anchor が main checkout ではなく worktree を指した場合、
# ガード自体が安全に無効化されることの検証。
#
# CREWVIA_REPO を渡さず、hook 自身の fallback (:77, dirname(BASH_SOURCE)/..) に
# 解決させる。このテストスクリプトが置かれている checkout (OWN_CHECKOUT_ROOT) が
# たまたま main checkout そのものだと anchor = main になり P2 は再現できないため、
# その場合はこの Test を明示的に skip する (通常の開発フローでは crewvia 自身を
# main checkout から直接編集することは無いはずだが、CI 環境等で main checkout
# 上でこのテストが走る可能性を考慮した安全策)。
# ---------------------------------------------------------------------------
echo ""
if [[ -d "${OWN_CHECKOUT_ROOT}/.git" ]]; then
  echo "--- Test 10 (t014 P2 fix): skipped (このスクリプトは main checkout 上で実行されている — anchor が worktree に化けるケースを再現できない) ---"
else
  echo "--- Test 10 (t014 P2 fix): anchor が worktree ('${OWN_CHECKOUT_ROOT}') に化けてもガードが無効化される ---"
  # CREWVIA_REPO を渡さないことで、hook の fallback にこの worktree 自身の
  # 場所 (OWN_CHECKOUT_ROOT) を anchor として解決させる。修正前ならここで
  # README.md のような通常ファイルへの Edit も (queue/registry/.claude/worktrees/
  # のどれにも該当しないため) 誤って deny されていたはずである。
  STDOUT=$(env -i \
    HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CREWVIA_TASK_ID=t001 AGENT_NAME=NobodyWithNoAssignment CREWVIA_TASKVIA=disabled TASKVIA_TOKEN="" SKILLS=bash \
    bash "$HOOK" <<< "$(_edit_payload "${OWN_CHECKOUT_ROOT}/README.md")" 2>/tmp/test_worktree_guard_stderr
  ); EXIT=$?
  _assert_not_blocked "anchor-is-worktree Edit (guard self-disables via .git type check)" "$EXIT" "$STDOUT"
fi

echo ""
echo "================================"
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
