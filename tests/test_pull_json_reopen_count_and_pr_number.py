"""`plan.sh pull` の JSON は card の `reopen_count` と `pr_number` を欄として出す。

`pr_number` は欄ごと無かった (Worker は card を読まないと PR を知れない)。`reopen_count` は
agents/worker.md の「reopen された task は前の結果を見直す回」の判定に使う。欄の無い古い card は 0 / None。
"""

from __future__ import annotations

import json

import pytest

import pull_execution_helpers as h
from pull_execution_helpers import Box, MISSION


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root")


def _pull_json(box):
    p = box.pull("Ren")
    assert p.returncode == 0, (p.stdout, p.stderr)
    return json.loads(p.stdout)


def _write_fields(box, **fields):
    text = box.card_text()
    meta = box.card()
    body = text.split("\n---\n", 1)[1]
    with h.store.transaction(box.queue, op="seed", actor="test") as t:
        t.write_card(MISSION, "t001", {**meta, **fields}, body)


def test_old_card_without_the_fields_gives_zero_and_none(box):
    meta = box.card()
    assert "reopen_count" not in meta and "pr_number" not in meta
    res = _pull_json(box)
    assert res["reopen_count"] == 0 and "pr_number" in res and res["pr_number"] is None


def test_card_values_are_copied(box):
    _write_fields(box, reopen_count=2, pr_number=321)
    res = _pull_json(box)
    assert res["reopen_count"] == 2 and res["pr_number"] == 321


def test_before_and_after_reopen_counts_differ(box):
    _write_fields(box, reopen_count=0)
    assert _pull_json(box)["reopen_count"] == 0
    box.reset()
    _write_fields(box, reopen_count=1, pr_number=7)
    res = _pull_json(box)
    assert (res["reopen_count"], res["pr_number"]) == (1, 7)
