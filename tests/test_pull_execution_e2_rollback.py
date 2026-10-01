"""01c E2 (t008): rollback (旧コード) との共存 (knowledge/execution.md §9.5 の表外 5)。

旧コードは git の履歴から取る (E2 の前 = E1 の merge `505d16b` の scripts/)。履歴が無い (CI の浅い clone) ときは skip する —
その場合は QA (t009) が d887acf の plan.sh で通す。
"""

from __future__ import annotations

import subprocess

import pytest

import pull_execution_helpers as h
from pull_execution_helpers import Box, MISSION
from test_pull_execution_e2 import _stage_patch, out
from fixture_tree import copy_plan_tree

OLD_CODE_SHA = "505d16b"      # E2 の前 (E1 の merge)。pull を Controller に移す前の plan.sh


def _old_plan_tree(tmp_path):
    src = tmp_path / "old-src"
    src.mkdir()
    packed = subprocess.run(["git", "-C", str(h.REPO_ROOT), "archive", OLD_CODE_SHA, "scripts", "tests/fixtures"], capture_output=True)
    if packed.returncode != 0:
        pytest.skip(f"{OLD_CODE_SHA} が git の履歴に無い (浅い clone)。旧コードとの共存は QA (t009) が通す")
    subprocess.run(["tar", "-x", "-C", str(src)], input=packed.stdout, check=True)
    return copy_plan_tree(tmp_path / "old-root", src)


def _tree(root):
    d = root / ".claude"
    return sorted(p.as_posix() for p in d.rglob("*")) if d.exists() else []


def test_old_code_pull_on_a_reserved_card_exits_1_and_touches_neither_the_card_nor_the_worktree(tmp_path):
    box = Box(tmp_path / "root")
    argv = ["--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001"]
    killed, _c, _ok = h.fork_run(box, _stage_patch("before_start"), argv, "Ren")
    assert killed and box.card()["execution_status"] == "reserved"
    old_plan = _old_plan_tree(tmp_path)
    before = {k: v for k, v in box.snapshot().items()}
    wt_before = _tree(box.root)
    for agent in ("Ren", "Sora"):
        p = subprocess.run([str(old_plan), "pull", "--agent", agent, "--skills", "code", "--task", "t001",
                            "--mission", MISSION], env=box.proc_env(agent), capture_output=True, text=True, timeout=120)
        assert p.returncode == 1 and "already in_progress" in p.stderr and p.stdout.strip() == "", (agent, p.stderr)
    # 旧コードの回復 (R-1 / R-2) は card の言うことに枠を合わせるだけ: reserved の card は枠が揃っているので何も変わらない
    assert box.snapshot() == before
    assert _tree(box.root) == wt_before, "旧コードの pull が worktree に触れた"
    # 新コードの同じ Worker の再 pull は、旧コードに触られた後でも同じ試行を再開できる
    xid = box.card()["current_execution_id"]
    assert out(box.pull("Ren"))["execution_id"] == xid
