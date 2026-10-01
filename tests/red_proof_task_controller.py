#!/usr/bin/env python3
"""tests/red_proof_task_controller.py — Controller の遷移の拒否・冪等・照合・書き込み順を外した変異で、狙ったテストが
赤になる実証 (01c E1 / t004)。

    python3 tests/red_proof_task_controller.py            # 全部
    python3 tests/red_proof_task_controller.py M01 M08    # 指定した変異だけ

1. リポジトリ (.git / .claude / queue / registry / logs を除く) を一時ディレクトリに写す。本番の状態には触れない
   (テストは自分で tmp の queue を作る)。
2. **変異なし**で単体テストが全部緑であることを確かめる (対照)。
3. 変異 (`lib_task_controller.py` の 1 か所) を写しに当て、**狙ったテスト名が FAILED** になることを確かめる。赤は
   「狙ったテスト名の FAILED」だけ: collection error・ImportError・SyntaxError は赤と数えない。
4. `PYTHONDONTWRITEBYTECODE=1` と `__pycache__` の掃除つき。置換元が**ちょうど 1 回**でなければ変異自体を失敗にする。
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
LIB = "scripts/lib_task_controller.py"
UNIT = "tests/test_task_controller_unit.py"
CRASH = "tests/test_task_controller_crash_injection.py"

# (id, 説明, 置換元, 置換先, テストファイル, 赤になるべきテスト名 (部分一致のどれか 1 つ以上が FAILED), -k)
MUTATIONS = [
    ("M01", "遷移の拒否を外す (§4.3 の狭めた表を見ない)",
     "    if command is not None and meta.get('status') not in _ACCEPTS[command]:\n",
     "    if False:\n", UNIT,
     ["test_complete_from_reserved_is_an_invalid_transition_and_writes_nothing",
      "test_done_is_refused_from_every_status_the_narrowed_table_drops"], None),
    ("M02", "冪等を外す (同じ terminal の再送を成功にしない)",
     "    if operation is not None and ex.IDEMPOTENT_OPERATION.get(end) == operation:\n",
     "    if False:\n", UNIT,
     ["test_10_5_10_resending_the_same_terminal_operation", "test_resend_after_a_retire_on_a_reserved_attempt"], None),
    ("M03", "違う結果の再送を成功にする (conflict を外す)",
     "    _refuse(txn, ex.EXECUTION_ALREADY_TERMINAL,\n"
     "            f\"{slug}/{tid}: その試行は別の結果で終了済みです\", slug, tid, meta, caller)\n",
     "    return IDEMPOTENT, ex.CHECK_VERIFIED\n", UNIT,
     ["test_10_5_11_a_different_terminal_result_is_a_conflict", "test_10_5_08_a_terminal_execution_never_moves"], None),
    ("M04", "名乗った ID の照合を外す (違う ID を通す)",
     "        if verdict == ex.MATCH:\n", "        if True:\n", UNIT,
     ["test_10_5_09_another_execution_id_cannot_complete_or_fail", "test_an_old_attempts_id_cannot_end_the_new_attempt"],
     None),
    ("M05", "形の不正な ID を NOT_FOUND にしない",
     "    if caller.presented and not ex.is_execution_id(caller.execution_id):\n", "    if False:\n", UNIT,
     ["test_a_malformed_id_is_not_found", "test_an_empty_presented_value_is_not_treated_as_absent"], None),
    ("M06", "attempt を足さない (常に 1)",
     "execution_count=int(meta.get('execution_count') or 0) + 1,", "execution_count=1,", UNIT,
     ["test_10_5_13_attempt_increases_by_one_per_reserve"], None),
    ("M07", "二重 reserve を拒否しない",
     "        if status in _HOLDING:\n            _refuse(txn, ex.TASK_ALREADY_RESERVED,",
     "        if False:\n            _refuse(txn, ex.TASK_ALREADY_RESERVED,", UNIT,
     ["test_10_5_02_duplicate_reserve_is_refused"], None),
    ("M08", "書き込み順: record を card より先に書く (card がコミット点でなくなる)",
     "    txn.write_card(slug, tid, meta, body)                       # コミット点\n    _write_record(txn, slug, tid, meta)\n    if agent is not None:",
     "    _write_record(txn, slug, tid, meta)\n    txn.write_card(slug, tid, meta, body)\n    if agent is not None:",
     CRASH, ["test_crash_at_every_point_converges"], "test_crash_at_every_point and reserve and not after_detached"),
]


def _copy_tree(dest: pathlib.Path) -> None:
    """リポジトリを丸ごと写す (.git・.claude・queue・registry・logs は除く。本番の状態は写さない)。"""
    ignore = shutil.ignore_patterns(".git", ".claude", "queue", "registry", "logs", "__pycache__", "*.pyc",
                                    "node_modules")
    shutil.copytree(REPO, dest, ignore=ignore, dirs_exist_ok=True)


def _purge_pyc(root: pathlib.Path) -> None:
    for p in root.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWVIA_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(root: pathlib.Path, test: str, k_expr: str | None):
    _purge_pyc(root)
    cmd = [sys.executable, "-m", "pytest", test, "-q", "-p", "no:cacheprovider", "-rfE"]
    if k_expr:
        cmd += ["-k", k_expr]
    r = subprocess.run(cmd, cwd=root, env=_env(), capture_output=True, text=True, timeout=900)
    out = r.stdout + r.stderr
    failed = set(re.findall(r"^FAILED \S+?::(\S+?)(?:\[| |$)", out, re.M))
    errors = re.findall(r"^ERROR .*$", out, re.M)
    return r.returncode, failed, errors, out


def main(argv) -> int:
    wanted = set(argv[1:])
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    results = []
    with tempfile.TemporaryDirectory(prefix="red-proof-task-controller-") as tmp:
        base = pathlib.Path(tmp) / "base"
        base.mkdir()
        _copy_tree(base)
        rc, failed, errors, out = _run(base, UNIT, None)
        if rc != 0 or failed or errors:
            print("対照 (変異なし) が緑でない。変異の実証に進めない:\n" + out[-3000:])
            return 1
        print(f"対照 (変異なし): 緑 ({re.search(r'(\d+) passed', out).group(1)} passed)")

        for mid, desc, old, new, test, targets, kexpr in selected:
            work = pathlib.Path(tmp) / mid
            shutil.copytree(base, work)
            lib = work / LIB
            src = lib.read_text()
            if src.count(old) != 1:
                results.append((mid, desc, "BROKEN", f"置換元が {src.count(old)} 回現れる (ちょうど 1 回でなければならない)"))
                continue
            lib.write_text(src.replace(old, new))
            k = kexpr or " or ".join(targets)
            rc, failed, errors, out = _run(work, test, k)
            hit = sorted(f for f in failed if any(t in f for t in targets))
            if errors or "SyntaxError" in out or "ImportError" in out:
                results.append((mid, desc, "BROKEN", "collection error / import error (変異が壊れている)"))
            elif hit:
                results.append((mid, desc, "RED", ", ".join(hit)))
            else:
                results.append((mid, desc, "GREEN", "狙ったテストが赤にならない (留め金になっていない)"))
            shutil.rmtree(work, ignore_errors=True)

    print()
    width = max(len(r[1]) for r in results) if results else 0
    for mid, desc, verdict, detail in results:
        print(f"{mid} {verdict:6} {desc}\n       -> {detail}")
    bad = [r for r in results if r[2] != "RED"]
    print(f"\n変異 {len(results)} 件: RED {len(results) - len(bad)} / それ以外 {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
