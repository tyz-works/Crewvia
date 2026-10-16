"""台帳に書く値は読み戻して同じになる / 壊れた record・card の欄で store-check と retry が落ちない (PR-E fix 1 / t034)。

* 台帳の読み手は `str.splitlines()` で行を割る。行区切りとみなされる文字 (\\n \\r \\v \\f \\x1c-\\x1e \\x85 U+2028 U+2029) が
  値に混じると、書いた台帳が読めない。「検査を通った値は読み戻すと同じ」「通らない値は何も書かない」を全文字で固定する。
* `orphan_reason` は過去の record / card の欄を集合と照合する前に型を見る (`"status": []` で TypeError にならない)。
"""

from __future__ import annotations

import pytest

import execution_e3_helpers as e3  # noqa: F401  (sys.path を整える)
from execution_e3_helpers import MISSION, Box, run, set_card, take

import lib_history as hist  # noqa: E402
import lib_task_controller as ctl  # noqa: E402
import lib_execution as ex  # noqa: E402

XID = "ex-" + "a" * 32
OTHER = "ex-" + "b" * 32

#: `str.splitlines()` が行区切りとみなす文字を実装から導く (一覧を手で書かない)。範囲は BMP の先頭 + 念のための端。
LINE_BREAKERS = [chr(i) for i in list(range(0x3000)) + [0xFEFF, 0xFFFF, 0x10000, 0x10FFFF]
                 if len(("a" + chr(i) + "b").splitlines()) != 1]
TEXT_FIELDS = ("end_code", "head", "worker", "actor", "reason", "handoff_path", "over_cap", "checkpoint_dir")


def test_the_oracle_finds_all_the_known_separators():
    assert {"\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " "} <= set(LINE_BREAKERS)


def _event(**over):
    base = dict(kind="end", execution_id=XID, attempt=1, ended_at="2026-10-16T00:00:00Z", end_code="WORKER_DONE")
    base.update(over)
    return hist.build_event(**base)


def test_check_reason_rejects_every_line_breaker():
    """値の中に混じった区切りは全部断る (前後の区切りは `strip()` で消えるので、それは通ってよい)。"""
    for ch in LINE_BREAKERS:
        with pytest.raises(ex.ControllerError) as e:
            ctl.check_reason(f"a{ch}b", "reason")
        assert e.value.code == ex.INVALID_ARGUMENT, repr(ch)


def test_check_reason_accepted_values_survive_a_ledger_round_trip():
    """検査を通る値 (全文字を 1 字ずつ) は、台帳に書いて読み戻すと同じ値になる。"""
    for i in list(range(0x3000)) + [0xFEFF, 0xFFFF, 0x10000]:
        value = f"a{chr(i)}b"
        try:
            checked = ctl.check_reason(value, "reason")
        except ex.ControllerError:
            continue
        fields, body = _event(kind="retry", reason=checked, over_cap=checked)
        back, _ = hist.parse(hist.render(fields, body))
        assert back["reason"] == checked and back["over_cap"] == checked, repr(chr(i))


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_render_never_writes_a_ledger_that_cannot_be_read_back(field):
    """どの文字列欄でも、行区切りが混じった値は書く前に `HistoryError`、通った値は読み戻して同じ。"""
    for ch in LINE_BREAKERS:
        fields, body = _event(**{field: f"a{ch}b"})
        try:
            text = hist.render(fields, body)
        except hist.HistoryError:
            continue                                   # 書かない
        back, _ = hist.parse(text)
        assert back[field] == f"a{ch}b", (field, repr(ch))


def test_render_rejects_a_separator_with_a_fixed_code_and_no_value():
    fields, body = _event(reason="SECRET tail")
    with pytest.raises(hist.HistoryError) as e:
        hist.render(fields, body)
    assert e.value.code == "not_roundtrip" and "SECRET" not in str(e.value)


@pytest.mark.parametrize("ch", LINE_BREAKERS)
def test_ledger_reason_of_needs_director_and_fail_folds_every_separator(ch):
    for end_code, out in ((ex.NEEDS_DIRECTOR, {"needs_director_reason": f"x{ch}y{ch}z"}),
                          (ex.WORKER_FAILED, {"fail_head_waiver": f"x{ch}y", "handoff_path": "/h"})):
        reason = ctl._ledger_reason(out, end_code)
        assert reason is not None
        fields, body = _event(reason=reason)
        back, _ = hist.parse(hist.render(fields, body))
        assert back["reason"] == reason


def test_a_result_body_with_separators_does_not_make_a_resend_mismatch():
    result = "line1 line2\x85line3\x0cend"
    fields, body = _event(result_state=hist.RESULT_RECORDED, result_text=result)
    back, back_body = hist.parse(hist.render(fields, body))
    assert hist.differing_fields(back, back_body, fields, body) == []


# ---------------------------------------------------------------------------
# retry のコマンド経由: 何も書かない
# ---------------------------------------------------------------------------

@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


@pytest.mark.parametrize("ch", ["\u0085", " ", " ", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"])
@pytest.mark.parametrize("opt", ["--reason", "--over-cap"])
def test_retry_refuses_a_separator_and_writes_nothing(box, ch, opt):
    xid = take(box)
    assert run(box, "fail", "t001", "--no-head", "x", "--mission", MISSION, "--execution", xid, agent="Ren").returncode == 0
    before = box.snapshot()
    args = ["retry", "t001", "--reason", "ok", "--mission", MISSION]
    if opt == "--reason":
        args[3] = f"a{ch}b"
    else:
        args += ["--over-cap", f"a{ch}b"]
    p = run(box, *args, agent="Director")
    assert p.returncode != 0
    assert box.snapshot() == before and box.card()["status"] == "failed"


@pytest.mark.parametrize("bad", [[], {}, True, 7, "x"])
@pytest.mark.parametrize("field", ["fail_head", "handoff_path", "worker", "pr_number"])
def test_retry_with_a_mistyped_evidence_field_fails_cleanly(box, field, bad):
    """証拠の写し元 (card の欄) の型が壊れていても、retry は固定の終わり方 (成功か制御された拒否) で、何も壊さない。"""
    xid = take(box)
    assert run(box, "fail", "t001", "--no-head", "x", "--mission", MISSION, "--execution", xid, agent="Ren").returncode == 0
    set_card(box, **{field: bad})
    before = box.snapshot()
    p = run(box, "retry", "t001", "--reason", "r", "--mission", MISSION, agent="Director")
    assert "Traceback" not in p.stderr, p.stderr
    if p.returncode != 0:
        assert box.snapshot() == before                 # 拒否なら何も書かない
    else:
        for name in [n for n in (box.queue / "missions" / MISSION / "history" / "t001").iterdir()]:
            hist.parse(name.read_text())                # 書いた台帳は全部読める


# ---------------------------------------------------------------------------
# orphan_reason: 壊れた record / card の欄
# ---------------------------------------------------------------------------

class _Txn:
    def __init__(self, record):
        self._record = record

    def read_execution_record(self, slug, xid):
        return ("ok", self._record)


def _record(**over):
    rec = {"schema_version": ex.RECORD_SCHEMA_VERSION, "execution_id": OTHER, "mission": MISSION, "task": "t001",
           "attempt": 1, "status": "failed", "end_code": "WORKER_FAILED", "reserved_at": "2026-10-16T00:00:00Z",
           "git": {}}
    rec.update(over)
    return rec


def _ledger_fields(**over):
    f = {"execution_id": OTHER, "attempt": 1, "kind": "end", "end_code": "WORKER_DONE",
         "ended_at": "2026-10-16T00:00:00Z", "reopen_count": 0, "result": "none"}
    f.update(over)
    return f


META = {"current_execution_id": XID, "reopen_count": 0, "status": "pending", "execution_status": "reserved"}
BAD_VALUES = [[], {}, True, 7, None, 1.5]


def test_orphan_reason_still_reports_a_healthy_closed_by_record():
    assert hist.orphan_reason(_Txn(_record()), MISSION, "t001", META, _ledger_fields()) == "end:closed_by=WORKER_FAILED"


@pytest.mark.parametrize("bad", BAD_VALUES)
@pytest.mark.parametrize("field", ["status", "end_code", "reserved_at", "attempt", "git", "execution_id", "agent"])
def test_orphan_reason_skips_a_record_with_a_mistyped_field(field, bad):
    rec = _record(**{field: bad})
    if field == "agent" and (bad is None):
        pytest.skip("agent は null が正常")
    result = hist.orphan_reason(_Txn(rec), MISSION, "t001", META, _ledger_fields())
    assert result is None or isinstance(result, str)
    if ex.record_shape_problem(rec):
        assert result is None


@pytest.mark.parametrize("bad", ["x", [], {}, 3, "", ["failed"]])
def test_orphan_reason_skips_a_record_that_is_not_an_object(bad):
    assert hist.orphan_reason(_Txn(bad), MISSION, "t001", META, _ledger_fields()) is None


@pytest.mark.parametrize("bad", BAD_VALUES)
@pytest.mark.parametrize("field", ["status", "execution_status", "execution_end_code", "reopen_count"])
@pytest.mark.parametrize("kind", ["end", "retry"])
def test_orphan_reason_survives_a_mistyped_card_field(kind, field, bad):
    meta = dict(META, status="failed", execution_status="failed")
    meta[field] = bad
    f = _ledger_fields(execution_id=XID, kind=kind)
    hist.orphan_reason(_Txn(_record()), MISSION, "t001", meta, f)      # 例外にならない


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_store_check_survives_a_broken_past_record_end_to_end(box, bad):
    """過去の試行の record の `status` を壊しても store-check は落ちない (Traceback なし)。"""
    xid = take(box)
    assert run(box, "fail", "t001", "--no-head", "x", "--mission", MISSION, "--execution", xid, agent="Ren").returncode == 0
    assert run(box, "retry", "t001", "--reason", "r", "--mission", MISSION, agent="Director").returncode == 0
    take(box, "Sora")                                                   # 次の試行が current。xid は過去の試行
    path = box.queue / "missions" / MISSION / "executions" / f"{xid}.json"
    import json
    rec = json.loads(path.read_text())
    rec["status"] = bad
    path.write_text(json.dumps(rec))
    r = box.plan("store-check", "--mission", MISSION)
    assert "Traceback" not in r.stderr + r.stdout, r.stderr


def test_a_reason_cut_at_the_limit_never_ends_with_a_space():
    """200 字の切り口が空白だと、frontmatter は前後の空白を落とすので読み戻した値が変わる (再送が食い違いになる)。"""
    for n in range(190, 210):
        raw = "あ " * n
        reason = ctl._ledger_reason({"needs_director_reason": raw}, ex.NEEDS_DIRECTOR)
        assert reason == reason.strip() and len(reason) <= ctl._LEDGER_REASON_MAX
        fields, body = _event(reason=reason)
        assert hist.parse(hist.render(fields, body))[0]["reason"] == reason
