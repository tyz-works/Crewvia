"""試行の台帳 (vNext 02 PR-D) — Controller 層のテスト (設計 `knowledge/vnext-02-attempt-reopen-checkpoint.md` §3.2・§8)。

lib (`lib_task_controller` / `lib_history` / `lib_state_store`) の API だけで場面を作る。plan.sh を通した外から見える挙動は
`test_history_ledger_plan_sh.py`。「何も書かない」は queue の全ファイルの sha256 の一致 (`snapshot`)。

* 帰属 (`result_execution_id`) を更新するのは `result_replaced=True` で呼ばれた操作だけ。verify-result 相当 (本文に節を足すだけ) では
  帰属が変わらず、台帳の Result は空 (`result: none`)
* 再送の冪等: 台帳だけ書けた孤児 (card の書き込みの失敗・kill) の後、同じ報告の再送は新しいファイルを足さず、`ended_at` が
  台帳・record で一致する。違う Result の再送は `HISTORY_MISMATCH` (card を書かない・値を出さない)
* 台帳が書けないとき遷移を止める (card を書かない)。retire だけは閉じる
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, AGENT, T0, T1, T2, SECRET, ex, store, ctl

import lib_history as hist  # noqa: E402  (task_controller_helpers が scripts/ を sys.path に足している)

TID = "t001"


@pytest.fixture
def q(tmp_path):
    return h.seed(tmp_path / "q", tids=("t001",))


def ledger_dir(q, tid=TID):
    return pathlib.Path(q, "missions", MISSION, "history", tid)


def ledger_files(q, tid=TID):
    d = ledger_dir(q, tid)
    return sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) if d.is_dir() else []


def ledger(q, name):
    fields, body = hist.parse(pathlib.Path(ledger_dir(q), name).read_text(), source=name)
    return fields, body


def only_ledger(q):
    names = ledger_files(q)
    assert len(names) == 1, names
    return names[0], *ledger(q, names[0])


def result_body(text):
    return h.body(result=text)


def done(q, xid, text="RESULT-A", *, now=T1, replaced=True, body=None, extra=None):
    with h.tx(q, op="done") as t:
        return ctl.complete_execution(
            t, MISSION, TID, h.caller(xid), to_status="done",
            meta_updates={"completed_at": "2026-10-16T00:00:00Z", **(extra or {})},
            body=body if body is not None else result_body(text), now=now, result_replaced=replaced)


def running_card_with_old_result(q, *, status="verifying", old_owner=None):
    """試行 2 (running) の card。`## Result` は試行 1 が置いたもの (`result_execution_id` = 試行 1)。"""
    extra = dict(current_execution_id=h.xid(2), execution_status="running", execution_count=2,
                 execution_reserved_at=T1, task_slug="task-t001", execution_agent=AGENT)
    if old_owner is not False:
        extra["result_execution_id"] = old_owner or h.xid(1)
    with h.tx(q, op="seed") as t:
        t.write_card(MISSION, TID, h.card(TID, status=status, worker=AGENT, started_at=T1, **extra),
                     result_body("OLD-RESULT-FROM-ATTEMPT-1"))
    return h.xid(2)


# ---------------------------------------------------------------------------
# 台帳の中身と帰属
# ---------------------------------------------------------------------------

def test_done_writes_one_ledger_file_with_the_result_and_attributes_it_to_the_attempt(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    done(q, xid, "THE-RESULT")
    name, fields, body = only_ledger(q)
    assert name == f"0001-end-{xid}.md"
    assert fields["execution_id"] == xid and fields["attempt"] == 1 and fields["kind"] == "end"
    assert fields["end_code"] == "DONE" and fields["result"] == "recorded" and fields["worker"] == AGENT
    assert fields["reopen_count"] == 0 and fields["ended_at"] == T1
    assert body.strip() == "THE-RESULT"
    assert h.read_meta(q)["result_execution_id"] == xid
    assert h.read_record(q, xid)["ended_at"] == T1                      # card・record・台帳が同じ時刻


def test_a_call_that_only_adds_a_section_does_not_move_the_attribution(q):
    """verify-result 相当: 既存の本文に節を足すだけ (result_replaced を渡さない)。古い Result を今の試行のものにしない (§8)。"""
    xid = running_card_with_old_result(q)
    appended = result_body("OLD-RESULT-FROM-ATTEMPT-1") + "\n## Verification\n- pass\n"
    with h.tx(q, op="verify-result") as t:
        ctl.complete_execution(t, MISSION, TID, h.caller(xid), to_status="verified",
                               meta_updates={"completed_at": "x"}, body=appended, now=T2)
    assert h.read_meta(q)["result_execution_id"] == h.xid(1)            # 帰属は試行 1 のまま
    _name, fields, body = only_ledger(q)
    assert fields["execution_id"] == xid and fields["end_code"] == "VERIFIED"
    assert fields["result"] == "none" and body.strip() == ""            # 試行 2 は Result を書いていない
    assert "OLD-RESULT" not in pathlib.Path(ledger_dir(q), _name).read_text()


def test_replacing_the_result_moves_the_attribution_and_the_ledger_takes_the_new_text(q):
    xid = running_card_with_old_result(q)
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, TID, h.caller(xid), to_status="verified", body=result_body("NEW"),
                               now=T2, result_replaced=True)
    assert h.read_meta(q)["result_execution_id"] == xid
    _n, fields, body = only_ledger(q)
    assert fields["result"] == "recorded" and body.strip() == "NEW"


def test_result_replaced_needs_a_new_body(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    with h.tx(q, op="done") as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.complete_execution, t, MISSION, TID, h.caller(xid),
                      to_status="done", result_replaced=True)


def test_a_card_without_the_attribution_field_is_recorded_as_unattributed(q):
    """この設計より前に作られた card (`result_execution_id` が無い): 旧本文の Result を今の試行のものにしない。"""
    xid = running_card_with_old_result(q, old_owner=False, status="in_progress")
    with h.tx(q, op="fail") as t:
        ctl.fail_execution(t, MISSION, TID, h.caller(xid), ex.NEEDS_DIRECTOR, to_status="needs_director",
                           meta_updates={"needs_director_reason": "r"}, body=result_body("OLD-RESULT-FROM-ATTEMPT-1"),
                           now=T2)
    _n, fields, body = only_ledger(q)
    assert fields["result"] == "unattributed" and body.strip() == ""
    assert "result_execution_id" not in h.read_meta(q)


def test_trailing_sections_after_the_result_are_not_part_of_the_recorded_result(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    done(q, xid, body=result_body("R-TEXT") + "\n## Needs-Director 詳細\nAPPENDIX\n")
    _n, fields, body = only_ledger(q)
    assert body.strip() == "R-TEXT"


@pytest.mark.parametrize("op, kwargs, kind, end_code", [
    ("reset_running", {}, "reset", "RESET_BY_DIRECTOR"),
    ("retire_running", {}, "retire", "RETIRED"),
    ("worker_fail", {}, "end", "WORKER_FAILED"),
])
def test_kinds_follow_the_end_code(q, op, kwargs, kind, end_code):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    with h.tx(q, op=op) as t:
        if op == "reset_running":
            ctl.reset_task(t, MISSION, TID, h.caller(xid), now=T1)
        elif op == "retire_running":
            ctl.fail_execution(t, MISSION, TID, h.caller(xid), ex.RETIRED, to_status="pending", now=T1)
        else:
            ctl.fail_execution(t, MISSION, TID, h.caller(xid), ex.WORKER_FAILED, to_status="failed",
                               meta_updates={"completed_at": "x"}, body=result_body("FAILED — head: none"), now=T1,
                               result_replaced=True)
    name, fields, _body = only_ledger(q)
    assert fields["kind"] == kind and fields["end_code"] == end_code and name.startswith(f"0001-{kind}-")


def test_released_reservation_is_recorded_too(q):
    ctx = h.reserve(q, factory=h.ids())
    with h.tx(q, op="reset") as t:
        ctl.reset_task(t, MISSION, TID, h.caller(ctx.execution_id), now=T1)
    _n, fields, _b = only_ledger(q)
    assert fields["kind"] == "reset" and fields["execution_id"] == ctx.execution_id


def test_a_card_without_an_attempt_writes_no_ledger(q):
    """試行の無い card (legacy・Director が開いた card) への報告は task の遷移だけ。帰属は消す (どの試行のものでもない)。"""
    with h.tx(q, op="seed") as t:
        t.write_card(MISSION, TID, h.card(TID, status="in_progress", worker=AGENT, started_at=T0,
                                          result_execution_id=h.xid(9)), result_body("old"))
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, TID, ctl.NO_CALLER, to_status="done", body=result_body("N"), now=T1,
                               result_replaced=True)
    assert ledger_files(q) == []
    assert "result_execution_id" not in h.read_meta(q)


def test_callers_cannot_write_the_attribution_field(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    with h.tx(q, op="done") as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.complete_execution, t, MISSION, TID, h.caller(xid), to_status="done",
                      meta_updates={"result_execution_id": h.xid(7)})


# ---------------------------------------------------------------------------
# 再送の冪等 (台帳だけ書けた孤児の後)
# ---------------------------------------------------------------------------

def orphan_by_failing_card_write(q, xid, text="RESULT-A"):
    """台帳は書けたが card の書き込みが失敗した、を作る (card の書き込みだけを失敗させる)。"""
    with pytest.raises(RuntimeError, match="boom"):
        with h.tx(q, op="done") as t:
            def boom(*a, **k):
                raise RuntimeError("boom")
            t.write_card = boom
            ctl.complete_execution(t, MISSION, TID, h.caller(xid), to_status="done",
                                   meta_updates={"completed_at": "x"}, body=result_body(text), now=T1,
                                   result_replaced=True)


def test_resend_after_an_orphan_ledger_adds_no_file_and_keeps_ended_at_everywhere(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    orphan_by_failing_card_write(q, xid)
    assert h.read_meta(q)["status"] == "in_progress"                      # card はコミットされていない
    [name] = ledger_files(q)
    before = pathlib.Path(ledger_dir(q), name).read_bytes()
    assert ledger(q, name)[0]["ended_at"] == T1

    done(q, xid, now=T2)                                                  # 同じ報告の再送。時刻だけが違う
    assert ledger_files(q) == [name]                                      # 新しいファイルが増えない
    assert pathlib.Path(ledger_dir(q), name).read_bytes() == before       # 書き直さない
    assert h.read_meta(q)["status"] == "done"
    assert h.read_record(q, xid)["ended_at"] == T1                        # record も台帳の ended_at (再送の now ではない)


def test_resend_with_a_different_result_is_refused_and_writes_nothing_but_the_audit_row(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    orphan_by_failing_card_write(q, xid, "RESULT-A")
    before = h.snapshot(q)
    with h.tx(q, op="done") as t:
        e = h.raises_code(ex.HISTORY_MISMATCH, ctl.complete_execution, t, MISSION, TID, h.caller(xid),
                          to_status="done", meta_updates={"completed_at": "x"},
                          body=result_body(f"DIFFERENT {SECRET}"), now=T2, result_replaced=True)
    assert "body" in e.message and SECRET not in e.message and "RESULT-A" not in e.message
    assert ex.EXIT_CODES[e.code] == 3
    assert h.snapshot(q) == before                                        # card・record・台帳・枠 1 バイトも書かない
    assert h.audit_rows(q)[-1]["result"] == "refused:HISTORY_MISMATCH"
    assert h.read_meta(q)["status"] == "in_progress"
    done(q, xid, "RESULT-A", now=T2)                                      # 出口: 同じ報告を送り直す
    assert h.read_meta(q)["status"] == "done"


@pytest.mark.parametrize("field, change", [
    ("pr_number", {"pr_number": 99}),
    ("head", {"fail_head": "abc1234"}),
])
def test_resend_compares_the_other_fields_too(q, field, change):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    with pytest.raises(RuntimeError, match="boom"):
        with h.tx(q, op="done") as t:
            def boom(*a, **k):
                raise RuntimeError("boom")
            t.write_card = boom
            ctl.complete_execution(t, MISSION, TID, h.caller(xid), to_status="done", body=result_body("R"),
                                   now=T1, result_replaced=True)
    with h.tx(q, op="done") as t:
        e = h.raises_code(ex.HISTORY_MISMATCH, ctl.complete_execution, t, MISSION, TID, h.caller(xid),
                          to_status="done", meta_updates=change, body=result_body("R"), now=T2, result_replaced=True)
    assert field in e.message


def test_two_files_with_the_same_identity_key_are_ambiguous(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    orphan_by_failing_card_write(q, xid)
    [name] = ledger_files(q)
    twin = ledger_dir(q) / f"0002-end-{xid}.md"
    twin.write_bytes(pathlib.Path(ledger_dir(q), name).read_bytes())
    before = h.snapshot(q)
    with h.tx(q, op="done") as t:
        e = h.raises_code(ex.HISTORY_AMBIGUOUS, ctl.complete_execution, t, MISSION, TID, h.caller(xid),
                          to_status="done", meta_updates={"completed_at": "x"}, body=result_body("RESULT-A"),
                          now=T2, result_replaced=True)
    assert ex.EXIT_CODES[e.code] == 3
    assert h.snapshot(q) == before
    assert h.audit_rows(q)[-1]["result"] == "refused:HISTORY_AMBIGUOUS"


def test_a_file_for_another_reopen_count_is_not_the_same_event(q):
    """同じ ID の 2 回目の遷移 (reopen_count が違う) は同じイベントではない。新しいファイルを足す。"""
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    other = hist.build_event(kind="end", execution_id=xid, attempt=1, ended_at=T0, end_code="DONE", reopen_count=1,
                             result_state="none")
    ledger_dir(q).mkdir(parents=True)
    (ledger_dir(q) / f"0001-end-{xid}.md").write_text(hist.render(*other))
    done(q, xid)
    assert ledger_files(q) == [f"0001-end-{xid}.md", f"0002-end-{xid}.md"]
    assert ledger(q, f"0002-end-{xid}.md")[0]["reopen_count"] == 0


def test_an_unreadable_candidate_stops_the_transition_without_writing(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    ledger_dir(q).mkdir(parents=True)
    (ledger_dir(q) / f"0001-end-{xid}.md").write_text(f"not a ledger {SECRET}")
    before = h.snapshot(q)
    with h.tx(q, op="done") as t:
        e = h.raises_code(ex.STATE_INVALID, ctl.complete_execution, t, MISSION, TID, h.caller(xid),
                          to_status="done", body=result_body("R"), now=T1, result_replaced=True)
    assert SECRET not in e.message
    assert h.snapshot(q) == before


# ---------------------------------------------------------------------------
# 台帳が書けないとき
# ---------------------------------------------------------------------------

def unwritable_ledger(q):
    d = ledger_dir(q)
    d.mkdir(parents=True)
    d.chmod(0o500)
    return d


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")
def test_when_the_ledger_cannot_be_written_the_transition_stops_and_the_card_is_untouched(q):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    d = unwritable_ledger(q)
    before = h.snapshot(q)
    try:
        with pytest.raises(store.StoreWriteError):
            done(q, xid)
    finally:
        d.chmod(0o700)
    assert h.snapshot(q) == before
    assert h.read_meta(q)["status"] == "in_progress" and h.read_meta(q)["execution_status"] == "running"


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")
def test_retire_still_closes_when_the_ledger_cannot_be_written(q, capsys):
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    d = unwritable_ledger(q)
    try:
        with h.tx(q, op="retire") as t:
            ctl.fail_execution(t, MISSION, TID, h.caller(xid), ex.RETIRED, to_status="pending", now=T1)
    finally:
        d.chmod(0o700)
    meta = h.read_meta(q)
    assert meta["status"] == "pending" and meta["execution_status"] == "failed" and meta["execution_end_code"] == "RETIRED"
    assert ledger_files(q) == []
    assert h.audit_rows(q)[-1]["detail"] == "history_unwritten"           # 監査ログの result に残す
    assert "台帳を書けないまま退役を閉じます" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")
def test_retire_also_closes_over_a_mismatching_ledger(q):
    """retire は台帳の食い違いでも閉じる (退役が止まると誰も窓を閉じない)。再送の actor が違っても止めない。"""
    xid = h.reserve_and_start(q, factory=h.ids()).execution_id
    other = hist.build_event(kind="retire", execution_id=xid, attempt=1, ended_at=T0, end_code="RETIRED",
                             worker="Someone-else", result_state="none")
    ledger_dir(q).mkdir(parents=True)
    (ledger_dir(q) / f"0001-retire-{xid}.md").write_text(hist.render(*other))
    with h.tx(q, op="retire") as t:
        ctl.fail_execution(t, MISSION, TID, h.caller(xid), ex.RETIRED, to_status="pending", now=T1)
    assert h.read_meta(q)["execution_end_code"] == "RETIRED"
    assert h.audit_rows(q)[-1]["detail"] == "history_unwritten"


# ---------------------------------------------------------------------------
# kill の境界 (台帳だけ書けた孤児を含む全点)
# ---------------------------------------------------------------------------

def copy_queue(src, dst):
    shutil.copytree(src, dst, symlinks=True)
    return dst


def _done_only(q):
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, TID, h.caller(h.xid(1)), to_status="done",
                               meta_updates={"completed_at": "x"}, body=result_body("RESULT-A"), now=T1,
                               result_replaced=True)


def test_kill_at_every_write_then_resend_converges_to_one_ledger_file(tmp_path):
    """done の書き込みの各点で SIGKILL → 同じ報告を (別の時刻で) 再送 → 台帳は 1 ファイル・ended_at が台帳と record で一致。"""
    seed = h.seed(tmp_path / "seed", tids=("t001",))
    h.reserve_and_start(seed, factory=h.ids())
    points = h.points_of(copy_queue(seed, tmp_path / "probe"), _done_only)
    assert len(points) >= 10, points                                      # 注入口が壊れて 0 点で PASS しない
    survived_with_orphan = 0
    for k in range(len(points)):
        q = copy_queue(seed, tmp_path / f"k{k}")
        assert h.fork_and_crash(q, _done_only, k)
        had_ledger = bool(ledger_files(q))
        if had_ledger and h.read_meta(q)["status"] == "in_progress":
            survived_with_orphan += 1                                     # 台帳だけ書けた孤児の点を通った
        expected = T1 if had_ledger else T2                               # 台帳があれば再利用、無ければ再送の時刻で新規
        with h.tx(q, op="done", recover=h.scope()) as t:
            ctl.complete_execution(t, MISSION, TID, h.caller(h.xid(1)), to_status="done",
                                   meta_updates={"completed_at": "x"}, body=result_body("RESULT-A"), now=T2,
                                   result_replaced=True)
        names = ledger_files(q)
        assert len(names) == 1, (k, names)
        fields, _b = ledger(q, names[0])
        assert fields["ended_at"] == expected, (k, fields)
        rec = h.read_record(q, h.xid(1))
        assert rec is not None and rec["ended_at"] == expected, (k, rec)      # 無条件: 台帳から戻る (card の後の crash でも)
        assert h.read_meta(q)["status"] == "done"
    assert survived_with_orphan >= 1                                      # 孤児の点を 1 つ以上確かめた
