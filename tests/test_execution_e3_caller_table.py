"""01c E3 (t012): done / fail / needs-director / ready-for-verification / verifying / verify-result が Execution ID を照合する。

設計: `knowledge/execution.md` §4.3 (狭める遷移)・§4.4 (冪等 / conflict)・§5.1〜5.3 (呼び出し元ごとの表)・§8 (監査)。受入条件:

1. **呼び出し元ごとの表 (§5.2 / §5.3) を 1 行ずつ**: 通るべき経路が通る・拒否すべき経路が exit で拒否され**何も書かない**
2. `--execution ""` (空の明示指定) は名乗りなしに倒さず exit 1 (retire `--execution ""` と同じ。01b G3)
3. 狭めた遷移 (§4.3) は exit 2・何も書かない。今の運用 (Director が開いた card の done・kai-review・verifier) は通る
4. `verify-result fail` は新しい試行 (attempt + 1)
5. 拒否の行・`caller_check`・`actor` (§8)。名乗られた値・card の中身は stderr / 監査ログに出ない

**本番の queue / registry / mux には触れない** (`execution_e3_helpers` が `pull_execution_helpers.Box` で隔離する)。
"""

from __future__ import annotations

import json

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import (MISSION, OTHER_ID, XID_RE, Box, audit_rows_for, last_error_code, legacy_in_progress,
                                  new_card, refusals, report_argv, run, set_card, take)


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001", "t002", "t003"))


# 報告コマンドごとの「通った後の試行」(execution_status, end_code) と task の status
OUTCOME = {
    "done": ("completed", "DONE", "done"),
    "fail": ("failed", "WORKER_FAILED", "failed"),
    "needs-director": ("failed", "NEEDS_DIRECTOR", "needs_director"),
    "ready-for-verification": ("running", None, "ready_for_verification"),
}
REPORTS = list(OUTCOME)


def card_outcome(box, tid="t001"):
    m = box.card(tid)
    return m["execution_status"], m.get("execution_end_code"), m["status"]


# ---------------------------------------------------------------------------
# 1. 呼び出し元ごとの表 (§5.2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", REPORTS)
def test_row_active_and_the_right_id_by_flag_passes_and_is_audited_as_verified(box, command):
    xid = take(box)
    p = run(box, *report_argv(command), "--execution", xid)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert card_outcome(box) == OUTCOME[command]
    row = audit_rows_for(box, command)[0]
    assert row["caller_check"] == "verified" and row["execution_id"] == xid
    assert box.record(xid)["status"] == OUTCOME[command][0]            # record は card の写し


@pytest.mark.parametrize("command", REPORTS)
def test_row_active_and_the_right_id_by_env_passes(box, command):
    xid = take(box)
    p = run(box, *report_argv(command), env={"CREWVIA_EXECUTION_ID": xid})
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert card_outcome(box) == OUTCOME[command]
    assert audit_rows_for(box, command)[0]["caller_check"] == "verified"


@pytest.mark.parametrize("command", REPORTS)
def test_row_active_and_a_wrong_id_is_refused_with_exit_3_and_nothing_is_written(box, command):
    xid = take(box)
    before = box.snapshot()
    p = run(box, *report_argv(command), "--execution", OTHER_ID)
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert last_error_code(p.stderr) == "EXECUTION_NOT_CURRENT"
    assert box.snapshot() == before                                    # card・record・枠・identity は 1 バイトも変わらない
    rows = refusals(box, command)
    assert len(rows) == 1 and rows[0]["result"] == "refused:EXECUTION_NOT_CURRENT"
    assert rows[0]["execution_id"] == xid                              # card の ID。名乗られた値を正として残さない
    assert rows[0]["detail"] == f"presented={OTHER_ID}"
    assert not [r for r in audit_rows_for(box, command) if r["result"] == "ok"]


@pytest.mark.parametrize("command", REPORTS)
def test_row_active_and_a_wrong_id_from_env_says_where_the_value_came_from(box, command):
    take(box)
    p = run(box, *report_argv(command), env={"CREWVIA_EXECUTION_ID": OTHER_ID})
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_NOT_CURRENT"
    assert "CREWVIA_EXECUTION_ID" in p.stderr and "unset" in p.stderr         # 直し方を拒否文に出す (§5.2)


def test_the_flag_wins_over_the_environment(box):
    xid = take(box)
    before = box.snapshot()
    bad = run(box, *report_argv("done"), "--execution", OTHER_ID, env={"CREWVIA_EXECUTION_ID": xid})
    assert bad.returncode == 3 and box.snapshot() == before            # 明示の誤りは env の正しい値で救われない
    good = run(box, *report_argv("done"), "--execution", xid, env={"CREWVIA_EXECUTION_ID": OTHER_ID})
    assert good.returncode == 0, good.stderr                           # 明示の正しい値は env の誤りに負けない
    assert box.card()["status"] == "done"


@pytest.mark.parametrize("command", REPORTS)
def test_row_active_and_no_claim_passes_as_unverified(box, command):
    take(box)
    p = run(box, *report_argv(command))
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert card_outcome(box) == OUTCOME[command]
    assert audit_rows_for(box, command)[0]["caller_check"] == "unverified"       # E5 の判断材料 (§5.2)


def test_the_agent_name_is_not_the_basis_of_the_check(box):
    """AC-04: agent 名だけで通さない・agent 名で拒否もしない。照合するのは名乗った ID だけ。"""
    xid = take(box, "Ren")
    before = box.snapshot()
    # 持ち主 (Ren) の名前でも、違う ID は拒否
    assert run(box, *report_argv("done"), "--execution", OTHER_ID, agent="Ren").returncode == 3
    assert box.snapshot() == before
    # 持ち主でない名前 (Director) でも、正しい ID を名乗れば通る (Director が見た試行を完了と判断する経路。§5.3)
    p = run(box, *report_argv("done"), "--execution", xid, agent="Director")
    assert p.returncode == 0, p.stderr
    assert box.card()["status"] == "done"
    # 持ち主の枠は card の worker から撤去される (AGENT_NAME が持ち主でなくても)
    assert box.slot("Ren") is None


@pytest.mark.parametrize("command", REPORTS)
def test_an_empty_explicit_claim_is_refused_not_turned_into_no_claim(box, command):
    """`--execution ""` を「名乗りなし」に倒さない (01b G3)。exit 1・何も書かない・監査行も出さない (使い方の誤り)。"""
    xid = take(box)
    before, rows_before = box.snapshot(), len(box.audit_rows())
    for empty in ("", "   "):
        p = run(box, *report_argv(command), "--execution", empty)
        assert p.returncode == 1, (empty, p.stdout, p.stderr)
        assert "--execution" in p.stderr
    # 空の env も同じ (env の名乗りを「無い」に倒さない)。正しい値を持つ --execution があっても env の空は…
    # flag が優先されるので、env だけが空のときを見る
    p = run(box, *report_argv(command), env={"CREWVIA_EXECUTION_ID": ""})
    assert p.returncode == 1 and "CREWVIA_EXECUTION_ID" in p.stderr
    assert box.snapshot() == before and len(box.audit_rows()) == rows_before
    # 空の env でも、明示の正しい --execution があればそちらが勝つ (flag > env)
    ok = run(box, *report_argv(command), "--execution", xid, env={"CREWVIA_EXECUTION_ID": ""})
    assert ok.returncode == 0, ok.stderr


@pytest.mark.parametrize("claim", ["not-an-id", "ex-123", "EX-" + "a" * 32, "ex-" + "g" * 32, "ex-" + "a" * 33])
def test_a_malformed_claim_is_refused_with_execution_not_found(box, claim):
    take(box)
    before = box.snapshot()
    p = run(box, *report_argv("done"), "--execution", claim)
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_NOT_FOUND"
    assert box.snapshot() == before
    assert claim not in p.stderr                                        # 名乗られた値を拒否文に出さない
    assert all(claim not in json.dumps(r) for r in box.audit_rows())


@pytest.mark.parametrize("command", ["done", "fail", "needs-director"])
def test_row_legacy_card_without_an_execution_passes_without_a_claim_and_refuses_a_claim(box, command):
    """E2 より前に pull された card (試行の欄なし)。名乗りなしは今までどおり通り、ID を名乗れば `EXECUTION_NOT_FOUND`。"""
    legacy_in_progress(box, "t002")
    before = box.snapshot()
    p = run(box, *report_argv(command, "t002"), "--execution", OTHER_ID)
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_NOT_FOUND"
    assert box.snapshot() == before
    ok = run(box, *report_argv(command, "t002"))
    assert ok.returncode == 0, ok.stderr
    assert box.card("t002")["status"] == OUTCOME[command][2]
    assert audit_rows_for(box, command)[-1]["caller_check"] == "legacy_generation"
    assert "current_execution_id" not in box.card("t002")              # 試行の欄を作らない (task の遷移だけ)


# ---------------------------------------------------------------------------
# 2. 同じ ID の再送 (§4.4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["done", "fail", "needs-director"])
def test_a_resend_with_the_same_id_succeeds_and_writes_nothing(box, command):
    xid = take(box)
    assert run(box, *report_argv(command), "--execution", xid).returncode == 0
    before = box.snapshot()
    again = run(box, *report_argv(command), "--execution", xid)
    assert again.returncode == 0, (again.stdout, again.stderr)
    assert "idempotent" in again.stdout
    assert box.snapshot() == before
    assert not refusals(box, command)


@pytest.mark.parametrize("first,second", [("done", "fail"), ("done", "needs-director"), ("fail", "done"),
                                           ("needs-director", "done"), ("needs-director", "fail"), ("fail", "needs-director")])
def test_a_different_result_for_a_terminal_execution_is_a_conflict(box, first, second):
    xid = take(box)
    assert run(box, *report_argv(first), "--execution", xid).returncode == 0
    before = box.snapshot()
    p = run(box, *report_argv(second), "--execution", xid)
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_ALREADY_TERMINAL"
    assert box.snapshot() == before
    assert refusals(box, second)[0]["result"] == "refused:EXECUTION_ALREADY_TERMINAL"


def test_a_resend_of_done_does_not_redo_the_derived_writes_or_the_mission_done(box):
    """done の再送は D1〜D5 (pr_number の伝播・mission の done) を走らせない。同じ中身は成功、`--pr` / Result が違えば conflict (exit 3)。

    違う中身を exit 0 で飲んでいた形 (`--pr 99` の再送が成功) は t034 で直した (knowledge/execution.md §16.10)。どちらも何も書かない。
    """
    xid = take(box)
    assert run(box, *report_argv("done"), "--execution", xid).returncode == 0
    before = box.snapshot()
    same = run(box, *report_argv("done"), "--execution", xid)
    assert same.returncode == 0 and "idempotent" in same.stdout and box.snapshot() == before
    for argv in (("done", "t001", "r", "--pr", "99"), ("done", "t001", "r2")):
        again = run(box, *argv, "--mission", MISSION, "--execution", xid)
        assert again.returncode == 3 and last_error_code(again.stderr) == "EXECUTION_ALREADY_TERMINAL"
        assert box.snapshot() == before
    assert "pr_number" not in box.card()


# ---------------------------------------------------------------------------
# 3. Director が開いた card・取り直された後 (§5.2 の TERMINAL / DETACHED の行・§5.3)
# ---------------------------------------------------------------------------

def test_a_card_the_director_reopened_after_needs_director_is_done_without_a_claim(box):
    """cutover review task の今の運用: `update --status in_progress --reset` → done (t019 で実績)。試行の無い経路で通る。"""
    xid = take(box)
    assert run(box, *report_argv("needs-director"), "--execution", xid).returncode == 0
    p = run(box, "update", "t001", "--status", "in_progress", "--reset", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    meta = box.card()
    assert meta["status"] == "in_progress" and meta["worker"] is None and meta["execution_status"] == "failed"
    done = run(box, *report_argv("done"), agent="Director")
    assert done.returncode == 0, (done.stdout, done.stderr)
    after = box.card()
    assert after["status"] == "done"
    assert (after["execution_status"], after["execution_end_code"], after["current_execution_id"]) == \
        ("failed", "NEEDS_DIRECTOR", xid)                                 # 閉じた試行の欄は触らない
    assert audit_rows_for(box, "done")[-1]["caller_check"] == "no_execution"


def test_the_old_attempt_cannot_close_a_card_the_director_reopened(box):
    xid = take(box)
    assert run(box, *report_argv("needs-director"), "--execution", xid).returncode == 0
    assert run(box, "update", "t001", "--status", "in_progress", "--reset", "--mission", MISSION,
               agent="Director").returncode == 0
    before = box.snapshot()
    p = run(box, *report_argv("done"), "--execution", xid)               # 古い試行 (needs-director で終わった) を名乗る done
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_ALREADY_TERMINAL"
    assert box.snapshot() == before


def test_a_stale_id_after_a_reset_and_a_new_attempt_is_not_current(box):
    """Director が見た後に Worker が reset → 再 pull していれば `EXECUTION_NOT_CURRENT` で止まる (退役の世代の照合が防いでいたのと同じ事故)。"""
    old = take(box, "Ren")
    box.reset()                                                           # 旧形式の reset (E4 まで試行の欄は触らない)
    new = take(box, "Sora")
    assert new != old
    before = box.snapshot()
    p = run(box, *report_argv("done"), "--execution", old, agent="Director")
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_NOT_CURRENT"
    assert box.snapshot() == before
    ok = run(box, *report_argv("done"), "--execution", new, agent="Director")
    assert ok.returncode == 0, ok.stderr
    assert box.card()["status"] == "done" and box.card()["worker"] == "Sora"
    assert box.slot("Sora") is None                                       # 持ち主 (Sora) の枠が card の worker から撤去される


def test_a_detached_card_refuses_a_claim_and_treats_no_claim_as_task_only(box):
    """旧形式の reset の後 (card は pending・試行の欄は running のまま = DETACHED)。X を名乗っても冪等の表を引かない。
    (E4a の前は `update --reset` がこの形を作った。今は試行を閉じるので、旧コードの reset を card に直接再現する)"""
    xid = take(box)
    e3.old_code_reset(box)
    assert box.card()["execution_status"] == "running" and box.card()["status"] == "pending"
    before = box.snapshot()
    p = run(box, *report_argv("done"), "--execution", xid)
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_NOT_CURRENT"
    assert box.snapshot() == before
    # 名乗りなし: 試行は触らない。task の遷移だけ (pending からの done は §4.3 で狭めた → exit 2)
    q = run(box, *report_argv("done"))
    assert q.returncode == 2 and last_error_code(q.stderr) == "INVALID_TRANSITION"
    assert box.snapshot() == before


# ---------------------------------------------------------------------------
# 4. 狭めた遷移 (§4.3)。exit 2・何も書かない。拒否の行が残る
# ---------------------------------------------------------------------------

#: (command, 拒否される task の status)。出口は knowledge/execution.md §4.3 の表
NARROWED = [
    ("done", "pending"), ("done", "blocked"), ("done", "ready_for_verification"), ("done", "verifying"),
    ("done", "needs_human_review"), ("done", "verification_failed"), ("done", "needs_director"),
    ("done", "done"), ("done", "failed"),
    ("fail", "pending"), ("fail", "blocked"), ("fail", "ready_for_verification"), ("fail", "verifying"),
    ("fail", "needs_human_review"), ("fail", "verification_failed"), ("fail", "done"),
    ("verify-pass", "pending"), ("verify-pass", "in_progress"), ("verify-pass", "blocked"),
    ("verify-pass", "needs_director"), ("verify-pass", "verification_failed"),
    ("verify-fail", "pending"), ("verify-fail", "in_progress"), ("verify-fail", "needs_director"),
    ("verify-nhr", "pending"), ("verify-nhr", "in_progress"), ("verify-nhr", "blocked"),
    ("needs-director", "pending"), ("needs-director", "needs_director"),
    ("ready-for-verification", "pending"), ("ready-for-verification", "done"),
]


@pytest.mark.parametrize("command,status", NARROWED)
def test_a_narrowed_transition_is_refused_with_exit_2_and_nothing_is_written(box, command, status):
    new_card(box, "t002", status, worker="Ren" if status != "pending" else None)
    before = box.snapshot()
    p = run(box, *report_argv(command, "t002"))
    assert p.returncode == 2, (command, status, p.stdout, p.stderr)
    assert last_error_code(p.stderr) == "INVALID_TRANSITION"
    assert box.snapshot() == before
    rows = refusals(box)
    assert rows and rows[-1]["result"] == "refused:INVALID_TRANSITION" and rows[-1]["task"] == "t002"


def test_done_on_a_needs_director_task_keeps_the_directors_way_out_in_the_message(box):
    new_card(box, "t002", "needs_director", worker="Ren", needs_director_reason="r")
    p = run(box, *report_argv("done", "t002"))
    assert p.returncode == 2
    assert "update t002 --status in_progress --reset" in p.stderr and "needs_director" in p.stderr


@pytest.mark.parametrize("command,status", [
    ("done", "in_progress"), ("fail", "in_progress"), ("fail", "needs_director"), ("needs-director", "in_progress"),
    ("ready-for-verification", "in_progress"), ("verify-pass", "ready_for_verification"),
    ("verify-pass", "verifying"), ("verify-pass", "needs_human_review"), ("verify-fail", "verifying"),
    ("verify-nhr", "ready_for_verification"), ("verify-nhr", "needs_human_review")])
def test_the_transitions_that_stay_are_accepted_on_a_task_without_an_execution(box, command, status):
    new_card(box, "t002", status, worker="Ren")
    p = run(box, *report_argv(command, "t002"))
    assert p.returncode == 0, (command, status, p.stdout, p.stderr)


def test_an_unknown_status_is_refused(box):
    new_card(box, "t002", "cancelled", worker="Ren")          # 手書きの card (語彙に無い)
    p = run(box, *report_argv("done", "t002"))
    assert p.returncode == 2


def test_the_status_refusal_comes_before_the_evidence_and_the_pr_checks(box):
    """今までも status の拒否が先だった (fail の証拠・done の --pr の要求より前)。"""
    new_card(box, "t002", "blocked", worker="Ren")
    p = run(box, "fail", "t002", "--mission", MISSION)                 # --head も --no-head も無い
    assert p.returncode == 2 and last_error_code(p.stderr) == "INVALID_TRANSITION"
    p = run(box, "fail", "t001", "--mission", MISSION)
    assert p.returncode == 2                                           # pending (走っていない) への fail


def test_a_wrong_claim_is_refused_before_the_derived_writes_of_done(box):
    """done は pr_number を依存先に伝える (D1 / D2) のをコミットの前に行う。照合・遷移の検査はその**前** (dry_run) なので、
    拒否された done は依存先の card にも何も書かない。"""
    new_card(box, "t003", "pending", blocked_by=["t001"], skills=["codex-review"])
    xid = take(box)
    before = box.snapshot()
    p = run(box, "done", "t001", "r", "--pr", "5", "--mission", MISSION, "--execution", OTHER_ID)
    assert p.returncode == 3 and box.snapshot() == before
    assert "pr_number" not in box.card("t001") and "pr_number" not in box.card("t003")
    ok = run(box, "done", "t001", "r", "--pr", "5", "--mission", MISSION, "--execution", xid)
    assert ok.returncode == 0, ok.stderr
    assert box.card("t001")["pr_number"] == 5 and box.card("t003")["pr_number"] == 5


# ---------------------------------------------------------------------------
# 5. 検証の流れ (ready-for-verification → verifying → verify-result)。verifier は持ち主ではない (§5.1)
# ---------------------------------------------------------------------------

def to_verification(box, xid):
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    p = run(box, *report_argv("verifying"), "--execution", xid, agent="verifier-dispatcher")
    assert p.returncode == 0, p.stderr
    assert box.card()["status"] == "verifying" and box.card()["verifier"] == "V1"


def test_verify_pass_by_a_verifier_who_is_not_the_owner_completes_the_attempt(box):
    xid = take(box, "Ren")
    to_verification(box, xid)
    assert box.card()["execution_status"] == "running"                  # 検証待ちの間も試行は running (§4.2)
    p = run(box, *report_argv("verify-pass"), "--execution", xid, agent="V1")
    assert p.returncode == 0, p.stderr
    m = box.card()
    assert (m["status"], m["execution_status"], m["execution_end_code"]) == ("verified", "completed", "VERIFIED")
    assert box.slot("Ren") == f"{MISSION}:t001"                         # 枠は残る (R-2 が後で消す。state-store.md §7)
    again = run(box, *report_argv("verify-pass"), "--execution", xid, agent="V1")
    assert again.returncode == 0 and "idempotent" in again.stdout
    conflict = run(box, *report_argv("verify-fail"), "--execution", xid, agent="V1")
    assert conflict.returncode == 3 and last_error_code(conflict.stderr) == "EXECUTION_ALREADY_TERMINAL"


def test_verify_result_with_a_wrong_id_is_refused(box):
    xid = take(box)
    to_verification(box, xid)
    before = box.snapshot()
    p = run(box, *report_argv("verify-pass"), "--execution", OTHER_ID, agent="V1")
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_NOT_CURRENT"
    assert box.snapshot() == before


def test_verifying_with_a_wrong_id_is_refused_and_writes_nothing(box):
    xid = take(box)
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    before = box.snapshot()
    p = run(box, *report_argv("verifying"), "--execution", OTHER_ID, agent="verifier-dispatcher")
    assert p.returncode == 3 and box.snapshot() == before and box.card()["status"] == "ready_for_verification"


def test_verify_fail_below_the_limit_starts_a_new_attempt(box):
    """`verify-result fail` (< max) は試行を failed (VERIFICATION_REJECTED) にして task を pending に戻す。
    worker・started_at を null・枠撤去。次の pull が新しい試行 (attempt + 1・新しい ID) を予約する (§3 / §4.2)。"""
    old = take(box, "Ren")
    to_verification(box, old)
    p = run(box, *report_argv("verify-fail"), "--execution", old, agent="V1")
    assert p.returncode == 0, p.stderr
    assert "pending" in p.stdout
    m = box.card()
    assert (m["status"], m["worker"], m["started_at"]) == ("pending", None, None)
    assert (m["execution_status"], m["execution_end_code"], m["rework_count"]) == ("failed", "VERIFICATION_REJECTED", 1)
    assert box.slot("Ren") is None and box.identity("Ren") is None
    assert box.record(old)["status"] == "failed" and box.record(old)["end_code"] == "VERIFICATION_REJECTED"
    # verifier の再送は冪等
    again = run(box, *report_argv("verify-fail"), "--execution", old, agent="V1")
    assert again.returncode == 0 and "idempotent" in again.stdout
    new = take(box, "Sora")
    assert new != old and box.card()["execution_count"] == 2
    assert box.record(new)["attempt"] == 2
    # 古い試行の判定の再送は、新しい試行を巻き込まない
    stale = run(box, *report_argv("verify-fail"), "--execution", old, agent="V1")
    assert stale.returncode == 3 and last_error_code(stale.stderr) == "EXECUTION_NOT_CURRENT"
    assert box.card()["current_execution_id"] == new and box.card()["status"] == "in_progress"


def test_verify_fail_at_the_limit_escalates_without_closing_the_attempt(box):
    set_card(box, "t001", max_rework=1)
    xid = take(box)
    to_verification(box, xid)
    p = run(box, *report_argv("verify-fail"), "--execution", xid, agent="V1")
    assert p.returncode == 0, p.stderr
    assert "needs_human_review" in p.stdout
    m = box.card()
    assert (m["status"], m["execution_status"]) == ("needs_human_review", "running")
    assert m["rework_count"] == 1 and m["worker"] == "Ren"
    # 人間の判断 (pass) で閉じられる
    ok = run(box, *report_argv("verify-pass"), "--execution", xid, agent="V1")
    assert ok.returncode == 0 and box.card()["status"] == "verified"


def test_verify_needs_human_review_verdict_keeps_the_attempt_running(box):
    xid = take(box)
    to_verification(box, xid)
    p = run(box, *report_argv("verify-nhr"), "--execution", xid, agent="V1")
    assert p.returncode == 0, p.stderr
    assert (box.card()["status"], box.card()["execution_status"]) == ("needs_human_review", "running")
    bad = run(box, *report_argv("verify-nhr"), "--execution", OTHER_ID, agent="V1")
    assert bad.returncode == 3


# ---------------------------------------------------------------------------
# 6. Director の経路を壊さない (§5.3) / status の表示 / 閉じる手段
# ---------------------------------------------------------------------------

def test_status_shows_the_id_and_attempt_of_an_active_attempt_only(box):
    xid = take(box)
    p = run(box, "status", "--mission", MISSION)
    assert f"[{xid} attempt 1]" in p.stdout
    assert run(box, *report_argv("done"), "--execution", xid).returncode == 0
    p = run(box, "status", "--mission", MISSION)
    assert xid not in p.stdout                                            # 終わった試行は出さない


def test_a_task_without_an_execution_shows_no_id(box):
    legacy_in_progress(box, "t002")
    p = run(box, "status", "--mission", MISSION)
    assert "ex-" not in p.stdout


def test_close_execution_closes_the_attempt_left_on_a_finished_task_without_touching_the_task(box):
    """E2 の間にできた card (done / fail が試行を閉じない): 終わった task に running の試行が残る。Director が閉じる。"""
    xid = take(box)
    set_card(box, "t001", status="done", completed_at="2026-10-01T00:00:00Z")      # E2 の done の跡を再現 (試行は running のまま)
    assert box.card()["execution_status"] == "running"
    assert "execution_active_on_finished_task" in box.store_check()
    before_task = {k: v for k, v in box.card().items() if not k.startswith("execution")}
    p = run(box, "update", "t001", "--close-execution", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, (p.stdout, p.stderr)
    m = box.card()
    assert (m["execution_status"], m["execution_end_code"]) == ("failed", "ABANDONED_OUTSIDE_CONTROLLER")
    assert {k: v for k, v in m.items() if not k.startswith("execution")} == before_task      # task の欄は 1 バイトも変わらない
    assert box.record(xid)["status"] == "failed"
    assert "execution_active_on_finished_task" not in box.store_check()
    again = run(box, "update", "t001", "--close-execution", "--mission", MISSION, agent="Director")
    assert again.returncode == 2 and last_error_code(again.stderr) == "INVALID_TRANSITION"


def test_close_execution_does_not_close_a_live_attempt_or_combine_with_other_options(box):
    xid = take(box)
    before = box.snapshot()
    live = run(box, "update", "t001", "--close-execution", "--mission", MISSION, agent="Director")
    assert live.returncode == 2 and box.snapshot() == before             # 持ち主がいる試行は閉じない
    mixed = run(box, "update", "t001", "--close-execution", "--status", "pending", "--mission", MISSION)
    assert mixed.returncode == 2 and box.snapshot() == before
    assert box.card()["current_execution_id"] == xid


def test_after_e3_a_finished_task_leaves_no_active_attempt_behind(box):
    """E3 の後に新しく `execution_active_on_finished_task` / `execution_active_on_non_holding_status` が出ない
    (done / fail / needs-director / verify-result が試行を閉じる)。"""
    for tid, command in (("t001", "done"), ("t002", "fail"), ("t003", "needs-director")):
        xid = take(box, f"W{tid}", tid)
        assert run(box, *report_argv(command, tid), "--execution", xid).returncode == 0
    out = box.store_check()
    assert "execution_active_on_finished_task" not in out and "execution_active_on_non_holding_status" not in out
    assert "execution_detached" not in out


# ---------------------------------------------------------------------------
# 7. 監査 (§8): actor・拒否の行。中身を出さない
# ---------------------------------------------------------------------------

def test_the_actor_falls_back_to_the_card_worker_only_when_there_is_no_agent_name(box):
    xid = take(box, "Ren")
    p = run(box, *report_argv("done"), "--execution", xid, agent=None)        # AGENT_NAME 無し (デーモン・手作業)
    assert p.returncode == 0, p.stderr
    assert audit_rows_for(box, "done")[0]["actor"] == "Ren"


def test_the_actor_is_not_replaced_when_an_agent_name_exists(box):
    xid = take(box, "Ren")
    p = run(box, *report_argv("done"), "--execution", xid, agent="Director")
    assert p.returncode == 0, p.stderr
    assert audit_rows_for(box, "done")[0]["actor"] == "Director"             # Director が打った行を Worker の行にしない


def test_refusals_and_errors_never_echo_the_card_or_the_claimed_value(box):
    secret = e3.SECRET
    xid = take(box)
    set_card(box, "t001", needs_director_reason=f"{secret}-reason", note=f"{secret}-note", title=f"{secret}-title")
    for argv in (report_argv("done") + ["--execution", f"ex-{secret}"],
                 report_argv("done") + ["--execution", OTHER_ID],
                 report_argv("fail", "t001") + ["--execution", OTHER_ID],
                 report_argv("verify-pass") + ["--execution", OTHER_ID],
                 ["done", "t001", "r", "--mission", MISSION, "--execution", ""]):
        p = run(box, *argv)
        assert p.returncode in (1, 2, 3), (argv, p.stdout, p.stderr)
        assert secret not in p.stdout + p.stderr, argv
    new_card(box, "t002", "pending", title=f"{secret}-t2")
    p = run(box, *report_argv("done", "t002"))                                 # 遷移の拒否
    assert p.returncode == 2 and secret not in p.stdout + p.stderr
    assert all(secret not in json.dumps(r) for r in box.audit_rows())


def test_the_error_code_line_is_the_last_line_and_has_a_fixed_form(box):
    take(box)
    p = run(box, *report_argv("done"), "--execution", OTHER_ID)
    lines = [l for l in p.stderr.strip().splitlines() if l.strip()]
    assert lines[-1] == "[plan.sh] error_code=EXECUTION_NOT_CURRENT"
    new_card(box, "t002", "pending")
    q = run(box, *report_argv("done", "t002"))
    assert q.stderr.strip().splitlines()[-1] == "[plan.sh] error_code=INVALID_TRANSITION"


def test_the_execution_flag_is_in_the_usage_of_every_reporting_command(box):
    for cmd in ("done", "fail", "needs-director", "ready-for-verification", "verifying", "verify-result"):
        p = run(box, cmd, "--help")
        assert p.returncode == 0 and "--execution <id>" in p.stdout, cmd


# ---------------------------------------------------------------------------
# 8. 今の運用 (kai-review・target_dir の task・複数 task)
# ---------------------------------------------------------------------------

def test_kai_review_style_pull_then_done_with_the_json_id_goes_through(box):
    """kai-review.sh は pull の JSON の `execution_id` を done / needs-director に渡す (execution.md §5.4)。"""
    p = box.plan("pull", "--task", "t002", "--agent", "Kai-codex", "--skills", "codex-review", "--mission", MISSION,
                 agent="Kai-codex")
    # skills が合わないと pull は idle になるので、codex-review の card を足してやり直す
    if p.returncode != 0:
        new_card(box, "t002", "pending", skills=["codex-review"], pr_number=7)
        p = box.plan("pull", "--task", "t002", "--agent", "Kai-codex", "--skills", "codex-review", "--mission",
                     MISSION, agent="Kai-codex")
    assert p.returncode == 0, p.stderr
    xid = json.loads(p.stdout)["execution_id"]
    ok = run(box, "done", "t002", "LGTM", "--mission", MISSION, "--execution", xid, agent="Kai-codex")
    assert ok.returncode == 0, ok.stderr
    assert box.card("t002")["status"] == "done" and box.slot("Kai-codex") is None


def test_a_target_dir_task_is_reported_with_the_id_from_the_pull_json(box, tmp_path):
    td = tmp_path / "target"
    td.mkdir()
    set_card(box, "t001", target_dir=str(td))
    p = box.plan("pull", "--agent", "Ren", "--skills", "code", "--task", "t001", "--mission", MISSION,
                 "--target-dir", str(td))
    assert p.returncode == 0, p.stderr
    res = json.loads(p.stdout)
    assert res["worktree_path"] is None                                      # `.crewvia-env` は無い。JSON が唯一の入手経路
    bad = run(box, *report_argv("done"), "--execution", OTHER_ID)
    assert bad.returncode == 3
    ok = run(box, *report_argv("done"), "--execution", res["execution_id"])
    assert ok.returncode == 0, ok.stderr


def test_two_tasks_do_not_confuse_each_others_ids(box):
    a = take(box, "Ren", "t001")
    b = take(box, "Sora", "t002")
    before = box.snapshot()
    p = run(box, *report_argv("done", "t001"), "--execution", b, agent="Sora")       # Sora が自分の ID で Ren の task を終わらせる
    assert p.returncode == 3 and box.snapshot() == before
    assert run(box, *report_argv("done", "t002"), "--execution", b, agent="Sora").returncode == 0
    assert run(box, *report_argv("done", "t001"), "--execution", a, agent="Ren").returncode == 0
