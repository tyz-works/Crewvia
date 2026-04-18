#!/usr/bin/env bash
# verifier-dispatcher.sh — Verifier Dispatcher daemon
#
# Polls every 30s for ready_for_verification tasks and assigns idle Verifiers.
#
# Idle Verifier detection:
#   registry/workers.yaml  — workers with 'verify' skill
#   queue/assignments/<name> — absent = idle, present = busy
#   tmux list-windows        — crewvia:<name>-verifier windows
#
# Self-verify prohibition: never assigns the same agent that was the task worker.
#
# Notification dedup: same key suppressed for NOTIFY_TTL seconds (default 60s).
# Standalone-safe: exits 0 silently when tmux is not available.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
QUEUE_DIR="${CREWVIA_QUEUE:-${REPO_ROOT}/queue}"
REGISTRY_DIR="${REPO_ROOT}/registry"
LOG_FILE="${REGISTRY_DIR}/verifier-dispatcher.log"
NOTIFY_CACHE="/tmp/verifier-dispatcher-notify-cache.$$.json"
NOTIFY_TTL="${NOTIFY_TTL:-60}"
POLL_INTERVAL="${VERIFIER_POLL_INTERVAL:-30}"

# Standalone-safe: silently exit when tmux is not installed
if ! command -v tmux &>/dev/null; then
  exit 0
fi

mkdir -p "$REGISTRY_DIR"

log() {
  local msg
  msg="[verifier-dispatcher $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
  echo "$msg" >&2
  echo "$msg" >> "$LOG_FILE"
}

log "Starting verifier-dispatcher (PID $$, poll=${POLL_INTERVAL}s, notify_ttl=${NOTIFY_TTL}s)"

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
from pathlib import Path
from datetime import datetime, timezone

QUEUE_DIR      = Path(sys.argv[1])
REGISTRY_DIR   = Path(sys.argv[2])
NOTIFY_CACHE   = Path(sys.argv[3])
NOTIFY_TTL     = int(sys.argv[4])

MISSIONS_DIR    = QUEUE_DIR / 'missions'
STATE_FILE      = QUEUE_DIR / 'state.yaml'
ASSIGNMENTS_DIR = QUEUE_DIR / 'assignments'
WORKERS_FILE    = REGISTRY_DIR / 'workers.yaml'
LOG_FILE        = REGISTRY_DIR / 'verifier-dispatcher.log'


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    line = f"[verifier-dispatcher {ts}] {msg}"
    print(line, file=sys.stderr)
    try:
        with LOG_FILE.open('a') as f:
            f.write(line + '\n')
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Minimal YAML parser (mirrors dispatcher.sh — no external deps)
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
    if val in ('null', '~', ''):
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


def parse_frontmatter(text):
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
    """Return dict {name: {'skills': [...], 'role': str}} from registry/workers.yaml."""
    if not WORKERS_FILE.exists():
        return {}
    workers = {}
    text = WORKERS_FILE.read_text()
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('- name:'):
            name = stripped[len('- name:'):].strip().strip('"\'')
            current = name
            workers[name] = {'skills': [], 'role': 'worker'}
        elif current and re.match(r'\s+skills:', line):
            m = re.search(r'\[([^\]]*)\]', line)
            if m:
                inner = m.group(1).strip()
                workers[current]['skills'] = [s.strip().strip('"\'') for s in inner.split(',')] if inner else []
        elif current and re.match(r'\s+role:', line):
            role = line.split(':', 1)[1].strip().strip('"\'')
            workers[current]['role'] = role
    return workers


def list_tasks_for_mission(slug):
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
            out.append((meta, body, path))
        except Exception as e:
            log(f"WARNING: failed to parse {path}: {e}")
    return out


# ---------------------------------------------------------------------------
# Task file update (atomic write to set verifier + status fields)
# ---------------------------------------------------------------------------

def _dump_scalar(s):
    s = str(s)
    if s == '':
        return '""'
    needs_quote = set(':#[]{},\'"\n&*!|>%@`')
    if any(ch in needs_quote for ch in s):
        return f'"{s}"'
    if s.lower() in ('true', 'false', 'null', 'yes', 'no', '~'):
        return f'"{s}"'
    if re.fullmatch(r'-?\d+', s):
        return f'"{s}"'
    return s


def update_task_fields(task_path, updates):
    """Atomically set specific frontmatter fields in a task .md file."""
    text = task_path.read_text()
    lines = text.split('\n')
    in_fm = False
    result_lines = []
    updated_keys = set()
    fm_end_idx = None

    for idx, line in enumerate(lines):
        if line.strip() == '---':
            if not in_fm:
                in_fm = True
                result_lines.append(line)
                continue
            else:
                # End of frontmatter — insert any keys we haven't seen yet
                for k, v in updates.items():
                    if k not in updated_keys:
                        val_str = 'null' if v is None else _dump_scalar(str(v))
                        result_lines.append(f'{k}: {val_str}')
                        updated_keys.add(k)
                in_fm = False
                result_lines.append(line)
                continue

        if in_fm:
            m = re.match(r'^([\w-]+):\s*', line)
            if m and m.group(1) in updates:
                k = m.group(1)
                v = updates[k]
                val_str = 'null' if v is None else _dump_scalar(str(v))
                result_lines.append(f'{k}: {val_str}')
                updated_keys.add(k)
                continue

        result_lines.append(line)

    new_text = '\n'.join(result_lines)
    tmp = str(task_path) + f'.tmp.{os.getpid()}'
    try:
        with open(tmp, 'w') as f:
            f.write(new_text)
        os.replace(tmp, str(task_path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Notification dedup cache (mirrors dispatcher.sh)
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
# tmux helpers (mirrors dispatcher.sh 2-step send-keys pattern)
# ---------------------------------------------------------------------------

def tmux_list_verifier_windows():
    """Return list of {'window_target', 'agent_name'} for crewvia:*-verifier windows."""
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
            if name.endswith('-verifier'):
                agent_name = name[:-len('-verifier')]
                windows.append({'window_target': f'crewvia:{name}', 'agent_name': agent_name})
        return windows
    except Exception as e:
        log(f"WARNING: tmux list-windows failed: {e}")
        return []


def tmux_send(target, message):
    """Send message to tmux window (2-step: message then Enter separately)."""
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


# ---------------------------------------------------------------------------
# Main dispatch logic
# ---------------------------------------------------------------------------

def dispatch():
    state = load_state()
    active_missions = list(state.get('active_missions') or [])
    if not active_missions:
        return

    # Collect all ready_for_verification tasks across active missions
    rfv_tasks = []
    for slug in active_missions:
        tasks = list_tasks_for_mission(slug)
        for meta, _, path in tasks:
            if meta.get('status') == 'ready_for_verification':
                rfv_tasks.append((slug, meta, path))

    if not rfv_tasks:
        return

    log(f"found {len(rfv_tasks)} ready_for_verification task(s)")

    # Load workers with verify skill (exclude directors)
    all_workers = load_workers()
    verify_workers = {
        name for name, info in all_workers.items()
        if 'verify' in (info.get('skills') or [])
        and info.get('role') != 'director'
    }

    # Find live verifier tmux windows
    verifier_windows = tmux_list_verifier_windows()
    live_verifier_map = {w['agent_name']: w for w in verifier_windows}

    # Track assigned verifiers this cycle to avoid double-dispatch
    assigned_verifiers = set()

    for slug, meta, task_path in rfv_tasks:
        task_id = meta.get('id', '?')
        task_worker = meta.get('worker') or ''

        # Find an idle verifier: verify skill + live window + idle + not the worker
        chosen = None
        for agent_name, window in live_verifier_map.items():
            if agent_name == task_worker:
                log(f"skip {agent_name}: self-verify prohibited (task worker={task_worker})")
                continue
            if agent_name not in verify_workers:
                log(f"skip {agent_name}: not in verify skill set")
                continue
            if agent_name in assigned_verifiers:
                continue  # already assigned this cycle
            assignment_file = ASSIGNMENTS_DIR / agent_name
            if assignment_file.exists():
                log(f"skip {agent_name}: busy (assignment file present)")
                continue
            chosen = (agent_name, window)
            break

        if chosen:
            agent_name, window = chosen
            log(f"assigning: task {task_id} (mission={slug}) → verifier {agent_name}")
            try:
                update_task_fields(task_path, {
                    'verifier': agent_name,
                    'status': 'verifying',
                })
                msg = (
                    f"タスク {task_id} (mission={slug}) の検証をしてください。"
                    f"plan.sh verify-result {task_id} <pass|fail|needs_human_review>"
                    f" [--notes \"...\"] で結果を記録してください。"
                )
                tmux_send(window['window_target'], msg)
                assigned_verifiers.add(agent_name)
            except Exception as e:
                log(f"ERROR: failed to assign task {task_id} to {agent_name}: {e}")
        else:
            # No idle verifier available — notify Director (dedup)
            notify_key = f"no_verifier_{task_id}"
            if should_notify(notify_key):
                has_any_verifier = bool(verify_workers)
                if has_any_verifier:
                    reason = "全 Verifier がビジー中または同一 worker"
                else:
                    reason = "verify スキルを持つ Verifier が未登録"
                msg = (
                    f"Verifier Dispatcher: task {task_id} (mission={slug}) が"
                    f" ready_for_verification だが idle Verifier がいない ({reason})。"
                    f"verify スキルの Verifier を起動してください。"
                )
                tmux_send('crewvia:Sora-director', msg)
                record_notify(notify_key)
                log(f"no idle verifier for task {task_id}: notified director")


dispatch()
PYEOF
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while true; do
  run_dispatch || log "dispatch cycle error (exit $?)"
  sleep "$POLL_INTERVAL"
done
