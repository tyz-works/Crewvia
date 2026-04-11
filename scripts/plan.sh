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
#   plan.sh status [--mission <slug>] [--all]
#   plan.sh archive <slug>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
QUEUE_DIR="${CREWVIA_QUEUE:-${REPO_ROOT}/queue}"

if [[ $# -eq 0 ]]; then
  echo "Usage: plan.sh <init|add|pull|done|status|archive> [args...]" >&2
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
from datetime import datetime, timezone

QUEUE_DIR = sys.argv[1]
SUBCOMMAND = sys.argv[2]
ARGS = sys.argv[3:]

STATE_FILE = os.path.join(QUEUE_DIR, 'state.yaml')
MISSIONS_DIR = os.path.join(QUEUE_DIR, 'missions')
ARCHIVE_DIR = os.path.join(QUEUE_DIR, 'archive')
LOCK_FILE = os.path.join(QUEUE_DIR, '.lock')

PRIORITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}
TERMINAL_STATUSES = {'done', 'skipped'}

STATUS_ICON = {
    'done': '✅',
    'in_progress': '🔄',
    'pending': '📋',
    'skipped': '⏭️',
}


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# Minimal YAML helpers (narrow subset, no external deps)
# ---------------------------------------------------------------------------

def parse_yaml(text):
    """Parse a narrow subset: scalar fields, inline lists, block lists."""
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
        key = m.group(1)
        val = m.group(2).rstrip()
        if val == '':
            # Possible block list on subsequent indented `- ` lines
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
    s = str(val)
    if any(ch in ',[]{}#:\'"\n' for ch in s) or s == '':
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return s


# ---------------------------------------------------------------------------
# Frontmatter helpers (.md task files)
# ---------------------------------------------------------------------------

TASK_META_KEY_ORDER = [
    'id', 'title', 'skills', 'priority', 'status',
    'blocked_by', 'worker', 'started_at', 'completed_at',
]


def parse_frontmatter(text):
    """Split a markdown file into (meta dict, body string)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise ValueError("missing frontmatter delimiter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end = i
            break
    if end is None:
        raise ValueError("unterminated frontmatter")
    meta_text = '\n'.join(lines[1:end])
    body = '\n'.join(lines[end + 1:])
    if body.startswith('\n'):
        body = body[1:]
    return parse_yaml(meta_text), body


def serialize_frontmatter(meta, body):
    yaml_text = dump_yaml(meta, key_order=TASK_META_KEY_ORDER)
    if not body.endswith('\n'):
        body = body + '\n'
    return f"---\n{yaml_text}---\n\n{body}"


def parse_task_body(body):
    """Extract Description and Result sections from task body."""
    sections = {}
    current = None
    buf = []
    for line in body.splitlines():
        m = re.match(r'^##\s+(Description|Result)\s*$', line, re.IGNORECASE)
        if m:
            if current is not None:
                sections[current] = '\n'.join(buf).rstrip()
            current = m.group(1).lower()
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
        data = parse_yaml(f.read())
    if 'active_missions' not in data or data['active_missions'] is None:
        data['active_missions'] = []
    if 'default_mission' not in data:
        data['default_mission'] = None
    return data


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
    text = '\n'.join(lines) + '\n'
    with open(STATE_FILE, 'w') as f:
        f.write(text)


def mission_dir(slug):
    return os.path.join(MISSIONS_DIR, slug)


def mission_yaml_path(slug):
    return os.path.join(mission_dir(slug), 'mission.yaml')


def tasks_dir(slug):
    return os.path.join(mission_dir(slug), 'tasks')


def task_path(slug, task_id):
    return os.path.join(tasks_dir(slug), f"{task_id}.md")


MISSION_KEY_ORDER = ['title', 'slug', 'status', 'created_at', 'completed_at', 'next_task_id']


def load_mission(slug):
    path = mission_yaml_path(slug)
    if not os.path.exists(path):
        die(f"mission '{slug}' not found at {path}")
    with open(path) as f:
        return parse_yaml(f.read())


def save_mission(slug, data):
    os.makedirs(mission_dir(slug), exist_ok=True)
    with open(mission_yaml_path(slug), 'w') as f:
        f.write(dump_yaml(data, key_order=MISSION_KEY_ORDER))


def load_task(slug, task_id):
    path = task_path(slug, task_id)
    if not os.path.exists(path):
        die(f"task '{task_id}' not found in mission '{slug}'")
    with open(path) as f:
        return parse_frontmatter(f.read())


def save_task(slug, task_id, meta, body):
    os.makedirs(tasks_dir(slug), exist_ok=True)
    with open(task_path(slug, task_id), 'w') as f:
        f.write(serialize_frontmatter(meta, body))


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
        with open(os.path.join(tdir, fn)) as f:
            try:
                meta, body = parse_frontmatter(f.read())
            except ValueError as e:
                die(f"failed to parse {fn}: {e}")
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
    os.makedirs(QUEUE_DIR, exist_ok=True)
    with open(LOCK_FILE, 'a+') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return callback()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


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

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if slug:
            existing = os.path.exists(mission_dir(slug))
            if existing and not force:
                die(f"mission '{slug}' already exists. Use --force to overwrite.")
            if existing:
                shutil.rmtree(mission_dir(slug))
        else:
            slug = generate_slug(title)

        os.makedirs(tasks_dir(slug), exist_ok=True)
        mission = {
            'title': title,
            'slug': slug,
            'status': 'in_progress',
            'created_at': now_iso(),
            'completed_at': None,
            'next_task_id': 1,
        }
        save_mission(slug, mission)

        active = state.get('active_missions') or []
        if slug not in active:
            active.append(slug)
        state['active_missions'] = active
        state['default_mission'] = slug
        save_state(state)
        print(f"Initialized mission: {slug}")
        print(f"  title: {title}")
        print(f"  path:  {mission_dir(slug)}")

    with_lock(_do)


def cmd_add(args):
    opts, positional = parse_opts(args, {
        '--mission': 'value',
        '--skills': 'value',
        '--blocked-by': 'value',
        '--priority': 'value',
        '--description': 'value',
    })
    if not positional:
        die("add requires a task title")
    title = positional[0]

    skills = [s.strip() for s in opts.get('--skills', '').split(',') if s.strip()]
    blocked_by = [s.strip() for s in opts.get('--blocked-by', '').split(',') if s.strip()]
    priority = opts.get('--priority', 'medium')
    if priority not in PRIORITY_ORDER:
        die(f"invalid priority '{priority}'. Use high|medium|low.")
    description = opts.get('--description', '')

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
            'worker': None,
            'started_at': None,
            'completed_at': None,
        }
        body = build_task_body(description, '')
        save_task(slug, task_id, meta, body)

        mission['next_task_id'] = task_num + 1
        save_mission(slug, mission)
        print(f"Added: {slug}/{task_id} — {title}")

    with_lock(_do)


def cmd_pull(args):
    opts, _ = parse_opts(args, {
        '--mission': 'value',
        '--skills': 'value',
        '--agent': 'value',
    })
    requested_skills = {s.strip() for s in opts.get('--skills', '').split(',') if s.strip()}
    agent = opts.get('--agent') or os.environ.get('AGENT_NAME', '')

    chosen_holder = [None]

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
            return

        candidates = []
        for slug in slugs:
            if not os.path.exists(mission_dir(slug)):
                continue
            tasks = list_tasks(slug)
            done_ids = {m['id'] for (m, _) in tasks if m.get('status') in TERMINAL_STATUSES}
            for (meta, body) in tasks:
                if meta.get('status') != 'pending':
                    continue
                if requested_skills and not requested_skills.intersection(set(meta.get('skills', []))):
                    continue
                bb = meta.get('blocked_by') or []
                if any(dep not in done_ids for dep in bb):
                    continue
                candidates.append((slug, meta, body))

        if not candidates:
            return

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
        }

    with_lock(_do)

    if chosen_holder[0] is None:
        sys.exit(1)
    print(json.dumps(chosen_holder[0], ensure_ascii=False))


def cmd_done(args):
    opts, positional = parse_opts(args, {'--mission': 'value'})
    if len(positional) < 2:
        die("done requires <task_id> and <result>")
    task_id = positional[0]
    result = positional[1]

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
        if meta.get('status') == 'done':
            die(f"task '{task_id}' is already done.")

        meta['status'] = 'done'
        meta['completed_at'] = now_iso()
        desc, _ = parse_task_body(body)
        new_body = build_task_body(desc, result)
        save_task(slug, task_id, meta, new_body)

        # Mission complete?
        tasks = list_tasks(slug)
        if all(m.get('status') in TERMINAL_STATUSES for (m, _) in tasks):
            mission = load_mission(slug)
            mission['status'] = 'done'
            mission['completed_at'] = now_iso()
            save_mission(slug, mission)

        print(f"Done: {slug}/{task_id}")

    with_lock(_do)


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
        mission = parse_yaml(f.read())
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
        mission = parse_yaml(f.read())
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
        if st == 'done':
            worker = m.get('worker') or ''
            suffix = f"({worker}, 完了)" if worker else "(完了)"
        elif st == 'in_progress':
            worker = m.get('worker') or ''
            suffix = f"({worker}, 進行中)" if worker else "(進行中)"
        elif bb:
            unmet = [d for d in bb if d not in done_ids]
            suffix = f"(blocked: {', '.join(unmet)})" if unmet else "(pending)"
        else:
            suffix = "(pending)"
        print(f"[{icon}] {tid} {title} {suffix}")

    total = len(tasks)
    done_count = sum(1 for (m, _) in tasks if m.get('status') == 'done')
    print()
    print(f"Progress: {done_count}/{total} done")


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


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

dispatch = {
    'init': cmd_init,
    'add': cmd_add,
    'pull': cmd_pull,
    'done': cmd_done,
    'status': cmd_status,
    'archive': cmd_archive,
}

if SUBCOMMAND not in dispatch:
    print(f"Unknown subcommand: {SUBCOMMAND}", file=sys.stderr)
    print(f"Available: {', '.join(dispatch)}", file=sys.stderr)
    sys.exit(1)

dispatch[SUBCOMMAND](ARGS)
PYEOF
