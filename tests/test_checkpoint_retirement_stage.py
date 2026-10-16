"""退役の `checkpoint` 段 (vNext 02 PR-G。設計 §5.4): 取れたら `--checkpoint <dir>`、取れなくても退役は止まらず `none`。

`test_retirement.py` の sandbox / FakeMux を使う (本物の Worker プロセス 1 つ・偽の plan.sh 呼び出し)。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lib_checkpoint
import lib_retirement
from test_retirement import (  # noqa: F401  (fixture と道具)
    AGENT, SANDBOX_XID, SLUG, TASK_ID, WINDOW, FakeMux, _wait_gone, make_executor, sandbox,
)


def drive_to_cleanup(sandbox, ex, mux, pane_pid):
    """request → 殺す → terminated → 後始末 の 1 周 (退役の段をそのまま通す)。"""
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    ex.process_all()
    os.kill(pane_pid, signal.SIGKILL)
    _wait_gone(pane_pid)
    ex.process_all()
    ex.process_all()


@pytest.fixture
def worker(sandbox):
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    return pane_pid, FakeMux({WINDOW: pane_pid})


def checkpoint_arg(argv):
    assert "--checkpoint" in argv, argv
    return argv[argv.index("--checkpoint") + 1]


def test_a_saved_checkpoint_is_handed_to_plan_sh_retire(sandbox, worker):
    pane_pid, mux = worker
    calls, taken = [], []

    def take(**kw):
        taken.append(kw)
        return "/saved/checkpoint/dir"

    ex = make_executor(sandbox, mux, run_command=lambda argv, env: (calls.append(list(argv)), (0, ""))[1],
                       take_checkpoint=take)
    drive_to_cleanup(sandbox, ex, mux, pane_pid)
    assert len(calls) == 1 and checkpoint_arg(calls[0]) == "/saved/checkpoint/dir"
    assert calls[0].index("--checkpoint") > calls[0].index("--execution")
    (kw,) = taken
    assert (kw["mission"], kw["task"], kw["execution_id"], kw["reason"]) == (SLUG, TASK_ID, SANDBOX_XID, "timeout")
    assert kw["seconds"] == 20 and Path(kw["registry_dir"]) == sandbox.registry
    assert not ex.has_marker(AGENT)                              # 退役は最後まで進んだ


@pytest.mark.parametrize("exc", [
    lib_checkpoint.CheckpointError(lib_checkpoint.E_GIT, "exit 128"),
    lib_checkpoint.CheckpointError(lib_checkpoint.E_DEADLINE),
    RuntimeError("something unexpected"),
    OSError("disk full"),
], ids=["git_failed", "deadline", "unexpected", "oserror"])
def test_a_failed_checkpoint_does_not_stop_the_retirement(sandbox, worker, exc):
    pane_pid, mux = worker
    calls = []

    def take(**kw):
        raise exc

    ex = make_executor(sandbox, mux, run_command=lambda argv, env: (calls.append(list(argv)), (0, ""))[1],
                       take_checkpoint=take)
    drive_to_cleanup(sandbox, ex, mux, pane_pid)
    assert len(calls) == 1 and checkpoint_arg(calls[0]) == "none"
    assert not ex.has_marker(AGENT), "checkpoint の失敗が退役を止めた"
    notices = [n for n in sandbox.notes if "[checkpoint]" in n]
    assert len(notices) == 1 and TASK_ID in notices[0] and SLUG in notices[0]
    assert "something unexpected" not in notices[0] and "disk full" not in notices[0]      # 例外の文面は出さない


def test_nothing_to_save_is_quiet(sandbox, worker):
    pane_pid, mux = worker
    calls = []

    def take(**kw):
        raise lib_checkpoint.NothingToSave("record_absent")

    ex = make_executor(sandbox, mux, run_command=lambda argv, env: (calls.append(list(argv)), (0, ""))[1],
                       take_checkpoint=take)
    drive_to_cleanup(sandbox, ex, mux, pane_pid)
    assert checkpoint_arg(calls[0]) == "none"
    assert not [n for n in sandbox.notes if "[checkpoint]" in n], "保存するものが無いだけなのに通知した"


def test_zero_seconds_disables_the_stage(sandbox, worker):
    pane_pid, mux = worker
    calls = []

    def take(**kw):
        raise AssertionError("checkpoint_seconds=0 なのに取ろうとした")

    ex = make_executor(sandbox, mux, run_command=lambda argv, env: (calls.append(list(argv)), (0, ""))[1],
                       take_checkpoint=take, checkpoint_seconds=0)
    drive_to_cleanup(sandbox, ex, mux, pane_pid)
    assert checkpoint_arg(calls[0]) == "none" and not [n for n in sandbox.notes if "[checkpoint]" in n]


def test_a_busy_queue_retry_reuses_the_checkpoint_and_does_not_take_it_again(sandbox, worker):
    pane_pid, mux = worker
    calls, taken = [], []
    answers = iter([(lib_retirement.PLAN_LOCK_BUSY, "busy"), (0, "")])

    def run(argv, env):
        calls.append(list(argv))
        return next(answers)

    def take(**kw):
        taken.append(kw)
        return "/saved/dir"

    ex = make_executor(sandbox, mux, run_command=run, take_checkpoint=take)
    drive_to_cleanup(sandbox, ex, mux, pane_pid)
    ex.process_all()
    assert len(calls) == 2 and len(taken) == 1
    assert [checkpoint_arg(c) for c in calls] == ["/saved/dir", "/saved/dir"]


def test_a_failed_checkpoint_is_not_retried_every_cycle_while_the_queue_is_busy(sandbox, worker):
    pane_pid, mux = worker
    taken = []
    answers = iter([(lib_retirement.PLAN_LOCK_BUSY, "busy")] * 3 + [(0, "")])

    def take(**kw):
        taken.append(1)
        raise lib_checkpoint.CheckpointError(lib_checkpoint.E_GIT)

    ex = make_executor(sandbox, mux, run_command=lambda argv, env: next(answers), take_checkpoint=take)
    drive_to_cleanup(sandbox, ex, mux, pane_pid)
    for _ in range(3):
        ex.process_all()
    assert len(taken) == 1
    assert len([n for n in sandbox.notes if "[checkpoint]" in n]) == 1


def test_the_pane_is_captured_only_while_the_window_is_alive(sandbox):
    class CapMux(FakeMux):
        def capture(self, name):
            return "pane tail"

    seen = []
    ex = make_executor(sandbox, CapMux({WINDOW: None}), take_checkpoint=lambda **kw: (seen.append(kw["pane_text"]), "/d")[1])
    req = {"window_target": WINDOW, "reason": "hard_idle"}
    ex._live_windows = {WINDOW}
    assert ex._checkpoint_stage(AGENT, req, {}, SLUG, TASK_ID, "ex-" + "3" * 32) == "/d"
    ex._live_windows = set()
    assert ex._checkpoint_stage(AGENT, req, {}, SLUG, TASK_ID, "ex-" + "4" * 32) == "/d"
    assert seen == ["pane tail", None]


def test_a_pane_capture_error_does_not_cost_the_checkpoint(sandbox):
    class BadMux(FakeMux):
        def capture(self, name):
            raise RuntimeError("mux down")

    seen = []
    ex = make_executor(sandbox, BadMux({WINDOW: None}), take_checkpoint=lambda **kw: (seen.append(kw["pane_text"]), "/d")[1])
    ex._live_windows = {WINDOW}
    assert ex._checkpoint_stage(AGENT, {"window_target": WINDOW}, {}, SLUG, TASK_ID, "ex-" + "5" * 32) == "/d"
    assert seen == [None]


def test_real_lib_end_to_end_from_the_record_and_deadline(sandbox, worker, tmp_path):
    """本物の lib_checkpoint で、record の git.worktree から保存して plan.sh retire の引数に載る。期限切れは none。"""
    pane_pid, mux = worker
    wt = tmp_path / "wt"
    wt.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.com")
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, env=env)
    (wt / "work.txt").write_text("uncommitted work\n")
    rec_dir = sandbox.queue / "missions" / SLUG / "executions"
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / f"{SANDBOX_XID}.json").write_text(json.dumps({"execution_id": SANDBOX_XID, "git": {"worktree": str(wt)}}))
    calls = []
    ex = make_executor(sandbox, mux, run_command=lambda argv, env: (calls.append(list(argv)), (0, ""))[1])
    drive_to_cleanup(sandbox, ex, mux, pane_pid)
    d = Path(checkpoint_arg(calls[0]))
    assert d == sandbox.registry / "checkpoints" / SLUG / TASK_ID / SANDBOX_XID
    assert (d / "untracked" / "work.txt").read_text() == "uncommitted work\n"
    lib_checkpoint.verify(d, SANDBOX_XID)

    # 期限が (ほぼ) 0 なら取れず none。途中の dir は残らない。退役は続く
    root = sandbox.registry / "checkpoints" / SLUG / TASK_ID
    os.rename(d, root / "moved-away")
    calls2 = []
    ex2 = make_executor(sandbox, mux, run_command=lambda argv, env: (calls2.append(list(argv)), (0, ""))[1],
                        checkpoint_seconds=1e-9)
    assert ex2._checkpoint_stage(AGENT, {"reason": "timeout"}, {}, SLUG, TASK_ID, SANDBOX_XID) == "none"
    assert sorted(p.name for p in root.iterdir()) == ["moved-away"]
