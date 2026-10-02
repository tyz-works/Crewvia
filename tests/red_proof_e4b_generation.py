#!/usr/bin/env python3
"""tests/red_proof_e4b_generation.py — 世代 (`started_at`) の照合と旧形式 marker の読み口を外した (01c E4b / t025) ことの、
戻した変異・外しすぎた変異で狙ったテストが赤になる実証。

    python3 tests/red_proof_e4b_generation.py            # 全部
    python3 tests/red_proof_e4b_generation.py B01 B04    # 指定した変異だけ

`tests/red_proof_e4a_retire.py` と同じ作法 (`red_proof_e2_pull` の道具を使う):

1. リポジトリを一時ディレクトリに写す (本番の queue / registry には触れない。`CREWVIA_*` の env は外して走らせる)
2. **変異なし**で対象のテストが全部緑であることを確かめる (対照)
3. 変異 (scripts/ の 1 か所) を写しに当て、**狙ったテスト名が FAILED** になることを確かめる。赤は「狙ったテスト名の
   assert の失敗」だけ。collection error・ImportError・SyntaxError・KeyError 等の例外は赤と数えない
4. `PYTHONDONTWRITEBYTECODE=1` と `__pycache__` の掃除つき。置換元が**ちょうど 1 回**でなければ変異自体を失敗 (BROKEN) にする

変異の対応:

世代の照合に**戻した**変異
- B01 retire が `--started-at` を受け付ける (世代での名指しが戻る)
- B02 identity の比較が ID の無い identity を一致にする (世代・無証拠で枠を「この試行のもの」と言う)
- B03 assignment の verdict が ID の無い identity を「読めない」にしない (保留が外れ、後任のものかもしれない枠を殺す)
- B04 marker の壊れた `task_execution_id` をそのまま束縛に使う (保留に倒さず `plan.sh retire` の引数にする)
- B06 旧形式 marker の `task_started_at` を後始末の証拠に戻す
照合を**外しすぎた**変異
- B05 終わった (TERMINAL) 試行を束縛しない (束縛しないと「ID を記録できていない」保留 = Director への通知になる)
- B07 枠の identity の ID が違っても後任と見ない (別の試行の枠を撤去する)
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
EXE = "scripts/lib_execution.py"
STORE = "scripts/lib_state_store.py"
E4A = "tests/test_execution_e4a_retire_reset.py"
GUARD = "tests/test_execution_e4b_no_generation_readers.py"
STORE_T = "tests/test_state_store_transaction.py"
UNIT = "tests/test_task_controller_unit.py"

MUTATIONS = [
    ("B01", "retire が --started-at を受け付ける (世代での名指しが戻る)", PLAN,
     "        '--execution': 'value',\n        '--outcome': 'value',\n        '--reason': 'value',\n        '--no-wait': 'bool',\n    })\n\n"
     "    if not positional:\n        die(\"retire requires a task_id",
     "        '--execution': 'value',\n        '--started-at': 'value',\n        '--outcome': 'value',\n        '--reason': 'value',\n"
     "        '--no-wait': 'bool',\n    })\n\n    if not positional:\n        die(\"retire requires a task_id",
     ["test_retire_by_generation_is_gone_and_writes_nothing",
      "test_the_current_id_retires_even_when_the_identity_carries_no_id",
      "test_no_generation_reader_is_back_in_the_code"], [E4A, GUARD]),
    ("B02", "identity_matches が ID の無い identity を一致にする", EXE,
     "    return recorded_id is not None and execution_id is not None and recorded_id == execution_id\n",
     "    if recorded_id is None:\n        return True\n    return execution_id is not None and recorded_id == execution_id\n",
     ["test_identity_matches_compares_the_execution_id_only",
      "test_an_identity_without_an_id_is_not_this_attempts_slot_and_is_never_removed",
      "test_the_current_id_retires_even_when_the_identity_carries_no_id"], [UNIT, STORE_T, E4A]),
    ("B03", "verdict が ID の無い identity を「読めない」にしない", RET,
     "    if recorded is None:\n        # `plan.sh pull` always writes the sidecar",
     "    if recorded is None and False:\n        # `plan.sh pull` always writes the sidecar",
     ["test_assignment_verdict_compares_the_execution_id_only"], [E4A]),
    ("B04", "marker の壊れた task_execution_id をそのまま束縛に使う", RET,
     "            return value if _ex.is_execution_id(value) else None\n",
     "            return value\n",
     ["test_a_marker_with_a_malformed_execution_id_is_held_not_read_as_a_generation",
      "test_a_dead_worker_with_a_malformed_execution_id_is_deferred_to_the_director_and_the_queue_is_untouched"], [E4A]),
    ("B05", "終わった (TERMINAL) 試行を束縛しない", RET,
     "    return meta[\"current_execution_id\"] if view in (_ex.ACTIVE, _ex.TERMINAL) else None\n",
     "    return meta[\"current_execution_id\"] if view == _ex.ACTIVE else None\n",
     ["test_a_terminal_attempt_is_still_bound_so_plan_retire_can_answer_nothing_is_owed"], [E4A]),
    ("B06", "旧形式 marker の task_started_at を後始末の証拠に戻す", RET,
     "            execution_id = self._bound_execution(req, prog)\n            if execution_id is None:\n",
     "            execution_id = self._bound_execution(req, prog) or (req or {}).get(\"task_started_at\") "
     "or prog.get(\"task_started_at\")\n            if execution_id is None:\n",
     ["test_a_dead_worker_with_a_malformed_execution_id_is_deferred_to_the_director_and_the_queue_is_untouched"], [E4A]),
    ("B07", "枠の identity の ID が違っても後任と見ない", STORE,
     "        if not _ex.identity_matches(identity, execution_id=execution_id):\n            return ASSIGN_SUCCESSOR\n",
     "        if False:\n            return ASSIGN_SUCCESSOR\n",
     ["test_publish_writes_identity_before_body_and_retire_uses_classify",
      "test_an_identity_without_an_id_is_not_this_attempts_slot_and_is_never_removed",
      "test_a_stale_attempt_cannot_retire_its_successor"], [STORE_T, E4A]),
]
CONTROL_TESTS = (E4A, GUARD, STORE_T, UNIT, "tests/test_execution_e4b_legacy_holding_card.py")


def main(argv) -> int:
    wanted = set(argv[1:])
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    results = []
    with tempfile.TemporaryDirectory(prefix="red-proof-e4b-generation-") as tmp:
        root = pathlib.Path(tmp) / "base"
        root.mkdir()
        base._copy_tree(root)
        rc, failed, errors, out = base._run(root, None, CONTROL_TESTS)
        if rc != 0 or failed or errors:
            print("対照 (変異なし) が緑でない。変異の実証に進めない:\n" + out[-3000:])
            return 1
        print(f"対照 (変異なし): 緑 ({re.search(r'(\d+) passed', out).group(1)} passed)")

        for mid, desc, rel, old, new, targets, test_files in selected:
            work = pathlib.Path(tmp) / mid
            shutil.copytree(root, work)
            f = work / rel
            src = f.read_text()
            if src.count(old) != 1:
                results.append((mid, desc, "BROKEN", f"置換元が {src.count(old)} 回現れる (ちょうど 1 回でなければならない)"))
                shutil.rmtree(work, ignore_errors=True)
                continue
            f.write_text(src.replace(old, new))
            rc, failed, errors, out = base._run(work, " or ".join(targets), tuple(test_files))
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
