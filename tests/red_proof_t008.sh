#!/usr/bin/env bash
# lib_state_store (vNext 01a / S2 / t008) の保証を 1 つずつ壊した欠陥版で、該当テストが赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t008.sh
#
# lib が無い状態は当然赤なので、「lib の保証を 1 つずつ壊す」形で実証する (受入条件)。
#
#   baseline — いまの木では state_store のテストがすべて緑
#   A  ファイルの fsync を消す                       → test_parent_directory_is_fsynced_...
#   B  置換後の親 dir fsync を消す                    → 同上 / test_each_stage_failure_...
#   C  unlink 後の親 dir fsync を消す                 → test_parent_directory_is_fsynced_...
#   D  失敗時に tmp を消さない                        → test_each_stage_failure_...
#   E  既存 file の mode を引き継がない               → test_existing_mode_is_preserved
#   F  取得後の読み直しを消す (card を持ち越す)       → test_reads_inside_the_lock_... / concurrency
#   G  flock を取らない                               → test_nonblocking_busy_... / concurrency
#   H  入れ子を待つ (NestedTransaction を出さない)     → test_nested_transaction_raises_immediately_...
#   I  回復の冪等性を壊す (R-3 が毎回「修復」)         → test_r3_advances_... / crash injection (add)
#   J  回復の行を with の出口で書く (即時でない)        → test_recovery_rows_are_written_immediately_...
#   K  所有の証拠の走査を消す                         → test_r1_refused_when_owner_holds_two_...
#   L  R-2 を「holding でない status すべて」に広げる    → test_r2_does_not_touch_blocked_card_assignment
#   M  R-2 が逆引きをしない                           → test_reset_crash_..._reverse_lookup
#   N  state.yaml が読めないとき既定値に潰す           → test_state_missing_is_default_but_unreadable_...
#   O  監査ログの失敗で遷移を止める                    → test_audit_failure_does_not_stop_...
#   P  直列化の規則が plan.sh とずれる                  → test_serialize_card_matches_plan_sh_...
#   Q  R-1 が正本を書く (worker を勝手に補う)          → test_r1_never_writes_the_authoritative_card
#   R  audit に Result 本文を出す                      → test_body_row_fields_and_no_content_leak
#   S  dump_yaml が key_order の表を書き換える (plan.sh の写し) → test_lib_dump_yaml_does_not_mutate_...
#
# 隔離: 使い捨ての複製 (scripts/ tests/ だけ) で欠陥を注入する。本番の worktree・queue・registry には触れない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さない)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t008.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0; N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

FAST="tests/test_state_store_atomic.py tests/test_state_store_transaction.py tests/test_state_store_concurrency.py tests/test_state_store_serialization_matches_plan_sh.py"
CRASH="tests/test_state_store_crash_injection.py"
LIB="scripts/lib_state_store.py"

TREE=""
fresh_copy() {
    N=$((N + 1)); TREE="$WORK/tree$N"; mkdir -p "$TREE"
    rsync -a --exclude='__pycache__' "$REPO_ROOT/scripts" "$REPO_ROOT/tests" "$TREE/"
    find "$TREE" -name '__pycache__' -prune -exec true \; 2>/dev/null
}

# inject <file(TREE 相対)> <old> <new> — old を new に置換 (ちょうど 1 か所。無ければ FATAL)
inject() {
    OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない、または複数ある ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

run_suite() {   # run_suite <files...>
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 timeout 600 python3 -m pytest "$@" -p no:cacheprovider -q 2>&1 )
}

expect_red() {  # expect_red <case名> "<files>" <赤になるはずのテスト名の断片>
    local out; out="$(run_suite $2)"
    if echo "$out" | grep -q "FAILED .*$3"; then ok "$1 → 赤 ($3)"
    else ng "$1 → 赤にならなかった ($3)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_suite $FAST $CRASH)"
if echo "$out" | grep -qE "[0-9]+ passed" && ! echo "$out" | grep -qE "[0-9]+ (failed|error)"; then
    ok "baseline は緑 ($(echo "$out" | tail -1))"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== A: ファイルの fsync を消す"
fresh_copy
inject $LIB "            try:
                _sys_fsync(fd)
            except OSError as e:
                raise StoreWriteError(path, 'fsync', e.errno) from e" "            pass"
expect_red "A" "$FAST" "test_parent_directory_is_fsynced_after_replace_and_after_unlink"

echo "== B: 置換の後の親 dir fsync を消す"
fresh_copy
inject $LIB "        _fault('atomic:replaced', path)
        _fsync_dir(parent, path)" "        _fault('atomic:replaced', path)"
expect_red "B" "$FAST" "test_parent_directory_is_fsynced_after_replace_and_after_unlink"

echo "== C: unlink の後の親 dir fsync を消す"
fresh_copy
inject $LIB "    _fault('remove:unlinked', path)
    _fsync_dir(os.path.dirname(path) or '.', path)" "    _fault('remove:unlinked', path)"
expect_red "C" "$FAST" "test_parent_directory_is_fsynced_after_replace_and_after_unlink"

echo "== D: 失敗時に tmp を消さない"
fresh_copy
inject $LIB "        if not replaced:
            try:
                _sys_unlink(tmp)
            except OSError:
                pass
        raise" "        raise"
expect_red "D" "$FAST" "test_each_stage_failure_keeps_a_readable_file_and_leaves_no_tmp"

echo "== E: 既存 file の mode を引き継がない"
fresh_copy
inject $LIB "        final_mode = _stat.S_IMODE(os.stat(path).st_mode)" "        os.stat(path); final_mode = 0o644 if mode is None else mode"
expect_red "E" "$FAST" "test_existing_mode_is_preserved"

echo "== F: 取得後の読み直しを消す (card を持ち越す)"
fresh_copy
inject $LIB "        path = self.card_path(slug, tid)
        kind, meta, body, reason, err_no = self._read_card(path, tid)" "        path = self.card_path(slug, tid)
        _STALE = globals().setdefault('_STALE_CARDS', {})
        if path not in _STALE:
            _STALE[path] = self._read_card(path, tid)
        kind, meta, body, reason, err_no = _STALE[path]
        meta = dict(meta) if meta is not None else meta"
expect_red "F" "$FAST" "test_reads_inside_the_lock_see_what_a_previous_holder_wrote"

echo "== G: flock を取らない"
fresh_copy
inject $LIB "            fcntl.flock(fd, flags)" "            pass"
expect_red "G" "tests/test_state_store_transaction.py" "test_nonblocking_busy_raises_lockbusy_and_writes_nothing"
expect_red "G'" "tests/test_state_store_concurrency.py" "test_concurrent_read_modify_write_loses_no_update_and_never_corrupts"

echo "== H: 入れ子を待つ"
fresh_copy
inject $LIB "            if _HELD.get(key) == me:
                raise NestedTransaction(" "            if False:
                raise NestedTransaction("
expect_red "H" "tests/test_state_store_transaction.py" "test_nested_transaction_raises_immediately_instead_of_deadlocking"

echo "== I: 回復の冪等性を壊す (R-3 が毎回修復)"
fresh_copy
inject $LIB "        if not nums or max(nums) < nxt:
            return" "        if not nums:
            return"
expect_red "I" "$FAST" "test_r3_advances_next_task_id_and_reports_leftover"
expect_red "I'" "$CRASH" "test_crash_at_every_point_converges_after_next_lock.add"

echo "== J: 回復の行を with の出口で書く"
fresh_copy
inject $LIB "        self.t._append_audit(dict(
            op='recover', mission=mission, task=task, result=result, detail=detail,
            from_status=from_status, to_status=to_status, generation=generation), files)" "        self.t._records.append(dict(
            op='recover', mission=mission, task=task, result=result, detail=detail,
            from_status=from_status, to_status=to_status, generation=generation))"
expect_red "J" "$FAST" "test_recovery_rows_are_written_immediately_even_if_the_body_then_dies"

echo "== K: 所有の証拠の走査を消す"
fresh_copy
inject $LIB "        problem = self._ownership_problem(worker, slug, tid)" "        problem = None"
expect_red "K" "$FAST" "test_r1_refused_when_owner_holds_two_in_progress_cards_even_across_missions"

echo "== L: R-2 を holding でない status すべてに広げる"
fresh_copy
inject $LIB "                or (status == 'pending' and worker is None):" "                or status not in _ASSIGNMENT_HOLDING_STATUSES:"
expect_red "L" "$FAST" "test_r2_does_not_touch_blocked_card_assignment"

echo "== M: R-2 が逆引きをしない"
fresh_copy
inject $LIB "            agents.extend(self._reverse_lookup(wanted))" "            pass"
expect_red "M" "$CRASH" "test_reset_crash_between_card_and_retire_is_repaired_by_reverse_lookup"

echo "== N: state.yaml が読めないとき既定値に潰す"
fresh_copy
inject $LIB "        if _cards.is_unreadable(text):
            raise StoreReadError(path, text.reason, text.errno)
        try:
            data = _cards.parse_yaml(text, source=path)" "        if _cards.is_unreadable(text):
            return {'active_missions': [], 'default_mission': None}
        try:
            data = _cards.parse_yaml(text, source=path)"
expect_red "N" "$FAST" "test_state_missing_is_default_but_unreadable_is_an_error"

echo "== O: 監査ログの失敗で遷移を止める"
fresh_copy
inject $LIB "            _warn(f\"[state-store warn] audit log を書けませんでした ({path}: {err_no})\")" "            _warn(f\"[state-store warn] audit log を書けませんでした ({path}: {err_no})\")
            raise"
expect_red "O" "$FAST" "test_audit_failure_does_not_stop_the_transition_and_is_visible"

echo "== P: 直列化の規則が plan.sh とずれる (# を quote しない)"
fresh_copy
inject $LIB "_NEEDS_QUOTE = set(':#[]{},\\'\"\\n&*!|>%@\`')" "_NEEDS_QUOTE = set(':[]{},\\'\"\\n&*!|>%@\`')"
expect_red "P" "$FAST" "test_serialize_card_matches_plan_sh_byte_for_byte"

echo "== Q: R-1 が正本を書く"
fresh_copy
inject $LIB "        def _publish():
            self.t.publish_assignment(worker, slug, tid, str(gen))" "        def _publish():
            self.t.write_card(slug, tid, {**meta, 'title': str(meta.get('title')) + ' '}, '## Description\nx\n')
            self.t.publish_assignment(worker, slug, tid, str(gen))"
expect_red "Q" "$FAST" "test_r1_never_writes_the_authoritative_card"

echo "== R: audit に Result 本文を出す"
fresh_copy
inject $LIB "            'files': list(files)," "            'files': list(files), 'leak': open(self.card_path(rec['mission'], rec['task'])).read() if rec.get('task') and rec.get('mission') and os.path.exists(self.card_path(rec['mission'], rec['task'])) else None,"
expect_red "R" "$FAST" "test_body_row_fields_and_no_content_leak"

echo "== S: dump_yaml が key_order の表を書き換える"
fresh_copy
inject $LIB "    keys = list(key_order) if key_order else list(data.keys())" "    keys = key_order if key_order else list(data.keys())"
expect_red "S" "$FAST" "test_lib_dump_yaml_does_not_mutate_its_key_order_table"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
