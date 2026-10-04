#!/usr/bin/env python3
"""
tests/test_hard_idle_suppression_unknown_zombie.py — hard_idle が unknown で永久に止まった件
(§11 / mission 20261004-watchdog-hard-idle-unknown t003)

設計: knowledge/watchdog-idle-judgment.md §11。

  1. zombie (state=Z) は木から外す → zombie だけの木は idle_process (hard_idle で終了できる)
  2. 読めない (EACCES) ノードで木全体を即 unknown にしない → job が他にあれば executing
     (pid の並びに依存しない)。job が無ければ unknown のまま
  3. unknown / executing / awaiting_human / mux_pid_unavailable の見送りが続いたら
     Director に 1 回 (fp は Execution ID)。解けたら台帳キーを消す
  4. `WARN:` の行は理由が変わった時 + 10 cycle ごと
  5. 閾値は config `daemons.hard_idle_suppressed_notify_seconds` (既定 600・env なし)

実プロセスを立てる (zombie は親が回収しない子、非 dumpable は prctl(PR_SET_DUMPABLE, 0))。
EACCES にならない環境 (root 等) では、赤の実証にならないので skip する。

実行: env -u AGENT_NAME python3 -m pytest tests/test_hard_idle_suppression_unknown_zombie.py -v
"""

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lib_daemon_state  # noqa: E402
import lib_daemon_watch  # noqa: E402
import lib_pane_process  # noqa: E402
import watchdog  # noqa: E402
from test_background_work_is_not_idle import (  # noqa: E402
    _direct_children, _env_prefix, _job_wrapper_cmd, _kill_pane_tree, _mcp_like_cmd,
    _write_wrapper_snapshot,
)
from test_usage_limit import NOTICE_REL, limited_screen  # noqa: E402
from test_watchdog_idle import (  # noqa: E402
    AGENT, TASK_ID, WINDOW, _FakeMux, _make_monitor, _write_activity,
)

# ---------------------------------------------------------------------------
# 実プロセス木
# ---------------------------------------------------------------------------

#: 親が回収しない子 = zombie。親自身は CLAUDECODE を持つ infra (本番の `npm exec` 相当)。
_ZOMBIE_PARENT = 'import os,time\nif os.fork()==0: os._exit(0)\ntime.sleep(300)'
#: 非 dumpable (PR_SET_DUMPABLE=0): 生きているが environ が EACCES (ブラウザの sandbox 子等の形)。
_NONDUMP = 'import ctypes,time\nctypes.CDLL(None).prctl(4,0,0,0,0)\ntime.sleep(300)'


def _py_cmd(code: str, *, claudecode: bool = True) -> str:
    env = {"CLAUDECODE": "1"} if claudecode else {}
    return f"{_env_prefix(env)} {shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _descendants(pid):
    out, frontier = [], [pid]
    while frontier:
        nxt = []
        for p in frontier:
            kids = _direct_children(p)
            out.extend(kids)
            nxt.extend(kids)
        frontier = nxt
    return out


def _spawn(cmds, *, wait_for=None):
    """root sh の下に cmds を並べて走らせる (spawn 順 = pid 順)。"""
    root = subprocess.Popen(["sh", "-c", " & ".join(cmds) + " & wait"], start_new_session=True)
    deadline = time.time() + 6
    while time.time() < deadline:
        time.sleep(0.2)
        if wait_for is None or wait_for(root.pid):
            time.sleep(0.3)
            break
    return root


def _zombies(root_pid):
    return [p for p in _descendants(root_pid) if lib_pane_process._proc_state(p) == "Z"]


@pytest.fixture
def trees():
    started = []

    def make(cmds, wait_for=None):
        root = _spawn(cmds, wait_for=wait_for)
        started.append(root)
        return root.pid

    yield make
    for proc in started:
        _kill_pane_tree(proc)


def _require_unreadable_environ(pid):
    try:
        Path(f"/proc/{pid}/environ").read_bytes()
    except PermissionError:
        return
    except OSError:
        pass
    pytest.skip("この環境では対象ノードの environ が EACCES にならない (root 等)。赤の実証にならない")


# ---------------------------------------------------------------------------
# 1. zombie は木から外す
# ---------------------------------------------------------------------------

def test_a_zombie_child_does_not_make_the_tree_unknown(trees):
    mcp = _mcp_like_cmd("npm exec @playwright/mcp@latest")
    pane = trees([mcp, _py_cmd(_ZOMBIE_PARENT)], wait_for=_zombies)
    zombie = _zombies(pane)
    assert zombie, "前提: zombie が立っている"
    _require_unreadable_environ(zombie[0])
    assert lib_pane_process.classify_process_tree(pane) == "idle_process"


def test_watchdog_terminates_hard_idle_for_a_zombie_only_tree(trees, tmp_path, monkeypatch):
    """原因 A の Worker は本来の 3600 秒 (idle*2) で hard_idle の terminate になる。"""
    mcp = _mcp_like_cmd("npm exec @playwright/mcp@latest")
    pane = trees([mcp, _py_cmd(_ZOMBIE_PARENT)], wait_for=_zombies)
    _require_unreadable_environ(_zombies(pane)[0])
    monkeypatch.setattr(watchdog, "_mux", _FakeMux(WINDOW, pane))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason, detail.process_signal) == (
        "terminate", "hard_idle", "idle_process")


def test_usage_limit_with_a_zombie_tree_stays_alive(trees, tmp_path, monkeypatch):
    """対照 (review t002 指摘 1): 利用枠切れの免除 (limit_excused) は zombie 除外より先に効く。"""
    mcp = _mcp_like_cmd("npm exec @playwright/mcp@latest")
    pane = trees([mcp, _py_cmd(_ZOMBIE_PARENT)], wait_for=_zombies)

    class Mux(_FakeMux):
        def capture(self, *a, **kw):
            return limited_screen(NOTICE_REL)

    monkeypatch.setattr(watchdog, "_mux", Mux(WINDOW, pane))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("alive", "usage_limit")


# ---------------------------------------------------------------------------
# 2. 読めないノードは job の探索を打ち切らない / B・C・D は unknown のまま
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ["unreadable-first", "job-first"])
def test_a_job_wins_over_an_unreadable_node_in_either_order(trees, tmp_path, order):
    snap = _write_wrapper_snapshot(tmp_path)
    job = _job_wrapper_cmd(snap, "sleep 300")
    nondump = _py_cmd(_NONDUMP)
    pane = trees([nondump, job] if order == "unreadable-first" else [job, nondump])
    nd = next(p for p in _descendants(pane)
              if "prctl" in (lib_pane_process._proc_cmdline(p) or ""))
    _require_unreadable_environ(nd)
    assert lib_pane_process.classify_process_tree(pane) == "executing"


def test_an_unreadable_node_without_a_job_stays_unknown(trees):
    pane = trees([_mcp_like_cmd("npm exec chrome"), _py_cmd(_NONDUMP)])
    nd = next(p for p in _descendants(pane)
              if "prctl" in (lib_pane_process._proc_cmdline(p) or ""))
    _require_unreadable_environ(nd)
    # 殺す根拠にしない向きは変えない: 読めないものは infra にも job にも倒さない。
    assert lib_pane_process.classify_process_tree(pane) == "unknown"
    notes = lib_pane_process.explain_unknown_tree(pane)
    assert any(f"pid={nd}" in n and "errno=EACCES(13)" in n and "state=S" in n for n in notes), notes


def test_a_process_without_claudecode_stays_unknown(trees):
    """C: CLAUDECODE の無い子は infra に倒さない (t091 の契約。zombie 除外の巻き添えにしない)。"""
    pane = trees([_mcp_like_cmd("npm exec foreign", claudecode=False)])
    assert lib_pane_process.classify_process_tree(pane) == "unknown"
    assert any("origin=unknown" in n for n in lib_pane_process.explain_unknown_tree(pane))


def test_a_state_that_cannot_be_read_is_not_taken_for_a_zombie(monkeypatch, trees):
    """`_proc_state` が None (読めない) のノードは外さない = 通常の分類に進む。"""
    pane = trees([_mcp_like_cmd("npm exec foreign", claudecode=False)])
    monkeypatch.setattr(lib_pane_process, "_proc_state", lambda pid: None)
    assert lib_pane_process.classify_process_tree(pane) == "unknown"


def _wrapper_with_job_child(snap):
    """pane 直下の包み役 (sh) とその子の job wrapper。包み役が Z になる競合を作る土台。"""
    return f"{_env_prefix({'CLAUDECODE': '1'})} sh -c {shlex.quote(_job_wrapper_cmd(snap, 'sleep 300') + ' & wait')}"


def test_a_wrapper_that_turns_zombie_after_the_snapshot_keeps_its_job(trees, tmp_path, monkeypatch):
    """t013 (Codex 3 巡目 P1): children map を作った後 (state を読む時点) で包み役が Z になっても、
    その子の job は木から消えない。修正前は idle_process (= 働いている Worker を terminate)。

    マイクロ秒の窓は実時間で再現できないので、`_proc_state` の差し替えで構造的に作る
    (snapshot 時は生きている = 実プロセスの木・読む時点で Z)。"""
    snap = _write_wrapper_snapshot(tmp_path)
    pane = trees([_wrapper_with_job_child(snap)])
    wrapper = next(p for p in _direct_children(pane) if _direct_children(p))
    real = lib_pane_process._proc_state
    monkeypatch.setattr(lib_pane_process, "_proc_state",
                        lambda pid: "Z" if pid == wrapper else real(pid))
    assert lib_pane_process.classify_process_tree(pane) == "executing"


def test_the_children_of_a_zombie_are_not_taken_for_a_session_body(trees, tmp_path, monkeypatch):
    """zombie の子は parent_origin に None を渡されない (pane 直下限定の session 判定に入らない)。"""
    snap = _write_wrapper_snapshot(tmp_path)
    pane = trees([_wrapper_with_job_child(snap)])
    wrapper = next(p for p in _direct_children(pane) if _direct_children(p))
    grandchildren = _descendants(wrapper)
    assert grandchildren, "前提: zombie になる包み役に子が居る"
    seen = []
    monkeypatch.setattr(lib_pane_process, "_is_session_body", lambda pid: seen.append(pid) or False)
    real = lib_pane_process._proc_state
    monkeypatch.setattr(lib_pane_process, "_proc_state",
                        lambda pid: "Z" if pid == wrapper else real(pid))
    lib_pane_process.classify_process_tree(pane)
    assert wrapper not in seen
    assert not set(grandchildren) & set(seen)


# ---------------------------------------------------------------------------
# 3. 見送りの通知
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _monitor(tmp_path, *, execution_id="ex-" + "a" * 32, card_started="2026-10-04T10:00:00Z"):
    card = {
        "worker": AGENT,
        "timeout": {"idle": 300, "max": 36000},
        "started_at": card_started,
    }
    if execution_id:
        card["current_execution_id"] = execution_id
    return watchdog.WorkerMonitor(task_id=TASK_ID, task_card=card,
                                  profiles=watchdog.PROFILES, repo_root=tmp_path)


def _detail(reason, process="unknown", awaiting=False, idle=3700.0):
    return watchdog.CheckResult("warn", reason, idle, process, awaiting)


@pytest.fixture
def notifier(tmp_path):
    clock = Clock()
    sent = []
    notify_once = watchdog.make_notify_once(
        tmp_path, send=lambda msg: sent.append(msg) or True, log=lambda m: None, now=clock)
    told = tmp_path / "registry" / "daemons" / "notified-state.json"
    n = watchdog.SuppressedIdleNotifier(
        notify_once, lambda key: lib_daemon_state.told_forget(told, key), 600, now=clock,
        read_ledger=lambda: watchdog._told_entries(told))
    return n, clock, sent, told


def _key(kind):
    return watchdog.suppression_key(AGENT, kind)


def _told_keys(told):
    store = lib_daemon_state.load_json_store(told, check=lib_daemon_state.told_ledger_problem)
    return set() if lib_daemon_state.is_missing(store) else set(store)


@pytest.mark.parametrize("reason,process,awaiting,needle", [
    ("hard_idle_but_process_unknown", "unknown", False, "process=unknown"),
    ("hard_idle_but_executing", "executing", False, "process=executing"),
    ("hard_idle_but_awaiting_human", "idle_process", True, "awaiting_human"),
])
def test_a_suppressed_hard_idle_notifies_the_director_once_after_the_threshold(
        notifier, tmp_path, monkeypatch, reason, process, awaiting, needle):
    kind = {"hard_idle_but_process_unknown": "process_unknown", "hard_idle_but_executing": "executing",
            "hard_idle_but_awaiting_human": "awaiting_human"}[reason]
    n, clock, sent, told = notifier
    monkeypatch.setattr(watchdog, "explain_unknown_tree", lambda pid: ["pid=7 state=Z errno=EACCES(13)"])
    monitor = _monitor(tmp_path)
    monitor._last_pane_pid = 4242
    detail = _detail(reason, process, awaiting)
    n.observe(monitor, detail)                    # 見送りの開始
    clock.t += 599
    n.observe(monitor, detail)
    assert sent == []                             # しきい値の前は黙る
    clock.t += 2
    n.observe(monitor, detail)
    assert len(sent) == 1 and needle in sent[0]
    for _ in range(20):                           # 以後のサイクルでは再送しない
        clock.t += 32
        n.observe(monitor, detail)
    assert len(sent) == 1
    assert _told_keys(told) == {_key(kind)}


def test_unknown_message_carries_the_node_diagnosis(notifier, tmp_path, monkeypatch):
    n, clock, sent, _ = notifier
    monkeypatch.setattr(watchdog, "explain_unknown_tree", lambda pid: ["pid=7 state=Z errno=EACCES(13)"])
    monitor = _monitor(tmp_path)
    monitor._last_pane_pid = 4242
    n.observe(monitor, _detail("hard_idle_but_process_unknown"))
    clock.t += 700
    n.observe(monitor, _detail("hard_idle_but_process_unknown"))
    assert "pid=7 state=Z errno=EACCES(13)" in sent[0]


def test_a_missing_pane_pid_is_reported_as_mux_pid_unavailable(notifier, tmp_path, monkeypatch):
    """窓はあるが pane pid が引けない (mux の不調) — 木を見ていないので理由を分ける。"""
    n, clock, sent, _ = notifier
    monkeypatch.setattr(watchdog, "_mux", type("M", (), {
        "list": lambda self, suffix=None: [WINDOW], "pid": lambda self, name: None})())
    monitor = _monitor(tmp_path)
    assert monitor._process_signal() == "unknown"
    detail = _detail("hard_idle_but_process_unknown")
    assert monitor.suppression_kind(detail) == "mux_pid_unavailable"
    n.observe(monitor, detail)
    clock.t += 700
    n.observe(monitor, detail)
    assert len(sent) == 1 and "mux_pid_unavailable" in sent[0]


def test_the_fingerprint_is_the_execution_id_with_a_started_at_fallback(tmp_path):
    new = _monitor(tmp_path, execution_id="ex-" + "b" * 32)
    assert new.suppression_fp("executing") == "ex-" + "b" * 32 + ":executing"
    legacy = _monitor(tmp_path, execution_id="", card_started="2026-10-04T10:00:00Z")
    assert legacy.suppression_fp("executing") == f"{TASK_ID}@2026-10-04T10:00:00Z:executing"
    # 同じ card なら監視 object を作り直しても (watchdog の再起動) 同じ fp
    assert _monitor(tmp_path, execution_id="").suppression_fp("executing") == legacy.suppression_fp("executing")


def test_a_new_attempt_by_the_same_worker_notifies_again(notifier, tmp_path):
    n, clock, sent, _ = notifier
    d = _detail("hard_idle_but_executing", "executing")
    first = _monitor(tmp_path, execution_id="ex-" + "1" * 32)
    n.observe(first, d)
    clock.t += 700
    n.observe(first, d)
    n.forget(first)                                # 試行の終わり
    second = _monitor(tmp_path, execution_id="ex-" + "2" * 32)
    n.observe(second, d)
    clock.t += 700
    n.observe(second, d)
    assert len(sent) == 2


def test_recovery_clears_the_ledger_key_and_a_relapse_notifies_again(notifier, tmp_path):
    n, clock, sent, told = notifier
    monitor = _monitor(tmp_path)
    d = _detail("hard_idle_but_executing", "executing")
    n.observe(monitor, d)
    clock.t += 700
    n.observe(monitor, d)
    assert _told_keys(told) == {_key("executing")}
    n.observe(monitor, watchdog.CheckResult("alive", "active", 5.0, "idle_process", False))
    assert _told_keys(told) == set()               # 解けた = 台帳キーを消す
    n.observe(monitor, d)                          # 再発
    clock.t += 700
    n.observe(monitor, d)
    assert len(sent) == 2


def _restarted_notifier(tmp_path, clock, sent):
    """watchdog 再起動 = 新しい SuppressedIdleNotifier (プロセス内の状態は空)。本番と同じ has_key つき。"""
    notify_once = watchdog.make_notify_once(
        tmp_path, send=lambda msg: sent.append(msg) or True, log=lambda m: None, now=clock)
    told = tmp_path / "registry" / "daemons" / "notified-state.json"
    return watchdog.SuppressedIdleNotifier(
        notify_once, lambda k: lib_daemon_state.told_forget(told, k), 600, now=clock,
        read_ledger=lambda: watchdog._told_entries(told))


def test_recovery_across_a_watchdog_restart_still_clears_the_ledger(notifier, tmp_path):
    """PR #278 Codex P2 (t009): 通知したあと watchdog が再起動し、回復を新プロセスが観測した。"""
    n, clock, sent, told = notifier
    monitor, d = _monitor(tmp_path), _detail("hard_idle_but_executing", "executing")
    n.observe(monitor, d)
    clock.t += 700
    n.observe(monitor, d)
    assert len(sent) == 1 and _told_keys(told) == {_key("executing")}
    n2 = _restarted_notifier(tmp_path, clock, sent)               # 再起動
    n2.observe(monitor, watchdog.CheckResult("alive", "active", 5.0, "idle_process", False))
    assert _told_keys(told) == set()                              # 回復 → 消える
    n2.observe(monitor, d)                                        # 同じ試行でまた見送り
    clock.t += 700
    n2.observe(monitor, d)
    assert len(sent) == 2                                         # 通知が出る


def test_a_pending_forget_lost_by_a_restart_is_redone_from_the_ledger(tmp_path):
    """消せなかった (ロック失敗) まま再起動しても、次の観測が台帳から掃除をやり直す。"""
    clock, sent = Clock(), []
    told = tmp_path / "registry" / "daemons" / "notified-state.json"
    n = _restarted_notifier(tmp_path, clock, sent)
    broken = watchdog.SuppressedIdleNotifier(
        n._notify_once, lambda k: False, 600, now=clock, read_ledger=n._read_ledger)
    monitor, d = _monitor(tmp_path), _detail("hard_idle_but_executing", "executing")
    broken.observe(monitor, d)
    clock.t += 700
    broken.observe(monitor, d)
    broken.observe(monitor, watchdog.CheckResult("alive", "active", 5.0, "idle_process", False))
    assert _told_keys(told) == {_key("executing")}  # 消せなかった (in-process の pending は再起動で失う)
    n3 = _restarted_notifier(tmp_path, clock, sent)
    n3.observe(monitor, watchdog.CheckResult("alive", "active", 5.0, "idle_process", False))
    assert _told_keys(told) == set()


def _flaky_forget(told, fail_times):
    """最初の fail_times 回だけ台帳の掃除に失敗する forget_key (ロック失敗の再現)。"""
    state = {"n": 0}

    def forget(key):
        state["n"] += 1
        if state["n"] <= fail_times:
            return False
        return lib_daemon_state.told_forget(told, key)
    return forget


def _notifier_with(tmp_path, clock, sent, forget_key):
    told = tmp_path / "registry" / "daemons" / "notified-state.json"
    notify_once = watchdog.make_notify_once(
        tmp_path, send=lambda msg: sent.append(msg) or True, log=lambda m: None, now=clock)
    return watchdog.SuppressedIdleNotifier(
        notify_once, forget_key, 600, now=clock,
        read_ledger=lambda: watchdog._told_entries(told)), told


def test_a_failed_ledger_deletion_is_retried_from_the_ledger_with_or_without_a_forget(tmp_path):
    """PR #278 Codex P2 (t011): 掃除が失敗しても、(a) 回復した Worker の次の観測 (b) その Worker が
    もう監視されていないときの cycle が、台帳を読み直して消す。プロセス内の pending は要らない。"""
    clock, sent = Clock(), []
    told = tmp_path / "registry" / "daemons" / "notified-state.json"
    alive = watchdog.CheckResult("alive", "active", 5.0, "idle_process", False)
    for retry in ("observe", "cycle"):
        told.unlink(missing_ok=True)
        n, told = _notifier_with(tmp_path, clock, sent, _flaky_forget(told, 1))
        monitor, d = _monitor(tmp_path), _detail("hard_idle_but_executing", "executing")
        n.observe(monitor, d)
        clock.t += 700
        n.observe(monitor, d)
        n.observe(monitor, alive)
        assert _told_keys(told) == {_key("executing")}                # 1 回目の掃除は失敗
        if retry == "observe":
            n.observe(monitor, alive)
        else:
            n.cycle(set())                                             # forget は来ない。サイクルだけが回る
        assert _told_keys(told) == set()


def test_a_pending_deletion_is_done_before_the_suppressed_branch_decides_to_notify(tmp_path):
    """掃除が残ったまま同じ試行で次の見送りが始まっても、通知は落ちない。"""
    clock, sent = Clock(), []
    told = tmp_path / "registry" / "daemons" / "notified-state.json"
    n, told = _notifier_with(tmp_path, clock, sent, _flaky_forget(told, 1))
    monitor, d = _monitor(tmp_path), _detail("hard_idle_but_executing", "executing")
    n.observe(monitor, d)
    clock.t += 700
    n.observe(monitor, d)
    assert len(sent) == 1
    n.observe(monitor, watchdog.CheckResult("alive", "active", 5.0, "idle_process", False))  # 掃除は失敗
    n.observe(monitor, d)                                              # 再発 (cycle を挟まない)
    clock.t += 700
    n.observe(monitor, d)
    assert len(sent) == 2                                              # 古いキーに阻まれない


def test_a_flush_that_raises_does_not_stop_the_cycle(tmp_path):
    clock, sent = Clock(), []

    def boom(key):
        raise OSError("disk")
    n, told = _notifier_with(tmp_path, clock, sent, boom)
    lib_daemon_state.told_record(told, _key("executing"),
                                 {"fp": "x", "kind": "k", "slug": "_daemon", "task": "t", "at": 1.0})
    n.cycle(set())                                                    # 例外は外に出ない
    assert _told_keys(told) == {_key("executing")}      # 次のサイクルでまた試す


def test_an_orphan_key_of_an_unmonitored_worker_is_swept_but_a_monitored_one_is_kept(tmp_path):
    """通知後に再起動し、Worker が終わっていた (forget の対象が二度と現れない) キーを掃除する。"""
    clock, sent = Clock(), []
    n, told = _notifier_with(tmp_path, clock, sent, lambda k: lib_daemon_state.told_forget(
        tmp_path / "registry" / "daemons" / "notified-state.json", k))
    entry = {"fp": "x", "kind": "k", "slug": "_daemon", "task": "t", "at": 1.0}
    lib_daemon_state.told_record(told, _key("executing"), entry)
    lib_daemon_state.told_record(told, "hard-idle-suppressed_Gone@executing", entry)
    lib_daemon_state.told_record(told, "usage-limit-overdue_Gone", entry)   # 別の族のキーには触らない
    n.cycle({AGENT})
    assert _told_keys(told) == {_key("executing"), "usage-limit-overdue_Gone"}


def test_an_unreadable_ledger_is_never_swept(tmp_path):
    clock, sent = Clock(), []
    n, told = _notifier_with(tmp_path, clock, sent, lambda k: True)
    told.parent.mkdir(parents=True, exist_ok=True)
    told.write_text("{not json")
    n.cycle(set())
    assert told.read_text() == "{not json"


def test_a_healthy_worker_never_touches_the_ledger_lock(tmp_path):
    """台帳に key が無い Worker (大多数) の毎サイクルの観測は forget_key (= ロック + 書き込み) を呼ばない。"""
    calls = []
    told = tmp_path / "registry" / "daemons" / "notified-state.json"
    n = watchdog.SuppressedIdleNotifier(
        lambda *a: True, lambda k: calls.append(k) or True, 600,
        read_ledger=lambda: watchdog._told_entries(told))
    monitor = _monitor(tmp_path)
    for _ in range(3):
        n.observe(monitor, watchdog.CheckResult("alive", "active", 5.0, "idle_process", False))
    assert calls == []


def test_a_failed_send_is_retried_and_not_recorded(tmp_path):
    clock = Clock()
    results = iter([False, True])
    sent = []
    nonce = watchdog.make_notify_once(
        tmp_path, send=lambda m: sent.append(m) or next(results), log=lambda m: None, now=clock)
    told = tmp_path / "registry" / "daemons" / "notified-state.json"
    n = watchdog.SuppressedIdleNotifier(nonce, lambda k: lib_daemon_state.told_forget(told, k), 600, now=clock,
                                        read_ledger=lambda: watchdog._told_entries(told))
    monitor, d = _monitor(tmp_path), _detail("hard_idle_but_executing", "executing")
    n.observe(monitor, d)
    clock.t += 700
    n.observe(monitor, d)                          # 送れなかった
    assert _told_keys(told) == set()
    clock.t += 32
    n.observe(monitor, d)                          # 次のサイクルで再送
    assert len(sent) == 2 and _told_keys(told) == {_key("executing")}


def test_an_unsuppressed_verdict_is_never_notified(notifier, tmp_path):
    n, clock, sent, _ = notifier
    monitor = _monitor(tmp_path)
    for detail in (_detail("soft_idle", "idle_process"),
                   watchdog.CheckResult("terminate", "hard_idle", 4000.0, "idle_process", False)):
        n.observe(monitor, detail)
        clock.t += 5000
        n.observe(monitor, detail)
    assert sent == []


def test_told_forget_is_idempotent(tmp_path):
    told = tmp_path / "registry" / "daemons" / "notified-state.json"
    assert lib_daemon_state.told_forget(told, "absent") is True          # 台帳が無い
    entry = {"fp": "x", "kind": "k", "slug": "_daemon", "task": "t", "at": 1.0}
    assert lib_daemon_state.told_record(told, "a", entry)
    assert lib_daemon_state.told_record(told, "b", entry)
    assert lib_daemon_state.told_forget(told, "a") is True
    assert _told_keys(told) == {"b"}
    assert lib_daemon_state.told_forget(told, "a") is True


# ---------------------------------------------------------------------------
# 4. WARN 行の間引き
# ---------------------------------------------------------------------------

def test_warn_lines_are_thinned_to_reason_changes_and_every_n_cycles(tmp_path):
    th = watchdog.WarnLineThrottle(summary_every=10)
    monitor = _monitor(tmp_path)
    d1, d2 = _detail("hard_idle_but_process_unknown"), _detail("hard_idle_but_awaiting_human")
    emitted = [th.should_emit(monitor, d1) for _ in range(25)]
    assert [i for i, e in enumerate(emitted) if e] == [0, 10, 20]
    assert th.should_emit(monitor, d2) is True                            # 理由が変わった
    th.forget(monitor)                                                    # warn でなくなった
    assert th.should_emit(monitor, d1) is True                            # 初回扱いに戻る


# ---------------------------------------------------------------------------
# 5. 設定値
# ---------------------------------------------------------------------------

def _cfg(tmp_path, text, env=None):
    path = tmp_path / "crewvia.yaml"
    path.write_text(text)
    return lib_daemon_watch.load_config(path, env=env or {})


def test_the_threshold_default_is_in_one_place_and_the_shipped_config_matches(tmp_path):
    assert lib_daemon_watch.WatchConfig().hard_idle_suppressed_notify_seconds == 600
    shipped = lib_daemon_watch.load_config(env={})
    assert shipped.hard_idle_suppressed_notify_seconds == 600


def test_the_threshold_comes_from_config_and_has_no_env_override(tmp_path):
    text = "daemons:\n  hard_idle_suppressed_notify_seconds: 123\n"
    assert _cfg(tmp_path, text).hard_idle_suppressed_notify_seconds == 123
    env = {"CREWVIA_DAEMON_HARD_IDLE_SUPPRESSED_NOTIFY_SECONDS": "5"}
    assert _cfg(tmp_path, text, env).hard_idle_suppressed_notify_seconds == 123


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "null"])
def test_an_unusable_threshold_falls_back_to_the_default(tmp_path, bad):
    cfg = _cfg(tmp_path, f"daemons:\n  hard_idle_suppressed_notify_seconds: {bad}\n")
    assert cfg.hard_idle_suppressed_notify_seconds == 600


# ---------------------------------------------------------------------------
# 6. 配線 (run() の中。ループ全体は走らせられないので AST で固定する)
# ---------------------------------------------------------------------------

def _run_calls():
    import ast
    tree = ast.parse((SCRIPTS_DIR / "watchdog.py").read_text())
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    calls = []
    for node in ast.walk(run):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name):
            calls.append((node.func.value.id, node.func.attr))
    return calls, run


def test_run_observes_every_monitor_each_cycle_and_forgets_through_one_helper():
    calls, run = _run_calls()
    assert ("suppressed_notifier", "observe") in calls
    # 台帳の後始末は forget の経路に頼らず 1 サイクルに 1 回 (Worker ごとのループの外)。
    assert calls.count(("suppressed_notifier", "cycle")) == 1
    assert ("warn_throttle", "should_emit") in calls
    # 監視から外すときの後始末は forget_monitor の 1 か所 (verdict_logger.forget を直に
    # 呼ぶ場所が残ると、見送り通知の台帳キーと WARN の間引きの記憶が取り残される)。
    direct = [c for c in calls if c == ("verdict_logger", "forget")]
    assert len(direct) == 1, "verdict_logger.forget は forget_monitor の中の 1 回だけ"
    import ast
    helper = next(n for n in ast.walk(run) if isinstance(n, ast.FunctionDef) and n.name == "forget_monitor")
    inner = [(n.func.value.id, n.func.attr) for n in ast.walk(helper)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)]
    assert {("verdict_logger", "forget"), ("warn_throttle", "forget"),
            ("suppressed_notifier", "forget")} <= set(inner)


def test_the_suppression_message_names_the_mission_and_separates_reason_from_evidence(
        notifier, tmp_path, monkeypatch):
    """t013: mission slug を入れる (task id だけでは別 mission の同名 task と区別できない)。
    理由と根拠 (該当ノード) を分けて書く。"""
    n, clock, sent, _ = notifier
    monkeypatch.setattr(watchdog, "explain_unknown_tree", lambda pid: ["pid=7 state=S errno=EACCES(13)"])
    card = {"worker": AGENT, "timeout": {"idle": 300, "max": 36000}}
    monitor = watchdog.WorkerMonitor(task_id=TASK_ID, task_card=card, profiles=watchdog.PROFILES,
                                     repo_root=tmp_path, mission_slug="20261004-some-mission")
    monitor._last_pane_pid = 4242
    n.observe(monitor, _detail("hard_idle_but_process_unknown"))
    clock.t += 700
    n.observe(monitor, _detail("hard_idle_but_process_unknown"))
    msg = sent[0]
    assert "mission 20261004-some-mission" in msg
    assert "終了を見送っています。理由:" in msg and "・該当ノード pid=7 state=S errno=EACCES(13)" in msg
    assert "ため終了" not in msg
