#!/usr/bin/env python3
"""tests/test_watchdog_post_job_grace.py

job (run_in_background の CI 待ち等) が終わった直後の hard_idle (t001 / knowledge/watchdog-idle-judgment.md §13)。

2026-10-30、CI 待ちが終わった最初のサイクルで watchdog が `idle observed_seconds=3854 > limit 600` と
数えて Worker を退役させた (done の直前)。見送り (executing) の間も無音の時計は進んでおり、
job が消えた瞬間に待った時間ぜんぶが無音として効いたため。

直し方: 最後に executing を観測した時刻からの無音 (job 消滅後の無音) だけを、job が消えた後の
hard_idle / soft_idle の判定に使う。`unknown` は活動扱いにしない。max は変えない。

    python3 -m pytest tests/test_watchdog_post_job_grace.py -v
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watchdog  # noqa: E402
from test_watchdog_idle import (  # noqa: E402
    AGENT, WINDOW, _make_monitor, _write_activity,
)

IDLE = 300          # hard しきい値 = 600s
WAIT = 3800         # CI 待ちの長さ (activity はこれだけ前)


class _Clock:
    def __init__(self, monkeypatch):
        self.offset = 0.0
        real = time.time
        monkeypatch.setattr(watchdog.time, "time", lambda: real() + self.offset)

    def advance(self, seconds: float) -> None:
        self.offset += seconds


def _monitor(tmp_path, signal=None, max_threshold=20000):
    m = _make_monitor(tmp_path, idle=IDLE, max_threshold=max_threshold)
    m._mux_window_name = lambda: WINDOW
    box = {"signal": signal or "idle_process"}
    m._process_signal = lambda: box["signal"]
    m.box = box
    return m


def test_job_ending_does_not_terminate_on_the_first_cycle(tmp_path):
    m = _monitor(tmp_path, "executing")
    _write_activity(tmp_path, age_seconds=WAIT)
    d = m.check_detail()
    assert (d.verdict, d.reason) == ("warn", "hard_idle_but_executing")
    m.box["signal"] = "idle_process"          # job が消えた最初のサイクル
    d = m.check_detail()
    assert d.verdict == "alive" and d.reason == "post_job_grace"


def test_silence_after_the_job_still_terminates(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    m = _monitor(tmp_path, "executing")
    _write_activity(tmp_path, age_seconds=WAIT)
    m.check_detail()
    m.box["signal"] = "idle_process"
    clock.advance(IDLE * 2 - 10)
    assert m.check_detail().verdict != "terminate"
    clock.advance(20)                          # job 消滅後の無音がしきい値を超えた
    d = m.check_detail()
    assert (d.verdict, d.reason) == ("terminate", "hard_idle")


def test_activity_after_the_job_resets_the_clock_as_usual(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    m = _monitor(tmp_path, "executing")
    _write_activity(tmp_path, age_seconds=WAIT)
    m.check_detail()
    m.box["signal"] = "idle_process"
    clock.advance(IDLE * 2 + 100)
    _write_activity(tmp_path, age_seconds=1)   # Worker が動き出した
    assert m.check_detail().verdict == "alive"


def test_no_job_ever_is_the_same_as_before(tmp_path):
    m = _monitor(tmp_path, "idle_process")
    _write_activity(tmp_path, age_seconds=WAIT)
    d = m.check_detail()
    assert (d.verdict, d.reason) == ("terminate", "hard_idle")


def test_max_is_outside_the_grace(tmp_path):
    m = _monitor(tmp_path, "executing", max_threshold=60)
    m.started_at = time.time() - 120
    _write_activity(tmp_path, age_seconds=WAIT)
    m.check_detail()
    m.box["signal"] = "idle_process"
    assert m.check_detail().reason == "max_exceeded"


def test_watchdog_restart_forgets_and_judges_as_before(tmp_path):
    m = _monitor(tmp_path, "executing")
    _write_activity(tmp_path, age_seconds=WAIT)
    m.check_detail()
    fresh = _monitor(tmp_path, "idle_process")      # 再起動 = 新しい monitor
    assert fresh.check_detail().verdict == "terminate"


def test_next_attempt_does_not_inherit(tmp_path):
    """同じ Worker 名の次の試行は新しい monitor (task が外れると捨てられる)。起点を持ち越さない。"""
    old = _monitor(tmp_path, "executing")
    _write_activity(tmp_path, age_seconds=WAIT)
    old.check_detail()
    nxt = _monitor(tmp_path, "idle_process")
    assert nxt._executing_seen_at is None
    assert nxt.check_detail().verdict == "terminate"


def test_unknown_is_not_activity_and_does_not_move_the_origin(tmp_path):
    m = _monitor(tmp_path, "executing")
    _write_activity(tmp_path, age_seconds=WAIT)
    m.check_detail()
    seen = m._executing_seen_at
    m.box["signal"] = "unknown"
    d = m.check_detail()
    assert (d.verdict, d.reason) == ("warn", "hard_idle_but_process_unknown")
    assert d.idle_seconds >= WAIT - 5               # 生の無音のまま
    assert m._executing_seen_at == seen


def test_suppression_origin_is_still_the_raw_activity(tmp_path):
    m = _monitor(tmp_path, "executing")
    act = _write_activity(tmp_path, age_seconds=WAIT)
    m.check_detail()
    assert m.suppression_origin() == int(act.stat().st_mtime)
