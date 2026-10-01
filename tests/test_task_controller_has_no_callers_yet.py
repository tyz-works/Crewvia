"""`lib_task_controller` の呼び出し元の集合を固定する (vNext 01c E1。01a S2 / 01b G2 と同じ作法)。

**E2 (t008) で最初の呼び出し元が入った: `scripts/plan.sh` の `pull` だけ** (reserve / start / G1 の fail)。
ファイル名の `_has_no_callers_yet` は E1 のときの名前で、今は「許可した呼び出し元だけ」を固定する。

R2 (ユーザー決定): 呼び出し側を移す PR は merge 前にユーザー承認。Controller を plan.sh / dispatcher / hooks が
import した瞬間が cutover (E2 以降)。E1 は本番の挙動を変えない lib だけなので、名前を出してよいのは
lib 自身と、コメントで名前を挙げるだけの 2 ファイル。

E2 で最初の呼び出し元 (plan.sh の pull) を足すときは `ALLOWED_MENTIONS` を意図して広げる (それが cutover の印)。
検査は import 文の形ではなく**名前の出現**で固定する (bash 経由・文字列の importlib・`-m` のどれでも当たる)。
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
NAME = "lib_task_controller"

#: 名前を出してよいファイル。`lib_execution.py` / `lib_task_status.py` はコメントで説明するだけ (import しない。
#: 下の `test_the_mentions_in_the_allowed_files_are_not_imports` が固定する)。
ALLOWED_MENTIONS = {"scripts/lib_task_controller.py", "scripts/lib_execution.py", "scripts/lib_task_status.py",
                    # E2 (t008): 最初の呼び出し元 = pull (reserve / start / G1 の fail)。cutover = ユーザー承認の PR。
                    # E3 (done 等) / E4 (reset / retire) は plan.sh の別のコマンドが増えるだけで、このファイルの集合は変わらない。
                    "scripts/plan.sh"}

SCAN_DIRS = ("scripts", "hooks", "agents", "config")
SCAN_FILES = ("crewvia",)


def _candidates():
    files = []
    for d in SCAN_DIRS:
        files += [p for p in (REPO / d).rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".md"]
    files += [REPO / f for f in SCAN_FILES if (REPO / f).is_file()]
    return files


def _mentions(text: str) -> bool:
    return NAME in text


def test_only_the_allowed_files_mention_the_controller():
    scanned, offenders = 0, []
    for p in _candidates():
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        scanned += 1
        rel = p.relative_to(REPO).as_posix()
        if _mentions(text) and rel not in ALLOWED_MENTIONS:
            offenders.append(rel)
    assert scanned >= 60, f"走査したファイルが少なすぎる ({scanned} 件)"
    assert offenders == [], f"lib_task_controller を呼ぶコードが増えた (cutover か確認。ユーザー承認が要る): {offenders}"


def test_the_allowed_files_exist_and_the_detector_finds_each_way_of_calling_it():
    for rel in ALLOWED_MENTIONS:
        assert (REPO / rel).is_file(), rel
    for s in ("import lib_task_controller", "from lib_task_controller import reserve_execution",
              "importlib.import_module('lib_task_controller')", 'python3 "$D/lib_task_controller.py"',
              "python3 -m lib_task_controller", '__import__("lib_task_controller")'):
        assert _mentions(s), s
    assert not _mentions("import lib_execution")


def test_the_mentions_in_the_allowed_files_are_not_imports():
    """許可した 2 ファイルはコメント・docstring で名前を挙げるだけ。import の形に変わったら呼び出し元になっている。"""
    form = re.compile(r"(?:^\s*(?:import|from)\s+lib_task_controller\b|import_module\(|__import__\(|-m\s+lib_task_controller)")
    for rel in ("scripts/lib_execution.py", "scripts/lib_task_status.py"):
        for line in (REPO / rel).read_text().splitlines():
            if NAME in line:
                assert not form.search(line), (rel, line)


#: plan.sh が Controller から呼んでよい操作 (E2 = pull だけ。E3 で done / fail / needs-director / ready-for-verification /
#: verify-result、E4 で reset / retire が足される。足すのは cutover = ユーザー承認の PR)。
PLAN_SH_ALLOWED_OPERATIONS = {"reserve_task", "start_execution", "fail_execution", "Caller"}


def test_plan_sh_calls_only_the_pull_operations_of_the_controller():
    text = (REPO / "scripts" / "plan.sh").read_text()
    used = set(re.findall(r"_CONTROLLER\.(\w+)", text))
    assert used == PLAN_SH_ALLOWED_OPERATIONS, (
        f"plan.sh が呼ぶ Controller の操作が変わった (cutover か確認。ユーザー承認が要る): {sorted(used)}")
    # 他の module (dispatcher / hooks / デーモン) は import しない (上の 1 本目が名前の出現で固定)
