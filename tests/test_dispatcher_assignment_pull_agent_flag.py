#!/usr/bin/env python3
"""dispatcher の割り当て文は `plan pull ... --agent <名前>` を含む (pull の agent 空の手前の防ぎ)。

pull が担当者なしで試行を始める事故は、Worker のシェルの AGENT_NAME が空だったことが原因だった。
割り当て文が --agent を明示すれば env に頼らない。本物の `dispatch()` を 1 サイクル回して確かめる。

    python3 -m pytest tests/test_dispatcher_assignment_pull_agent_flag.py -v
"""

from __future__ import annotations

import pathlib
import sys

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from test_assignment_routing import _set_card, _cycle, _kickoffs  # noqa: E402,F401
from test_dispatcher_retirement_exclusion import AGENT, _build_repo, _load_dispatcher  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    root = _build_repo(tmp_path)
    (root / "registry" / "workers.yaml").write_text(
        f"workers:\n  - name: {AGENT}\n    skills: [code]\n    experience: 0\n"
        "  - name: Sora\n    skills: [director]\n    role: director\n    experience: 0\n")
    return root


def test_assignment_message_names_the_agent(repo):
    _set_card(repo, "t001")
    mux, _ = _cycle(repo)
    msgs = _kickoffs(mux, "t001")
    assert msgs, mux.sent
    assert f"plan pull --task t001 --mission " in msgs[0]
    assert f" --agent {AGENT} で取得後" in msgs[0], msgs[0]


@pytest.mark.parametrize("name,expected", [
    ("Luna", " --agent Luna"),
    ("", ""),
    ("   ", ""),
    (" Luna", ""),
    ("Lu na", ""),
    ("Lu'na", ""),
    ("a;rm", ""),
    (".hidden", ""),
    ("a/b", ""),
    ("x.restarting", ""),
    (None, ""),
])
def test_pull_agent_flag_only_for_names_pull_accepts_and_shell_safe(repo, name, expected):
    _, ns = _cycle(repo)
    assert ns["pull_agent_flag"](name) == expected
