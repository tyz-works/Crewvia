#!/usr/bin/env bash
# PR 番号の推移的な伝播 (t017 / backlog #29) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t017.sh
#
#   baseline — いまの木では tests/test_pr_number_transitive.py と
#              tests/test_assignment_routing.py が緑
#   A  _reachable_pr_targets: 直接の依存先で探索を止める (通過を無効化、旧実装に戻す)  → 赤
#   B  _is_pr_passthrough: 宣言の有無・値を見ずに常に通過とみなす                       → 赤
#   C  propagate_pr_number: 合流点 (上流に deliverable: pr が 2 つ以上) の判定を外す    → 赤
#   D  codex_reviews_awaiting_pr: 合流点の判定を外す (待っていないものを待たせる)       → 赤
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# pytest は隔離した queue・registry (Sandbox) で動き、本物の herdr / tmux / 本番 queue には届かない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避するだけ)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t017.XXXXXX")"
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
TESTS=(tests/test_pr_number_transitive.py tests/test_assignment_routing.py tests/test_task_deliverable.py)

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

echo "== case A: _reachable_pr_targets が直接の依存先で止まる (通過を無効化、旧実装に戻す)"
fresh_copy
inject scripts/plan.sh \
"        reached[tid] = meta
        if _is_pr_passthrough(meta):
            queue.extend(dependents_of.get(tid, []))" \
"        reached[tid] = meta
        if False:
            queue.extend(dependents_of.get(tid, []))"
expect_red "case A" "test_reaches_a_review_task_past_a_non_pr_qa_and_codex_review"

echo "== case B: _is_pr_passthrough が宣言を見ずに常に真を返す (別 PR・宣言なしでも通過してしまう)"
fresh_copy
inject scripts/plan.sh \
"    return 'deliverable' in meta and meta.get('deliverable') in ('file', 'none')" \
"    return True"
expect_red "case B" "test_stops_at_another_pr_producing_task"

echo "== case C: propagate_pr_number が合流点の判定を外す (どの PR か一意でなくても書いてしまう)"
fresh_copy
inject scripts/plan.sh \
"        ancestors = _pr_source_ancestors(tid, tasks_by_id, memo)
        if len(ancestors) >= 2:" \
"        ancestors = _pr_source_ancestors(tid, tasks_by_id, memo)
        if False:"
expect_red "case C" "test_a_confluence_of_two_pr_ancestors_is_not_written"

echo "== case D: codex_reviews_awaiting_pr が合流点の判定を外す (待っていないものまで待たせる)"
fresh_copy
inject scripts/plan.sh \
"        if len(_pr_source_ancestors(tid, tasks_by_id, memo)) >= 2:
            continue" \
"        if False:
            continue"
expect_red "case D" "test_confluence_does_not_force_pr_on_the_upstream_done"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
