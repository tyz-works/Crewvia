"""t002 項目 5: `plan.sh pull` のロックの前の拒否 (不正な agent 名・role: director の pull・skills が空) は、
何も書かず (task-graph も再生成せず) exit 1 で断る。die() だと末尾の dispatch が task-graph を再生成して lock と
tasks.json を書く (PR #285 で agent 空の拒否を UsageExit にしたのと同じ族)。exit 2 は idle の意味なので使わない。"""

from __future__ import annotations

import pytest

from pull_execution_helpers import Box, MISSION
from test_pull_refuses_empty_agent import _graph_files, _with_task_graph


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root")


def _director_registry(box):
    (box.root / "registry" / "workers.yaml").write_text(
        "workers:\n  - name: Sora\n    skills: [director]\n    role: director\n    experience: 0\n")


CASES = {
    "invalid-name": (lambda box: None, ("--agent", "bad name/../x", "--skills", "code"), "invalid agent name"),
    "director": (_director_registry, ("--agent", "Sora", "--skills", "code"), "role: director"),
    "no-skills": (lambda box: None, ("--agent", "Ren"), "skills"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_early_refusal_exits_1_and_writes_nothing_including_the_task_graph(box, case):
    setup, args, needle = CASES[case]
    setup(box)
    _with_task_graph(box)
    before_graph, before = _graph_files(box), box.snapshot(with_audit=True)
    p = box.plan("pull", *args, "--task", "t001", "--mission", MISSION)
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "" and needle in p.stderr
    assert box.snapshot(with_audit=True) == before
    assert _graph_files(box) == before_graph
    assert box.card()["status"] == "pending"
