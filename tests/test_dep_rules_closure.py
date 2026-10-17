"""`lib_dep_rules.downstream_closure` / `path_closure` / `dependency_path` (vNext 02 PR-F。設計 §4.3)。

reopen の下流の列挙の唯一の置き場。依存判定 `unmet_dependencies` は推移的にしない (別のテストが固定している)。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import lib_dep_rules as dep  # noqa: E402


def g(**edges):
    return {k: list(v) for k, v in edges.items()}


def test_downstream_closure_is_transitive_far_first_and_excludes_the_root():
    graph = g(t001=[], t002=["t001"], t003=["t002"], t004=["t001"], t005=["t003", "t004"], t006=[])
    out = dep.downstream_closure(graph, "t001")
    assert set(out) == {"t002", "t003", "t004", "t005"} and "t001" not in out and "t006" not in out
    pos = {t: i for i, t in enumerate(out)}
    assert pos["t005"] < pos["t003"] < pos["t002"] and pos["t005"] < pos["t004"]      # 依存される側より先 (遠い順)
    assert out == dep.downstream_closure(graph, "t001")                               # 決定的


def test_longest_distance_orders_a_diamond_with_a_shortcut():
    # t001 → t002 → t003 と t001 → t003 の近道: t003 は t002 より先 (t003 は t002 に依存する)
    graph = g(t001=[], t002=["t001"], t003=["t001", "t002"])
    assert dep.downstream_closure(graph, "t001") == ["t003", "t002"]


def test_cycle_and_unknown_dependencies_terminate():
    graph = g(t001=["t003"], t002=["t001"], t003=["t002"], t004=["t999"])
    out = dep.downstream_closure(graph, "t001")
    assert set(out) == {"t002", "t003"}
    assert dep.downstream_closure(graph, "t999") == ["t004"] or dep.downstream_closure(graph, "t999") == []


def test_path_closure_is_the_downstream_of_root_intersected_with_the_upstream_of_targets():
    graph = g(t001=[], t002=["t001"], t003=["t002"], t004=["t001"], t005=["t003"], t006=["t004"])
    assert dep.path_closure(graph, "t001", ["t005"]) == ["t005", "t003", "t002"]      # t004 / t006 は経路上にない
    assert set(dep.path_closure(graph, "t001", ["t005", "t006"])) == {"t002", "t003", "t004", "t005", "t006"}
    assert dep.path_closure(graph, "t001", []) == []
    assert dep.path_closure(graph, "t001", ["t001"]) == []                            # root 自身は下流でない
    assert dep.path_closure(graph, "t004", ["t005"]) == []                            # 経路が無い


def test_path_closure_follows_every_route_to_a_target():
    graph = g(t001=[], t002=["t001"], t003=["t001"], t004=["t002", "t003"])
    assert set(dep.path_closure(graph, "t001", ["t004"])) == {"t002", "t003", "t004"}


def test_dependency_path_is_a_shortest_route_from_the_root():
    graph = g(t001=[], t002=["t001"], t003=["t002"], t004=["t001"], t005=["t003", "t004"])
    assert dep.dependency_path(graph, "t001", "t005") == ["t001", "t004", "t005"]
    assert dep.dependency_path(graph, "t001", "t001") is None
    assert dep.dependency_path(graph, "t005", "t001") is None


def test_unmet_dependencies_stays_direct_only():
    """閉包を足しても「依存が満たされた」の定義は推移的にしない (R は A の done だけを見る。I が pending でも unmet にならない)。"""
    statuses = {"t001": "pending", "t002": "done"}
    assert dep.unmet_dependencies(["t002"], {"t002"}, statuses) == []
