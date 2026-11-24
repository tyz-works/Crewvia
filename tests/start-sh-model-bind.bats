#!/usr/bin/env bats
# tests/start-sh-model-bind.bats
#
# モデルの段上げ PR-2 (knowledge/model-escalation.md §4.3-3・§4.5・§4.8): `CREWVIA_BIND_TASK=<slug>/<tid>` の束ねた起動。
# モデルは `plan.sh model-for` (card の model_override > skills の既定) で取り、model.json に bound_task を書く。
# 明示 env が勝つ (低いときは警告)。決められないときは起動しない (exit 1・refusals.log)。
#
# 実 start.sh + 実 lib_worker_target.py + fake tmux。実 tmux / herdr には触れない。
# **start.sh は実 checkout では走らせない** (start-sh-registry-skills.bats と同じ理由): 作業ツリーを
# 使い捨ての複製にして、そこの registry に書く。teardown が消すのはその複製だけ。
#
# Run: bats tests/start-sh-model-bind.bats

REAL_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
source "$(dirname "$BATS_TEST_FILENAME")/trust_fixture.sh"   # t021: start.sh の trust 事前検査を通す偽の設定

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
    trust_fixture_setup
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
    unset CREWVIA_MUX_ENABLED CREWVIA_BENCH_MODE CREWVIA_TMUX_SESSION AGENT_NAME CREWVIA_QUEUE CREWVIA_REPO_ROOT
}

teardown() {
    trust_fixture_teardown
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
    python3 "${REPO_ROOT}/scripts/lib_worker_model.py" show "${REPO_ROOT}/registry" "$1"
}

_mission() {   # _mission <override|-> : 使い捨ての queue に mission と t001 を置く
    local ov="${1:--}" d="${REPO_ROOT}/queue/missions/m-bind"
    mkdir -p "$d/tasks"
    printf '%s\n' 'title: bind' 'slug: m-bind' 'status: in_progress' 'created_at: "2026-11-24T00:00:00Z"' \
        'completed_at: null' 'next_task_id: 2' > "$d/mission.yaml"
    {
        printf '%s\n' '---' 'id: t001' 'title: bound work' 'skills: [code, python]' 'priority: high' \
            'status: pending' 'blocked_by: []' 'target_dir: null' 'worker: null' 'started_at: null' 'completed_at: null'
        [[ "$ov" != "-" ]] && printf 'model_override: %s\n' "$ov"
        printf '%s\n' '---' '' '## Description' 'w' '' '## Result' ''
    } > "$d/tasks/t001.md"
}

@test "a bound start takes the model from the card's model_override and records bound_task with source override" {
    _mission opus
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE
    CREWVIA_PRINT_MODEL=1 CREWVIA_BIND_TASK=m-bind/t001 run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    [ "$output" = "opus" ]
    CREWVIA_BIND_TASK=m-bind/t001 run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    run _record Ren
    [[ "$output" == *'"model": "opus"'* ]]
    [[ "$output" == *'"source": "override"'* ]]
    [[ "$output" == *'"bound_task": "m-bind/t001"'* ]]
}

@test "without an override the bound Worker gets the skills default" {
    _mission -
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE
    run bash "${REPO_ROOT}/scripts/plan.sh" model-for t001 --mission m-bind
    [ "$status" -eq 0 ]
    EXPECT="$output"
    CREWVIA_PRINT_MODEL=1 CREWVIA_BIND_TASK=m-bind/t001 run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    [ "$output" = "$EXPECT" ]
}

@test "a bound start for a card without model_override records source skill, not override" {
    _mission -
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE
    CREWVIA_BIND_TASK=m-bind/t001 run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    run _record Ren
    [[ "$output" == *'"source": "skill"'* ]]
    [[ "$output" == *'"bound_task": "m-bind/t001"'* ]]
}

@test "an explicit env wins over the card, and a lower one warns" {
    _mission opus
    unset CREWVIA_QUEUE
    CREWVIA_WORKER_MODEL=haiku CREWVIA_BIND_TASK=m-bind/t001 run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    [[ "$output" == *"より低いですが"* ]]
    run _record Ren
    [[ "$output" == *'"model": "haiku"'* ]]
    [[ "$output" == *'"source": "env"'* ]]
    [[ "$output" == *'"bound_task": "m-bind/t001"'* ]]
}

@test "the bound kickoff pulls that task" {
    _mission opus
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE
    export CREWVIA_BIND_TASK=m-bind/t001
    run bash -c "grep -c 'plan pull \${BIND_PULL_FLAGS}--agent' '$START_SH'"
    [ "$output" = "2" ]
}

@test "a broken bind value, a missing card or a broken override refuses to start and leaves no record" {
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE
    _mission opus
    for v in "m-bind" "m-bind/t001/x" "/t001" "m-bind/" "../x/t001" "m-bind/t999" "nomission/t001" "m-bind/--mission"; do
        CREWVIA_BIND_TASK="$v" run bash "$START_SH" worker --name Ren code python
        [ "$status" -eq 1 ]
        [ ! -e "${REPO_ROOT}/registry/workers/Ren/model.json" ]
    done
    _mission opus-4
    CREWVIA_BIND_TASK=m-bind/t001 run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 1 ]
    [ ! -e "${REPO_ROOT}/registry/workers/Ren/model.json" ]
    grep -q 'kind=bind-task' "${REPO_ROOT}/logs/start-sh/refusals.log"
    CREWVIA_PRINT_MODEL=1 CREWVIA_BIND_TASK=m-bind/t999 run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 1 ]
}

@test "a bound start whose model.json cannot be written refuses to start (fail-closed)" {
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE
    _mission opus
    mkdir -p "${REPO_ROOT}/registry/workers"
    printf 'x' > "${REPO_ROOT}/registry/workers/Ren"      # Ren/ を作れない = model.json を書けない
    CREWVIA_BIND_TASK=m-bind/t001 run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 1 ]
    [[ "$output" == *"起動しません"* ]]
    grep -q 'kind=bind-task-record' "${REPO_ROOT}/logs/start-sh/refusals.log"
    rm -f "${REPO_ROOT}/registry/workers/Ren"
}

@test "an unbound start whose model.json cannot be written also refuses (a predecessor's record must not outlive it)" {
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE CREWVIA_BIND_TASK
    mkdir -p "${REPO_ROOT}/registry/workers"
    printf 'x' > "${REPO_ROOT}/registry/workers/Ren"
    run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 1 ]
    [[ "$output" == *"起動しません"* ]]
    rm -f "${REPO_ROOT}/registry/workers/Ren"
}

@test "CREWVIA_BIND_TASK is for workers only" {
    _mission opus
    CREWVIA_BIND_TASK=m-bind/t001 run bash "$START_SH" director --name Sora
    [ "$status" -eq 1 ]
}

@test "without CREWVIA_BIND_TASK the record's bound_task stays null" {
    unset CREWVIA_WORKER_MODEL CREWVIA_BIND_TASK
    run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    run _record Ren
    [[ "$output" == *'"bound_task": null'* ]]
}

# --- attempt 6: queue の明示・model.json は spawn の前・生きた Worker の記録は残る ---

_wrap_tmux() {   # _wrap_tmux <new-window/new-session のときに実行するシェル片> : 本物の fake tmux を包む
    mv "${FAKE_DIR}/tmux" "${FAKE_DIR}/tmux.real"
    printf '%s\n' '#!/usr/bin/env bash' \
        "case \"\${1:-}\" in new-session|new-window) $1 ;; esac" \
        "$2" \
        "exec \"${FAKE_DIR}/tmux.real\" \"\$@\"" > "${FAKE_DIR}/tmux"
    chmod +x "${FAKE_DIR}/tmux"
}

@test "model-for reads REPO_ROOT/queue even when the inherited CREWVIA_QUEUE points elsewhere" {
    _mission opus
    unset CREWVIA_WORKER_MODEL
    mkdir -p "${BATS_TEST_TMPDIR}/other-queue"
    CREWVIA_QUEUE="${BATS_TEST_TMPDIR}/other-queue" CREWVIA_PRINT_MODEL=1 CREWVIA_BIND_TASK=m-bind/t001 \
        run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    [ "$output" = "opus" ]
}

@test "model.json exists before the window is spawned" {
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE CREWVIA_BIND_TASK
    _wrap_tmux "[ -f \"${REPO_ROOT}/registry/workers/Ren/model.json\" ] && touch \"${BATS_TEST_TMPDIR}/had-record\"" ''
    run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    [ -f "${BATS_TEST_TMPDIR}/had-record" ]
}

@test "a refused spawn leaves a live Worker's model.json as it was" {
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE CREWVIA_BIND_TASK
    python3 "${REPO_ROOT}/scripts/lib_worker_model.py" record "${REPO_ROOT}/registry" Ren env opus m-old/t009 >/dev/null
    before="$(_record Ren)"
    # 窓が既にあり (list-windows)、中身は読めない = busy → spawn は 10/11 で断る
    _wrap_tmux 'true' 'case "${1:-}" in list-windows) echo "Ren-worker"; exit 0 ;; esac'
    run bash "$START_SH" worker --name Ren code python
    after="$(_record Ren)"
    [ "$before" = "$after" ]
    [[ "$after" == *'"bound_task": "m-old/t009"'* ]]
}


# --- attempt 7: 生きているかの判定は mux_spawn の 1 か所・記録はその後・起動の直前 ---

@test "a live window leaves model.json byte-for-byte unchanged and the record command never runs (exit 10/11 path)" {
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE CREWVIA_BIND_TASK
    python3 "${REPO_ROOT}/scripts/lib_worker_model.py" record "${REPO_ROOT}/registry" Ren env opus m-old/t009 >/dev/null
    f="${REPO_ROOT}/registry/workers/Ren/model.json"
    before="$(sha256sum "$f")"; mt_before="$(stat -c %Y.%y "$f")"
    _wrap_tmux 'true' 'case "${1:-}" in list-windows) echo "Ren-worker"; exit 0 ;; esac'
    run bash "$START_SH" worker --name Ren code python
    [ "$(sha256sum "$f")" = "$before" ]
    [ "$(stat -c %Y.%y "$f")" = "$mt_before" ]
    [[ "$output" == *"already running"* || "$output" == *"could not be read"* ]]
    [[ "$output" != *"Agent launched"* ]]
}

@test "when nothing is alive the record exists before the spawn (a fresh name)" {
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE CREWVIA_BIND_TASK
    rm -rf "${REPO_ROOT}/registry/workers/Ren"
    _wrap_tmux "[ -f \"${REPO_ROOT}/registry/workers/Ren/model.json\" ] && touch \"${BATS_TEST_TMPDIR}/had-record\"" ''
    run bash "$START_SH" worker --name Ren code python
    [ "$status" -eq 0 ]
    [ -f "${BATS_TEST_TMPDIR}/had-record" ]
    [ -f "${REPO_ROOT}/registry/workers/Ren/model.json" ]
}

@test "a spawn that fails for another reason removes the record this call wrote (the predecessor's is not restored)" {
    unset CREWVIA_WORKER_MODEL CREWVIA_QUEUE CREWVIA_BIND_TASK
    python3 "${REPO_ROOT}/scripts/lib_worker_model.py" record "${REPO_ROOT}/registry" Ren env opus m-old/t009 >/dev/null
    # 窓は居ない (list-windows は空) が new-window が失敗する → 記録は spawn の前に書かれ、失敗で消える
    _wrap_tmux 'touch "${BATS_TEST_TMPDIR}/hook-ran-before-new-window"; exit 1' ''
    run bash "$START_SH" worker --name Ren code python
    [ -f "${BATS_TEST_TMPDIR}/hook-ran-before-new-window" ]
    [[ "$output" != *"Agent launched"* ]]
    [ ! -e "${REPO_ROOT}/registry/workers/Ren/model.json" ]
}
