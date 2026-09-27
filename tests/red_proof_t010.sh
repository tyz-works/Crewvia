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
#   --- t026 (PR #214 の Kai 3 巡目 P2 x2: 台帳の値検証を構造で閉じる / kai-review の mission 解決) ---
#   M15 台帳の各エントリの形を検証しない          → 「壊れたエントリで落ちない・再送側」が赤
#   M16 拒否記録の値を検証しない (入口経由)       → 「不正な値の記録は保留」が赤 (M8 の t026 版)
#   M17 kai-review が --mission 省略を解決しない  → scripts/test_kai_review.sh の t026 が赤
#   M18 resolve-mission が pull と別の順で探す     → 「resolve-mission = pull が選んだ mission」が赤
#   M18b resolve-mission が探索順を自前で持つ      → 「pull と resolve-mission は探索順を共有」(構造) が赤
#   M19 通知スロットルの値を検証しない             → 「NaN/文字列/未来のスロットルで落ちない・遮らない」が赤
#   M20 Rule 5 の状態の値を検証しない              → 「壊れた Rule 5 の状態で落ちない」が赤
#   M21 相互監視の watch 状態の値を検証しない       → 「壊れた watch 状態で落ちない」が赤
#   M22 respawn 記録の object でないエントリを落とし漏れる → 「壊れたエントリは捨てる」が赤
#   M23 【構造】dispatcher.sh の埋め込み python の load_told を外側検証だけの旧形に戻す
#                                                 → 「入口を通さない json.loads が増えた」が赤
#   M24 【構造】別のモジュール (lib_daemon_watch) にストアの直接読み取りを 1 つ注入 → 同上が赤
#   M25 入口が check を無視する                    → 入口の単体・台帳・スロットルが赤
#   M26 入口が JSON の ValueError しか捕まえない    → 「深いネストでも落ちない」が赤
#   M27 台帳エントリの欄の型を見ない               → 「fp が dict / slug が list …」が赤
#   M29 数の検証が NaN / inf を通す                → 「NaN のスロットル・watch 状態」が赤
#   M30 スロットルの「未来 / 負」を通す            → 「遠い未来・負のスロットル」が赤
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
# t026: 入口の単体・構造・「壊れたストアで倒れる向き」も一緒に走らせる。
run_pytest_t026() {
  (cd "$WORK/tree" && python3 -m pytest tests/test_dispatcher_notify_once.py \
     tests/test_daemon_state_fail_direction.py tests/test_daemon_state_reads_go_through_the_entry.py \
     tests/test_queue_reads_go_through_the_guard.py -q --no-header -p no:cacheprovider 2>&1); }

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
mutate scripts/lib_review_refusal.py "check=lambda d: _invalid_reason(d, mission, task)," "check=None," || bad "M8 注入失敗"
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

# ---------------------------------------------------------------------------
# t026: 同じ根 (読めた JSON の「中身の形」を確かめずに使う) を構造で閉じた。
# ---------------------------------------------------------------------------

echo "== baseline (t026): 変異なしで、入口・構造・倒れる向きのテストが緑"
fresh_copy
out="$(run_pytest_t026)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed"; then ok "baseline (t026) は緑"; else bad "baseline (t026) が緑でない"; echo "$out" | tail -8; fi

echo "== M15: 台帳の各エントリの形を検証しない (外側だけ検証する旧形。Kai 3 巡目 P2 の根)"
fresh_copy
mutate scripts/dispatcher.sh "    told = load_json_store(TOLD_FILE, check=told_ledger_problem)" "    told = load_json_store(TOLD_FILE)" || bad "M15 注入失敗"
out="$(run_pytest_t026)"
expect_red M15 "$out" "test_a_malformed_ledger_never_crashes_the_cycle_and_resends"
expect_red M15 "$out" "test_a_bad_entry_beside_a_told_one_does_not_silence_the_told_state"
expect_red M15 "$out" "test_the_store_reader_passes_a_shape_validator"

echo "== M16: 拒否記録の値を検証しない (入口へ渡す check を外す。M8 の t026 版)"
fresh_copy
mutate scripts/lib_review_refusal.py "check=lambda d: _invalid_reason(d, mission, task)," "check=None," || bad "M16 注入失敗"
out="$(run_pytest_t026)"
expect_red M16 "$out" "test_invalid_refusal_values_hold_the_spawn_and_do_not_crash_the_cycle"
expect_red M16 "$out" "test_refusal_load_rejects_invalid_field_values"

echo "== M17: kai-review が --mission 省略を解決しない (解決した値を捨てる = 元の欠陥)"
fresh_copy
mutate scripts/kai-review.sh "    MISSION_SLUG=\"\$RESOLVED_MISSION\"" "    :" || bad "M17 注入失敗"
kout="$(cd "$WORK/tree" && timeout 900 bash scripts/test_kai_review.sh 2>&1)"
if echo "$kout" | grep -q "REGRESSION (t026): omitted --mission"; then
  ok "M17 → scripts/test_kai_review.sh の t026 (--mission 省略) が赤になった"
else
  bad "M17 → t026 のテストが赤にならなかった"
  echo "$kout" | tail -5
fi

echo "== M18: resolve-mission が pull と別の順で探す (食い違う)"
fresh_copy
mutate scripts/plan.sh "    slugs = mission_search_order(opts.get('--mission'), state)
    for slug in slugs:" "    slugs = list(reversed(mission_search_order(opts.get('--mission'), state)))
    for slug in slugs:" || bad "M18 注入失敗"
kout="$(cd "$WORK/tree" && timeout 900 bash scripts/test_kai_review.sh 2>&1)"
if echo "$kout" | grep -q "REGRESSION (t026): resolve-mission / pull / record disagree"; then
  ok "M18 → resolve-mission と pull の食い違いが scripts/test_kai_review.sh で赤になった"
else
  bad "M18 → 食い違いが赤にならなかった"
  echo "$kout" | tail -5
fi

echo "== M18b: resolve-mission が探索順を自前で持つ (今は偶然一致している = 振る舞いでは見えない)"
fresh_copy
mutate scripts/plan.sh "    slugs = mission_search_order(opts.get('--mission'), state)
    for slug in slugs:" "    slugs = [opts['--mission']] if opts.get('--mission') else list(state.get('active_missions') or [])
    for slug in slugs:" || bad "M18b 注入失敗"
out="$(run_pytest_t026)"
expect_red M18b "$out" "test_pull_and_resolve_mission_share_one_search_order"

echo "== M19: 通知スロットルの値を検証しない"
fresh_copy
mutate scripts/dispatcher.sh "load_json_store(NOTIFY_CACHE, check=notify_cache_problem)" "load_json_store(NOTIFY_CACHE)" || bad "M19 注入失敗"
out="$(run_pytest_t026)"
expect_red M19 "$out" "test_a_malformed_throttle_cache_does_not_crash_or_silence"
expect_red M19 "$out" "test_the_store_reader_passes_a_shape_validator"

echo "== M20: Rule 5 の状態の値を検証しない"
fresh_copy
mutate scripts/dispatcher.sh "        _state_json_path(name), check=rule5_state_problem," "        _state_json_path(name)," || bad "M20 注入失敗"
out="$(run_pytest_t026)"
expect_red M20 "$out" "test_a_malformed_rule5_state_entry_means_the_grace_restarts"

echo "== M21: 相互監視の watch 状態の値を検証しない"
fresh_copy
mutate scripts/lib_daemon_watch.py "watch_state_path(self.registry_dir, self.peer_name),
                               check=watch_state_problem)" "watch_state_path(self.registry_dir, self.peer_name))" || bad "M21 注入失敗"
out="$(run_pytest_t026)"
expect_red M21 "$out" "test_a_malformed_watch_state_reads_as_the_defaults_not_a_crash"

echo "== M22: respawn 記録の object でないエントリを落とし漏れる (元の AttributeError)"
fresh_copy
mutate scripts/lib_daemon_watch.py "            if not isinstance(entry, dict) or not is_finite_number(entry.get(\"at\")):
                continue" "            if False:
                continue" || bad "M22 注入失敗"
out="$(run_pytest_t026)"
expect_red M22 "$out" "test_a_malformed_respawn_entry_is_dropped_not_a_crash"

echo "== M23: 【構造】dispatcher.sh の埋め込み python の load_told を、外側検証だけの旧形に戻す"
fresh_copy
mutate scripts/dispatcher.sh "    told = load_json_store(TOLD_FILE, check=told_ledger_problem)
    if is_missing(told):
        return {}
    if is_unreadable(told):
        _told_trouble(f\"{TOLD_FILE} を使えない ({told.reason})\")
    return told" "    text = read_regular_text_or_unreadable(TOLD_FILE)
    if is_missing(text):
        return {}
    if is_unreadable(text):
        return text
    return json.loads(text)" || bad "M23 注入失敗"
out="$(run_pytest_t026)"
expect_red M23 "$out" "test_no_json_parse_bypasses_the_entry"
expect_red M23 "$out" "test_the_store_reader_goes_through_the_entry"

echo "== M24: 【構造】別のモジュール (lib_daemon_watch) にストアの直接読み取りを 1 つ注入"
fresh_copy
mutate scripts/lib_daemon_watch.py "def read_pause(registry_dir, name: str) -> Optional[dict]:" "def _peek_heartbeat(registry_dir, name):
    return json.loads(heartbeat_path(registry_dir, name).read_text())


def read_pause(registry_dir, name: str) -> Optional[dict]:" || bad "M24 注入失敗"
out="$(run_pytest_t026)"
expect_red M24 "$out" "test_no_json_parse_bypasses_the_entry"

echo "== M25: 入口が check を無視する"
fresh_copy
mutate scripts/lib_daemon_state.py "    if check is not None:" "    if False:" || bad "M25 注入失敗"
out="$(run_pytest_t026)"
expect_red M25 "$out" "test_a_failing_validator_means_unreadable_not_a_crash"
expect_red M25 "$out" "test_a_malformed_ledger_never_crashes_the_cycle_and_resends"

echo "== M26: 入口が JSON の ValueError しか捕まえない"
fresh_copy
mutate scripts/lib_daemon_state.py "    except Exception as e:  # noqa: BLE001 — ValueError 以外 (RecursionError 等) も同じ扱い" "    except ValueError as e:" || bad "M26 注入失敗"
out="$(run_pytest_t026)"
expect_red M26 "$out" "test_unparseable_is_unreadable_not_missing_and_never_raises"

echo "== M27: 台帳エントリの欄の型を見ない"
fresh_copy
mutate scripts/lib_daemon_state.py "        if not isinstance(value, str) or not value:
            return f'entry {key!r}: field" "        if False:
            return f'entry {key!r}: field" || bad "M27 注入失敗"
out="$(run_pytest_t026)"
expect_red M27 "$out" "test_a_malformed_ledger_is_unreadable_as_a_whole"
expect_red M27 "$out" "test_a_malformed_ledger_never_crashes_the_cycle_and_resends"

echo "== M29: 数の検証が NaN / inf を通す"
fresh_copy
mutate scripts/lib_daemon_state.py "            and math.isfinite(v))" "            and True)" || bad "M29 注入失敗"
out="$(run_pytest_t026)"
expect_red M29 "$out" "test_a_malformed_throttle_cache_does_not_crash_or_silence"
expect_red M29 "$out" "test_a_malformed_watch_state_reads_as_the_defaults_not_a_crash"

echo "== M30: スロットルの「遠い未来 / 負」を通す"
fresh_copy
mutate scripts/lib_daemon_state.py "        if value < 0 or value > now + FUTURE_SLACK_SECONDS:" "        if False:" || bad "M30 注入失敗"
out="$(run_pytest_t026)"
expect_red M30 "$out" "test_a_malformed_throttle_cache_does_not_crash_or_silence"

echo
echo "== 結果: OK=$PASS BAD=$FAIL"
[[ $FAIL -eq 0 ]]
