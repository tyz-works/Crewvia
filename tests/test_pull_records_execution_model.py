"""モデルの段上げ PR-1: pull が Worker の記録 (`registry/workers/<Name>/model.json`) のモデルを card・record・台帳・
`previous_attempt`・status に写す。記録が無い・読めない Worker では欄を書かず、pull は止まらない。割り当ては変えない。

設計: knowledge/model-escalation.md §4.2・§6 (pull)・§8 (PR-1)。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import MISSION, Box, run, take

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lib_history as hist  # noqa: E402
import lib_worker_model as wm  # noqa: E402

TID = "t001"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def put_record(box, agent="Ren", **kw):
    reg = box.root / "registry"
    return wm.write_record(reg, agent, kw.get("source", "skill"), kw.get("model", "sonnet"), None)


def put_raw(box, text, agent="Ren"):
    p = box.root / "registry" / "workers" / agent / "model.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def ledger(box, kind_substr):
    d = box.queue / "missions" / MISSION / "history" / TID
    names = sorted(n for n in (p.name for p in d.iterdir()) if f"-{kind_substr}-" in n)
    return [hist.parse((d / n).read_text(), source=n)[0] for n in names]


def test_pull_copies_the_workers_model_to_card_record_and_json(box):
    put_record(box, model="sonnet")
    p = box.pull("Ren")
    assert p.returncode == 0, p.stderr
    res = json.loads(p.stdout)
    xid = res["execution_id"]
    assert res["execution_model"] == "sonnet"
    assert box.card()["execution_model"] == "sonnet"
    assert box.record(xid)["model"] == "sonnet"


def test_env_source_model_with_a_version_is_copied_as_is(box):
    put_record(box, model="claude-opus-4-1", source="env")
    res = json.loads(box.pull("Ren").stdout)
    assert res["execution_model"] == "claude-opus-4-1"
    assert box.card()["execution_model"] == "claude-opus-4-1"


@pytest.mark.parametrize("how", ["missing", "broken_json", "bad_shape", "blank_model", "model_null"])
def test_no_usable_record_writes_no_field_and_pull_still_works(box, how):
    agent = "Ren"
    if how == "broken_json":
        put_raw(box, "{")
    elif how == "bad_shape":
        put_raw(box, json.dumps({"agent": "Ren", "model": 5, "source": "env", "bound_task": None, "written_at": 1}))
    elif how == "blank_model":
        # 空白だけの model は Controller が INVALID_ARGUMENT で断るので、記録の検査で先に落とす (pull を止めない)
        put_raw(box, json.dumps({"agent": "Ren", "model": "  ", "source": "env", "bound_task": None, "written_at": 1}))
    elif how == "model_null":
        wm.write_record(box.root / "registry", "Ren", "none", None)
    p = box.pull(agent)
    assert p.returncode == 0, p.stderr
    res = json.loads(p.stdout)
    assert "execution_model" not in res
    assert "execution_model" not in box.card()
    assert "model" not in box.record(res["execution_id"]) or box.record(res["execution_id"])["model"] is None
    if how in ("broken_json", "bad_shape", "blank_model"):
        assert "execution_model を書きません" in p.stderr
    else:
        assert "execution_model" not in p.stderr
    # `unknown` のような値を作らない
    assert "unknown" not in box.card_text().lower().replace("unknown_", "")


def test_ledger_end_carries_the_closed_attempts_model_and_previous_attempt_shows_it(box):
    put_record(box, model="haiku")
    xid1 = take(box)
    p = run(box, "fail", TID, "--no-head", "x", "--mission", MISSION, "--execution", xid1, agent="Ren")
    assert p.returncode == 0, p.stderr
    ends = ledger(box, "end")
    assert [e["model"] for e in ends] == ["haiku"]
    box.reset()
    # 次の試行は別のモデルの Worker が取る → card の execution_model は新しい試行の分だけ (持ち越さない)
    put_record(box, "Hana", model="opus")
    q = box.pull("Hana")
    assert q.returncode == 0, q.stderr
    res = json.loads(q.stdout)
    assert res["execution_model"] == "opus"
    assert box.card()["execution_model"] == "opus"
    assert res["previous_attempt"]["model"] == "haiku"
    assert res["previous_attempt"]["execution_id"] == xid1


def test_a_new_reservation_does_not_inherit_the_previous_attempts_model(box):
    put_record(box, model="opus")
    xid = take(box)
    assert run(box, "fail", TID, "--no-head", "x", "--mission", MISSION, "--execution", xid, agent="Ren").returncode == 0
    box.reset()
    # 記録の無い Worker が取る: 前の試行の opus を写してはいけない
    q = box.pull("Hana")
    assert q.returncode == 0, q.stderr
    assert "execution_model" not in json.loads(q.stdout)
    assert "execution_model" not in box.card()


def test_retry_ledger_carries_the_model_too(box):
    put_record(box, model="sonnet")
    xid = take(box)
    assert run(box, "fail", TID, "--no-head", "x", "--mission", MISSION, "--execution", xid, agent="Ren").returncode == 0
    p = run(box, "retry", TID, "--reason", "やり直し", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    assert [e["model"] for e in ledger(box, "retry")] == ["sonnet"]
    # 再送 (同じ retry をもう一度) は pending なので拒否されるだけで、台帳は増えない
    assert len(ledger(box, "retry")) == 1


def test_old_ledger_without_model_still_compares_equal_on_resend(box):
    """モデルの記録が無い試行の台帳は `model` 欄なし。同じ報告の再送は一致 (HISTORY_MISMATCH にならない)。"""
    xid = take(box)
    a = run(box, "fail", TID, "--no-head", "x", "--mission", MISSION, "--execution", xid, agent="Ren")
    b = run(box, "fail", TID, "--no-head", "x", "--mission", MISSION, "--execution", xid, agent="Ren")
    assert a.returncode == 0 and b.returncode == 0, (a.stderr, b.stderr)
    ends = ledger(box, "end")
    assert len(ends) == 1 and "model" not in ends[0]


def test_status_shows_the_running_attempts_model_only_when_known(box):
    put_record(box, model="sonnet")
    take(box)
    out = run(box, "status", "--mission", MISSION).stdout
    line = [l for l in out.splitlines() if "t001" in l][0]
    assert " model=sonnet" in line and "attempt 1]" in line


def test_status_ignores_a_hand_edited_model_with_odd_characters(box):
    put_record(box, model="sonnet")
    take(box)
    p = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    text = p.read_text().replace("execution_model: sonnet", "execution_model: \"so\\x1b[31mnet\"")
    p.write_text(text)
    out = run(box, "status", "--mission", MISSION).stdout
    assert "\x1b" not in out and "model=" not in out


def test_status_has_no_model_for_a_worker_without_a_record(box):
    take(box)
    out = run(box, "status", "--mission", MISSION).stdout
    assert "model=" not in out


def test_assignment_is_unchanged_by_the_record(box):
    """PR-1 は書いて見せるだけ: どんな記録でも (opus の Worker が sonnet の task を・逆も) pull は同じに通る。"""
    for agent, model in (("Ren", "haiku"), ("Hana", "opus")):
        b = Box(box.root.parent / f"root-{agent}", tasks=("t001",))
        put_record(b, agent, model=model)
        assert b.pull(agent).returncode == 0
