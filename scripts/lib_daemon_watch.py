#!/usr/bin/env python3
"""
scripts/lib_daemon_watch.py — デーモンの相互監視 (t005)

`dispatcher.sh` と `watchdog.py` は互いの存在を知らないまま並んで動いており、
片方が死んでも誰も気付かない。外部 supervisor (pm2 等) は使わない方針なので
(ユーザー決定 2026-09-21)、**互いを見る**のが唯一の手段になる。

    dispatcher (5s cycle)  ──見る──>  watchdog
    watchdog   (30s cycle) ──見る──>  dispatcher

このモジュールは両者に共通の 5 つの部品を持つ:

  1. 自分の heartbeat を書く        `DaemonWatch.beat()`
  2. 相手の生死を判定する            `DaemonWatch.watch_peer()`
  3. 死んでいたら起こし直す          `spawn_command()` + `mux.spawn()`
  4. 起こしたことを Director に報告   `_report()` (届くまで再試行)
  5. 繰り返す respawn を止める        flap ガード

---------------------------------------------------------------------------
設計の中心: 二重起動を絶対に起こさない
---------------------------------------------------------------------------

詰まっているだけで生きている dispatcher を respawn すると、2 つの dispatcher が
同じ queue を割り当て、同じ task が 2 人の Worker に渡る。これは相互監視が
救う障害 (デーモンの停止) よりはるかに重い。したがって判定は **fail closed** —
確証が無ければ respawn しない。

「確証」の定義は PR #205 (Codex 3 巡 / P1 9 件) の教訓をそのまま持ち込む。

**観測できないことは証拠にならない。** `mux.list()` は tmux / herdr のどちらも
失敗・タイムアウトを黙って `[]` に変換する (`lib_mux.py` TmuxBackend.list は
`returncode != 0` と 5s タイムアウトで、HerdrBackend.list は workspace 解決失敗と
10s タイムアウトで、いずれも空リストを返す)。`available()` は tmux では PATH に
バイナリがあるかしか見ず、`server_running()` もタイムアウト・例外で False を返す。
これらの失敗を「相手が死んだ」の証拠に使うと、**バックエンドの一時的な不調が
そのまま二重起動になる**。よって死亡の証拠は **プロセスだけ** に置く。`/proc` は
バックエンドを介さずカーネルが答えるので、mux の不調から独立している。

**タブの名前は生存の証拠ではない (t035 / t006 QA FAIL-1)。** 両 backend とも pane に
シェルを置いてそこへコマンドを流し込むので、デーモンは pane シェルの子である。
デーモンが落ちてもシェルは生き残り、pane は label を保ったまま残る (= husk)。
つまり**普通のクラッシュでは名前は必ず残る**。かつて「権威ある窓一覧に名前が無い」
ことを死亡の必要条件にしていたが、それは実質「タブごと消えた時だけ起こす」であり、
相互監視の目的 (片系が落ちたら起こし直す) が本番のプロセス構成で一度も発火しない、
という無音の欠陥だった。

窓一覧は**判定から外した** — この判定に `mux.list()` は登場しない。宛先の解決は
`mux.spawn()` 側に任せ、二重起動に対する最後の防壁も名前の有無ではなく
**その pane の中身の生死** に移した (herdr は `pane process-info`、tmux は pane
シェルの pid から `/proc`)。名前より強い証拠であり、両 backend で同じ強さになる。
`spawn()` が False を返す = 「生きたものが居るので断った」なので、そのまま保留する。

**名前は同じでも中身は別物。** PID は再利用される。heartbeat に記録した PID が
「存在する」だけでは同じデーモンである保証がないので、`/proc` の starttime を
世代 (`generation`) として併せて記録し、両方一致したときだけ「生きている」と
読む (`instance_alive()`)。窓名は定数 (`dispatcher` / `watchdog`) なので、判定に
使った名前をそのまま spawn に渡し、間で再解決しない (TOCTOU)。

保留 (`hold`) には必ず出口を付ける。黙って居座る保留は「要求 → 破棄」の
無限ループと同じ無音の故障になるので、一定時間続いたら Director に 1 度だけ
報告する (memory: fail-closed-discard-vs-hold)。

---------------------------------------------------------------------------
死亡と判定する条件 (すべて満たしたときだけ)
---------------------------------------------------------------------------

  0. 相互監視が有効で、自分の checkout が本物である (`repo_identity_ok`)
  1. 停止マーカー (`<name>.paused`) が無い            … 意図的な停止でない
  2. 直前の respawn から猶予が過ぎている              … 起動中を死亡と読まない
  3. heartbeat が stale (または最初から無い)
  4. **プロセスが存在しない** — 記録 PID が世代ごと不在 **かつ**
     `/proc` 走査が成功して 0 件。走査できなければ保留
  5. flap しきい値に達していない

4 が唯一の生死の証拠である。走査が失敗したら (`None`) 保留 — 「見られなかった」は
「居なかった」ではない。タブの有無は条件に入らない (上記「タブの名前は生存の証拠では
ない」)。

respawn したつもりで何も起きていない、を無くすために `mux.spawn()` の戻り値は必ず
見る。False (= pane に生きたプロセスが居る / backend が断った) なら保留して報告する。

CLI:
  python3 scripts/lib_daemon_watch.py spawn-cmd <dispatcher|watchdog>
  python3 scripts/lib_daemon_watch.py beat <name> [--pid N] [--generation G]
  python3 scripts/lib_daemon_watch.py status
  python3 scripts/lib_daemon_watch.py pause <name> [--reason R]
  python3 scripts/lib_daemon_watch.py resume <name> [--token T] [--force]
  python3 scripts/lib_daemon_watch.py restart <name> [--force]

restart は kill の前に「そのペインで走っているのが自分の checkout のデーモンか」を
/proc で確かめる (t037 / §7-11)。別 checkout のもの (foreign)・読めない (unknown)
なら何もせず非ゼロで終わる。--force がその確認だけを飛ばす操作者の逃げ道。
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, NamedTuple, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib_mux import (  # noqa: E402
    MUX_TEST_ISOLATION_EXIT,
    Mux,
    MuxTestIsolationError,
    OWNER_FOREIGN,
    OWNER_MINE,
    OWNER_NONE,
    OWNER_UNKNOWN,
    pane_script_owner,
    repo_identity_ok,
)
from lib_retirement import (  # noqa: E402
    process_alive,
    read_json,
    recorded_pid,
    unlink_quiet,
    write_json_atomic,
)

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - PyYAML is present in every known env
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Names and paths
# ---------------------------------------------------------------------------

DAEMON_DISPATCHER = "dispatcher"
DAEMON_WATCHDOG = "watchdog"
DAEMONS = (DAEMON_DISPATCHER, DAEMON_WATCHDOG)

#: Who watches whom.  Deliberately a 2-cycle: neither daemon is privileged, so
#: there is no single process whose death disables the whole mechanism.
PEER_OF = {DAEMON_DISPATCHER: DAEMON_WATCHDOG, DAEMON_WATCHDOG: DAEMON_DISPATCHER}

#: The script each daemon runs, relative to `<repo_root>/scripts/`.  Also the
#: needle for the /proc scan, which is why it must stay an exact path.
SCRIPT_OF = {DAEMON_DISPATCHER: "dispatcher.sh", DAEMON_WATCHDOG: "watchdog.py"}

HEARTBEAT_VERSION = 1

#: Deliberately NOT `registry/heartbeats/`.  That directory is keyed by *agent
#: name* and is walked by dispatcher's vanished-worker detection; a file named
#: "watchdog" in there would be read as a Worker called "watchdog".
_DAEMONS_SUBDIR = "daemons"


def daemons_dir(registry_dir) -> Path:
    return Path(registry_dir) / _DAEMONS_SUBDIR


def heartbeat_path(registry_dir, name: str) -> Path:
    return daemons_dir(registry_dir) / f"{name}.heartbeat"


def pause_path(registry_dir, name: str) -> Path:
    return daemons_dir(registry_dir) / f"{name}.paused"


def respawn_log_path(registry_dir, name: str) -> Path:
    return daemons_dir(registry_dir) / f"{name}.respawns.json"


def watch_state_path(registry_dir, name: str) -> Path:
    return daemons_dir(registry_dir) / f"{name}.watch.json"


def reports_path(registry_dir, name: str) -> Path:
    """Undelivered Director reports owned by `name` (the *reporter*)."""
    return daemons_dir(registry_dir) / f"{name}.reports.json"


def lock_path(registry_dir, name: str) -> Path:
    """The file whose flock serialises every decision *about* `name`."""
    return daemons_dir(registry_dir) / f"{name}.lock"


# ---------------------------------------------------------------------------
# Serialisation — one decision about a daemon at a time
# ---------------------------------------------------------------------------
#
# The pause marker alone does not make maintenance and mutual watch exclusive.
# Writing it before the kill only shortens the window; it does not close it:
#
#     watcher : reads the marker  → none
#     operator: writes the marker → kills the peer
#     watcher : spawns                       ← next to the operator's own spawn
#
# The watcher's read and its spawn are two separate instants, and anything can
# happen in between.  Neither backend helps: both check for an existing window
# and then create one as two steps, so "spawn refuses a live pane" is a last
# line of defence, not mutual exclusion.  This is the same lesson PR #205 took
# six rounds to learn — *a decision is only a decision if the moment it is
# written down is serialised*.
#
# So the whole transaction (read the marker → judge → spawn → record) runs
# under one lock per daemon, and maintenance takes the same lock.  flock is
# used rather than a marker file because the kernel releases it when the
# holder dies: a lock that outlives a crashed restart would silently disable
# mutual watch, which is the failure mode the marker's stale-report exists to
# catch in the first place.

#: Watch runs every cycle and must not stall it; maintenance is a person
#: waiting at a terminal and should queue behind whatever is in flight.
WATCH_LOCK_TIMEOUT_SECONDS = 2.0
MAINTENANCE_LOCK_TIMEOUT_SECONDS = 60.0

_LOCK_POLL_SECONDS = 0.05

#: flock is per *open file description*: a second `open()` of the same path in
#: the same process conflicts with the first, so re-entrancy (restart → pause)
#: has to be tracked here rather than left to the kernel.  Keyed by path, and
#: re-entrant only for the thread that actually holds it — another thread must
#: wait exactly as another process does, or the tests that drive both sides at
#: once would pass while production still races.
_LOCK_GUARD = threading.Lock()
_LOCK_SLOTS: dict = {}


def _lock_slot(path: str) -> dict:
    with _LOCK_GUARD:
        slot = _LOCK_SLOTS.get(path)
        if slot is None:
            slot = {"tlock": threading.Lock(), "owner": None, "depth": 0}
            _LOCK_SLOTS[path] = slot
        return slot


@contextlib.contextmanager
def daemon_lock(registry_dir, name: str, *,
                timeout: float = MAINTENANCE_LOCK_TIMEOUT_SECONDS):
    """Hold the per-daemon lock for the duration of the block.

    Yields True when the lock is held and False when it could not be taken —
    including when the lock file itself cannot be created.  Every caller must
    branch on it, and every "no" is a reason *not* to act: the thing on the
    other side of this lock is either a respawn or a maintenance restart, and
    doing one while the other is in flight is the double start.
    """
    path = str(lock_path(registry_dir, name))
    slot = _lock_slot(path)
    me = threading.get_ident()

    if slot["owner"] == me:                      # re-entrant, same thread
        slot["depth"] += 1
        try:
            yield True
        finally:
            slot["depth"] -= 1
        return

    deadline = time.monotonic() + max(0.0, float(timeout))
    if not slot["tlock"].acquire(timeout=max(0.0, deadline - time.monotonic())):
        yield False
        return

    fd = None
    try:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            # No lock file means no serialisation, and no serialisation means
            # no permission to do anything destructive.
            yield False
            return
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(_LOCK_POLL_SECONDS)
        slot["owner"] = me
        slot["depth"] = 1
        try:
            yield True
        finally:
            slot["owner"] = None
            slot["depth"] = 0
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        slot["tlock"].release()


# ---------------------------------------------------------------------------
# Process identity — PID plus generation
# ---------------------------------------------------------------------------

#: The only read failures that mean "this process is not there".  Everything
#: else (EACCES under hidepid, EIO, EPERM …) means "could not look", and the
#: two must never collapse into one answer.  lib_mux keeps its own copy: it is
#: imported *by* this module, so it cannot import back.
_PROC_GONE_ERRNOS = frozenset({errno.ENOENT, errno.ESRCH})


def process_generation(pid, *, proc_root: str = "/proc") -> Optional[str]:
    """`"<pid>:<starttime>"`, or None when it cannot be read.

    `starttime` is field 22 of `/proc/<pid>/stat`: the clock ticks since boot
    at which the process started.  Together with the pid it identifies a
    *particular run*, which a bare pid cannot — pids are recycled, and on a
    machine that restarts daemons all day the recycled one is quite likely to
    be another crewvia process.  Comparing only the number is the same defect
    as trusting a reused Worker name (§5-2), one level down.
    """
    if pid is None:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        stat = Path(proc_root, str(pid), "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm (field 2) is parenthesised and may itself contain spaces / ')', so
    # split after the LAST ')': what follows starts at field 3 (state).
    try:
        tail = stat[stat.rindex(")") + 2:].split()
    except ValueError:
        return None
    if len(tail) < 20:  # field 22 == tail[19]
        return None
    return f"{pid}:{tail[19]}"


def instance_alive(pid, generation: Optional[str]) -> bool:
    """True only while *this very run* of `pid` is still going.

    Falls back to "alive" whenever the generation cannot be read: an
    unreadable `/proc` entry is not evidence of death, and the direction we
    must fail in is "do not respawn".
    """
    resolved = recorded_pid(pid)
    if resolved is None:
        return False
    if not process_alive(resolved):
        return False
    if not generation:
        return True
    current = process_generation(resolved)
    if current is None:
        return True
    return current == generation


def scan_daemon_pids(repo_root, name: str, *, proc_root="/proc",
                     exclude_pids: Optional[Iterable[int]] = None) -> Optional[List[int]]:
    """Live pids running `<repo_root>/scripts/<script>`, or None if unreadable.

    The needle is the **absolute** script path, so two crewvia checkouts on one
    machine (a worktree used for isolated QA, a second WSL) never see each
    other's daemons — matching on `"dispatcher.sh"` alone would make a test
    daemon look like production's.

    `None` means "could not look" and is not the same answer as `[]`.  Callers
    hold on None; only an empty list is evidence of absence.

    That distinction is made **per entry**, not just for the directory as a
    whole.  A process that vanished between the listing and the read really is
    gone (ENOENT), but a permission error or any other read failure proves
    nothing — and a scan that silently dropped such an entry would report
    "nothing running" on an incomplete walk, which is the one answer that
    authorises a respawn.  Under `hidepid` this makes the scan answer None
    every time; holding forever (with the 30-minute report as the way out) is
    the correct direction to fail in.
    """
    root = Path(proc_root)
    needle = str(Path(repo_root) / "scripts" / SCRIPT_OF[name])
    excluded = set(exclude_pids or ())
    excluded.add(os.getpid())
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    found: List[int] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in excluded:
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError as exc:
            if exc.errno in _PROC_GONE_ERRNOS:
                continue      # exited between listing and read — really gone
            return None       # could not read it — the walk is incomplete
        if not any(arg.decode("utf-8", "replace") == needle for arg in argv):
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            if stat[stat.rindex(")") + 2] == "Z":
                continue  # a zombie has already exited
        except (OSError, ValueError, IndexError):
            pass
        found.append(pid)
    return sorted(found)


# ---------------------------------------------------------------------------
# Pane ownership — is the thing in that pane ours to end?
# ---------------------------------------------------------------------------
#
# `repo_identity_ok()` asks whether *we* still have a checkout to act from.
# This asks the other half, which is the half the 2026-09-23 incident needed:
# whose daemon is in the pane we are about to kill?  A run rooted at a pytest
# tmpdir killed and replaced the pane that was running
# `/home/tkadmin/workspace/crewvia/scripts/dispatcher.sh`, and nothing in the
# path from CLI to `mux.kill()` ever looked at what was in there.
#
# Deliberately independent of the test-isolation marker in lib_mux: that one
# knows it is a test, this one does not care.  Two checkouts of crewvia on one
# machine — a QA worktree, a second WSL, a stale herdr env — are the ordinary
# case here, and the guard has to hold when nobody has declared anything.
#
# `lib_retirement._exit_evidence()` is the precedent: prove the thing you are
# about to act on is the thing you meant, from the process layer, before you
# act.

#: Who is in the pane.  `UNKNOWN` is not `NONE` — the distinction this repo has
#: now had to make at four separate layers.  The values are `lib_mux`'s: the
#: walk itself lives there because `lib_mux.kill()` needs the same answer for
#: its own backstop, and two copies of this judgment would drift.
PANE_OWNER_MINE = OWNER_MINE
PANE_OWNER_FOREIGN = OWNER_FOREIGN
PANE_OWNER_NONE = OWNER_NONE
PANE_OWNER_UNKNOWN = OWNER_UNKNOWN


def pane_daemon_owner(mux, name: str, repo_root, *, proc_root: str = "/proc"):
    """`(owner, detail)` — whose `name` daemon, if any, runs in `name`'s pane.

      MINE     a process under this pane runs `<repo_root>/scripts/<script>`
      FOREIGN  it runs that script out of a **different** checkout
      NONE     nothing recognisable is in there — a husk, or a fresh pane.
               Restarting into it is the ordinary crash-recovery path and must
               keep working (t035), so this is not a refusal.
      UNKNOWN  the pane's pid, /proc, a working directory, or the launch shape
               could not be read.  Not the same answer as NONE: production
               breaks precisely when things cannot be read, and a guard that
               only holds while everything is readable is not one.

    The identification is `lib_mux.pane_script_owner()`, which resolves the
    script a process is actually *running* against that process's own working
    directory.  The earlier version matched any argument ending in
    `/scripts/<script>`, which meant a daemon started the way people actually
    start one — `bash scripts/dispatcher.sh`, a relative path — matched
    nothing and its pane was classified as empty, i.e. safe to destroy.
    """
    try:
        pane_pid = mux.pid(name)
    except Exception as exc:                      # a backend may still throw
        return PANE_OWNER_UNKNOWN, f"could not ask the backend for the pane pid: {exc!r}"
    if pane_pid is None:
        return PANE_OWNER_UNKNOWN, f"no pane pid for {name!r}"

    script = SCRIPT_OF[name]
    mine = str(Path(repo_root) / "scripts" / script)
    return pane_script_owner(pane_pid, script, mine, proc_root=proc_root)


#: What may be killed.  An allowlist, because the denylist version of this
#: question — "is it obviously someone else's?" — is the shape that let three
#: earlier defects through: every answer it has not thought of falls on the
#: destructive side (memory: approve-judgment-needs-allowlist-and-scope).
_MAY_KILL_OWNERS = frozenset({PANE_OWNER_MINE, PANE_OWNER_NONE})


# ---------------------------------------------------------------------------
# Launch command — one source of truth, shared with start.sh
# ---------------------------------------------------------------------------

#: Which mux, and — just as important — *which one of it*.  CREWVIA_MUX alone
#: decides the backend but not the destination: a daemon that inherits only
#: the mode falls back to the default session / workspace name, and then every
#: mux verb it performs lands somewhere nobody is looking — it would spawn its
#: own peer into a session no one is attached to and report to a Director that
#: isn't there.  The QA harness hit this for real (t006 QA FINDING-2);
#: production is invisible to it only because production uses the default
#: names.
#:
#: `CREWVIA_MUX_TEST_ISOLATION` / `CREWVIA_MUX_PANE_PREFIX` belong here for a
#: reason that is easy to miss: a process started **through the mux** inherits
#: the mux *server's* environment, not its caller's (herdr replays the env its
#: server was born with onto every pane).  So a daemon respawned from inside a
#: namespaced test pane would come back holding neither marker, address the
#: bare production pane names, and — on herdr — be able to read the production
#: checkout's cache entry for that bare name.  The command text is the only
#: thing that crosses the spawn boundary, so both travel in it.
_SPAWN_ENV_MUX = (
    "CREWVIA_MUX_TEST_ISOLATION",
    "CREWVIA_MUX_PANE_PREFIX",
    "CREWVIA_MUX",
    "CREWVIA_MUX_ENABLED",
    "CREWVIA_TMUX_SESSION",
    "CREWVIA_HERDR_WORKSPACE",
    "CREWVIA_HERDR_SOCK",
)

#: What the daemon *does*, and to whose files.  A respawn that drops these
#: does not bring the daemon back — it starts a different daemon wearing the
#: same name:
#:
#:   CREWVIA_QUEUE            it begins assigning out of another queue
#:   CREWVIA_KILL_AUTHORITY   dispatcher and watchdog end up disagreeing about
#:                            who may end a Worker, which dispatcher's own code
#:                            calls out as guaranteed breakage in both
#:                            directions (nobody closes a window, or two kills
#:                            land on a reused name)
#:   CREWVIA_TASKVIA          a "disabled" run starts talking to Taskvia again
#:   CREWVIA_NOTIFY_CACHE     an isolated QA daemon rejoins production's shared
#:                            notify cache — the exact shape that produced a
#:                            false PASS before (memory:
#:                            dispatcher-isolated-qa-harness)
#:   CREWVIA_*_GRACE, BENCH   timing overrides a harness set on purpose
_SPAWN_ENV_OPERATIONAL = (
    "CREWVIA_QUEUE",
    "CREWVIA_KILL_AUTHORITY",
    "CREWVIA_TASKVIA",
    "CREWVIA_PROJECT",
    "CREWVIA_NOTIFY_CACHE",
    "CREWVIA_SPAWN_GRACE",
    "CREWVIA_STATE_GRACE",
    "CREWVIA_BENCH_MODE",
)

#: The mutual watch's own settings.  Asymmetry here is self-inflicted flap:
#: a checkout running with relaxed thresholds respawns its peer, the peer comes
#: back with the shipped defaults, reads the relaxed side as stale, and starts
#: it again.
_SPAWN_ENV_WATCH = ("CREWVIA_DAEMON_MUTUAL_WATCH",) + tuple(
    f"CREWVIA_DAEMON_{key.upper()}" for key in (
        "dispatcher_stale_seconds", "watchdog_stale_seconds",
        "flap_window_seconds", "flap_threshold", "respawn_grace_seconds",
        "pause_report_after_seconds", "hold_report_after_seconds",
        "watch_lock_timeout_seconds", "maintenance_lock_timeout_seconds",
    ))

#: An allowlist, and deliberately not `os.environ`.  Carrying everything would
#: (a) print TASKVIA_TOKEN / NTFY_PASS into `ps`, the pane's scrollback and
#: every mux log, and (b) drag along whatever the herdr server happened to be
#: born with — the stale-env trap this repo already has a memory note about.
#: Secrets keep travelling the way they do today, through the environment the
#: pane is created in; a daemon that comes back without one degrades (no
#: Taskvia sync) instead of leaking it.
#:
#: Transitive by construction: because these are exported into the daemon's
#: own environment, the daemon it later respawns receives them in turn.
_SPAWN_ENV_VARS = _SPAWN_ENV_MUX + _SPAWN_ENV_OPERATIONAL + _SPAWN_ENV_WATCH


def _sh_single_quote(value: str) -> str:
    """`'...'` with embedded quotes escaped, so a session name cannot inject."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def spawn_command(name: str, repo_root, *, env=None) -> str:
    """The exact command `start.sh` uses to launch `name`.

    `Mux.spawn()` takes an `env=` argument that **both backends ignore**, and
    herdr additionally replays its server's startup environment onto every new
    pane — so a daemon started by its peer would inherit whatever that server
    was born with rather than what the launcher meant.  The only thing that
    reliably crosses the spawn boundary is the command text, so the mux
    variables travel inside it (start.sh's `_MUX_ENV_PREFIX`, same shape).

    One `export` per variable, not one shared statement: the shape is asserted
    on elsewhere, and a single joined statement changes when an unrelated
    variable happens to be set in the caller's environment.

    start.sh calls this through the CLI instead of keeping its own copy: a
    literal in two places is how the launched and the respawned daemon come to
    differ by exactly the variable that decides which backend they talk to.
    """
    repo_root = Path(repo_root)
    scripts = repo_root / "scripts"
    env = os.environ if env is None else env
    values = {var: str(env.get(var, "") or "") for var in _SPAWN_ENV_VARS}
    prefix = "".join(f"export {var}={_sh_single_quote(value)}; "
                     for var, value in values.items() if value)
    if name == DAEMON_DISPATCHER:
        body = f"bash {_sh_single_quote(scripts / 'dispatcher.sh')}"
    elif name == DAEMON_WATCHDOG:
        body = f"python3 {_sh_single_quote(scripts / 'watchdog.py')}"
    else:
        raise ValueError(f"unknown daemon: {name!r}")
    # The repo path is data too.  It was written bare inside `'...'`, which a
    # checkout under `/home/o'brien/` closes — and everything after the
    # apostrophe is then command, not path.  Quoting the env values but not
    # the paths is the shape where the *unremarkable* input is the dangerous
    # one, so every interpolation goes through the same function.
    return f"cd {_sh_single_quote(repo_root)} && {prefix}{body}"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class WatchConfig:
    """Thresholds, all overridable from `config/crewvia.yaml` and the env.

    The stale thresholds are per-daemon because the cycle times differ by 6x,
    and watchdog additionally blocks for up to 70s inside
    `graceful_terminate()`.  Each must leave room for several missed cycles:
    a threshold tight enough to trip on one slow cycle turns a healthy daemon
    into a respawn candidate, which is the double-start we are guarding.
    """

    enabled: bool = True
    dispatcher_stale_seconds: int = 60      # 12 cycles of 5s
    watchdog_stale_seconds: int = 240       # 8 cycles of 30s, > the 70s block
    flap_window_seconds: int = 900
    flap_threshold: int = 3
    respawn_grace_seconds: int = 120
    pause_report_after_seconds: int = 1800
    hold_report_after_seconds: int = 1800
    #: How long each side waits for the per-daemon lock (see `daemon_lock`).
    #: The watch side is short on purpose — a watcher that blocks on the lock
    #: is a watcher that is not running its own daemon's cycle, and holding is
    #: free: it looks again next cycle.
    watch_lock_timeout_seconds: int = 2
    maintenance_lock_timeout_seconds: int = 60

    def stale_seconds_for(self, name: str) -> int:
        return (self.dispatcher_stale_seconds if name == DAEMON_DISPATCHER
                else self.watchdog_stale_seconds)


_CONFIG_KEYS = (
    "dispatcher_stale_seconds", "watchdog_stale_seconds", "flap_window_seconds",
    "flap_threshold", "respawn_grace_seconds", "pause_report_after_seconds",
    "hold_report_after_seconds", "watch_lock_timeout_seconds",
    "maintenance_lock_timeout_seconds",
)


def _truthy(value: str) -> bool:
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def load_config(config_path=None, env=None) -> WatchConfig:
    """`config/crewvia.yaml` → WatchConfig, with env vars winning.

    Unreadable / absent config is not an error: the defaults above are the
    shipped values, and a daemon must not refuse to start because a key is
    missing.
    """
    env = os.environ if env is None else env
    if config_path is None:
        config_path = _SCRIPTS_DIR.parent / "config" / "crewvia.yaml"
    cfg = WatchConfig()

    block = {}
    if yaml is not None:
        try:
            loaded = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict) and isinstance(loaded.get("daemons"), dict):
                block = loaded["daemons"]
        except Exception:
            block = {}

    if "mutual_watch" in block:
        cfg.enabled = bool(block["mutual_watch"])
    for key in _CONFIG_KEYS:
        if key in block:
            try:
                setattr(cfg, key, int(block[key]))
            except (TypeError, ValueError):
                pass

    if "CREWVIA_DAEMON_MUTUAL_WATCH" in env:
        cfg.enabled = _truthy(str(env["CREWVIA_DAEMON_MUTUAL_WATCH"]))
    for key in _CONFIG_KEYS:
        var = f"CREWVIA_DAEMON_{key.upper()}"
        if var in env:
            try:
                setattr(cfg, key, int(str(env[var]).strip()))
            except (TypeError, ValueError):
                pass
    return cfg


# ---------------------------------------------------------------------------
# Maintenance marker
# ---------------------------------------------------------------------------

def pause(registry_dir, name: str, *, reason: str = "", now=None,
          timeout: Optional[float] = None) -> Optional[str]:
    """Suppress respawn of `name`; returns the token that can lift it.

    Needed because the documented restart recipe is "kill, then spawn": in the
    gap between the two the daemon genuinely is dead, and without a marker the
    peer is right to respawn it — landing a second copy next to the one the
    operator is about to start by hand.

    The token binds the marker to *this* restart.  Two overlapping restarts
    would otherwise have the first one's `resume` lift the second one's
    protection, which is the same "name is not identity" mistake the rest of
    this module is built around.

    **Returns None when the marker did not reach the disk** — a full disk, a
    read-only registry, a directory that isn't one.  A token handed back for a
    marker nobody can read is a promise of protection that does not exist, and
    the caller's very next step is destructive.  Callers must treat None as
    "do not proceed" (`restart()` does).

    Taken under the daemon's lock so that it cannot land in the middle of a
    watcher's decision: the peer must either see this marker or still be
    waiting for the lock when it looks.
    """
    with daemon_lock(registry_dir, name,
                     timeout=(MAINTENANCE_LOCK_TIMEOUT_SECONDS
                              if timeout is None else timeout)) as locked:
        if not locked:
            return None
        return _write_pause_marker(registry_dir, name, reason=reason, now=now)


def _write_pause_marker(registry_dir, name: str, *, reason: str = "",
                        now=None) -> Optional[str]:
    """The marker write itself.  Assumes the daemon's lock is already held."""
    token = uuid.uuid4().hex
    path = pause_path(registry_dir, name)
    ok = write_json_atomic(path, {
        "daemon": name,
        "token": token,
        "reason": reason,
        "created_at": float(now if now is not None else time.time()),
        "by_pid": os.getpid(),
    })
    if not ok:
        return None
    # Read it back.  write_json_atomic() reports on the write; this reports on
    # what a *reader* will find, which is the thing actually being promised.
    marker = read_json(path)
    if not marker or marker.get("token") != token:
        return None
    return token


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _remove_marker(path) -> bool:
    """True once `path` is gone.  Unlike `unlink_quiet()`, it reports.

    `unlink_quiet()` swallows every OSError, which is right where the removal
    is housekeeping and wrong where it *is* the promise being made.  A marker
    that is a directory, or sits in a directory we may not write, survives the
    unlink — and a caller that answered "lifted" to that leaves the protection
    in place while telling everyone it is gone, which parks mutual watch on a
    hold that only the 30-minute stale-pause report ever escapes.
    """
    path = Path(path)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return not path.exists()


def resume(registry_dir, name: str, *, token: Optional[str] = None,
           force: bool = False,
           timeout: float = MAINTENANCE_LOCK_TIMEOUT_SECONDS,
           log: Callable[[str], None] = _stderr) -> bool:
    """Lift the marker.  False when it belongs to someone else.

    Under the daemon's lock, for the same reason `pause()` is: read, verify
    and unlink are three instants, and between the first and the third another
    maintenance run can replace the marker with its own.  Unlinking then
    removes *that* run's protection — the checked token was never the token of
    the file that got deleted — and the peer is free to respawn next to the
    kill the second operator is about to do.  Classic read-modify-write; the
    only reason it looks safe is that the write is a delete.

    Re-entrant for `restart()`, which already holds this lock when its
    `finally:` gets here.

    Returns False when the lock cannot be taken: no serialisation, no
    permission to remove a protection.  The marker stays, and the stale-pause
    report is the way out.

    Returns False, too, when the marker could not actually be removed — see
    `_remove_marker()`.  "Lifted" is a statement about the file on disk, and
    the only honest way to make it is to look.
    """
    with daemon_lock(registry_dir, name, timeout=timeout) as locked:
        if not locked:
            return False
        path = pause_path(registry_dir, name)
        state, marker = read_pause_state(registry_dir, name)
        if state == PAUSE_ABSENT:
            return True  # nothing to lift — idempotent
        if state == PAUSE_UNREADABLE:
            # `--force` is exactly the case for a marker nobody can parse: an
            # operator looking at a corrupt file and deciding it is rubbish.
            # Without it there is nothing to check the token against, so the
            # honest answer is "no".
            if not force:
                return False
            if _remove_marker(path):
                return True
            log(f"[daemon-watch] resume {name}: the pause marker at {path} "
                f"could not be read *and* could not be removed (is it a "
                f"directory, or is {path.parent} not writable?). The "
                f"protection is still in place, so mutual watch will keep "
                f"holding — remove it by hand.")
            return False
        if not force and token is None:
            return False
        if not force and marker.get("token") != token:
            return False
        if _remove_marker(path):
            return True
        log(f"[daemon-watch] resume {name}: the pause marker at {path} could "
            f"not be removed. The protection is still in place and mutual "
            f"watch will keep holding — remove it by hand.")
        return False


#: `read_pause_state()` outcomes.  Three, not two: "there is no marker" and
#: "there may be a marker and we cannot read it" authorise opposite actions,
#: and `read_json()` — which answers None to missing, unreadable and corrupt
#: alike — cannot tell them apart.  The caller that matters here is `_decide()`,
#: whose next step on "no marker" is a respawn.
PAUSE_ABSENT = "absent"
PAUSE_ACTIVE = "active"
PAUSE_UNREADABLE = "unreadable"


def read_pause_state(registry_dir, name: str):
    """`(state, marker)` for `name`'s pause marker.

    Only `FileNotFoundError` is evidence of absence.  A directory in the
    marker's place, a permission error, a half-written file, a document that
    is not an object — none of those say the maintenance is over, and reading
    them as "no marker" lifts a protection nobody lifted.
    """
    path = pause_path(registry_dir, name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PAUSE_ABSENT, None
    except OSError:
        return PAUSE_UNREADABLE, None
    try:
        data = json.loads(text)
    except ValueError:
        return PAUSE_UNREADABLE, None
    if not isinstance(data, dict):
        return PAUSE_UNREADABLE, None
    return PAUSE_ACTIVE, data


def read_pause(registry_dir, name: str) -> Optional[dict]:
    """The marker, or None when there is none *or* it cannot be read.

    Kept for the callers that only display it (`status`).  Anything that acts
    on the answer must use `read_pause_state()` — collapsing the two Nones is
    the defect this pair exists to separate.
    """
    return read_pause_state(registry_dir, name)[1]


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def read_heartbeat(registry_dir, name: str) -> Optional[dict]:
    hb = read_json(heartbeat_path(registry_dir, name))
    if hb is None:
        return None
    try:
        hb["updated_at"] = float(hb.get("updated_at"))
    except (TypeError, ValueError):
        return None
    pid = recorded_pid(hb.get("pid"))
    if pid is None:
        return None
    hb["pid"] = pid
    return hb


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

ACTION_HEALTHY = "healthy"
ACTION_RESPAWNED = "respawned"
ACTION_HOLD = "hold"
ACTION_PAUSED = "paused"
ACTION_FLAPPING = "flapping"
ACTION_DISABLED = "disabled"
ACTION_REFUSED = "refused"
ACTION_GRACE = "grace"


class Verdict(NamedTuple):
    action: str
    reason: str
    down_seconds: Optional[float] = None


def _minutes(down_seconds: Optional[float]) -> str:
    if down_seconds is None:
        return "不明"
    return f"{int(down_seconds // 60)} 分"


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------

@dataclass
class DaemonWatch:
    """One daemon's view of its peer.  Every observation is an injected seam.

    `mux`, `scan`, `now` and `identity_ok` are parameters rather than module
    lookups so the whole decision table can be driven in tests without a mux
    backend, a real /proc or a production registry — the isolation the
    dispatcher QA harness had to build by hand (memory:
    dispatcher-isolated-qa-harness).
    """

    registry_dir: Path
    repo_root: Path
    self_name: str
    mux: object = None
    config: WatchConfig = field(default_factory=WatchConfig)
    log: Callable[[str], None] = lambda msg: None
    notify: Optional[Callable[[str], bool]] = None
    now: Callable[[], float] = time.time
    scan: Optional[Callable[[object, str], Optional[List[int]]]] = None
    identity_ok: Optional[Callable[[], bool]] = None

    def __post_init__(self) -> None:
        self.registry_dir = Path(self.registry_dir)
        self.repo_root = Path(self.repo_root)
        if self.self_name not in PEER_OF:
            raise ValueError(f"unknown daemon: {self.self_name!r}")
        self.peer_name = PEER_OF[self.self_name]
        if self.mux is None:
            self.mux = Mux()
        if self.scan is None:
            self.scan = scan_daemon_pids
        if self.identity_ok is None:
            self.identity_ok = lambda: repo_identity_ok(self.repo_root)
        self._started_at = float(self.now())
        daemons_dir(self.registry_dir).mkdir(parents=True, exist_ok=True)

    # -- 1. own heartbeat ---------------------------------------------------

    def beat(self, pid: Optional[int] = None, generation: Optional[str] = None,
             window: Optional[str] = None) -> None:
        """Record that this daemon is alive, right now.

        `pid` is a parameter because the process that *writes* is not always
        the process that *lives*: dispatcher re-execs python every cycle, so
        the enduring process is its bash wrapper and that is the pid the peer
        must probe.  (dispatcher writes from bash for the same reason — see
        `lib_daemon_watch.sh`; this path is the fallback and the format
        reference.)
        """
        pid = os.getpid() if pid is None else int(pid)
        if generation is None:
            generation = process_generation(pid)
        stamp = float(self.now())
        write_json_atomic(heartbeat_path(self.registry_dir, self.self_name), {
            "daemon": self.self_name,
            "pid": pid,
            "generation": generation,
            "started_at": self._started_at,
            "updated_at": stamp,
            "window": window or self.self_name,
            "repo_root": str(self.repo_root),
            "version": HEARTBEAT_VERSION,
        })

    # -- 4. reports that keep trying ---------------------------------------

    def _director_name(self) -> str:
        try:
            names = self.mux.list(suffix="-director")
        except Exception:
            names = []
        return names[0] if names else "Sora-director"

    def _send(self, message: str) -> bool:
        if self.notify is not None:
            return bool(self.notify(message))
        try:
            return bool(self.mux.send(self._director_name(), message))
        except Exception:
            return False

    def _report(self, key: str, message: str) -> None:
        """Say it once — and keep trying until it is actually said.

        A `send()` that returned False looks identical to one that worked if
        nobody checks, and the report this module exists to produce is the
        only trace a silent outage leaves.  Undelivered reports are persisted
        and retried at the top of every cycle (`flush_reports`).
        """
        state = self._read_reports()
        if any(r.get("key") == key for r in state["pending"]):
            return
        if key in state["delivered"]:
            return
        if self._send(message):
            state["delivered"][key] = float(self.now())
        else:
            state["pending"].append({
                "key": key, "message": message, "created_at": float(self.now()),
            })
            self.log(f"[daemon-watch] report not delivered, will retry: {key}")
        self._write_reports(state)

    def flush_reports(self) -> None:
        state = self._read_reports()
        if not state["pending"]:
            return
        still: List[dict] = []
        for report in state["pending"]:
            if self._send(report.get("message", "")):
                state["delivered"][report.get("key", "")] = float(self.now())
                self.log(f"[daemon-watch] queued report delivered: {report.get('key')}")
            else:
                still.append(report)
        state["pending"] = still
        self._write_reports(state)

    def _read_reports(self) -> dict:
        data = read_json(reports_path(self.registry_dir, self.self_name)) or {}
        pending = data.get("pending")
        delivered = data.get("delivered")
        return {
            "pending": pending if isinstance(pending, list) else [],
            "delivered": delivered if isinstance(delivered, dict) else {},
        }

    def _write_reports(self, state: dict) -> None:
        path = reports_path(self.registry_dir, self.self_name)
        if not state["pending"] and not state["delivered"]:
            unlink_quiet(path)
            return
        if not state["pending"]:
            # Nothing outstanding: keep only the "already said" ledger, which
            # is what stops a resolved incident being re-announced forever.
            write_json_atomic(path, {"pending": [], "delivered": state["delivered"]})
            return
        write_json_atomic(path, state)

    # -- state ---------------------------------------------------------------

    def _read_state(self) -> dict:
        data = read_json(watch_state_path(self.registry_dir, self.peer_name)) or {}
        return {
            "grace_until": float(data.get("grace_until") or 0.0),
            "last_respawn_at": data.get("last_respawn_at"),
            "hold_since": data.get("hold_since"),
        }

    def _write_state(self, state: dict) -> None:
        write_json_atomic(watch_state_path(self.registry_dir, self.peer_name), state)

    # -- 5. flap guard -------------------------------------------------------

    def _flap_entries(self) -> List[dict]:
        data = read_json(respawn_log_path(self.registry_dir, self.peer_name)) or {}
        entries = data.get("entries")
        if not isinstance(entries, list):
            return []
        cutoff = float(self.now()) - self.config.flap_window_seconds
        kept = []
        for entry in entries:
            try:
                if float(entry.get("at")) >= cutoff:
                    kept.append(entry)
            except (TypeError, ValueError):
                continue
        return kept

    def _record_respawn(self, entries: List[dict], replaced_generation) -> None:
        entries = list(entries) + [{
            "at": float(self.now()),
            "by": self.self_name,
            "daemon": self.peer_name,
            # Which instance this respawn replaced.  A bare count cannot tell
            # "restarted the same corpse three times" (a real flap) from
            # "three healthy generations came and went over the window".
            "replaced_generation": replaced_generation,
        }]
        write_json_atomic(respawn_log_path(self.registry_dir, self.peer_name),
                          {"daemon": self.peer_name, "entries": entries})

    # -- 2 + 3. the decision -------------------------------------------------

    def watch_peer(self) -> Verdict:
        """One cycle of mutual watch.  Never raises into the caller's loop."""
        try:
            return self._watch_peer()
        except Exception as exc:                      # pragma: no cover - guard
            self.log(f"[daemon-watch] cycle error: {exc!r}")
            return Verdict(ACTION_HOLD, f"cycle error: {exc!r}")

    def _watch_peer(self) -> Verdict:
        self.flush_reports()
        peer = self.peer_name
        now = float(self.now())

        if not self.config.enabled:
            return Verdict(ACTION_DISABLED, "daemons.mutual_watch is off")

        # t003, extended from "may not kill" to "may not start".  A daemon
        # whose own checkout is gone can still be holding a production mux
        # workspace through herdr's inherited env; it has nothing left to
        # prove ownership with, so it acts on nothing.
        if not self.identity_ok():
            return Verdict(ACTION_REFUSED,
                           f"self-identity check failed for {self.repo_root}")

        # Everything from here to the spawn is one transaction.  Reading the
        # pause marker outside it would only prove the marker was absent at
        # *that* instant — which is exactly the gap a maintenance restart
        # slips into (see `daemon_lock`).
        with daemon_lock(self.registry_dir, peer,
                         timeout=self.config.watch_lock_timeout_seconds) as locked:
            if not locked:
                # Somebody else is mid-decision about this daemon.  Holding
                # costs one cycle; guessing costs two daemons.
                return self._hold(
                    self._read_state(), now,
                    f"another restart or respawn of {peer} is in flight "
                    f"(could not take {lock_path(self.registry_dir, peer)} within "
                    f"{self.config.watch_lock_timeout_seconds}s)")
            return self._decide(now)

    def _decide(self, now: float) -> Verdict:
        """The judgment itself.  Runs with this peer's lock held."""
        peer = self.peer_name

        state = self._read_state()

        pause_state, marker = read_pause_state(self.registry_dir, peer)
        if pause_state == PAUSE_UNREADABLE:
            # A marker is there — or might be — and we cannot say whose.  The
            # only two things to do are "respawn" and "wait", and a respawn
            # into somebody's half-done maintenance is the double start.
            return self._hold(
                state, now,
                f"{peer} の停止マーカー ({pause_path(self.registry_dir, peer)}) が "
                f"読めません。maintenance 中かどうか判断できないので respawn は "
                f"しません")
        if pause_state == PAUSE_ACTIVE:
            self._maybe_report_stale_pause(marker, now)
            return Verdict(ACTION_PAUSED,
                           f"{peer} is paused for maintenance "
                           f"({marker.get('reason') or 'no reason given'})")

        if now < state["grace_until"]:
            since = state.get("last_respawn_at")
            ago = f"{now - float(since):.0f}s ago" if since else "just now"
            return Verdict(ACTION_GRACE,
                           f"{peer} was respawned {ago} — still booting, and a "
                           f"daemon that has not written its first heartbeat yet "
                           f"looks exactly like one that died")

        hb = read_heartbeat(self.registry_dir, peer)

        # A heartbeat from another checkout is another crewvia's business.
        if hb is not None and hb.get("repo_root") not in (None, str(self.repo_root)):
            return Verdict(ACTION_REFUSED,
                           f"{peer} heartbeat belongs to {hb.get('repo_root')!r}, "
                           f"not {str(self.repo_root)!r}")

        down_seconds: Optional[float] = None
        if hb is not None:
            down_seconds = now - hb["updated_at"]
            if down_seconds < self.config.stale_seconds_for(peer):
                self._clear_hold(state)
                return Verdict(ACTION_HEALTHY,
                               f"{peer} heartbeat is {down_seconds:.0f}s old",
                               down_seconds)

        # --- evidence (b): the process ------------------------------------
        # Asked first, and decisive.  /proc answers without the mux backend's
        # involvement, so it is the one signal a backend outage cannot forge.
        if hb is not None and instance_alive(hb.get("pid"), hb.get("generation")):
            return self._hold(
                state, now,
                f"{peer} heartbeat is stale ({_fmt(down_seconds)}) but its recorded "
                f"process {hb.get('pid')} (generation {hb.get('generation')}) is still "
                f"running — wedged, not dead; respawning would make two of them")

        live = self.scan(self.repo_root, peer)
        if live is None:
            return self._hold(state, now,
                              f"could not read /proc to look for {peer} — "
                              f"'could not look' is not 'nothing there'")
        if live:
            return self._hold(
                state, now,
                f"{peer} heartbeat is stale ({_fmt(down_seconds)}) but pid(s) "
                f"{','.join(str(p) for p in live)} are still running it")

        # No window-list check stands between here and the spawn.  See the
        # "the tab is not evidence" note in the module docstring: a pane's
        # name outlives the process inside it, so requiring the name to be
        # absent meant never respawning after an ordinary crash.  The pane's
        # *contents* still guard the spawn — mux.spawn() refuses a pane that
        # holds a live process — which is a stronger check than the name was.

        # --- flap guard -----------------------------------------------------
        entries = self._flap_entries()
        if len(entries) >= self.config.flap_threshold:
            self._report(
                f"flap:{peer}:{int(entries[0]['at'])}",
                f"[daemon-watch] {peer} の respawn が短時間に繰り返し発生しています "
                f"({len(entries)} 回 / {self.config.flap_window_seconds // 60} 分)。"
                f"自動 respawn を停止しました。{peer} が起動直後に落ちる原因 "
                f"(logs/{peer}/ の最新ログ) を見てください。復旧後は "
                f"scripts/lib_daemon_watch.py restart {peer} で起こし直せます。")
            return self._hold(state, now,
                              f"flap guard: {len(entries)} respawns of {peer} within "
                              f"{self.config.flap_window_seconds}s",
                              action=ACTION_FLAPPING)

        # --- respawn --------------------------------------------------------
        # `peer` is a constant name that was never re-resolved between the
        # judgment above and this call — no TOCTOU window to lose.
        cmd = spawn_command(peer, self.repo_root)
        started = False
        try:
            started = bool(self.mux.spawn(peer, cmd, cwd=str(self.repo_root)))
        except Exception as exc:
            self.log(f"[daemon-watch] spawn of {peer} raised: {exc!r}")
        if not started:
            # spawn() returns False when a live process already holds the name
            # — the backend's own double-start guard, and the last line of
            # defence behind everything above.  Treat it as "do not proceed".
            return self._hold(state, now,
                              f"spawn of {peer} did not start anything (a live "
                              f"one in the tab, or the backend refused)")

        self._record_respawn(entries, hb.get("generation") if hb else None)
        # Persisted, not held in memory: the grace has to survive a restart of
        # *this* daemon too, or a watcher that bounces right after respawning
        # its peer comes back with a clean slate and respawns it again.
        state["grace_until"] = now + self.config.respawn_grace_seconds
        state["last_respawn_at"] = now
        state["hold_since"] = None
        self._write_state(state)
        self.log(f"[daemon-watch] respawned {peer} ({_fmt(down_seconds)} down)")
        self._report(
            f"respawn:{peer}:{int(now)}",
            f"[daemon-watch] {peer} が停止していたので再起動しました。"
            f"停止推定 {_minutes(down_seconds)} (最終 heartbeat 基準)。対応は不要です。")
        return Verdict(ACTION_RESPAWNED, f"respawned {peer}", down_seconds)

    # -- helpers -------------------------------------------------------------

    def _clear_hold(self, state: dict) -> None:
        if state.get("hold_since") is not None:
            state["hold_since"] = None
            self._write_state(state)

    def _hold(self, state: dict, now: float, reason: str,
              action: str = ACTION_HOLD) -> Verdict:
        """Hold, and make sure the hold has a way out.

        Every branch above is "wait and look again next cycle", which is right
        for the transient causes (the backend answers, the wedged daemon dies)
        and silent for the ones that never resolve on their own.  A hold that
        outlives `hold_report_after_seconds` is escalated to the Director
        once — not automatically resolved, because every automatic resolution
        available here is a respawn, and a respawn is the dangerous direction.
        """
        if state.get("hold_since") is None:
            state["hold_since"] = now
            self._write_state(state)
        held_for = now - float(state["hold_since"])
        if held_for >= self.config.hold_report_after_seconds:
            self._report(
                f"hold:{self.peer_name}:{int(state['hold_since'])}",
                f"[daemon-watch] {self.peer_name} の生死を "
                f"{int(held_for // 60)} 分ぶん判定できないままです: {reason}。"
                f"自動 respawn は安全側で止めています。")
        self.log(f"[daemon-watch] hold ({self.peer_name}): {reason}")
        return Verdict(action, reason)

    def _maybe_report_stale_pause(self, marker: dict, now: float) -> None:
        """A forgotten pause marker disables mutual watch for good.

        Lifting it automatically is the wrong repair — "the marker is old" is
        no evidence that the maintenance finished, and respawning into an
        operator's half-done restart is the double start again.  So the marker
        stands and a person is told, once, keyed to the marker's own token so
        a later marker gets its own report.
        """
        try:
            age = now - float(marker.get("created_at"))
        except (TypeError, ValueError):
            return
        if age < self.config.pause_report_after_seconds:
            return
        token = marker.get("token") or "notoken"
        self._report(
            f"stale-pause:{self.peer_name}:{token}",
            f"[daemon-watch] {self.peer_name} の停止マーカー (paused) が "
            f"{int(age // 60)} 分残っており、その間は相互監視が効きません。"
            f"理由: {marker.get('reason') or '(未記載)'}。作業が終わっているなら "
            f"scripts/lib_daemon_watch.py resume {self.peer_name} --force で解除してください。")


def _fmt(down_seconds: Optional[float]) -> str:
    return "no heartbeat ever" if down_seconds is None else f"{down_seconds:.0f}s"


# ---------------------------------------------------------------------------
# Launch / restart helpers
# ---------------------------------------------------------------------------

def spawn_daemon(name: str, *, repo_root=None, mux=None,
                 timeout: float = MAINTENANCE_LOCK_TIMEOUT_SECONDS,
                 log: Callable[[str], None] = print) -> bool:
    """Start `name`, under its lock, with the one launch command.

    There are three things that start a daemon — `./crewvia`, the peer's
    mutual watch, and a maintenance restart — and a lock that only two of them
    take is not a lock.  Running `./crewvia` while a peer has just decided to
    respawn produces exactly the double start everything else here is built to
    prevent, so the launcher comes through this door too.
    """
    repo_root = Path(repo_root) if repo_root is not None else _SCRIPTS_DIR.parent
    mux = mux if mux is not None else Mux()
    registry_dir = repo_root / "registry"
    with daemon_lock(registry_dir, name, timeout=timeout) as locked:
        if not locked:
            log(f"[daemon-watch] spawn {name}: another restart or respawn is "
                f"already in flight — not starting a second one")
            return False
        return bool(mux.spawn(name, spawn_command(name, repo_root),
                              cwd=str(repo_root)))

def restart(name: str, *, repo_root=None, mux=None, reason: str = "manual restart",
            log: Callable[[str], None] = print, force: bool = False,
            proc_root: str = "/proc") -> bool:
    """Stop and start `name` without its peer racing us into a double start.

    Order is load-bearing: **pause first**, then kill.  The recipe people
    actually run is "kill, then spawn", and in the gap the daemon really is
    dead — a peer watching at that moment is correct to respawn it, and the
    operator's own spawn a second later lands on top.  Pausing first closes
    the gap; resuming last reopens the watch only once the new one is up.

    The pane is also checked before the kill: what is in it has to be *this*
    checkout's daemon, or nothing recognisable at all.  Restarting a daemon
    that belongs to another checkout is what a pytest run did to production on
    2026-09-23, and `--repo-root` pointing somewhere harmless is no protection
    at all — the mux destination comes from the environment, not from it.

    `force=True` is the operator's way past an *unreadable* pane (or a foreign
    one they mean to take over).  It exists so the refusal is a pause rather
    than a dead end: a fail-closed guard with no exit is how "cannot judge"
    turns into "nothing ever works again" (memory: fail-closed-discard-vs-hold).
    """
    repo_root = Path(repo_root) if repo_root is not None else _SCRIPTS_DIR.parent
    mux = mux if mux is not None else Mux()
    registry_dir = repo_root / "registry"

    # The whole transaction, not just the marker: the peer's judgment and its
    # spawn are one interval too, so overlapping them is what produced the
    # double start.  Taking the lock here also means the marker written below
    # cannot appear *after* a peer has already decided to respawn.
    with daemon_lock(registry_dir, name,
                     timeout=MAINTENANCE_LOCK_TIMEOUT_SECONDS) as locked:
        if not locked:
            log(f"[daemon-watch] restart {name}: another restart or respawn is "
                f"already in flight (could not take {lock_path(registry_dir, name)})")
            return False

        # Before the marker, not after: a refusal here must leave nothing
        # behind, and a pause marker left on disk silently disables mutual
        # watch until somebody notices the 30-minute stale-pause report.
        owner, detail = (
            (PANE_OWNER_MINE, "forced") if force
            else pane_daemon_owner(mux, name, repo_root, proc_root=proc_root))
        if owner not in _MAY_KILL_OWNERS:
            log(f"[daemon-watch] restart {name}: refused — the pane is not "
                f"this checkout's to end ({owner}: {detail}). Nothing was "
                f"killed. Pass --force to override.")
            return False

        token = _write_pause_marker(registry_dir, name, reason=reason)
        if token is None:
            # No marker, no protection.  Killing now would hand the peer a
            # daemon that is genuinely dead and has nothing saying why.
            log(f"[daemon-watch] restart {name}: refused — the pause marker "
                f"could not be written ({pause_path(registry_dir, name)}). "
                f"Nothing was killed.")
            return False
        try:
            # `force` has to reach the mux layer too.  Its identity backstop
            # reads the pane rather than the environment, so an exit that only
            # lifts the check *here* would look like an exit and not be one.
            if force:
                mux.kill(name, allow_foreign=True)
            else:
                mux.kill(name)
            # Re-entrant: we already hold this daemon's lock, and the launch
            # command must come from the same place as every other start.
            ok = spawn_daemon(name, repo_root=repo_root, mux=mux, log=log)
            if not ok:
                log(f"[daemon-watch] restart {name}: spawn reported no start")
            return ok
        finally:
            # Even on failure: leaving the marker behind would silently disable
            # mutual watch for this daemon, and the stale-pause report is a
            # 30-minute detour compared with just not leaking it.
            resume(registry_dir, name, token=token, log=log)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _repo_root_default() -> Path:
    return _SCRIPTS_DIR.parent


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="lib_daemon_watch.py",
                                     description="crewvia daemon mutual watch")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _sub(cmd: str, help_text: str, *, with_name: bool = True):
        # --repo-root lives on each subcommand rather than on the top-level
        # parser so it can be written AFTER the verb, which is the order every
        # caller (start.sh included) naturally reaches for.
        sp = sub.add_parser(cmd, help=help_text)
        if with_name:
            sp.add_argument("name", choices=DAEMONS)
        sp.add_argument("--repo-root", type=Path, default=_repo_root_default())
        return sp

    _sub("spawn-cmd", "print the launch command for a daemon")
    _sub("spawn", "start a daemon under its lock (the launcher's path)")

    p = _sub("beat", "write a heartbeat for a daemon")
    p.add_argument("--pid", type=int, default=None)
    p.add_argument("--generation", default=None)

    _sub("watch", "run one mutual-watch cycle as <name>")
    _sub("status", "show both daemons' heartbeats", with_name=False)

    p = _sub("pause", "suppress respawn of a daemon")
    p.add_argument("--reason", default="manual pause")

    p = _sub("resume", "lift a pause marker")
    p.add_argument("--token", default=None)
    p.add_argument("--force", action="store_true")

    p = _sub("restart", "pause \u2192 kill \u2192 spawn \u2192 resume")
    p.add_argument("--force", action="store_true",
                   help="restart even when the pane's owner cannot be shown "
                        "to be this checkout")

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    registry_dir = repo_root / "registry"

    if args.cmd == "spawn-cmd":
        print(spawn_command(args.name, repo_root))
        return 0

    if args.cmd == "spawn":
        # Same exit-code contract as `lib_mux.py spawn`, which start.sh used
        # before: 0 = started, 1 = did not (already live there, or refused).
        return 0 if spawn_daemon(args.name, repo_root=repo_root,
                                 log=lambda m: print(m, file=sys.stderr)) else 1

    if args.cmd == "beat":
        DaemonWatch(registry_dir=registry_dir, repo_root=repo_root,
                    self_name=args.name).beat(pid=args.pid,
                                              generation=args.generation)
        return 0

    if args.cmd == "watch":
        watch = DaemonWatch(registry_dir=registry_dir, repo_root=repo_root,
                            self_name=args.name, config=load_config(),
                            log=lambda m: print(m, file=sys.stderr))
        verdict = watch.watch_peer()
        print(f"{verdict.action}: {verdict.reason}")
        return 0

    if args.cmd == "status":
        # Reports the same two signals the watcher itself uses, in the same
        # order: the recorded instance, then an independent /proc scan.  A
        # heartbeat alone is not the answer to "is it running" — that is the
        # whole point of the mechanism — so `running` is shown even when there
        # is no heartbeat at all, and PAUSED is shown either way (a paused
        # daemon that never wrote a heartbeat is precisely the state someone
        # checking this command is most likely chasing).
        for name in DAEMONS:
            parts = [f"{name}:"]
            hb = read_heartbeat(registry_dir, name)
            if hb is None:
                parts.append("no heartbeat")
            else:
                age = time.time() - hb["updated_at"]
                parts.append(
                    f"pid={hb['pid']} generation={hb.get('generation')} "
                    f"age={age:.0f}s recorded_instance_alive="
                    f"{instance_alive(hb.get('pid'), hb.get('generation'))}")
            found = scan_daemon_pids(repo_root, name)
            if found is None:
                parts.append("running=unknown(/proc unreadable)")
            else:
                parts.append(f"running={found or 'no'}")
            marker = read_pause(registry_dir, name)
            if marker is not None:
                parts.append(
                    f"PAUSED(reason={marker.get('reason') or 'n/a'!r} "
                    f"token={marker.get('token')})")
            print(" ".join(parts))
        return 0

    if args.cmd == "pause":
        token = pause(registry_dir, args.name, reason=args.reason)
        if token is None:
            # Nothing on stdout: a caller that captures the token must not be
            # handed an empty string it will then pass to `resume`.
            print(f"refused: could not persist the pause marker for {args.name} "
                  f"({pause_path(registry_dir, args.name)}). "
                  f"Mutual watch is NOT suppressed — do not kill it yet.",
                  file=sys.stderr)
            return 1
        print(token)
        return 0

    if args.cmd == "resume":
        ok = resume(registry_dir, args.name, token=args.token, force=args.force)
        if not ok:
            print(f"refused: the marker on {args.name} was created by another run "
                  f"(pass --force to lift it anyway)", file=sys.stderr)
        return 0 if ok else 1

    if args.cmd == "restart":
        # Refusals explain themselves on stderr (see restart()); the exit code
        # is what a script driving this will actually branch on.
        return 0 if restart(args.name, repo_root=repo_root, force=args.force,
                            log=lambda m: print(m, file=sys.stderr)) else 1

    return 2  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MuxTestIsolationError as exc:
        # The 2026-09-23 shape: a test ran this CLI in a subprocess and the
        # destination came from the ambient environment.  Refused before
        # anything was executed; said in one line, not a traceback.
        print(str(exc), file=sys.stderr)
        sys.exit(MUX_TEST_ISOLATION_EXIT)
