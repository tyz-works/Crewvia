#!/usr/bin/env python3
"""
tests/test_usage_limit.py — 利用枠切れを普通の idle と区別する (C2 / t005)

## 背景 (2026-09-27 夜)

Worker の画面が `⚠ Usage limit reached · continuing automatically at 6pm` で止まったのを、
Rule 5 も watchdog も普通の idle と読んだ。Rule 5 は 5 分おきに 5 人分発火して 2 時間で約 80 通、
watchdog は idle×2 で Worker を終了させ、止まっていた時間も max に数えて Haruto を kill した。
枠はアカウント単位なので、起動し直しても回復しない (リセット後の再開の促しが正しい)。

## 方法

* 同定は `lib_usage_limit.detect()` の 1 か所 (dispatcher と watchdog が共有)。**位置と構造**
  (行頭の `⚠` + 入力欄の枠が直下) に束縛し、文言の部分一致にしない。実物の画面の形の記録が
  リポジトリに無いため、fixture は Claude Code の画面の構造 (本文 / 通知行 / 入力欄の枠 /
  ステータス行) をそのまま組んだもの。
* dispatcher は **本物の** dispatcher.sh の埋め込み python を `exec()` して `check_rule5()` を呼ぶ
  (`tests/test_background_work_is_not_idle.py` の Harness)。差し替えるのは mux だけ。
  リセット時刻の経過は、記録ファイル (`<name>.usage-limit.json`) の `reset_at` を書き換えて表す。
* watchdog は本物の `WorkerMonitor.check_detail()`。時計は `time.time` を差し替えた模擬時計。

fail の向き: 画面が読めない・同定できない → 常に「利用枠切れではない」(従来どおり通知/判定)。
免除には上限がある (`excuse_deadline`)。

実行: python3 -m pytest tests/test_usage_limit.py -v
"""

import json
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lib_mux  # noqa: E402
import lib_usage_limit  # noqa: E402
import watchdog  # noqa: E402
from test_dispatcher_notify_once import FakeMux, Harness, SLUG  # noqa: E402
from test_watchdog_idle import AGENT as WD_AGENT, WINDOW as WD_WINDOW, _make_monitor  # noqa: E402

AGENT = "sofia"
TARGET = f"{AGENT}-worker"

NOTICE = "⚠ Usage limit reached · continuing automatically at 6pm"
NOTICE_REL = "⚠ Usage limit reached · continuing automatically · +30m"


# ---------------------------------------------------------------------------
# 画面 fixture (Claude Code の構造: 本文 / 通知行 / 入力欄の枠 / ステータス行)
# ---------------------------------------------------------------------------

def _body():
    return [
        "⏺ Bash(pytest tests/test_x.py)",
        "  ⎿  ============ 12 passed in 3.2s ============",
        "",
        "⏺ テストは通りました。次に PR を作ります。",
        "",
    ]


def _box():
    return ["╭" + "─" * 70 + "╮", "│ ❯                                                                    │",
            "╰" + "─" * 70 + "╯"]


def _rules():
    return ["─" * 72, "❯ ", "─" * 72]


def _status():
    return ["  5h ██████████ 100% +1h57m   7d ▍░░░░░░░░░ 3%   Sonnet 5.5"]


def limited_screen(notice=NOTICE, frame=_box):
    return "\n".join(_body() + [notice] + frame() + _status())


def healthy_screen():
    return "\n".join(_body() + _box() + _status())


# ---------------------------------------------------------------------------
# 1. 同定 (lib_usage_limit.detect) — 位置と構造
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("frame", [_box, _rules], ids=["box", "rules"])
def test_a_notice_line_above_the_input_frame_is_a_usage_limit(frame):
    found = lib_usage_limit.detect(limited_screen(frame=frame))
    assert found is not None and found.notice == NOTICE


NOW = time.mktime((2026, 9, 30, 14, 0, 0, 0, 0, -1))   # ローカル 14:00


@pytest.mark.parametrize("text,delta", [
    ("continuing automatically at 6pm", 4 * 3600),
    ("continuing automatically at 11:16 PM", 9 * 3600 + 16 * 60),
    ("continuing automatically at 2pm", 24 * 3600),          # 過ぎた時刻は次の日
    ("continuing automatically at 18:00", 4 * 3600),
    ("continuing automatically · +1h57m", 1 * 3600 + 57 * 60),
    ("continuing automatically · +45m", 45 * 60),
])
def test_the_reset_time_is_read_from_the_notice(text, delta):
    found = lib_usage_limit.detect(limited_screen(f"⚠ Usage limit reached · {text}"), now=NOW)
    assert found is not None and found.reset_at == pytest.approx(NOW + delta, abs=1)


def test_an_unreadable_reset_time_is_unknown_not_a_failure():
    found = lib_usage_limit.detect(limited_screen("⚠ Usage limit reached"), now=NOW)
    assert found is not None and found.reset_at is None


def _negatives():
    frame = "\n".join(_box() + _status())
    return {
        # 4 (陰性): 文言が出力・パス・引用の中にあるだけ
        "in-a-path": "\n".join(_body() + [f"Working directory: /tmp/{NOTICE}"]) + "\n" + frame,
        "tool-output": "\n".join(_body() + [f"  ⎿  {NOTICE}"]) + "\n" + frame,
        "cat-output": "\n".join(_body() + ["⏺ Bash(cat notes.txt)", f"  ⎿  {NOTICE}"]) + "\n" + frame,
        "typed-input-echo": "\n".join(_body() + [f"❯ {NOTICE}"]) + "\n" + frame,
        "assistant-quote": "\n".join(_body() + [f"⏺ 画面には「{NOTICE}」と出ていました"]) + "\n" + frame,
        "indented": "\n".join(_body() + [f"  {NOTICE}"]) + "\n" + frame,
        # 本物の形の通知行でも、位置が違う: 入力欄の枠が直下に無い (スクロールで流れた古い通知)
        "scrolled-away": "\n".join(_body() + [NOTICE] + ["⏺ 再開しました", "  ⎿  ok", "⏺ 続き", "  ⎿ ok"]
                                   + _box() + _status()),
        "no-frame-at-all": "\n".join(_body() + [NOTICE]),
        "old-notice-far-above-the-tail": "\n".join([NOTICE] + ["⏺ x"] * 30 + _box() + _status()),
        # 読めない
        "empty": "",
        "blank-lines": "\n\n  \n",
    }


@pytest.mark.parametrize("name", sorted(_negatives()))
def test_a_screen_without_the_real_notice_position_is_not_a_usage_limit(name):
    assert lib_usage_limit.detect(_negatives()[name]) is None


@pytest.mark.parametrize("bad", [None, 0, b"bytes", ["list"], {"d": 1}])
def test_a_non_string_screen_is_not_a_usage_limit(bad):
    assert lib_usage_limit.detect(bad) is None


def test_the_reset_time_is_kept_while_the_same_notice_stays():
    """リセット時刻を過ぎても画面の `at 6pm` は残る。毎回計算し直すと「明日の 6pm」に化ける。"""
    first = lib_usage_limit.observe(None, limited_screen(), NOW)
    later = lib_usage_limit.observe(first, limited_screen(), NOW + 5 * 3600)   # 19:00
    assert later["reset_at"] == first["reset_at"] and later["first_seen"] == first["first_seen"]


def test_a_relative_notice_that_ticks_down_is_still_the_same_notice():
    a = lib_usage_limit.observe(None, limited_screen("⚠ Usage limit reached · +1h57m"), NOW)
    b = lib_usage_limit.observe(a, limited_screen("⚠ Usage limit reached · +1h56m"), NOW + 60)
    assert b["first_seen"] == a["first_seen"] and b["reset_at"] == a["reset_at"]


def test_a_different_notice_starts_a_new_usage_limit():
    a = lib_usage_limit.observe(None, limited_screen(), NOW)
    b = lib_usage_limit.observe(a, limited_screen("⚠ Usage limit reached · continuing automatically at 11pm"),
                                NOW + 100)
    assert b["first_seen"] == NOW + 100


# ---------------------------------------------------------------------------
# 2. dispatcher (Rule 5)
# ---------------------------------------------------------------------------

class PaneMux(FakeMux):
    """herdr の agent_status と capture を、テストが決める。Worker 宛の send も記録する。"""

    pane_state = "idle"
    screen = ""
    capture_raises = False

    def state(self, *a, **kw):
        return PaneMux.pane_state

    def capture(self, *a, **kw):
        if PaneMux.capture_raises:
            raise RuntimeError("capture failed")
        return PaneMux.screen

    def pid(self, name):
        return None

    def send(self, name, text):
        if name == TARGET:
            FakeMux.sent.append({"target": name, "message": text})
            return True
        return FakeMux.send(self, name, text)


class Rule5:
    def __init__(self, h, monkeypatch):
        # ファイル先頭で import した lib_mux ではなく、いま sys.modules にあるもの (dispatcher の
        # 埋め込み python が `from lib_mux import Mux` で拾う方) を差し替える。他のテストが
        # lib_mux を読み直していると、両者は別のモジュールオブジェクトになる (全 suite の中でだけ
        # 実 Mux が使われて赤くなる)。
        live_mux = sys.modules["lib_mux"]
        monkeypatch.setattr(live_mux, "Mux", PaneMux)
        monkeypatch.setattr(live_mux, "repo_identity_ok", lambda *a, **kw: True)
        PaneMux.pane_state, PaneMux.screen, PaneMux.capture_raises = "idle", "", False
        self.h = h
        h.cycle()
        self.ns = h.ns
        self.assignment = self.ns["ASSIGNMENTS_DIR"] / AGENT
        self.assignment.parent.mkdir(parents=True, exist_ok=True)
        self.assignment.write_text(f"{SLUG}:t001\n")
        self.entry_path = h.registry / "mux" / f"{AGENT}.usage-limit.json"

    def age_state(self, seconds=600):
        self.ns["_save_state_entry"](AGENT, "idle-with-task", time.time() - seconds)

    def run(self):
        FakeMux.sent = []
        self.ns["check_rule5"](AGENT, TARGET, self.assignment, {})
        return list(FakeMux.sent)

    def to_director(self, sent):
        return [m["message"] for m in sent if m["target"] != TARGET]

    def to_worker(self, sent):
        return [m["message"] for m in sent if m["target"] == TARGET]

    def entry(self):
        return json.loads(self.entry_path.read_text())

    def rewrite_entry(self, **fields):
        data = self.entry()
        data.update(fields)
        self.entry_path.write_text(json.dumps(data))
        # 通知のスロットル (NOTIFY_TTL) は時間の経過で切れる。ここでは経過済みにする。
        if self.h.notify_cache.exists():
            self.h.notify_cache.unlink()


@pytest.fixture
def r5(tmp_path, monkeypatch):
    return Rule5(Harness(tmp_path / "repo", monkeypatch), monkeypatch)


def test_a_usage_limited_worker_gets_one_director_notice_and_no_rule5(r5):
    """(1) Rule 5 は出ず、Director への通知は 1 回だけ (何サイクル回しても)。"""
    PaneMux.screen = limited_screen(NOTICE_REL)
    r5.age_state()
    first = r5.run()
    assert len(r5.to_director(first)) == 1
    msg = r5.to_director(first)[0]
    assert "利用枠切れ" in msg and AGENT in msg and "idle-with-task" not in msg
    assert "時刻不明" not in msg          # `+30m` を読めている
    for _ in range(5):
        r5.age_state()
        assert r5.run() == []            # 台帳が重複を止める (Rule 5 も黙る)
    # 台帳は 1 件、mission ではなく '_daemon' (prune_told に捨てられない)
    told = json.loads((r5.h.registry / "daemons" / "notified-state.json").read_text())
    assert told[f"usage-limit_{AGENT}"]["slug"] == "_daemon"


def test_the_notice_says_unknown_when_the_reset_time_is_unreadable(r5):
    PaneMux.screen = limited_screen("⚠ Usage limit reached")
    r5.age_state()
    msgs = r5.to_director(r5.run())
    assert len(msgs) == 1 and "時刻不明" in msgs[0]


def test_the_same_pane_without_the_notice_is_still_rule5(r5):
    """対照: 通知行が無ければ従来どおり idle-with-task (常に黙る実装を弾く)。"""
    PaneMux.screen = healthy_screen()
    r5.age_state()
    msgs = r5.to_director(r5.run())
    assert len(msgs) == 1 and "idle-with-task" in msgs[0]
    assert not r5.entry_path.exists()


@pytest.mark.parametrize("name", ["in-a-path", "tool-output", "cat-output", "typed-input-echo",
                                  "assistant-quote", "scrolled-away"])
def test_the_notice_text_elsewhere_on_the_screen_is_not_a_usage_limit(r5, name):
    """(4) 陰性: 文言が出力・パスの中にあるだけの画面は、通常の Rule 5 のまま。"""
    PaneMux.screen = _negatives()[name]
    r5.age_state()
    msgs = r5.to_director(r5.run())
    assert len(msgs) == 1 and "idle-with-task" in msgs[0]
    assert not r5.entry_path.exists()


def test_an_unreadable_screen_falls_to_the_normal_rule5(r5):
    """画面が読めない → 利用枠切れではない (従来どおり通知する側)。"""
    PaneMux.capture_raises = True
    r5.age_state()
    msgs = r5.to_director(r5.run())
    assert len(msgs) == 1 and "idle-with-task" in msgs[0]


def test_after_the_reset_time_the_worker_is_nudged_exactly_once(r5):
    """(2) リセット時刻 + 猶予を過ぎたら、Worker に再開の促しを 1 回だけ送る。"""
    PaneMux.screen = limited_screen(NOTICE_REL)
    r5.age_state()
    r5.run()                                          # 最初の観測 (Director に 1 回)
    assert r5.to_worker(r5.run()) == []               # まだリセット前 → 促さない
    r5.rewrite_entry(reset_at=time.time() - 600)      # リセット時刻を過ぎた
    sent = r5.run()
    assert len(r5.to_worker(sent)) == 1 and "再開" in r5.to_worker(sent)[0]
    for _ in range(4):
        r5.age_state()
        assert r5.to_worker(r5.run()) == []           # 2 回目は送らない
    assert r5.entry()["resumed_at"] is not None


def test_no_nudge_within_the_grace_right_after_the_reset(r5):
    PaneMux.screen = limited_screen(NOTICE_REL)
    r5.age_state()
    r5.run()
    r5.rewrite_entry(reset_at=time.time() - 10)       # リセットの 10 秒後 (猶予 120 秒の内)
    assert r5.to_worker(r5.run()) == []


def test_still_limited_after_the_nudge_notifies_the_director_once_more(r5):
    """促した後もなお利用枠切れのままなら、Director に 1 回だけ再通知する。"""
    PaneMux.screen = limited_screen(NOTICE_REL)
    r5.age_state()
    r5.run()
    r5.rewrite_entry(reset_at=time.time() - 600)
    r5.run()                                          # 促し
    r5.rewrite_entry(resumed_at=time.time() - 600)
    again = r5.to_director(r5.run())
    assert len(again) == 1 and "再開を促した後も" in again[0]
    for _ in range(3):
        r5.age_state()
        assert r5.to_director(r5.run()) == []         # それ以上は送らない


def test_the_reset_time_passing_does_not_turn_into_tomorrow(r5):
    """`at 6pm` が画面に残ったままリセット時刻を過ぎても、時刻を計算し直さず促しが出る。"""
    PaneMux.screen = limited_screen(NOTICE)
    r5.age_state()
    r5.run()
    r5.rewrite_entry(reset_at=time.time() - 600)      # 記録上は過去 (画面は `at 6pm` のまま)
    assert len(r5.to_worker(r5.run())) == 1
    assert r5.entry()["reset_at"] < time.time()        # 明日に化けていない


def test_the_exemption_has_an_upper_bound_and_then_rule5_returns(r5):
    """(5) 上限 (リセット予定 + 1 時間) を過ぎても利用枠切れのままなら、通常の Rule 5 に戻る。"""
    PaneMux.screen = limited_screen(NOTICE_REL)
    r5.age_state()
    r5.run()
    r5.rewrite_entry(reset_at=time.time() - lib_usage_limit.GRACE_AFTER_RESET_SECONDS - 60)
    r5.age_state()
    msgs = r5.to_director(r5.run())
    assert any("idle-with-task" in m for m in msgs)


def test_an_unknown_reset_time_is_bounded_too(r5):
    PaneMux.screen = limited_screen("⚠ Usage limit reached")
    r5.age_state()
    r5.run()
    r5.rewrite_entry(first_seen=time.time() - lib_usage_limit.UNKNOWN_RESET_MAX_SECONDS - 60)
    r5.age_state()
    assert any("idle-with-task" in m for m in r5.to_director(r5.run()))


def test_when_the_notice_goes_away_the_record_and_ledger_entry_are_cleared(r5):
    """解消したら記録も台帳も畳む。次に枠切れになったらまた 1 回通知する。"""
    PaneMux.screen = limited_screen(NOTICE_REL)
    r5.age_state()
    assert len(r5.to_director(r5.run())) == 1
    PaneMux.screen = healthy_screen()
    r5.run()
    assert not r5.entry_path.exists()
    told = json.loads((r5.h.registry / "daemons" / "notified-state.json").read_text())
    assert f"usage-limit_{AGENT}" not in told
    PaneMux.screen = limited_screen(NOTICE_REL)
    r5.h.notify_cache.unlink(missing_ok=True)
    assert len(r5.to_director(r5.run())) == 1


def test_a_corrupt_record_does_not_make_the_notice_repeat(r5):
    """記録ファイルが壊れていても、通知は台帳 (通知行の同一性) が 1 回に止める。"""
    PaneMux.screen = limited_screen(NOTICE_REL)
    r5.age_state()
    assert len(r5.to_director(r5.run())) == 1
    for _ in range(3):
        r5.entry_path.write_text("{not json")
        r5.age_state()
        assert r5.to_director(r5.run()) == []


# ---------------------------------------------------------------------------
# 3. watchdog (idle / max)
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self, t0):
        self.t = t0

    def __call__(self):
        return self.t


class ScreenMux:
    def __init__(self):
        self.screen = ""

    def list(self, suffix=None):
        return [WD_WINDOW]

    def pid(self, name):
        return None

    def capture(self, name):
        return self.screen


@pytest.fixture
def wd(tmp_path, monkeypatch):
    """idle 300 / max 3600 の monitor。時計と画面はテストが動かす。プロセス層は idle_process 固定
    (hard idle が terminate になる条件。ここの主題ではない)。"""
    mux = ScreenMux()
    clock = Clock(time.time())
    monkeypatch.setattr(watchdog, "_mux", mux)
    monkeypatch.setattr(watchdog.time, "time", clock)
    monkeypatch.setattr(watchdog.WorkerMonitor, "_process_signal", lambda self: "idle_process")
    monitor = _make_monitor(tmp_path, idle=300, max_threshold=3600, pulled_seconds_ago=0)
    monitor.started_at = clock.t
    monitor.monitoring_since = clock.t - 86400          # activity file の古さで idle を作る
    act = tmp_path / "registry" / "activity" / WD_AGENT / "t999.activity"
    act.parent.mkdir(parents=True, exist_ok=True)
    act.write_text("tool-use\n")

    class W:
        pass
    w = W()
    w.mux, w.clock, w.monitor, w.act = mux, clock, monitor, act

    def touch(age=0):
        import os
        os.utime(act, (clock.t - age, clock.t - age))
    w.touch = touch

    def advance(seconds, step=30):
        out = None
        for _ in range(int(seconds // step)):
            clock.t += step
            out = monitor.check_detail()
        return out
    w.advance = advance
    return w


def test_idle_does_not_terminate_a_usage_limited_worker(wd):
    """(3a) 利用枠切れの間は、idle が hard threshold を超えても終了させない。"""
    wd.touch(age=100)
    wd.mux.screen = limited_screen(NOTICE_REL)
    res = wd.advance(1800)                                   # idle 1900s > 300*2
    assert res.verdict == "alive" and res.reason == "usage_limit"


def test_the_same_idle_without_the_notice_still_terminates(wd):
    """対照: 通知行が無ければ従来どおり hard idle で終了 (常に免除する実装を弾く)。"""
    wd.touch(age=100)
    wd.mux.screen = healthy_screen()
    res = wd.advance(1800)
    assert res.verdict == "terminate" and res.reason == "hard_idle"


@pytest.mark.parametrize("name", ["in-a-path", "tool-output", "cat-output", "typed-input-echo",
                                  "assistant-quote", "scrolled-away"])
def test_the_notice_text_elsewhere_does_not_excuse_idle(wd, name):
    """(4) 陰性: 文言が出力・パスの中にあるだけの画面は免除しない。"""
    wd.touch(age=100)
    wd.mux.screen = _negatives()[name]
    assert wd.advance(1800).verdict == "terminate"


def test_time_spent_usage_limited_is_not_counted_toward_max(wd):
    """(3b) 利用枠切れで止まっていた時間は max に数えない。"""
    wd.touch()
    wd.mux.screen = healthy_screen()
    wd.advance(600)                                          # 通常の 10 分
    wd.mux.screen = limited_screen(NOTICE_REL)
    wd.advance(3600 * 2)                                     # 2 時間、枠切れ (max 3600 の倍)
    wd.mux.screen = healthy_screen()
    res = None
    for _ in range(2):                                       # リセット後に再開 (活動が続く)
        wd.touch()
        res = wd.advance(300)
    assert res.verdict == "alive", res                       # 経過は 2h20m でも、数えるのは ~20 分
    # そして通常の時間は今までどおり max に数える
    for _ in range(12):
        wd.touch()
        res = wd.advance(300)
    assert res.verdict == "terminate" and res.reason == "max_exceeded"


def test_without_the_notice_the_same_wall_clock_exceeds_max(wd):
    """対照: 枠切れの表示が無い 2 時間は max を超える。"""
    wd.touch()
    wd.mux.screen = healthy_screen()
    res = wd.advance(3600 + 60)
    assert res.verdict == "terminate" and res.reason == "max_exceeded"


def test_idle_restarts_from_the_end_of_the_limit_not_from_the_stale_silence(wd):
    """リセット後に表示が消えても、止まっていた沈黙で即 hard idle にならない。"""
    wd.touch(age=100)
    wd.mux.screen = limited_screen(NOTICE_REL)
    wd.advance(1800)
    wd.mux.screen = healthy_screen()                         # 表示が消えた (まだ activity は無い)
    assert wd.advance(30).verdict == "alive"
    assert wd.advance(300).verdict == "warn"                 # そこから数えて soft idle
    assert wd.advance(600).verdict == "terminate"            # そこから数えて hard idle


def test_the_exemption_has_an_upper_bound_and_the_director_is_told(wd):
    """(5) 上限 (時刻不明なら見え始め + 6 時間) を過ぎたら、通常の判定に戻り Director に知らせる。"""
    wd.touch(age=100)
    wd.mux.screen = limited_screen("⚠ Usage limit reached")   # 時刻不明
    assert wd.advance(3600).verdict == "alive"
    assert wd.monitor.usage_limit_overdue_message() is None
    wd.advance(lib_usage_limit.UNKNOWN_RESET_MAX_SECONDS, step=600)
    res = wd.monitor.check_detail()
    assert res.verdict == "terminate"
    assert wd.monitor.usage_limit_overdue_message() is not None


def test_the_known_reset_time_bounds_the_exemption(wd):
    wd.touch(age=100)
    wd.mux.screen = limited_screen(NOTICE_REL)                # +30m
    assert wd.advance(600).verdict == "alive"
    wd.advance(1800 + lib_usage_limit.GRACE_AFTER_RESET_SECONDS, step=300)
    # 免除が終わった後は、最後に免除した瞬間から idle を数える (hard idle = 600s)
    assert wd.advance(900, step=300).verdict == "terminate"


def test_an_unreadable_screen_is_not_a_usage_limit_for_the_watchdog(wd, monkeypatch):
    """画面が読めない → 免除しない (従来どおり)。"""
    wd.touch(age=100)

    def boom(name):
        raise RuntimeError("capture failed")
    monkeypatch.setattr(wd.mux, "capture", boom)
    assert wd.advance(1800).verdict == "terminate"


def test_one_long_unobserved_gap_is_not_credited_to_the_limit(wd):
    """観測できなかった (watchdog が止まっていた) 長い空白は、免除した時間として max から除かない。"""
    wd.touch()
    wd.mux.screen = limited_screen("⚠ Usage limit reached")   # 時刻不明 (上限は 6 時間先)
    wd.advance(60)
    wd.clock.t += 3600 * 3                                   # watchdog が 3 時間動いていなかった
    wd.monitor.check_detail()
    assert wd.monitor._limit_excluded <= watchdog.WorkerMonitor.LIMIT_EXCLUDE_MAX_STEP + 60
