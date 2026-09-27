#!/usr/bin/env python3
"""
tests/test_background_work_is_not_idle.py — 裏の shell / monitor は「止まっている」ではない (B1 / #27)

## 背景

長いテストを `run_in_background` / Monitor で待っている Worker は、ツール呼び出しが止まる。
2026-09-27 に自分の pane で実測した (裏の `sleep` が生きている間):

    herdr agent_status : `idle` (Monitor のときは `done`)   ← どちらも Rule 5 の通知対象
    画面末尾           : `1 shell` / `1 monitor`
    claude の直下      : `bash -c ... eval 'sleep 300'` が生える (後から生えた子)
    classify_process_tree(pane_pid) : 終始 `executing`、終わると `idle_process`

その結果、dispatcher の Rule 5 が `[Rule 5] idle-with-task` を約 2 分おきに Director に送り続けた
(mission 20260926-mechanize-guards-a では Director が手でツールを 1 回使わせて回避した)。
watchdog はプロセス層 (t016) が既に hard idle を terminate にしない — ここではそれを
**実プロセス木で** 通しで確かめ、上限 (max) が裏の子があっても効くことを固定する。

## 方法

* プロセス木は本物 (`sh` の親子)。`PROCESS_WORK_START_GRACE` だけ 1 秒に縮める。
* dispatcher は **本物の** dispatcher.sh の埋め込み python を `exec()` して `check_rule5()` を呼ぶ
  (`tests/test_dispatcher_notify_once.py` の Harness)。差し替えるのは mux (状態と pane pid) だけ。
* watchdog は本物の `WorkerMonitor.check_detail()`。差し替えるのは mux の pane pid 解決だけで、
  プロセス層は実物を通す (`_process_signal` をスタブしない)。

fail の向き (memory: fail-direction-is-per-judgment) はテスト名に出す:
  * Rule 5 (通知を止める判定): 観測できない → **通知する**側
  * watchdog (殺す判定):        観測できない → **殺さない**側 (既存: test_watchdog_idle.py)

実行: python3 -m pytest tests/test_background_work_is_not_idle.py -v
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lib_mux  # noqa: E402
import lib_pane_process  # noqa: E402
import watchdog  # noqa: E402
from test_dispatcher_notify_once import FakeMux, Harness, SLUG  # noqa: E402
from test_watchdog_idle import (  # noqa: E402
    WINDOW, _FakeMux as _WatchdogFakeMux, _make_monitor, _write_activity,
)

AGENT = "sofia"
TARGET = f"{AGENT}-worker"


# ---------------------------------------------------------------------------
# 実プロセス木 (pane 相当)。1 テストファイルで 2 本だけ立てて使い回す
# ---------------------------------------------------------------------------

def _pgid_of(pid: int) -> Optional[int]:
    """/proc/<pid>/stat の field 5 (pgid)。読めなければ None (= 既に居ない)。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    rest = raw[close + 1:].split()
    if len(rest) < 3:
        return None
    try:
        return int(rest[2])
    except ValueError:
        return None


def _members_of_group(pgid: int) -> list:
    """この pgid に属する現存 pid の一覧 (テストの「孤児が残っていないか」の判定に使う)。"""
    members = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return members
    for entry in entries:
        if entry.name.isdigit() and _pgid_of(int(entry.name)) == pgid:
            members.append(int(entry.name))
    return members


def _kill_pane_tree(proc: subprocess.Popen) -> None:
    """t049 (Codex review, PR#238 P3): root だけでなく group ごと kill する。

    root の `sh` 自身しか kill しないと、非対話シェルの背後の子 (孫の `sh` /
    `sleep`) は同じ group のまま孤児になり、自分の `sleep 300` の寿命 (最大 5分)
    だけ生き残って標準出力の fd を掴み続ける (mutation script を繰り返すたびに
    残骸が積み上がり、EOF 待ちの呼び出し側を遅延させうる)。spawn 側が
    `start_new_session=True` (setsid) で自分専用の process group を持つ前提。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass  # 既に居ない (group ごと消えた) — teardown の目的は既に達成
    proc.wait()


@pytest.fixture(scope="module")
def panes():
    """{"busy": pid, "quiet": pid} — どちらも本番のペインと同じ形の木。

    busy   root sh ─ sleep(t=0, MCP 相当) ─ sh(t=0, claude 相当) ─ sleep(t=2, 裏の job)
    quiet  root sh ─ sleep(t=0, MCP 相当) ─ sh(t=0, claude 相当)   … 裏の job 無し

    基準時刻は「最も古い直下の子」なので、t=0 の子を生かしたままにする。
    各木は `start_new_session=True` で自分専用の process group を持つ (setsid)。
    teardown は `_kill_pane_tree()` で group ごと落とす (P3)。
    """
    busy = subprocess.Popen(
        ["sh", "-c", 'sleep 300 & sh -c "sleep 2; sleep 300 & wait" & wait'],
        start_new_session=True)
    quiet = subprocess.Popen(
        ["sh", "-c", 'sleep 300 & sh -c "sleep 300 & wait" & wait'],
        start_new_session=True)
    time.sleep(3.5)  # 遅れて生える子が出そろうまで
    try:
        yield {"busy": busy.pid, "quiet": quiet.pid}
    finally:
        for p in (busy, quiet):
            _kill_pane_tree(p)


@pytest.fixture(autouse=True)
def short_grace(monkeypatch):
    monkeypatch.setattr(lib_pane_process, "PROCESS_WORK_START_GRACE", 1)


def test_the_fixture_trees_classify_as_intended(panes):
    """前提: 木の形が意図どおり。ここが崩れると以降の緑・赤は何の証明にもならない。"""
    assert lib_pane_process.classify_process_tree(panes["busy"]) == "executing"
    assert lib_pane_process.classify_process_tree(panes["quiet"]) == "idle_process"


# ---------------------------------------------------------------------------
# dispatcher Rule 5
# ---------------------------------------------------------------------------

class PaneMux(FakeMux):
    """herdr が返す agent_status と pane pid を、テストが決める。"""

    pane_state = "idle"
    pane_pid = None
    pid_raises = None

    def state(self, *a, **kw):
        return PaneMux.pane_state

    def pid(self, name):
        if PaneMux.pid_raises is not None:
            raise PaneMux.pid_raises
        return PaneMux.pane_pid


class Rule5:
    """`check_rule5()` を、grace 経過済みの idle-with-task から 1 回呼ぶ口。"""

    def __init__(self, h: Harness, monkeypatch):
        monkeypatch.setattr(lib_mux, "Mux", PaneMux)
        PaneMux.pane_state, PaneMux.pane_pid, PaneMux.pid_raises = "idle", None, None
        self.h = h
        h.cycle()   # 本物の埋め込み python の名前空間を作る (Worker 窓は無いので何も送らない)
        self.ns = h.ns
        self.assignment = self.ns["ASSIGNMENTS_DIR"] / AGENT
        self.assignment.parent.mkdir(parents=True, exist_ok=True)
        self.assignment.write_text(f"{SLUG}:t001\n")

    def age_state(self, state="idle-with-task", seconds=600):
        """Rule 5 の grace (60 秒) を経過済みにする。"""
        self.ns["_save_state_entry"](AGENT, state, time.time() - seconds)

    def entry(self):
        return json.loads(
            (self.h.registry / "mux" / f"{AGENT}.state.json").read_text())

    def run(self):
        FakeMux.sent = []
        self.ns["check_rule5"](AGENT, TARGET, self.assignment, {})
        return [m["message"] for m in FakeMux.sent]


@pytest.fixture
def r5(tmp_path, monkeypatch):
    return Rule5(Harness(tmp_path / "repo", monkeypatch), monkeypatch)


@pytest.mark.parametrize("herdr_state", ["idle", "done"])
def test_a_live_background_job_is_not_idle_with_task(r5, panes, herdr_state):
    """裏の shell (`idle`) / Monitor (`done`) が生きている間は Rule 5 は黙る。"""
    PaneMux.pane_state, PaneMux.pane_pid = herdr_state, panes["busy"]
    r5.age_state()
    assert r5.run() == []


def test_the_same_pane_without_a_background_job_is_still_notified(r5, panes):
    """対照: 裏の job が無ければ従来どおり通知する (常に黙る実装を弾く)。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["quiet"]
    r5.age_state()
    msgs = r5.run()
    assert len(msgs) == 1 and "idle-with-task" in msgs[0]


def test_a_background_job_restarts_the_grace_and_clears_the_dedup_key(r5, panes):
    """裏の job 中は 'working' 扱い: grace を測り直し、通知済みの印を外す。"""
    r5.ns["record_notify"](f"idle_with_task_{AGENT}")
    assert f"idle_with_task_{AGENT}" in r5.ns["load_notify_cache"]()
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.age_state()
    assert r5.run() == []
    assert r5.entry()["state"] == "working"
    assert time.time() - r5.entry()["since"] < 30          # 測り直された
    assert f"idle_with_task_{AGENT}" not in r5.ns["load_notify_cache"]()


def test_grace_counts_from_the_end_of_the_job(r5, panes):
    """job が終わったあとの grace は、終わった時点から数える (終わった瞬間に通知しない)。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.age_state()
    assert r5.run() == []                                   # job 中
    PaneMux.pane_pid = panes["quiet"]                       # job が終わった
    assert r5.run() == []                                   # 状態が変わった最初の cycle は grace の起点
    assert r5.entry()["state"] == "idle-with-task"
    r5.age_state()                                          # そのまま grace が過ぎた
    assert len(r5.run()) == 1                               # 本当に止まっているなら通知する


def test_blocked_is_still_notified_with_a_background_job(r5, panes):
    """`blocked` (承認・質問待ち) は裏の job があっても通知する — 待っているのは人間。"""
    PaneMux.pane_state, PaneMux.pane_pid = "blocked", panes["busy"]
    r5.age_state("blocked")
    msgs = r5.run()
    assert len(msgs) == 1 and "blocked" in msgs[0]


def test_no_assignment_means_the_process_tree_is_not_even_read(r5, panes):
    """assignment が無ければ条件 B ではない — /proc の走査は idle-with-task のときだけ。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.assignment.unlink()
    calls = []
    real = r5.ns["classify_process_tree"]
    r5.ns["classify_process_tree"] = lambda *a, **kw: calls.append(a) or real(*a, **kw)
    r5.run()
    assert calls == []


# -- fail の向き: 観測できない → 通知する側 ------------------------------------

def test_unobservable_pane_pid_falls_to_the_notifying_side(r5):
    """pane pid が引けない (mux 不調) → 通知する。誤って黙るより 1 通余計な方が安い。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", None
    r5.age_state()
    assert len(r5.run()) == 1


def test_a_classifier_error_falls_to_the_notifying_side_and_is_logged(r5):
    """分類が例外 → 通知する。黙って握りつぶさず WARNING を残す。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", 12345
    PaneMux.pid_raises = OSError("herdr unreachable")
    r5.age_state()
    assert len(r5.run()) == 1
    assert "cannot classify pane process tree" in r5.h.log_text()


def test_a_vanished_pane_process_falls_to_the_notifying_side(r5):
    """pane pid はあるが /proc に無い (`no_process`) → 裏の job は見えない → 通知する。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", 2 ** 22
    r5.age_state()
    assert len(r5.run()) == 1


# ---------------------------------------------------------------------------
# P2 (Codex review, PR#238): grace_seconds の枠内で始まった裏 job も拾う
# ---------------------------------------------------------------------------

def test_a_job_started_within_the_grace_window_is_caught_via_assignment_mtime(
    tmp_path, monkeypatch
):
    """しきい値 (ここでは 5 秒に上書き) の枠内で始まった裏 job は、session_start
    だけを根拠にすると classify_process_tree が一生 idle_process と読む欠陥が
    あった (実測: mission 20260927-mechanize-guards-b の QA Worker Arjun — 着手
    直後に投げた裏の pytest が、watchdog の verdict でも同じ形で idle_process と
    観測された)。既存テスト (`short_grace` で 1 秒に縮め、t=2 で job を生やす) は
    grace より**後**に job が始まる経路しか通らないため、この穴を検出できない。

    assignment file (このWorkerが「今の task」を割り当てられた時刻。job より必ず
    前に書かれる) の mtime を根拠に足すことで、grace の枠内でも拾えることを
    本物の check_rule5() 経由で固定する。
    """
    monkeypatch.setattr(lib_pane_process, "PROCESS_WORK_START_GRACE", 5)
    root = subprocess.Popen(
        ["sh", "-c", 'sleep 300 & sh -c "sleep 1.5; sleep 300 & wait" & wait'],
        start_new_session=True)
    try:
        time.sleep(0.3)  # MCP 相当だけが立った状態 (裏 job はまだ t=1.5 に生えていない)

        # Harness.__init__ 自身が lib_mux.Mux を FakeMux に差し替えるので、
        # PaneMux への差し替えは Harness 構築の**後**でないと上書きされて消える
        # (`Rule5.__init__` と同じ順序)。
        h = Harness(tmp_path / "repo", monkeypatch)
        monkeypatch.setattr(lib_mux, "Mux", PaneMux)
        PaneMux.pane_state, PaneMux.pane_pid, PaneMux.pid_raises = "idle", root.pid, None
        h.cycle()
        assignment = h.ns["ASSIGNMENTS_DIR"] / AGENT
        assignment.parent.mkdir(parents=True, exist_ok=True)
        assignment.write_text(f"{SLUG}:t001\n")  # mtime ≈ ここ (裏 job より前)
        h.ns["_save_state_entry"](AGENT, "idle-with-task", time.time() - 600)  # grace 経過済み

        # 対照: assignment を使わない生の分類は、まだ grace(5s) の枠内なので
        # 依然として idle_process に誤読される — これが P2 の欠陥そのもの
        assert lib_pane_process.classify_process_tree(root.pid) == "idle_process"

        time.sleep(1.8)  # 裏 job (t=1.5 に生えた) が生きている。総経過はまだ grace(5s) 未満

        FakeMux.sent = []
        h.ns["check_rule5"](AGENT, TARGET, assignment, {})
        assert FakeMux.sent == []  # 裏 job を拾い、idle-with-task を通知しない
    finally:
        _kill_pane_tree(root)


# ---------------------------------------------------------------------------
# P3 (Codex review, PR#238): pane tree の teardown は group ごと
# ---------------------------------------------------------------------------

def test_kill_pane_tree_leaves_no_orphans_behind():
    """root の `sh` だけを kill すると、孫の `sh` / `sleep` が同じ group のまま
    孤児になり、自分の `sleep 300` の寿命 (最大 5分) だけ生き残って標準出力の fd を
    掴み続ける (mutation script を繰り返すたびに残骸が積み上がる)。
    `_kill_pane_tree()` が group ごと落とし、孤児を残さないことを固定する。
    """
    proc = subprocess.Popen(
        ["sh", "-c", 'sleep 300 & sh -c "sleep 2; sleep 300 & wait" & wait'],
        start_new_session=True)
    time.sleep(3.5)  # 孫の裏 job まで出そろうまで
    pgid = os.getpgid(proc.pid)

    # 前提: 木の形が意図どおり (root + MCP 相当 + wrapper + 裏 job の 4 プロセス)
    assert len(_members_of_group(pgid)) == 4

    _kill_pane_tree(proc)
    time.sleep(0.3)  # SIGKILL の反映を待つ
    assert _members_of_group(pgid) == []


# ---------------------------------------------------------------------------
# watchdog — 実プロセス木を通しで (`_process_signal` をスタブしない)
# ---------------------------------------------------------------------------

def test_watchdog_does_not_terminate_a_worker_with_a_live_background_job(
    tmp_path, monkeypatch, panes
):
    """hard idle でも、裏の job が生きている間は terminate しない (warn)。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["busy"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)          # 300 * 2 を大きく超える
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("warn", "hard_idle_but_executing")
    assert detail.process_signal == "executing"


def test_watchdog_still_terminates_the_same_silence_without_a_job(
    tmp_path, monkeypatch, panes
):
    """対照: 同じ無音でも裏の job が無ければ terminate (常に見送る実装を弾く)。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["quiet"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("terminate", "hard_idle")


def test_the_absolute_max_still_applies_with_a_live_background_job(
    tmp_path, monkeypatch, panes
):
    """裏で止まったままの job を持つ Worker が永久に残らない — max は裏の job があっても効く。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["busy"]))
    monitor = _make_monitor(tmp_path, idle=300, max_threshold=60)
    _write_activity(tmp_path, age_seconds=3000)
    monitor.started_at = time.time() - 120
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("terminate", "max_exceeded")


def test_watchdog_and_dispatcher_share_one_classifier():
    """「裏で何かが走っているか」の定義は 1 つ (lib_pane_process) — コピーが生えたら赤。"""
    assert watchdog.classify_process_tree is lib_pane_process.classify_process_tree
    src = (SCRIPTS_DIR / "dispatcher.sh").read_text()
    assert "from lib_pane_process import classify_process_tree" in src
    for name in ("dispatcher.sh", "watchdog.py"):
        text = (SCRIPTS_DIR / name).read_text()
        assert "/proc/" not in text.replace("lib_pane_process", ""), (
            f"{name} が /proc を直接読んでいる — 分類は lib_pane_process に 1 つだけ")
