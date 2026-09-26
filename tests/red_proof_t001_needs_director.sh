#!/usr/bin/env bash
# mission 20260926-mechanize-guards-a / t001 (backlog #13) の回帰テストが
# 「欠陥を戻すと赤くなる」ことの実証。
#
# 使い方:
#     bash tests/red_proof_t001_needs_director.sh
#
# 使い捨てのコピーに 1 つずつ欠陥を注入し、見張るはずのテストが赤 (= 失敗) になることを
# 確かめる。緑のままなら、そのテストは欠陥の留め金になっていない
# (memory: regression-test-must-prove-red / red-proof-catches-tests-green-for-the-wrong-reason)。
#
#   M0  修正前の plan.sh + dispatcher.sh (99f3617) 一式    → (a)(b)(c)(d) が全部赤
#   M1  needs-director が assignment を外さない              → (a) が赤 ((d) は dispatcher が孤児を塞がないので緑のまま — 2 層)
#   M2a Rule 2 が in_progress しか「仕事」と数えない          → 「手放していない card の Worker は退役しない」が赤
#   M2b 判断待ちの Worker を busy と読まない                  → 「新しい task を渡さない」が赤
#   M3  Kai-codex の assignment を有無で判定する (孤児を区別しない) → (c) の孤児が赤 ((d) は plan.sh が外すので緑のまま)
#   M3b 孤児の判定が甘い (証明できないものも孤児にする)        → 「孤児と証明できないものは塞ぐ」が赤
#   M4  Rule 5 が card を見ない                               → 「Rule 5 は二重通知しない」が赤
#   M6  task-graph が assignment の不在を許さない            → needs_director の pane_match が消える回帰が赤 (M6b/M6c は許す範囲)
#   M5  手放した status の集合が failed / cancelled を欠く      → 「手放した Worker は退役」「孤児」が赤
#
# 隔離: 本番の checkout には触らない (使い捨てのコピーの中だけで変異させる)。テスト自体も
# queue / registry を tmpdir に置き mux をフェイクにする。pytest は herdr の無い `env -i` で走らせる。
# $PYTHONDONTWRITEBYTECODE=1: 欠陥注入は .pyc を通して古い姿を拾わせない
# (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t001-nd.XXXXXX")"
export PYTHONDONTWRITEBYTECODE=1
PRE_FIX_COMMIT="99f3617"

cleanup() { chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null; }
trap cleanup EXIT

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  OK:   $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  BAD:  $1"; }

fresh_copy() {
  rm -rf "$WORK/tree"; mkdir -p "$WORK/tree"
  rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
        --exclude='node_modules' --exclude='logs' \
        "$REPO_ROOT/scripts" "$REPO_ROOT/tests" "$REPO_ROOT/config" "$WORK/tree/"
}

# mutate <file> <old> <new> — 置換できなかったら注入失敗として止まる。
mutate() {
  python3 - "$WORK/tree/$1" "$2" "$3" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path).read()
if old not in s:
    print(f"INJECTION FAILED: pattern not found in {path}: {old[:70]!r}", file=sys.stderr)
    sys.exit(3)
open(path, "w").write(s.replace(old, new, 1))
PYEOF
}

run_pytest() {
  (cd "$WORK/tree" && env -i PATH="$PATH" HOME="$HOME" PYTHONUSERBASE="$HOME/.local" \
     PYTHONDONTWRITEBYTECODE=1 \
     python3 -m pytest tests/test_needs_director_releases_assignment.py \
       -q --no-header -p no:cacheprovider 2>&1)
}

run_pytest_graph() {
  (cd "$WORK/tree" && env -i PATH="$PATH" HOME="$HOME" PYTHONUSERBASE="$HOME/.local" \
     PYTHONDONTWRITEBYTECODE=1 \
     python3 -m pytest tests/test_task_graph.py -q --no-header -p no:cacheprovider 2>&1)
}

expect_red() {   # <label> <output> <substring of a test name that must fail>
  local label="$1" out="$2" name="$3"
  if echo "$out" | grep -q "^FAILED .*${name}"; then
    ok "$label → ${name} が赤になった"
  else
    bad "$label → ${name} が赤にならなかった (欠陥の留め金になっていない)"
    echo "$out" | tail -5
  fi
}

echo "== baseline: 変異なしで緑"
fresh_copy
out="$(run_pytest)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed"; then ok "baseline は緑"; else bad "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== M0: 修正前の plan.sh + dispatcher.sh ($PRE_FIX_COMMIT)"
fresh_copy
git -C "$REPO_ROOT" show "$PRE_FIX_COMMIT:scripts/plan.sh"       > "$WORK/tree/scripts/plan.sh"       || bad "M0 注入失敗"
git -C "$REPO_ROOT" show "$PRE_FIX_COMMIT:scripts/dispatcher.sh" > "$WORK/tree/scripts/dispatcher.sh" || bad "M0 注入失敗"
out="$(run_pytest)"
expect_red M0 "$out" "test_needs_director_removes_the_assignment_that_points_at_the_task"
expect_red M0 "$out" "test_a_worker_holding_a_card_is_not_retired_as_no_task"
expect_red M0 "$out" "test_an_orphan_kai_codex_assignment_does_not_block_the_next_review"
expect_red M0 "$out" "test_end_to_end_needs_director_then_the_next_review_spawns"

echo "== M1: needs-director が assignment を外さない"
fresh_copy
mutate scripts/plan.sh "        # (\`worker_holds_work()\`) — assignment を外す前提はそちらに置いてある。
        agent_name = os.environ.get('AGENT_NAME', '')" "        # (\`worker_holds_work()\`) — assignment を外す前提はそちらに置いてある。
        agent_name = ''" || bad "M1 注入失敗"
out="$(run_pytest)"
expect_red M1 "$out" "test_needs_director_removes_the_assignment_that_points_at_the_task"
# (d) end-to-end は plan.sh と dispatcher の 2 層が両方壊れたときだけ赤 (M0)。片方だけなら
# もう片方が孤児を塞がない (多層防御。旧版が残した assignment にも dispatcher が効く)。

echo "== M2a: Rule 2 が in_progress しか「仕事」と数えない"
fresh_copy
mutate scripts/dispatcher.sh "            has_in_progress = worker_holds_work(agent_name, all_tasks)" "            has_in_progress = any(
                meta.get('worker') == agent_name
                for _, meta in all_tasks
                if meta.get('status') == 'in_progress'
            )" || bad "M2a 注入失敗"
out="$(run_pytest)"
expect_red M2a "$out" "test_a_worker_holding_a_card_is_not_retired_as_no_task"
expect_red M2a "$out" "test_a_worker_holding_a_card_is_not_retired_as_blocked_stuck"

echo "== M2b: 判断待ちの Worker を busy と読まない"
fresh_copy
mutate scripts/dispatcher.sh "        is_idle = not assignment_file.exists() and not waits_on_director" "        is_idle = not assignment_file.exists()" || bad "M2b 注入失敗"
out="$(run_pytest)"
expect_red M2b "$out" "test_a_worker_parked_on_needs_director_is_not_handed_a_new_task"

echo "== M3: Kai-codex の assignment を有無で判定する"
fresh_copy
mutate scripts/dispatcher.sh "    raw = read_assignment(CODEX_REVIEW_AGENT)
    if is_missing(raw):
        return False" "    return (ASSIGNMENTS_DIR / CODEX_REVIEW_AGENT).exists()
    raw = read_assignment(CODEX_REVIEW_AGENT)
    if is_missing(raw):
        return False" || bad "M3 注入失敗"
out="$(run_pytest)"
expect_red M3 "$out" "test_an_orphan_kai_codex_assignment_does_not_block_the_next_review"

echo "== M3b: 孤児の判定が甘い (証明できないものも孤児にする)"
fresh_copy
mutate scripts/dispatcher.sh "    if status in RELEASED_WORK_STATUSES or status == 'needs_director':" "    if status not in ('in_progress', 'pending'):" || bad "M3b 注入失敗"
out="$(run_pytest)"
expect_red M3b "$out" "test_an_assignment_that_cannot_be_proven_an_orphan_still_blocks"
expect_red M3b "$out" "test_a_kai_codex_assignment_on_a_live_run_still_blocks"

echo "== M3c: 形の崩れた assignment を孤児にする"
fresh_copy
mutate scripts/dispatcher.sh "    if not slug or not task_id:
        return True" "    if not slug or not task_id:
        return False" || bad "M3c 注入失敗"
out="$(run_pytest)"
expect_red M3c "$out" "test_an_assignment_that_cannot_be_proven_an_orphan_still_blocks"

echo "== M3d: 読めない assignment を孤児にする"
fresh_copy
mutate scripts/dispatcher.sh "    if is_unreadable(raw):
        return True
    slug, _, task_id" "    if is_unreadable(raw):
        return False
    slug, _, task_id" || bad "M3d 注入失敗"
out="$(run_pytest)"
expect_red M3d "$out" "test_an_unreadable_kai_codex_assignment_still_blocks"

echo "== M4: Rule 5 が card を見ない"
fresh_copy
mutate scripts/dispatcher.sh "        if assigned_task_status == 'needs_director' or waits_on_director:" "        if assigned_task_status == 'needs_director':" || bad "M4 注入失敗"
out="$(run_pytest)"
expect_red M4 "$out" "test_rule5_does_not_double_notify_a_worker_parked_on_needs_director"

echo "== M5: 手放した status の集合が failed / cancelled を欠く"
fresh_copy
mutate scripts/dispatcher.sh "    TERMINAL_STATUSES | set(DEAD_DEP_STATUSES) | set(HELD_DEP_STATUSES)" "    TERMINAL_STATUSES" || bad "M5 注入失敗"
out="$(run_pytest)"
expect_red M5 "$out" "test_control_a_worker_that_released_its_card_is_still_retired"
expect_red M5 "$out" "test_an_orphan_kai_codex_assignment_does_not_block_the_next_review"

echo "== M6: task-graph が assignment の不在を許さない (needs_director の pane_match が消える)"
fresh_copy
mutate scripts/plan.sh "        return status in TASK_GRAPH_PANE_WITHOUT_ASSIGNMENT and _is_enoent(path)" "        return False" || bad "M6 注入失敗"
out="$(run_pytest_graph)"
expect_red M6 "$out" "test_a_worker_parked_on_needs_director_points_at_its_pane_without_an_assignment"
expect_red M6 "$out" "test_needs_director_through_the_real_plan_sh_keeps_the_pane_link"

echo "== M6b: task-graph が「読めない」も「無い」と読む"
fresh_copy
mutate scripts/plan.sh "status in TASK_GRAPH_PANE_WITHOUT_ASSIGNMENT and _is_enoent(path)" "status in TASK_GRAPH_PANE_WITHOUT_ASSIGNMENT" || bad "M6b 注入失敗"
out="$(run_pytest_graph)"
expect_red M6b "$out" "test_an_unreadable_assignment_does_not_count_as_absent_for_needs_director"

echo "== M6c: 不在を許す status が広すぎる"
fresh_copy
mutate scripts/plan.sh "TASK_GRAPH_PANE_WITHOUT_ASSIGNMENT = {'needs_director'}" "TASK_GRAPH_PANE_WITHOUT_ASSIGNMENT = set(TASK_GRAPH_PANE_STATUSES)" || bad "M6c 注入失敗"
out="$(run_pytest_graph)"
expect_red M6c "$out" "test_no_assignment_is_not_enough_for_the_other_pane_statuses"

echo
echo "== 結果: OK=$PASS BAD=$FAIL"
[ "$FAIL" -eq 0 ]
