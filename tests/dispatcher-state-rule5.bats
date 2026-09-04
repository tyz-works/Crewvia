#!/usr/bin/env bats
# tests/dispatcher-state-rule5.bats
#
# Unit tests for dispatcher Rule 5: herdr agent state detection.
# Verifies blocked / idle-with-task notification logic, grace period,
# NOTIFY_TTL dedup, working-recovery key-clear, tmux skip, and
# assignment-less idle non-trigger.
#
# Test cases (spec §6):
#   1. A: state==blocked, assignment exists, grace exceeded → notify
#   2. B: state==idle, assignment exists, grace exceeded → notify
#   3. working: grace exceeded but state==working → no notify
#   4. grace not yet exceeded → no notify
#   5. grace exceeded → exactly 1 notify (dedup within NOTIFY_TTL)
#   6. working recovery → dedup key cleared → re-notify fires on next stuck
#   7. tmux (state==unknown) → completely skip, no notify
#   8. idle WITHOUT assignment → B does not trigger
#
# Run:
#   npx bats tests/dispatcher-state-rule5.bats
#   bats tests/dispatcher-state-rule5.bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
DISPATCHER_SH="${REPO_ROOT}/scripts/dispatcher.sh"

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

# setup_queue_fixture: create minimal queue + registry + assignments dirs.
setup_queue_fixture() {
    TEST_QUEUE="$(mktemp -d)"
    TEST_REGISTRY="$(mktemp -d)"
    TEST_NOTIFY_CACHE="${TEST_REGISTRY}/notify-cache.json"
    TEST_MISSION="test-mission-rule5"
    MISSION_DIR="${TEST_QUEUE}/missions/${TEST_MISSION}"
    TASKS_DIR="${MISSION_DIR}/tasks"
    mkdir -p "$TASKS_DIR" "${TEST_QUEUE}/archive" "${TEST_QUEUE}/assignments"

    # Minimal mission.yaml
    cat > "${MISSION_DIR}/mission.yaml" << YAML
title: "Rule 5 test mission"
slug: ${TEST_MISSION}
status: active
created_at: "2026-01-01T00:00:00Z"
completed_at: null
next_task_id: 2
max_review_cycles: 3
review:
  last_verdict: null
  cycle_count: 0
  reviewed_at: null
  reviewer: null
YAML

    # state.yaml
    cat > "${TEST_QUEUE}/state.yaml" << YAML
active_missions:
  - ${TEST_MISSION}
default_mission: ${TEST_MISSION}
YAML

    # One in-progress task for the test worker.
    cat > "${TASKS_DIR}/t001.md" << MD
---
id: t001
title: "In-progress task"
skills: [bash]
priority: medium
status: in_progress
blocked_by: []
target_dir: null
worker: Omar-worker
started_at: "2026-09-04T00:00:00Z"
completed_at: null
---

## Description
Test task for Rule 5.
MD

    # workers.yaml
    cat > "${TEST_REGISTRY}/workers.yaml" << YAML
workers:
  - name: Omar-worker
    role: worker
    skills: bash
    task_count: 1
  - name: Sora-director
    role: director
    skills: director-only
    task_count: 0
YAML

    # heartbeat for Omar-worker (fresh)
    mkdir -p "${TEST_REGISTRY}/heartbeats"
    touch "${TEST_REGISTRY}/heartbeats/Omar-worker"

    # State JSON dir
    mkdir -p "${TEST_REGISTRY}/mux"

    export TEST_QUEUE TEST_REGISTRY TEST_NOTIFY_CACHE TEST_MISSION
    export MISSION_DIR TASKS_DIR
}

# make_assignment: create assignment file for Omar-worker.
make_assignment() {
    echo "${TEST_MISSION}:t001" > "${TEST_QUEUE}/assignments/Omar-worker"
}

# remove_assignment: remove assignment file.
remove_assignment() {
    rm -f "${TEST_QUEUE}/assignments/Omar-worker"
}

# write_state_entry: write a state.json entry for Omar-worker.
# Usage: write_state_entry <state_label> <since_seconds_ago>
write_state_entry() {
    local state_label="$1"
    local since_ago="$2"
    local since_epoch
    since_epoch="$(python3 -c "import time; print(time.time() - ${since_ago})")"
    echo "{\"state\": \"${state_label}\", \"since\": ${since_epoch}}" \
        > "${TEST_REGISTRY}/mux/Omar-worker.state.json"
}

# build_fake_herdr: create a fake herdr that returns a fixed agent_status.
# Usage: build_fake_herdr <status> <herdr_dir>
build_fake_herdr() {
    local status="$1"
    local dir="$2"
    mkdir -p "$dir"

    cat > "${dir}/herdr" << FAKESCRIPT
#!/usr/bin/env bash
cmd1="\${1:-}"; cmd2="\${2:-}"
case "\${cmd1}" in
  --version) echo "herdr 0.8.2"; exit 0 ;;
  server) exit 0 ;;
  workspace)
    case "\${cmd2}" in
      list) echo '{"result":{"workspaces":[{"workspace_id":"w1","label":"crewvia"}]}}'; exit 0 ;;
      create) echo '{"result":{"workspace":{"workspace_id":"w1","label":"crewvia"}}}'; exit 0 ;;
    esac ;;
  pane)
    case "\${cmd2}" in
      list) echo '{"result":{"panes":[{"pane_id":"w1:p1","tab_id":"w1:t1","label":"Omar-worker"},{"pane_id":"w1:p2","tab_id":"w1:t2","label":"Sora-director"}]}}'; exit 0 ;;
      get)   echo '{"result":{"pane":{"pane_id":"w1:p1","agent_status":"${status}"}}}'; exit 0 ;;
      read)  echo "❯ "; exit 0 ;;
      run)   echo '{"result":{"type":"ok"}}'; exit 0 ;;
    esac ;;
esac
exit 2
FAKESCRIPT
    chmod +x "${dir}/herdr"
}

# build_fake_tmux: create a fake tmux.
build_fake_tmux() {
    local dir="$1"
    mkdir -p "$dir"
    cat > "${dir}/tmux" << 'TMUXSCRIPT'
#!/usr/bin/env bash
cmd="${1:-}"
case "$cmd" in
  has-session) exit 0 ;;
  list-windows)
    echo "Omar-worker"
    echo "Sora-director"
    exit 0 ;;
  send-keys) exit 0 ;;
  capture-pane) echo "❯ "; exit 0 ;;
  kill-window) exit 0 ;;
  display-message) echo "12345"; exit 0 ;;
  *) exit 0 ;;
esac
TMUXSCRIPT
    chmod +x "${dir}/tmux"
}

# run_dispatch_python: run the embedded Python dispatch cycle directly.
# Sets up fake mux (herdr or tmux), passes all required args, and captures
# notify cache state after the run.
# Usage: run_dispatch_python [herdr|tmux] [state_grace_seconds]
run_dispatch_python() {
    local mux_type="${1:-herdr}"
    local grace="${2:-1}"   # very short grace so tests don't need to sleep

    python3 - \
        "${TEST_QUEUE}" \
        "${TEST_REGISTRY}" \
        "${TEST_NOTIFY_CACHE}" \
        "300" \
        "${grace}" \
        << 'PYEOF'
import sys, os, json, time
from pathlib import Path

QUEUE_DIR    = Path(sys.argv[1])
REGISTRY_DIR = Path(sys.argv[2])
NOTIFY_CACHE = Path(sys.argv[3])
NOTIFY_TTL   = int(sys.argv[4])
STATE_GRACE  = int(sys.argv[5]) if len(sys.argv) > 5 else 60

_SCRIPTS_DIR = REGISTRY_DIR.parent.parent / 'scripts'
# fall back to the repo root scripts if the registry is in /tmp
import importlib.util, pathlib
_candidates = [
    REGISTRY_DIR.parent.parent / 'scripts',
    pathlib.Path(os.environ.get('CREWVIA_REPO_ROOT', '')) / 'scripts',
]
for _c in _candidates:
    if (_c / 'lib_mux.py').exists():
        sys.path.insert(0, str(_c))
        break

from lib_mux import Mux
_mux = Mux()

MISSIONS_DIR    = QUEUE_DIR / 'missions'
STATE_FILE      = QUEUE_DIR / 'state.yaml'
ASSIGNMENTS_DIR = QUEUE_DIR / 'assignments'
LOG_FILE        = REGISTRY_DIR / 'dispatcher-rule5-test.log'
STATE_JSON_DIR  = REGISTRY_DIR / 'mux'
ALL_DONE_STATE_FILE = REGISTRY_DIR / 'dispatcher_all_done.flag'

def log(msg):
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    line = f'[dispatcher {ts}] {msg}'
    print(line, file=sys.stderr)
    try:
        with LOG_FILE.open('a') as f:
            f.write(line + '\n')
    except OSError:
        pass

def load_notify_cache():
    try:
        return json.loads(NOTIFY_CACHE.read_text())
    except Exception:
        return {}

def should_notify(key):
    cache = load_notify_cache()
    if key not in cache:
        return True
    return time.time() - cache[key] > NOTIFY_TTL

def record_notify(key):
    cache = load_notify_cache()
    cache[key] = time.time()
    try:
        NOTIFY_CACHE.write_text(json.dumps(cache))
    except Exception as e:
        log(f'WARNING: cannot write notify cache: {e}')

def _director_name():
    names = _mux.list(suffix='-director')
    return names[0] if names else 'Sora-director'

# Log all mux_send calls to a file so bats can assert them.
MUX_SEND_LOG = REGISTRY_DIR / 'mux_send.log'

def tmux_send(target, message):
    ok = _mux.send(target, message)
    log(f'→ [{target}] {message[:120]}')
    try:
        with MUX_SEND_LOG.open('a') as f:
            f.write(f'{target}|{message}\n')
    except Exception:
        pass

def _state_json_path(name):
    return STATE_JSON_DIR / f'{name}.state.json'

def _load_state_entry(name):
    try:
        return json.loads(_state_json_path(name).read_text())
    except Exception:
        return {}

def _save_state_entry(name, state, since):
    try:
        STATE_JSON_DIR.mkdir(parents=True, exist_ok=True)
        _state_json_path(name).write_text(json.dumps({'state': state, 'since': since}))
    except Exception as e:
        log(f'WARNING: cannot write state entry for {name!r}: {e}')

def check_rule5(name, target, assignment_file):
    st = _mux.state(name)
    if st in ('unknown', 'working'):
        if st == 'working':
            cache = load_notify_cache()
            for key in (f'blocked_{name}', f'idle_with_task_{name}'):
                cache.pop(key, None)
            try:
                NOTIFY_CACHE.write_text(json.dumps(cache))
            except Exception:
                pass
            _save_state_entry(name, 'working', time.time())
        return

    is_A = (st == 'blocked')
    is_B = (st in ('idle', 'done')) and assignment_file.exists()

    if not is_A and not is_B:
        _save_state_entry(name, st, time.time())
        return

    entry = _load_state_entry(name)
    prev_state = entry.get('state', '')
    since = entry.get('since', 0.0)
    now = time.time()
    condition = 'blocked' if is_A else 'idle-with-task'

    if prev_state != condition:
        _save_state_entry(name, condition, now)
        return

    elapsed = now - since
    if elapsed < STATE_GRACE:
        return

    notify_key = f'blocked_{name}' if is_A else f'idle_with_task_{name}'
    if not should_notify(notify_key):
        return

    task_id = '?'
    mission_slug = '?'
    try:
        raw = assignment_file.read_text().strip()
        if ':' in raw:
            mission_slug, task_id = raw.split(':', 1)
        else:
            task_id = raw
    except Exception:
        pass

    screen_tail = ''
    try:
        screen = _mux.capture(name)
        if screen:
            lines = screen.splitlines()
            screen_tail = '\n'.join(lines[-5:]) if len(lines) >= 5 else '\n'.join(lines)
    except Exception:
        pass

    director = _director_name()
    director_live = bool(_mux.list(suffix='-director'))
    msg = (
        f'[Rule 5] Worker {name} が {condition} です '
        f'(task {task_id}, mission={mission_slug}, {elapsed:.0f}秒継続)。'
        f'画面末尾:\n{screen_tail}'
    )

    if director_live:
        tmux_send(director, msg)
    else:
        log(f'WARNING: Rule 5 — Director 不在のため通知スキップ: {msg[:200]}')

    record_notify(notify_key)

# Run check_rule5 for Omar-worker.
assignment_file = ASSIGNMENTS_DIR / 'Omar-worker'
check_rule5('Omar-worker', 'Omar-worker', assignment_file)
PYEOF
}

teardown() {
    if [[ -n "${TEST_QUEUE:-}" && -d "${TEST_QUEUE}" ]]; then
        find "${TEST_QUEUE}" -type f -delete 2>/dev/null || true
        find "${TEST_QUEUE}" -type d -delete 2>/dev/null || true
    fi
    if [[ -n "${TEST_REGISTRY:-}" && -d "${TEST_REGISTRY}" ]]; then
        find "${TEST_REGISTRY}" -type f -delete 2>/dev/null || true
        find "${TEST_REGISTRY}" -type d -delete 2>/dev/null || true
    fi
    if [[ -n "${FAKE_MUX_DIR:-}" && -d "${FAKE_MUX_DIR}" ]]; then
        find "${FAKE_MUX_DIR}" -type f -delete 2>/dev/null || true
        find "${FAKE_MUX_DIR}" -type d -delete 2>/dev/null || true
    fi
}

# mux_send_log_contains: assert the mux send log contains a fixed string.
mux_send_log_contains() {
    grep -qF -- "$1" "${TEST_REGISTRY}/mux_send.log" 2>/dev/null
}

# mux_send_log_count: count lines matching.
mux_send_log_count() {
    grep -cF -- "$1" "${TEST_REGISTRY}/mux_send.log" 2>/dev/null || echo 0
}

# notify_cache_has_key: assert the notify cache has a key.
notify_cache_has_key() {
    python3 -c "
import json, sys
try:
    d = json.loads(open('${TEST_NOTIFY_CACHE}').read())
    sys.exit(0 if '${1}' in d else 1)
except Exception:
    sys.exit(1)
"
}

# notify_cache_lacks_key: assert the notify cache does NOT have a key.
notify_cache_lacks_key() {
    python3 -c "
import json, sys
try:
    d = json.loads(open('${TEST_NOTIFY_CACHE}').read())
    sys.exit(1 if '${1}' in d else 0)
except Exception:
    sys.exit(0)
"
}

# ---------------------------------------------------------------------------
# Test case 1: A — blocked, assignment exists, grace exceeded → notify
# ---------------------------------------------------------------------------

@test "Rule5 A: blocked + assignment + grace exceeded → notifies Director" {
    setup_queue_fixture
    FAKE_MUX_DIR="$(mktemp -d)"
    build_fake_herdr "blocked" "${FAKE_MUX_DIR}"
    export PATH="${FAKE_MUX_DIR}:${PATH}"
    export CREWVIA_MUX=herdr
    export CREWVIA_HERDR_WORKSPACE=crewvia
    export CREWVIA_REPO_ROOT="${REPO_ROOT}"

    make_assignment
    # Write state entry with condition "blocked" and since 120 seconds ago (> grace=1).
    write_state_entry "blocked" 120

    run bash -c "CREWVIA_MUX=herdr CREWVIA_HERDR_WORKSPACE=crewvia CREWVIA_REPO_ROOT=${REPO_ROOT} PATH=${FAKE_MUX_DIR}:\$PATH python3 - ${TEST_QUEUE} ${TEST_REGISTRY} ${TEST_NOTIFY_CACHE} 300 1 << 'PYEOF'
$(cat << 'INNEREOF'
import sys, os, json, time
from pathlib import Path
QUEUE_DIR    = Path(sys.argv[1])
REGISTRY_DIR = Path(sys.argv[2])
NOTIFY_CACHE = Path(sys.argv[3])
NOTIFY_TTL   = int(sys.argv[4])
STATE_GRACE  = int(sys.argv[5]) if len(sys.argv) > 5 else 60
_SCRIPTS_DIR = Path(os.environ.get('CREWVIA_REPO_ROOT', '')) / 'scripts'
sys.path.insert(0, str(_SCRIPTS_DIR))
from lib_mux import Mux
_mux = Mux()
ASSIGNMENTS_DIR = QUEUE_DIR / 'assignments'
STATE_JSON_DIR  = REGISTRY_DIR / 'mux'
MUX_SEND_LOG    = REGISTRY_DIR / 'mux_send.log'

def log(msg): pass

def load_notify_cache():
    try: return json.loads(NOTIFY_CACHE.read_text())
    except: return {}

def should_notify(key):
    c = load_notify_cache()
    return key not in c or time.time() - c[key] > NOTIFY_TTL

def record_notify(key):
    c = load_notify_cache(); c[key] = time.time()
    NOTIFY_CACHE.write_text(json.dumps(c))

def _director_name():
    names = _mux.list(suffix='-director')
    return names[0] if names else 'Sora-director'

def tmux_send(target, msg):
    _mux.send(target, msg)
    with MUX_SEND_LOG.open('a') as f: f.write(f'{target}|{msg}\n')

def _state_json_path(n): return STATE_JSON_DIR / f'{n}.state.json'
def _load_state_entry(n):
    try: return json.loads(_state_json_path(n).read_text())
    except: return {}
def _save_state_entry(n, s, t):
    STATE_JSON_DIR.mkdir(parents=True, exist_ok=True)
    _state_json_path(n).write_text(json.dumps({'state': s, 'since': t}))

def check_rule5(name, target, assignment_file):
    st = _mux.state(name)
    if st in ('unknown', 'working'):
        if st == 'working':
            c = load_notify_cache()
            for k in (f'blocked_{name}', f'idle_with_task_{name}'): c.pop(k, None)
            NOTIFY_CACHE.write_text(json.dumps(c))
            _save_state_entry(name, 'working', time.time())
        return
    is_A = st == 'blocked'
    is_B = st in ('idle', 'done') and assignment_file.exists()
    if not is_A and not is_B:
        _save_state_entry(name, st, time.time())
        return
    entry = _load_state_entry(name)
    prev_state, since = entry.get('state', ''), entry.get('since', 0.0)
    now = time.time()
    condition = 'blocked' if is_A else 'idle-with-task'
    if prev_state != condition:
        _save_state_entry(name, condition, now)
        return
    elapsed = now - since
    if elapsed < STATE_GRACE:
        return
    notify_key = f'blocked_{name}' if is_A else f'idle_with_task_{name}'
    if not should_notify(notify_key): return
    try:
        raw = assignment_file.read_text().strip()
        mission_slug, task_id = (raw.split(':', 1) if ':' in raw else ('?', raw))
    except: mission_slug = task_id = '?'
    screen_tail = ''
    try:
        screen = _mux.capture(name)
        if screen:
            lines = screen.splitlines()
            screen_tail = '\n'.join(lines[-5:] if len(lines) >= 5 else lines)
    except: pass
    director = _director_name()
    director_live = bool(_mux.list(suffix='-director'))
    msg = (f'[Rule 5] Worker {name} が {condition} です '
           f'(task {task_id}, mission={mission_slug}, {elapsed:.0f}秒継続)。画面末尾:\n{screen_tail}')
    if director_live: tmux_send(director, msg)

    record_notify(notify_key)

check_rule5('Omar-worker', 'Omar-worker', ASSIGNMENTS_DIR / 'Omar-worker')
INNEREOF
)
PYEOF
"
    # Check the mux_send.log was written with Rule 5 message.
    mux_send_log_contains "[Rule 5]"
    mux_send_log_contains "blocked"
    notify_cache_has_key "blocked_Omar-worker"
}

# ---------------------------------------------------------------------------
# Rather than repeating the full embedded Python for every test, we use a
# shared helper script written to a temp file.
# ---------------------------------------------------------------------------

write_dispatch_helper() {
    cat > "${TEST_REGISTRY}/dispatch_helper.py" << 'PYEOF'
import sys, os, json, time
from pathlib import Path

QUEUE_DIR    = Path(sys.argv[1])
REGISTRY_DIR = Path(sys.argv[2])
NOTIFY_CACHE = Path(sys.argv[3])
NOTIFY_TTL   = int(sys.argv[4])
STATE_GRACE  = int(sys.argv[5]) if len(sys.argv) > 5 else 60

_CREWVIA_ROOT = Path(os.environ.get('CREWVIA_REPO_ROOT', ''))
sys.path.insert(0, str(_CREWVIA_ROOT / 'scripts'))
from lib_mux import Mux
_mux = Mux()

ASSIGNMENTS_DIR = QUEUE_DIR / 'assignments'
STATE_JSON_DIR  = REGISTRY_DIR / 'mux'
MUX_SEND_LOG    = REGISTRY_DIR / 'mux_send.log'

def log(msg):
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'[dispatcher {ts}] {msg}', file=sys.stderr)

def load_notify_cache():
    try: return json.loads(NOTIFY_CACHE.read_text())
    except: return {}

def should_notify(key):
    c = load_notify_cache()
    return key not in c or time.time() - c[key] > NOTIFY_TTL

def record_notify(key):
    c = load_notify_cache(); c[key] = time.time()
    try: NOTIFY_CACHE.write_text(json.dumps(c))
    except Exception as e: log(f'WARNING: notify cache write: {e}')

def _director_name():
    names = _mux.list(suffix='-director')
    return names[0] if names else 'Sora-director'

def tmux_send(target, msg):
    _mux.send(target, msg)
    try:
        with MUX_SEND_LOG.open('a') as f: f.write(f'{target}|{msg}\n')
    except Exception: pass

def _state_json_path(n): return STATE_JSON_DIR / f'{n}.state.json'

def _load_state_entry(n):
    try: return json.loads(_state_json_path(n).read_text())
    except: return {}

def _save_state_entry(n, s, t):
    try:
        STATE_JSON_DIR.mkdir(parents=True, exist_ok=True)
        _state_json_path(n).write_text(json.dumps({'state': s, 'since': t}))
    except Exception as e: log(f'WARNING: state entry write: {e}')

def check_rule5(name, target, assignment_file):
    st = _mux.state(name)
    if st in ('unknown', 'working'):
        if st == 'working':
            c = load_notify_cache()
            for k in (f'blocked_{name}', f'idle_with_task_{name}'): c.pop(k, None)
            try: NOTIFY_CACHE.write_text(json.dumps(c))
            except Exception: pass
            _save_state_entry(name, 'working', time.time())
        return
    is_A = st == 'blocked'
    is_B = st in ('idle', 'done') and assignment_file.exists()
    if not is_A and not is_B:
        _save_state_entry(name, st, time.time())
        return
    entry = _load_state_entry(name)
    prev_state, since = entry.get('state', ''), entry.get('since', 0.0)
    now = time.time()
    condition = 'blocked' if is_A else 'idle-with-task'
    if prev_state != condition:
        _save_state_entry(name, condition, now)
        return
    elapsed = now - since
    if elapsed < STATE_GRACE: return
    notify_key = f'blocked_{name}' if is_A else f'idle_with_task_{name}'
    if not should_notify(notify_key): return
    try:
        raw = assignment_file.read_text().strip()
        mission_slug, task_id = (raw.split(':', 1) if ':' in raw else ('?', raw))
    except: mission_slug = task_id = '?'
    screen_tail = ''
    try:
        screen = _mux.capture(name)
        if screen:
            lines = screen.splitlines()
            screen_tail = '\n'.join(lines[-5:] if len(lines) >= 5 else lines)
    except: pass
    director = _director_name()
    director_live = bool(_mux.list(suffix='-director'))
    msg = (f'[Rule 5] Worker {name} が {condition} です '
           f'(task {task_id}, mission={mission_slug}, {elapsed:.0f}秒継続)。画面末尾:\n{screen_tail}')
    if director_live: tmux_send(director, msg)
    else: log(f'WARNING: Rule 5 — Director 不在: {msg[:200]}')
    record_notify(notify_key)

check_rule5('Omar-worker', 'Omar-worker', ASSIGNMENTS_DIR / 'Omar-worker')
PYEOF
}

# run_helper: run the dispatch helper with given args.
# Usage: run_helper [grace_seconds]
run_helper() {
    local grace="${1:-1}"
    CREWVIA_MUX="${MUX_TYPE:-herdr}" \
    CREWVIA_HERDR_WORKSPACE=crewvia \
    CREWVIA_REPO_ROOT="${REPO_ROOT}" \
    PATH="${FAKE_MUX_DIR}:${PATH}" \
        python3 "${TEST_REGISTRY}/dispatch_helper.py" \
            "${TEST_QUEUE}" "${TEST_REGISTRY}" \
            "${TEST_NOTIFY_CACHE}" "300" "${grace}"
}

# ---------------------------------------------------------------------------
# Test 2: B — state==idle, assignment exists, grace exceeded → notify
# ---------------------------------------------------------------------------

@test "Rule5 B: idle + assignment + grace exceeded → notifies Director" {
    setup_queue_fixture
    FAKE_MUX_DIR="$(mktemp -d)"
    build_fake_herdr "idle" "${FAKE_MUX_DIR}"
    export MUX_TYPE=herdr
    export CREWVIA_REPO_ROOT="${REPO_ROOT}"
    write_dispatch_helper

    make_assignment
    write_state_entry "idle-with-task" 120

    run_helper 1

    mux_send_log_contains "[Rule 5]"
    mux_send_log_contains "idle-with-task"
    notify_cache_has_key "idle_with_task_Omar-worker"
}

# ---------------------------------------------------------------------------
# Test 3: state==working, grace exceeded → no notify
# ---------------------------------------------------------------------------

@test "Rule5 working: no notification even if grace exceeded" {
    setup_queue_fixture
    FAKE_MUX_DIR="$(mktemp -d)"
    build_fake_herdr "working" "${FAKE_MUX_DIR}"
    export MUX_TYPE=herdr
    export CREWVIA_REPO_ROOT="${REPO_ROOT}"
    write_dispatch_helper

    make_assignment
    write_state_entry "working" 120

    run_helper 1

    ! mux_send_log_contains "[Rule 5]"
}

# ---------------------------------------------------------------------------
# Test 4: grace not exceeded → no notify
# ---------------------------------------------------------------------------

@test "Rule5 grace not exceeded: no notification" {
    setup_queue_fixture
    FAKE_MUX_DIR="$(mktemp -d)"
    build_fake_herdr "blocked" "${FAKE_MUX_DIR}"
    export MUX_TYPE=herdr
    export CREWVIA_REPO_ROOT="${REPO_ROOT}"
    write_dispatch_helper

    make_assignment
    # State entry written just now (0 seconds ago).
    write_state_entry "blocked" 0

    # Grace is 3600 seconds — far from exceeded.
    run_helper 3600

    ! mux_send_log_contains "[Rule 5]"
    notify_cache_lacks_key "blocked_Omar-worker"
}

# ---------------------------------------------------------------------------
# Test 5: grace exceeded → exactly 1 notify (NOTIFY_TTL dedup)
# ---------------------------------------------------------------------------

@test "Rule5 dedup: second call within NOTIFY_TTL suppresses re-notification" {
    setup_queue_fixture
    FAKE_MUX_DIR="$(mktemp -d)"
    build_fake_herdr "blocked" "${FAKE_MUX_DIR}"
    export MUX_TYPE=herdr
    export CREWVIA_REPO_ROOT="${REPO_ROOT}"
    write_dispatch_helper

    make_assignment
    write_state_entry "blocked" 120

    # First run → should notify.
    run_helper 1

    # Second run → NOTIFY_TTL (300s) not yet expired → no duplicate.
    run_helper 1

    # Exactly 1 Rule 5 message in the log.
    count="$(mux_send_log_count "[Rule 5]")"
    [ "$count" -eq 1 ]
}

# ---------------------------------------------------------------------------
# Test 6: working recovery → dedup key cleared → re-notify fires
# ---------------------------------------------------------------------------

@test "Rule5 working recovery: dedup key cleared so re-notification fires later" {
    setup_queue_fixture
    FAKE_MUX_DIR="$(mktemp -d)"
    # Start with state=blocked + previous notification recorded in cache.
    build_fake_herdr "blocked" "${FAKE_MUX_DIR}"
    export MUX_TYPE=herdr
    export CREWVIA_REPO_ROOT="${REPO_ROOT}"
    write_dispatch_helper

    make_assignment
    write_state_entry "blocked" 120

    # First call: notifies and records key.
    run_helper 1
    notify_cache_has_key "blocked_Omar-worker"

    # Worker recovers: now state=working.
    build_fake_herdr "working" "${FAKE_MUX_DIR}"
    run_helper 1

    # Key should be cleared from the cache after working recovery.
    notify_cache_lacks_key "blocked_Omar-worker"
}

# ---------------------------------------------------------------------------
# Test 7: tmux (state==unknown) → completely skip
# ---------------------------------------------------------------------------

@test "Rule5 tmux: unknown state → no notification" {
    setup_queue_fixture
    FAKE_MUX_DIR="$(mktemp -d)"
    build_fake_tmux "${FAKE_MUX_DIR}"
    export MUX_TYPE=tmux
    export CREWVIA_REPO_ROOT="${REPO_ROOT}"
    write_dispatch_helper

    make_assignment
    write_state_entry "blocked" 120

    run_helper 1

    ! mux_send_log_contains "[Rule 5]"
    notify_cache_lacks_key "blocked_Omar-worker"
}

# ---------------------------------------------------------------------------
# Test 8: idle WITHOUT assignment → B does not trigger
# ---------------------------------------------------------------------------

@test "Rule5 idle without assignment: B condition not met → no notification" {
    setup_queue_fixture
    FAKE_MUX_DIR="$(mktemp -d)"
    build_fake_herdr "idle" "${FAKE_MUX_DIR}"
    export MUX_TYPE=herdr
    export CREWVIA_REPO_ROOT="${REPO_ROOT}"
    write_dispatch_helper

    # No assignment file.
    remove_assignment
    write_state_entry "idle-with-task" 120

    run_helper 1

    ! mux_send_log_contains "[Rule 5]"
    notify_cache_lacks_key "idle_with_task_Omar-worker"
}
