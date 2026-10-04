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

MUTATIONS = [
    ("zombie を木から外さない", LIB, '        if _proc_state(pid) == "Z":', '        if False:',
     ["test_a_zombie_child_does_not_make_the_tree_unknown",
      "test_watchdog_terminates_hard_idle_for_a_zombie_only_tree"]),
    ("読めないノードで即 unknown (旧実装)", LIB,
     '            saw_unknown = True\n            for child in children.get(pid, []):\n'
     '                queue.append((child, "unknown"))\n            continue',
     '            return "unknown"',
     ["test_a_job_wins_over_an_unreadable_node_in_either_order"]),
    ("通知がしきい値で出ない", WD, '        if lasted < self.threshold:\n            return',
     '        if True:\n            return', ["test_a_suppressed_hard_idle_notifies_the_director_once_after_the_threshold"]),
    ("fp が Execution ID を見ない", WD, '        if self.execution_id:\n            ident = self.execution_id',
     '        if False:\n            ident = self.execution_id', ["test_the_fingerprint_is_the_execution_id_with_a_started_at_fallback"]),
    ("解けても台帳キーを消さない", WD, '            self._pending_forget.add(key)',
     '            pass', ["test_recovery_clears_the_ledger_key_and_a_relapse_notifies_again"]),
    ("回復の判定をプロセス内の状態に戻す (t009)", WD,
     '        self._state.pop((monitor.agent_name, monitor.task_id), None)\n        key = self.ledger_key(monitor)\n        if self._has_key is None or self._has_key(key):',
     '        st = self._state.pop((monitor.agent_name, monitor.task_id), None)\n        key = self.ledger_key(monitor)\n        if st and st["notified"]:',
     ["test_recovery_across_a_watchdog_restart_still_clears_the_ledger",
      "test_a_pending_forget_lost_by_a_restart_is_redone_from_the_ledger"]),
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
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
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
                [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=300).stdout
        finally:
            path.write_text(original)
        failed = {line.split("::")[-1].split(" ")[0] for line in out.splitlines()
                  if line.startswith("FAILED")}
        missing = [t for t in targets if not any(f.startswith(t) for f in failed)]
        if missing:
            print(f"GREEN   {name}: 赤にならなかった {missing}")
            bad += 1
        else:
            print(f"RED     {name}: {sorted(failed)[:3]}")
    print("OK" if not bad else f"NG ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
