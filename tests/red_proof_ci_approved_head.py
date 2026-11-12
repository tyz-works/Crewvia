#!/usr/bin/env python3
"""赤の実証: 承認 head の比較 (PR-D1) の条件を 1 つずつ壊し、tests/test_ci_approved_head.py が赤になることを確かめる。

    python3 tests/red_proof_ci_approved_head.py

壊す前に lib を退避 (cp) し、終わったら退避から戻す (git checkout で戻さない)。全部の欠陥が赤になれば exit 0。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib_ci_status.py"

DEFECTS = {
    "末尾行でなく本文のどこかを読む": ("m = _APPROVED_HEAD_RE.fullmatch(lines[-1])", "m = _APPROVED_HEAD_RE.search(result)"),
    "fullmatch をやめて前方一致にする": ("m = _APPROVED_HEAD_RE.fullmatch(lines[-1])", "m = _APPROVED_HEAD_RE.match(lines[-1])"),
    "終端でない review を許す": ("    if any(r.get('status') not in TERMINAL_STATUSES for r in reviews):\n        return False, 'no_approval'\n", ""),
    "最新でなく最古の review を見る": ("newest = max(stamp(r) for r in reviews)", "newest = min(stamp(r) for r in reviews)"),
    "同時刻の review の一部だけ見る": ("    if None in heads:\n        return False, 'no_approval'\n", "    heads.discard(None)\n    if not heads:\n        return False, 'no_approval'\n"),
    "現 head との比較を外す": ("    if heads != {pr_head}:\n        return False, 'approved_head_mismatch'\n", ""),
    "review が 1 本も無いのを承認とする": ("    if not reviews:\n        return False, 'no_approval'\n", ""),
    "現 head を取り直さない": ("            current = head\n            if head_of is not None:", "            current = head\n            if False:"),
    "取り直しの失敗で記録の head に倒す": ("                if err is not None:\n                    continue\n", "                if err is not None:\n                    current = head\n"),
    "承認が無くても merge-ready": ("            if approved:\n                events.append({'kind': 'merge-ready'", "            if True:\n                events.append({'kind': 'merge-ready'"),
    "review 系でない skill を数える": ("if set(t.get('skills') or ()) & set(APPROVAL_SKILLS):", "if True:"),
}


def main() -> int:
    backup = pathlib.Path(tempfile.mkdtemp()) / "lib_ci_status.py.orig"
    shutil.copy2(LIB, backup)
    original = LIB.read_text()
    survived = []
    try:
        for name, (old, new) in DEFECTS.items():
            assert original.count(old) == 1, f"注入箇所が 1 つに決まらない: {name}"
            LIB.write_text(original.replace(old, new))
            p = subprocess.run([sys.executable, "-m", "pytest", str(REPO / "tests" / "test_ci_approved_head.py"), "-q", "-x",
                                "-p", "no:cacheprovider"], capture_output=True, text=True, cwd=REPO,
                               env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"})
            red = p.returncode != 0
            print(f"{'RED  ' if red else 'GREEN'} {name}")
            if not red:
                survived.append(name)
    finally:
        shutil.copy2(backup, LIB)
    assert LIB.read_text() == original
    if survived:
        print("赤にならなかった欠陥:", survived)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
