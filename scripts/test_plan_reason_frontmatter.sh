#!/usr/bin/env bash
# test_plan_reason_frontmatter.sh — t009 回帰テスト
#
# 不具合: `plan.sh needs-director <task_id> "<複数行の理由>"` を実行すると、
# 改行を含む文字列がそのまま quoted scalar として frontmatter に書き込まれ
# (`_dump_scalar` が改行をエスケープしていなかった)、書き戻された tNNN.md の
# frontmatter が壊れる。壊れた frontmatter は `parse_yaml` が
# `malformed line N: '=== 環境 ==='` のような例外を投げて `plan.sh status` /
# `plan.sh pull` が丸ごと死ぬ (`list_tasks` が `die()` していたため)。
# dispatcher も同ファイルを読めなくなり、ミッション全体が停止する
# (mission 20260908-codex-reviewer-phase3 の t003 で実機再現、約20分停止)。
#
# 修正 (scripts/plan.sh):
#   1. 書き込み側で防ぐ: `_dump_scalar` が改行を含む値を書く前に
#      改行を " / " に畳んで1行に正規化する (frontmatter の全経路が通る
#      唯一の関数なので、needs_director_reason に限らず title 等も守られる)。
#   2. 長文は本文へ: `split_long_freeform()` を追加し、
#      `cmd_needs_director` は 200文字/改行ありの reason を要約1行に縮めて
#      frontmatter に書き、全文は body の "## Needs-Director 詳細" に退避する。
#   3. 読み込み側の堅牢化: `list_tasks` は1ファイルのパース失敗で die() せず、
#      status='corrupted' ([破損]) のプレースホルダに差し替えて処理を続行する。
#      pull/dispatch は 'pending' のみを対象にするため自動的に除外される。
#
# このテストで検証:
#   1. 複数行 reason で needs-director → 例外なく完了し、frontmatter の
#      needs_director_reason が改行を含まない1行になっている
#   2. 全文が body の "## Needs-Director 詳細" セクションに保存されている
#   3. 上記の後、plan.sh status --mission が例外なく動作し「要Director」と
#      表示される（[破損] にならない = frontmatter が壊れていない証拠）
#   4. 複数行 result で done を実行 → 従来通り正常動作（body 経由なので
#      元から安全だが、回帰確認として残す）
#   5. 意図的に壊した tNNN.md が1つあっても、plan.sh status --mission は
#      例外にならず「[破損]」表示 + 他タスクは通常表示を継続する
#   6. 同じ状況で plan.sh pull は死なずに他の pending task を選ぶ
#
# 実行: bash scripts/test_plan_reason_frontmatter.sh
# 副作用: /tmp 配下に一時 queue を作成し終了時に削除する
#         (crewvia の実データ = queue/ には一切触れない)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLAN_SH="$OWN_CHECKOUT_ROOT/scripts/plan.sh"

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

echo "== test_plan_reason_frontmatter.sh (t009: multi-line reason corrupts frontmatter) =="

# ---------------------------------------------------------------------------
# Setup: 一時 queue ディレクトリを構築 (実データには一切触れない)
# ---------------------------------------------------------------------------
TMPDIR_TEST="/tmp/crewvia-test-reason-$$"
QUEUE="$TMPDIR_TEST/queue"
MISSION_SLUG="test-reason-mission"
TASKS_DIR="$QUEUE/missions/$MISSION_SLUG/tasks"
mkdir -p "$TASKS_DIR" "$QUEUE/archive"

printf 'active_missions:\n  - %s\ndefault_mission: %s\n' \
  "$MISSION_SLUG" "$MISSION_SLUG" > "$QUEUE/state.yaml"

printf 'title: Test Mission\nslug: %s\nstatus: in_progress\ncreated_at: 2026-09-08T00:00:00Z\ncompleted_at: null\nnext_task_id: 5\n' \
  "$MISSION_SLUG" > "$QUEUE/missions/$MISSION_SLUG/mission.yaml"

# t001: in_progress — needs-director 実行対象
printf -- '---\nid: t001\ntitle: Task for needs-director\nskills: [bash]\npriority: medium\nstatus: in_progress\nblocked_by: []\nworker: sofia\nstarted_at: 2026-09-08T00:00:00Z\ncompleted_at: null\n---\n\n## Description\nDo the thing.\n\n## Result\n' \
  > "$TASKS_DIR/t001.md"

# t002: in_progress — done 実行対象
printf -- '---\nid: t002\ntitle: Task for done\nskills: [bash]\npriority: medium\nstatus: in_progress\nblocked_by: []\nworker: sofia\nstarted_at: 2026-09-08T00:00:00Z\ncompleted_at: null\n---\n\n## Description\nDo another thing.\n\n## Result\n' \
  > "$TASKS_DIR/t002.md"

# t003: 意図的に壊れた frontmatter (実インシデントの再現: `=== 環境 ===` のような
# key: value でも block list item でもない行)
printf -- '---\nid: t003\ntitle: Corrupted task\nskills: [bash]\npriority: medium\nstatus: needs_director\nblocked_by: []\nworker: sofia\nstarted_at: 2026-09-08T00:00:00Z\ncompleted_at: null\nneeds_director_reason: "broken\n=== 環境 ===\nsomething"\n---\n\n## Description\nCorrupted.\n\n## Result\n' \
  > "$TASKS_DIR/t003.md"

# t004: 健全な pending task — corruption isolation の対照
printf -- '---\nid: t004\ntitle: Healthy pending task\nskills: [bash]\npriority: medium\nstatus: pending\nblocked_by: []\nworker: null\nstarted_at: null\ncompleted_at: null\n---\n\n## Description\nHealthy.\n\n## Result\n' \
  > "$TASKS_DIR/t004.md"

run_plan_stdout() {
  CREWVIA_QUEUE="$QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
  bash "$PLAN_SH" "$@" 2>/dev/null
}
run_plan_all() {
  CREWVIA_QUEUE="$QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
  bash "$PLAN_SH" "$@" 2>&1 || true
}
run_plan_rc() {
  CREWVIA_QUEUE="$QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
  bash "$PLAN_SH" "$@" > /dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Test 1-3: 複数行 reason で needs-director
# ---------------------------------------------------------------------------
MULTILINE_REASON="=== 環境 ===
WSL, memory 6GB
=== 症状 ===
plan.sh status crashed"

echo ""
echo "--- Test 1: needs-director with multi-line reason exits 0 ---"
if CREWVIA_QUEUE="$QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
   bash "$PLAN_SH" needs-director t001 "$MULTILINE_REASON" --mission "$MISSION_SLUG" > /dev/null 2>&1; then
  pass "needs-director with multi-line reason exits 0"
else
  fail "needs-director with multi-line reason should exit 0"
fi

echo ""
echo "--- Test 2: t001.md frontmatter block contains no raw embedded newline in a value ---"
# frontmatter block = between the first and second '---' line
FRONTMATTER=$(awk '/^---$/{c++; if (c==2) exit} c==1 && NR>1' "$TASKS_DIR/t001.md")
# Every non-empty frontmatter line must match `key: value` or `  - item` —
# a line like '=== 環境 ===' (no colon, not a list item) proves the bug is back.
BROKEN_LINE=$(echo "$FRONTMATTER" | grep -Ev '^[A-Za-z_][A-Za-z0-9_-]*:.*$|^  -.*$|^$' || true)
if [[ -z "$BROKEN_LINE" ]]; then
  pass "t001.md frontmatter has no malformed line"
else
  fail "t001.md frontmatter contains a malformed line: $BROKEN_LINE"
fi

echo ""
echo "--- Test 3: needs_director_reason line has no literal newline (single physical line) ---"
REASON_LINE=$(echo "$FRONTMATTER" | grep '^needs_director_reason:')
REASON_LINE_COUNT=$(echo "$FRONTMATTER" | grep -c '^needs_director_reason:')
if [[ "$REASON_LINE_COUNT" -eq 1 && -n "$REASON_LINE" ]]; then
  pass "needs_director_reason is exactly one physical line: $REASON_LINE"
else
  fail "needs_director_reason should be exactly one physical line (got count=$REASON_LINE_COUNT)"
fi

echo ""
echo "--- Test 4: full reason text preserved in body (## Needs-Director 詳細) ---"
if grep -q '## Needs-Director 詳細' "$TASKS_DIR/t001.md" \
   && grep -q '=== 環境 ===' "$TASKS_DIR/t001.md" \
   && grep -q 'plan.sh status crashed' "$TASKS_DIR/t001.md"; then
  pass "full multi-line reason text preserved in task body"
else
  fail "full multi-line reason text should be preserved in task body"
fi

echo ""
echo "--- Test 5: plan.sh status --mission works after needs-director (no crash, shows 要Director not 破損) ---"
status_out=$(run_plan_all status --mission "$MISSION_SLUG")
if echo "$status_out" | grep -q 't001' && echo "$status_out" | grep -q '要Director' \
   && ! echo "$status_out" | grep -q '破損.*t001'; then
  pass "status shows t001 as 要Director (frontmatter intact, not corrupted)"
else
  fail "status should show t001 as 要Director without corruption — got: $status_out"
fi

# ---------------------------------------------------------------------------
# Test 6: 複数行 result で done (body 経由なので元から安全 — 回帰確認)
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 6: done with multi-line result exits 0 and status stays healthy ---"
MULTILINE_RESULT="Step 1: did X
Step 2: did Y
Step 3: verified Z"
if run_plan_rc done t002 "$MULTILINE_RESULT" --mission "$MISSION_SLUG"; then
  pass "done with multi-line result exits 0"
else
  fail "done with multi-line result should exit 0"
fi
if grep -q 'Step 2: did Y' "$TASKS_DIR/t002.md"; then
  pass "multi-line result preserved verbatim in body"
else
  fail "multi-line result should be preserved verbatim in body"
fi
status_out2=$(run_plan_all status --mission "$MISSION_SLUG")
if echo "$status_out2" | grep -q 't002' && ! echo "$status_out2" | grep -q '破損.*t002'; then
  pass "status still healthy for t002 after done"
else
  fail "status should stay healthy for t002 after done — got: $status_out2"
fi

# ---------------------------------------------------------------------------
# Test 7-8: t003 (元から壊れているファイル) が他タスクを巻き込まないこと
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 7: plan.sh status --mission survives a pre-corrupted task file (t003) ---"
status_out3=$(run_plan_all status --mission "$MISSION_SLUG")
rc3=0
run_plan_rc status --mission "$MISSION_SLUG" || rc3=$?
if [[ "$rc3" -eq 0 ]] && echo "$status_out3" | grep -q '破損' && echo "$status_out3" | grep -q 't004'; then
  pass "status survives corrupted t003 (exit 0, shows 破損 for t003, still lists t004)"
else
  fail "status should survive corrupted t003 and keep listing t004 — rc=$rc3 out=$status_out3"
fi

echo ""
echo "--- Test 8: plan.sh pull still finds the healthy pending task (t004) despite corrupted t003 ---"
pull_out=$(run_plan_stdout pull --mission "$MISSION_SLUG" --skills bash) && rc_pull=0 || rc_pull=$?
picked=$(echo "$pull_out" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','?'))" 2>/dev/null || echo "?")
if [[ "$rc_pull" -eq 0 ]] && [[ "$picked" == "t004" ]]; then
  pass "pull picks the healthy pending task t004 despite corrupted t003"
else
  fail "pull should pick t004 despite corrupted t003 — rc=$rc_pull picked=$picked out=$pull_out"
fi

echo ""
echo "================================"
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
