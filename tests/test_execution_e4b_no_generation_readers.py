#!/usr/bin/env python3
"""01c E4b (t025): 世代 (`started_at`) の照合と旧形式の退役 marker の読み口が、コードに戻っていないことの構造ガード。

設計: `knowledge/execution.md` §2.1 (#4・#8・#11・#12)・§2.4 の 4・§7 (E4b)。E4b で外したもの:

- `plan.sh retire --started-at` (世代での名指し) — 未知のオプション
- `lib_retirement` の `read_task_started_at` / `_bound_generation` / `UNKNOWN_STARTED_AT` / marker の `task_started_at` の読み書き
- `lib_execution.execution_matches(started_at=)` / `identity_matches(generation=)` / `Txn.classify_assignment(generation)`

**本番の queue / registry には触れない** (ソースの文字列を見るだけ)。引用符つきの語 (= コードに現れる形) だけを見るので、
コメント・docstring の説明では落ちない。戻したら (どれか 1 つでも引数・キー・関数が復活したら) 赤。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lib_execution as ex  # noqa: E402
import lib_retirement as rt  # noqa: E402
import lib_state_store as store  # noqa: E402

CODE_FILES = sorted(
    [p for p in (REPO / "scripts").iterdir() if p.suffix in (".py", ".sh") and not p.name.startswith("test_")]
    + [p for p in (REPO / "hooks").iterdir() if p.suffix in (".py", ".sh")]
    + [REPO / "crewvia"])

#: コードに現れる形 (引用符つき・識別子) の、外した世代の読み口。
FORBIDDEN = {
    "read_task_started_at": re.compile(r"\bread_task_started_at\b"),
    "_bound_generation": re.compile(r"\b_bound_generation\b"),
    "UNKNOWN_STARTED_AT": re.compile(r"\bUNKNOWN_STARTED_AT\b"),
    "marker key task_started_at": re.compile(r"""["']task_started_at["']"""),
    "retire --started-at (option)": re.compile(r"""["']--started-at["']"""),
}


def test_the_scan_sees_the_real_files():
    names = {p.name for p in CODE_FILES}
    assert {"plan.sh", "lib_retirement.py", "lib_execution.py", "lib_state_store.py", "dispatcher.sh"} <= names
    assert len(CODE_FILES) >= 20, len(CODE_FILES)


@pytest.mark.parametrize("what", sorted(FORBIDDEN))
def test_no_generation_reader_is_back_in_the_code(what):
    pattern = FORBIDDEN[what]
    hits = []
    for path in CODE_FILES:
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not hits, f"E4b で外した世代の読み口が戻っている ({what}): {hits}"


def _params(fn):
    return set(inspect.signature(fn).parameters)


def test_the_matching_rules_take_no_generation():
    assert "started_at" not in _params(ex.execution_matches)
    assert "generation" not in _params(ex.identity_matches) and "started_at" not in _params(ex.identity_matches)
    assert "generation" not in _params(store.Txn.classify_assignment)
    assert "generation" not in _params(store.Txn.retire_assignment)
    assert "generation" not in _params(rt.assignment_execution_verdict)
    assert "task_started_at" not in _params(rt.build_request)
    assert not hasattr(rt, "read_task_started_at") and not hasattr(rt, "UNKNOWN_STARTED_AT")
    assert not hasattr(rt.RetirementExecutor, "_bound_generation")


def test_the_started_at_comparisons_that_remain_are_not_matching_rules():
    """`started_at` を「照合」に使う残りの場所は `attempt_view` (DETACHED の判定。card 1 枚から) だけ。
    lib_retirement / plan.sh の retire は card の `started_at` を identity や名乗りと比べない。"""
    tree = ast.parse((REPO / "scripts" / "lib_retirement.py").read_text(encoding="utf-8"))
    compared = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            src = ast.unparse(node)
            if "started_at" in src or "generation" in src:
                compared.append(src)
    assert not compared, compared
