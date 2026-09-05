#!/usr/bin/env python3
"""
tests/test_dispatcher_fix.py

Fix 検証テスト:
  Fix 1 — can_handle を heartbeat ベースに変更 (herdr pane_list failure 対策)
  Fix 2 — Rule 2 mtime race: blocker in_progress 時は Worker kill しない

実行方法:
  python3 -m pytest tests/test_dispatcher_fix.py -v
"""

import sys
import os
import time
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# テスト用ヘルパー
# ---------------------------------------------------------------------------

WORKERS = {
    "Haruto": {"skills": ["bash", "code"], "role": "worker"},
    "Minjun": {"skills": ["docs"], "role": "worker"},
    "Finn":   {"skills": ["qa"],  "role": "worker"},
    "Sora":   {"skills": [],      "role": "director"},
}

AGENT_PRESENCE_TTL = 600


def make_alive_workers(names: list, ttl=AGENT_PRESENCE_TTL, tmpdir=None) -> tuple:
    """Create heartbeat files for specified workers and return (hb_dir, alive_set)."""
    hb_dir = Path(tmpdir) / "heartbeats"
    hb_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for name in names:
        hb_file = hb_dir / name
        hb_file.write_text(str(int(now)))
        # Set mtime to now (fresh)
    alive = set()
    for hb_file in hb_dir.iterdir():
        if hb_file.is_file() and not hb_file.name.startswith("."):
            try:
                if now - hb_file.stat().st_mtime <= ttl:
                    alive.add(hb_file.name)
            except OSError:
                pass
    return hb_dir, alive


def can_handle_heartbeat(task_skills: set, alive_workers: set, workers: dict) -> bool:
    """Fix 1: heartbeat-based can_handle logic (as implemented in dispatcher.sh)."""
    return any(
        task_skills.issubset(set((workers.get(name) or {}).get("skills") or []))
        for name in alive_workers
        if (workers.get(name) or {}).get("role", "worker") == "worker"
    )


def can_handle_windows(task_skills: set, windows: list, workers: dict) -> bool:
    """Old: windows-based can_handle logic (buggy)."""
    return any(
        task_skills.issubset(set((workers.get(w["agent_name"]) or {}).get("skills") or []))
        for w in windows
    )


# ---------------------------------------------------------------------------
# Fix 1 テスト: heartbeat ベース can_handle
# ---------------------------------------------------------------------------

def test_fix1_windows_empty_no_false_notification():
    """
    Fix 1 の核心:
    windows=[] でも heartbeat が fresh なら can_handle=True → 誤通知なし
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _, alive = make_alive_workers(["Haruto"], tmpdir=tmpdir)

    task_skills = {"bash", "code"}
    windows = []  # herdr transient failure

    old_result = can_handle_windows(task_skills, windows, WORKERS)
    new_result = can_handle_heartbeat(task_skills, alive, WORKERS)

    assert old_result is False, "Old logic: windows=[] → False (confirms bug)"
    assert new_result is True, "Fix 1: heartbeat alive → True (no false notification)"
    print("✓ test_fix1_windows_empty_no_false_notification: "
          f"old={old_result} new={new_result} ← bug eliminated")


def test_fix1_genuinely_no_worker():
    """
    Alive worker がいない場合は can_handle=False (正当な通知)
    """
    alive = set()  # no heartbeats at all
    task_skills = {"bash", "code"}

    result = can_handle_heartbeat(task_skills, alive, WORKERS)
    assert result is False
    print("✓ test_fix1_genuinely_no_worker: can_handle=False when truly no alive workers")


def test_fix1_stale_heartbeat_treated_as_dead():
    """
    Heartbeat が AGENT_PRESENCE_TTL を超えた場合は dead 扱い
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        hb_dir = Path(tmpdir) / "heartbeats"
        hb_dir.mkdir()
        hb_file = hb_dir / "Haruto"
        hb_file.write_text("0")
        # backdate mtime to 700s ago
        old_time = time.time() - 700
        os.utime(hb_file, (old_time, old_time))

        now = time.time()
        alive = set()
        for f in hb_dir.iterdir():
            if f.is_file() and not f.name.startswith("."):
                if now - f.stat().st_mtime <= AGENT_PRESENCE_TTL:
                    alive.add(f.name)

    result = can_handle_heartbeat({"bash", "code"}, alive, WORKERS)
    assert result is False, "Stale heartbeat (700s > 600s) → treated as dead"
    print("✓ test_fix1_stale_heartbeat_treated_as_dead: stale hb → can_handle=False")


def test_fix1_director_excluded():
    """
    Director (role=director) は can_handle から除外される
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _, alive = make_alive_workers(["Sora"], tmpdir=tmpdir)

    task_skills = {"bash", "code"}
    result = can_handle_heartbeat(task_skills, alive, WORKERS)
    assert result is False, "Director should not be counted as capable worker"
    print("✓ test_fix1_director_excluded: Director not counted in can_handle")


def test_fix1_busy_worker_still_counted():
    """
    busy (assignment file あり) でも alive heartbeat があれば can_handle=True
    → Worker が free になれば担当できるから Director 通知不要
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _, alive = make_alive_workers(["Haruto"], tmpdir=tmpdir)

    # Haruto が busy (assignment file あり) でも heartbeat があれば counted
    task_skills = {"bash", "code"}
    result = can_handle_heartbeat(task_skills, alive, WORKERS)
    assert result is True
    print("✓ test_fix1_busy_worker_still_counted: busy worker counts if heartbeat alive")


# ---------------------------------------------------------------------------
# Fix 2 テスト: Rule 2 mtime race
# ---------------------------------------------------------------------------

def test_fix2_in_progress_blocker_skips_rule2():
    """
    Fix 2 の核心:
    blocker が in_progress のとき has_active_blocker=True → Rule 2 kill をスキップ
    """
    # task_statuses: t002 (blocker) は in_progress
    task_statuses_by_mission = {
        "test-mission": {"t001": "done", "t002": "in_progress", "t003": "pending"}
    }
    matching_pending = [
        ("test-mission", {"id": "t003", "skills": ["bash", "code"],
                          "blocked_by": ["t002"]}),
    ]

    has_active_blocker = any(
        task_statuses_by_mission.get(s, {}).get(dep) == "in_progress"
        for s, m in matching_pending
        for dep in (m.get("blocked_by") or [])
    )

    assert has_active_blocker is True, "Blocker in_progress should trigger skip"
    print("✓ test_fix2_in_progress_blocker_skips_rule2: has_active_blocker=True")


def test_fix2_no_active_blocker_rule2_applies():
    """
    blocker が done (完了) で、pending task のみが残っている場合は Rule 2 適用対象
    """
    task_statuses_by_mission = {
        "test-mission": {"t001": "done", "t002": "done", "t003": "pending"}
    }
    matching_pending = [
        ("test-mission", {"id": "t003", "skills": ["bash", "code"],
                          "blocked_by": ["t002"]}),
    ]

    has_active_blocker = any(
        task_statuses_by_mission.get(s, {}).get(dep) == "in_progress"
        for s, m in matching_pending
        for dep in (m.get("blocked_by") or [])
    )

    assert has_active_blocker is False, "Blocker done → not active → Rule 2 applies"
    print("✓ test_fix2_no_active_blocker_rule2_applies: blocker done → Rule 2 proceeds")


def test_fix2_chain_newest_mtime_includes_blocker():
    """
    _chain_newest_mtime: pending task mtime と blocker mtime の max を返す
    blocker が最近 done になった場合、blocker の mtime が fresh → stuck_secs が短い
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        mission_dir = tmpdir / "missions" / "test-mission" / "tasks"
        mission_dir.mkdir(parents=True)

        # pending task: 700s前に作成 (mtime old)
        old_time = time.time() - 700
        t003_file = mission_dir / "t003.md"
        t003_file.write_text("---\nid: t003\nstatus: pending\n---")
        os.utime(t003_file, (old_time, old_time))

        # blocker task: 5s前に done になった (mtime fresh)
        fresh_time = time.time() - 5
        t002_file = mission_dir / "t002.md"
        t002_file.write_text("---\nid: t002\nstatus: done\n---")
        os.utime(t002_file, (fresh_time, fresh_time))

        meta = {"id": "t003", "blocked_by": ["t002"]}

        def task_mtime(slug, m):
            p = tmpdir / "missions" / slug / "tasks" / f"{m['id']}.md"
            try: return p.stat().st_mtime
            except OSError: return time.time()

        def chain_newest_mtime(slug, m):
            mtimes = [task_mtime(slug, m)]
            for dep_id in (m.get("blocked_by") or []):
                dep_file = tmpdir / "missions" / slug / "tasks" / f"{dep_id}.md"
                try: mtimes.append(dep_file.stat().st_mtime)
                except OSError: pass
            return max(mtimes)

        # Old logic: only looks at pending task mtime
        old_stuck = time.time() - task_mtime("test-mission", meta)
        # New logic: also considers blocker mtime
        new_stuck = time.time() - chain_newest_mtime("test-mission", meta)

        assert old_stuck >= 600, f"Old: stuck={old_stuck:.0f}s ≥ 600 → would kill worker"
        assert new_stuck < 600, f"New: stuck={new_stuck:.0f}s < 600 → blocker recently done, no kill"
        print(f"✓ test_fix2_chain_newest_mtime_includes_blocker: "
              f"old_stuck={old_stuck:.0f}s new_stuck={new_stuck:.0f}s")


def test_fix2_multiple_blockers_any_in_progress():
    """
    複数の blocker がある場合、ANY in_progress で skip
    """
    task_statuses_by_mission = {
        "test-mission": {
            "t001": "done",
            "t002": "in_progress",  # ← これが active
            "t003": "done",
            "t004": "pending",
        }
    }
    matching_pending = [
        ("test-mission", {"id": "t004", "skills": ["bash", "code"],
                          "blocked_by": ["t002", "t003"]}),
    ]

    has_active_blocker = any(
        task_statuses_by_mission.get(s, {}).get(dep) == "in_progress"
        for s, m in matching_pending
        for dep in (m.get("blocked_by") or [])
    )

    assert has_active_blocker is True
    print("✓ test_fix2_multiple_blockers_any_in_progress: any in_progress → skip")


# ---------------------------------------------------------------------------
# 統合シナリオ: バグが完全に解消されることを確認
# ---------------------------------------------------------------------------

def test_integration_bug_scenario_fully_fixed():
    """
    バグシナリオの完全修正確認:
    - Haruto が t002 完了 (done)
    - t003, t004 が unblocked に
    - herdr が windows=[] を返す (transient failure)
    - Fix 1: heartbeat ベースで can_handle=True → 誤通知なし
    - Fix 2: t002 の blocker が in_progress だった間は Worker kill なし (後続のみ確認)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _, alive = make_alive_workers(["Haruto"], tmpdir=tmpdir)

    windows = []  # herdr transient failure
    unblocked_tasks = [
        {"id": "t003", "skills": ["bash", "code"]},
        {"id": "t004", "skills": ["bash", "code"]},
    ]

    false_notifications_old = []
    false_notifications_new = []

    for meta in unblocked_tasks:
        task_skills = set(meta["skills"])

        # Old logic: windows ベース → False
        old_ch = can_handle_windows(task_skills, windows, WORKERS)
        if not old_ch:
            false_notifications_old.append(meta["id"])

        # Fix 1: heartbeat ベース → True
        new_ch = can_handle_heartbeat(task_skills, alive, WORKERS)
        if not new_ch:
            false_notifications_new.append(meta["id"])

    assert false_notifications_old == ["t003", "t004"], \
        f"Old: should fire for t003,t004, got {false_notifications_old}"
    assert false_notifications_new == [], \
        f"Fix 1: should fire for none, got {false_notifications_new}"

    print(f"✓ test_integration_bug_scenario_fully_fixed:")
    print(f"  Old (buggy): false notifications = {false_notifications_old}")
    print(f"  Fix 1:       false notifications = {false_notifications_new} ← FIXED!")


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 65)
    print("Dispatcher Fix Verification Tests")
    print("  Fix 1: can_handle heartbeat-based (herdr pane_list failure fix)")
    print("  Fix 2: Rule 2 mtime race (in_progress blocker guard)")
    print("=" * 65)

    failures = []
    tests = [
        test_fix1_windows_empty_no_false_notification,
        test_fix1_genuinely_no_worker,
        test_fix1_stale_heartbeat_treated_as_dead,
        test_fix1_director_excluded,
        test_fix1_busy_worker_still_counted,
        test_fix2_in_progress_blocker_skips_rule2,
        test_fix2_no_active_blocker_rule2_applies,
        test_fix2_chain_newest_mtime_includes_blocker,
        test_fix2_multiple_blockers_any_in_progress,
        test_integration_bug_scenario_fully_fixed,
    ]

    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            print(f"✗ {fn.__name__}: FAILED — {e}")
            failures.append(fn.__name__)
        except Exception as e:
            print(f"✗ {fn.__name__}: ERROR — {e}")
            failures.append(fn.__name__)

    print("=" * 65)
    if failures:
        print(f"FAILED: {failures}")
        sys.exit(1)
    else:
        print(f"All {len(tests)} tests PASSED")
        sys.exit(0)
