#!/usr/bin/env bash
# PR #257 Codex 2 巡目 P2 ×2 (t029) の修正が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t029.sh
#
#   baseline — いまの木では tests/test_state_store_observability.py が緑
#   case 0   — 修正前の lib (PR #257 の 1 巡目の head 307bde0) に戻す → 赤 (全体)
#   P2-1 (読めない入力を健全と報告しない)
#   A  列挙の失敗 (EACCES) を空リスト・コードなしに戻す     → test_r3_reports_an_unlistable_... (Scope 側)
#      ※ diagnose の `os.walk(onerror=)` が同じ失敗を別経路で拾う (二重の網)。A 単独では
#        test_unlistable_tasks_dir_is_a_finding_... は緑のまま。A + K (walk の onerror 削除) で赤になる
#   B  読めない state.yaml を「active なし・コードなし」に戻す → test_unreadable_state_yaml_...
#   C  R-2 が読めない枠を黙って飛ばす                        → test_unreadable_assignment_is_reported_...
#   D  R-4 が観測できないのに archive 済みと読む              → test_r4_does_not_drop_...
#   E  orphan_identity を lexists で判定 (EACCES → 「無い」)   → test_orphan_identity_is_not_declared_...
#   P2-2 (内容を出さない)
#   F  parse エラーの文字列 (問題の行を含む) をそのまま理由に  → test_load_card_error_and_its_chain_...
#   G  例外を except の中で投げる (連鎖に行が残る)              → test_broken_mission_and_state_errors_...
#   H  detail の門を外す (自由文を通す)                        → test_audit_row_is_gated_...
#   I  監査ログの actor を門に通さない                         → test_audit_row_is_gated_...
#   K  A に加えて walk の onerror を消す (二重の網の両方)      → test_unlistable_tasks_dir_...
#
# 隔離: 使い捨ての複製 (scripts/ tests/ だけ) で欠陥を注入する。本番の worktree・queue・registry には触れない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さない)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t029.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0; N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

SUITE="tests/test_state_store_observability.py tests/test_state_store_transaction.py"
LIB="scripts/lib_state_store.py"
BEFORE_SHA="307bde0"       # PR #257 の 1 巡目の head (この修正の直前)

TREE=""
fresh_copy() {
    N=$((N + 1)); TREE="$WORK/tree$N"; mkdir -p "$TREE"
    rsync -a --exclude='__pycache__' "$REPO_ROOT/scripts" "$REPO_ROOT/tests" "$TREE/"
}

inject() {   # inject <file(TREE 相対)> <old> <new> — old を new に置換 (ちょうど 1 か所。無ければ FATAL)
    OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない、または複数ある ($1)"; exit 2; }
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
        PYTHONDONTWRITEBYTECODE=1 timeout 600 python3 -m pytest $SUITE -p no:cacheprovider -q 2>&1 )
}

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local out; out="$(run_suite)"
    if echo "$out" | grep -q "FAILED .*$2"; then ok "$1 → 赤 ($2)"
    else ng "$1 → 赤にならなかった ($2)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_suite)"
if echo "$out" | grep -qE "[0-9]+ passed" && ! echo "$out" | grep -qE "[0-9]+ (failed|error)"; then
    ok "baseline は緑 ($(echo "$out" | tail -1))"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== case 0: 修正前の lib ($BEFORE_SHA)"
fresh_copy
if git -C "$REPO_ROOT" show "$BEFORE_SHA:$LIB" > "$TREE/$LIB" 2>/dev/null; then
    out="$(run_suite)"
    if echo "$out" | grep -qE "[0-9]+ (failed|error)"; then ok "case 0 → 赤 ($(echo "$out" | tail -1))"
    else ng "case 0 → 赤にならなかった"; echo "$out" | tail -5; fi
else
    echo "  SKIP: $BEFORE_SHA を取得できない"
fi

echo "== A: 列挙の失敗を空リストに戻す"
fresh_copy
inject $LIB "        return [], f\"list_error:{_errno_name(e.errno)}\"" "        return [], None"
expect_red "A" "test_r3_reports_an_unlistable_tasks_dir_instead_of_skipping"
expect_red "A'" "test_reverse_lookup_reports_an_unlistable_assignments_dir"

echo "== B: 読めない state.yaml を「active なし・コードなし」に戻す"
fresh_copy
inject $LIB "        return [], _unreadable_code(text)
    try:" "        return [], None
    try:"
expect_red "B" "test_unreadable_state_yaml_is_a_finding_and_r4_never_runs_on_it"

echo "== C: R-2 が読めない枠を黙って飛ばす"
fresh_copy
inject $LIB "            self._emit('reported:assignment_unverifiable', agent=agent, detail=slot[1])
            return
        pslug" "            return
        pslug"
expect_red "C" "test_unreadable_assignment_is_reported_even_if_no_card_refers_to_it"

echo "== D: R-4 が観測できないのに archive 済みと読む"
fresh_copy
inject $LIB "        if src == 'unobservable' or dst == 'unobservable':" "        if False:"
expect_red "D" "test_r4_does_not_drop_an_active_mission_it_cannot_observe"

echo "== E: orphan_identity を lexists で判定"
fresh_copy
inject $LIB "            state = _path_state(os.path.join(adir, body))" "            state = 'present' if os.path.lexists(os.path.join(adir, body)) else 'absent'"
expect_red "E" "test_orphan_identity_is_not_declared_when_the_body_cannot_be_observed"

echo "== F: parse エラーの文字列をそのまま理由にする"
fresh_copy
inject $LIB "    return f\"parse_error:line={m.group(1)}\" if m else \"parse_error\"" "    return f\"parse_error: {exc}\""
expect_red "F" "test_load_card_error_and_its_chain_do_not_contain_the_line"

echo "== G: 例外を except の中で投げる (連鎖に行が残る)"
fresh_copy
inject $LIB "            code = _parse_code(e)
        # except の外で投げる: 例外の連鎖 (__context__) に問題の行を残さない
        raise StoreReadError(path, code)" "            raise StoreReadError(path, _parse_code(e))"
expect_red "G" "test_broken_mission_and_state_errors_carry_no_line"

echo "== H: detail の門を外す"
fresh_copy
inject $LIB "    return text if len(text) <= 300 and any(r.fullmatch(text) for r in _SAFE_DETAIL_RES) else 'redacted'" "    return text"
expect_red "H" "test_audit_row_is_gated_even_for_caller_supplied_values"

echo "== I: 監査ログの actor を門に通さない"
fresh_copy
inject $LIB "            'actor': _safe_token(self.actor) or 'unknown'," "            'actor': self.actor,"
expect_red "I" "test_audit_row_is_gated_even_for_caller_supplied_values"

echo "== K: 列挙の失敗を握り潰す (Scope 側) + walk の onerror を消す (二重の網の両方)"
fresh_copy
inject $LIB "        return [], f\"list_error:{_errno_name(e.errno)}\"" "        return [], None"
inject $LIB "os.walk(queue_dir, onerror=walk_errors.append)" "os.walk(queue_dir)"
expect_red "K" "test_unlistable_tasks_dir_is_a_finding_not_an_empty_queue"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
