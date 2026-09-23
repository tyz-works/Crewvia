#!/usr/bin/env python3
"""tests/test_watchdog_stale_signal_floor.py

**監視が始まる前の沈黙を、この監視対象の沈黙として数えない** (t044)。

## 現象 (2026-09-23 に本番で発生)

起動して数秒の Worker が `hard_idle` で terminate された:

    [verdict] Seo/t007 terminate idle=64877s idle_threshold=1800 max_threshold=7200
              process=idle_process awaiting_human=false reason=hard_idle
    TERMINATE: Seo/t007 elapsed=0s          ← 起動直後
    [retire] Seo: SIGTERM → SIGKILL → cleaned up (status→pending, assignment removed)

## 真因

`WorkerMonitor._last_activity_mtime()` は

    return max(candidates) if candidates else self.started_at

だった。`started_at` は **候補が 1 つも無いときの fallback にしか使われない**。
候補は 3 つで、うち 2 つ (`registry/heartbeats/<agent>`,
`registry/notifications/<agent>/*`) は **agent 単位**、すなわち task を跨いで
残る。今回は t007 の activity ファイルがまだ無く、前日 18 時間前の heartbeat が
唯一の候補だったので idle=64877s になった。

crewvia は Worker 名を再利用する設計 (同じスキルの Worker は同じ名前を継承する)
なので、**同じ名前が別の task で起動されるたびにこの穴が開く**。運用では
Director が起動前に `touch registry/heartbeats/<agent>` して回避していた。

## 直し方

基準を「**この監視対象が始まった時刻** (floor) と、floor 以降のシグナルの
うち最新のもの」にする。floor より古いシグナルは、この監視対象の沈黙では
ないので候補から落とす。

floor は **task frontmatter の `started_at` (= Worker が pull した時刻) と
監視オブジェクト生成時刻の早いほう**。単に生成時刻にすると watchdog を
再起動するたびにハング中の Worker の idle 時計が 0 に戻り、**反対側の欠陥**
(本当にハングした Worker が検知されない) を作ってしまう。
`test_a_hung_worker_is_still_terminated_after_a_watchdog_restart` がそれを固定する。

同じ floor を notification 側にも当てる。前の task で鳴った通知が
`_awaiting_human()` を永久に True にすると、やはりハングが検知されなくなる。
「監視開始前のシグナルはこの監視対象のものではない」という判断はひとつなので、
読み手 3 か所 (activity/heartbeat/notification) すべてに同じものを当てる。

    python3 -m pytest tests/test_watchdog_stale_signal_floor.py -v
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import watchdog  # noqa: E402


AGENT = "FloorWorker"
TASK_ID = "t777"
WINDOW = f"{AGENT}-worker"

IDLE = 300          # soft しきい値。hard は その 2 倍 = 600s
DAY = 86400


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(epoch: float) -> str:
    """plan.sh が frontmatter に書くのと同じ形 (小数秒 + Z)。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + \
        f".{int((epoch % 1) * 1_000_000):06d}Z"


def _monitor(tmp_path: Path, *, pulled_seconds_ago=None, started_at="__auto__",
             idle=IDLE, max_threshold=7200):
    """WorkerMonitor を、task frontmatter 相当の dict から組み立てる。

    `pulled_seconds_ago` は「Worker が task を pull してからの経過秒」。
    `started_at` を直接渡せば生の値 (欠損・壊れた値・未来) も試せる。
    """
    card = {
        "worker": AGENT,
        "timeout": {"idle": idle, "max": max_threshold},
    }
    if started_at != "__auto__":
        card["started_at"] = started_at
    elif pulled_seconds_ago is not None:
        card["started_at"] = _iso(time.time() - pulled_seconds_ago)

    monitor = watchdog.WorkerMonitor(
        task_id=TASK_ID, task_card=card,
        profiles=watchdog.PROFILES, repo_root=tmp_path,
    )
    # プロセス層と mux は確かめたい層ではない。既存の test_watchdog_idle.py と
    # 同じ形で差し替える (プロセス層そのものは向こうが実プロセスで検証済み)。
    monitor._mux_window_name = lambda: WINDOW
    monitor._process_signal = lambda: "idle_process"
    return monitor


def _age(path: Path, seconds: float) -> Path:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


def _write_activity(tmp_path: Path, age_seconds: float) -> Path:
    p = tmp_path / "registry" / "activity" / AGENT / f"{TASK_ID}.activity"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("tool-use\n")
    return _age(p, age_seconds)


def _write_heartbeat(tmp_path: Path, age_seconds: float) -> Path:
    p = tmp_path / "registry" / "heartbeats" / AGENT
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("alive\n")
    return _age(p, age_seconds)


def _write_notification(tmp_path: Path, age_seconds: float,
                        notification_type="permission_request") -> Path:
    p = tmp_path / "registry" / "notifications" / AGENT / "n1.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"notification_type": notification_type}))
    return _age(p, age_seconds)


# ===========================================================================
# 本番で起きたこと
# ===========================================================================

def test_a_fresh_worker_is_not_terminated_by_yesterdays_agent_heartbeat(tmp_path):
    """**2026-09-23 の再現**。pull した直後で activity はまだ無く、agent 単位の
    heartbeat だけが前日のまま残っている。

    欠陥版は候補が heartbeat 1 つだけなので idle=18h と判定し、起動 0 秒の
    Worker を terminate する。

    これが緑になることで、運用回避策 (起動前に
    `touch registry/heartbeats/<agent>`) が不要になる。
    """
    monitor = _monitor(tmp_path, pulled_seconds_ago=2)
    _write_heartbeat(tmp_path, age_seconds=18 * 3600)

    detail = monitor.check_detail()
    assert detail.verdict == "alive", (
        f"起動直後の Worker が {detail.reason} で {detail.verdict} にされた "
        f"(idle={detail.idle_seconds:.0f}s)")
    assert detail.idle_seconds < 60, (
        f"監視開始前の沈黙が idle に数えられている: {detail.idle_seconds:.0f}s")


def test_a_fresh_worker_is_not_terminated_by_a_stale_notification(tmp_path):
    """notification も agent 単位で task を跨いで残る。前の task の通知だけが
    ある状態で pull 直後の Worker が terminate されてはいけない。"""
    monitor = _monitor(tmp_path, pulled_seconds_ago=2)
    _write_notification(tmp_path, age_seconds=18 * 3600)

    detail = monitor.check_detail()
    assert detail.verdict == "alive", (
        f"前の task の通知で {detail.verdict} ({detail.reason}, "
        f"idle={detail.idle_seconds:.0f}s)")


# ===========================================================================
# 反対側の欠陥を作らない — 本当にハングした Worker は検知されること
# ===========================================================================

def test_a_hung_worker_is_still_terminated_after_a_watchdog_restart(tmp_path):
    """**floor を「監視オブジェクト生成時刻」だけにすると落ちる**。

    task は 3 時間前に pull され、Worker はシグナルを 1 つも出していない
    (起動直後に固まった)。watchdog を再起動すると監視オブジェクトは作り直され、
    生成時刻は「今」になる。生成時刻を floor にすると idle=0 → alive で、
    ハングした Worker が再起動のたびに延命される。

    floor を「task の pull 時刻と生成時刻の早いほう」にすると 3 時間の沈黙が
    残るので terminate される。欠陥版でも `candidates` が空なので
    `self.started_at` (= 今) に落ちて alive になる。
    """
    monitor = _monitor(tmp_path, pulled_seconds_ago=3 * 3600)

    detail = monitor.check_detail()
    assert detail.verdict == "terminate", (
        f"3 時間沈黙している Worker が {detail.verdict} "
        f"({detail.reason}, idle={detail.idle_seconds:.0f}s)")


def test_a_stale_notification_does_not_hold_a_hung_worker_alive(tmp_path):
    """監視開始より前の通知が `_awaiting_human()` を True にし続けてはいけない。

    通知は 1 回しか鳴らず、その後 activity も heartbeat も止まる。前の task の
    通知が残っているだけで「人間待ち」と見なすと、今の task でハングした
    Worker が永久に warn のままになる。
    """
    monitor = _monitor(tmp_path, pulled_seconds_ago=3 * 3600)
    _write_notification(tmp_path, age_seconds=18 * 3600)

    assert monitor._awaiting_human() is False, \
        "監視開始前の通知が「人間待ち」と見なされた"
    detail = monitor.check_detail()
    assert detail.verdict == "terminate", (
        f"古い通知で terminate が抑制された: {detail.verdict} ({detail.reason})")


def test_silence_inside_the_window_is_still_counted(tmp_path):
    """floor 以降に一度動いてから止まった Worker は、その沈黙で判定される。"""
    monitor = _monitor(tmp_path, pulled_seconds_ago=3 * 3600)
    _write_activity(tmp_path, age_seconds=2 * 3600)

    detail = monitor.check_detail()
    assert detail.verdict == "terminate", \
        f"2 時間の沈黙が数えられていない: {detail.verdict} ({detail.reason})"
    assert 7000 < detail.idle_seconds < 7400, (
        f"idle が activity の mtime から測られていない: "
        f"{detail.idle_seconds:.0f}s")


# ===========================================================================
# 可用性 — 既存の挙動を壊さない
# ===========================================================================

def test_a_notification_inside_the_window_still_suppresses_terminate(tmp_path):
    """監視開始より後の通知は、これまで通り terminate を warn に落とす。"""
    monitor = _monitor(tmp_path, pulled_seconds_ago=3 * 3600)
    _write_notification(tmp_path, age_seconds=30 * 60)

    detail = monitor.check_detail()
    assert detail.awaiting_human is True
    assert detail.verdict == "warn", \
        f"人間待ちの Worker が {detail.verdict} ({detail.reason})"


def test_activity_after_a_notification_clears_it(tmp_path):
    """通知のあとに実活動があれば解除済み — 既存の判定のまま。"""
    monitor = _monitor(tmp_path, pulled_seconds_ago=3 * 3600)
    _write_notification(tmp_path, age_seconds=2 * 3600)
    _write_heartbeat(tmp_path, age_seconds=1 * 3600)

    assert monitor._awaiting_human() is False
    assert monitor.check_detail().verdict == "terminate"


def test_fresh_activity_is_alive(tmp_path):
    """働いている Worker はこれまで通り alive。"""
    monitor = _monitor(tmp_path, pulled_seconds_ago=3 * 3600)
    _write_activity(tmp_path, age_seconds=5)

    assert monitor.check_detail().verdict == "alive"


def test_soft_idle_still_warns(tmp_path):
    """soft しきい値の段は変えていない。"""
    monitor = _monitor(tmp_path, pulled_seconds_ago=3 * 3600)
    _write_activity(tmp_path, age_seconds=450)   # 300 < 450 <= 600

    assert monitor.check_detail().verdict == "warn"


# ===========================================================================
# floor が読めないとき
# ===========================================================================

@pytest.mark.parametrize("started_at", [
    None,                       # frontmatter に無い
    "",                         # 空
    "null",                     # plan.sh --reset の残り / 引用符付き null
    "not-a-timestamp",          # 壊れている
    "2026-99-99T00:00:00Z",     # 形は timestamp だが日付が存在しない
])
def test_an_unusable_started_at_falls_back_to_when_monitoring_began(
        tmp_path, started_at):
    """読めない `started_at` で落ちない。読めないときは監視開始時刻に倒す。

    倒す先が「今」なので、前の task の heartbeat では terminate されない。
    ハングの検知は watchdog 再起動ぶん遅れるが、それは欠陥版と同じ挙動であって
    退行ではない (読める値なら遅れない — 下のテスト)。
    """
    monitor = _monitor(tmp_path, started_at=started_at)
    _write_heartbeat(tmp_path, age_seconds=18 * 3600)

    detail = monitor.check_detail()
    assert detail.verdict == "alive", (
        f"started_at={started_at!r} で {detail.verdict} "
        f"({detail.reason}, idle={detail.idle_seconds:.0f}s)")


def test_a_started_at_without_fractional_seconds_is_understood(tmp_path):
    """小数秒なしの形 (`...:49Z`) も読む。

    `started_at` は t021 以降 小数秒付きで書かれるが、それ以前に pull された
    task の card はまだ秒精度で残っている。読めずに fallback すると、その
    task だけ watchdog 再起動でハング検知が遅れる。

    「読めている」ことを示すために 2 方向から見る — 直後なら alive、
    3 時間前なら terminate。fallback していれば後者は alive になる。
    """
    def second_precision(seconds_ago):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() - seconds_ago))

    fresh = _monitor(tmp_path, started_at=second_precision(2))
    _write_heartbeat(tmp_path, age_seconds=18 * 3600)
    assert fresh.check_detail().verdict == "alive"

    hung = _monitor(tmp_path, started_at=second_precision(3 * 3600))
    detail = hung.check_detail()
    assert detail.verdict == "terminate", (
        f"秒精度の started_at が読めていない (fallback した): "
        f"{detail.verdict} / idle={detail.idle_seconds:.0f}s")


def test_a_started_at_in_the_future_does_not_make_idle_negative(tmp_path):
    """時刻がずれて `started_at` が未来でも、floor は監視開始時刻より後にしない。

    未来を floor にすると idle が負になり、どれだけ沈黙しても alive のままに
    なる = ハングが永久に検知されない。
    """
    monitor = _monitor(tmp_path, started_at=_iso(time.time() + DAY))
    _write_heartbeat(tmp_path, age_seconds=18 * 3600)

    detail = monitor.check_detail()
    assert detail.idle_seconds >= 0, f"idle が負: {detail.idle_seconds}"
    assert detail.verdict == "alive"
