#!/usr/bin/env python3
"""tests/test_daemon_restart_files_match_load_model.py — `DAEMON_RESTART_FILES`
(scripts/lib_daemon_watch.py) は、それぞれのデーモンが実際に一度だけ読み込む
コードと一致していること (t112 / PR#246 Codex 1 巡目 P1)。

## 何を守るか

`watchdog.py` は単一の長寿命インタプリタで、起動時に import した `lib_*.py`
は最後まで disk の変更を拾わない。逆に `dispatcher.sh` の本体
(`run_dispatch()` の python heredoc) はサイクルごとに **新しい python3
プロセス**を起動し直すので、その heredoc が `import` する `lib_*.py` は
次のサイクルで disk から素直に読み直される — dispatcher の restart は要らない。
dispatcher で restart が要るのは、この bash プロセス自身が**起動時に 1 回だけ
読む**もの: `dispatcher.sh` 自身と、`source` する `lib_daemon_watch.sh` だけ。

`DAEMON_RESTART_FILES` はこの区別を反映した手書きの一覧なので、実際の
`import` / `source` 文から漏れたら気付ける構造ガードをここに置く
(memory: structural-guard-must-match-real-call-shape)。**手で足すだけにしない**
— 一覧に何を足すかではなく、足し忘れたら落ちる仕組みそのものを作る。

`>=` (subset) で比較する: 一覧が実際の依存を **上回る** ぶんには
(過剰な restart で無害) 構わないが、**下回れば** (P1 の欠陥そのもの) 必ず
落ちる。

実行: python3 -m pytest tests/test_daemon_restart_files_match_load_model.py -v
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_daemon_watch as w  # noqa: E402


# ---------------------------------------------------------------------------
# python 側: 直接 + 推移的な `lib_*` import を AST で拾う
# ---------------------------------------------------------------------------

def _lib_imports(py_path: Path) -> set[str]:
    """`py_path` が直接 `import` する `lib_*` モジュール名 (拡張子なし) の集合。

    AST で見る — コメントアウトされた `# import lib_x` のようなテキスト一致の
    誤検出を避ける。
    """
    tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names if a.name.startswith("lib_"))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("lib_"):
                names.add(node.module)
    return names


def _transitive_lib_files(entry: Path, scripts_dir: Path) -> set[str]:
    """`entry` が直接・間接に `import` する `lib_*.py` の集合 (`entry` 自身は
    含まない)。返り値は常に `"scripts/<name>.py"` の形 (DAEMON_RESTART_FILES と
    同じ表記) — `scripts_dir` の実際のディレクトリ名がテストの一時ディレクトリ
    であっても同じ形で比較できるようにする。
    """
    seen: set[str] = set()
    frontier = list(_lib_imports(entry))
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        mod_path = scripts_dir / f"{name}.py"
        if mod_path.exists():
            frontier.extend(_lib_imports(mod_path) - seen)
    return {f"scripts/{name}.py" for name in seen}


# ---------------------------------------------------------------------------
# bash 側: 常駐プロセスが起動時に `source` する相対パスを拾う
# ---------------------------------------------------------------------------

_SOURCE_RE = re.compile(r'^\s*source\s+"\$\{SCRIPT_DIR\}/([^"]+)"', re.MULTILINE)


def _sourced_bash_files(entry_sh: Path) -> set[str]:
    """`entry_sh` (常駐する bash 自身) が `source "${SCRIPT_DIR}/<name>"` の形で
    読み込むファイルの集合 (`"scripts/<name>"` の形)。

    heredoc の中身 (per-cycle で新しい python3 が読み直す部分) にはこの bash の
    `source` 構文は現れない — `test_sourced_bash_files_ignores_per_cycle_python_imports`
    がそれを固定する。
    """
    text = entry_sh.read_text(encoding="utf-8")
    return {f"scripts/{m}" for m in _SOURCE_RE.findall(text)}


# ---------------------------------------------------------------------------
# watchdog: 単一の長寿命インタプリタ — 推移的な import を全部要求する
# ---------------------------------------------------------------------------

def test_watchdog_restart_files_cover_every_transitively_imported_lib():
    expected = {"scripts/watchdog.py"} | _transitive_lib_files(
        SCRIPTS_DIR / "watchdog.py", SCRIPTS_DIR)
    actual = set(w.DAEMON_RESTART_FILES[w.DAEMON_WATCHDOG])
    missing = expected - actual
    assert not missing, (
        f"DAEMON_RESTART_FILES[watchdog] is missing {sorted(missing)} — "
        "watchdog.py imports these once at startup, so a merge that only "
        "changes them leaves the running process silently stale")


# ---------------------------------------------------------------------------
# dispatcher: 毎サイクル新しい python3 — 持続する bash が起動時に source する
# ものだけを要求する (per-cycle の import は要求しない)
# ---------------------------------------------------------------------------

def test_dispatcher_restart_files_cover_every_sourced_bash_file():
    expected = {"scripts/dispatcher.sh"} | _sourced_bash_files(SCRIPTS_DIR / "dispatcher.sh")
    actual = set(w.DAEMON_RESTART_FILES[w.DAEMON_DISPATCHER])
    missing = expected - actual
    assert not missing, (
        f"DAEMON_RESTART_FILES[dispatcher] is missing {sorted(missing)} — "
        "dispatcher.sh sources these once when the persistent bash process "
        "starts, so a merge that only changes them leaves it silently stale")


def test_sourced_bash_files_ignores_per_cycle_python_imports():
    """dispatcher.sh の heredoc は python の import 文であって bash の source
    ではない。次のサイクルで新しい python3 が disk から読み直すので、ここに
    含めると (不要な restart を招くだけで害はないが) 過剰検出になる — 検出器が
    正しく bash の `source` だけを見ていることを固定する。"""
    found = _sourced_bash_files(SCRIPTS_DIR / "dispatcher.sh")
    assert "scripts/lib_dep_rules.py" not in found
    assert "scripts/lib_task_cards.py" not in found
    assert "scripts/lib_review_refusal.py" not in found


# ---------------------------------------------------------------------------
# 陽性対照: 検出ロジック自体が、新しい import / source を足すと気付くこと
# (実際の import / source 文の形で。本物の watchdog.py / dispatcher.sh は汚さない)
# ---------------------------------------------------------------------------

def test_positive_control_new_transitive_import_is_detected(tmp_path):
    """多段の import (`entry -> lib_a -> lib_b`) も辿ることを、実際の import
    文の形 (`import` / `from ... import`) で確かめる。"""
    (tmp_path / "lib_b.py").write_text("z = 1\n")
    (tmp_path / "lib_a.py").write_text("from lib_b import z\n")
    entry = tmp_path / "entry.py"
    entry.write_text("import lib_a\n")
    assert _transitive_lib_files(entry, tmp_path) == {"scripts/lib_a.py", "scripts/lib_b.py"}


def test_positive_control_stale_watchdog_list_would_be_caught(tmp_path):
    """「一覧を更新し忘れた」状態を模して、上のカバレッジテストと同じ比較
    (`expected - actual`) が非空になる = 落ちることを確かめる。"""
    (tmp_path / "lib_mux.py").write_text("x = 1\n")
    (tmp_path / "lib_brand_new_dep.py").write_text("y = 2\n")
    fake_watchdog = tmp_path / "watchdog.py"
    fake_watchdog.write_text("import lib_mux\nimport lib_brand_new_dep\n")

    expected = {"scripts/watchdog.py"} | _transitive_lib_files(fake_watchdog, tmp_path)
    stale_recorded = {"scripts/watchdog.py", "scripts/lib_mux.py"}  # 新しい import を反映していない
    missing = expected - stale_recorded
    assert missing == {"scripts/lib_brand_new_dep.py"}


def test_positive_control_new_dispatcher_source_is_detected(tmp_path):
    fake = tmp_path / "dispatcher.sh"
    fake.write_text(
        '#!/usr/bin/env bash\n'
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'source "${SCRIPT_DIR}/lib_daemon_watch.sh"\n'
        'source "${SCRIPT_DIR}/lib_brand_new.sh"\n')
    assert _sourced_bash_files(fake) == {
        "scripts/lib_daemon_watch.sh", "scripts/lib_brand_new.sh"}
