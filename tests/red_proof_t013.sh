#!/usr/bin/env bash
# task の成果物の宣言 `deliverable` (t013 / backlog #31) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t013.sh
#
#   baseline — いまの木では tests/test_task_deliverable.py が緑
#   A  done: deliverable: pr でも --pr / --no-pr を求めない                         → 赤
#   B  done: 読めない宣言の値を「宣言なし」と読む                                   → 赤
#   C  lint: pr|file の task を実際の権限 (check_permission) で判定しない (常に通す) → 赤
#   D  lint: check_permission() への委譲をやめ、宣言の集計 (旧実装、t088 で撤去) に戻す → 赤
#   E  lint: 印のある mission でも宣言の無い task を通す                            → 赤
#   F  lint: 印が無くても宣言を必須にする (既存 mission を止める)                   → 赤
#   G  lint: config が読めなくても pr|file を通す                                   → 赤
#   H  lint: 不正な can_produce_deliverable を False に倒す (truthiness で潰す)     → 赤
#   I  lint: mission.yaml が読めないのを「印なし」に潰す                            → 赤
#   J  init: mission.yaml に印を書かない                                            → 赤
#   K  add / update: 不正な --deliverable を受け付ける                              → 赤
#   L  config: codex-review の can_produce_deliverable: false を外す                → 赤
#
# t084 (Codex 6巡目 P2) で追加:
#   M  done: 'deliverable' キーはあるが値が null の task を「宣言なし」と読む         → 赤 (t084 P2)
#      (has_declaration を見ず、旧来の `declared is not None` に戻す)
#   N  lint: check_deliverable() が per-task の null を「宣言なし」と読む            → 赤 (t084 P2)
#      (has_declaration を見ず、旧来の `declared is None` に戻す)
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

# inject_span <file> <old_content_file> <new_content_file> — old/new をファイル経由で渡す版
# (バッククォート・$ を多く含む大きなブロックを引用符地獄なしで置換するため。red_proof_t055.sh と同じ)。
inject_span() {
    FILE="$1" OLDFILE="$2" NEWFILE="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = pathlib.Path(os.environ["OLDFILE"]).read_text()
new = pathlib.Path(os.environ["NEWFILE"]).read_text()
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
inject scripts/plan.sh "            if has_declaration and (declared == 'pr' or declared not in DELIVERABLE_VALUES):" "            if False:"
expect_red "case A" "test_pr_without_a_flag_is_refused_with_exit_2_and_writes_nothing"

echo "== case B: 読めない宣言の値を「宣言なし」と読む"
fresh_copy
inject scripts/plan.sh "            if has_declaration and (declared == 'pr' or declared not in DELIVERABLE_VALUES):" "            if has_declaration and declared == 'pr':"
expect_red "case B" "test_an_unreadable_declaration_is_refused_not_read_as_undeclared"

echo "== case C: pr|file の task を実際の権限 (check_permission) で判定しない (常に通す)"
fresh_copy
inject scripts/lint_plan.py "        can_produce, reason = _skills_can_produce(config, skills, declared)
        if not can_produce:" "        can_produce, reason = _skills_can_produce(config, skills, declared)
        if False:"
expect_red "case C" "test_a_deliverable_with_only_non_producing_skills_fails"

echo "== case D: check_permission() への委譲をやめ、宣言の集計 (旧実装、t088 で撤去) に戻す"
# t088 (PR#236 7巡目 P2) の本題: 「1 つでも can_produce_deliverable: true な skill が
# あれば作れる」という旧実装 (all(table.get(s) is False for s in skills)) は、hook が
# skill の deny の和を allow より先に union で適用することを見ていない。[research, code]
# のような組み合わせで、宣言の集計と実際の権限がズレる (このケースを戻すと、それを検出する
# ために書いた test_a_denying_skill_mixed_in_still_fails / test_a_mixed_skill_list_with_a_denying_skill_fails
# が緑に戻ってしまう = 赤になるはず)。
fresh_copy
cat > "$WORK/d_new.txt" <<'BLOCK'
        config, hook_problem = hook_config()
        if hook_problem is not None:
            results.append(('FAIL', 'deliverable',
                            f"{prefix}: deliverable '{declared}' を skills と突き合わせられません — {hook_problem}"))
            continue

        can_produce, reason = _skills_can_produce(config, skills, declared)
        if not can_produce:
            results.append(('FAIL', 'deliverable',
                            f"{prefix}: deliverable '{declared}' を宣言していますが、skills {skills} は"
                            f" 実際には成果物を作れません — {reason}"))
BLOCK
cat > "$WORK/d_old.txt" <<'BLOCK'
        if all(table.get(s) is False for s in skills):
            results.append(('FAIL', 'deliverable',
                            f"{prefix}: deliverable '{declared}' を宣言していますが、skills {skills} は"
                            f" すべて can_produce_deliverable: false で、成果物を作れません "
                            f"(成果物を作れる skill を足すか、deliverable を none にする)"))
BLOCK
inject_span scripts/lint_plan.py "$WORK/d_new.txt" "$WORK/d_old.txt"
expect_red "case D" "test_a_denying_skill_mixed_in_still_fails"

echo "== case E: 印のある mission でも宣言の無い task を通す"
fresh_copy
inject scripts/lint_plan.py "            if required:" "            if False:"
expect_red "case E" "test_a_marked_mission_requires_a_declaration_on_every_card"

echo "== case F: 印が無くても宣言を必須にする"
fresh_copy
inject scripts/lint_plan.py "            if required:" "            if True:"
expect_red "case F" "test_an_old_mission_without_the_mark_is_not_stopped"

echo "== case G: config が読めなくても pr|file を通す"
# t088 (PR#236 7巡目 P2) で check_deliverable() に guard が 2 つになった (capabilities() の
# problem と hook_config() の hook_problem — どちらも同じ skill_permissions_path を読む)。
# 「config が無い」テストは元は前者だけで拾えたが、今は後者が独立に同じ FAIL を出すため、
# 前者だけを外しても赤にならない (これは退行ではなく、多重防御が効いている証拠)。
# この case は両方を外して初めて「config が読めなくても通ってしまう」を再現する。
fresh_copy
inject scripts/lint_plan.py "        if problem is not None:" "        if False:"
inject scripts/lint_plan.py "        if hook_problem is not None:" "        if False:"
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

echo "== case M: done が 'deliverable' キーはあるが値が null の task を「宣言なし」と読む (t084 P2)"
fresh_copy
inject scripts/plan.sh "            has_declaration = 'deliverable' in meta
            declared = meta.get('deliverable')
            if has_declaration and (declared == 'pr' or declared not in DELIVERABLE_VALUES):" \
       "            has_declaration = 'deliverable' in meta
            declared = meta.get('deliverable')
            if declared == 'pr' or (declared is not None and declared not in DELIVERABLE_VALUES):"
expect_red "case M" "test_an_explicit_null_declaration_is_refused_not_read_as_undeclared"

echo "== case N: lint の check_deliverable() が per-task の null を「宣言なし」と読む (t084 P2)"
fresh_copy
inject scripts/lint_plan.py "        if not has_declaration:" "        if declared is None:"
expect_red "case N" "test_an_explicit_null_is_rejected_as_unreadable_not_treated_as_undeclared"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
