#!/usr/bin/env python3
"""red proof: t001 (dispatcher Rule 5 が mux state 'error' を黙殺する) の修正を 1 つずつ
戻すと tests/test_rule5_mux_state_error.py が赤になる。

欠陥を注入した版に **同じテスト** を走らせる。ファイルは in-place で書き換えて必ず元に
戻す (finally)。置換元がちょうど 1 回でなければ BROKEN (注入点が消えている = 失効)。
赤と数えるのは `FAILED <テスト名>` が出たときだけ。

実行: env -u AGENT_NAME python3 tests/red_proof_rule5_mux_state_error.py   (約 15 秒)
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPATCHER = ROOT / "scripts" / "dispatcher.sh"
TESTS = ["tests/test_rule5_mux_state_error.py"]

MUTATIONS = [
    ("is_E を常に False に戻す (= 修正前そのもの: error は is_A/is_B どちらにもならない)",
     DISPATCHER,
     "    is_E = not is_A and not is_B and st not in ('idle', 'done')",
     "    is_E = False",
     ["test_error_state_notifies_director_after_grace",
      "test_error_state_dedups_within_notify_ttl",
      "test_error_state_renotifies_after_ttl_expiry",
      "test_error_state_notifies_even_without_an_assignment",
      "test_recovery_to_working_clears_the_error_dedup_key_and_resets_grace",
      "test_a_fresh_error_after_recovery_starts_a_new_grace_period",
      "test_an_unrecognised_future_state_value_also_notifies_not_silently_drops"]),

    ("needs_director 除外のゲートに is_E を含めない (error が needs_director を無視して通知する)",
     DISPATCHER,
     "    if is_A or is_B or is_E:",
     "    if is_A or is_B:",
     ["test_error_state_is_suppressed_when_the_card_waits_on_director"]),

    ("最終の素通り分岐に is_E を含めない (= 修正前と同じ場所で同じ素通りが起きる)",
     DISPATCHER,
     "    if not is_A and not is_B and not is_E:",
     "    if not is_A and not is_B:",
     ["test_error_state_notifies_director_after_grace",
      "test_an_unrecognised_future_state_value_also_notifies_not_silently_drops"]),

    ("condition / notify_key が is_E を見ず、実際の state 文字列でなく固定の "
     "'idle-with-task' ラベルに化ける",
     DISPATCHER,
     "    condition = 'blocked' if is_A else (st if is_E else 'idle-with-task')",
     "    condition = 'blocked' if is_A else 'idle-with-task'",
     ["test_error_state_notifies_director_after_grace",
      "test_an_unrecognised_future_state_value_also_notifies_not_silently_drops"]),

    ("notify_key が is_E を見ず idle_with_task_{name} に化ける (working 復帰時の掃除と食い違う)",
     DISPATCHER,
     "    notify_key = f'blocked_{name}' if is_A else (f'{st}_{name}' if is_E else f'idle_with_task_{name}')",
     "    notify_key = f'blocked_{name}' if is_A else f'idle_with_task_{name}'",
     ["test_recovery_to_working_clears_the_error_dedup_key_and_resets_grace"]),

    ("working 復帰時の dedup キー掃除から error_{name} を外す (消え残ってまた黙る)",
     DISPATCHER,
     "            for key in (f'blocked_{name}', f'idle_with_task_{name}', f'error_{name}'):",
     "            for key in (f'blocked_{name}', f'idle_with_task_{name}'):",
     ["test_recovery_to_working_clears_the_error_dedup_key_and_resets_grace"]),
]


def main() -> int:
    bad = 0
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="400")
    env.pop("AGENT_NAME", None)
    only = os.environ.get("RED_PROOF_ONLY")
    extra = ["-k", os.environ["RED_PROOF_K"]] if os.environ.get("RED_PROOF_K") else []
    for name, path, old, new, targets in MUTATIONS:
        if only and only not in name:
            continue
        original = path.read_text()
        if original.count(old) != 1:
            print(f"BROKEN  {name}: 注入点が {original.count(old)} 回 (失効)")
            bad += 1
            continue
        try:
            path.write_text(original.replace(old, new))
            out = subprocess.run(
                [sys.executable, "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider", *extra],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=300).stdout
        finally:
            path.write_text(original)
        lines = [line for line in out.splitlines() if line.startswith("FAILED")]
        failed = {line.split("::")[-1].split(" ")[0] for line in lines}
        missing = [t for t in targets if not any(f.startswith(t) for f in failed)]
        if missing:
            print(f"GREEN   {name}: 赤にならなかった {missing}")
            bad += 1
        else:
            print(f"RED     {name}: {sorted(failed)}")
    print("OK" if not bad else f"NG ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
