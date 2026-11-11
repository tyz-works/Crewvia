#!/usr/bin/env python3
"""赤の実証: gate_verdict の条件を 1 つずつ壊し、tests/test_ci_gate.py が赤になることを確かめる。

    python3 tests/red_proof_ci_gate.py

壊す前に lib を退避 (cp) し、終わったら退避から戻す (git checkout で戻さない = 未 commit の修正を消さない)。
純粋関数のテストだけを流す (-k 'not cli')。全部の欠陥が赤になれば exit 0。
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
    "head の照合を外す": ("    if record['head'] != pr_head:\n        return False, 'head_mismatch'\n", ""),
    "古さの検査を外す": ("    if age < 0 or age > max_age:", "    if False:"),
    "未来の時刻を新しいと認める": ("    if age < 0 or age > max_age:", "    if age > max_age:"),
    "全体の state を見ない": ("    if state != 'passed':               # running / failed / none\n        return False, state\n", ""),
    "必須 check を見ない": ("    if not required or any(c['state'] != 'passed' for c in required):", "    if False:"),
    "必須 check が 0 件を通す": ("    if not required or any(", "    if any("),
    "neutral の ci-ok を通す": ("any(c['state'] != 'passed' for c in required)", "any(c['state'] in ('failed', 'running') for c in required)"),
    "pr_head を検査しない": ("    if not isinstance(pr_head, str) or not _SHA_RE.fullmatch(pr_head):\n        return False, 'unknown:no_pr_head'\n", ""),
    "壊れた記録を通す": ("    if record is None or record_problem(record) is not None:", "    if record is None:"),
    "unknown を通す": ("    if state == 'unknown':", "    if False:"),
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
            p = subprocess.run([sys.executable, "-m", "pytest", str(REPO / "tests" / "test_ci_gate.py"), "-q", "-x",
                                "-k", "not cli", "-p", "no:cacheprovider"], capture_output=True, text=True, cwd=REPO)
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
