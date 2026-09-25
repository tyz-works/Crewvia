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
import os
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
    ("dispatcher.sh", "_mux_created_at", {"load_json_store"}),
    ("dispatcher.sh", "_spawn_time_fallback", {"read_queue_text"}),
    # t026: JSON の状態ストアは入口 (`lib_daemon_state.load_json_store`) 経由。入口の中で
    # ガード (`read_regular_text_or_unreadable`) を通ることは下の lib_daemon_state 行が見る。
    ("dispatcher.sh", "_load_state_entry", {"load_json_store"}),
    ("dispatcher.sh", "load_notify_cache", {"load_json_store"}),
    # t010: 「伝えた」台帳。壊れた/読めない台帳を空として扱わない (ENOENT だけが「まだ無い」)。
    ("dispatcher.sh", "load_told", {"load_json_store"}),
    ("lib_review_refusal.py", "load", {"load_json_store"}),
    ("lib_daemon_state.py", "load_json_store", {"read_regular_text_or_unreadable"}),
    ("verifier-dispatcher.sh", "load_notify_cache", {"load_json_store"}),

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
    ("lib_retirement.py", "read_json", {"load_json_store"}),
    ("lib_retirement.py", "read_task_started_at",
     {"read_regular_text_or_unreadable"}),
    ("lib_retirement.py", "assignment_execution_verdict",
     {"read_regular_text_or_unreadable"}),
    ("lib_retirement.py", "created_at_from_cache",
     {"read_regular_text_or_unreadable"}),

    # --- lib_daemon_watch.py (t018) --------------------------------------
    ("lib_daemon_watch.py", "load_config", {"read_regular_text_or_unreadable"}),
    ("lib_daemon_watch.py", "read_pause_state", {"load_json_store"}),

    # --- lib_mux.py / lib_model.py (t018) --------------------------------
    ("lib_mux.py", "read_pane_record", {"load_json_store"}),
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
# **それでもまだ漏れていた** (Codex 11 巡目 P2, t020)。t019 の検出器は
#
#   * `os.open()` の flags を「`O_CREAT` / `O_APPEND` / `O_RDWR` が式のどこかに
#     あれば書き込み」と読んでいた。この 3 つは書き込み専用を意味しない ——
#     `os.open(path, os.O_RDONLY | os.O_CREAT)` は読めるし、既存の FIFO を
#     指していれば無期限に止まる
#   * 見慣れない名前のレシーバを **全部 `Path` だと仮定** していた。そのため
#     `import io as fs; fs.open("queue/state.yaml")` の **ファイル名が
#     モードとして解釈され**、その中の `a` が書き込み除外を発火させていた
#   * 間接読み取り (`from io import open as read_file`) と subprocess 経由の
#     読み取りを、コメントで認めたうえで **防止を規約に委ねていた**
#
# を持っていた。どれも「全部緑のまま未ガードの queue 読み取りを足せる」形
# である。検出器の穴は、構造テスト全体を「守っているつもり」に戻す。
#
# 見える:
#   * `open(...)` (裸)、`<何か>.open(...)` (`Path.open` / `io.open` /
#     `os.open` を含む)、`.read_text()` / `.read_bytes()`
#   * import の別名を解決したうえでの `os` / `io` / `codecs` / `builtins`
#     (`import io as fs` の `fs.open()` も、`from io import open as X` の
#     `X()` も) —— `_module_aliases()` / `_from_import_aliases()`
#   * `getattr(p, "open")` のような文字列経由の間接呼び出しと、
#     `fn = p.open` のように **呼ばずに取り置く** 属性
#   * `os.fdopen()` / `os.read()`
#   * `lib_task_cards.py` に限り `.read()` (ガード本体を `open()` と対で数える)
#   * `subprocess.run/Popen/check_output/…` と `os.system/popen` ——
#     こちらは `ALLOWED_SUBPROCESS_CALLS` の表で別に見る
#   * 書き込みは除く。ただし **証明できるときだけ**: モード文字列に
#     `w` / `a` / `x` を含むもの、`os.open()` の **アクセスモード** が
#     `O_WRONLY` だと式から読み取れるもの。レシーバの型が分からない
#     `<x>.open("...")` は、第 1 引数が **モード文字列として通る形**
#     (`_looks_like_mode()`) のときだけ `Path.open(mode)` と読む。
#     `_is_write_open()` 参照
#   * `from os import ...` で入ってきた名前は **実体を解決してから**
#     モード/フラグ引数を選ぶ。整数フラグを取るのは `os.open` だけで、
#     `os.fdopen` はモード文字列を取る (t021 / Codex 12 巡目 指摘 6)
#
# ---------------------------------------------------------------------------
# 検出できない形 —— **実態より狭く書かないこと**
# ---------------------------------------------------------------------------
#
# 以前ここには「動的に組み立てた argv だけが手動レビューを要する」と書いて
# あった。**これは実態より狭い**。下の (B) はどれも静的に読める形であり、
# (A-1) は argv が完全にリテラルでも素通りする。**守れない範囲を狭く書くと、
# レビュアーはそこを見なくなる** —— 記述の甘さは検出漏れと同じだけ危ない。
#
# (A) 検出はするが、表が通してしまう:
#   * A-1 `subprocess.run(["bash", "-c", "cat queue/state.yaml"])` ——
#     program は `bash` として **拾えている**。しかし
#     `ALLOWED_SUBPROCESS_CALLS` の鍵は `(script, 関数, program)` だけで、
#     **引数を縛っていない**。だから `cmd_review` の既存の `bash` 許可に
#     一致し、**完全にリテラルな shell 読み取りでも通る**。動的 argv
#     (`subprocess.run(cmd)`) も同じ理由で通る —— 表は `cmd` という式のまま
#     1 行を持つので、中身が `cat` に変わっても当たり続ける。
#     → **argv の変更は、それ自体をレビュー対象にすること**
#       (knowledge/review.md)
#
# (B) 静的には読めるのに、現実装が見ていない (t021 で Director 判断により
#     backlog。**規約ではなく、別の手段で担保すること**):
#   * B-1 別名 import された subprocess 関数 ——
#     `from subprocess import check_output as capture; capture([...])`。
#     `_scan_subprocess()` は **呼ばれている名前** で表を引く
#   * B-2 変数に取り置いた subprocess 関数 ——
#     `run = subprocess.check_output; run([...])`。取り置きの検出は
#     open 系の属性 (`_READER_ATTRS`) だけを見ている
#   * B-3 定数 `getattr` の subprocess 版 ——
#     `getattr(subprocess, "check_output")([...])`。`getattr` の検出も
#     `_READER_ATTRS` だけを見ている
#   * B-4 **再束縛された `O_*` 定数** —— `O_WRONLY = 0` と置いてから
#     `os.open(path, O_WRONLY)`。`_flag_names()` は裸の `O_*` を
#     **出自を確かめずに信じる**ので、これは書き込み専用と判定されて
#     検出から外れる。**倒れる方向が危険な唯一の項目** —— 他は「見えない」
#     だけだが、これは読み取りを書き込みだと **積極的に誤判定** する
#   * B-5 裸の名前を変数に取り置く形 —— `reader = open; reader(path).read()`。
#     取り置きの検出は `ast.Attribute` だけで、`ast.Name` を見ていない
#   * B-6 `from os import read as read_fd; read_fd(fd, 100)` ——
#     `_READER_ATTRS` に `read` が無いため、import 経由の `os.read` は
#     そもそも候補に入らない
#
# (C) 原理的に閉じないもの:
#   * `exec()` / `eval()` / C 拡張 / `ctypes` 経由の読み取り
#   * 完全に動的な属性名 (`getattr(p, verb)()` の `verb` が変数)
#   * `AUDITED_MODULES` に載っていないモジュールそのもの (hooks/ は対象外)
#   * `.sh` の中では `<<'PYEOF'` ブロック **1 つ目だけ** (`_python_source()`)
#   * `os` / `io` / `codecs` という名前の **変数** に Path を入れて
#     `.open()` する形 (import の別名解決が優先される)
#
# **この検出器は「うっかり直接読み取りを足す」ことを止める補助であって、
# 敵対的なすり抜けを防ぐ境界ではない。** (B) はすべて「意図的に分かりにくく
# 書いたコード」を要求する。そこを完璧にしても得られる安全は小さく、検出器の
# 複雑さだけが増えるので、t021 で追いかけっこを打ち切った (Codex 12 巡目は
# P1 ゼロ・3 巡連続)。
#
# 閉じない指摘は閉じないと明言する
# (memory: and-condition-beats-unforgeable-evidence)。(A) (B) (C) は
# allowlist では止められないので、`knowledge/review.md` のレビュー観点に
# 載せて人の目で見る —— 新しい読み方が要るときは、まずこの検出器を広げること。

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
    # t026: デーモン側 JSON 状態ストアの入口と、その読み手。
    "lib_daemon_state.py",
    "lib_review_refusal.py",
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

    # -- ファイルの中身を読まない open --------------------------------------
    # t020 (Codex 11 巡目 P2-1): `os.open()` の flags を具体的に解析するように
    # したので、`O_RDWR` で開くロックファイルが見えるようになった。読める形で
    # 開いてはいるが、**read は一度もしない**。
    ("lib_daemon_watch.py", "daemon_lock",
     "os.open(path, os.O_CREAT | os.O_RDWR, 0o644)"):
        "flock 用のロックファイル。fd は fcntl.flock にしか渡さず、中身は "
        "読まない。`O_RDWR` なので FIFO を置かれても open は返る (Linux)",

    # -- ガードの実装そのもの ----------------------------------------------
    ("lib_task_cards.py", "_read_regular_file", "f.read()"):
        "ガード本体。ここが唯一の `open()` で、O_NONBLOCK + fstat の判定を持つ",
    # t020: `os.fdopen()` も検出するようにしたので、ガード本体の
    # 「fd をテキスト stream にする」1 行が見えるようになった。
    ("lib_task_cards.py", "_read_regular_file", "os.fdopen(fd, newline=newline)"):
        "ガード本体。すでに fstat で通常ファイルだと確かめた fd を包むだけで、"
        "ここで新しくパスを開いてはいない",
    # t019: 属性形式の open を検出するようにして、ガード本体の `os.open()` も
    # 見えるようになった。`f.read()` と対で 1 組。
    ("lib_task_cards.py", "_read_regular_file",
     "os.open(path, os.O_RDONLY | os.O_NONBLOCK)"):
        "ガード本体。`O_NONBLOCK` を付けて開くこと自体が判定の一部なので、"
        "ここだけは自分自身を通せない",
}

_READ_ATTRS = ("read_text", "read_bytes")

#: `open()` を作り出す名前。別名 import / `getattr` / 属性の取り置きを
#: 追いかけるときの終点になる (t020)。
_READER_ATTRS = frozenset({"open", "read_text", "read_bytes", "fdopen"})

#: `os.open()` の **アクセスモード**。低 2bit の排他的な 3 値で、
#: `O_CREAT` / `O_APPEND` / `O_EXCL` / `O_TRUNC` のような修飾フラグとは
#: 別物である (t020 / Codex 11 巡目 P2-1)。
_OS_ACCESS_WRITE_ONLY = "O_WRONLY"
_OS_ACCESS_READABLE = frozenset({"O_RDONLY", "O_RDWR"})

#: モジュール関数としての `open()` —— パスが第 1 引数で、モード/フラグが
#: 第 2 引数に来る。`Path.open()` (モードが **第 1** 引数) と区別する。
_MODULE_OPEN_RECEIVERS = frozenset({"os", "io", "codecs", "builtins"})

#: `open()` のモード文字列に現れうる文字。**ファイル名と見分けるため**に
#: 使う —— `"queue/state.yaml"` はここで落ちる (t020 / Codex 11 巡目 P2-2)。
_MODE_CHARS = frozenset("rwxab+tU")

#: 外部プロセスを起こす呼び出し。`cat` に読ませる形を機械で見張るための入口。
_SUBPROCESS_FUNCS = frozenset({
    ("subprocess", "run"), ("subprocess", "Popen"),
    ("subprocess", "call"), ("subprocess", "check_call"),
    ("subprocess", "check_output"), ("subprocess", "getoutput"),
    ("subprocess", "getstatusoutput"),
    ("os", "system"), ("os", "popen"),
    ("os", "spawnv"), ("os", "spawnvp"), ("os", "spawnvpe"),
})


def _arg(node: ast.Call, index: int, keyword: str):
    """位置引数 `index`、無ければキーワード `keyword` の値。どちらも無ければ `None`。"""
    if len(node.args) > index:
        return node.args[index]
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    """`import io as fs` → `{"fs": "io"}`、`import os` → `{"os": "os"}`。

    **別名を解決してからモード引数を選ぶ** ために要る (Codex 11 巡目 P2-2)。
    これが無いと `fs.open("queue/state.yaml")` の第 1 引数がモードだと
    読まれ、ファイル名の中の `a` が書き込み除外を発火させていた。
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for entry in node.names:
                if entry.asname:
                    aliases[entry.asname] = entry.name
                else:
                    root = entry.name.split(".")[0]
                    aliases[root] = root
    return aliases


def _from_import_aliases(tree: ast.AST, modules, names) -> dict[str, tuple]:
    """`from io import open as read_file` → `{"read_file": ("io", "open")}`。

    裸の名前で呼ばれる「開く関数」を `open()` と同じ土俵に乗せる
    (Codex 11 巡目 P2-3)。

    **モジュール名だけでなく、import 元の関数名も持つ** (Codex 12 巡目
    指摘 6)。`os` から来たことだけを覚えていると、モード引数の読み方が
    決まらない —— `os.open()` は第 2 引数が **整数フラグ**、
    `os.fdopen()` は **モード文字列** である。実体を区別しないと
    `from os import fdopen; fdopen(fd, "w")` が「読み切れないフラグ」
    として読み取り扱いになり、正当な書き込みが誤検出される。
    """
    out: dict[str, tuple] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in modules:
            for entry in node.names:
                if entry.name in names:
                    out[entry.asname or entry.name] = (node.module, entry.name)
    return out


def _looks_like_mode(value) -> bool:
    """`open()` のモード文字列として通る形か。

    レシーバの型が分からない `<x>.open("...")` で、第 1 引数がモードなのか
    パスなのかを分けるのに使う。モード文字列は `"rwxab+tU"` の文字だけから
    なる 4 文字以下の文字列で、`"queue/state.yaml"` はそのどちらの条件にも
    当たらない。
    """
    return (isinstance(value, str) and 1 <= len(value) <= 4
            and set(value) <= _MODE_CHARS)


def _mode_is_write(node) -> bool:
    """モード引数が、書き込みを **明示している** ときだけ True。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return any(c in node.value for c in "wax")
    return False


def _flag_names(node):
    """`os.O_WRONLY | os.O_CREAT` から `{"O_WRONLY", "O_CREAT"}` を作る。

    `os.O_*` / 裸の `O_*` / `|` / 整数リテラル以外が混ざったら `None` ——
    **読み切れない式は「書き込みだ」と決めない**。
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _flag_names(node.left)
        right = _flag_names(node.right)
        if left is None or right is None:
            return None
        return left | right
    if isinstance(node, ast.Attribute) and node.attr.startswith("O_"):
        return {node.attr}
    if isinstance(node, ast.Name) and node.id.startswith("O_"):
        return {node.id}
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        # 生の整数。`O_RDONLY` が 0 である以上、ここから書き込み専用は
        # 証明できない。
        return set()
    return None


def _os_open_is_write_only(node: ast.Call) -> bool:
    """`os.open()` が **書き込み専用だと証明できる** ときだけ True。

    t019 までは「`O_CREAT` / `O_APPEND` / `O_RDWR` のどれかが式のどこかに
    現れたら捨てる」という見方をしていた。これらは書き込み専用を意味しない
    —— アクセスモードは `O_RDONLY` / `O_WRONLY` / `O_RDWR` の排他的な 3 値
    で、残りは修飾にすぎない。そのため
    `os.read(os.open(path, os.O_RDONLY | os.O_CREAT), 100)` が検出から
    外れていた。既存の FIFO を指していれば、この `open` は無期限に止まる
    (Codex 11 巡目 P2-1)。

    しかも旧実装は `ast.walk(node)` で **呼び出し全体** を見ていたので、
    パス側の式に `O_` で終わる属性があるだけでも除外されえた。ここでは
    flags 引数だけを見る。
    """
    flags = _arg(node, 1, "flags")
    if flags is None:
        return False
    names = _flag_names(flags)
    if names is None:
        return False
    if names & _OS_ACCESS_READABLE:
        return False
    return _OS_ACCESS_WRITE_ONLY in names


def _open_kind(node: ast.Call, aliases, open_aliases) -> str:
    """`open` の呼び方を 4 つに分ける。

    * `"builtin"` —— 組み込みの `open(path, mode)` と同じ並び (`io.open` も)
    * `"os"`      —— `os.open(path, flags)`。モードではなく整数フラグ
    * `"module"`  —— `io` / `codecs` / `builtins` の `open(path, mode)`
    * `"ambiguous"` —— レシーバの型が分からない。`Path.open(mode)` かも
      しれないし、追えなかったモジュールの `open(path, mode)` かもしれない

    裸の名前は **import の実体を先に解決する** (Codex 12 巡目 指摘 6)。
    `from os import open` を組み込みの `open` と読むと、第 2 引数の
    `O_WRONLY` が「モード文字列ではない」として書き込み判定から落ち、
    正当な書き込みが読み取りとして報告されていた。逆に `from os import
    fdopen` を `os.open` と同じ整数フラグ扱いにすると、`fdopen(fd, "w")`
    が読み切れないフラグとして誤検出される。**整数フラグを取るのは
    `os.open` だけ**で、`os.fdopen` はモード文字列を取る。
    """
    func = node.func
    if isinstance(func, ast.Name):
        origin = open_aliases.get(func.id)
        if origin is not None:
            module, orig_name = origin
            if module == "os" and orig_name == "open":
                return "os"
            return "builtin"
        if func.id == "open":
            return "builtin"
        return "ambiguous"
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        module = aliases.get(func.value.id)
        if module == "os":
            return "os"
        if module in _MODULE_OPEN_RECEIVERS:
            return "module"
        return "ambiguous"
    return "ambiguous"


def _is_write_open(node: ast.Call, aliases=None, open_aliases=None) -> bool:
    """`open(path, 'w')` のような書き込みは読み取り経路ではない。

    `'a+'` (ロック取得) もここで落ちる —— `flock` のために開くだけで、
    中身を読んでいないからである。

    モードがどこにあるかは呼び方で違う:

    * 組み込みの `open(path, "a")`   → 第 2 引数
    * `Path.open("a")`               → 第 1 引数 (パスはレシーバ側)
    * `os.open(path, os.O_WRONLY|…)` → 第 2 引数だが **整数フラグ**

    **「書き込みだ」と判定した読み取りは検出から外れる**ので、この判定は
    狭いほうへ倒す —— 迷ったら読み取りとして報告し、allowlist に理由を
    書かせる (memory: approve-judgment-needs-allowlist-and-scope)。

    t020 で変えたのは 2 点:

    1. `os.open()` は flags を **具体的に解析** し、書き込み専用だと
       証明できるときだけ除外する (`_os_open_is_write_only`)
    2. レシーバは import の別名を解決してから分類し、**型が分からない
       レシーバは Path だと決めつけない**。第 1 引数がモード文字列として
       通る形のときだけ `Path.open(mode)` と読む (`_looks_like_mode`)
    """
    aliases = aliases or {}
    open_aliases = open_aliases or {}
    kind = _open_kind(node, aliases, open_aliases)

    if kind == "os":
        return _os_open_is_write_only(node)

    if kind in ("builtin", "module"):
        return _mode_is_write(_arg(node, 1, "mode"))

    # ambiguous —— `Path.open("w")` かもしれず、追えなかったモジュールの
    # `open(path, "w")` かもしれない。**モードとして通る文字列** が見つかった
    # ときだけ書き込みと読む。ファイル名は `_looks_like_mode()` で落ちるので、
    # `fs.open("queue/state.yaml")` は読み取りとして残る。
    first = _arg(node, 0, "mode")
    if isinstance(first, ast.Constant) and _looks_like_mode(first.value):
        return _mode_is_write(first)
    second = _arg(node, 1, "mode")
    if isinstance(second, ast.Constant) and _looks_like_mode(second.value):
        return _mode_is_write(second)
    return False


def _owner_by_line(tree: ast.AST) -> dict[int, str]:
    """行番号 → その行を含む関数名。"""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    owner.setdefault(sub.lineno, node.name)
    return owner


def _scan_reads(source: str, filename: str = "<synthetic>"):
    """`(関数名, ソース断片)` を、そのソースの読み取り全部について返す。

    `filename` は 2 つの役目を持つ: AST のエラー表示と、`lib_task_cards.py`
    だけ `.read()` / `os.fdopen()` を `open()` と対で数える分岐。

    **合成ソースでも呼べる形にしてある** —— 検出器そのものの回帰テスト
    (`test_the_detector_sees_*`) が、本番コードを触らずに「この形を
    見落としていないか」を確かめられるようにするため (t020)。
    """
    tree = ast.parse(source, filename=filename)
    aliases = _module_aliases(tree)
    open_aliases = _from_import_aliases(tree, _MODULE_OPEN_RECEIVERS,
                                        {"open", "fdopen"})
    owner = _owner_by_line(tree)
    guard_body = filename == "lib_task_cards.py"
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

    found = []

    def _record(node):
        segment = ast.get_source_segment(source, node) or "<unparsed>"
        found.append((owner.get(node.lineno, "<module>"),
                      " ".join(segment.split())))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and id(node) not in call_funcs:
            # `fn = p.open` のように **呼ばずに取り置く** 形 (t020)。
            # ここを見ていないと、別名にしてから呼ぶだけで検出を抜けられる。
            if node.attr in _READER_ATTRS or (guard_body and node.attr == "read"):
                _record(node)
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and (func.id == "open"
                                           or func.id in open_aliases):
            # t020 (Codex 11 巡目 P2-3): `from io import open as read_file` の
            # ように **別名で import された開く関数** も裸の `open()` と同じに
            # 数える。
            if _is_write_open(node, aliases, open_aliases):
                continue
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            # t019 (Codex 10 巡目 P2-1): **属性形式の open も検出する**。
            # ここを裸の `open()` だけにしていたせいで、`lib_model.resolve()`
            # の `p.open(encoding="utf-8")` —— PyYAML がある通常経路 ——
            # が最初から視界の外にいた。置き違えた FIFO 1 枚でモデル解決が
            # 無期限に止まるのに、この構造テストは緑のままだった。
            # **検出器に漏れがあると、構造テスト自体が「守っているつもり」
            # になる。**
            if _is_write_open(node, aliases, open_aliases):
                continue
        elif isinstance(func, ast.Attribute) and func.attr in _READ_ATTRS:
            pass
        elif isinstance(func, ast.Name) and func.id == "getattr":
            # `getattr(p, "open")()` —— 文字列で名前を渡す間接呼び出し (t020)。
            name = _arg(node, 1, "name")
            if not (isinstance(name, ast.Constant)
                    and name.value in _READER_ATTRS):
                continue
        elif (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and aliases.get(func.value.id) == "os"
                and func.attr in ("fdopen", "read")):
            # fd から直に読む形 (t020)。`os.fdopen()` はモードが第 2 引数
            # なので、書き込みはここで落とす。読み取りはガード本体が持って
            # いるぶんだけ allowlist に入る。
            if func.attr == "fdopen" and _mode_is_write(_arg(node, 1, "mode")):
                continue
        elif (isinstance(func, ast.Attribute) and func.attr == "read"
                and guard_body):
            # ガード本体の `f.read()` だけは、`open()` と対で数える。
            pass
        else:
            continue
        _record(node)
    return found


def _direct_reads(script: str):
    """`(関数名, ソース断片)` を、そのモジュールの読み取り全部について返す。"""
    return _scan_reads(_python_source(script), script)


def _program_of(node: ast.Call) -> str:
    """外部プロセス呼び出しが起こすプログラム名。読み切れなければ式そのもの。"""
    argv = _arg(node, 0, "args")
    if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
        head = argv.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return os.path.basename(head.value)
        return ast.unparse(head)
    if isinstance(argv, ast.Constant) and isinstance(argv.value, str):
        parts = argv.value.split()
        return os.path.basename(parts[0]) if parts else "<empty>"
    if argv is None:
        return "<none>"
    return ast.unparse(argv)


def _scan_subprocess(source: str, filename: str = "<synthetic>"):
    """`(関数名, プログラム名)` を、そのソースの外部プロセス呼び出し全部について返す。"""
    tree = ast.parse(source, filename=filename)
    aliases = _module_aliases(tree)
    bare = _from_import_aliases(tree, {"subprocess", "os"},
                               {name for _, name in _SUBPROCESS_FUNCS})
    owner = _owner_by_line(tree)

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = aliases.get(func.value.id, func.value.id)
            if (module, func.attr) not in _SUBPROCESS_FUNCS:
                continue
        elif isinstance(func, ast.Name) and func.id in bare:
            # `bare` は `(モジュール, import 元の関数名)` を持つが、ここで
            # 見るのは **呼ばれている名前** のほうである。import 元の名前で
            # 照合すると `from subprocess import check_output as capture`
            # まで拾えるようになるが、別名 import の追跡は今回の範囲外
            # (Director 判断で backlog)。振る舞いを変えないため、
            # モジュールだけ取り出して従来どおり `func.id` と対で見る。
            module, _orig_name = bare[func.id]
            if (module, func.id) not in _SUBPROCESS_FUNCS:
                continue
        else:
            continue
        found.append((owner.get(node.lineno, "<module>"), _program_of(node)))
    return found


def _subprocess_calls(script: str):
    return _scan_subprocess(_python_source(script), script)


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
# 外部プロセス経由の読み取り (t020 / Codex 11 巡目 P2-3)
# ---------------------------------------------------------------------------
#
# `open()` を全部ガードに通しても、`subprocess.check_output(["cat", path])`
# なら素通りできる。t019 のコメントはこの抜け道を **認めたうえで、防止を
# 規約に委ねていた** —— つまり構造テストが全部緑のまま、未ガードの queue
# 読み取りを足せる状態だった。
#
# 規約でなく表にする。対象モジュールの外部プロセス呼び出しを AST で全部拾い、
# `(モジュール, 関数, プログラム名)` が下の表に無ければ落とす。`cat` を
# 足した瞬間に赤くなるし、既存の行は「何を起こしていて、なぜファイルを
# 読んでいないと言えるのか」を 1 行で持つ。
#
# プログラム名は argv の先頭。リテラルなら basename、式なら式そのもの。
# **式のままの行 (`cmd` / `argv`) は、その関数の中身が変わっても同じ行に
# 当たり続ける** —— そこは下の「原理的に閉じないもの」に書いてある。

ALLOWED_SUBPROCESS_CALLS = {
    # -- plan.sh -----------------------------------------------------------
    ("plan.sh", "cmd_pull", "bash"):
        "pull 後の worktree 作成スクリプト。queue のファイルは渡していない",
    ("plan.sh", "cmd_review", "bash"):
        "scripts/review-plan.sh を起こす。プラン本体は向こうが "
        "lib_task_cards 経由で読む",
    ("plan.sh", "cmd_done", "sys.executable"):
        "lib_registry.py bump-task-count のサブコマンド。registry の読み書きは "
        "向こうの with_lock() + read_regular_text() の中",

    # -- dispatcher.sh -----------------------------------------------------
    ("dispatcher.sh", "spawn_kai_review", "cmd"):
        "kai-review.sh を起こすだけ。カードを読むのは向こうの plan.sh pull",

    # -- lib_retirement.py -------------------------------------------------
    ("lib_retirement.py", "_default_run_command", "argv"):
        "退役の後始末コマンド (git worktree remove 等) を走らせる注入点。"
        "argv は呼び出し側が組み、ファイルの中身は受け取らない",

    # -- lib_mux.py: mux の制御コマンド -------------------------------------
    # どれも tmux / herdr にペインを操作させるもので、ファイルを読ませていない。
    ("lib_mux.py", "spawn", "tmux"): "ペインを作る",
    ("lib_mux.py", "kill", "tmux"): "ペインを畳む",
    ("lib_mux.py", "list", "tmux"): "ペイン一覧",
    ("lib_mux.py", "capture", "tmux"): "ペインの画面を取る",
    ("lib_mux.py", "attach", "tmux"): "セッションに繋ぐ",
    ("lib_mux.py", "available", "_HERDR_CLI['version']"): "herdr の版を訊く",
    ("lib_mux.py", "server_running", "tmux"): "サーバーの生死",
    ("lib_mux.py", "server_identity", "tmux"): "サーバーの socket と pid",
    ("lib_mux.py", "_send_to_target", "tmux"): "ペインにキーを送る",
    ("lib_mux.py", "_inspect_pane_full", "tmux"): "ペインの属性を訊く",
    ("lib_mux.py", "record_existence", "tmux"):
        "記録が指す window id の実在を訊く (t001。tmux に `list-windows -a` を"
        "投げるだけで、queue / registry のファイルは読ませない)",
    ("lib_mux.py", "_destroy_window_on", "tmux"):
        "if-shell で 1 つの接続に検証と破壊を流す "
        "(memory: verify-and-destroy-must-share-one-connection)",
    ("lib_mux.py", "_herdr_run", "cmd"):
        "`_HERDR_CLI[key] + extra_args`。herdr CLI の固定語彙しか入らない",
    ("lib_mux.py", "_herdr_run_raw", "cmd"): "同上 (JSON に包まない版)",
    ("lib_mux.py", "_herdr_start_server", "_HERDR_CLI['server_start']"):
        "herdr サーバーを起こす",
}


@pytest.mark.parametrize("script", AUDITED_MODULES)
def test_no_unaudited_subprocess_remains(script):
    """このモジュールに、表に無い外部プロセス呼び出しが残っていないこと。

    RED の作り方 —— どれかのモジュールに
    `subprocess.check_output(["cat", "queue/state.yaml"])` を足すと、
    `cat` の行が表に無いのでここが落ちる。
    """
    unexpected = sorted(
        {(fn, prog) for fn, prog in _subprocess_calls(script)
         if (script, fn, prog) not in ALLOWED_SUBPROCESS_CALLS})
    assert not unexpected, (
        f"{script}: 表に無い外部プロセス呼び出しがある:\n"
        + "\n".join(f"  {fn}(): {prog}" for fn, prog in unexpected)
        + "\n\n  外部プロセスに queue / registry を読ませると、"
          "ガードを丸ごと迂回できる。\n"
          "  ファイルを読ませていないなら ALLOWED_SUBPROCESS_CALLS に "
          "**理由付きで** 1 行足すこと。\n"
          "  読ませる必要があるなら、読むのは python 側で "
          "lib_task_cards を通すこと。")


def test_the_subprocess_allowlist_has_no_dead_entries():
    """外部プロセスの表にも、死んだ行を残さない。

    死んだ行が残ると、その関数に別のプログラムが戻ってきたときに黙って
    許可される —— `ALLOWED_DIRECT_READS` と同じ理由である。
    """
    live = {
        (script, fn, prog)
        for script in AUDITED_MODULES
        for fn, prog in _subprocess_calls(script)
    }
    dead = sorted(k for k in ALLOWED_SUBPROCESS_CALLS if k not in live)
    assert not dead, (
        "ALLOWED_SUBPROCESS_CALLS に、もう存在しない呼び出しの行が残っている:\n"
        + "\n".join(f"  {s}:{f}(): {p}" for s, f, p in dead)
        + "\n  直したなら、その行は消すこと。")


# ---------------------------------------------------------------------------
# 検出器そのものの回帰 (t020)
# ---------------------------------------------------------------------------
#
# 上の 2 つの表は「検出器が拾ったもの」しか見ない。**拾えていない形**は
# どちらの表にも現れず、全部緑のままになる —— それが Codex 10 巡目 (属性
# 形式の `open`) と 11 巡目 (os.open のフラグ / 別名 import / 間接読み取り)
# で 2 度続けて起きたことだった。
#
# だから検出器を合成ソースに当てて、**見えるべき形が見えていること**と
# **書き込みを読み取りと誤認しないこと**を対にして固定する。ここが緑でも
# 本番が緑とは限らないが、ここが赤なら検出器に穴が開いている。

#: `(名前, 拾ってほしい断片 または None, ソース)`。
#:
#: 断片を書くのは、**どの式が拾われたか** まで固定するためである。「何か 1 つ
#: 拾えた」で通す形にすると、同じソースの別の式 (たとえば `os.read()`) が
#: 拾われているだけで緑になり、直したはずの穴が閉じていなくても気付けない。
_DETECTOR_READ_CASES = [
    # --- Codex 11 巡目 P2-1: os.open のフラグ --------------------------
    # `O_CREAT` / `O_APPEND` / `O_RDWR` は書き込み **専用** を意味しない。
    ("os-rdonly-with-creat", "os.open(path, os.O_RDONLY | os.O_CREAT)",
     "import os\nos.read(os.open(path, os.O_RDONLY | os.O_CREAT), 100)\n"),
    ("os-rdwr-with-creat", "os.open(path, os.O_CREAT | os.O_RDWR, 0o644)",
     "import os\nfd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)\n"),
    ("os-append-without-access-mode", "os.open(path, os.O_APPEND | os.O_CREAT)",
     "import os\nfd = os.open(path, os.O_APPEND | os.O_CREAT)\n"),
    ("os-flags-from-a-variable", "os.open(path, flags)",
     "import os\nfd = os.open(path, flags)\n"),
    ("os-provably-write-only", None,
     "import os\nfd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)\n"),
    ("os-write-only-with-append", None,
     "import os\nfd = os.open(path, os.O_WRONLY | os.O_APPEND)\n"),

    # --- Codex 11 巡目 P2-2: 別名 import されたモジュール ---------------
    # ファイル名の中の `a` をモードだと読むと、検出から外れる。
    ("aliased-module-open-reads-a-filename", 'fs.open("queue/state.yaml")',
     'import io as fs\nfs.open("queue/state.yaml").read()\n'),
    ("aliased-module-open-writes", None,
     'import io as fs\nfs.open("queue/state.yaml", "w")\n'),
    ("unresolvable-receiver-with-a-filename", 'fs.open("queue/state.yaml")',
     'fs.open("queue/state.yaml")\n'),
    ("path-open-append-is-a-write", None,
     "LOG_FILE.open('a')\n"),
    ("path-open-read", 'p.open(encoding="utf-8")',
     'p.open(encoding="utf-8")\n'),
    ("path-call-open-write", None,
     'from pathlib import Path\nPath(x).open("w")\n'),

    # --- Codex 11 巡目 P2-3: 間接読み取り -------------------------------
    ("from-import-open-alias", "read_file(path)",
     "from io import open as read_file\nread_file(path).read()\n"),
    ("getattr-open", 'getattr(p, "open")',
     'getattr(p, "open")()\n'),
    ("attribute-taken-aside", "p.open",
     "fn = p.open\n"),
    ("os-fdopen-read", "os.fdopen(fd, newline=None)",
     "import os\nos.fdopen(fd, newline=None)\n"),
    ("os-fdopen-write", None,
     'import os\nos.fdopen(fd, "w", encoding="utf-8")\n'),

    # --- Codex 12 巡目 指摘 6: import した関数の実体を解決する ----------
    # `from os import ...` で入ってきた名前は、モジュール名だけでは
    # モード引数の読み方が決まらない。`os.open()` は **整数フラグ**、
    # `os.fdopen()` は **モード文字列** を第 2 引数に取る。実体を見ずに
    # 「os から来た」で括ると、正当な書き込みが読み取りとして報告される。
    #
    # **誤検出は allowlist を「書き込みの置き場」に変えてしまう** ——
    # 検出器が邪魔になると、人は理由を書かずに黙らせる方向へ流れ、
    # そこに本物の読み取りが紛れても気付けなくなる。
    ("from-import-os-fdopen-write", None,
     'from os import fdopen\nfdopen(fd, "w")\n'),
    ("from-import-os-open-write-only", None,
     "from os import open, O_WRONLY\nopen(path, O_WRONLY)\n"),

    # 実体を解決したあとも、**読み取りは読み取りとして残る**こと。
    # 上の 2 件を黙らせるだけの直し方をすると、`from os import open` 経由の
    # 読み取りがまとめて視界から消える —— 誤検出を消す修正が、本物の
    # 見落としを作らないことを対で固定する。
    ("from-import-os-fdopen-read", "fdopen(fd)",
     "from os import fdopen\nfdopen(fd)\n"),
    ("from-import-os-open-rdonly", "open(path, O_RDONLY)",
     "from os import open, O_RDONLY\nopen(path, O_RDONLY)\n"),
]


@pytest.mark.parametrize("name,expected,source", _DETECTOR_READ_CASES,
                         ids=[c[0] for c in _DETECTOR_READ_CASES])
def test_the_detector_sees_this_read(name, expected, source):
    """この書き方を、検出器が拾う (拾わない) こと。"""
    segments = [seg for _, seg in _scan_reads(source, "<synthetic>")]
    if expected is not None:
        assert expected in segments, (
            f"{name}: 検出器がこの読み取りを見落としている:\n{source}\n"
            f"  拾ってほしかった: {expected}\n"
            f"  実際に拾ったもの: {segments}\n"
            "  見落とした形は allowlist にも現れないので、"
            "構造テストは緑のまま未ガードの読み取りを通す。")
    else:
        assert not segments, (
            f"{name}: 書き込みを読み取りとして報告している:\n{source}\n"
            f"  検出したもの: {segments}\n"
            "  誤検出は allowlist を「書き込みの置き場」にしてしまい、"
            "次に本物の読み取りが混ざっても気付けなくなる。")


_DETECTOR_SUBPROCESS_CASES = [
    ("subprocess-check-output-cat", "cat",
     'import subprocess\nsubprocess.check_output(["cat", path])\n'),
    ("aliased-subprocess-module", "cat",
     'import subprocess as sp\nsp.run(["cat", path])\n'),
    ("from-import-check-output", "cat",
     'from subprocess import check_output\ncheck_output(["cat", path])\n'),
    ("os-system-shell-string", "cat",
     'import os\nos.system("cat queue/state.yaml")\n'),
    ("absolute-path-program", "cat",
     'import subprocess\nsubprocess.run(["/bin/cat", path])\n'),
]


@pytest.mark.parametrize("name,program,source", _DETECTOR_SUBPROCESS_CASES,
                         ids=[c[0] for c in _DETECTOR_SUBPROCESS_CASES])
def test_the_detector_sees_this_subprocess_read(name, program, source):
    """外部プロセスに読ませる形を、プログラム名まで含めて拾えること。"""
    found = _scan_subprocess(source, "<synthetic>")
    assert [prog for _, prog in found] == [program], (
        f"{name}: 外部プロセス経由の読み取りを取りこぼしている:\n{source}\n"
        f"  拾ったもの: {found}")


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
