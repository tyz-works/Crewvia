"""Rule 5 / rex_screen_state: 入力欄の上のスピナーを working と判定する (t001)。

esc to interrupt が出ない今の Claude Code でも作業中を idle と誤報しない。
定義は lib_mux.spinner_above_input_box の 1 か所で、IRC の _is_busy と共有。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lib_mux  # noqa: E402
import lib_irc_relay  # noqa: E402

RULE = "─" * 40
BOX = f"{RULE}\n❯ \n{RULE}\n  📁 crewvia ⚠️  main │ Opus 5.5\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"


def screen(above):
    return "some earlier output\n" + above + BOX


SPINNERS = [
    "✽ Catapulting… (1m 24s · ↓ 6.1k tokens · thinking)",  # 11-14 Seo
    "✽ Precipitating… 6m",                                  # 11-08
    "· Lollygagging… 3m",                                   # 11-08
    "✻ Discombobulating… (thought for 5s)",
    "running PostToolUse hooks… (5/6)",
]


@pytest.mark.parametrize("spinner", SPINNERS)
def test_spinner_directly_above_box_is_working(spinner):
    assert lib_mux.rex_screen_state(screen(spinner + "\n")) == "working"


@pytest.mark.parametrize("blanks", [1, 5, 8])
def test_spinner_with_blank_lines_before_the_box_is_working(blanks):
    s = screen(SPINNERS[0] + "\n" + "\n" * blanks)
    assert lib_mux.rex_screen_state(s) == "working"


def test_spinner_with_blank_lines_and_todo_rows_is_working():
    todo = "  ⎿  ☐ one\n     ☒ two\n     ☐ three\n"
    s = screen(SPINNERS[0] + "\n" + todo + "\n" * 8)
    assert lib_mux.rex_screen_state(s) == "working"


def test_indented_hooks_row_under_spinner_is_working():
    s = screen(SPINNERS[1] + "\n  ⎿  Running PostToolUse hooks… (5/6)\n")
    assert lib_mux.rex_screen_state(s) == "working"


@pytest.mark.parametrize("above", [
    "✻ Cooked for 5s\n",                       # 完了後 (… が無い)
    "* Next steps… are listed below\n",        # 箇条書き本文
    "All done. Tests pass.\n",
    "",
])
def test_idle_and_finished_screens_stay_idle(above):
    assert lib_mux.rex_screen_state(screen(above)) == "idle"


def test_old_scrollback_spinner_beyond_window_is_not_picked_up():
    s = screen(SPINNERS[0] + "\n" + "\n" * (lib_mux.SPINNER_WINDOW + 2))
    assert lib_mux.rex_screen_state(s) == "idle"


def test_old_spinner_followed_by_text_is_idle():
    s = screen(SPINNERS[0] + "\nDone: edited 3 files.\n")
    assert lib_mux.rex_screen_state(s) == "idle"


def test_dialog_still_wins_over_spinner():
    dlg = ("Do you want to proceed?\n❯ 1. Yes\n  2. No\n\nEsc to cancel\n")
    assert lib_mux.rex_screen_state(screen(SPINNERS[0] + "\n") + dlg) == "blocked"


def test_no_input_box_is_unknown():
    assert lib_mux.rex_screen_state(SPINNERS[0] + "\n") == "unknown"
    assert lib_mux.rex_screen_state("") == "unknown"


@pytest.mark.parametrize("spinner", SPINNERS)
def test_irc_busy_shares_the_definition(spinner):
    lines = screen(spinner + "\n" + "\n" * 8).splitlines()
    assert lib_irc_relay._is_busy(lines) is True
    assert lib_irc_relay._is_busy(screen("✻ Cooked for 5s\n").splitlines()) is False
