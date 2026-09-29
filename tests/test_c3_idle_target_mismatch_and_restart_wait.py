#!/usr/bin/env python3
"""tests/test_c3_idle_target_mismatch_and_restart_wait.py — C3 (mission 20260930-ops-gaps-c / t009)

## (1) TARGET_DIR が合わない task しか無い idle Worker は退役させる

2026-09-29、Minerva の t018 (skills [review]、target_dir = Minerva、blocked) が
「skill が合う残り task」に数えられ、review Worker (TARGET_DIR が別) が task も assignment も
無いのに残り続けた (dispatcher.sh の #21 分岐が「退役させず待機」)。
TARGET_DIR が合わない task は、その Worker が永久に取れない — 待つ理由が無い。

`dispatch()` を本物の dispatcher.sh の python で 1 サイクル回す (tests/test_dispatcher_
retirement_exclusion.py と同じ方式。mux はフェイク、queue / registry は使い捨て)。

## (2) sync-main-checkout.sh は restart 後、新しい世代の heartbeat を待ってから状態を出す

2026-09-28 21:11、restart 直後の状態行が記録上の旧 pid で `recorded_instance_alive=False`
(15 秒後は新 pid で正常) だった。`wait_for_new_heartbeat()` の単体 (偽の時計) と、
sync-main-checkout.sh を本物の heartbeat 読み取りで走らせる結合の両方を張る。
restart の実行だけスタブ (本物の mux には触れない)。

実行: python3 -m pytest tests/test_c3_idle_target_mismatch_and_restart_wait.py -v
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent / "scripts"))

import lib_daemon_watch as w  # noqa: E402
import lib_retirement  # noqa: E402
import lib_worker_target  # noqa: E402
import test_dispatcher_retirement_exclusion as base  # noqa: E402
import test_main_checkout_sync as sync_base  # noqa: E402

AGENT = base.AGENT
SLUG = base.SLUG
OTHER_REPO = "/somewhere/else/other-repo"
MY_REPO = "/somewhere/else/my-repo"


# ---------------------------------------------------------------------------
# (1) dispatcher
# ---------------------------------------------------------------------------

def _card(task_id, *, status, blocked_by="[]", target="null", worker="null", skills="[code]"):
    return (f"---\nid: {task_id}\ntitle: {task_id}\nskills: {skills}\npriority: high\n"
            f"status: {status}\nblocked_by: {blocked_by}\ntarget_dir: {target}\n"
            f"worker: {worker}\nstarted_at: null\ncompleted_at: null\n---\n\n"
            "## Description\nwork\n\n## Result\n")


def _repo_with_blocked_target_task(tmp_path, *, worker_target, task_target, record=True):
    """idle Worker (AGENT) と、blocked で target_dir 付きの task t001 だけがある queue。

    t001 は t002 (別 Worker が実行中) を待つ = Minerva t018 / t017 と同じ形。
    """
    root = base._build_repo(tmp_path)
    tasks = root / "queue" / "missions" / SLUG / "tasks"
    (tasks / "t001.md").write_text(
        _card("t001", status="pending", blocked_by="[t002]", target=task_target))
    (tasks / "t002.md").write_text(
        _card("t002", status="in_progress", worker="SomeoneElse", target=task_target))
    if record:
        lib_worker_target.write_record(root / "registry", AGENT, worker_target)
    return root


def _cycle(root):
    mux = base.FakeMux([base.WINDOW])
    base._load_dispatcher(root, mux)["dispatch"]()
    return mux


def _retirement_requested(root):
    return lib_retirement.request_path(root / "registry", AGENT).exists()


def test_red_idle_worker_with_only_mismatched_blocked_task_is_retired(tmp_path):
    """修正前は退役を依頼されない (「退役させず待機」)。"""
    root = _repo_with_blocked_target_task(
        tmp_path, worker_target=MY_REPO, task_target=OTHER_REPO)
    _cycle(root)
    assert _retirement_requested(root), (
        "取れない task (TARGET_DIR 不一致) しか無い idle Worker の退役が依頼されていない")


def test_worker_waiting_for_its_own_target_dir_task_is_not_retired(tmp_path):
    """対照 (#21 の回帰なし): TARGET_DIR が合う blocked task を待つ Worker は殺さない。"""
    root = _repo_with_blocked_target_task(
        tmp_path, worker_target=MY_REPO, task_target=MY_REPO)
    _cycle(root)
    assert not _retirement_requested(root), "自分の TARGET_DIR の task を待つ Worker を退役させた"


def test_worker_without_a_target_record_keeps_waiting(tmp_path):
    """記録が無い (PR3 より前に起動した) Worker は、task が合わないと確定できない。
    観測できなかったものを根拠に退役しない。"""
    root = _repo_with_blocked_target_task(
        tmp_path, worker_target=MY_REPO, task_target=OTHER_REPO, record=False)
    _cycle(root)
    assert not _retirement_requested(root)


def test_unreadable_target_record_keeps_waiting(tmp_path):
    root = _repo_with_blocked_target_task(
        tmp_path, worker_target=MY_REPO, task_target=OTHER_REPO, record=False)
    rec = lib_worker_target.record_path(root / "registry", AGENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text("{ not json")
    _cycle(root)
    assert not _retirement_requested(root)


def test_worker_still_holding_a_card_is_not_retired(tmp_path):
    """has_in_progress の保護は変わらない: 自分の in_progress card がある Worker は退役させない。"""
    root = _repo_with_blocked_target_task(
        tmp_path, worker_target=MY_REPO, task_target=OTHER_REPO)
    tasks = root / "queue" / "missions" / SLUG / "tasks"
    (tasks / "t002.md").write_text(
        _card("t002", status="in_progress", worker=AGENT, target=MY_REPO))
    _cycle(root)
    assert not _retirement_requested(root)


def test_mixed_takeable_and_mismatched_tasks_keep_the_worker(tmp_path):
    """takeable な blocked task が 1 つでもあれば (不一致が他にあっても) 待つ。"""
    root = _repo_with_blocked_target_task(
        tmp_path, worker_target=MY_REPO, task_target=OTHER_REPO)
    tasks = root / "queue" / "missions" / SLUG / "tasks"
    (tasks / "t003.md").write_text(
        _card("t003", status="pending", blocked_by="[t002]", target=MY_REPO))
    _cycle(root)
    assert not _retirement_requested(root)


def test_no_task_at_all_is_still_retired(tmp_path):
    """既存の no-task 退役 (branch を 1 つにまとめた) が壊れていない。"""
    root = base._build_repo(tmp_path)
    (root / "queue" / "missions" / SLUG / "tasks" / "t001.md").write_text(
        _card("t001", status="done"))
    (root / "queue" / "missions" / SLUG / "tasks" / "t002.md").write_text(
        _card("t002", status="pending", skills="[docs]"))
    _cycle(root)
    assert _retirement_requested(root)


def test_the_log_no_longer_claims_a_director_request_it_did_not_make(tmp_path):
    root = _repo_with_blocked_target_task(
        tmp_path, worker_target=MY_REPO, task_target=OTHER_REPO)
    _cycle(root)
    log = (root / "dispatcher.log").read_text()
    assert "Director に起動要求済み" not in log
    assert "退役を依頼" in log


# ---------------------------------------------------------------------------
# (2) wait_for_new_heartbeat — 単体 (偽の時計)
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _write_hb(registry, name, pid, generation, updated_at):
    d = w.daemons_dir(registry)
    d.mkdir(parents=True, exist_ok=True)
    w.write_json_atomic(w.heartbeat_path(registry, name), {
        "daemon": name, "pid": pid, "generation": generation, "updated_at": updated_at})


def _live_pid():
    """生きている (この) プロセスの pid と世代。"""
    pid = os.getpid()
    return pid, w.process_generation(pid)


def _wait(registry, name, before, since, timeout, clock, on_sleep=None):
    def sleep(s):
        clock.sleep(s)
        if on_sleep:
            on_sleep(clock)
    return w.wait_for_new_heartbeat(registry, name, before=before, since=since,
                                    timeout=timeout, poll=1.0,
                                    now=clock.now, sleep=sleep)


def test_old_dead_heartbeat_times_out(tmp_path):
    """restart 直後は旧世代の記録 (dead pid) のまま。新しいものが無ければ None (= 失敗)。"""
    reg = tmp_path / "registry"
    _write_hb(reg, "dispatcher", 2 ** 22 + 12345, "x:1", 1000.0)
    before = w.heartbeat_identity(reg, "dispatcher")
    clock = Clock()
    assert _wait(reg, "dispatcher", before, since=1000.0, timeout=5, clock=clock) is None
    assert clock.t >= 1005.0


def test_new_generation_heartbeat_is_returned_once_it_appears(tmp_path):
    reg = tmp_path / "registry"
    _write_hb(reg, "dispatcher", 2 ** 22 + 12345, "x:1", 1000.0)
    before = w.heartbeat_identity(reg, "dispatcher")
    pid, gen = _live_pid()

    def appear(clock):
        if clock.t >= 1003.0:
            _write_hb(reg, "dispatcher", pid, gen, clock.t)

    hb = _wait(reg, "dispatcher", before, since=1000.0, timeout=30, clock=Clock(), on_sleep=appear)
    assert hb is not None and hb["pid"] == pid


def test_same_instance_beating_again_is_not_a_new_generation(tmp_path):
    """restart していない (同じ pid・世代) 記録の更新は「新しい世代」ではない。"""
    reg = tmp_path / "registry"
    pid, gen = _live_pid()
    _write_hb(reg, "dispatcher", pid, gen, 1000.0)
    before = w.heartbeat_identity(reg, "dispatcher")
    _write_hb(reg, "dispatcher", pid, gen, 1004.0)
    assert _wait(reg, "dispatcher", before, since=1000.0, timeout=3, clock=Clock()) is None


def test_heartbeat_older_than_the_restart_is_not_new(tmp_path):
    reg = tmp_path / "registry"
    pid, gen = _live_pid()
    _write_hb(reg, "dispatcher", pid, gen, 990.0)
    assert _wait(reg, "dispatcher", "none", since=1000.0, timeout=3, clock=Clock()) is None


def test_new_heartbeat_whose_instance_is_dead_is_not_accepted(tmp_path):
    reg = tmp_path / "registry"
    _write_hb(reg, "dispatcher", 2 ** 22 + 999, "x:2", 1002.0)
    assert _wait(reg, "dispatcher", "none", since=1000.0, timeout=3, clock=Clock()) is None


def test_no_heartbeat_before_and_a_fresh_one_after_is_accepted(tmp_path):
    reg = tmp_path / "registry"
    pid, gen = _live_pid()
    _write_hb(reg, "dispatcher", pid, gen, 1001.0)
    assert _wait(reg, "dispatcher", "none", since=1000.0, timeout=3, clock=Clock()) is not None


def test_cli_wait_heartbeat_exit_codes(tmp_path):
    repo = tmp_path / "repo"
    reg = repo / "registry"
    pid, gen = _live_pid()
    _write_hb(reg, "watchdog", pid, gen, time.time() + 5)
    base_cmd = [sys.executable, str(TESTS_DIR.parent / "scripts" / "lib_daemon_watch.py")]
    ok = subprocess.run(base_cmd + ["wait-heartbeat", "watchdog", "--repo-root", str(repo),
                                    "--before", "none", "--since", str(time.time() - 5),
                                    "--timeout", "2"], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    late = subprocess.run(base_cmd + ["wait-heartbeat", "watchdog", "--repo-root", str(repo),
                                      "--before", f"{pid} {gen}", "--since", str(time.time() - 5),
                                      "--timeout", "1"], capture_output=True, text=True)
    assert late.returncode == 1 and "timeout" in late.stderr


# ---------------------------------------------------------------------------
# (2) sync-main-checkout.sh — 結合 (heartbeat 読み取りは本物、restart の実行だけスタブ)
# ---------------------------------------------------------------------------

_HYBRID_LIB = '''#!/usr/bin/env python3
"""restart / restart-needed / restart-targets だけスタブ、heartbeat-id / wait-heartbeat /
status は本物の lib_daemon_watch に委ねる。restart は「旧世代の記録を残したまま、
STUB_NEW_HB_DELAY 秒後に新世代の heartbeat を書く」(遅れて書かないなら書かない) を再現する。
本物の Mux には触れない。"""
import os, subprocess, sys, time
sys.path.insert(0, os.environ["REAL_SCRIPTS"])
import lib_daemon_watch as w

argv = sys.argv[1:]
cmd = argv[0]
if cmd == "restart-needed":
    print("true" if argv[1] == os.environ.get("STUB_RESTART") else "false")
    sys.exit(0)
if cmd == "restart-targets":
    sys.exit(0)
if cmd == "restart":
    name = argv[1]
    delay = os.environ.get("STUB_NEW_HB_DELAY")
    print(f"stub restarted {name}", file=sys.stderr)
    if delay is not None:
        # 新世代 = 新しく起こした生きているプロセス。delay 秒後に beat を書く。
        child = subprocess.Popen([sys.executable, "-c", (
            "import os,sys,time;sys.path.insert(0,os.environ['REAL_SCRIPTS']);"
            "import lib_daemon_watch as w;time.sleep(float(os.environ['STUB_NEW_HB_DELAY']));"
            "w.DaemonWatch(registry_dir=w.Path(os.environ['STUB_REGISTRY']),"
            "repo_root=w.Path(os.environ['STUB_REPO']),self_name=sys.argv[1]).beat();"
            "time.sleep(30)"), name], start_new_session=True)
        open(os.environ["STUB_CHILD_PID_FILE"], "w").write(str(child.pid))
    sys.exit(0)
sys.exit(w.main(argv))
'''


def _run_sync(tmp_path, *, new_hb_delay, wait_seconds):
    _, checkout = sync_base._make_repo(tmp_path)
    (checkout / "scripts" / "lib_daemon_watch.py").write_text(_HYBRID_LIB)
    reg = checkout / "registry"
    # 旧世代の記録: 死んだ pid (restart 前の dispatcher)
    _write_hb(reg, "dispatcher", 2 ** 22 + 4242, "old:1", time.time() - 100)
    env = sync_base._isolated_env()
    env.update({
        "REAL_SCRIPTS": str(TESTS_DIR.parent / "scripts"),
        "STUB_RESTART": "dispatcher",
        "STUB_REGISTRY": str(reg),
        "STUB_REPO": str(checkout),
        "STUB_CHILD_PID_FILE": str(tmp_path / "child.pid"),
    })
    if new_hb_delay is not None:
        env["STUB_NEW_HB_DELAY"] = str(new_hb_delay)
    try:
        proc = subprocess.run(
            ["bash", str(checkout / "scripts" / "sync-main-checkout.sh"),
             "--repo-root", str(checkout), "--restart-wait-seconds", str(wait_seconds)],
            cwd=str(checkout), capture_output=True, text=True, env=env, timeout=120)
    finally:
        pidf = tmp_path / "child.pid"
        if pidf.exists():
            try:
                os.kill(int(pidf.read_text()), 9)
            except (OSError, ValueError):
                pass
    return proc


def test_status_after_restart_shows_the_new_generation(tmp_path):
    proc = _run_sync(tmp_path, new_hb_delay=2, wait_seconds=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(l for l in proc.stdout.splitlines() if l.startswith("dispatcher:"))
    assert "recorded_instance_alive=True" in line, proc.stdout
    assert "old:1" not in line


def test_red_status_is_not_printed_from_the_old_record_without_waiting(tmp_path):
    """待たずに status を出す旧実装だと、旧 pid で recorded_instance_alive=False の行が出る。
    ここでは「新世代が来ないまま上限 (2 秒) に達した」ことが失敗として積まれ非 0 になる。"""
    proc = _run_sync(tmp_path, new_hb_delay=None, wait_seconds=2)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "FAILURE" in proc.stderr and "heartbeat" in proc.stderr


def test_a_heartbeat_arriving_after_the_limit_is_a_failure(tmp_path):
    proc = _run_sync(tmp_path, new_hb_delay=6, wait_seconds=2)
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_wait_seconds_needs_a_whole_number(tmp_path):
    _, checkout = sync_base._make_repo(tmp_path)
    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "sync-main-checkout.sh"),
         "--repo-root", str(checkout), "--restart-wait-seconds", "abc"],
        cwd=str(checkout), capture_output=True, text=True, env=sync_base._isolated_env())
    assert proc.returncode == 2


def test_rule2_ignores_a_mismatched_tasks_active_blocker(tmp_path):
    """同族 (Rule 2): 取れない task の blocker が in_progress でも、取れる task だけが
    10 分以上 stuck なら Worker は blocked-stuck で退役する (取れない task に生かされない)。"""
    root = _repo_with_blocked_target_task(
        tmp_path, worker_target=MY_REPO, task_target=OTHER_REPO)   # t001 (取れない) は t002 待ち
    tasks = root / "queue" / "missions" / SLUG / "tasks"
    (tasks / "t002.md").write_text(_card("t002", status="in_progress", worker="SomeoneElse"))
    (tasks / "t003.md").write_text(_card("t003", status="pending", blocked_by="[t004]", target=MY_REPO))
    (tasks / "t004.md").write_text(_card("t004", status="needs_director", worker="SomeoneElse"))
    old = time.time() - 3600
    for name in ("t003.md", "t004.md"):
        os.utime(tasks / name, (old, old))
    _cycle(root)
    assert _retirement_requested(root)
