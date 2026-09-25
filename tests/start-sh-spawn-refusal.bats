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
# Run: bats tests/start-sh-spawn-refusal.bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
START_SH="${REPO_ROOT}/scripts/start.sh"

setup() {
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
    rm -f "${REPO_ROOT}/.claude/settings.local.json"
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
