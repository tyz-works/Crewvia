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

# `repo` (idle Worker 1 人 + Director の使い捨て registry) は test_assignment_routing の fixture を使う。
# ここで registry を組み立て直さない (scripts/test_registry_lock.sh の静的検査の除外を増やさない)。
from test_assignment_routing import _set_card, _cycle, _kickoffs, repo  # noqa: E402,F401
from test_dispatcher_retirement_exclusion import AGENT  # noqa: E402


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


def test_the_name_rule_has_one_definition_shared_by_pull_and_the_dispatcher(repo):
    """名前の規則は lib_agent_name の 1 つ (PR #294 Codex P2)。lib_state_store は同じ関数を re-export し
    (plan.sh pull --agent の受け付け)、dispatcher は lib_state_store を import せずに同じ関数を使う。"""
    sys.path.insert(0, str(TESTS_DIR.parent / "scripts"))
    import ast
    import lib_agent_name
    import lib_state_store
    assert lib_state_store.agent_name_problem is lib_agent_name.agent_name_problem
    assert lib_state_store.RESERVED_AGENT_SUFFIXES is lib_agent_name.RESERVED_AGENT_SUFFIXES
    assert lib_state_store.IDENTITY_SUFFIX == lib_agent_name.IDENTITY_SUFFIX
    _, ns = _cycle(repo)
    assert ns["_agent_name_problem"] is lib_agent_name.agent_name_problem
    # 純粋 lib: import するのは __future__ だけ (dispatcher が読んでも I/O も依存も増えない)
    tree = ast.parse((TESTS_DIR.parent / "scripts" / "lib_agent_name.py").read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert all(isinstance(n, ast.ImportFrom) and n.module == "__future__" for n in imports), imports
