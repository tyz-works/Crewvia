#!/usr/bin/env bash
# t010 (#10 + #11) の回帰テストが「欠陥を戻すと赤くなる」ことの実証。
#
# 使い方:
#     bash tests/red_proof_t010.sh
#
# 作業用のコピーに 1 つずつ欠陥を注入し、その欠陥を見張るはずのテストが赤になる
# (= 失敗する) ことを確かめる。緑のままなら、そのテストは欠陥の留め金になって
# いない (memory: regression-test-must-prove-red / red-proof-catches-tests-green-for-the-wrong-reason)。
#
#   M1  already_told が常に False            → 「TTL 後も 1 回だけ」が赤
#   M2  fingerprint が定数                    → 「入力が変わったら再通知」が赤
#   M3  prune_told が何もしない               → 「状態を離れて戻ったら再通知」が赤
#   M4  拒否記録を見ない (常に 'none')         → 「拒否済みは再 spawn しない」が赤
#   M5  読めない拒否記録を 'none' に倒す        → 「壊れた記録は spawn を保留」が赤
#   M6  台帳に書けない/壊れているときに黙る     → 「使えない台帳を声に出す」が赤
#   M7  kai-review.sh が拒否記録を書かない      → scripts/test_kai_review.sh が赤
#   --- t021 (PR #214 の Kai 指摘 P2 x2 + QA t011 の P3) ---
#   M8  拒否記録の値を検証しない                → 「不正な値の記録は保留・サイクルを落とさない」が赤
#   M9  離脱時にスロットルを捨てない            → 「離脱→再入 (キャッシュを消さない)」が赤
#   M10 fingerprint が変わってもスロットルを残す → 「A → B → A」が赤
#   M11 Director 生存確認を先に無条件で呼ぶ      → 「idle サイクルで mux を叩かない」が赤
#   M12 Director 生存確認をサイクル内で使い回さない → 「サイクル内 1 回」が赤
#   --- t023 (PR #214 の Kai 2 巡目 P2: 観測できなかったときは台帳とスロットルを捨てない) ---
#   M13 handoff 検知が mission を走査し直す     → 「検知と pruning は 1 つのスナップショット」が赤
#   M14 prune_told が観測の可否を見ない          → 「破損カード / 走査失敗で台帳とスロットルが残る」が赤
#
# 隔離: 本番の worktree には触らない (使い捨てのコピーの中だけで変異させる)。
# $PYTHONDONTWRITEBYTECODE=1: 欠陥注入は .pyc を通して古い姿を拾わせない
# (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t010.XXXXXX")"
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

# mutate <file> <python-expression-over-s>  — 置換できなかったら注入失敗として止まる。
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

run_pytest() { (cd "$WORK/tree" && python3 -m pytest tests/test_dispatcher_notify_once.py -q --no-header -p no:cacheprovider 2>&1); }

expect_red() {   # <label> <output> <substring of a test name that must fail>
  local label="$1" out="$2" name="$3"
  if echo "$out" | grep -q "^FAILED .*${name}"; then
    ok "$label → ${name} が赤になった"
  else
    bad "$label → ${name} が赤にならなかった (欠陥の留め金になっていない)"
    echo "$out" | tail -5
  fi
}

echo "== baseline: 変異なしで緑"
fresh_copy
out="$(run_pytest)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed"; then ok "baseline は緑"; else bad "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== M1: already_told が常に False"
fresh_copy
mutate scripts/dispatcher.sh "    if is_unreadable(told):
        return False
    entry = told.get(key)" "    return False
    entry = told.get(key)" || { bad "M1 注入失敗"; }
out="$(run_pytest)"
expect_red M1 "$out" "test_needs_director_is_told_once_even_after_the_throttle_expires"
expect_red M1 "$out" "test_handoff_is_told_once_even_after_the_throttle_expires"

echo "== M2: fingerprint が定数"
fresh_copy
mutate scripts/dispatcher.sh "    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]" "    return 'constant'" || bad "M2 注入失敗"
out="$(run_pytest)"
expect_red M2 "$out" "test_needs_director_is_told_again_when_the_reason_changes"
expect_red M2 "$out" "test_handoff_is_told_again_when_the_handoff_path_changes"

echo "== M3: prune_told が何もしない"
fresh_copy
mutate scripts/dispatcher.sh "    told = load_told()
    if is_unreadable(told):
        return
    stale =" "    return
    stale =" || bad "M3 注入失敗"
out="$(run_pytest)"
expect_red M3 "$out" "test_needs_director_left_and_re_entered_with_same_reason_is_told_again"
expect_red M3 "$out" "test_handoff_left_and_re_entered_is_told_again"

echo "== M4: 拒否記録を見ない"
fresh_copy
mutate scripts/dispatcher.sh "    task_id = meta.get('id', '?')
    try:
        rec = lib_review_refusal.load(" "    return 'none', None
    task_id = meta.get('id', '?')
    try:
        rec = lib_review_refusal.load(" || bad "M4 注入失敗"
out="$(run_pytest)"
expect_red M4 "$out" "test_a_refused_codex_review_task_is_not_respawned"
expect_red M4 "$out" "test_refusal_tells_the_director_to_review_by_hand_once"

echo "== M5: 読めない拒否記録を 'none' に倒す"
fresh_copy
mutate scripts/dispatcher.sh "    if is_unreadable(rec):
        return 'unreadable', rec" "    if is_unreadable(rec):
        return 'none', None" || bad "M5 注入失敗"
out="$(run_pytest)"
expect_red M5 "$out" "test_unreadable_refusal_holds_the_spawn"

echo "== M6: 使えない台帳を黙って読み飛ばす (声に出さない)"
fresh_copy
mutate scripts/dispatcher.sh "    if should_notify('told_ledger_trouble'):" "    if False:" || bad "M6 注入失敗"
out="$(run_pytest)"
expect_red M6 "$out" "test_unwritable_store_degrades_to_the_throttle_and_says_so"
expect_red M6 "$out" "test_corrupt_store_is_reported_then_repaired_by_the_next_successful_record"

echo "== M7: kai-review.sh が拒否記録を書かない"
fresh_copy
mutate scripts/kai-review.sh "  if [[ \$DRY_RUN -eq 0 && -n \"\$MISSION_SLUG\" ]]; then" "  if false; then" || bad "M7 注入失敗"
kout="$(cd "$WORK/tree" && timeout 900 bash scripts/test_kai_review.sh 2>&1)"
if echo "$kout" | grep -q "REGRESSION (t010/#11)"; then
  ok "M7 → scripts/test_kai_review.sh の拒否記録テストが赤になった"
else
  bad "M7 → 拒否記録テストが赤にならなかった"
  echo "$kout" | tail -5
fi

echo "== M8: 拒否記録の値を検証しない (欄が在れば受理)"
fresh_copy
mutate scripts/lib_review_refusal.py "    problem = _invalid_reason(data, mission, task)" "    problem = None" || bad "M8 注入失敗"
out="$(run_pytest)"
expect_red M8 "$out" "test_invalid_refusal_values_hold_the_spawn_and_do_not_crash_the_cycle"
expect_red M8 "$out" "test_refusal_load_rejects_invalid_field_values"
expect_red M8 "$out" "test_a_refusal_record_for_another_task_is_not_accepted_as_this_ones"

echo "== M9: 状態を離れても、その key のスロットルを捨てない"
fresh_copy
mutate scripts/dispatcher.sh "    for k in stale:
        forget_notify(f'{k}#')" "    pass" || bad "M9 注入失敗"
out="$(run_pytest)"
expect_red M9 "$out" "test_needs_director_left_and_re_entered_with_same_reason_is_told_again"
expect_red M9 "$out" "test_handoff_left_and_re_entered_is_told_again"

echo "== M10: fingerprint が変わっても旧 fingerprint のスロットルを残す"
fresh_copy
mutate scripts/dispatcher.sh "        forget_notify(f'{key}#', keep=throttle_key)" "        pass" || bad "M10 注入失敗"
out="$(run_pytest)"
expect_red M10 "$out" "test_needs_director_reason_A_then_B_then_A_is_told_each_time"
expect_red M10 "$out" "test_handoff_path_A_then_B_then_A_is_told_each_time"

echo "== M11: Director 生存確認をサイクルの先頭で無条件に呼ぶ (遅延評価をやめる)"
fresh_copy
mutate scripts/dispatcher.sh "    _director_live_memo.clear()
    state = load_state()" "    _director_live_memo.clear()
    director_live_for_state_notices()
    state = load_state()" || bad "M11 注入失敗"
out="$(run_pytest)"
expect_red M11 "$out" "test_an_idle_cycle_does_not_ask_the_mux_whether_a_director_is_live"
expect_red M11 "$out" "test_a_cycle_with_only_already_told_states_does_not_ask_the_mux"

echo "== M12: Director 生存確認をサイクル内で使い回さない"
fresh_copy
mutate scripts/dispatcher.sh "    if not _director_live_memo:
        _director_live_memo.append" "    if True:
        _director_live_memo[:] = []
        _director_live_memo.append" || bad "M12 注入失敗"
out="$(run_pytest)"
expect_red M12 "$out" "test_liveness_is_looked_up_once_per_cycle_however_many_notices"

echo "== M13: handoff 検知が all_tasks ではなく mission を走査し直す (t021 が作った欠陥)"
fresh_copy
mutate scripts/dispatcher.sh "    for slug, meta in all_tasks:
        if meta.get('status') != 'failed':
            continue
        handoff_path = meta.get('handoff_path')" "    for slug, meta in [(_s, _m) for _s in active_missions for _m, _ in list_tasks_for_mission(_s)]:
        if meta.get('status') != 'failed':
            continue
        handoff_path = meta.get('handoff_path')" || bad "M13 注入失敗"
out="$(run_pytest)"
expect_red M13 "$out" "test_handoff_detection_and_pruning_share_one_snapshot"

echo "== M14: prune_told が「観測できた mission」で絞らない (観測不能を「離れた」と読む)"
fresh_copy
mutate scripts/dispatcher.sh "    prune_told(live_state_keys, observed_missions(all_tasks, active_missions))" "    prune_told(live_state_keys, set(active_missions))" || bad "M14 注入失敗"
out="$(run_pytest)"
expect_red M14 "$out" "test_handoff_ledger_and_throttle_survive_a_corrupt_card"
expect_red M14 "$out" "test_handoff_ledger_and_throttle_survive_a_scan_failure"

echo
echo "== 結果: OK=$PASS BAD=$FAIL"
[[ $FAIL -eq 0 ]]
