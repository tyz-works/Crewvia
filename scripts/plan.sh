#!/usr/bin/env bash
set -euo pipefail

# plan.sh — Crewvia タスクプラン管理 CLI
# Usage:
#   plan.sh init "<mission>" [--force]
#   plan.sh add "<title>" --skills "<csv>" [--blocked-by "<csv>"] [--priority high|medium|low] [--description "<text>"]
#   plan.sh pull --skills "<csv>" [--agent "<name>"]
#   plan.sh done <task_id> "<result>"
#   plan.sh status

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLAN_FILE="${CREWVIA_PLAN:-${REPO_ROOT}/queue/plan.yaml}"

if [[ $# -eq 0 ]]; then
  echo "Usage: plan.sh <init|add|pull|done|status> [args...]" >&2
  exit 1
fi

SUBCOMMAND="$1"
shift

mkdir -p "$(dirname "$PLAN_FILE")"

# Delegate all logic to Python3
python3 - "$PLAN_FILE" "$SUBCOMMAND" "$@" <<'PYEOF'
import sys
import os
import json
import fcntl
import re
from datetime import datetime, timezone

plan_path = sys.argv[1]
subcommand = sys.argv[2]
args = sys.argv[3:]

PRIORITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}


# ---------------------------------------------------------------------------
# Minimal YAML helpers (no external deps)
# ---------------------------------------------------------------------------

def load_plan(path):
    """Load plan.yaml → dict. Returns None if file does not exist."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return parse_plan_yaml(f.read())


def parse_plan_yaml(text):
    """Parse the narrow subset of YAML used by plan.yaml."""
    lines = text.splitlines()
    plan = {'tasks': []}
    i = 0
    while i < len(lines):
        line = lines[i]
        # Top-level scalar fields
        m = re.match(r'^(\w+):\s*(.*)$', line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key == 'tasks':
                i += 1
                tasks, i = parse_task_list(lines, i)
                plan['tasks'] = tasks
                continue
            else:
                plan[key] = _scalar(val)
        i += 1
    return plan


def parse_task_list(lines, i):
    tasks = []
    while i < len(lines):
        line = lines[i]
        m_id_line = re.match(r'^  - id:\s*(\S+)', line)
        if m_id_line:
            task = {'id': m_id_line.group(1)}
            i += 1
            while i < len(lines) and not re.match(r'^  - id:', lines[i]) and not re.match(r'^\S', lines[i]):
                tline = lines[i]
                # list field: skills / blocked_by
                m_list_key = re.match(r'^    (\w+): \[(.*)\]$', tline)
                if m_list_key:
                    lkey = m_list_key.group(1)
                    items_str = m_list_key.group(2).strip()
                    task[lkey] = [s.strip() for s in items_str.split(',') if s.strip()] if items_str else []
                    i += 1
                    continue
                m_kv = re.match(r'^    (\w+): (.*)$', tline)
                if m_kv:
                    task[m_kv.group(1)] = _scalar(m_kv.group(2).strip())
                i += 1
            tasks.append(task)
            continue
        break
    return tasks, i


def _scalar(val):
    if val == 'null':
        return None
    if val in ('true', 'True'):
        return True
    if val in ('false', 'False'):
        return False
    # quoted string
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    return val


def dump_plan(plan):
    """Serialize plan dict → YAML string."""
    lines = []
    lines.append(f"mission: {_dump_scalar(plan.get('mission', ''))}")
    lines.append(f"created_at: {_dump_scalar(plan.get('created_at', ''))}")
    lines.append(f"status: {plan.get('status', 'in_progress')}")
    lines.append("tasks:")
    for t in plan.get('tasks', []):
        lines.append(f"  - id: {t['id']}")
        lines.append(f"    title: {_dump_scalar(t['title'])}")
        lines.append(f"    description: {_dump_scalar(t.get('description', ''))}")
        skills_str = ', '.join(t.get('skills', []))
        lines.append(f"    skills: [{skills_str}]")
        lines.append(f"    priority: {t.get('priority', 'medium')}")
        lines.append(f"    status: {t.get('status', 'backlog')}")
        bb_str = ', '.join(t.get('blocked_by', []))
        lines.append(f"    blocked_by: [{bb_str}]")
        lines.append(f"    worker: {_dump_scalar(t.get('worker'))}")
        lines.append(f"    started_at: {_dump_scalar(t.get('started_at'))}")
        lines.append(f"    completed_at: {_dump_scalar(t.get('completed_at'))}")
        lines.append(f"    result: {_dump_scalar(t.get('result'))}")
    return '\n'.join(lines) + '\n'


def _dump_scalar(val):
    if val is None:
        return 'null'
    s = str(val)
    # Quote if contains special chars
    if any(c in s for c in (':',  '#', '[', ']', '{', '}', ',', "'", '"', '\n')):
        escaped = s.replace('"', '\\"')
        return f'"{escaped}"'
    return s if s else '""'


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def make_task(task_id, title, skills, description='', priority='medium', blocked_by=None):
    return {
        'id': task_id,
        'title': title,
        'description': description,
        'skills': skills,
        'priority': priority,
        'status': 'backlog',
        'blocked_by': blocked_by or [],
        'worker': None,
        'started_at': None,
        'completed_at': None,
        'result': None,
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(args):
    if not args:
        print("init requires a mission title", file=sys.stderr)
        sys.exit(1)
    mission = args[0]
    force = '--force' in args

    if os.path.exists(plan_path) and not force:
        print(f"plan.yaml already exists. Use --force to overwrite.", file=sys.stderr)
        sys.exit(1)

    plan = {
        'mission': mission,
        'created_at': now_iso(),
        'status': 'in_progress',
        'tasks': [],
    }
    with open(plan_path, 'w') as f:
        f.write(dump_plan(plan))
    print(f"Initialized: {plan_path}")


def cmd_add(args):
    if not args:
        print("add requires a title", file=sys.stderr)
        sys.exit(1)
    title = args[0]
    rest = args[1:]

    # Parse options
    skills_csv = ''
    blocked_by_csv = ''
    priority = 'medium'
    description = ''
    i = 0
    while i < len(rest):
        opt = rest[i]
        if opt == '--skills' and i + 1 < len(rest):
            skills_csv = rest[i + 1]; i += 2
        elif opt == '--blocked-by' and i + 1 < len(rest):
            blocked_by_csv = rest[i + 1]; i += 2
        elif opt == '--priority' and i + 1 < len(rest):
            priority = rest[i + 1]; i += 2
        elif opt == '--description' and i + 1 < len(rest):
            description = rest[i + 1]; i += 2
        else:
            i += 1

    skills = [s.strip() for s in skills_csv.split(',') if s.strip()]
    blocked_by = [s.strip() for s in blocked_by_csv.split(',') if s.strip()]

    if not os.path.exists(plan_path):
        print("plan.yaml not found. Run 'plan.sh init' first.", file=sys.stderr)
        sys.exit(1)

    with open(plan_path, 'r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        plan = parse_plan_yaml(f.read())
        task_num = len(plan['tasks']) + 1
        task_id = f"t{task_num:03d}"
        task = make_task(task_id, title, skills, description, priority, blocked_by)
        plan['tasks'].append(task)
        f.seek(0)
        f.write(dump_plan(plan))
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)

    print(f"Added: {task_id} — {title}")


def cmd_pull(args):
    skills_csv = ''
    agent = os.environ.get('AGENT_NAME', '')
    i = 0
    while i < len(args):
        opt = args[i]
        if opt == '--skills' and i + 1 < len(args):
            skills_csv = args[i + 1]; i += 2
        elif opt == '--agent' and i + 1 < len(args):
            agent = args[i + 1]; i += 2
        else:
            i += 1

    requested_skills = {s.strip() for s in skills_csv.split(',') if s.strip()}

    if not os.path.exists(plan_path):
        sys.exit(1)

    with open(plan_path, 'r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        plan = parse_plan_yaml(f.read())

        done_ids = {t['id'] for t in plan['tasks'] if t.get('status') in ('done', 'skipped')}

        # Candidates: backlog, sorted by priority
        candidates = [
            t for t in plan['tasks']
            if t.get('status') == 'backlog'
        ]
        candidates.sort(key=lambda t: PRIORITY_ORDER.get(t.get('priority', 'medium'), 1))

        chosen = None
        for t in candidates:
            # Skill match: any skill overlap
            if requested_skills and not requested_skills.intersection(set(t.get('skills', []))):
                continue
            # Dependency check
            blocked_by = t.get('blocked_by', [])
            if any(dep not in done_ids for dep in blocked_by):
                continue
            chosen = t
            break

        if chosen is None:
            fcntl.flock(f, fcntl.LOCK_UN)
            sys.exit(1)

        chosen['status'] = 'in_progress'
        chosen['worker'] = agent
        chosen['started_at'] = now_iso()

        f.seek(0)
        f.write(dump_plan(plan))
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)

    print(json.dumps(chosen))


def cmd_done(args):
    if len(args) < 2:
        print("done requires <task_id> and <result>", file=sys.stderr)
        sys.exit(1)
    task_id, result = args[0], args[1]

    if not os.path.exists(plan_path):
        print("plan.yaml not found.", file=sys.stderr)
        sys.exit(1)

    with open(plan_path, 'r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        plan = parse_plan_yaml(f.read())

        found = False
        for t in plan['tasks']:
            if t['id'] == task_id:
                t['status'] = 'done'
                t['completed_at'] = now_iso()
                t['result'] = result
                found = True
                break

        if not found:
            fcntl.flock(f, fcntl.LOCK_UN)
            print(f"Task '{task_id}' not found.", file=sys.stderr)
            sys.exit(1)

        # If all tasks done/skipped → mission done
        terminal = {'done', 'skipped'}
        if all(t.get('status') in terminal for t in plan['tasks']):
            plan['status'] = 'done'

        f.seek(0)
        f.write(dump_plan(plan))
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)

    print(f"Done: {task_id}")


def cmd_status(args):
    plan = load_plan(plan_path)
    if plan is None:
        print("No plan found. Run 'plan.sh init' first.")
        return

    tasks = plan.get('tasks', [])
    total = len(tasks)
    done_count = sum(1 for t in tasks if t.get('status') == 'done')

    print(f"Mission: {plan.get('mission', '(unnamed)')}")
    print(f"Status:  {plan.get('status', 'in_progress')}")
    print()

    STATUS_ICON = {
        'done': '✅',
        'in_progress': '🔄',
        'backlog': '📋',
        'skipped': '⏭️',
    }

    for t in tasks:
        st = t.get('status', 'backlog')
        icon = STATUS_ICON.get(st, '❓')
        tid = t['id']
        title = t['title']
        blocked_by = t.get('blocked_by', [])

        if st == 'done':
            worker = t.get('worker') or ''
            suffix = f"({worker}, 完了)" if worker else "(完了)"
        elif st == 'in_progress':
            worker = t.get('worker') or ''
            suffix = f"({worker}, 進行中)" if worker else "(進行中)"
        elif blocked_by:
            deps = ', '.join(blocked_by)
            suffix = f"(blocked: {deps})"
        else:
            suffix = "(backlog)"

        print(f"[{icon}] {tid} {title} {suffix}")

    print()
    print(f"Progress: {done_count}/{total} done")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

dispatch = {
    'init': cmd_init,
    'add': cmd_add,
    'pull': cmd_pull,
    'done': cmd_done,
    'status': cmd_status,
}

if subcommand not in dispatch:
    print(f"Unknown subcommand: {subcommand}", file=sys.stderr)
    print(f"Available: {', '.join(dispatch)}", file=sys.stderr)
    sys.exit(1)

dispatch[subcommand](args)
PYEOF
