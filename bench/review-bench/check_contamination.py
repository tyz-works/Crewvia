#!/usr/bin/env python3
"""汚染検査: claude の session jsonl の Read / Grep / Glob の path が、巡の clone と diff dir の下に収まるか。

usage: check_contamination.py --clone <dir> --diff-dir <dir> --session-id <uuid> [--projects-dir ~/.claude/projects]
--session-id は runner が `<out>/<label>.claude.session` に記録した UUID。その 1 ファイル (<uuid>.jsonl) だけを見る
(clone の dir にある他の session を混ぜない。再実行や別の実行の記録で誤って陽性/陰性にならない)。
exit 0 = 外を読んだ記録 0 件 / 1 = 外を読んだ疑い (path を stdout に出す) / 2 = jsonl が無い (検査不能)
/ 3 = 壊れた JSON 行がある・検査した tool_use が 0 件 (inconclusive。汚染なしとは言えない)
Glob の pattern・Grep の glob が絶対パス・~・`..` 区間を含めば外とみなす (1)。path 無しの Grep / Glob は cwd (= clone) なので許容。Read の path が無い・非文字列は inconclusive (3)。StructuredOutput 等の入力は見ない。
"""
import argparse
import json
import os
import re
import sys

TOOLS = {'Read': ('file_path',), 'Grep': ('path',), 'Glob': ('path',)}
# path の他に、パターンの形で root の外に出られる欄 (Glob の pattern・Grep の glob)
# Grep の pattern は検索する中身の正規表現でパスではない (どこを読むかは path と glob が決める) ので見ない
PATTERN_FIELDS = {'Glob': ('pattern',), 'Grep': ('glob',)}


def pattern_escapes(v):
    """絶対パス・~ 始まり・`..` の区間を含むパターンは root の外を指しうる → 外とみなす"""
    v = v.strip()
    return v.startswith(('/', '~')) or re.search(r'(^|/)\.\.(/|$)', v) is not None



def under(path, roots):
    p = os.path.realpath(path)
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def session_files(projects_dir, clone, session_id):
    # claude は cwd の / . _ を - に置換した名前の dir に jsonl を置く
    key = ''.join('-' if c in '/._' else c for c in os.path.realpath(clone))
    d = os.path.join(projects_dir, key)
    if not os.path.isdir(d):
        return []
    fp = os.path.join(d, session_id + '.jsonl')
    return [fp] if os.path.isfile(fp) else []


def outside_reads(files, roots, cwd):
    bad, total, malformed, unauditable = [], 0, 0, 0
    for fp in files:
        with open(fp, encoding='utf-8', errors='replace') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    malformed += 1
                    continue
                if not isinstance(rec, dict):
                    malformed += 1
                    continue
                msg = rec.get('message') or {}
                content = msg.get('content') if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict) or c.get('type') != 'tool_use' or c.get('name') not in TOOLS:
                        continue
                    total += 1
                    inp = c.get('input')
                    if not isinstance(inp, dict):
                        unauditable += 1
                        continue
                    for k in PATTERN_FIELDS.get(c['name'], ()):
                        pv = inp.get(k)
                        if isinstance(pv, str) and pattern_escapes(pv):
                            bad.append((c['name'] + '.' + k, pv))
                    for k in TOOLS[c['name']]:
                        v = inp.get(k)
                        if v is None and c['name'] != 'Read':
                            continue  # path 無しの Grep / Glob は cwd (= clone)
                        if not isinstance(v, str) or not v:
                            unauditable += 1  # Read の path が無い・文字列でない → 何を読んだか分からない
                            continue
                        full = v if os.path.isabs(v) else os.path.join(cwd, v)
                        if not under(full, roots):
                            bad.append((c['name'], v))
    return total, bad, malformed + unauditable


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--clone', required=True)
    ap.add_argument('--diff-dir', required=True)
    ap.add_argument('--session-id', required=True)
    ap.add_argument('--projects-dir', default=os.path.expanduser('~/.claude/projects'))
    a = ap.parse_args(argv)
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', a.session_id):
        print('bad --session-id', file=sys.stderr)
        return 2
    files = session_files(a.projects_dir, a.clone, a.session_id)
    if not files:
        print('no session jsonl for', a.clone, a.session_id, file=sys.stderr)
        return 2
    roots = [os.path.realpath(a.clone), os.path.realpath(a.diff_dir)]
    total, bad, malformed = outside_reads(files, roots, os.path.realpath(a.clone))
    for name, v in bad:
        print(f'OUTSIDE {name} {v}')
    print(f'checked={total} outside={len(bad)} malformed={malformed}', file=sys.stderr)
    if bad:
        return 1
    if malformed or total == 0:
        print('inconclusive: malformed log lines, unauditable path or no tool_use audited', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
