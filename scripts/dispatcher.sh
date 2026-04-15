#!/usr/bin/env bash
# dispatcher.sh — Crewvia Worker dispatcher daemon (bg, 5-second poll)
#
# Observes:
#   queue/missions/**/tasks/*.md   pending tasks
#   tmux list-windows              live Worker / Director windows
#   queue/assignments/<NAME>       busy (exists) / idle (absent) state
#
# Dispatch logic (every 5 s):
#   idle Worker + unblocked pending task with skill intersection
#     → tmux send-keys assign message to Worker window
#   unblocked pending task with NO matching live Worker
#     → notify crewvia:Sora-director to spawn a Worker
#   Worker with zero tasks for its skill set (blocked included)
#     → send shutdown message and kill the window
#   All active missions done
#     → notify crewvia:Sora-director
#
# Notification dedup: same key is suppressed for NOTIFY_TTL seconds.
# Standalone-safe: exits 0 silently when tmux is not available.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
QUEUE_DIR="${CREWVIA_QUEUE:-${REPO_ROOT}/queue}"
REGISTRY_DIR="${REPO_ROOT}/registry"
LOG_FILE="${REGISTRY_DIR}/dispatcher.log"
NOTIFY_CACHE="/tmp/dispatcher-notify-cache.$$.json"
NOTIFY_TTL=60   # seconds before repeating the same notification

# Standalone-safe: silently exit when tmux is not installed
if ! command -v tmux &>/dev/null; then
  exit 0
fi

mkdir -p "$REGISTRY_DIR"

log() {
  local msg
  msg="[dispatcher $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
  echo "$msg" >&2
  echo "$msg" >> "$LOG_FILE"
}

log "Starting dispatcher (PID $$, queue=$QUEUE_DIR)"

# ---------------------------------------------------------------------------
# One dispatch cycle — implemented in Python for YAML / file parsing
# ---------------------------------------------------------------------------
run_dispatch() {
  python3 - "$QUEUE_DIR" "$REGISTRY_DIR" "$NOTIFY_CACHE" "$NOTIFY_TTL" <<'PYEOF'
import sys
import os
import re
import json
import time
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

QUEUE_DIR      = Path(sys.argv[1])
REGISTRY_DIR   = Path(sys.argv[2])
NOTIFY_CACHE   = Path(sys.argv[3])
NOTIFY_TTL     = int(sys.argv[4])

MISSIONS_DIR   = QUEUE_DIR / 'missions'
ARCHIVE_DIR    = QUEUE_DIR / 'archive'
STATE_FILE     = QUEUE_DIR / 'state.yaml'
ASSIGNMENTS_DIR = QUEUE_DIR / 'assignments'
WORKERS_FILE   = REGISTRY_DIR / 'workers.yaml'
LOG_FILE       = REGISTRY_DIR / 'dispatcher.log'

PRIORITY_ORDER  = {'high': 0, 'medium': 1, 'low': 2}
TERMINAL_STATUSES = {'done', 'skipped'}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    line = f"[dispatcher {ts}] {msg}"
    print(line, file=sys.stderr)
    try:
        with LOG_FILE.open('a') as f:
            f.write(line + '\n')
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Minimal YAML parser (scalar fields + inline/block lists, no external deps)
# ---------------------------------------------------------------------------

def _split_inline_list(s):
    out, cur, in_q = [], [], None
    for ch in s:
        if in_q:
            cur.append(ch)
            if ch == in_q:
                in_q = None
            continue
        if ch in ('"', "'"):
            in_q = ch
            cur.append(ch)
            continue
        if ch == ',':
            out.append(''.join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        out.append(''.join(cur).strip())
    return [x for x in out if x]


def _scalar(val):
    if val in ('null', '~'):
        return None
    if val in ('true', 'True'):
        return True
    if val in ('false', 'False'):
        return False
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return val[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    if re.fullmatch(r'-?\d+', val):
        return int(val)
    return val


def parse_yaml(text):
    """Parse a minimal subset of YAML used in this project."""
    lines = text.splitlines()
    result = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            i += 1
            continue
        m = re.match(r'^([\w-]+):\s*(.*)$', line)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).rstrip()
        if val == '':
            i += 1
            items = []
            while i < len(lines):
                lst = re.match(r'^\s+-\s*(.*)$', lines[i])
                if lst:
                    items.append(_scalar(lst.group(1).strip()))
                    i += 1
                else:
                    break
            result[key] = items if items else None
        elif val.startswith('[') and val.endswith(']'):
            inner = val[1:-1].strip()
            result[key] = [_scalar(s.strip()) for s in _split_inline_list(inner)] if inner else []
            i += 1
        else:
            result[key] = _scalar(val)
            i += 1
    return result


# ---------------------------------------------------------------------------
# Frontmatter parser for task .md files
# ---------------------------------------------------------------------------

def parse_frontmatter(text):
    """Return (meta dict, body string) from a task .md file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}, text
    end = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() == '---':
            end = idx
            break
    if end < 0:
        return {}, text
    front = '\n'.join(lines[1:end])
    body = '\n'.join(lines[end + 1:])
    meta = parse_yaml(front)
    meta.setdefault('skills', [])
    meta.setdefault('blocked_by', [])
    if meta.get('skills') is None:
        meta['skills'] = []
    if meta.get('blocked_by') is None:
        meta['blocked_by'] = []
    return meta, body


# ---------------------------------------------------------------------------
# State / workers / tasks loading
# ---------------------------------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return {}
    return parse_yaml(STATE_FILE.read_text())


def load_workers():
    """Return dict {name: {'skills': [...], ...}} from registry/workers.yaml."""
    if not WORKERS_FILE.exists():
        return {}
    data = parse_yaml(WORKERS_FILE.read_text())
    workers = {}
    # workers.yaml has a top-level 'workers' block list
    # parse_yaml returns it as a list of scalars which isn't right.
    # We need a proper block-list-of-mappings parser.
    # Instead, parse manually.
    text = WORKERS_FILE.read_text()
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('- name:'):
            name = stripped[len('- name:'):].strip().strip('"\'')
            current = name
            workers[name] = {'skills': [], 'task_count': 0}
        elif current and re.match(r'\s+skills:', line):
            m = re.search(r'\[([^\]]*)\]', line)
            if m:
                inner = m.group(1).strip()
                if inner:
                    workers[current]['skills'] = [s.strip() for s in inner.split(',')]
                else:
                    workers[current]['skills'] = []
        elif current and re.match(r'\s+task_count:', line):
            m = re.search(r'task_count:\s*(\d+)', line)
            if m:
                workers[current]['task_count'] = int(m.group(1))
        elif current and re.match(r'\s+role:', line):
            role = line.split(':', 1)[1].strip()
            workers[current]['role'] = role
    return workers


def list_tasks_for_mission(slug):
    """Return list of (meta, body) sorted by task number."""
    tdir = MISSIONS_DIR / slug / 'tasks'
    if not tdir.exists():
        return []
    entries = []
    for fn in tdir.iterdir():
        m = re.fullmatch(r't(\d+)\.md', fn.name)
        if m:
            entries.append((int(m.group(1)), fn))
    entries.sort()
    out = []
    for _, path in entries:
        try:
            meta, body = parse_frontmatter(path.read_text())
            out.append((meta, body))
        except Exception as e:
            log(f"WARNING: failed to parse {path}: {e}")
    return out


def load_all_tasks(active_missions):
    """Return (all_tasks, done_ids_by_mission) where all_tasks is list of (slug, meta)."""
    all_tasks = []
    done_ids_by_mission = {}
    for slug in active_missions:
        tasks = list_tasks_for_mission(slug)
        done_ids = {m['id'] for m, _ in tasks if m.get('status') in TERMINAL_STATUSES}
        done_ids_by_mission[slug] = done_ids
        for meta, _ in tasks:
            all_tasks.append((slug, meta))
    return all_tasks, done_ids_by_mission


# ---------------------------------------------------------------------------
# Notification dedup cache
# ---------------------------------------------------------------------------

def load_notify_cache():
    if not NOTIFY_CACHE.exists():
        return {}
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
    except OSError as e:
        log(f"WARNING: cannot write notify cache: {e}")


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

def tmux_list_worker_windows():
    """Return list of {'window_target': str, 'agent_name': str} for crewvia:*-worker windows."""
    try:
        r = subprocess.run(
            ['tmux', 'list-windows', '-t', 'crewvia', '-F', '#{window_name}'],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return []
        windows = []
        for line in r.stdout.splitlines():
            name = line.strip()
            if name.endswith('-worker'):
                agent_name = name[:-len('-worker')]
                windows.append({'window_target': f'crewvia:{name}', 'agent_name': agent_name})
        return windows
    except Exception as e:
        log(f"WARNING: tmux list-windows failed: {e}")
        return []


def tmux_send(target, message):
    """Send a message to a tmux window (Enter-terminated).

    Split into two send-keys invocations: Claude TUI's bracketed paste
    swallows Enter when message+Enter arrive in one burst, so Enter must
    be delivered as a separate key event after the paste closes.
    """
    try:
        subprocess.run(
            ['tmux', 'send-keys', '-t', target, message],
            capture_output=True, timeout=5,
        )
        time.sleep(0.1)
        subprocess.run(
            ['tmux', 'send-keys', '-t', target, 'Enter'],
            capture_output=True, timeout=5,
        )
        log(f"→ [{target}] {message[:120]}")
    except Exception as e:
        log(f"WARNING: tmux send-keys to {target} failed: {e}")


def tmux_kill_window(target):
    """Kill a tmux window."""
    try:
        subprocess.run(
            ['tmux', 'kill-window', '-t', target],
            capture_output=True, timeout=5,
        )
        log(f"killed window: {target}")
    except Exception as e:
        log(f"WARNING: tmux kill-window {target} failed: {e}")


# ---------------------------------------------------------------------------
# Main dispatch logic
# ---------------------------------------------------------------------------

AGENT_PRESENCE_TTL = 600  # 10 minutes — heartbeat files older than this are ignored


def publish_agents():
    """Publish active agents to Taskvia /api/agents every dispatch cycle.

    Director: always published (from registry role=director).
    Workers: published only when registry/heartbeats/<name> mtime is within
    AGENT_PRESENCE_TTL (10 min) — i.e. the Worker has been seen recently.
    TASKVIA_TOKEN not set → silently skip (standalone mode).
    """
    taskvia_url = os.environ.get('TASKVIA_URL', 'https://taskvia.vercel.app')
    taskvia_token = os.environ.get('TASKVIA_TOKEN', '')
    if os.environ.get('CREWVIA_TASKVIA') == 'disabled' or not taskvia_token:
        return

    now = time.time()
    workers = load_workers()
    agents_to_publish = []

    # Collect heartbeat mtimes for all agents
    heartbeats_dir = REGISTRY_DIR / 'heartbeats'
    hb_mtimes = {}
    if heartbeats_dir.exists():
        for hb_file in heartbeats_dir.iterdir():
            if hb_file.is_file() and not hb_file.name.startswith('.'):
                try:
                    hb_mtimes[hb_file.name] = hb_file.stat().st_mtime
                except OSError:
                    pass

    for name, info in workers.items():
        role = info.get('role', 'worker')
        skills = info.get('skills') or []

        if role == 'director':
            # Director is always published regardless of heartbeat TTL.
            # Use heartbeat mtime for last_seen if available, else current time.
            mtime = hb_mtimes.get(name, now)
            last_seen = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            agents_to_publish.append({
                'name': name,
                'role': role,
                'skills': skills,
                'current_task_id': None,
                'current_task_title': None,
                'last_seen': last_seen,
            })
        else:
            # Workers must have a fresh heartbeat file.
            mtime = hb_mtimes.get(name)
            if mtime is None or now - mtime > AGENT_PRESENCE_TTL:
                continue

            # Resolve current task from assignments file.
            task_id = None
            task_title = None
            assignment_file = ASSIGNMENTS_DIR / name
            if assignment_file.exists():
                try:
                    assignment = assignment_file.read_text().strip()
                    # Format: "mission_slug:task_id"
                    if ':' in assignment:
                        mission_slug, task_id = assignment.split(':', 1)
                        task_file = MISSIONS_DIR / mission_slug / 'tasks' / f'{task_id}.md'
                        if task_file.exists():
                            meta, _ = parse_frontmatter(task_file.read_text())
                            task_title = meta.get('title')
                except Exception:
                    pass

            last_seen = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            agents_to_publish.append({
                'name': name,
                'role': role,
                'skills': skills,
                'current_task_id': task_id,
                'current_task_title': task_title,
                'last_seen': last_seen,
            })

    endpoint = f'{taskvia_url}/api/agents'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {taskvia_token}',
    }

    for agent in agents_to_publish:
        payload = json.dumps(agent).encode('utf-8')
        try:
            req = urllib.request.Request(endpoint, data=payload, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as e:
            log(f"WARNING: /api/agents publish failed for {agent['name']}: {e}")


def dispatch():
    state = load_state()
    active_missions = list(state.get('active_missions') or [])

    # No active missions — nothing to dispatch, stay silent
    if not active_missions:
        return

    workers = load_workers()

    # Check if all active missions are done (empty list = all done)
    all_done = True
    for slug in active_missions:
        mfile = MISSIONS_DIR / slug / 'mission.yaml'
        if not mfile.exists():
            all_done = False
            break
        m = parse_yaml(mfile.read_text())
        if m.get('status') != 'done':
            all_done = False
            break

    if all_done:
        # Shut down all idle workers before notifying director
        windows = tmux_list_worker_windows()
        for window in windows:
            agent_name = window['agent_name']
            target = window['window_target']
            assignment_file = ASSIGNMENTS_DIR / agent_name
            is_idle = not assignment_file.exists()
            if is_idle:
                notify_key = f"shutdown_{agent_name}"
                if should_notify(notify_key):
                    tmux_send(target, 'タスクなし、shutdown')
                    record_notify(notify_key)
                    time.sleep(1)
                    tmux_kill_window(target)

        key = 'all_missions_done'
        if should_notify(key):
            tmux_send('crewvia:Sora-director', '全ミッション完了')
            record_notify(key)
        return

    # Load all tasks
    all_tasks, done_ids_by_mission = load_all_tasks(active_missions)

    # Unblocked pending tasks (eligible for assignment), sorted by priority
    unblocked_pending = []
    for slug, meta in all_tasks:
        if meta.get('status') != 'pending':
            continue
        done_ids = done_ids_by_mission.get(slug, set())
        bb = meta.get('blocked_by') or []
        if any(dep not in done_ids for dep in bb):
            continue
        task_skills = set(meta.get('skills') or [])
        if not task_skills:
            log(f"WARNING: task {meta.get('id')} (mission={slug}) has no skills — dispatcher cannot assign it")
            continue
        unblocked_pending.append((slug, meta))
    unblocked_pending.sort(key=lambda c: PRIORITY_ORDER.get(c[1].get('priority', 'medium'), 1))

    # All pending tasks (including blocked), for shutdown eligibility check
    all_pending = [(slug, meta) for slug, meta in all_tasks if meta.get('status') == 'pending']

    # Live Worker windows
    windows = tmux_list_worker_windows()

    # Track which tasks were assigned this cycle to avoid double-dispatch
    assigned_task_ids = set()

    for window in windows:
        agent_name = window['agent_name']
        target = window['window_target']

        worker_info = workers.get(agent_name)
        if worker_info is None:
            log(f"unknown worker in tmux: {agent_name} — not in registry/workers.yaml, skipping")
            continue

        worker_skills = set(worker_info.get('skills') or [])

        # Idle = no assignment file
        assignment_file = ASSIGNMENTS_DIR / agent_name
        is_idle = not assignment_file.exists()

        if not is_idle:
            continue  # Worker is busy; do not interrupt

        # Find best unblocked pending task with skill match
        best = None
        for slug, meta in unblocked_pending:
            if meta['id'] in assigned_task_ids:
                continue
            task_skills = set(meta.get('skills') or [])
            if task_skills.issubset(worker_skills):
                best = (slug, meta)
                break

        if best:
            slug, meta = best
            task_id = meta['id']
            notify_key = f"assign_{agent_name}_{task_id}"
            if should_notify(notify_key):
                msg = (
                    f"タスク {task_id} (mission={slug}) を実行して。"
                    f"plan.sh pull --task {task_id} --mission {slug} で取得後、"
                    f"作業→plan.sh done で完了。"
                )
                tmux_send(target, msg)
                record_notify(notify_key)
                assigned_task_ids.add(task_id)
        else:
            # No unblocked pending task for this worker.
            # If there are ZERO tasks (even blocked) matching this worker's skills
            # across all active missions → worker is no longer needed.
            has_any = any(
                set(meta.get('skills') or []).issubset(worker_skills)
                for _, meta in all_pending
            )
            # Defense-in-depth: also keep the worker alive if it owns an
            # in_progress task.  plan.sh pull writes the assignment file before
            # the Taskvia sync, but there is still a narrow window between
            # save_task (task→in_progress) and the assignment file write where
            # the dispatcher could see is_idle=True + no pending tasks.
            has_in_progress = any(
                meta.get('worker') == agent_name
                for _, meta in all_tasks
                if meta.get('status') == 'in_progress'
            )
            if not has_any and not has_in_progress:
                notify_key = f"shutdown_{agent_name}"
                if should_notify(notify_key):
                    tmux_send(target, 'タスクなし、shutdown')
                    record_notify(notify_key)
                    time.sleep(1)  # allow the message to land before killing
                    tmux_kill_window(target)

    # Notify Sora about unblocked pending tasks that NO live worker can handle
    for slug, meta in unblocked_pending:
        task_id = meta['id']
        task_skills = set(meta.get('skills') or [])
        can_handle = any(
            task_skills.issubset(set((workers.get(w['agent_name']) or {}).get('skills') or []))
            for w in windows
        )
        if not can_handle:
            notify_key = f"no_worker_{task_id}"
            if should_notify(notify_key):
                msg = (
                    f"要求スキル {sorted(task_skills)} の Worker を起動してください "
                    f"(task {task_id}, mission={slug})"
                )
                tmux_send('crewvia:Sora-director', msg)
                record_notify(notify_key)


publish_agents()
dispatch()
PYEOF
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while true; do
  run_dispatch || log "dispatch cycle error (exit $?)"
  sleep 5
done
