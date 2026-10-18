"""PR-F intent の穴 6 (vNext 02 PR-F intent fix 6 / t072。Codex 指摘 P1 × 1)。

指摘: intent を書く前の検査が done_set (戻す task) の台帳しか見ず、reset_set (試行を閉じる task) を見なかった。孤児の reset の台帳が
ある実行中の下流で、reopen は intent を書いてから `HISTORY_MISMATCH` で止まり、凍結された mission は元の reset も打ち直せない。
検査を種類ごとに足すのをやめ、**intent が行う書き込みの全部を実行と同じ関数の dry_run で通す**。1 つでも通らなければ intent を書かない
(exit 3・何も書かない)。総当たりの次元: 「各対象 (I / 戻す下流 / 閉じる下流) に、中身の違う孤児の台帳がある」。
"""

from __future__ import annotations

import pytest

from execution_e3_helpers import MISSION, last_error_code, run

import lib_history as hist  # noqa: E402

from test_reopen_cascade_plan_sh import CHAIN, build, finish_all, ledger_dir, ledger_files, reopen, running_downstream, status  # noqa: E402
from test_reopen_intent_recovery import intents  # noqa: E402


def _orphan(box, tid, kind):
    """tid の今の試行に、reopen / reset の台帳を**中身の違う形**で 1 つ足す (cascade の `reason` / `root` が無い)。"""
    card = box.card(tid)
    fields, body = hist.build_event(
        kind=kind, execution_id=card["current_execution_id"], attempt=card["execution_count"], ended_at="2026-01-01T00:00:00Z",
        end_code="RESET_BY_DIRECTOR" if kind == "reset" else None, reopen_count=card.get("reopen_count") or 0,
        worker=card.get("worker"), actor="Director")
    d = ledger_dir(box, tid)
    d.mkdir(parents=True, exist_ok=True)
    name = hist.filename(hist.next_seq([p.name for p in d.iterdir()]), kind, card["current_execution_id"])
    (d / name).write_text(hist.render(fields, body))
    return name


def _box(tmp_path):
    """I(t001) done → A(t002) done → R(t003, review) 実行中。cascade reviews は A を戻し R の試行を閉じる。"""
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002"))
    running_downstream(box)
    return box


# (孤児を置く task, kind)。I = 戻す起点・A = 戻す下流 (done_set)・R = 試行を閉じる下流 (reset_set。Codex の再現)
TARGETS = {"root": ("t001", "reopen"), "done_downstream": ("t002", "reopen"), "reset_downstream": ("t003", "reset")}


@pytest.mark.parametrize("where", sorted(TARGETS))
def test_an_orphan_ledger_on_any_target_is_refused_before_the_intent_is_written(tmp_path, where):
    box = _box(tmp_path)
    tid, kind = TARGETS[where]
    _orphan(box, tid, kind)
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "HISTORY_MISMATCH", (where, p.returncode, p.stderr)
    assert intents(box) == [] and box.snapshot() == before, where           # intent も card も台帳も 1 バイトも書かない
    assert status(box, "t001") == "done" and status(box, "t003") == "in_progress"


def test_the_original_reset_can_still_be_retried_after_the_refusal(tmp_path):
    """抜け出せない状態にならない: 拒否の後も mission は凍結されず、元の `update --reset` が通る。"""
    box = _box(tmp_path)
    _orphan(box, "t003", "reset")
    assert reopen(box).returncode == 3
    p = run(box, "update", "t003", "--reset", "--mission", MISSION, agent="Director")
    assert p.returncode == 3 and last_error_code(p.stderr) != "REOPEN_INTENT_GRAPH_FROZEN", p.stderr   # 台帳の食い違いで止まるだけ
    assert intents(box) == []


def test_without_an_orphan_the_same_reopen_still_succeeds_and_closes_the_running_downstream(tmp_path):
    box = _box(tmp_path)
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert intents(box) == [] and status(box, "t003") == "pending" and len(ledger_files(box, "t003", "reset")) == 1

