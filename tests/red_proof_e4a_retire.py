#!/usr/bin/env python3
"""tests/red_proof_e4a_retire.py — `update --reset` / `plan.sh retire` / 退役 marker の Execution ID 化
(01c E4a / t016) の欠陥を戻した変異で、狙ったテストが赤になる実証。

    python3 tests/red_proof_e4a_retire.py            # 全部
    python3 tests/red_proof_e4a_retire.py R01 R04    # 指定した変異だけ

`tests/red_proof_e3_caller.py` と同じ作法 (`red_proof_e2_pull` の道具を使う):

1. リポジトリを一時ディレクトリに写す (本番の queue / registry には触れない。`CREWVIA_*` の env は外して走らせる)
2. **変異なし**で対象のテストが全部緑であることを確かめる (対照)
3. 変異 (scripts/ の 1 か所) を写しに当て、**狙ったテスト名が FAILED** になることを確かめる。赤は「狙ったテスト名の
   assert の失敗」だけ。collection error・ImportError・SyntaxError は赤と数えない
4. `PYTHONDONTWRITEBYTECODE=1` と `__pycache__` の掃除つき。置換元が**ちょうど 1 回**でなければ変異自体を失敗 (BROKEN) にする

変異の対応 (受入条件の「赤の実証: 照合を世代に戻した変異・照合を外した変異で『新しい試行を退役させる』テストが赤」+ 必須条件):

- R01 照合を世代に戻す: retire が `--execution` を無視し、card の今の世代で照合する (新しい試行を退役させる)
- R02 照合を外す: retire が試行の照合の結果を見ない
- R03 `--execution ""` を「指定なし」に倒す (値の真偽で判定。世代だけの経路に落ちる。01b G3 の型)
- R04 marker に ID を書かない (producer を戻す。後始末が世代の経路のまま)
- R05 assignment の照合が ID を見ない (世代が同じ後任に SIGTERM が届く)
- R06 `update --reset` が試行を閉じない
- R07 retire が別の終了コードで試行を閉じる (RETIRED でなく RESET_BY_DIRECTOR)
- R08 retire が同じ ID の再送を成功にしない
- R09 watchdog が `AGENT_NAME` を入れない (監査の actor が unknown)
- R10 dispatcher の reap が `AGENT_NAME` を入れない
- R11 retire が試行の欄が壊れた card も通す (止まらない)
- R12 DETACHED の card の古い ID を marker に束縛する (旧コードの持ち主の退役を ID で縛って保留にする)
"""

from __future__ import annotations

import pathlib
import re
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import red_proof_e2_pull as base  # noqa: E402

PLAN = "scripts/plan.sh"
RET = "scripts/lib_retirement.py"
DISP = "scripts/dispatcher.sh"
TEST = "tests/test_execution_e4a_retire_reset.py"

MUTATIONS = [
    ("R01", "照合を世代に戻す (--execution を無視して card の今の世代で照合 = 新しい試行を退役させる)", PLAN,
     "matched, _check = _EXEC.execution_matches(view, meta, execution_id=execution_id, started_at=generation)\n",
     "matched, _check = _EXEC.execution_matches(view, meta, execution_id=None,\n"
     "                                                  started_at=generation if generation is not None\n"
     "                                                  else str(meta.get('started_at')))\n",
     ["test_the_execution_id_decides_even_when_the_generation_is_identical",
      "test_a_stale_attempt_cannot_retire_its_successor"], TEST),
    ("R02", "照合を外す (試行が違っても通す)", PLAN,
     "        if matched != _EXEC.MATCH:\n            if execution_id is not None:",
     "        if False:\n            if execution_id is not None:",
     ["test_a_wrong_execution_is_refused_with_exit_3_and_nothing_is_written",
      "test_a_stale_attempt_cannot_retire_its_successor",
      "test_the_execution_id_decides_even_when_the_generation_is_identical"], TEST),
    ("R03", "--execution \"\" を指定なしに倒す (世代だけの経路に落ちる)", PLAN,
     "    if '--execution' in opts:\n        execution_id = opts['--execution'].strip()",
     "    if opts.get('--execution'):\n        execution_id = opts['--execution'].strip()",
     ["test_an_empty_or_missing_claim_is_refused_with_exit_1_and_nothing_is_written"], TEST),
    ("R04", "marker に ID を書かない (producer を戻す)", RET,
     "    if task_execution_id is not UNKNOWN_EXECUTION_ID:\n        req[\"task_execution_id\"] = task_execution_id\n",
     "",
     ["test_the_marker_and_the_cleanup_are_bound_to_the_execution",
      "test_the_progress_file_carries_the_execution_forward_after_the_request_is_gone"], TEST),
    ("R05", "assignment の照合が ID を見ない (世代が同じ後任に SIGTERM が届く)", RET,
     "    if execution_id is not None and (identity or {}).get(\"execution_id\") is not None:\n",
     "    if False:\n",
     ["test_a_successor_with_the_same_generation_is_not_signalled_at_the_guard",
      "test_assignment_verdict_prefers_the_execution_id_over_the_generation"], TEST),
    ("R06", "update --reset が試行を閉じない", PLAN,
     "                    _CONTROLLER.reset_task(_txn(), slug, task_id, _CONTROLLER.NO_CALLER,\n"
     "                                           meta_updates={'completed_at': None})\n",
     "                    pass\n",
     ["test_reset_fails_a_running_attempt_as_reset_by_director",
      "test_the_attempt_after_a_reset_is_a_new_one_and_the_old_id_is_stale"], TEST),
    ("R07", "retire が別の終了コードで試行を閉じる (RETIRED でなく RESET_BY_DIRECTOR)", PLAN,
     "                _CONTROLLER.fail_execution(_txn(), slug, task_id, caller, _EXEC.RETIRED,\n",
     "                _CONTROLLER.fail_execution(_txn(), slug, task_id, caller, _EXEC.RESET_BY_DIRECTOR,\n",
     ["test_retire_by_execution_closes_a_running_attempt_and_resets_the_task"], TEST),
    ("R08", "retire が同じ ID の再送を成功にしない", PLAN,
     "        if (execution_id is not None and view == _EXEC.TERMINAL\n",
     "        if (False and view == _EXEC.TERMINAL\n",
     ["test_a_resend_of_a_retire_that_already_closed_the_attempt_is_a_success_and_writes_nothing",
      "test_a_resend_of_the_cleanup_after_the_card_was_written_still_settles_the_marker"], TEST),
    ("R09", "watchdog が AGENT_NAME を入れない (監査の actor が unknown)", RET,
     "            env[\"AGENT_NAME\"] = \"watchdog\"\n",
     "            pass\n",
     ["test_the_marker_and_the_cleanup_are_bound_to_the_execution"], TEST),
    ("R10", "dispatcher の reap が AGENT_NAME を入れない", DISP,
     "    env['AGENT_NAME'] = 'dispatcher'\n",
     "    pass\n",
     ["test_dispatcher_sets_the_actor_for_the_reap"], "tests/test_execution_e4a_daemon_env.py"),
    ("R11", "retire が試行の欄が壊れた card も通す", PLAN,
     "        if fields_problem:\n            die(f\"{prefix}試行の欄が壊れています",
     "        if False:\n            die(f\"{prefix}試行の欄が壊れています",
     ["test_a_card_with_broken_execution_fields_is_not_retired"], TEST),
    ("R12", "DETACHED の card の古い ID を marker に束縛する", RET,
     "    return meta[\"current_execution_id\"] if view == _ex.ACTIVE else None\n",
     "    return meta[\"current_execution_id\"] if view in (_ex.ACTIVE, _ex.DETACHED) else None\n",
     ["test_the_marker_reader_binds_only_an_active_attempt"], TEST),
]
CONTROL_TESTS = (TEST, "tests/test_execution_e4a_daemon_env.py")


def main(argv) -> int:
    wanted = set(argv[1:])
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    results = []
    with tempfile.TemporaryDirectory(prefix="red-proof-e4a-retire-") as tmp:
        root = pathlib.Path(tmp) / "base"
        root.mkdir()
        base._copy_tree(root)
        rc, failed, errors, out = base._run(root, None, CONTROL_TESTS)
        if rc != 0 or failed or errors:
            print("対照 (変異なし) が緑でない。変異の実証に進めない:\n" + out[-3000:])
            return 1
        print(f"対照 (変異なし): 緑 ({re.search(r'(\d+) passed', out).group(1)} passed)")

        for mid, desc, rel, old, new, targets, test_file in selected:
            work = pathlib.Path(tmp) / mid
            shutil.copytree(root, work)
            f = work / rel
            src = f.read_text()
            if src.count(old) != 1:
                results.append((mid, desc, "BROKEN", f"置換元が {src.count(old)} 回現れる (ちょうど 1 回でなければならない)"))
                shutil.rmtree(work, ignore_errors=True)
                continue
            f.write_text(src.replace(old, new))
            rc, failed, errors, out = base._run(work, " or ".join(targets), (test_file,))
            hit = sorted(n for n in failed if any(t in n for t in targets))
            if errors or "SyntaxError" in out or "ImportError" in out or "collected 0 items" in out:
                results.append((mid, desc, "BROKEN", "collection error / import error (変異が壊れている)"))
            elif hit:
                asserts = [n for n in hit if failed[n].startswith(("AssertionError", "assert ", "Failed: "))]
                reasons = "; ".join(f"{n}: {failed[n][:110]}" for n in hit)
                results.append((mid, desc, "RED" if asserts else "EXC", reasons))
            else:
                results.append((mid, desc, "GREEN", "狙ったテストが赤にならない (留め金になっていない)"))
            shutil.rmtree(work, ignore_errors=True)

    print()
    for mid, desc, verdict, detail in results:
        print(f"{mid} {verdict:6} {desc}\n       -> {detail}")
    bad = [r for r in results if r[2] != "RED"]
    print(f"\n変異 {len(results)} 件: RED {len(results) - len(bad)} / それ以外 {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
