#!/usr/bin/env bash
# lib_mux.sh — thin bash wrappers around lib_mux.py
#
# Source this file in scripts that need mux operations:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib_mux.sh"
#
# Functions:
#   mux_available               → exit 0 if backend available
#   mux_spawn <name> <cmd> [<cwd>]
#   mux_send  <name> <text>
#   mux_capture <name>          → prints screen contents
#   mux_list [<suffix>]         → one name per line
#   mux_kill <name>
#   mux_pid  <name>             → prints PID integer
#   mux_attach <name>           → exec into the mux if outside; tab/switch if already inside

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_LIB_MUX_PY="${SCRIPT_DIR}/lib_mux.py"

mux_available() {
    python3 "$_LIB_MUX_PY" available
}

mux_spawn() {
    python3 "$_LIB_MUX_PY" spawn "$@"
}

mux_send() {
    python3 "$_LIB_MUX_PY" send "$@"
}

mux_capture() {
    python3 "$_LIB_MUX_PY" capture "$@"
}

mux_list() {
    python3 "$_LIB_MUX_PY" list "$@"
}

mux_kill() {
    python3 "$_LIB_MUX_PY" kill "$@"
}

mux_pid() {
    python3 "$_LIB_MUX_PY" pid "$@"
}

mux_attach() {
    local name="${1:-}"
    # Query attach-cmd: non-empty output = argv to exec (one arg per line).
    # Empty output = already inside the mux session → use python attach().
    #
    # NOTE: start.sh calls mux_attach at the very end of the director branch,
    # so exec here is safe — no clean-up code follows this call.
    local cmd_output
    cmd_output=$(python3 "$_LIB_MUX_PY" attach-cmd "$name" 2>/dev/null)
    if [[ -n "$cmd_output" ]]; then
        # Reconstruct argv from one-arg-per-line output and exec into the mux.
        # exec gives the TTY to the mux process (tmux attach-session / herdr).
        local -a argv
        mapfile -t argv <<< "$cmd_output"
        exec "${argv[@]}"
    fi
    # Already inside the mux — use python attach (switch-client / tab focus).
    python3 "$_LIB_MUX_PY" attach "$name"
}
