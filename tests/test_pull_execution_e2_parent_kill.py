"""01c E2 fix 2 巡目 (t031 / PR #271 Codex P2): 準備ロックを worktree を作る subprocess にも持たせる。

準備ロック (execution.md §6.1) は pull の python が持つ flock だった。記述子が準備の subprocess に渡っていなければ、
worktree の作成中に **python だけ**が SIGKILL されたとき (helper / git の子孫は生き残る)、kernel がロックを外す。
再試行の pull は同じ予約を再開して**別の helper を同時に走らせ**、作りかけの worktree を見るか `WORKSPACE_CREATE_FAILED` で失敗しうる。

既存の crash テストは**プロセスグループごと** kill するのでこの形を見逃す。ここは **親 (plan.sh の python) の pid だけ**を SIGKILL する
(helper stub が `$PPID` を `hold/<task>.parent` に残す)。子孫 (stub の bash) は止めたまま生かしておき、その間に再試行を打つ。

修正: `PrepareLock.fileno()` を `subprocess.run(..., pass_fds=...)` で helper と `git rev-parse` に渡す
(flock は開いたファイル記述に付くので、子孫が持っている間は親が死んでも外れない)。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

import pytest

import pull_execution_helpers as h
from pull_execution_helpers import Box, MISSION


def alive(pid: int) -> bool:
    """生きているか (ゾンビは死んでいる扱い。kill された子は親が reap するまでゾンビで `kill(pid, 0)` が通る)。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False


def wait_dead(pid: int, timeout=20.0):
    deadline = time.time() + timeout
    while alive(pid):
        assert time.time() < deadline, f"pid {pid} が {timeout}s 以内に終わらなかった"
        time.sleep(0.02)


def invocations(box: Box, tid="t001"):
    p = box.root / "hold" / f"{tid}.invocations"
    return [int(x) for x in p.read_text().split()] if p.exists() else []


def one_round(box: Box):
    """worktree 作成中に python だけを kill → 子孫が生きている間に再試行 → 子孫が終わった後に再試行。"""
    box.hold()
    first = box.popen("pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001", agent="Ren")
    helper_pid = None
    try:
        box.reached()
        parent = int((box.root / "hold" / "t001.parent").read_text())
        helper_pids = invocations(box)
        assert len(helper_pids) == 1
        helper_pid = helper_pids[0]
        assert alive(parent) and alive(helper_pid) and parent != helper_pid
        xid = box.card()["current_execution_id"]

        os.kill(parent, signal.SIGKILL)                           # **python だけ**。プロセスグループは kill しない
        wait_dead(parent)
        assert alive(helper_pid), "helper (子孫) まで死んだ: 親だけを kill できていない"

        # 子孫が生きている間の再試行: 同時に 2 本目の helper を走らせてはいけない
        second = box.popen("pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001",
                           agent="Ren")
        timed_out = False
        try:
            out, err = second.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            timed_out = True
        if timed_out:
            h.kill_group(second)
            pytest.fail(f"2 本目の pull が 6 秒たっても終わらない (helper が同時に走っている: {invocations(box)})")
        assert second.returncode == 1 and out.strip() == "", (second.returncode, out, err)
        assert h.last_error_code(err) == "TASK_ALREADY_RESERVED", err
        assert invocations(box) == [helper_pid], f"helper が同時に 2 本走った: {invocations(box)}"
        assert box.card()["execution_status"] == "reserved" and box.card()["current_execution_id"] == xid

        # 子孫が終わった後は、同じ予約を再開できる (ロックは子孫と一緒に外れる)
        box.go()
        wait_dead(helper_pid)
        third = box.pull("Ren")
        assert third.returncode == 0, (third.stdout, third.stderr)
        res = json.loads(third.stdout)
        assert res["execution_id"] == xid and res["attempt"] == 1
        assert box.card()["execution_status"] == "running"
    finally:
        h.kill_group(first) if first.poll() is None else None
        if helper_pid is not None and alive(helper_pid):
            box.go()
            wait_dead(helper_pid)


def test_killing_only_the_parent_python_during_worktree_creation_keeps_the_lock_while_the_helper_lives(tmp_path):
    """20 回反復 (毎回使い捨ての clone)。修正前は 2 本目の helper が同時に走って赤。"""
    seed = Box(tmp_path / "seed")
    for i in range(20):
        one_round(seed.clone(tmp_path / f"r{i:02d}"))


def test_the_helper_inherits_the_lock_descriptor_and_a_plain_kill_of_the_group_still_releases_it(tmp_path):
    """陽性対照: プロセスグループごと kill (今までのテストの形) では子孫も死ぬのでロックは外れ、再 pull が再開できる。
    記述子を渡す修正が、グループごと kill した後の再開を妨げていないこと。"""
    box = Box(tmp_path / "root")
    box.hold()
    first = box.popen("pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001", agent="Ren")
    box.reached()
    xid = box.card()["current_execution_id"]
    helper_pid = invocations(box)[0]
    h.kill_group(first)
    wait_dead(helper_pid)
    box.unhold()
    res = json.loads(box.pull("Ren").stdout)
    assert res["execution_id"] == xid and res["attempt"] == 1
