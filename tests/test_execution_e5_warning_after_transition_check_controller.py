"""E5 PR-1 (t011 / PR #287 P2): Controller を直接呼んだとき (`_finish` 経路) も、警告は遷移の検査が通った後にだけ出る。

plan.sh は done / fail / needs-director 等を dry_run で先に検査するので、`_finish` の警告の位置の誤りは plan.sh 経由の
テストでは見えない (`test_execution_e5_warning_after_transition_check.py` は `mark_task` 経路だけが赤になる)。
ここは lib の API で、**名乗りなし × active な試行 × 遷移が拒否される**場面を 3 つの終わらせ方で作る。
"""

from __future__ import annotations

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, ex, ctl

MARK = "名乗りなしで報告しました"


@pytest.fixture
def q(tmp_path):
    queue = h.seed(tmp_path / "q", tids=("t001",))
    h.reserve_and_start(queue, factory=h.ids())
    return queue


CALLS = {
    "done": lambda t: ctl.complete_execution(t, MISSION, "t001", ctl.NO_CALLER, to_status="done", now=h.T1),
    "fail": lambda t: ctl.fail_execution(t, MISSION, "t001", ctl.NO_CALLER, ex.WORKER_FAILED, to_status="failed", now=h.T1),
    "needs-director": lambda t: ctl.fail_execution(t, MISSION, "t001", ctl.NO_CALLER, ex.NEEDS_DIRECTOR,
                                                   to_status="needs_director", now=h.T1),
    "verify-pass": lambda t: ctl.complete_execution(t, MISSION, "t001", ctl.NO_CALLER, to_status="verified", now=h.T1),
}


@pytest.mark.parametrize("name", list(CALLS))
def test_a_refused_unnamed_finish_does_not_warn(q, capsys, name):
    # 試行は running のまま ACTIVE (status が pending だと DETACHED = 試行を見ない別の分岐になる)。task の遷移元として不正な status:
    # done / fail / needs-director は in_progress だけ・verify-pass は検証待ちだけ (in_progress のまま)
    if name != "verify-pass":
        h.set_status(q, "ready_for_verification")
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_TRANSITION, CALLS[name], t)
    assert MARK not in capsys.readouterr().err
    assert h.snapshot(q) == before


@pytest.mark.parametrize("name", ["done", "fail", "needs-director"])
def test_an_unnamed_finish_that_passes_warns_once(q, capsys, name):
    with h.tx(q) as t:
        CALLS[name](t)
    assert capsys.readouterr().err.count(MARK) == 1


def test_dry_run_never_warns(q, capsys):
    with h.tx(q) as t:
        assert ctl.complete_execution(t, MISSION, "t001", ctl.NO_CALLER, to_status="done", now=h.T1, dry_run=True) == ctl.PROCEED
    assert MARK not in capsys.readouterr().err
