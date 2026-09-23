#!/usr/bin/env python3
"""queue / registry を読む経路が、**全部** ガードを通っていること (t017)。

## なぜ構造で見るのか

Codex 8 巡目の P2 は「今回の 2 箇所」の指摘ではなく、**同じ型が 5 回出た**
という指摘だった (循環 / 空配列 / 重複 id / デコード失敗 / 走査失敗)。1 件ずつ
振る舞いで固定しても、6 回目は *まだ書かれていないコード* に出る。だから
「どの関数が、どのガードを通って読むか」という表そのものをテストにする
(memory: microsecond-race-fix-needs-structural-test)。

振る舞いのテストは別にある —— `tests/test_guarded_reads_on_direct_paths.py`
(FIFO で実際に止まらないこと) と `tests/test_stat_failure_is_not_silence.py`
(stat の失敗を沈黙と読まないこと)。こちらはその 2 つが **カバーしていない
2 つのデーモン** (`verifier-dispatcher.sh` / `taskvia-sync.sh`) まで含めて、
表の全行を押さえる。

## 表 (= このファイルの `GUARDED_READS`)

ガードは 3 つの入口のどれかである。どれも `lib_task_cards` の
`_read_regular_file()` —— `O_NONBLOCK` で開いて `fstat` で通常ファイルを
確かめる、1 つだけの判定 —— に行き着く。

* `read_task_card()`            カード 1 枚 (読めなければ `[破損]`)
* `read_regular_text()`         中身か例外
* `read_regular_text_or_none()` 中身か None (警告 1 行)

## 閉じていないもの

`plan.sh` の `load_state()` は **意図的にガードを通していない**。
`tests/test_retirement.py` の `_pull_parked_inside_the_queue_lock()` が、
Codex 6 巡目 P1 の回帰テストを成立させるために、そこを唯一の停止点として
使っているからである。理由と取引の中身は
`knowledge/empty-vs-unobservable.md` §4。下の
`test_the_one_deliberate_exception_is_still_the_only_one` が、例外が **1 つ
だけ** であり続けることを見張る (memory:
and-condition-beats-unforgeable-evidence —— 閉じない指摘は閉じないと明言する)。

    python3 -m pytest tests/test_queue_reads_go_through_the_guard.py -v
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ---------------------------------------------------------------------------
# bash に埋め込まれた python を取り出す
# ---------------------------------------------------------------------------

def _python_source(script: str) -> str:
    """`.py` はそのまま、`.sh` は `<<'PYEOF'` ブロックを返す。"""
    path = SCRIPTS_DIR / script
    text = path.read_text()
    if script.endswith(".py"):
        return text
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    assert m, f"{script}: python ヒアドキュメント (PYEOF) が見つからない"
    return m.group(1)


def _function(script: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(_python_source(script), filename=script)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{script}: 関数 {name}() が見つからない — "
                f"改名したなら、この表 (GUARDED_READS) も一緒に直すこと")


def _called_names(node: ast.AST) -> set[str]:
    """その関数の中で呼ばれている名前 (`f()` と `x.f()` の両方)。"""
    names: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


# ---------------------------------------------------------------------------
# 表そのもの
# ---------------------------------------------------------------------------
#
# (スクリプト, 関数, そこが通っていなければならないガードのどれか)

GUARDED_READS = [
    # --- plan.sh ---------------------------------------------------------
    ("plan.sh", "try_read_queue_file", {"read_regular_text"}),
    ("plan.sh", "read_queue_file", {"try_read_queue_file"}),
    ("plan.sh", "load_task", {"read_queue_file"}),
    ("plan.sh", "load_mission", {"read_queue_file"}),
    ("plan.sh", "_print_mission_summary", {"try_read_queue_file"}),
    ("plan.sh", "_print_mission_detail", {"read_queue_file"}),
    ("plan.sh", "_mission_data", {"try_read_queue_file"}),
    ("plan.sh", "list_tasks", {"list_task_cards"}),

    # --- dispatcher.sh ---------------------------------------------------
    ("dispatcher.sh", "read_queue_text", {"read_regular_text_or_none"}),
    ("dispatcher.sh", "load_state", {"read_queue_text"}),
    ("dispatcher.sh", "load_workers", {"read_queue_text"}),
    ("dispatcher.sh", "publish_agents", {"read_task_card"}),
    ("dispatcher.sh", "dispatch", {"read_queue_text"}),
    ("dispatcher.sh", "list_tasks_for_mission", {"list_task_cards"}),

    # --- verifier-dispatcher.sh ------------------------------------------
    ("verifier-dispatcher.sh", "_read_queue_text", {"read_regular_text_or_none"}),
    ("verifier-dispatcher.sh", "load_state", {"_read_queue_text"}),
    ("verifier-dispatcher.sh", "load_workers", {"_read_queue_text"}),
    # 読んでから書き戻す経路。種類を見ないと、`os.replace()` が置き換えるのが
    # *別の何か* になる。
    ("verifier-dispatcher.sh", "update_task_fields", {"read_regular_text"}),

    # --- taskvia-sync.sh --------------------------------------------------
    ("taskvia-sync.sh", "_read_queue_text", {"read_regular_text_or_none"}),
    ("taskvia-sync.sh", "scan_missions", {"_read_queue_text"}),

    # --- watchdog.py ------------------------------------------------------
    ("watchdog.py", "load_active_tasks", {"read_regular_text"}),

    # --- lib_registry.py --------------------------------------------------
    # `with_lock()` の内側。素の open() だと **レジストリのロックを握ったまま**
    # 止まり、Worker の起動も task_count の更新も進まなくなる。
    ("lib_registry.py", "parse", {"read_regular_text"}),
]


@pytest.mark.parametrize("script,function,guards", GUARDED_READS,
                         ids=[f"{s}:{f}" for s, f, _ in GUARDED_READS])
def test_the_read_goes_through_a_guard(script, function, guards):
    """この関数は、queue / registry を **ガード経由で** 読んでいること。"""
    called = _called_names(_function(script, function))
    assert called & guards, (
        f"{script}:{function}() が {sorted(guards)} のどれも通っていない。\n"
        f"  queue / registry のファイルを直接 open() すると、書き手のいない "
        f"FIFO 1 枚で無期限に止まる。\n"
        f"  実際に呼んでいるもの: {sorted(called)}")


# ---------------------------------------------------------------------------
# 取り除いた読み方が戻ってきていないこと
# ---------------------------------------------------------------------------
#
# 上の表は「ガードを通っている」ことしか見ない。ガードを **足したうえで** 直接の
# 読み取りも残す、という形は表を通ってしまうので、Codex が名指しした式そのものを
# 別に見張る。

FORBIDDEN_READS = [
    ("dispatcher.sh", "task_file.read_text()",
     "publish_agents が割り当て済みカードを直接読んでいる"),
    ("dispatcher.sh", "STATE_FILE.read_text()", "state.yaml の直接読み"),
    ("dispatcher.sh", "WORKERS_FILE.read_text()", "workers.yaml の直接読み"),
    ("dispatcher.sh", "mfile.read_text()", "mission.yaml の直接読み"),
    ("verifier-dispatcher.sh", "STATE_FILE.read_text()", "state.yaml の直接読み"),
    ("verifier-dispatcher.sh", "WORKERS_FILE.read_text()", "workers.yaml の直接読み"),
    ("verifier-dispatcher.sh", "task_path.read_text()",
     "書き戻す前のカードを直接読んでいる"),
    ("watchdog.py", "state_file.read_text()", "state.yaml の直接読み"),
]


@pytest.mark.parametrize("script,snippet,why", FORBIDDEN_READS,
                         ids=[f"{s}:{t}" for s, t, _ in FORBIDDEN_READS])
def test_the_direct_read_did_not_come_back(script, snippet, why):
    source = _python_source(script)
    assert snippet not in source, f"{script}: {why} ({snippet} が復活している)"


# ---------------------------------------------------------------------------
# 例外は 1 つだけ
# ---------------------------------------------------------------------------

def test_the_one_deliberate_exception_is_still_the_only_one():
    """`plan.sh` の `load_state()` だけが、意図的にガードを通っていない。

    これは見落としではなく取引で、理由は
    `knowledge/empty-vs-unobservable.md` §4 にある。ここが **黙って** 他の
    関数にも広がらないよう、例外であること自体を固定する。

    逆に、いつかこれを閉じたときは、このテストが「もう例外ではない」と赤で
    知らせる —— そのときは `tests/test_retirement.py` の停止点を先に別の
    仕組みへ移すこと。
    """
    load_state = _function("plan.sh", "load_state")
    called = _called_names(load_state)

    assert "open" in called, (
        "plan.sh:load_state() がガードを通るようになった。\n"
        "  それ自体は良いことだが、tests/test_retirement.py の\n"
        "  _pull_parked_inside_the_queue_lock() は queue/state.yaml の FIFO を\n"
        "  唯一の停止点にしている。先にそちらを別の仕組みへ移し、\n"
        "  knowledge/empty-vs-unobservable.md §4 とこのテストを更新すること。")
    assert not (called & {"read_queue_file", "try_read_queue_file"}), \
        "load_state() が両方の読み方を持っている — どちらが効くのか読めない"
