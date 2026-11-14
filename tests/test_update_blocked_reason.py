"""plan.sh update --blocked-reason (CI 待ちの運用改善 P3 12)。

付く / 空・長すぎ・改行は拒否 / blocked にならない card への指定は拒否 / 戻すと消える / lint が通る・案内を出す / help。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from test_assignment_routing import MISSION, Sandbox, _card  # noqa: E402,F401
import lint_plan  # noqa: E402

pytestmark = pytest.mark.structural


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


def _update(sb, *args):
    return sb.run("update", "t001", "--mission", MISSION, *args)


def _meta(sb):
    meta, _ = lint_plan.lib_task_cards.parse_frontmatter(sb.text("t001"))
    return meta


def test_status_blocked_with_reason_is_written_and_lints_clean(sb):
    sb.card("t001")
    r = _update(sb, "--status", "blocked", "--blocked-reason", "先行 PR #12 の merge 待ち")
    assert r.returncode == 0, (r.stdout, r.stderr)
    meta = _meta(sb)
    assert meta["status"] == "blocked"
    assert meta["blocked_reason"] == "先行 PR #12 の merge 待ち"
    assert lint_plan.check_frontmatter([meta]) == []


def test_reason_alone_rewrites_the_reason_of_a_blocked_card(sb):
    sb.card("t001", status="blocked", extra=['blocked_reason: "古い"'])
    r = _update(sb, "--blocked-reason", "新しい")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _meta(sb)["blocked_reason"] == "新しい"


@pytest.mark.parametrize("reason", ["", "   ", "a\nb", "a\tb", "x" * 501])
def test_unusable_reason_is_refused_and_nothing_is_written(sb, reason):
    sb.card("t001")
    before = sb.snapshot()
    r = _update(sb, "--status", "blocked", "--blocked-reason", reason)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert sb.snapshot() == before


def test_500_chars_is_accepted(sb):
    sb.card("t001")
    assert _update(sb, "--status", "blocked", "--blocked-reason", "x" * 500).returncode == 0


@pytest.mark.parametrize("status_args", [[], ["--status", "pending"], ["--status", "in_progress"], ["--reset"]])
def test_reason_on_a_card_that_will_not_be_blocked_is_refused(sb, status_args):
    sb.card("t001", status="pending")
    before = sb.snapshot()
    r = _update(sb, *status_args, "--blocked-reason", "理由")
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "blocked" in r.stderr
    assert sb.snapshot() == before


@pytest.mark.parametrize("back", [["--status", "pending"], ["--status", "in_progress"], ["--reset"]])
def test_leaving_blocked_clears_the_reason(sb, back):
    sb.card("t001", status="blocked", extra=['blocked_reason: "待ち"'])
    r = _update(sb, *back)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "blocked_reason" not in sb.text("t001")


def test_unrelated_update_keeps_the_reason_of_a_blocked_card(sb):
    sb.card("t001", status="blocked", extra=['blocked_reason: "待ち"'])
    assert _update(sb, "--priority", "high").returncode == 0
    assert _meta(sb)["blocked_reason"] == "待ち"


def test_blocked_without_reason_warns_with_the_fix(sb):
    sb.card("t001")
    r = _update(sb, "--status", "blocked")
    assert r.returncode == 0
    assert "--blocked-reason" in r.stderr


def test_lint_message_guides_to_the_command():
    results = lint_plan.check_frontmatter([{"id": "t001", "title": "x", "skills": ["code"],
                                            "status": "blocked", "priority": "high"}])
    msgs = [m for lvl, _c, m in results if lvl == "FAIL"]
    assert any('plan.sh update t001 --status blocked --blocked-reason "..."' in m for m in msgs), msgs


def test_help_shows_the_option(sb):
    r = sb.run("update", "--help")
    assert "--blocked-reason" in (r.stdout + r.stderr)
    r = sb.run("--help")
    assert "--blocked-reason" in (r.stdout + r.stderr)
