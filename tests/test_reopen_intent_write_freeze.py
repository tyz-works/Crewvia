"""PR-F intent の穴 4 (vNext 02 PR-F intent fix 4 / t067。Codex 指摘 P1 × 1)。

指摘: intent の一覧に無い A を、回復が止まっている間に `update --status in_progress --worker …` で動かせる。回復は A を置いたまま
intent を消し、A が legacy の報告で done → R が I より先に pull できる。4 巡とも「許している書き込みが割り込む」形だったので、
1 つずつ塞ぐのをやめて **未完の intent がある mission の書き込みを allowlist で凍結する**。

二重の網:
  1. `_INTENT_FREEZE` (コマンド名 → allow / refuse / own の表) に載っていない書き込みは、完了できない intent がある間は拒否する。
  2. 打ち直しの検査は、一覧に無い task が実行中でも止める (1 を通り抜けた書き込み = 手編集・旧形式の書き手でも)。
テストは 1 と 2 を別々に赤にできる形にしてある。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from execution_e3_helpers import MISSION, run, set_card

from test_reopen_cascade_plan_sh import SKILLS, status, store_check  # noqa: E402
from test_reopen_intent_graph_frozen import (  # noqa: E402
    FROZEN, SPEC, TIDS, _bypass_rewire, _code, _stopped_box, _stuck_box,
)
from test_reopen_intent_recovery import intents  # noqa: E402

PLAN_SH = Path(__file__).resolve().parent.parent / "scripts" / "plan.sh"


def _src():
    return PLAN_SH.read_text()


def _table():
    src = _src()
    m = re.search(r"^_INTENT_FREEZE = \{(.*?)^\}", src, re.S | re.M)
    assert m, "_INTENT_FREEZE が見つからない"
    return dict(re.findall(r"'([a-z-]+)': '(allow|refuse|own)'", m.group(1)))


def _dispatch():
    m = re.search(r"^dispatch = \{(.*?)^\}", _src(), re.S | re.M)
    return re.findall(r"'([a-z-]+)': cmd_", m.group(1))


# ---------------------------------------------------------------------------
# 構造: 表が plan.sh の全サブコマンドを覆う
# ---------------------------------------------------------------------------

def test_every_plan_sh_subcommand_is_in_the_freeze_table_and_nothing_else():
    """新しいサブコマンドを足したら、通すか止めるかを表で決めない限り赤になる (allowlist。載っていない = 判定の漏れ)。"""
    table, commands = _table(), _dispatch()
    assert len(commands) > 20
    assert sorted(table) == sorted(commands)


def test_every_refuse_row_calls_the_gate_inside_its_own_function():
    src = _src()
    for cmd, verdict in _table().items():
        if verdict != "refuse":
            continue
        fn = "cmd_" + cmd.replace("-", "_")
        m = re.search(rf"^def {fn}\(.*?(?=^def |\Z)", src, re.S | re.M)
        assert m, fn
        assert f"_refuse_write_while_intent_pending(slug, '{cmd}'" in m.group(0), f"{cmd} が門を呼んでいない"


def test_the_table_rows_the_director_decided():
    t = _table()
    for cmd in ("reopen", "retire", "done", "fail", "needs-director", "ready-for-verification", "verifying", "verify-result",
                "status", "history", "store-check", "lint", "snapshot"):
        assert t[cmd] in ("allow", "own"), cmd
    for cmd in ("update", "add", "release-dep", "retry"):
        assert t[cmd] == "refuse", cmd
    for cmd in ("pull", "archive"):
        assert t[cmd] == "own", cmd


# ---------------------------------------------------------------------------
# 網 1: 凍結 (止める側の全行を 1 つずつ)
# ---------------------------------------------------------------------------

def _update(*argv):
    return lambda box: run(box, "update", *argv, "--mission", MISSION, agent="Director")


FREEZE_ROWS = {
    # Codex の再現と、同じ形 (status を動かす update)
    "update --status in_progress": _update("t004", "--status", "in_progress", "--worker", "Mika"),
    "update --status skipped": _update("t004", "--status", "skipped"),
    "update --status pending": _update("t002", "--status", "pending"),
    "update --blocked-by": _update("t003", "--blocked-by", "t004"),
    "update --priority": _update("t004", "--priority", "low"),
    "update --description": _update("t004", "--description", "x"),
    "update --skills": _update("t003", "--skills", "review,qa"),
    "add": lambda box: run(box, "add", "無関係な task", "--skills", "code", "--mission", MISSION, agent="Director"),
    "add --blocked-by": lambda box: run(box, "add", "新しい下流", "--skills", "code", "--blocked-by", "t001",
                                        "--mission", MISSION, agent="Director"),
    "release-dep": lambda box: run(box, "release-dep", "t003", "--dep", "t002", "--mission", MISSION, agent="Director"),
    "retry": lambda box: run(box, "retry", "t004", "--reason", "x", "--mission", MISSION, agent="Director"),
}


@pytest.mark.parametrize("name", sorted(FREEZE_ROWS))
def test_a_frozen_write_is_refused_and_writes_nothing(tmp_path, name):
    box = _stuck_box(tmp_path)
    before = box.snapshot()
    p = FREEZE_ROWS[name](box)
    assert p.returncode == 3 and _code(p) == FROZEN, (name, p.returncode, p.stderr)
    assert "plan.sh reopen" in p.stderr, p.stderr                       # 出口が文面にある
    assert box.snapshot() == before, name
    assert intents(box) == ["t001-1.json"] and status(box, "t001") == "done"


def test_the_codex_repro_is_refused_by_the_gate(tmp_path):
    """指摘そのもの: 一覧に無い t004 (done) を、止まっている間に動かす。"""
    box = _stuck_box(tmp_path)
    p = _update("t004", "--status", "in_progress", "--worker", "Mika")(box)
    assert p.returncode == 3 and _code(p) == FROZEN, p.stderr
    assert status(box, "t004") == "done"


def test_the_allowed_rows_still_work_while_the_intent_is_stuck(tmp_path):
    """読み取りは通る。止めた側の拒否が他の mission の書き込みを巻き込まない (止めるのは intent のある mission だけ)。"""
    box = _stuck_box(tmp_path)
    for argv in (("status",), ("store-check",), ("history", "t001"), ("lint",)):
        p = run(box, *argv, "--mission", MISSION, agent="Director")
        assert p.returncode in (0, 1) and _code(p) != FROZEN, (argv, p.stderr)
    assert "reopen-intent-blocked" in store_check(box)                   # 止めた理由は読める


def test_the_gate_lifts_once_the_intent_is_gone(tmp_path):
    box = _stuck_box(tmp_path)
    set_card(box, "t002", status="pending")                              # 出口: Director が card を直接直す
    from test_reopen_cascade_plan_sh import reopen
    assert reopen(box).returncode == 0 and intents(box) == []
    assert _update("t004", "--priority", "low")(box).returncode == 0


# ---------------------------------------------------------------------------
# 網 2: 一覧に無い task が実行中でも打ち直しは止まる (網 1 を通り抜けた書き込み)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("running", ["in_progress", "ready_for_verification", "verifying", "needs_human_review"])
def test_an_unlisted_running_task_on_the_path_stops_the_resume(tmp_path, running):
    """網 1 を通らない付け替え (I → B → R) の B が実行中 (一覧に無い)。回復が B を置いたまま intent を消すと、B の done 報告で
    R が I より先に走れる。実行中でも止める (PATH_CHANGED_WHILE_STALLED)。"""
    box = _stopped_box(tmp_path)
    _bypass_rewire(box)
    set_card(box, "t004", status=running, worker="Mika")
    p = box.pull("Mika", "t001", skills=SKILLS)
    assert p.returncode == 1 and _code(p) == "REOPEN_INTENT_INCOMPLETE", (p.returncode, p.stderr)
    assert "t004" in p.stderr and "PATH_CHANGED_WHILE_STALLED" in p.stderr, p.stderr
    # card は戻さない (回復の孤児 assignment の復元は別の回復なので snapshot 全体では比べない)
    assert intents(box) == ["t001-1.json"] and [status(box, t) for t in TIDS[:3]] == ["done"] * 3 and status(box, "t004") == running


def test_a_worker_report_after_the_block_is_still_caught_by_the_resume(tmp_path):
    """止まっている間に Worker の報告 (通す側) で B が done になっても、決定の後に完了した task として止まる。"""
    box = _stopped_box(tmp_path)
    _bypass_rewire(box)
    set_card(box, "t004", status="in_progress", worker="Mika")
    assert box.pull("Mika", "t001", skills=SKILLS).returncode == 1
    set_card(box, "t004", status="done")
    p = box.pull("Mika", "t001", skills=SKILLS)
    assert p.returncode == 1 and "t004" in p.stderr and "PATH_CHANGED_WHILE_STALLED" in p.stderr, p.stderr
    assert intents(box) == ["t001-1.json"] and status(box, "t001") == "done"


def test_an_unlisted_running_task_off_the_path_does_not_block(tmp_path):
    """経路に無い実行中の task (無関係な B) は止める理由が無い。"""
    box = _stopped_box(tmp_path)
    set_card(box, "t004", status="in_progress", worker="Mika")
    box.pull("Mika", "t003", skills=SKILLS)
    assert intents(box) == []
    assert [status(box, t) for t in TIDS[:3]] == ["pending"] * 3 and status(box, "t004") == "in_progress"


def test_a_listed_running_task_is_still_reset_and_reopened(tmp_path):
    """一覧にある実行中の task は従来どおり試行を閉じて戻す (網 2 は一覧に無いものだけを止める)。"""
    box = _stopped_box(tmp_path)
    set_card(box, "t002", status="in_progress", worker="Mika")
    p = box.pull("Mika", "t001", skills=SKILLS)
    assert intents(box) == [], p.stderr
    assert status(box, "t002") == "pending"
