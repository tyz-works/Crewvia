#!/usr/bin/env python3
"""red proof: §11 (t003) の修正を 1 つずつ戻すと tests/test_hard_idle_suppression_unknown_zombie.py が赤になる。

欠陥を注入した版に**同じテスト**を走らせる。ファイルは in-place で書き換えて必ず元に戻す
(finally)。置換元がちょうど 1 回でなければ BROKEN (注入点が消えている = 失効)。
赤と数えるのは `FAILED <テスト名>` が出たときだけ (collection error は数えない)。
実行: env -u AGENT_NAME python3 tests/red_proof_hard_idle_unknown.py   (約 2 分)
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts" / "lib_pane_process.py"
WD = ROOT / "scripts" / "watchdog.py"
TEST = "tests/test_hard_idle_suppression_unknown_zombie.py"
TESTS = [TEST, "tests/test_suppression_notifier_model.py"]

MUTATIONS = [
    ("zombie を木から外さない", LIB, '        if _proc_state(pid) == "Z":', '        if False:',
     ["test_a_zombie_child_does_not_make_the_tree_unknown",
      "test_watchdog_terminates_hard_idle_for_a_zombie_only_tree"]),
    ("zombie の子をキューに積まない (t013)", LIB,
     '            for child in children.get(pid, []):\n                queue.append((child, "zombie"))\n            continue',
     '            continue',
     ["test_a_wrapper_that_turns_zombie_after_the_snapshot_keeps_its_job"]),
    ("zombie の子に None を渡す (t013)", LIB, 'queue.append((child, "zombie"))', 'queue.append((child, None))',
     ["test_the_children_of_a_zombie_are_not_taken_for_a_session_body"]),
    ("通知文から mission を外す (t013)", WD, "(mission {self.mission_slug or '?'} ", "(",
     ["test_the_suppression_message_names_the_mission_and_separates_reason_from_evidence"]),
    ("読めないノードで即 unknown (旧実装)", LIB,
     '            saw_unknown = True\n            for child in children.get(pid, []):\n'
     '                queue.append((child, "unknown"))\n            continue',
     '            return "unknown"',
     ["test_a_job_wins_over_an_unreadable_node_in_either_order"]),
    ("通知がしきい値で出ない", WD, '    if lasted < threshold:\n        return SuppressionPlan(forget=mine)',
     '    if True:\n        return SuppressionPlan(forget=mine)', ["test_a_suppressed_hard_idle_notifies_the_director_once_after_the_threshold"]),
    ("fp が Execution ID を見ない", WD, '        if self.execution_id:\n            ident = self.execution_id',
     '        if False:\n            ident = self.execution_id', ["test_the_fingerprint_is_the_execution_id_with_a_started_at_fallback"]),
    ("解けても台帳キーを消さない", WD,
     '            self._unsettled.add(monitor.agent_name)\n        self._execute(plan, None)',
     '            self._unsettled.add(monitor.agent_name)\n        self._execute(SuppressionPlan(), None)',
     ["test_recovery_clears_the_ledger_key_and_a_relapse_notifies_again"]),
    # --- 3 巡の指摘 (t009 / t011 / t016) を戻すと網羅テストが落ちる (構造に切り替えた証明) ---
    ("【1 巡目 t009】回復の判定をプロセス内の状態に戻す", WD,
     '        self._state.pop((monitor.agent_name, monitor.task_id), None)\n        plan = plan_suppression(self._read(), monitor.agent_name, None, "", 0, 0)',
     '        if self._state.pop((monitor.agent_name, monitor.task_id), None) is None:\n            return\n        plan = plan_suppression(self._read(), monitor.agent_name, None, "", 0, 0)',
     ["test_recovery_across_a_watchdog_restart_still_clears_the_ledger",
      ("test_every_sequence_up_to_length_5_keeps_the_invariants", "I3")]),
    ("【2 巡目 t011】サイクルが掃除をやり直さない", WD,
     '        for agent in sorted(orphans):\n            self._execute(plan_suppression(told, agent, None, "", 0, 0), None)',
     '        for agent in sorted(orphans):\n            pass',
     ["test_a_failed_ledger_deletion_is_retried_from_the_ledger_with_or_without_a_forget",
      ("test_every_sequence_up_to_length_5_keeps_the_invariants", "I3")]),
    ("【4 巡目 t016】台帳キーが理由ごとでなく Worker ごと (fp だけが理由で変わる)", WD,
     '    return f"{HARD_IDLE_SUPPRESSED_KEY_PREFIX}{agent}{_KIND_SEP}{kind}"',
     '    return f"{HARD_IDLE_SUPPRESSED_KEY_PREFIX}{agent}{_KIND_SEP}any"',
     [("test_every_sequence_up_to_length_5_keeps_the_invariants", "I1"),
      "test_the_two_sequences_codex_found_are_covered_by_the_model"]),
    ("閉じた episode の古いキーを次の episode の前に消さない", WD,
     'unsettled=agent in self._unsettled)', 'unsettled=False)',
     ["test_a_pending_deletion_is_done_before_the_suppressed_branch_decides_to_notify",
      ("test_every_sequence_over_a_reduced_alphabet_keeps_the_invariants", "I2")]),
    ("【5 巡目 t017】通知する plan で _unsettled を解かない (連投)", WD,
     '        if plan.notify is not None:\n            # 通知する plan の forget には',
     '        if False:\n            # 通知する plan の forget には',
     [("test_a_deletion_that_recovers_after_a_relapse_does_not_renotify_every_cycle", "I1")]),
    ("古い試行 (別 Execution ID) のキーを消さない", WD,
     '            and not str(v).startswith(f"{ident}:"))', '            and False)',
     [("test_every_sequence_up_to_length_5_keeps_the_invariants", "I4")]),
    ("台帳の掃除の例外を外に出す", WD,
     '            except Exception as e:\n                _log(f"hard-idle 通知台帳の掃除に失敗 ({key}): {e}")',
     '            except KeyboardInterrupt as e:\n                _log(f"hard-idle 通知台帳の掃除に失敗 ({key}): {e}")',
     ["test_a_flush_that_raises_does_not_stop_the_cycle",
      ("test_every_sequence_up_to_length_5_keeps_the_invariants", "I5")]),
    ("run() がサイクルを呼ばない (t011)", WD,
     "            suppressed_notifier.cycle({m.agent_name for m in monitors.values()})\n", "",
     ["test_run_observes_every_monitor_each_cycle_and_forgets_through_one_helper"]),
    ("mux pid 不明を区別しない", WD, '            self._pid_unavailable = True', '            pass',
     ["test_a_missing_pane_pid_is_reported_as_mux_pid_unavailable"]),
    ("WARN を間引かない", WD,
     '        return self.summary_every > 0 and repeats % self.summary_every == 0\n\n    def forget(self, monitor: "WorkerMonitor") -> None:\n        self._state.pop((monitor.agent_name, monitor.task_id), None)\n\n\n_LEGACY',
     '        return True\n\n    def forget(self, monitor: "WorkerMonitor") -> None:\n        self._state.pop((monitor.agent_name, monitor.task_id), None)\n\n\n_LEGACY',
     ["test_warn_lines_are_thinned_to_reason_changes_and_every_n_cycles"]),
    ("run() が observe を呼ばない", WD, '                suppressed_notifier.observe(monitor, detail)\n', '',
     ["test_run_observes_every_monitor_each_cycle_and_forgets_through_one_helper"]),
]


def main() -> int:
    bad = 0
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="400")  # FAILED 行が端末幅で切れると理由の印が消える
    env.pop("AGENT_NAME", None)
    for name, path, old, new, targets in MUTATIONS:
        original = path.read_text()
        if original.count(old) != 1:
            print(f"BROKEN  {name}: 注入点が {original.count(old)} 回 (失効)")
            bad += 1
            continue
        try:
            path.write_text(original.replace(old, new))
            out = subprocess.run(
                [sys.executable, "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=900).stdout
        finally:
            path.write_text(original)
        lines = [line for line in out.splitlines() if line.startswith("FAILED")]
        failed = {line.split("::")[-1].split(" ")[0] for line in lines}

        def is_red(target):
            # (テスト名, 理由の印 [または印の組]) は、その不変条件の assert の文言で落ちたときだけ赤と数える。
            # 全並びの列挙は最初に落ちた並びで止まるので、複数の不変条件に触れる欠陥は印の組で受ける。
            if isinstance(target, tuple):
                marks = (target[1],) if isinstance(target[1], str) else target[1]
                return any(l.split("::")[-1].startswith(target[0]) and any(f"{m}:" in l for m in marks)
                           for l in lines)
            return any(f.startswith(target) for f in failed)
        missing = [t for t in targets if not is_red(t)]
        if missing:
            print(f"GREEN   {name}: 赤にならなかった {missing}")
            bad += 1
        else:
            print(f"RED     {name}: {sorted(failed)[:3]}")
    print("OK" if not bad else f"NG ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
