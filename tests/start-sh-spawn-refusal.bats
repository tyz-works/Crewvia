#!/usr/bin/env bats
# tests/start-sh-spawn-refusal.bats
#
# t001 (mission 20260925-ops-gap-fixes): start.sh が「<name> is already running」と
# 言ってよいのは、mux_spawn が「pane に live なプロセスが居る」(exit 10) または
# 「pane の中身が読めなかったので busy 扱いにした」(exit 11) と答えたときだけ。
#
# 以前は mux_spawn の失敗を全部 `mux_list | grep -qx <name>` で切り分けていた。
# 空の pane への再起動が定着しなかった場合も名前は list に出続けるので、
# 「起動していないのに already running」と言っていた。
#
# 実 lib_mux.py + 状態を持つ fake tmux。実 tmux / herdr には触れない。
#
# **start.sh は実 checkout では走らせない** (Kai P1, PR #215)。start.sh は自分の位置から
# REPO_ROOT を決め、`.claude/settings.local.json` (開発者のローカル設定) を書き、
# `registry/workers.yaml` と `registry/mux/` の記録を更新する。実 checkout で走らせ、
# teardown で `settings.local.json` を無条件に消すと、ドキュメント通りの bats コマンドが
# 開発者の設定を壊す。そこで作業ツリー (未コミットの変更を含む) を使い捨ての
# checkout に複製し、そこの start.sh を走らせる。teardown が消すのはその複製だけ。
#
# Run: bats tests/start-sh-spawn-refusal.bats

REAL_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

# 開発者のファイルに触れていないことを見るための印 (中身は読まない)。
_real_footprint() {
    local f
    for f in .claude/settings.local.json registry/workers.yaml; do
        if [[ -e "${REAL_ROOT}/${f}" ]]; then
            echo "${f} $(stat -c '%s %Y' "${REAL_ROOT}/${f}")"
        else
            echo "${f} absent"
        fi
    done
    if [[ -d "${REAL_ROOT}/registry/mux" ]]; then
        ls -A "${REAL_ROOT}/registry/mux" | sort
    fi
}

setup() {
    REAL_FOOTPRINT_BEFORE="$(_real_footprint)"

    SANDBOX="$(mktemp -d)"
    if git -C "$REAL_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        # 追跡ファイル + 未追跡 (ignore 除く)。ignore された物 (registry/mux 等) は持ち込まない。
        ( cd "$REAL_ROOT" && git ls-files -z --cached --others --exclude-standard \
            | tar --null --ignore-failed-read -T - -cf - 2>/dev/null ) \
            | tar -xf - -C "$SANDBOX"
    else
        # git の木でない (アーカイブ展開・red proof の作業コピー): 木ごと複製する。
        ( cd "$REAL_ROOT" && tar --exclude=./.git --exclude=./.claude/worktrees -cf - . ) \
            | tar -xf - -C "$SANDBOX"
    fi
    REPO_ROOT="$SANDBOX"
    START_SH="${REPO_ROOT}/scripts/start.sh"
    [[ -f "$START_SH" ]]

    FAKE_DIR="$(mktemp -d)"
    FAKE_TMUX_LOG="${FAKE_DIR}/tmux_calls.log"
    FAKE_CLAUDE_LOG="${FAKE_DIR}/claude_calls.log"
    FAKE_LIST_COUNT="${FAKE_DIR}/list_count"
    touch "$FAKE_TMUX_LOG" "$FAKE_CLAUDE_LOG"
    echo 0 > "$FAKE_LIST_COUNT"

    cat > "${FAKE_DIR}/tmux" <<'FAKESCRIPT'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
cmd="${1:-}"
case "$cmd" in
  has-session) exit 0 ;;
  list-sessions) echo "crewvia: 1 windows"; exit 0 ;;
  new-session|new-window)
    # FAKE_NEW_WINDOW_FAIL=1: the window could not be created.
    if [[ "${FAKE_NEW_WINDOW_FAIL:-0}" == "1" ]]; then echo "create failed" >&2; exit 1; fi
    for arg in "$@"; do
      case "$arg" in '#{window_id}'*) echo "@1" ;; esac
    done
    exit 0 ;;
  list-windows)
    # `-a` is the stale-record sweep asking about every window on the server
    # (start.sh runs it before spawning when a record exists).  It is not
    # spawn's own look at the session, so it must not move the counter.
    case " $* " in *" -a "*) exit 0 ;; esac
    n=$(cat "$FAKE_LIST_COUNT"); echo $((n + 1)) > "$FAKE_LIST_COUNT"
    # FAKE_LIST_MODE=present : the window is always listed.
    # FAKE_LIST_MODE=late    : not listed the first time (spawn's own look), listed after.
    case "${FAKE_LIST_MODE:-present}" in
      present) echo "${FAKE_WINDOW}" ;;
      late)    if [[ $n -ge 1 ]]; then echo "${FAKE_WINDOW}"; fi ;;
    esac
    exit 0 ;;
  send-keys) exit 0 ;;
  capture-pane) echo "❯ "; exit 0 ;;
  kill-window) exit 0 ;;
  display-message)
    fmt="${!#}"
    fmt="${fmt//'#{window_id}'/@1}"
    fmt="${fmt//'#{pane_pid}'/${FAKE_PANE_PID:-999999}}"
    fmt="${fmt//'#{pid}'/900}"
    fmt="${fmt//'#{socket_path}'//tmp/tmux-fake/default}"
    echo "$fmt"
    exit 0 ;;
  *) exit 1 ;;
esac
FAKESCRIPT
    chmod +x "${FAKE_DIR}/tmux"

    # 万一インライン (exec claude) に転落しても実 claude は起動させない。
    cat > "${FAKE_DIR}/claude" <<'FAKESCRIPT'
#!/usr/bin/env bash
echo "FAKE_CLAUDE_INVOKED $*" >> "$FAKE_CLAUDE_LOG"
exit 0
FAKESCRIPT
    chmod +x "${FAKE_DIR}/claude"

    export FAKE_DIR FAKE_TMUX_LOG FAKE_CLAUDE_LOG FAKE_LIST_COUNT
    export PATH="${FAKE_DIR}:${PATH}"
    export CREWVIA_TASKVIA=disabled
    export CREWVIA_MUX=tmux
    unset CREWVIA_MUX_ENABLED CREWVIA_BENCH_MODE CREWVIA_TMUX_SESSION
    export AGENT_NAME="RefusalTest"
    export FAKE_WINDOW="RefusalTest-worker"
}

teardown() {
    [[ -n "${LIVE_PID:-}" ]] && kill "$LIVE_PID" 2>/dev/null || true
    if [[ -n "${FAKE_DIR:-}" && -d "$FAKE_DIR" ]]; then
        find "$FAKE_DIR" -mindepth 1 -delete 2>/dev/null || true
        rmdir "$FAKE_DIR" 2>/dev/null || true
    fi
    # 消すのは setup が作った複製だけ。実 checkout のファイルは一切消さない。
    if [[ -n "${SANDBOX:-}" && "$SANDBOX" != "$REAL_ROOT" && -d "$SANDBOX" ]]; then
        find "$SANDBOX" -depth -delete 2>/dev/null || true
    fi
    # どのテストも、開発者の checkout を 1 バイトも変えていない。
    [ "$(_real_footprint)" = "$REAL_FOOTPRINT_BEFORE" ]
}

@test "a live process in the pane is reported as already running" {
    sleep 30 >/dev/null 2>&1 3>&- &
    LIVE_PID=$!
    export FAKE_PANE_PID="$LIVE_PID"
    export FAKE_LIST_MODE=present

    run bash "$START_SH" worker --name RefusalTest code

    [ "$status" -eq 0 ]
    [[ "$output" == *"RefusalTest-worker is already running"* ]]
    [[ "$output" != *"Agent launched"* ]]
}

@test "a pane that cannot be read is not reported as running" {
    export FAKE_PANE_PID=999999     # no such process: the pane cannot be read
    export FAKE_LIST_MODE=present

    run bash "$START_SH" worker --name RefusalTest code

    [ "$status" -eq 0 ]
    [[ "$output" == *"could not be read"* ]]
    [[ "$output" != *"is already running"* ]]
    [[ "$output" != *"Agent launched"* ]]
}

@test "a spawn that failed while the name is listed is NOT reported as already running" {
    # spawn の目にはまだ窓が無く (new-window が失敗)、その直後の mux_list には
    # 名前が出る、という並び。以前はここで「already running」と言っていた。
    export FAKE_LIST_MODE=late
    export FAKE_NEW_WINDOW_FAIL=1

    run bash "$START_SH" worker --name RefusalTest code

    [ "$status" -eq 0 ]
    [[ "$output" == *"launch did not take"* ]]
    [[ "$output" != *"is already running"* ]]
    [[ "$output" != *"Agent launched"* ]]
}

@test "a spawn that failed with the name absent is a plain error" {
    export FAKE_LIST_MODE=none
    export FAKE_NEW_WINDOW_FAIL=1

    run bash "$START_SH" worker --name RefusalTest code

    [ "$status" -eq 0 ]
    [[ "$output" == *"Failed to spawn mux window"* ]]
    [[ "$output" != *"is already running"* ]]
}

@test "a successful spawn still launches" {
    export FAKE_LIST_MODE=none

    run bash "$START_SH" worker --name RefusalTest code

    [ "$status" -eq 0 ]
    [[ "$output" == *"Agent launched in mux window"* ]]
}

@test "start.sh writes into the disposable checkout, never the developer's" {
    export FAKE_LIST_MODE=none

    run bash "$START_SH" worker --name RefusalTest code

    [ "$status" -eq 0 ]
    [[ "$output" == *"Agent launched in mux window"* ]]
    # spawn の記録と settings.local.json は複製の側にできている ...
    [ -f "${SANDBOX}/registry/mux/RefusalTest-worker.json" ]
    [ -f "${SANDBOX}/.claude/settings.local.json" ]
    # ... 実 checkout は (teardown の footprint 比較でも) 何も変わっていない。
    [ "$(_real_footprint)" = "$REAL_FOOTPRINT_BEFORE" ]
    [ "$REPO_ROOT" != "$REAL_ROOT" ]
}
