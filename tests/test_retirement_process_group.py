#!/usr/bin/env python3
"""退役が process group ごと止める (knowledge/retirement-process-group.md)。

2026-11-09 の実例: 窓の bash が SIGTERM で死に、SIGTERM を無視する claude (同じ group の子) が
2 時間以上生き残った。退役は「記録した pane_pid が消えた = 終了」としたので SIGKILL の段に進まなかった。

    python3 -m pytest tests/test_retirement_process_group.py -v
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(TESTS))

import lib_retirement as lr  # noqa: E402
# 隔離 sandbox (plan.sh + queue + registry) と偽 mux は既存の退役テストのものをそのまま使う
from test_retirement import (  # noqa: E402,F401
    AGENT, SLUG, TASK_ID, WINDOW, FakeMux, make_executor, sandbox,
)

#: SIGTERM を無視する「固まった claude」役の python。子 (MCP / Bash tool 役) も SIGTERM を無視する。
STUCK = (
    "import os, signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "child = subprocess.Popen([sys.executable, '-c',\n"
    "    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1000)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(1000)\n"
)

#: bash が group leader で、SIGTERM を無視する子 (claude 役) を持つ形 (exec が無かった頃の窓)。
SHELL_THEN_STUCK = (
    "sh -c 'trap \"\" TERM; echo $$ > \"$1\"; exec sleep 1000' sh \"$1\" & wait"
)


def _alive(pid: int) -> bool:
    return lr.process_alive(pid)


def _wait_file(path: Path, timeout: float = 10.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.02)
    raise AssertionError(f"{path} was not written")


@pytest.fixture
def spawned(tmp_path):
    procs = []

    def start(argv):
        p = subprocess.Popen(argv, start_new_session=True)   # 窓の shell と同じ: 自分が session / group leader
        procs.append(p)
        return p

    yield start
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass


def _prog(sandbox):
    return lr.read_json(lr.progress_path(sandbox.registry, AGENT))


def _drive(ex, sandbox, cycles=20):
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    for _ in range(cycles):
        ex.process_all()
        if not ex.has_marker(AGENT):
            return True
    return False


# ---------------------------------------------------------------------------
# B: group を止める
# ---------------------------------------------------------------------------

def test_shell_dies_on_sigterm_but_stuck_child_is_killed_with_the_group(sandbox, spawned, tmp_path):
    """bash は SIGTERM で死ぬ・子は無視する。group が空になって初めて terminated になる。"""
    child_file = tmp_path / "child.pid"
    shell = spawned(["bash", "-c", SHELL_THEN_STUCK, "bash", str(child_file)])
    child = _wait_file(child_file)
    sandbox.record_identity(WINDOW, shell.pid)
    mux = FakeMux({WINDOW: shell.pid})

    ex = make_executor(sandbox, mux)
    settled = _drive(ex, sandbox)

    assert settled, "退役が終わらない"
    assert not _alive(child), "SIGTERM を無視する子が生き残った (2026-11-09 の再現)"
    assert lr.proc_group_members(shell.pid) == []
    assert any("SIGKILL → process group" in m for m in sandbox.logs), sandbox.logs


def test_exec_form_leader_and_its_child_are_both_killed(sandbox, spawned, tmp_path):
    """exec 版: pane_pid 自身が SIGTERM を無視する claude。子も無視する。"""
    child_file = tmp_path / "child.pid"
    leader = spawned([sys.executable, "-c", STUCK, str(child_file)])
    child = _wait_file(child_file)
    sandbox.record_identity(WINDOW, leader.pid)
    mux = FakeMux({WINDOW: leader.pid})

    assert _drive(make_executor(sandbox, mux), sandbox), "退役が終わらない"
    leader.wait(timeout=5)
    assert not _alive(child)
    assert lr.proc_group_members(leader.pid) == []


def test_terminated_is_not_declared_while_the_group_is_alive(sandbox, spawned, tmp_path):
    """SIGKILL が group に届かない間は、shell が消えていても terminated にしない (task を配り直さない)。"""
    child_file = tmp_path / "child.pid"
    shell = spawned(["bash", "-c", SHELL_THEN_STUCK, "bash", str(child_file)])
    child = _wait_file(child_file)
    sandbox.record_identity(WINDOW, shell.pid)
    mux = FakeMux({WINDOW: shell.pid})

    def drop_sigkill(pgid, sig):
        if sig == signal.SIGKILL:
            return False                     # 届かなかった
        return lr._default_kill_group(pgid, sig)

    ex = make_executor(sandbox, mux, kill_group=drop_sigkill)
    assert not _drive(ex, sandbox, cycles=8), "group が生きているのに終わったことにした"
    assert _alive(child)
    assert _prog(sandbox)["phase"] == lr.PHASE_SIGTERM_SENT
    assert task_still_held(sandbox)

    # 別の executor (= デーモンの再起動後) が実際に届けば終わる
    assert _drive_resume(make_executor(sandbox, mux), 20)
    assert not _alive(child)


def test_shell_dying_right_after_the_orphan_check_does_not_discard_a_live_group(sandbox, spawned, tmp_path):
    """_orphaned_group が shell 生存と読んだ直後に shell が死ぬ窓: group が残るのに discard してはいけない (t007)。

    窓は決定的に開く: 最初の観測 (shell 生存) を「死ぬ前」の答えに差し替え、その直後の _guard は
    実際に死んだ shell を見る。
    """
    child_file = tmp_path / "child.pid"
    shell = spawned(["bash", "-c", SHELL_THEN_STUCK, "bash", str(child_file)])
    child = _wait_file(child_file)
    sandbox.record_identity(WINDOW, shell.pid)
    mux = FakeMux({WINDOW: shell.pid})
    ex = make_executor(sandbox, mux)

    real = ex._orphaned_group
    calls = {"n": 0}

    def racy(prog):
        calls["n"] += 1
        if calls["n"] == 1 and not lr.process_alive(shell.pid):
            return None                      # 観測の瞬間は shell が生きていた
        return real(prog)

    ex._orphaned_group = racy
    assert _drive(ex, sandbox), "退役が終わらない"
    assert not _alive(child), "group (固まった claude) が残ったまま discard された"
    assert not any("NOT sending SIGKILL" in m for m in sandbox.logs), sandbox.logs


def task_still_held(sandbox) -> bool:
    return sandbox.task_status() == "in_progress" and sandbox.task_worker() == AGENT


def _drive_resume(ex, cycles):
    ex.recover()
    for _ in range(cycles):
        ex.process_all()
        if not ex.has_marker(AGENT):
            return True
    return False


def test_unobservable_group_is_not_proof_of_an_exit(sandbox, spawned, tmp_path):
    """/proc が読めず group を数えられないときは終了の証明にしない。"""
    shell = spawned(["sleep", "1000"])
    sandbox.record_identity(WINDOW, shell.pid)
    mux = FakeMux({WINDOW: shell.pid})
    ex = make_executor(sandbox, mux, kill_group=lambda pgid, sig: True,
                       group_members=lambda pgid: None)
    assert not _drive(ex, sandbox, cycles=6)
    os.killpg(shell.pid, signal.SIGKILL)
    shell.wait(timeout=5)
    for _ in range(4):
        ex.process_all()
    assert ex.has_marker(AGENT), "観測できないのに終了と見なした"


# ---------------------------------------------------------------------------
# 安全側: 証明できないときは今までの pane_pid の挙動
# ---------------------------------------------------------------------------

def test_non_leader_pane_pid_keeps_the_pid_only_behaviour(sandbox):
    p = subprocess.Popen(["sleep", "30"])      # 親 (pytest) の group に居る = leader ではない
    try:
        assert lr.proc_group_leader(p.pid) is None
    finally:
        p.kill()
        p.wait()


def test_never_targets_our_own_group_or_pid_1():
    assert lr.proc_group_leader(os.getpid()) is None
    assert lr.proc_group_leader(1) is None
    assert lr._default_kill_group(os.getpgrp(), signal.SIGKILL) is False
    assert lr._default_kill_group(1, signal.SIGKILL) is False


def test_fake_pids_do_not_take_the_group_path(sandbox):
    """既存の (偽 pid を使う) 退役テストの挙動が変わらない: leader と証明できない pid は group を使わない。"""
    assert lr.proc_group_leader(2 ** 22 + 12345) is None
    assert lr.proc_group_members(2 ** 22 + 12345) == []


# ---------------------------------------------------------------------------
# A: exec claude
# ---------------------------------------------------------------------------

def test_start_sh_launch_cmd_execs_claude():
    """LAUNCH_CMD が `cd ... && exec claude` でなくなったら赤 (pane_pid が claude 本体であることが前提)。"""
    text = (REPO_ROOT / "scripts" / "start.sh").read_text()
    lines = [l for l in text.splitlines() if l.lstrip().startswith('LAUNCH_CMD="')]
    assert len(lines) == 1, lines
    assert re.search(r'cd \$\(_shq "\$WORK_DIR"\) && exec claude\$\{MODEL_CLI_ARG\}', lines[0]), lines[0]


def test_exec_makes_the_shell_pid_the_claude_pid(tmp_path):
    """exec 版と非 exec 版で、窓の shell の pid が何になるかの実測 (記録される pane_pid の意味)。"""
    out = tmp_path / "pid"
    stub = tmp_path / "claude"
    stub.write_text(f'#!/bin/sh\necho $$ > {out}\n')
    stub.chmod(0o755)
    for prefix, same in (("exec ", True), ("", False)):
        out.unlink(missing_ok=True)
        p = subprocess.Popen(["sh", "-c", f"cd {tmp_path} && {prefix}{stub}"], start_new_session=True)
        p.wait(timeout=10)
        assert (int(out.read_text()) == p.pid) is same
