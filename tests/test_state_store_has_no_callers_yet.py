"""呼び出し元ゼロの確認 (S2 / R2): plan.sh・dispatcher・hooks は `lib_state_store` をまだ使わない。

R2 (ユーザー決定): 呼び出し元ゼロの lib は通常どおり merge、**呼び出し側を移す PR は merge 前に
ユーザー承認**。plan.sh は主 checkout から直接実行されるので、plan.sh が import した瞬間が
cutover になる。この PR (S2) が本番の挙動を変えないことを、import 文の形ではなく**名前の出現**で
固定する (bash の `sys.path` 経由・文字列の `importlib`・`-m` 起動のどれでも当たる)。

**S3 (t012) が plan.sh を移すとき、このテストの `ALLOWED_CALLERS` を更新する**のが正しい手順
(=「呼び出し元が増えた」ことが差分に見える)。赤くなったら、意図した cutover かを先に確かめる。
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
NAME = "lib_state_store"

#: 名前を出してよいファイル (repo 相対)。S2 では lib 自身だけ (テストと knowledge/ は走査の対象外)。
ALLOWED_CALLERS = {"scripts/lib_state_store.py"}

#: 走査する場所。**ディレクトリごと**で列挙しない (新しい書き手が増えても自動で対象になる)。
SCAN_DIRS = ("scripts", "hooks", "agents", "config")
SCAN_FILES = ("crewvia",)


def _candidates():
    files = []
    for d in SCAN_DIRS:
        # 文書 (.md) は対象にしない: 入れ子の CLAUDE.md が lib を説明するのは呼び出しではない
        # (ガードを説明する文書が自分で引っかかる型。memory: red-proof-for-a-text-pattern-guard-trips-itself)
        files += [p for p in (REPO / d).rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".md"]
    files += [REPO / f for f in SCAN_FILES if (REPO / f).is_file()]
    return files


def _mentions(text: str) -> bool:
    return NAME in text


def test_no_production_code_references_the_store_yet():
    files = _candidates()
    scanned = 0
    offenders = []
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        scanned += 1
        rel = p.relative_to(REPO).as_posix()
        if _mentions(text) and rel not in ALLOWED_CALLERS:
            offenders.append(rel)
    # 空虚な PASS を防ぐ: 走査した件数を出す (scripts/ hooks/ の実ファイルが十分に入っている)
    assert scanned >= 60, f"走査したファイルが少なすぎる ({scanned} 件) — 検査が空になっていないか"
    assert offenders == [], f"lib_state_store を呼ぶコードが増えた (S3 以降の cutover か確認): {offenders}"


def test_the_allowed_caller_exists_and_the_detector_finds_each_way_of_calling_it():
    """陽性対照: 検出器が呼び方の全形を拾う (拾えないなら上のテストは何も守っていない)。"""
    assert (REPO / "scripts/lib_state_store.py").is_file()
    samples = [
        "import lib_state_store",
        "from lib_state_store import transaction",
        "import lib_state_store as store",
        "importlib.import_module('lib_state_store')",
        'python3 "$SCRIPT_DIR/lib_state_store.py" recover',
        "python3 -m lib_state_store",
        "sys.path.insert(0, scripts); __import__(\"lib_state_store\")",
    ]
    for s in samples:
        assert _mentions(s), s
    assert not _mentions("import lib_task_cards")


def test_documents_that_describe_the_lib_are_not_counted_as_callers():
    """scripts/CLAUDE.md は lib を説明する (名前を出す)。それで赤くならないこと — 走査から .md を外している。"""
    assert NAME in (REPO / "scripts" / "CLAUDE.md").read_text()
    assert all(p.suffix != ".md" for p in _candidates())


def test_fixture_copy_glob_does_not_count_as_a_caller():
    """`tests/fixture_tree.py` が lib_* を glob で写すのは呼び出しではない (名前を出さない)。"""
    text = (REPO / "tests" / "fixture_tree.py").read_text()
    assert NAME not in text
