"""試行の台帳 (vNext 02 PR-D) — Codex 1 巡目 (t024) の 2 点。

1. [P1] 台帳の frontmatter の欄が**型の違う値** (list / dict / bool / int / null) でも、例外を漏らさず固定コードの
   `HistoryError` / `HistoryUnreadable` / store-check の報告に落ちる。欄を 1 つずつ網羅し (族で洗う)、pull・store-check・retire の
   3 経路で「壊れた台帳でも落ちない」を確かめる。
2. [P2] card の書き込みの後・record の書き込みの前で落ちると、台帳には `ended_at` があるのに record は null のまま残る。
   回復 (R-5) と冪等な再送の**両方**が台帳の `ended_at` から戻す。台帳が無い旧来の card は null のまま (時刻を作らない)。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, T0, T1, T2, ex, store, ctl
from execution_e3_helpers import MISSION as BOX_MISSION, Box, run, take

import lib_history as hist  # noqa: E402

TID = "t001"
XID = "ex-" + "1" * 32

VALID = dict(execution_id=XID, attempt=1, kind="end", end_code="DONE", ended_at=T1, reopen_count=0, head="abc",
             pr_number=305, worker="Ren", actor="Ren", reason="why", checkpoint_dir="/tmp/x", result="none")

BAD_VALUES = ([], {}, True, 7, None, ["a"], {"a": 1})


def _expected_bad(field, value):
    """欄 × 値 で「壊れている」と言うべきか (仕様の表。実装から導かない)。"""
    if field in ("execution_id", "kind", "ended_at"):
        return True                                              # どの非文字列も不正 (None も必須)
    if field == "attempt":
        return value is None or isinstance(value, (list, dict, bool))      # 7 は有効
    if field == "reopen_count":
        return isinstance(value, (list, dict, bool))                        # None (欠落と同じ) と 7 は有効
    if field == "result":
        return value is not None                                             # 文字列以外は全部不正
    if field == "pr_number":
        return isinstance(value, (list, dict, bool))
    return value is not None                                                 # end_code / head / worker / actor / reason / checkpoint_dir


FAMILY = [(f, v) for f in VALID for v in BAD_VALUES]


def _label(param):
    return repr(param)[:24]


@pytest.mark.parametrize("field,value", FAMILY, ids=lambda p: _label(p))
def test_fields_problem_never_raises_and_flags_every_wrongly_typed_value(field, value):
    meta = {**VALID, field: value}
    problem = hist.fields_problem(meta)                          # TypeError を漏らさない
    assert problem is None or isinstance(problem, str)
    if _expected_bad(field, value):
        assert isinstance(problem, str) and problem, (field, value)
    else:
        assert problem is None, (field, value)


@pytest.fixture
def q(tmp_path):
    return h.seed(tmp_path / "q", tids=("t001",))


def ledger_dir(q, tid=TID):
    return pathlib.Path(q, "missions", MISSION, "history", tid)


def dump(meta, body=""):
    return store.serialize_history(meta, body)


def put(q, name, meta, body=""):
    d = ledger_dir(q)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(dump(meta, body))


# 台帳のパーサは `{}` を文字列 '{}' として読む (空の dict は欄の値にならない)。ファイル経由では入れ子の dict で確かめる
BAD_ONLY = [(f, v) for f, v in FAMILY if _expected_bad(f, v) and v != {}]


@pytest.mark.parametrize("field,value", BAD_ONLY, ids=lambda p: _label(p))
def test_every_wrongly_typed_ledger_file_is_unreadable_not_a_crash(q, field, value):
    name = f"0001-end-{XID}.md"
    put(q, name, {**VALID, field: value})
    got = hist.read_event(q, MISSION, TID, name)
    assert got[0] == "unreadable" and isinstance(got[1], str)
    with pytest.raises(hist.HistoryUnreadable):
        hist.load_events(q, MISSION, TID)
    kinds = [k for k, *_ in hist.diagnose(q, [MISSION])]          # 報告に落ちる (例外にならない)
    assert "history_unobservable" in kinds


@pytest.mark.parametrize("field,value", BAD_ONLY, ids=lambda p: _label(p))
def test_a_wrongly_typed_candidate_stops_a_resend_with_a_domain_error_not_a_type_error(q, field, value):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    put(q, f"0001-end-{xid}.md", {**VALID, "execution_id": xid, field: value} if field != "execution_id" else VALID)
    with pytest.raises(Exception) as exc:
        with h.tx(q, op="done") as t:
            ctl.complete_execution(t, MISSION, TID, h.caller(xid), to_status="done",
                                   meta_updates={"completed_at": "x"}, body=h.body(result="R"), now=T1,
                                   result_replaced=True)
    # execution_id が別の試行の名前を指す場合は候補にならず書ける。それ以外は ControllerError (TypeError ではない)
    assert not isinstance(exc.value, TypeError), (field, value)


# ---------------------------------------------------------------------------
# plan.sh を通した 3 経路: pull / store-check / retire
# ---------------------------------------------------------------------------

PLAN_SH_CASES = [("result", []), ("result", {"a": 1}), ("kind", []), ("kind", {"a": 1}), ("end_code", []), ("head", {"a": 1}),
                 ("pr_number", []), ("reopen_count", {"a": 1}), ("attempt", True), ("worker", 7), ("execution_id", [])]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def _plan_ledger_dir(box):
    return box.queue / "missions" / BOX_MISSION / "history" / TID


def _break(box, name, field, value):
    path = _plan_ledger_dir(box) / name
    meta, body = hist.parse(path.read_text(), source=name)
    path.write_text(dump({**meta, field: value}, body))


@pytest.mark.parametrize("field,value", PLAN_SH_CASES, ids=lambda p: _label(p))
def test_pull_and_store_check_survive_a_wrongly_typed_ledger(box, field, value):
    xid1 = take(box)
    assert run(box, "fail", TID, "--no-head", "x", "--mission", BOX_MISSION, "--execution", xid1, agent="Ren").returncode == 0
    [name] = sorted(p.name for p in _plan_ledger_dir(box).iterdir())
    box.reset()
    _break(box, name, field, value)
    out = box.store_check()
    assert "Traceback" not in out and "TypeError" not in out
    assert "history_unobservable" in out
    p = box.pull("Ren", TID)
    assert p.returncode == 0, p.stderr
    assert "Traceback" not in p.stderr and "TypeError" not in p.stderr
    assert json.loads(p.stdout)["execution_id"]                    # pull は JSON を返す (previous_attempt は出ない)
    assert "previous_attempt" not in json.loads(p.stdout)


@pytest.mark.parametrize("field,value", [("result", []), ("kind", {"a": 1}), ("end_code", []), ("pr_number", [])],
                         ids=lambda p: _label(p))
def test_retire_closes_over_a_wrongly_typed_ledger(box, field, value):
    xid = take(box)
    d = _plan_ledger_dir(box)
    d.mkdir(parents=True)
    fields, body = hist.build_event(kind="retire", execution_id=xid, attempt=1, ended_at=T0, end_code="RETIRED",
                                    worker="Ren", result_state="none")
    name = f"0001-retire-{xid}.md"
    (d / name).write_text(hist.render(fields, body))
    _break(box, name, field, value)
    p = box.plan("retire", TID, "--agent", "Ren", "--execution", xid, "--mission", BOX_MISSION, agent="Director")
    assert "Traceback" not in p.stderr and "TypeError" not in p.stderr, p.stderr
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert box.card()["execution_end_code"] == "RETIRED"


# ---------------------------------------------------------------------------
# [P2] card の後・record の前で落ちた試行: ended_at を台帳から戻す
# ---------------------------------------------------------------------------

def _done_args(xid):
    return dict(to_status="done", meta_updates={"completed_at": "x"}, body=h.body(result="RESULT-A"), now=T2,
                result_replaced=True)


def _crash_between_card_and_record(q, xid):
    """card は書けた (コミット済み) が record の書き込みで落ちる。台帳には ended_at がある。"""
    with pytest.raises(RuntimeError, match="boom"):
        with h.tx(q, op="done") as t:
            def boom(*a, **k):
                raise RuntimeError("boom")
            t.write_execution_record = boom
            ctl.complete_execution(t, MISSION, TID, h.caller(xid), **{**_done_args(xid), "now": T1})
    assert h.read_meta(q)["status"] == "done"                         # card はコミット済み
    assert h.read_record(q, xid)["ended_at"] is None                  # record は遅れたまま


def test_idempotent_resend_restores_the_record_ended_at_from_the_ledger(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    _crash_between_card_and_record(q, xid)
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, TID, h.caller(xid), **_done_args(xid))     # 再送 (now は T2)
    assert h.read_record(q, xid)["ended_at"] == T1                    # 台帳の時刻 (再送の now ではない)
    assert len(list(ledger_dir(q).iterdir())) == 1


def test_recovery_r5_restores_the_record_ended_at_from_the_ledger(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    _crash_between_card_and_record(q, xid)
    with h.tx(q, op="recover", recover=h.scope()):
        pass                                                           # 名指しの回復 (R-5)
    assert h.read_record(q, xid)["ended_at"] == T1


def test_without_a_ledger_the_record_ended_at_stays_null_and_nothing_is_invented(q):
    """台帳が無い (この設計より前の card) とき、回復も再送も時刻を作らない。"""
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    _crash_between_card_and_record(q, xid)
    shutil.rmtree(ledger_dir(q))
    with h.tx(q, op="recover", recover=h.scope()):
        pass
    assert h.read_record(q, xid)["ended_at"] is None
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, TID, h.caller(xid), **_done_args(xid))
    assert h.read_record(q, xid)["ended_at"] is None


def test_a_record_that_already_has_ended_at_is_never_rewritten(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, TID, h.caller(xid), **{**_done_args(xid), "now": T1})
    before = pathlib.Path(q, "missions", MISSION, "executions", f"{xid}.json").read_bytes()
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, TID, h.caller(xid), **_done_args(xid))
    with h.tx(q, op="recover", recover=h.scope()):
        pass
    assert pathlib.Path(q, "missions", MISSION, "executions", f"{xid}.json").read_bytes() == before
