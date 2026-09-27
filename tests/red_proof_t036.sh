#!/usr/bin/env bash
# PR 番号の付け忘れ防止 (t036 / t027 P2-1) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t036.sh
#
#   baseline — いまの木では TestDone* (plan.sh done) と test_dispatcher_notify_once.py が緑
#   A  done: 待っている codex-review があっても拒否しない                         → 赤
#   B  done: pr_number が設定済みの codex-review も「待っている」に数える        → 赤
#   C  done: 終わった (done/failed/...) codex-review も「待っている」に数える    → 赤
#   D  done: 間接の依存先まで「待っている」に数える                              → 赤
#   E  done: --no-pr の理由を card に残さない                                    → 赤
#   F  done: --no-pr の理由が空でも受理する                                      → 赤
#   G  done: --pr と --no-pr の同時指定を受理する                                → 赤
#   H  dispatcher: pr_number の無い ready な codex-review を Director に知らせない → 赤
#   I  dispatcher: 状態が続くあいだ毎サイクル知らせる (台帳を効かせない)          → 赤
#   J  dispatcher: pr_number があっても知らせて spawn を止める                    → 赤
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# pytest は FakeMux / 隔離した queue・registry で動き、本物の herdr / tmux / 本番 queue には届かない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避するだけ)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t036.XXXXXX")"
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
DONE_TESTS=(tests/test_assignment_routing.py -k "TestDonePr or TestDoneRequiresPr")
DISP_TESTS=(tests/test_dispatcher_notify_once.py)

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片> <pytest の引数...>
    local name="$1" frag="$2"; shift 2
    local out; out="$(run_py "$@")"
    if echo "$out" | grep -q "FAILED .*$frag"; then ok "$name → 赤 ($frag)"
    else ng "$name → 赤にならなかった ($frag)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_py "${DONE_TESTS[@]}")"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then ok "baseline (done) は緑"
else ng "baseline (done) が緑でない"; echo "$out" | tail -8; fi
out="$(run_py "${DISP_TESTS[@]}")"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then ok "baseline (dispatcher) は緑"
else ng "baseline (dispatcher) が緑でない"; echo "$out" | tail -8; fi

echo "== case A: 待っている codex-review があっても拒否しない"
fresh_copy
inject scripts/plan.sh "            if awaiting:
                die(" "            if False:
                die("
expect_red "case A" "test_missing_pr_is_refused_with_exit_2_and_writes_nothing" "${DONE_TESTS[@]}"

echo "== case B: pr_number が設定済みの codex-review も数える"
fresh_copy
inject scripts/plan.sh "        if meta.get('pr_number') not in (None, ''):
            continue
        waiting.append(meta['id'])" "        waiting.append(meta['id'])"
expect_red "case B" "test_a_codex_review_that_already_has_its_number_does_not_count" "${DONE_TESTS[@]}"

echo "== case C: 終わった codex-review も数える"
fresh_copy
inject scripts/plan.sh "PR_NOT_AWAITED_STATUSES = {'done', 'verified', 'failed', 'skipped', 'cancelled', 'verification_failed'}" "PR_NOT_AWAITED_STATUSES = set()"
expect_red "case C" "test_a_codex_review_that_is_already_over_does_not_count" "${DONE_TESTS[@]}"

echo "== case D: 間接の依存先まで数える"
fresh_copy
inject scripts/plan.sh "        if not isinstance(deps, list) or task_id not in deps:
            continue
        if 'codex-review' not in set(meta.get('skills') or []):" "        if 'codex-review' not in set(meta.get('skills') or []):"
expect_red "case D" "test_an_indirect_dependent_does_not_count" "${DONE_TESTS[@]}"

echo "== case E: --no-pr の理由を card に残さない"
fresh_copy
inject scripts/plan.sh "            meta['no_pr_waiver'] = no_pr_reason" "            pass"
expect_red "case E" "test_no_pr_waives_it_and_leaves_the_reason_on_the_card_and_stderr" "${DONE_TESTS[@]}"

echo "== case F: --no-pr の理由が空でも受理する"
fresh_copy
inject scripts/plan.sh "        if not no_pr_reason:
            _usage_exit('--no-pr には理由 (空でない 1 行) が必要です')" "        if False:
            _usage_exit('x')"
expect_red "case F" "test_no_pr_needs_a_reason" "${DONE_TESTS[@]}"

echo "== case G: --pr と --no-pr の同時指定を受理する"
fresh_copy
inject scripts/plan.sh "        if pr_number is not None:
            _usage_exit(\"--pr と --no-pr は同時に指定できません\")" "        if False:
            _usage_exit('x')"
expect_red "case G" "test_pr_and_no_pr_together_are_refused" "${DONE_TESTS[@]}"

echo "== case H: pr_number の無い ready な codex-review を知らせない"
fresh_copy
inject scripts/dispatcher.sh "        notify_state_once(key, fp, 'no-pr-number', slug, task_id, build_msg,
                          director_live=director_live_for_state_notices)
        return" "        return"
expect_red "case H" "test_ready_codex_review_without_pr_number_is_told_to_the_director_once" "${DISP_TESTS[@]}"

echo "== case I: 状態が続くあいだ毎サイクル知らせる"
fresh_copy
inject scripts/dispatcher.sh "        fp = fingerprint('review_no_pr')" "        fp = fingerprint('review_no_pr', time.time())"
expect_red "case I" "test_ready_codex_review_without_pr_number_is_told_to_the_director_once" "${DISP_TESTS[@]}"

echo "== case J: pr_number があっても知らせて spawn を止める"
fresh_copy
inject scripts/dispatcher.sh "    if not meta.get('pr_number'):
        # t036" "    if True:
        # t036"
expect_red "case J" "test_no_pr_notice_is_not_sent_when_pr_number_is_set" "${DISP_TESTS[@]}"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
