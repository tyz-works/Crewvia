#!/usr/bin/env python3
"""赤の実証 (t001): (repo, PR) の鍵・fail-closed・gh の --repo を 1 つずつ壊し、tests/test_ci_pr_repo_scope.py (と ci-gate の
テスト) が赤になることを確かめる。

    python3 tests/red_proof_ci_pr_repo_scope.py

壊す前に対象を退避し、終わったら退避から戻す (git checkout は使わない)。置換元がちょうど 1 回でなければ BROKEN。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib_ci_status.py"
PLAN = REPO / "scripts" / "plan.sh"
T1 = REPO / "tests" / "test_ci_pr_repo_scope.py"
T2 = REPO / "tests" / "test_ci_gate.py::test_cli_gate_binds_records_by_pr_repo"

DEFECTS = [
    ("scope が pr_repo を見ない", LIB, "    if repo:\n        return f'repo:{repo}'\n    td =", "    if False:\n        return f'repo:{repo}'\n    td =", T1),
    ("scope が target_dir を見ない", LIB, "    return f'dir:{td}' if td else ''", "    return ''", T1),
    ("bad pr_repo を None にしない", LIB, "    except ValueError:\n        return None\n    if repo:", "    except ValueError:\n        return ''\n    if repo:", T1),
    ("pr view に --repo を付けない", LIB, "'headRefOid,statusCheckRollup,state', *repo_args]", "'headRefOid,statusCheckRollup,state']", T1),
    ("取得済みの束ねが番号だけ", LIB, "    key = (scope, pr)\n    if by_pr", "    key = ('', pr)\n    if by_pr", T1),
    ("closed を PR 番号だけで束ねる", LIB, "        k = (scope, pr_number)\n", "        k = ('', pr_number)\n", T1),
    ("台帳のキーに repo を入れない", LIB, "{pr_tag(event['pr'], event.get('scope') or '')}", "pr{event['pr']}", T1),
    ("bad pr_repo の card も取得する", LIB, "    if scope is None:\n        # repo が決められない", "    if False:\n        # repo が決められない", T1),
    ("bad pr_repo の card も通知する", LIB, "        if scope is None:\n            continue        # repo が決められない card は何も通知しない", "        if False:\n            continue", T1),
    ("head_of に scope を渡さない", LIB, "    return head_of(pr) if not scope else head_of(pr, scope)", "    return head_of(pr)", T1),
    ("ci-gate が repo で絞らない", PLAN, "or _CI_STATUS.pr_scope(meta) != gate_scope:", "or False:", T2),
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
        for name, path, old, new, test in DEFECTS:
            text = originals[path]
            if text.count(old) != 1:
                print(f"BROKEN {name}")
                broken.append(name)
                continue
            path.write_text(text.replace(old, new))
            try:
                p = subprocess.run([sys.executable, "-m", "pytest", str(test), "-q", "-x", "-p", "no:cacheprovider"],
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
