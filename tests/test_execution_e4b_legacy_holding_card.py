#!/usr/bin/env python3
"""01c E4b (t025): 本番に 1 件残る legacy の holding card (Minerva t017 の形) は、E4b の後も今どおり閉じられる。

merge 条件 (1) の数え方 (`execution.md` §7・`lib_execution.is_legacy_execution`) は「holding の status
(in_progress / ready_for_verification / verifying / needs_human_review) で `current_execution_id` が無い card」。本番の
`queue/missions/20260912-minerva-stage0-1/tasks/t017.md` は **needs_human_review・worker なし・started_at なし・枠なし・
execution の欄なし** で、この数え方には 1 件として入る。だが E4b が外すのは **世代 (started_at) の照合と旧形式 marker の読み口**
(§2.4 の 4 の #4・#8・#11・#12) で、その 4 つはどれも「worker・枠・started_at のどれか」を前提にする。この形の card は

- 退役の対象にならない (退役 marker は枠を持つ Worker の task にだけ作られる。この card は worker も枠も無い)
- `plan.sh retire` は worker 不一致で exit 3 (何も書かない)
- 枠の identity と比べる相手が無い (`classify_assignment` に至らない)
- 閉じる道 (`verify-result` / `update --status` / `update --reset`) は照合に世代を使わない (名乗りなしの経路。
  `caller_check=legacy_generation` の**ラベル**は残るが、世代で比べない)

ので、E4b の後も壊れず、照合できない経路が生まれない。この性質を、本番と同じ形の card を隔離 queue に置いて示す。
**本番の queue / registry には触れない** (`pull_execution_helpers.Box` が隔離する)。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import MISSION, OTHER_ID, Box, run

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lib_execution as ex  # noqa: E402
import lib_retirement as rt  # noqa: E402
import lib_task_status as task_status  # noqa: E402

TID = "t002"


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path / "root", tasks=("t001", TID))
    # 本番の t017 の形: needs_human_review・worker なし・started_at なし・execution の欄なし・枠なし
    e3.new_card(b, TID, "needs_human_review")
    return b


def _count_like_the_merge_condition(box):
    meta = box.card(TID)
    return ex.is_legacy_execution(meta, task_status.ASSIGNMENT_HOLDING_STATUSES)


def test_the_production_shape_is_counted_by_the_merge_condition_but_has_no_worker_slot_or_generation(box):
    meta = box.card(TID)
    assert _count_like_the_merge_condition(box) is True              # 数え方には 1 件として入る
    assert meta.get("worker") in (None, "null", "") and meta.get("started_at") in (None, "null", "")
    assert not ex.has_execution_fields(meta)
    assert box.slot("Ren") is None or box.slot("Ren") != f"{MISSION}:{TID}"


def test_no_retirement_can_name_it_so_the_generation_readers_E4b_removes_are_never_reached(box):
    """退役 marker の束縛: 束縛する試行が無い (None)。`plan.sh retire` はどの ID でも worker 不一致・試行なしで exit 3。"""
    assert rt.read_task_execution_id(box.queue, MISSION, TID) is None
    before = box.snapshot()
    p = run(box, "retire", TID, "--agent", "Ren", "--mission", MISSION, "--execution", OTHER_ID, agent="watchdog")
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert box.snapshot() == before


@pytest.mark.parametrize("verdict,expect", [("pass", "verified"), ("fail", "pending")])
def test_verify_result_still_closes_it_without_a_generation_or_an_invented_attempt(box, verdict, expect):
    """`verify-result` (Director / reviewer の閉じ方) は名乗りなしで通る。試行の欄を発明しない。"""
    p = run(box, "verify-result", TID, verdict, "--mission", MISSION, "--notes", "ok", agent="Director")
    assert p.returncode == 0, (p.stdout, p.stderr)
    m = box.card(TID)
    assert m["status"] == expect
    assert not ex.has_execution_fields(m)                            # 試行を発明しない (原案 §9.1)
    rows = [r for r in e3.audit_rows_for(box, "verify-result") if r["result"] == "ok"]
    assert rows and rows[-1]["caller_check"] in ("legacy_generation", "no_execution"), rows


def test_update_reset_still_reopens_it(box):
    p = run(box, "update", TID, "--status", "pending", "--reset", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, (p.stdout, p.stderr)
    m = box.card(TID)
    assert m["status"] == "pending" and not ex.has_execution_fields(m)


def test_a_later_pull_of_it_issues_an_attempt_the_normal_way(box):
    """閉じた (または reset した) 後の再 pull は、新しい試行を発行する (legacy ではなくなる)。"""
    assert run(box, "update", TID, "--status", "pending", "--reset", "--mission", MISSION, agent="Director").returncode == 0
    p = run(box, "pull", "--agent", "Ren", "--skills", "bash,code", "--task", TID, "--mission", MISSION, agent="Ren")
    assert p.returncode == 0, (p.stdout, p.stderr)
    m = box.card(TID)
    assert ex.is_execution_id(m["current_execution_id"]) and m["execution_status"] == "running"
    assert _count_like_the_merge_condition(box) is False
