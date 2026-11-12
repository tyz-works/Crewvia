#!/usr/bin/env python3
"""赤の実証 (PR-C): 取得の対象・終端・間隔・通知の条件を 1 つずつ壊し、tests/test_ci_follow_and_notify.py が赤になることを確かめる。

    python3 tests/red_proof_ci_follow_notify.py

壊す前に対象を退避 (cp) し、終わったら退避から戻す (git checkout で戻さない = 未 commit の修正を消さない)。
置換元がちょうど 1 回でなければ BROKEN。全部の欠陥が赤なら exit 0。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib_ci_status.py"
DISP = REPO / "scripts" / "dispatcher.sh"
TEST = REPO / "tests" / "test_ci_follow_and_notify.py"

DEFECTS = [
    ("done の task を見ない (status を絞る)", LIB, "    return status in FOLLOW_STATUSES and card_pr(task.get('pr_number')) is not None",
     "    return False"),
    ("MERGED / CLOSED を外さない", LIB, "        if pr_closed(prev, t.get('pr_number')):\n            continue", "        if False:\n            continue"),
    ("結論が出た PR も 60 秒", LIB, "    if prev is not None and prev.get('state') in ('passed', 'failed'):", "    if False:"),
    ("上限を task 単位で数える", LIB, "        free = pr_key is not None and (t.get('target_dir') or '', pr_key) in by_pr", "        free = False"),
    ("unknown を 1 回で通知する", LIB, "            if rec.get('unknown_streak', 0) >= UNKNOWN_NOTIFY_STREAK:", "            if True:"),
    ("unknown の連続を数えない", LIB, "            rec['unknown_streak'] = (prev.get('unknown_streak', 1) if was else 0) + 1", "            rec['unknown_streak'] = 1"),
    ("門を通らない緑を ci-green にする", LIB, "            if ok:\n                events.append({'kind': 'ci-green'", "            if True:\n                events.append({'kind': 'ci-green'"),
    ("終端の PR にも通知する", LIB, "        if rec.get('pr_state') in PR_TERMINAL_STATES:\n            closed.add(pr_number)\n            continue", "        if False:\n            continue"),
    ("通知のキーに head を入れない", LIB, "    head = (event.get('head') or 'nohead')[:12]", "    head = 'x'"),
    ("main の失敗を見ない", LIB, "        if r is not None and r['conclusion'] in _RUN_BAD:", "        if False:"),
    ("main の進行中 run を完了済みとして数える", LIB, "        if event not in MAIN_EVENTS or status != 'completed':", "        if event not in MAIN_EVENTS:"),
    ("PR の state を同じ呼び出しで取らない", LIB, "'headRefOid,statusCheckRollup,state'", "'headRefOid,statusCheckRollup'"),
    ("通知を dispatcher のサイクルに載せない", DISP, "run_ci_notify()\n# t002 (t020", "# t002 (t020"),
    ("終端の PR の台帳を畳まない", DISP, "            for k in stale:\n                clear_told_key(k)", "            for k in []:\n                clear_told_key(k)"),
]


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp())
    originals = {p: p.read_text() for p in {d[1] for d in DEFECTS}}
    backups = {}
    for p in originals:
        backups[p] = tmp / (p.name + ".orig")
        shutil.copy2(p, backups[p])
    survived, broken = [], []
    try:
        for name, path, old, new in DEFECTS:
            text = originals[path]
            if text.count(old) != 1:
                print(f"BROKEN {name}")
                broken.append(name)
                continue
            path.write_text(text.replace(old, new))
            try:
                p = subprocess.run([sys.executable, "-m", "pytest", str(TEST), "-q", "-x", "-p", "no:cacheprovider"],
                                   capture_output=True, text=True, cwd=REPO)
            finally:
                path.write_text(text)
            red = p.returncode != 0
            print(f"{'RED  ' if red else 'GREEN'} {name}")
            if not red:
                survived.append(name)
    finally:
        for p, b in backups.items():
            shutil.copy2(b, p)
    for p, t in originals.items():
        assert p.read_text() == t
    if survived or broken:
        print("赤にならなかった欠陥:", survived, "置換元が決まらない:", broken)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
