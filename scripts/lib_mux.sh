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
#   mux_attach <name>

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
    python3 "$_LIB_MUX_PY" attach "$@"
}
