#!/usr/bin/env python3
"""Registry helper — parse and write registry/workers.yaml while preserving
header comments and deduping same-name entries.

Usable as:
  - Module: from lib_registry import parse, write, register_director, ...
  - CLI:    python3 lib_registry.py <command> [args...]

CLI commands:
  get-director PATH
      Print the registered director name (if any).

  register-director PATH NAME
      Mark NAME as director. If an entry with that name already exists,
      update its role field in place (avoids duplicate entries).

  set-last-active PATH NAME [YYYY-MM-DD]
      Update last_active for NAME. No-op if name not in registry.

  bump-task-count PATH NAME [YYYY-MM-DD]
      Increment task_count and update last_active for NAME (Worker Step4).
      No-op if name not in registry.
"""
import fcntl
import os
import re
import sys
from datetime import date


def _lock_path(path):
    """Dedicated lock file for a given registry path, sibling to the registry
    file itself. Not shared with queue/.lock (scripts/plan.sh) — a distinct
    lock file per resource keeps registry and queue contention independent,
    and lets tests target an isolated temp registry without touching the
    real lock."""
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    return os.path.join(directory, '.workers.lock')


def with_lock(path, callback):
    """Run callback() while holding an exclusive lock scoped to `path`'s
    registry directory. Mirrors scripts/plan.sh's with_lock (fcntl.flock).

    ★Critical: callers must perform their ENTIRE parse-modify-write cycle
    inside `callback`, not just the write. Locking only the write step does
    not fix the lost-update race — a caller that already parsed a stale
    snapshot before the lock existed would still write stale data once it
    gets the lock. The lock must be held from before parse() through write().
    """
    lock_file = _lock_path(path)
    try:
        os.makedirs(os.path.dirname(lock_file) or '.', exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"cannot create registry dir for lock {lock_file}: {e}")
    try:
        lf = open(lock_file, 'a+')
    except OSError as e:
        raise RuntimeError(
            f"cannot open registry lock {lock_file}: {e}\n"
            f"  hint: check write permission, or remove a stale lock file."
        )
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return callback()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        lf.close()


def parse(path):
    """Read registry → (header_text, worker_order, workers_by_name).

    - header_text: everything before the 'workers:' top-level key, preserving
      comments and blank lines verbatim.
    - worker_order: list of worker names in first-seen order.
    - workers_by_name: dict of name → entry dict. Duplicate entries with the
      same name are merged (non-empty skills win, max task_count wins, latest
      last_active and role win).

    Returns ('', [], {}) for a missing file.
    Returns (content, [], {}) if the file exists but has no 'workers:' key.
    """
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        return '', [], {}

    m = re.search(r'^workers:.*$', content, re.MULTILINE)
    if not m:
        return content, [], {}

    header = content[:m.start()]
    body = content[m.end():]

    workers_by_name = {}
    order = []
    current = None

    for line in body.splitlines():
        txt = re.sub(r'\s*#.*$', '', line).rstrip()
        if not txt:
            continue
        m_name = re.match(r'^\s+-\s+name:\s+(\S+)', txt)
        if m_name:
            name = m_name.group(1)
            if name in workers_by_name:
                current = workers_by_name[name]
            else:
                current = {
                    'name': name,
                    'role': '',
                    'skills': [],
                    'task_count': 0,
                    'last_active': '',
                }
                workers_by_name[name] = current
                order.append(name)
            continue
        if current is None:
            continue
        m_role = re.match(r'^\s+role:\s+(\S+)', txt)
        if m_role:
            current['role'] = m_role.group(1)
            continue
        m_sk = re.match(r'^\s+skills:\s+\[([^\]]*)\]', txt)
        if m_sk:
            skills = [s.strip() for s in m_sk.group(1).split(',') if s.strip()]
            if skills:  # don't overwrite with empty
                current['skills'] = skills
            continue
        m_tc = re.match(r'^\s+task_count:\s+(\d+)', txt)
        if m_tc:
            current['task_count'] = max(current['task_count'], int(m_tc.group(1)))
            continue
        m_la = re.match(r'^\s+last_active:\s+(\S+)', txt)
        if m_la:
            current['last_active'] = m_la.group(1)
            continue
    return header, order, workers_by_name


def write(path, header, order, workers_by_name):
    """Write registry back to path, preserving header and emitting role when set."""
    out = []
    if header:
        out.append(header)
    if not order:
        out.append('workers: []\n')
    else:
        out.append('workers:\n')
        for name in order:
            w = workers_by_name[name]
            out.append(f"  - name: {w['name']}\n")
            if w.get('role'):
                out.append(f"    role: {w['role']}\n")
            skills_str = ', '.join(w.get('skills', []))
            out.append(f"    skills: [{skills_str}]\n")
            out.append(f"    task_count: {w.get('task_count', 0)}\n")
            out.append(f"    last_active: {w.get('last_active', '')}\n")
    with open(path, 'w') as f:
        f.writelines(out)


def register_director(path, name):
    """Add or update NAME as director. Same-name worker entries are upgraded."""
    def _do():
        header, order, by_name = parse(path)
        today = str(date.today())
        if name in by_name:
            by_name[name]['role'] = 'director'
            if not by_name[name].get('last_active'):
                by_name[name]['last_active'] = today
        else:
            by_name[name] = {
                'name': name,
                'role': 'director',
                'skills': [],
                'task_count': 0,
                'last_active': today,
            }
            order.append(name)
        write(path, header, order, by_name)
    with_lock(path, _do)


def get_director(path):
    """Return the director name, or None if none registered."""
    _, order, by_name = parse(path)
    for n in order:
        if by_name[n].get('role') == 'director':
            return n
    return None


def set_last_active(path, name, day=None):
    """Update last_active for NAME. No-op if name is not in the registry."""
    def _do():
        header, order, by_name = parse(path)
        if name not in by_name:
            return
        by_name[name]['last_active'] = day or str(date.today())
        write(path, header, order, by_name)
    with_lock(path, _do)


def bump_task_count(path, name, day=None):
    """Increment task_count and update last_active for NAME (Worker Step4).
    No-op if name is not in the registry. Replaces the raw read_text/
    write_text idiom formerly inlined in agents/worker.md."""
    def _do():
        header, order, by_name = parse(path)
        if name not in by_name:
            return
        by_name[name]['task_count'] = by_name[name].get('task_count', 0) + 1
        by_name[name]['last_active'] = day or str(date.today())
        write(path, header, order, by_name)
    with_lock(path, _do)


def _main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1
    cmd = argv[1]
    if cmd == 'get-director':
        if len(argv) < 3:
            print("usage: get-director PATH", file=sys.stderr)
            return 2
        result = get_director(argv[2])
        if result is not None:
            print(result)
        return 0
    if cmd == 'register-director':
        if len(argv) < 4:
            print("usage: register-director PATH NAME", file=sys.stderr)
            return 2
        register_director(argv[2], argv[3])
        return 0
    if cmd == 'set-last-active':
        if len(argv) < 4:
            print("usage: set-last-active PATH NAME [YYYY-MM-DD]", file=sys.stderr)
            return 2
        day = argv[4] if len(argv) > 4 else None
        set_last_active(argv[2], argv[3], day)
        return 0
    if cmd == 'bump-task-count':
        if len(argv) < 4:
            print("usage: bump-task-count PATH NAME [YYYY-MM-DD]", file=sys.stderr)
            return 2
        day = argv[4] if len(argv) > 4 else None
        bump_task_count(argv[2], argv[3], day)
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(_main(sys.argv))
