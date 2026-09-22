#!/usr/bin/env python3
"""
tests/test_daemon_mutual_watch.py

デーモンの相互監視 (t005) の回帰テスト。

## このテストが押さえている 3 つの柱 (タスク要件)

1. **片方を落としたら相手が起こす** — `test_dead_peer_is_respawned`
2. **生きている相手は起こさない** — `test_*_is_not_respawned` 群。
   とくに `test_stuck_but_alive_peer_is_not_respawned` は二重起動の本体:
   「詰まっているだけで生きている」デーモンを respawn すると、同じ task を
   2 つの dispatcher が割り当てる。
3. **flap で止まる** — `test_flap_guard_stops_respawning`

## 証拠の強度についての方針 (t035 で改訂 / 元は PR #205 の教訓)

死亡の証拠は **プロセスだけ** (/proc 走査 + 記録 PID の世代照合) で、走査が失敗した
cycle は hold する (`test_unreadable_proc_holds`)。

**窓一覧は証拠に入らない** (t006 QA FINDING-1)。両 backend とも pane にシェルを置いて
そこへコマンドを流し込むので、デーモンはシェルの子であり、落ちても pane と label は
残る (husk)。「名前が無いこと」を死亡の必要条件にすると、**普通のクラッシュでは
一度も respawn しない**。よって:

- husk タブは respawn を妨げない。`test_a_husk_tab_does_not_veto_the_respawn`
- 二重起動に対する最後の防壁は `mux.spawn()` の戻り値 (= その pane の中身が生きて
  いるか) に移した。`test_a_refused_spawn_holds_instead_of_claiming_a_respawn`

実プロセス・実 tmux 窓で同じことを確かめるのは
`tests/test_daemon_husk_respawn.py` — 偽の mux では husk を作れないので、
判定表のテストだけでは FINDING-1 は二度と捕まらない。

## 同名別インスタンス (identity 束縛)

PID は再利用される。heartbeat に記録した PID が「生きている」だけでは、それが
同じデーモンである保証はない。`test_recycled_pid_is_not_mistaken_for_alive` が
世代 (/proc の starttime) 照合を固定する。

実行方法:
  python3 -m pytest tests/test_daemon_mutual_watch.py -v
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_daemon_watch as dw  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes — no real mux, no real processes, no production registry
# ---------------------------------------------------------------------------

class FakeMux:
    """Records spawns/sends; `windows` is what list() reports.

    `windows = []` deliberately models BOTH "nothing is running" and "the
    backend failed to answer", because that is exactly what the real backends
    collapse into (lib_mux TmuxBackend.list returns [] on a non-zero exit or a
    5s timeout; HerdrBackend.list returns [] when the workspace lookup fails).
    """

    def __init__(self, windows=None, spawn_ok=True):
        self.windows = list(windows) if windows is not None else []
        self.spawn_ok = spawn_ok
        self.spawned = []
        self.sent = []

    def list(self, suffix=None):
        names = list(self.windows)
        if suffix:
            names = [n for n in names if n.endswith(suffix)]
        return names

    def spawn(self, name, cmd, cwd=None, env=None):
        self.spawned.append({"name": name, "cmd": cmd, "cwd": cwd})
        if self.spawn_ok:
            self.windows.append(name)
        return self.spawn_ok

    def send(self, name, text):
        self.sent.append({"name": name, "text": text})
        return True


class DeadMux(FakeMux):
    """A mux whose send() always fails — used for the report-retry test."""

    def send(self, name, text):
        self.sent.append({"name": name, "text": text})
        return False


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


@pytest.fixture
def repo(tmp_path):
    """A throwaway directory that passes repo_identity_ok() (has a .git)."""
    root = tmp_path / "crewvia"
    (root / "registry" / "daemons").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / ".git").mkdir()
    return root


def make_watch(repo, *, self_name="dispatcher", mux=None, clock=None,
               scan=None, config=None, notify=None, identity_ok=None):
    """A DaemonWatch with every observation seam injected.

    Nothing here touches the real /proc, the real mux or the production
    registry: `scan` stands in for the /proc walk and `mux` for the backend.
    """
    clock = clock or Clock()
    mux = mux if mux is not None else FakeMux(windows=["Sora-director"])
    if scan is None:
        scan = lambda repo_root, name: []          # noqa: E731 — nothing running
    watch = dw.DaemonWatch(
        registry_dir=repo / "registry",
        repo_root=repo,
        self_name=self_name,
        mux=mux,
        config=config or dw.WatchConfig(),
        log=lambda msg: None,
        notify=notify,
        now=clock,
        scan=scan,
        identity_ok=identity_ok,
    )
    return watch, mux, clock


def write_peer_heartbeat(repo, name, *, pid=999999, generation="999999:1",
                         updated_at, repo_root=None):
    dw.heartbeat_path(repo / "registry", name).write_text(
        json.dumps({
            "daemon": name,
            "pid": pid,
            "generation": generation,
            "started_at": updated_at,
            "updated_at": updated_at,
            "window": name,
            "repo_root": str(repo_root or repo),
            "version": dw.HEARTBEAT_VERSION,
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. 片方を落としたら相手が起こす
# ---------------------------------------------------------------------------

def test_dead_peer_is_respawned(repo):
    """Stale heartbeat + no process anywhere + a corroborated listing that does
    not mention the window ⇒ proven dead ⇒ respawn."""
    clock = Clock()
    mux = FakeMux(windows=["Sora-director", "Ren-worker"])
    watch, mux, clock = make_watch(repo, self_name="dispatcher", mux=mux, clock=clock)
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 600)

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_RESPAWNED, verdict.reason
    assert [s["name"] for s in mux.spawned] == ["watchdog"]
    assert mux.spawned[0]["cmd"] == dw.spawn_command("watchdog", repo)


def test_respawn_reports_to_director_without_asking_for_confirmation(repo):
    """The self-report is one line, states the estimated downtime, and asks the
    Director for nothing — it is an after-the-fact notice, not a work item."""
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher",
        mux=FakeMux(windows=["Sora-director", "Ren-worker"]), clock=clock)
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 630)

    watch.watch_peer()

    assert len(mux.sent) == 1, mux.sent
    msg = mux.sent[0]["text"]
    assert mux.sent[0]["name"] == "Sora-director"
    assert "\n" not in msg
    assert "watchdog" in msg
    assert "10" in msg, f"estimated downtime (~10 min) missing from: {msg}"
    for nagging in ("確認してください", "確認し", "対応してください"):
        assert nagging not in msg, f"report asks the Director to do work: {msg}"


def test_missing_heartbeat_with_no_process_is_respawned(repo):
    """A daemon that never wrote a heartbeat is still respawnable — but only
    because the /proc scan (not the window list) proves nothing is running."""
    watch, mux, clock = make_watch(
        repo, self_name="watchdog",
        mux=FakeMux(windows=["Sora-director", "dispatcher-ignored"]))

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_RESPAWNED, verdict.reason
    assert [s["name"] for s in mux.spawned] == ["dispatcher"]


def test_respawn_command_matches_start_sh(repo):
    """The respawn must not drift from the launch path.

    start.sh has to *call* the same builder rather than keep its own copy of
    the string; a literal in both places is how env prefixes come to differ
    between a daemon started by ./crewvia and one started by its peer.
    """
    start_sh = (REPO_ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
    for name in ("dispatcher", "watchdog"):
        # t036: the launcher went one step further and now calls `spawn`,
        # which builds the command *and* takes the daemon's lock — three
        # starting points, one command, one lock.
        assert re.search(
            rf"lib_daemon_watch\.py['\"]? spawn(-cmd)? {name}", start_sh
        ), f"start.sh does not launch {name} via lib_daemon_watch"


def test_spawn_command_embeds_env_in_the_command_string(repo, monkeypatch):
    """spawn()'s `env=` argument is ignored by BOTH backends, so CREWVIA_MUX
    has to travel inside the command text (same as start.sh's _MUX_ENV_PREFIX)."""
    monkeypatch.setenv("CREWVIA_MUX", "herdr")
    cmd = dw.spawn_command("dispatcher", repo)
    assert "export CREWVIA_MUX='herdr';" in cmd
    assert str(repo) in cmd
    assert "dispatcher.sh" in cmd

    monkeypatch.delenv("CREWVIA_MUX")
    assert "CREWVIA_MUX" not in dw.spawn_command("dispatcher", repo)


def test_spawn_command_carries_the_mux_destination_too(repo):
    """The backend alone is not enough — the daemon must reach the SAME session.

    A respawned daemon that inherits only CREWVIA_MUX falls back to the
    default session / workspace name.  Its `list()` then queries a session
    that isn't there, comes back empty, and mutual watch degrades to a
    permanent hold without saying a word (t006 QA FINDING-2, hit for real in
    the isolated harness).  Production happens to use the default names, which
    is exactly why this would have stayed invisible.
    """
    cmd = dw.spawn_command("watchdog", repo, env={
        "CREWVIA_MUX": "tmux", "CREWVIA_TMUX_SESSION": "qa-t006-s1"})
    assert "export CREWVIA_MUX='tmux';" in cmd
    assert "export CREWVIA_TMUX_SESSION='qa-t006-s1';" in cmd

    cmd = dw.spawn_command("watchdog", repo, env={
        "CREWVIA_MUX": "herdr", "CREWVIA_HERDR_WORKSPACE": "crewvia-qa"})
    assert "export CREWVIA_HERDR_WORKSPACE='crewvia-qa';" in cmd
    assert "CREWVIA_TMUX_SESSION" not in cmd, "an unset variable was exported empty"


def test_spawn_command_quotes_a_hostile_session_name(repo):
    """A session name is data, not shell.  It is exported, never executed."""
    hostile = "a'; echo PWNED; '"
    cmd = dw.spawn_command("watchdog", repo, env={
        "CREWVIA_MUX": "tmux", "CREWVIA_TMUX_SESSION": hostile})
    # Everything up to the last "; " is the export prefix; the body follows.
    exports = cmd[:cmd.rindex("; ") + 2]
    out = subprocess.run(
        ["bash", "-c", exports + 'printf %s "$CREWVIA_TMUX_SESSION"'],
        capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    # Exact equality is the proof: had the quoting broken, `echo PWNED` would
    # have run and printed its own line ahead of printf's output.
    assert out.stdout == hostile, out


# ---------------------------------------------------------------------------
# 2. 生きている相手は起こさない (= 二重起動を絶対に起こさない)
# ---------------------------------------------------------------------------

def test_fresh_heartbeat_is_not_respawned(repo):
    clock = Clock()
    watch, mux, clock = make_watch(repo, self_name="dispatcher", clock=clock)
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 5)

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_HEALTHY
    assert mux.spawned == []


def test_stuck_but_alive_peer_is_not_respawned(repo):
    """The double-start case.

    The heartbeat is ancient (the daemon is wedged and has stopped writing),
    the window is gone from the listing — and the process is still there.  A
    respawn here puts two dispatchers on the same queue, which hands the same
    task to two Workers.  Process evidence outranks both other signals.
    """
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director"]),          # watchdog window absent
        scan=lambda repo_root, name: [4242],              # ...but the process lives
    )
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_HOLD, verdict.reason
    assert mux.spawned == []
    assert "4242" in verdict.reason


def test_recorded_pid_still_alive_is_not_respawned(repo, monkeypatch):
    """Even with an empty /proc scan, a recorded pid+generation that is still
    live blocks the respawn.  Two independent process probes, either of which
    can veto."""
    clock = Clock()
    watch, mux, clock = make_watch(repo, self_name="dispatcher", clock=clock)
    write_peer_heartbeat(repo, "watchdog", pid=777, generation="777:5150",
                         updated_at=clock() - 99999)
    monkeypatch.setattr(dw, "instance_alive", lambda pid, gen: True)

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_HOLD, verdict.reason
    assert mux.spawned == []


def test_recycled_pid_is_not_mistaken_for_alive(repo, monkeypatch):
    """A live pid whose *generation* differs is a stranger wearing the number.

    `instance_alive` must compare the recorded starttime, not just ask whether
    the pid exists — otherwise a recycled pid keeps a genuinely dead daemon
    down forever ("名前は同じでも中身は別物", one level below the window name).
    """
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director"]))
    write_peer_heartbeat(repo, "watchdog", pid=777, generation="777:5150",
                         updated_at=clock() - 99999)
    # pid 777 exists, but it booted at a different time → not our daemon.
    monkeypatch.setattr(dw, "process_generation", lambda pid: "777:99999")

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_RESPAWNED, verdict.reason
    assert [s["name"] for s in mux.spawned] == ["watchdog"]


def test_instance_alive_compares_generation():
    """Unit-level fixing of the same rule, against this very process."""
    me = os.getpid()
    gen = dw.process_generation(me)
    assert gen is not None
    assert dw.instance_alive(me, gen) is True
    assert dw.instance_alive(me, "%d:1" % me) is False      # wrong generation
    assert dw.instance_alive(me, None) is True              # nothing to contradict
    assert dw.instance_alive(None, gen) is False


def test_a_husk_tab_does_not_veto_the_respawn(repo):
    """A tab named after the peer is NOT evidence that the peer is alive.

    Both backends put a shell in the pane and type the command into it, so the
    daemon is the shell's child and the pane outlives it with its label
    intact.  That is the ordinary shape of a crash, not a rare one — requiring
    the name to be absent meant the respawn fired only when someone had
    already killed the tab by hand (t006 QA FINDING-1).

    The real-process version of this is
    tests/test_daemon_husk_respawn.py::test_real_husk_pane_does_not_block_respawn;
    this one pins the decision table.
    """
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director", "watchdog"]))   # husk still listed
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_RESPAWNED, verdict.reason
    assert [s["name"] for s in mux.spawned] == ["watchdog"]


def test_a_refused_spawn_holds_instead_of_claiming_a_respawn(repo):
    """`spawn()` returning False is the last line of defence, and it is honoured.

    With the window name out of the decision, "is anything alive in that pane"
    is answered by the backend at the moment of the spawn — herdr via `pane
    process-info`, tmux via the pane shell's /proc entry.  False means it
    refused (or failed), so nothing was started and nothing may be reported as
    started.
    """
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director", "watchdog"], spawn_ok=False))
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_HOLD, verdict.reason
    assert mux.sent == [], "a refused spawn was reported to the Director"
    # No flap entry either: nothing was replaced, so nothing was spent.
    assert not dw.respawn_log_path(repo / "registry", "watchdog").exists()


def test_unreadable_proc_holds(repo):
    """`scan` returning None means "could not look", which is not "nothing
    there".  The death needs positive process evidence."""
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director"]),
        scan=lambda repo_root, name: None)
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_HOLD, verdict.reason
    assert mux.spawned == []


def test_foreign_repo_heartbeat_is_never_respawned(repo, tmp_path):
    """A heartbeat written by a daemon rooted in another checkout belongs to
    another crewvia (another WSL, a test worktree).  Not ours to restart."""
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director"]))
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999,
                         repo_root=tmp_path / "some-other-crewvia")

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_REFUSED, verdict.reason
    assert mux.spawned == []


def test_orphan_daemon_refuses_to_respawn(repo):
    """t003's guard, extended to spawning.

    A daemon whose own checkout is gone (a removed worktree) can still be
    holding a production mux workspace through herdr's inherited env.  It may
    not kill there, and it may not start things there either.
    """
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director"]),
        identity_ok=lambda: False)
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_REFUSED, verdict.reason
    assert mux.spawned == []


def test_respawn_grace_prevents_immediate_second_respawn(repo):
    """A freshly started daemon has not written a heartbeat yet.  Without a
    grace window the very next cycle reads that absence as another death."""
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director"]))
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)

    assert watch.watch_peer().action == dw.ACTION_RESPAWNED
    mux.windows.remove("watchdog")          # still booting, nothing listed yet

    clock.advance(5)
    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_GRACE, verdict.reason
    assert len(mux.spawned) == 1


# ---------------------------------------------------------------------------
# 3. flap で止まる
# ---------------------------------------------------------------------------

def test_flap_guard_stops_respawning(repo):
    """Repeated respawns inside the window stop being respawns and become a
    request for a human."""
    clock = Clock()
    cfg = dw.WatchConfig(flap_threshold=3, flap_window_seconds=900,
                         respawn_grace_seconds=60)
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock, config=cfg,
        mux=FakeMux(windows=["Sora-director"]))

    for _ in range(3):
        write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)
        assert watch.watch_peer().action == dw.ACTION_RESPAWNED
        mux.windows.remove("watchdog")
        clock.advance(120)

    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)
    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_FLAPPING, verdict.reason
    assert len(mux.spawned) == 3, "a 4th respawn was issued despite the flap guard"
    escalations = [s["text"] for s in mux.sent if "flap" in s["text"].lower()
                   or "繰り返" in s["text"]]
    assert escalations, f"no escalation sent to the Director: {mux.sent}"


def test_flap_guard_releases_once_the_window_passes(repo):
    """The guard is a rolling window, not a latch — a daemon that misbehaved
    an hour ago must still be restartable today."""
    clock = Clock()
    cfg = dw.WatchConfig(flap_threshold=2, flap_window_seconds=600,
                         respawn_grace_seconds=60)
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock, config=cfg,
        mux=FakeMux(windows=["Sora-director"]))

    for _ in range(2):
        write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)
        assert watch.watch_peer().action == dw.ACTION_RESPAWNED
        mux.windows.remove("watchdog")
        clock.advance(70)

    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)
    assert watch.watch_peer().action == dw.ACTION_FLAPPING

    clock.advance(cfg.flap_window_seconds + 10)
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)
    assert watch.watch_peer().action == dw.ACTION_RESPAWNED


def test_flap_entries_record_which_instance_they_belong_to(repo):
    """The counter is bound to instances, not to a name: a bare count cannot
    tell "restarted the same corpse 3 times" from "3 healthy generations"."""
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director"]))
    write_peer_heartbeat(repo, "watchdog", pid=321, generation="321:42",
                         updated_at=clock() - 99999)

    watch.watch_peer()

    entries = json.loads(
        dw.respawn_log_path(repo / "registry", "watchdog").read_text())["entries"]
    assert len(entries) == 1
    assert entries[0]["replaced_generation"] == "321:42"
    assert entries[0]["by"] == "dispatcher"
    assert isinstance(entries[0]["at"], (int, float))


# ---------------------------------------------------------------------------
# maintenance marker — 意図的な停止では respawn しない
# ---------------------------------------------------------------------------

def test_paused_peer_is_not_respawned(repo):
    """The restart recipe (pause → kill → spawn → resume) must not race the
    peer's mutual watch into a double start."""
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director"]))
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)
    dw.pause(repo / "registry", "watchdog", reason="merge restart", now=clock())

    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_PAUSED, verdict.reason
    assert mux.spawned == []


def test_resume_restores_watching(repo):
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        mux=FakeMux(windows=["Sora-director"]))
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)
    token = dw.pause(repo / "registry", "watchdog", reason="x", now=clock())
    assert watch.watch_peer().action == dw.ACTION_PAUSED

    assert dw.resume(repo / "registry", "watchdog", token=token) is True
    assert watch.watch_peer().action == dw.ACTION_RESPAWNED


def test_resume_with_a_foreign_token_does_not_unpause(repo):
    """Two overlapping restarts: the first one finishing must not un-pause the
    second one's window.  The marker is bound to the run that created it."""
    clock = Clock()
    registry = repo / "registry"
    first = dw.pause(registry, "watchdog", reason="restart A", now=clock())
    second = dw.pause(registry, "watchdog", reason="restart B", now=clock())
    assert first != second

    assert dw.resume(registry, "watchdog", token=first) is False
    assert dw.pause_path(registry, "watchdog").exists()
    assert dw.resume(registry, "watchdog", token=second) is True
    assert not dw.pause_path(registry, "watchdog").exists()


def test_status_shows_a_pause_marker_even_without_a_heartbeat(repo, capsys):
    """The state an operator is most likely chasing — "why is nobody restarting
    this?" — is a daemon that is paused AND down, i.e. has no heartbeat at all.
    Reporting only heartbeat-bearing daemons hides exactly that case.
    """
    dw.pause(repo / "registry", "watchdog", reason="left over")

    dw.main(["status", "--repo-root", str(repo)])

    out = capsys.readouterr().out
    assert "PAUSED" in out, out
    assert "left over" in out, out


def test_stale_pause_marker_is_reported_once(repo):
    """A forgotten marker silently disables mutual watch.  Fail-closed keeps
    it in force, so the exit is a report — sent once, not every cycle."""
    clock = Clock()
    cfg = dw.WatchConfig(pause_report_after_seconds=1800)
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock, config=cfg,
        mux=FakeMux(windows=["Sora-director"]))
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)
    dw.pause(repo / "registry", "watchdog", reason="forgotten", now=clock())

    clock.advance(1801)
    assert watch.watch_peer().action == dw.ACTION_PAUSED
    assert len(mux.sent) == 1, mux.sent
    assert "paused" in mux.sent[0]["text"] or "停止マーカー" in mux.sent[0]["text"]

    clock.advance(60)
    watch.watch_peer()
    assert len(mux.sent) == 1, f"stale-pause report repeated: {mux.sent}"


def test_restart_helper_locks_then_pauses_then_kills(repo):
    """Order matters, and t036 added a step in front of it.

    The marker has to exist *before* the process goes away, or the peer sees a
    dead daemon during the gap and races the manual spawn.  But the marker
    alone never closed that gap: the peer can already have read "no marker"
    and be on its way to a spawn.  So the lock comes first, the marker second,
    and only then may anything be killed.
    """
    source = (SCRIPTS / "lib_daemon_watch.py").read_text(encoding="utf-8")
    body = source.split("\ndef restart(", 1)[1].split("\ndef ", 1)[0]
    assert body.index("daemon_lock(") < body.index("_write_pause_marker("), \
        "restart() writes its marker outside the daemon lock"
    assert body.index("_write_pause_marker(") < body.index("kill("), \
        "restart() kills before it pauses — that gap is a double start"


class RecordingMux(FakeMux):
    """Also records kills, in call order, so restart()'s sequence is visible."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    def kill(self, name):
        self.calls.append(("kill", name))
        if name in self.windows:
            self.windows.remove(name)
        return True

    def spawn(self, name, cmd, cwd=None, env=None):
        self.calls.append(("spawn", name))
        return super().spawn(name, cmd, cwd=cwd, env=env)


def test_restart_holds_the_marker_across_kill_and_spawn(repo):
    """Behavioural companion to the structural check: the marker must exist for
    the whole dangerous stretch, and must be gone afterwards."""
    registry = repo / "registry"
    mux = RecordingMux(windows=["dispatcher"])
    seen = []
    mux_kill, mux_spawn = mux.kill, mux.spawn
    mux.kill = lambda n: (seen.append(("kill", dw.pause_path(registry, n).exists())),
                          mux_kill(n))[1]
    mux.spawn = lambda n, c, cwd=None, env=None: (
        seen.append(("spawn", dw.pause_path(registry, n).exists())),
        mux_spawn(n, c, cwd=cwd, env=env))[1]

    assert dw.restart("dispatcher", repo_root=repo, mux=mux, log=lambda m: None) is True

    assert seen == [("kill", True), ("spawn", True)], seen
    assert not dw.pause_path(registry, "dispatcher").exists(), \
        "restart leaked its pause marker — mutual watch stays off for this daemon"


def test_restart_clears_the_marker_even_when_the_spawn_fails(repo):
    """A leaked marker silently disables mutual watch; that must not be the
    price of a failed restart."""
    mux = RecordingMux(windows=["dispatcher"], spawn_ok=False)

    assert dw.restart("dispatcher", repo_root=repo, mux=mux, log=lambda m: None) is False
    assert not dw.pause_path(repo / "registry", "dispatcher").exists()


# ---------------------------------------------------------------------------
# heartbeat の書き手と形式
# ---------------------------------------------------------------------------

def test_beat_records_pid_and_generation(repo):
    watch, mux, clock = make_watch(repo, self_name="watchdog")
    watch.beat()

    hb = json.loads(dw.heartbeat_path(repo / "registry", "watchdog").read_text())
    assert hb["daemon"] == "watchdog"
    assert hb["pid"] == os.getpid()
    assert hb["generation"] == dw.process_generation(os.getpid())
    assert hb["repo_root"] == str(repo)
    assert hb["updated_at"] == clock()


def test_beat_can_record_a_different_pid_than_the_writer(repo):
    """dispatcher's living process is the bash wrapper; the python cycle that
    writes on its behalf must record the wrapper's pid, not its own."""
    watch, mux, clock = make_watch(repo, self_name="dispatcher")
    watch.beat(pid=12345, generation="12345:7")

    hb = json.loads(dw.heartbeat_path(repo / "registry", "dispatcher").read_text())
    assert hb["pid"] == 12345
    assert hb["generation"] == "12345:7"


def test_daemon_heartbeats_live_outside_the_worker_heartbeat_dir(repo):
    """registry/heartbeats/ is scanned per *agent name* by dispatcher's
    vanished-worker detection; a file called "watchdog" in there would be read
    as a Worker."""
    path = dw.heartbeat_path(repo / "registry", "watchdog")
    assert "heartbeats" not in path.parts, path
    assert path.parent.name == "daemons"


def test_bash_written_heartbeat_is_readable_by_python(repo):
    """dispatcher writes its heartbeat from bash so that a crashing python
    cycle cannot make a live daemon look dead.  Two writers, one format —
    this pins the contract between them.
    """
    helper = SCRIPTS / "lib_daemon_watch.sh"
    assert helper.exists(), "scripts/lib_daemon_watch.sh is missing"
    subprocess.run(
        ["bash", "-c",
         f'source "{helper}"; daemon_beat dispatcher "{repo}" "{repo / "registry"}"'],
        check=True, capture_output=True, text=True, timeout=30,
    )

    hb = dw.read_heartbeat(repo / "registry", "dispatcher")
    assert hb is not None, "python could not read the bash-written heartbeat"
    assert hb["daemon"] == "dispatcher"
    assert hb["repo_root"] == str(repo)
    assert isinstance(hb["pid"], int) and hb["pid"] > 0
    assert hb["generation"].startswith(f'{hb["pid"]}:')
    assert abs(float(hb["updated_at"]) - time.time()) < 60
    assert hb["version"] == dw.HEARTBEAT_VERSION


def test_dispatcher_beats_independently_of_the_dispatch_cycle(repo):
    """Structural: the beat must not be chained to run_dispatch's success.

    A python cycle that throws every time (a bad deploy, a corrupt card) would
    otherwise stop the heartbeat while bash keeps looping — the exact "alive
    but declared dead" shape that respawns into a double start.
    """
    source = (SCRIPTS / "dispatcher.sh").read_text(encoding="utf-8")
    # Executable lines only — the surrounding comments name both symbols.
    code = [ln.strip() for ln in source.split("while true; do", 1)[1].splitlines()
            if ln.strip() and not ln.strip().startswith("#")]

    beat_lines = [ln for ln in code if "daemon_beat" in ln]
    assert beat_lines, "dispatcher.sh main loop never calls daemon_beat"
    for line in beat_lines:
        assert "run_dispatch" not in line, \
            f"heartbeat is chained to the dispatch cycle: {line!r}"

    beat_at = next(i for i, ln in enumerate(code) if "daemon_beat" in ln)
    dispatch_at = next(i for i, ln in enumerate(code) if "run_dispatch" in ln)
    assert beat_at < dispatch_at, \
        "daemon_beat runs after run_dispatch; a hung cycle would skip it"


def test_watchdog_beats_inside_the_blocking_terminate_wait(repo, monkeypatch):
    """graceful_terminate() blocks the main loop for up to 70s.  Without a beat
    inside those loops the watchdog is declared dead mid-termination and gets
    a second copy started on top of it."""
    import watchdog

    watch, _mux, clock = make_watch(repo, self_name="watchdog")
    monkeypatch.setattr(watchdog, "_DAEMON_WATCH", watch, raising=False)
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: clock.advance(s))
    monkeypatch.setattr(watchdog, "repo_identity_ok", lambda root: True)

    beats = []
    monkeypatch.setattr(watch, "beat", lambda *a, **k: beats.append(clock()))

    class FakeMonitor:
        agent_name = "Ren"
        task_id = "t001"
        repo_root = repo
        _names = ["Ren-worker"] * watchdog.TERMINATE_GRACE_PERIOD + [None]

        def _mux_window_name(self):
            return self._names.pop(0) if self._names else None

    monkeypatch.setattr(watchdog, "_mux", FakeMux(windows=["Ren-worker"]))
    watchdog.graceful_terminate(FakeMonitor())

    assert len(beats) >= watchdog.TERMINATE_GRACE_PERIOD - 1, \
        f"only {len(beats)} beats during a {watchdog.TERMINATE_GRACE_PERIOD}s wait"


# ---------------------------------------------------------------------------
# 報告の到達性
# ---------------------------------------------------------------------------

def test_undelivered_report_is_retried_until_it_lands(repo):
    """A report that nobody received is the silent half of an outage — the one
    case where "we told the Director" is false but looks true."""
    clock = Clock()
    dead = DeadMux(windows=["Sora-director", "Ren-worker"])
    watch, mux, clock = make_watch(repo, self_name="dispatcher", clock=clock, mux=dead)
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)

    assert watch.watch_peer().action == dw.ACTION_RESPAWNED
    assert len(dead.sent) == 1
    store = dw.reports_path(repo / "registry", "dispatcher")
    assert store.exists(), "an undelivered report was dropped"
    assert json.loads(store.read_text())["pending"], "report not queued for retry"

    dead.send = FakeMux.send.__get__(dead, DeadMux)   # the Director comes back
    clock.advance(30)
    watch.watch_peer()

    assert len(dead.sent) == 2
    # The file stays — it now holds only the "already said" ledger, which is
    # what stops the redelivered report being announced a third time.
    assert json.loads(store.read_text())["pending"] == [], \
        "delivered report was not taken off the retry queue"


def test_report_is_not_repeated_once_delivered(repo):
    """One respawn, one report — however many cycles run afterwards.

    `scan` has to follow the spawn here.  A fake that keeps answering "nothing
    is running" after its own spawn succeeded describes no real machine, and
    before t035 this test passed only because the window check happened to
    stop the second cycle: the FakeMux appended the name on spawn and the
    watcher then held on the name.  With the name no longer standing in for
    life (t006 QA FINDING-1), the honest fake is one where a successful spawn
    makes the process visible to the next /proc scan.
    """
    clock = Clock()
    mux = FakeMux(windows=["Sora-director", "Ren-worker"])
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock, mux=mux,
        scan=lambda repo_root, name: [4242] if mux.spawned else [])
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)
    watch.watch_peer()

    for _ in range(5):
        clock.advance(30)
        watch.watch_peer()

    assert len(mux.spawned) == 1, mux.spawned
    assert len(mux.sent) == 1, mux.sent


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_thresholds_come_from_config_file(repo):
    cfg_path = repo / "crewvia.yaml"
    cfg_path.write_text(
        "daemons:\n"
        "  mutual_watch: true\n"
        "  dispatcher_stale_seconds: 45\n"
        "  watchdog_stale_seconds: 222\n"
        "  flap_threshold: 7\n"
        "  flap_window_seconds: 1234\n"
        "  respawn_grace_seconds: 99\n"
        "  pause_report_after_seconds: 4321\n",
        encoding="utf-8",
    )
    cfg = dw.load_config(cfg_path)

    assert cfg.enabled is True
    assert cfg.stale_seconds_for("dispatcher") == 45
    assert cfg.stale_seconds_for("watchdog") == 222
    assert cfg.flap_threshold == 7
    assert cfg.flap_window_seconds == 1234
    assert cfg.respawn_grace_seconds == 99
    assert cfg.pause_report_after_seconds == 4321


def test_shipped_config_declares_the_daemon_block():
    import yaml
    shipped = yaml.safe_load(
        (REPO_ROOT / "config" / "crewvia.yaml").read_text(encoding="utf-8"))
    block = shipped.get("daemons")
    assert isinstance(block, dict), \
        "config/crewvia.yaml has no `daemons:` block — the thresholds are not tunable"
    for key in ("mutual_watch", "dispatcher_stale_seconds", "watchdog_stale_seconds",
                "flap_threshold", "flap_window_seconds", "respawn_grace_seconds",
                "pause_report_after_seconds", "hold_report_after_seconds"):
        assert key in block, f"daemons.{key} missing from config/crewvia.yaml"

    cfg = dw.load_config(REPO_ROOT / "config" / "crewvia.yaml")
    assert cfg.stale_seconds_for("dispatcher") > 5 * 4, \
        "dispatcher stale threshold leaves no slack over its 5s cycle"
    assert cfg.stale_seconds_for("watchdog") > 30 * 3, \
        "watchdog stale threshold leaves no slack over its 30s cycle"
    # The 70s graceful_terminate block must fit inside the threshold even if a
    # beat is missed on either side of it.
    assert cfg.stale_seconds_for("watchdog") > 70 + 30


def test_mutual_watch_can_be_switched_off(repo):
    clock = Clock()
    watch, mux, clock = make_watch(
        repo, self_name="dispatcher", clock=clock,
        config=dw.WatchConfig(enabled=False),
        mux=FakeMux(windows=["Sora-director"]))
    write_peer_heartbeat(repo, "watchdog", updated_at=clock() - 99999)

    assert watch.watch_peer().action == dw.ACTION_DISABLED
    assert mux.spawned == []


def test_env_overrides_config(repo, monkeypatch):
    monkeypatch.setenv("CREWVIA_DAEMON_MUTUAL_WATCH", "0")
    assert dw.load_config(REPO_ROOT / "config" / "crewvia.yaml").enabled is False


# ---------------------------------------------------------------------------
# /proc 走査
# ---------------------------------------------------------------------------

def test_scan_matches_only_this_repos_daemon(tmp_path):
    """Two crewvia checkouts on one machine must not see each other's daemons.
    The match is on the absolute script path, not on the script name."""
    proc = tmp_path / "proc"
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    for pid, root in ((101, mine), (102, theirs)):
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(
            b"python3\x00" + str(root / "scripts" / "watchdog.py").encode() + b"\x00")
        (d / "stat").write_text(f"{pid} (python3) S 1 " + "0 " * 40)

    assert dw.scan_daemon_pids(mine, "watchdog", proc_root=proc) == [101]
    assert dw.scan_daemon_pids(theirs, "watchdog", proc_root=proc) == [102]
    assert dw.scan_daemon_pids(mine, "dispatcher", proc_root=proc) == []


def test_scan_returns_none_when_proc_is_unreadable(tmp_path):
    assert dw.scan_daemon_pids(tmp_path, "watchdog",
                               proc_root=tmp_path / "nope") is None


def test_scan_ignores_the_callers_own_process(tmp_path):
    """The watcher itself appears in /proc.  A dispatcher scanning for
    "dispatcher" would otherwise always find itself and never respawn."""
    proc = tmp_path / "proc"
    root = tmp_path / "mine"
    d = proc / str(os.getpid())
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(
        b"bash\x00" + str(root / "scripts" / "dispatcher.sh").encode() + b"\x00")
    (d / "stat").write_text(f"{os.getpid()} (bash) S 1 " + "0 " * 40)

    assert dw.scan_daemon_pids(root, "dispatcher", proc_root=proc,
                               exclude_pids={os.getpid()}) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
