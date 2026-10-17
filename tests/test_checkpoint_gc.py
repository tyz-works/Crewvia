"""checkpoint_gc.py (vNext 02 PR-G。設計 §5.1・§7 判断 7): 既定 dry-run・`--apply` は削除でなく隔離・`--restore`・
対象は「task が終端で、保存から 30 日以上」だけ。読めない・判定できないものは残す。
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import checkpoint_gc as gc  # noqa: E402
import lib_checkpoint as ck  # noqa: E402

MISSION = "m-gc"
XID = "ex-" + "a" * 32
DAY = 86400


def x(n):
    return "ex-" + f"{n:032x}"


CARD = """---
id: {tid}
title: gc fixture
skills: [code]
priority: high
status: {status}
blocked_by: []
target_dir: null
worker: null
started_at: null
completed_at: null
---

## Description
fixture

## Result
"""


class Env:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.registry = self.root / "registry"
        self.queue = self.root / "queue"
        self.wt = self.root / "wt"
        self.wt.mkdir(parents=True)
        e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        e.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.com")
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "i"]):
            subprocess.run(["git", "-C", str(self.wt), *args], check=True, capture_output=True, env=e)
        (self.wt / "w.txt").write_text("work\n")

    def card(self, tid, status, mission=MISSION, base="missions"):
        d = self.queue / base / mission / "tasks"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.md").write_text(CARD.format(tid=tid, status=status))

    def checkpoint(self, tid="t001", xid=XID, mission=MISSION):
        return pathlib.Path(ck.take(registry_dir=self.registry, queue_dir=self.queue, mission=mission, task=tid,
                                    execution_id=xid, worktree=str(self.wt), seconds=30))

    def tree(self):
        out = {}
        for p in sorted(self.registry.rglob("*")):
            out[p.relative_to(self.registry).as_posix()] = p.read_bytes() if p.is_file() else None
        return out

    def scan(self, days_ahead=31, **kw):
        return gc.scan(str(self.registry), str(self.queue), now=time.time() + days_ahead * DAY, **kw)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def by_reason(verdicts):
    return {(v.task, v.execution_id): (v.action, v.reason) for v in verdicts}


def test_old_checkpoint_of_a_terminal_task_is_a_candidate_and_dry_run_changes_nothing(env):
    env.card("t001", "done")
    d = env.checkpoint()
    before = env.tree()
    verdicts, err = env.scan()
    assert err is None and by_reason(verdicts) == {("t001", XID): (gc.REMOVE, gc.R_OLD_TERMINAL)}
    assert env.tree() == before and d.exists()
    out = subprocess.run([sys.executable, str(REPO / "scripts" / "checkpoint_gc.py"), "--registry", str(env.registry),
                          "--queue", str(env.queue), "--days", "0"], capture_output=True, text=True)
    assert out.returncode == 0 and "would be quarantined" in out.stdout and "dry-run" in out.stdout
    assert env.tree() == before


@pytest.mark.parametrize("status", ["done", "verified", "skipped"])
def test_every_terminal_status_counts(env, status):
    env.card("t001", status)
    env.checkpoint()
    assert env.scan()[0][0].action == gc.REMOVE


@pytest.mark.parametrize("status", ["pending", "in_progress", "failed", "needs_director", "blocked", "verification_failed",
                                    "ready_for_verification"])
def test_a_task_that_is_not_terminal_keeps_its_checkpoint_however_old(env, status):
    env.card("t001", status)
    env.checkpoint()
    (v,) = env.scan(days_ahead=3650)[0]
    assert (v.action, v.reason) == (gc.KEEP, gc.R_NOT_TERMINAL)


def test_retention_boundary_is_thirty_days(env):
    env.card("t001", "done")
    d = env.checkpoint()
    taken = ck.verify(d)["taken_at"]
    base = gc._taken_at_epoch(taken)
    young, _ = gc.scan(str(env.registry), str(env.queue), now=base + 30 * DAY - 1)
    exact, _ = gc.scan(str(env.registry), str(env.queue), now=base + 30 * DAY)
    assert (young[0].action, young[0].reason) == (gc.KEEP, gc.R_TOO_NEW)
    assert exact[0].action == gc.REMOVE
    assert gc.DEFAULT_DAYS == 30


def test_a_card_that_is_missing_or_unreadable_or_the_wrong_task_keeps_the_checkpoint(env):
    env.checkpoint("t001", x(1))                                      # card 無し
    env.card("t002", "done")
    (env.queue / "missions" / MISSION / "tasks" / "t002.md").write_text("---\nstatus: [unclosed\n---\n")   # 壊れた card
    env.checkpoint("t002", x(2))
    env.card("t003", "done")
    (env.queue / "missions" / MISSION / "tasks" / "t003.md").chmod(0)                                       # 読めない
    env.checkpoint("t003", x(3))
    try:
        got = by_reason(env.scan()[0])
    finally:
        (env.queue / "missions" / MISSION / "tasks" / "t003.md").chmod(0o644)
    assert got[("t001", x(1))] == (gc.KEEP, gc.R_CARD_MISSING)
    # 壊れた frontmatter は、終端と読めない status になる (読み口の寛容さに依らず、終端と確定できなければ残す)
    assert got[("t002", x(2))][0] == gc.KEEP and got[("t002", x(2))][1] in (gc.R_CARD_UNREADABLE, gc.R_NOT_TERMINAL)
    if os.geteuid() != 0:
        assert got[("t003", x(3))] == (gc.KEEP, gc.R_CARD_UNREADABLE)


def test_an_archived_missions_terminal_card_counts(env):
    env.card("t001", "done", base="archive")
    env.checkpoint()
    assert env.scan()[0][0].action == gc.REMOVE


def test_unverifiable_partial_and_oddly_placed_things_are_kept(env):
    env.card("t001", "done")
    good = env.checkpoint("t001", x(1))
    bad_meta = env.checkpoint("t001", x(2))
    (bad_meta / "meta.json").write_text("{not json")
    no_meta = env.checkpoint("t001", x(3))
    (no_meta / "meta.json").unlink()                                   # テスト fixture の細工 (gc の外)
    wrong_id = env.checkpoint("t001", x(4))
    meta = json.loads((wrong_id / "meta.json").read_text())
    meta["execution_id"] = x(99)
    (wrong_id / "meta.json").write_text(json.dumps(meta))
    bad_date = env.checkpoint("t001", x(5))
    meta = json.loads((bad_date / "meta.json").read_text())
    meta["taken_at"] = "yesterday-ish"
    (bad_date / "meta.json").write_text(json.dumps(meta))
    other_task = env.checkpoint("t001", x(6))
    meta = json.loads((other_task / "meta.json").read_text())
    meta["task"] = "t777"
    (other_task / "meta.json").write_text(json.dumps(meta))
    root = env.registry / "checkpoints" / MISSION / "t001"
    (root / (x(7) + ck.PARTIAL_SUFFIX)).mkdir()
    (root / "stray-file").write_text("x")
    (env.registry / "checkpoints" / "weird name!").mkdir()
    os.symlink(good, root / x(8))
    got = {pathlib.Path(v.path).name: (v.action, v.reason) for v in env.scan()[0]}
    assert got[x(1)] == (gc.REMOVE, gc.R_OLD_TERMINAL)
    for n in (2, 3, 4, 5, 6):
        assert got[x(n)][0] == gc.KEEP, (n, got[x(n)])
    assert got[x(5)][1] == gc.R_TAKEN_AT
    assert got[x(7) + ck.PARTIAL_SUFFIX] == (gc.KEEP, gc.R_PARTIAL)
    assert got["stray-file"][0] == gc.KEEP and got["weird name!"][0] == gc.KEEP and got[x(8)][0] == gc.KEEP


def test_apply_quarantines_instead_of_deleting_and_restore_puts_it_back(env):
    env.card("t001", "done")
    d = env.checkpoint()
    content = {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}
    verdicts, _ = env.scan()
    applied = gc.apply_quarantine(str(env.registry), str(env.queue), verdicts, now=time.time() + 31 * DAY)
    assert applied[0]["quarantined"] is True
    q = pathlib.Path(applied[0]["quarantine"])
    assert not d.exists() and q.is_dir() and q.relative_to(env.registry / "checkpoints" / ".quarantine").parts[1:] == (
        MISSION, "t001", XID)
    assert {p.relative_to(q).as_posix(): p.read_bytes() for p in q.rglob("*") if p.is_file()} == content    # 1 バイトも失っていない
    # 隔離済みは次の走査で対象にならない (.quarantine は走査しない)
    assert env.scan()[0] == []
    entries, err = gc.find_quarantine_entries(str(env.registry))
    assert err is None and entries == [(str(q), str(d))]
    ok, msg = gc.restore(str(env.registry), str(q))
    assert ok and msg == str(d) and d.is_dir() and not q.exists()
    assert {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()} == content
    # 元のパスを渡しても戻せる / 既にある場所には戻さない
    gc.apply_quarantine(str(env.registry), str(env.queue), env.scan()[0], now=time.time() + 31 * DAY)
    ok2, _ = gc.restore(str(env.registry), str(d))
    assert ok2 and d.is_dir()
    gc.apply_quarantine(str(env.registry), str(env.queue), env.scan()[0], now=time.time() + 31 * DAY)
    env.checkpoint()                                                   # 同じ場所に別の checkpoint が出来た
    ok3, msg3 = gc.restore(str(env.registry), str(d))
    assert not ok3 and "destination exists" in msg3
    assert gc.restore(str(env.registry), str(env.root / "nowhere"))[0] is False


def test_apply_rejudges_right_before_moving(env):
    env.card("t001", "done")
    d = env.checkpoint()
    verdicts, _ = env.scan()
    env.card("t001", "pending")                                        # 判定の後で task が開き直された (reopen)
    applied = gc.apply_quarantine(str(env.registry), str(env.queue), verdicts, now=time.time() + 31 * DAY)
    assert applied == [{"path": str(d), "quarantined": False, "reason": f"rejudged:{gc.R_NOT_TERMINAL}"}]
    assert d.is_dir()


def test_cli_apply_list_restore_and_json(env):
    env.card("t001", "done")
    d = env.checkpoint()
    base = [sys.executable, str(REPO / "scripts" / "checkpoint_gc.py"), "--registry", str(env.registry), "--queue", str(env.queue)]
    j = subprocess.run([*base, "--days", "0", "--json"], capture_output=True, text=True)
    assert j.returncode == 0 and json.loads(j.stdout)["verdicts"][0]["action"] == "remove" and d.is_dir()
    a = subprocess.run([*base, "--days", "0", "--apply"], capture_output=True, text=True)
    assert a.returncode == 0 and "1 of 1 checkpoint(s) quarantined" in a.stdout and not d.exists()
    lq = subprocess.run([*base, "--list-quarantine"], capture_output=True, text=True)
    q, orig = lq.stdout.strip().split("\t")
    assert orig == str(d) and pathlib.Path(q).is_dir()
    r = subprocess.run([*base, "--restore", q], capture_output=True, text=True)
    assert r.returncode == 0 and d.is_dir()
    r2 = subprocess.run([*base, "--restore", q], capture_output=True, text=True)
    assert r2.returncode == 1


def test_no_checkpoints_directory_is_not_an_error(env):
    assert gc.scan(str(env.registry), str(env.queue)) == ([], None)


def test_an_unreadable_root_judges_nothing(env):
    env.card("t001", "done")
    d = env.checkpoint()
    root = env.registry / "checkpoints"
    if os.geteuid() == 0:
        pytest.skip("root は 0o000 を読める")
    root.chmod(0)
    try:
        verdicts, err = gc.scan(str(env.registry), str(env.queue), now=time.time() + 31 * DAY)
    finally:
        root.chmod(0o700)
    assert verdicts == [] and err == "unreadable" and d.exists()


def test_gc_never_calls_a_deleting_function():
    """`worktree_gc` と同じ作法: 削除を呼ばない (隔離は rename だけ)。実際の削除はこのツールの外。"""
    tree = ast.parse((REPO / "scripts" / "checkpoint_gc.py").read_text())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    forbidden = {"rmtree", "remove", "unlink", "rmdir", "removedirs", "system", "popen", "run", "Popen", "check_call",
                 "check_output", "call"}
    assert not (called & forbidden), sorted(called & forbidden)
    assert "move_between" in called            # 移動は lib_checkpoint.move_between (dir fd 基点の rename) の 1 か所
