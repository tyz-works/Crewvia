#!/usr/bin/env python3
"""checkpoint_gc.py — 古い checkpoint を片付ける (vNext 02 PR-G。設計 `knowledge/vnext-02-attempt-reopen-checkpoint.md` §5.1)。

`registry/checkpoints/<mission>/<task>/<execution_id>/` (退役の直前に `lib_checkpoint.py` が保存した worktree の状態) は
patch・untracked を含むので溜め続けない。`worktree_gc.py` と同じ作法:

    python3 scripts/checkpoint_gc.py                      # dry-run (既定。何も変えない・何も書かない)
    python3 scripts/checkpoint_gc.py --apply              # 対象を**削除せず隔離する**
    python3 scripts/checkpoint_gc.py --list-quarantine    # 隔離済みの一覧
    python3 scripts/checkpoint_gc.py --restore <PATH>     # 隔離を元に戻す (隔離先 or 元のパス)
    オプション: --repo <主 checkout> --registry <dir> --queue <dir> --days 30 --json

対象にしてよいのは**全部**満たすものだけ:
  1. dir が checkpoint として読める (`lib_checkpoint.verify`。meta.json の execution_id と dir 名が一致)
  2. 保存 (meta の `taken_at`) から `--days` 日 (既定 30。ユーザー判断 7) 以上
  3. その task の card が**読めて**終端 (`lib_task_status.TERMINAL_STATUSES` = done / verified / skipped)。
     card が無い (mission dir にも archive にも)・読めない・終端でない (pending / failed / needs_director …) は残す
読めない・判定できない・形が違うものは**残す** (破棄ではなく保留)。`--apply` は `git worktree move` ではなく
`os.rename` で `registry/checkpoints/.quarantine/<YYYYMMDD-HHMMSS>/<mission>/<task>/<execution_id>/` へ移すだけ。
**削除は呼ばない** (`rm -rf` / `shutil.rmtree` / `os.remove` / `unlink` はこのファイルに無い。テストが AST で固定)。
実際の削除はこのツールの外、人間が手で行う。隔離する直前に判定をやり直す。
"""
import argparse
import calendar
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lib_checkpoint  # noqa: E402
import lib_task_cards  # noqa: E402
import lib_task_status  # noqa: E402

DEFAULT_DAYS = 30
QUARANTINE_DIRNAME = '.quarantine'

REMOVE = 'remove'      # 隔離してよい (dry-run では「隔離する予定」)
KEEP = 'keep'

R_OLD_TERMINAL = 'old-and-task-terminal'
R_UNVERIFIED = 'unverified-checkpoint'
R_PARTIAL = 'partial'
R_BAD_LAYOUT = 'unexpected-layout'
R_TAKEN_AT = 'taken-at-unreadable'
R_TOO_NEW = 'newer-than-retention'
R_CARD_MISSING = 'task-card-missing'
R_CARD_UNREADABLE = 'task-card-unreadable'
R_NOT_TERMINAL = 'task-not-terminal'
R_QUARANTINE_BUSY = 'quarantine-destination-exists'

_SLUG_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
_TID_RE = re.compile(r't\d+')
_EXEC_RE = re.compile(r'ex-[0-9a-f]{32}')
_TS_RE = re.compile(r'\d{8}-\d{6}')


class Verdict:
    def __init__(self, path, action, reason, mission=None, task=None, execution_id=None, detail=None):
        self.path, self.action, self.reason = path, action, reason
        self.mission, self.task, self.execution_id, self.detail = mission, task, execution_id, detail

    def as_dict(self):
        return {'path': self.path, 'action': self.action, 'reason': self.reason, 'mission': self.mission,
                'task': self.task, 'execution_id': self.execution_id, 'detail': self.detail}


def _listdir(path):
    """`(names, None)` / `([], 固定コード)`。ENOENT は `([], 'absent')`。"""
    try:
        return sorted(os.listdir(path)), None
    except FileNotFoundError:
        return [], 'absent'
    except OSError:
        return [], 'unreadable'


def _taken_at_epoch(value):
    try:
        return calendar.timegm(time.strptime(value, '%Y-%m-%dT%H:%M:%SZ'))
    except (TypeError, ValueError):
        return None


def _card_status(queue_dir, mission, task):
    """`('ok', status)` / `('missing',)` / `('unreadable',)`。mission dir を先に、無ければ archive を見る。"""
    seen_missing = 0
    for base in ('missions', 'archive'):
        path = os.path.join(queue_dir, base, mission, 'tasks', f"{task}.md")
        try:
            os.lstat(path)
        except FileNotFoundError:
            seen_missing += 1
            continue
        except OSError:
            return ('unreadable',)
        meta, _body = lib_task_cards.read_task_card(path, task)
        status = meta.get('status') if isinstance(meta, dict) else None
        if (not isinstance(status, str) or meta.get('parse_error')
                or status == lib_task_cards.CORRUPT_TASK_STATUS):
            return ('unreadable',)
        return ('ok', status)
    return ('missing',)


def judge(path, mission, task, execution_id, queue_dir, now, days):
    """1 つの checkpoint dir の判定 (何も書かない)。"""
    ident = dict(mission=mission, task=task, execution_id=execution_id)
    try:
        meta = lib_checkpoint.verify(path, execution_id)
    except lib_checkpoint.CheckpointError as e:
        return Verdict(path, KEEP, R_UNVERIFIED, detail=e.code, **ident)
    taken = _taken_at_epoch(meta.get('taken_at'))
    if taken is None:
        return Verdict(path, KEEP, R_TAKEN_AT, **ident)
    if now - taken < days * 86400:
        return Verdict(path, KEEP, R_TOO_NEW, **ident)
    if meta.get('mission') != mission or meta.get('task') != task:
        return Verdict(path, KEEP, R_UNVERIFIED, detail='mission_task_mismatch', **ident)
    got = _card_status(queue_dir, mission, task)
    if got[0] == 'missing':
        return Verdict(path, KEEP, R_CARD_MISSING, **ident)
    if got[0] != 'ok':
        return Verdict(path, KEEP, R_CARD_UNREADABLE, **ident)
    if got[1] not in lib_task_status.TERMINAL_STATUSES:
        return Verdict(path, KEEP, R_NOT_TERMINAL, detail=got[1], **ident)
    return Verdict(path, REMOVE, R_OLD_TERMINAL, **ident)


def scan(registry_dir, queue_dir, now=None, days=DEFAULT_DAYS):
    """全 checkpoint の判定の一覧。`(verdicts, error_code | None)`。`checkpoints/` が無ければ `([], None)`。"""
    now = time.time() if now is None else now
    root = os.path.join(registry_dir, 'checkpoints')
    missions, code = _listdir(root)
    if code == 'absent':
        return [], None
    if code:
        return [], code
    out = []
    for m in missions:
        if m == QUARANTINE_DIRNAME:
            continue
        mdir = os.path.join(root, m)
        if not _SLUG_RE.fullmatch(m) or not os.path.isdir(mdir) or os.path.islink(mdir):
            out.append(Verdict(mdir, KEEP, R_BAD_LAYOUT))
            continue
        tasks, code = _listdir(mdir)
        if code:
            out.append(Verdict(mdir, KEEP, R_BAD_LAYOUT, detail=code))
            continue
        for t in tasks:
            tdir = os.path.join(mdir, t)
            if not _TID_RE.fullmatch(t) or not os.path.isdir(tdir) or os.path.islink(tdir):
                out.append(Verdict(tdir, KEEP, R_BAD_LAYOUT, mission=m))
                continue
            execs, code = _listdir(tdir)
            if code:
                out.append(Verdict(tdir, KEEP, R_BAD_LAYOUT, mission=m, task=t, detail=code))
                continue
            for x in execs:
                xdir = os.path.join(tdir, x)
                if x.endswith(lib_checkpoint.PARTIAL_SUFFIX):
                    out.append(Verdict(xdir, KEEP, R_PARTIAL, mission=m, task=t))
                    continue
                if not _EXEC_RE.fullmatch(x) or os.path.islink(xdir):
                    out.append(Verdict(xdir, KEEP, R_BAD_LAYOUT, mission=m, task=t))
                    continue
                out.append(judge(xdir, m, t, x, queue_dir, now, days))
    return out, None


def quarantine_root(registry_dir):
    return os.path.join(registry_dir, 'checkpoints', QUARANTINE_DIRNAME)


def apply_quarantine(registry_dir, queue_dir, verdicts, now=None, days=DEFAULT_DAYS):
    """REMOVE の判定のものを**隔離する** (削除しない)。直前に判定をやり直す。結果の dict の一覧。"""
    now = time.time() if now is None else now
    stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))
    results = []
    for v in verdicts:
        if v.action != REMOVE:
            continue
        again = judge(v.path, v.mission, v.task, v.execution_id, queue_dir, now, days)
        if again.action != REMOVE:
            results.append({'path': v.path, 'quarantined': False, 'reason': f"rejudged:{again.reason}"})
            continue
        dest = os.path.join(quarantine_root(registry_dir), stamp, v.mission, v.task, v.execution_id)
        if os.path.lexists(dest):
            results.append({'path': v.path, 'quarantined': False, 'reason': R_QUARANTINE_BUSY})
            continue
        try:
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            os.rename(v.path, dest)
        except OSError as e:
            results.append({'path': v.path, 'quarantined': False, 'reason': f"rename-failed:{type(e).__name__}"})
            continue
        results.append({'path': v.path, 'quarantined': True, 'quarantine': dest})
    return results


def find_quarantine_entries(registry_dir):
    """`([(隔離先, 元のパス)], error | None)`。形が違うものは数えない。"""
    qroot = quarantine_root(registry_dir)
    stamps, code = _listdir(qroot)
    if code == 'absent':
        return [], None
    if code:
        return [], code
    root = os.path.join(registry_dir, 'checkpoints')
    found = []
    for ts in stamps:
        if not _TS_RE.fullmatch(ts):
            continue
        for m in _listdir(os.path.join(qroot, ts))[0]:
            for t in _listdir(os.path.join(qroot, ts, m))[0]:
                for x in _listdir(os.path.join(qroot, ts, m, t))[0]:
                    if _EXEC_RE.fullmatch(x):
                        found.append((os.path.join(qroot, ts, m, t, x), os.path.join(root, m, t, x)))
    return found, None


def restore(registry_dir, given):
    """隔離を元に戻す。`(ok, message)`。元の場所に既にあれば断る (上書きしない)。"""
    entries, code = find_quarantine_entries(registry_dir)
    if code:
        return False, f"quarantine unreadable ({code})"
    given = os.path.abspath(given)
    matches = [(q, o) for q, o in entries if given in (q, o)]
    if not matches:
        return False, 'no such quarantined checkpoint'
    q, orig = sorted(matches)[-1]          # 同じ元のパスが複数なら新しい隔離
    if os.path.lexists(orig):
        return False, 'destination exists (not overwritten)'
    try:
        os.makedirs(os.path.dirname(orig), mode=0o700, exist_ok=True)
        os.rename(q, orig)
    except OSError as e:
        return False, f"rename-failed:{type(e).__name__}"
    return True, orig


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='対象を隔離する (削除しない)')
    mode.add_argument('--restore', metavar='PATH', default=None, help='隔離先 or 元のパスを元に戻す')
    mode.add_argument('--list-quarantine', action='store_true')
    ap.add_argument('--repo', default=os.environ.get('CREWVIA_REPO_ROOT') or '.')
    ap.add_argument('--registry', default=None)
    ap.add_argument('--queue', default=None)
    ap.add_argument('--days', type=float, default=DEFAULT_DAYS)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    repo = os.path.abspath(args.repo)
    registry = os.path.abspath(args.registry or os.path.join(repo, 'registry'))
    queue = os.path.abspath(args.queue or os.environ.get('CREWVIA_QUEUE') or os.path.join(repo, 'queue'))

    if args.restore:
        ok, msg = restore(registry, args.restore)
        print(('restored: ' if ok else 'refused: ') + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if args.list_quarantine:
        entries, code = find_quarantine_entries(registry)
        if code:
            print(f"quarantine unreadable ({code})", file=sys.stderr)
            return 1
        for q, o in entries:
            print(f"{q}\t{o}")
        return 0

    verdicts, code = scan(registry, queue, days=args.days)
    if code:
        print(f"checkpoints unreadable ({code}) — nothing judged", file=sys.stderr)
        return 1
    applied = apply_quarantine(registry, queue, verdicts, days=args.days) if args.apply else None
    if args.json:
        print(json.dumps({'verdicts': [v.as_dict() for v in verdicts], 'applied': applied}, ensure_ascii=False, indent=2))
    else:
        for v in verdicts:
            print(f"{v.action}\t{v.reason}\t{v.path}")
        n = sum(1 for v in verdicts if v.action == REMOVE)
        if applied is None:
            print(f"{n} of {len(verdicts)} checkpoint(s) would be quarantined (dry-run; --apply で隔離)")
        else:
            done = sum(1 for a in applied if a.get('quarantined'))
            print(f"{done} of {n} checkpoint(s) quarantined (of {len(verdicts)} judged)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
