#!/usr/bin/env bats
# tests/start-sh-target-dir-record.bats
#
# t009 (mission 20260926-mechanize-guards-a / PR3, backlog #21): start.sh の Worker 起動は、その
# Worker の TARGET_DIR (無ければ null) を `registry/workers/<Name>/target_dir.json` に残す。
# dispatcher が「別 repo 用の Worker に crewvia 本体の task を回す」のを止める根拠。
#
# 書き先は spawn 記録 (`registry/mux/<Name>-worker.json`) とは別のファイル — あれは kill の認可の
# 唯一の証拠で、相乗りすると kill の恒久拒否や、pane 消滅と同時に TARGET_DIR も消える経路ができる。
#
# 実 start.sh + 実 lib_worker_target.py + fake tmux。実 tmux / herdr には触れない。
# **start.sh は実 checkout では走らせない** (start-sh-registry-skills.bats と同じ理由): 作業ツリーを
# 使い捨ての複製にして、そこの registry に書く。teardown が消すのはその複製だけ。
#
# Run: bats tests/start-sh-target-dir-record.bats

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


_record() {
    python3 "${REPO_ROOT}/scripts/lib_worker_target.py" show "${REPO_ROOT}/registry" "$1"
}

@test "a Worker launched without TARGET_DIR is recorded as crewvia-local (target_dir: null)" {
    unset TARGET_DIR
    run bash "$START_SH" worker --name Ren code python

    [ "$status" -eq 0 ]
    run _record Ren
    [ "$status" -eq 0 ]
    [[ "$output" == *'"target_dir": null'* ]]
    [[ "$output" == *'"agent": "Ren"'* ]]
}

@test "a Worker launched with TARGET_DIR records the canonical path" {
    OTHER="$(mktemp -d)"
    TARGET_DIR="$OTHER" run bash "$START_SH" worker --name Ren code python

    [ "$status" -eq 0 ]
    run _record Ren
    [ "$status" -eq 0 ]
    [[ "$output" == *"\"target_dir\": \"$(cd "$OTHER" && pwd)\""* ]]
    rm -rf "$OTHER"
}

@test "TARGET_DIR pointing at crewvia itself is recorded as null (the Worker env has no TARGET_DIR either)" {
    TARGET_DIR="$REPO_ROOT" run bash "$START_SH" worker --name Ren code python

    [ "$status" -eq 0 ]
    run _record Ren
    [[ "$output" == *'"target_dir": null'* ]]
}

@test "a restart with a different TARGET_DIR replaces the record (the name is a position)" {
    OTHER="$(mktemp -d)"
    TARGET_DIR="$OTHER" run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    unset TARGET_DIR
    run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]

    run _record Ren
    [[ "$output" == *'"target_dir": null'* ]]
    rm -rf "$OTHER"
}

@test "the record is a separate file: the spawn record is not touched by it" {
    run bash "$START_SH" worker --name Ren code python

    [ "$status" -eq 0 ]
    [ -f "${REPO_ROOT}/registry/workers/Ren/target_dir.json" ]
    if [[ -f "${REPO_ROOT}/registry/mux/Ren-worker.json" ]]; then
        ! grep -q 'target_dir' "${REPO_ROOT}/registry/mux/Ren-worker.json"
    fi
}

@test "a Director launch writes no TARGET_DIR record" {
    run bash "$START_SH" director --name Sora

    [ ! -e "${REPO_ROOT}/registry/workers/Sora" ]
}

@test "a record that cannot be written does not stop the launch" {
    # registry/workers をファイルにして、ディレクトリを作れなくする (複製の中だけ。
    # 複製元の木にディレクトリが残っていても始められるよう、先に片付ける)。
    find "${REPO_ROOT}/registry/workers" -depth -delete 2>/dev/null || true
    : > "${REPO_ROOT}/registry/workers"
    run bash "$START_SH" worker --name Ren code python

    [ "$status" -eq 0 ]
    [[ "$output" == *"Agent launched in mux window"* ]]
    [[ "$output" == *"TARGET_DIR を記録できませんでした"* ]]
}
