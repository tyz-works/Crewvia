#!/usr/bin/env python3
"""tests/test_fixture_tree_is_the_only_copier.py — plan.sh / lib_* の隔離コピーは helper 経由だけ (t013 / PR4)。

## 何を止めるか

plan.sh や `scripts/lib_mux.py` などを一時ディレクトリへ写す fixture が「そのとき要ると思った lib」
だけを名前で列挙していると、lib を足すたびに、列挙を直し忘れた fixture が **CI でだけ** 赤くなる
(手元では主 checkout の scripts/ が見える)。この型は 3 回再発した (memory:
shared-module-breaks-single-script-fixtures / new-lib-import-breaks-single-copy-fixtures-again)。

写す入口は 2 つに集めた: `tests/fixture_tree.py` (pytest) と `tests/fixture_tree.sh` (bats / sh)。
どちらも `lib_*` を glob で **まとめて** 写す。このテストは、tests/ と scripts/test_*.sh の中で
`plan.sh` か `lib_*` を **名前を指して** コピーしている箇所を機械で拾い、helper を通っていなければ
赤にする (新しい直書きは必ず赤くなる)。

- Python: `shutil.copy*` の呼び出し (と、それを回す `for` の反復子) が plan.sh / lib_* / scripts/ を指す
- shell / bats: 論理行 (行継続を連結) の `cp` が plan.sh / lib_* を指す

`scripts/` **全体**を写す (`cp -a scripts`) のは全部を写すので対象外。`review-plan.sh` などの
エントリポイントの単体コピーも対象外 (それが読む lib は helper で写す)。

## 許容リスト (理由付き)

`ALLOWED` の各行は「この fixture は lib を名前で指すことに意味がある」理由を持つ。**理由を
書けないなら helper に置き換える**。許容した行に該当が無くなったら (直した／消した) 、その行を
消し忘れているので赤 (許容リストが腐らないように)。
"""

from __future__ import annotations

import ast
import pathlib
import re
import shutil
import subprocess

import pytest

import fixture_tree

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
THIS_FILE = pathlib.Path(__file__).resolve()

#: 個別コピーとみなす対象: plan.sh 本体 (`review-plan.sh` は除く) と `lib_*` の名前。
_TARGET = re.compile(
    r"(?<![-\w])plan\.sh|\bPLAN_SH\b|\blib_[A-Za-z0-9_]+|\bREAL_LIB_")
#: Python の copy 呼び出しで、scripts/ を指しているとみなす語。
_PY_SCRIPTS = re.compile(
    r"(?<![-\w])plan\.sh|\bPLAN_SH\b|\blib_[A-Za-z0-9_]+|\bSCRIPTS_DIR\b|[\"']scripts[\"']")
_COPY_CALLS = {"copy", "copy2", "copyfile", "copytree"}

#: ファイル (repo 相対) → 許容する理由。
ALLOWED: dict[str, str] = {
    "tests/watchdog-idle-e2e.sh":
        "本番の lib_mux.py ではなく **偽 lib_mux** (本番の herdr / tmux に触れないためのもの) を "
        "ROOT に置き、対照実験用の OLDROOT へその偽物を写す。real scripts/ の写しではない。"
        "CI に載っていない手動の e2e",
    "scripts/test_main_repo_git_guard.sh":
        "`hooks/lib_main_repo_git_guard.py` (hooks/ の 1 ファイル) を写す。scripts/lib_* ではない",
}


def _candidate_files() -> list[pathlib.Path]:
    files = []
    for pattern in ("tests/*.py", "tests/*.sh", "tests/*.bats", "scripts/test_*.sh"):
        files += sorted(REPO_ROOT.glob(pattern))
    helpers = {REPO_ROOT / "tests" / "fixture_tree.py", REPO_ROOT / "tests" / "fixture_tree.sh"}
    return [p for p in files if p.resolve() != THIS_FILE and p not in helpers]


def _python_hits(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_text()
    tree = ast.parse(text)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _COPY_CALLS):
            continue
        segment = ast.get_source_segment(text, node) or ""
        cursor = node
        while cursor in parents:                     # `for extra in (...): shutil.copy2(...)`
            cursor = parents[cursor]
            if isinstance(cursor, ast.For):
                segment += " " + (ast.get_source_segment(text, cursor.iter) or "")
                break
        if _PY_SCRIPTS.search(segment):
            hits.append((node.lineno, " ".join(segment.split())[:120]))
    return hits


def _shell_hits(path: pathlib.Path) -> list[tuple[int, str]]:
    hits = []
    logical = path.read_text().replace("\\\n", " ").splitlines()
    for number, line in enumerate(logical, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"(^|[;&|(]|\bthen\b|\bdo\b|!\s)\s*cp(\s|$)", stripped) and _TARGET.search(stripped):
            hits.append((number, stripped[:120]))
    return hits


def _all_hits() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for path in _candidate_files():
        hits = _python_hits(path) if path.suffix == ".py" else _shell_hits(path)
        if hits:
            found[str(path.relative_to(REPO_ROOT))] = hits
    return found


def test_plan_sh_and_lib_copies_go_through_the_helper():
    """名前を指した plan.sh / lib_* のコピーは helper (`fixture_tree`) だけ。"""
    offenders = {rel: hits for rel, hits in _all_hits().items() if rel not in ALLOWED}
    assert not offenders, (
        "plan.sh / lib_* を名前で指してコピーしている fixture がある。lib を足すたびに CI でだけ赤くなる "
        "型なので、tests/fixture_tree.py (pytest) か tests/fixture_tree.sh (bats / sh) の "
        "copy_plan_tree / copy_scripts_libs に置き換えること (理由があって名前を指すなら "
        "ALLOWED に理由を書く):\n"
        + "\n".join(f"  {rel}:{n}: {text}" for rel, hits in sorted(offenders.items())
                    for n, text in hits))


def test_allowed_entries_still_have_a_reason_to_exist():
    """許容リストの各行は、今も該当する箇所を持つ (直した／消したのに残った行は赤)。"""
    hits = _all_hits()
    stale = sorted(rel for rel in ALLOWED if rel not in hits)
    assert not stale, f"ALLOWED に該当の無い行が残っている (消すこと): {stale}"
    for rel, reason in ALLOWED.items():
        assert reason.strip(), f"{rel}: 理由が空"


def test_the_scanner_sees_a_direct_copy(tmp_path, monkeypatch):
    """陽性対照: 直書きのコピーを (Python にも shell にも) 置けば、この走査は拾う。"""
    py = tmp_path / "t_direct.py"
    py.write_text(
        "import shutil\n"
        "def f(root):\n"
        "    shutil.copy2(PLAN_SH, root / 'scripts' / 'plan.sh')\n"
        "    for extra in ('lib_dep_rules.py',):\n"
        "        shutil.copy2(SRC / extra, root / 'scripts' / extra)\n")
    assert [n for n, _ in _python_hits(py)] == [3, 5]
    sh = tmp_path / "t_direct.sh"
    sh.write_text(
        'cp "$REAL/scripts/plan.sh" "$T/scripts/plan.sh"\n'
        'cp "$REAL/scripts/lib_task_cards.py" \\\n    "$T/scripts/"\n'
        'cp "$REAL/scripts/review-plan.sh" "$T/scripts/"\n'          # エントリポイントは対象外
        '# cp "$REAL/scripts/plan.sh" is a comment\n')
    assert [n for n, _ in _shell_hits(sh)] == [1, 2]


# -- helper 自身の契約: lib は glob で拾う (新しい lib は何もしなくても写る) -----------------

def _fake_checkout(root: pathlib.Path) -> pathlib.Path:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("plan.sh", "lint_plan.py", "lib_a.py", "lib_brand_new.py", "lib_x.sh",
                 "git-helpers.sh", "review-plan.sh", "watchdog.py"):
        (scripts / name).write_text(f"# {name}\n")
    return root


def test_python_helper_copies_every_lib_by_glob(tmp_path):
    src = _fake_checkout(tmp_path / "src")
    plan = fixture_tree.copy_plan_tree(tmp_path / "dest", src_root=src)
    copied = sorted(p.name for p in (tmp_path / "dest" / "scripts").iterdir())
    assert plan == tmp_path / "dest" / "scripts" / "plan.sh"
    # 新しい lib (lib_brand_new.py) は、一覧に足さなくても写る。git-helpers.sh / review-plan.sh は写さない
    assert copied == ["lib_a.py", "lib_brand_new.py", "lib_x.sh", "lint_plan.py", "plan.sh"]


def test_python_helper_covers_every_real_lib():
    real = {p.name for p in (REPO_ROOT / "scripts").glob("lib_*")}
    assert real, "scripts/lib_* が見つからない (test の前提が崩れている)"
    assert real <= {p.name for p in fixture_tree.plan_tree_files()}


@pytest.mark.parametrize("fn,expected", [
    ("copy_scripts_libs", ["lib_a.py", "lib_brand_new.py", "lib_x.sh"]),
    ("copy_plan_tree", ["lib_a.py", "lib_brand_new.py", "lib_x.sh", "lint_plan.py", "plan.sh"]),
])
def test_shell_helper_copies_every_lib_by_glob(tmp_path, fn, expected):
    src = _fake_checkout(tmp_path / "src")
    dest = tmp_path / "dest"
    proc = subprocess.run(
        ["bash", "-c", f'source "{REPO_ROOT}/tests/fixture_tree.sh"; {fn} "{src}" "{dest}"'],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in (dest / "scripts").iterdir()) == expected


def test_shell_helper_fails_loudly_without_a_scripts_dir(tmp_path):
    proc = subprocess.run(
        ["bash", "-c", f'source "{REPO_ROOT}/tests/fixture_tree.sh"; '
                       f'copy_scripts_libs "{tmp_path / "nope"}" "{tmp_path / "d"}"'],
        capture_output=True, text=True)
    assert proc.returncode != 0
    assert "no " in proc.stderr
