#!/usr/bin/env python3
"""
tests/test_watchdog_idle.py

Regression テスト: watchdog の idle 判定が到達しない欠陥
(t016, mission 20260921-daemon-authority-and-mutual-watch)

## 背景 (欠陥の再現条件)

`scripts/watchdog.py` の `check()` は以下の順で判定していた:

    1. now - started_at > max_threshold   → terminate
    2. mux 窓が無い                        → kill
    3. _has_child_processes()             → alive     ← ここで必ず返る
    4. idle_seconds を評価                 → warn / terminate

`_has_child_processes()` は `pgrep -P <pane_pid>` が 1 件でも返せば True を返す。
Worker のペインは `bash → claude → (MCP サーバー…)` という木を常に持つため、
**生きている Worker では 3 が必ず True になり、4 に到達しない**。結果として
task frontmatter の `timeout.idle` と `config/timeout-profiles.yaml` の idle
しきい値は全て死に設定で、実際に発火しうるのは 1 の絶対上限だけだった。

実測 (2026-09-21): Worker Ren が PreToolUse hook でハングし heartbeat が
2.5 時間更新されない状態でも `registry/watchdog-observations.jsonl` は
`idle_seconds: 9098.3 / idle_threshold: 1800 / check_result: "alive"` を
記録し続けていた。

## 修正の方針

子プロセスの存在は「生存」ではなく **プロセス層のシグナル** として扱い、
idle 判定は常に評価する。プロセス層は terminate を **抑制する方向にだけ**
効かせる (fail closed = 判断が付かないなら殺さない):

  - `executing`    … claude 起動から十分遅れて始まった子孫が居る
                     (= Bash tool が今まさに走っている) → terminate せず warn
  - `idle_process` … claude と MCP サーバーだけ → idle しきい値どおりに判定
  - `no_process`   … claude すら居ない            → idle しきい値どおりに判定

加えて「人間待ちで無音」(直近の Notification が activity/heartbeat で解除
されていない) も terminate を warn に落とす。

## テストケース

RED (修正前の実装では失敗する = 欠陥の再現):
  test_red_hard_idle_terminates_despite_child_processes
  test_red_soft_idle_warns_despite_child_processes

安全側 (誤 terminate に倒さないこと):
  test_executing_subprocess_downgrades_terminate_to_warn
  test_awaiting_human_downgrades_terminate_to_warn
  test_notification_cleared_by_activity_allows_terminate
  test_fresh_activity_is_alive

既存挙動の回帰:
  test_window_gone_is_kill
  test_max_threshold_still_terminates
  test_heartbeat_counts_as_activity

プロセス層の分類 (実プロセス + /proc):
  test_no_children_is_no_process
  test_startup_children_are_not_executing
  test_late_started_child_is_executing
  test_process_signal_unreadable_pane_pid_is_no_process

実行方法:
  python3 -m pytest tests/test_watchdog_idle.py -v
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import watchdog  # noqa: E402


AGENT = "TestWorker"
TASK_ID = "t999"
WINDOW = f"{AGENT}-worker"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_monitor(tmp_path: Path, idle: int = 300, max_threshold: int = 3600):
    """task frontmatter 相当の dict から WorkerMonitor を組み立てる。"""
    card = {
        "worker": AGENT,
        "timeout": {"idle": idle, "max": max_threshold},
    }
    return watchdog.WorkerMonitor(
        task_id=TASK_ID,
        task_card=card,
        profiles=watchdog.PROFILES,
        repo_root=tmp_path,
    )


def _write_activity(tmp_path: Path, age_seconds: float) -> Path:
    """registry/activity/<agent>/<task>.activity を age_seconds 前の mtime で作る。"""
    p = tmp_path / "registry" / "activity" / AGENT / f"{TASK_ID}.activity"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("tool-use\n")
    stamp = time.time() - age_seconds
    os.utime(p, (stamp, stamp))
    return p


def _write_heartbeat(tmp_path: Path, age_seconds: float) -> Path:
    p = tmp_path / "registry" / "heartbeats" / AGENT
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("alive\n")
    stamp = time.time() - age_seconds
    os.utime(p, (stamp, stamp))
    return p


def _write_notification(tmp_path: Path, age_seconds: float,
                        notification_type: str = "permission_request") -> Path:
    p = tmp_path / "registry" / "notifications" / AGENT / "n1.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"notification_type": notification_type}))
    stamp = time.time() - age_seconds
    os.utime(p, (stamp, stamp))
    return p


def _stub(monitor, *, window=WINDOW, process_signal="idle_process"):
    """mux / プロセス層を差し替える (判定ロジックだけを見るテスト用)。

    プロセス層そのものの挙動は、下の RED 群と "プロセス層の分類" 群が
    **実プロセスを使って** 別途検証する。
    """
    monitor._mux_window_name = lambda: window
    monitor._process_signal = lambda: process_signal
    return monitor


class _FakeMux:
    """window 名 → pane_pid だけを返す最小の mux スタブ。

    本番 mux (herdr / tmux) には一切触れない。
    """

    def __init__(self, name: str, pane_pid: int):
        self._map = {name: pane_pid}

    def list(self, suffix=None):
        return list(self._map)

    def pid(self, name):
        return self._map.get(name)


@pytest.fixture
def worker_pane():
    """Worker ペインと同じ形の実プロセス木を立てる。

        root sh          … ペイン (pane_pid — 本番では /bin/bash)
          └ sh           … claude 本体
              ├ sleep    … MCP サーバー
              └ sleep    … MCP サーバー

    本番の実測 (2026-09-21, Ren-worker pane 3850583) と同じく、
    `pgrep -P <pane_pid>` は常に 1 件 (claude 相当) を返す。これが
    「生きている Worker は必ず alive と判定される」欠陥の再現条件である。
    子はすべて root とほぼ同時に起動するので、修正後の分類では
    executing ではなく idle_process になる。
    """
    root = subprocess.Popen(
        ["sh", "-c", 'sh -c "sleep 300 & sleep 300 & wait" & wait']
    )
    time.sleep(0.5)  # 木が出そろうまで
    try:
        yield root.pid
    finally:
        root.kill()
        root.wait()


# ---------------------------------------------------------------------------
# RED — 修正前の実装ではここが失敗する (欠陥の再現)
#
# ここだけは _process_signal / _has_child_processes をスタブしない。
# スタブすると「子プロセスが居るのに idle を無視する」という欠陥そのものを
# 迂回してしまい、修正前でも通ってしまう (実際に一度そうなった)。
# 差し替えるのは mux の pane_pid 解決だけで、プロセス層は実物を通す。
# ---------------------------------------------------------------------------

def test_red_hard_idle_terminates_despite_child_processes(
    tmp_path, monkeypatch, worker_pane
):
    """子プロセスが実在しても、hard idle (idle_threshold * 2) 超過なら terminate。

    修正前: _has_child_processes() が True → 無条件 "alive"。
    idle_seconds が 10 倍でも一切発火しなかった。
    """
    monkeypatch.setattr(watchdog, "_mux", _FakeMux(WINDOW, worker_pane))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)  # 300 * 2 を大きく超える

    assert monitor.check() == "terminate"


def test_red_soft_idle_warns_despite_child_processes(
    tmp_path, monkeypatch, worker_pane
):
    """子プロセスが実在しても、soft idle (idle_threshold) 超過なら warn。"""
    monkeypatch.setattr(watchdog, "_mux", _FakeMux(WINDOW, worker_pane))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=450)  # 300 < 450 <= 600

    assert monitor.check() == "warn"


def test_red_live_pane_still_alive_when_activity_fresh(
    tmp_path, monkeypatch, worker_pane
):
    """実プロセス木があり activity も新しければ alive (誤 warn を出さない)。

    上の 2 件が「常に terminate/warn を返すだけの実装」でも通ってしまうのを防ぐ。
    """
    monkeypatch.setattr(watchdog, "_mux", _FakeMux(WINDOW, worker_pane))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=5)

    assert monitor.check() == "alive"


# ---------------------------------------------------------------------------
# 安全側 — 誤 terminate に倒さないこと
# ---------------------------------------------------------------------------

def test_executing_subprocess_downgrades_terminate_to_warn(tmp_path):
    """Bash tool が実行中 (executing) なら hard idle でも terminate しない。

    長時間のビルド / 学習 / CI 待ちは PostToolUse が発火しないので activity は
    stale になる。プロセス層が「今まさに走っている」と言っている間は殺さない。
    """
    monitor = _stub(_make_monitor(tmp_path, idle=300), process_signal="executing")
    _write_activity(tmp_path, age_seconds=3000)

    detail = monitor.check_detail()
    assert detail.verdict == "warn"
    assert detail.reason == "hard_idle_but_executing"


def test_awaiting_human_downgrades_terminate_to_warn(tmp_path):
    """人間の承認待ち (未解除の Notification) なら hard idle でも terminate しない。"""
    monitor = _stub(_make_monitor(tmp_path, idle=300))
    _write_activity(tmp_path, age_seconds=3000)
    _write_notification(tmp_path, age_seconds=2000)  # activity より新しい = 未解除

    detail = monitor.check_detail()
    assert detail.verdict == "warn"
    assert detail.reason == "hard_idle_but_awaiting_human"
    assert detail.awaiting_human is True


def test_notification_cleared_by_activity_allows_terminate(tmp_path):
    """Notification の後に activity が動いていれば「人間待ち」は解除済み。

    解除済みの古い通知が永久に terminate を抑止しないことを確かめる
    (抑止が外れないと「殺さない」に倒れっぱなしで watchdog が無力化する)。
    """
    monitor = _stub(_make_monitor(tmp_path, idle=300))
    _write_notification(tmp_path, age_seconds=5000)
    _write_activity(tmp_path, age_seconds=3000)  # 通知より後に活動 = 解除

    detail = monitor.check_detail()
    assert detail.awaiting_human is False
    assert detail.verdict == "terminate"


def test_unreadable_pane_pid_downgrades_terminate_to_warn(tmp_path, monkeypatch):
    """窓はあるが pane pid が引けない (mux backend 不調) なら terminate しない。

    実行中かハング中かを見分ける材料が無い = 判断が付かない → 殺さない。
    _is_mass_kill() が設定ミスを N 体の死と誤認しないのと同じ向きに倒す。
    """
    class _NoPidMux:
        def list(self, suffix=None):
            return [WINDOW]

        def pid(self, name):
            return None

    monkeypatch.setattr(watchdog, "_mux", _NoPidMux())
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)

    detail = monitor.check_detail()
    assert detail.process_signal == "unknown"
    assert detail.verdict == "warn"
    assert detail.reason == "hard_idle_but_process_unknown"


def test_fresh_activity_is_alive(tmp_path):
    """しきい値内なら alive。"""
    monitor = _stub(_make_monitor(tmp_path, idle=300))
    _write_activity(tmp_path, age_seconds=10)

    assert monitor.check() == "alive"


# ---------------------------------------------------------------------------
# 既存挙動の回帰
# ---------------------------------------------------------------------------

def test_window_gone_is_kill(tmp_path):
    """窓が消えていれば idle に関係なく kill (監視対象からの除外)。"""
    monitor = _stub(_make_monitor(tmp_path, idle=300), window=None)
    _write_activity(tmp_path, age_seconds=10)

    assert monitor.check() == "kill"


def test_max_threshold_still_terminates(tmp_path):
    """絶対上限は idle に関係なく terminate (既存の唯一生きていた経路)。"""
    monitor = _stub(_make_monitor(tmp_path, idle=300, max_threshold=60))
    _write_activity(tmp_path, age_seconds=1)
    monitor.started_at = time.time() - 120

    assert monitor.check() == "terminate"


def test_heartbeat_counts_as_activity(tmp_path):
    """activity が古くても heartbeat が新しければ idle は小さい。"""
    monitor = _stub(_make_monitor(tmp_path, idle=300))
    _write_activity(tmp_path, age_seconds=3000)
    _write_heartbeat(tmp_path, age_seconds=5)

    assert monitor.check() == "alive"


# ---------------------------------------------------------------------------
# プロセス層の分類 — 実プロセス + /proc
# ---------------------------------------------------------------------------

def test_no_children_is_no_process():
    """子を持たないプロセスは no_process。"""
    p = subprocess.Popen(["sleep", "30"])
    try:
        assert watchdog.classify_process_tree(p.pid, grace_seconds=1) == "no_process"
    finally:
        p.kill()
        p.wait()


def test_startup_children_are_not_executing():
    """起動直後に生えた子 (= MCP サーバー相当) は executing と見なさない。

    claude のペインでは MCP サーバーが claude 起動の 1-2 秒後に立ち上がる。
    これを「作業中」と読むと、今回直した欠陥と同じく idle 判定が永久に
    抑止されてしまう。
    """
    # root(sh) が即座に子 sleep を産む = 起動時からの常駐子プロセス
    root = subprocess.Popen(["sh", "-c", "sleep 30 & wait"])
    try:
        time.sleep(1.0)
        assert watchdog.classify_process_tree(root.pid, grace_seconds=10) == "idle_process"
    finally:
        root.kill()
        root.wait()


def test_late_started_child_is_executing():
    """セッション起動から遅れて生えた子孫は executing (= Bash tool 実行中)。

        root sh
          ├ sleep 300                  … t=0。基準時刻を t=0 に固定する
          └ sh                         … t=0。claude 相当 (wait で生存)
              └ sleep 300              … t=2。遅れて生えた = 実行中の tool

    基準は「最も古い直下の子の起動時刻」なので、t=0 の子を 1 つ生かしたまま
    にしておかないと基準が遅い方へずれて executing を検出できない。
    """
    root = subprocess.Popen(
        ["sh", "-c", 'sleep 300 & sh -c "sleep 2; sleep 300 & wait" & wait']
    )
    try:
        time.sleep(3.5)  # 遅れて生えた子が出そろうまで待つ
        assert watchdog.classify_process_tree(root.pid, grace_seconds=1) == "executing"
        # grace を十分大きくすれば同じ木でも executing にはならない
        # (しきい値が効いていることの確認 — 常に executing を返す実装を弾く)
        assert watchdog.classify_process_tree(root.pid, grace_seconds=60) == "idle_process"
    finally:
        root.kill()
        root.wait()


def test_process_signal_unreadable_pane_pid_is_no_process():
    """存在しない pid は no_process (例外にしない)。"""
    assert watchdog.classify_process_tree(2 ** 22, grace_seconds=1) == "no_process"


# ---------------------------------------------------------------------------
# 観測性 — 判定がログに残ること
# ---------------------------------------------------------------------------

def test_verdict_logged_on_every_change(tmp_path, monkeypatch):
    """判定が変わるたびに 1 行残る (QA が判定を検証できること)。"""
    log_file = tmp_path / "watchdog.log"
    monkeypatch.setattr(watchdog, "_LOG_FILE", log_file)

    monitor = _stub(_make_monitor(tmp_path, idle=300))
    tracker = watchdog.VerdictLogger(summary_every=1000)

    _write_activity(tmp_path, age_seconds=10)
    tracker.record(monitor, monitor.check_detail())
    _write_activity(tmp_path, age_seconds=450)
    tracker.record(monitor, monitor.check_detail())

    lines = log_file.read_text().splitlines()
    assert len(lines) == 2
    assert "alive" in lines[0]
    assert "warn" in lines[1]
    # 判定の根拠 (idle 秒数 / しきい値 / プロセス層) が同じ行に載っていること
    assert "idle=" in lines[1] and "idle_threshold=300" in lines[1]
    assert "process=idle_process" in lines[1]


def test_unchanged_verdict_is_summarised_not_repeated(tmp_path, monkeypatch):
    """同じ判定が続く間は毎 cycle 書かず、一定間隔でまとめて 1 行残す。

    30 秒ごとに全 Worker 分を書くとログが肥大するが、まったく書かないと
    「watchdog が生きていて alive と判定し続けている」ことを後から確認できない。
    """
    log_file = tmp_path / "watchdog.log"
    monkeypatch.setattr(watchdog, "_LOG_FILE", log_file)

    monitor = _stub(_make_monitor(tmp_path, idle=300))
    _write_activity(tmp_path, age_seconds=10)
    tracker = watchdog.VerdictLogger(summary_every=3)

    for _ in range(7):
        tracker.record(monitor, monitor.check_detail())

    lines = log_file.read_text().splitlines()
    # 1 行目 = 初回の判定、その後 3 cycle ごとの要約 (3, 6 回目) で計 3 行
    assert len(lines) == 3
    assert "still" in lines[1]
