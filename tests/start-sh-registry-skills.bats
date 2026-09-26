#!/usr/bin/env bats
# tests/start-sh-registry-skills.bats
#
# t017 (mission 20260926-mechanize-guards-a / PR5a, backlog #14): start.sh の Worker 起動は、
# 渡された skills を registry/workers.yaml の当該 Worker に **和集合で** 足す。
#
# 以前は `set-last-active` だけだった。dispatcher は task の skills を registry と突き合わせる
# ので、registry が古い Worker には task が割り当たらず、「起動要求 ⇄ 仕事なし退役」が互いを
# 打ち消した (4 人で発生)。
#
# 実 start.sh + 実 lib_registry.py + fake tmux。実 tmux / herdr には触れない。
# **start.sh は実 checkout では走らせない** (tests/start-sh-spawn-refusal.bats と同じ理由):
# 作業ツリーを使い捨ての複製にして、そこの registry を読み書きする。teardown が消すのは
# その複製だけで、実 checkout の registry / settings.local.json は 1 バイトも変わらない。
#
# Run: bats tests/start-sh-registry-skills.bats

REAL_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

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
        ( cd "$REAL_ROOT" && git ls-files -z --cached --others --exclude-standard \
            | tar --null --ignore-failed-read -T - -cf - 2>/dev/null ) \
            | tar -xf - -C "$SANDBOX"
    else
        ( cd "$REAL_ROOT" && tar --exclude=./.git --exclude=./.claude/worktrees -cf - . ) \
            | tar -xf - -C "$SANDBOX"
    fi
    REPO_ROOT="$SANDBOX"
    START_SH="${REPO_ROOT}/scripts/start.sh"
    REGISTRY="${REPO_ROOT}/registry/workers.yaml"
    [[ -f "$START_SH" ]]

    # 決まった registry に差し替える (複製の中だけ)。
    mkdir -p "${REPO_ROOT}/registry"
    printf '%s\n' \
        '# registry/workers.yaml (bats fixture)' \
        '' \
        'workers:' \
        '  - name: Ren' \
        '    skills: [code, python]' \
        '    task_count: 7' \
        '    last_active: 2026-09-01' \
        '  - name: Hana' \
        '    skills: [bash, ops]' \
        '    task_count: 5' \
        '    last_active: 2026-08-25' \
        > "$REGISTRY"

    FAKE_DIR="$(mktemp -d)"
    printf '%s\n' '#!/usr/bin/env bash' \
        'cmd="${1:-}"' \
        'case "$cmd" in' \
        '  has-session) exit 0 ;;' \
        '  list-sessions) echo "crewvia: 1 windows"; exit 0 ;;' \
        '  new-session|new-window)' \
        '    for arg in "$@"; do case "$arg" in "#{window_id}"*) echo "@1" ;; esac; done; exit 0 ;;' \
        '  list-windows) exit 0 ;;' \
        '  send-keys) exit 0 ;;' \
        '  capture-pane) echo "❯ "; exit 0 ;;' \
        '  display-message)' \
        '    fmt="${!#}"; fmt="${fmt//"#{window_id}"/@1}"; fmt="${fmt//"#{pane_pid}"/999999}"' \
        '    fmt="${fmt//"#{pid}"/900}"; fmt="${fmt//"#{socket_path}"//tmp/tmux-fake/default}"' \
        '    echo "$fmt"; exit 0 ;;' \
        '  *) exit 1 ;;' \
        'esac' > "${FAKE_DIR}/tmux"
    printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "${FAKE_DIR}/claude"
    chmod +x "${FAKE_DIR}/tmux" "${FAKE_DIR}/claude"

    export PATH="${FAKE_DIR}:${PATH}"
    export CREWVIA_TASKVIA=disabled
    export CREWVIA_MUX=tmux
    unset CREWVIA_MUX_ENABLED CREWVIA_BENCH_MODE CREWVIA_TMUX_SESSION AGENT_NAME
}

teardown() {
    if [[ -n "${FAKE_DIR:-}" && -d "$FAKE_DIR" ]]; then
        find "$FAKE_DIR" -mindepth 1 -delete 2>/dev/null || true
        rmdir "$FAKE_DIR" 2>/dev/null || true
    fi
    if [[ -n "${SANDBOX:-}" && "$SANDBOX" != "$REAL_ROOT" && -d "$SANDBOX" ]]; then
        find "$SANDBOX" -depth -delete 2>/dev/null || true
    fi
    # どのテストも、開発者の checkout を 1 バイトも変えていない。
    [ "$(_real_footprint)" = "$REAL_FOOTPRINT_BEFORE" ]
}

_skills_of() {
    grep -A2 "name: $1\$" "$REGISTRY" | grep 'skills:'
}

@test "a launch skill the registry lacks is added to the Worker's entry" {
    run bash "$START_SH" worker --name Ren code python bash

    [ "$status" -eq 0 ]
    [[ "$(_skills_of Ren)" == *"[code, python, bash]"* ]]
}

@test "a skill only the registry has survives a launch with fewer skills" {
    run bash "$START_SH" worker --name Hana bash

    [ "$status" -eq 0 ]
    [[ "$(_skills_of Hana)" == *"[bash, ops]"* ]]
}

@test "--skills a,b (comma form) is unioned the same way" {
    run bash "$START_SH" worker --name Hana --skills qa,python

    [ "$status" -eq 0 ]
    [[ "$(_skills_of Hana)" == *"[bash, ops, qa, python]"* ]]
}

@test "the launch still updates last_active and leaves other Workers alone" {
    run bash "$START_SH" worker --name Ren code python bash

    [ "$status" -eq 0 ]
    [[ "$(grep -A3 'name: Ren$' "$REGISTRY")" != *"last_active: 2026-09-01"* ]]
    [[ "$(_skills_of Hana)" == *"[bash, ops]"* ]]
    grep -q 'task_count: 7' "$REGISTRY"
}

@test "a launch with no skills leaves the skills alone and still starts" {
    run bash "$START_SH" worker --name Ren

    [ "$status" -eq 0 ]
    [[ "$(_skills_of Ren)" == *"[code, python]"* ]]
}
