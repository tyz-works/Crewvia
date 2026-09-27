#!/usr/bin/env bash
# start.sh の trust 事前検査 + 最後の網 (t021 / B6 / backlog #28) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_b6_trust_precheck.sh
#
#   baseline — いまの木では tests/test_trust_precheck.py と tests/start-sh-trust-precheck.bats が緑
#   Z  修正前の start.sh (merge-base の版) に戻す                         → 赤 (bats: 拒否・ログ・最後の網の全部)
#   A  start.sh: 事前検査の結果を見ない (信頼されていなくても起動する)      → 赤 (bats)
#   M  start.sh: 拒否しても exit 0                                        → 赤 (bats)
#   J  start.sh: 拒否をログに書かない (backlog #18)                       → 赤 (bats)
#   K  start.sh: 最後の網が窓を片付けない                                 → 赤 (bats)
#   G  start.sh: 最後の網の「送信後」の検査を外す (verified と言い切る)      → 赤 (bats)
#   H  start.sh: 最後の網の「送信前」の検査を外す                          → 赤 (bats)
#   I  start.sh: 最後の網の「❯ 待機中」+「送信前」の検査を外す              → 赤 (bats)
#   B  lib_trust: 読めない設定を「信頼済み」に潰す                          → 赤 (pytest)
#   P  lib_trust: 形の崩れを「信頼しない (10)」に潰す (決められないを決めた)   → 赤 (pytest)
#   C  lib_trust: `is True` を truthiness にする ("true" 文字列で信頼済み)    → 赤 (pytest)
#   D  lib_trust: 祖先を辿らない                                          → 赤 (pytest + bats)
#   E  lib_trust: 論理パスの候補を落とす (物理パスへ畳む)                    → 赤 (pytest)
#   R  lib_trust: 折り返した文言の判定を外す (空白を畳まない)                → 赤 (pytest)
#   S  lib_trust: 単独の "No, exit" もダイアログに数える                    → 赤 (pytest)
#
# t051 (B6 fix: PR#237 の Codex findings) で追加:
#   T  start.sh: CLAUDE_CONFIG_DIR を spawn 先に伝播しない (P1)              → 赤 (bats)
#   U  start.sh: 未設定時に spawn 先の古い値を unset しない (P1)             → 赤 (bats)
#   W  start.sh: 空画面を「ダイアログなし」に倒す (P2)                       → 赤 (bats)
#   X  start.sh: 送信直前/直後の網を緩い版に戻す (観測不能でも Enter を送る) (P2) → 赤 (bats)
#   Y  start.sh: プロンプト待ちループの網に bench mode の除外を付けない (P2x2) → 赤 (bats)
#   V2 start.sh: HOME を spawn 先に伝播しない (Director 指示の族 B 掃除で発見)     → 赤 (bats)
#
# t070 (B6 fix 2巡目: Codex review 2巡目の findings) で追加:
#   INJ  start.sh: LAUNCH_CMD の cd を _shq を使わない生の '$WORK_DIR' 埋め込みに戻す (P1)  → 赤 (bats)
#   INJ2 start.sh: ENV_EXPORTS の AGENT_NAME を生の '$AGENT_NAME' 埋め込みに戻す (P1 / 族D)  → 赤 (bats)
#   REL  start.sh: CLAUDE_CONFIG_DIR / HOME の絶対パス解決を外す (P2)                        → 赤 (bats)
#
# t078 (B6 fix 3巡目: 族B — 文言の部分一致だけでは同定にならない) で追加:
#   SUBM lib_trust: ダイアログの操作構造 (選択肢のカーソル行 / Enter to confirm) を見ずに、
#        文言が一致した時点で即 True にする (部分一致に戻す)                                  → 赤 (pytest)
#
# 注意 (defense in depth): 最後の網は 3 か所 (待機中 / 送信前 / 送信後) にあり、**1 か所だけ**を外しても、
# 別の 1 か所が同じ画面を捕まえるので、多くのテストは緑のまま。だから G / H は「その 1 か所にだけ
# 反応するテスト」(送信前の窓・送信後の verified) が赤になることを名指しで確かめ、I は 2 か所を同時に
# 外して kickoff が実際に送られてしまう (= 送信後の網しか残らない) ことを確かめる。
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# 触れうる mux は fake tmux (bats 側) だけで、本物の herdr / tmux / 本番 registry・queue・~/.claude.json には
# 届かない (CREWVIA_HERDR_SOCK は存在しないパス、CLAUDE_CONFIG_DIR は各テストが使い捨ての dir に向ける)。
# 内側の env -i は pytest を ~/.local から見つけるために PYTHONUSERBASE を渡す (偽 HOME にしない)。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (rm -rf を使わない。終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-b6.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

BATS_FILE=tests/start-sh-trust-precheck.bats
PY_FILE=tests/test_trust_precheck.py

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

run_py() {
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$HOME" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest "$PY_FILE" -q -p no:cacheprovider 2>&1 )
}
run_bats() {
    ( cd "$TREE" && env CREWVIA_HERDR_SOCK="$WORK/no-such-herdr.sock" bats "$BATS_FILE" 2>&1 )
}

expect_red_py() {  # expect_red_py <case名> <赤になるはずのテスト名の断片>...
    local name="$1"; shift
    local out; out="$(run_py)"
    local frag
    for frag in "$@"; do
        if echo "$out" | grep -q "FAILED .*$frag"; then ok "$name → 赤 ($frag)"
        else ng "$name → 赤にならなかった ($frag)"; echo "$out" | tail -6; fi
    done
}
expect_red_bats() {  # expect_red_bats <case名> <赤になる @test 名の断片>...   (bats は 1 回だけ走らせる)
    local name="$1"; shift
    local out; out="$(run_bats)"
    local frag
    for frag in "$@"; do
        if echo "$out" | grep -q "^not ok .*$frag"; then ok "$name → 赤 ($frag)"
        else ng "$name → 赤にならなかった ($frag)"; echo "$out" | grep -E "^(ok|not ok)" | head -20; fi
    done
}

fresh_copy
echo "== baseline"
out="$(run_py)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then ok "baseline (pytest) は緑"
else ng "baseline (pytest) が緑でない"; echo "$out" | tail -8; fi
out="$(run_bats)"
if ! echo "$out" | grep -q "^not ok" && echo "$out" | grep -q "^ok"; then ok "baseline (bats) は緑"
else ng "baseline (bats) が緑でない"; echo "$out" | tail -8; fi

echo "== case Z: 修正前の start.sh (merge-base の版) に戻す"
fresh_copy
BASE="$(git -C "$REPO_ROOT" merge-base HEAD origin/main)"
git -C "$REPO_ROOT" show "$BASE:scripts/start.sh" > "$TREE/scripts/start.sh" \
    || { echo "FATAL: merge-base の start.sh を取れない"; exit 2; }
expect_red_bats "case Z" \
    "an untrusted TARGET_DIR is refused before claude is launched" \
    "the refusal tells the user what to type" \
    "the refusal is written to the log file" \
    "a refused launch leaves nothing behind" \
    "crewvia itself (no TARGET_DIR) is checked too" \
    "a config that cannot be parsed is refused" \
    "a config of the wrong shape is refused" \
    "a missing config means nothing is trusted" \
    "last net: a trust dialog on the pane stops the launch" \
    "last net: the dialog cursor" \
    "last net: a dialog that shows up after the prompt was seen" \
    "last net: a dialog still on screen after the send"

echo "== case A: 事前検査の結果を見ない"
fresh_copy
inject scripts/start.sh '  if [[ $_TRUST_RC -ne 0 ]]; then' '  if false; then'
expect_red_bats "case A" \
    "an untrusted TARGET_DIR is refused before claude is launched" \
    "a config that cannot be parsed is refused"

echo "== case M: 拒否しても exit 0"
fresh_copy
inject scripts/start.sh '    _log_refusal trust "$_TRUST_MSG"
    exit 1' '    _log_refusal trust "$_TRUST_MSG"
    exit 0'
expect_red_bats "case M" "an untrusted TARGET_DIR is refused before claude is launched"

echo "== case J: 拒否をログに書かない"
fresh_copy
inject scripts/start.sh '    _log_refusal trust "$_TRUST_MSG"' '    true'
expect_red_bats "case J" "the refusal is written to the log file"

echo "== case K: 最後の網が窓を片付けない"
fresh_copy
# t051 で _abort_on_unobservable_dialog にも同じ mux_kill 行が増えたので、直前の _log_refusal 行込みで
# _abort_on_trust_dialog 側だけを名指しする (unobservable 側は _log_refusal trust-dialog-unobservable)。
inject scripts/start.sh '    _log_refusal trust-dialog "$msg"
    mux_kill "$WINDOW_NAME" >/dev/null 2>&1 \' '    _log_refusal trust-dialog "$msg"
    true \'
expect_red_bats "case K" "last net: a trust dialog on the pane stops the launch"

echo "== case G: 送信後の検査を外す (dialog のまま verified と言い切る)"
fresh_copy
inject scripts/start.sh '    _require_no_trust_dialog "kickoff 送信後"' '    true'
expect_red_bats "case G" "last net: a dialog still on screen after the send"

echo "== case H: 送信前の検査を外す"
fresh_copy
inject scripts/start.sh '      _require_no_trust_dialog "kickoff 送信前 (${_kickoff_attempt}/3)"' '      true'
expect_red_bats "case H" "last net: a dialog that shows up after the prompt was seen"

echo "== case I: 待機中と送信前の検査を外す (送信後の網しか残らない → kickoff が送られてしまう)"
fresh_copy
inject scripts/start.sh '      _require_no_trust_dialog "kickoff 送信前 (${_kickoff_attempt}/3)"' '      true'
inject scripts/start.sh '    _trust_dialog_check "❯ の待機中"' '    true'
expect_red_bats "case I" \
    "last net: a trust dialog on the pane stops the launch" \
    "last net: the dialog cursor" \
    "last net: a dialog that shows up after the prompt was seen"

echo "== case B: 読めない設定を「信頼済み」に潰す"
fresh_copy
inject scripts/lib_trust.py '    if is_unreadable(data):
        return Verdict(UNVERIFIABLE,' '    if is_unreadable(data):
        return Verdict(TRUSTED,'
expect_red_py "case B" "test_a_config_that_cannot_be_used_is_unverifiable" "test_a_config_that_is_a_directory_is_unverifiable"
expect_red_bats "case B (bats)" "a config that cannot be parsed is refused" "a config of the wrong shape is refused"

echo "== case P: 形の崩れを「信頼しない (10)」に潰す"
fresh_copy
inject scripts/lib_trust.py '    if malformed:
        return Verdict(UNVERIFIABLE,' '    if malformed:
        return Verdict(UNTRUSTED,'
expect_red_py "case P" "test_only_the_boolean_true_trusts_a_dir" "test_a_relevant_entry_that_is_not_an_object_is_unverifiable"

echo "== case C: is True を truthiness にする"
fresh_copy
inject scripts/lib_trust.py '                if value is True:' '                if value:'
expect_red_py "case C" "test_only_the_boolean_true_trusts_a_dir"

echo "== case D: 祖先を辿らない"
fresh_copy
inject scripts/lib_trust.py '        for anc in ancestors(cand):' '        for anc in [cand]:'
expect_red_py "case D" "test_a_trusted_ancestor_covers_everything_below_it" "test_the_filesystem_root_trusted_covers_everything"
expect_red_bats "case D (bats)" "a TARGET_DIR under a trusted ancestor launches"

echo "== case E: 論理パスの候補を落とす"
fresh_copy
inject scripts/lib_trust.py '    for p in (logical, physical):' '    for p in (physical,):'
expect_red_py "case E" "test_a_symlinked_dir_is_trusted_by_its_logical_path"

echo "== case R: 折り返した文言の判定を外す (空白を畳まない)"
fresh_copy
inject scripts/lib_trust.py "    flat = ' '.join(str(screen).lower().split())" "    flat = str(screen).lower()"
expect_red_py "case R" "test_the_trust_dialog_is_recognised"

echo "== case S: 単独の No, exit もダイアログに数える"
fresh_copy
inject scripts/lib_trust.py "    if not phrase_hit:
        return False
    return bool(_MENU_CURSOR_RE.search(flat)) or 'enter to confirm' in flat" \
                              "    if not phrase_hit:
        return False
    return True"
expect_red_py "case S" "test_ordinary_screens_are_not_mistaken_for_the_dialog"

echo "== case SUBM: 文言の部分一致だけで同定する (族B、t078) — 選択肢の構造を見ない"
fresh_copy
inject scripts/lib_trust.py "    phrase_hit = any(p in flat for p in _DIALOG_PHRASES) or 'no, exit' in flat
    if not phrase_hit:
        return False
    return bool(_MENU_CURSOR_RE.search(flat)) or 'enter to confirm' in flat" \
                              "    if any(p in flat for p in _DIALOG_PHRASES):
        return True
    return 'no, exit' in flat and 'enter to confirm' in flat"
expect_red_py "case SUBM" "test_ordinary_screens_are_not_mistaken_for_the_dialog"

echo "== case T: CLAUDE_CONFIG_DIR を spawn 先に伝播しない (t051 P1)"
fresh_copy
inject scripts/start.sh '    ENV_EXPORTS+=" CLAUDE_CONFIG_DIR=$(_shq "${CLAUDE_CONFIG_DIR}")"' '    true'
expect_red_bats "case T" "CLAUDE_CONFIG_DIR read by the precheck is forwarded to the spawned claude"

echo "== case U: 未設定時に spawn 先の古い値を unset しない (t051 P1)"
fresh_copy
inject scripts/start.sh '    _TRUST_UNSET_STALE_CONFIG_DIR="unset CLAUDE_CONFIG_DIR; "' '    _TRUST_UNSET_STALE_CONFIG_DIR=""'
expect_red_bats "case U" "without CLAUDE_CONFIG_DIR, the launch command clears any stale value"

echo "== case W: 空画面を『ダイアログなし』に倒す (t051 P2)"
fresh_copy
inject scripts/start.sh '    if [[ -z "$screen" ]]; then
      return 2
    fi
    printf' '    printf'
expect_red_bats "case W" "last net: a screen that cannot be captured is never treated"

echo "== case X: 送信直前/直後の網を緩い版に戻す (t051 P2)"
fresh_copy
inject scripts/start.sh '      _require_no_trust_dialog "kickoff 送信前 (${_kickoff_attempt}/3)"' '      _trust_dialog_check "kickoff 送信前 (${_kickoff_attempt}/3)"'
inject scripts/start.sh '    _require_no_trust_dialog "kickoff 送信後"' '    _trust_dialog_check "kickoff 送信後"'
expect_red_bats "case X" "last net: a screen that cannot be captured is never treated"

echo "== case Y: プロンプト待ちループの網に bench mode の除外を付けない (t051 P2x2)"
fresh_copy
inject scripts/start.sh '    [[ "${CREWVIA_BENCH_MODE:-0}" == "1" ]] && return 0
    local rc=0
    _trust_dialog_shown || rc=$?
    case $rc in
      0) _abort_on_trust_dialog "$1" ;;' '    local rc=0
    _trust_dialog_shown || rc=$?
    case $rc in
      0) _abort_on_trust_dialog "$1" ;;'
expect_red_bats "case Y" "bench mode is not gated by the last-net trust dialog check"

echo "== case V2: HOME を spawn 先に伝播しない (族 B 掃除で発見)"
fresh_copy
inject scripts/start.sh '  ENV_EXPORTS+=" HOME=$(_shq "${HOME}")"' "  true"
expect_red_bats "case V2" "HOME used by the precheck (when CLAUDE_CONFIG_DIR is unset) is forwarded too"

echo "== case INJ: LAUNCH_CMD の cd を生の '\$WORK_DIR' 埋め込みに戻す (t070 P1 シェルインジェクション)"
fresh_copy
inject scripts/start.sh 'cd $(_shq "$WORK_DIR"); claude' "cd '\$WORK_DIR'; claude"
expect_red_bats "case INJ" \
    "LAUNCH_CMD survives a TARGET_DIR containing a quote and a shell-injection payload"

echo "== case INJ2: ENV_EXPORTS の AGENT_NAME を生の '\$AGENT_NAME' 埋め込みに戻す (t070 P1 / 族D、別の箇所)"
fresh_copy
inject scripts/start.sh 'AGENT_NAME=$(_shq "$AGENT_NAME")' "AGENT_NAME='\$AGENT_NAME'"
expect_red_bats "case INJ2" \
    "AGENT_NAME containing a quote does not break LAUNCH_CMD either"

echo "== case REL: CLAUDE_CONFIG_DIR / HOME の絶対パス解決を外す (t070 P2)"
fresh_copy
inject scripts/start.sh '    _RESOLVED_CONFIG_DIR="$(cd "${CLAUDE_CONFIG_DIR}" 2>/dev/null && pwd)" || _RESOLVED_CONFIG_DIR=""
    [[ -n "$_RESOLVED_CONFIG_DIR" ]] && export CLAUDE_CONFIG_DIR="$_RESOLVED_CONFIG_DIR"' '    true'
inject scripts/start.sh '    _RESOLVED_HOME="$(cd "${HOME}" 2>/dev/null && pwd)" || _RESOLVED_HOME=""
    [[ -n "$_RESOLVED_HOME" ]] && export HOME="$_RESOLVED_HOME"' '    true'
expect_red_bats "case REL" \
    "a relative CLAUDE_CONFIG_DIR resolves to the same absolute dir the spawned claude will read" \
    "a relative HOME (no CLAUDE_CONFIG_DIR) resolves to the same absolute dir too"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
