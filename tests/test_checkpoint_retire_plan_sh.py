"""`plan.sh retire --checkpoint <dir|none>` (vNext 02 PR-G。設計 §5.5): 台帳の retire の `checkpoint_dir`・pull の `previous_attempt`。

隔離 plan.sh + 隔離 queue (`pull_execution_helpers.Box`)。checkpoint の dir は本物の `lib_checkpoint.take` で作る。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import lib_checkpoint as ck
import lib_history as hist
from execution_e3_helpers import MISSION, Box, run

TID = "t001"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


@pytest.fixture
def worktree(tmp_path):
    d = tmp_path / "wt"
    d.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.com")
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True, env=env)
    (d / "wip.txt").write_text("work in progress\n")
    return d


def checkpoint_for(box, xid, worktree):
    return ck.take(registry_dir=box.root / "registry", queue_dir=box.queue, mission=MISSION, task=TID,
                   execution_id=xid, worktree=str(worktree), seconds=30)


def retire(box, xid, *extra, agent="Ren"):
    return run(box, "retire", TID, "--agent", agent, "--execution", xid, "--mission", MISSION, *extra, agent="watchdog")


def retire_ledger(box):
    d = box.queue / "missions" / MISSION / "history" / TID
    files = sorted(p for p in d.iterdir() if "-retire-" in p.name) if d.is_dir() else []
    return [hist.parse(p.read_text(), source=p.name)[0] for p in files]


def started(box):
    p = box.pull("Ren")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["execution_id"]


def test_retire_with_a_checkpoint_records_it_in_the_ledger_and_pull_hands_it_on(box, worktree):
    xid = started(box)
    d = checkpoint_for(box, xid, worktree)
    p = retire(box, xid, "--checkpoint", d)
    assert p.returncode == 0, p.stderr
    (fields,) = retire_ledger(box)
    assert fields["checkpoint_dir"] == d and fields["end_code"] == "RETIRED"
    h = run(box, "history", TID, "--mission", MISSION)
    assert d in h.stdout
    q = box.pull("Kai")                                         # 次の試行
    assert q.returncode == 0, q.stderr
    prev = json.loads(q.stdout)["previous_attempt"]
    assert prev["execution_id"] == xid and prev["checkpoint_dir"] == d
    assert (pathlib.Path(prev["checkpoint_dir"]) / "untracked" / "wip.txt").read_text() == "work in progress\n"


def test_retire_with_none_or_without_the_option_leaves_the_field_empty(box, worktree):
    xid = started(box)
    assert retire(box, xid, "--checkpoint", "none").returncode == 0
    (fields,) = retire_ledger(box)
    assert fields.get("checkpoint_dir") in (None, "")
    box.reset()
    xid2 = started(box)
    assert retire(box, xid2).returncode == 0                    # 無ければ今まで通り
    assert [f.get("checkpoint_dir") or None for f in retire_ledger(box)] == [None, None]
    q = box.pull("Kai")
    assert json.loads(q.stdout)["previous_attempt"].get("checkpoint_dir") in (None, "")


def test_a_directory_of_another_attempt_is_refused_and_nothing_is_written(box, worktree):
    xid = started(box)
    other = "ex-" + "9" * 32
    foreign = checkpoint_for(box, other, worktree)              # 別の試行の dir (自分の dir ではない)
    before = box.snapshot()
    p = retire(box, xid, "--checkpoint", foreign)
    assert p.returncode == 3 and "execution_mismatch" in p.stderr
    assert box.snapshot() == before and box.card()["status"] == "in_progress"
    # 自分の試行の名前で別の試行の中身を置いた dir (meta の ID が dir 名と違う) も通らない
    liar = pathlib.Path(foreign).parent / xid
    os.rename(foreign, liar)
    p2 = retire(box, xid, "--checkpoint", str(liar))
    assert p2.returncode == 3 and "dir_name_mismatch" in p2.stderr and box.snapshot() == before


@pytest.mark.parametrize("value", ["/no/such/dir", "relative/dir", "NONE"])
def test_unreadable_missing_or_relative_directories_are_refused(box, value):
    xid = started(box)
    before = box.snapshot()
    p = retire(box, xid, "--checkpoint", value)
    assert p.returncode == 3 and "verify_failed" in p.stderr
    assert box.snapshot() == before


def test_an_empty_checkpoint_value_is_not_none(box):
    xid = started(box)
    before = box.snapshot()
    p = retire(box, xid, "--checkpoint", "")
    assert p.returncode == 1 and box.snapshot() == before


def test_a_resent_retire_is_idempotent_and_adds_no_second_ledger(box, worktree):
    xid = started(box)
    d = checkpoint_for(box, xid, worktree)
    assert retire(box, xid, "--checkpoint", d).returncode == 0
    snap = box.snapshot()
    again = retire(box, xid, "--checkpoint", d)
    assert again.returncode == 0 and "idempotent" in again.stdout
    assert box.snapshot() == snap and len(retire_ledger(box)) == 1


def test_the_checkpoint_does_not_loosen_the_preconditions(box, worktree):
    """checkpoint を付けても、別の試行の ID・worker 違いは今までどおり exit 3 で何も書かない。"""
    xid = started(box)
    d = checkpoint_for(box, xid, worktree)
    before = box.snapshot()
    stale = retire(box, "ex-" + "8" * 32, "--checkpoint", d)
    assert stale.returncode == 3, stale.stderr
    wrong_worker = retire(box, xid, "--checkpoint", d, agent="Somebody")
    assert wrong_worker.returncode == 3
    assert box.snapshot() == before
