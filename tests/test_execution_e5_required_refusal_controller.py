"""E5 PR-2: Controller を直接呼んだときも、名乗りなし × active な試行は `EXECUTION_REQUIRED` (対象は報告の操作だけ)。

plan.sh 経由の表は `test_execution_e5_unnamed_report_refused.py`。ここは lib の API で、(1) 3 つの終わらせ方と `mark_task` が
拒否される・何も書かない (監査の `refused:` の 1 行だけ) (2) `dry_run` も同じ拒否 (3) 報告でない操作 (retire / reset / G1 の fail) は
名乗りなしで通る、を固定する。
"""

from __future__ import annotations

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, ex, ctl


@pytest.fixture
def q(tmp_path):
    queue = h.seed(tmp_path / "q", tids=("t001",))
    h.reserve_and_start(queue, factory=h.ids())
    return queue


CALLS = {
    "done": lambda t: ctl.complete_execution(t, MISSION, "t001", ctl.NO_CALLER, to_status="done", now=h.T1),
    "verify-pass": lambda t: ctl.complete_execution(t, MISSION, "t001", ctl.NO_CALLER, to_status="verified", now=h.T1),
    "fail": lambda t: ctl.fail_execution(t, MISSION, "t001", ctl.NO_CALLER, ex.WORKER_FAILED, to_status="failed", now=h.T1),
    "needs-director": lambda t: ctl.fail_execution(t, MISSION, "t001", ctl.NO_CALLER, ex.NEEDS_DIRECTOR,
                                                   to_status="needs_director", now=h.T1),
    "verify-fail": lambda t: ctl.fail_execution(t, MISSION, "t001", ctl.NO_CALLER, ex.VERIFICATION_REJECTED,
                                                to_status="pending", now=h.T1),
    "ready-for-verification": lambda t: ctl.mark_task(t, MISSION, "t001", ctl.NO_CALLER, command="ready-for-verification",
                                                      to_status="ready_for_verification"),
}


@pytest.mark.parametrize("name", list(CALLS))
def test_an_unnamed_report_on_an_active_attempt_is_refused_and_writes_nothing(q, name):
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.EXECUTION_REQUIRED, CALLS[name], t)
    assert h.snapshot(q) == before


@pytest.mark.parametrize("name", ["done", "verify-pass"])
def test_dry_run_refuses_the_same_way(q, name):
    before = h.snapshot(q)
    to_status = "done" if name == "done" else "verified"
    with h.tx(q) as t:
        h.raises_code(ex.EXECUTION_REQUIRED, lambda tt: ctl.complete_execution(
            tt, MISSION, "t001", ctl.NO_CALLER, to_status=to_status, now=h.T1, dry_run=True), t)
    assert h.snapshot(q) == before


def test_the_refusal_is_before_the_transition_check(q):
    """ACTIVE の試行で task の遷移元が不正 (ready_for_verification への done) でも、名乗りなしは `EXECUTION_REQUIRED`。"""
    h.set_status(q, "ready_for_verification")
    with h.tx(q) as t:
        h.raises_code(ex.EXECUTION_REQUIRED, CALLS["done"], t)


def test_the_exit_code_is_3_like_the_other_claim_refusals():
    assert ex.EXIT_CODES[ex.EXECUTION_REQUIRED] == 3 and ex.EXECUTION_REQUIRED in ex.AUDITED_REFUSALS
    assert ex.EXECUTION_REQUIRED in ex.ERROR_CODES


def test_a_reset_by_the_director_needs_no_claim(q):
    """報告でない操作 (reset) は名乗りなしで通る: 止まった task の出口 (§20.3)。"""
    with h.tx(q) as t:
        ctl.reset_task(t, MISSION, "t001", ctl.NO_CALLER, now=h.T1)
    assert h.read_meta(q, "t001")["status"] == "pending"
