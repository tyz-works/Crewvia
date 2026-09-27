"""plan.sh の assignment 操作が単一トランザクションから外れないことを固定する。

このファイルが守るのは実行時の挙動ではなく *構造* である。理由は、割れた
トランザクションの実害 (ロック解放と assignment 削除の間に同名 Worker が
pull して、後任の assignment が消える) を実時間の競合として決定的に再現
できないからだ。窓は数マイクロ秒しかなく、それを広げる細工を入れると本番
に存在しない経路を試すことになる。

そこで「窓が存在しないこと」そのものを検査する:

  1. queue/assignments 配下のパスを組み立ててよいのはアイデンティティ
     ヘルパーだけ。各コマンドが自前で os.path.join(..., 'assignments')
     すると、規約から外れた 4 つ目の経路が静かに生える。
  2. assignment を公開・撤去するヘルパーの呼び出しは、必ず with_lock() に
     渡されるコールバックの内側にあること。ロックの外に 1 つでも出た瞬間、
     「判定して書く」が 2 つのトランザクションに割れる。

Codex が PR #205 に対して 3 巡連続で出した P1 は、どちらもこの 2 つの規約
から外れた経路で起きていた。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLAN_SH = REPO_ROOT / "scripts" / "plan.sh"

#: assignment ファイルを公開・撤去する唯一の入口。
MUTATORS = {"publish_assignment", "retire_assignment"}

#: assignment のパスを組み立ててよい関数 (ヘルパー自身)。
PATH_OWNERS = {"assignment_path", "assignment_identity_path"}


def _plan_py_source() -> str:
    """plan.sh に埋め込まれた python 本体を取り出す。"""
    text = PLAN_SH.read_text()
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    assert m, "plan.sh の python ヒアドキュメント (PYEOF) が見つからない"
    return m.group(1)


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(_plan_py_source(), filename=str(PLAN_SH))


def _enclosing_functions(tree: ast.Module) -> dict[int, ast.FunctionDef]:
    """node id → それを直接含む最も内側の FunctionDef。"""
    owner: dict[int, ast.FunctionDef] = {}

    def walk(node: ast.AST, current: ast.FunctionDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            if current is not None:
                owner[id(child)] = current
            nxt = child if isinstance(child, ast.FunctionDef) else current
            walk(child, nxt)

    walk(tree, None)
    return owner


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_assignment_paths_are_built_in_one_place(tree: ast.Module) -> None:
    """'assignments' というディレクトリ名を知ってよいのはヘルパーだけ。"""
    owners = _enclosing_functions(tree)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "assignments":
            fn = owners.get(id(node))
            where = fn.name if fn else "<module>"
            if where not in PATH_OWNERS and where != "<module>":
                offenders.append(f"{where} (line {node.lineno})")
    assert not offenders, (
        "assignment のパスをヘルパー以外で組み立てている: "
        + ", ".join(offenders)
        + " — assignment_path() / assignment_identity_path() を使うこと"
    )


def test_assignment_mutations_happen_inside_the_queue_lock(tree: ast.Module) -> None:
    """publish/retire は必ず with_lock() に渡すコールバックの内側で呼ぶ。"""
    owners = _enclosing_functions(tree)

    # with_lock(cb) に渡されている関数名を集める。
    locked_callbacks: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "with_lock":
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    locked_callbacks.add(arg.id)

    assert locked_callbacks, "with_lock() の呼び出しが 1 つも見つからない"

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in MUTATORS:
            continue
        fn = owners.get(id(node))
        if fn is None:
            offenders.append(f"<module> 直下 (line {node.lineno})")
            continue
        # ヘルパー同士の呼び出し (retire が publish を使う等) は、呼び出し元が
        # ロック内にいるかで判定されるので除外する。
        if fn.name in MUTATORS or fn.name in PATH_OWNERS:
            continue
        if fn.name not in locked_callbacks:
            offenders.append(f"{name}() in {fn.name} (line {node.lineno})")

    assert not offenders, (
        "assignment の変更がキューロックの外にある: "
        + ", ".join(offenders)
        + " — ロックの外に出た瞬間、判定と書き込みが別トランザクションに割れる"
    )


def test_retire_assignment_is_the_only_remover(tree: ast.Module) -> None:
    """assignment ファイルの削除は retire_assignment() だけが行う。"""
    owners = _enclosing_functions(tree)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in ("remove", "unlink"):
            continue
        fn = owners.get(id(node))
        where = fn.name if fn else "<module>"
        src = ast.unparse(node)
        if "assignment" not in src.lower():
            continue
        if where != "retire_assignment":
            offenders.append(f"{where} (line {node.lineno}): {src}")
    assert not offenders, (
        "assignment を retire_assignment() 以外で削除している: " + ", ".join(offenders)
    )
