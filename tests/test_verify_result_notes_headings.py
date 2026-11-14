"""verify-result の notes の行頭 `#` 見出しは書き込み前に無害化される (#382 レビュー P3-3)。

notes に行頭 `## x` / `### y` があると `## Verification` 節・項目が割れ、(a) done / fail が本文を組み直すとき後ろが失われ、
(b) 再送の照合が外れて二重に記録され、(c) `result_section` の読みがずれた。直しは書き手 (`_verification_entry_tail`) の 1 か所。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import Box, report_argv, run, take

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lib_history  # noqa: E402

NOTES = "first line\n## x\n### y\n  ## indented ok\n#### z\nlast line"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def to_verification(box, xid):
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    assert run(box, *report_argv("verifying"), "--execution", xid, agent="verifier-dispatcher").returncode == 0


def verify_fail_with_notes(box):
    xid = take(box)
    to_verification(box, xid)
    argv = report_argv("verify-fail") + ["--notes", NOTES]
    assert run(box, *argv, "--execution", xid, agent="V1").returncode == 0
    return xid, argv


def heading_lines(text):
    return [ln for ln in text.split("\n") if ln.startswith("## ") or ln.startswith("### ")]


def test_note_headings_are_escaped_and_the_content_is_kept(box):
    verify_fail_with_notes(box)
    text = box.card_text()
    # 項目の見出し (`### <時刻>`) と節の見出しだけが行頭の見出し。notes の見出しは `\#` になり内容は残る
    assert [ln for ln in heading_lines(text) if ln.startswith("## ")].count("## Verification") == 1
    assert "\n## x\n" not in text and "\n### y\n" not in text and "\n#### z\n" not in text
    assert "\\## x" in text and "\\### y" in text and "\\#### z" in text
    assert "first line" in text and "last line" in text
    assert len([ln for ln in heading_lines(text) if ln.startswith("### ")]) == 1   # 項目は 1 つだけ


def test_resend_with_heading_notes_is_not_recorded_twice(box):
    xid, argv = verify_fail_with_notes(box)
    before, n = box.snapshot(), box.card_text().count("**Verdict:**")
    p = run(box, *argv, "--execution", xid, agent="V1")
    assert p.returncode == 0 and "idempotent" in p.stdout, (p.stdout, p.stderr)
    assert box.snapshot() == before and box.card_text().count("**Verdict:**") == n == 1


def test_recorded_matches_for_the_same_notes_and_not_for_others(box):
    verify_fail_with_notes(box)
    # _verification_recorded は plan.sh (bash + 埋め込み python) の中なので再送の挙動で固定する。違う notes は同じ記録とみなされない
    xid = box.card()["current_execution_id"]
    argv = report_argv("verify-fail") + ["--notes", NOTES + "\nmore"]
    p = run(box, *argv, "--execution", xid, agent="V1")
    assert "already recorded" not in p.stdout, (p.stdout, p.stderr)


def test_done_after_the_rejection_keeps_the_verification_section(box):
    verify_fail_with_notes(box)
    xid2 = take(box)                       # 新しい試行 (attempt 2)
    p = run(box, *report_argv("done"), "--no-pr", "test", "--execution", xid2)
    assert p.returncode == 0, (p.stdout, p.stderr)
    text = box.card_text()
    assert "## Verification" in text
    tail = text.split("## Verification", 1)[1]
    assert "last line" in tail and "\\## x" in tail and "**Verdict:** fail" in tail


def test_result_section_is_not_shifted_by_note_headings(box):
    verify_fail_with_notes(box)
    xid2 = take(box)
    assert run(box, *report_argv("done"), "--no-pr", "test", "--execution", xid2).returncode == 0
    body = box.card_text().split("---", 2)[2]
    result = lib_history.result_section(body)
    assert "last line" not in result and "\\## x" not in result and result.strip() != ""
