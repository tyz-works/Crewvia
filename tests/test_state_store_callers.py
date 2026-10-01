"""`lib_state_store` の呼び出し元の一覧を固定する (旧 `test_state_store_has_no_callers_yet.py`)。

R2 (ユーザー決定): 呼び出し側を移す PR は merge 前にユーザー承認。plan.sh は主 checkout から直接
実行されるので、plan.sh が import した瞬間が cutover になる。

- S2 (t008): 呼び出し元ゼロ。許可は lib 自身だけだった。
- **S3 (t012): plan.sh が最初の (そして S3 時点で唯一の) 呼び出し元**。dispatcher・hooks・verifier-dispatcher・
  taskvia-sync・watchdog 等はまだ import しない。
- **S5 (t020): `lib_registry.py` (workers.yaml の原子的書き込み) と `taskvia-sync.sh` (map の `locked_update_json`) が加わる**。
  verifier-dispatcher と hooks/pre-compact.sh は lib を import せず、**plan.sh の subcommand (`verifying` / `snapshot`)
  を呼ぶ** (書き手を増やさない。queue の書き込みは plan.sh の `with_lock` の中だけ)。

呼び出し元が増えたら `ALLOWED_CALLERS` の差分にそれが見える。赤くなったら、意図した cutover かを先に確かめる。
検査は import 文の形ではなく**名前の出現**で固定する (bash の `sys.path` 経由・文字列の `importlib`・
`-m` 起動のどれでも当たる)。
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
NAME = "lib_state_store"

#: 名前を出してよいファイル (repo 相対)。テストと knowledge/ は走査の対象外。
#: S3: plan.sh (queue の書き手)。S5: lib_registry.py (registry/workers.yaml)・taskvia-sync.sh (queue/.taskvia-map.json)。
#: 01c E1: lib_task_controller.py (試行の予約・終了。`Txn` を受けて書く lib で、それ自身の呼び出し元はゼロ —
#: `tests/test_task_controller_has_no_callers_yet.py` が固定する。本番の queue を書く経路はまだ増えていない)。
#: ここに足すのは cutover (= ユーザー承認が要る PR) だけ。
ALLOWED_CALLERS = {"scripts/lib_state_store.py", "scripts/plan.sh", "scripts/lib_registry.py",
                   "scripts/taskvia-sync.sh", "scripts/lib_task_controller.py"}

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


def test_only_the_allowed_callers_reference_the_store():
    files = _candidates()
    scanned = 0
    offenders = []
    seen_allowed = set()
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        scanned += 1
        rel = p.relative_to(REPO).as_posix()
        if _mentions(text):
            if rel in ALLOWED_CALLERS:
                seen_allowed.add(rel)
            else:
                offenders.append(rel)
    # 空虚な PASS を防ぐ: 走査した件数を出す (scripts/ hooks/ の実ファイルが十分に入っている)
    assert scanned >= 60, f"走査したファイルが少なすぎる ({scanned} 件) — 検査が空になっていないか"
    assert offenders == [], f"lib_state_store を呼ぶコードが増えた (cutover か確認。ユーザー承認が要る): {offenders}"
    # 許可表の行が死んでいない (plan.sh が使わなくなったのに表に残る = cutover が戻された、を見逃さない)
    assert seen_allowed == ALLOWED_CALLERS, f"許可表にあるのに名前が出ないファイル: {ALLOWED_CALLERS - seen_allowed}"


def test_the_allowed_callers_exist_and_the_detector_finds_each_way_of_calling_it():
    """陽性対照: 検出器が呼び方の全形を拾う (拾えないなら上のテストは何も守っていない)。"""
    for rel in ALLOWED_CALLERS:
        assert (REPO / rel).is_file(), rel
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


def test_plan_sh_imports_the_store_the_normal_way():
    """plan.sh は `_load_scripts_module` (sys.modules に載せない) ではなく普通の import で読む。
    lib は dataclass を持つので、sys.modules に無いと import 時に落ちる。"""
    text = (REPO / "scripts" / "plan.sh").read_text()
    assert "_import_scripts_module('lib_state_store')" in text
    assert "_load_scripts_module('lib_state_store')" not in text


def test_documents_that_describe_the_lib_are_not_counted_as_callers():
    """scripts/CLAUDE.md は lib を説明する (名前を出す)。それで赤くならないこと — 走査から .md を外している。"""
    assert NAME in (REPO / "scripts" / "CLAUDE.md").read_text()
    assert all(p.suffix != ".md" for p in _candidates())


def test_fixture_copy_glob_does_not_count_as_a_caller():
    """`tests/fixture_tree.py` が lib_* を glob で写すのは呼び出しではない (名前を出さない)。"""
    text = (REPO / "tests" / "fixture_tree.py").read_text()
    assert NAME not in text
