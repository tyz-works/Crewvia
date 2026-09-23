#!/usr/bin/env python3
"""tests/test_daemon_watch_failclosed.py

Codex 2 巡目の指摘 4 件 (t037 / PR #209)。どれも同じ形をしている — **判断
できなかったことが、判断できたことと同じ扱いになっている**:

  1. P1 `resume()` がロックを取らずに「読む → 照合する → 消す」をやる。
     読んだあとに別の maintenance がマーカーを差し替えると、**実行中の
     maintenance の保護を外す**。
  2. P1 `read_pause()` が「無い」と「読めない」を同じ None にする。`_decide()`
     はそのため、**マーカーがあるのに読めない状態で respawn を許す**。
  3. P2 起動確認が、観測できなかったことを起動成功と数える。コマンドが飲まれて
     何も起きていなくても spawn が成功を返し、猶予と flap のカウントだけが減る。
  4. P2 `spawn_command()` が repo パスをクォートしない。アポストロフィを含む
     checkout で起動も復旧も壊れ、続くメタ文字はコマンドとして走りうる。

1 と 3 は「Yes/No」を返す関数に「わからない」を押し込んだ結果で、この repo が
もう 4 層で踏んでいる形 (memory: fail-closed-guard-can-recreate-the-defect)。

  python3 -m pytest tests/test_daemon_watch_failclosed.py -v
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_daemon_watch as dw  # noqa: E402
import lib_mux  # noqa: E402

from test_daemon_mutual_watch import (  # noqa: E402
    Clock,
    FakeMux,
    make_watch,
    repo,  # noqa: F401 — pytest fixture
    write_peer_heartbeat,
)

PEER = dw.DAEMON_WATCHDOG


# ---------------------------------------------------------------------------
# 1. resume() を直列化する (P1)
# ---------------------------------------------------------------------------

def test_a_pause_cannot_land_while_a_resume_is_deciding(repo, monkeypatch):
    """resume の「読む → 照合 → 消す」の途中に pause が入り込めないこと。

    入り込めると、こうなる:

        resume : token A のマーカーを読む            … A のものだと確認
        pause  : マーカーを token B で置き換える     … B の maintenance 開始
        resume : unlink                              … **B の保護を外す**

    B は今まさに相手を kill しようとしている。保護が外れたその瞬間に相互監視が
    respawn すれば、B の spawn と並んで 2 つ目が立つ — この module 全体が防ごう
    としている二重起動そのもの。

    数マイクロ秒の窓は実時間では再現できないので、resume が読んだ**直後**に
    別スレッドの pause を必ず走らせて、窓を決定的に開く
    (memory: microsecond-race-fix-needs-structural-test)。
    """
    registry = repo / "registry"
    token_a = dw.pause(registry, PEER, reason="first")
    assert token_a

    landed = {}
    original = dw.read_pause_state

    def read_then_let_a_second_maintenance_try(*args, **kwargs):
        result = original(*args, **kwargs)
        if "b" not in landed:
            landed["b"] = None    # 先に埋めて再入を防ぐ
            thread = threading.Thread(target=lambda: landed.__setitem__(
                "b", dw.pause(registry, PEER, reason="second", timeout=1.0)))
            thread.start()
            thread.join(timeout=10)
        return result

    monkeypatch.setattr(dw, "read_pause_state",
                        read_then_let_a_second_maintenance_try)
    dw.resume(registry, PEER, token=token_a)

    assert landed["b"] is None, (
        "a second pause landed while resume was mid-decision — resume is not "
        "holding this daemon's lock")


def test_resume_refuses_when_it_cannot_take_the_lock(repo):
    """ロックが取れない = 直列化できない。直列化できないなら、外さない。"""
    registry = repo / "registry"
    token = dw.pause(registry, PEER, reason="maintenance")
    assert token

    held = threading.Event()
    release = threading.Event()

    def hold_the_lock():
        with dw.daemon_lock(registry, PEER, timeout=5) as locked:
            assert locked
            held.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=hold_the_lock)
    thread.start()
    try:
        assert held.wait(timeout=10)
        assert dw.resume(registry, PEER, token=token, timeout=0.2) is False
        assert dw.pause_path(registry, PEER).exists(), \
            "the marker was removed although the decision was not serialised"
    finally:
        release.set()
        thread.join(timeout=10)


def test_resume_is_still_idempotent_and_still_honours_the_token(repo):
    """直列化しても、元の契約は変えない。"""
    registry = repo / "registry"
    assert dw.resume(registry, PEER, token="whatever") is True      # 無ければ True

    token = dw.pause(registry, PEER, reason="maintenance")
    assert dw.resume(registry, PEER, token="someone-elses") is False
    assert dw.pause_path(registry, PEER).exists()
    assert dw.resume(registry, PEER, token=token) is True
    assert not dw.pause_path(registry, PEER).exists()


# ---------------------------------------------------------------------------
# 2. 読めないマーカーを「無い」にしない (P1)
# ---------------------------------------------------------------------------

@pytest.fixture
def unreadable_marker(repo):
    """マーカーの位置に **ディレクトリ** を置く。

    read すると EISDIR になるので、root で走らせても必ず「読めない」を再現
    できる (chmod 000 は root では効かない)。
    """
    dw.pause_path(repo / "registry", PEER).mkdir(parents=True)
    return repo


def test_read_pause_state_separates_absent_from_unreadable(repo, unreadable_marker):
    assert dw.read_pause_state(repo / "registry", dw.DAEMON_DISPATCHER) == \
        (dw.PAUSE_ABSENT, None)

    status, marker = dw.read_pause_state(repo / "registry", PEER)
    assert status == dw.PAUSE_UNREADABLE, (status, marker)
    assert marker is None


def test_a_corrupt_marker_is_unreadable_not_absent(repo):
    path = dw.pause_path(repo / "registry", PEER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert dw.read_pause_state(repo / "registry", PEER)[0] == dw.PAUSE_UNREADABLE


def test_an_unreadable_marker_holds_the_respawn(unreadable_marker):
    """マーカーが読めない相手は respawn しない。

    「読めない」を「停止マーカーは無い」と読み替えると、maintenance の真っ最中
    に respawn が走る。fail-closed の契約はここが要で、ここだけが例外になって
    いると、契約があること自体が危険になる (安心して kill するから)。
    """
    clock = Clock()
    watch, mux, clock = make_watch(unreadable_marker, self_name="dispatcher",
                                   clock=clock)
    write_peer_heartbeat(unreadable_marker, PEER, updated_at=clock() - 600)

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_HOLD, verdict
    assert mux.spawned == [], "a daemon was respawned past an unreadable marker"


def test_a_readable_marker_still_reports_paused(repo):
    """読める停止マーカーの挙動は変えない。"""
    clock = Clock()
    watch, mux, clock = make_watch(repo, self_name="dispatcher", clock=clock)
    write_peer_heartbeat(repo, PEER, updated_at=clock() - 600)
    assert dw.pause(repo / "registry", PEER, reason="maintenance")

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_PAUSED, verdict
    assert mux.spawned == []


def test_no_marker_at_all_still_respawns(repo):
    """逆方向の確認 — 「読めない」を広げすぎて、普通の復旧まで止めていないこと。"""
    clock = Clock()
    watch, mux, clock = make_watch(repo, self_name="dispatcher", clock=clock)
    write_peer_heartbeat(repo, PEER, updated_at=clock() - 600)

    assert watch.watch_peer().action == dw.ACTION_RESPAWNED


# ---------------------------------------------------------------------------
# 3. 失敗した生存確認を起動成功と数えない (P2)
# ---------------------------------------------------------------------------

def _herdr_reply(payload):
    return {"result": payload}


_IDLE_SHELL = {"name": "bash", "argv": ["bash"], "pid": 4242}


@pytest.fixture
def husk_workspace(monkeypatch):
    """husk ペイン 1 枚だけの herdr。process-info の答えはテストが決める。

    `answers` に "idle" / "live" / "unreadable" を積むと、`pane_process_info`
    がその順に答える (最後の答えは以降ずっと繰り返す)。
    """
    state = {"answers": ["idle"], "run_calls": 0}

    def fake_run(verb, args, timeout=10):
        if verb == "workspace_list":
            return _herdr_reply({"workspaces": [
                {"workspace_id": "w1", "label": os.environ["CREWVIA_HERDR_WORKSPACE"]}]})
        if verb == "pane_list":
            return _herdr_reply({"panes": [{
                "label": lib_mux._pane_name(dw.DAEMON_DISPATCHER),
                "pane_id": "w1:p1", "tab_id": "w1:t1"}]})
        if verb == "pane_get":
            return _herdr_reply({"pane": {"pane_id": "w1:p1"}})
        if verb == "pane_run":
            state["run_calls"] += 1
            return _herdr_reply({"ok": True})
        if verb == "pane_process_info":
            answer = state["answers"][0] if len(state["answers"]) == 1 \
                else state["answers"].pop(0)
            if answer == "unreadable":
                return None
            procs = [] if answer == "idle" else [
                {"name": "bash", "argv": ["bash", "scripts/dispatcher.sh"], "pid": 77}]
            return _herdr_reply({"process_info": {
                "shell_pid": 4242,
                "foreground_processes": [_IDLE_SHELL] if answer == "idle" else procs}})
        return _herdr_reply({})

    monkeypatch.setattr(lib_mux, "_herdr_run", fake_run)
    monkeypatch.setattr(lib_mux, "_herdr_ping", lambda: True)
    monkeypatch.setenv("CREWVIA_MUX_LAUNCH_VERIFY_SECONDS", "0.4")
    return state


def test_spawn_does_not_report_success_when_the_pane_cannot_be_read_afterwards(
        husk_workspace):
    """送ったあとペインが読めなくなったら、それは「起動した」ではない。

    `_pane_has_live_process()` は「二度目を起こさない」ために、読めなければ
    True (= 使用中) を返す設計になっている。同じ関数を「起動したか」に使うと、
    **読めなかったことが起動成功になる**。同じ述語に正反対の安全側を求めて
    いるのが欠陥の正体で、片方を裏返すのではなく 3 値にするのが直し方。

    実害: 相互監視は respawn を 1 回成功として記録し、猶予と flap のカウンタを
    消費したうえで、実際には何も起きていない死体を見続ける。
    """
    husk_workspace["answers"] = ["idle", "unreadable"]
    backend = lib_mux.HerdrBackend()

    started = backend.spawn(dw.DAEMON_DISPATCHER, "echo hi", cwd="/tmp")

    assert husk_workspace["run_calls"] == 1, "the launch command was never sent"
    assert started is False, \
        "spawn reported success although the pane could not be observed running"


def test_spawn_reports_success_when_the_pane_is_observed_running(husk_workspace):
    """積極的に観測できたときだけ True。生存性を殺していないことの確認。"""
    husk_workspace["answers"] = ["idle", "live"]
    assert lib_mux.HerdrBackend().spawn(dw.DAEMON_DISPATCHER, "echo hi",
                                        cwd="/tmp") is True


def test_a_pane_that_cannot_be_read_is_still_never_relaunched_into(husk_workspace):
    """反対側の契約は変えない: 読めないペインには**送らない**。

    「読めない」を起動確認では失敗に、占有判定では占有中に倒す。2 つの答えが
    要るから 3 値にした、というのがこのテストの主張。
    """
    husk_workspace["answers"] = ["unreadable"]
    assert lib_mux.HerdrBackend().spawn(dw.DAEMON_DISPATCHER, "echo hi",
                                        cwd="/tmp") is False
    assert husk_workspace["run_calls"] == 0, \
        "a launch command was typed into a pane that could not be read"


def test_pane_state_is_three_valued(tmp_path):
    """/proc 側 (tmux backend が使う層) も 3 値であること。"""
    assert lib_mux._pane_shell_state(None, proc_root=str(tmp_path)) == \
        lib_mux.PANE_UNKNOWN
    assert lib_mux._pane_shell_state(999_999_999, proc_root=str(tmp_path)) == \
        lib_mux.PANE_UNKNOWN
    # 自分自身は python であってシェルではない = 何か走っている。
    assert lib_mux._pane_shell_state(os.getpid()) == lib_mux.PANE_LIVE


def test_the_old_boolean_still_means_exactly_what_it_meant(tmp_path):
    """`_pane_shell_is_idle()` は 3 値化の前後で同じ答えでなければならない。

    「アイドルと言えるのは IDLE のときだけ」— UNKNOWN も LIVE も idle ではない。
    ここが変わると spawn の占有判定が静かに緩む。
    """
    for pid in (None, 999_999_999, os.getpid()):
        assert lib_mux._pane_shell_is_idle(pid) is False


# ---------------------------------------------------------------------------
# 4. 生成するシェルコマンドで repo パスをエスケープする (P2)
# ---------------------------------------------------------------------------

_HOSTILE_DIR = "o'brien'; touch PWNED; '"


@pytest.fixture
def hostile_repo(tmp_path):
    """アポストロフィとメタ文字を含む checkout パス。

    人の名前 (O'Brien) が入っただけのディレクトリで実際に起きる形で、同時に
    そのまま注入にもなる。
    """
    root = tmp_path / _HOSTILE_DIR / "crewvia"
    (root / "scripts").mkdir(parents=True)
    (root / "registry" / "daemons").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "scripts" / "dispatcher.sh").write_text(
        "#!/usr/bin/env bash\nprintf 'DISPATCHER_STARTED_IN:%s\\n' \"$PWD\"\n",
        encoding="utf-8")
    return root


def test_the_launch_command_survives_an_apostrophe_in_the_checkout_path(
        hostile_repo, tmp_path):
    """起動コマンドは、パスに `'` があっても**そのまま bash に渡せる**こと。

    `spawn()` は両 backend ともコマンドを **文字列として** シェルに流し込む
    (start.sh も同じ) ので、ここで壊れると起動も respawn も両方死ぬ。しかも
    壊れ方が「クォートが閉じて、続きがコマンドになる」なので、ただの故障では
    済まない。
    """
    cmd = dw.spawn_command(dw.DAEMON_DISPATCHER, hostile_repo, env={})
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                         timeout=30, cwd=str(tmp_path))

    assert out.returncode == 0, (out.stdout, out.stderr)
    assert out.stdout.strip() == f"DISPATCHER_STARTED_IN:{hostile_repo}", out
    assert not (tmp_path / "PWNED").exists(), \
        "the interpolated path escaped its quotes and ran as a command"
    assert not (hostile_repo / "PWNED").exists()


def test_the_watchdog_launch_command_is_quoted_too(tmp_path):
    """dispatcher 側だけ直して watchdog 側を忘れる、が起きないように両方見る。"""
    root = tmp_path / _HOSTILE_DIR / "crewvia"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "watchdog.py").write_text(
        "import os\nprint('WATCHDOG_STARTED_IN:' + os.getcwd())\n", encoding="utf-8")

    cmd = dw.spawn_command(dw.DAEMON_WATCHDOG, root, env={})
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                         timeout=30, cwd=str(tmp_path))

    assert out.returncode == 0, (out.stdout, out.stderr)
    assert out.stdout.strip() == f"WATCHDOG_STARTED_IN:{root}", out
    assert not (tmp_path / "PWNED").exists()
