"""done の mission に task が戻ってきたら mission を in_progress に戻す (vNext 02 PR-A / 設計 §4.2)。

dispatcher は mission.yaml の `status != done` で「全ミッション完了」を判定し idle Worker を退役させる。
done のまま `add` すると、足した task を誰も取れない (2026-10-11 の実例)。
"""

from __future__ import annotations

import re

import pytest

from pull_execution_helpers import Box, MISSION

M = MISSION


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path / "root", tasks=("t001", "t002"))
    # 回復は最初の書き込み系コマンドが行う。比較の前に済ませる
    p = b.plan("add", "warm-up", "--skills", "code", "--mission", M, agent="Director")
    assert p.returncode == 0, p.stderr
    return b


def _mission_path(box):
    return box.queue / "missions" / M / "mission.yaml"


def _mission_status(box):
    text = _mission_path(box).read_text()
    m = re.search(r"^status:\s*(\S+)\s*$", text, re.MULTILINE)
    assert m, text
    return m.group(1)


def _has_completed_at(box):
    return bool(re.search(r"^completed_at:\s*(?!null\b|~|$)\S", _mission_path(box).read_text(), re.MULTILINE))


def _make_done(box):
    """テスト設定: mission を done にする (本番の書き手は done / verify-result)。"""
    text = _mission_path(box).read_text()
    text = re.sub(r"^status:.*$", "status: done", text, flags=re.MULTILINE)
    text = re.sub(r"^completed_at:.*\n", "", text, flags=re.MULTILINE)
    text = text.rstrip("\n") + "\ncompleted_at: '2026-10-01T00:00:00+0900'\n"
    _mission_path(box).write_text(text)
    assert _mission_status(box) == "done" and _has_completed_at(box)


def test_add_to_done_mission_reactivates_it(box):
    _make_done(box)
    p = box.plan("add", "late task", "--skills", "code", "--mission", M, agent="Director")
    assert p.returncode == 0, p.stderr
    assert _mission_status(box) == "in_progress"
    assert not _has_completed_at(box)
    assert "Reactivated" in p.stdout


@pytest.mark.parametrize("args", [("--status", "pending"), ("--status", "in_progress"), ("--reset",)])
def test_update_returning_a_task_to_unfinished_reactivates(box, args):
    _make_done(box)
    p = box.plan("update", "t002", *args, "--mission", M, agent="Director")
    assert p.returncode == 0, p.stderr
    assert _mission_status(box) == "in_progress"
    assert not _has_completed_at(box)


@pytest.mark.parametrize("start", ["in_progress", "ready", "drafting"])
def test_not_done_mission_is_left_alone(box, start):
    text = _mission_path(box).read_text()
    _mission_path(box).write_text(re.sub(r"^status:.*$", f"status: {start}", text, flags=re.MULTILINE))
    before = _mission_path(box).read_text()
    p = box.plan("update", "t002", "--status", "pending", "--mission", M, agent="Director")
    assert p.returncode == 0, p.stderr
    assert _mission_status(box) == start
    assert "Reactivated" not in p.stdout
    assert _mission_path(box).read_text() == before


def test_update_not_returning_to_unfinished_keeps_done(box):
    _make_done(box)
    p = box.plan("update", "t002", "--priority", "low", "--mission", M, agent="Director")
    assert p.returncode == 0, p.stderr
    assert _mission_status(box) == "done"
