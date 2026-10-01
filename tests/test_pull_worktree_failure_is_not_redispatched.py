"""GIT-05: worktree を作れず needs_director になった task は、同じ Worker に配り直され続けない。

pending に戻す設計だと、dispatcher が同じ task を同じ Worker に送り直し、決定的な失敗が無限に続く
(knowledge/git-policy.md §1.2 案 A を捨てた理由)。本物の plan.sh (pull の失敗) と本物の dispatcher の
1 サイクルをつないで、失敗の前は割り当てが送られ (対照)、後は送られないことを確かめる。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from fixture_tree import copy_plan_tree  # noqa: E402
from test_dispatcher_retirement_exclusion import (  # noqa: E402
    AGENT, SLUG, WINDOW, FakeMux, _build_repo,
)
from test_needs_director_releases_assignment import _one_cycle, _assign_messages  # noqa: E402


def test_failed_pull_task_is_not_sent_to_the_same_worker_again(tmp_path):
    root = _build_repo(tmp_path)
    copy_plan_tree(root)
    (root / "scripts" / "git-helpers.sh").unlink()   # N2: worktree を作れない
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8",
           "CREWVIA_QUEUE": str(root / "queue"), "CREWVIA_REPO_ROOT": str(root),
           "CREWVIA_TASKVIA": "disabled", "CREWVIA_TASK_GRAPH": "0", "AGENT_NAME": AGENT}

    # 対照: pending の task は idle の Worker に送られる (harness が割り当てを観測できる)
    mux, _ = _one_cycle(root, FakeMux([WINDOW]))
    assert _assign_messages(mux), "対照が成立しない: pending の task が送られていない"

    p = subprocess.run([str(root / "scripts" / "plan.sh"), "pull", "--agent", AGENT, "--skills", "code",
                        "--task", "t001", "--mission", SLUG], env=env, capture_output=True, text=True)
    assert p.returncode == 1 and p.stdout.strip() == "", (p.stdout, p.stderr)
    card = (root / "queue" / "missions" / SLUG / "tasks" / "t001.md").read_text()
    assert "status: needs_director" in card

    for _ in range(3):
        mux, _ = _one_cycle(root, FakeMux([WINDOW]))
        assert not _assign_messages(mux), f"失敗した task が配り直された: {mux.sent}"
