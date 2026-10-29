"""`plan.sh retry` と総試行数の上限 (vNext 02 PR-E / t031。設計 §3.3・§3.7)・store-check の孤児 (§8 の 3 件目)。

隔離 plan.sh + 隔離 queue (`pull_execution_helpers.Box`)。本番の queue / registry / config には触れない
(config は `Box.root/config/crewvia.yaml` に置く = 隔離コピーの plan.sh が読む場所)。

* 遷移元ごとの可否 (許す 4 つ / それ以外は exit 3 で何も書かない)・active の拒否・理由の検査
* 保つ欄 / 消す欄・証拠 (handoff_path / fail_head) が台帳の retry に写ること・理由が監査ログに出ないこと
* 上限 (既定 6・config `attempts.max_total`・card の `max_attempts`) と `--over-cap`
* 再送の冪等 (台帳だけ書けた孤児のあと同じ retry で新しいファイルが増えない・違う理由は `HISTORY_MISMATCH`)
* retry → pull の `previous_attempt`・retry → pull → done が同じ pr_number で閉じる
* `update --reset` が残っていること
* store-check: 別の遷移で閉じた試行の孤児 (done が台帳だけ書いて落ちた後に reset / retire / close-execution) を報告する・
  done で閉じたものは報告しない・2 回報告しない・読み取りだけ
"""

from __future__ import annotations

import json
import os
import re

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import MISSION, Box, last_error_code, new_card, run, set_card, take

import lib_history as hist  # noqa: E402

TID = "t001"
SECRET_REASON = "SECRET-REASON-xyzzy"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def ledger_dir(box, tid=TID):
    return box.queue / "missions" / MISSION / "history" / tid


def ledger_files(box, tid=TID):
    d = ledger_dir(box, tid)
    return sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) if d.is_dir() else []


def ledger(box, name, tid=TID):
    return hist.parse((ledger_dir(box, tid) / name).read_text(), source=name)


def retry_files(box):
    return [n for n in ledger_files(box) if "-retry-" in n]


def retry(box, *extra, reason="やり直し", tid=TID, agent="Director"):
    args = ["retry", tid, *(["--reason", reason] if reason is not None else []), "--mission", MISSION, *extra]
    return run(box, *args, agent=agent)


def failed_box(box, **card):
    """t001 を pull → fail まで進める。`card` の欄は fail の後に足す。戻り値: 失敗した試行の ID。"""
    xid = take(box)
    p = run(box, "fail", TID, "--no-head", "x", "--mission", MISSION, "--execution", xid, agent="Ren")
    assert p.returncode == 0, p.stderr
    if card:
        set_card(box, **card)
    return xid


def needs_director_box(box):
    xid = take(box)
    p = run(box, "needs-director", TID, "why", "--mission", MISSION, "--execution", xid, agent="Ren")
    assert p.returncode == 0, p.stderr
    return xid


def audit_text(box):
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in box.audit_rows())


def refused_codes(box):
    return [r["result"] for r in e3.refusals(box, "retry")]


# ---------------------------------------------------------------------------
# 遷移元ごとの可否・active の拒否
# ---------------------------------------------------------------------------

def test_retry_from_failed_and_needs_director_returns_the_card_to_pending(tmp_path):
    for name, setup in (("failed", failed_box), ("needs_director", needs_director_box)):
        b = Box(tmp_path / name, tasks=("t001",))
        xid = setup(b)
        assert b.card()["status"] == name
        p = retry(b)
        assert p.returncode == 0, p.stderr
        card = b.card()
        assert card["status"] == "pending" and card["worker"] is None and card["started_at"] is None
        assert card["current_execution_id"] == xid and card["execution_status"] == "failed"   # 試行の欄は終わったまま
        [rname] = retry_files(b)
        fields, _ = ledger(b, rname)
        assert fields["kind"] == "retry" and fields["execution_id"] == xid and fields["reason"] == "やり直し"


@pytest.mark.parametrize("status", ["needs_human_review", "verification_failed"])
def test_retry_from_the_other_two_sources_when_the_attempt_is_over(box, status):
    """検証待ちの 2 つ。試行が終わっている (terminal) card なら通る — 試行が running のままの needs_human_review は
    active として拒否される (次のテスト)。"""
    failed_box(box)
    set_card(box, status=status)
    p = retry(box)
    assert p.returncode == 0, p.stderr
    assert box.card()["status"] == "pending"


def test_needs_human_review_with_a_running_attempt_is_refused_as_active(box):
    """verify-result needs_human_review は試行 (running) を変えない。active なので先に reset / close-execution が要る。"""
    xid = take(box)
    set_card(box, status="needs_human_review")
    assert box.card()["execution_status"] == "running"
    before = box.snapshot()
    p = retry(box)
    assert p.returncode == 3, p.stderr
    assert last_error_code(p.stderr) == "ATTEMPT_ACTIVE"
    assert box.snapshot() == before and not ledger_files(box)
    assert box.card()["current_execution_id"] == xid
    # 持ち主のいる active (running) は close-execution では閉じられない (INVALID_TRANSITION)。出口は reset だけ
    assert f"plan.sh update {TID} --status pending --reset" in p.stderr
    assert f"先に `plan.sh update {TID} --close-execution`" not in p.stderr
    c = run(box, "update", TID, "--close-execution", "--mission", MISSION, agent="Director")
    assert c.returncode == 2 and last_error_code(c.stderr) == "INVALID_TRANSITION"


@pytest.mark.parametrize("status", ["pending", "in_progress", "done", "verified", "skipped", "blocked",
                                    "ready_for_verification", "verifying"])
def test_retry_from_any_other_status_is_refused_and_writes_nothing(box, status):
    failed_box(box)
    set_card(box, status=status, worker=None)             # worker があると回復 (R-1) が枠を作る。拒否のテストでは要らない
    before = box.snapshot()
    p = retry(box)
    assert p.returncode == 3, (status, p.stderr)
    assert last_error_code(p.stderr) in ("RETRY_NOT_ALLOWED", "ATTEMPT_ACTIVE")
    assert box.snapshot() == before and not retry_files(box)
    assert refused_codes(box) == [f"refused:{last_error_code(p.stderr)}"]


@pytest.mark.parametrize("execution_status", ["reserved", "running"])
def test_retry_refuses_an_active_attempt_whatever_the_task_status(box, execution_status):
    failed_box(box)
    set_card(box, execution_status=execution_status, execution_end_code=None)
    p = retry(box)
    assert p.returncode == 3
    assert last_error_code(p.stderr) == "ATTEMPT_ACTIVE"
    assert "close-execution" in p.stderr and "--reset" in p.stderr
    assert "持ち主がいる" not in p.stderr                  # DETACHED (手放された card) は close-execution が正しい出口
    assert not retry_files(box) and box.card()["status"] == "failed"      # (record の追従は回復の仕事で retry は書かない)


def test_retry_refuses_a_card_without_an_attempt_record(box):
    new_card(box, "t002", status="failed")
    before = box.snapshot()
    p = retry(box, tid="t002")
    assert p.returncode == 3
    assert last_error_code(p.stderr) == "RETRY_NOT_ALLOWED" and "update --reset" in p.stderr
    assert box.snapshot() == before


# ---------------------------------------------------------------------------
# 理由
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [None, "", "   ", "a\nb", "a\tb", "a\x07b", "x" * 501])
def test_a_missing_empty_or_control_reason_is_refused_and_writes_nothing(box, reason):
    failed_box(box)
    before = box.snapshot()
    p = retry(box, reason=reason)
    assert p.returncode != 0
    assert box.snapshot() == before and not retry_files(box)
    assert box.card()["status"] == "failed"


def test_over_cap_reason_is_checked_like_reason(box):
    failed_box(box)
    before = box.snapshot()
    for bad in ("", "a\nb"):
        p = retry(box, "--over-cap", bad)
        assert p.returncode != 0, bad
    assert box.snapshot() == before


def test_the_reason_lives_in_the_ledger_only(box):
    failed_box(box)
    assert retry(box, reason=SECRET_REASON).returncode == 0
    [rname] = retry_files(box)
    assert ledger(box, rname)[0]["reason"] == SECRET_REASON
    assert SECRET_REASON not in audit_text(box)
    card_text = (box.queue / "missions" / MISSION / "tasks" / "t001.md").read_text()
    assert SECRET_REASON not in card_text


# ---------------------------------------------------------------------------
# 保つ欄・消す欄・証拠の写し
# ---------------------------------------------------------------------------

def test_kept_and_cleared_fields_and_evidence_copied_to_the_ledger(box):
    xid = failed_box(box, pr_number=7, rework_count=2, handoff_path="/h/Ren/m/t001_HANDOFF.md", fail_head="a" * 40,
                     fail_head_waiver="w", completed_at="2026-10-16T00:00:00Z")
    before = box.card()
    assert retry(box).returncode == 0
    card = box.card()
    for kept in ("pr_number", "task_slug", "execution_count", "rework_count", "current_execution_id",
                 "execution_status", "execution_end_code", "result_execution_id"):
        assert card[kept] == before[kept], kept
    for cleared in ("worker", "started_at", "completed_at", "handoff_path", "fail_head", "fail_head_waiver"):
        assert card.get(cleared) is None, cleared
    [rname] = retry_files(box)
    fields, body = ledger(box, rname)
    assert fields["handoff_path"] == "/h/Ren/m/t001_HANDOFF.md" and fields["head"] == "a" * 40
    assert fields["pr_number"] == 7 and fields["worker"] == "Ren" and fields["attempt"] == 1
    assert fields["end_code"] == "WORKER_FAILED" and fields["actor"] == "Director" and body.strip() == ""
    # 結果の台帳 (end) は retry より前に 1 つだけあり、retry が後ろに足される
    assert ledger_files(box)[0].startswith("0001-end-") and rname.startswith("0002-retry-")
    assert hist.parse((ledger_dir(box) / ledger_files(box)[0]).read_text())[0]["execution_id"] == xid


def test_retry_does_not_touch_the_result_section(box):
    failed_box(box)
    path = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    body_before = path.read_text().split("---", 2)[2]
    assert retry(box).returncode == 0
    assert path.read_text().split("---", 2)[2] == body_before


def test_retry_removes_the_stale_assignment_of_the_old_worker(box):
    failed_box(box)
    assert retry(box).returncode == 0
    assert not (box.queue / "assignments" / "Ren").exists()


def test_the_handoff_file_is_left_in_place_for_the_next_worker(box, tmp_path):
    handoff = box.root / "registry" / "handoffs" / "Ren" / MISSION / "t001_HANDOFF.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("HANDOFF-BODY\n")
    failed_box(box, handoff_path=str(handoff))
    assert retry(box).returncode == 0
    assert handoff.read_text() == "HANDOFF-BODY\n"          # 退避 (.stale-) しない: previous_attempt が指すファイルだから


# ---------------------------------------------------------------------------
# 上限
# ---------------------------------------------------------------------------

def test_default_cap_is_six_and_over_cap_passes_and_is_recorded(box):
    failed_box(box, execution_count=6)
    before = box.snapshot()
    p = retry(box)
    assert p.returncode == 3
    assert last_error_code(p.stderr) == "ATTEMPT_CAP" and "6" in p.stderr and "--over-cap" in p.stderr
    assert box.snapshot() == before and not retry_files(box)
    assert refused_codes(box) == ["refused:ATTEMPT_CAP"]

    p = retry(box, "--over-cap", "レビュー 7 巡目が必要")
    assert p.returncode == 0, p.stderr
    [rname] = retry_files(box)
    assert ledger(box, rname)[0]["over_cap"] == "レビュー 7 巡目が必要"
    assert box.card()["status"] == "pending" and box.card()["execution_count"] == 6    # 数は変えない


def test_below_the_cap_retry_passes_and_an_unneeded_over_cap_is_not_recorded(box):
    failed_box(box, execution_count=5)
    assert retry(box, "--over-cap", "要らない").returncode == 0
    [rname] = retry_files(box)
    assert "over_cap" not in ledger(box, rname)[0]


def test_card_max_attempts_overrides_the_default_both_ways(tmp_path):
    low = Box(tmp_path / "low", tasks=("t001",))
    failed_box(low, execution_count=2, max_attempts=2)
    p = retry(low)
    assert p.returncode == 3 and last_error_code(p.stderr) == "ATTEMPT_CAP"

    high = Box(tmp_path / "high", tasks=("t001",))
    failed_box(high, execution_count=8, max_attempts=9)
    assert retry(high).returncode == 0


@pytest.mark.parametrize("bad", [0, -1, "abc", True, [3]])
def test_a_broken_card_max_attempts_stops_instead_of_falling_back(box, bad):
    failed_box(box, max_attempts=bad)
    before = box.snapshot()
    p = retry(box)
    assert p.returncode == 1 and last_error_code(p.stderr) == "STATE_INVALID"
    assert box.snapshot() == before


def test_config_attempts_max_total_changes_the_default(box):
    (box.root / "config").mkdir()
    (box.root / "config" / "crewvia.yaml").write_text("attempts:\n  max_total: 3\n")
    failed_box(box, execution_count=3)
    p = retry(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "ATTEMPT_CAP"


def test_a_bad_config_value_falls_back_to_six_with_a_warning(box):
    (box.root / "config").mkdir()
    (box.root / "config" / "crewvia.yaml").write_text("attempts:\n  max_total: lots\n")
    failed_box(box, execution_count=5)
    p = retry(box)
    assert p.returncode == 0 and "attempts.max_total" in p.stderr
    failed_box_again = run(box, "update", TID, "--status", "failed", "--mission", MISSION, agent="Director")
    assert failed_box_again.returncode == 0


def test_the_repository_default_config_documents_attempts_max_total():
    text = open(os.path.join(os.path.dirname(__file__), "..", "config", "crewvia.yaml")).read()
    m = re.search(r"^attempts:\n  max_total: (\d+)", text, re.M)
    assert m and int(m.group(1)) == 6 == hist.DEFAULT_MAX_ATTEMPTS
    assert hist.load_default_max_attempts(os.path.join(os.path.dirname(__file__), "..", "config", "crewvia.yaml")) == 6


def test_reserve_does_not_apply_the_cap(box):
    """上限の検査は retry の入口だけ。既に上限を超えた card (execution_count 6) は今までどおり pull できる。"""
    failed_box(box, status="pending", execution_count=6)
    xid = take(box, "Sora")
    assert box.card()["execution_count"] == 7 and box.card()["current_execution_id"] == xid


def test_max_rework_is_a_separate_number(box):
    failed_box(box, rework_count=3)
    assert retry(box).returncode == 0                       # max_rework (3) は retry を止めない
    assert box.card()["rework_count"] == 3


# ---------------------------------------------------------------------------
# 再送の冪等 (台帳だけ書けた孤児)
# ---------------------------------------------------------------------------

def make_retry_orphan(box, **kw):
    tasks = box.queue / "missions" / MISSION / "tasks"
    tasks.chmod(0o500)
    try:
        p = retry(box, **kw)
    finally:
        tasks.chmod(0o700)
    assert p.returncode != 0
    return p


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")
def test_resend_after_an_orphan_ledger_adds_no_file(box):
    failed_box(box, handoff_path="/h/x_HANDOFF.md")
    make_retry_orphan(box)
    [rname] = retry_files(box)
    first = (ledger_dir(box) / rname).read_bytes()
    assert box.card()["status"] == "failed" and box.card()["handoff_path"] == "/h/x_HANDOFF.md"
    p = retry(box)
    assert p.returncode == 0, p.stderr
    assert retry_files(box) == [rname] and (ledger_dir(box) / rname).read_bytes() == first
    card = box.card()
    assert card["status"] == "pending" and card.get("handoff_path") is None
    assert len(ledger_files(box)) == 2                      # end + retry


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")
def test_a_resend_with_a_different_reason_is_a_history_mismatch(box):
    failed_box(box)
    make_retry_orphan(box, reason="最初の理由")
    before = box.snapshot()
    p = retry(box, reason="違う理由")
    assert p.returncode == 3 and last_error_code(p.stderr) == "HISTORY_MISMATCH"
    assert "reason" in p.stderr and "違う理由" not in p.stderr and "最初の理由" not in p.stderr
    assert box.snapshot() == before and box.card()["status"] == "failed"
    assert retry(box, reason="最初の理由").returncode == 0     # 出口は「同じ理由で送り直す」


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")
def test_store_check_reports_the_retry_orphan_until_the_resend_completes(box):
    failed_box(box)
    make_retry_orphan(box)
    r = box.plan("store-check", "--mission", MISSION)
    assert re.search(r"history_orphan.*retry", r.stdout), r.stdout
    assert retry(box).returncode == 0
    assert "history_orphan" not in box.plan("store-check", "--mission", MISSION).stdout


# ---------------------------------------------------------------------------
# retry → pull → previous_attempt / done
# ---------------------------------------------------------------------------

def test_pull_after_retry_shows_the_previous_attempt_with_its_result_handoff_and_reason(box):
    xid = failed_box(box, handoff_path="/h/Ren/m/t001_HANDOFF.md", fail_head="b" * 40)
    assert retry(box, reason="Codex 指摘 P2 を直す").returncode == 0
    p = box.pull("Sora", TID)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["execution_id"] != xid and out["attempt"] == 2
    prev = out["previous_attempt"]
    assert prev["execution_id"] == xid and prev["attempt"] == 1 and prev["end_code"] == "WORKER_FAILED"
    assert prev["handoff_path"] == "/h/Ren/m/t001_HANDOFF.md" and prev["retry_reason"] == "Codex 指摘 P2 を直す"
    assert prev["head"] == "b" * 40
    assert os.path.basename(prev["history_path"]).startswith("0001-end-")      # Result の全文がある台帳
    assert "FAILED" in open(prev["history_path"]).read()


def test_the_new_attempt_keeps_the_same_branch_name(box):
    xid = failed_box(box)
    slug = box.card()["task_slug"]
    assert retry(box).returncode == 0
    out = json.loads(box.pull("Sora", TID).stdout)
    assert box.card()["task_slug"] == slug and out["task_slug"] == slug


def test_retry_then_pull_then_done_closes_with_the_same_pr_number(box):
    failed_box(box, pr_number=7)
    assert retry(box).returncode == 0
    xid2 = take(box, "Sora")
    p = run(box, "done", TID, "fixed", "--pr", "7", "--mission", MISSION, "--execution", xid2, agent="Sora")
    assert p.returncode == 0, p.stderr
    card = box.card()
    assert card["status"] == "done" and card["pr_number"] == 7 and card["execution_count"] == 2
    names = ledger_files(box)
    assert [n.split("-")[1] for n in names] == ["end", "retry", "end"]


def test_two_retries_in_a_row_each_get_their_own_ledger_file(box):
    failed_box(box)
    assert retry(box, reason="1 回目").returncode == 0
    xid2 = take(box, "Sora")
    assert run(box, "fail", TID, "--no-head", "y", "--mission", MISSION, "--execution", xid2, agent="Sora").returncode == 0
    assert retry(box, reason="2 回目").returncode == 0
    assert [n.split("-")[1] for n in ledger_files(box)] == ["end", "retry", "end", "retry"]
    out = json.loads(box.pull("Ren", TID).stdout)
    assert out["previous_attempt"]["execution_id"] == xid2 and out["previous_attempt"]["retry_reason"] == "2 回目"
    assert out["attempt"] == 3


def test_update_reset_is_still_there_and_needs_no_reason(box):
    failed_box(box)
    p = run(box, "update", TID, "--status", "pending", "--reset", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    assert box.card()["status"] == "pending"


def test_retry_of_a_task_in_a_done_mission_reactivates_the_mission(box):
    failed_box(box)
    mission_yaml = box.queue / "missions" / MISSION / "mission.yaml"
    text = mission_yaml.read_text()
    mission_yaml.write_text(re.sub(r"^status: .*$", "status: done", text, flags=re.M)
                            if re.search(r"^status: ", text, re.M) else text + "status: done\n")
    assert retry(box).returncode == 0
    assert re.search(r"^status: in_progress$", mission_yaml.read_text(), re.M)


# ---------------------------------------------------------------------------
# store-check: 別の遷移で閉じた試行の孤児 (設計 §8 の 3 件目)
# ---------------------------------------------------------------------------

def make_done_orphan(box, xid):
    tasks = box.queue / "missions" / MISSION / "tasks"
    tasks.chmod(0o500)
    try:
        p = run(box, "done", TID, "RESULT-ONE", "--no-pr", "x", "--mission", MISSION, "--execution", xid, agent="Ren")
    finally:
        tasks.chmod(0o700)
    assert p.returncode != 0
    assert [n for n in ledger_files(box) if "-end-" in n], "done の台帳が書かれていない"


def orphan_lines(box):
    r = box.plan("store-check", "--mission", MISSION)
    assert r.returncode in (0, 1), r.stderr
    return [ln for ln in r.stdout.splitlines() if ln.startswith("history_orphan")]


pytestmark_root = pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")


@pytestmark_root
def test_active_orphan_is_still_reported(box):
    xid = take(box)
    make_done_orphan(box, xid)
    lines = orphan_lines(box)
    assert len(lines) == 1 and "end" in lines[0]


@pytestmark_root
def test_a_done_orphan_closed_by_reset_is_reported_even_after_the_next_pull(box):
    xid = take(box)
    make_done_orphan(box, xid)
    box.reset()
    lines = orphan_lines(box)
    assert len(lines) == 1 and "closed_by=RESET_BY_DIRECTOR" in lines[0], lines
    take(box, "Sora")                                       # 次の試行が current になっても、過去の試行の孤児は record で判定できる
    lines = orphan_lines(box)
    assert len(lines) == 1 and "closed_by=RESET_BY_DIRECTOR" in lines[0], lines


@pytestmark_root
def test_a_done_orphan_closed_by_close_execution_is_reported(box):
    xid = take(box)
    make_done_orphan(box, xid)
    set_card(box, status="failed")                          # Controller の外で手放された (DETACHED)
    p = run(box, "update", TID, "--close-execution", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    lines = orphan_lines(box)
    assert len(lines) == 1 and "closed_by=ABANDONED_OUTSIDE_CONTROLLER" in lines[0], lines


@pytestmark_root
def test_a_done_orphan_closed_by_retire_is_reported(box):
    xid = take(box)
    make_done_orphan(box, xid)
    p = run(box, "retire", TID, "--agent", "Ren", "--execution", xid, "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    assert box.card()["execution_end_code"] == "RETIRED"
    lines = orphan_lines(box)
    assert len(lines) == 1 and "closed_by=RETIRED" in lines[0], lines


@pytestmark_root
def test_a_done_that_the_card_followed_is_not_an_orphan(box):
    xid = take(box)
    make_done_orphan(box, xid)
    p = run(box, "done", TID, "RESULT-ONE", "--no-pr", "x", "--mission", MISSION, "--execution", xid, agent="Ren")
    assert p.returncode == 0, p.stderr                      # 再送が先へ進めた
    assert orphan_lines(box) == []


def test_a_normal_history_has_no_orphan_in_any_state(box):
    xid = take(box)
    assert orphan_lines(box) == []
    assert run(box, "done", TID, "r", "--no-pr", "x", "--mission", MISSION, "--execution", xid, agent="Ren").returncode == 0
    assert orphan_lines(box) == []
    box.reset()
    assert orphan_lines(box) == []


@pytestmark_root
def test_store_check_reports_each_orphan_once_and_writes_nothing(box):
    xid = take(box)
    make_done_orphan(box, xid)
    box.reset()
    before = box.snapshot()
    first = orphan_lines(box)
    second = orphan_lines(box)
    assert len(first) == 1 and first == second              # 2 回走らせても 1 件・同じ行
    assert box.snapshot() == before                         # 台帳・card・record を書かない
