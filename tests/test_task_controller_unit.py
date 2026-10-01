"""Task Controller の単体テスト (vNext 01c E1。原案 §10.5 の 14 項目 + 設計 §4・§5 の表)。

lib (`lib_task_controller` / `lib_execution` / `lib_state_store`) の API だけで場面を作る (呼び出し元ゼロの段)。
「何も書かない」は queue の全ファイルの sha256 の一致で確かめる (`snapshot`)。名前の `test_10_5_NN` は原案 §10.5 の
項目番号 (14 項目: 1 reserve 成功 … 14 assignment 作成・解除との整合)。
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, AGENT, OTHER, T0, T1, T2, ex, store, ctl


@pytest.fixture
def q(tmp_path):
    return h.seed(tmp_path / "q", tids=("t001", "t002"))


# ---------------------------------------------------------------------------
# 1. reserve 成功
# ---------------------------------------------------------------------------

def test_10_5_01_reserve_writes_card_record_identity_slot_and_audit_in_one_transaction(q):
    ctx = h.reserve(q, factory=h.ids())
    assert ctx.execution_id == h.xid(1) and ctx.attempt == 1 and ctx.agent == AGENT
    assert ctx.started_at == T0 and ctx.task_slug == "task-t001" and ctx.abandoned_execution_id is None

    meta = h.read_meta(q)
    assert meta["status"] == "in_progress" and meta["worker"] == AGENT and meta["started_at"] == T0
    assert meta["current_execution_id"] == h.xid(1) and meta["execution_status"] == "reserved"
    assert meta["execution_count"] == 1 and meta["execution_reserved_at"] == T0
    assert meta["execution_agent"] == AGENT and meta["task_slug"] == "task-t001"
    assert "execution_end_code" not in meta
    assert ex.attempt_view(meta, store._ASSIGNMENT_HOLDING_STATUSES) == ex.ACTIVE

    rec = h.read_record(q, h.xid(1))
    assert rec["status"] == "reserved" and rec["attempt"] == 1 and rec["agent"] == AGENT
    assert rec["reserved_at"] == T0 and rec["end_code"] is None and rec["task"] == "t001"
    assert h.slot_text(q) == f"{MISSION}:t001"
    assert h.identity(q) == {"mission": MISSION, "task": "t001", "worker": AGENT,
                             "started_at": T0, "execution_id": h.xid(1)}
    row = h.audit_rows(q)[-1]
    assert row["op"] == "pull" and row["execution_id"] == h.xid(1) and row["generation"] == T0
    assert row["from_status"] == "pending" and row["to_status"] == "in_progress"
    assert h.diagnose_kinds(q) == []


def test_default_id_generator_is_uuid4_shaped_and_not_derived_from_names_or_time(q):
    a = h.reserve(q, tid="t001", now=T0)
    b = h.reserve(q, agent=OTHER, tid="t002", now=T0)               # 同じ時刻・別の agent・別の task
    for c in (a, b):
        assert re.fullmatch(r"ex-[0-9a-f]{32}", c.execution_id)
    assert a.execution_id != b.execution_id
    assert ex.uuid4_execution_id() != ex.uuid4_execution_id()


def test_reserve_without_an_agent_publishes_no_slot_but_still_issues_an_execution(q):
    ctx = h.reserve(q, agent=None, factory=h.ids())
    meta = h.read_meta(q)
    assert meta["worker"] is None and "execution_agent" not in meta
    assert not (pathlib.Path(q) / "assignments").exists() or not list((pathlib.Path(q) / "assignments").iterdir())
    assert h.read_record(q, ctx.execution_id)["agent"] is None


@pytest.mark.parametrize("agent", ["", "a/b", ".hidden", "x.identity", "bad name", 5])
def test_reserve_refuses_an_unusable_agent_instead_of_falling_back_to_none(q, agent):
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.reserve_task, t, MISSION, "t001", agent, now=T0)
    assert h.snapshot(q) == before


@pytest.mark.parametrize("bad_factory", [lambda: "ex-xyz", lambda: "", lambda: None, lambda: "EX-" + "a" * 32])
def test_an_id_factory_that_returns_a_malformed_id_writes_nothing(q, bad_factory):
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.reserve_task, t, MISSION, "t001", AGENT, now=T0, id_factory=bad_factory)
    assert h.snapshot(q) == before


def test_an_id_factory_that_repeats_an_id_is_refused_not_overwritten(q):
    h.reserve(q, tid="t001", factory=h.ids())
    snap = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.reserve_task, t, MISSION, "t002", OTHER, now=T0,
                      id_factory=lambda: h.xid(1))
    assert h.snapshot(q) == snap


# ---------------------------------------------------------------------------
# 2. duplicate reserve 拒否 / 3. reserve の冪等性方針
# ---------------------------------------------------------------------------

def test_10_5_02_duplicate_reserve_is_refused_and_writes_nothing_but_the_refusal_row(q):
    h.reserve(q, factory=h.ids())
    before = h.snapshot(q)
    with h.tx(q, op="pull") as t:
        e = h.raises_code(ex.TASK_ALREADY_RESERVED, ctl.reserve_task, t, MISSION, "t001", OTHER, now=T1,
                          id_factory=h.ids(50))
    assert e.exit_code == 1 and e.execution_id == h.xid(1)
    assert h.snapshot(q) == before                                       # card / record / 枠 / identity は 1 バイトも変わらない
    row = h.audit_rows(q)[-1]
    assert row["result"] == "refused:TASK_ALREADY_RESERVED" and row["execution_id"] == h.xid(1)
    assert h.record_files(q) == [h.xid(1) + ".json"]


def test_10_5_03_reserve_is_not_idempotent_even_for_the_same_agent_resume_is_a_pull_concern(q):
    """方針 (execution.md §6): Controller の reserve は再送を成功にしない。同じ Worker の再 pull の再開は E2 が
    card の `execution_status == reserved` を見て reserve を呼ばずに行う。ここで attempt が増えない・新しい ID が出ない。"""
    h.reserve(q, factory=h.ids())
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.TASK_ALREADY_RESERVED, ctl.reserve_task, t, MISSION, "t001", AGENT, now=T1, id_factory=h.ids(9))
    assert h.snapshot(q) == before
    assert h.read_meta(q)["execution_count"] == 1


@pytest.mark.parametrize("status", ["done", "failed", "blocked", "needs_director", "skipped", "verification_failed"])
def test_reserve_refuses_a_task_that_is_not_pending_as_not_eligible(tmp_path, status):
    extra = {"needs_director_reason": "r"} if status == "needs_director" else {}
    if status == "blocked":
        extra = {"blocked_reason": "r"}
    q = h.seed(tmp_path / "q", {"t001": (h.card("t001", status, **extra), h.body())})
    before = h.snapshot(q)
    with h.tx(q) as t:
        e = h.raises_code(ex.TASK_NOT_ELIGIBLE, ctl.reserve_task, t, MISSION, "t001", AGENT, now=T0)
    assert e.exit_code == 1
    assert h.snapshot(q) == before
    assert not any(r["result"].startswith("refused") for r in h.audit_rows(q))          # 拒否の行にしない (行にするのは照合と遷移だけ)


def test_reserve_refuses_when_the_agents_slot_points_at_another_task(q):
    h.reserve(q, tid="t001", factory=h.ids())
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.TASK_NOT_ELIGIBLE, ctl.reserve_task, t, MISSION, "t002", AGENT, now=T1, id_factory=h.ids(9))
    assert h.snapshot(q) == before


def test_missing_card_and_unreadable_card_have_their_own_codes(q):
    with h.tx(q) as t:
        e = h.raises_code(ex.TASK_NOT_FOUND, ctl.reserve_task, t, MISSION, "t099", AGENT, now=T0)
        assert e.exit_code == 1
    (pathlib.Path(q) / "missions" / MISSION / "tasks" / "t001.md").write_text("---\nstatus: [oops\n---\n")
    with h.tx(q) as t:
        e = h.raises_code(ex.STATE_INVALID, ctl.reserve_task, t, MISSION, "t001", AGENT, now=T0)
        assert e.exit_code == 1


def test_a_card_with_half_the_execution_fields_is_state_invalid_for_every_operation(q):
    p = pathlib.Path(q) / "missions" / MISSION / "tasks" / "t001.md"
    p.write_text(p.read_text().replace("status: pending", "status: in_progress\nexecution_status: running"))
    before = h.snapshot(q)
    with h.tx(q) as t:
        for fn, args, kw in [
            (ctl.reserve_task, (t, MISSION, "t001", AGENT), {"now": T0}),
            (ctl.start_execution, (t, MISSION, "t001", h.xid(1)), {}),
            (ctl.complete_execution, (t, MISSION, "t001"), {"to_status": "done"}),
            (ctl.fail_execution, (t, MISSION, "t001", ctl.NO_CALLER, ex.WORKER_FAILED), {"to_status": "failed"}),
            (ctl.release_execution, (t, MISSION, "t001", ctl.NO_CALLER, ex.RETIRED), {}),
            (ctl.reset_task, (t, MISSION, "t001"), {}),
            (ctl.mark_task, (t, MISSION, "t001"), {"command": "ready-for-verification", "to_status": "ready_for_verification"}),
        ]:
            h.raises_code(ex.STATE_INVALID, fn, *args, **kw)
    assert h.snapshot(q) == before


# ---------------------------------------------------------------------------
# 4. reserved → running / 5. running → completed
# ---------------------------------------------------------------------------

def test_10_5_04_start_moves_reserved_to_running_and_records_the_git_context(q):
    ctx = h.reserve(q, factory=h.ids())
    git = {"branch": "task/m/t001-x", "base": "origin/main", "pr_base": "main", "worktree": "/abs/wt",
           "head_at_start": "0123abcd"}
    with h.tx(q) as t:
        out = ctl.start_execution(t, MISSION, "t001", ctx.execution_id, git, now="2026-10-01T10:00:03Z")
    assert out.status == "running" and out.git == git and out.running_at == "2026-10-01T10:00:03Z"
    meta = h.read_meta(q)
    assert meta["execution_status"] == "running" and meta["status"] == "in_progress" and meta["started_at"] == T0
    rec = h.read_record(q, ctx.execution_id)
    assert rec["status"] == "running" and rec["git"] == git and rec["running_at"] == "2026-10-01T10:00:03Z"
    assert h.audit_rows(q)[-1]["caller_check"] == "verified"
    assert h.diagnose_kinds(q) == []


def test_start_twice_is_an_idempotent_no_write_and_says_so(q):
    ctx = h.reserve_and_start(q, factory=h.ids())
    before = h.snapshot(q)
    with h.tx(q) as t:
        out = ctl.start_execution(t, MISSION, "t001", ctx.execution_id, None)
    assert out.idempotent is True and out.status == "running"
    assert h.snapshot(q) == before


@pytest.mark.parametrize("git", [{"nope": "x"}, {"worktree": "relative/path"}, {"branch": 5}, "str"])
def test_start_refuses_a_malformed_git_context(q, git):
    ctx = h.reserve(q, factory=h.ids())
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.start_execution, t, MISSION, "t001", ctx.execution_id, git)
    assert h.snapshot(q) == before


def test_10_5_05_complete_moves_running_to_completed_and_releases_the_slot(q):
    ctx = h.reserve_and_start(q, factory=h.ids())
    with h.tx(q, op="done") as t:
        out = ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="done",
                                     meta_updates={"completed_at": T2}, body=h.body(result="finished"))
    assert out.status == "completed" and out.end_code == "DONE"
    meta = h.read_meta(q)
    assert meta["status"] == "done" and meta["completed_at"] == T2
    assert meta["execution_status"] == "completed" and meta["execution_end_code"] == "DONE"
    assert "finished" in h.read_card_text(q)
    assert meta["worker"] == AGENT and meta["started_at"] == T0           # done は持ち主・予約の世代を残す
    rec = h.read_record(q, ctx.execution_id)
    assert rec["status"] == "completed" and rec["end_code"] == "DONE" and rec["agent"] == AGENT
    assert h.slot_text(q) is None and h.identity(q) is None
    row = h.audit_rows(q)[-1]
    assert row["caller_check"] == "verified" and row["execution_id"] == ctx.execution_id
    assert h.diagnose_kinds(q) == []


def test_complete_from_reserved_is_an_invalid_transition_and_writes_nothing(q):
    ctx = h.reserve(q, factory=h.ids())                                      # まだ start していない
    before = h.snapshot(q)
    with h.tx(q) as t:
        e = h.raises_code(ex.INVALID_TRANSITION, ctl.complete_execution, t, MISSION, "t001",
                          h.caller(ctx.execution_id), to_status="done")
    assert e.exit_code == 2 and h.snapshot(q) == before
    assert h.audit_rows(q)[-1]["result"] == "refused:INVALID_TRANSITION"


def test_verify_result_pass_completes_with_verified_and_keeps_the_slot(q):
    ctx = h.reserve_and_start(q, factory=h.ids())
    with h.tx(q, op="ready") as t:
        assert ctl.mark_task(t, MISSION, "t001", h.caller(ctx.execution_id), command="ready-for-verification",
                             to_status="ready_for_verification").status == "running"
    with h.tx(q, op="verify") as t:
        out = ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="verified")
    assert out.end_code == "VERIFIED"
    assert h.read_meta(q)["status"] == "verified"
    assert h.slot_text(q) == f"{MISSION}:t001"                             # 今と同じ: R-2 が後で消す (state-store.md §7)
    assert h.diagnose_kinds(q) == [] or all(f.kind.startswith("would-repair:R-2") for f in h.diagnose_kinds(q))


# ---------------------------------------------------------------------------
# 6. reserved / running → failed / 7. reserved → released
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("code,to_status,clears_owner", [
    (ex.WORKER_FAILED, "failed", False),
    (ex.NEEDS_DIRECTOR, "needs_director", False),
    (ex.RESET_BY_DIRECTOR, "pending", True),
    (ex.RETIRED, "pending", True),
    (ex.RETIRED, "needs_director", False),
])
def test_10_5_06_fail_from_reserved_or_running(q, started, code, to_status, clears_owner):
    ctx = h.reserve_and_start(q, factory=h.ids()) if started else h.reserve(q, factory=h.ids())
    meta = h.read_meta(q)
    if code == ex.WORKER_FAILED and not started:
        pass                                                                # reserved → failed も EXEC-04 の許可遷移
    with h.tx(q, op="fail") as t:
        out = ctl.fail_execution(t, MISSION, "t001", h.caller(ctx.execution_id), code, to_status=to_status,
                                 meta_updates={"needs_director_reason": "r"} if to_status == "needs_director" else None)
    assert out.status == "failed" and out.end_code == code
    meta = h.read_meta(q)
    assert meta["status"] == to_status and meta["execution_status"] == "failed" and meta["execution_end_code"] == code
    assert (meta["worker"] is None and meta["started_at"] is None) == clears_owner
    assert meta["execution_reserved_at"] == T0                              # 予約の世代は null にしても残す
    assert h.read_record(q, ctx.execution_id)["end_code"] == code
    assert h.slot_text(q) is None and h.identity(q) is None
    assert h.diagnose_kinds(q) == []


def test_10_5_07_release_moves_reserved_to_released_and_running_cannot_be_released(q):
    ctx = h.reserve(q, factory=h.ids())
    with h.tx(q, op="update") as t:
        out = ctl.release_execution(t, MISSION, "t001", ctl.NO_CALLER, ex.RESET_BY_DIRECTOR)
    assert out.status == "released" and out.end_code == "RESET_BY_DIRECTOR"
    meta = h.read_meta(q)
    assert meta["status"] == "pending" and meta["worker"] is None and meta["started_at"] is None
    assert h.slot_text(q) is None and h.diagnose_kinds(q) == []

    ctx2 = h.reserve_and_start(q, tid="t002", agent=OTHER, factory=h.ids(10))
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_TRANSITION, ctl.release_execution, t, MISSION, "t002",
                      h.caller(ctx2.execution_id), ex.RETIRED)
    assert h.snapshot(q) == before


# ---------------------------------------------------------------------------
# 8. terminal 状態からの不正遷移拒否
# ---------------------------------------------------------------------------

def _terminal(q, how):
    """`how` の終わり方をした試行を作って (ctx, 終了コード) を返す。"""
    ctx = h.reserve_and_start(q, factory=h.ids())
    c = h.caller(ctx.execution_id)
    with h.tx(q, op="end") as t:
        if how == "completed":
            ctl.complete_execution(t, MISSION, "t001", c, to_status="done")
        elif how == "failed":
            ctl.fail_execution(t, MISSION, "t001", c, ex.WORKER_FAILED, to_status="failed")
        elif how == "reset":
            ctl.reset_task(t, MISSION, "t001")
    return ctx


@pytest.mark.parametrize("how", ["completed", "failed", "reset"])
def test_10_5_08_a_terminal_execution_never_moves_to_another_status(q, how):
    ctx = _terminal(q, how)
    c = h.caller(ctx.execution_id)
    before_meta = h.read_meta(q)
    before = h.snapshot(q)
    with h.tx(q, op="again") as t:
        h.raises_code(ex.INVALID_TRANSITION, ctl.start_execution, t, MISSION, "t001", ctx.execution_id)
        for fn, args, kw in [
            (ctl.complete_execution, (t, MISSION, "t001", c), {"to_status": "verified"}),
            (ctl.fail_execution, (t, MISSION, "t001", c, ex.NEEDS_DIRECTOR), {"to_status": "needs_director"}),
            (ctl.release_execution, (t, MISSION, "t001", c, ex.RETIRED), {}),
        ]:
            with pytest.raises(ex.ControllerError) as info:
                fn(*args, **kw)
            assert info.value.code in (ex.EXECUTION_ALREADY_TERMINAL, ex.EXECUTION_NOT_CURRENT)
    assert h.snapshot(q) == before                                          # 拒否の行を除き 1 バイトも変わらない
    assert {k: v for k, v in h.read_meta(q).items()} == before_meta
    assert h.read_record(q, ctx.execution_id)["status"] == h.read_meta(q)["execution_status"]


def test_without_a_caller_a_terminal_execution_is_never_touched_only_the_task_moves(q):
    ctx = _terminal(q, "completed")
    # Director が開き直す: `update --status in_progress --reset` 相当 (試行は終わったまま)。名乗りなしの done は task の遷移だけ
    p = pathlib.Path(q) / "missions" / MISSION / "tasks" / "t001.md"
    p.write_text(p.read_text().replace("status: done", "status: in_progress"))
    with h.tx(q, op="done") as t:
        out = ctl.complete_execution(t, MISSION, "t001", ctl.NO_CALLER, to_status="done")
    assert out is None                                                       # 試行なしの経路: 試行の欄を書かない
    meta = h.read_meta(q)
    assert meta["status"] == "done" and meta["execution_status"] == "completed" and meta["current_execution_id"] == ctx.execution_id
    assert h.audit_rows(q)[-1]["caller_check"] in ("no_execution", "detached_execution")


# ---------------------------------------------------------------------------
# 9. current でない Execution ID による complete / fail の拒否 (execution.md §5.2)
# ---------------------------------------------------------------------------

def test_10_5_09_another_execution_id_cannot_complete_or_fail(q):
    ctx = h.reserve_and_start(q, factory=h.ids())
    other_ctx = h.reserve_and_start(q, tid="t002", agent=OTHER, factory=h.ids(10))
    before = h.snapshot(q)
    with h.tx(q) as t:
        for who in (h.caller(other_ctx.execution_id), h.caller(h.xid(999)), h.caller(other_ctx.execution_id, "env")):
            e = h.raises_code(ex.EXECUTION_NOT_CURRENT, ctl.complete_execution, t, MISSION, "t001", who, to_status="done")
            assert e.exit_code == 3
            h.raises_code(ex.EXECUTION_NOT_CURRENT, ctl.fail_execution, t, MISSION, "t001", who, ex.WORKER_FAILED,
                          to_status="failed")
    assert h.snapshot(q) == before
    rows = [r for r in h.audit_rows(q) if r["result"].startswith("refused:")]
    assert len(rows) == 6 and all(r["execution_id"] == ctx.execution_id for r in rows)  # card の ID。名乗った値ではない
    assert not any(other_ctx.execution_id == r["execution_id"] for r in rows)
    assert {r.get("detail") for r in rows} == {f"presented={other_ctx.execution_id}", f"presented={h.xid(999)}"}


def test_the_env_origin_is_named_in_the_refusal_with_the_way_out(q):
    h.reserve_and_start(q, factory=h.ids())
    with h.tx(q) as t:
        e = h.raises_code(ex.EXECUTION_NOT_CURRENT, ctl.complete_execution, t, MISSION, "t001",
                          h.caller(h.xid(999), "env"), to_status="done")
    assert "CREWVIA_EXECUTION_ID" in e.message and "--execution" in e.message
    with h.tx(q) as t:
        e = h.raises_code(ex.EXECUTION_NOT_CURRENT, ctl.complete_execution, t, MISSION, "t001",
                          h.caller(h.xid(999), "flag"), to_status="done")
    assert "CREWVIA_EXECUTION_ID" not in e.message


def test_an_old_attempts_id_cannot_end_the_new_attempt(q):
    first = h.reserve_and_start(q, factory=h.ids())
    with h.tx(q) as t:
        ctl.reset_task(t, MISSION, "t001")
    second = h.reserve_and_start(q, factory=h.ids(2))
    assert second.attempt == 2
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.EXECUTION_NOT_CURRENT, ctl.complete_execution, t, MISSION, "t001",
                      h.caller(first.execution_id), to_status="done")
    assert h.snapshot(q) == before


@pytest.mark.parametrize("bad", ["", "ex-1234", "EX-" + "a" * 32, "ex-" + "g" * 32, "x" * 200, h.SECRET])
def test_a_malformed_id_is_not_found_and_never_reaches_the_audit_log_or_the_message(q, bad):
    h.reserve_and_start(q, factory=h.ids())
    before = h.snapshot(q)
    with h.tx(q) as t:
        e = h.raises_code(ex.EXECUTION_NOT_FOUND, ctl.complete_execution, t, MISSION, "t001", ctl.Caller(bad, "flag"),
                          to_status="done")
    assert h.snapshot(q) == before
    assert bad not in e.message or bad == ""
    blob = json.dumps(h.audit_rows(q))
    assert h.SECRET not in blob and (bad == "" or bad not in blob)


def test_a_legacy_card_has_no_execution_so_a_presented_id_is_not_found_and_no_id_passes(tmp_path):
    q = h.seed(tmp_path / "q", {"t001": (h.card("t001", "in_progress", AGENT, T0), h.body())})
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.EXECUTION_NOT_FOUND, ctl.complete_execution, t, MISSION, "t001", h.caller(h.xid(1)),
                      to_status="done")
    assert h.snapshot(q) == before
    with h.tx(q, op="done") as t:
        assert ctl.complete_execution(t, MISSION, "t001", ctl.NO_CALLER, to_status="done") is None
    meta = h.read_meta(q)
    assert meta["status"] == "done" and not ex.has_execution_fields(meta)     # 試行の欄は 1 つも足さない
    assert h.audit_rows(q)[-1]["caller_check"] == "legacy_generation"


def test_no_caller_on_an_active_execution_passes_as_unverified(q):
    """E3 は名乗りなしを拒否しない (execution.md §5.2)。照合の結果は監査に残る。"""
    ctx = h.reserve_and_start(q, factory=h.ids())
    with h.tx(q, op="done") as t:
        out = ctl.complete_execution(t, MISSION, "t001", ctl.NO_CALLER, to_status="done")
    assert out.end_code == "DONE"
    assert h.audit_rows(q)[-1]["caller_check"] == "unverified"
    assert h.read_record(q, ctx.execution_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# 10. 同一 terminal request の再送 / 11. 異なる terminal result への変更拒否 (§4.4 の表)
# ---------------------------------------------------------------------------

def _do(q, op, ctx):
    c = h.caller(ctx.execution_id)
    with h.tx(q, op=op) as t:
        if op == "done":
            return ctl.complete_execution(t, MISSION, "t001", c, to_status="done")
        if op == "verify-pass":
            return ctl.complete_execution(t, MISSION, "t001", c, to_status="verified")
        if op == "fail":
            return ctl.fail_execution(t, MISSION, "t001", c, ex.WORKER_FAILED, to_status="failed")
        if op == "needs-director":
            return ctl.fail_execution(t, MISSION, "t001", c, ex.NEEDS_DIRECTOR, to_status="needs_director",
                                      meta_updates={"needs_director_reason": "r"})
        if op == "verify-fail":
            return ctl.fail_execution(t, MISSION, "t001", c, ex.VERIFICATION_REJECTED, to_status="pending")
        if op == "retire":
            return ctl.fail_execution(t, MISSION, "t001", c, ex.RETIRED, to_status="pending")


def _prepare_for(q, op):
    ctx = h.reserve_and_start(q, factory=h.ids())
    if op in ("verify-pass", "verify-fail"):
        with h.tx(q, op="ready") as t:
            ctl.mark_task(t, MISSION, "t001", h.caller(ctx.execution_id), command="ready-for-verification",
                          to_status="ready_for_verification")
    return ctx


@pytest.mark.parametrize("op", ["done", "verify-pass", "fail", "needs-director", "verify-fail", "retire"])
def test_10_5_10_resending_the_same_terminal_operation_is_an_idempotent_success_that_writes_nothing(q, op):
    ctx = _prepare_for(q, op)
    first = _do(q, op, ctx)
    assert first.idempotent is False
    before = h.snapshot(q)
    rows = len(h.audit_rows(q))
    again = _do(q, op, ctx)
    assert again.idempotent is True and again.execution_id == ctx.execution_id
    assert again.status == first.status and again.end_code == first.end_code
    assert h.snapshot(q) == before and len(h.audit_rows(q)) == rows          # 監査行も増えない


def test_resend_after_a_retire_on_a_reserved_attempt_is_idempotent_too(q):
    ctx = h.reserve(q, factory=h.ids())
    with h.tx(q) as t:
        a = ctl.release_execution(t, MISSION, "t001", h.caller(ctx.execution_id), ex.RETIRED)
    with h.tx(q) as t:
        b = ctl.release_execution(t, MISSION, "t001", h.caller(ctx.execution_id), ex.RETIRED)
    assert a.status == "released" and b.idempotent and b.end_code == "RETIRED"


@pytest.mark.parametrize("first,second,code", [
    ("done", "fail", ex.EXECUTION_ALREADY_TERMINAL),
    ("done", "needs-director", ex.EXECUTION_ALREADY_TERMINAL),
    ("done", "verify-fail", ex.EXECUTION_ALREADY_TERMINAL),
    ("fail", "done", ex.EXECUTION_ALREADY_TERMINAL),
    ("fail", "needs-director", ex.EXECUTION_ALREADY_TERMINAL),
    ("needs-director", "fail", ex.EXECUTION_ALREADY_TERMINAL),
    ("needs-director", "done", ex.EXECUTION_ALREADY_TERMINAL),
    ("verify-fail", "done", ex.EXECUTION_ALREADY_TERMINAL),
    ("verify-fail", "fail", ex.EXECUTION_ALREADY_TERMINAL),
    ("retire", "done", ex.EXECUTION_NOT_CURRENT),               # 持ち主以外の操作で終わった試行 (§4.4 の最終行)
    ("retire", "fail", ex.EXECUTION_NOT_CURRENT),
])
def test_10_5_11_a_different_terminal_result_is_a_conflict_and_writes_nothing(q, first, second, code):
    ctx = _prepare_for(q, first)
    _do(q, first, ctx)
    before = h.snapshot(q)
    with pytest.raises(ex.ControllerError) as info:
        _do(q, second, ctx)
    assert info.value.code == code and info.value.exit_code == 3
    assert h.snapshot(q) == before
    assert h.audit_rows(q)[-1]["result"] == f"refused:{code}"


def test_a_reset_attempt_is_not_current_for_anyone_and_stays_so_after_the_director_changes_the_status(q):
    ctx = _terminal(q, "reset")
    p = pathlib.Path(q) / "missions" / MISSION / "tasks" / "t001.md"
    p.write_text(p.read_text().replace("status: pending", "status: blocked\nblocked_reason: r"))   # update --status X (--reset なし)
    for fn in (lambda t: ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="done"),
               lambda t: ctl.fail_execution(t, MISSION, "t001", h.caller(ctx.execution_id), ex.WORKER_FAILED,
                                            to_status="failed")):
        with h.tx(q) as t:
            with pytest.raises(ex.ControllerError) as info:
                fn(t)
        assert info.value.code == ex.EXECUTION_NOT_CURRENT                    # 終了コードは card に残るので答えが変わらない


# ---------------------------------------------------------------------------
# 12. Execution 履歴保持 / 13. attempt 増加
# ---------------------------------------------------------------------------

def test_10_5_12_every_execution_record_is_kept_and_frozen_after_the_next_reserve(q):
    first = h.reserve_and_start(q, factory=h.ids())
    with h.tx(q, op="update") as t:
        ctl.reset_task(t, MISSION, "t001")
    first_rec = h.read_record(q, first.execution_id)
    second = h.reserve_and_start(q, agent=OTHER, now=T1, factory=h.ids(2))
    with h.tx(q, op="done", actor=OTHER) as t:
        ctl.complete_execution(t, MISSION, "t001", h.caller(second.execution_id), to_status="done")

    assert h.record_files(q) == [first.execution_id + ".json", second.execution_id + ".json"]
    assert h.read_record(q, first.execution_id) == first_rec                  # 次の試行が増えても書き換わらない
    a = ctl.get_execution(q, MISSION, first.execution_id)
    b = ctl.get_execution(q, MISSION, second.execution_id)
    assert (a.attempt, a.agent, a.status, a.end_code) == (1, AGENT, "failed", "RESET_BY_DIRECTOR")
    assert (b.attempt, b.agent, b.status, b.end_code) == (2, OTHER, "completed", "DONE")
    assert h.diagnose_kinds(q) == []


def test_10_5_13_attempt_increases_by_one_per_reserve_and_task_slug_is_fixed_at_the_first(q):
    seen = []
    for n in range(1, 5):
        ctx = h.reserve(q, now=f"2026-10-01T1{n}:00:00.000000Z", factory=h.ids(n * 10))
        seen.append((ctx.attempt, ctx.task_slug))
        with h.tx(q, op="update") as t:
            ctl.reset_task(t, MISSION, "t001")
        # title を変えても task_slug は変わらない (git-policy.md §10 の 1: branch が変わらない)
        p = pathlib.Path(q) / "missions" / MISSION / "tasks" / "t001.md"
        p.write_text(p.read_text().replace("title: task t001", f"title: renamed {n}"))
    assert seen == [(1, "task-t001"), (2, "task-t001"), (3, "task-t001"), (4, "task-t001")]
    assert h.read_meta(q)["execution_count"] == 4


def test_task_slug_for_a_non_ascii_title_falls_back_to_the_task_id(tmp_path):
    q = h.seed(tmp_path / "q", {"t001": (h.card("t001", title="日本語だけ"), h.body())})
    assert h.reserve(q, factory=h.ids()).task_slug == "t001"


# ---------------------------------------------------------------------------
# 14. assignment 作成・解除との整合
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("end", ["done", "fail", "needs-director", "verify-fail", "retire", "reset"])
def test_10_5_14_the_slot_exists_exactly_while_the_attempt_is_active_and_goes_with_the_terminal_write(q, end):
    ctx = _prepare_for(q, end if end != "reset" else "done")
    assert h.slot_text(q) == f"{MISSION}:t001" and h.identity(q)["execution_id"] == ctx.execution_id
    if end == "reset":
        with h.tx(q, op="update") as t:
            ctl.reset_task(t, MISSION, "t001")
    else:
        _do(q, end, ctx)
    assert h.slot_text(q) is None and h.identity(q) is None
    assert h.diagnose_kinds(q) == []


def test_a_removal_that_fails_is_raised_not_swallowed_and_recovery_finishes_it(q, monkeypatch):
    ctx = h.reserve_and_start(q, factory=h.ids())

    def boom(path):
        raise OSError(13, "denied")
    with h.tx(q, op="done") as t:
        monkeypatch.setattr(store, "_sys_unlink", boom)
        with pytest.raises(store.StoreWriteError):
            ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="done")
        monkeypatch.undo()
    # card (コミット点) と record は先に書かれている。枠だけ残る = R-2 が次のロック取得で消す
    assert h.read_meta(q)["status"] == "done" and h.slot_text(q) == f"{MISSION}:t001"
    with h.tx(q, op="next", recover=h.scope()) as t:
        pass
    assert h.slot_text(q) is None and h.diagnose_kinds(q) == []


def test_reserve_publishes_the_identity_before_the_slot_and_reserve_refuses_a_foreign_slot(q):
    """identity → 本体の順 (本体が世代不明で観測されない)。書き込み順は FAULT_HOOK の path で見る。"""
    order = []
    ctx_q = q
    pid_points = []
    import os
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        store.FAULT_HOOK = lambda point, path: order.append(pathlib.Path(path).name) if point == "atomic:replaced" else None
        try:
            h.reserve(ctx_q, factory=h.ids())
            os.write(w, json.dumps(order).encode())
        finally:
            os._exit(0)
    os.close(w)
    data = os.read(r, 65536)
    os.waitpid(pid, 0)
    written = json.loads(data)
    assert written == ["t001.md", h.xid(1) + ".json", AGENT + ".identity", AGENT]       # card → record → identity → 本体


# ---------------------------------------------------------------------------
# §4.3 の狭めた遷移表 (E1 ではデータ + Controller だけ。plan.sh はまだ読まない)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,extra", [
    ("pending", {}), ("blocked", {"blocked_reason": "r"}), ("ready_for_verification", {}), ("verifying", {}),
    ("needs_human_review", {}), ("verification_failed", {}), ("needs_director", {"needs_director_reason": "r"}),
])
def test_done_is_refused_from_every_status_the_narrowed_table_drops(tmp_path, status, extra):
    q = h.seed(tmp_path / "q", {"t001": (h.card("t001", status, AGENT, T0, **extra), h.body())})
    before = h.snapshot(q)
    with h.tx(q) as t:
        e = h.raises_code(ex.INVALID_TRANSITION, ctl.complete_execution, t, MISSION, "t001", ctl.NO_CALLER,
                          to_status="done")
    assert e.exit_code == 2 and h.snapshot(q) == before


def test_fail_stays_open_from_needs_director_and_is_a_task_only_move(tmp_path):
    """Director が判断待ちの task を諦める出口 (execution.md §4.3)。試行は needs-director で既に failed。"""
    q = h.seed(tmp_path / "q", {"t001": (h.card("t001", "pending"), h.body())})
    ctx = h.reserve_and_start(q, factory=h.ids())
    with h.tx(q) as t:
        ctl.fail_execution(t, MISSION, "t001", h.caller(ctx.execution_id), ex.NEEDS_DIRECTOR, to_status="needs_director",
                           meta_updates={"needs_director_reason": "r"})
    with h.tx(q, op="fail") as t:
        assert ctl.fail_execution(t, MISSION, "t001", ctl.NO_CALLER, ex.WORKER_FAILED, to_status="failed") is None
    meta = h.read_meta(q)
    assert meta["status"] == "failed" and meta["execution_end_code"] == "NEEDS_DIRECTOR"   # 試行の欄は触らない


def test_meta_updates_cannot_touch_the_fields_only_the_controller_writes(q):
    ctx = h.reserve_and_start(q, factory=h.ids())
    before = h.snapshot(q)
    for key in ("status", "worker", "started_at", "execution_status", "current_execution_id", "task_slug", "id"):
        with h.tx(q) as t:
            h.raises_code(ex.INVALID_ARGUMENT, ctl.complete_execution, t, MISSION, "t001", h.caller(ctx.execution_id),
                          to_status="done", meta_updates={key: "x"})
    assert h.snapshot(q) == before


@pytest.mark.parametrize("code,to_status", [(ex.WORKER_FAILED, "pending"), (ex.NEEDS_DIRECTOR, "failed"),
                                            (ex.ABANDONED_OUTSIDE_CONTROLLER, "pending"), ("NOPE", "failed"),
                                            (ex.RESET_BY_DIRECTOR, "needs_director")])
def test_fail_execution_refuses_a_code_and_status_pair_that_is_not_in_the_table(q, code, to_status):
    ctx = h.reserve_and_start(q, factory=h.ids())
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.fail_execution, t, MISSION, "t001", h.caller(ctx.execution_id), code,
                      to_status=to_status)
    assert h.snapshot(q) == before


def test_workspace_create_failed_is_only_for_a_reserved_attempt(q):
    ctx = h.reserve_and_start(q, factory=h.ids())                              # running
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_TRANSITION, ctl.fail_execution, t, MISSION, "t001", h.caller(ctx.execution_id),
                      ex.WORKSPACE_CREATE_FAILED, to_status="needs_director")
    q2 = h.seed(q.parent / "q2", tids=("t001",))
    ctx2 = h.reserve(q2, factory=h.ids(40))
    with h.tx(q2) as t:
        ctl.fail_execution(t, MISSION, "t001", h.caller(ctx2.execution_id), ex.WORKSPACE_CREATE_FAILED,
                           to_status="needs_director", meta_updates={"needs_director_reason": "r"})
    meta = h.read_meta(q2)
    assert meta["status"] == "needs_director" and meta["worker"] == AGENT       # G1: worker は残す
    assert meta["execution_end_code"] == "WORKSPACE_CREATE_FAILED" and h.slot_text(q2) is None


# ---------------------------------------------------------------------------
# attempt_view と照合の表 (execution.md §1.2 / §2.2 / §5.2)
# ---------------------------------------------------------------------------

HOLD = store._ASSIGNMENT_HOLDING_STATUSES


def _m(**kw):
    meta = {"status": "in_progress", "worker": AGENT, "started_at": T0}
    meta.update(kw)
    return meta


def _act(**kw):
    base = dict(current_execution_id=h.xid(1), execution_status="running", execution_count=1,
                execution_reserved_at=T0, task_slug="x")
    base.update(kw)
    return _m(**base)


@pytest.mark.parametrize("meta,view", [
    (_m(), ex.NONE),
    (_m(status="pending", started_at=None), ex.NONE),
    (_act(), ex.ACTIVE),
    (_act(execution_status="reserved"), ex.ACTIVE),
    # TERMINAL: started_at が予約の値か null
    (_act(execution_status="completed", execution_end_code="DONE", status="done"), ex.TERMINAL),
    (_act(execution_status="failed", execution_end_code="RESET_BY_DIRECTOR", status="pending", started_at=None, worker=None),
     ex.TERMINAL),
    # DETACHED (a): 空でない started_at が予約の値と違う — 旧コードの pull が取り直した (試行の status に依らない)
    (_act(started_at=T1), ex.DETACHED),
    (_act(execution_status="completed", execution_end_code="DONE", started_at=T1), ex.DETACHED),
    (_act(execution_status="failed", execution_end_code="WORKER_FAILED", started_at=T1), ex.DETACHED),
    # DETACHED (b): active なのに task が holding でない (旧コードの reset / done / Director の update --status)
    (_act(status="pending", started_at=None, worker=None), ex.DETACHED),
    (_act(status="done"), ex.DETACHED),
    (_act(status="blocked"), ex.DETACHED),
    (_act(execution_status="reserved", status="failed"), ex.DETACHED),
])
def test_attempt_view_table(meta, view):
    assert ex.attempt_view(meta, HOLD) == view


def test_attempt_view_is_computed_from_the_card_alone_and_never_defaults_a_broken_card():
    for broken in (_act(execution_status="bogus"), _act(execution_count=0), _act(execution_count=True),
                   _m(task_slug="x"), _act(execution_status="completed")):       # completed なのに終了コードなし
        with pytest.raises(ValueError):
            ex.attempt_view(broken, HOLD)


@pytest.mark.parametrize("status,end,ok", [
    ("completed", "DONE", True), ("completed", "VERIFIED", True), ("completed", "WORKER_FAILED", False),
    ("failed", "WORKER_FAILED", True), ("failed", "ABANDONED_OUTSIDE_CONTROLLER", True), ("failed", "DONE", False),
    ("released", "RESET_BY_DIRECTOR", True), ("released", "RETIRED", True), ("released", "NEEDS_DIRECTOR", False),
    ("reserved", None, True), ("running", None, True), ("reserved", "DONE", False), ("running", "RETIRED", False),
])
def test_status_and_end_code_pairs_follow_the_table(status, end, ok):
    meta = _act(execution_status=status)
    if end:
        meta["execution_end_code"] = end
    assert (ex.fields_problem(meta) is None) == ok


@pytest.mark.parametrize("view_meta,presented,started,expected", [
    # ID が優先 (started_at が一致していても不一致)
    (_act(), h.xid(1), None, (ex.MATCH, "verified")),
    (_act(), h.xid(2), None, (ex.MISMATCH, None)),
    # 名乗りが started_at だけ (旧 marker): 予約の値と一致すれば legacy_generation
    (_act(), None, T0, (ex.MATCH, "legacy_generation")),
    (_act(), None, T1, (ex.MISMATCH, None)),
    # legacy の card: 今の世代の照合
    (_m(), None, T0, (ex.MATCH, "legacy_generation")),
    (_m(), None, T1, (ex.MISMATCH, None)),
    (_m(), h.xid(1), None, (ex.MISMATCH, None)),
    # DETACHED: ID は不一致・started_at だけなら旧コードの持ち主の世代で照合
    (_act(started_at=T1), h.xid(1), None, (ex.MISMATCH, None)),
    (_act(started_at=T1), None, T1, (ex.MATCH, "detached_execution")),
    (_act(started_at=T1), None, T0, (ex.MISMATCH, None)),
    # 証拠なし
    (_act(), None, None, (ex.MISMATCH, None)),
])
def test_execution_matches_table(view_meta, presented, started, expected):
    view = ex.attempt_view(view_meta, HOLD)
    assert ex.execution_matches(view, view_meta, execution_id=presented, started_at=started) == expected


def test_an_empty_presented_value_is_not_treated_as_absent():
    view = ex.attempt_view(_act(), HOLD)
    assert ex.execution_matches(view, _act(), execution_id="") == (ex.MISMATCH, None)
    assert ex.execution_matches(view, _act(), execution_id=None, started_at="") == (ex.MISMATCH, None)


def test_a_reset_terminal_attempt_still_answers_a_marker_that_carries_the_reserved_generation():
    meta = _act(execution_status="failed", execution_end_code="RETIRED", status="pending", started_at=None, worker=None)
    view = ex.attempt_view(meta, HOLD)
    assert view == ex.TERMINAL
    assert ex.execution_matches(view, meta, started_at=T0) == (ex.MATCH, "legacy_generation")


# ---------------------------------------------------------------------------
# DETACHED の扱い (execution.md §1.4 の手順 0・§4.2・§4.4)
# ---------------------------------------------------------------------------

def _old_code_reset(q, tid="t001"):
    """旧コード (d887acf) の `update --reset` が card にすることだけを写す (欄は触らない・worker / started_at を null・pending)。"""
    p = pathlib.Path(q) / "missions" / MISSION / "tasks" / f"{tid}.md"
    text = p.read_text()
    text = re.sub(r"^status: .*$", "status: pending", text, flags=re.M)
    text = re.sub(r"^worker: .*$", "worker: null", text, flags=re.M)
    text = re.sub(r"^started_at: .*$", "started_at: null", text, flags=re.M)
    p.write_text(text)
    for name in (AGENT, AGENT + ".identity"):
        f = pathlib.Path(q) / "assignments" / name
        if f.exists():
            f.unlink()


def test_reserve_closes_a_detached_active_attempt_in_the_card_before_issuing_the_next_id(q):
    first = h.reserve_and_start(q, factory=h.ids())
    _old_code_reset(q)
    assert ex.attempt_view(h.read_meta(q), HOLD) == ex.DETACHED
    second = h.reserve(q, agent=OTHER, now=T1, factory=h.ids(2))
    assert second.abandoned_execution_id == first.execution_id and second.attempt == 2
    old = h.read_record(q, first.execution_id)
    assert old["status"] == "failed" and old["end_code"] == "ABANDONED_OUTSIDE_CONTROLLER"
    assert old["agent"] == AGENT and old["reserved_at"] == T0                   # 不変の欄は残る
    assert any(r["result"] == "reported:stale_execution_status" and r["execution_id"] == first.execution_id
               for r in h.audit_rows(q))
    # 古い試行の再送は NOT_CURRENT (冪等の表を引かない)
    with h.tx(q) as t:
        h.raises_code(ex.EXECUTION_NOT_CURRENT, ctl.complete_execution, t, MISSION, "t001", h.caller(first.execution_id),
                      to_status="done")
    assert h.diagnose_kinds(q) == []


def test_a_detached_terminal_attempt_is_not_idempotent_for_its_old_id(q):
    """Codex P1 の形: X を completed にした後、旧コードの reset → pull が card を取り直した。X の done の再送を成功にしない。"""
    ctx = h.reserve_and_start(q, factory=h.ids())
    with h.tx(q) as t:
        ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="done")
    _old_code_reset(q)
    p = pathlib.Path(q) / "missions" / MISSION / "tasks" / "t001.md"             # 旧コードの pull: 新しい started_at で in_progress
    p.write_text(re.sub(r"^status: .*$", "status: in_progress", p.read_text(), flags=re.M)
                 .replace("worker: null", f"worker: {OTHER}").replace("started_at: null", f'started_at: "{T2}"'))
    meta = h.read_meta(q)
    assert ex.attempt_view(meta, HOLD) == ex.DETACHED and meta["execution_status"] == "completed"
    before = h.snapshot(q)
    with h.tx(q) as t:
        h.raises_code(ex.EXECUTION_NOT_CURRENT, ctl.complete_execution, t, MISSION, "t001", h.caller(ctx.execution_id),
                      to_status="done")
    assert h.snapshot(q) == before


def test_reset_closes_a_detached_active_attempt_as_abandoned_not_as_a_reset(tmp_path):
    q = h.seed(tmp_path / "q")
    first = h.reserve_and_start(q, factory=h.ids())
    meta_path = pathlib.Path(q) / "missions" / MISSION / "tasks" / "t001.md"
    meta_path.write_text(meta_path.read_text().replace("status: in_progress", "status: blocked\nblocked_reason: r"))
    assert ex.attempt_view(h.read_meta(q), HOLD) == ex.DETACHED                  # (b)
    with h.tx(q, op="update") as t:
        assert ctl.reset_task(t, MISSION, "t001") is None
    meta = h.read_meta(q)
    assert meta["status"] == "pending" and meta["worker"] is None
    assert meta["execution_end_code"] == "ABANDONED_OUTSIDE_CONTROLLER" and meta["execution_status"] == "failed"
    assert h.read_record(q, first.execution_id)["end_code"] == "ABANDONED_OUTSIDE_CONTROLLER"
    assert h.slot_text(q) is None


def test_reset_on_a_terminal_or_legacy_card_moves_only_the_task_and_keeps_the_terminal_fields(q):
    ctx = _terminal(q, "completed")
    before_fields = {k: v for k, v in h.read_meta(q).items() if k in ex.EXECUTION_FIELDS}
    with h.tx(q, op="update") as t:
        assert ctl.reset_task(t, MISSION, "t001") is None
    meta = h.read_meta(q)
    assert meta["status"] == "pending" and {k: v for k, v in meta.items() if k in ex.EXECUTION_FIELDS} == before_fields
    assert meta["current_execution_id"] == ctx.execution_id


def test_abandon_detached_execution_is_the_directors_way_to_close_a_finished_tasks_stale_attempt(q):
    ctx = h.reserve_and_start(q, factory=h.ids())
    p = pathlib.Path(q) / "missions" / MISSION / "tasks" / "t001.md"
    p.write_text(p.read_text().replace("status: in_progress", "status: done"))            # 旧コードが done にした
    assert ex.attempt_view(h.read_meta(q), HOLD) == ex.DETACHED
    kinds = [f.kind for f in store.diagnose(q, store.Scope.everything(q))]
    assert "reported:execution_active_on_finished_task" in kinds
    with h.tx(q, op="update") as t:
        out = ctl.abandon_detached_execution(t, MISSION, "t001")
    assert out.status == "failed" and out.end_code == "ABANDONED_OUTSIDE_CONTROLLER"
    assert h.read_meta(q)["status"] == "done"                                             # task には触れない
    assert "reported:execution_active_on_finished_task" not in [f.kind for f in store.diagnose(q, store.Scope.everything(q))]
    with h.tx(q) as t:                                                                    # もう閉じるものが無い
        h.raises_code(ex.INVALID_TRANSITION, ctl.abandon_detached_execution, t, MISSION, "t001")


def test_a_record_left_behind_for_an_unnamed_card_is_only_counted_never_repaired_and_never_used_to_decide(q):
    """設計転記 3 (§9.5 表外): 名指しされない card の record が遅れたまま残る。回復は書かず、store-check が
    `reported:execution_record_stale` で数えるだけ。判断 (冪等の答え) は card から返る — record は使わない。"""
    ctx = h.reserve_and_start(q, factory=h.ids())
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="done",
                               meta_updates={"completed_at": T2})
    rec_path = pathlib.Path(q) / "missions" / MISSION / "executions" / f"{ctx.execution_id}.json"
    stale = json.loads(rec_path.read_text())
    assert stale["status"] == "completed"
    stale["status"], stale["end_code"] = "running", None                     # 旧コードの時代・欠けた書き込みの跡
    rec_path.write_text(json.dumps(stale, sort_keys=True, indent=2) + "\n")

    before = h.snapshot(q)
    unnamed = store.Scope(cards=((MISSION, "t002"),), agents=())          # t001 を名指ししない
    with h.tx(q, op="next", actor="test") as t:
        out = t.recover(unnamed)
    assert [r for r in out if r.repaired] == [] and h.snapshot(q) == before          # 書かない
    kinds = [f.kind for f in store.diagnose(q, store.Scope.everything(q))]
    assert kinds.count("reported:execution_record_stale") == 1                        # 数える
    assert h.snapshot(q) == before                                                    # 数えるだけ (diagnose も書かない)

    with h.tx(q, op="done") as t:                                                      # 判断は card から: 同じ終端の再送は成功
        again = ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="done",
                                       meta_updates={"completed_at": T2})
    assert again.status == "completed"
    assert json.loads(rec_path.read_text())["status"] == "running"                    # 冪等の再送も record を直さない


# ---------------------------------------------------------------------------
# mark_task (試行を変えない task の遷移)
# ---------------------------------------------------------------------------

def test_mark_task_checks_the_caller_when_the_attempt_is_active_and_never_touches_the_attempt(q):
    ctx = h.reserve_and_start(q, factory=h.ids())
    before_fields = {k: v for k, v in h.read_meta(q).items() if k in ex.EXECUTION_FIELDS}
    with h.tx(q) as t:
        h.raises_code(ex.EXECUTION_NOT_CURRENT, ctl.mark_task, t, MISSION, "t001", h.caller(h.xid(77)),
                      command="ready-for-verification", to_status="ready_for_verification")
        out = ctl.mark_task(t, MISSION, "t001", h.caller(ctx.execution_id), command="ready-for-verification",
                            to_status="ready_for_verification")
    assert out.status == "running"
    meta = h.read_meta(q)
    assert meta["status"] == "ready_for_verification"
    assert {k: v for k, v in meta.items() if k in ex.EXECUTION_FIELDS} == before_fields
    assert h.slot_text(q) == f"{MISSION}:t001"                                            # 枠は残る (今と同じ)
    with h.tx(q) as t:
        ctl.mark_task(t, MISSION, "t001", h.caller(ctx.execution_id), command="verifying", to_status="verifying")
        h.raises_code(ex.INVALID_TRANSITION, ctl.mark_task, t, MISSION, "t001", ctl.NO_CALLER,
                      command="ready-for-verification", to_status="ready_for_verification")


def test_mark_task_on_a_terminal_attempt_with_a_caller_is_a_conflict(q):
    ctx = _terminal(q, "completed")
    with h.tx(q) as t:
        with pytest.raises(ex.ControllerError) as info:
            ctl.mark_task(t, MISSION, "t001", h.caller(ctx.execution_id), command="verifying", to_status="verifying")
    assert info.value.code == ex.EXECUTION_ALREADY_TERMINAL


# ---------------------------------------------------------------------------
# 内容を出さない (01a / 01b で 3 回指摘された族。secret 文字列を仕込んで示す)
# ---------------------------------------------------------------------------

def test_no_error_message_audit_row_or_record_carries_card_content_or_the_presented_value(tmp_path):
    q = h.seed(tmp_path / "q", {
        "t001": (h.card("t001", title=f"title {h.SECRET}", needs_director_reason=h.SECRET, notes=h.SECRET),
                 h.body(desc=h.SECRET, result=h.SECRET)),
        "t002": (h.card("t002", "done"), h.body(desc=h.SECRET)),
    })
    messages = []

    def attempt(fn, *a, **kw):
        try:
            fn(*a, **kw)
        except ex.ControllerError as e:
            messages.append(str(e) + e.message)
        except store.StoreError as e:                                           # pragma: no cover
            messages.append(str(e))

    with h.tx(q) as t:
        attempt(ctl.reserve_task, t, MISSION, "t002", AGENT, now=T0)                       # not eligible
        attempt(ctl.reserve_task, t, MISSION, "t001", "bad/name", now=T0)
        attempt(ctl.reserve_task, t, MISSION, "t001", AGENT, now="not a generation " + h.SECRET)
        attempt(ctl.reserve_task, t, MISSION, "t099", AGENT, now=T0)
    ctx = h.reserve_and_start(q, factory=h.ids())
    with h.tx(q) as t:
        attempt(ctl.reserve_task, t, MISSION, "t001", OTHER, now=T1)                       # already reserved
        attempt(ctl.complete_execution, t, MISSION, "t001", ctl.Caller(h.SECRET, "flag"), to_status="done")
        attempt(ctl.complete_execution, t, MISSION, "t001", h.caller(h.xid(5)), to_status="done")
        attempt(ctl.start_execution, t, MISSION, "t001", h.SECRET)
        attempt(ctl.complete_execution, t, MISSION, "t001", h.caller(ctx.execution_id), to_status="nope " + h.SECRET)
        attempt(ctl.get_execution, q, MISSION, h.SECRET)
        attempt(ctl.get_execution, q, MISSION, h.xid(404))
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="done")
    assert messages and all(h.SECRET not in m for m in messages), [m for m in messages if h.SECRET in m]
    blob = json.dumps(h.audit_rows(q)) + json.dumps(h.read_record(q, ctx.execution_id))
    assert h.SECRET not in blob


def test_get_execution_distinguishes_absent_unreadable_and_foreign_records(q):
    ctx = h.reserve(q, factory=h.ids())
    assert ctl.get_execution(q, MISSION, ctx.execution_id).status == "reserved"
    h.raises_code(ex.EXECUTION_NOT_FOUND, ctl.get_execution, q, MISSION, h.xid(404))
    h.raises_code(ex.EXECUTION_NOT_FOUND, ctl.get_execution, q, MISSION, "not-an-id")
    path = pathlib.Path(q) / "missions" / MISSION / "executions" / f"{ctx.execution_id}.json"
    path.write_text("{ not json")
    h.raises_code(ex.STATE_INVALID, ctl.get_execution, q, MISSION, ctx.execution_id)             # 空・「無い」に潰さない
    path.write_text(json.dumps({"schema_version": 1, "execution_id": h.xid(9)}))
    h.raises_code(ex.STATE_INVALID, ctl.get_execution, q, MISSION, ctx.execution_id)
    path.write_text("[1, 2]")
    h.raises_code(ex.STATE_INVALID, ctl.get_execution, q, MISSION, ctx.execution_id)


def test_the_controller_needs_a_transaction_and_a_consistent_caller():
    with pytest.raises(TypeError):
        ctl.reserve_task(object(), MISSION, "t001", AGENT, now=T0)
    with pytest.raises(ValueError):
        ctl.Caller("ex-" + "a" * 32, "none")
    with pytest.raises(ValueError):
        ctl.Caller(None, "flag")
    with pytest.raises(ValueError):
        ctl.Caller(None, "bogus")
    assert ctl.Caller("", "flag").presented is True                                         # 空文字は「名乗りなし」に倒さない


def test_domain_error_codes_and_exit_codes_match_the_design_table():
    assert ex.EXIT_CODES[ex.INVALID_TRANSITION] == 2                                         # 今の REFUSED_TRANSITION
    assert {c for c, n in ex.EXIT_CODES.items() if n == 3} == {
        ex.EXECUTION_NOT_FOUND, ex.EXECUTION_NOT_CURRENT, ex.EXECUTION_ALREADY_TERMINAL}
    assert all(ex.EXIT_CODES[c] == 1 for c in (ex.STATE_INVALID, ex.TASK_NOT_FOUND, ex.TASK_NOT_ELIGIBLE,
                                               ex.TASK_ALREADY_RESERVED, ex.GIT_POLICY_INVALID, ex.LOCK_FAILED))
    # pull の idle (exit 2) になりうる domain error は無い (memory: pull-exit-2-is-idle-usage-errors-must-be-1)
    assert 2 not in {ex.EXIT_CODES[c] for c in ex.ERROR_CODES if c != ex.INVALID_TRANSITION}
    # TRANSACTION_RECOVERY_REQUIRED は使わない (01a の回復は拒否を足さない)
    assert "TRANSACTION_RECOVERY_REQUIRED" not in ex.ERROR_CODES and "NO_TASK" not in ex.ERROR_CODES
    with pytest.raises(ValueError):
        ex.ControllerError("MADE_UP", "x")
    assert set(ex.EXIT_CODES) == set(ex.ERROR_CODES)


def test_slugify_is_the_same_formula_as_plan_sh_until_e2_deletes_the_copy():
    text = (h.REPO_ROOT / "scripts" / "plan.sh").read_text()
    for line in ("ascii_only = re.sub(r'[^\\x00-\\x7F]+', ' ', title)",
                 "normalized = re.sub(r'[^a-zA-Z0-9]+', ' ', ascii_only)",
                 "parts = [p.lower() for p in normalized.split() if p]",
                 "slug = '-'.join(parts)[:40].rstrip('-')",
                 "return slug or fallback"):
        assert line in text, line
    assert ex.slugify_title("E1: Task Controller + Execution の lib (呼び出し元ゼロ)", "t004") == "e1-task-controller-execution-lib"
    assert ex.slugify_title("x" * 80, "t1") == "x" * 40
    assert ex.slugify_title("a b  c-d_e", "t1") == "a-b-c-d-e"
