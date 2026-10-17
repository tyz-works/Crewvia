"""PR-F intent の 2 つの穴 (vNext 02 PR-F fix 2 / t063。Codex 指摘 P1 × 2)。

1. 回復の callback が `_reactivate_done_mission` を呼び、`Reactivated: …` を **stdout** に出していた。pull は本体の前に回復を走らせるので、
   stdout が JSON 1 つでなくなる (stdout を JSON として読む Worker・テストが壊れる。試行は既に running)。回復の経路の文言は stderr。
2. 打ち直し (`_execute_reopen_intent(resumed=True)`) が done / verified / 実行中以外の status を「処理済み」とみなしていた。回復が止まっている間に
   `update --status skipped` 等で中間 A を変えられると、card を直した後の回復が A を飛ばして I を pending にし intent を消す
   → I → A(skipped) → R(pending) で R がすぐ pull できる。打ち直しも入口の拒否規則 (`CASCADE_SKIPPED_ON_PATH` / `CASCADE_FAILED_ON_PATH`
   と、決定の後に完了した中間) を残りの対象に当て直し、当てはまれば intent を残して mission の pull を止める。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from execution_e3_helpers import MISSION, last_error_code, run, set_card

from test_reopen_cascade_plan_sh import (  # noqa: E402
    CHAIN, REASON, SKILLS, _fork_reopen, _restore, build, count, finish_all, reopen, status, store_check,
)
from test_reopen_intent_recovery import ARGV, TIDS, _chain_box, _stopped_box, intents, mission_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. 回復の出力は stderr
# ---------------------------------------------------------------------------

def test_the_recovery_reactivation_line_goes_to_stderr_at_every_kill_point(tmp_path):
    """mission が done のとき、pull が走らせる回復は『Reactivated』を出す。どの kill 位置でも pull の stdout は JSON 1 つ。"""
    box = _chain_box(tmp_path, "root")
    f = mission_file(box)
    f.write_text(f.read_text().replace("status: in_progress", "status: done"))
    saved = tmp_path / "saved-queue"
    shutil.copytree(box.queue, saved)
    recovered, reactivated_on_stderr = 0, 0
    for k in range(1, 400):
        _restore(box, saved)
        killed, _calls = _fork_reopen(box, ARGV, k)
        if not killed:
            break
        had_intent = bool(intents(box))
        if status(box, "t001") == "done" and not had_intent:
            continue                                            # intent を書く前に止まった: 回復は起きない (pull できる task も無い)
        p = box.pull("Mika", "t001", skills=SKILLS)
        assert p.returncode == 0, (k, p.stderr)
        json.loads(p.stdout)                                    # 回復が起きても起きなくても、stdout は JSON だけ
        assert "Reactivated" not in p.stdout, (k, p.stdout)
        if had_intent:
            recovered += 1
            reactivated_on_stderr += "Reactivated" in p.stderr
    assert recovered >= 8 and reactivated_on_stderr >= 1, (recovered, reactivated_on_stderr)


def test_a_pull_without_any_recovery_still_prints_only_json(tmp_path):
    box = _chain_box(tmp_path, "root")
    reopen(box)
    p = box.pull("Mika", "t001", skills=SKILLS)
    assert p.returncode == 0, p.stderr
    json.loads(p.stdout)


def test_commands_that_reactivate_on_their_own_keep_the_stdout_line(tmp_path):
    """回復を stderr に寄せただけで、reopen / add / update 自身の『Reactivated』(利用者向けの出力) は stdout のまま。"""
    box = _chain_box(tmp_path, "root")
    f = mission_file(box)
    f.write_text(f.read_text().replace("status: in_progress", "status: done"))
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert "Reactivated" in p.stdout and "Reactivated" not in p.stderr


def test_the_recovery_callback_passes_stderr_to_the_reactivation():
    """構造: 回復の経路 (`_complete_reopen_intents`) の `_reactivate_done_mission` は out=sys.stderr を渡す。"""
    text = (ROOT / "scripts" / "plan.sh").read_text()
    body = text[text.index("def _complete_reopen_intents("):text.index("def recover_before(")]
    calls = re.findall(r"_reactivate_done_mission\([^\n]*\)", body)
    assert calls and all("out=sys.stderr" in c for c in calls), calls


# ---------------------------------------------------------------------------
# 2. 打ち直しで入口の拒否規則を当て直す
# ---------------------------------------------------------------------------

def _blocked_pull(box):
    return box.pull("Mika", "t001", skills=SKILLS)


def _recovering_pull(box):
    """回復を走らせるだけの pull (R を名指し。I の再完了を待つので pull 自体は取れない)。"""
    return box.pull("Mika", "t003", skills=SKILLS)


@pytest.mark.parametrize("a_status, code", [("skipped", "CASCADE_SKIPPED_ON_PATH"), ("failed", "CASCADE_FAILED_ON_PATH")])
def test_a_middle_task_turned_skipped_or_failed_while_stopped_blocks_the_resume_and_keeps_the_intent(tmp_path, a_status, code):
    box = _stopped_box(tmp_path)
    set_card(box, "t002", status=a_status)                       # 回復が止まっている間に中間 A を変えた
    before = box.snapshot()
    p = _blocked_pull(box)
    assert p.returncode == 1 and last_error_code(p.stderr) == "REOPEN_INTENT_INCOMPLETE", (p.returncode, p.stderr)
    assert "t002" in p.stderr and code in p.stderr, p.stderr     # どの task がなぜ
    assert box.snapshot() == before                              # 何も書かない (I は done のまま・intent も残る)
    assert intents(box) == ["t001-1.json"] and status(box, "t001") == "done"
    out = store_check(box)
    assert re.search(rf"reopen-intent-blocked.*task=t002.*{code}", out), out
    # mission の pull は止まったまま。R は done のままなので、A の先は走れない
    assert _blocked_pull(box).returncode == 1
    assert [status(box, t) for t in TIDS] == ["done", a_status, "done"]


@pytest.mark.parametrize("a_status", ["skipped", "failed"])
def test_after_the_director_clears_the_task_the_same_reopen_finishes_everything(tmp_path, a_status):
    box = _stopped_box(tmp_path)
    set_card(box, "t002", status=a_status)
    assert _blocked_pull(box).returncode == 1
    set_card(box, "t002", status="pending")                      # Director の出口 (`update --status pending` 等) の結果
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert [status(box, t) for t in TIDS] == ["pending"] * 3 and intents(box) == []
    assert count(box, "t001") == 1 and count(box, "t003") == 1
    assert "reopen-intent" not in store_check(box)


@pytest.mark.parametrize("how", ["pull", "update", "reopen"])
def test_every_next_command_stops_at_the_same_place(tmp_path, how):
    """回復を走らせるどのコマンドでも、skipped の中間があれば I は done のまま (I → A(skipped) → R(pending) を作らない)。"""
    box = _stopped_box(tmp_path)
    set_card(box, "t002", status="skipped")
    if how == "pull":
        p = _blocked_pull(box)
        assert p.returncode == 1
    elif how == "update":
        p = run(box, "update", "t003", "--priority", "low", "--mission", MISSION, agent="Director")
        assert p.returncode == 0, p.stderr                         # 他の書き込みコマンドは止めない (出口を塞がない)
    else:
        p = reopen(box)
        assert p.returncode == 1 and last_error_code(p.stderr) == "REOPEN_INTENT_INCOMPLETE", p.stderr
    assert status(box, "t001") == "done" and status(box, "t003") == "done", how
    assert intents(box) == ["t001-1.json"]


@pytest.mark.parametrize("a_status", ["done", "verified", "pending", "needs_director", "blocked", "verification_failed"])
def test_statuses_that_are_safe_do_not_block_the_resume(tmp_path, a_status):
    """処理済みとみなしてよい status (done / verified = 戻す・それ以外の未完了 = そのまま) は、打ち直しを止めない。"""
    box = _stopped_box(tmp_path)
    if a_status != "done":
        set_card(box, "t002", status=a_status)
    p = _recovering_pull(box)
    assert intents(box) == [], (a_status, p.stderr)
    assert [status(box, t) for t in ("t001", "t003")] == ["pending", "pending"]
    assert status(box, "t002") == ("pending" if a_status in ("done", "verified") else a_status)


def test_a_failed_task_at_the_end_of_the_path_does_not_block(tmp_path):
    """経路の端の failed (依存元を持たない) は走る下流が無い。入口と同じく拒否しない。"""
    box = _stopped_box(tmp_path)
    set_card(box, "t003", status="failed")
    p = _recovering_pull(box)
    assert intents(box) == [] and status(box, "t001") == "pending", p.stderr
    assert status(box, "t003") == "failed"


def test_an_intermediate_that_finished_after_the_decision_blocks_the_resume(tmp_path):
    """決定の時点で未完了 (一覧に無い) だった経路上の中間が、止まっている間に done になった = 古い I の結果の上の完了。
    一覧を作り直さないので戻せない → 止めて理由を出す (戻さないまま I を pending にすると R が古い A で走る)。"""
    box = _chain_box(tmp_path, "root")
    set_card(box, "t002", status="pending")                      # reopen の時点で A は未完了 (一覧に入らない)
    saved = tmp_path / "saved-queue"
    shutil.copytree(box.queue, saved)
    for k in range(1, 400):
        _restore(box, saved)
        killed, _calls = _fork_reopen(box, ARGV, k)
        assert killed
        if intents(box):
            break
    assert intents(box) == ["t001-1.json"]
    assert "t002" not in json.loads((box.queue / "missions" / MISSION / "reopen-intents" / "t001-1.json").read_text())["tasks"].__repr__()
    set_card(box, "t002", status="done")                         # 止まっている間に A が done (手編集・旧形式の書き手)
    before = box.snapshot()
    p = _blocked_pull(box)
    assert p.returncode == 1 and last_error_code(p.stderr) == "REOPEN_INTENT_INCOMPLETE" and "t002" in p.stderr, p.stderr
    assert "PATH_CHANGED_WHILE_STALLED" in p.stderr
    assert box.snapshot() == before and status(box, "t001") == "done"
    assert re.search(r"reopen-intent-blocked.*task=t002.*PATH_CHANGED_WHILE_STALLED", store_check(box))


def test_the_first_run_is_not_rechecked_and_a_clean_reopen_is_unchanged(tmp_path):
    box = _chain_box(tmp_path, "root")
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert [status(box, t) for t in TIDS] == ["pending"] * 3 and intents(box) == []


def test_a_blocked_intent_does_not_stop_other_missions_or_read_only_commands(tmp_path):
    box = _stopped_box(tmp_path)
    set_card(box, "t002", status="skipped")
    assert _blocked_pull(box).returncode == 1
    assert run(box, "status", "--mission", MISSION, agent="Director").returncode == 0
    assert run(box, "store-check", "--mission", MISSION, agent="Director").returncode == 0
    u = run(box, "update", "t003", "--priority", "low", "--mission", MISSION, agent="Director")
    assert u.returncode == 0, u.stderr
