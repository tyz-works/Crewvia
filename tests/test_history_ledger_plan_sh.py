"""試行の台帳 (vNext 02 PR-D) — plan.sh を通した外から見える挙動 (設計 §3.2・§8)。

隔離 plan.sh + 隔離 queue (`pull_execution_helpers.Box`)。本番の queue / registry には触れない。

* done / fail / needs-director の台帳と `result_execution_id` の帰属 (`--result-file` あり / なし)
* 同じ報告の再送で新しいファイルが増えず、`ended_at` が台帳・card・record で一致する / 違う Result の再送は exit 3 (`HISTORY_MISMATCH`)
* `plan.sh history` (読み取り専用・壊れたファイルは飛ばさず止まる)・`pull` の `previous_attempt`・`store-check` の報告
* mission の退避 (`plan.sh archive`) で `history/` が一緒に動く
* `lib_history.result_section` が plan.sh の `parse_task_body` と同じ読み方をすること
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import MISSION, Box, run, take, last_error_code
from execution_e3_helpers import SECRET  # noqa: F401  (pull_execution_helpers 由来)

import lib_history as hist  # noqa: E402

TID = "t001"


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


def done(box, xid, result="RESULT-ONE", *extra, tid=TID, agent="Ren"):
    return run(box, "done", tid, result, "--no-pr", "x", "--mission", MISSION, "--execution", xid, *extra, agent=agent)


def fail(box, xid, *extra, tid=TID, agent="Ren"):
    return run(box, "fail", tid, "--no-head", "x", "--mission", MISSION, "--execution", xid, *extra, agent=agent)


# ---------------------------------------------------------------------------
# 報告ごとの台帳と帰属
# ---------------------------------------------------------------------------

def test_done_positional_and_result_file_both_record_the_result_and_attribute_it(tmp_path):
    for variant in ("positional", "file"):
        box = Box(tmp_path / variant, tasks=("t001",))
        xid = take(box)
        if variant == "positional":
            p = done(box, xid, "THE-RESULT")
        else:
            f = tmp_path / "result.txt"
            f.write_text("THE-RESULT\n")
            p = run(box, "done", TID, "--result-file", str(f), "--no-pr", "x", "--mission", MISSION,
                    "--execution", xid, agent="Ren")
        assert p.returncode == 0, p.stderr
        [name] = ledger_files(box)
        fields, body = ledger(box, name)
        assert fields["result"] == "recorded" and body.strip() == "THE-RESULT" and fields["end_code"] == "DONE"
        assert box.card()["result_execution_id"] == xid
        assert box.record(xid)["ended_at"] == fields["ended_at"]


def test_fail_records_the_failed_result_and_needs_director_leaves_the_attribution_alone(box):
    xid = take(box)
    assert fail(box, xid).returncode == 0
    [name] = ledger_files(box)
    fields, body = ledger(box, name)
    assert fields["end_code"] == "WORKER_FAILED" and fields["result"] == "recorded"
    assert body.startswith("FAILED — head: none")
    assert box.card()["result_execution_id"] == xid

    box2 = Box(box.root.parent / "second", tasks=("t001",))
    xid2 = take(box2)
    p = run(box2, "needs-director", TID, "LONG\nreason", "--mission", MISSION, "--execution", xid2)
    assert p.returncode == 0, p.stderr
    [name2] = ledger_files(box2)
    fields2, body2 = ledger(box2, name2)
    assert fields2["end_code"] == "NEEDS_DIRECTOR" and fields2["result"] == "unattributed" and body2.strip() == ""
    assert "result_execution_id" not in box2.card()                    # Result 節を置き換えていない


def test_a_resend_of_a_finished_report_adds_no_file(box):
    xid = take(box)
    assert done(box, xid).returncode == 0
    before = box.snapshot()
    again = done(box, xid)
    assert again.returncode == 0 and "idempotent" in again.stdout
    assert box.snapshot() == before and len(ledger_files(box)) == 1


# ---------------------------------------------------------------------------
# 孤児の台帳の後の再送
# ---------------------------------------------------------------------------

def make_orphan(box, xid, result="RESULT-ONE"):
    """card の書き込みだけを失敗させる (tasks/ を読み取り専用にする)。台帳は先に書かれる。"""
    tasks = box.queue / "missions" / MISSION / "tasks"
    tasks.chmod(0o500)
    try:
        p = done(box, xid, result)
    finally:
        tasks.chmod(0o700)
    assert p.returncode != 0
    return p


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")
def test_resend_after_an_orphan_ledger_keeps_one_file_and_one_ended_at(box):
    xid = take(box)
    make_orphan(box, xid)
    [name] = ledger_files(box)
    first = (ledger_dir(box) / name).read_bytes()
    assert box.card()["status"] == "in_progress"
    time.sleep(1.2)                                                   # 再送の now を作り直すと秒が違う
    p = done(box, xid)
    assert p.returncode == 0, p.stderr
    assert ledger_files(box) == [name] and (ledger_dir(box) / name).read_bytes() == first
    fields, _ = ledger(box, name)
    assert box.record(xid)["ended_at"] == fields["ended_at"]
    assert box.card()["status"] == "done"
    assert box.card()["completed_at"] == fields["ended_at"]            # t021 F1: card の完了時刻も台帳の時刻 (再送の now ではない)


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")
def test_fail_resend_after_an_orphan_ledger_keeps_completed_at_equal_to_ended_at(box):
    xid = take(box)
    tasks = box.queue / "missions" / MISSION / "tasks"
    tasks.chmod(0o500)
    try:
        assert fail(box, xid).returncode != 0
    finally:
        tasks.chmod(0o700)
    [name] = ledger_files(box)
    time.sleep(1.2)
    assert fail(box, xid).returncode == 0
    fields, _ = ledger(box, name)
    assert box.card()["completed_at"] == fields["ended_at"] == box.record(xid)["ended_at"]


def test_needs_director_reason_is_recorded_in_the_ledger(box):
    xid = take(box)
    assert run(box, "needs-director", TID, "REASON-ONE", "--mission", MISSION, "--execution", xid).returncode == 0
    [name] = ledger_files(box)
    assert ledger(box, name)[0]["reason"] == "REASON-ONE"


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0o500 の dir にも書ける")
def test_a_different_result_after_an_orphan_is_refused_with_exit_3_and_writes_nothing(box):
    xid = take(box)
    make_orphan(box, xid, "RESULT-ONE")
    before = box.snapshot()
    p = done(box, xid, f"CHANGED {SECRET}")
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert last_error_code(p.stderr) == "HISTORY_MISMATCH"
    assert SECRET not in p.stdout + p.stderr and "RESULT-ONE" not in p.stdout + p.stderr
    assert "body" in p.stderr                                          # 違う欄の名前だけ出る
    assert box.snapshot() == before
    assert any(r["result"] == "refused:HISTORY_MISMATCH" for r in box.audit_rows())
    assert done(box, xid, "RESULT-ONE").returncode == 0                # 出口: 同じ報告を送り直す


# ---------------------------------------------------------------------------
# history / previous_attempt / store-check
# ---------------------------------------------------------------------------

def second_attempt(box):
    xid1 = take(box)
    assert fail(box, xid1).returncode == 0
    box.reset()
    xid2 = take(box)
    return xid1, xid2


def test_history_lists_every_file_in_order_and_writes_nothing(box):
    xid1, xid2 = second_attempt(box)
    assert done(box, xid2, "SECOND-RESULT").returncode == 0
    before = box.snapshot(with_audit=True)
    p = run(box, "history", TID, "--mission", MISSION)
    assert p.returncode == 0, p.stderr
    heads = re.findall(r"^==> (\S+)$", p.stdout, re.M)
    assert heads == [f"0001-end-{xid1}.md", f"0002-end-{xid2}.md"]
    assert "WORKER_FAILED" in p.stdout and "SECOND-RESULT" in p.stdout
    assert box.snapshot(with_audit=True) == before


def test_history_stops_at_a_broken_file_instead_of_skipping_it(box):
    xid1, xid2 = second_attempt(box)
    assert done(box, xid2).returncode == 0
    (ledger_dir(box) / f"0001-end-{xid1}.md").write_text(f"garbage {SECRET}")
    p = run(box, "history", TID, "--mission", MISSION)
    assert p.returncode == 1
    assert p.stdout == "" and SECRET not in p.stderr                   # 残りを出さない・中身を出さない
    assert f"0001-end-{xid1}.md" in p.stderr


def test_history_of_a_task_without_a_ledger_is_empty_and_exit_0(box):
    p = run(box, "history", TID, "--mission", MISSION)
    assert p.returncode == 0 and p.stdout == ""


def test_pull_json_carries_previous_attempt_only_after_an_earlier_attempt(box):
    p = box.pull("Ren")
    assert "previous_attempt" not in json.loads(p.stdout)
    xid1 = json.loads(p.stdout)["execution_id"]
    assert fail(box, xid1).returncode == 0
    box.reset()
    q = box.pull("Ren")
    assert q.returncode == 0, q.stderr
    prev = json.loads(q.stdout)["previous_attempt"]
    assert prev["execution_id"] == xid1 and prev["attempt"] == 1 and prev["end_code"] == "WORKER_FAILED"
    assert prev["ended_at"] and os.path.isabs(prev["history_path"]) and os.path.isfile(prev["history_path"])
    assert pathlib.Path(prev["history_path"]).name == f"0001-end-{xid1}.md"
    assert set(prev) <= {"execution_id", "attempt", "end_code", "ended_at", "head", "history_path", "checkpoint_dir"}


def test_pull_warns_but_still_runs_when_the_ledger_is_unreadable(box):
    xid1 = take(box)
    assert fail(box, xid1).returncode == 0
    box.reset()
    (ledger_dir(box) / f"0001-end-{xid1}.md").write_text("garbage")
    q = box.pull("Ren")
    assert q.returncode == 0 and "previous_attempt" not in json.loads(q.stdout)
    assert "previous_attempt を出しません" in q.stderr and "garbage" not in q.stderr


def test_store_check_reports_orphans_duplicates_and_unreadable_files_without_writing(box):
    xid = take(box)
    d = ledger_dir(box)
    d.mkdir(parents=True)
    fields, body = hist.build_event(kind="end", execution_id=xid, attempt=1, ended_at="2026-10-01T00:00:00Z",
                                    end_code="DONE", result_state="none")
    text = hist.render(fields, body)
    (d / f"0001-end-{xid}.md").write_text(text)                        # 試行はまだ running: 孤児
    (d / f"0002-end-{xid}.md").write_text(text)                        # 同じ同定キーが 2 つ
    other = "ex-" + "2" * 32
    (d / f"0003-end-{other}.md").write_text("garbage")                 # 読めない
    ghost = box.queue / "missions" / MISSION / "history" / "t099"
    ghost.mkdir(parents=True)                                          # card の無い台帳
    before = box.snapshot(with_audit=True)
    out = box.store_check()
    assert "history_orphan" in out and "history_duplicate_key" in out
    assert "history_unobservable" in out and "history_without_card" in out
    assert "garbage" not in out
    assert box.snapshot(with_audit=True) == before


def test_store_check_is_quiet_about_a_clean_ledger(box):
    xid = take(box)
    assert done(box, xid).returncode == 0
    assert "history_" not in box.store_check()


# ---------------------------------------------------------------------------
# mission の退避
# ---------------------------------------------------------------------------

def test_archiving_the_mission_moves_the_ledger_with_it(box):
    xid = take(box)
    assert done(box, xid).returncode == 0
    [name] = ledger_files(box)
    p = run(box, "archive", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    assert not (box.queue / "missions" / MISSION).exists()
    moved = list((box.queue / "archive").glob(f"*/history/{TID}/{name}"))
    assert len(moved) == 1 and moved[0].read_text().count("RESULT-ONE") == 1


# ---------------------------------------------------------------------------
# Result 節の読み方は plan.sh の parse_task_body と同じ
# ---------------------------------------------------------------------------

SAMPLES = [
    "## Description\nd\n\n## Result\nR\n",
    "## Description\nd\n\n## Result\n",
    "## Description\nd\n\n## Result\nline1\n\n## Needs-Director 詳細\nAPPENDIX\n",
    "## Description\nd\n\n## Result\nR\n\n## Verification\n- pass\n",
    "## Description\nd\n\n## Result\nwith ## Result inside\n## Description\nagain\ntail\n",
    "## Description\nd\n",
    "",
]


def test_result_section_agrees_with_plan_sh_parse_task_body(tmp_path):
    import task_graph_publisher_harness as harness
    b = Box(tmp_path / "ns", tasks=("t001",))
    ns = harness.load_plan_namespace(b.plan_sh, str(b.queue), str(b.root))
    for body in SAMPLES:
        _desc, result = ns["parse_task_body"](body)
        trailing = ns["extract_trailing_body_section"](body)
        expected = result[: len(result) - len(trailing)].rstrip() if trailing else result
        assert hist.result_section(body) == expected.strip(), body
