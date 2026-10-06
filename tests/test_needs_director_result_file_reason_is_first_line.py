"""`plan.sh needs-director --result-file` の理由 (frontmatter / 通知) は、ファイルの最初の空でない行だけ。

kai-review.sh は先頭行に 1 行の要約を置き、その後ろに見出し・file・本文を続ける。以前は split_long_freeform が全文を
' / ' で連結して 200 字で切ったため、要約の後ろに見出しや本文が混ざった (PR #282 QA t002)。本物の plan.sh を通す
(t001 の新テストは plan.sh をスタブにしていて見られなかった)。全文は `## Needs-Director 詳細` に残る。
"""

from __future__ import annotations

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import MISSION, Box, run, take

SUMMARY = "Codex の指摘 2 件 (P1 1 / P2 1) — 修正が要る"
FULL = SUMMARY + "\n\n## Findings\n- file: scripts/x.sh\n- body: " + "長い本文 " * 80 + "\n"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def nd(box, text, tmp_path):
    xid = take(box)
    f = tmp_path / "reason.md"
    f.write_text(text)
    return run(box, "needs-director", "t001", "--result-file", str(f), "--mission", MISSION, "--execution", xid)


def test_reason_is_only_the_first_line_and_the_full_text_stays_in_the_card(box, tmp_path):
    p = nd(box, FULL, tmp_path)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert box.card()["needs_director_reason"] == SUMMARY
    body = box.card_text()
    assert "## Needs-Director 詳細" in body and "scripts/x.sh" in body and "長い本文" in body


def test_leading_blank_lines_are_skipped(box, tmp_path):
    assert nd(box, "\n\n  " + FULL, tmp_path).returncode == 0
    assert box.card()["needs_director_reason"] == SUMMARY


def test_single_line_file_has_no_detail_section(box, tmp_path):
    assert nd(box, SUMMARY + "\n", tmp_path).returncode == 0
    assert box.card()["needs_director_reason"] == SUMMARY
    assert "## Needs-Director 詳細" not in box.card_text()


def test_overlong_first_line_is_cut_and_full_text_kept(box, tmp_path):
    first = "あ" * 300
    assert nd(box, first + "\nrest\n", tmp_path).returncode == 0
    r = box.card()["needs_director_reason"]
    assert r.startswith("あ" * 200) and "\n" not in r and len(r) < 230
    assert first in box.card_text()


def test_positional_reason_keeps_the_old_join(box):
    xid = take(box)
    p = run(box, "needs-director", "t001", "a\nb", "--mission", MISSION, "--execution", xid)
    assert p.returncode == 0, p.stderr
    assert box.card()["needs_director_reason"] == "a / b"


def test_single_overlong_line_without_newline_keeps_the_text_past_the_cut(box, tmp_path):
    # 改行のない 200 字超の 1 行: rest == first でも要約は切れているので全文を詳細節に残す (PR #282 t005 P1)
    text = "あ" * 300 + "END"
    assert nd(box, text, tmp_path).returncode == 0
    r = box.card()["needs_director_reason"]
    assert r.startswith("あ" * 200) and "END" not in r and "\n" not in r
    body = box.card_text()
    assert "## Needs-Director 詳細" in body and "あ" * 300 + "END" in body


LONG = "い" * 250
CASES = {
    "empty-ish": ("\n  \n", None),
    "one-short-line": ("短い 1 行 END", None),
    "one-long-line": (LONG + "END", None),
    "multi-short-first": ("短い先頭\n" + LONG + "END\n", None),
    "multi-long-first": (LONG + "\n本文 END\n", None),
    "leading-blank-then-short": ("\n\n短い先頭\n本文 END\n", None),
    "leading-blank-then-long": ("\n\n" + LONG + "\n本文 END\n", None),
}


@pytest.mark.parametrize("name", [k for k in CASES if k != "empty-ish"])
def test_full_text_survives_somewhere_for_every_shape(box, tmp_path, name):
    text, _ = CASES[name]
    assert nd(box, text, tmp_path).returncode == 0
    reason = box.card()["needs_director_reason"]
    whole = text.strip()
    # 理由がそのまま全文か、全文が card 本文のどこかに残る (どちらでもない = 一部が消えた)
    assert reason == whole or whole in box.card_text()
    assert "END" in reason or "END" in box.card_text()


def test_blank_only_file_is_refused_and_writes_nothing(box, tmp_path):
    xid = take(box)
    f = tmp_path / "reason.md"
    f.write_text(CASES["empty-ish"][0])
    p = run(box, "needs-director", "t001", "--result-file", str(f), "--mission", MISSION, "--execution", xid)
    assert p.returncode == 2
    assert "needs_director_reason" not in box.card()
