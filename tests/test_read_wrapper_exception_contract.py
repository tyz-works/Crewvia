#!/usr/bin/env python3
"""例外を出さない読み取り wrapper が、**本当に例外を出さない**こと (t018)。

## 同じ漏れが 3 回出ている

| 巡 | どこ | 漏れた例外 |
|---|---|---|
| 6 | `read_task_card()` (t014 で集約したとき) | `UnicodeDecodeError` |
| 9 | `read_regular_text_or_unreadable()` (t017 で新設) | `UnicodeDecodeError` |
| 9 | `plan.sh:try_read_queue_file()` (t017 で新設) | `UnicodeDecodeError` |

3 回とも形は同じ —— **`OSError` だけを名前で捕まえ、`ValueError` 側にいる
`UnicodeDecodeError` を素通りさせた**。`read_task_card()` は 6 巡目のあと
`except Exception` の backstop を持ったので 9 巡目では無傷だったが、その
とき一緒に作った新しい wrapper 2 つには backstop が無かった。

だから「今回の 1 件 (`UnicodeError`) を足す」では閉じない。閉じ方は 2 つ
組み合わせる。

1. **構造** —— wrapper が `except Exception` の backstop を持っていることを
   AST で見る (`test_the_wrapper_has_a_backstop`)。名前で数えた表と、実際に
   起こりうる例外との差分を人が目で合わせる形をやめる。
2. **注入** —— 読み取り本体に代表的な例外を実際に投げさせ、wrapper が
   `Unreadable` / `problem` を返して例外を出さないことを確かめる。

`BaseException` で `Exception` ではないもの (`KeyboardInterrupt` /
`SystemExit` / `GeneratorExit`) は **握り潰さない**。あれは「読み取りの
失敗」ではなく「この実行を終わらせろ」という指示で、飲むと Ctrl-C が効か
なくなる。差分がゼロであることの主張はここまでを含む。

    python3 -m pytest tests/test_read_wrapper_exception_contract.py -v
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_task_cards  # noqa: E402
from lib_task_cards import is_unreadable  # noqa: E402


# ---------------------------------------------------------------------------
# 注入する例外の一覧
# ---------------------------------------------------------------------------
#
# 「読み取りで起こりうる例外」を網羅しようとして表を書くのは、上の 3 回が
# まさに失敗したやり方である。ここに並べるのは **代表** であって全部ではない。
# 全部を押さえるのは backstop の構造テストのほう。

INJECTED = [
    ("OSError", OSError(5, "I/O error")),
    ("PermissionError", PermissionError(13, "Permission denied")),
    ("FileNotFoundError", FileNotFoundError(2, "No such file or directory")),
    ("IsADirectoryError", IsADirectoryError(21, "Is a directory")),
    ("NotARegularFile", lib_task_cards.NotARegularFile("FIFO (named pipe)")),
    # ここが 3 回漏れた型。`OSError` ではなく `ValueError` の側にいる。
    ("UnicodeDecodeError",
     UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")),
    # backstop が受けるもの —— 「まだ書かれていない失敗」の代役。
    ("ValueError", ValueError("something else")),
    ("RuntimeError", RuntimeError("something else")),
    ("MemoryError", MemoryError()),
    ("RecursionError", RecursionError()),
]

#: 飲んではいけないもの。`Exception` を継承していない。
NOT_SWALLOWED = [
    ("KeyboardInterrupt", KeyboardInterrupt()),
    ("SystemExit", SystemExit(1)),
]


# ===========================================================================
# 1. lib_task_cards.read_regular_text_or_unreadable()
# ===========================================================================

@pytest.mark.parametrize("name,exc", INJECTED, ids=[n for n, _ in INJECTED])
def test_the_wrapper_never_raises(name, exc, monkeypatch):
    """どの失敗でも `Unreadable` が返り、例外は出ないこと。

    RED (`except UnicodeError` と backstop を外した t017 の形):
    `UnicodeDecodeError` / `ValueError` / `RuntimeError` がここを突き抜け、
    **1 つの読めない mission.yaml が dispatcher のサイクルごと中断させる**。
    """
    def _boom(_path, newline=None):
        raise exc

    monkeypatch.setattr(lib_task_cards, "read_regular_text", _boom)
    got = lib_task_cards.read_regular_text_or_unreadable("/queue/mission.yaml")
    assert is_unreadable(got), got
    assert got.path == "/queue/mission.yaml"


@pytest.mark.parametrize("name,exc", NOT_SWALLOWED,
                         ids=[n for n, _ in NOT_SWALLOWED])
def test_the_wrapper_does_not_swallow_interrupts(name, exc, monkeypatch):
    """`KeyboardInterrupt` / `SystemExit` は通すこと。

    飲むと Ctrl-C が効かなくなり、「例外を出さない」の名のもとに
    **終わらせる指示まで握り潰す**ことになる。
    """
    def _boom(_path, newline=None):
        raise exc

    monkeypatch.setattr(lib_task_cards, "read_regular_text", _boom)
    with pytest.raises(type(exc)):
        lib_task_cards.read_regular_text_or_unreadable("/queue/mission.yaml")


def test_a_non_utf8_file_is_isolated_not_fatal(tmp_path):
    """実物での確認 —— 不正な UTF-8 のファイルで、例外ではなく `Unreadable`。"""
    f = tmp_path / "mission.yaml"
    f.write_bytes(b"title: \xff\xfe broken\n")
    got = lib_task_cards.read_regular_text_or_unreadable(f)
    assert is_unreadable(got)
    assert "decode" in got.reason


def test_the_warning_survives_a_failing_logger(tmp_path):
    """`warn` 自体が落ちても、隔離は失われないこと。

    ログが書けない日にだけ全 mission が止まる、という組み合わせを作らない
    (`_safe_warn` と同じ理由)。
    """
    f = tmp_path / "mission.yaml"
    f.write_bytes(b"\xff\xfe\n")

    def _broken_warn(_msg):
        raise OSError(28, "No space left on device")

    got = lib_task_cards.read_regular_text_or_unreadable(f, warn=_broken_warn)
    assert is_unreadable(got)


# ===========================================================================
# 2. plan.sh の try_read_queue_file()
# ===========================================================================

def _plan_namespace():
    """`plan.sh` の python ブロックを、実行せずに関数だけ取り出す。

    `plan.sh` は import されるようには書かれていないので、ヒアドキュメントを
    そのまま `exec()` する —— `__main__` ガードの無い末尾の dispatch が走ら
    ないよう、`sys.argv` を `--help` 相当にしておく。
    """
    text = (SCRIPTS_DIR / "plan.sh").read_text()
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    assert m, "plan.sh: PYEOF ブロックが見つからない"
    src = m.group(1)

    # 末尾の CLI dispatch を落とす。`try_read_queue_file()` が要るのは
    # `_TASK_CARDS` (= `_load_scripts_module('lib_task_cards')`) だけ。
    tree = ast.parse(src)
    wanted = {"try_read_queue_file", "_load_scripts_module"}
    keep = [n for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
            or (isinstance(n, ast.FunctionDef) and n.name in wanted)
            or (isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_TASK_CARDS"
                        for t in n.targets))]
    module = ast.Module(body=keep, type_ignores=[])
    ns: dict = {"__name__": "plan_sh_fragment", "REPO_ROOT": str(REPO_ROOT)}
    exec(compile(module, "plan.sh", "exec"), ns)      # noqa: S102
    assert "try_read_queue_file" in ns, (
        "plan.sh から try_read_queue_file() を取り出せない — "
        "改名したならこのテストも直すこと")
    assert "_TASK_CARDS" in ns, "plan.sh の _TASK_CARDS 代入が見つからない"
    return ns


@pytest.mark.parametrize("name,exc", INJECTED, ids=[n for n, _ in INJECTED])
def test_plan_sh_try_read_queue_file_never_raises(name, exc, monkeypatch):
    """`plan.sh` 側の wrapper も、どの失敗でも `(None, 理由)` を返すこと。

    RED (`except UnicodeError` と backstop を外した t017 の形): 1 つの読め
    ない mission.yaml が `plan.sh status` の一覧そのものを中断させる ——
    健全な mission まで画面から消える。
    """
    ns = _plan_namespace()

    def _boom(_path, newline=None):
        raise exc

    monkeypatch.setattr(ns["_TASK_CARDS"], "read_regular_text", _boom)
    text, problem = ns["try_read_queue_file"]("/queue/mission.yaml")
    assert text is None
    assert problem and "/queue/mission.yaml" in problem


@pytest.mark.parametrize("name,exc", NOT_SWALLOWED,
                         ids=[n for n, _ in NOT_SWALLOWED])
def test_plan_sh_try_read_queue_file_does_not_swallow_interrupts(
        name, exc, monkeypatch):
    ns = _plan_namespace()

    def _boom(_path, newline=None):
        raise exc

    monkeypatch.setattr(ns["_TASK_CARDS"], "read_regular_text", _boom)
    with pytest.raises(type(exc)):
        ns["try_read_queue_file"]("/queue/mission.yaml")


def test_plan_sh_try_read_queue_file_still_returns_text(tmp_path):
    """逆向きの担保 —— 普通のファイルは `(中身, None)` で返ること。"""
    ns = _plan_namespace()
    f = tmp_path / "mission.yaml"
    f.write_text("title: ok\n")
    assert ns["try_read_queue_file"](f) == ("title: ok\n", None)


# ===========================================================================
# 3. 構造 —— backstop があること
# ===========================================================================
#
# 上の注入テストは「並べた例外」しか試さない。次に読み取り経路へ入る失敗は
# まだ名前が無いので、そちらは形で押さえる。

def _function_source(script: str, name: str) -> ast.FunctionDef:
    text = (SCRIPTS_DIR / script).read_text()
    if not script.endswith(".py"):
        m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
        assert m, f"{script}: PYEOF ブロックが見つからない"
        text = m.group(1)
    for node in ast.walk(ast.parse(text, filename=script)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{script}: {name}() が見つからない")


NON_THROWING_READERS = [
    ("lib_task_cards.py", "read_regular_text_or_unreadable"),
    ("lib_task_cards.py", "read_task_card"),
    ("plan.sh", "try_read_queue_file"),
]


@pytest.mark.parametrize("script,function", NON_THROWING_READERS,
                         ids=[f"{s}:{f}" for s, f in NON_THROWING_READERS])
def test_the_wrapper_has_a_backstop(script, function):
    """「例外を出さない」と名乗る読み取りは、`except Exception` を持つこと。

    名前で並べた except だけだと、**そこに載っていない失敗**が素通りする。
    3 回とも同じ形でそれが起きた。表を数えるのをやめて、形で見張る。
    """
    node = _function_source(script, function)
    handlers = [h for sub in ast.walk(node)
                if isinstance(sub, ast.Try) for h in sub.handlers]
    backstops = [
        h for h in handlers
        if h.type is not None
        and isinstance(h.type, ast.Name) and h.type.id == "Exception"
    ]
    assert backstops, (
        f"{script}:{function}() に `except Exception` の backstop が無い。\n"
        f"  名前で並べた except だけだと、次に読み取り経路へ入る失敗が\n"
        f"  そのまま呼び出し側へ抜ける (Codex 6 巡目 P1 / 9 巡目 P2 と同じ形)。")

    bare = [h for h in handlers if h.type is None]
    assert not bare, (
        f"{script}:{function}() に裸の `except:` がある。"
        f"`KeyboardInterrupt` / `SystemExit` まで飲む。")
