"""Controller の「書いてから失敗する」「崩れた projection で落ちる」族 (vNext 01c E1 fix 2 巡目 / t029。PR #270 の P2×3)。

族 1: 拒否は何も書かない — Txn の最初の書き込みより前に、引数・ID・遷移・照合の検証がすべて終わっている。
      (拒否の**監査行**は書いてよい。それ以外 = card・record・assignment・identity は 1 バイトも変わらない。`snapshot` は audit/ を除く)
族 2: record の各欄が欠ける・型が違う・空のとき、判断 (start / complete / fail / 冪等)・回復・診断のどれも例外で落ちず、
      card から導くか finding にする (正本は card。record は判断に使わない)。

表 (§ execution.md §14.2):

| 操作 | 書き込み前に終わる検証 |
|---|---|
| reserve | agent・now・card の status・枠の持ち主・**候補 ID の形と衝突** (手順 0 の `_abandon` より前) |
| start | git_context・ID の形・NOT_FOUND / NOT_CURRENT / terminal |
| complete / fail / release | 引数・照合 (`_authorize`)・meta_updates・試行の遷移・**task の遷移 (`_abandon` より前)** |
| abandon_detached | DETACHED かつ active |
| mark | 引数・照合・task の遷移・meta_updates |
"""

from __future__ import annotations

import json
import pathlib

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, AGENT, OTHER, T0, T1, T2, ex, store, ctl

HOLD = store._ASSIGNMENT_HOLDING_STATUSES


@pytest.fixture
def q(tmp_path):
    return h.seed(tmp_path / "q", tids=("t001", "t002"))


def _record_path(q, execution_id):
    return pathlib.Path(q) / "missions" / MISSION / "executions" / f"{execution_id}.json"


def _edit_record(q, execution_id, fn):
    p = _record_path(q, execution_id)
    data = json.loads(p.read_text())
    fn(data)
    p.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")


def _detached(q):
    """X (running) の card を旧コードの reset が手放した DETACHED (b)・pending。"""
    first = h.reserve_and_start(q, factory=h.ids())
    h.old_code_reset(q)
    assert ex.attempt_view(h.read_meta(q), HOLD) == ex.DETACHED
    return first


# ---------------------------------------------------------------------------
# P2-2 / 族 1: 候補の ID を、最初の書き込みの前に検証する
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["ex-xyz", "", None, "EX-" + "a" * 32, "ex-%032x" % 1, "ex-%032x" % 7])
def test_a_bad_id_candidate_on_a_detached_card_writes_nothing_not_even_the_abandon(q, bad):
    """欠陥版: `_abandon` (card X → failed・record X → failed) を書いた後に INVALID_ARGUMENT を投げ、変更が残った。
    bad の最後の 2 つは「現在の試行 X と同じ ID」と「既に record がある ID」(衝突)。"""
    _detached(q)
    h.reserve(q, tid="t002", agent=OTHER, factory=h.ids(7))                          # xid(7) の record を作っておく
    before = h.snapshot(q)
    with h.tx(q) as t:
        e = h.raises_code(ex.INVALID_ARGUMENT, ctl.reserve_task, t, MISSION, "t001", AGENT, now=T1,
                          id_factory=lambda: bad)
    assert h.snapshot(q) == before                       # card・record・枠・identity のどれも変わらない
    assert h.read_meta(q)["execution_status"] == "running"                          # X は閉じられていない
    assert h.read_record(q, h.xid(1))["status"] == "running"
    assert "reported:stale_execution_status" not in [r["result"] for r in h.audit_rows(q)]
    assert str(bad) not in str(e) or bad in ("", None)                              # 値は出さない


def test_after_a_refused_candidate_the_same_reserve_still_works_and_closes_the_detached_attempt(q):
    first = _detached(q)
    with h.tx(q) as t:
        h.raises_code(ex.INVALID_ARGUMENT, ctl.reserve_task, t, MISSION, "t001", AGENT, now=T1, id_factory=lambda: "bad")
    second = h.reserve(q, agent=OTHER, now=T1, factory=h.ids(2))
    assert second.abandoned_execution_id == first.execution_id and second.attempt == 2


# ---------------------------------------------------------------------------
# 族 1: 全拒否コードで、拒否は何も書かない (audit/ を除く全ファイルの sha256 が同じ)
# ---------------------------------------------------------------------------

def _refusal_cases():
    """(名前, 場面を作る関数 → 引数なしの呼び出しを返す, 期待するコード)。場面は fresh な queue に作る。"""
    X, Y = h.xid(1), h.xid(77)

    def pending(q):
        return None

    def reserved(q):
        h.reserve(q, factory=h.ids())

    def running(q):
        h.reserve_and_start(q, factory=h.ids())

    def done(q):
        running(q)
        with h.tx(q) as t:
            ctl.complete_execution(t, MISSION, "t001", h.caller(X), to_status="done")

    def detached_finished(q):
        running(q)
        h.set_status(q, "done")

    def detached_pending(q):
        running(q)
        h.old_code_reset(q)

    def legacy_in_progress(q):
        h.seed(q, {"t001": (h.card("t001", status="in_progress", worker=AGENT, started_at=T0), h.body())})

    return [
        ("reserve:not-pending", reserved, lambda t: ctl.reserve_task(t, MISSION, "t001", AGENT, now=T0,
                                                                      id_factory=h.ids(2)), ex.TASK_ALREADY_RESERVED),
        ("reserve:done-card-not-pending", done, lambda t: ctl.reserve_task(t, MISSION, "t001", AGENT, now=T0,
                                                                          id_factory=h.ids(2)), ex.TASK_NOT_ELIGIBLE),
        ("reserve:bad-agent", pending, lambda t: ctl.reserve_task(t, MISSION, "t001", "", now=T0), ex.INVALID_ARGUMENT),
        ("reserve:bad-now", pending, lambda t: ctl.reserve_task(t, MISSION, "t001", AGENT, now="not a generation!"), ex.INVALID_ARGUMENT),
        ("reserve:bad-id", detached_pending, lambda t: ctl.reserve_task(t, MISSION, "t001", AGENT, now=T1,
                                                                        id_factory=lambda: "bad"), ex.INVALID_ARGUMENT),
        ("reserve:colliding-id", detached_pending, lambda t: ctl.reserve_task(t, MISSION, "t001", AGENT, now=T1,
                                                                              id_factory=lambda: X), ex.INVALID_ARGUMENT),
        ("start:not-found", legacy_in_progress, lambda t: ctl.start_execution(t, MISSION, "t001", X), ex.EXECUTION_NOT_FOUND),
        ("start:bad-id", reserved, lambda t: ctl.start_execution(t, MISSION, "t001", "nope"), ex.EXECUTION_NOT_FOUND),
        ("start:not-current", reserved, lambda t: ctl.start_execution(t, MISSION, "t001", Y), ex.EXECUTION_NOT_CURRENT),
        ("start:detached", detached_pending, lambda t: ctl.start_execution(t, MISSION, "t001", X), ex.EXECUTION_NOT_CURRENT),
        ("start:terminal", done, lambda t: ctl.start_execution(t, MISSION, "t001", X), ex.INVALID_TRANSITION),
        ("start:bad-git", reserved, lambda t: ctl.start_execution(t, MISSION, "t001", X, {"worktree": "rel"}),
         ex.INVALID_ARGUMENT),
        ("complete:not-current", running, lambda t: ctl.complete_execution(t, MISSION, "t001", h.caller(Y), to_status="done"),
         ex.EXECUTION_NOT_CURRENT),
        ("complete:from-reserved", reserved, lambda t: ctl.complete_execution(t, MISSION, "t001", h.caller(X), to_status="done"),
         ex.INVALID_TRANSITION),
        ("complete:conflict", done, lambda t: ctl.complete_execution(t, MISSION, "t001", h.caller(X), to_status="verified"),
         ex.EXECUTION_ALREADY_TERMINAL),
        ("complete:bad-to-status", running, lambda t: ctl.complete_execution(t, MISSION, "t001", h.caller(X), to_status="x"),
         ex.INVALID_ARGUMENT),
        ("complete:forbidden-update", running, lambda t: ctl.complete_execution(
            t, MISSION, "t001", h.caller(X), to_status="done", meta_updates={"status": "x"}), ex.INVALID_ARGUMENT),
        ("complete:not-found", legacy_in_progress, lambda t: ctl.complete_execution(
            t, MISSION, "t001", h.caller(X), to_status="done"), ex.EXECUTION_NOT_FOUND),
        ("fail:bad-code", running, lambda t: ctl.fail_execution(t, MISSION, "t001", h.caller(X), "NOPE", to_status="failed"),
         ex.INVALID_ARGUMENT),
        ("fail:bad-pair", running, lambda t: ctl.fail_execution(t, MISSION, "t001", h.caller(X), ex.WORKER_FAILED,
                                                                to_status="pending"), ex.INVALID_ARGUMENT),
        ("fail:g1-on-running", running, lambda t: ctl.fail_execution(
            t, MISSION, "t001", h.caller(X), ex.WORKSPACE_CREATE_FAILED, to_status="needs_director"), ex.INVALID_TRANSITION),
        ("fail:not-current", running, lambda t: ctl.fail_execution(t, MISSION, "t001", h.caller(Y), ex.WORKER_FAILED,
                                                                   to_status="failed"), ex.EXECUTION_NOT_CURRENT),
        ("release:running", running, lambda t: ctl.release_execution(t, MISSION, "t001", h.caller(X), ex.RETIRED),
         ex.INVALID_TRANSITION),
        ("release:bad-reason", reserved, lambda t: ctl.release_execution(t, MISSION, "t001", h.caller(X), "NOPE"),
         ex.INVALID_ARGUMENT),
        ("abandon:not-detached", running, lambda t: ctl.abandon_detached_execution(t, MISSION, "t001"),
         ex.INVALID_TRANSITION),
        ("abandon:already-closed", detached_finished, lambda t: (ctl.abandon_detached_execution(t, MISSION, "t001"),
                                                                 ctl.abandon_detached_execution(t, MISSION, "t001")),
         ex.INVALID_TRANSITION),
        ("mark:not-current", running, lambda t: ctl.mark_task(t, MISSION, "t001", h.caller(Y), command="ready-for-verification",
                                                             to_status="ready_for_verification"), ex.EXECUTION_NOT_CURRENT),
        ("mark:bad-command", running, lambda t: ctl.mark_task(t, MISSION, "t001", command="done", to_status="done"),
         ex.INVALID_ARGUMENT),
        ("mark:invalid-transition", pending, lambda t: ctl.mark_task(t, MISSION, "t001", command="ready-for-verification",
                                                                    to_status="ready_for_verification"),
         ex.INVALID_TRANSITION),
    ]


@pytest.mark.parametrize("name", [c[0] for c in _refusal_cases()])
def test_every_refusal_writes_nothing_but_the_audit_row(tmp_path, name):
    scene, call, code = next((c[1], c[2], c[3]) for c in _refusal_cases() if c[0] == name)
    q = h.seed(tmp_path / "q", tids=("t001", "t002"))
    scene(q)
    before = h.snapshot(q)
    pre_rows = len(h.audit_rows(q))
    with h.tx(q) as t:
        if name == "abandon:already-closed":                  # 1 回目は成功し、2 回目が拒否。1 回目の後を基準にする
            ctl.abandon_detached_execution(t, MISSION, "t001")
            before = None
            h.raises_code(code, ctl.abandon_detached_execution, t, MISSION, "t001")
        else:
            h.raises_code(code, call, t)
    if before is not None:
        assert h.snapshot(q) == before, name
    assert len(h.audit_rows(q)) >= pre_rows


# ---------------------------------------------------------------------------
# P2-1 / 族 2: record の欄が欠ける・型が違う・空 — 判断・回復・診断のどれも落ちない
# ---------------------------------------------------------------------------

#: (欄, 崩し方の名前, 崩す関数)。必須 (execution_id / mission / task / attempt / status / reserved_at / git) と任意の欄
def _delete(key):
    return lambda d: d.pop(key, None)


def _set(key, value):
    return lambda d: d.__setitem__(key, value)


#: 任意の欄 (agent / end_code / running_at / ended_at) の**欠け**は健全 (`record.get`)。型の違いだけが崩れ
MALFORMS = [(key, "missing", _delete(key)) for key in
            ("schema_version", "execution_id", "mission", "task", "attempt", "status", "reserved_at", "git")]
MALFORMS += [(key, f"type:{name}", _set(key, val)) for key in
             ("execution_id", "mission", "task", "attempt", "status", "reserved_at", "git", "agent", "end_code",
              "running_at", "ended_at")
             for name, val in (("list", []), ("dict", {}), ("int", 7), ("empty", ""))
             if not (key in ("agent", "end_code", "running_at", "ended_at") and name == "empty")
             and not (key == "git" and name == "dict")
             and not (key == "attempt" and name == "int")]
MALFORM_IDS = [f"{k}-{n}" for k, n, _ in MALFORMS]


@pytest.mark.parametrize("key,how,fn", MALFORMS, ids=MALFORM_IDS)
def test_a_malformed_current_record_never_breaks_complete_resend_and_diagnose_and_the_answer_comes_from_the_card(
        tmp_path, key, how, fn):
    """正本は card。record の `reserved_at` が欠けると `Execution.from_record` が KeyError だった (card の書き込みが
    確定した後に落ち、正当な冪等の再送も落とす)。どの欄が崩れても complete / 再送 / diagnose / recover は例外にならず、
    返す値の `reserved_at` / `status` は card から導かれる。崩れた record は上書きしない (報告だけ)。"""
    q = h.seed(tmp_path / "q", tids=("t001", "t002"))
    ctx = h.reserve_and_start(q, factory=h.ids())
    _edit_record(q, ctx.execution_id, fn)
    broken = _record_path(q, ctx.execution_id).read_text()

    with h.tx(q, op="done") as t:
        out = ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="done",
                                     meta_updates={"completed_at": T2})
    assert out.status == "completed" and out.reserved_at == T0 and out.idempotent is False
    assert out.execution_id == ctx.execution_id and out.attempt == 1 and out.mission == MISSION and out.task == "t001"
    meta = h.read_meta(q)
    assert meta["status"] == "done" and meta["execution_status"] == "completed"       # card は確定している
    assert _record_path(q, ctx.execution_id).read_text() == broken                     # 崩れた record を上書きしない

    with h.tx(q, op="done2") as t:                                                      # 正当な冪等の再送も落ちない
        again = ctl.complete_execution(t, MISSION, "t001", h.caller(ctx.execution_id), to_status="done")
    assert again.idempotent is True and again.status == "completed" and again.reserved_at == T0

    kinds = [f.kind for f in store.diagnose(q, store.Scope.everything(q))]              # store-check も落ちない
    assert isinstance(kinds, list)
    with h.tx(q, op="next", actor="test") as t:
        t.recover(store.Scope(cards=((MISSION, "t001"), (MISSION, "t002")), agents=(AGENT,)))   # 回復も落ちない
    assert _record_path(q, ctx.execution_id).read_text() == broken


@pytest.mark.parametrize("key,how,fn", [m for m in MALFORMS if m[0] in ("reserved_at", "status", "attempt", "git", "agent")],
                         ids=[i for i, m in zip(MALFORM_IDS, MALFORMS) if m[0] in ("reserved_at", "status", "attempt", "git", "agent")])
def test_start_and_fail_and_mark_survive_a_malformed_record_of_a_reserved_attempt(tmp_path, key, how, fn):
    q = h.seed(tmp_path / "q", tids=("t001", "t002"))
    ctx = h.reserve(q, factory=h.ids())
    _edit_record(q, ctx.execution_id, fn)
    with h.tx(q, op="pull2") as t:
        started = ctl.start_execution(t, MISSION, "t001", ctx.execution_id, {"worktree": "/abs/wt", "head_at_start": "abc"})
    assert started.status == "running" and started.reserved_at == T0
    assert h.read_meta(q)["execution_status"] == "running"
    with h.tx(q, op="start-again") as t:
        assert ctl.start_execution(t, MISSION, "t001", ctx.execution_id).idempotent is True
    with h.tx(q, op="fail") as t:
        failed = ctl.fail_execution(t, MISSION, "t001", h.caller(ctx.execution_id), ex.WORKER_FAILED, to_status="failed")
    assert failed.status == "failed" and failed.end_code == ex.WORKER_FAILED


def test_get_execution_reports_a_malformed_record_as_state_invalid_never_as_an_exception(tmp_path):
    for key, how, fn in MALFORMS:
        q = h.seed(tmp_path / f"q-{key}-{how}", tids=("t001",))
        ctx = h.reserve_and_start(q, factory=h.ids())
        _edit_record(q, ctx.execution_id, fn)
        if how == "type:empty" and key in ("agent",):
            continue
        try:
            ctl.get_execution(q, MISSION, ctx.execution_id)
        except ex.ControllerError as e:
            assert e.code == ex.STATE_INVALID, (key, how, e.code)
        # optional 欄の欠けは「健全」でもよい (返せる)。どちらでも例外で落ちないことが条件


# ---------------------------------------------------------------------------
# P2-3: store-check (diagnose) が、崩れた record 1 件で他の task の検査ごと止まらない
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [[], {}, 7, None, "", "bogus", ["running"], True], ids=repr)
def test_diagnose_reports_a_record_with_an_unhashable_or_unknown_status_and_keeps_checking_other_tasks(q, bad):
    """欠陥版: `got[1].get('status') in _ex.ACTIVE_STATUSES` が `status: []` / `{}` で TypeError (01a S2 の族)。
    他の task (t002 の DETACHED の報告) の検査まで止まった。"""
    first = h.reserve_and_start(q, factory=h.ids())
    other = h.reserve_and_start(q, tid="t002", agent=OTHER, factory=h.ids(5))
    h.old_code_reset(q, tid="t002", agent=OTHER)                                       # t002 を DETACHED (b) にする
    _edit_record(q, first.execution_id, _set("status", bad))
    findings = store.diagnose(q, store.Scope.everything(q))                            # 例外にならない
    kinds = [(f.kind, f.task) for f in findings]
    assert ("reported:execution_record_malformed", None) in [(k, None) for k, _t in kinds]
    assert any(k == "reported:execution_active_on_non_holding_status" and t == "t002" for k, t in kinds)
    assert all("bogus" not in repr(f) and "running" not in (f.detail or "") for f in findings)   # 値は出さない
    with h.tx(q, op="next", actor="test") as t:
        t.recover(store.Scope(cards=((MISSION, "t001"), (MISSION, "t002")), agents=(AGENT, OTHER)))
    assert other.execution_id


def test_a_malformed_record_finding_never_carries_the_record_content(q):
    ctx = h.reserve_and_start(q, factory=h.ids())
    _edit_record(q, ctx.execution_id, lambda d: d.update(status=[h.SECRET], agent={"k": h.SECRET}))
    findings = store.diagnose(q, store.Scope.everything(q))
    assert any(f.kind == "reported:execution_record_malformed" for f in findings)
    assert h.SECRET not in repr(findings)
    for p in (pathlib.Path(q) / "audit").glob("*.jsonl"):
        assert h.SECRET not in p.read_text()
