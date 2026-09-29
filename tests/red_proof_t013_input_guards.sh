#!/usr/bin/env bash
# plan.sh の入力の守り 3 つ (t013 / C4) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t013_input_guards.sh
#
#   baseline — いまの木では tests/test_plan_input_guards.py が緑
#   case A   — 修正前 (origin/main 版) の plan.sh / lint_plan.py / lib_dep_rules.py に戻す → 赤 (全体)
#   case B   — init --inactive が state.yaml を書く (active に足し default を奪う)       → 赤
#   case C   — update --blocked-by が循環を拒否しない                                   → 赤
#   case D   — add が循環を拒否しない                                                   → 赤
#   case E   — 循環の検出が「同じ経路の重複表示」に戻る (find_dependency_cycle)          → 赤
#   case F   — lint が deliverable: pr の下流 review を見ない (WARN が出ない)            → 赤
#   case G   — lint が下流を直接の 1 段しか見ない (間接の review で誤 WARN)              → 赤
#   case H   — lint が skills を見ずに「下流があれば足りる」とする                        → 赤
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t013.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

TREE=""
fresh_copy() {
    N=$((N + 1)); TREE="$WORK/tree$N"; mkdir -p "$TREE"
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
          --exclude='queue' --exclude='logs' "$REPO_ROOT/" "$TREE/"
}

# inject <file(TREE 相対)> <old> <new> — old を new に置換 (ちょうど 1 か所。無ければ FATAL)
inject() {
    OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

# 呼び出し元の AGENT_NAME / SKILLS / CREWVIA_* を引き継がない (テストは自前の env を組む)
run_suite() {
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_plan_input_guards.py -p no:cacheprovider 2>&1 )
}

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local out; out="$(run_suite)"
    if echo "$out" | grep -q "FAILED .*$2"; then ok "$1 → 赤 ($2)"
    else ng "$1 → 赤にならなかった ($2)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_suite)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed"; then ok "baseline は緑 ($(echo "$out" | tail -1))"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== case A: 修正前の plan.sh / lint_plan.py / lib_dep_rules.py"
fresh_copy
if git -C "$REPO_ROOT" show origin/main:scripts/plan.sh > "$TREE/scripts/plan.sh" 2>/dev/null \
   && git -C "$REPO_ROOT" show origin/main:scripts/lint_plan.py > "$TREE/scripts/lint_plan.py" \
   && git -C "$REPO_ROOT" show origin/main:scripts/lib_dep_rules.py > "$TREE/scripts/lib_dep_rules.py" \
   && ! grep -q "find_dependency_cycle" "$TREE/scripts/lib_dep_rules.py"; then
    out="$(run_suite)"
    if echo "$out" | grep -qE "[0-9]+ (failed|error)"; then ok "case A → 赤 ($(echo "$out" | tail -1))"
    else ng "case A → 赤にならなかった"; echo "$out" | tail -5; fi
else
    echo "  SKIP: origin/main が修正済み (または取得できない) ので、修正前の版を作れない"
fi

echo "== case B: init --inactive が state を書く"
fresh_copy
inject scripts/plan.sh "        if inactive:
            # dispatcher は" "        if False:
            # dispatcher は"
expect_red "case B" "test_init_inactive_does_not_touch_state"

echo "== case C: update が循環を拒否しない"
fresh_copy
inject scripts/plan.sh "            _reject_dependency_cycle(slug, task_id, new_blocked)
            meta['blocked_by'] = new_blocked" "            meta['blocked_by'] = new_blocked"
expect_red "case C" "test_update_that_closes_a_cycle_is_refused_and_writes_nothing"

echo "== case D: add が循環を拒否しない"
fresh_copy
inject scripts/plan.sh "        _reject_dependency_cycle(slug, task_id, blocked_by)
" ""
expect_red "case D" "test_add_that_closes_a_cycle_is_refused_and_writes_nothing"

echo "== case E: 循環の経路が重複表示に戻る"
fresh_copy
inject scripts/lib_dep_rules.py "            return path[path.index(node):]" "            return path[path.index(node):] + [node]"
expect_red "case E" "test_cycle_definition_is_only_in_lib_dep_rules"

echo "== case F: lint が下流の review を見ない"
fresh_copy
inject scripts/lint_plan.py "    all_results += check_pr_has_review_downstream(valid_tasks)
" ""
expect_red "case F" "test_pr_task_without_a_downstream_review_warns_but_does_not_fail"

echo "== case G: lint が直接の 1 段しか見ない"
fresh_copy
inject scripts/lint_plan.py "            stack.extend(dependents.get(cur, []))" "            pass"
expect_red "case G" "test_pr_task_with_an_indirect_downstream_review_does_not_warn"

echo "== case H: skills を見ずに下流があれば足りるとする"
fresh_copy
inject scripts/lint_plan.py "            if isinstance(skills, list) and _DEP_RULES.PR_REVIEW_SKILL in skills:" "            if True:"
expect_red "case H" "test_a_downstream_task_that_is_not_review_still_warns"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
