"""Controller の「任意の引数も書く前に検証する」族と、diagnose の「閉じた試行を報告し続けない」族
(vNext 01c E1 fix 3 巡目 / t030。PR #270 の P2×2)。

族 1 (P2-1): `now` / `caller` / `body` / `meta_updates` の不正値は、最初の書き込みの前に `INVALID_ARGUMENT` で止まる。
      欠陥版は `now=object()` で card を確定させてから record の直列化で TypeError になり、新しい形の検査に通らない状態を残した。
族 2 (P2-2): diagnose の DETACHED 系の報告は **active な試行**にだけ出す。閉じた試行 (`abandon_detached_execution` 済み・
      正常に終わった後に旧コードが取り直した) では消える。「消せない報告」を 0 にするため、報告ごとに
      「その報告を消す正当な操作」を表にし、操作の後に消えることを示す。

Controller の公開関数の全引数 (`inspect.signature` から導く。表に無い引数が増えたら `test_every_argument_has_a_row` が赤):

| 関数 | 引数 | 最初の書き込みの前の検証 |
|---|---|---|
| reserve_task | txn / slug / tid | `_require_txn` / `_load` (INVALID_ARGUMENT・TASK_NOT_FOUND) |
|  | agent / now / id_factory | `_check_agent_arg` / `_check_generation` / 候補 ID の形と衝突 |
| start_execution | execution_id / git_context / now | 形・照合 / `_check_git_context` / `_check_now` |
| complete_execution | caller / to_status / meta_updates / meta_remove / body / now / dry_run | `_check_caller` / 表 / `_check_updates` / `_check_updates` / `_check_body` / `_check_now` / (真偽。書くかどうかだけ) |
| fail_execution | caller / failure_code / to_status / meta_updates / meta_remove / body / now / dry_run | 同上 + 表 |
| release_execution | caller / reason_code / to_status / now | `_check_caller` / 表 / 表 / `_check_now` |
| reset_task | caller / now | `_check_caller` / `_check_now` |
| abandon_detached_execution | now | `_check_now` |
| mark_task | caller / command / to_status / meta_updates / meta_remove / body | `_check_caller` / 表 / 表 / `_check_updates` / `_check_updates` / `_check_body` |
| get_execution | queue_dir / slug / execution_id | 形 (EXECUTION_NOT_FOUND / INVALID_ARGUMENT)。読むだけ |
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, AGENT, T1, ex, store, ctl

HOLD = store._ASSIGNMENT_HOLDING_STATUSES
X = h.xid(1)

# 全引数の表に載せた名前 (載っていない引数が増えたら赤)
_COVERED = {
    "reserve_task": {"txn", "slug", "tid", "agent", "now", "id_factory", "foreign_slot_checked"},
    "start_execution": {"txn", "slug", "tid", "execution_id", "git_context", "now"},
    "complete_execution": {"txn", "slug", "tid", "caller", "to_status", "meta_updates", "meta_remove", "body", "now",
                           "dry_run"},
    "fail_execution": {"txn", "slug", "tid", "caller", "failure_code", "to_status", "meta_updates", "meta_remove", "body",
                       "now", "dry_run"},
    "release_execution": {"txn", "slug", "tid", "caller", "reason_code", "to_status", "now"},
    "reset_task": {"txn", "slug", "tid", "caller", "now"},
    "abandon_detached_execution": {"txn", "slug", "tid", "now"},
    "mark_task": {"txn", "slug", "tid", "caller", "command", "to_status", "meta_updates", "meta_remove", "body"},
    "get_execution": {"queue_dir", "slug", "execution_id"},
}


def test_every_argument_has_a_row():
    for name, covered in _COVERED.items():
        assert set(inspect.signature(getattr(ctl, name)).parameters) == covered, name


@pytest.fixture
def q(tmp_path):
    return h.seed(tmp_path / "q", tids=("t001", "t002"))


def _reserved(q):
    h.reserve(q, factory=h.ids())


def _running(q):
    h.reserve_and_start(q, factory=h.ids())


def _detached_finished(q):
    _running(q)
    h.set_status(q, "done")


_BAD_NOW = [object(), 12345, "", "bad value!", "x" * 65, ["a"], b"2026"]


def _time_cases():
    """(名前, 場面, now を受けて呼ぶ関数)。`now` を取る全公開関数。"""
    return [
        ("reserve", lambda q: None, lambda t, now: ctl.reserve_task(t, MISSION, "t001", AGENT, now=now)),
        ("start", _reserved, lambda t, now: ctl.start_execution(t, MISSION, "t001", X, now=now)),
        ("complete", _running, lambda t, now: ctl.complete_execution(t, MISSION, "t001", h.caller(X), to_status="done", now=now)),
        ("fail", _running, lambda t, now: ctl.fail_execution(t, MISSION, "t001", h.caller(X), ex.WORKER_FAILED,
                                                              to_status="failed", now=now)),
        ("release", _reserved, lambda t, now: ctl.release_execution(t, MISSION, "t001", h.caller(X), ex.RETIRED, now=now)),
        ("reset-running", _running, lambda t, now: ctl.reset_task(t, MISSION, "t001", now=now)),
        ("reset-reserved", _reserved, lambda t, now: ctl.reset_task(t, MISSION, "t001", now=now)),
        ("abandon", _detached_finished, lambda t, now: ctl.abandon_detached_execution(t, MISSION, "t001", now=now)),
    ]


@pytest.mark.parametrize("name", [c[0] for c in _time_cases()])
@pytest.mark.parametrize("bad", range(len(_BAD_NOW)))
def test_a_bad_now_is_refused_before_the_first_write_in_every_operation(tmp_path, name, bad):
    """欠陥版 (`_now_text` が渡された値をそのまま返す): card を確定させてから record の直列化で TypeError。"""
    scene, call = next((c[1], c[2]) for c in _time_cases() if c[0] == name)
    q = h.seed(tmp_path / "q", tids=("t001", "t002"))
    scene(q)
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, call, t, _BAD_NOW[bad])
    assert h.snapshot(q) == before


def test_a_good_now_and_no_now_still_work(q):
    _running(q)
    with h.tx(q) as t:
        out = ctl.complete_execution(t, MISSION, "t001", h.caller(X), to_status="done", now=T1)
    assert out.status == "completed" and h.read_record(q, X)["ended_at"] == T1


@pytest.mark.parametrize("bad_caller", ["ex-" + "a" * 32, X, {"execution_id": X}, object()])
@pytest.mark.parametrize("op", ["complete", "fail", "release", "reset", "mark"])
def test_a_caller_that_is_not_a_Caller_is_refused_before_any_write(tmp_path, op, bad_caller):
    q = h.seed(tmp_path / "q", tids=("t001",))
    _reserved(q)
    calls = {
        "complete": lambda t: ctl.complete_execution(t, MISSION, "t001", bad_caller, to_status="done"),
        "fail": lambda t: ctl.fail_execution(t, MISSION, "t001", bad_caller, ex.WORKER_FAILED, to_status="failed"),
        "release": lambda t: ctl.release_execution(t, MISSION, "t001", bad_caller, ex.RETIRED),
        "reset": lambda t: ctl.reset_task(t, MISSION, "t001", bad_caller),
        "mark": lambda t: ctl.mark_task(t, MISSION, "t001", bad_caller, command="ready-for-verification",
                                        to_status="ready_for_verification"),
    }
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, calls[op], t)
    assert h.snapshot(q) == before


@pytest.mark.parametrize("bad_body", [123, ["x"], b"x", object()])
def test_a_body_that_is_not_text_is_refused_before_any_write(q, bad_body):
    _running(q)
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.complete_execution, t, MISSION, "t001", h.caller(X),
                      to_status="done", body=bad_body)
        h.raises_code(ex.INVALID_ARGUMENT, ctl.mark_task, t, MISSION, "t001", command="ready-for-verification",
                      to_status="ready_for_verification", body=bad_body)
    assert h.snapshot(q) == before


@pytest.mark.parametrize("bad", [{"pr_number": object()}, {"x": float("nan")}, {"x": {1, 2}}])
def test_meta_updates_that_cannot_be_serialized_are_refused_before_any_write(q, bad):
    _running(q)
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.complete_execution, t, MISSION, "t001", h.caller(X),
                      to_status="done", meta_updates=bad)
    assert h.snapshot(q) == before


# ---------------------------------------------------------------------------
# 族 2: diagnose の報告は、その報告を消す正当な操作の後に消える
# ---------------------------------------------------------------------------

def _retake_by_old_code(q):
    """旧コードの pull が started_at を取り直した (DETACHED (a)。status は in_progress のまま)。"""
    p = pathlib.Path(q) / "missions" / MISSION / "tasks" / "t001.md"
    p.write_text(re.sub(r"^started_at: .*$", "started_at: 2026-10-01T23:00:00.000000Z", p.read_text(), flags=re.M))


def _kinds(q):
    return [f.kind for f in h.diagnose_kinds(q)]


#: (報告, 場面, その報告を消す正当な操作)。DETACHED で active な試行の報告は `abandon_detached_execution` で消える。
_DETACHED_REPORTS = [
    ("reported:execution_detached", lambda q: (_running(q), _retake_by_old_code(q))),
    ("reported:execution_active_on_finished_task", _detached_finished),
    ("reported:execution_active_on_non_holding_status", lambda q: (_running(q), h.set_status(q, "needs_director"))),
]


@pytest.mark.parametrize("kind", [r[0] for r in _DETACHED_REPORTS])
def test_a_detached_report_goes_away_after_the_operation_that_closes_the_attempt(tmp_path, kind):
    """欠陥版: 閉じた試行 (`failed` / `ABANDONED_OUTSIDE_CONTROLLER`) でも報告が出続け、もう一度 abandon すると
    INVALID_TRANSITION (報告を消す手段が無い)。"""
    scene = next(r[1] for r in _DETACHED_REPORTS if r[0] == kind)
    q = h.seed(tmp_path / "q", tids=("t001",))
    scene(q)
    assert kind in _kinds(q)                                          # 場面が本当にその報告を出している
    with h.tx(q) as t:
        out = ctl.abandon_detached_execution(t, MISSION, "t001")
    assert out.status == "failed" and out.end_code == "ABANDONED_OUTSIDE_CONTROLLER"
    assert not [k for k in _kinds(q) if k.startswith("reported:execution_")], _kinds(q)


def test_an_attempt_closed_normally_is_not_reported_when_old_code_retakes_the_task(q):
    """正常に終わった試行 (completed) の後に旧コードが started_at を取り直しても、閉じた試行を active と報告しない。"""
    _running(q)
    with h.tx(q) as t:
        ctl.complete_execution(t, MISSION, "t001", h.caller(X), to_status="done")
    h.set_status(q, "in_progress")
    _retake_by_old_code(q)
    assert not [k for k in _kinds(q) if k.startswith("reported:execution_")], _kinds(q)
