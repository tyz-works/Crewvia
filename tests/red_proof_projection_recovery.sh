#!/usr/bin/env bash
# S4 (vNext 01a / t016): 「ロックを取ったとき card を正本に projection を作り直す」修正が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_projection_recovery.sh        (約 10 分)
#
#   baseline — いまの木では tests/test_projection_recovery_on_lock.py と lib の回復のテストが緑
#   case A   — plan.sh が回復を呼ばない (recover_before が何もしない = 修正前と同じ)              → 赤 (crash 注入 5 本)
#   case B   — R-2 が holding の status (in_progress) の枠も孤児として消す (進行中の遷移を壊す)    → 赤 (並行・後任)
#   case C   — 逆引き (この card を指す枠) を外す                                               → 赤 (reset の途中)
#   case D   — 所有の証拠が archive/ を読まない (旧設計 §2.5)                                    → 赤 (archive の中の所有)
#   case E   — done の D1 (自分の card に pr_number を先に永続化) を外す                         → 赤 (done の順序)
#   case F   — done の D0 (a) (依存先の食い違いの拒否) を外す                                     → 赤
#   case G   — done の D0 (b) (card に番号があるのに --no-pr の拒否) を外す                        → 赤
#   case H   — reap-orphan-assignment が旧集合 (needs_director / worker なしの pending を消さない) → 赤
#   case I   — 診断 (持ち越し): worker なしの holding を in_progress だけ報告に戻す                → 赤
#   case J   — 診断 (持ち越し): identity の検査を in_progress だけに戻す                          → 赤
#   case K   — R-2 が退避済み mission の card を見ない                                          → 赤
#   case L   — done の回復を前提検査の後ろに置く (拒否された done では回復が走らない)              → 赤
#   case M   — R-1 が所有の証拠を見ずに枠を書く                                                  → 赤 (lib の回復のテスト)
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree・queue・registry には触れない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-s4.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

TREE=""
MUTATED=()
fresh_copy() {
    N=$((N + 1)); TREE="$WORK/tree$N"; mkdir -p "$TREE"; MUTATED=()
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
          --exclude='queue' --exclude='logs' "$REPO_ROOT/" "$TREE/"
    find "$TREE" -name '__pycache__' -prune -exec rm -r {} + 2>/dev/null || true
}

# inject <tree 内の相対パス> <old> <new> — old が 1 か所だけ在ること (無ければ FATAL)
inject() {
    MUTATED+=("$1")
    F="$1" OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

#: 走らせるテスト。case ごとに -k で絞る (baseline は全部)。
SUITE_FILES="tests/test_projection_recovery_on_lock.py tests/test_state_store_transaction.py tests/test_state_store_observability.py tests/test_state_store_crash_injection.py"

run_suite() {   # run_suite [<-k 式>]
    local kexpr="${1:-}"
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        CREWVIA_HERDR_SOCK="$WORK/no-such-herdr.sock" PYTHONDONTWRITEBYTECODE=1 \
        python3 -m pytest $SUITE_FILES ${kexpr:+-k "$kexpr"} -p no:cacheprovider 2>&1 )
}

# 変異後のソースが構文として通ること (通らないと pytest は collection で落ち、狙ったテストが走らずに「赤」に見える)。
assert_mutation_parses() {
    local f
    for f in "${MUTATED[@]}"; do
        case "$f" in
            scripts/plan.sh)
                bash -n "$TREE/$f" || { echo "FATAL: 変異後の $f が bash として通らない"; exit 2; }
                python3 - "$TREE/$f" <<'PY' || { echo "FATAL: 変異後の $f の埋め込み python が構文として通らない"; exit 2; }
import ast, re, sys
m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF\n", open(sys.argv[1]).read(), re.S)
ast.parse(m.group(1))
PY
                ;;
            *.py) python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$TREE/$f" \
                    || { echo "FATAL: 変異後の $f が python として通らない"; exit 2; } ;;
        esac
    done
}

expect_red() {  # expect_red <case名> <-k 式> <赤になるはずのテスト名の断片>...
    local name="$1" kexpr="$2"; shift 2
    assert_mutation_parses
    local out; out="$(run_suite "$kexpr")"
    local frag
    for frag in "$@"; do
        if echo "$out" | grep -q "FAILED .*$frag"; then ok "$name → 赤 ($frag)"
        else ng "$name → 赤にならなかった ($frag)"; echo "$out" | tail -12; fi
    done
}

fresh_copy
echo "== baseline"
out="$(run_suite)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q " failed"; then ok "baseline は緑 ($(echo "$out" | grep -o '[0-9]* passed'))"
else ng "baseline が緑でない"; echo "$out" | tail -12; fi

echo "== case A: plan.sh が回復を呼ばない"
fresh_copy
inject scripts/plan.sh "    caller = os.environ.get('AGENT_NAME', '').strip() if include_caller else ''
    scope = _STORE.Scope(" "    return []
    caller = os.environ.get('AGENT_NAME', '').strip() if include_caller else ''
    scope = _STORE.Scope("
expect_red "case A" "killed or random or add_killed or mission_moved or store_check_lists" \
    "test_pull_killed_at_every_write_point" "test_done_killed_at_every_write_point" \
    "test_reset_killed_between_card_and_assignment" "test_random_crash_injection_converges_20_times" \
    "test_a_mission_moved_out_but_still_active"

echo "== case B: R-2 が holding の status の枠も孤児として消す"
fresh_copy
inject scripts/lib_state_store.py "    return (status in ORPHAN_ASSIGNMENT_FINISHED_STATUSES or status == _NEEDS_DIRECTOR
            or (status == 'pending' and not worker))" "    return (status in ORPHAN_ASSIGNMENT_FINISHED_STATUSES or status == _NEEDS_DIRECTOR
            or status == 'in_progress' or (status == 'pending' and not worker))"
expect_red "case B" "concurrent or successor" \
    "test_concurrent_recovery_never_rewrites_a_correct_in_flight_transaction" \
    "test_recovery_does_not_remove_a_successors_assignment"

echo "== case C: 逆引きを外す"
fresh_copy
inject scripts/lib_state_store.py "            agents.extend(self._reverse_lookup(wanted))" "            pass"
expect_red "case C" "reset_killed" "test_reset_killed_between_card_and_assignment"

echo "== case D: 所有の証拠が archive/ を読まない"
fresh_copy
inject scripts/lib_state_store.py "for base, prefix in (('missions', ''), ('archive', 'archive/')):" "for base, prefix in (('missions', ''),):"
expect_red "case D" "archive" "test_r1_does_not_write_when_the_owner_also_has_an_in_progress_card_in_the_archive"

echo "== case E: done の D1 (pr_number の先行永続化) を外す"
fresh_copy
inject scripts/plan.sh "            _txn().write_card(slug, task_id, {**meta, 'pr_number': pr_number}, body)" "            pass"
expect_red "case E" "done_with_pr or different_pr_number_is_refused_and" \
    "test_done_with_pr_never_leaves_a_dependent_number_the_card_does_not_carry"

echo "== case F: D0 (a) を外す"
fresh_copy
inject scripts/plan.sh "            conflicts = pr_propagation_conflicts(slug, task_id, pr_number)" "            conflicts = []"
expect_red "case F" "dependent_carries_a_different_number" \
    "test_done_is_refused_when_a_dependent_carries_a_different_number_and_writes_nothing"

echo "== case G: D0 (b) を外す"
fresh_copy
inject scripts/plan.sh "        if no_pr_reason is not None and meta.get('pr_number') not in (None, ''):" "        if False:"
expect_red "case G" "no_pr_is_refused" "test_done_no_pr_is_refused_when_the_card_already_carries_a_number"

echo "== case H: reap が旧集合"
fresh_copy
inject scripts/plan.sh "        if not _STORE.is_orphan_target(status, meta.get('worker')):" "        if status not in ORPHAN_ASSIGNMENT_FINISHED_STATUSES:"
expect_red "case H" "reap_orphan" "test_reap_orphan_assignment_uses_the_same_set_as_the_recovery"

echo "== case I: worker なしの holding の報告を in_progress だけに"
fresh_copy
inject scripts/lib_state_store.py "            else:
                self._emit('reported:holding_without_worker', slug, tid,
                           detail=f\"status={_safe_status(status) or '<unknown>'}\")
            return" "            return"
expect_red "case I" "without_a_worker" "test_diagnose_reports_a_holding_card_without_a_worker"

echo "== case J: identity の検査を in_progress だけに"
fresh_copy
inject scripts/lib_state_store.py "            if xid:
                verdict = self.t.classify_assignment(worker, slug, tid, execution_id=xid)" "            if xid and status == 'in_progress':
                verdict = self.t.classify_assignment(worker, slug, tid, execution_id=xid)"
inject scripts/lib_state_store.py "            elif self.t._read_identity(worker) is None:" "            elif status == 'in_progress' and self.t._read_identity(worker) is None:"
expect_red "case J" "broken_identity" "test_diagnose_reports_a_broken_identity_for_every_assignment_holding_status"

echo "== case K: R-2 が退避済み mission の card を見ない"
fresh_copy
inject scripts/lib_state_store.py "            kind, meta, _b, reason, _e = self._archived_card(pslug, ptid)
        if kind == 'missing':
            self._emit('reported:assignment_target_missing'" "            pass
        if kind == 'missing':
            self._emit('reported:assignment_target_missing'"
expect_red "case K" "points_into_the_archive" "test_recovery_removes_an_orphan_slot_that_points_into_the_archive"

echo "== case L: done の回復を前提検査の後ろに置く"
fresh_copy
inject scripts/plan.sh "        recover_before(cards=[(slug, task_id)])
        if not os.path.exists(task_path(slug, task_id)):
            die(f\"task '{task_id}' not found in mission '{slug}'.\")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status in _TASK_STATUS.WAITS_ON_DIRECTOR_STATUSES:" "        if not os.path.exists(task_path(slug, task_id)):
            die(f\"task '{task_id}' not found in mission '{slug}'.\")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status in _TASK_STATUS.WAITS_ON_DIRECTOR_STATUSES:"
inject scripts/plan.sh "        if not _TASK_STATUS.accepts('done', cur_status):
            refuse_transition('done', task_id, cur_status)
" "        if not _TASK_STATUS.accepts('done', cur_status):
            refuse_transition('done', task_id, cur_status)
        recover_before(cards=[(slug, task_id)])
"
expect_red "case L" "done_killed or random" "test_done_killed_at_every_write_point"

echo "== case M: R-1 が所有の証拠を見ない"
fresh_copy
inject scripts/lib_state_store.py "        problem = self._ownership_problem(worker, slug, tid)" "        problem = None"
expect_red "case M" "duplicate or owner or archive or unreadable" \
    "test_r1_does_not_write_when_the_owner_also_has_an_in_progress_card_in_the_archive"

echo
echo "== 結果: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
