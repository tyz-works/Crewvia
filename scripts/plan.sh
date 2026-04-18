#!/usr/bin/env bash
set -euo pipefail

# plan.sh — Crewvia タスクプラン管理 CLI（per-task / multi-mission 版）
#
# Layout:
#   queue/
#     state.yaml                    active mission slugs + default_mission
#     missions/<slug>/
#       mission.yaml                title / status / next_task_id
#       tasks/tNNN.md               frontmatter + body (Description / Result)
#     archive/                      完了済み mission の退避先
#     .lock                         fcntl 排他ロックファイル
#
# Usage:
#   plan.sh init "<title>" [--mission <slug>] [--force]
#   plan.sh add  "<title>" [--mission <slug>] --skills <csv> [--blocked-by <csv>]
#                          [--priority high|medium|low] [--description <text>]
#   plan.sh pull [--mission <slug>] --skills <csv> [--agent <name>]
#   plan.sh done <task_id> "<result>" [--mission <slug>]
#   plan.sh fail <task_id> [<handoff_path>] [--mission <slug>]
#   plan.sh status [--mission <slug>] [--all]
#   plan.sh archive <slug>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
QUEUE_DIR="${CREWVIA_QUEUE:-${REPO_ROOT}/queue}"

if [[ $# -eq 0 ]]; then
  echo "Usage: plan.sh <init|add|pull|done|fail|ready-for-verification|verify-result|review|launch|lint|status|archive> [args...]" >&2
  exit 1
fi

SUBCOMMAND="$1"
shift

mkdir -p "$QUEUE_DIR" "$QUEUE_DIR/missions" "$QUEUE_DIR/archive"

# Delegate to Python3
python3 - "$QUEUE_DIR" "$SUBCOMMAND" "$@" <<'PYEOF'
import sys
import os
import json
import fcntl
import re
import shutil
import hashlib
import urllib.request
from datetime import datetime, timezone

QUEUE_DIR = sys.argv[1]
SUBCOMMAND = sys.argv[2]
ARGS = sys.argv[3:]

STATE_FILE = os.path.join(QUEUE_DIR, 'state.yaml')
MISSIONS_DIR = os.path.join(QUEUE_DIR, 'missions')
ARCHIVE_DIR = os.path.join(QUEUE_DIR, 'archive')
LOCK_FILE = os.path.join(QUEUE_DIR, '.lock')

PRIORITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}
TERMINAL_STATUSES = {'done', 'verified', 'skipped'}

STATUS_ICON = {
    'done': '✅',
    'verified': '✅',
    'in_progress': '🔄',
    'pending': '📋',
    'skipped': '⏭️',
    'failed': '❌',
    'ready_for_verification': '🔍',
    'verifying': '🔎',
    'verification_failed': '⚠️',
    'needs_human_review': '👁️',
}


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# Minimal YAML helpers (narrow subset, no external deps)
# ---------------------------------------------------------------------------

def parse_yaml(text, source='<yaml>'):
    """Parse a narrow subset: scalar fields, inline lists, block lists.

    Unrecognized lines raise instead of being silently dropped, so a hand-edit
    typo (e.g. missing colon, mis-indented block list) cannot quietly produce a
    half-loaded dict.
    """
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
            if line and line[0] in (' ', '\t'):
                # Deeply nested / orphaned indented line (e.g. inside verification.commands)
                # — skip silently to maintain backward compatibility with unknown block structures
                i += 1
                continue
            raise ValueError(
                f"{source}: malformed line {i + 1}: {line!r} "
                f"(expected `key: value`, `key: [a, b]`, or `key:` followed by `  - item` lines)"
            )
        key = m.group(1)
        val = m.group(2).rstrip()
        if val == '':
            # Possible block list (`- item`) or block mapping (`  key: val`)
            i += 1
            items = []
            sub_dict = {}
            while i < len(lines):
                lst = re.match(r'^\s+-\s*(.*)$', lines[i])
                if lst:
                    items.append(_scalar(lst.group(1).strip()))
                    i += 1
                else:
                    map_m = re.match(r'^  ([\w-]+):\s*(.*)$', lines[i])
                    if map_m:
                        sub_key = map_m.group(1)
                        sub_val = _scalar(map_m.group(2).rstrip())
                        sub_dict[sub_key] = sub_val
                        i += 1
                    else:
                        break
            if items:
                result[key] = items
            elif sub_dict:
                result[key] = sub_dict
            else:
                result[key] = None
        elif val.startswith('[') and val.endswith(']'):
            inner = val[1:-1].strip()
            if not inner:
                result[key] = []
            else:
                result[key] = [_scalar(s.strip()) for s in _split_inline_list(inner)]
            i += 1
        else:
            result[key] = _scalar(val)
            i += 1
    return result


def _split_inline_list(s):
    """Split inline list respecting quoted strings."""
    out = []
    cur = []
    in_q = None
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
    if val == 'null' or val == '~':
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


def dump_yaml(data, key_order=None):
    """Serialize a flat dict (with optional list values) to YAML."""
    lines = []
    keys = key_order if key_order else list(data.keys())
    # Append any keys not in key_order
    if key_order:
        for k in data.keys():
            if k not in keys:
                keys.append(k)
    for k in keys:
        if k not in data:
            continue
        v = data[k]
        lines.append(_dump_kv(k, v))
    return '\n'.join(lines) + '\n'


def _dump_kv(key, val):
    if val is None:
        return f"{key}: null"
    if isinstance(val, bool):
        return f"{key}: {'true' if val else 'false'}"
    if isinstance(val, int):
        return f"{key}: {val}"
    if isinstance(val, dict):
        lines = [f"{key}:"]
        for k, v in val.items():
            lines.append(f"  {k}: {_dump_inline(v)}")
        return '\n'.join(lines)
    if isinstance(val, list):
        if not val:
            return f"{key}: []"
        # Always inline — our lists are short (skills, blocked_by)
        items = ', '.join(_dump_inline(x) for x in val)
        return f"{key}: [{items}]"
    return f"{key}: {_dump_scalar(str(val))}"


_NEEDS_QUOTE = set(':#[]{},\'"\n&*!|>%@`')


def _dump_scalar(s):
    if s == '':
        return '""'
    if any(ch in _NEEDS_QUOTE for ch in s):
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if s.lower() in ('true', 'false', 'null', 'yes', 'no', '~'):
        return f'"{s}"'
    if re.fullmatch(r'-?\d+', s):
        return f'"{s}"'
    return s


def _dump_inline(val):
    if val is None:
        return 'null'
    if isinstance(val, bool):
        return 'true' if val else 'false'
    if isinstance(val, int):
        return str(val)
    # Strings: delegate to _dump_scalar so reserved-word / int-shaped strings
    # ('true', '123', etc.) are preserved through the YAML round-trip.
    return _dump_scalar(str(val))


# ---------------------------------------------------------------------------
# Frontmatter helpers (.md task files)
# ---------------------------------------------------------------------------

TASK_META_KEY_ORDER = [
    'id', 'title', 'skills', 'priority', 'status',
    'blocked_by', 'timeout', 'target_dir', 'worker', 'started_at', 'completed_at',
    'handoff_path',
    'acceptance_criteria', 'verification', 'rework_count', 'max_rework',
]

TASK_META_DEFAULTS = {
    'acceptance_criteria': None,
    'verification': None,
    'rework_count': 0,
    'max_rework': 3,
}


def parse_frontmatter(text, source='<task>'):
    """Split a markdown file into (meta dict, body string)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise ValueError(
            f"missing frontmatter delimiter — file must begin with a line "
            f"containing only `---`"
        )
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end = i
            break
    if end is None:
        raise ValueError(
            f"unterminated frontmatter — frontmatter block must close with a "
            f"line containing only `---` before the body"
        )
    meta_text = '\n'.join(lines[1:end])
    body = '\n'.join(lines[end + 1:])
    if body.startswith('\n'):
        body = body[1:]
    return parse_yaml(meta_text, source=f"{source} (frontmatter)"), body


def serialize_frontmatter(meta, body):
    yaml_text = dump_yaml(meta, key_order=TASK_META_KEY_ORDER)
    if not body.endswith('\n'):
        body = body + '\n'
    return f"---\n{yaml_text}---\n\n{body}"


def parse_task_body(body):
    """Extract Description and Result sections from task body.

    Only the *first* `## Description` and `## Result` headers are treated as
    section boundaries. Later occurrences are kept verbatim inside the current
    section, so a worker-supplied result that contains the literal text
    `## Result` does not silently corrupt the file on the next round-trip.
    """
    sections = {}
    current = None
    buf = []
    for line in body.splitlines():
        m = re.match(r'^##\s+(Description|Result)\s*$', line, re.IGNORECASE)
        if m and m.group(1).lower() not in sections and current != m.group(1).lower():
            name = m.group(1).lower()
            if current is not None:
                sections[current] = '\n'.join(buf).rstrip()
            current = name
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = '\n'.join(buf).rstrip()
    return sections.get('description', '').strip(), sections.get('result', '').strip()


def build_task_body(description, result):
    desc = (description or '').rstrip()
    res = (result or '').rstrip()
    return f"## Description\n{desc}\n\n## Result\n{res}\n"


# ---------------------------------------------------------------------------
# State / mission / task I/O
# ---------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {'active_missions': [], 'default_mission': None}
    with open(STATE_FILE) as f:
        text = f.read()
    try:
        data = parse_yaml(text, source=STATE_FILE)
    except ValueError as e:
        die(f"failed to parse {STATE_FILE}: {e}")
    if 'active_missions' not in data or data['active_missions'] is None:
        data['active_missions'] = []
    if 'default_mission' not in data:
        data['default_mission'] = None
    return data


def _atomic_write(path, text):
    """Write text to path via tmp + os.replace, with fsync, so a crash mid-write
    cannot leave a half-written file behind. The caller is responsible for
    holding any necessary lock."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup; never mask the original exception.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state(state):
    out = {
        'active_missions': state.get('active_missions', []) or [],
        'default_mission': state.get('default_mission'),
    }
    # active_missions as block list for readability
    lines = []
    if out['active_missions']:
        lines.append('active_missions:')
        for slug in out['active_missions']:
            lines.append(f"  - {_dump_inline(slug)}")
    else:
        lines.append('active_missions: []')
    lines.append(_dump_kv('default_mission', out['default_mission']))
    _atomic_write(STATE_FILE, '\n'.join(lines) + '\n')


def mission_dir(slug):
    return os.path.join(MISSIONS_DIR, slug)


def mission_yaml_path(slug):
    return os.path.join(mission_dir(slug), 'mission.yaml')


def tasks_dir(slug):
    return os.path.join(mission_dir(slug), 'tasks')


def task_path(slug, task_id):
    return os.path.join(tasks_dir(slug), f"{task_id}.md")


MISSION_KEY_ORDER = ['title', 'slug', 'status', 'created_at', 'completed_at', 'next_task_id', 'max_review_cycles', 'review']


def load_mission(slug):
    path = mission_yaml_path(slug)
    if not os.path.exists(path):
        die(f"mission '{slug}' not found at {path}")
    with open(path) as f:
        text = f.read()
    try:
        return parse_yaml(text, source=path)
    except ValueError as e:
        die(f"failed to parse {path}: {e}")


def save_mission(slug, data):
    os.makedirs(mission_dir(slug), exist_ok=True)
    _atomic_write(mission_yaml_path(slug), dump_yaml(data, key_order=MISSION_KEY_ORDER))


def load_task(slug, task_id):
    path = task_path(slug, task_id)
    if not os.path.exists(path):
        die(f"task '{task_id}' not found in mission '{slug}'")
    with open(path) as f:
        text = f.read()
    try:
        return parse_frontmatter(text, source=path)
    except ValueError as e:
        die(f"failed to parse {path}: {e}")


def save_task(slug, task_id, meta, body):
    os.makedirs(tasks_dir(slug), exist_ok=True)
    _atomic_write(task_path(slug, task_id), serialize_frontmatter(meta, body))


def list_tasks(slug, base_dir=None):
    """Return list of (meta, body) sorted by tNNN."""
    tdir = base_dir if base_dir else tasks_dir(slug)
    if not os.path.exists(tdir):
        return []
    entries = []
    for fn in os.listdir(tdir):
        m = re.fullmatch(r't(\d+)\.md', fn)
        if not m:
            continue
        entries.append((int(m.group(1)), fn))
    entries.sort()
    out = []
    for _, fn in entries:
        path = os.path.join(tdir, fn)
        with open(path) as f:
            text = f.read()
        try:
            meta, body = parse_frontmatter(text, source=path)
        except ValueError as e:
            die(
                f"failed to parse {path}: {e}\n"
                f"  hint: task files start with `---` / frontmatter / `---` / "
                f"`## Description` / `## Result` (see existing tNNN.md for the template)."
            )
        # Normalize defaults
        meta.setdefault('skills', [])
        meta.setdefault('blocked_by', [])
        if meta.get('skills') is None:
            meta['skills'] = []
        if meta.get('blocked_by') is None:
            meta['blocked_by'] = []
        out.append((meta, body))
    return out


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def with_lock(callback):
    try:
        os.makedirs(QUEUE_DIR, exist_ok=True)
    except OSError as e:
        die(f"cannot create queue dir {QUEUE_DIR}: {e}")
    try:
        lf = open(LOCK_FILE, 'a+')
    except OSError as e:
        die(
            f"cannot open queue lock {LOCK_FILE}: {e}\n"
            f"  hint: check write permission on {QUEUE_DIR}, or remove a stale lock file."
        )
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return callback()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        lf.close()


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def generate_slug(title):
    date = datetime.now(timezone.utc).strftime('%Y%m%d')
    ascii_part = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:30]
    if ascii_part:
        base = f"{date}-{ascii_part}"
    else:
        h = hashlib.sha1(title.encode('utf-8')).hexdigest()[:8]
        base = f"{date}-{h}"
    # Avoid collisions
    slug = base
    n = 2
    while os.path.exists(mission_dir(slug)) or os.path.exists(os.path.join(ARCHIVE_DIR, slug)):
        slug = f"{base}-{n}"
        n += 1
    return slug


# ---------------------------------------------------------------------------
# Taskvia sync helpers (best-effort, standalone-compatible)
# ---------------------------------------------------------------------------

_TASKVIA_URL = os.environ.get('TASKVIA_URL', '').rstrip('/')
_TASKVIA_TOKEN = os.environ.get('TASKVIA_TOKEN', '')
_TASKVIA_TOKEN_WARNING_SHOWN = False


def _taskvia_request(method, path, payload=None):
    """HTTP call to Taskvia. Best-effort: never raises, logs warnings to stderr.
    Returns parsed response dict on success, None on failure.
    """
    global _TASKVIA_TOKEN_WARNING_SHOWN
    if _TASKVIA_URL and not _TASKVIA_TOKEN and not _TASKVIA_TOKEN_WARNING_SHOWN:
        print("[plan.sh] WARNING: TASKVIA_URL is set but TASKVIA_TOKEN is empty — Taskvia sync will be skipped.", file=sys.stderr)
        _TASKVIA_TOKEN_WARNING_SHOWN = True
    if not (_TASKVIA_URL and _TASKVIA_TOKEN):
        return None
    url = f"{_TASKVIA_URL}{path}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {_TASKVIA_TOKEN}',
    }
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode('utf-8', errors='replace')
        except Exception:
            body_text = '(unreadable)'
        print(f"[taskvia-sync] WARNING: {method} {path} HTTP {e.code}: {body_text}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[taskvia-sync] WARNING: {method} {path} failed: {e}", file=sys.stderr)
        return None


def _taskvia_enabled():
    """Return True if Taskvia credentials are configured."""
    return bool(_TASKVIA_URL and _TASKVIA_TOKEN)


def _taskvia_map_update(slug, task_id, status='pending'):
    """Update .taskvia-map.json after a successful inline sync.
    Keeps taskvia-sync.sh from re-registering tasks already pushed inline.
    """
    map_path = os.path.join(QUEUE_DIR, '.taskvia-map.json')
    try:
        with open(map_path) as f:
            task_map = json.load(f)
    except (OSError, json.JSONDecodeError):
        task_map = {}
    map_key = f"{slug}:{task_id}"
    task_map[map_key] = {'registered': True, 'status': status}
    try:
        with open(map_path, 'w') as f:
            json.dump(task_map, f, indent=2, ensure_ascii=False)
            f.write('\n')
    except OSError as e:
        print(f"[taskvia-sync] WARNING: .taskvia-map.json 更新失敗: {e}", file=sys.stderr)


def _taskvia_map_update_status(slug, task_id, status):
    """Update status of an existing .taskvia-map.json entry."""
    map_path = os.path.join(QUEUE_DIR, '.taskvia-map.json')
    try:
        with open(map_path) as f:
            task_map = json.load(f)
    except (OSError, json.JSONDecodeError):
        task_map = {}
    map_key = f"{slug}:{task_id}"
    if map_key in task_map:
        task_map[map_key]['status'] = status
    else:
        task_map[map_key] = {'registered': True, 'status': status}
    try:
        with open(map_path, 'w') as f:
            json.dump(task_map, f, indent=2, ensure_ascii=False)
            f.write('\n')
    except OSError as e:
        print(f"[taskvia-sync] WARNING: .taskvia-map.json 更新失敗: {e}", file=sys.stderr)


def _print_sync_summary(ok):
    """Print a one-line sync result to stdout (only when Taskvia is configured)."""
    if not _taskvia_enabled():
        return
    if ok:
        print("[taskvia-sync] ok")
    else:
        print("[taskvia-sync] failed — run scripts/taskvia-sync.sh to retry")


def taskvia_sync_init(slug, title):
    resp = _taskvia_request('POST', '/api/missions', {'slug': slug, 'title': title})
    return resp is not None


def taskvia_sync_add(slug, task_id, title, skills, priority, blocked_by):
    resp = _taskvia_request('POST', f'/api/missions/{slug}/tasks', {
        'id': task_id,
        'title': title,
        'skills': skills,
        'priority': priority,
        'blocked_by': blocked_by,
    })
    if resp is not None:
        _taskvia_map_update(slug, task_id, 'pending')
    return resp is not None


def taskvia_sync_pull(slug, task_id, assignee):
    resp = _taskvia_request('PATCH', f'/api/missions/{slug}/tasks/{task_id}', {
        'status': 'in_progress',
        'assignee': assignee,
    })
    if resp is not None:
        _taskvia_map_update_status(slug, task_id, 'in_progress')
    return resp is not None


def taskvia_sync_done(slug, task_id, result):
    resp = _taskvia_request('PATCH', f'/api/missions/{slug}/tasks/{task_id}', {
        'status': 'done',
        'result': result,
    })
    if resp is not None:
        _taskvia_map_update_status(slug, task_id, 'done')
    return resp is not None


def taskvia_sync_archive(slug):
    resp = _taskvia_request('DELETE', f'/api/missions/{slug}')
    return resp is not None


# ---------------------------------------------------------------------------
# Argument parsing helper
# ---------------------------------------------------------------------------

def parse_opts(args, spec):
    """spec: dict of {flag: 'value' or 'bool'}. Returns (opts dict, positional list)."""
    opts = {}
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in spec:
            kind = spec[a]
            if kind == 'value':
                if i + 1 >= len(args):
                    die(f"option {a} requires a value")
                opts[a] = args[i + 1]
                i += 2
            else:
                opts[a] = True
                i += 1
        else:
            positional.append(a)
            i += 1
    return opts, positional


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(args):
    opts, positional = parse_opts(args, {'--mission': 'value', '--force': 'bool'})
    if not positional:
        die("init requires a mission title")
    title = positional[0]
    force = opts.get('--force', False)
    sync_holder = [None]  # (slug, title)

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if slug:
            existing = os.path.exists(mission_dir(slug))
            if existing and not force:
                die(f"mission '{slug}' already exists. Use --force to overwrite.")
            if existing:
                # Non-destructive overwrite: move the previous mission to
                # archive/<slug>.overwritten-<timestamp>/ instead of rm -rf,
                # so worker output is never silently lost.
                ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                backup_name = f"{slug}.overwritten-{ts}"
                os.makedirs(ARCHIVE_DIR, exist_ok=True)
                shutil.move(mission_dir(slug), os.path.join(ARCHIVE_DIR, backup_name))
                print(
                    f"[plan.sh init] previous '{slug}' moved to archive/{backup_name}",
                    file=sys.stderr,
                )
                # Drop the slug from active_missions so save_state doesn't
                # leave a dangling reference if the rebuild below fails.
                active = state.get('active_missions') or []
                if slug in active:
                    active.remove(slug)
                state['active_missions'] = active
                if state.get('default_mission') == slug:
                    state['default_mission'] = active[0] if active else None
        else:
            slug = generate_slug(title)

        os.makedirs(tasks_dir(slug), exist_ok=True)
        mission = {
            'title': title,
            'slug': slug,
            'status': 'drafting',
            'created_at': now_iso(),
            'completed_at': None,
            'next_task_id': 1,
            'max_review_cycles': 3,
            'review': {
                'last_verdict': None,
                'cycle_count': 0,
                'reviewed_at': None,
                'reviewer': None,
            },
        }
        save_mission(slug, mission)

        active = state.get('active_missions') or []
        if slug not in active:
            active.append(slug)
        state['active_missions'] = active
        state['default_mission'] = slug
        save_state(state)
        sync_holder[0] = (slug, title)
        print(f"Initialized mission: {slug}")
        print(f"  title: {title}")
        print(f"  path:  {mission_dir(slug)}")

    with_lock(_do)
    if sync_holder[0]:
        ok = taskvia_sync_init(*sync_holder[0])
        _print_sync_summary(ok)


def cmd_add(args):
    opts, positional = parse_opts(args, {
        '--mission': 'value',
        '--skills': 'value',
        '--blocked-by': 'value',
        '--priority': 'value',
        '--description': 'value',
        '--target-dir': 'value',
        '--idle-timeout': 'value',
        '--max-timeout': 'value',
    })
    if not positional:
        die("add requires a task title")
    title = positional[0]
    sync_holder = [None]  # (slug, task_id, title, skills, priority, blocked_by)

    skills = [s.strip() for s in opts.get('--skills', '').split(',') if s.strip()]
    if not skills:
        print("WARNING: task added with no skills — dispatcher will not be able to assign it automatically", file=sys.stderr)
    blocked_by = [s.strip() for s in opts.get('--blocked-by', '').split(',') if s.strip()]
    priority = opts.get('--priority', 'medium')
    if priority not in PRIORITY_ORDER:
        die(f"invalid priority '{priority}'. Use high|medium|low.")
    description = opts.get('--description', '')

    # --target-dir: Worker が起動時に cd する target project のパス。
    # 未指定なら None (crewvia 本体を触るタスク扱い)。
    # ~ 展開と絶対パス化をかけ、ディレクトリ存在確認を入れる。
    target_dir = opts.get('--target-dir')
    if target_dir:
        target_dir = os.path.abspath(os.path.expanduser(target_dir))
        if not os.path.isdir(target_dir):
            die(f"--target-dir does not exist or is not a directory: {target_dir}")
    else:
        target_dir = None

    # --idle-timeout / --max-timeout: タスクごとの timeout 秒数（省略可）
    timeout = {}
    if opts.get('--idle-timeout'):
        try:
            timeout['idle'] = int(opts['--idle-timeout'])
        except ValueError:
            die("--idle-timeout must be an integer number of seconds")
    if opts.get('--max-timeout'):
        try:
            timeout['max'] = int(opts['--max-timeout'])
        except ValueError:
            die("--max-timeout must be an integer number of seconds")
    timeout = timeout if timeout else None

    def _do():
        state = load_state()
        slug = opts.get('--mission') or state.get('default_mission')
        if not slug:
            die("no active mission. Run 'plan.sh init' first or pass --mission.")
        if not os.path.exists(mission_dir(slug)):
            die(f"mission '{slug}' not found.")

        mission = load_mission(slug)
        task_num = int(mission.get('next_task_id') or 1)
        task_id = f"t{task_num:03d}"

        meta = {
            'id': task_id,
            'title': title,
            'skills': skills,
            'priority': priority,
            'status': 'pending',
            'blocked_by': blocked_by,
            'target_dir': target_dir,
            'worker': None,
            'started_at': None,
            'completed_at': None,
        }
        if timeout:
            meta['timeout'] = timeout
        body = build_task_body(description, '')
        save_task(slug, task_id, meta, body)
        sync_holder[0] = (slug, task_id, title, skills, priority, blocked_by)

        mission['next_task_id'] = task_num + 1
        save_mission(slug, mission)
        suffix = f" [target: {target_dir}]" if target_dir else ""
        print(f"Added: {slug}/{task_id} — {title}{suffix}")

    with_lock(_do)
    if sync_holder[0]:
        ok = taskvia_sync_add(*sync_holder[0])
        _print_sync_summary(ok)


def cmd_pull(args):
    opts, _ = parse_opts(args, {
        '--mission': 'value',
        '--skills': 'value',
        '--agent': 'value',
        '--target-dir': 'value',
        '--task': 'value',   # specific task ID (dispatcher-assigned; bypasses skill/target/blocked filters)
    })
    requested_skills = {s.strip() for s in opts.get('--skills', '').split(',') if s.strip()}
    agent = opts.get('--agent') or os.environ.get('AGENT_NAME', '')
    specific_task = opts.get('--task')

    # Determine effective target_dir for filtering:
    # --target-dir flag > TARGET_DIR env var > None (crewvia-local)
    explicit_td = opts.get('--target-dir')
    if explicit_td:
        effective_target = os.path.abspath(os.path.expanduser(explicit_td))
    else:
        env_td = os.environ.get('TARGET_DIR', '').strip()
        effective_target = os.path.abspath(env_td) if env_td else None

    chosen_holder = [None]
    diag = {'reason': None, 'detail': ''}

    def _do():
        state = load_state()
        if opts.get('--mission'):
            slugs = [opts['--mission']]
        else:
            slugs = list(state.get('active_missions') or [])
            default = state.get('default_mission')
            if default and default in slugs:
                slugs.remove(default)
                slugs.insert(0, default)

        if not slugs:
            diag['reason'] = 'no_active_missions'
            diag['detail'] = 'state.yaml lists no active missions'
            return

        # Diagnostic counters per slug
        scanned = 0
        pending_count = 0
        skill_mismatch = 0
        blocked_count = 0
        target_mismatch = 0
        missing_dirs = []

        candidates = []
        for slug in slugs:
            if not os.path.exists(mission_dir(slug)):
                missing_dirs.append(slug)
                continue
            tasks = list_tasks(slug)
            scanned += len(tasks)
            done_ids = {m['id'] for (m, _) in tasks if m.get('status') in TERMINAL_STATUSES}
            for (meta, body) in tasks:
                # --task: match by ID only, bypass skill/target/blocked filters
                if specific_task:
                    if meta.get('id') != specific_task:
                        continue
                    st = meta.get('status')
                    if st in TERMINAL_STATUSES:
                        die(f"task '{specific_task}' is already {st} (use plan.sh status to review)")
                    if st == 'in_progress':
                        die(
                            f"task '{specific_task}' is already in_progress "
                            f"(assigned to {meta.get('worker', '?')}). "
                            f"If the worker crashed, reset the task status manually."
                        )
                    if st != 'pending':
                        die(f"task '{specific_task}' has unexpected status: {st}")
                    # Warn if worker skills don't fully cover the task's required skills
                    task_req = set(meta.get('skills') or [])
                    worker_skills = {s.strip() for s in requested_skills}
                    missing = task_req - worker_skills
                    if task_req and missing:
                        print(
                            f"WARNING: task '{specific_task}' requires skills {sorted(task_req)} "
                            f"but worker has {sorted(worker_skills) or '<none>'}; "
                            f"missing: {sorted(missing)}",
                            file=sys.stderr,
                        )
                    candidates.append((slug, meta, body))
                    break  # task IDs are unique within a mission
                # Regular auto-selection flow
                if meta.get('status') != 'pending':
                    continue
                pending_count += 1
                if requested_skills and not set(meta.get('skills', [])).issubset(requested_skills):
                    skill_mismatch += 1
                    continue
                # Target-dir filtering:
                # (1) effective_target is None AND task target_dir is None → match (crewvia-local)
                # (2) effective_target is set AND task target_dir matches   → match
                # (3) otherwise → skip
                task_td = meta.get('target_dir')
                if effective_target is None:
                    if task_td is not None:
                        target_mismatch += 1
                        continue
                else:
                    if task_td != effective_target:
                        target_mismatch += 1
                        continue
                bb = meta.get('blocked_by') or []
                if any(dep not in done_ids for dep in bb):
                    blocked_count += 1
                    continue
                candidates.append((slug, meta, body))

        if missing_dirs:
            print(
                f"[plan.sh pull] WARNING: state.yaml references missing mission "
                f"directories: {missing_dirs}",
                file=sys.stderr,
            )

        if not candidates:
            if specific_task:
                die(f"task '{specific_task}' not found in mission(s): {slugs}")
            if pending_count == 0:
                diag['reason'] = 'no_pending_tasks'
                diag['detail'] = f'{scanned} task(s) scanned across {len(slugs)} mission(s); none pending'
            elif skill_mismatch and not blocked_count and not target_mismatch:
                diag['reason'] = 'no_skill_match'
                diag['detail'] = (
                    f'{pending_count} pending task(s) found but none match skills '
                    f'{sorted(requested_skills) or "<any>"}'
                )
            elif target_mismatch and not skill_mismatch and not blocked_count:
                diag['reason'] = 'no_target_match'
                target_label = effective_target or '(crewvia-local)'
                diag['detail'] = (
                    f'{pending_count} pending task(s) found but none match target_dir '
                    f'{target_label!r}'
                )
            elif blocked_count and not skill_mismatch and not target_mismatch:
                diag['reason'] = 'all_blocked'
                diag['detail'] = f'{blocked_count} pending task(s) blocked by unmet dependencies'
            else:
                diag['reason'] = 'no_eligible_task'
                diag['detail'] = (
                    f'{pending_count} pending; {skill_mismatch} skill-mismatch; '
                    f'{target_mismatch} target-mismatch; {blocked_count} blocked'
                )
            return

        # Priority-first sort: high-priority tasks across all active missions
        # win before any lower-priority work, regardless of which mission they
        # live in. Default-mission ordering is only a tiebreaker.
        slug_index = {s: i for i, s in enumerate(slugs)}
        candidates.sort(key=lambda c: (
            PRIORITY_ORDER.get(c[1].get('priority', 'medium'), 1),
            slug_index.get(c[0], 999),
            c[1].get('id', ''),
        ))

        slug, meta, body = candidates[0]
        meta['status'] = 'in_progress'
        meta['worker'] = agent or None
        meta['started_at'] = now_iso()
        save_task(slug, meta['id'], meta, body)

        desc, _result = parse_task_body(body)
        chosen_holder[0] = {
            'mission': slug,
            'id': meta['id'],
            'title': meta['title'],
            'description': desc,
            'skills': meta.get('skills') or [],
            'priority': meta.get('priority', 'medium'),
            'blocked_by': meta.get('blocked_by') or [],
            'target_dir': meta.get('target_dir'),  # None for crewvia-local tasks
        }

    with_lock(_do)

    if chosen_holder[0] is None:
        # exit 2 = "no task available" (idle / sleep & retry)
        # exit 1 is reserved for real errors raised via die()
        print(
            f"[plan.sh pull] no task available: {diag['reason']} — {diag['detail']}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Write assignment file BEFORE Taskvia sync to prevent a dispatcher race:
    # the dispatcher checks assignment_file existence to decide "is worker idle?"
    # If we write it after a slow Taskvia sync, the dispatcher may see
    # task.status=in_progress (no longer pending) + no assignment file → shutdown.
    if agent:
        assignments_dir = os.path.join(QUEUE_DIR, 'assignments')
        os.makedirs(assignments_dir, exist_ok=True)
        assignment_file = os.path.join(assignments_dir, agent)
        with open(assignment_file, 'w') as _f:
            _f.write(f"{chosen_holder[0]['mission']}:{chosen_holder[0]['id']}\n")

    ok = taskvia_sync_pull(chosen_holder[0]['mission'], chosen_holder[0]['id'], agent)
    _print_sync_summary(ok)

    print(json.dumps(chosen_holder[0], ensure_ascii=False))


def cmd_done(args):
    opts, positional = parse_opts(args, {'--mission': 'value'})
    if len(positional) < 2:
        die("done requires <task_id> and <result>")
    task_id = positional[0]
    result = positional[1]
    sync_holder = [None]  # (slug, task_id, result)

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = []
            for s in state.get('active_missions') or []:
                if os.path.exists(task_path(s, task_id)):
                    matches.append(s)
            if not matches:
                # Check archived missions to surface a more useful error if the
                # mission was archived between pull and done (race window).
                archived_matches = []
                if os.path.isdir(ARCHIVE_DIR):
                    for entry in os.listdir(ARCHIVE_DIR):
                        archived_task = os.path.join(ARCHIVE_DIR, entry, 'tasks', f"{task_id}.md")
                        if os.path.exists(archived_task):
                            archived_matches.append(entry)
                if archived_matches:
                    die(
                        f"task '{task_id}' exists only in archived mission(s) "
                        f"{archived_matches}. The mission was archived before this "
                        f"done report — re-activate the mission to record the result, "
                        f"or merge the result manually into the archived task file."
                    )
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                die(f"task '{task_id}' exists in multiple missions: {matches}. Use --mission.")
            slug = matches[0]

        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status in ('done', 'verified', 'failed', 'skipped'):
            die(f"task '{task_id}' is already {cur_status}.")

        meta['status'] = 'done'
        meta['completed_at'] = now_iso()
        desc, _ = parse_task_body(body)
        new_body = build_task_body(desc, result)
        save_task(slug, task_id, meta, new_body)
        sync_holder[0] = (slug, task_id, result)

        # Mission complete?
        tasks = list_tasks(slug)
        if all(m.get('status') in TERMINAL_STATUSES for (m, _) in tasks):
            mission = load_mission(slug)
            mission['status'] = 'done'
            mission['completed_at'] = now_iso()
            save_mission(slug, mission)

        print(f"Done: {slug}/{task_id}")

    with_lock(_do)

    # Remove assignment file on done
    agent_name = os.environ.get('AGENT_NAME', '')
    if agent_name:
        assignment_file = os.path.join(QUEUE_DIR, 'assignments', agent_name)
        if os.path.exists(assignment_file):
            os.remove(assignment_file)

    if sync_holder[0]:
        ok = taskvia_sync_done(*sync_holder[0])
        _print_sync_summary(ok)


def cmd_fail(args):
    opts, positional = parse_opts(args, {'--mission': 'value'})
    if not positional:
        die("fail requires <task_id>")
    task_id = positional[0]
    handoff_path = positional[1] if len(positional) > 1 else None
    knowledge_info = [None]  # populated inside _do if rework limit reached

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = []
            for s in state.get('active_missions') or []:
                if os.path.exists(task_path(s, task_id)):
                    matches.append(s)
            if not matches:
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                die(f"task '{task_id}' exists in multiple missions: {matches}. Use --mission.")
            slug = matches[0]

        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status in ('done', 'verified', 'failed', 'skipped'):
            die(f"task '{task_id}' is already {cur_status}.")

        meta['status'] = 'failed'
        meta['completed_at'] = now_iso()
        if handoff_path:
            meta['handoff_path'] = handoff_path
        desc, _ = parse_task_body(body)
        new_body = build_task_body(desc, f"FAILED — handoff: {handoff_path or 'none'}")
        save_task(slug, task_id, meta, new_body)
        print(f"Failed: {slug}/{task_id}")

        # Rework learning loop: record in knowledge/director.md if rework limit was reached
        rework = meta.get('rework_count') or 0
        max_rework = meta.get('max_rework') or 3
        if rework >= max_rework:
            knowledge_info[0] = (task_id, slug, rework, max_rework, handoff_path)

    with_lock(_do)

    # Post-lock: write rework pattern to knowledge/director.md
    if knowledge_info[0]:
        _append_knowledge_director(*knowledge_info[0])

    # Remove assignment file (same as done)
    agent_name = os.environ.get('AGENT_NAME', '')
    if agent_name:
        assignment_file = os.path.join(QUEUE_DIR, 'assignments', agent_name)
        if os.path.exists(assignment_file):
            os.remove(assignment_file)


def cmd_status(args):
    opts, _ = parse_opts(args, {'--mission': 'value', '--all': 'bool'})
    state = load_state()

    if opts.get('--mission'):
        _print_mission_detail(opts['--mission'])
        return

    slugs = list(state.get('active_missions') or [])
    archived_slugs = []
    if opts.get('--all') and os.path.exists(ARCHIVE_DIR):
        for entry in sorted(os.listdir(ARCHIVE_DIR)):
            full = os.path.join(ARCHIVE_DIR, entry)
            if os.path.isdir(full) and os.path.exists(os.path.join(full, 'mission.yaml')):
                archived_slugs.append(entry)

    if not slugs and not archived_slugs:
        print("No active missions.")
        return

    if slugs:
        print(f"Active missions ({len(slugs)}):")
        print()
        for slug in slugs:
            _print_mission_summary(slug, archived=False)
            print()

    if archived_slugs:
        print(f"Archived missions ({len(archived_slugs)}):")
        print()
        for slug in archived_slugs:
            _print_mission_summary(slug, archived=True)
            print()


def _resolve_mission_base(slug):
    if os.path.exists(mission_dir(slug)):
        return mission_dir(slug), False
    archived = os.path.join(ARCHIVE_DIR, slug)
    if os.path.exists(archived):
        return archived, True
    return None, False


def _print_mission_summary(slug, archived=False):
    base = os.path.join(ARCHIVE_DIR, slug) if archived else mission_dir(slug)
    mission_path = os.path.join(base, 'mission.yaml')
    if not os.path.exists(mission_path):
        print(f"  {slug} — (mission.yaml missing)")
        return
    with open(mission_path) as f:
        text = f.read()
    try:
        mission = parse_yaml(text, source=mission_path)
    except ValueError as e:
        print(f"  {slug} — (mission.yaml unparseable: {e})")
        return
    tasks = list_tasks(slug, base_dir=os.path.join(base, 'tasks'))
    total = len(tasks)
    done = sum(1 for (m, _) in tasks if m.get('status') == 'done')
    in_prog = [(m, b) for (m, b) in tasks if m.get('status') == 'in_progress']

    title = mission.get('title', '(unnamed)')
    status = mission.get('status', 'in_progress')
    marker = '[archived] ' if archived else ''
    print(f"  {marker}{slug} — {title}")
    print(f"    Status: {status}  Progress: {done}/{total}")
    for (m, _) in in_prog:
        worker = m.get('worker') or '?'
        print(f"    🔄 {m['id']} {m['title']} ({worker})")


def _print_mission_detail(slug):
    base, archived = _resolve_mission_base(slug)
    if not base:
        die(f"mission '{slug}' not found.")
    mission_path = os.path.join(base, 'mission.yaml')
    with open(mission_path) as f:
        text = f.read()
    try:
        mission = parse_yaml(text, source=mission_path)
    except ValueError as e:
        die(f"failed to parse {mission_path}: {e}")
    tasks = list_tasks(slug, base_dir=os.path.join(base, 'tasks'))

    print(f"Mission: {mission.get('title', '(unnamed)')}")
    print(f"Slug:    {slug}{' [archived]' if archived else ''}")
    print(f"Status:  {mission.get('status', 'in_progress')}")
    print()

    done_ids = {m['id'] for (m, _) in tasks if m.get('status') in TERMINAL_STATUSES}
    for (m, _) in tasks:
        st = m.get('status', 'pending')
        icon = STATUS_ICON.get(st, '❓')
        tid = m['id']
        title = m['title']
        bb = m.get('blocked_by') or []
        if st in ('done', 'verified'):
            worker = m.get('worker') or ''
            suffix = f"({worker}, 完了)" if worker else "(完了)"
        elif st == 'in_progress':
            worker = m.get('worker') or ''
            suffix = f"({worker}, 進行中)" if worker else "(進行中)"
        elif st == 'ready_for_verification':
            worker = m.get('worker') or ''
            suffix = f"({worker}, 検証待ち)" if worker else "(検証待ち)"
        elif st == 'verifying':
            suffix = "(検証中)"
        elif st == 'verification_failed':
            suffix = "(検証失敗)"
        elif st == 'needs_human_review':
            suffix = "(要人間レビュー)"
        elif bb:
            unmet = [d for d in bb if d not in done_ids]
            suffix = f"(blocked: {', '.join(unmet)})" if unmet else "(pending)"
        else:
            suffix = "(pending)"
        timeout_suffix = ''
        to = m.get('timeout')
        if isinstance(to, dict):
            parts = []
            if 'idle' in to:
                parts.append(f"idle={to['idle']}s")
            if 'max' in to:
                parts.append(f"max={to['max']}s")
            if parts:
                timeout_suffix = f" [{' '.join(parts)}]"
        print(f"[{icon}] {tid} {title} {suffix}{timeout_suffix}")

    total = len(tasks)
    done_count = sum(1 for (m, _) in tasks if m.get('status') == 'done')
    print()
    print(f"Progress: {done_count}/{total} done")


def cmd_ready_for_verification(args):
    opts, positional = parse_opts(args, {'--mission': 'value'})
    if not positional:
        die("ready-for-verification requires <task_id>")
    task_id = positional[0]

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = []
            for s in state.get('active_missions') or []:
                if os.path.exists(task_path(s, task_id)):
                    matches.append(s)
            if not matches:
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                die(f"task '{task_id}' exists in multiple missions: {matches}. Use --mission.")
            slug = matches[0]

        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status != 'in_progress':
            die(
                f"ready-for-verification requires task to be in_progress, "
                f"but '{task_id}' is currently '{cur_status}'."
            )

        meta['status'] = 'ready_for_verification'
        save_task(slug, task_id, meta, body)
        print(f"Ready for verification: {slug}/{task_id}")

    with_lock(_do)


def cmd_verify_result(args):
    """
    Usage: plan.sh verify-result <task_id> <verdict> [--notes "..."] [--mission <slug>]
    verdict: pass | fail | needs_human_review

    Appends a verification entry to the ## Verification section of the task file.
    pass          → status: verified (terminal)
    fail          → rework_count += 1, status: in_progress (or needs_human_review if max_rework exceeded)
    needs_human_review → status: needs_human_review
    """
    opts, positional = parse_opts(args, {'--mission': 'value', '--notes': 'value'})
    if len(positional) < 2:
        die("verify-result requires <task_id> <verdict>")
    task_id = positional[0]
    verdict = positional[1]
    VALID_VERDICTS = {'pass', 'fail', 'needs_human_review'}
    if verdict not in VALID_VERDICTS:
        die(f"verdict must be one of: {', '.join(sorted(VALID_VERDICTS))}")
    notes = opts.get('--notes', '')

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = []
            for s in state.get('active_missions') or []:
                if os.path.exists(task_path(s, task_id)):
                    matches.append(s)
            if not matches:
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                die(f"task '{task_id}' exists in multiple missions: {matches}. Use --mission.")
            slug = matches[0]

        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status in ('done', 'verified', 'skipped', 'failed'):
            die(f"task '{task_id}' is already {cur_status}.")

        # Build and append verification entry
        timestamp = now_iso()
        entry_lines = [f"\n### {timestamp}", f"**Verdict:** {verdict}"]
        if notes:
            entry_lines.append(f"**Notes:** {notes}")
        verification_entry = '\n'.join(entry_lines) + '\n'

        if '## Verification' in body:
            body_new = body.rstrip() + '\n' + verification_entry
        else:
            body_new = body.rstrip() + '\n\n## Verification\n' + verification_entry

        # Status transition
        if verdict == 'pass':
            meta['status'] = 'verified'
            meta['completed_at'] = now_iso()
        elif verdict == 'fail':
            rework = (meta.get('rework_count') or 0) + 1
            max_rework = meta.get('max_rework') or 3
            meta['rework_count'] = rework
            if rework >= max_rework:
                meta['status'] = 'needs_human_review'
                print(
                    f"rework_count ({rework}) >= max_rework ({max_rework}): "
                    f"escalating to needs_human_review"
                )
            else:
                meta['status'] = 'in_progress'
        else:  # needs_human_review
            meta['status'] = 'needs_human_review'

        save_task(slug, task_id, meta, body_new)

        # Check mission completion (only on pass→verified)
        if verdict == 'pass':
            tasks = list_tasks(slug)
            if all(m.get('status') in TERMINAL_STATUSES for (m, _) in tasks):
                mission = load_mission(slug)
                mission['status'] = 'done'
                mission['completed_at'] = now_iso()
                save_mission(slug, mission)

        print(f"verify-result: {slug}/{task_id} → {meta['status']} (verdict={verdict})")

    with_lock(_do)


_MODE_ORDER = ['light', 'standard', 'strict']


def upgrade_mode(current, proposed):
    """Return the stricter of two verification modes (light < standard < strict)."""
    ci = _MODE_ORDER.index(current) if current in _MODE_ORDER else 0
    pi = _MODE_ORDER.index(proposed) if proposed in _MODE_ORDER else 0
    return _MODE_ORDER[max(ci, pi)]


def _apply_risk_flags(slug, plan_review_path):
    """Parse ## Risk Flags from plan_review.md and upgrade task verification.mode."""
    try:
        with open(plan_review_path) as f:
            content = f.read()
    except OSError:
        return

    # Find ## Risk Flags section
    in_risk_flags = False
    current_task_id = None
    recommended_mode = None
    upgrades: list = []  # list of (task_id, mode)

    for line in content.splitlines():
        if re.match(r'^##\s+Risk\s+Flags', line, re.IGNORECASE):
            in_risk_flags = True
            continue
        if in_risk_flags:
            if re.match(r'^##', line):
                break  # Next section — stop
            m_task = re.match(r'^-\s+task:\s*(\S+)', line)
            if m_task:
                if current_task_id and recommended_mode:
                    upgrades.append((current_task_id, recommended_mode))
                current_task_id = m_task.group(1).strip()
                recommended_mode = None
                continue
            m_mode = re.match(r'\s+recommended_mode:\s*(\S+)', line)
            if m_mode and current_task_id:
                recommended_mode = m_mode.group(1).strip()

    if current_task_id and recommended_mode:
        upgrades.append((current_task_id, recommended_mode))

    if not upgrades:
        return

    # Apply upgrades to task files
    for task_id, proposed_mode in upgrades:
        task_file = None
        for fn in os.listdir(os.path.join(MISSIONS_DIR, slug, 'tasks')):
            if fn == f"{task_id}.md":
                task_file = os.path.join(MISSIONS_DIR, slug, 'tasks', fn)
                break
        if not task_file or not os.path.exists(task_file):
            print(f"[risk-flags] task '{task_id}' not found in mission '{slug}' — skipping", file=sys.stderr)
            continue

        meta, body = load_task(slug, task_id)
        verification = meta.get('verification') or {}
        if not isinstance(verification, dict):
            verification = {}
        current_mode = verification.get('mode') or 'standard'
        new_mode = upgrade_mode(current_mode, proposed_mode)
        if new_mode != current_mode:
            verification['mode'] = new_mode
            meta['verification'] = verification
            save_task(slug, task_id, meta, body)
            print(
                f"[risk-flags] task '{task_id}': verification.mode {current_mode} → {new_mode} "
                f"(recommended_mode={proposed_mode})"
            )
        else:
            print(
                f"[risk-flags] task '{task_id}': verification.mode stays {current_mode} "
                f"(already >= recommended {proposed_mode})"
            )


def _append_knowledge_director(task_id, slug, rework_count, max_rework, handoff_path=None):
    """Append a rework-limit record to knowledge/director.md (create with header if absent)."""
    repo_root = os.path.dirname(QUEUE_DIR)
    knowledge_dir = os.path.join(repo_root, 'knowledge')
    os.makedirs(knowledge_dir, exist_ok=True)
    knowledge_path = os.path.join(knowledge_dir, 'director.md')
    header = (
        "# Director Knowledge Base\n\n"
        "Director が過去のミッション実績から学んだパターンを自動追記するファイル。\n"
        "計画精度改善のために参照すること。\n"
    )
    timestamp = now_iso()
    entry = (
        f"\n## {timestamp} — rework 上限到達: {task_id}\n"
        f"- mission: {slug}\n"
        f"- rework_count: {rework_count} / max_rework: {max_rework}\n"
        f"- handoff_path: {handoff_path or 'none'}\n"
    )
    try:
        if not os.path.exists(knowledge_path):
            with open(knowledge_path, 'w') as f:
                f.write(header)
        with open(knowledge_path, 'a') as f:
            f.write(entry)
        print(
            f"[knowledge] appended rework pattern to knowledge/director.md "
            f"(task={task_id}, rework={rework_count}/{max_rework})"
        )
    except OSError as e:
        print(f"WARNING: failed to write knowledge/director.md: {e}", file=sys.stderr)


def _load_lint_module():
    """Load lint_plan.py dynamically (same pattern as cmd_lint)."""
    import importlib.util, pathlib
    repo_root = os.path.dirname(QUEUE_DIR)
    lint_path = pathlib.Path(repo_root) / 'scripts' / 'lint_plan.py'
    spec = importlib.util.spec_from_file_location('lint_plan', lint_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, os.path.join(repo_root, 'config')


def cmd_review(args):
    """
    Usage: plan.sh review <slug>

    1. Runs lint as a pre-step (FAIL → treated as revise, exit 1)
    2. Checks max_review_cycles
    3. Sets mission status to reviewing, increments cycle_count
    4. Invokes scripts/review-plan.sh <slug>
    5. Reads plan_review.md verdict and updates mission:
       - approve → status: ready
       - revise  → status: drafting, cycle_count++
       - reject  → status: drafting, cycle_count++
    """
    opts, positional = parse_opts(args, {})
    if not positional:
        die("review requires <mission_slug>")
    slug = positional[0]

    # --- Step 1: lint pre-check ---
    print(f"[review] running lint on '{slug}'...", file=sys.stderr)
    try:
        lint_mod, config_dir = _load_lint_module()
        lint_rc = lint_mod.lint_mission(slug, QUEUE_DIR, config_dir, strict=False)
    except Exception as e:
        die(f"lint failed to load: {e}")
    if lint_rc != 0:
        print(
            f"[review] lint FAIL — treating as revise. Fix lint errors before review.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"[review] lint OK", file=sys.stderr)

    # --- Step 2: check mission + cycle limit ---
    def _do_start():
        mission = load_mission(slug)
        review = mission.get('review') or {}
        if not isinstance(review, dict):
            review = {}
        cycle_count = review.get('cycle_count') or 0
        max_cycles = mission.get('max_review_cycles') or 3
        if cycle_count >= max_cycles:
            print(
                f"[review] ESCALATION: '{slug}' has reached max_review_cycles ({max_cycles}). "
                f"Director intervention required.",
                file=sys.stderr,
            )
            sys.exit(1)

        review['cycle_count'] = cycle_count + 1
        review['reviewed_at'] = now_iso()
        mission['review'] = review
        mission['status'] = 'reviewing'
        save_mission(slug, mission)
        print(f"[review] mission '{slug}' → reviewing (cycle {cycle_count + 1}/{max_cycles})")

    with_lock(_do_start)

    # --- Step 3: invoke review-plan.sh ---
    import subprocess
    repo_root = os.path.dirname(QUEUE_DIR)
    review_script = os.path.join(repo_root, 'scripts', 'review-plan.sh')
    print(f"[review] invoking review-plan.sh...", file=sys.stderr)
    proc = subprocess.run(['bash', review_script, slug])
    if proc.returncode != 0:
        die(f"review-plan.sh failed or timed out for mission '{slug}'")

    # --- Step 4: read verdict from plan_review.md ---
    review_output = os.path.join(MISSIONS_DIR, slug, 'plan_review.md')
    if not os.path.exists(review_output):
        die(f"plan_review.md not found for mission '{slug}' after review-plan.sh completed")

    verdict = None
    with open(review_output) as f:
        for line in f:
            vm = re.search(r'\*\*Verdict:\*\*\s*(approve|revise|reject)', line)
            if vm:
                verdict = vm.group(1)
                break
    if not verdict:
        die(f"No valid verdict found in plan_review.md for mission '{slug}'")

    # --- Step 5: update mission based on verdict ---
    def _do_verdict():
        mission = load_mission(slug)
        review = mission.get('review') or {}
        if not isinstance(review, dict):
            review = {}
        review['last_verdict'] = verdict
        review['reviewed_at'] = now_iso()
        mission['review'] = review
        if verdict == 'approve':
            mission['status'] = 'ready'
            print(f"[review] verdict: approve → mission '{slug}' is now ready for launch")
        else:
            mission['status'] = 'drafting'
            print(
                f"[review] verdict: {verdict} → mission '{slug}' returned to drafting "
                f"(cycle {review.get('cycle_count', '?')} of {mission.get('max_review_cycles', 3)})"
            )
        save_mission(slug, mission)

    with_lock(_do_verdict)

    # --- Step 6: apply risk_flags → verification.mode upgrade ---
    _apply_risk_flags(slug, review_output)


def cmd_launch(args):
    """
    Usage: plan.sh launch <slug>

    Transitions mission from status: ready → in_progress, enabling workers to pull tasks.
    Rejects with an error if status is not ready (drafting/reviewing require plan.sh review first).
    """
    opts, positional = parse_opts(args, {})
    if not positional:
        die("launch requires <mission_slug>")
    slug = positional[0]

    def _do():
        mission = load_mission(slug)
        cur_status = mission.get('status')
        if cur_status != 'ready':
            die(
                f"mission '{slug}' status is '{cur_status}' — only 'ready' missions can be launched. "
                f"Run 'plan.sh review {slug}' first to get reviewer approval."
            )
        mission['status'] = 'in_progress'
        save_mission(slug, mission)
        print(f"Launched: '{slug}' is now in_progress — workers can pull tasks")

    with_lock(_do)


def cmd_lint(args):
    opts, positional = parse_opts(args, {'--strict': 'bool', '--mission': 'value'})
    slug = opts.get('--mission') or (positional[0] if positional else None)
    if not slug:
        state = load_state()
        active = state.get('active_missions') or []
        if not active:
            die("lint: no active missions and no --mission specified")
        slug = active[0]
    strict = bool(opts.get('--strict'))
    import importlib.util, pathlib
    repo_root = os.path.dirname(QUEUE_DIR)
    lint_path = pathlib.Path(repo_root) / 'scripts' / 'lint_plan.py'
    spec = importlib.util.spec_from_file_location('lint_plan', lint_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    config_dir = os.path.join(repo_root, 'config')
    rc = mod.lint_mission(slug, QUEUE_DIR, config_dir, strict=strict)
    sys.exit(rc)


def cmd_archive(args):
    opts, positional = parse_opts(args, {})
    if not positional:
        die("archive requires <slug>")
    slug = positional[0]

    def _do():
        state = load_state()
        src = mission_dir(slug)
        if not os.path.exists(src):
            die(f"mission '{slug}' not found.")
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        dst = os.path.join(ARCHIVE_DIR, slug)
        if os.path.exists(dst):
            die(f"archive target already exists: {dst}")
        shutil.move(src, dst)

        active = state.get('active_missions') or []
        if slug in active:
            active.remove(slug)
        state['active_missions'] = active
        if state.get('default_mission') == slug:
            state['default_mission'] = active[0] if active else None
        save_state(state)
        print(f"Archived: {slug} → archive/{slug}")

    with_lock(_do)
    ok = taskvia_sync_archive(slug)
    _print_sync_summary(ok)


def _resync_one(slug):
    """Push one local mission + all its tasks to Taskvia (upsert, idempotent)."""
    if not os.path.exists(mission_dir(slug)):
        print(f"[resync] WARNING: mission '{slug}' not found locally, skipping.", file=sys.stderr)
        return

    mission = load_mission(slug)
    title = mission.get('title', slug)
    print(f"[resync] syncing mission: {slug}")

    # Upsert mission (POST; Taskvia returns existing record if slug already registered)
    _taskvia_request('POST', '/api/missions', {'slug': slug, 'title': title})

    tasks = list_tasks(slug)
    for meta, body in tasks:
        task_id = meta.get('id')
        if not task_id:
            continue

        # Try to create the task; silently ignored if it already exists
        _taskvia_request('POST', f'/api/missions/{slug}/tasks', {
            'id': task_id,
            'title': meta.get('title', ''),
            'skills': meta.get('skills', []),
            'priority': meta.get('priority', 'medium'),
            'blocked_by': meta.get('blocked_by', []),
        })

        # Always PATCH to sync current status / assignee / result
        status = meta.get('status', 'pending')
        patch: dict = {'status': status}
        if meta.get('worker'):
            patch['assignee'] = meta['worker']
        if status == 'done':
            _, result_text = parse_task_body(body)
            if result_text:
                patch['result'] = result_text

        _taskvia_request('PATCH', f'/api/missions/{slug}/tasks/{task_id}', patch)

    print(f"[resync] done: {slug} ({len(tasks)} task(s) synced)")


def cmd_resync(args):
    """Resync local mission(s) to Taskvia.

    Usage:
      plan.sh resync <slug>    — resync a specific mission
      plan.sh resync --all     — resync all active missions
    """
    opts, positional = parse_opts(args, {'--all': 'bool'})
    if opts.get('--all'):
        state = load_state()
        slugs = state.get('active_missions') or []
        if not slugs:
            print("[resync] No active missions to resync.", file=sys.stderr)
            return
    elif positional:
        slugs = [positional[0]]
    else:
        die("resync requires <slug> or --all")

    for slug in slugs:
        _resync_one(slug)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

dispatch = {
    'init': cmd_init,
    'add': cmd_add,
    'pull': cmd_pull,
    'done': cmd_done,
    'fail': cmd_fail,
    'ready-for-verification': cmd_ready_for_verification,
    'verify-result': cmd_verify_result,
    'review': cmd_review,
    'launch': cmd_launch,
    'lint': cmd_lint,
    'status': cmd_status,
    'archive': cmd_archive,
    'resync': cmd_resync,
}

if SUBCOMMAND not in dispatch:
    print(f"Unknown subcommand: {SUBCOMMAND}", file=sys.stderr)
    print(f"Available: {', '.join(dispatch)}", file=sys.stderr)
    sys.exit(1)

dispatch[SUBCOMMAND](ARGS)
PYEOF
