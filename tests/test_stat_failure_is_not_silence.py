#!/usr/bin/env python3
"""**stat の失敗を「活動なし」と読まない** (Codex 8 巡目 P1, t017)。

## 指摘

t016 で `_notification_files()` には「観測できなかった」を入れたが、
`WorkerMonitor._mtimes_since_floor()` は **すべての `OSError` を `continue`**
のままだった。`knowledge/empty-vs-unobservable.md` §3 はここを明示的に後回しと
書いていて、その理由はこうだった ——

> D を直した後にここへ届くのは `activity` と `heartbeat` の 2 ファイルだけで、
> **両方とも「まだ無い」のが普通の状態**である。ここで欠損を観測不能に倒すと、
> 正常な起動直後が毎回「観測不能」になり、ハングした Worker が永久に検知され
> なくなる。

この理由づけは **「無い」と「読めない」を同じものとして扱っている**。「まだ無い」
が普通なのは事実だが、それは `ENOENT` の話であって、`EACCES` や `EIO` の話では
ない。`registry/activity/<agent>/` と `registry/heartbeats/` が検索権限を失うと

  * `activity` の stat が `EACCES`  → 候補から落ちる
  * `heartbeat` の stat が `EACCES` → 候補から落ちる
  * `notifications/` は存在しない   → `[]` (これは正しい)

となり、候補が 1 つも残らない。`_last_activity_mtime()` は
`max(candidates + [floor])` なので **floor そのもの** を返す。floor は
「この監視対象が始まった時刻」なので、数時間前に pull された健全な Worker は
`idle = 数時間` と判定され、`hard_idle` で **terminate される**。

つまり t016 で直したのと **完全に同じ型** —— 観測の失敗を「無い」と読み、
危険な結論 (Worker の終了) の側に落ちる —— が、別モジュールに残っていた。
前ミッションで P1 を 39 件生んだ `evidence-for-destructive-decisions` の型。

## 直し方

`ENOENT` **だけ**を「本当に無い」とする (allowlist。
memory: approve-judgment-needs-allowlist-and-scope)。それ以外の `OSError` は
*観測の失敗* なので、沈黙を主張しない側に倒す。これで §3 の懸念は解ける ——
起動直後に activity ファイルが無いのは `ENOENT` なので、これまで通り「無い」
として数えられ、ハングの検知は鈍らない。下の
`test_a_missing_activity_file_is_still_genuine_silence` がそれを固定する。

列挙の **あと** の stat も同じ。`_notification_files()` 自体は t016 で直って
いる (`Path.is_file()` は `EACCES` を握り潰さないので、列挙の内側で権限を失えば
そのまま「観測不能」になる)。残っていたのは **読み手 2 者** のほうで、
`_mtimes_since_floor()` と `_newest_notification()` がどちらも
`except OSError: continue` を持っている。列挙と stat の間に権限が変われば、
通知はどちらからも黙って消え、抑制が外れる。

    python3 -m pytest tests/test_stat_failure_is_not_silence.py -v
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import watchdog  # noqa: E402


AGENT = "StatWorker"
TASK_ID = "t808"
WINDOW = f"{AGENT}-worker"

IDLE = 300          # soft しきい値。hard は その 2 倍 = 600s
HOURS_3 = 3 * 3600


# ---------------------------------------------------------------------------
# helpers (tests/test_watchdog_stale_signal_floor.py と同じ組み立て方)
# ---------------------------------------------------------------------------

def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + \
        f".{int((epoch % 1) * 1_000_000):06d}Z"


def _monitor(tmp_path: Path, *, pulled_seconds_ago: float, idle=IDLE,
             max_threshold=7200):
    card = {
        "worker": AGENT,
        "started_at": _iso(time.time() - pulled_seconds_ago),
        "timeout": {"idle": idle, "max": max_threshold},
    }
    monitor = watchdog.WorkerMonitor(
        task_id=TASK_ID, task_card=card,
        profiles=watchdog.PROFILES, repo_root=tmp_path,
    )
    # 確かめたいのはシグナル層だけ。プロセス層と mux は既存テストが実プロセスで
    # 見ているので、ここでは固定する (fixture は確かめたい層だけを壊す。
    # memory: red-proof-catches-tests-green-for-the-wrong-reason)。
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


class _unsearchable:
    """ディレクトリから **検索 (x) 権限** を外す。stat が `EACCES` で落ちる。

    root では権限が効かないので skip する。後始末は必ず戻す —— 戻せないと
    `tmp_path` の掃除が失敗して、無関係なテストが赤くなる。
    """

    def __init__(self, directory: Path, mode: int = 0o000):
        self.directory = directory
        self.mode = mode

    def __enter__(self) -> Path:
        self.directory.chmod(self.mode)
        probe = self.directory / "probe-for-search-permission"
        try:
            os.stat(probe)
        except PermissionError:
            return self.directory
        except OSError:
            pass
        self.directory.chmod(0o755)
        pytest.skip("検索権限を落とせない環境 (root?)")

    def __exit__(self, *exc):
        self.directory.chmod(0o755)
        return False


# ===========================================================================
# P1: stat の失敗が terminate の側に落ちない
# ===========================================================================

def test_an_unsearchable_activity_dir_is_not_counted_as_silence(tmp_path):
    """**指摘の再現**。activity は *いま書かれている* のに、ディレクトリの検索
    権限が無いので stat できない。

    RED (`except OSError: continue` のまま): 候補が 0 件になり
    `_last_activity_mtime()` は floor (= 3 時間前の pull 時刻) を返す。
    idle=3h > hard しきい値 600s で `hard_idle` → **健全な Worker を terminate**。
    """
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    activity = _write_activity(tmp_path, age_seconds=5)

    with _unsearchable(activity.parent):
        detail = monitor.check_detail()

    assert detail.verdict != "terminate", (
        f"activity を観測できなかっただけで終了させた: "
        f"{detail.verdict} / {detail.reason} / idle={detail.idle_seconds:.0f}s")


def test_an_unsearchable_heartbeat_dir_is_not_counted_as_silence(tmp_path):
    """heartbeat 側も同じ。こちらは `registry/heartbeats/` が agent 単位ですら
    なく全 Worker 共通なので、1 回の権限事故で **全員** が終了対象になる。"""
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    heartbeat = _write_heartbeat(tmp_path, age_seconds=5)

    with _unsearchable(heartbeat.parent):
        detail = monitor.check_detail()

    assert detail.verdict != "terminate", (
        f"heartbeat を観測できなかっただけで終了させた: "
        f"{detail.verdict} / {detail.reason} / idle={detail.idle_seconds:.0f}s")


def _lose_permission_after_listing(monitor, notif_dir: Path):
    """`_notification_files()` が返った **後** に、そのディレクトリの検索権限を
    落とす。列挙と stat の間に権限が変わる、という実際に起こりうる並びを作る。

    差し替えるのは *協力者* だけで、戻り値は本物のまま —— 確かめたいのは
    「列挙したあとの stat の失敗を、読み手 2 者がどう扱うか」であって
    `_notification_files()` そのものではない (t016 でそこは直っている:
    `Path.is_file()` は `EACCES` を握り潰さないので、列挙の内側で失敗すると
    そのまま `None` = 観測不能になる)。

    権限は **呼ばれるたびに** 戻してから落とす。1 回だけ落とすと、2 人目の
    読み手は `iterdir()` の時点で失敗し、t016 で既に直っている経路 (列挙の
    失敗 → 観測不能) に助けられて緑になる —— 確かめたい層ではないところで
    先に止まる、典型的な偽の緑である
    (memory: red-proof-catches-tests-green-for-the-wrong-reason)。
    """
    real = monitor._notification_files

    def list_then_revoke():
        notif_dir.chmod(0o755)      # 列挙は常にできる
        files = real()
        notif_dir.chmod(0o000)      # 以降、中身の stat は EACCES
        return files

    monitor._notification_files = list_then_revoke


def test_a_notification_unstattable_after_listing_is_not_read_as_no_signal(tmp_path):
    """列挙のあとに stat できなくなった通知を「無かったこと」にしないこと。

    RED: 読み手 2 者がどちらも `except OSError: continue` で捨てている。

      * `_mtimes_since_floor()` —— 候補から消える → idle が floor まで伸びる
      * `_newest_notification()` —— `(None, None)` → `_awaiting_human()` が
        False → **抑制が外れる**

    2 つとも terminate の側なので、通知を読めなかっただけで終了する。
    """
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    notif = _write_notification(tmp_path, age_seconds=30 * 60)
    if os.geteuid() == 0:
        pytest.skip("検索権限を落とせない環境 (root)")
    _lose_permission_after_listing(monitor, notif.parent)

    try:
        detail = monitor.check_detail()
    finally:
        notif.parent.chmod(0o755)

    assert detail.verdict != "terminate", (
        f"通知を stat できなかっただけで終了させた: "
        f"{detail.verdict} / {detail.reason} / idle={detail.idle_seconds:.0f}s")


def test_an_unstattable_notification_does_not_look_like_no_notification(tmp_path):
    """同じことを `_newest_notification()` に直接訊く。

    `check_detail()` 経由だと `_last_activity_mtime()` 側の修正だけでも緑に
    なりうるので、**もう一方の読み手も** 直っていることを別に固定する
    (memory: red-proof-catches-tests-green-for-the-wrong-reason)。
    """
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    notif = _write_notification(tmp_path, age_seconds=30 * 60)
    if os.geteuid() == 0:
        pytest.skip("検索権限を落とせない環境 (root)")
    _lose_permission_after_listing(monitor, notif.parent)

    try:
        mtime, notif_type = monitor._newest_notification()
    finally:
        notif.parent.chmod(0o755)

    assert mtime is not None, \
        "stat できなかった通知が「通知は 1 通も無い」に潰された"
    assert notif_type == "(unobservable)", (
        f"観測できなかったことが読み手に伝わっていない: {notif_type!r}")


def test_an_unsearchable_dir_does_not_crash_the_check(tmp_path):
    """観測できなくても判定そのものは返ること (例外で watchdog の 1 サイクルを
    落とさない)。落とすと、その回は **どの Worker も** 判定されない。"""
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    activity = _write_activity(tmp_path, age_seconds=5)
    _write_heartbeat(tmp_path, age_seconds=5)

    with _unsearchable(activity.parent):
        detail = monitor.check_detail()

    assert detail.verdict in ("alive", "warn", "terminate", "kill")


# ===========================================================================
# 逆向きの担保 —— 「本当に無い」は、これまで通り沈黙として数える
# ===========================================================================
#
# これが無いと「読めなければ常に活動中」に倒しただけで上が緑になる。
# `knowledge/empty-vs-unobservable.md` §3 が後回しの理由に挙げていた懸念
# (起動直後の Worker には activity ファイルが無いのが普通) は、まさにここで
# 固定される —— `ENOENT` は「本当に無い」のままである。

def test_a_missing_activity_file_is_still_genuine_silence(tmp_path):
    """activity も heartbeat も無く、3 時間沈黙している Worker は terminate。

    §3 の懸念そのもの。`ENOENT` まで「観測不能」に倒すと、**ハングした Worker が
    永久に検知されなくなる**。
    """
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    assert not (tmp_path / "registry" / "activity" / AGENT).exists()
    assert not (tmp_path / "registry" / "heartbeats" / AGENT).exists()

    detail = monitor.check_detail()

    assert detail.verdict == "terminate", (
        f"本当に沈黙している Worker を見逃した: "
        f"{detail.verdict} / {detail.reason} / idle={detail.idle_seconds:.0f}s")


def test_a_missing_activity_file_beside_a_present_heartbeat_is_not_unobservable(
        tmp_path):
    """片方だけ無いのも普通の状態 (pull 直後は activity がまだ無い)。

    ここで「1 つでも stat に失敗したら観測不能」に倒すと、**起動直後が毎回
    観測不能**になり、やはりハングが検知されなくなる。無いものは無い。
    """
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    _write_heartbeat(tmp_path, age_seconds=2 * 3600)

    detail = monitor.check_detail()

    assert detail.verdict == "terminate", (
        f"heartbeat だけがある 2 時間の沈黙が数えられていない: "
        f"{detail.verdict} / {detail.reason} / idle={detail.idle_seconds:.0f}s")


def test_a_notification_deleted_between_listing_and_stat_is_not_unobservable(
        tmp_path):
    """列挙と stat の間に消えた通知は、**本当に無い**。

    通知ファイルは読まれたあと消される。列挙直後に消えるのは競合ではなく通常の
    並びなので、これを「観測できなかった」に倒すと terminate が永久に抑制される
    (= ハングが検知されない)。
    """
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    notif = _write_notification(tmp_path, age_seconds=30 * 60)

    real_iterdir = Path.iterdir

    def vanishing_iterdir(self):
        entries = list(real_iterdir(self))
        if self == notif.parent:
            notif.unlink()          # 列挙は済んだ。stat の前に消える。
        return iter(entries)

    Path.iterdir = vanishing_iterdir
    try:
        detail = monitor.check_detail()
    finally:
        Path.iterdir = real_iterdir

    assert detail.verdict == "terminate", (
        f"列挙後に消えた通知が「観測不能」として terminate を抑制した: "
        f"{detail.verdict} / {detail.reason}")


# ===========================================================================
# 既存の挙動を壊さない (t044 / t016 の保証)
# ===========================================================================

def test_fresh_activity_is_still_alive(tmp_path):
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    _write_activity(tmp_path, age_seconds=5)
    assert monitor.check_detail().verdict == "alive"


def test_soft_idle_still_warns(tmp_path):
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    _write_activity(tmp_path, age_seconds=450)   # 300 < 450 <= 600
    assert monitor.check_detail().verdict == "warn"


def test_a_readable_notification_still_suppresses_terminate(tmp_path):
    monitor = _monitor(tmp_path, pulled_seconds_ago=HOURS_3)
    _write_notification(tmp_path, age_seconds=30 * 60)

    detail = monitor.check_detail()
    assert detail.awaiting_human is True
    assert detail.verdict == "warn", \
        f"人間待ちの Worker が {detail.verdict} ({detail.reason})"


def test_a_stale_signal_before_the_floor_is_still_dropped(tmp_path):
    """t044 の保証 —— 監視開始前のシグナルは、この監視対象のものではない。"""
    monitor = _monitor(tmp_path, pulled_seconds_ago=2)
    _write_heartbeat(tmp_path, age_seconds=18 * 3600)

    detail = monitor.check_detail()
    assert detail.verdict == "alive", (
        f"起動直後の Worker が {detail.reason} で {detail.verdict} にされた "
        f"(idle={detail.idle_seconds:.0f}s)")
