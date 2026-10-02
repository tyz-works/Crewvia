#!/usr/bin/env python3
"""tests/red_proof_e3_caller.py — done / fail / needs-director / ready-for-verification / verify-result の Controller 化
(01c E3 / t012) の欠陥を戻した変異で、狙ったテストが赤になる実証。

    python3 tests/red_proof_e3_caller.py            # 全部
    python3 tests/red_proof_e3_caller.py R01 R04    # 指定した変異だけ

`tests/red_proof_e2_pull.py` と同じ作法 (その道具を import して使う):

1. リポジトリを一時ディレクトリに写す (本番の queue / registry には触れない。`CREWVIA_*` の env は外して走らせる)
2. **変異なし**で対象のテストが全部緑であることを確かめる (対照)
3. 変異 (scripts/ の 1 か所) を写しに当て、**狙ったテスト名が FAILED** になることを確かめる。赤は「狙ったテスト名の
   assert の失敗」だけ。collection error・ImportError・SyntaxError は赤と数えない
4. `PYTHONDONTWRITEBYTECODE=1` と `__pycache__` の掃除つき。置換元が**ちょうど 1 回**でなければ変異自体を失敗 (BROKEN) にする

変異の対応 (受入条件の「赤の実証: 照合を外した変異で『他人の task を done できる』テストが赤」+ 必須条件):

- R01 照合を外す: active な試行に**別の ID を名乗っても**通る (他人の task を done できる)
- R02 `--execution ""` を名乗りなしに倒す (値の真偽で「指定されたか」を判定する。01b G3 の型)
- R03 空の env `CREWVIA_EXECUTION_ID=` を名乗りなしに倒す
- R04 狭めを戻す: done が pending / blocked から通る
- R05 冪等を外す: 同じ ID の同じ操作の再送が conflict になる
- R06 `verify-result fail` が worker を手放さない (新しい試行にならない)
- R07 照合・遷移の検査 (dry_run) を派生値の書き込みの後に回す: 拒否された done が依存先に pr_number を書く
- R08 拒否の行を残さない (監査ログの `refused:`)
- R09 監査の actor を、AGENT_NAME があっても card の worker に置き換える
- R10 verifier-dispatcher が `verifying` に `--execution` を渡さない
- R11 `update --close-execution` が active な試行 (持ち主がいる) も閉じる
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
CTL = "scripts/lib_task_controller.py"
STATUS = "scripts/lib_task_status.py"
STORE = "scripts/lib_state_store.py"
VERIFIER = "scripts/verifier-dispatcher.sh"
TABLE = "tests/test_execution_e3_caller_table.py"
VTEST = "tests/test_verifier_dispatcher_names_the_attempt.py"

# (id, 説明, ファイル, 置換元, 置換先, 赤になるべきテスト名 (部分一致のどれか 1 つ以上が FAILED), テストファイル)
MUTATIONS = [
    ("R01", "照合を外す (active な試行に別の ID を名乗っても通る)", CTL,
     "    verdict, check = ex.execution_matches(view, meta, execution_id=caller.execution_id)\n"
     "    if caller.presented:\n"
     "        if verdict == ex.MATCH:\n",
     "    verdict, check = ex.execution_matches(view, meta, execution_id=caller.execution_id)\n"
     "    if caller.presented:\n"
     "        verdict = ex.MATCH if view == ex.ACTIVE else verdict\n"
     "        if verdict == ex.MATCH:\n",
     ["test_row_active_and_a_wrong_id_is_refused_with_exit_3_and_nothing_is_written",
      "test_a_stale_id_after_a_reset_and_a_new_attempt_is_not_current",
      "test_two_tasks_do_not_confuse_each_others_ids",
      "test_verify_result_with_a_wrong_id_is_refused"], TABLE),
    ("R02", "--execution \"\" を名乗りなしに倒す (値の真偽で判定)", PLAN,
     "    if '--execution' in opts:\n",
     "    if opts.get('--execution'):\n",
     ["test_an_empty_explicit_claim_is_refused_not_turned_into_no_claim"], TABLE),
    ("R03", "空の env CREWVIA_EXECUTION_ID を名乗りなしに倒す", PLAN,
     "    value = os.environ.get('CREWVIA_EXECUTION_ID')\n    if value is not None:\n",
     "    value = os.environ.get('CREWVIA_EXECUTION_ID')\n    if value:\n",
     ["test_an_empty_explicit_claim_is_refused_not_turned_into_no_claim"], TABLE),
    ("R04", "狭めを戻す (done が pending / blocked から通る)", STATUS,
     "    'done': frozenset({'in_progress'}),\n",
     "    'done': frozenset({'in_progress', 'pending', 'blocked'}),\n",
     ["test_a_narrowed_transition_is_refused_with_exit_2_and_nothing_is_written"], TABLE),
    ("R05", "冪等を外す (同じ ID の同じ操作の再送が conflict になる)", CTL,
     "    if operation is not None and ex.IDEMPOTENT_OPERATION.get(end) == operation:\n",
     "    if False:\n",
     ["test_a_resend_with_the_same_id_succeeds_and_writes_nothing",
      "test_verify_pass_by_a_verifier_who_is_not_the_owner_completes_the_attempt"], TABLE),
    ("R06", "verify-result fail が worker を手放さない (新しい試行にならない)", CTL,
     "    ex.VERIFICATION_REJECTED: (frozenset({_S_PENDING}), _C_VERIFY_RESULT, _OP_VERIFY_FAIL, True, True),\n",
     "    ex.VERIFICATION_REJECTED: (frozenset({_S_PENDING}), _C_VERIFY_RESULT, _OP_VERIFY_FAIL, False, True),\n",
     ["test_verify_fail_below_the_limit_starts_a_new_attempt"], TABLE),
    ("R07", "照合・遷移の検査を派生値の書き込みの後に回す (拒否された done が pr_number を書く)", PLAN,
     "            decision = _CONTROLLER.complete_execution(_txn(), slug, task_id, caller, to_status='done', dry_run=True)\n",
     "            decision = _CONTROLLER.PROCEED\n",
     ["test_a_wrong_claim_is_refused_before_the_derived_writes_of_done"], TABLE),
    ("R08", "拒否の行を残さない", CTL,
     "    txn.refuse(code, slug, tid, meta.get('status'),\n"
     "               execution_id=meta.get('current_execution_id'), presented=presented)\n",
     "",
     ["test_row_active_and_a_wrong_id_is_refused_with_exit_3_and_nothing_is_written",
      "test_a_narrowed_transition_is_refused_with_exit_2_and_nothing_is_written"], TABLE),
    ("R09", "監査の actor を AGENT_NAME があっても card の worker に置き換える", STORE,
     "    if safe and safe != 'unknown':\n",
     "    if False:\n",
     ["test_the_actor_is_not_replaced_when_an_agent_name_exists"], TABLE),
    ("R10", "verifier-dispatcher が verifying に --execution を渡さない", VERIFIER,
     "'--mission', slug, *claim],",
     "'--mission', slug],",
     ["test_verifying_passes_the_execution_flag_only_when_there_is_an_id"], VTEST),
    ("R11", "update --close-execution が active な試行も閉じる", CTL,
     "    if _view(meta) != ex.DETACHED or meta.get('execution_status') not in ex.ACTIVE_STATUSES:\n"
     "        _refuse(txn, ex.INVALID_TRANSITION, f\"{slug}/{tid}: 閉じるべき DETACHED の試行がありません\", slug, tid, meta)\n",
     "    if meta.get('execution_status') not in ex.ACTIVE_STATUSES:\n"
     "        _refuse(txn, ex.INVALID_TRANSITION, f\"{slug}/{tid}: 閉じるべき DETACHED の試行がありません\", slug, tid, meta)\n",
     ["test_close_execution_does_not_close_a_live_attempt_or_combine_with_other_options"], TABLE),
]
CONTROL_TESTS = (TABLE, VTEST)


def main(argv) -> int:
    wanted = set(argv[1:])
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    results = []
    with tempfile.TemporaryDirectory(prefix="red-proof-e3-caller-") as tmp:
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
