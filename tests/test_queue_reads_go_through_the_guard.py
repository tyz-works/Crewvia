#!/usr/bin/env python3
"""queue / registry / config を読む経路が、**全部** ガードを通っていること。

## なぜ構造で見るのか

Codex 8 巡目の P2 は「今回の 2 箇所」の指摘ではなく、**同じ型が 5 回出た**
という指摘だった (循環 / 空配列 / 重複 id / デコード失敗 / 走査失敗)。1 件ずつ
振る舞いで固定しても、6 回目は *まだ書かれていないコード* に出る。だから
「どの関数が、どのガードを通って読むか」という表そのものをテストにする
(memory: microsecond-race-fix-needs-structural-test)。

**それでも足りなかった** (Codex 9 巡目 P2)。t017 の表は「この関数はガードを
通っている」ことしか見ておらず、**表に載っていない関数**——
`plan.sh:_load_workers_from_registry`、`dispatcher.publish_agents` の
assignment 読み取り、`lib_retirement.read_task_started_at`、`plan.sh` の
assignment 読み取り —— は最初から視界の外にいた。人が表を書き足すかぎり、
次に足された読み取りも同じ理由で漏れる。

だから t018 で **向きを逆にした**。表 (allowlist) に載っていない読み取りが
1 つでもあれば落ちる、という形にする:

* `test_no_unguarded_read_remains` —— 対象モジュールの `open()` (読みモード) /
  `.read_text()` / `.read_bytes()` を AST で **機械的に全部** 拾い、
  `ALLOWED_DIRECT_READS` に無ければ落とす。新しい直接読み取りが増えたら
  **必ず落ちる**。
* `GUARDED_READS` の表は残す —— そちらは「ガードを通っている」ことの
  positive な主張で、関数の改名に気付くために要る。

denylist ではなく allowlist にしたのは、denylist が次の種類で必ず穴を開ける
からである (memory: approve-judgment-needs-allowlist-and-scope)。

## ガードの入口

どれも `lib_task_cards` の `_read_regular_file()` —— `O_NONBLOCK` で開いて
`fstat` で通常ファイルを確かめる、1 つだけの判定 —— に行き着く。

* `read_task_card()`                 カード 1 枚 (読めなければ `[破損]`)
* `read_regular_text()`              中身か例外
* `read_regular_text_or_unreadable()` 中身か `Unreadable` (警告 1 行)

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
# 表 (positive): この関数は、このガードを通っている
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
    # t018 (Codex 9 巡目 P2): 表に無かったので最初から視界の外にいた 4 件。
    ("plan.sh", "_load_workers_from_registry", {"try_read_queue_file"}),
    ("plan.sh", "task_graph_assignment_holds", {"try_read_queue_file"}),
    ("plan.sh", "_read_assignment_identity", {"try_read_queue_file"}),
    # ENOENT (撤去済み) と「読めない」(証明できない) を分ける必要があるので、
    # 文字列の problem ではなく errno を持つ `Unreadable` のほうを直接使う。
    ("plan.sh", "classify_assignment", {"read_regular_text_or_unreadable"}),
    ("plan.sh", "_load_taskvia_map", {"try_read_queue_file"}),
    ("plan.sh", "_apply_risk_flags", {"try_read_queue_file"}),
    ("plan.sh", "_task_graph_pending_outstanding",
     {"read_regular_text_or_unreadable"}),

    # --- dispatcher.sh ---------------------------------------------------
    ("dispatcher.sh", "read_queue_text", {"read_regular_text_or_unreadable"}),
    ("dispatcher.sh", "load_state", {"read_queue_text"}),
    ("dispatcher.sh", "load_workers", {"read_queue_text"}),
    ("dispatcher.sh", "publish_agents", {"read_task_card"}),
    ("dispatcher.sh", "dispatch", {"read_queue_text"}),
    ("dispatcher.sh", "list_tasks_for_mission", {"list_task_cards"}),
    # t018: assignment / registry/mux の固定パスもガード経由へ。
    ("dispatcher.sh", "read_assignment", {"read_queue_text"}),
    ("dispatcher.sh", "check_rule5", {"read_assignment"}),
    ("dispatcher.sh", "_mux_created_at", {"read_queue_text"}),
    ("dispatcher.sh", "_spawn_time_fallback", {"read_queue_text"}),
    ("dispatcher.sh", "_load_state_entry", {"read_queue_text"}),

    # --- verifier-dispatcher.sh ------------------------------------------
    ("verifier-dispatcher.sh", "_read_queue_text",
     {"read_regular_text_or_unreadable"}),
    ("verifier-dispatcher.sh", "load_state", {"_read_queue_text"}),
    ("verifier-dispatcher.sh", "load_workers", {"_read_queue_text"}),
    # 読んでから書き戻す経路。種類を見ないと、`os.replace()` が置き換えるのが
    # *別の何か* になる。
    ("verifier-dispatcher.sh", "update_task_fields", {"read_regular_text"}),

    # --- taskvia-sync.sh --------------------------------------------------
    ("taskvia-sync.sh", "_read_queue_text", {"read_regular_text_or_unreadable"}),
    ("taskvia-sync.sh", "scan_missions", {"_read_queue_text"}),
    ("taskvia-sync.sh", "load_map", {"_read_queue_text"}),

    # --- watchdog.py ------------------------------------------------------
    ("watchdog.py", "load_active_tasks", {"read_regular_text"}),

    # --- lib_registry.py --------------------------------------------------
    # `with_lock()` の内側。素の open() だと **レジストリのロックを握ったまま**
    # 止まり、Worker の起動も task_count の更新も進まなくなる。
    ("lib_registry.py", "parse", {"read_regular_text"}),

    # --- lib_retirement.py (t018) ----------------------------------------
    # watchdog のサイクルの中で退役要求ごとに走る。止まると退役処理全体が
    # 返らない。
    ("lib_retirement.py", "read_json", {"read_regular_text_or_unreadable"}),
    ("lib_retirement.py", "read_task_started_at",
     {"read_regular_text_or_unreadable"}),
    ("lib_retirement.py", "assignment_execution_verdict",
     {"read_regular_text_or_unreadable"}),
    ("lib_retirement.py", "created_at_from_cache",
     {"read_regular_text_or_unreadable"}),

    # --- lib_daemon_watch.py (t018) --------------------------------------
    ("lib_daemon_watch.py", "load_config", {"read_regular_text_or_unreadable"}),
    ("lib_daemon_watch.py", "read_pause_state",
     {"read_regular_text_or_unreadable"}),

    # --- lib_mux.py / lib_model.py (t018) --------------------------------
    ("lib_mux.py", "read_pane_record", {"read_regular_text_or_unreadable"}),
    ("lib_mux.py", "_config_mode", {"read_regular_text_or_unreadable"}),
    ("lib_model.py", "_parse_yaml_fallback",
     {"read_regular_text_or_unreadable"}),
    # t019: PyYAML がある **通常の** 経路。t018 は fallback 側にだけガードを
    # 足しており、本番で実際に走るほうが素の `p.open()` のまま残っていた。
    ("lib_model.py", "resolve", {"read_regular_text_or_unreadable"}),
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
# 機械的な全数検出 (negative): 表に無い直接読み取りが 1 つも残っていないこと
# ---------------------------------------------------------------------------
#
# ここが t018 で足したほう。上の表は「載せた関数」しか見ないので、**載せ忘れた
# 関数**が素の `open()` を持っていても緑になる —— それが Codex 9 巡目 P2 で
# 名指しされた 4 件の正体である。
#
# 対象モジュールの読み取りを AST で全部拾い、下の allowlist に無ければ落とす。
#
# ## 検出器が見えるもの / 見えないもの (t019)
#
# t018 の検出器は **裸の `open()` しか認識していなかった**。`Path.open()` /
# `io.open()` / `os.open()` はどれも `ast.Attribute` なので素通りし、その穴に
# `lib_model.resolve()` の `p.open(encoding="utf-8")` —— PyYAML がある本番の
# 経路 —— が入っていた。FIFO を置くとモデル解決が無期限に止まるのに、この
# テストは緑だった (Codex 10 巡目 P2-1)。**検出器の漏れは、構造テストを
# 「守っているつもり」に変える。**
#
# 見える:
#   * `open(...)` (裸)、`<何か>.open(...)` (`Path.open` / `io.open` /
#     `os.open` を含む)、`.read_text()` / `.read_bytes()`
#   * `lib_task_cards.py` に限り `.read()` (ガード本体を `open()` と対で数える)
#   * 書き込みは除く: モード文字列に `w` / `a` / `x` を含むもの、および
#     `os.open()` の書き込みフラグ (`O_WRONLY` 等)。`_is_write_open()` 参照
#
# 見えない (**ここを埋めるのは、これを読む人の仕事**):
#   * `getattr(p, "open")()` のような間接呼び出し、`fn = p.open` の別名経由
#   * `os.fdopen()` / `os.read()` を fd から直に使う形 (ガード本体だけが持つ)
#   * `subprocess` 経由で `cat` 等に読ませる形
#   * `AUDITED_MODULES` に載っていないモジュールそのもの (hooks/ は対象外)
#   * `.sh` の中では `<<'PYEOF'` ブロック **1 つ目だけ** (`_python_source()`)
#   * 受け手の名前だけでモード位置を決めているので、`os` / `io` / `codecs`
#     という名前の **変数** に Path を入れて `.open()` すると読み違える
#     (逆に `from pathlib import Path` した `Path(x).open("w")` は正しく
#     書き込みと読む)
#
# 見えないものは **allowlist では止められない** ので、増やさないこと自体が
# 規約になる。新しい読み方が要るときは、まずこの検出器を広げること。

AUDITED_MODULES = [
    "plan.sh",
    "dispatcher.sh",
    "verifier-dispatcher.sh",
    "taskvia-sync.sh",
    "watchdog.py",
    "lib_registry.py",
    "lib_retirement.py",
    "lib_daemon_watch.py",
    "lib_task_cards.py",
    "lib_mux.py",
    "lib_model.py",
    "lib_verdict.py",
    "lib_dep_rules.py",
]

#: (モジュール, 関数, ソースの断片) → なぜガードを通さなくてよいか。
#:
#: **理由を書けないものはここに載せない。** 載せるとはガードの外にあることを
#: 誰かが引き受けたという意味で、次にこの表を読む人はその 1 行だけを見て
#: 判断することになる。
ALLOWED_DIRECT_READS = {
    # -- crewvia のファイルではない: /proc ---------------------------------
    ("watchdog.py", "_proc_stat", 'Path(f"/proc/{pid}/stat").read_text()'):
        "/proc は procfs。FIFO にも通常ファイルにも置き換えられない",
    ("lib_retirement.py", "process_alive",
     'Path(f"/proc/{pid}/stat").read_text()'):
        "同上",
    ("lib_daemon_watch.py", "process_generation",
     'Path(proc_root, str(pid), "stat").read_text(encoding="utf-8")'):
        "同上",
    ("lib_daemon_watch.py", "scan_daemon_pids",
     '(entry / "cmdline").read_bytes()'):
        "同上",
    ("lib_daemon_watch.py", "scan_daemon_pids",
     '(entry / "stat").read_text(encoding="utf-8")'):
        "同上",
    ("lib_mux.py", "_read_proc",
     'path.read_text(encoding="utf-8", errors="replace")'):
        "同上",
    ("lib_mux.py", "proc_table", '(entry / "cmdline").read_bytes()'):
        "同上",
    ("lib_mux.py", "proc_table",
     '(entry / "stat").read_text(encoding="utf-8")'):
        "同上",

    # -- crewvia のファイルではない: プロセス固有の一時ファイル ------------
    ("dispatcher.sh", "bench_current_strategy",
     "BENCH_STRATEGY_CONF.read_text()"):
        "/tmp のベンチ用スイッチ。queue でも registry でもなく、"
        "落ちる先はベンチの分岐だけ",
    ("dispatcher.sh", "load_notify_cache", "NOTIFY_CACHE.read_text()"):
        "/tmp の通知重複キャッシュ。失っても倒れる先は「もう一度送る」(冪等)",
    ("verifier-dispatcher.sh", "load_notify_cache", "NOTIFY_CACHE.read_text()"):
        "同上",

    # -- 呼び出し側から渡される任意のパス ----------------------------------
    ("lib_verdict.py", "main", 'open(path, encoding="utf-8", newline="")'):
        "CLI 引数で渡された任意のファイル。crewvia のディレクトリ構造に"
        "属さないので、ここで種類を決め打ちできない",
    ("watchdog.py", "_leave_legacy_log_pointer",
     'legacy.read_text(errors="replace")'):
        "旧 logs/ の残骸に 1 行ポインタを残すだけの移行コード。"
        "失敗しても watchdog の判定には一切入らない",

    # -- 意図的な 1 つの例外 (knowledge/empty-vs-unobservable.md §4) --------
    ("plan.sh", "load_state", "open(STATE_FILE)"):
        "tests/test_retirement.py の _pull_parked_inside_the_queue_lock() が、"
        "Codex 6 巡目 P1 の回帰テストを成立させる唯一の停止点として使っている。"
        "閉じるには harness の停止点を先に別の仕組みへ移すこと",

    # -- ガードの実装そのもの ----------------------------------------------
    ("lib_task_cards.py", "_read_regular_file", "f.read()"):
        "ガード本体。ここが唯一の `open()` で、O_NONBLOCK + fstat の判定を持つ",
    # t019: 属性形式の open を検出するようにして、ガード本体の `os.open()` も
    # 見えるようになった。`f.read()` と対で 1 組。
    ("lib_task_cards.py", "_read_regular_file",
     "os.open(path, os.O_RDONLY | os.O_NONBLOCK)"):
        "ガード本体。`O_NONBLOCK` を付けて開くこと自体が判定の一部なので、"
        "ここだけは自分自身を通せない",
}

_READ_ATTRS = ("read_text", "read_bytes")

#: `os.open()` の書き込み側フラグ。`os.open()` はモードが文字列ではなく整数
#: フラグなので、モード文字列とは別の見方をする。
_OS_WRITE_FLAGS = frozenset({
    "O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_EXCL", "O_TRUNC",
})


#: モジュール関数としての `open()` —— パスが第 1 引数で、モード/フラグが
#: 第 2 引数に来る。`Path.open()` (モードが **第 1** 引数) と区別する。
_MODULE_OPEN_RECEIVERS = frozenset({"os", "io", "codecs"})


def _open_receiver(node: ast.Call):
    """`<受け手>.open(...)` の受け手の名前。属性形式でなければ `None`。"""
    func = node.func
    if (isinstance(func, ast.Attribute) and func.attr == "open"
            and isinstance(func.value, ast.Name)):
        return func.value.id
    return None


def _is_write_open(node: ast.Call) -> bool:
    """`open(path, 'w')` のような書き込みは読み取り経路ではない。

    `'a+'` (ロック取得) もここで落ちる —— `flock` のために開くだけで、
    中身を読んでいないからである。

    モードがどこにあるかは呼び方で違う (t019):

    * 組み込みの `open(path, "a")`   → 第 2 引数
    * `Path.open("a")`               → 第 1 引数 (パスはレシーバ側)
    * `os.open(path, os.O_WRONLY|…)` → 第 2 引数だが **整数フラグ**

    **「書き込みだ」と判定した読み取りは検出から外れる**ので、この判定は
    狭いほうへ倒す —— 迷ったら読み取りとして報告し、allowlist に理由を
    書かせる (memory: approve-judgment-needs-allowlist-and-scope)。
    """
    receiver = _open_receiver(node)

    if receiver == "os":
        flags = {sub.attr for sub in ast.walk(node)
                 if isinstance(sub, ast.Attribute)}
        return bool(flags & _OS_WRITE_FLAGS)

    if receiver is not None and receiver not in _MODULE_OPEN_RECEIVERS:
        modes = list(node.args[:1])      # Path.open(mode, ...)
    else:
        modes = list(node.args[1:2])     # open(path, mode, ...)
    modes += [kw.value for kw in node.keywords if kw.arg == "mode"]
    for m in modes:
        if isinstance(m, ast.Constant) and isinstance(m.value, str):
            if any(c in m.value for c in "wax"):
                return True
    return False


def _direct_reads(script: str):
    """`(関数名, ソース断片)` を、そのモジュールの読み取り全部について返す。"""
    source = _python_source(script)
    tree = ast.parse(source, filename=script)

    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    owner.setdefault(sub.lineno, node.name)

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            if _is_write_open(node):
                continue
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            # t019 (Codex 10 巡目 P2-1): **属性形式の open も検出する**。
            # ここを裸の `open()` だけにしていたせいで、`lib_model.resolve()`
            # の `p.open(encoding="utf-8")` —— PyYAML がある通常経路 ——
            # が最初から視界の外にいた。置き違えた FIFO 1 枚でモデル解決が
            # 無期限に止まるのに、この構造テストは緑のままだった。
            # **検出器に漏れがあると、構造テスト自体が「守っているつもり」
            # になる。**
            if _is_write_open(node):
                continue
        elif isinstance(func, ast.Attribute) and func.attr in _READ_ATTRS:
            pass
        elif (isinstance(func, ast.Attribute) and func.attr == "read"
                and script == "lib_task_cards.py"):
            # ガード本体の `f.read()` だけは、`open()` と対で数える。
            pass
        else:
            continue
        segment = ast.get_source_segment(source, node) or "<unparsed>"
        segment = " ".join(segment.split())
        found.append((owner.get(node.lineno, "<module>"), segment))
    return found


@pytest.mark.parametrize("script", AUDITED_MODULES)
def test_no_unguarded_read_remains(script):
    """このモジュールに、allowlist に無い直接読み取りが残っていないこと。

    RED の作り方 —— `scripts/plan.sh` の `_load_workers_from_registry()` を
    `with open(registry_path) as f:` に戻すと、この test が
    `plan.sh:_load_workers_from_registry` を報告して落ちる。表に 1 行足し
    忘れても落ちるので、**新しい直接読み取りは必ずここで止まる**。
    """
    unexpected = [
        (fn, seg) for fn, seg in _direct_reads(script)
        if (script, fn, seg) not in ALLOWED_DIRECT_READS
    ]
    assert not unexpected, (
        f"{script}: ガードを通っていない読み取りが残っている:\n"
        + "\n".join(f"  {fn}(): {seg}" for fn, seg in unexpected)
        + "\n\n  queue / registry / config のファイルを直接開くと、書き手の "
          "いない FIFO 1 枚で無期限に止まる。\n"
          "  lib_task_cards の read_regular_text() / "
          "read_regular_text_or_unreadable() / read_task_card() を通すこと。\n"
          "  そこを通さない理由があるなら ALLOWED_DIRECT_READS に "
          "**理由付きで** 1 行足すこと。")


def test_the_allowlist_has_no_dead_entries():
    """allowlist の行が、実在する読み取りを指していること。

    死んだ行が残ると、次にその関数へ直接読み取りが戻ってきたときに
    **黙って許可される**。allowlist が守っているのは「例外が見えていること」
    であって「例外を増やしてよいこと」ではない。
    """
    live = {
        (script, fn, seg)
        for script in AUDITED_MODULES
        for fn, seg in _direct_reads(script)
    }
    dead = sorted(k for k in ALLOWED_DIRECT_READS if k not in live)
    assert not dead, (
        "ALLOWED_DIRECT_READS に、もう存在しない読み取りの行が残っている:\n"
        + "\n".join(f"  {s}:{f}(): {seg}" for s, f, seg in dead)
        + "\n  直したなら、その行は消すこと。")


# ---------------------------------------------------------------------------
# 取り除いた読み方が戻ってきていないこと
# ---------------------------------------------------------------------------
#
# 上の機械検出は式の形 (`open()` / `.read_text()`) を見る。ガードを **足した
# うえで** 直接の読み取りも残す、という形はそちらでも落ちるが、Codex が名指し
# した式そのものは名指しのまま見張っておく (落ちたときに理由が 1 行で分かる)。

FORBIDDEN_READS = [
    ("dispatcher.sh", "task_file.read_text()",
     "publish_agents が割り当て済みカードを直接読んでいる"),
    ("dispatcher.sh", "assignment_file.read_text()",
     "assignment を直接読んでいる (Codex 9 巡目 P2)"),
    ("dispatcher.sh", "STATE_FILE.read_text()", "state.yaml の直接読み"),
    ("dispatcher.sh", "WORKERS_FILE.read_text()", "workers.yaml の直接読み"),
    ("dispatcher.sh", "mfile.read_text()", "mission.yaml の直接読み"),
    ("verifier-dispatcher.sh", "STATE_FILE.read_text()", "state.yaml の直接読み"),
    ("verifier-dispatcher.sh", "WORKERS_FILE.read_text()", "workers.yaml の直接読み"),
    ("verifier-dispatcher.sh", "task_path.read_text()",
     "書き戻す前のカードを直接読んでいる"),
    ("watchdog.py", "state_file.read_text()", "state.yaml の直接読み"),
    ("plan.sh", "open(registry_path)", "workers.yaml の直接読み (Codex 9 巡目 P2)"),
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

    # 例外が **1 つだけ** であることは、機械検出の allowlist のうち
    # 「crewvia のファイルを、意図的にガードの外で読む」行を数えて示す。
    deliberate = [
        key for key in ALLOWED_DIRECT_READS
        if key[0] == "plan.sh"
    ]
    assert deliberate == [("plan.sh", "load_state", "open(STATE_FILE)")], (
        "queue のファイルをガードの外で読む例外が 1 つではなくなっている:\n"
        + "\n".join(f"  {k}" for k in deliberate))
