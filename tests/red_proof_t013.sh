#!/usr/bin/env bash
# task の成果物の宣言 `deliverable` (t013 / backlog #31) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t013.sh
#
#   baseline — いまの木では tests/test_task_deliverable.py が緑
#   A  done: deliverable: pr でも --pr / --no-pr を求めない                         → 赤
#   B  done: 読めない宣言の値を「宣言なし」と読む                                   → 赤
#   C  lint: pr|file の task を skills と突き合わせない                             → 赤
#   D  lint: skills の「どれか 1 つ」が作れない (all でなく any) で FAIL にする     → 赤
#   E  lint: 印のある mission でも宣言の無い task を通す                            → 赤
#   F  lint: 印が無くても宣言を必須にする (既存 mission を止める)                   → 赤
#   G  lint: config が読めなくても pr|file を通す                                   → 赤
#   H  lint: 不正な can_produce_deliverable を False に倒す (truthiness で潰す)     → 赤
#   I  lint: mission.yaml が読めないのを「印なし」に潰す                            → 赤
#   J  init: mission.yaml に印を書かない                                            → 赤
#   K  add / update: 不正な --deliverable を受け付ける                              → 赤
#   L  config: codex-review の can_produce_deliverable: false を外す                → 赤
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# pytest は隔離した queue・registry (Sandbox) で動き、本物の herdr / tmux / 本番 queue には届かない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避するだけ)。
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

# inject <file> <old> <new> — 複製した <file> の old を new に置換 (ちょうど 1 か所。無ければ FATAL)
inject() {
    FILE="$1" OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    print(f"count={s.count(old)}", file=sys.stderr)
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

# 呼び出し元の AGENT_NAME / SKILLS / CREWVIA_* を引き継がない (テストは自前の env を組む)
run_py() {  # run_py <pytest の引数...>
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider "$@" 2>&1 )
}
TESTS=(tests/test_task_deliverable.py)

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local name="$1" frag="$2"
    local out; out="$(run_py "${TESTS[@]}")"
    if echo "$out" | grep -q "FAILED .*$frag"; then ok "$name → 赤 ($frag)"
    else ng "$name → 赤にならなかった ($frag)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_py "${TESTS[@]}")"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then ok "baseline は緑"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== case A: deliverable: pr でも --pr / --no-pr を求めない"
fresh_copy
inject scripts/plan.sh "            if declared == 'pr' or (declared is not None and declared not in DELIVERABLE_VALUES):" "            if False:"
expect_red "case A" "test_pr_without_a_flag_is_refused_with_exit_2_and_writes_nothing"

echo "== case B: 読めない宣言の値を「宣言なし」と読む"
fresh_copy
inject scripts/plan.sh "            if declared == 'pr' or (declared is not None and declared not in DELIVERABLE_VALUES):" "            if declared == 'pr':"
expect_red "case B" "test_an_unreadable_declaration_is_refused_not_read_as_undeclared"

echo "== case C: pr|file の task を skills と突き合わせない"
fresh_copy
inject scripts/lint_plan.py "        if all(table.get(s) is False for s in skills):" "        if False:"
expect_red "case C" "test_a_deliverable_with_only_non_producing_skills_fails"

echo "== case D: skills のどれか 1 つが作れないだけで FAIL (any)"
fresh_copy
inject scripts/lint_plan.py "        if all(table.get(s) is False for s in skills):" "        if any(table.get(s) is False for s in skills):"
expect_red "case D" "test_one_producing_skill_is_enough"

echo "== case E: 印のある mission でも宣言の無い task を通す"
fresh_copy
inject scripts/lint_plan.py "            if required:" "            if False:"
expect_red "case E" "test_a_marked_mission_requires_a_declaration_on_every_card"

echo "== case F: 印が無くても宣言を必須にする"
fresh_copy
inject scripts/lint_plan.py "            if required:" "            if True:"
expect_red "case F" "test_an_old_mission_without_the_mark_is_not_stopped"

echo "== case G: config が読めなくても pr|file を通す"
fresh_copy
inject scripts/lint_plan.py "        if problem is not None:" "        if False:"
expect_red "case G" "test_an_unreadable_config_is_a_fail_not_a_pass"

echo "== case H: 不正な can_produce_deliverable を False に倒す"
fresh_copy
inject scripts/lint_plan.py \
"caps[name] = raw if isinstance(raw, bool) else str(raw)" \
"caps[name] = raw if isinstance(raw, bool) else False"
expect_red "case H" "test_a_malformed_capability_is_a_fail_not_silently_true_or_false"

echo "== case I: mission.yaml が読めないのを「印なし」に潰す"
fresh_copy
inject scripts/lint_plan.py "    if problem is not None:
        return False, problem
    if 'deliverable_required' not in data:
        return False, None
    value = data['deliverable_required']" "    if problem is not None:
        return False, None
    if 'deliverable_required' not in data:
        return False, None
    value = data['deliverable_required']"
expect_red "case I" "test_an_unreadable_mission_yaml_is_a_problem_not_a_no"

echo "== case J: init が mission.yaml に印を書かない"
fresh_copy
inject scripts/plan.sh "            'deliverable_required': True," "            'deliverable_required': None,"
expect_red "case J" "test_init_writes_deliverable_required_true"

echo "== case K: 不正な --deliverable を受け付ける"
fresh_copy
inject scripts/plan.sh "    if value not in DELIVERABLE_VALUES:
        _usage_exit(" "    if False:
        _usage_exit("
expect_red "case K" "test_add_rejects_an_invalid_value_with_exit_2_and_writes_nothing"

echo "== case L: config の codex-review の欄を外す"
fresh_copy
inject config/skill-permissions.yaml "  codex-review:
    can_produce_deliverable: false
" "  codex-review:
"
expect_red "case L" "test_the_six_non_producers_declare_false"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
