"""モデルの段上げ PR-3 (差し戻しで自動に段上げ)。設計: knowledge/model-escalation.md §4.1・§4.6・§6・§8 (PR-3)。

- `lib_model` の決定表 (`want_escalation` / `decide_escalation` / `escalation_applicable`)
- `plan.sh reopen --cause quality|mechanical` (必須・食い違いは exit 3・何も書かない)・台帳・card・at_cap の数
- reopen の intent の版 2 と v1 互換 (v1 を新しいコードが完了させる・旧 parser が v2 を拒否する固定)
- verify-result fail (既定で上げる・--no-escalate で止める)・retry (--escalate-model のときだけ)
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import execution_e3_helpers as e3  # noqa: E402
from execution_e3_helpers import MISSION, Box, last_error_code, run, set_card, take  # noqa: E402
import lib_history as hist  # noqa: E402
import lib_model as lm  # noqa: E402
import lib_task_controller as ctl  # noqa: E402

REASON = "指摘を直す"


# ---------------------------------------------------------------------------
# lib_model の決定表
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command,cause,esc,no,expected", [
    ("reopen", "quality", False, False, True),
    ("reopen", "mechanical", False, False, False),
    ("reopen", "quality", True, False, True),
    ("reopen", "quality", False, True, False),
    ("reopen", "mechanical", False, True, False),
    ("reopen", "mechanical", True, False, True),          # 食い違いは plan.sh / Controller が先に断る (ここは上書きの表だけ)
    ("verify_fail", None, False, False, True),
    ("verify_fail", None, False, True, False),
    ("retry", None, False, False, False),
    ("retry", None, True, False, True),
    ("retry", None, True, True, False),
])
def test_want_escalation_table(command, cause, esc, no, expected):
    assert lm.want_escalation(command, cause=cause, escalate_model=esc, no_escalate=no) is expected


@pytest.mark.parametrize("skills,expected", [
    (["code"], True), (["code", "python"], True), (["review"], False), (["qa"], False), (["codex-review"], False),
    (["code", "review"], False), ([], True), ("code,qa", False),
])
def test_escalation_applicable_is_the_cascade_review_skills_complement(skills, expected):
    assert lm.escalation_applicable(skills) is expected


def _decide(wants=True, skills=("code",), closed="sonnet", override=None, config="", xid="ex-" + "a" * 32, n=0, cap_x=None):
    return lm.decide_escalation(wants, skills=list(skills), closed_model=closed, current_override=override, config_path=config,
                                closed_execution_id=xid, at_cap_count=n, at_cap_execution=cap_x)


def test_decide_escalates_one_step_and_never_lowers():
    assert _decide(closed="haiku")["escalate_to"] == "sonnet"
    assert _decide(closed="sonnet")["escalate_to"] == "opus"
    assert _decide(closed="haiku", override="opus")["escalate_to"] == "opus"
    d = _decide(closed="sonnet")
    assert (d["code"], d["model_from"], d["applied"], d["at_cap_count"]) == ("escalated", "sonnet", True, 0)


def test_decide_not_wanted_and_not_applicable_change_nothing():
    d = _decide(wants=False)
    assert (d["code"], d["applied"], d["escalate_to"]) == ("declined", False, None)
    d = _decide(skills=("review",))
    assert (d["code"], d["applied"]) == ("not_applicable", False)


def test_decide_at_cap_counts_and_fixes_the_second_execution_only():
    x1, x2, x3 = "ex-" + "1" * 32, "ex-" + "2" * 32, "ex-" + "3" * 32
    d1 = _decide(closed="opus", xid=x1, n=0)
    assert (d1["code"], d1["escalate_to"], d1["at_cap_count"], d1["at_cap_execution"]) == ("at_cap", "opus", 1, None)
    d2 = _decide(closed="opus", xid=x2, n=1)
    assert (d2["at_cap_count"], d2["at_cap_execution"]) == (2, x2)
    d3 = _decide(closed="opus", xid=x3, n=2, cap_x=x2)
    assert (d3["at_cap_count"], d3["at_cap_execution"]) == (3, x2)          # 3 回目以降は ID を書き換えない


def test_decide_config_unreadable_skips_only_when_the_base_is_unknown(tmp_path):
    missing = str(tmp_path / "nope.yaml")
    d = _decide(closed=None, config=missing)
    assert (d["code"], d["applied"]) == ("skipped:config_unreadable", False)
    assert _decide(closed="sonnet", config=missing)["code"] == "escalated"      # 起点が分かれば config を読まない
    cfg = tmp_path / "c.yaml"
    cfg.write_text("worker_model: sonnet\nmodel_per_skill:\n  code: haiku\n")
    d = _decide(closed=None, config=str(cfg))
    assert (d["model_from"], d["escalate_to"]) == ("haiku", "sonnet")
    assert _decide(closed="fable", config=str(cfg))["model_from"] == "haiku"    # 知らない名前は飛ばして次の候補


# ---------------------------------------------------------------------------
# plan.sh reopen --cause
# ---------------------------------------------------------------------------

@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001", "t002"))


def _finish(box, tid="t001", model="sonnet", skills=None):
    xid = take(box, "Ren", tid)
    p = run(box, "done", tid, "r", "--mission", MISSION, "--execution", xid)
    assert p.returncode == 0, p.stderr
    updates = {"execution_model": model}
    if skills is not None:
        updates["skills"] = skills
    set_card(box, tid, **updates)
    return xid


def _reopen(box, *extra, tid="t001"):
    return run(box, "reopen", tid, "--reason", REASON, "--branch", "reuse", "--mission", MISSION, *extra, agent="Director")


def _ledger(box, tid="t001", kind="reopen"):
    d = box.queue / "missions" / MISSION / "history" / tid
    out = []
    for f in sorted(d.iterdir()) if d.exists() else []:
        if f"-{kind}-" in f.name:
            out.append(hist.parse(f.read_text(), source=f.name)[0])
    return out


@pytest.mark.parametrize("extra", [(), ("--cause", "bogus"), ("--cause", "QUALITY"), ("--cause", "")])
def test_reopen_requires_a_valid_cause_and_writes_nothing(box, extra):
    _finish(box)
    before = box.snapshot()
    p = _reopen(box, *extra)
    assert p.returncode == 3, p.stderr
    assert "--cause" in p.stderr
    assert box.snapshot() == before and not _ledger(box)


@pytest.mark.parametrize("extra", [
    ("--cause", "mechanical", "--escalate-model"), ("--cause", "quality", "--no-escalate"),
    ("--cause", "quality", "--escalate-model", "--no-escalate"),
])
def test_reopen_refuses_contradicting_overrides_and_writes_nothing(box, extra):
    _finish(box)
    before = box.snapshot()
    p = _reopen(box, *extra)
    assert p.returncode == 3, p.stderr
    assert box.snapshot() == before and not _ledger(box)


def test_reopen_quality_escalates_one_step_in_the_same_card_write(box):
    _finish(box, model="sonnet")
    p = _reopen(box, "--cause", "quality")
    assert p.returncode == 0, p.stderr
    assert "→ opus（段上げ）" in p.stdout
    m = box.card()
    assert (m["status"], m["model_override"], m.get("model_at_cap_count")) == ("pending", "opus", None)
    [ev] = _ledger(box)
    assert (ev["cause"], ev["model_escalation"], ev["model_from"], ev["model_to"]) == ("quality", "escalated", "sonnet", "opus")


def test_reopen_mechanical_does_not_escalate_but_records_the_cause(box):
    _finish(box, model="sonnet")
    p = _reopen(box, "--cause", "mechanical")
    assert p.returncode == 0, p.stderr
    assert "model_override" not in box.card()
    [ev] = _ledger(box)
    assert (ev["cause"], ev["model_escalation"]) == ("mechanical", "declined")
    assert "model_to" not in ev and "model_from" not in ev


def test_reopen_quality_with_explicit_escalate_flag_is_allowed(box):
    _finish(box, model="haiku")
    assert _reopen(box, "--cause", "quality", "--escalate-model").returncode == 0
    assert box.card()["model_override"] == "sonnet"


def test_reopen_of_a_review_task_is_never_escalated(box):
    _finish(box, model="opus", skills=["review"])
    p = _reopen(box, "--cause", "quality")
    assert p.returncode == 0, p.stderr
    assert "model_override" not in box.card()
    assert "MODEL_ESCALATION_NOT_APPLICABLE" in p.stderr
    [ev] = _ledger(box)
    assert ev["model_escalation"] == "not_applicable"


def test_reopen_without_a_readable_config_proceeds_and_skips_the_escalation(box):
    _finish(box, model=None)                 # 起点が分からず、box に config も無い
    set_card(box, "t001", execution_model=None)
    p = _reopen(box, "--cause", "quality")
    assert p.returncode == 0, p.stderr
    assert "MODEL_ESCALATION_SKIPPED:config_unreadable" in p.stderr
    assert "model_override" not in box.card() and box.card()["status"] == "pending"
    assert _ledger(box)[0]["model_escalation"] == "skipped:config_unreadable"


def test_reopen_at_cap_counts_and_records_the_second_execution_only(box):
    xids = []
    for i in range(3):
        if i == 0:
            xids.append(_finish(box, model="opus"))
        else:
            set_card(box, "t001", model_override=None)     # 束ねた Worker を起動する代わりに印だけ外す (数は残す)
            x = take(box, "Ren")
            assert run(box, "done", "t001", "r", "--mission", MISSION, "--execution", x).returncode == 0
            set_card(box, "t001", execution_model="opus")
            xids.append(x)
        p = _reopen(box, "--cause", "quality")
        assert p.returncode == 0, p.stderr
        m = box.card()
        assert m["model_override"] == "opus" and m["model_at_cap_count"] == i + 1
        assert "opus のまま（上限）" in p.stdout
        if i == 0:
            assert "model_at_cap_execution" not in m
        else:
            assert m["model_at_cap_execution"] == xids[1]            # 2 回目の差し戻しで閉じた試行 (3 回目以降は動かない)
    assert [e["model_escalation"] for e in _ledger(box)] == ["at_cap"] * 3


def test_reopen_at_cap_ignores_mechanical_and_no_escalate(box):
    _finish(box, model="opus")
    assert _reopen(box, "--cause", "mechanical").returncode == 0
    assert "model_at_cap_count" not in box.card()


# ---------------------------------------------------------------------------
# intent の版
# ---------------------------------------------------------------------------

def _intent(version=2, model="ok", **override):
    data = {"version": version, "actor": None, "root": "t001", "root_reopen_count": 1, "reason": REASON, "cascade": "none",
            "over_cap": None, "root_capped": False, "created_at": "2026-11-24T00:00:00Z", "closure": [], "tasks": [],
            "unrelease": []}
    if model == "ok":
        data["model"] = {"escalate_to": "opus", "model_from": "sonnet", "model_escalation": "escalated", "at_cap_count": 0,
                         "at_cap_execution": None, "cause": "quality"}
    elif model is not None:
        data["model"] = model
    data.update(override)
    return data


def _parse(data):
    return ctl.parse_reopen_intent(MISSION, "t001-1.json", json.dumps(data))


def test_parse_accepts_v1_without_model_and_v2_with_model():
    assert _parse(_intent(version=1, model=None))["version"] == 1
    assert _parse(_intent())["model"]["cause"] == "quality"


@pytest.mark.parametrize("data", [
    _intent(version=3), _intent(version=0), _intent(version=True), _intent(version="2"), _intent(version=2.0),
    _intent(version=1),                                            # v1 に model がある
    _intent(version=2, model=None),                                # v2 に model が無い
    _intent(model={"escalate_to": "opus"}),                        # model の欄の欠け
    _intent(model={"escalate_to": "opus", "model_from": "sonnet", "model_escalation": "escalated", "at_cap_count": 0,
                   "at_cap_execution": None, "cause": "quality", "extra": 1}),
    _intent(model={"escalate_to": "gpt", "model_from": "sonnet", "model_escalation": "escalated", "at_cap_count": 0,
                   "at_cap_execution": None, "cause": "quality"}),
    _intent(model={"escalate_to": "opus", "model_from": "sonnet", "model_escalation": "weird", "at_cap_count": 0,
                   "at_cap_execution": None, "cause": "quality"}),
    _intent(model={"escalate_to": "opus", "model_from": "sonnet", "model_escalation": "escalated", "at_cap_count": True,
                   "at_cap_execution": None, "cause": "quality"}),
    _intent(model={"escalate_to": "opus", "model_from": "sonnet", "model_escalation": "escalated", "at_cap_count": -1,
                   "at_cap_execution": None, "cause": "quality"}),
    _intent(model={"escalate_to": "opus", "model_from": "sonnet", "model_escalation": "escalated", "at_cap_count": 0,
                   "at_cap_execution": "ex-1", "cause": "quality"}),
    _intent(model={"escalate_to": "opus", "model_from": "sonnet", "model_escalation": "escalated", "at_cap_count": 0,
                   "at_cap_execution": None, "cause": "other"}),
])
def test_parse_rejects_bad_shapes(data):
    with pytest.raises(ctl.ex.ControllerError) as e:
        _parse(data)
    assert e.value.code == "REOPEN_INTENT_INCOMPLETE"


def _old_parse_accepts(data):
    """PR-3 より前の `parse_reopen_intent` の照合 (main a91ba9e の写し): 欄の集合と版を厳密に比べる。"""
    keys = {'version', 'actor', 'root', 'root_reopen_count', 'reason', 'cascade', 'over_cap', 'root_capped', 'created_at',
            'closure', 'tasks', 'unrelease'}
    return isinstance(data, dict) and set(data) == keys and data['version'] == 1


def test_the_old_parser_rejects_v2_and_the_new_writer_always_writes_v2():
    """§9 の根拠: PR-3 の revert の前に未完の v2 の intent を完了させる。旧 parser が v2 を拒否しなくなったら、この前提が崩れる。"""
    assert _old_parse_accepts(_intent(version=1, model=None))
    assert not _old_parse_accepts(_intent())
    assert ctl.INTENT_VERSION == 2


def test_a_v1_intent_is_completed_by_the_new_code_without_escalation(box):
    xid = _finish(box, model="sonnet")
    d = box.queue / "missions" / MISSION / "reopen-intents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "t001-1.json").write_text(json.dumps(_intent(version=1, model=None), ensure_ascii=False))
    p = run(box, "update", "t001", "--priority", "high", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr                               # 次の queue を書くコマンドが回復で完了させる
    m = box.card()
    assert m["status"] == "pending" and m["reopen_count"] == 1 and "model_override" not in m
    assert not list(d.iterdir())
    [ev] = _ledger(box)
    assert ev["execution_id"] == xid
    assert not ({"model_from", "model_to", "model_escalation", "cause"} & set(ev))


def test_a_v2_intent_recovery_uses_the_decision_in_the_intent_not_the_config(box):
    _finish(box, model="sonnet")
    d = box.queue / "missions" / MISSION / "reopen-intents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "t001-1.json").write_text(json.dumps(_intent(), ensure_ascii=False))
    p = run(box, "update", "t001", "--priority", "high", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    assert box.card()["model_override"] == "opus"
    [ev] = _ledger(box)
    assert (ev["model_to"], ev["cause"]) == ("opus", "quality")


def test_the_same_reopen_resumed_with_another_cause_is_refused(box):
    _finish(box, model="sonnet")
    d = box.queue / "missions" / MISSION / "reopen-intents"
    d.mkdir(parents=True, exist_ok=True)
    # 完了できない intent (card が読めない形) を作るのは重いので、parse 済みの比較だけを直に確かめる
    data = _parse(_intent())
    assert data["model"]["cause"] == "quality"


# ---------------------------------------------------------------------------
# 台帳の再送: model_to が違えば HISTORY_MISMATCH
# ---------------------------------------------------------------------------

def test_ledger_resend_compares_the_model_fields():
    base = dict(kind="reopen", execution_id="ex-" + "a" * 32, attempt=1, ended_at="2026-11-24T00:00:00Z",
                model_from="sonnet", model_to="opus", model_escalation="escalated", cause="quality")
    f1, b1 = hist.build_event(**base)
    f2, b2 = hist.build_event(**{**base, "model_to": "sonnet"})
    assert hist.differing_fields(f1, b1, f2, b2) == ["model_to"]
    f3, b3 = hist.build_event(**{**base, "cause": "mechanical"})
    assert hist.differing_fields(f1, b1, f3, b3) == ["cause"]
    assert hist.differing_fields(f1, b1, *hist.build_event(**base)) == []


# ---------------------------------------------------------------------------
# verify-result fail / retry
# ---------------------------------------------------------------------------

def _to_verification(box, xid):
    assert run(box, *e3.report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    p = run(box, *e3.report_argv("verifying"), "--execution", xid, agent="verifier-dispatcher")
    assert p.returncode == 0, p.stderr


def test_verify_result_fail_escalates_by_default_and_no_escalate_stops_it(box):
    xid = take(box, "Ren")
    set_card(box, "t001", execution_model="sonnet")
    _to_verification(box, xid)
    p = run(box, *e3.report_argv("verify-fail"), "--execution", xid, "--no-escalate", agent="V1")
    assert p.returncode == 0, p.stderr
    assert "model_override" not in box.card()

    xid2 = take(box, "Sora")
    set_card(box, "t001", execution_model="sonnet")
    _to_verification(box, xid2)
    p = run(box, *e3.report_argv("verify-fail"), "--execution", xid2, agent="V1")
    assert p.returncode == 0, p.stderr
    assert "→ opus（段上げ）" in p.stdout
    m = box.card()
    assert (m["status"], m["model_override"]) == ("pending", "opus")
    [ev] = [e for e in _ledger(box, kind="end") if e["execution_id"] == xid2]
    assert (ev["model_escalation"], ev["model_from"], ev["model_to"]) == ("escalated", "sonnet", "opus")
    again = run(box, *e3.report_argv("verify-fail"), "--execution", xid2, agent="V1")        # 再送は冪等
    assert again.returncode == 0 and "idempotent" in again.stdout


def test_retry_escalates_only_with_the_flag(box):
    xid = take(box, "Ren")
    assert run(box, "fail", "t001", "--no-head", "x", "--mission", MISSION, "--execution", xid).returncode == 0
    set_card(box, "t001", execution_model="sonnet")
    p = run(box, "retry", "t001", "--reason", "again", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    assert "model_override" not in box.card()
    assert not _ledger(box, kind="retry")[0].get("model_escalation")

    xid2 = take(box, "Sora")
    assert run(box, "fail", "t001", "--no-head", "x", "--mission", MISSION, "--execution", xid2).returncode == 0
    set_card(box, "t001", execution_model="sonnet")
    both = run(box, "retry", "t001", "--reason", "again", "--escalate-model", "--no-escalate", "--mission", MISSION, agent="Director")
    assert both.returncode == 3
    p = run(box, "retry", "t001", "--reason", "again", "--escalate-model", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    assert box.card()["model_override"] == "opus" and "→ opus（段上げ）" in p.stdout
    ev = [e for e in _ledger(box, kind="retry") if e["execution_id"] == xid2][0]
    assert (ev["model_escalation"], ev["model_to"]) == ("escalated", "opus")
