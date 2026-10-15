"""PR-C (vNext 02): `plan.sh snapshot --self` と hooks/pre-compact.sh。

task は pull のあとに決まるので hook の env には無い。`--self` は AGENT_NAME の assignment から決め、
自分の in_progress の card だけに書く。決められなければ何も書かず exit 1。
赤の実証: `--self` の検査 (worker / status / assignment) を外した版で拒否のテストが赤になること。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess

import pytest

import test_plan_sh_state_store_cutover as cut

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
M1, M2 = "m-self-a", "m-self-b"
SECTION = "## Pre-Compact Snapshot\n\n- trigger: auto\n- agent: Ren\n"


@pytest.fixture
def sb(tmp_path):
    box = cut.Sandbox(tmp_path)
    for m in (M1, M2):
        box.run("init", m, "--mission", m)
        box.run("add", "t", "--skills", "bash", "--mission", m)
    return box


def _card(sb, m=M1):
    return sb.queue / "missions" / m / "tasks" / "t001.md"


def _pull(sb, m=M1, agent="Ren"):
    sb.run("pull", "--agent", agent, "--skills", "bash", "--task", "t001", "--mission", m)


def _snap(sb, agent="Ren", expect=0, *extra):
    return sb.run("snapshot", "--self", "--section-file", "-", *extra, agent=agent, stdin=SECTION, expect=expect)


def _bytes(sb):
    return {m: _card(sb, m).read_bytes() for m in (M1, M2)}


def test_self_writes_the_section_with_execution_id(sb):
    _pull(sb)
    _snap(sb)
    text = _card(sb).read_text()
    eid = re.search(r"^current_execution_id: (ex-\S+)$", text, re.M).group(1)
    assert text.count("## Pre-Compact Snapshot") == 1
    assert f"- execution_id: {eid}" in text and f"- mission_task: {M1}/t001" in text
    assert "trigger: auto" in text
    assert b"Pre-Compact" not in _card(sb, M2).read_bytes()      # 同じ tNNN の別 mission には書かない


def test_self_writes_to_the_assigned_mission_when_both_have_the_same_task_id(sb):
    _pull(sb, M2)
    _snap(sb)
    assert b"Pre-Compact" in _card(sb, M2).read_bytes()
    assert b"Pre-Compact" not in _card(sb, M1).read_bytes()


def test_self_refuses_without_an_assignment(sb):
    before = _bytes(sb)
    p = _snap(sb, expect=1)
    assert "何も書いていません" in p.stderr
    assert _bytes(sb) == before


def test_self_refuses_another_workers_card(sb):
    _pull(sb, agent="Wei")
    before = _bytes(sb)
    _snap(sb, agent="Ren", expect=1)                   # Ren は assignment を持たない
    # Ren の assignment を Wei の card に向けても、worker が違うので拒否
    (sb.queue / "assignments" / "Ren").write_text(f"{M1}:t001")
    _snap(sb, agent="Ren", expect=1)
    assert _bytes(sb) == before


def test_self_refuses_a_done_card(sb):
    _pull(sb)
    sb.run("done", "t001", "x", "--no-pr", "test", "--mission", M1, agent="Ren")
    (sb.queue / "assignments").mkdir(exist_ok=True)
    (sb.queue / "assignments" / "Ren").write_text(f"{M1}:t001")   # 孤児の assignment
    before = _bytes(sb)
    _snap(sb, expect=1)
    assert _bytes(sb) == before


def test_self_refuses_an_assignment_pointing_at_a_missing_card(sb):
    (sb.queue / "assignments").mkdir(exist_ok=True)
    (sb.queue / "assignments" / "Ren").write_text(f"{M1}:t999")
    before = _bytes(sb)
    _snap(sb, expect=1)
    assert _bytes(sb) == before


def test_self_cannot_be_combined_with_task_or_mission(sb):
    _pull(sb)
    before = _bytes(sb)
    sb.run("snapshot", "t001", "--self", "--section-file", "-", agent="Ren", stdin=SECTION, expect=2)
    sb.run("snapshot", "--self", "--mission", M1, "--section-file", "-", agent="Ren", stdin=SECTION, expect=2)
    assert _bytes(sb) == before


def test_snapshot_without_self_still_requires_a_task_id(sb):
    sb.run("snapshot", "--section-file", "-", stdin=SECTION, expect=1)


def test_the_hook_reaches_the_card_without_task_env(sb, tmp_path):
    """hook の env に CREWVIA_TASK_ID / CREWVIA_MISSION_SLUG が無くても、AGENT_NAME の assignment で card に届く。"""
    _pull(sb)
    hooks = sb.root / "hooks"
    hooks.mkdir()
    (hooks / "pre-compact.sh").write_bytes((REPO_ROOT / "hooks" / "pre-compact.sh").read_bytes())
    os.chmod(hooks / "pre-compact.sh", 0o755)
    env = {k: v for k, v in sb.env.items() if not k.startswith("CREWVIA_TASK") and k != "CREWVIA_MISSION_SLUG"}
    env["AGENT_NAME"] = "Ren"
    payload = json.dumps({"trigger": "auto"})
    p = subprocess.run(["bash", str(hooks / "pre-compact.sh")], input=payload, env=env, text=True,
                       capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "## Pre-Compact Snapshot" in _card(sb).read_text()
    assert not (sb.queue / "pre-compact-fallback.log").exists()

    env["AGENT_NAME"] = "Nobody"
    subprocess.run(["bash", str(hooks / "pre-compact.sh")], input=payload, env=env, text=True,
                   capture_output=True, timeout=120)
    assert "agent=Nobody" in (sb.queue / "pre-compact-fallback.log").read_text()
