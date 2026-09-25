#!/usr/bin/env bash
# 欠陥を戻すと赤くなることの実証 (PR #217 への Kai-codex の指摘 P1 / P2, t025)。
#
# 期待値をテストの中に書き直しただけのテストは、欠陥の留め金になる
# (memory: regression-test-must-prove-red)。だから「直したら緑」ではなく
# **「戻したら赤」** を機械的に確かめる。
#
# 使い方 (commit してから):
#     bash tests/red_proof_t025.sh
#
# 隔離: 本番の worktree には触らない。`git archive HEAD` で使い捨ての木を作り、
# そこへ注入する。`PYTHONDONTWRITEBYTECODE=1` は必須 —— 同サイズ・同秒の注入は
# 前の .pyc が再利用されて偽の緑になる (memory: defect-injection-needs-pyc-purge)。
#
# 注入するのは 2 つの指摘のそれぞれ:
#   P1  解除の案内から `--mission` を落とす (元の欠陥) / 1 経路だけ別 mission を渡す
#   P2  released_deps の検証を外す (元の欠陥) / 1 枚目だけ外す / 2 枚目の網だけ外す

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t025.XXXXXX")"
export PYTHONDONTWRITEBYTECODE=1
TEST=tests/test_held_hint_and_released_deps.py

cleanup() { chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null; }
trap cleanup EXIT

echo "== 作業用のコピーを作る: $WORK"
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$WORK" || {
    echo "FATAL: git archive に失敗 (commit してから実行すること)"; exit 1; }

PASS=0
FAIL=0

# replace <ファイル> <置換前> <置換後> [回数]  — 置換前が無ければ注入失敗 (= コードの形が変わった)
replace() {
    python3 - "$WORK/$1" "$2" "$3" "${4:-1}" <<'PY'
import sys
path, old, new, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
s = open(path, encoding="utf-8").read()
if old not in s:
    sys.exit(f"注入点が無い: {old!r}")
open(path, "w", encoding="utf-8").write(s.replace(old, new, count))
PY
}

# 元の状態に戻す (git archive の内容へ)。
restore() {
    git -C "$REPO_ROOT" archive HEAD scripts | tar -x -C "$WORK"
    find "$WORK" -name '*.pyc' -delete 2>/dev/null
}

# expect_red <名前> <-k 式>   — 直前に注入した状態で、その -k が **失敗する** こと
expect_red() {
    local name="$1" selector="$2" out rc
    out="$(cd "$WORK" && env -i PATH="$PATH" HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
        python3 -m pytest "$TEST" -q -k "$selector" -p no:cacheprovider 2>&1)"
    rc=$?
    echo "$out" | tail -8
    if [ "$rc" -ne 0 ]; then
        echo "  => RED (期待どおり: 欠陥を戻すと落ちる) — $name"
        PASS=$((PASS + 1))
    else
        echo "  => 緑のまま (**テストが欠陥を捕まえていない**) — $name"
        FAIL=$((FAIL + 1))
    fi
}

section() { echo; echo "================================================================"; echo "== $1"; echo "================================================================"; }

# ---- 前提: 注入前は緑 -------------------------------------------------------
section "baseline (注入なし) — 緑でなければならない"
restore
out="$(cd "$WORK" && env -i PATH="$PATH" HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
    python3 -m pytest "$TEST" -q -p no:cacheprovider 2>&1)"; rc=$?
echo "$out" | tail -3
[ "$rc" -eq 0 ] || { echo "FATAL: 注入前から赤"; exit 1; }

# ---- P1 ---------------------------------------------------------------------
section "P1-a: 解除の案内から --mission を落とす (元の欠陥)"
restore
replace scripts/plan.sh 'release-dep {task_id} --mission {slug}` ' 'release-dep {task_id}` ' &&
replace scripts/plan.sh 'update {task_id} --mission {slug} --status skipped' 'update {task_id} --status skipped' &&
expect_red "P1-a" 'pasted or every_command'

section "P1-b: status 要約だけ、別 mission (default) の slug を渡す"
restore
replace scripts/plan.sh "held_dependency_hint(m['id'], deps, slug)" "held_dependency_hint(m['id'], deps, 'm-hold')" &&
expect_red "P1-b" 'status-summary'

section "P1-c: pull --task の拒否だけ、別 mission の slug を渡す"
restore
replace scripts/plan.sh 'held_dependency_hint(specific_task, verdict.held, slug)' "held_dependency_hint(specific_task, verdict.held, 'm-hold')" &&
expect_red "P1-c" 'pull-task-refusal'

section "P1-d: pull 診断だけ、別 mission の slug を渡す"
restore
replace scripts/plan.sh 'held_dependency_hint(t, h, s) for s, t, h in held_tasks' "held_dependency_hint(t, h, 'm-hold') for s, t, h in held_tasks" 2 &&
expect_red "P1-d" 'pull-diagnostic'

section "P1-e: slug に既定値を戻す (渡し忘れを黙って許す形)"
restore
replace scripts/plan.sh 'def held_dependency_hint(task_id, held, slug):' "def held_dependency_hint(task_id, held, slug='m-hold'):" &&
expect_red "P1-e" 'no_call_of_the_hint'

# ---- P2 ---------------------------------------------------------------------
section "P2-a: released_deps の検証を両方外す (元の欠陥: 読み取りは素通し + set(...) にそのまま渡す)"
restore
replace scripts/lib_task_cards.py '    problem = released_deps_problem(meta.get('"'"'released_deps'"'"'))
    if problem:' '    problem = None
    if problem:' &&
replace scripts/lib_dep_rules.py "    if not (isinstance(released, (list, tuple))
            and all(isinstance(d, str) for d in released)):" "    if False:" &&
replace scripts/lib_dep_rules.py "    released = meta.get('released_deps')
    if False:" "    released = meta.get('released_deps') or ()
    if False:" &&
expect_red "P2-a" 'bad_released or second_layer or release_dep_is_still or update_blocked_by'

section "P2-b: 読み取りの隔離だけ外す (2 枚目の網は残る)"
restore
replace scripts/lib_task_cards.py '    problem = released_deps_problem(meta.get('"'"'released_deps'"'"'))
    if problem:' '    problem = None
    if problem:' &&
expect_red "P2-b" 'isolates_the_card'

section "P2-c: 2 枚目の網だけ外す (読み取りの隔離は残る)"
restore
replace scripts/lib_dep_rules.py "    if not (isinstance(released, (list, tuple))
            and all(isinstance(d, str) for d in released)):" "    if False:" &&
replace scripts/lib_dep_rules.py "    released = meta.get('released_deps')
    if False:" "    released = meta.get('released_deps') or ()
    if False:" &&
expect_red "P2-c" 'second_layer'

section "P2-d: 検証を「mapping / 数値だけ通す」形に緩める (list の要素検査を外す)"
restore
replace scripts/lib_task_cards.py '    if bad:
        return (f"released_deps の要素は' '    if False:
        return (f"released_deps の要素は' &&
expect_red "P2-d" 'list_non_id or list_mixed or list_int or list_bool or list_almost or block_list_non_id or validator_rejects'

section "P2-e: 隔離が出口を塞ぐ形 (release-dep が不正な値を捨てず、そのまま読む)"
restore
replace scripts/plan.sh "        problem = _TASK_CARDS.released_deps_problem(meta.get('released_deps'))
        if problem:
            print(f\"[plan.sh warn] {slug}/{task_id}: {problem} — 不正な released_deps は\"
                  f\"捨てて書き直します\", file=sys.stderr)
            meta['released_deps'] = []" "        pass" &&
expect_red "P2-e" 'release_dep_is_still'

echo
echo "================================================================"
echo "== 結果: 期待どおり赤 ${PASS} 件 / 緑のまま (検出漏れ) ${FAIL} 件"
echo "================================================================"
[ "$FAIL" -eq 0 ]
