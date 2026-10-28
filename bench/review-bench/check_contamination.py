#!/usr/bin/env python3
"""汚染検査: claude の session jsonl の Read / Grep / Glob の path が、巡の clone と diff dir の下に収まるか。

usage: check_contamination.py --clone <dir> --diff-dir <dir> [--projects-dir ~/.claude/projects]
exit 0 = 外を読んだ記録 0 件 / 1 = 外を読んだ疑い (path を stdout に出す) / 2 = jsonl が無い (検査不能)
path 無しの Grep / Glob は cwd (= clone) なので許容。StructuredOutput 等の入力は見ない。
"""
import argparse
import json
import os
import sys

TOOLS = {'Read': ('file_path',), 'Grep': ('path',), 'Glob': ('path',)}


def under(path, roots):
    p = os.path.realpath(path)
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def session_files(projects_dir, clone):
    # claude は cwd の / . _ を - に置換した名前の dir に jsonl を置く
    key = ''.join('-' if c in '/._' else c for c in os.path.realpath(clone))
    d = os.path.join(projects_dir, key)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, n) for n in os.listdir(d) if n.endswith('.jsonl'))


def outside_reads(files, roots, cwd):
    bad, total = [], 0
    for fp in files:
        with open(fp, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    msg = json.loads(line).get('message') or {}
                except (ValueError, AttributeError):
                    continue
                content = msg.get('content') if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict) or c.get('type') != 'tool_use' or c.get('name') not in TOOLS:
                        continue
                    total += 1
                    inp = c.get('input') or {}
                    for k in TOOLS[c['name']]:
                        v = inp.get(k)
                        if not isinstance(v, str):
                            continue
                        full = v if os.path.isabs(v) else os.path.join(cwd, v)
                        if not under(full, roots):
                            bad.append((c['name'], v))
    return total, bad


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--clone', required=True)
    ap.add_argument('--diff-dir', required=True)
    ap.add_argument('--projects-dir', default=os.path.expanduser('~/.claude/projects'))
    a = ap.parse_args(argv)
    files = session_files(a.projects_dir, a.clone)
    if not files:
        print('no session jsonl for', a.clone, file=sys.stderr)
        return 2
    roots = [os.path.realpath(a.clone), os.path.realpath(a.diff_dir)]
    total, bad = outside_reads(files, roots, os.path.realpath(a.clone))
    for name, v in bad:
        print(f'OUTSIDE {name} {v}')
    print(f'checked={total} outside={len(bad)}', file=sys.stderr)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
