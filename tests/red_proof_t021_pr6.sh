#!/usr/bin/env bash
# PR6 (mission 20260926-mechanize-guards-a / t021) の回帰テストが
# 「欠陥を戻すと赤くなる」ことの実証。
#
# 使い方:
#     bash tests/red_proof_t021_pr6.sh        # 全部で 6〜8 分 (待機の上限を試す変異があるため)
#
# 使い捨てのコピーに 1 つずつ欠陥を注入し、その欠陥を見張るはずのテストが赤になる
# (= 失敗する) ことを確かめる。緑のままなら、そのテストは欠陥の留め金になっていない
# (memory: regression-test-must-prove-red / red-proof-catches-tests-green-for-the-wrong-reason)。
#
#   --- plan.sh pull: 前任の後始末待ち ---
#   M1  predecessor_cleanup_pending が常に False        → 「待って取り直す」が赤
#   M2  待機をキューロックの中で行う                     → 「待機中もロックを握らない」が赤
#   M3  待機に上限が無い                                 → 「上限で拒否に戻る」が赤
#   M4  死んだ pid の判定が常に True                     → 「pid が生きていれば待たない」が赤
#   M5  phase を見ない                                   → 「terminated でなければ待たない」が赤
#   M6  待った後に判定 (`_do`) をやり直さない            → 「待って取り直す」が赤
#   --- watchdog の timeout 通知 ---
#   M7  台帳を見ない (何度でも送る)                       → 「同じ退役は 2 度告げない」が赤
#   M8  送れなかったのに台帳に書く                        → 「Director 不在 → 戻ったら 1 通」が赤
#   M9  timeout 用の通知を使わず従来の 1 通に戻す          → 「必要な情報つき」が赤
#   M10 再送しない                                        → 「Director 不在 → 戻ったら 1 通」が赤
#   M11 fingerprint が退役ごとでなく定数                   → 「同じ task の 2 度目は新しい事象」が赤
#   M12 台帳の書き込みがロックなし                         → 「2 人の書き手のエントリが消えない」が赤
#   M13 dispatcher の prune が timeout 通知を捨てる         → 「TTL 内の timeout 通知を残す」が赤
#   M14 watchdog が request に detail を渡さない           → 「detail を渡している」(構造) が赤
#
# 隔離: 本番の worktree・registry・queue・mux には触れない (使い捨てのコピーの中だけで変異
# させ、pytest は herdr の無い env -i で走らせる)。
# $PYTHONDONTWRITEBYTECODE=1: 欠陥注入は .pyc を通して古い姿を拾わせない
# (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t021pr6.XXXXXX")"
export PYTHONDONTWRITEBYTECODE=1

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

# 本番を汚さない: herdr の無い env -i。-k で対象のテストだけに絞る (待機の上限を試す変異は遅い)。
run_pytest() {   # <-k expression>
  (cd "$WORK/tree" && env -i PATH="$PATH" HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
     CREWVIA_HERDR_SOCK=/nonexistent/x.sock \
     python3 -m pytest tests/test_retirement_wait_and_timeout_notice.py -q --no-header \
       -p no:cacheprovider -k "$1" 2>&1)
}

expect_red() {   # <label> <output> <substring of a test name that must fail>
  local label="$1" out="$2" name="$3"
  if echo "$out" | grep -q "^FAILED .*${name}"; then
    ok "$label → ${name} が赤になった"
  else
    bad "$label → ${name} が赤にならなかった (欠陥の留め金になっていない)"
    echo "$out" | tail -6
  fi
}

inject() {       # <label> <file> <old> <new>
  mutate "$2" "$3" "$4" || { bad "$1 注入失敗"; return 1; }
}

echo "== baseline: 変異なしで緑"
fresh_copy
out="$(run_pytest "")"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed"; then ok "baseline は緑"; else bad "baseline が緑でない"; echo "$out" | tail -8; fi

# --------------------------------------------------------------------------
echo "== M1: predecessor_cleanup_pending が常に False"
fresh_copy
inject M1 scripts/plan.sh "    if not agent or agent_name_problem(agent):
        return False
    root = os.environ.get('CREWVIA_REPO_ROOT') or REPO_ROOT
    base = os.path.join(root, 'registry', 'retirements')

    def _load(suffix):" "    return False
    root = os.environ.get('CREWVIA_REPO_ROOT') or REPO_ROOT
    base = os.path.join(root, 'registry', 'retirements')

    def _load(suffix):" && {
  out="$(run_pytest "waits_for_the_dead or does_not_hold_the_queue_lock or rejudges")"
  expect_red M1 "$out" "test_pull_waits_for_the_dead_predecessors_cleanup_and_then_takes_the_task"
  expect_red M1 "$out" "test_waiting_pull_does_not_hold_the_queue_lock"
  expect_red M1 "$out" "test_pull_rejudges_from_the_top_after_waiting"
}

echo "== M2: 待機をキューロックの中で行う"
fresh_copy
inject M2 scripts/plan.sh "        reserved = retirement_reservation(agent)
        if reserved:
            diag['reason'] = 'retirement_reserved'" "        reserved = retirement_reservation(agent)
        if reserved:
            if predecessor_cleanup_pending(agent):
                time.sleep(6)
            diag['reason'] = 'retirement_reserved'" && {
  out="$(run_pytest "does_not_hold_the_queue_lock")"
  expect_red M2 "$out" "test_waiting_pull_does_not_hold_the_queue_lock"
}

echo "== M3: 待機に上限が無い"
fresh_copy
inject M3 scripts/plan.sh "        if time.monotonic() >= wait_deadline or not predecessor_cleanup_pending(agent):" "        if not predecessor_cleanup_pending(agent):" && {
  out="$(run_pytest "stops_waiting_at_the_limit")"
  expect_red M3 "$out" "test_pull_stops_waiting_at_the_limit_and_refuses"
}

echo "== M4: 死んだ pid の判定が常に True (生きている pid を後始末待ちと読む)"
fresh_copy
inject M4 scripts/plan.sh "    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False" "    return True" && {
  out="$(run_pytest "recorded_pid_is_still_alive")"
  expect_red M4 "$out" "test_pull_does_not_wait_when_the_recorded_pid_is_still_alive"
}

echo "== M5: phase を見ない (kill がまだでも待つ)"
fresh_copy
inject M5 scripts/plan.sh "    if prog is None or prog.get('phase') != 'terminated':
        return False" "    if prog is None:
        return False" && {
  out="$(run_pytest "still_refuses_at_once")"
  expect_red M5 "$out" "test_pull_still_refuses_at_once_when_the_predecessor_is_not_proven_gone"
}

echo "== M6: 待ったあとに判定をやり直さず、もう一度だけ待つ (= _do を呼ばない)"
fresh_copy
inject M6 scripts/plan.sh "    while True:
        with_lock(_do)
        if chosen_holder[0] is not None or diag['reason'] != 'retirement_reserved':
            break" "    first = True
    while True:
        if first:
            with_lock(_do)
            first = False
        if chosen_holder[0] is not None or diag['reason'] != 'retirement_reserved':
            break" && {
  out="$(run_pytest "waits_for_the_dead or rejudges")"
  expect_red M6 "$out" "test_pull_waits_for_the_dead_predecessors_cleanup_and_then_takes_the_task"
}

# --------------------------------------------------------------------------
echo "== M7: 台帳を見ない (何度でも送る)"
fresh_copy
inject M7 scripts/watchdog.py "        if told_matches(told, key, fp):
            return True" "        if False:
            return True" && {
  out="$(run_pytest "never_told_twice")"
  expect_red M7 "$out" "test_the_same_retirement_is_never_told_twice"
}

echo "== M8: 送れなかったのに台帳に書く"
fresh_copy
inject M8 scripts/watchdog.py "        if not send(message):
            return False" "        send(message)" && {
  out="$(run_pytest "unreachable_director")"
  expect_red M8 "$out" "test_an_unreachable_director_is_retried_until_it_hears_once"
}

echo "== M9: timeout 用の通知を使わず従来の 1 通に戻す"
fresh_copy
inject M9 scripts/lib_retirement.py "        if reason != \"timeout\" or self.notify_once is None:
            return False" "        return False" && {
  out="$(run_pytest "tells_the_director_once_with_what or idle_timeout_names")"
  expect_red M9 "$out" "test_timeout_termination_tells_the_director_once_with_what_it_needs"
  expect_red M9 "$out" "test_idle_timeout_names_the_idle_limit"
}

echo "== M10: 再送しない"
fresh_copy
inject M10 scripts/lib_retirement.py "        self._retry_owed_notices()

        agents = list_agents" "        agents = list_agents" && {
  out="$(run_pytest "unreachable_director")"
  expect_red M10 "$out" "test_an_unreachable_director_is_retried_until_it_hears_once"
}

echo "== M11: fingerprint が退役ごとでなく定数"
fresh_copy
inject M11 scripts/lib_retirement.py "        fp = str((req or {}).get(\"request_id\") or prog.get(\"request_id\") or f\"gen:{generation}\")" "        fp = \"constant\"" && {
  out="$(run_pytest "second_timeout")"
  expect_red M11 "$out" "test_a_second_timeout_of_the_same_task_is_a_new_event_and_is_told"
}

echo "== M12: 台帳の書き込みがロックなし"
fresh_copy
inject M12 scripts/lib_daemon_state.py "            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = True
                    break" "            deadline = time.monotonic() + wait
            while True:
                try:
                    held = True
                    break" && {
  out="$(run_pytest "concurrent_ledger or busy_lock")"
  expect_red M12 "$out" "test_concurrent_ledger_writers_do_not_lose_each_others_entries"
  expect_red M12 "$out" "test_told_lock_reports_failure_instead_of_blocking_on_a_busy_lock"
}

echo "== M13: dispatcher の prune が TTL 内の timeout 通知も捨てる"
fresh_copy
inject M13 scripts/dispatcher.sh " and k not in live_keys and not told_is_fresh_timeout(e)]" " and k not in live_keys]" && {
  out="$(run_pytest "prune_keeps_fresh_timeout")"
  expect_red M13 "$out" "test_dispatcher_prune_keeps_fresh_timeout_notices_and_drops_stale_ones"
}

echo "== M14: watchdog が request に detail を渡さない"
fresh_copy
inject M14 scripts/watchdog.py "                            detail=timeout_detail(monitor, detail, elapsed),
" "" && {
  out="$(run_pytest "hands_the_detail")"
  expect_red M14 "$out" "test_the_watchdog_terminate_branch_hands_the_detail_to_the_request"
}

echo
echo "結果: OK=$PASS BAD=$FAIL"
[ "$FAIL" -eq 0 ]
