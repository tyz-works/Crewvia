#!/usr/bin/env bash
# task の status の語彙・許可遷移 (vNext 01a S1 / lib_task_status.py) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_s1_task_status.sh
#
#   baseline — いまの木では tests/test_task_status_single_definition.py が緑
#   case A   — lint の語彙から needs_director を外す (修正前の形)                  → 赤 (lint が FAIL)
#   case B   — done の遷移表の拒否を外す (どの status からも done が通る)            → 赤
#   case C   — dispatcher.sh に TERMINAL_STATUSES のコピーを書き戻す                → 赤 (構造ガード)
#   case D   — 語彙に cancelled を戻す                                              → 赤
#   case E   — needs_director の理由必須チェックを外す                              → 赤
#   case F   — verify-result の拒否を外す (pending / done から通る)                 → 赤
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番には触らない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-s1.XXXXXX")"
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
    find "$TREE" -name '__pycache__' -prune -exec rm -r {} + 2>/dev/null || true
}

# inject <file> <old> <new> — 複製した <file> の old を new に置換 (1 か所だけ。無ければ FATAL)
inject() {
    F="$1" OLD="$2" NEW="$3" python3 - "$TREE/scripts/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

run_suite() {
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        CREWVIA_HERDR_SOCK="$WORK/no-such-herdr.sock" PYTHONDONTWRITEBYTECODE=1 \
        python3 -m pytest tests/test_task_status_single_definition.py -p no:cacheprovider 2>&1 )
}

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local out; out="$(run_suite)"
    if echo "$out" | grep -q "FAILED .*$2"; then ok "$1 → 赤 ($2)"
    else ng "$1 → 赤にならなかった ($2)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_suite)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed"; then ok "baseline は緑"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== case A: lint の語彙から needs_director を外す"
fresh_copy
inject lint_plan.py 'VALID_STATUSES = _TASK_STATUS.TASK_STATUSES' \
       "VALID_STATUSES = _TASK_STATUS.TASK_STATUSES - {'needs_director'}"
expect_red "case A" "test_lint_accepts_a_needs_director_card_that_needs_director_wrote"

echo "== case B: done の遷移表の拒否を外す"
fresh_copy
inject plan.sh "        if not _TASK_STATUS.accepts('done', cur_status):
            refuse_transition('done', task_id, cur_status)" "        pass"
expect_red "case B" "test_transition_table_is_enforced\[done-done\]"

echo "== case C: dispatcher.sh に TERMINAL_STATUSES のコピーを書き戻す"
fresh_copy
inject dispatcher.sh "# Skills that mark a task as Director-only" "TERMINAL_STATUSES = {'done', 'verified', 'skipped'}
# Skills that mark a task as Director-only"
expect_red "case C" "test_no_status_set_is_defined_outside_lib_task_status"

echo "== case D: 語彙に cancelled を戻す"
fresh_copy
inject lib_task_status.py "    'verification_failed',
})

#: 依存を満たす完了。" "    'verification_failed',
    'cancelled',
})

#: 依存を満たす完了。"
expect_red "case D" "test_the_vocabulary_is_self_consistent"

echo "== case E: needs_director の理由必須チェックを外す"
fresh_copy
inject lint_plan.py "        if status == 'needs_director':" "        if False:"
expect_red "case E" "test_lint_rejects_needs_director_without_a_reason"

echo "== case F: verify-result の拒否を外す"
fresh_copy
inject plan.sh "        if not _TASK_STATUS.accepts('verify-result', cur_status):
            refuse_transition('verify-result', task_id, cur_status)" "        pass"
expect_red "case F" "test_transition_table_is_enforced\[done-verify-result\]"

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
