"""01c E3 fix 2 巡目 (t032 / PR #273 Codex P2): 「同じ内容の再送」で card・record・監査・rework_count・検証の記録が二重に増えない。

`verify-result fail` は以前、冪等の確認より前に rework_count を増やして経路を選んでいた (上限の手前の再送が mark_task で exit 3・
上限での再送が count と検証の記録を重複させる)。ここは各操作 × 境界 (手前・ちょうど・超えた後) の再送を固定する。
"""

from __future__ import annotations

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import MISSION, Box, audit_rows_for, report_argv, run, set_card, take


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def to_verification(box, xid):
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    assert run(box, *report_argv("verifying"), "--execution", xid, agent="verifier-dispatcher").returncode == 0


def verifications(box):
    return box.card_text().count("**Verdict:**")


def ok_rows(box):
    return [r for r in box.audit_rows() if r["result"] == "ok"]


def resend_changes_nothing(box, argv, xid, times=2):
    before, rows, n = box.snapshot(), len(ok_rows(box)), verifications(box)
    for _ in range(times):
        p = run(box, *argv, "--execution", xid, agent="V1")
        assert p.returncode == 0, (argv, p.stdout, p.stderr)
        assert "idempotent" in p.stdout
    assert box.snapshot() == before                       # card (rework_count・Verification 節を含む)・record・枠が同じ
    assert len(ok_rows(box)) == rows and verifications(box) == n


@pytest.mark.parametrize("rework,max_rework,label", [
    (1, 3, "one before the limit's boundary (count 1 -> 2)"),
    (0, 3, "far below"),
])
def test_fail_resend_below_the_limit_is_idempotent(box, rework, max_rework, label):
    set_card(box, "t001", rework_count=rework, max_rework=max_rework)
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-fail"), "--execution", xid, agent="V1").returncode == 0
    m = box.card()
    assert m["rework_count"] == rework + 1 and m["status"] == "pending" and m["execution_end_code"] == "VERIFICATION_REJECTED"
    resend_changes_nothing(box, report_argv("verify-fail"), xid)
    assert box.card()["rework_count"] == rework + 1 and verifications(box) == 1


def test_fail_resend_at_the_limit_does_not_increase_the_count_or_duplicate_the_record(box):
    set_card(box, "t001", rework_count=2, max_rework=3)       # この fail でちょうど上限 (3) に達する
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-fail"), "--execution", xid, agent="V1").returncode == 0
    m = box.card()
    assert (m["status"], m["rework_count"], m["execution_status"]) == ("needs_human_review", 3, "running")
    assert verifications(box) == 1
    resend_changes_nothing(box, report_argv("verify-fail"), xid, times=3)
    assert box.card()["rework_count"] == 3 and verifications(box) == 1


def test_fail_resend_past_the_limit_is_still_idempotent(box):
    set_card(box, "t001", rework_count=5, max_rework=3)       # 既に上限を超えている (手で直した等)
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-fail"), "--execution", xid, agent="V1").returncode == 0
    assert box.card()["status"] == "needs_human_review"
    resend_changes_nothing(box, report_argv("verify-fail"), xid)


def test_needs_human_review_verdict_resend_is_idempotent(box):
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-nhr"), "--execution", xid, agent="V1").returncode == 0
    assert verifications(box) == 1
    resend_changes_nothing(box, report_argv("verify-nhr"), xid)


def test_a_resend_with_a_wrong_id_is_still_refused_not_swallowed(box):
    set_card(box, "t001", rework_count=2, max_rework=3)
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-fail"), "--execution", xid, agent="V1").returncode == 0
    before = box.snapshot()
    for argv in (report_argv("verify-fail"), report_argv("verify-nhr")):
        p = run(box, *argv, "--execution", e3.OTHER_ID, agent="V1")
        assert p.returncode == 3 and e3.last_error_code(p.stderr) == "EXECUTION_NOT_CURRENT"
    assert box.snapshot() == before


def test_a_fail_after_human_review_below_the_limit_is_a_new_decision_not_a_resend(box):
    """nhr の verdict で人間の判断待ちになった後 (count は増えていない) の fail は新しい判定: 1 度だけ記録され、その再送は冪等。"""
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-nhr"), "--execution", xid, agent="V1").returncode == 0
    p = run(box, *report_argv("verify-fail"), "--execution", xid, agent="V1")
    assert p.returncode == 0 and "idempotent" not in p.stdout
    assert box.card()["status"] == "pending" and box.card()["rework_count"] == 1
    resend_changes_nothing(box, report_argv("verify-fail"), xid)


# --- 他の操作の再送の表 (族の掃除): どれも card・record・監査・枠が増えない -----------------------------------------

@pytest.mark.parametrize("command", ["done", "fail", "needs-director", "verify-pass"])
def test_resend_of_every_terminal_operation_changes_nothing(box, command):
    xid = take(box)
    if command == "verify-pass":
        to_verification(box, xid)
        agent = "V1"
    else:
        agent = "Ren"
    first = run(box, *report_argv(command), "--execution", xid, agent=agent)
    assert first.returncode == 0, first.stderr
    if command == "verify-pass":
        # verify-result pass は枠を残す (R-2 が次の呼び出しで消す。state-store.md §7)。最初の再送で回復が枠を片付けるので、
        # それを済ませてから「何も増えない」を比べる
        assert run(box, *report_argv(command), "--execution", xid, agent=agent).returncode == 0
    resend_changes_nothing(box, report_argv(command), xid, times=3)


def test_ready_for_verification_resend_is_refused_without_writing(box):
    """試行を動かさない遷移の再送は、status が既に進んでいるので exit 2 (INVALID_TRANSITION)。何も書かない (拒否の行だけ)。"""
    xid = take(box)
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    before, rows = box.snapshot(), len(ok_rows(box))
    p = run(box, *report_argv("ready-for-verification"), "--execution", xid)
    assert p.returncode == 2 and box.snapshot() == before and len(ok_rows(box)) == rows


def test_execution_count_and_attempt_do_not_move_on_resend(box):
    set_card(box, "t001", rework_count=0, max_rework=3)
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-fail"), "--execution", xid, agent="V1").returncode == 0
    count = box.card()["execution_count"]
    resend_changes_nothing(box, report_argv("verify-fail"), xid)
    assert box.card()["execution_count"] == count == 1
    assert len(box.records()) == 1


# --- t033 (Codex 3 巡目 P2): 再送の根拠は status / 回数の推測ではなく、この試行に結び付いた検証の記録 ----------------------

def test_first_nhr_verdict_after_a_manual_status_update_is_recorded_not_swallowed(box):
    xid = take(box)
    to_verification(box, xid)
    set_card(box, "t001", status="needs_human_review")      # `update --status needs_human_review` の結果と同じ card
    assert verifications(box) == 0
    p = run(box, *report_argv("verify-nhr"), "--notes", "first", "--execution", xid, agent="V1")
    assert p.returncode == 0 and "idempotent" not in p.stdout
    assert verifications(box) == 1 and "first" in box.card_text()
    resend_changes_nothing(box, report_argv("verify-nhr") + ["--notes", "first"], xid)


def test_a_different_nhr_verdict_after_a_fail_escalation_is_a_new_record(box):
    set_card(box, "t001", rework_count=2, max_rework=3)
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-fail"), "--notes", "bad", "--execution", xid, agent="V1").returncode == 0
    assert box.card()["status"] == "needs_human_review" and verifications(box) == 1
    p = run(box, *report_argv("verify-nhr"), "--notes", "human please", "--execution", xid, agent="V1")
    assert p.returncode == 0 and "idempotent" not in p.stdout
    assert verifications(box) == 2 and "human please" in box.card_text()
    resend_changes_nothing(box, report_argv("verify-nhr") + ["--notes", "human please"], xid)


def test_same_verdict_with_other_notes_is_a_new_record_when_escalated(box):
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-nhr"), "--notes", "a", "--execution", xid, agent="V1").returncode == 0
    p = run(box, *report_argv("verify-nhr"), "--notes", "b", "--execution", xid, agent="V1")
    assert p.returncode == 0 and "idempotent" not in p.stdout and verifications(box) == 2


def test_a_terminal_attempt_resend_with_other_notes_is_a_conflict_not_swallowed(box):
    set_card(box, "t001", rework_count=0, max_rework=3)
    xid = take(box)
    to_verification(box, xid)
    assert run(box, *report_argv("verify-fail"), "--notes", "a", "--execution", xid, agent="V1").returncode == 0
    before = box.snapshot()
    p = run(box, *report_argv("verify-fail"), "--notes", "b", "--execution", xid, agent="V1")
    assert p.returncode == 3 and box.snapshot() == before
    assert run(box, *report_argv("verify-fail"), "--notes", "a", "--execution", xid, agent="V1").returncode == 0
