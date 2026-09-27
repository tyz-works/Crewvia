#!/usr/bin/env bash
# 割り当ての機械照合 (t009 / backlog #21 #22 #24) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t009.sh
#
#   baseline — いまの木では tests/test_assignment_routing.py と start-sh の bats が緑
#   A  pull --task: target_dir の照合を外す                                → 赤
#   B  pull --task: 二重割り当ての拒否を外す                               → 赤
#   C  二重割り当て: 孤児の assignment も「持っている」に数える (#13 の再発) → 赤
#   D  二重割り当て: needs_director の card も「持っている」に数える         → 赤
#   E  done --pr: 設定済みの pr_number を上書きする                        → 赤
#   F  done --pr: blocked の codex-review を pending に戻さない            → 赤
#   G  lint: blocked を受理しない                                          → 赤
#   H  dispatcher: TARGET_DIR の照合を外す (元の欠陥 #21)                    → 赤
#   I  dispatcher: 送信済み・pull 待ちの Worker への二重送信 (元の欠陥 #22)   → 赤
#   J  dispatcher: 起動コマンドを Director への通知に載せない                → 赤
#   K  判定表: null の task も「記録が無い Worker」に回さない                → 赤
#   L  掃除: 古さを見ず、窓の無い記録を全部消す                            → 赤
#   M  掃除: 窓の一覧が空でも全部消す                                      → 赤
#   N  start.sh: 記録を書かない                                            → 赤 (bats)
#   O  start.sh: 起動を試す前に記録を書く (断られた起動が記録を上書きする)   → 赤 (bats)
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# 触れうる mux は、pytest 側はフェイク (FakeMux)、bats 側は fake tmux + 使い捨ての checkout の複製で、
# 本物の herdr / tmux / 本番 registry・queue には届かない (CREWVIA_HERDR_SOCK も存在しないパスにする)。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (rm -rf を使わない。終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t009.XXXXXX")"
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
run_py() {
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_assignment_routing.py -q -p no:cacheprovider 2>&1 )
}
run_bats() {  # run_bats <bats ファイル>
    ( cd "$TREE" && env CREWVIA_HERDR_SOCK="$WORK/no-such-herdr.sock" bats "$1" 2>&1 )
}

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local out; out="$(run_py)"
    if echo "$out" | grep -q "FAILED .*$2"; then ok "$1 → 赤 ($2)"
    else ng "$1 → 赤にならなかった ($2)"; echo "$out" | tail -8; fi
}
expect_red_bats() {  # expect_red_bats <case名> <bats ファイル> <赤になる @test 名の断片>
    local out; out="$(run_bats "$2")"
    if echo "$out" | grep -q "^not ok .*$3"; then ok "$1 → 赤 ($3)"
    else ng "$1 → 赤にならなかった ($3)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_py)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then ok "baseline (pytest) は緑"
else ng "baseline (pytest) が緑でない"; echo "$out" | tail -8; fi
for f in tests/start-sh-target-dir-record.bats tests/start-sh-spawn-refusal.bats; do
    out="$(run_bats "$f")"
    if ! echo "$out" | grep -q "^not ok" && echo "$out" | grep -q "^ok"; then ok "baseline ($f) は緑"
    else ng "baseline ($f) が緑でない"; echo "$out" | tail -8; fi
done

echo "== case A: pull --task の target_dir 照合を外す"
fresh_copy
inject scripts/plan.sh "                    task_td = meta.get('target_dir') or None
                    if task_td != effective_target:" "                    task_td = meta.get('target_dir') or None
                    if False:"
expect_red "case A" "TestPullTargetDir"

echo "== case B: 二重割り当ての拒否を外す"
fresh_copy
inject scripts/plan.sh "busy = agent_busy_elsewhere(agent, slug, specific_task, slugs)" "busy = None"
expect_red "case B" "TestPullDoubleAssignment"

echo "== case C: 孤児の assignment も持っている扱い"
fresh_copy
inject scripts/plan.sh "            if status in released or status == CORRUPT_TASK_STATUS:
                return None" "            if False:
                return None"
expect_red "case C" "test_orphan_assignment_pointing_at_a_released_task_is_overwritten"

echo "== case D: needs_director の card も持っている扱い"
fresh_copy
inject scripts/plan.sh "meta.get('worker') == agent and meta.get('status') == 'in_progress'" "meta.get('worker') == agent and meta.get('status') not in ('done', 'pending')"
expect_red "case D" "test_needs_director_card_alone_does_not_block_a_reviewer"

echo "== case E: 設定済みの pr_number を上書きする"
fresh_copy
inject scripts/plan.sh "        if meta.get('pr_number') not in (None, ''):
            continue
        meta['pr_number'] = pr_number" "        meta['pr_number'] = pr_number"
expect_red "case E" "test_an_existing_pr_number_is_not_overwritten"

echo "== case F: blocked の codex-review を pending に戻さない"
fresh_copy
inject scripts/plan.sh "            meta['status'] = 'pending'
            meta.pop('blocked_reason', None)" "            meta.pop('blocked_reason', None)"
expect_red "case F" "test_pr_number_reaches_codex_review_and_unblocks_it"

echo "== case G: lint が blocked を受理しない"
fresh_copy
inject scripts/lint_plan.py "    'blocked',
}" "}"
expect_red "case G" "TestLintBlocked"

echo "== case H: dispatcher が TARGET_DIR を照合しない"
fresh_copy
inject scripts/dispatcher.sh "                may_take, why_not = worker_may_take_task(agent_name, meta)
                if not may_take:" "                may_take, why_not = (True, '')
                if not may_take:"
expect_red "case H" "TestDispatcherRouting.*test_target_worker_is_not_given_a_crewvia_local_task"

echo "== case I: 送信済み・pull 待ちの Worker に別の task を送る"
fresh_copy
inject scripts/dispatcher.sh "outstanding = worker_outstanding_assignment(agent_name, all_tasks)" "outstanding = None"
expect_red "case I" "test_task_sent_but_not_yet_pulled_blocks_a_second_task"

echo "== case J: Director への通知に起動コマンドを載せない"
fresh_copy
inject scripts/dispatcher.sh "                    + worker_start_command(task_skills, meta.get('target_dir'), fresh=bool(skill_ok))" "                    + ''"
expect_red "case J" "test_no_worker_request_carries_a_pasteable_start_command"

echo "== case K: 記録の無い Worker に null の task も回さない"
fresh_copy
inject scripts/lib_worker_target.py "    if task_target is None:
        if not known:
            return True, ''" "    if task_target is None:
        if not known:
            return False, 'record missing'"
expect_red "case K" "test_worker_without_a_record_still_gets_crewvia_local_tasks"

echo "== case L: 掃除が古さを見ない"
fresh_copy
inject scripts/lib_worker_target.py "        if age < SWEEP_MIN_AGE_SECONDS:
            continue" "        if False:
            continue"
expect_red "case L" "test_a_successors_fresh_record_survives"

echo "== case M: 窓の一覧が空でも掃除する"
fresh_copy
inject scripts/lib_worker_target.py "    if not live_agents:
        return removed" "    if False:
        return removed"
expect_red "case M" "test_an_empty_window_list_sweeps_nothing"

echo "== case N: start.sh が記録を書かない"
fresh_copy
inject scripts/start.sh "    python3 \"\${SCRIPT_DIR}/lib_worker_target.py\" record" "    true \"\${SCRIPT_DIR}/lib_worker_target.py\" record"
expect_red_bats "case N" tests/start-sh-target-dir-record.bats "a Worker launched with TARGET_DIR records the canonical path"

echo "== case O: start.sh が起動を試す前に記録を書く"
fresh_copy
inject scripts/start.sh "  SPAWN_RC=0
  mux_spawn \"\$WINDOW_NAME\"" "  python3 \"\${SCRIPT_DIR}/lib_worker_target.py\" record \"\${REPO_ROOT}/registry\" \"\$AGENT_NAME\" \"\${TARGET_DIR:-}\" >/dev/null 2>&1 || true
  SPAWN_RC=0
  mux_spawn \"\$WINDOW_NAME\""
expect_red_bats "case O" tests/start-sh-spawn-refusal.bats "a refused launch does not overwrite the live Worker's TARGET_DIR record"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
