#!/usr/bin/env bats
# tests/lib-mux.bats
#
# Unit tests for scripts/lib_mux.py (TmuxBackend).
# Uses a fake `tmux` placed at the front of PATH to record calls and return
# fixed responses — no real tmux session required.
#
# Run:
#   npx bats tests/lib-mux.bats
#   bats tests/lib-mux.bats       (if bats is installed globally)
#
# Coverage:
#   - available(): tmux present / absent
#   - spawn():     new-session path, new-window path, existing window (→ False)
#   - send():      2-step send-keys with 0.1s sleep, CREWVIA_TMUX_SESSION override
#   - capture():   capture-pane -p output
#   - list():      no suffix / with suffix filter
#   - kill():      kill-window args
#   - pid():       display-message #{pane_pid}
#   - attach():    switch-client when $TMUX set, attach-session otherwise

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
LIB_MUX_PY="${REPO_ROOT}/scripts/lib_mux.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# setup_fake_tmux builds a fake tmux binary in a temp dir.
# The fake tmux reads FAKE_TMUX_LOG and FAKE_TMUX_WINDOWS from the environment.
# All invocations are appended to FAKE_TMUX_LOG.
# list-windows returns the contents of FAKE_TMUX_WINDOWS.
setup_fake_tmux() {
    FAKE_TMUX_DIR="$(mktemp -d)"
    FAKE_TMUX_LOG="${FAKE_TMUX_DIR}/calls.log"
    FAKE_TMUX_WINDOWS="${FAKE_TMUX_DIR}/windows"
    touch "$FAKE_TMUX_LOG"

    # Default list-windows response.
    cat >"$FAKE_TMUX_WINDOWS" <<'EOF'
Sora-director
Omar-worker
Priya-worker
watchdog
EOF

    # The fake tmux binary: reads env vars for config.
    cat >"${FAKE_TMUX_DIR}/tmux" <<'FAKESCRIPT'
#!/usr/bin/env bash
# Append all args (space-joined) to the call log.
echo "$*" >> "$FAKE_TMUX_LOG"

cmd="${1:-}"

case "$cmd" in
  has-session)
    # FAKE_HAS_SESSION=1 → simulate "no session" (exit 1).
    [[ "${FAKE_HAS_SESSION:-0}" == "1" ]] && exit 1
    exit 0
    ;;
  new-session)
    exit 0
    ;;
  new-window)
    exit 0
    ;;
  list-windows)
    cat "$FAKE_TMUX_WINDOWS"
    exit 0
    ;;
  send-keys)
    exit 0
    ;;
  capture-pane)
    echo "❯ prompt line"
    exit 0
    ;;
  kill-window)
    exit 0
    ;;
  display-message)
    echo "12345"
    exit 0
    ;;
  switch-client)
    exit 0
    ;;
  attach-session)
    exit 0
    ;;
  *)
    exit 1
    ;;
esac
FAKESCRIPT

    chmod +x "${FAKE_TMUX_DIR}/tmux"

    # Export everything that the fake tmux and lib_mux.py need.
    export FAKE_TMUX_DIR
    export FAKE_TMUX_LOG
    export FAKE_TMUX_WINDOWS
    export PATH="${FAKE_TMUX_DIR}:${PATH}"
    export CREWVIA_MUX=tmux

    # Ensure no session override from a previous test.
    unset CREWVIA_TMUX_SESSION
}

teardown() {
    # Clean temp dir without rm -rf (security rule).
    if [[ -n "${FAKE_TMUX_DIR:-}" && -d "$FAKE_TMUX_DIR" ]]; then
        find "$FAKE_TMUX_DIR" -type f -delete 2>/dev/null || true
        find "$FAKE_TMUX_DIR" -type d -delete 2>/dev/null || true
    fi
}

# Assert the call log contains a string.
log_contains() {
    grep -qF -- "$1" "$FAKE_TMUX_LOG"
}

# Count lines matching a fixed string.
log_count() {
    grep -cF -- "$1" "$FAKE_TMUX_LOG" || true
}

# ---------------------------------------------------------------------------
# available()
# ---------------------------------------------------------------------------

@test "available: returns 0 when tmux is in PATH" {
    setup_fake_tmux
    run python3 "$LIB_MUX_PY" available
    [ "$status" -eq 0 ]
}

@test "available: returns 1 when tmux is not in PATH" {
    # Create an isolated bin dir containing python3 only (no tmux).
    ISOLATED_BIN="$(mktemp -d)"
    ln -sf "$(command -v python3)" "${ISOLATED_BIN}/python3"
    run env CREWVIA_MUX=tmux PATH="${ISOLATED_BIN}" python3 "$LIB_MUX_PY" available
    [ "$status" -eq 1 ]
}

# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------

@test "spawn: new-session path — creates session + sends cmd + Enter" {
    setup_fake_tmux
    export FAKE_HAS_SESSION=1   # has-session exits 1 → trigger new-session

    run python3 "$LIB_MUX_PY" spawn "Omar-worker" "claude --some-flag" "/tmp"
    [ "$status" -eq 0 ]

    log_contains "new-session"
    log_contains "send-keys -t crewvia:Omar-worker claude --some-flag"
    log_contains "send-keys -t crewvia:Omar-worker Enter"
}

@test "spawn: new-window path — session exists, window is new" {
    setup_fake_tmux
    # Default: has-session exits 0, list-windows returns names without "New-worker".

    run python3 "$LIB_MUX_PY" spawn "New-worker" "claude" "/tmp"
    [ "$status" -eq 0 ]

    log_contains "new-window"
    log_contains "send-keys -t crewvia:New-worker claude"
    log_contains "send-keys -t crewvia:New-worker Enter"
}

@test "spawn: existing window returns exit 1 (no-op)" {
    setup_fake_tmux
    # Omar-worker is already in the default window list.

    run python3 "$LIB_MUX_PY" spawn "Omar-worker" "claude"
    [ "$status" -eq 1 ]

    # Must NOT call new-window for an existing name.
    ! log_contains "new-window"
}

@test "spawn: CREWVIA_TMUX_SESSION overrides session name" {
    setup_fake_tmux
    export FAKE_HAS_SESSION=1
    export CREWVIA_TMUX_SESSION="mytest"

    run python3 "$LIB_MUX_PY" spawn "Test-worker" "claude"
    [ "$status" -eq 0 ]

    log_contains "new-session -d -s mytest"
    log_contains "send-keys -t mytest:Test-worker"
}

# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------

@test "send: issues 2 send-keys calls (text then Enter) with correct target" {
    setup_fake_tmux

    run python3 "$LIB_MUX_PY" send "Omar-worker" "タスクなし、shutdown"
    [ "$status" -eq 0 ]

    # Both send-keys invocations must be present.
    log_contains "send-keys -t crewvia:Omar-worker タスクなし、shutdown"
    log_contains "send-keys -t crewvia:Omar-worker Enter"

    # There must be exactly 2 send-keys lines total.
    count="$(log_count "send-keys")"
    [ "$count" -eq 2 ]
}

@test "send: text send-keys appears before Enter in call log (ordering)" {
    setup_fake_tmux

    python3 "$LIB_MUX_PY" send "Omar-worker" "hello" 2>/dev/null

    text_line="$(grep -n "send-keys -t crewvia:Omar-worker hello$" "$FAKE_TMUX_LOG" | head -1 | cut -d: -f1)"
    enter_line="$(grep -n "send-keys -t crewvia:Omar-worker Enter$" "$FAKE_TMUX_LOG" | head -1 | cut -d: -f1)"
    [ -n "$text_line" ]
    [ -n "$enter_line" ]
    [ "$text_line" -lt "$enter_line" ]
}

@test "send: CREWVIA_TMUX_SESSION override used in target" {
    setup_fake_tmux
    export CREWVIA_TMUX_SESSION="custom"

    run python3 "$LIB_MUX_PY" send "Omar-worker" "msg"
    [ "$status" -eq 0 ]
    log_contains "send-keys -t custom:Omar-worker msg"
}

# ---------------------------------------------------------------------------
# capture()
# ---------------------------------------------------------------------------

@test "capture: calls capture-pane -p with correct target and returns output" {
    setup_fake_tmux

    run python3 "$LIB_MUX_PY" capture "Sora-director"
    [ "$status" -eq 0 ]
    log_contains "capture-pane -t crewvia:Sora-director -p"
    [[ "$output" == *"❯ prompt line"* ]]
}

# ---------------------------------------------------------------------------
# list()
# ---------------------------------------------------------------------------

@test "list: no suffix returns all window names" {
    setup_fake_tmux

    run python3 "$LIB_MUX_PY" list
    [ "$status" -eq 0 ]
    [[ "$output" == *"Sora-director"* ]]
    [[ "$output" == *"Omar-worker"* ]]
    [[ "$output" == *"watchdog"* ]]
    log_contains "list-windows -t crewvia -F #{window_name}"
}

@test "list: suffix filter returns only matching names" {
    setup_fake_tmux

    run python3 "$LIB_MUX_PY" list "-worker"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Omar-worker"* ]]
    [[ "$output" == *"Priya-worker"* ]]
    [[ "$output" != *"Sora-director"* ]]
    [[ "$output" != *"watchdog"* ]]
}

@test "list: empty result when no windows match suffix" {
    setup_fake_tmux

    run python3 "$LIB_MUX_PY" list "-nonexistent"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# kill()
# ---------------------------------------------------------------------------

@test "kill: calls kill-window with session:name target" {
    setup_fake_tmux

    run python3 "$LIB_MUX_PY" kill "Omar-worker"
    [ "$status" -eq 0 ]
    log_contains "kill-window -t crewvia:Omar-worker"
}

@test "kill: CREWVIA_TMUX_SESSION override in kill target" {
    setup_fake_tmux
    export CREWVIA_TMUX_SESSION="testsession"

    run python3 "$LIB_MUX_PY" kill "Omar-worker"
    [ "$status" -eq 0 ]
    log_contains "kill-window -t testsession:Omar-worker"
}

# ---------------------------------------------------------------------------
# pid()
# ---------------------------------------------------------------------------

@test "pid: calls display-message with #{pane_pid} and returns integer" {
    setup_fake_tmux

    run python3 "$LIB_MUX_PY" pid "Sora-director"
    [ "$status" -eq 0 ]
    [[ "$output" == "12345" ]]
    log_contains "display-message -p -t crewvia:Sora-director #{pane_pid}"
}

# ---------------------------------------------------------------------------
# attach()
# ---------------------------------------------------------------------------

@test "attach: uses switch-client when TMUX env is set" {
    setup_fake_tmux
    export TMUX="/tmp/tmux-1000/default,12345,0"

    run python3 "$LIB_MUX_PY" attach "Sora-director"
    [ "$status" -eq 0 ]
    log_contains "switch-client -t crewvia"
}

@test "attach: uses attach-session when TMUX env is not set" {
    setup_fake_tmux
    unset TMUX

    run python3 "$LIB_MUX_PY" attach "Sora-director"
    [ "$status" -eq 0 ]
    log_contains "attach-session -t crewvia:Sora-director"
}

# ---------------------------------------------------------------------------
# CREWVIA_TMUX_SESSION global override
# ---------------------------------------------------------------------------

@test "CREWVIA_TMUX_SESSION: list uses custom session name" {
    setup_fake_tmux
    export CREWVIA_TMUX_SESSION="isolated-qa"

    run python3 "$LIB_MUX_PY" list
    [ "$status" -eq 0 ]
    log_contains "list-windows -t isolated-qa -F #{window_name}"
}

# ---------------------------------------------------------------------------
# tmux not installed → available() returns False
# ---------------------------------------------------------------------------

@test "available: False when tmux binary absent from PATH" {
    # Same isolated bin trick as test 2.
    ISOLATED_BIN="$(mktemp -d)"
    ln -sf "$(command -v python3)" "${ISOLATED_BIN}/python3"
    run env CREWVIA_MUX=tmux PATH="${ISOLATED_BIN}" python3 "$LIB_MUX_PY" available
    [ "$status" -eq 1 ]
}
