#!/usr/bin/env python3
"""tests/test_pytest_workspace_sweep.py — pytest 終了時の宛先の後始末 (backlog #15)。

`tests/pytest_workspace_sweep.py` の判断と実行を、**偽の herdr / tmux** に対して確かめる。
本番の herdr には一切触れない (実機の確認は Result の `workspace-diff` を参照)。

  python3 -m pytest tests/test_pytest_workspace_sweep.py -v

欠陥を戻すと赤くなることの実証は `bash tests/red_proof_t001_pytest_workspace.sh`。
"""

import errno
import itertools
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import pytest_workspace_sweep as sweep
from conftest import PRODUCTION_DESTINATION

TESTS_DIR = Path(__file__).resolve().parent
OWN = "crewvia-pytest-4242-deadbeef"
DEAD = "crewvia-pytest-111-aaaaaaaa"      # pid 111 は下の pid_alive で「死んでいる」
ALIVE = "crewvia-pytest-222-bbbbbbbb"     # 生きている
EPERM_LABEL = "crewvia-pytest-333-cccccccc"


def pid_alive_table(table):
    def probe(pid):
        return table.get(pid, sweep.PID_ALIVE)
    return probe


DEFAULT_PIDS = pid_alive_table({111: sweep.PID_DEAD, 4242: sweep.PID_ALIVE})


class FakeBackend:
    """workspace の集合を持つ偽 backend。close の履歴が残る。"""

    def __init__(self, names, empty=None, entries_ok=True, close_ok=True):
        self.workspaces = {f"w{i}": n for i, n in enumerate(names)}
        self.empty = {} if empty is None else empty   # name -> bool (無ければ空)
        self.entries_ok = entries_ok
        self.close_ok = close_ok
        self.closed = []
        self.empty_asked = []

    def entries(self, deadline):
        if not self.entries_ok:
            return None
        return [(n, h) for h, n in self.workspaces.items()]

    def is_empty(self, handle, deadline):
        name = self.workspaces[handle]
        self.empty_asked.append(name)
        return self.empty.get(name, True)

    def close(self, handle, deadline):
        self.closed.append(self.workspaces[handle])
        return self.close_ok


def run(backend, own=OWN, production=PRODUCTION_DESTINATION, enabled=True,
        pid_alive=DEFAULT_PIDS):
    warnings = []
    closed = sweep.cleanup(backend, own, production, enabled,
                           pid_alive=pid_alive, warn=warnings.append)
    return closed, warnings


# ---------------------------------------------------------------------------
# 自分の宛先
# ---------------------------------------------------------------------------

def test_only_own_label_is_closed_and_production_and_home_are_untouched():
    b = FakeBackend(["~", PRODUCTION_DESTINATION, OWN])
    closed, _ = run(b)
    assert b.closed == [OWN]
    assert closed == [OWN]


def test_nothing_to_close_when_own_workspace_was_never_created():
    """作られなかった宛先は普通のこと。何もしないし警告も出さない。"""
    b = FakeBackend(["~", PRODUCTION_DESTINATION])
    closed, warnings = run(b)
    assert closed == [] and b.closed == [] and warnings == []


def test_own_label_is_matched_exactly_not_by_prefix_or_substring():
    lookalikes = [OWN + "-2", OWN[:-1], "x" + OWN, OWN.upper(), OWN + " "]
    b = FakeBackend(lookalikes + [OWN])
    run(b, enabled=False)
    assert b.closed == [OWN]


# ---------------------------------------------------------------------------
# 強いガード
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_own", [
    PRODUCTION_DESTINATION,        # 本番そのもの
    "~",                           # 接頭辞が違う
    "crewvia-pytes",               # 接頭辞に届かない
    "prod-crewvia-pytest-1-aaaaaaaa",   # 部分一致 (前方ではない)
    "",                            # 読めない
    None,
])
def test_guard_refuses_to_close_a_destination_that_is_not_ours(bad_own):
    names = [PRODUCTION_DESTINATION, "~", "crewvia-pytes", OWN,
             "prod-crewvia-pytest-1-aaaaaaaa"]
    b = FakeBackend(names)
    closed, warnings = run(b, own=bad_own)
    assert b.closed == [] and closed == []
    assert warnings, "拒否したことを警告 1 行で残す"


def test_guard_refusal_is_pure_and_names_the_reason():
    assert sweep.guard_refusal(OWN, PRODUCTION_DESTINATION) is None
    assert "本番" in sweep.guard_refusal(PRODUCTION_DESTINATION, PRODUCTION_DESTINATION)
    # 本番の宛先が将来 `crewvia-pytest-` で始まる名前になっても、等しければ拒否する。
    assert "本番" in sweep.guard_refusal("crewvia-pytest-x", "crewvia-pytest-x")


# ---------------------------------------------------------------------------
# 残骸掃除
# ---------------------------------------------------------------------------

def test_leftover_with_dead_pid_is_closed_and_live_pid_is_kept():
    b = FakeBackend([OWN, DEAD, ALIVE])
    closed, _ = run(b)
    assert sorted(b.closed) == sorted([OWN, DEAD])
    assert ALIVE not in b.closed


def test_a_concurrent_pytest_in_another_worktree_is_never_closed():
    """別 worktree で同時に走っている pytest の workspace (pid が生きている)。"""
    other = f"crewvia-pytest-{os.getpid()}-12345678"
    b = FakeBackend([other])
    run(b, pid_alive=sweep.pid_state)     # 本物の os.kill(pid, 0)
    assert b.closed == []
    assert b.empty_asked == [], "生きている pid の宛先は、空かどうかを尋ねる前に対象外"


def test_eperm_is_alive_only_esrch_is_dead():
    def kill_raising(err):
        def kill(pid, sig):
            raise err
        return kill

    assert sweep.pid_state(1, kill_raising(ProcessLookupError())) == sweep.PID_DEAD
    assert sweep.pid_state(1, kill_raising(OSError(errno.ESRCH, "x"))) == sweep.PID_DEAD
    assert sweep.pid_state(1, kill_raising(PermissionError(errno.EPERM, "x"))) \
        == sweep.PID_ALIVE
    assert sweep.pid_state(1, kill_raising(OSError(errno.EINVAL, "x"))) == sweep.PID_ALIVE
    assert sweep.pid_state(1, kill_raising(OverflowError())) == sweep.PID_ALIVE
    assert sweep.pid_state(1, kill_raising(RuntimeError())) == sweep.PID_ALIVE
    assert sweep.pid_state(1, lambda pid, sig: None) == sweep.PID_ALIVE


def test_eperm_pid_workspace_is_kept_end_to_end():
    def kill(pid, sig):
        if pid == 333:
            raise PermissionError(errno.EPERM, "not permitted")
        raise ProcessLookupError()

    b = FakeBackend([EPERM_LABEL, DEAD])
    run(b, pid_alive=lambda pid: sweep.pid_state(pid, kill))
    assert b.closed == [DEAD]


def test_pid_that_is_really_dead_is_dead():
    """偽物でなく本物の os.kill で ESRCH を確かめる (終了して回収した子の pid)。"""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert sweep.pid_state(child.pid) == sweep.PID_DEAD
    assert sweep.pid_state(os.getpid()) == sweep.PID_ALIVE


@pytest.mark.parametrize("label", [
    "crewvia", "~", "my-workspace",
    "crewvia-pytest-",                       # pid が無い
    "crewvia-pytest-abc-deadbeef",           # pid が数字でない
    "crewvia-pytest-111-DEADBEEF",           # hex が大文字
    "crewvia-pytest-111-aaaaaaa",            # hex が 8 桁でない
    "crewvia-pytest-111-aaaaaaaa-extra",     # 後ろに余分
    "xcrewvia-pytest-111-aaaaaaaa",          # 前に余分
    "crewvia-pytest-0-aaaaaaaa",             # pid 0 は os.kill(0, 0) が常に成功する
    "crewvia-pytest--1-aaaaaaaa",
    "crewvia-pytest-99999999999-aaaaaaaa",   # 桁あふれ
])
def test_labels_that_do_not_match_the_shape_are_never_touched(label):
    assert sweep.leftover_pid(label) is None
    b = FakeBackend([label])
    run(b, pid_alive=lambda pid: sweep.PID_DEAD)   # 全部死んでいると言われても
    assert b.closed == []


def test_a_workspace_with_a_live_pane_is_kept_even_when_its_pid_is_dead():
    b = FakeBackend([DEAD], empty={DEAD: False})
    closed, warnings = run(b)
    assert b.closed == [] and closed == []
    assert b.empty_asked == [DEAD]
    assert warnings


def test_pane_state_unobservable_is_not_empty():
    """`is_empty` が False (= 空だと示せなかった) なら閉じない。観測できないを空に倒さない。"""
    b = FakeBackend([DEAD], empty={DEAD: False})
    run(b)
    assert b.closed == []


def test_kill_switch_stops_the_leftover_sweep_but_not_own_cleanup():
    b = FakeBackend([OWN, DEAD])
    closed, _ = run(b, enabled=False)
    assert b.closed == [OWN]
    assert b.empty_asked == [], "停止中は残骸を何も見ない"


def test_listing_failure_closes_nothing_and_does_not_raise():
    b = FakeBackend([OWN, DEAD], entries_ok=False)
    closed, warnings = run(b)
    assert closed == [] and b.closed == [] and len(warnings) == 1


def test_close_failure_is_a_warning_not_an_exception():
    b = FakeBackend([OWN], close_ok=False)
    closed, warnings = run(b)
    assert closed == [] and warnings


def test_time_budget_stops_the_sweep():
    b = FakeBackend([DEAD, "crewvia-pytest-112-aaaaaaaa"])
    ticks = itertools.chain([0], itertools.repeat(100))
    warnings = []
    sweep.cleanup(b, OWN, PRODUCTION_DESTINATION, True,
                  pid_alive=lambda pid: sweep.PID_DEAD, warn=warnings.append,
                  clock=lambda: next(ticks), budget=10)
    assert b.closed == [] and b.empty_asked == []
    assert any("時間予算" in w for w in warnings)


# ---------------------------------------------------------------------------
# 実行層 — herdr の CLI 出力を偽の run で返す
# ---------------------------------------------------------------------------

class FakeHerdr:
    """`herdr ...` の argv に JSON で答える。"""

    def __init__(self, workspaces, panes=None, process=None, fail=()):
        self.workspaces = workspaces            # {id: label}
        self.panes = panes or {}                # {ws_id: [pane_id]}
        self.process = process or {}            # {pane_id: shell_pid}
        self.fail = set(fail)                   # 失敗させる verb
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(argv)
        verb = " ".join(argv[1:3])
        if verb in self.fail:
            return 1, json.dumps({"error": {"code": "boom"}}), ""
        if verb == "workspace list":
            ws = [{"workspace_id": i, "label": l, "pane_count": 1}
                  for i, l in self.workspaces.items()]
            return 0, json.dumps({"result": {"workspaces": ws}}), ""
        if verb == "pane list":
            wid = argv[argv.index("--workspace") + 1]
            if wid not in self.workspaces:
                return 1, json.dumps({"error": {"code": "workspace_not_found"}}), ""
            panes = [{"pane_id": p} for p in self.panes.get(wid, [])]
            return 0, json.dumps({"result": {"panes": panes}}), ""
        if verb == "pane process-info":
            pid = self.process.get(argv[-1])
            if pid is None:
                return 1, json.dumps({"error": {"code": "pane_not_found"}}), ""
            return 0, json.dumps({"result": {"process_info": {"shell_pid": pid}}}), ""
        if verb == "workspace close":
            self.workspaces.pop(argv[3], None)
            return 0, json.dumps({"result": {"type": "ok"}}), ""
        raise AssertionError(f"unexpected herdr call: {argv}")

    def verbs(self, verb):
        return [c for c in self.calls if " ".join(c[1:3]) == verb]


def test_herdr_backend_closes_only_own_and_dead_empty_leftover():
    fake = FakeHerdr(
        {"wA": "~", "wB": PRODUCTION_DESTINATION, "wC": OWN, "wD": DEAD,
         "wE": ALIVE},
        panes={"wD": ["wD:p1"]}, process={"wD:p1": 900})
    backend = sweep.HerdrBackend(run=fake, pane_state=lambda pid: "idle")
    closed, _ = run(backend)
    assert sorted(closed) == sorted([OWN, DEAD])
    assert sorted(w for w in fake.workspaces.values()) == sorted(
        ["~", PRODUCTION_DESTINATION, ALIVE])


@pytest.mark.parametrize("state", ["live", "unknown", "", None, "IDLE"])
def test_herdr_pane_that_is_not_provably_idle_keeps_the_workspace(state):
    fake = FakeHerdr({"wD": DEAD}, panes={"wD": ["wD:p1"]}, process={"wD:p1": 900})
    backend = sweep.HerdrBackend(run=fake, pane_state=lambda pid: state)
    run(backend)
    assert fake.verbs("workspace close") == []


def test_herdr_one_live_pane_among_idle_ones_keeps_the_workspace():
    fake = FakeHerdr({"wD": DEAD}, panes={"wD": ["wD:p1", "wD:p2"]},
                     process={"wD:p1": 900, "wD:p2": 901})
    backend = sweep.HerdrBackend(
        run=fake, pane_state=lambda pid: "idle" if pid == 900 else "live")
    run(backend)
    assert fake.verbs("workspace close") == []


@pytest.mark.parametrize("fail", ["pane list", "pane process-info"])
def test_herdr_pane_count_or_process_unobservable_keeps_the_workspace(fail):
    fake = FakeHerdr({"wD": DEAD}, panes={"wD": ["wD:p1"]},
                     process={"wD:p1": 900}, fail=[fail])
    backend = sweep.HerdrBackend(run=fake, pane_state=lambda pid: "idle")
    run(backend)
    assert fake.verbs("workspace close") == []


def test_herdr_zero_panes_answer_is_not_treated_as_empty():
    fake = FakeHerdr({"wD": DEAD}, panes={"wD": []})
    backend = sweep.HerdrBackend(run=fake, pane_state=lambda pid: "idle")
    run(backend)
    assert fake.verbs("workspace close") == []


@pytest.mark.parametrize("shell_pid", [None, 0, -5, "900", True])
def test_herdr_unusable_shell_pid_keeps_the_workspace(shell_pid):
    fake = FakeHerdr({"wD": DEAD}, panes={"wD": ["wD:p1"]},
                     process={"wD:p1": shell_pid})
    backend = sweep.HerdrBackend(run=fake, pane_state=lambda pid: "idle")
    run(backend)
    assert fake.verbs("workspace close") == []


def test_own_workspace_is_closed_even_if_it_has_live_panes():
    """自分が作った宛先。中身も自分のテストが作ったものなので AND は要らない。"""
    fake = FakeHerdr({"wC": OWN}, panes={"wC": ["wC:p1"]}, process={"wC:p1": 900})
    backend = sweep.HerdrBackend(run=fake, pane_state=lambda pid: "live")
    closed, _ = run(backend)
    assert closed == [OWN] and fake.workspaces == {}


def test_herdr_close_uses_the_workspace_id_not_the_label():
    fake = FakeHerdr({"wC": OWN})
    run(sweep.HerdrBackend(run=fake))
    assert fake.verbs("workspace close") == [["herdr", "workspace", "close", "wC"]]


@pytest.mark.parametrize("failing", [
    lambda argv, t: (_ for _ in ()).throw(FileNotFoundError("herdr")),
    lambda argv, t: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 5)),
    lambda argv, t: (1, "", "boom"),
    lambda argv, t: (0, "not json", ""),
    lambda argv, t: (0, json.dumps({"error": {"code": "x"}}), ""),
    lambda argv, t: (0, json.dumps({"result": {"workspaces": "no"}}), ""),
])
def test_herdr_absent_timeout_or_error_never_raises(failing):
    closed, _ = run(sweep.HerdrBackend(run=failing))
    assert closed == []


def test_pane_idle_word_matches_lib_mux():
    sys.path.insert(0, str(TESTS_DIR.parent / "scripts"))
    import lib_mux
    assert sweep._pane_is_idle(lib_mux.PANE_IDLE)
    assert not sweep._pane_is_idle(lib_mux.PANE_LIVE)
    assert not sweep._pane_is_idle(lib_mux.PANE_UNKNOWN)


def test_real_idle_shell_is_empty_and_a_python_process_is_not(idle_pane_shell):
    """`_default_pane_state` (lib_mux の本物の判定) が使えることの確認。"""
    assert sweep._pane_is_idle(sweep._default_pane_state(idle_pane_shell))
    assert not sweep._pane_is_idle(sweep._default_pane_state(os.getpid()))


# ---------------------------------------------------------------------------
# tmux — 判定は共有し、実行層だけ違う
# ---------------------------------------------------------------------------

class FakeTmux:
    def __init__(self, sessions, panes=None, server=True):
        self.sessions = set(sessions)
        self.panes = panes or {}
        self.server = server
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(argv)
        if not self.server:
            return 1, "", "no server running on /tmp/tmux-1000/default"
        cmd = argv[1]
        if cmd == "list-sessions":
            return 0, "".join(f"{s}\n" for s in sorted(self.sessions)), ""
        if cmd == "list-panes":
            name = argv[argv.index("-t") + 1].lstrip("=")
            if name not in self.sessions:
                return 1, "", "can't find session"
            return 0, "".join(f"{p}\n" for p in self.panes.get(name, [])), ""
        if cmd == "kill-session":
            target = argv[argv.index("-t") + 1]
            assert target.startswith("="), "完全一致 (`=`) でなければ別のセッションを撃つ"
            self.sessions.discard(target[1:])
            return 0, "", ""
        raise AssertionError(f"unexpected tmux call: {argv}")


def test_tmux_closes_own_and_dead_empty_leftover_only():
    fake = FakeTmux({"main", PRODUCTION_DESTINATION, OWN, DEAD, ALIVE},
                    panes={DEAD: [900]})
    backend = sweep.TmuxBackend(run=fake, pane_state=lambda pid: "idle")
    closed, _ = run(backend)
    assert sorted(closed) == sorted([OWN, DEAD])
    assert fake.sessions == {"main", PRODUCTION_DESTINATION, ALIVE}


def test_tmux_live_pane_or_unreadable_panes_keeps_the_session():
    fake = FakeTmux({DEAD}, panes={DEAD: [900]})
    run(sweep.TmuxBackend(run=fake, pane_state=lambda pid: "live"))
    assert fake.sessions == {DEAD}
    fake = FakeTmux({DEAD}, panes={DEAD: []})       # 0 個の答えは空と読まない
    run(sweep.TmuxBackend(run=fake, pane_state=lambda pid: "idle"))
    assert fake.sessions == {DEAD}


def test_tmux_no_server_is_a_quiet_no_op():
    """tmux の server が居ないのは異常ではない。毎回の pytest に警告を出さない。"""
    closed, warnings = run(sweep.TmuxBackend(run=FakeTmux(set(), server=False)))
    assert closed == [] and warnings == []


# ---------------------------------------------------------------------------
# 入口 — run_cleanup と conftest のフック
# ---------------------------------------------------------------------------

def test_run_cleanup_honours_the_kill_switch_env_only_for_the_leftovers():
    b = FakeBackend([OWN, DEAD])
    sweep.run_cleanup(OWN, PRODUCTION_DESTINATION, environ={sweep.SWEEP_SWITCH: "0"},
                      backends=[b])
    assert b.closed == [OWN]

    for value in (None, "1", "", "false"):       # `0` 以外では止まらない
        b = FakeBackend([OWN, DEAD])
        env = {} if value is None else {sweep.SWEEP_SWITCH: value}
        sweep.run_cleanup(OWN, PRODUCTION_DESTINATION, environ=env, backends=[b])
        assert sorted(b.closed) == sorted([OWN, DEAD]), value


def test_cleanup_itself_never_raises_even_if_a_backend_explodes():
    class Exploding:
        def entries(self, deadline):
            raise RuntimeError("boom")

    warnings = []
    assert sweep.cleanup(Exploding(), OWN, PRODUCTION_DESTINATION, True,
                         warn=warnings.append) == []
    assert warnings


def test_run_cleanup_never_raises_even_if_a_backend_explodes():
    class Exploding:
        def entries(self, deadline):
            raise RuntimeError("boom")

    sweep.run_cleanup(OWN, PRODUCTION_DESTINATION, backends=[Exploding()])
    sweep.run_cleanup(OWN, PRODUCTION_DESTINATION, backends=[object()])


def test_no_herdr_no_tmux_no_backends(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert sweep.default_backends() == []


def test_herdr_without_a_socket_is_not_contacted(monkeypatch, tmp_path):
    """server が居ない (socket が無い) なら CLI を呼ばない — 呼ぶと server を起こしうる。"""
    stub = tmp_path / "herdr"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("CREWVIA_HERDR_SOCK", str(tmp_path / "missing.sock"))
    assert sweep.default_backends() == []
    (tmp_path / "present.sock").write_text("")
    monkeypatch.setenv("CREWVIA_HERDR_SOCK", str(tmp_path / "present.sock"))
    assert [type(b) for b in sweep.default_backends()] == [sweep.HerdrBackend]


def test_cli_timeout_is_finite_and_a_hung_herdr_does_not_hang_the_session(
        monkeypatch, tmp_path):
    stub = tmp_path / "herdr"
    stub.write_text("#!/bin/sh\nexec sleep 30\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(sweep, "CLI_TIMEOUT_SECONDS", 0.3)
    closed, warnings = run(sweep.HerdrBackend())
    assert closed == [] and warnings
    assert 0 < sweep.CLI_TIMEOUT_SECONDS <= 10


# ---------------------------------------------------------------------------
# 全体の時間上限 — 締切は 1 つで、backend の subprocess 呼び出しまで届く
#
# 時計を差し替えるので実時間は使わない。偽の herdr は「応答に latency 秒かかる」と
# 時計を進めるだけで、timeout がそれより短ければ本物の subprocess.run と同じく
# timeout ぶんだけ進めて TimeoutExpired を出す。
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SlowHerdr(FakeHerdr):
    def __init__(self, clock, latency, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock = clock
        self.latency = latency
        self.timeouts = []

    def __call__(self, argv, timeout=None):
        self.timeouts.append(timeout)
        assert timeout is not None, "timeout の無い subprocess は締切を越えて走りうる"
        if timeout < self.latency:
            self.clock.now += max(timeout, 0)
            raise subprocess.TimeoutExpired(argv, timeout)
        self.clock.now += self.latency
        return super().__call__(argv, timeout)


def _ten_pane_leftover(clock, latency=4):
    """pid が本当に死んでいる残骸 workspace 1 つ (pane 10 個)。"""
    label = f"crewvia-pytest-{_dead_pid()}-aaaaaaaa"
    panes = [f"wD:p{i}" for i in range(10)]
    fake = SlowHerdr(clock, latency, {"wD": label}, panes={"wD": panes},
                     process={p: 900 + i for i, p in enumerate(panes)})
    return fake, label


def test_ten_panes_at_four_seconds_each_stay_within_the_overall_budget():
    """pane 10 個 × 応答 4 秒でも、後始末全体が上限 (30 秒) を超えない。

    以前は宛先の境目でしか締切を見ず、process-info を 10 回 (40 秒) 走らせたうえ
    close まで走って 52 秒かかった。
    """
    clock = FakeClock()
    fake, label = _ten_pane_leftover(clock)
    backend = sweep.HerdrBackend(run=fake, pane_state=lambda pid: "idle")
    sweep.run_cleanup(OWN, PRODUCTION_DESTINATION, environ={}, backends=[backend],
                      clock=clock, budget=30)
    assert clock.now <= 30, f"{clock.now} 秒かかった"
    assert fake.verbs("workspace close") == [], "空だと示し切れなかったので閉じない"
    assert fake.workspaces == {"wD": label}


def test_the_budget_is_shared_across_backends_not_restarted_per_backend():
    """herdr が予算を使い切ったら、tmux は始めない (herdr と tmux で合わせて 30 秒)。"""
    clock = FakeClock()
    herdr, _ = _ten_pane_leftover(clock)
    tmux = FakeTmux({OWN})
    sweep.run_cleanup(
        OWN, PRODUCTION_DESTINATION, environ={}, clock=clock, budget=30,
        backends=[sweep.HerdrBackend(run=herdr, pane_state=lambda pid: "idle"),
                  sweep.TmuxBackend(run=tmux)])
    assert clock.now <= 30
    assert tmux.calls == [], "予算が尽きているのに tmux を呼んだ (backend ごとに予算が戻っている)"


def test_each_subprocess_timeout_is_capped_by_the_remaining_time():
    clock = FakeClock()
    fake, _ = _ten_pane_leftover(clock)
    backend = sweep.HerdrBackend(run=fake, pane_state=lambda pid: "idle")
    warnings = []
    sweep.cleanup(backend, OWN, PRODUCTION_DESTINATION, True, warn=warnings.append,
                  clock=clock, budget=7)
    # 1 回目 (一覧) は上限の 5 秒。4 秒かかって残り 3 秒 → 2 回目は 3 秒で頭打ち。
    assert fake.timeouts == [5, 3]
    assert clock.now == 7


def test_close_is_not_run_after_the_budget_is_spent():
    """空だと示せても、そこで予算が尽きたなら close は走らせない。"""
    clock = FakeClock()
    label = f"crewvia-pytest-{_dead_pid()}-aaaaaaaa"
    fake = SlowHerdr(clock, 4, {"wD": label}, panes={"wD": ["wD:p1"]},
                     process={"wD:p1": 900})
    backend = sweep.HerdrBackend(run=fake, pane_state=lambda pid: "idle")
    warnings = []
    closed = sweep.cleanup(backend, OWN, PRODUCTION_DESTINATION, True,
                           warn=warnings.append, clock=clock, budget=12)
    # 一覧 4 + pane list 4 + process-info 4 = 12。ここで尽きる。
    assert closed == [] and fake.verbs("workspace close") == []
    assert fake.workspaces == {"wD": label}
    assert clock.now <= 12 and any("時間予算" in w for w in warnings)


def test_no_subprocess_is_started_once_the_budget_is_spent():
    clock = FakeClock()
    deadline = sweep.Deadline(10, clock)
    clock.now = 10
    started = []
    rc, _, _ = sweep._exec(lambda argv, timeout: started.append(argv),
                           ["herdr", "x"], deadline)
    assert rc is None and started == []


def test_the_remaining_budget_reaches_the_real_subprocess(monkeypatch, tmp_path):
    """`_subprocess_run` に timeout が渡っている (応答しない herdr を予算で切る)。

    1 回の上限 (5 秒) より短い予算を与え、上限まで待たずに戻ることを見る。
    """
    stub = tmp_path / "herdr"
    stub.write_text("#!/bin/sh\nexec sleep 30\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert sweep.CLI_TIMEOUT_SECONDS >= 5
    started = time.monotonic()
    warnings = []
    closed = sweep.cleanup(sweep.HerdrBackend(), OWN, PRODUCTION_DESTINATION, True,
                           warn=warnings.append, budget=0.5)
    assert closed == [] and warnings
    assert time.monotonic() - started < 3


# ---------------------------------------------------------------------------
# 入口から出口まで — 本物の pytest セッション (偽の herdr を PATH に置く)
# ---------------------------------------------------------------------------

HERDR_STUB = textwrap.dedent('''\
    #!__PYTHON__
    """偽の herdr。state.json に workspace を持ち、呼び出しを calls.log に残す。"""
    import json, os, sys
    root = os.environ["STUB_ROOT"]
    argv = sys.argv[1:]
    with open(os.path.join(root, "calls.log"), "a") as f:
        f.write(" ".join(argv) + "\\n")
    state = json.load(open(os.path.join(root, "state.json")))
    own = os.environ.get("CREWVIA_HERDR_WORKSPACE")
    if own and not state.get("own_created"):
        state["own_created"] = True
        state["workspaces"]["w-own"] = own      # pytest 中に自動作成された、という体
    def save():
        json.dump(state, open(os.path.join(root, "state.json"), "w"))
    def out(result):
        print(json.dumps({"result": result}))
    if argv[:2] == ["workspace", "list"]:
        out({"workspaces": [{"workspace_id": i, "label": l}
                            for i, l in state["workspaces"].items()]})
    elif argv[:2] == ["workspace", "close"]:
        state["workspaces"].pop(argv[2], None); save(); out({"type": "ok"})
    elif argv[:2] == ["pane", "list"]:
        wid = argv[argv.index("--workspace") + 1]
        out({"panes": [{"pane_id": p} for p in state["panes"].get(wid, [])]})
    elif argv[:2] == ["pane", "process-info"]:
        out({"process_info": {"shell_pid": state["process"][argv[-1]]}})
    else:
        sys.exit(2)
    save()
''')


def _session(tmp_path, workspaces, panes, process, extra_env=None,
             test_body="def test_ok():\n    pass\n", herdr_stub=True,
             cli_timeout=None):
    # herdr_stub: True = 状態を持つ偽 herdr / False = 無し / str = その内容のスクリプト
    # cli_timeout: コピーした sweep の CLI_TIMEOUT_SECONDS だけを短くする (本物は 5 秒)。
    """conftest と sweep をコピーした小さな tests/ を、偽 herdr を PATH に置いて走らせる。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    for name in ("conftest.py", "pytest_workspace_sweep.py"):
        (proj / name).write_text((TESTS_DIR / name).read_text())
    if cli_timeout is not None:
        copied = proj / "pytest_workspace_sweep.py"
        text = copied.read_text()
        short = text.replace(f"CLI_TIMEOUT_SECONDS = {sweep.CLI_TIMEOUT_SECONDS}\n",
                             f"CLI_TIMEOUT_SECONDS = {cli_timeout}\n", 1)
        assert short != text, "CLI_TIMEOUT_SECONDS の定義が見つからず短縮できない"
        copied.write_text(short)
    (tmp_path / "scripts").symlink_to(TESTS_DIR.parent / "scripts")   # lib_mux の置き場
    (proj / "test_inner.py").write_text(test_body)

    stubs = tmp_path / "bin"
    stubs.mkdir()
    if herdr_stub:
        herdr = stubs / "herdr"
        herdr.write_text(HERDR_STUB.replace("__PYTHON__", sys.executable)
                         if herdr_stub is True else herdr_stub)
        herdr.chmod(0o755)
    tmux = stubs / "tmux"
    tmux.write_text("#!/bin/sh\necho 'no server running' >&2\nexit 1\n")
    tmux.chmod(0o755)
    (tmp_path / "herdr.sock").write_text("")

    root = tmp_path / "stub"
    root.mkdir()
    (root / "state.json").write_text(json.dumps(
        {"workspaces": workspaces, "panes": panes, "process": process}))

    env = {"PATH": str(stubs), "HOME": str(tmp_path), "STUB_ROOT": str(root),
           "CREWVIA_HERDR_SOCK": str(tmp_path / "herdr.sock"),
           "PYTHONDONTWRITEBYTECODE": "1",
           # 隔離した HOME では user site が見えず pytest 自身が import できない。
           "PYTHONPATH": os.pathsep.join(
               sorted({str(Path(m.__file__).resolve().parent.parent)
                       for m in (pytest, __import__("_pytest"), __import__("pluggy"))}))}
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        str(proj)], capture_output=True, text=True, env=env,
                       cwd=str(proj), timeout=120)
    state = json.loads((root / "state.json").read_text())
    calls = (root / "calls.log").read_text().splitlines() \
        if (root / "calls.log").exists() else []
    return r, state, calls


def _dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_a_real_pytest_session_closes_its_own_workspace_on_exit(tmp_path):
    r, state, calls = _session(
        tmp_path, {"wA": "~", "wB": PRODUCTION_DESTINATION}, {}, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(state["workspaces"].values()) == sorted(["~", PRODUCTION_DESTINATION]), \
        "自分の crewvia-pytest-* だけが消え、~ と crewvia は残る"
    assert any(c.startswith("workspace close w-own") for c in calls)


def test_a_real_session_sweeps_only_dead_empty_leftovers(tmp_path, idle_pane_shell):
    dead = f"crewvia-pytest-{_dead_pid()}-aaaaaaaa"
    dead_live_pane = f"crewvia-pytest-{_dead_pid()}-bbbbbbbb"
    alive = f"crewvia-pytest-{os.getpid()}-cccccccc"      # この pytest 自身 (生きている)
    r, state, calls = _session(
        tmp_path,
        {"wA": "~", "wB": PRODUCTION_DESTINATION, "wD": dead, "wL": dead_live_pane,
         "wV": alive},
        {"wD": ["wD:p1"], "wL": ["wL:p1"], "wV": ["wV:p1"]},
        # wD の pane は本物の idle シェル。wL の pane は python (idle なシェルではない)。
        {"wD:p1": idle_pane_shell, "wL:p1": os.getpid(), "wV:p1": idle_pane_shell})
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(state["workspaces"].values()) == sorted(
        ["~", PRODUCTION_DESTINATION, dead_live_pane, alive])


def test_the_kill_switch_stops_the_sweep_in_a_real_session_but_not_the_own_cleanup(
        tmp_path, idle_pane_shell):
    dead = f"crewvia-pytest-{_dead_pid()}-aaaaaaaa"
    r, state, calls = _session(
        tmp_path, {"wA": "~", "wD": dead}, {"wD": ["wD:p1"]},
        {"wD:p1": idle_pane_shell}, extra_env={sweep.SWEEP_SWITCH: "0"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(state["workspaces"].values()) == sorted(["~", dead])
    assert not any("pane list" in c or "process-info" in c for c in calls), \
        "停止中は残骸を何も見ない"
    assert any(c.startswith("workspace close w-own") for c in calls)


@pytest.mark.parametrize("body,expected_rc", [
    ("def test_ok():\n    pass\n", 0),
    ("def test_bad():\n    assert False\n", 1),
])
@pytest.mark.parametrize("herdr_script", [
    "#!/bin/sh\nexit 1\n",                       # CLI エラー
    "#!/bin/sh\necho 'not json'\nexit 0\n",     # 読めない答え
])
def test_cleanup_failure_never_changes_the_exit_code(
        tmp_path, body, expected_rc, herdr_script, monkeypatch):
    """herdr が壊れていても、pytest の終了コードはテスト結果のまま。"""
    r, _, _ = _session(tmp_path, {}, {}, {}, test_body=body, herdr_stub=herdr_script)
    assert r.returncode == expected_rc, r.stdout + r.stderr


HUNG_HERDR = """#!{python}
import os, sys, time
with open(os.path.join(os.environ["STUB_ROOT"], "hung.log"), "a") as f:
    f.write("%d %r\\n" % (os.getpid(), time.time()))
time.sleep(60)
"""


@pytest.mark.parametrize("body,expected_rc", [
    ("def test_ok():\n    pass\n", 0),
    ("def test_bad():\n    assert False\n", 1),
])
def test_a_hung_herdr_is_cut_by_the_timeout_and_never_changes_the_exit_code(
        tmp_path, body, expected_rc):
    """応答しない herdr は timeout で切られ、pytest の終了コードはテスト結果のまま。

    「exit code が変わらない」だけでは CLI エラーでも緑になる (PATH に sleep が無くて
    即死するスタブでも通っていた)。だから**timeout を実際に通ったこと**を別に示す:
    スタブは眠る前に自分の pid と時刻を残し、pytest がそれから CLI_TIMEOUT 以上待って
    (= 即死していない) 60 秒より前に終わり (= 眠り切っていない)、スタブが殺されている。

    下限の計測: subprocess.run の timeout は**子の起動前**から数え始めるが、スタブの
    時刻は python が立ち上がって書いた**後**なので、スタブ基準だけで `timeout <= waited`
    と言うと、正しく timeout を通っても起動が遅い回に timeout をわずかに下回る (実測 1.98 秒)。
    だから下限は 2 本に分ける: (1) subprocess を起動する**前**から測る (起動遅延に
    影響されない。timeout を通ったなら必ず超える)、(2) スタブ基準は起動の余裕
    STARTUP_TOLERANCE を引いて見る (起動後すぐ死ぬスタブを、セッション全体の所要時間で
    誤魔化して緑にしないため。0 にはしない)。
    """
    timeout = 2
    startup_tolerance = 1.0
    launched = time.time()
    r, _, _ = _session(
        tmp_path, {}, {}, {}, test_body=body, cli_timeout=timeout,
        herdr_stub=HUNG_HERDR.format(python=sys.executable))
    finished = time.time()
    assert r.returncode == expected_rc, r.stdout + r.stderr

    log_file = tmp_path / "stub" / "hung.log"
    assert log_file.exists(), "偽 herdr が眠り始めた記録が無い (起動できずに即死している)"
    log = log_file.read_text().splitlines()
    assert len(log) == 1, f"偽 herdr が 1 回だけ呼ばれて眠り始めたはず: {log}"
    pid, started = log[0].split(" ", 1)
    waited = finished - float(started)
    assert timeout <= finished - launched, \
        f"timeout ({timeout}s) を通ったなら起動前からの待ちは {timeout}s 以上: " \
        f"{finished - launched:.2f}s"
    assert timeout - startup_tolerance <= waited < 60, \
        f"スタブが起動してからの待ちは {timeout - startup_tolerance}s 以上 60s 未満のはず: " \
        f"{waited:.2f}s (眠り始めてすぐ死んでいる / 眠り切っている)"
    assert sweep.pid_state(int(pid)) == sweep.PID_DEAD, \
        "timeout で偽 herdr が殺されているはず"
    assert "宛先の一覧を読めなかった" in r.stderr, "timeout は警告 1 行になる"


def test_no_herdr_at_all_does_not_change_the_exit_code(tmp_path):
    r, _, calls = _session(tmp_path, {}, {}, {}, herdr_stub=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls == []
