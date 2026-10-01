"""01c E2 (t008): `plan.sh pull` を Controller (reserve + start) 経由にし、Execution ID を発行する。

設計: `knowledge/execution.md` §1 (置き場)・§3 (attempt)・§6 (pull の冪等化 N8)・§6.1 (準備ロック)。受入条件:

1. 互換性 (task の選択・stdout の JSON は追加の欄だけ・exit code・assignment・`.crewvia-env` は追加の行だけ) —
   固定 fixture の比較は `test_plan_sh_compat_s3.py`。ここは**足した出力そのもの**を固定する
2. 並行 pull (同じ task を 2 人の Worker が取り合う・20 回) で 1 人だけが取る
3. pull の途中の各点で SIGKILL → 同じ Worker の再 pull が設計どおりに収束 (N8)
4. 赤の実証: 冪等化・Execution ID の発行・二重予約の拒否 (`tests/red_proof_e2_pull.py`)

Codex 3 巡目 (t002) の P1 (旧形式の reset と共存する CAS)・設計 t024 の P2-1 (同じ予約の並行再開)・E1 QA の O1 (自分の古い枠)・
O2 (時刻形でない `now`) もここ。**本番の queue / worktree / mux には触れない** (`pull_execution_helpers.Box` が隔離する)。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

import pytest

import pull_execution_helpers as h
from pull_execution_helpers import Box, MISSION

XID_RE = re.compile(r"ex-[0-9a-f]{32}")


def out(p):
    assert p.returncode == 0, (p.stdout, p.stderr)
    return json.loads(p.stdout)


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root")


# ---------------------------------------------------------------------------
# 1. 発行した ID がどこにあるか (card / record / identity / `.crewvia-env` / JSON / 監査)
# ---------------------------------------------------------------------------

def test_pull_issues_one_execution_and_every_place_agrees(box):
    res = out(box.pull("Ren"))
    xid = res.get("execution_id")
    assert isinstance(xid, str) and XID_RE.fullmatch(xid) and res.get("attempt") == 1, res

    meta = box.card()
    assert meta.get("current_execution_id") == xid
    assert meta.get("execution_status") == "running"             # start まで進めてから JSON を出す
    assert meta["execution_count"] == 1
    assert meta["execution_reserved_at"] == meta["started_at"]   # 世代 (started_at) の置き換え: 同じ値の写し
    assert meta["execution_agent"] == "Ren"
    assert meta.get("task_slug") == res["task_slug"] == "task-t001"
    assert "execution_end_code" not in meta

    rec = box.record(xid)
    assert (rec["status"], rec["attempt"], rec["agent"], rec["reserved_at"]) == ("running", 1, "Ren",
                                                                                  meta["started_at"])
    assert rec["git"]["worktree"] == res["worktree_path"]        # start に渡した git の文脈 (記録だけ)
    assert box.records() == [f"{xid}.json"]

    ident = box.identity("Ren")
    assert ident.get("execution_id") == xid and ident["started_at"] == meta["started_at"]
    assert box.slot("Ren") == f"{MISSION}:t001"                  # 枠の本文は今までと同じ (dispatcher の busy 判定)

    env_lines = box.env_file().splitlines()
    assert env_lines[-1] == f"export CREWVIA_EXECUTION_ID={xid}"
    assert [l.split("=")[0] for l in env_lines[:3]] == ["export CREWVIA_MISSION_SLUG", "export CREWVIA_TASK_ID",
                                                        "export CREWVIA_TASK_SLUG"]

    rows = [r for r in box.audit_rows() if r["op"] == "pull"]
    assert [r["execution_id"] for r in rows] == [xid, xid], rows       # reserve の行と start の行
    assert rows[0]["from_status"] == "pending" and rows[0]["to_status"] == "in_progress"
    assert rows[1].get("caller_check") == "verified"


def test_started_at_stays_a_timestamp_the_watchdog_can_read(box):
    """O2: `started_at` は watchdog の idle 時計の起点。pull が書く値は時刻として読める (世代の形だけでは足りない)。"""
    out(box.pull("Ren"))
    started = box.card()["started_at"]
    assert datetime.fromisoformat(started.replace("Z", "+00:00")).tzinfo is not None


def test_a_non_time_now_is_refused_before_anything_is_written(tmp_path):
    """O2: 世代の形 (`[A-Za-z0-9:_.+-]{1,64}`) には合うが時刻でない `now` を Controller が通さない。"""
    box = Box(tmp_path / "root")
    before = box.snapshot()
    import lib_state_store as store
    import lib_task_controller as ctl
    for bad in ("yesterday", "2026-13-45T99:99:99Z", "20261001", ""):
        with store.transaction(box.queue, op="probe", actor="test") as txn:
            with pytest.raises(h.ex.ControllerError) as e:
                ctl.reserve_task(txn, MISSION, "t001", "Ren", now=bad)
        assert e.value.code == h.ex.INVALID_ARGUMENT, bad
    assert box.snapshot() == before


def test_pull_without_an_agent_still_works_and_creates_no_slot(box):
    p = box.plan("pull", "--skills", "code", "--task", "t001", "--mission", MISSION)
    res = out(p)
    meta = box.card()
    assert meta["worker"] is None and meta["current_execution_id"] == res["execution_id"]
    assert "execution_agent" not in meta
    assert not (box.queue / "assignments").exists() or not any((box.queue / "assignments").iterdir())
    assert box.record(res["execution_id"])["agent"] is None


def test_target_dir_task_goes_through_the_controller_without_a_worktree(box, tmp_path):
    td = tmp_path / "target"
    td.mkdir()
    path = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    path.write_text(path.read_text().replace("target_dir: null", f"target_dir: {td}"))
    p = box.plan("pull", "--agent", "Ren", "--skills", "code", "--task", "t001", "--mission", MISSION,
                 "--target-dir", str(td))
    res = out(p)
    assert res["worktree_path"] is None and XID_RE.fullmatch(res["execution_id"])
    meta = box.card()
    assert meta["execution_status"] == "running" and meta["current_execution_id"] == res["execution_id"]
    assert box.record(res["execution_id"])["git"]["worktree"] is None


def test_a_second_attempt_closes_the_first_and_counts_up(box):
    first = out(box.pull("Ren"))
    box.reset()                                                  # 旧形式の reset (E4 まで試行の欄は触らない)
    assert box.card()["status"] == "pending"
    second = out(box.pull("Sora"))
    assert second["attempt"] == 2 and second["execution_id"] != first["execution_id"]
    rec1 = box.record(first["execution_id"])
    # reset は試行を閉じない (E4a から) ので、reserve の手順 0 が「Controller の外で手放された試行」として閉じる
    assert (rec1["status"], rec1["end_code"]) == ("failed", "ABANDONED_OUTSIDE_CONTROLLER")
    assert box.card()["execution_count"] == 2
    assert box.slot("Ren") is None and box.slot("Sora") == f"{MISSION}:t001"


def test_the_task_slug_is_fixed_at_the_first_reserve_even_if_the_title_changes(box):
    first = out(box.pull("Ren"))
    box.reset()
    path = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    path.write_text(path.read_text().replace("title: task t001", "title: A completely different title"))
    second = out(box.pull("Ren"))
    assert second["task_slug"] == first["task_slug"] == "task-t001"
    assert second["worktree_path"] == first["worktree_path"]       # branch / worktree は変わらない (W2 で再利用)


# ---------------------------------------------------------------------------
# 2. 二重予約の拒否・並行 pull (20 回)
# ---------------------------------------------------------------------------

def test_a_second_pull_of_a_reserved_task_by_someone_else_is_refused_with_a_fixed_code(box):
    out(box.pull("Ren"))
    before = box.snapshot()
    p = box.pull("Sora")
    assert p.returncode == 1 and p.stdout.strip() == ""
    assert h.last_error_code(p.stderr) == "TASK_ALREADY_RESERVED"
    assert box.snapshot() == before
    # 取れる task が無い自動選択は今までどおり idle (exit 2)・何も書かない
    q = box.pull("Sora", auto=True)
    assert q.returncode == 2 and h.last_error_code(q.stderr) is None
    assert box.snapshot() == before


@pytest.mark.parametrize("form", ["task", "auto"])
def test_concurrent_pulls_of_one_task_give_it_to_exactly_one_worker(tmp_path, form):
    seed = Box(tmp_path / "seed")
    wins = 0
    for i in range(20):
        b = seed.clone(tmp_path / f"it{i:02d}")
        barrier = threading.Barrier(2)
        procs = {}

        def go(agent):
            barrier.wait()
            args = ["pull", "--agent", agent, "--skills", "code", "--mission", MISSION]
            if form == "task":
                args += ["--task", "t001"]
            procs[agent] = b.plan(*args)

        threads = [threading.Thread(target=go, args=(a,)) for a in ("Ren", "Sora")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        codes = sorted(p.returncode for p in procs.values())
        won = [a for a, p in procs.items() if p.returncode == 0]
        assert len(won) == 1, (i, {a: (p.returncode, p.stderr[-200:]) for a, p in procs.items()})
        lost = procs["Ren" if won == ["Sora"] else "Sora"]
        assert lost.stdout.strip() == "" and lost.returncode in (1, 2, 3), codes
        meta = b.card()
        assert meta["worker"] == won[0] and meta["execution_status"] == "running" and meta["execution_count"] == 1
        assert len(b.records()) == 1 and b.slot(won[0]) == f"{MISSION}:t001"
        assert b.slot("Ren" if won == ["Sora"] else "Sora") is None
        wins += 1
    assert wins == 20


def test_two_reserved_cards_of_one_worker_are_not_guessed_between(tmp_path):
    box = Box(tmp_path / "root", tasks=("t001", "t002"))
    out(box.pull("Ren", "t001"))
    # 2 枚目の予約を作る (本番では agent_busy_elsewhere が止めるので、card を直接 reserve 済みの形にする)
    import lib_state_store as store
    import lib_task_controller as ctl
    with store.transaction(box.queue, op="seed", actor="test") as txn:
        meta, body = txn.load_card(MISSION, "t001")
        meta.update(execution_status="reserved")
        txn.write_card(MISSION, "t001", meta, body)
        ctl.reserve_task(txn, MISSION, "t002", None, now="2026-10-01T10:00:00.000000Z")
        m2, b2 = txn.load_card(MISSION, "t002")
        m2["worker"] = "Ren"
        txn.write_card(MISSION, "t002", m2, b2)
    def cards_and_slots():       # record は回復 (R-5) が seed の食い違い (card = reserved・record = running) を合わせうる
        return {k: v for k, v in box.snapshot().items() if "/executions/" not in k}
    before = cards_and_slots()
    p = box.pull("Ren", auto=True)
    assert p.returncode == 3 and p.stdout.strip() == "", (p.returncode, p.stderr)
    assert cards_and_slots() == before


# ---------------------------------------------------------------------------
# 3. 冪等化 (N8): pull の途中で死んだ後の再 pull
# ---------------------------------------------------------------------------

def _stage_patch(stage):
    """pull の途中の地点で自分に SIGKILL を送る (名前空間の協力者を差し替える。本番のコードにフックは足さない)。"""
    def patch(ns):
        import lib_state_store as store
        if stage == "before_prepare_lock":
            def boom(*a, **k):
                h.die_here()
            store.acquire_prepare_lock = boom
        elif stage == "after_prepare_lock":
            real = store.acquire_prepare_lock

            def after(*a, **k):
                real(*a, **k)
                h.die_here()
            store.acquire_prepare_lock = after
        elif stage == "before_worktree":
            ns["taskvia_sync_pull"] = lambda *a, **k: h.die_here()
        elif stage == "before_start":
            ns["_pull_start"] = lambda *a, **k: h.die_here()
        elif stage == "after_start":
            def boom(self):
                h.die_here()
            store.PrepareLock.release = boom
        else:
            raise AssertionError(stage)
    return patch


RESUMABLE_STAGES = ["before_prepare_lock", "after_prepare_lock", "before_worktree", "before_start"]


@pytest.mark.parametrize("stage", RESUMABLE_STAGES)
@pytest.mark.parametrize("repull", ["task", "auto"])
def test_a_pull_killed_before_start_is_resumed_by_the_same_worker_with_the_same_execution(tmp_path, stage, repull):
    box = Box(tmp_path / "root")
    argv = ["--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001"]
    killed, _calls, _ok = h.fork_run(box, _stage_patch(stage), argv, "Ren")
    assert killed, stage
    meta = box.card()
    assert meta["status"] == "in_progress" and meta["execution_status"] == "reserved"
    xid = meta["current_execution_id"]
    assert box.slot("Ren") == f"{MISSION}:t001" and box.identity("Ren")["execution_id"] == xid

    p = box.pull("Ren", auto=(repull == "auto"))
    res = out(p)
    assert res["execution_id"] == xid and res["attempt"] == 1, "再開は新しい試行を作らない"
    meta = box.card()
    assert meta["execution_status"] == "running" and meta["execution_count"] == 1
    assert box.records() == [f"{xid}.json"] and box.record(xid)["status"] == "running"
    assert box.env_file().splitlines()[-1] == f"export CREWVIA_EXECUTION_ID={xid}"
    assert "generation_mismatch" not in box.store_check()


def test_a_pull_killed_after_start_is_not_resumed_because_the_json_may_have_been_handed_over(tmp_path):
    box = Box(tmp_path / "root")
    argv = ["--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001"]
    killed, _c, _ok = h.fork_run(box, _stage_patch("after_start"), argv, "Ren")
    assert killed
    xid = box.card()["current_execution_id"]
    assert box.card()["execution_status"] == "running"
    before = box.snapshot()
    again = box.pull("Ren")
    assert again.returncode == 1 and again.stdout.strip() == ""
    assert h.last_error_code(again.stderr) == "TASK_ALREADY_RESERVED"
    assert box.pull("Ren", auto=True).returncode == 2           # 取れる task は無い (idle)
    assert box.snapshot() == before
    # 出口は今と同じ Director の reset → 次の pull は新しい試行 (attempt 2)
    box.reset()
    res = out(box.pull("Ren"))
    assert res["attempt"] == 2 and res["execution_id"] != xid


def test_pull_killed_at_every_write_point_converges_and_never_forks_the_execution(tmp_path):
    """lib の書き込み点の k 番目で落とし (reserve・start の全点)、同じ Worker の再 pull が 1 つの試行に収束する。"""
    seed = Box(tmp_path / "seed")
    argv = ["--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001"]
    points, converged_from_reserved = 0, 0
    for k in range(1, 80):
        b = seed.clone(tmp_path / f"k{k:02d}")
        killed, calls, ok = h.fork_run(b, None, argv, "Ren", fault_kill_at=k)
        if not killed:
            assert ok and calls == k - 1
            break
        points = k
        card_xid = b.card().get("current_execution_id")
        for how in ("task", "auto"):
            t = b.clone(tmp_path / f"k{k:02d}-{how}")
            p = t.pull("Ren", auto=(how == "auto"))
            meta = t.card()
            if meta.get("execution_status") == "running" and card_xid and meta["current_execution_id"] == card_xid \
                    and b.card().get("execution_status") == "running":
                # start のコミット後に落ちた: 再開しない (running)。exit 1 / 2 で何も書かない
                assert p.returncode in (1, 2) and p.stdout.strip() == "", (k, how, p.stderr)
                continue
            res = out(p)
            if card_xid:
                assert res["execution_id"] == card_xid, (k, how, "同じ試行を再開するはず")
                converged_from_reserved += 1
            assert meta["execution_count"] == 1 and len(t.records()) == 1, (k, how)
            assert t.identity("Ren")["execution_id"] == res["execution_id"]
            assert t.slot("Ren") == f"{MISSION}:t001"
            assert meta["execution_status"] == "running"
    assert points >= 10, f"落とせる点が {points} 個しか無い — 注入口が壊れている"
    assert converged_from_reserved >= 6, "reserve 済みの点から再開できた場合が少なすぎる (テストが空回りしている)"


def test_a_worktree_failure_closes_the_execution_and_is_not_resumed(box):
    box.hold(fail=True, block=False)
    p = box.pull("Ren")
    assert p.returncode == 1 and p.stdout.strip() == ""
    meta = box.card()
    assert meta["status"] == "needs_director"
    assert (meta["execution_status"], meta["execution_end_code"]) == ("failed", "WORKSPACE_CREATE_FAILED")
    rec = box.record(meta["current_execution_id"])
    assert (rec["status"], rec["end_code"]) == ("failed", "WORKSPACE_CREATE_FAILED")
    assert box.slot("Ren") is None
    again = box.pull("Ren")
    assert again.returncode == 1                                  # needs_director は取れない (再開もしない)
    assert box.pull("Ren", auto=True).returncode == 2


# ---------------------------------------------------------------------------
# 4. 準備ロック (execution.md §6.1): 同じ予約の並行 pull
# ---------------------------------------------------------------------------

def test_a_second_pull_of_the_same_reservation_during_preparation_touches_nothing(box):
    box.hold()
    first = box.popen("pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001",
                      agent="Ren")
    try:
        box.reached()
        xid = box.card()["current_execution_id"]
        snap = box.snapshot(with_audit=True)
        wt_before = sorted(p.name for p in (box.root / ".claude").rglob("*"))
        for form in (False, True):
            p = box.pull("Ren", auto=form)
            assert p.returncode == 1 and p.stdout.strip() == "", (form, p.returncode, p.stderr)
            assert h.last_error_code(p.stderr) == "TASK_ALREADY_RESERVED"
        assert box.snapshot(with_audit=True) == snap, "2 本目が card / record / 枠 / 監査の本体行のどれかを書いた"
        assert sorted(p.name for p in (box.root / ".claude").rglob("*")) == wt_before
        assert box.env_file() is None, "2 本目が .crewvia-env に触れた"
        box.go()
        stdout, stderr = first.communicate(timeout=60)
        assert first.returncode == 0, stderr
        res = json.loads(stdout)
        assert res["execution_id"] == xid and box.card()["execution_status"] == "running"
    finally:
        h.kill_group(first) if first.poll() is None else None


def test_the_preparing_pull_that_lost_its_reservation_exits_without_json(box):
    """1 本目をロック 1 の後・準備ロックの前で止め、2 本目が最後まで進む → 1 本目は読み直しで exit 1・JSON なし・何も書かない。"""
    argv = ["--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001"]

    def patch(ns):
        import lib_state_store as store
        real = store.acquire_prepare_lock

        def gated(*a, **k):
            (box.root / "hold" / "stopped").write_text("")
            h.wait_for(box.root / "hold" / "resume")
            return real(*a, **k)
        store.acquire_prepare_lock = gated

    first = h.fork_start(box, patch, argv, "Ren")
    h.wait_for(box.root / "hold" / "stopped")
    second = out(box.pull("Ren"))                                 # 2 本目 (同じ agent の再 pull) が最後まで進む
    snap = box.snapshot(with_audit=True)
    (box.root / "hold" / "resume").write_text("")
    killed, _calls, ok = first.wait()
    assert not killed and not ok, "1 本目は SystemExit (exit 1) で終わるはず"
    assert box.snapshot(with_audit=True) == snap, "1 本目が card / 枠 / 監査のどれかを書いた"
    assert box.card()["current_execution_id"] == second["execution_id"] and box.card()["execution_count"] == 1


def test_the_preparation_lock_holder_that_fails_closes_the_execution_and_the_other_pull_was_refused(box):
    box.hold(fail=True)
    first = box.popen("pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001",
                      agent="Ren")
    try:
        box.reached()
        p = box.pull("Ren")
        assert p.returncode == 1 and h.last_error_code(p.stderr) == "TASK_ALREADY_RESERVED"
        assert box.card()["execution_status"] == "reserved"          # 他の pull が準備中: 2 本目は G1 を書けない
        box.go()
        _o, _e = first.communicate(timeout=60)
        assert first.returncode == 1
        meta = box.card()
        assert (meta["status"], meta["execution_status"], meta["execution_end_code"]) == (
            "needs_director", "failed", "WORKSPACE_CREATE_FAILED")
    finally:
        h.kill_group(first) if first.poll() is None else None


def test_a_killed_preparing_pull_leaves_no_stale_lock_and_the_next_pull_resumes_the_same_execution(box):
    box.hold()
    first = box.popen("pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001",
                      agent="Ren")
    try:
        box.reached()
        xid = box.card()["current_execution_id"]
        h.kill_group(first)                                          # 準備ロックを持ったまま SIGKILL
    finally:
        h.kill_group(first) if first.poll() is None else None
    box.unhold()
    res = out(box.pull("Ren"))
    assert res["execution_id"] == xid and res["attempt"] == 1


def test_the_preparation_lock_is_per_task(tmp_path):
    box = Box(tmp_path / "root", tasks=("t001", "t002"))
    box.hold("t001")
    first = box.popen("pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001",
                      agent="Ren")
    try:
        box.reached("t001")
        res = out(box.pull("Sora", "t002"))                          # 別 task・別 Worker は止まらない
        assert res["id"] == "t002"
        box.go("t001")
        first.communicate(timeout=60)
        assert first.returncode == 0
    finally:
        h.kill_group(first) if first.poll() is None else None


# ---------------------------------------------------------------------------
# 5. CAS: 旧形式の書き手 (今の `update --reset`・rollback 中の旧コード) との共存 (Codex 3 巡目 P1)
# ---------------------------------------------------------------------------

def _old_reset_cmd(box):
    return (f"AGENT_NAME=Director {box.plan_sh} update t001 --status pending --reset --mission {MISSION}\n")


def test_an_old_format_reset_during_preparation_is_not_overwritten_by_start(box):
    """旧形式の reset は status / worker / started_at だけを動かし、execution の欄 (X / reserved) を残す。
    新しい欄だけの CAS は、解放済みの予約を running にして JSON を渡す。今までの欄との AND で拒否する。"""
    box.hold(cmd=_old_reset_cmd(box), block=False)
    p = box.pull("Ren")
    assert p.returncode == 1 and p.stdout.strip() == "", (p.returncode, p.stdout, p.stderr)
    meta = box.card()
    assert meta["status"] == "pending" and meta["worker"] is None and meta["started_at"] is None
    assert meta["execution_status"] == "reserved", "X / reserved は旧形式の reset の後も残っている (これが罠)"
    assert box.slot("Ren") is None
    assert "開始 (start) できませんでした" in p.stderr


def test_an_old_format_reset_during_preparation_is_not_overwritten_by_the_worktree_failure_path_either(box):
    box.hold(cmd=_old_reset_cmd(box), fail=True, block=False)
    p = box.pull("Ren")
    assert p.returncode == 1 and p.stdout.strip() == ""
    meta = box.card()
    assert meta["status"] == "pending", "Director の reset を G1 が needs_director で上書きしてはいけない"
    assert meta["execution_status"] == "reserved"
    assert "書き換えませんでした" in p.stderr


def test_a_successor_that_took_the_task_after_an_old_format_reset_wins_over_the_stale_pull(box):
    """reset → 別の Worker (Sora) が新しい試行 Y を予約 → 古い pull (Ren) の start は (X ≠ Y) で外れ、Y を奪わない。
    Sora の pull は Ren が準備ロックを持っている間に走るので exit 1 (同じ予約の pull が進行中) で、Y は reserved のまま
    Sora の再 pull を待つ (再開できる)。"""
    cmd = (f"rm -f '{box.root}/hold/t001.cmd'\n" + _old_reset_cmd(box) +
           f"AGENT_NAME=Sora CREWVIA_QUEUE='{box.queue}' CREWVIA_REPO_ROOT='{box.root}' CREWVIA_TASKVIA=disabled "
           f"CREWVIA_TASK_GRAPH=0 {box.plan_sh} pull --agent Sora --skills code --task t001 --mission {MISSION} >&2\n")
    box.hold(cmd=cmd, block=False)
    p = box.pull("Ren")
    assert p.returncode == 1 and p.stdout.strip() == "" and "開始 (start) できませんでした" in p.stderr
    meta = box.card()
    assert meta["worker"] == "Sora" and meta["execution_status"] == "reserved" and meta["execution_count"] == 2
    assert box.slot("Ren") is None and box.slot("Sora") == f"{MISSION}:t001"
    y = meta["current_execution_id"]
    res = out(box.pull("Sora"))                                  # 後任の再 pull は自分の予約 Y を再開する
    assert res["execution_id"] == y and res["attempt"] == 2 and box.card()["execution_status"] == "running"


def _old_reset_then_reopen_cmd(box):
    """旧形式の reset の後、Director が card を自分で開き直す (`update --status in_progress`)。worker・started_at は空のまま、
    status は holding・execution の欄は X / reserved のまま: 新しい欄 (X / reserved) だけの CAS は通ってしまう形。"""
    return _old_reset_cmd(box) + (
        f"AGENT_NAME=Director {box.plan_sh} update t001 --status in_progress --mission {MISSION} >&2\n")


def test_a_card_reopened_by_the_director_after_an_old_format_reset_is_not_started_by_the_stale_pull(box):
    """新しい欄だけの CAS (`current_execution_id == X かつ execution_status == reserved`) は、この card を通す
    (status は in_progress に戻り、欄は X / reserved のまま。`attempt_view` も ACTIVE)。Director が開いた card を、
    解放済みの予約の pull が running にして JSON を渡してはいけない。今までの欄 (worker・started_at) との AND で拒否する。"""
    box.hold(cmd=_old_reset_then_reopen_cmd(box), block=False)
    p = box.pull("Ren")
    assert p.returncode == 1 and p.stdout.strip() == "", (p.returncode, p.stdout, p.stderr)
    meta = box.card()
    assert meta["status"] == "in_progress" and meta["worker"] is None and meta["started_at"] is None
    assert meta["execution_status"] == "reserved", "start が通って running になった (CAS が新しい欄しか見ていない)"
    assert "開始 (start) できませんでした" in p.stderr
    assert box.slot("Ren") is None


def test_the_worktree_failure_path_does_not_overwrite_a_card_the_director_reopened(box):
    box.hold(cmd=_old_reset_then_reopen_cmd(box), fail=True, block=False)
    p = box.pull("Ren")
    assert p.returncode == 1 and p.stdout.strip() == ""
    meta = box.card()
    assert meta["status"] == "in_progress", "G1 が Director の開いた card を needs_director で上書きした"
    assert meta["execution_status"] == "reserved" and "execution_end_code" not in meta
    assert "書き換えませんでした" in p.stderr


def test_someone_elses_reserved_task_is_never_taken_over_by_another_worker(box):
    box.hold()
    first = box.popen("pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001",
                      agent="Ren")
    try:
        box.reached()
        snap = box.snapshot(with_audit=True)
        for agent in ("Sora", "Ren"):                              # 別の Worker は奪えない。同じ Worker は準備ロックで待たされる
            p = box.pull(agent)
            assert p.returncode == 1 and p.stdout.strip() == "", (agent, p.returncode, p.stderr)
            assert h.last_error_code(p.stderr) == "TASK_ALREADY_RESERVED"
        assert box.snapshot(with_audit=True) == snap, "別の Worker の pull が枠・card・record のどれかを書いた"
        assert box.card()["worker"] == "Ren" and box.slot("Sora") is None
        box.go()
        first.communicate(timeout=60)
        assert first.returncode == 0
    finally:
        h.kill_group(first) if first.poll() is None else None


# ---------------------------------------------------------------------------
# 6. O1: 自分の古い枠が残ったまま reserve が途中で落ちた (E1 QA)
# ---------------------------------------------------------------------------

def test_o1_a_stale_slot_of_the_previous_attempt_next_to_a_reserved_card_is_republished_by_the_resume(tmp_path):
    """E1 QA の O1: card は新しい Y・identity は前の試行 X のまま (R-1 は枠が**無い**ときだけ作り直すので
    `reported:generation_mismatch` が出続け、reserve の再送は `TASK_ALREADY_RESERVED`)。決定: **card が正本・枠は projection**
    なので、card の持ち主 (自分) の再 pull が card の試行 Y で公開し直す (R-1 に「食い違う identity を作り直す」を足さない:
    R-1 は名指しの card に対し本体の前に走る回復で、別の Worker の取り直し後の枠を巻き戻しうる)。
    plan.sh の pull は reserve の前の回復 (R-2) が孤児の古い枠を消すので、この状態は reserve の card の後に古い枠が現れたときだけ
    (rollback 中の旧書き手など)。その状態を card の後ろの全書き込み点で作り、再 pull が収束することを見る。"""
    seed = Box(tmp_path / "seed")
    first = out(seed.pull("Ren"))
    old_slot = (seed.queue / "assignments" / "Ren").read_text()
    old_identity = (seed.queue / "assignments" / "Ren.identity").read_text()
    seed.reset()
    argv = ["--agent", "Ren", "--skills", "code", "--mission", MISSION, "--task", "t001"]
    probes = 0
    for k in range(1, 40):
        b = seed.clone(tmp_path / f"o1-{k:02d}")
        killed, calls, ok = h.fork_run(b, None, argv, "Ren", fault_kill_at=k)
        if not killed:
            break
        meta = b.card()
        if not (meta.get("execution_status") == "reserved" and meta["current_execution_id"] != first["execution_id"]):
            continue
        probes += 1
        y = meta["current_execution_id"]
        # 前の試行 X の枠と identity が card の後に残っている形を作る (E1 QA の probe と同じ形)
        (b.queue / "assignments").mkdir(exist_ok=True)
        (b.queue / "assignments" / "Ren").write_text(old_slot)
        (b.queue / "assignments" / "Ren.identity").write_text(old_identity)
        assert b.identity("Ren")["execution_id"] == first["execution_id"] != y
        res = out(b.pull("Ren"))                                   # 修正前: TASK_ALREADY_RESERVED で exit 1
        assert res["execution_id"] == y and res["attempt"] == 2
        assert b.identity("Ren")["execution_id"] == y and b.slot("Ren") == f"{MISSION}:t001"
        assert "generation_mismatch" not in b.store_check()
    assert probes >= 2, "O1 の状態 (card = Y reserved) を作れていない — 注入が効いていない"


# ---------------------------------------------------------------------------
# 7. 進行中の legacy card・他のコマンドとの共存
# ---------------------------------------------------------------------------

def test_a_legacy_in_progress_card_is_not_rewritten_and_pull_still_refuses_it(tmp_path):
    def legacy(t):
        meta = h.sc.card("t001", "in_progress", "Ren", "2026-10-01T09:00:00.000000Z")
        t.write_card(MISSION, "t001", meta, h.sc.body())
        t.publish_assignment("Ren", MISSION, "t001", "2026-10-01T09:00:00.000000Z")
    box = Box(tmp_path / "root", seed_extra=legacy)
    before = box.snapshot()
    p = box.pull("Ren")
    assert p.returncode == 1 and "already in_progress" in p.stderr and p.stdout.strip() == ""
    assert box.snapshot() == before, "legacy の進行中 card に ID を後付けしてはいけない (推測で書き換えない)"
    done = box.plan("done", "t001", "finished", "--no-pr", "legacy", "--mission", MISSION, agent="Ren")
    assert done.returncode == 0, done.stderr
    assert box.card()["status"] == "done" and "current_execution_id" not in box.card_text()


def test_a_new_pull_followed_by_the_unchanged_done_still_completes(box):
    out(box.pull("Ren"))
    done = box.plan("done", "t001", "finished", "--no-pr", "x", "--mission", MISSION, agent="Ren")
    assert done.returncode == 0, done.stderr
    assert box.card()["status"] == "done" and box.slot("Ren") is None


def test_kai_review_style_pull_discarding_the_json_goes_through_the_same_path(tmp_path):
    """kai-review.sh (:226-237) は `pull --task <id> --agent Kai-codex --skills codex-review [--mission]` の JSON を捨てる。"""
    box = Box(tmp_path / "root")
    path = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    path.write_text(path.read_text().replace("skills:\n- code", "skills:\n- codex-review")
                    if "skills:\n- code" in path.read_text() else path.read_text())
    p = box.plan("pull", "--task", "t001", "--agent", "Kai-codex", "--skills", "codex-review,code",
                 "--mission", MISSION)
    assert p.returncode == 0, p.stderr
    meta = box.card()
    assert meta["worker"] == "Kai-codex" and meta["execution_status"] == "running"
    # dispatcher の二重着弾 (同じ指示がもう一度届く) は今までどおり exit 1 (running は再開しない)
    again = box.plan("pull", "--task", "t001", "--agent", "Kai-codex", "--skills", "codex-review,code",
                     "--mission", MISSION)
    assert again.returncode == 1 and "already in_progress" in again.stderr


def test_the_controller_refuses_a_foreign_slot_unless_the_caller_already_checked_it_is_an_orphan(tmp_path):
    """今の pull は、別の task を指す**孤児の枠** (手放し済み・pending・無い mission を指す) を上書きする
    (`agent_busy_elsewhere` が判定。tests/test_assignment_routing.py)。Controller は他の card を読まないので、既定では
    別の task を指す枠を拒否し、呼び出し側が確かめ済みのときだけ (`foreign_slot_checked=True`) 上書きする。"""
    import lib_state_store as store
    import lib_task_controller as ctl
    box = Box(tmp_path / "root", tasks=("t001", "t002"))
    (box.queue / "assignments").mkdir(exist_ok=True)
    (box.queue / "assignments" / "Ren").write_text(f"{MISSION}:t002\n")
    before = box.snapshot()
    with store.transaction(box.queue, op="probe", actor="test") as txn:
        with pytest.raises(h.ex.ControllerError) as e:
            ctl.reserve_task(txn, MISSION, "t001", "Ren", now="2026-10-01T10:00:00.000000Z")
    assert e.value.code == h.ex.TASK_NOT_ELIGIBLE and box.snapshot() == before
    with store.transaction(box.queue, op="probe", actor="test") as txn:
        ctx = ctl.reserve_task(txn, MISSION, "t001", "Ren", now="2026-10-01T10:00:00.000000Z",
                               foreign_slot_checked=True)
    assert box.slot("Ren") == f"{MISSION}:t001" and box.identity("Ren")["execution_id"] == ctx.execution_id


@pytest.mark.parametrize("name", ["ミナ", "Kai-codex", "worker_01", "A.B"])
def test_every_worker_name_the_old_pull_accepted_is_still_accepted(tmp_path, name):
    """名前は worker 名 (ポジション) で、英数字に限らない。Controller の agent 名の検査は今の pull と同じ規則だけ
    (枠のファイル名として使えるか)。監査ログの actor は門が `unknown` にするので、名前の中身は監査に出ない。"""
    box = Box(tmp_path / "root")
    res = out(box.pull(name))
    meta = box.card()
    assert meta["worker"] == name and meta["execution_agent"] == name
    assert box.record(res["execution_id"])["agent"] == name
    assert box.slot(name) == f"{MISSION}:t001"
    for r in box.audit_rows():
        assert r["actor"] == name or r["actor"] == "unknown"


def test_pull_overwrites_an_orphan_slot_of_a_released_task_but_not_a_live_one(tmp_path):
    box = Box(tmp_path / "root", tasks=("t001", "t002"))
    (box.queue / "assignments").mkdir(exist_ok=True)
    # t002 は pending (手放し済み) = 孤児: 今までどおり上書きされる
    (box.queue / "assignments" / "Ren").write_text(f"{MISSION}:t002\n")
    res = out(box.pull("Ren", "t001"))
    assert box.slot("Ren") == f"{MISSION}:t001" and box.identity("Ren")["execution_id"] == res["execution_id"]
    # 生きている別の task を指す枠は拒否される (exit 3・何も書かない)
    path = box.queue / "missions" / MISSION / "tasks" / "t002.md"
    path.write_text(path.read_text().replace("status: pending", "status: in_progress")
                    .replace("worker: null", "worker: Sora"))
    (box.queue / "assignments" / "Sora").write_text(f"{MISSION}:t002\n")
    (box.queue / "assignments" / "Sora.identity").write_text(json.dumps(
        {"mission": MISSION, "task": "t002", "worker": "Sora", "started_at": "2026-10-01T10:00:00.000000Z"}) + "\n")
    path.write_text(path.read_text().replace("started_at: null", "started_at: 2026-10-01T10:00:00.000000Z"))
    box.reset("t001")
    (box.queue / "assignments" / "Ren").write_text(f"{MISSION}:t002\n")
    before = {k: v for k, v in box.snapshot().items()}
    p = box.pull("Ren", "t001")
    assert p.returncode == 3 and p.stdout.strip() == "", (p.returncode, p.stderr)
    assert box.snapshot() == before


def test_dispatcher_visible_state_is_unchanged_the_slot_body_and_idle_busy_files(box):
    out(box.pull("Ren"))
    assert box.slot("Ren") == f"{MISSION}:t001"
    names = sorted(p.name for p in (box.queue / "assignments").iterdir())
    assert names == ["Ren", "Ren.identity"], names


# ---------------------------------------------------------------------------
# 8. 利用者に見えるエラー・監査・record に card の中身を出さない (secret を仕込む)
# ---------------------------------------------------------------------------

def test_no_secret_from_the_card_leaks_into_errors_audit_or_record(tmp_path):
    secret = h.SECRET

    def seed_secret(t):
        meta = h.sc.card("t001", title=f"title {secret}", needs_director_reason=f"reason {secret}")
        t.write_card(MISSION, "t001", meta, h.sc.body(desc=f"description {secret}"))
    box = Box(tmp_path / "root", seed_extra=seed_secret)
    res = box.pull("Ren")
    assert res.returncode == 0, res.stderr
    assert secret in res.stdout                                   # JSON は task の中身を渡す今までの契約 (欄は変えない)
    assert secret not in res.stderr
    xid = json.loads(res.stdout)["execution_id"]
    for p in (box.queue / "audit").glob("*.jsonl"):
        assert secret not in p.read_text()
    assert secret not in (box.queue / "missions" / MISSION / "executions" / f"{xid}.json").read_text()
    # 試行の欄が壊れた card (片方だけ): STATE_INVALID は固定コード + 位置だけ
    box.reset()
    path = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    path.write_text(path.read_text().replace("current_execution_id:", "x_current_execution_id:"))
    bad = box.pull("Ren")
    assert bad.returncode == 1 and bad.stdout.strip() == ""
    assert secret not in bad.stderr and h.last_error_code(bad.stderr) == "STATE_INVALID"
    for p in (box.queue / "audit").glob("*.jsonl"):
        assert secret not in p.read_text()
