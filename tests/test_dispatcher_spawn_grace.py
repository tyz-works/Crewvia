#!/usr/bin/env python3
"""
tests/test_dispatcher_spawn_grace.py

Regression テスト: spawn 猶予期間 (t010, mission 20260908-launch-reliability)

## 背景

`scripts/dispatcher.sh` の `shutdown_idle_workers()` (旧 L850-864) と
`dispatch()` の no-task 分岐 (旧 L1077-1084) は、いずれも
`queue/assignments/<agent>` の有無だけで idle 判定していた。

Worker は spawn 後 TUI 起動 → kickoff → `plan.sh pull` まで実測 10〜30 秒
かかるが、その間 assignment file は存在しないため、dispatcher の 5 秒
ポーリングが先に来ると「タスクなし、shutdown」を送って即 kill していた
(spawn 直後の Worker が pull 前に消える) — `start.sh` が spawn ごとに
生成する約 48KB の prompt が丸ごと無駄になる。

## 修正

`registry/mux/<window>.json` (Herdr の spawn キャッシュ) の `created_at`
から `SPAWN_GRACE_SECONDS` (デフォルト 90s) 以内の Worker は idle 判定を
スキップする。キャッシュが無い場合 (tmux backend / 欠損) は
`registry/mux/<window>.firstseen` に dispatcher 自身が初検知時刻を記録し、
それを基準に同じ猶予を与える (無期限延命にはしない)。

両呼び出し箇所 (`shutdown_idle_workers()` と `dispatch()` 内 no-task 分岐)
に同じ `in_spawn_grace()` を適用した。

## テストケース

  test_created_at_within_grace_not_killed        — 猶予内 (herdr cache) → kill しない
  test_created_at_past_grace_killed               — 猶予超過 (herdr cache) → 従来どおり kill
  test_tmux_fallback_first_seen_then_expires       — cache 無し (tmux) → firstseen 記録 → 時間経過で kill
  test_corrupt_created_at_json_falls_back_to_firstseen — 壊れた json → firstseen フォールバック
  test_shutdown_idle_workers_path_respects_grace   — L850 経路 (shutdown_idle_workers) の回帰
  test_dispatch_no_task_path_respects_grace        — L1077 経路 (dispatch no-task 分岐) の回帰
  test_worker_with_task_never_touches_grace        — task 保持中 Worker は is_idle=False で影響なし

実行方法:
  python3 -m pytest tests/test_dispatcher_spawn_grace.py -v
"""

import json
import time
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# dispatcher.sh からの再実装 (heredoc 埋め込みの python を直接 import できない
# ため、既存テスト tests/test_dispatcher_fix.py / test_dispatcher_worker_vanish.py
# と同じ方式で、対象ロジックを忠実に複製する)。
# ---------------------------------------------------------------------------

SPAWN_GRACE_SECONDS = 90  # dispatcher.sh と同値


def _mux_created_at(state_json_dir: Path, window_target: str):
    p = state_json_dir / f'{window_target}.json'
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        ts = data.get('created_at')
        if not ts:
            return None
        dt = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _spawn_time_fallback(state_json_dir: Path, window_target: str) -> float:
    """t015 F3 fix: on a persistent write failure, lean toward grace-expired
    (return an already-past epoch) instead of `now` (which used to grant
    indefinite grace — `in_spawn_grace()` would read True every cycle)."""
    p = state_json_dir / f'{window_target}.firstseen'
    try:
        return float(p.read_text().strip())
    except Exception:
        pass
    now = time.time()
    try:
        state_json_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(str(now))
        return now
    except OSError:
        return 0.0


def _parse_spawn_grace_seconds(env_value, default=SPAWN_GRACE_SECONDS) -> int:
    """t015 F4 fix: scripts/dispatcher.sh module-level `int(os.environ.get(...))`
    used to raise ValueError (killing the whole daemon at import time, before
    log()/LOG_FILE exist) on a non-numeric CREWVIA_SPAWN_GRACE. Falls back to
    the default instead."""
    try:
        return int(env_value)
    except (TypeError, ValueError):
        return default


def in_spawn_grace(state_json_dir: Path, window_target: str, grace=SPAWN_GRACE_SECONDS) -> bool:
    created = _mux_created_at(state_json_dir, window_target)
    if created is None:
        created = _spawn_time_fallback(state_json_dir, window_target)
    return (time.time() - created) < grace


def _write_herdr_cache(state_json_dir: Path, window_target: str, created_at_epoch: float):
    state_json_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at_epoch))
    (state_json_dir / f'{window_target}.json').write_text(
        json.dumps({"tab_id": "wP:t1", "pane_id": "wP:p1", "backend": "herdr", "created_at": ts})
    )


# ---------------------------------------------------------------------------
# 両呼び出し箇所を模した orchestration (kill/send を記録するモック付き)
# ---------------------------------------------------------------------------

class MockMux:
    def __init__(self, kill_ok=True):
        self.sent = []
        self.killed = []
        self._kill_ok = kill_ok

    def send(self, target, msg):
        self.sent.append((target, msg))
        return True

    def kill(self, target):
        if self._kill_ok:
            self.killed.append(target)
        return self._kill_ok


def tmux_kill_window_sim(state_json_dir: Path, target: str, mux: MockMux):
    """scripts/dispatcher.sh tmux_kill_window() の複製 (t015 F2 fix).

    Deletes `<target>.firstseen` after a *successful* kill so a Worker
    respawned under the same name (crewvia reuses names) gets a fresh
    grace window instead of inheriting this dead Worker's stale marker."""
    ok = mux.kill(target)
    if ok:
        firstseen = state_json_dir / f'{target}.firstseen'
        try:
            firstseen.unlink(missing_ok=True)
        except OSError:
            pass
    return ok


def shutdown_idle_workers_sim(state_json_dir, windows, assignment_files, mux: MockMux):
    """scripts/dispatcher.sh shutdown_idle_workers() の複製 (L850 経路)."""
    for window in windows:
        agent_name = window['agent_name']
        target = window['window_target']
        is_idle = agent_name not in assignment_files
        if is_idle:
            if in_spawn_grace(state_json_dir, target):
                continue  # skip shutdown — spawn grace
            mux.send(target, 'タスクなし、shutdown')
            tmux_kill_window_sim(state_json_dir, target, mux)


def dispatch_no_task_branch_sim(state_json_dir, agent_name, target, has_any, has_in_progress, mux: MockMux):
    """scripts/dispatcher.sh dispatch() 内 no-task 分岐の複製 (L1077 経路)."""
    if not has_any and not has_in_progress:
        if in_spawn_grace(state_json_dir, target):
            return  # skip shutdown — spawn grace
        mux.send(target, 'タスクなし、shutdown')
        tmux_kill_window_sim(state_json_dir, target, mux)


def rule2_blocked_stuck_sim(state_json_dir, target, stuck_secs, threshold, mux: MockMux):
    """scripts/dispatcher.sh Rule 2 (blocked-stuck) 分岐の複製 (t015 F1 fix:
    3rd kill path, previously had no in_spawn_grace() guard at all)."""
    if stuck_secs < threshold:
        return
    if in_spawn_grace(state_json_dir, target):
        return  # skip shutdown — spawn grace
    mux.send(target, 'タスクなし、shutdown')
    tmux_kill_window_sim(state_json_dir, target, mux)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_state_dir():
    d = Path(tempfile.mkdtemp())
    yield d / 'mux'
    shutil.rmtree(d, ignore_errors=True)


def test_created_at_within_grace_not_killed(tmp_state_dir):
    """spawn 直後 (created_at = now-10s) の Worker は kill 対象外."""
    _write_herdr_cache(tmp_state_dir, 'Sofia-worker', time.time() - 10)
    assert in_spawn_grace(tmp_state_dir, 'Sofia-worker') is True


def test_created_at_past_grace_killed(tmp_state_dir):
    """猶予 (90s) を超えた無タスク Worker は従来どおり kill 対象 (回帰なし)."""
    _write_herdr_cache(tmp_state_dir, 'Sofia-worker', time.time() - 200)
    assert in_spawn_grace(tmp_state_dir, 'Sofia-worker') is False


def test_tmux_fallback_first_seen_then_expires(tmp_state_dir):
    """created_at cache が無い (tmux backend) 場合、初検知時刻を firstseen に
    記録して同じ猶予を与える。無期限延命にはならない。"""
    # 1st sighting: no cache at all -> records firstseen=now -> within grace
    assert in_spawn_grace(tmp_state_dir, 'Haruto-worker') is True
    fs_path = tmp_state_dir / 'Haruto-worker.firstseen'
    assert fs_path.exists()

    # Simulate SPAWN_GRACE_SECONDS+ elapsed since first sighting by rewriting
    # the recorded timestamp into the past (as if this dispatch cycle ran
    # long after the first one).
    fs_path.write_text(str(time.time() - (SPAWN_GRACE_SECONDS + 5)))
    assert in_spawn_grace(tmp_state_dir, 'Haruto-worker') is False


def test_corrupt_created_at_json_falls_back_to_firstseen(tmp_state_dir):
    """壊れた herdr cache は None 扱いになり firstseen フォールバックに落ちる
    (例外で丸ごと落ちない)."""
    tmp_state_dir.mkdir(parents=True, exist_ok=True)
    (tmp_state_dir / 'Finn-worker.json').write_text('{not json')
    assert in_spawn_grace(tmp_state_dir, 'Finn-worker') is True
    assert (tmp_state_dir / 'Finn-worker.firstseen').exists()


def test_shutdown_idle_workers_path_respects_grace(tmp_state_dir):
    """L850 shutdown_idle_workers() 経路: 猶予内は kill せず、猶予超過は kill."""
    _write_herdr_cache(tmp_state_dir, 'Sofia-worker', time.time() - 10)   # fresh spawn
    _write_herdr_cache(tmp_state_dir, 'Omar-worker', time.time() - 200)   # stale idle
    windows = [
        {'agent_name': 'Sofia', 'window_target': 'Sofia-worker'},
        {'agent_name': 'Omar', 'window_target': 'Omar-worker'},
    ]
    mux = MockMux()
    shutdown_idle_workers_sim(tmp_state_dir, windows, assignment_files=set(), mux=mux)
    assert 'Sofia-worker' not in mux.killed  # spawn grace protected it
    assert 'Omar-worker' in mux.killed        # genuinely idle -> reaped as before


def test_dispatch_no_task_path_respects_grace(tmp_state_dir):
    """L1077 no-task 分岐: 猶予内は kill せず、猶予超過は kill (回帰なし)."""
    mux_fresh = MockMux()
    _write_herdr_cache(tmp_state_dir, 'Sofia-worker', time.time() - 10)
    dispatch_no_task_branch_sim(
        tmp_state_dir, 'Sofia', 'Sofia-worker',
        has_any=False, has_in_progress=False, mux=mux_fresh,
    )
    assert mux_fresh.killed == []

    mux_stale = MockMux()
    _write_herdr_cache(tmp_state_dir, 'Omar-worker', time.time() - 200)
    dispatch_no_task_branch_sim(
        tmp_state_dir, 'Omar', 'Omar-worker',
        has_any=False, has_in_progress=False, mux=mux_stale,
    )
    assert mux_stale.killed == ['Omar-worker']


def test_worker_with_task_never_touches_grace(tmp_state_dir):
    """task を保持している (is_idle=False / has_in_progress=True) Worker は
    grace 判定に到達すらしない — 既存の「実際に task を持つ Worker は
    影響を受けない」要件の回帰確認。"""
    # shutdown_idle_workers path: assignment file exists -> is_idle False
    windows = [{'agent_name': 'Sofia', 'window_target': 'Sofia-worker'}]
    mux = MockMux()
    shutdown_idle_workers_sim(tmp_state_dir, windows, assignment_files={'Sofia'}, mux=mux)
    assert mux.killed == []
    assert not (tmp_state_dir / 'Sofia-worker.json').exists()  # grace never consulted

    # dispatch no-task branch: has_in_progress=True short-circuits before grace check
    mux2 = MockMux()
    dispatch_no_task_branch_sim(
        tmp_state_dir, 'Omar', 'Omar-worker',
        has_any=False, has_in_progress=True, mux=mux2,
    )
    assert mux2.killed == []


# ---------------------------------------------------------------------------
# t015 regression tests (QA FAIL on PR#190 + Seo review F1/F3/F4)
# ---------------------------------------------------------------------------

def test_kill_deletes_firstseen_marker(tmp_state_dir):
    """t015 F2 (QA FAIL, root cause): a successful kill must remove the
    `.firstseen` marker so it cannot outlive the Worker it was recorded for."""
    in_spawn_grace(tmp_state_dir, 'Haruto-worker')  # records firstseen=now
    fs_path = tmp_state_dir / 'Haruto-worker.firstseen'
    assert fs_path.exists()

    mux = MockMux()
    tmux_kill_window_sim(tmp_state_dir, 'Haruto-worker', mux)
    assert 'Haruto-worker' in mux.killed
    assert not fs_path.exists()


def test_kill_failure_does_not_delete_firstseen(tmp_state_dir):
    """If the underlying mux kill fails (window may still be alive), the
    grace marker must be left alone — no reason to reset a live Worker's
    grace window on a failed kill attempt."""
    in_spawn_grace(tmp_state_dir, 'Haruto-worker')
    fs_path = tmp_state_dir / 'Haruto-worker.firstseen'
    assert fs_path.exists()

    mux = MockMux(kill_ok=False)
    tmux_kill_window_sim(tmp_state_dir, 'Haruto-worker', mux)
    assert fs_path.exists()


def test_respawn_after_kill_gets_fresh_grace(tmp_state_dir):
    """t015 core regression (QA FAIL repro): kill a Worker, then respawn a
    Worker under the SAME name (crewvia reuses names) — the new spawn must
    get the full SPAWN_GRACE_SECONDS window, not zero.

    Before the fix: the stale `.firstseen` from the killed Worker survived,
    so the respawned Worker was born already "past grace" and got killed
    within one dispatch cycle (QA measured 151616Z spawn -> 151649Z kill,
    33s, no spawn_grace log line)."""
    name = 'Seo-worker'

    # --- Worker #1: spawned long ago (its firstseen is already past
    # SPAWN_GRACE_SECONDS — e.g. it did real work for 30+ minutes before
    # finally going idle), then genuinely reaped for being idle too long.
    fs_path = tmp_state_dir / f'{name}.firstseen'
    tmp_state_dir.mkdir(parents=True, exist_ok=True)
    fs_path.write_text(str(time.time() - (SPAWN_GRACE_SECONDS + 1200)))
    assert in_spawn_grace(tmp_state_dir, name) is False  # correctly past grace

    mux1 = MockMux()
    tmux_kill_window_sim(tmp_state_dir, name, mux1)
    assert name in mux1.killed
    assert not fs_path.exists()

    # --- Worker #2: respawned under the identical name. Before the fix,
    # `in_spawn_grace()` would have read the dead Worker's leftover
    # `.firstseen` (already old) and returned False immediately — zero
    # grace for a Worker that just started.
    windows = [{'agent_name': 'Seo', 'window_target': name}]
    mux2 = MockMux()
    shutdown_idle_workers_sim(tmp_state_dir, windows, assignment_files=set(), mux=mux2)
    assert name not in mux2.killed, (
        "respawned Worker under a reused name was killed with zero grace "
        "-- .firstseen from the previous (killed) Worker was not cleared"
    )


def test_grace_period_still_expires_normally_after_respawn(tmp_state_dir):
    """The fix must not create indefinite protection: a respawned Worker
    that genuinely sits idle past SPAWN_GRACE_SECONDS is still reaped."""
    name = 'Seo-worker'
    in_spawn_grace(tmp_state_dir, name)
    tmux_kill_window_sim(tmp_state_dir, name, MockMux())

    # Respawn, then simulate SPAWN_GRACE_SECONDS+ having elapsed since the
    # new firstseen was recorded.
    in_spawn_grace(tmp_state_dir, name)  # records fresh firstseen=now
    fs_path = tmp_state_dir / f'{name}.firstseen'
    fs_path.write_text(str(time.time() - (SPAWN_GRACE_SECONDS + 5)))

    windows = [{'agent_name': 'Seo', 'window_target': name}]
    mux = MockMux()
    shutdown_idle_workers_sim(tmp_state_dir, windows, assignment_files=set(), mux=mux)
    assert name in mux.killed


def test_kill_does_not_touch_herdr_created_at_cache(tmp_state_dir):
    """t015 (QA prerequisite (c)): deleting `.firstseen` on kill must never
    touch `<target>.json` (herdr's created_at cache, rewritten by lib_mux.py
    on every spawn) -- the two files are independent."""
    _write_herdr_cache(tmp_state_dir, 'Arjun-worker', time.time() - 5)
    json_path = tmp_state_dir / 'Arjun-worker.json'
    assert json_path.exists()

    mux = MockMux()
    tmux_kill_window_sim(tmp_state_dir, 'Arjun-worker', mux)
    assert json_path.exists()  # untouched
    assert json.loads(json_path.read_text())['backend'] == 'herdr'


def test_rule2_blocked_stuck_respects_grace(tmp_state_dir):
    """t015 F1 (Seo review, MEDIUM): the 3rd kill path (Rule 2 /
    blocked-stuck) previously had no in_spawn_grace() guard at all -- a
    Worker pre-spawned for a task whose blocker looked stale from cycle one
    was killed immediately, same real damage as the bug this PR fixes for
    the other two paths."""
    name = 'Priya-worker'
    _write_herdr_cache(tmp_state_dir, name, time.time() - 10)  # fresh spawn

    mux = MockMux()
    rule2_blocked_stuck_sim(
        tmp_state_dir, name,
        stuck_secs=700, threshold=600,  # over BLOCKED_STUCK_THRESHOLD
        mux=mux,
    )
    assert name not in mux.killed  # spawn grace protects it

    # Existing behavior preserved: past grace AND past threshold -> killed.
    _write_herdr_cache(tmp_state_dir, name, time.time() - 200)
    mux2 = MockMux()
    rule2_blocked_stuck_sim(
        tmp_state_dir, name,
        stuck_secs=700, threshold=600,
        mux=mux2,
    )
    assert name in mux2.killed

    # Under threshold -> never reaches the grace check, never killed.
    mux3 = MockMux()
    rule2_blocked_stuck_sim(
        tmp_state_dir, name,
        stuck_secs=100, threshold=600,
        mux=mux3,
    )
    assert name not in mux3.killed


def test_write_failure_treated_as_grace_expired(tmp_state_dir, monkeypatch):
    """t015 F3 (Seo review, LOW): if registry/mux is not writable, grace must
    NOT become indefinite (old code returned `now` every cycle -> always
    "within grace"). It must lean toward "expired" instead."""
    import pathlib

    real_write_text = pathlib.Path.write_text

    def failing_write_text(self, *a, **kw):
        if self.name.endswith('.firstseen'):
            raise OSError("read-only filesystem (simulated)")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, 'write_text', failing_write_text)

    ts = _spawn_time_fallback(tmp_state_dir, 'Wei-worker')
    assert ts == 0.0
    assert (time.time() - ts) >= SPAWN_GRACE_SECONDS  # in_spawn_grace() -> False


def test_parse_spawn_grace_env_invalid_falls_back_to_default():
    """t015 F4 (Seo review, LOW): a non-numeric CREWVIA_SPAWN_GRACE must not
    crash the dispatcher at import time -- it must fall back to the default."""
    assert _parse_spawn_grace_seconds('90s') == SPAWN_GRACE_SECONDS
    assert _parse_spawn_grace_seconds(None) == SPAWN_GRACE_SECONDS
    assert _parse_spawn_grace_seconds('120') == 120


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
