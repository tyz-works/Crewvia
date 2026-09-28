#!/usr/bin/env bash
# tests/red_proof_t005_main_checkout_sync.sh — B2 (backlog #26) の実装が
# 欠陥を戻すと赤くなることの実証。
#
# 使い方: bash tests/red_proof_t005_main_checkout_sync.sh
#
#   baseline — いまの木では対象テストが緑
#   case A   — 版の digest 比較を「常に等しい」に潰す (バージョン記録が意味を失う) → 赤
#   case B   — restart_needed() が「読めない」を False (= ずれ無し) に潰す        → 赤
#   case C   — clear_told_key() が notify のスロットルを畳み忘れる (実際に踏んだ欠陥) → 赤
#   case D   — sync-main-checkout.sh が restart-needed の結果を見ずに常に restart する → 赤
#   case E   — check_main_checkout_drift() が CYCLE ENTRY POINT から呼ばれない        → 赤
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触れない。
# 複製への `python3 lib_daemon_watch.py restart` の呼び出しは、この赤の実証全体を
# 通して一度も本物の mux に届かない (case D の対象テストがスタブの
# lib_daemon_watch.py で mux 呼び出しをすべて置き換えているため)。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t005-mcs.XXXXXX")"
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

# inject <file> <old> <new> — 複製した <file> の old を new に置換 (1 か所だけ。無ければ FATAL)
inject() {
    local file="$1" old="$2" new="$3"
    OLD="$old" NEW="$new" python3 - "$TREE/$file" <<'PY' || { echo "FATAL: 注入点が見つからない ($file)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

# 本物の HOME を使う (偽 HOME にすると内側の env -i が ~/.local の pytest を
# 見失う。加えてこのテストは実 git を叩くので、本物の HOME の git config
# (identity 以外は repo-local で上書き済み) が無いと push が壊れうる —
# memory: red-proof-inner-env-i-loses-pytest-under-fake-home)。
REAL_HOME="$HOME"
run_pytest() {  # run_pytest <test file or ::-qualified nodeid...>
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$REAL_HOME" \
        PYTHONUSERBASE="${PYTHONUSERBASE:-$REAL_HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest "$@" -q -p no:cacheprovider 2>&1 )
}

expect_red() {  # expect_red <case名> <pytest 引数...>
    local case_name="$1"; shift
    local out; out="$(run_pytest "$@")"
    if echo "$out" | grep -qE "[0-9]+ failed"; then ok "$case_name → 赤 ($(echo "$out" | tail -1))"
    else ng "$case_name → 赤にならなかった"; echo "$out" | tail -12; fi
}

# --------------------------------------------------------------------------
fresh_copy
echo "== baseline"
out="$(run_pytest tests/test_main_checkout_sync.py tests/test_dispatcher_main_checkout_drift.py)"
if echo "$out" | grep -qE " passed" && ! echo "$out" | grep -qE "failed"; then
    ok "baseline は緑 ($(echo "$out" | tail -1))"
else
    ng "baseline が緑でない"; echo "$out" | tail -20
fi

# --------------------------------------------------------------------------
echo "== case A: 版の digest 比較を潰す (常に等しい)"
fresh_copy
inject scripts/lib_daemon_watch.py \
    'return current_digest != recorded.get("files_digest")' \
    'return False  # BUG (red-proof injected): never reports drift'
expect_red "case A" tests/test_main_checkout_sync.py::test_record_and_compare_version_across_a_merge

# --------------------------------------------------------------------------
echo "== case B: restart_needed() が「読めない」を False に潰す"
fresh_copy
inject scripts/lib_daemon_watch.py \
    'if is_unreadable(recorded):
        return None' \
    'if is_unreadable(recorded):
        return False  # BUG (red-proof injected): unknown treated as no-drift'
expect_red "case B" tests/test_main_checkout_sync.py::test_restart_needed_is_unknown_without_a_recorded_version

# --------------------------------------------------------------------------
echo "== case C: clear_told_key() が notify スロットルを畳み忘れる (実際に t005 開発中に踏んだ欠陥)"
fresh_copy
inject scripts/dispatcher.sh \
    'if key in told:
            del told[key]
            save_told(told)
    forget_notify(f'"'"'{key}#'"'"')' \
    'if key in told:
            del told[key]
            save_told(told)'
expect_red "case C" tests/test_dispatcher_main_checkout_drift.py::test_resolution_then_recurrence_notifies_again

# --------------------------------------------------------------------------
echo "== case D: sync-main-checkout.sh が restart-needed を見ずに常に restart する"
fresh_copy
inject scripts/sync-main-checkout.sh \
    '  case "$v" in
    true)' \
    '  case "always" in
    always)'
expect_red "case D" tests/test_main_checkout_sync.py::test_sync_invokes_restart_when_a_daemon_is_flagged

# --------------------------------------------------------------------------
echo "== case E: check_main_checkout_drift() が CYCLE ENTRY POINT から呼ばれない"
fresh_copy
inject scripts/dispatcher.sh \
    'run_daemon_watch()
check_main_checkout_drift()
publish_agents()' \
    'run_daemon_watch()
publish_agents()'
expect_red "case E" tests/test_dispatcher_main_checkout_drift.py::test_check_main_checkout_drift_is_wired_into_the_cycle_entry_point

# --------------------------------------------------------------------------
echo ""
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
