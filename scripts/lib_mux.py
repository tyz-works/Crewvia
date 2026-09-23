#!/usr/bin/env python3
"""Mux backend abstraction — spawn / send / capture / list / kill / pid / attach / attach_cmd / state / verify_sent.

Backends:
  TmuxBackend  — wraps current tmux CLI calls verbatim (Phase 1)
  HerdrBackend — herdr terminal workspace manager (Phase 2)

Backend selection (highest priority first):
  1. CREWVIA_MUX env  ("tmux" | "herdr")
  2. config/crewvia.yaml  `mode: tmux|herdr|inline`
  3. Auto: use tmux if `tmux` is in PATH

Usage as module:
  from lib_mux import Mux, repo_identity_ok
  m = Mux()
  m.spawn("Omar-worker", "claude ...", cwd="/path/to/repo")
  m.send("Omar-worker", "タスクなし、shutdown")
  screen = m.capture("Omar-worker")
  workers = m.list(suffix="-worker")
  m.kill("Omar-worker")
  pid = m.pid("Omar-worker")
  m.attach("Sora-director")
  cmd = m.attach_cmd("Sora-director")  # list[str] | None
  ok = m.available()
  st = m.state("Omar-worker")  # "blocked"|"working"|"idle"|"done"|"unknown"
  landed = m.verify_sent("Omar-worker", kickoff_text)  # False = still stuck in input line, retry send()
  safe = repo_identity_ok("/path/to/repo")  # False = refuse to act (deleted worktree etc.)

CLI usage (for bash callers):
  python3 lib_mux.py available            # exit 0 = available (starts herdr server if needed)
  python3 lib_mux.py server-running       # exit 0 = mux server already up (never starts it)
  python3 lib_mux.py spawn <name> <cmd> [<cwd>]
  python3 lib_mux.py send  <name> <text>
  python3 lib_mux.py capture <name>       # prints raw screen text
  python3 lib_mux.py list [<suffix>]      # one name per line
  python3 lib_mux.py kill <name>
  python3 lib_mux.py pid  <name>          # prints integer PID
  python3 lib_mux.py attach <name>
  python3 lib_mux.py attach-cmd <name>    # prints argv one arg per line, empty = use attach()
  python3 lib_mux.py state <name>         # prints agent state string
  python3 lib_mux.py verify-sent <name> <text>  # exit 0 = text left the input line (landed)
  python3 lib_mux.py identity-ok <repo_root>    # exit 0 = repo_root still a valid git checkout
"""

import errno
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SESSION = "crewvia"


def _session() -> str:
    """Return the tmux session name (CREWVIA_TMUX_SESSION overrides default)."""
    return os.environ.get("CREWVIA_TMUX_SESSION", _DEFAULT_SESSION)


# ---------------------------------------------------------------------------
# Test isolation — production is not reachable from a test run
# ---------------------------------------------------------------------------
#
# On 2026-09-23 a test took production's dispatcher pane away for four and a
# half hours.  The test ran the real CLI in a real subprocess to check that it
# refused something; the CLI builds its `Mux()` **from the ambient
# environment**, so while `--repo-root` pointed at a pytest tmpdir, the kill
# and the spawn went to the default workspace `crewvia` and the default pane
# name `dispatcher` — production.  The pane was replaced with a command
# rooted in the tmpdir, and died when pytest deleted it.
#
# The lesson is not "fix that test".  A red test proves a defect by *running
# the defective destructive path*; that is its job.  So isolation cannot be
# left to the test author's memory.  Two environment variables are required
# of every test run, and without them the destructive verbs refuse:
#
#   CREWVIA_MUX_TEST_ISOLATION   the marker; set by tests/conftest.py into
#                                os.environ, so subprocesses inherit it (the
#                                one thing the incident was missing)
#   CREWVIA_MUX_PANE_PREFIX      a namespace for pane names.  Production
#                                leaves it empty and this whole layer is then
#                                the identity function; a test run sets it, so
#                                `spawn("dispatcher")` lands on
#                                `<prefix>dispatcher` and the production pane
#                                name is not something a test can even say.
#
# plus a destination (tmux session / herdr workspace) that is not the default.
#
# Deliberately *not* the only defence: lib_daemon_watch also refuses to kill a
# pane whose process belongs to another checkout, which holds in production
# too, where no marker is set.  A single guard keyed on "are we testing" would
# be exactly the fail-closed-in-one-place shape that reproduced this repo's
# defects three times over.

#: Raised instead of returning False: a test that aims at production must fail
#: loudly, not degrade into a no-op it could mistake for a passing assertion.
#: The `_Backend` "never raise" contract holds everywhere else — this is only
#: reachable when CREWVIA_MUX_TEST_ISOLATION is set, i.e. never in production.
class MuxTestIsolationError(RuntimeError):
    pass


#: Exit code the CLIs use for this refusal — distinct from the verbs' own
#: 0/1/2 so a harness can tell "aimed at production" from "did not start".
MUX_TEST_ISOLATION_EXIT = 3


def _test_isolation_active() -> bool:
    """True while a test run owns this process (or its parent's environment).

    `PYTEST_CURRENT_TEST` is pytest's own per-test variable and is a backstop;
    `CREWVIA_MUX_TEST_ISOLATION` is what conftest sets for the whole session,
    which is what actually reaches a subprocess started between two tests.
    """
    return bool(os.environ.get("CREWVIA_MUX_TEST_ISOLATION")
                or os.environ.get("PYTEST_CURRENT_TEST"))


def _pane_prefix() -> str:
    """The pane-name namespace.  Empty in production — this layer is a no-op."""
    return os.environ.get("CREWVIA_MUX_PANE_PREFIX", "")


def _pane_name(name: str) -> str:
    """The backend-level name for the caller's `name`."""
    return f"{_pane_prefix()}{name}"


def _caller_name(pane_name: str) -> str:
    """Inverse of `_pane_name()` — what `list()` hands back to callers.

    The namespace is mux-internal.  Leaking it outwards would silently break
    every caller that matches on a name it did not create: dispatcher's
    `-worker` suffix scan, watchdog's per-agent heartbeat lookup.
    """
    prefix = _pane_prefix()
    if prefix and pane_name.startswith(prefix):
        return pane_name[len(prefix):]
    return pane_name


def _guard_test_isolation(backend: str, verb: str, name: str,
                          destination: str) -> None:
    """Refuse a verb that a test run aimed at a production pane.

    Checked *before* anything is executed, so a refusal also means nothing
    happened — which is the assertion the regression test actually makes.

    Reads (`pid`, `capture`) are guarded as well as writes.  They are the step
    that *authorises* the write — `restart()` asks for the pane's pid to decide
    whether it may kill it — so leaving them open would mean a test could still
    take production's measurements and then act on them.  The rule is simply
    that a test cannot **name** a production pane.  `list()` names none and
    stays open; its results are namespaced by `_caller_name()` instead.
    """
    if not _test_isolation_active():
        return
    problems = []
    if destination == _DEFAULT_SESSION:
        problems.append(
            f"the destination is the production default {_DEFAULT_SESSION!r} "
            f"(set CREWVIA_TMUX_SESSION / CREWVIA_HERDR_WORKSPACE to a name of "
            f"this test's own)")
    if not _pane_prefix():
        problems.append(
            "CREWVIA_MUX_PANE_PREFIX is empty, so this would claim the bare "
            "pane name production uses")
    if not problems:
        return
    raise MuxTestIsolationError(
        f"[mux:{backend}] refusing {verb}({name!r}) under test isolation: "
        + "; ".join(problems)
        + ". Nothing was executed. See tests/conftest.py — a test run must not "
          "be able to reach the production mux (2026-09-23 incident)."
    )


def repo_identity_ok(repo_root) -> bool:
    """Self-identity guard for long-running daemons (watchdog.py, dispatcher.sh).

    Both daemons resolve their own repo_root once (from their own script
    path, or from an explicit --repo-root / CREWVIA_QUEUE override) and then
    trust it for the rest of their life — including when deciding whether
    it's safe to kill a Worker pane. herdr additionally snapshots the
    environment of whichever shell first started its server and replays it
    onto every pane spawned afterwards (see herdr-server-stale-env-inheritance
    knowledge doc), so a daemon launched from a throwaway git worktree (e.g.
    for isolated dispatcher/watchdog testing) can end up operating against
    the *production* mux workspace while its own repo_root still points at
    that worktree. If the worktree is later removed (`git worktree remove`,
    mission cleanup, ...), the daemon process itself does not die — it just
    has no valid checkout left to claim ownership from, and must refuse to
    act rather than keep killing panes in whatever workspace it inherited.

    Returns True only if repo_root still exists on disk and is still a git
    working tree (has a `.git` entry — file for a linked worktree, directory
    for a normal checkout). Anything else (deleted, recreated as an empty
    directory, a permission error) returns False so callers can fail closed:
    skip the action (do not kill) rather than proceed with a repo_root they
    can no longer prove is real. Deliberately conservative — a false "not
    ok" only costs a skipped cycle; a false "ok" can kill a live Worker.
    """
    try:
        root = Path(repo_root)
        return root.is_dir() and (root / ".git").exists()
    except OSError:
        return False


def _config_mode(config_path: Optional[Path] = None) -> Optional[str]:
    """Read `mode:` key from config/crewvia.yaml relative to this script's repo root.

    `config_path` overrides the file read — only ever passed by tests; every
    real caller uses the default (this script's own repo root).

    Returns "tmux", "herdr", "inline", or None if not found / unreadable.
    """
    if config_path is None:
        script_dir = Path(__file__).parent
        config_path = script_dir.parent / "config" / "crewvia.yaml"
    try:
        text = config_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("mode:") and not stripped.startswith("#"):
                value = stripped.split(":", 1)[1]
                # Drop a trailing inline comment (`mode: herdr  # switched back
                # on 2026-09-04`) before matching against the allowed values.
                # Without this, a value like "herdr  # ..." never equals
                # "herdr", _config_mode() silently returns None, and every
                # process with no CREWVIA_MUX env falls back to TmuxBackend
                # even though config says herdr (t016: this is what made the
                # watchdog blind — mux.list() came back empty against a dead
                # tmux session, so every live Worker looked "window gone").
                value = value.split("#", 1)[0].strip().strip('"').strip("'")
                if value in ("tmux", "herdr", "inline"):
                    return value
    except Exception:
        pass
    return None


def _select_backend() -> "type":
    """Choose backend class based on env > config > auto."""
    env_mux = os.environ.get("CREWVIA_MUX", "").lower()
    if env_mux == "herdr":
        return HerdrBackend
    if env_mux == "tmux":
        return TmuxBackend

    config = _config_mode()
    if config == "herdr":
        return HerdrBackend
    if config == "tmux":
        return TmuxBackend
    # config == "inline" or unknown → auto
    if shutil.which("tmux"):
        return TmuxBackend
    return TmuxBackend  # fallback; available() will return False


# ---------------------------------------------------------------------------
# Backend base (interface contract)
# ---------------------------------------------------------------------------

class _Backend:
    """Abstract mux backend.

    All verbs must:
      - Never raise exceptions to the caller.
      - Return False / None / "" on failure.
      - Print "[mux:<backend>] WARNING: ..." to stderr on failure.
    """

    BACKEND_NAME = "base"

    def _inspect_pane(self, name: str):
        """`(handle, pane_pid)` for `name`, both from **one** query.

        `handle` is the backend's own immutable identity for the thing a kill
        would destroy (a tmux `@window_id`, a herdr `tab_id`) — not the caller
        name, which is mutable and which another checkout can make point
        somewhere else between the two calls.  `(None, None)` when the pane
        could not be resolved; the allowlist turns that into a refusal.
        """
        raise NotImplementedError

    def server_identity(self):
        """`(endpoint, generation)` for the mux server, or None.

        `endpoint` distinguishes two servers running side by side (a tmux
        socket path, herdr's API socket); `generation` distinguishes one
        server's lifetime from the next one's at the same endpoint.  A pane
        id is only meaningful inside one `(endpoint, generation)`, so a spawn
        record that does not carry this is a claim about an id that the next
        server is free to hand to somebody else (Codex 6巡目 P1-3).

        None means "could not be established", which `pane_record_status()`
        reads as a refusal rather than as a match — with `--force` as the way
        past it.
        """
        return None

    def _inspect_pane_full(self, name: str):
        """`(handle, pane_pid, server)` — `_inspect_pane()` plus the server.

        Overridden where the backend can answer all three from one query, so
        that the pane judged and the pane destroyed are the same pane on the
        same server.  The default asks separately, which is still correct:
        a server that changed between the two answers cannot match the record
        either way.
        """
        handle, pane_pid = self._inspect_pane(name)
        return handle, pane_pid, self.server_identity()

    def _refuses_foreign_daemon(self, name: str, handle, pane_pid,
                                server=None) -> bool:
        """True unless `name`'s pane is provably this checkout's to destroy.

        Delegates the whole judgment to `may_destroy_pane()` — one decision
        unit, so that there is no second table to drift out of step with it.

        `handle` and `pane_pid` are both passed in rather than fetched, and
        both come out of the *same* backend query, so the pane this judges is
        the pane the caller goes on to destroy.

        Shared by both backends so the two cannot diverge — the shape in which
        "the peer is dead but its tab is still listed" became un-actionable on
        tmux only (t006 QA FAIL-1) started as exactly that kind of
        per-backend divergence.
        """
        allowed, reason = may_destroy_pane(
            name, self.BACKEND_NAME, handle, pane_pid, server=server)
        if allowed:
            return False
        self._warn(
            f"kill {name!r}: refused — {reason}. Nothing was killed. This "
            f"guard reads what this checkout recorded when it created the "
            f"pane, not the environment, so a clean env does not lift it; use "
            f"`lib_daemon_watch.py restart {name} --force` when you mean to "
            f"take the pane over."
        )
        return True

    def _warn_forced_bypass(self, name: str) -> None:
        """Say out loud that the guard was skipped.

        An exit nobody can see is how a safety decision becomes a ceremony:
        the refusal gets worked around once, the workaround becomes the
        recipe, and the next incident looks exactly like the last one.
        """
        if name in DAEMON_PANE_NAMES:
            self._warn(
                f"kill {name!r}: --force — the identity guard was NOT "
                f"consulted and this checkout's spawn record was not checked. "
                f"Destroying the pane under that name whatever is in it.")

    def may_destroy(self, name: str):
        """`(allowed, reason)` for `name`'s pane, as `kill()` would judge it.

        For callers that want to stop *before* doing anything destructive —
        `lib_daemon_watch.restart()` writes a pause marker first and a refusal
        afterwards would leave it behind.  Advisory only: it inspects the pane
        a second time, so the answer that governs is still the one `kill()`
        takes from its own inspection.
        """
        handle, pane_pid, server = self._inspect_pane_full(name)
        return may_destroy_pane(name, self.BACKEND_NAME, handle, pane_pid,
                                server=server)

    def _warn(self, msg: str) -> None:
        print(f"[mux:{self.BACKEND_NAME}] WARNING: {msg}", file=sys.stderr)

    def spawn(self, name: str, cmd: str, cwd: Optional[str] = None,
              env: Optional[dict] = None) -> bool:
        raise NotImplementedError

    def send(self, name: str, text: str) -> bool:
        raise NotImplementedError

    def capture(self, name: str) -> str:
        raise NotImplementedError

    def list(self, suffix: Optional[str] = None) -> List[str]:
        raise NotImplementedError

    def kill(self, name: str, *, allow_foreign: bool = False) -> bool:
        raise NotImplementedError

    def pid(self, name: str) -> Optional[int]:
        raise NotImplementedError

    def attach(self, name: str) -> bool:
        raise NotImplementedError

    def attach_cmd(self, name: str) -> Optional[List[str]]:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError

    def server_running(self) -> bool:
        raise NotImplementedError

    def state(self, name: str) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Pane liveness — is anything actually running in there?
# ---------------------------------------------------------------------------
#
# Both backends put a *shell* in the pane and type the command into it, so the
# daemon (or the agent) is the shell's child.  When it dies the shell survives
# and the pane keeps its label: a husk.  A pane's name therefore says nothing
# about whether anything is running in it, and code that reads the name as
# life will hold forever on the ordinary shape of a crash (t006 QA FAIL-1).
#
# herdr answers this with `pane process-info`.  tmux has no equivalent, so the
# answer is read from /proc starting at the pane's shell pid — which is the
# stronger source anyway, since the kernel answers without the backend.

# Process names that can be a pane's *idle* shell.  A leading '-' (login shell,
# e.g. "-bash") is stripped before the lookup.  The name alone is never enough:
# `bash scripts/dispatcher.sh` also reports name "bash", so something else has
# to separate an idle shell from a shell running a script — argv length for
# herdr (_is_idle_shell_process), the absence of children for tmux
# (_pane_shell_is_idle).
_SHELL_PROCESS_NAMES = frozenset({
    "bash", "sh", "zsh", "fish", "dash", "ksh", "tcsh", "csh",
})


#: Reading `/proc/<pid>/<file>`: the answer, and whether it is one at all.
_PROC_OK, _PROC_GONE, _PROC_UNREADABLE = "ok", "gone", "unreadable"

#: The only errnos that mean the process is not there.  Anything else — EACCES
#: under `hidepid`, EIO, a path that is not what we expected — means we could
#: not look, and "could not look" is not "nothing there".  lib_daemon_watch
#: keeps its own copy of this constant: it imports this module, so this module
#: cannot import it back.
_PROC_GONE_ERRNOS = frozenset({errno.ENOENT, errno.ESRCH})


def _read_proc(pid, filename: str, proc_root: str = "/proc"):
    """`(status, text)` for `/proc/<pid>/<filename>`.

    Callers branch on the status: `_PROC_GONE` is a real answer (the process
    ended), `_PROC_UNREADABLE` is the absence of one.
    """
    try:
        path = Path(proc_root, str(int(pid)), filename)
    except (TypeError, ValueError):
        return _PROC_UNREADABLE, ""
    try:
        return _PROC_OK, path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        if exc.errno in _PROC_GONE_ERRNOS:
            return _PROC_GONE, ""
        return _PROC_UNREADABLE, ""


def _proc_stat_fields(pid, proc_root: str = "/proc"):
    """`(status, fields)` where fields are `/proc/<pid>/stat` from field 3 on.

    comm (field 2) is parenthesised and may itself contain spaces and ')', so
    the split is after the LAST ')'.  Same parse as
    lib_daemon_watch.process_generation() — kept here rather than imported to
    avoid a cycle (lib_daemon_watch imports this module).

    Indices into `fields`: 0 state, 1 ppid, 2 pgrp, 3 session, 4 tty_nr,
    5 tpgid, … 19 starttime.
    """
    status, stat = _read_proc(pid, "stat", proc_root)
    if status != _PROC_OK:
        return status, []
    try:
        return _PROC_OK, stat[stat.rindex(")") + 2:].split()
    except ValueError:
        return _PROC_UNREADABLE, []      # not a stat line we understand


def _proc_stat_tail(pid, proc_root: str = "/proc") -> Optional[List[str]]:
    """`_proc_stat_fields()` collapsed to the old Optional shape."""
    status, fields = _proc_stat_fields(pid, proc_root)
    return fields if status == _PROC_OK else None


def _process_comm(pid, proc_root: str = "/proc") -> Optional[str]:
    """The process name between the parentheses of `/proc/<pid>/stat`."""
    status, stat = _read_proc(pid, "stat", proc_root)
    if status != _PROC_OK:
        return None
    try:
        return stat[stat.index("(") + 1:stat.rindex(")")]
    except ValueError:
        return None


def _process_argv(pid, proc_root: str = "/proc") -> Optional[List[str]]:
    """`/proc/<pid>/cmdline` as a list, or None when it cannot be read."""
    status, raw = _read_proc(pid, "cmdline", proc_root)
    if status != _PROC_OK:
        return None
    return [arg for arg in raw.split("\0") if arg]


def _live_children(pid, proc_root: str = "/proc") -> Optional[List[int]]:
    """Live, non-zombie children of `pid`; None when the walk was incomplete.

    `None` and `[]` are different answers and callers must keep them apart:
    "could not look" is not "nothing there" — the distinction this repo has
    now had to make at three separate layers.

    The distinction is per entry.  An entry that vanished mid-walk was never a
    live child, but one we could not *read* might be, and a list that quietly
    omitted it would be read as "this pane is empty" by the one caller that
    then launches something into it.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return None
    found: List[int] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        status, tail = _proc_stat_fields(entry.name, proc_root)
        if status == _PROC_GONE:
            continue          # exited between listing and read — not a child
        if status != _PROC_OK or len(tail) < 2:
            return None       # could not read it — this walk proves nothing
        if tail[0] == "Z":
            continue          # a zombie has already stopped running
        try:
            if int(tail[1]) == pid:
                found.append(int(entry.name))
        except ValueError:
            return None
    return sorted(found)


#: How long spawn() waits for a reused pane to actually be running something.
#: Only the husk path pays it, and only when the launch fails — a daemon that
#: starts is seen on the first or second poll.  Overridable for slow machines
#: (and for tests that deliberately let it expire) via
#: CREWVIA_MUX_LAUNCH_VERIFY_SECONDS.
_LAUNCH_VERIFY_SECONDS = 10.0
_LAUNCH_VERIFY_POLL = 0.25


def _launch_verify_seconds() -> float:
    try:
        value = float(os.environ.get("CREWVIA_MUX_LAUNCH_VERIFY_SECONDS", ""))
    except ValueError:
        return _LAUNCH_VERIFY_SECONDS
    return value if value > 0 else _LAUNCH_VERIFY_SECONDS


def _wait_until_launched(pane_state, *, warn, name: str) -> bool:
    """True once `pane_state()` **observes** something running; False otherwise.

    Relaunching into an existing pane means *typing into a shell*, and a shell
    accepts anything.  If it was in fact still busy — the one case /proc cannot
    rule out, an interactive `read` — the launch command is swallowed as input
    and nothing starts, while every caller above is told the daemon is back.
    The peer would record a respawn, spend its grace period and a flap slot,
    and watch the same corpse.  So the pane is asked afterwards, and the answer
    to "did it start" is the pane's, not the send's.

    `pane_state()` must return the three-valued `PANE_*`, not a bool.  The
    boolean version of this question was answered by `_pane_has_live_process()`,
    which returns True for *anything it cannot read* — correct for "may I
    launch in here", and exactly backwards for "did my launch work".  Fed that
    predicate, this loop reported success the moment the backend stopped
    answering: the command is swallowed, the pane query then fails, and the
    spawn declares a recovery it never saw.  One predicate cannot fail safe in
    two directions, so `PANE_UNKNOWN` is neither — it keeps the loop waiting
    and, at the deadline, fails.
    """
    limit = _launch_verify_seconds()
    deadline = time.time() + limit
    last = PANE_UNKNOWN
    while True:
        last = pane_state()
        if last == PANE_LIVE:
            return True
        if time.time() >= deadline:
            if last == PANE_UNKNOWN:
                warn(f"spawn {name!r}: the pane could not be read in the "
                     f"{limit:.0f}s after the launch command — 'could not look' "
                     "is not 'it started', so the spawn is treated as failed")
            else:
                warn(f"spawn {name!r}: nothing is running in the pane "
                     f"{limit:.0f}s after the launch command — treating the spawn as "
                     "failed (the pane may have taken it as input)")
            return False
        time.sleep(_LAUNCH_VERIFY_POLL)


#: What is in a pane.  `UNKNOWN` is a third answer, not a shade of the other
#: two: "may I launch here" must read it as busy, "did my launch work" must
#: read it as no.  Collapsing it into either makes one of those wrong.
PANE_IDLE, PANE_LIVE, PANE_UNKNOWN = "idle", "live", "unknown"


def _pane_shell_state(pane_pid, proc_root: str = "/proc") -> str:
    """`PANE_IDLE` / `PANE_LIVE` / `PANE_UNKNOWN` for a pane's shell pid.

    The three-valued form of `_pane_shell_is_idle()` below, which is now a
    wrapper over it and answers exactly what it always did (`is idle` ⇔
    `state is PANE_IDLE`).  Every check that used to return "not idle" is split
    here into the two reasons it could have: something is demonstrably running
    (`LIVE`), or the check could not be made (`UNKNOWN`).
    """
    status, fields = _proc_stat_fields(pane_pid, proc_root)
    if status != _PROC_OK or len(fields) < 6:
        return PANE_UNKNOWN

    comm = _process_comm(pane_pid, proc_root)
    if comm is None:
        return PANE_UNKNOWN
    if comm.lstrip("-") not in _SHELL_PROCESS_NAMES:
        return PANE_LIVE          # the pane is rooted at something that is not
                                  # a shell — that something is running

    argv = _process_argv(pane_pid, proc_root)
    if argv is None:
        return PANE_UNKNOWN
    if len(argv) != 1:
        return PANE_LIVE          # `bash script.sh`, `bash -c …`

    if fields[0] != "S":
        return PANE_LIVE          # R burns CPU; T/D is not sitting at a prompt

    try:
        pgrp, tty_nr, tpgid = int(fields[2]), int(fields[4]), int(fields[5])
    except ValueError:
        return PANE_UNKNOWN
    if tty_nr == 0 or tpgid <= 0:
        return PANE_UNKNOWN       # not a pane shell as we understand one
    if tpgid != pgrp:
        return PANE_LIVE          # the terminal's foreground is a child

    children = _live_children(pane_pid, proc_root)
    if children is None:
        return PANE_UNKNOWN
    return PANE_LIVE if children else PANE_IDLE


def _pane_shell_is_idle(pane_pid, proc_root: str = "/proc") -> bool:
    """True only when `pane_pid` is provably an interactive shell at a prompt.

    Being a shell with no children is **not** that.  All of these are shells,
    all of them have no children, and all of them are working:

        bash script.sh          a script whose work is builtins only
        bash -c 'while :; …'    same, one level shorter
        bash < pipe             reading its input from somewhere that is not
                                a terminal

    Sending a launch command into any of them does not start a daemon: at best
    it is ignored, at worst it is read as *data* by whatever is running.  So
    idleness has to be shown, not assumed, and every check below is one of the
    ways these differ from a pane sitting at its prompt:

      1. the process is a **shell** (a pane rooted at something else was not
         left behind by a dead child);
      2. it was invoked as a **bare** shell — argv is just the shell itself,
         which no `bash script.sh` and no `bash -c` can be;
      3. it has a **controlling terminal**, and
      4. **its own process group is that terminal's foreground** — together,
         "this shell is the thing the pane is talking to";
      5. it is **sleeping**, not running — a builtin loop burns CPU in R;
      6. it has **no live children**, counted completely (backgrounded jobs
         included).

    Every unreadable answer returns False (= busy).  The direction to fail in
    is "do not start a second one" (knowledge/daemon-authority.md §7-1).

    What this still cannot see: a shell blocked in an interactive `read` looks
    identical to one blocked at its prompt — same state, same argv, same
    foreground group — because at that level it *is* the same thing.  That gap
    is closed one layer up, by spawn() verifying that something actually
    started rather than trusting that the command was accepted.
    """
    return _pane_shell_state(pane_pid, proc_root) == PANE_IDLE


# ---------------------------------------------------------------------------
# Pane identity — whose checkout is running in there?
# ---------------------------------------------------------------------------
#
# `_guard_test_isolation()` above is a guard on **the caller**: it asks whether
# the process making the call is a test run.  That is a property of the
# environment, and an environment is something a caller can simply not have —
# start a subprocess with a clean `env` and the guard is gone, back to the
# production defaults (Codex 3 巡目 P1-1, demonstrated against this very
# module: `TmuxBackend().kill("dispatcher")` issued
# `tmux kill-window -t crewvia:dispatcher`).
#
# So the real wall cannot be there.  It has to be a property of **the target**:
# whose daemon is in the pane we are about to destroy?  That question is
# answered from /proc, needs nobody to have declared anything, and holds
# between two production checkouts exactly as it holds for a test run — which
# is what the 2026-09-23 incident actually needed.
#
# Two layers, deliberately shaped differently and standing on separate
# evidence (memory: fail-closed-guard-can-recreate-the-defect):
#
#   * here, at the mux layer: a **backstop** that refuses only what it can
#     positively prove is another checkout's daemon.  It must stay a narrow
#     denial, because every Worker pane also comes through `kill()` and a
#     Worker runs arbitrary commands — an allowlist here would mean watchdog
#     could never end a Worker again.
#   * at the daemon layer (`lib_daemon_watch.pane_daemon_owner`): an
#     **allowlist** — only `mine` and `none` may be killed, and everything it
#     cannot judge is refused.  There the set of legitimate occupants is known,
#     so the allowlist is the right shape (memory:
#     approve-judgment-needs-allowlist-and-scope).
#
# Neither layer replaces the env-based isolation; it stays as the convenience
# layer that keeps a test from *naming* a production pane in the first place.

#: "the caller did not supply this", kept apart from `None`, which is a real
#: answer meaning "the pane has no readable pid".
_UNSET = object()

#: `/proc`, as a module-level name so tests can point the identity layer at a
#: fixture without threading a parameter through every backend verb.  Never
#: read from the environment: this guard exists precisely because the
#: environment is what a caller can drop.
_PROC_ROOT = "/proc"


def _own_repo_root() -> Path:
    """The checkout this module is running out of.

    From `__file__`, not from `CREWVIA_REPO_ROOT` — the whole point is an
    identity the caller cannot restate.  A worktree gets its own answer, which
    is correct: a daemon started from a worktree owns that worktree's panes.
    """
    return Path(__file__).resolve().parent.parent


#: The scripts whose presence in a pane binds it to a checkout.  Kept as a
#: literal because `lib_daemon_watch` imports *this* module and so cannot be
#: imported back; `tests/test_pane_identity_guard.py` asserts the two stay in
#: step, because a daemon added to one and not the other would fall silently
#: outside the guard.
DAEMON_SCRIPTS = ("dispatcher.sh", "watchdog.py")

#: The pane names this backstop applies to, and **only** these.
#:
#: Scope is the safety property here, not an optimisation.  Widening it to
#: every pane would mean a Worker that happens to run `bash scripts/…` in its
#: own pane — a QA Worker exercising the dispatcher, which this repo does —
#: could never be retired again, because what it is running belongs to the
#: worktree it was given rather than to watchdog's checkout.  The incident
#: this guards is a *daemon pane* being taken over; keeping the judgment to
#: one unit is what stopped the earlier versions of it from turning into a
#: refusal nobody can clear (memory: approve-judgment-needs-allowlist-and-scope).
DAEMON_PANE_NAMES = ("dispatcher", "watchdog")

#: Whose daemon is in the pane.  `UNKNOWN` is not `NONE`: "there is nothing
#: recognisable in there" authorises a relaunch, "we could not tell" must not.
OWNER_MINE, OWNER_FOREIGN = "mine", "foreign"
OWNER_NONE, OWNER_UNKNOWN = "none", "unknown"

#: Most serious answer first.  A pane holding both somebody else's daemon and
#: ours is not ours to end, so `FOREIGN` outranks `MINE` rather than the
#: reverse — the first reading of the pane that forbids the kill wins.
_OWNER_PRECEDENCE = (OWNER_FOREIGN, OWNER_UNKNOWN, OWNER_MINE, OWNER_NONE)

#: argv[0] values after which the *script* is a later argument rather than
#: argv[0] itself.  Anything not in here is taken to be the executable, so an
#: unrecognised wrapper (`env FOO=1 bash …`) does not resolve to a script and
#: falls into the ambiguous branch below rather than the empty one.
_INTERPRETER_NAMES = frozenset({
    "bash", "sh", "zsh", "fish", "dash", "ksh", "tcsh", "csh",
    "python", "python3", "python2", "perl", "ruby",
})

#: The same list with the version suffix taken off.  Interpreters are installed
#: under their version at least as often as under their bare name — `python3.12`
#: is what a venv's shebang and a distro's `update-alternatives` both hand you —
#: and `python3.12` is not in the set above.  Missing it meant `argv[0]` was
#: taken to be the program, so the *actual* script argument was never resolved:
#: `python3.12 /theirs/monitor` answered "no daemon here", which is the verdict
#: that authorises destroying the pane (Codex 5巡目 P1-2).
_INTERPRETER_BASE_NAMES = frozenset({
    "bash", "sh", "zsh", "fish", "dash", "ksh", "tcsh", "csh",
    "python", "perl", "ruby", "php", "node",
})

#: `python3.12` → `python`, `perl5.36` → `perl`, `bash` → `bash`.  Deliberately
#: refuses to match anything with a non-numeric suffix, so `watchdog.py` and
#: `dispatcher.sh` — the two names this module must never mistake for a
#: wrapper — come back unchanged.
_VERSIONED_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z_-]*?)[0-9]*(?:\.[0-9]+)*$")


def _is_interpreter(head: str) -> bool:
    """Whether `head` (a basename) runs a *later* argument as its program."""
    if head in _INTERPRETER_NAMES:
        return True
    match = _VERSIONED_NAME_RE.match(head)
    return bool(match) and match.group(1) in _INTERPRETER_BASE_NAMES

#: Interpreter flags after which no script path follows — the next word is a
#: program text (`-c`) or a module name (`-m`).  Treated as "there is no
#: script here" rather than "the next word is one".
_INTERPRETER_NO_SCRIPT_FLAGS = frozenset({"-c", "--command", "-m"})

#: readlink() on a working directory that has been removed answers with this
#: suffix appended.  The path still identifies the checkout it came from,
#: which is the only thing asked of it here.
_PROC_DELETED_SUFFIX = " (deleted)"

#: How many argv entries the mention scan resolves.  A bound, not a judgment:
#: the scan touches the filesystem per entry, and a command line long enough to
#: matter (`grep -r … <10k paths>`) is not a daemon launch.
_MAX_ARGV_SCAN = 64


def _executed_script_arg(argv) -> Optional[str]:
    """The script `argv` is *running*, or None when we cannot point at one.

    The distinction this function exists to make: `bash scripts/dispatcher.sh`
    runs a script, `tail -f …/scripts/dispatcher.sh` mentions one.  Scanning
    argv for anything that looks like the path answers `mine` to the second —
    which, at the caller, reads as "ours to kill".
    """
    if not argv:
        return None
    head = os.path.basename(argv[0]).lstrip("-")
    if not _is_interpreter(head):
        return argv[0]
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in _INTERPRETER_NO_SCRIPT_FLAGS:
            return None
        if arg.startswith("-") and arg != "-":
            index += 1          # a flag; its value (if any) is handled below
            continue
        return arg
    return None


def _proc_cwd(pid, proc_root: str):
    """`(status, path)` for `/proc/<pid>/cwd`.

    Three-valued like `_read_proc()`: a process that has exited is a real
    answer, a directory we may not read is the absence of one.
    """
    try:
        link = Path(proc_root, str(int(pid)), "cwd")
    except (TypeError, ValueError):
        return _PROC_UNREADABLE, ""
    try:
        target = os.readlink(link)
    except OSError as exc:
        if exc.errno in _PROC_GONE_ERRNOS and not link.parent.exists():
            # The whole `/proc/<pid>` is gone: the process exited between the
            # table walk and this read, so it is not in the pane.  ENOENT on
            # `cwd` while `/proc/<pid>` is still there is a different thing
            # entirely — the process is alive and we could not look — and
            # reading the two as one answer is how "could not look" becomes
            # "nothing there" for the fourth time in this repo.
            return _PROC_GONE, ""
        return _PROC_UNREADABLE, ""
    if target.endswith(_PROC_DELETED_SUFFIX):
        target = target[:-len(_PROC_DELETED_SUFFIX)]
    return _PROC_OK, target


#: Outcomes of `_resolve_for_ownership()`.  `UNDECIDED` is not an error — it is
#: the answer "this path cannot be turned into a file name without guessing",
#: and guessing is what turned another checkout's daemon into ours.
_RESOLVE_OK, _RESOLVE_UNDECIDED = "ok", "undecided"

#: Symlink hops before we call it a loop.  Same order as the kernel's ELOOP.
_MAX_SYMLINK_HOPS = 40


def _resolve_for_ownership(path: str, _hops: int = 0):
    """`(status, resolved)` — `path` with its symlinks followed, or UNDECIDED.

    `os.path.realpath()` is *non-strict*: when a component on the way is
    missing it stops asking the filesystem and finishes the job lexically,
    which is fine for a path made only of names and catastrophic for one that
    contains `..`.  The shape Codex 5巡目 P1-3 demonstrated:

        at launch   /ours/link         → /theirs/subdir
        so          /ours/link/../scripts/watchdog.py   *runs*
                    /theirs/scripts/watchdog.py
        later       /ours/link is removed
        realpath()  folds `..` on the spelling and answers
                    /ours/scripts/watchdog.py

    — i.e. after the link is gone, a *foreign* daemon's command line resolves
    to our own script, and `MINE` is the verdict that authorises killing it.

    So `..` is only followed when the directory we would be climbing out of
    really exists.  When it does not, there is no fact of the matter about
    where the path pointed, and the answer is UNDECIDED rather than a guess.

    Everything else stays non-strict on purpose: `/gone/worktree/scripts/
    watchdog.py`, with no `..` in it, still names the checkout it came from
    after that checkout is deleted, and the retirement paths need that.

    `ValueError` as well as `OSError`: a path out of `/proc` can carry bytes
    the filesystem calls will not take, and this runs underneath `kill()`,
    which owes its callers a return value rather than an exception.
    """
    if not path or _hops > _MAX_SYMLINK_HOPS:
        return _RESOLVE_UNDECIDED, ""
    if not os.path.isabs(path):
        # Callers resolve against the process's own cwd before getting here;
        # a relative path this far in is one nobody could anchor.
        return _RESOLVE_UNDECIDED, ""
    try:
        current = os.sep
        for component in path.split(os.sep):
            if component in ("", "."):
                continue
            if component == "..":
                if not os.path.isdir(current):
                    return _RESOLVE_UNDECIDED, ""
                current = os.path.dirname(current) or os.sep
                continue
            current = os.path.join(current, component)
            if os.path.islink(current):
                target = os.readlink(current)
                if not os.path.isabs(target):
                    target = os.path.join(os.path.dirname(current), target)
                # Through the same walk, not `normpath()`: a target of its own
                # can carry `..`, and folding *that* lexically is the same
                # defect one level down.
                status, current = _resolve_for_ownership(target, _hops + 1)
                if status != _RESOLVE_OK:
                    return _RESOLVE_UNDECIDED, ""
        return _RESOLVE_OK, current
    except (OSError, ValueError):
        return _RESOLVE_UNDECIDED, ""


#: Outcomes of `_script_file_match()`.
_MATCH_SAME, _MATCH_OTHER, _MATCH_UNDECIDED = "same", "other", "undecided"


def _script_file_match(a: str, b: str):
    """`(verdict, resolved_a)` — do two paths name the same script file?

    Three-valued, because the two-valued version had to answer "no" to a path
    it could not resolve, and "no" reads at the caller as "somebody else's" or
    — worse, once it falls past the basename check — as "nothing here".
    `samefile()` after resolution, for what resolution alone cannot settle
    (bind mounts, hard links).
    """
    status_a, resolved_a = _resolve_for_ownership(a)
    status_b, resolved_b = _resolve_for_ownership(b)
    if status_a != _RESOLVE_OK or status_b != _RESOLVE_OK:
        return _MATCH_UNDECIDED, resolved_a
    if resolved_a == resolved_b:
        return _MATCH_SAME, resolved_a
    try:
        if os.path.samefile(resolved_a, resolved_b):
            return _MATCH_SAME, resolved_a
    except OSError:
        pass
    return _MATCH_OTHER, resolved_a


def proc_table(proc_root: str = "/proc"):
    """`{pid: (ppid, [argv…])}` for every process, or None if incomplete.

    Per entry: a process that vanished mid-walk was never in our pane, but one
    we could not *read* might be, and a table that quietly dropped it would
    answer "nothing of ours in there" to the one caller that then kills the
    pane.
    """
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return None
    table = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            argv = [a.decode("utf-8", "replace")
                    for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError as exc:
            if exc.errno in _PROC_GONE_ERRNOS:
                continue
            return None
        try:
            tail = stat[stat.rindex(")") + 2:].split()
            ppid = int(tail[1])
        except (ValueError, IndexError):
            return None
        table[pid] = (ppid, argv)
    return table


def descendants(table: dict, root_pid: int) -> set:
    """`root_pid` and everything below it, per the ppid edges in `table`."""
    children: dict = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen = {root_pid}
    stack = [root_pid]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def script_owner(pid, argv, script: str, mine: str, *, proc_root: str):
    """`(owner, detail)` — does `pid` run `script`, and out of whose checkout?

    `mine` is the absolute path this checkout would run.  A **relative** launch
    (`bash scripts/dispatcher.sh`, which is what people actually type) is
    resolved against the process's own working directory; before this, such a
    process matched nothing and the pane it was in was classified as empty —
    i.e. the ordinary way of starting a daemon put it outside the guard.

    The path is resolved through its symlinks *before* it is recognised, so
    that what a process runs is decided by the file it reaches and not by how
    the path was spelled.  The two ways spelling used to win: a differently
    named alias (`python3 /theirs/monitor`) answered "not this script", and a
    `..` folded lexically across a symlink answered "ours".  Both land on the
    destructive side, so neither is a judgment this may make on a name.
    """
    cwd_cache: list = []            # [(status, path)] — read at most once

    def absolute(arg: str):
        """`(status, absolute_path)` for an argv entry, against the pid's cwd."""
        if os.path.isabs(arg):
            return _PROC_OK, arg
        if not cwd_cache:
            cwd_cache.append(_proc_cwd(pid, proc_root))
        status, cwd = cwd_cache[0]
        return (status, os.path.join(cwd, arg)) if status == _PROC_OK else (status, "")

    exec_arg = _executed_script_arg(argv)
    if exec_arg is not None:
        status, path = absolute(exec_arg)
        if status == _PROC_GONE:
            return OWNER_NONE, ""          # exited; it is not in the pane
        if status != _PROC_OK:
            # A relative program name with no working directory to anchor it.
            # This used to answer UNKNOWN only when the *spelling* already
            # looked like our script, so a renamed alias (`./monitor`) fell
            # straight through to "nothing here" — an empty pane, which is the
            # verdict that authorises destroying it (Codex 5巡目 P1-1).  What
            # a relative path names cannot be known without the cwd, whatever
            # it is spelled, so the answer is the same either way.
            return OWNER_UNKNOWN, (
                f"pid {pid} runs {exec_arg!r} but its working directory could "
                f"not be read, so which checkout it belongs to is undecided")

        # Recognition happens on the *resolved* path, never on the spelling in
        # argv.  `python3 /theirs/monitor`, where `monitor` is a symlink to
        # `/theirs/scripts/watchdog.py`, is a running daemon; matching the
        # basename of argv first answered "not this script" and so classified
        # its pane as genuinely empty.
        verdict, resolved = _script_file_match(path, mine)
        if verdict == _MATCH_SAME:
            return OWNER_MINE, f"pid {pid} runs {resolved}"
        if verdict == _MATCH_UNDECIDED:
            if script in (os.path.basename(path), os.path.basename(exec_arg)):
                return OWNER_UNKNOWN, (
                    f"pid {pid} runs {path!r}, which could not be resolved to a "
                    f"file (a symlink on the way is gone), so whether it is "
                    f"{mine} is undecided")
        elif script in (os.path.basename(resolved), os.path.basename(path)):
            return OWNER_FOREIGN, f"pid {pid} runs {resolved}, not {mine}"

    # Not provably *executing* the script — but the name may still be in there.
    # Answering NONE to an unrecognised launch shape is what let "a daemon
    # started some way we did not think of" be read as an empty pane.
    #
    # Both spellings are looked for: the basename as written, and — because a
    # daemon reached through a renamed symlink carries neither its own name nor
    # its own directory in argv — the file each path-like argument resolves to.
    for arg in argv[:_MAX_ARGV_SCAN]:
        if os.path.basename(arg) == script:
            return OWNER_UNKNOWN, (
                f"pid {pid} mentions {arg!r} in a launch shape this cannot "
                f"identify ({' '.join(argv)!r})")
        if os.sep not in arg:
            continue
        status, path = absolute(arg)
        if status != _PROC_OK:
            continue
        verdict, resolved = _script_file_match(path, mine)
        if verdict == _MATCH_SAME or (
                verdict == _MATCH_OTHER and os.path.basename(resolved) == script):
            return OWNER_UNKNOWN, (
                f"pid {pid} has {arg!r} in its argv, which reaches {resolved}, "
                f"in a launch shape this cannot identify ({' '.join(argv)!r})")
    return OWNER_NONE, ""


def _pane_recognition(pane_pid, targets, *, proc_root: str):
    """`(verdicts, others, problem, root_state)` from **one** walk of `pane_pid`.

    `targets` is `[(script, mine), …]`; `verdicts` maps each owner this walk
    recognised to the detail that justified it; `others` is the set of live
    pids under the pane apart from its own shell; `problem` is a detail string
    when the walk could not be completed at all, and None otherwise;
    `root_state` is what the pane's own root process turned out to be
    (`PANE_IDLE` / `PANE_LIVE` / `PANE_UNKNOWN`).

    One walk rather than one per script, because the two questions a caller
    asks — "is *this* script in there?" and "is the pane empty?" — have to be
    answered about the same instant, and because folding a second script's
    "I did not recognise anything" into the first script's answer turns a pane
    running our own dispatcher into an undecided one.

    `root_state` exists because `others` deliberately drops `pane_pid`, and
    that subtraction used to be unconditional.  Dropping the root is only
    right when the root really is the pane's idle shell; when the shell has
    `exec`'d a daemon — through an alias `script_owner()` cannot resolve, say
    — the root *is* the occupant, and subtracting it left `others` empty and
    the pane classified as provably empty (Codex 6巡目 P1-1).  Whether the
    root is a shell is a separate fact from whether argv is recognisable, and
    it has to be established positively rather than assumed.
    """
    try:
        pane_pid = int(pane_pid)
    except (TypeError, ValueError):
        return {}, set(), f"unusable pane pid {pane_pid!r}", PANE_UNKNOWN
    table = proc_table(proc_root)
    if table is None:
        return {}, set(), f"{proc_root} could not be walked completely", PANE_UNKNOWN
    if pane_pid not in table:
        return {}, set(), f"pane pid {pane_pid} is not in {proc_root}", PANE_UNKNOWN

    under = descendants(table, pane_pid)
    verdicts: dict = {}
    for pid in under:
        for script, mine in targets:
            owner, detail = script_owner(
                pid, table[pid][1], script, os.path.normpath(str(mine)),
                proc_root=proc_root)
            if owner != OWNER_NONE:
                verdicts.setdefault(owner, detail)
    root_state = _pane_shell_state(pane_pid, proc_root)
    return verdicts, under - {pane_pid}, None, root_state


def _settle_pane(verdicts: dict, others: set, pane_pid, what: str,
                 root_state: str = PANE_UNKNOWN):
    """The pane's verdict once the walk is done.

    `NONE` is a claim about the pane, not about how the scan went.  "Nothing
    in there was recognisable" and "there is nothing in there" are different
    answers, and only the second is a reason to destroy it — every P1 in this
    area has been the classifier failing to recognise something and the pane
    being read as empty because of it.  So `NONE` requires two positive facts
    together:

      * nothing but the pane's root process is running, **and**
      * that root process is demonstrably an **idle shell** — the same test
        `spawn()` already applies before it relaunches into a pane, so the two
        cannot disagree about what a reusable husk is.

    A husk satisfies both, which is what keeps ordinary crash recovery working
    (t035).  A pane whose root has `exec`'d something unrecognisable satisfies
    only the first, and that gap is what authorised destroying it.
    """
    for owner in _OWNER_PRECEDENCE:
        if owner in verdicts:
            return owner, verdicts[owner]
    if others:
        shown = ", ".join(str(p) for p in sorted(others)[:5])
        return OWNER_UNKNOWN, (
            f"pane pid {pane_pid} holds {len(others)} live process(es) besides "
            f"its shell ({shown}{'…' if len(others) > 5 else ''}) and none of "
            f"them could be identified as {what}; not recognised is not "
            f"not there")
    if root_state != PANE_IDLE:
        return OWNER_UNKNOWN, (
            f"nothing under pane pid {pane_pid} could be identified as {what}, "
            f"and its root process is not a shell sitting at a prompt "
            f"({root_state}) — so the root is itself running something, and "
            f"what that is could not be established")
    return OWNER_NONE, (
        f"nothing but the pane's idle shell is running under pane pid "
        f"{pane_pid}")


def pane_script_owner(pane_pid, script: str, mine: str, *,
                      proc_root: Optional[str] = None):
    """`(owner, detail)` — is `script`, out of `mine`'s checkout, under the pane?

    `UNKNOWN` for anything that could not be read — the pane's pid, /proc, a
    working directory — and for a pane holding live processes none of which
    could be identified.  Production breaks precisely when things cannot be
    read, and a guard that only holds while everything is readable is not one.
    """
    proc_root = _PROC_ROOT if proc_root is None else proc_root
    verdicts, others, problem, root_state = _pane_recognition(
        pane_pid, [(script, mine)], proc_root=proc_root)
    if problem is not None:
        return OWNER_UNKNOWN, problem
    return _settle_pane(verdicts, others, pane_pid, script, root_state)


#: Classifier answers that forbid destruction outright, whatever else is known.
#: `FOREIGN` is the one answer that is a *positive* identification of somebody
#: else's daemon, so it outranks even our own spawn record: the record proves
#: we created the pane, not that what is in it now is ours to end.
_OWNERS_THAT_VETO = frozenset({OWNER_FOREIGN})

#: Classifier answers that are themselves a positive proof that destroying the
#: pane takes nothing with it.  `NONE` now means "nothing but the pane shell is
#: running" (see `pane_script_owner`), which is a fact about the pane rather
#: than about how well the scan went.
_OWNERS_THAT_PROVE_EMPTY = frozenset({OWNER_NONE})

#: Classifier answers that let a *matching record* go on to authorise
#: destruction.  The record answers "did I make this pane?"; these answer "and
#: is what is in it now still mine, or nothing at all?".
#:
#: `UNKNOWN` is deliberately not here, and that is the whole of the 7th round.
#: Its five findings are five ways for a record to match when it should not —
#: a generation captured a moment after the id it belongs to, a pane id reused
#: by a server that restarted between validation and destruction, a tmux
#: "generation" that is a pid and so can come round again.  Every one of them
#: needs the pane's occupant to be unidentifiable to do any harm: if the
#: occupant is positively foreign the veto stops it, and if it is ours or
#: provably empty then destroying it is not the harm.  Making the record
#: *and* the occupant both carry the decision is therefore worth more than
#: making the record unforgeable, and costs no boot ids or process start
#: times to say.
_OWNERS_THAT_CONFIRM_A_RECORD = frozenset({OWNER_MINE, OWNER_NONE})


def daemon_pane_owner(pane_pid, *, repo_root=None,
                      proc_root: Optional[str] = None):
    """`(owner, detail)` — whose crewvia daemon, if any, `pane_pid` holds.

    Three-valued across *every* daemon script, worst answer first: a pane with
    another checkout's dispatcher and our watchdog is not ours to end, and a
    pane we could not read is not a pane we know to be empty.

    `UNKNOWN` is the answer that changed: it used to be folded into "no reason
    to refuse" here, which is how a guard that reads the pane rather than the
    environment still let a clean-env `kill("watchdog")` reach production.
    """
    root = Path(repo_root) if repo_root is not None else _own_repo_root()
    proc_root = _PROC_ROOT if proc_root is None else proc_root
    verdicts, others, problem, root_state = _pane_recognition(
        pane_pid,
        [(script, str(root / "scripts" / script)) for script in DAEMON_SCRIPTS],
        proc_root=proc_root)
    if problem is not None:
        return OWNER_UNKNOWN, problem
    return _settle_pane(verdicts, others, pane_pid, "a crewvia daemon",
                        root_state)


# ---------------------------------------------------------------------------
# Spawn records — what this checkout wrote down when it created a pane
# ---------------------------------------------------------------------------
#
# Everything above this line is *observation*: read /proc, resolve a path,
# decide whose daemon is in the pane.  Five rounds of review have now found
# five different ways for that observation to come back wrong, and each one
# came back wrong on the destructive side — a pane misread as empty, a foreign
# script misread as ours.  That is not a run of bad luck; it is what happens
# when a destructive decision rests on inference about the outside world.
#
# So the basis is moved.  `registry/mux/<name>.json` is written by `spawn()`
# at the moment this process creates a pane, and it holds the backend's own
# immutable identifier for it (tmux `@window_id`, herdr `tab_id`).  It is not
# an observation to be re-derived; it is a note this checkout wrote to itself.
# No symlink, interpreter name or unreadable `cwd` can change what it says.
#
# The record answers "did I make this?", which is the question destruction
# actually turns on.  The classifier stays, demoted to two narrow jobs: a veto
# when it can positively identify a foreign daemon, and a second positive proof
# (a provably empty pane) for the case where the record is gone but there is
# nothing in there to lose.

#: Where the records live, relative to the checkout that wrote them.  Reading
#: another checkout's directory would defeat the point, so this is anchored to
#: `_own_repo_root()` — the location of *this file*, the same identity the
#: classifier uses and the one thing the environment cannot lie about.
_PANE_RECORD_DIR_NAME = Path("registry") / "mux"

#: The three answers of `pane_record_status()`.  ABSENT and MISMATCH are kept
#: apart because they mean different things to an operator — "this pane was
#: never mine" versus "the pane under that name is not the one I made" — and
#: the refusal message has to be able to say which.
PANE_RECORD_MATCH = "match"
PANE_RECORD_ABSENT = "absent"
PANE_RECORD_MISMATCH = "mismatch"


def pane_record_dir(repo_root=None) -> Path:
    root = Path(repo_root) if repo_root is not None else _own_repo_root()
    return root / _PANE_RECORD_DIR_NAME


def pane_record_path(name: str, *, repo_root=None) -> Path:
    # `_pane_name()` so a test run's records cannot be read back as
    # production's out of the same directory.
    return pane_record_dir(repo_root) / f"{_pane_name(name)}.json"


def _record_checkout_identity(repo_root=None) -> str:
    """The checkout a record belongs to, as it is written into the record.

    The *content* of the record, not merely its location.  Anchoring the file
    under `_own_repo_root()` isolates records only while every checkout has a
    directory of its own; two worktrees sharing `registry/mux` through a
    symlink — or a record simply copied from one tree to another — both read
    the same file and, before this, both recognised it as their own (Codex
    6巡目 P1-2).  A path written *into* the file survives both.
    """
    root = Path(repo_root) if repo_root is not None else _own_repo_root()
    try:
        return str(root.resolve())
    except OSError:
        return str(root)


def _record_storage_is_shared(repo_root=None) -> bool:
    """Whether this checkout's record directory can be reached by another one.

    True when any component of `registry/mux` leaves the checkout — i.e. the
    directory resolves somewhere other than directly beneath our own root.
    That is exactly the condition under which "the file is under my root"
    stops being evidence of who wrote it, and therefore the condition under
    which a record with no recorded provenance has to be refused.

    Unreadable answers count as shared: this is the input to a *migration*
    allowance, so the direction to fail in is "do not extend the allowance".
    """
    root = Path(repo_root) if repo_root is not None else _own_repo_root()
    directory = pane_record_dir(repo_root)
    try:
        return directory.resolve() != (root.resolve() / _PANE_RECORD_DIR_NAME)
    except OSError:
        return True


def write_pane_record(name: str, backend: str, handle: str, *,
                      pane_id: str = "", server=None, repo_root=None) -> bool:
    """Note that *this* checkout created `name`'s pane, as `handle`.

    `server` is `(endpoint, generation)` for the mux server the pane was
    created on.  A pane id is unique only within one server's lifetime, so
    without it the record is a claim about an id that a later server can hand
    to somebody else — and `pane_record_status()` refuses such a record.

    So when the backend could not establish a server, nothing is written and
    False comes back.  Writing anyway was reachable through an ordinary spawn
    and produced a record that matched on any endpoint and any generation
    (Codex 7巡目 P1-2).  Any *previous* record for this name goes too: it was
    written about a pane this spawn has just replaced, and leaving it behind
    is the one way a stale record can go on naming a live pane.
    """
    path = pane_record_path(name, repo_root=repo_root)
    endpoint, generation = ("", "")
    if server is not None:
        # Anything that is not a usable `(endpoint, generation)` is "could not
        # be established", not a crash: this runs just after a pane was
        # created, and raising here would lose the pane's launch as well as
        # its record.
        try:
            endpoint, generation = str(server[0] or ""), str(server[1] or "")
        except (TypeError, IndexError, KeyError):
            endpoint, generation = ("", "")
    if not endpoint or not generation:
        print(f"[mux] WARNING: not recording the spawn of {name!r}: the mux "
              f"server it was created on could not be identified, and a "
              f"record that is not bound to one would match the same pane id "
              f"on any other server or generation. Killing this pane later "
              f"will refuse unless the pane is provably empty — use --force, "
              f"or respawn it once the server can be identified.",
              file=sys.stderr)
        drop_pane_record(name, repo_root=repo_root)
        return False
    record = {
        "handle": str(handle or ""),
        # `tab_id` / `pane_id` are what HerdrBackend already resolved
        # sends/captures through; `handle` is the one the guard reads, and
        # for herdr they are the same string.
        "tab_id": str(handle or ""),
        "pane_id": str(pane_id or ""),
        "backend": backend,
        "checkout": _record_checkout_identity(repo_root),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {"endpoint": endpoint, "generation": generation},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return True
    except Exception as exc:
        print(f"[mux] WARNING: could not record the spawn of {name!r} at "
              f"{path}: {exc}", file=sys.stderr)
        return False


def read_pane_record(name: str, *, repo_root=None) -> Optional[dict]:
    try:
        data = json.loads(
            pane_record_path(name, repo_root=repo_root).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def drop_pane_record(name: str, *, repo_root=None) -> None:
    try:
        pane_record_path(name, repo_root=repo_root).unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[mux] WARNING: could not drop the spawn record for {name!r}: "
              f"{exc}", file=sys.stderr)


def _server_binding_problem(recorded_server, server, path):
    """Why the record's mux server does not vouch for `server`, or None.

    Split out because "the record names no server" and "the record names a
    different server" used to be answered in two different ways: the second
    refused, and the first *skipped the whole check* and fell through to a
    match.  A record with no binding is not a record that binds to everything
    (Codex 7巡目 P1-2); both answers are a refusal, and they are next to each
    other here so a later reader cannot re-introduce the asymmetry.
    """
    if not isinstance(recorded_server, dict):
        return (f"the spawn record at {path} names no mux server, so it does "
                f"not say which server's pane id it is about — the same id on "
                f"another server is another pane. Respawn the pane, or use "
                f"--force if you mean to take it over")
    recorded_endpoint = str(recorded_server.get("endpoint") or "")
    recorded_generation = str(recorded_server.get("generation") or "")
    if not recorded_endpoint or not recorded_generation:
        return (f"the spawn record at {path} binds to an incomplete mux "
                f"server (endpoint {recorded_endpoint!r}, generation "
                f"{recorded_generation!r}), which is not enough to tell one "
                f"server's pane id from another's")
    if server is None:
        return (f"the spawn record names the mux server {recorded_endpoint!r} "
                f"(generation {recorded_generation!r}), but the current "
                f"server could not be identified, so the record cannot be "
                f"shown to be about this pane")
    endpoint, generation = str(server[0]), str(server[1])
    if recorded_endpoint != endpoint:
        return (f"the spawn record was written against the mux server at "
                f"{recorded_endpoint!r}, and this is {endpoint!r} — the same "
                f"pane id on a different server is a different pane")
    if recorded_generation != generation:
        return (f"the spawn record was written against generation "
                f"{recorded_generation!r} of the mux server, and this is "
                f"{generation!r} — the server has restarted since, so the "
                f"recorded id is free to have been given to somebody else")
    return None


def pane_record_status(name: str, backend: str, handle, *, server=None,
                       repo_root=None):
    """`(status, detail)` — does our own record name the pane we are holding?

    `handle` is the identifier the backend handed back from the *same* query
    that produced the pane pid, so this compares the thing that was inspected
    with the thing that was created.  Names are not compared at all: a name is
    what another checkout can move onto a different pane between two calls,
    and doing exactly that is how the 4th round's P1-4 was demonstrated.

    `server` is the mux server the caller is holding the pane on, as
    `(endpoint, generation)`.  A pane id means nothing without it: a tmux
    `@window_id` is unique only inside one server's lifetime, records outlive
    both external window destruction and server shutdown, and a second socket
    can hand out the very same id at the same moment (Codex 6巡目 P1-3).

    Three things therefore have to line up, not one: the checkout that wrote
    the record, the server it was written against, and the id itself.
    """
    record = read_pane_record(name, repo_root=repo_root)
    path = pane_record_path(name, repo_root=repo_root)
    if record is None:
        return PANE_RECORD_ABSENT, (
            f"this checkout has no spawn record at {path}")
    recorded = str(record.get("handle") or record.get("tab_id") or "")
    if not recorded:
        return PANE_RECORD_MISMATCH, "the spawn record names no pane id"
    if record.get("backend") != backend:
        return PANE_RECORD_MISMATCH, (
            f"the spawn record was written by the {record.get('backend')!r} "
            f"backend, and this is {backend!r}")

    # --- whose record is it? ------------------------------------------------
    mine = _record_checkout_identity(repo_root)
    written_by = record.get("checkout")
    legacy = False
    if written_by:
        if str(written_by) != mine:
            return PANE_RECORD_MISMATCH, (
                f"the spawn record at {path} was written by the checkout at "
                f"{written_by!r}, and this is {mine!r} — it is not ours")
    elif _record_storage_is_shared(repo_root):
        return PANE_RECORD_MISMATCH, (
            f"the spawn record at {path} records no checkout, and the record "
            f"directory is shared with other checkouts, so there is nothing "
            f"to show it was written here — re-spawn the pane, or use "
            f"--force if you mean to take it over")
    else:
        # Migration, and the only place a record without provenance is taken
        # at its word.  Every daemon running across this change has one, and
        # refusing them all would mean the panes they name could never be
        # ended again — a permanent refusal is not a safe default, it is a
        # different outage (memory: fail-closed-discard-vs-hold).  Its
        # location is still weak evidence here, because nothing else can
        # reach this directory.  It is never silent.
        #
        # What it does *not* do any more is return.  It used to answer MATCH
        # from here, before the pane the caller is holding had been compared
        # with the one the record names — so a legacy record for `@7`
        # authorised destroying `@99` (Codex 7巡目 P1-1).  The allowance is
        # for how well the record's *provenance* is evidenced; it was never
        # meant to be an allowance about *which pane* the record is for.
        legacy = True
        print(f"[mux] WARNING: the spawn record at {path} is in the legacy "
              f"format (no checkout, no mux server recorded). It is being "
              f"accepted because this record directory is not shared, but "
              f"that is weaker than a record written by this checkout. It "
              f"will be replaced the next time this pane is spawned.",
              file=sys.stderr)

    # --- on which server, and which generation of it? -----------------------
    # Only of the new format: a legacy record has no server by definition, and
    # requiring one of it would refuse every daemon now running (the very
    # outage the migration allowance exists to avoid).
    if not legacy:
        problem = _server_binding_problem(record.get("server"), server, path)
        if problem is not None:
            return PANE_RECORD_MISMATCH, problem

    # --- and is it this pane? ----------------------------------------------
    if not handle:
        return PANE_RECORD_MISMATCH, (
            "the backend would not say which pane it inspected, so there is "
            "nothing to compare the record against")
    if str(handle) != recorded:
        return PANE_RECORD_MISMATCH, (
            f"the spawn record names {recorded!r}, but the pane under that "
            f"name is {str(handle)!r} — somebody else made this one")
    if legacy:
        return PANE_RECORD_MATCH, (
            f"spawned by this checkout as {recorded} (legacy record, "
            f"provenance taken from its unshared location)")
    return PANE_RECORD_MATCH, f"spawned by this checkout as {recorded}"


def may_destroy_pane(name: str, backend: str, handle, pane_pid, *,
                     server=None, repo_root=None,
                     proc_root: Optional[str] = None):
    """`(allowed, reason)` — the one place that decides a pane may be destroyed.

    Two positive proofs and one veto:

      * **provenance AND occupancy** — our own spawn record names this exact
        pane, *and* what is in the pane right now is either our own daemon or
        nothing.  Both halves, not either: the record is a fact we wrote, but
        five rounds of review have now found as many ways for a record to be
        stale, shared or reused, and each one of them needs an occupant we
        cannot identify in order to reach anything destructive.  Requiring the
        occupant as well costs one classifier answer we already compute and
        closes all of them at once (Codex 7巡目 — see
        `_OWNERS_THAT_CONFIRM_A_RECORD`).
      * **emptiness** — the pane provably holds no live process at all.  This
        one stands on its own, without a record, and has to: a herdr restart
        restores tabs with fresh ids and leaves every record stale, and
        without it the husks could never be cleared again (memory:
        fail-closed-discard-vs-hold).  It is not a weakening — a pane with
        nothing in it is a pane whose destruction takes nothing with it.
      * **veto** — a positively identified foreign daemon stops both. The
        record says we made the pane, not that what is in it now is ours.

    Being *recognised as ours* is still not a proof on its own. That was the
    old basis, and `MINE` is precisely what the 5th round produced out of a
    `..` folded across a deleted symlink; here it can only confirm a record,
    never stand in for one.

    Panes outside `DAEMON_PANE_NAMES` never reach any of this — see that
    constant for why the scope is the safety property here.
    """
    if name not in DAEMON_PANE_NAMES:
        return True, "not a daemon pane — outside this guard's scope"

    owner, owner_detail = daemon_pane_owner(
        pane_pid, repo_root=repo_root, proc_root=proc_root)
    if owner in _OWNERS_THAT_VETO:
        return False, (f"another checkout's daemon is running in it "
                       f"({owner}: {owner_detail})")

    status, record_detail = pane_record_status(
        name, backend, handle, server=server, repo_root=repo_root)
    if status == PANE_RECORD_MATCH and owner in _OWNERS_THAT_CONFIRM_A_RECORD:
        return True, (f"{record_detail}; what is in it: {owner} "
                      f"({owner_detail})")
    if owner in _OWNERS_THAT_PROVE_EMPTY:
        return True, (f"{record_detail}, but the pane provably holds nothing: "
                      f"{owner_detail}")
    if status == PANE_RECORD_MATCH:
        return False, (
            f"the spawn record matches ({record_detail}), but what is in the "
            f"pane could not be identified: {owner} ({owner_detail}) — a "
            f"record on its own no longer ends a pane, because a record can "
            f"be stale or reused and this is the reading that would notice. "
            f"Use --force if you mean to end it anyway")
    return False, (f"nothing proves this pane is ours to end — {record_detail}; "
                   f"what is in it: {owner} ({owner_detail})")


# ---------------------------------------------------------------------------
# TmuxBackend
# ---------------------------------------------------------------------------

class TmuxBackend(_Backend):
    """tmux backend — wraps current crewvia tmux CLI calls verbatim.

    Session name: "crewvia" (overridden by CREWVIA_TMUX_SESSION env).
    Window target format: "<session>:<window_name>".

    Verb signatures and return values
    ----------------------------------
    spawn(name, cmd, cwd=None, env=None) -> bool
        Create a new window named `name` in the session and run `cmd`.
        Returns True on success, False on error or when a window of that name
        already holds a live process.  A window holding only an idle shell (a
        husk) is reused: `cmd` is typed into it and True is returned.

    send(name, text) -> bool
        Send `text` to the window as a 2-step: send-keys text, 0.1s sleep, send-keys Enter.
        This matches dispatcher.sh L421-440 to work around Claude TUI bracketed paste.
        Returns True on success, False on error.

    capture(name) -> str
        Return the current screen contents (capture-pane -p).
        Returns "" on error.

    list(suffix=None) -> [str]
        Return names of live windows. When suffix is given, only windows whose
        name ends with suffix are returned.

    kill(name) -> bool
        Kill the window named `name`. Returns True on success, False on error.

    pid(name) -> int | None
        Return the shell PID of the window's pane (#{pane_pid}). None on error.

    attach(name) -> bool
        If $TMUX is set, switch-client to session. Otherwise attach-session.
        Returns True on success, False on error.

    available() -> bool
        True if `tmux` binary is found in PATH.

    server_running() -> bool
        True if a tmux server is up (`tmux list-sessions` exits 0). Never
        starts one.
    """

    BACKEND_NAME = "tmux"

    def _guard(self, verb: str, name: str) -> None:
        _guard_test_isolation(self.BACKEND_NAME, verb, name, _session())

    def _target(self, name: str) -> str:
        """`<session>:<window>` for the caller's `name`.

        The single place a caller-level name becomes a tmux window name, which
        is why the test namespace is applied here rather than in each verb.
        """
        return f"{_session()}:{_pane_name(name)}"

    def _pane_process_state(self, name: str, pane_pid=_UNSET) -> str:
        """`PANE_IDLE` / `PANE_LIVE` / `PANE_UNKNOWN` for the window `name`.

        The tmux counterpart of HerdrBackend._pane_process_state, and the
        reason spawn() can tell a husk from an occupied window on both
        backends.  tmux offers no `process-info`, so the pane's shell pid
        (`#{pane_pid}`) is the entry point and /proc answers the rest.

        `pane_pid` may be supplied by a caller that has already inspected the
        pane, and spawn() does supply it: looking the name up again here would
        be a second resolution, and the window under a name can change between
        two of them (Codex 6巡目 P1-4).  Omitting it keeps the old behaviour
        of resolving the name, which is right for a one-shot question.
        """
        if pane_pid is _UNSET:
            pane_pid = self.pid(name)
        if pane_pid is None:
            self._warn(f"could not read pane pid for {name!r} — pane state unknown")
            return PANE_UNKNOWN
        return _pane_shell_state(pane_pid, _PROC_ROOT)

    def _pane_has_live_process(self, name: str, pane_pid=_UNSET) -> bool:
        """True unless the window is *demonstrably* an idle shell.

        The occupancy question, where unknown must read as busy: never
        relaunch on top of a pane we cannot see into.  The launch-verification
        question needs the opposite default and therefore uses
        `_pane_process_state()` directly.
        """
        return self._pane_process_state(name, pane_pid) != PANE_IDLE

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def server_running(self) -> bool:
        """True if a tmux server is up right now (never starts one).

        `tmux list-sessions` exits non-zero when no server is running, so it
        answers the same question _herdr_ping() does for herdr.  Deliberately
        not available(), which would report "up" for a mere binary in PATH.
        """
        if shutil.which("tmux") is None:
            return False
        try:
            r = subprocess.run(
                ["tmux", "list-sessions"], capture_output=True, timeout=5
            )
            return r.returncode == 0
        except Exception:
            return False

    def spawn(self, name: str, cmd: str, cwd: Optional[str] = None,
              env: Optional[dict] = None) -> bool:
        """Create a tmux window named `name` and run `cmd`.

        Mirrors start.sh L468-481:
          - has-session → new-session (if session missing) → new-window
          - send-keys <cmd> + Enter

        Returns False (without error) when a window of this name already holds
        a live process.  When the window exists but holds nothing but an idle
        shell — the husk every ordinary crash leaves behind, since the daemon
        is the pane shell's child — the command is typed into that window and
        True is returned, which is what HerdrBackend.spawn already did.  The
        two backends answering differently here is how "the peer is dead but
        its tab is still listed" became un-actionable on tmux (t006 QA
        FAIL-1).
        """
        self._guard("spawn", name)
        session = _session()
        window = _pane_name(name)
        try:
            # Check / create session
            has = subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True, timeout=5,
            )
            if has.returncode != 0:
                # `-P -F` so the id comes from the creation itself.  Resolving
                # it afterwards by name is what let another checkout's window
                # be recorded as ours (Codex 6巡目 P1-4).
                r = subprocess.run(
                    ["tmux", "new-session", "-d", "-s", session, "-n", window,
                     "-P", "-F", "#{window_id}"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode != 0:
                    self._warn(f"new-session failed for {session!r}: {r.stderr}")
                    return False
                window_id = r.stdout.strip()
            else:
                # Session exists — check if window already exists
                existing = subprocess.run(
                    ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
                    capture_output=True, text=True, timeout=5,
                )
                if existing.returncode == 0 and window in existing.stdout.splitlines():
                    # One inspection, and everything below uses *its* answer.
                    # Asking again by name would re-open the very window this
                    # is deciding about to replacement in between.
                    inspected_id, pane_pid, _ = self._inspect_pane_full(name)
                    if self._pane_has_live_process(name, pane_pid):
                        return False  # live one (or unreadable) → no-op, per spec
                    if not inspected_id:
                        self._warn(
                            f"spawn {name!r}: the window under that name could "
                            f"not be identified, so nothing was relaunched "
                            f"into it.")
                        return False
                    # Husk: the shell outlived whatever it was running.
                    # Relaunching in place keeps the window (and its position)
                    # and is what lets a peer — or ./crewvia — actually revive
                    # a daemon that merely crashed.
                    self._warn(
                        f"spawn {name!r}: window {inspected_id} holds only an "
                        "idle shell (crashed?) — relaunching in place"
                    )
                    if not self._send_to_target(inspected_id, cmd, what=name):
                        return False
                    # Before the wait, not after: the window is ours from the
                    # moment we relaunch into it, and a daemon that takes a
                    # while to come up must not be a daemon we cannot later
                    # restart.
                    self._record_spawn(name, inspected_id)
                    return _wait_until_launched(
                        lambda: self._pane_process_state(name, pane_pid),
                        warn=self._warn, name=name)
                r = subprocess.run(
                    ["tmux", "new-window", "-t", session, "-n", window,
                     "-P", "-F", "#{window_id}"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode != 0:
                    self._warn(f"new-window failed for {name!r}: {r.stderr}")
                    return False
                window_id = r.stdout.strip()

            if not window_id.startswith("@"):
                # Falling back to the name here would put back exactly the
                # hole this closed, so there is no fallback: the window exists
                # but this checkout cannot prove which one it is, and nothing
                # is typed into a window we cannot name.
                self._warn(
                    f"spawn {name!r}: tmux did not report the id of the window "
                    f"it created ({window_id!r}), so nothing was launched into "
                    f"it and no spawn record was written.")
                return False

            subprocess.run(
                ["tmux", "send-keys", "-t", window_id, cmd],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", window_id, "Enter"],
                capture_output=True, timeout=5,
            )
            self._record_spawn(name, window_id)
            return True
        except Exception as e:
            self._warn(f"spawn {name!r} failed: {e}")
            return False

    def send(self, name: str, text: str) -> bool:
        """Send `text` + Enter to the named window.

        Uses 2-step send-keys with 0.1 s sleep between text and Enter to work
        around Claude TUI's bracketed paste handling (same as dispatcher.sh).

        Clears the input line (``C-u``) immediately before typing (t007,
        PR#189 レビュー指摘 F3): a caller that retries after `verify_sent()`
        reports the previous attempt as not-landed would otherwise type on
        top of the still-present text, concatenating messages
        ("ミッション開始…ミッション開始…"). Clearing first is a no-op when the
        line is already empty, so this is safe on a first attempt too.
        """
        self._guard("send", name)
        return self._send_to_target(self._target(name), text, what=name)

    def _send_to_target(self, target: str, text: str, *, what: str) -> bool:
        """The send itself, against whatever `target` names.

        Split out so `spawn()`'s husk path can type into the `@window_id` it
        just inspected instead of going back to the name.  `send()` keeps
        using the name, which is right for it: a caller that says "send to
        dispatcher" means whatever is called that now.  `spawn()` means the
        window it just looked at, and those are different questions whenever
        another checkout is moving names around.
        """
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "C-u"],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", target, text],
                capture_output=True, timeout=5,
            )
            time.sleep(0.1)
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Enter"],
                capture_output=True, timeout=5,
            )
            return True
        except Exception as e:
            self._warn(f"send to {what!r} failed: {e}")
            return False

    def capture(self, name: str) -> str:
        """Return the current pane contents via capture-pane -p."""
        self._guard("capture", name)
        target = self._target(name)
        try:
            r = subprocess.run(
                ["tmux", "capture-pane", "-t", target, "-p"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                self._warn(f"capture-pane failed for {name!r}: {r.stderr}")
                return ""
            return r.stdout
        except Exception as e:
            self._warn(f"capture {name!r} failed: {e}")
            return ""

    def list(self, suffix: Optional[str] = None) -> List[str]:
        """Return names of live windows, optionally filtered by suffix."""
        session = _session()
        try:
            r = subprocess.run(
                ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return []
            prefix = _pane_prefix()
            names = [_caller_name(line.strip())
                     for line in r.stdout.splitlines()
                     if line.strip() and line.strip().startswith(prefix)]
            if suffix:
                names = [n for n in names if n.endswith(suffix)]
            return names
        except Exception as e:
            self._warn(f"list failed: {e}")
            return []

    def kill(self, name: str, *, allow_foreign: bool = False) -> bool:
        """Kill the named window.

        `allow_foreign=True` is the operator's way past the identity backstop
        (`restart --force`).  Without an exit, "cannot judge" turns into
        "nothing ever works again" (memory: fail-closed-discard-vs-hold).
        """
        self._guard("kill", name)
        target = self._target(name)
        if allow_foreign:
            self._warn_forced_bypass(name)
        if not allow_foreign and name in DAEMON_PANE_NAMES:
            window_id, pane_pid, server = self._inspect_pane_full(name)
            if self._refuses_foreign_daemon(name, window_id, pane_pid, server):
                return False
            if window_id is None:
                # Unreachable while the allowlist holds (no pid means UNKNOWN
                # means refused), and kept anyway: the invariant that matters
                # is that nothing is destroyed except through the identity the
                # inspection returned.
                self._warn(
                    f"kill {name!r}: refused — the window that was inspected "
                    f"could not be identified. Nothing was killed.")
                return False
            # The window *id*, not the name: window names are mutable, and the
            # /proc walk between the two takes real time.  If the inspected
            # window is gone and another checkout has recreated one under the
            # same name, `@id` no longer resolves and the kill fails — which
            # is the answer we want, rather than closing a stranger.
            target = window_id
        try:
            r = subprocess.run(
                ["tmux", "kill-window", "-t", target],
                capture_output=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            # The pane this checkout made is gone, so the note saying it made
            # one has to go too.  Left behind, it would still be there the next
            # time a window appears under this name — and that window is as
            # likely to be another checkout's as ours.
            drop_pane_record(name)
            return True
        except Exception as e:
            self._warn(f"kill {name!r} failed: {e}")
            return False

    def _record_spawn(self, name: str, window_id: str) -> None:
        """Write down which window this checkout just created.

        `window_id` is passed in rather than looked up, and that is the whole
        point.  It used to be re-resolved *by name* after the window had been
        created and the command sent — and in that interval another checkout
        can create or rename a window of the same name, so what came back was
        as likely to be theirs as ours.  Recording it forged exactly the
        provenance this guard rests on, and a forged record authorises
        destroying somebody else's window later (Codex 6巡目 P1-4).

        The id therefore comes from the command that *created* the window
        (`new-window -P -F '#{window_id}'`), or, on the husk path, from the
        inspection that decided the window was reusable.  Both are answers
        about the window we acted on; a name is not.
        """
        server = self.server_identity()
        if not server:
            self._warn(
                f"spawn {name!r}: tmux would not say which server this is, so "
                f"this spawn goes unrecorded — see the warning below.")
        write_pane_record(name, self.BACKEND_NAME, window_id, server=server)

    def _inspect_pane(self, name: str):
        """`(window_id, pane_pid)` in one `display-message` — see _Backend."""
        handle, pane_pid, _ = self._inspect_pane_full(name)
        return handle, pane_pid

    def _inspect_pane_full(self, name: str):
        """`(window_id, pane_pid, server)` from **one** `display-message`.

        All four values come out of the same query, so the window, the pid
        and the server they belong to cannot drift apart between reads.
        `#{pid}` is the tmux *server*'s pid — a fresh one per server lifetime,
        which is exactly the generation a `@window_id` is unique within —
        and `#{socket_path}` separates two servers running at once.
        """
        self._guard("pid", name)
        target = self._target(name)
        try:
            r = subprocess.run(
                ["tmux", "display-message", "-p", "-t", target,
                 "#{window_id} #{pane_pid} #{pid} #{socket_path}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                self._warn(f"display-message failed for {name!r}: {r.stderr}")
                return None, None, None
            parts = r.stdout.strip().split()
            if len(parts) < 2 or not parts[0].startswith("@"):
                return None, None, None
            try:
                pane_pid = int(parts[1])
            except ValueError:
                return None, None, None
            # A tmux too old to know these formats prints them back verbatim
            # rather than failing, so an unexpanded token is "no answer".
            server = None
            if len(parts) >= 4 and "#{" not in parts[2] + parts[3]:
                server = (parts[3], parts[2])
            return parts[0], pane_pid, server
        except Exception as e:
            self._warn(f"pid {name!r} failed: {e}")
            return None, None, None

    def server_identity(self):
        """`(socket_path, server_pid)` straight from the running server."""
        try:
            r = subprocess.run(
                ["tmux", "display-message", "-p", "#{socket_path} #{pid}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return None
            parts = r.stdout.strip().split()
            if len(parts) != 2 or "#{" in parts[0] + parts[1]:
                return None
            return parts[0], parts[1]
        except Exception:
            return None

    def pid(self, name: str) -> Optional[int]:
        """Return the shell PID of the pane (display-message #{pane_pid})."""
        return self._inspect_pane(name)[1]

    def attach(self, name: str) -> bool:
        """Attach to the session (called when already inside tmux).

        If $TMUX is set (already inside tmux), use switch-client to bring the
        session into view.  When $TMUX is not set, the caller should have used
        attach_cmd() + exec instead — this path returns False.

        Returns True on success, False on error.
        """
        session = _session()
        try:
            if os.environ.get("TMUX"):
                r = subprocess.run(
                    ["tmux", "switch-client", "-t", session],
                    capture_output=True, timeout=5,
                )
                return r.returncode == 0
            # Not inside tmux — attach_cmd() + exec should have been used.
            return False
        except Exception as e:
            self._warn(f"attach {name!r} failed: {e}")
            return False

    def attach_cmd(self, name: str) -> Optional[List[str]]:
        """Return the argv to exec in order to attach from outside the session.

        - Inside tmux ($TMUX set): return None → caller uses attach() (switch-client).
        - Outside tmux: return ['tmux', 'attach-session', '-t', '<session>:<name>']
          so the bash caller can exec it, giving the TTY to tmux.
        """
        if os.environ.get("TMUX"):
            return None
        return ["tmux", "attach-session", "-t", self._target(name)]

    def state(self, name: str) -> str:
        """Return agent state — always 'unknown' for tmux (state not available)."""
        return "unknown"


# ---------------------------------------------------------------------------
# HerdrBackend (Phase 2)
# ---------------------------------------------------------------------------

# Cache directory relative to repo root (script's parent's parent).
_HERDR_CACHE_DIR_NAME = Path("registry") / "mux"

# Verified herdr version.
_HERDR_VERIFIED_VERSION = "0.9.0"


def _idle_shell_entry_state(proc) -> str:
    """`PANE_IDLE` / `PANE_LIVE` / `PANE_UNKNOWN` for one process-info entry.

    Verified against herdr 0.9.0 `pane process-info`:
      idle prompt      → {"name": "bash", "argv": ["/bin/bash"]}
      running a script → {"name": "bash", "argv": ["bash", "…/dispatcher.sh"]}

    So the name must be a shell *and* it must have been invoked with no
    arguments.  Matching on the name alone would read a live `bash
    scripts/dispatcher.sh` pane as idle and start a second dispatcher on top
    of it.

    Three-valued rather than boolean, for the same reason `_pane_shell_state()`
    is: an entry we cannot classify (`{}`, a name with no argv) is not evidence
    of anything, and the two callers need it to fall in opposite directions.
    The boolean version collapsed it into "not idle", which the pane-level
    `all(...)` then turned into **PANE_LIVE** — so `_wait_until_launched()` read
    an unreadable entry as a successful launch and reported a daemon back from
    the dead (Codex 3 巡目 P2-4).
    """
    if not isinstance(proc, dict):
        return PANE_UNKNOWN
    name = proc.get("name")
    if not isinstance(name, str) or not name:
        return PANE_UNKNOWN
    if name.lstrip("-") not in _SHELL_PROCESS_NAMES:
        return PANE_LIVE          # rooted at something that is not a shell
    argv = proc.get("argv")
    if not isinstance(argv, list) or not argv:
        return PANE_UNKNOWN       # argv unavailable → cannot prove either way
    return PANE_IDLE if len(argv) == 1 else PANE_LIVE


def _is_idle_shell_process(proc: dict) -> bool:
    """True only when `proc` is *demonstrably* a shell at its prompt."""
    return _idle_shell_entry_state(proc) == PANE_IDLE


# Herdr CLI subcommand table — single place to update on CLI rename.
# Format: {key: (subcommand_parts...)} where subcommand_parts is joined with
# actual arguments at call time.
_HERDR_CLI = {
    "version":            ["herdr", "--version"],
    "server_start":       ["herdr", "server"],
    "workspace_list":     ["herdr", "workspace", "list"],
    "workspace_create":   ["herdr", "workspace", "create"],
    "tab_create":         ["herdr", "tab", "create"],
    "tab_list":           ["herdr", "tab", "list"],
    "tab_close":          ["herdr", "tab", "close"],
    "tab_focus":          ["herdr", "tab", "focus"],
    "pane_list":          ["herdr", "pane", "list"],
    "pane_get":           ["herdr", "pane", "get"],
    "pane_rename":        ["herdr", "pane", "rename"],
    "pane_run":           ["herdr", "pane", "run"],
    "pane_read":          ["herdr", "pane", "read", "--source", "visible"],
    "pane_send_keys":     ["herdr", "pane", "send-keys"],
    "pane_process_info":  ["herdr", "pane", "process-info", "--pane"],
}

# CREWVIA_HERDR_SOCK is a test-only override (see tests/lib-mux.bats).  `or` —
# not a .get() default — so that an exported-but-empty value falls back instead
# of resolving to Path("") == ".", which would make every ping fail.
_HERDR_SOCK_PATH = Path(
    os.environ.get("CREWVIA_HERDR_SOCK")
    or str(Path.home() / ".config" / "herdr" / "herdr.sock")
)

# Env vars that must never be baked into the herdr server process.
# herdr server keeps its startup env for its whole lifetime and hands it to
# every pane it spawns, so a session-scoped variable picked up here leaks
# into every future Director / Worker (see knowledge: herdr server stale env
# inheritance).  ./crewvia may now start the server itself, possibly from
# inside an agent pane, so the env is scrubbed at spawn time.
_HERDR_SERVER_ENV_DENY_PREFIXES = (
    "CLAUDE_",      # CLAUDE_CODE_*, CLAUDE_EFFORT, CLAUDE_PID, ...
    "CODEX_",
    "CREWVIA_",
    "TASK_",        # TASK_ID / TASK_TITLE (not TASKVIA_URL)
)
_HERDR_SERVER_ENV_DENY_EXACT = frozenset({
    # Secrets.  ./crewvia exports TASKVIA_TOKEN before it may start the server,
    # and start.sh re-exports all of these per pane in LAUNCH_CMD, so nothing
    # needs them in the server env — where they would sit in
    # /proc/<pid>/environ and reach every pane the server ever spawns.
    "TASKVIA_TOKEN",
    "NTFY_USER",
    "NTFY_PASS",
    "CLAUDECODE",
    "AI_AGENT",
    "AGENT_NAME",
    "ROLE",
    "SKILLS",
    "TARGET_DIR",
    "TMUX",
    "TMUX_PANE",
    "HERDR_ENV",
})


def _herdr_run(cmd_key: str, extra_args: List[str], timeout: int = 10) -> Optional[dict]:
    """Run a herdr CLI command and return parsed JSON result.

    On exit 2 (usage error), logs 'herdr CLI の引数が変わった可能性' and returns None.
    On other non-zero exit, returns None.
    Parses both stdout JSON and stderr JSON (herdr may put errors in stderr).
    Never raises.
    """
    cmd = _HERDR_CLI[cmd_key] + extra_args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        print(f"[mux:herdr] WARNING: {cmd_key} subprocess failed: {e}", file=sys.stderr)
        return None

    if r.returncode == 2:
        print(
            f"[mux:herdr] WARNING: herdr CLI の引数が変わった可能性 (exit 2): {' '.join(cmd)}",
            file=sys.stderr,
        )
        return None

    # Parse stdout as JSON first.
    if r.stdout.strip():
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            pass

    # Fall back to stderr JSON (herdr error responses).
    if r.stderr.strip():
        try:
            return json.loads(r.stderr)
        except json.JSONDecodeError:
            pass

    if r.returncode != 0:
        return None

    return {}


def _herdr_run_raw(cmd_key: str, extra_args: List[str], timeout: int = 10) -> Optional[str]:
    """Run a herdr CLI command and return raw stdout as a string.

    Use for commands whose stdout is plain text, not JSON (e.g. ``pane read``).
    ``herdr pane read <pane> --source visible`` outputs the pane content directly
    to stdout as plain text; trying to JSON-parse it would always fail.

    Returns the raw stdout string on success (exit 0), or None on error.
    Never raises.
    """
    cmd = _HERDR_CLI[cmd_key] + extra_args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        print(f"[mux:herdr] WARNING: {cmd_key} subprocess failed: {e}", file=sys.stderr)
        return None

    if r.returncode == 2:
        print(
            f"[mux:herdr] WARNING: herdr CLI の引数が変わった可能性 (exit 2): {' '.join(cmd)}",
            file=sys.stderr,
        )
        return None

    if r.returncode != 0:
        return None

    return r.stdout



# t007 (PR#189 レビュー指摘 F1, Seo 実測): verify_sent() が使う既定の 5 行では、
# 実ペイン (入力欄の下に下罫線/status/auto-mode の 3 行が常にあり、かつ
# kickoff 本文が日本語主体で表示幅 358 桁あるため狭い端末幅では折り返す) で
# ❯ 行が末尾 5 行の窓から押し出されてしまう (worker kickoff は端末幅 182 桁
# 以下、TARGET_DIR worker は 111 桁以下で壊れ始めることを実測)。send() の
# Enter insurance (既存の default 5) はそのままにし、verify_sent() 側だけ
# より広い窓 (_VERIFY_SENT_TAIL_LINES) を渡せるよう tail_lines を引数化する。
_DEFAULT_TAIL_LINES = 5
_VERIFY_SENT_TAIL_LINES = 30


def _text_in_input_line(screen: str, text: str, tail_lines: int = _DEFAULT_TAIL_LINES) -> bool:
    """Check if ``text`` appears in the active input line of the pane.

    Examines only the **last ``tail_lines`` lines** of the screen to avoid
    matching text that already appears in the scrollback (i.e. a previously
    executed command whose output is still visible).

    Detection order (TUI-first, falls back to plain bash):

    1. **Claude TUI mode**: scan the last ``tail_lines`` lines for any line
       that starts with ``❯``.  If found, return ``True`` only if the first
       30 characters of ``text`` appear in one of those lines.  (Claude TUI
       renders a status/hint line *below* the ``❯`` prompt, so the
       absolute-last line is unreliable.)

    2. **Plain bash / other**: if no ``❯`` line is found, check the last
       non-empty line.  Return ``True`` if the first 30 characters of
       ``text`` appear in it.

    **Why ``tail_lines`` is configurable (t007, F1)**: ``send()``'s own Enter
    insurance only needs to look a few lines back (the pane was just written
    to, so the ``❯`` line is near the bottom) and keeps the original default
    of 5 to preserve existing behavior. ``verify_sent()``, however, is called
    well after send() and against a pane that may show a TUI footer (status /
    auto-mode lines) below the input line plus multiple wrapped lines of a
    long kickoff message above it — both of which can push the ``❯`` line
    past a 5-line window on narrower terminals. Callers that need to see
    further back (currently only ``verify_sent()``) pass a larger value.

    **Why 30 characters instead of the full text**: notification messages
    sent to the director (e.g. "要求スキル ['review'] の Worker を起動して…")
    often exceed terminal column width and wrap across multiple display lines.
    Matching the entire string against a single line always fails for wrapped
    text, causing the Enter insurance to miss the case where Enter was dropped.
    Using a 30-character prefix is long enough to distinguish from unrelated
    scrollback entries while being short enough to fit on any prompt line.

    Returns ``False`` when ``text`` or ``screen`` is empty, or when the text
    prefix is not found in the detected input line(s).  This prevents the
    scrollback false-positive where an already-executed ``text`` command
    appears in the history and would cause ``text in screen`` to be always
    ``True``.
    """
    if not screen or not text:
        return False
    # Use only the first 30 chars so long text that wraps across columns still
    # matches the prompt line that carries the beginning of the input.
    prefix = text[:30]
    # Examine only the tail to avoid scrollback matches.
    tail = screen.splitlines()[-tail_lines:]
    # Claude TUI: prompt lines start with ❯.
    prompt_lines = [line for line in tail if line.startswith("❯")]
    if prompt_lines:
        return any(prefix in line for line in prompt_lines)
    # Plain bash / other: check last non-empty line.
    non_empty = [line for line in tail if line.strip()]
    if non_empty:
        return prefix in non_empty[-1]
    return False


def _herdr_server_env() -> dict:
    """Return a copy of os.environ with session-scoped variables removed.

    See _HERDR_SERVER_ENV_DENY_* for why.  A denylist (not a whitelist) is used
    so that things the panes legitimately need — SSH_AUTH_SOCK, XDG_*, WSL and
    proxy vars — survive.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in _HERDR_SERVER_ENV_DENY_EXACT
        and not k.startswith(_HERDR_SERVER_ENV_DENY_PREFIXES)
    }


def _herdr_start_server() -> None:
    """Start `herdr server` detached, without waiting for it.

    `herdr server` only self-daemonizes when its stdout/stderr are a TTY.  With
    a pipe or a file it holds the fd open and never returns, so running it via
    ``subprocess.run(capture_output=True, timeout=N)`` blocks for the full
    timeout and then *kills the server it just started* — auto-start could
    never succeed.  Spawn it in its own session with the streams on /dev/null
    and return immediately; herdr keeps its own log at
    ~/.config/herdr/herdr-server.log.  Never raises.
    """
    try:
        subprocess.Popen(
            _HERDR_CLI["server_start"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_herdr_server_env(),
        )
    except Exception:
        pass


def _herdr_ping() -> bool:
    """Send a NDJSON ping to herdr socket and return True on pong."""
    sock_path = str(_HERDR_SOCK_PATH)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(sock_path)
            s.sendall(b'{"id":"1","method":"ping","params":{}}\n')
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
        response = json.loads(data.decode().strip())
        return response.get("result", {}).get("type") == "pong"
    except Exception:
        return False


def _herdr_server_identity():
    """`(socket_path, "<pid>:<starttime>")` for the herdr server, or None.

    The generation is taken from the server process itself, over
    `SO_PEERCRED` on the connection we are about to use: the kernel names the
    process on the other end, so a restarted server — which restores tabs with
    fresh ids while every record still holds the old ones — is a different
    generation whatever the socket file did.  `starttime` is in there because
    a pid on its own can come round again.
    """
    sock_path = str(_HERDR_SOCK_PATH)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(sock_path)
            cred = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
            pid, _uid, _gid = struct.unpack("3i", cred)
    except Exception:
        return None
    if pid <= 0:
        return None
    status, fields = _proc_stat_fields(pid)
    if status != _PROC_OK or len(fields) < 20:
        return None
    return sock_path, f"{pid}:{fields[19]}"


class HerdrBackend(_Backend):
    """herdr terminal workspace manager backend (Phase 2).

    Workspace label: "crewvia" (overridden by CREWVIA_HERDR_WORKSPACE env).
    Tab ≡ tmux window; pane ≡ tmux pane.  Names are the same <Agent>-<role> labels.

    Cache: registry/mux/<name>.json → {tab_id, pane_id, backend, created_at}
    Cache is used for send/capture/pid.  list() always queries herdr live.
    kill() deletes the cache on success.

    Call order for spawn():
      1. Ensure server is available (available() = True).
      2. Resolve / create workspace.
      3. tab create → get root_pane.pane_id.
      4. pane rename <pane_id> <name>   (tab label does NOT propagate to pane, Phase 0).
      5. pane run <pane_id> <cmd>.
      6. Cache tab_id + pane_id.

    Call order for send():
      send() internally waits up to 5 s for '❯' to appear before calling
      pane run.  pane run with '❯' present submits reliably; without it,
      Enter is dropped (Phase 0 finding).  send() adds an Enter insurance:
      if capture() after pane run still shows the text prefix in the input
      line, one pane send-keys enter is appended.

    Herdr CLI subcommands are collected in _HERDR_CLI table (module level) so
    a CLI rename requires editing only that table.
    """

    BACKEND_NAME = "herdr"

    def server_identity(self):
        """`(endpoint, generation)` for the herdr server — see _Backend."""
        return _herdr_server_identity()

    def _cache_path(self, name: str) -> Path:
        return pane_record_path(name)

    def _guard(self, verb: str, name: str) -> None:
        _guard_test_isolation(self.BACKEND_NAME, verb, name,
                              self._workspace_label())

    def _write_cache(self, name: str, tab_id: str, pane_id: str) -> None:
        # The resolution cache and the spawn record are the same file: both
        # say "this checkout put a pane here, and here is its id".  Writing
        # them separately would mean two answers to one question.
        #
        # The server identity goes in with it: herdr restores a workspace's
        # tabs across a restart with *fresh* ids, so a record that did not say
        # which server it was written against would keep vouching for an id
        # the new server is free to give to another tab.
        write_pane_record(name, self.BACKEND_NAME, tab_id, pane_id=pane_id,
                          server=self.server_identity())

    def _read_cache(self, name: str) -> Optional[dict]:
        return read_pane_record(name)

    def _delete_cache(self, name: str) -> None:
        drop_pane_record(name)

    def _resolve_pane_id(self, name: str) -> Optional[str]:
        """Resolve pane_id for `name` — cache first, then live pane list.

        If cache exists but herdr says the pane is gone, drops cache and
        re-resolves via pane list.  Returns None if not found.
        """
        cached = self._read_cache(name)
        if cached:
            pane_id = cached.get("pane_id")
            # Verify cache is still live.
            data = _herdr_run("pane_get", [pane_id or ""], timeout=5)
            if data is not None and "result" in data:
                return pane_id
            if data is not None:
                # herdr answered, and the answer is that the pane is gone.
                self._delete_cache(name)
            # Otherwise we could not *ask*.  The record stays: dropping it on a
            # timeout would let a transient failure erase the only proof that
            # this checkout made the pane, and a missing record is a permanent
            # refusal at `may_destroy_pane()`.  Re-resolving by label below is
            # safe even so — the label finds *a* pane, and the destruction gate
            # compares that pane's id against the record rather than its name.

        # Live lookup via pane list.
        ws_id = self._workspace_id()
        if ws_id is None:
            return None
        data = _herdr_run("pane_list", ["--workspace", ws_id], timeout=10)
        if data is None:
            return None
        panes = data.get("result", {}).get("panes", [])
        for pane in panes:
            if pane.get("label") == _pane_name(name):
                return pane.get("pane_id")
        return None

    def _resolve_ids(self, name: str) -> Optional[dict]:
        """Return {tab_id, pane_id} for `name` using cache → live fallback."""
        cached = self._read_cache(name)
        if cached:
            pane_id = cached.get("pane_id")
            data = _herdr_run("pane_get", [pane_id or ""], timeout=5)
            if data is not None and "result" in data:
                return cached
            if data is not None:
                self._delete_cache(name)   # answered: the pane is gone
            # could not ask → keep the record; see _resolve_pane_id()

        # Live lookup.
        ws_id = self._workspace_id()
        if ws_id is None:
            return None
        data = _herdr_run("pane_list", ["--workspace", ws_id], timeout=10)
        if data is None:
            return None
        panes = data.get("result", {}).get("panes", [])
        for pane in panes:
            if pane.get("label") == _pane_name(name):
                return {
                    "tab_id": pane.get("tab_id"),
                    "pane_id": pane.get("pane_id"),
                    "backend": "herdr",
                }
        return None

    def _pane_process_state(self, pane_id: str) -> str:
        """`PANE_IDLE` / `PANE_LIVE` / `PANE_UNKNOWN` for `pane_id`.

        When the herdr server restarts it restores a workspace's tab layout but
        not the processes inside it, so panes keep their <Agent>-<role> label
        while holding nothing but a bare shell.  spawn() uses this to tell such
        a husk (safe to relaunch into) from a pane where an agent is still
        running — and, separately, to tell whether its own launch took.

        Every unreadable answer is `PANE_UNKNOWN`, which the two callers read
        in opposite directions: occupancy treats it as busy, launch
        verification as not-started.
        """
        data = _herdr_run("pane_process_info", [pane_id], timeout=10)
        if data is None:
            self._warn(f"process-info failed for {pane_id!r} — pane state unknown")
            return PANE_UNKNOWN
        try:
            procs = data["result"]["process_info"]["foreground_processes"]
        except (KeyError, TypeError):
            self._warn(
                f"process-info for {pane_id!r} has no foreground_processes "
                "— pane state unknown"
            )
            return PANE_UNKNOWN
        if not isinstance(procs, list):
            self._warn(
                f"process-info for {pane_id!r} returned a non-list "
                "foreground_processes — pane state unknown"
            )
            return PANE_UNKNOWN
        # Per entry, and in this order.  One demonstrably running process makes
        # the pane live whatever the rest are; only when nothing is live does
        # an unclassifiable entry matter, and then it is `unknown`, never
        # `live` — occupancy reads both as busy, but launch verification must
        # tell "it started" from "we could not see".
        #
        # An *empty* list stays `idle`: herdr answers a failure with no
        # `result` at all (caught above), so zero entries is a successful
        # "nothing in this pane", which is exactly the husk a server restart
        # leaves behind and which t035 requires spawn() to relaunch into.
        states = [_idle_shell_entry_state(p) for p in procs]
        if PANE_LIVE in states:
            return PANE_LIVE
        if PANE_UNKNOWN in states:
            self._warn(
                f"process-info for {pane_id!r} returned an entry that could not "
                "be classified — pane state unknown"
            )
            return PANE_UNKNOWN
        return PANE_IDLE

    def _pane_has_live_process(self, pane_id: str) -> bool:
        """True unless the pane is *demonstrably* an idle shell (see above)."""
        return self._pane_process_state(pane_id) != PANE_IDLE

    # ------------------------------------------------------------------
    # Server / workspace helpers
    # ------------------------------------------------------------------

    def _ensure_server(self) -> bool:
        """Ensure herdr server is running.  Start it if not, wait up to 10s."""
        if _herdr_ping():
            return True
        _herdr_start_server()

        deadline = time.time() + 10
        while time.time() < deadline:
            if _herdr_ping():
                return True
            time.sleep(0.3)
        return False

    def _workspace_label(self) -> str:
        return os.environ.get("CREWVIA_HERDR_WORKSPACE", "crewvia")

    def _workspace_id(self) -> Optional[str]:
        """Return the workspace id for label `crewvia` (or env override).

        Creates the workspace if it doesn't exist.
        """
        label = self._workspace_label()
        data = _herdr_run("workspace_list", [], timeout=10)
        if data is None:
            return None
        workspaces = data.get("result", {}).get("workspaces", [])
        for ws in workspaces:
            if ws.get("label") == label:
                return ws.get("workspace_id")

        # Create workspace.
        repo_root = os.environ.get(
            "CREWVIA_REPO_ROOT",
            str(Path(__file__).parent.parent),
        )
        create_data = _herdr_run(
            "workspace_create",
            ["--label", label, "--cwd", repo_root, "--no-focus"],
            timeout=10,
        )
        if create_data is None:
            return None
        ws = create_data.get("result", {}).get("workspace", {})
        return ws.get("workspace_id")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """True if herdr binary exists and server is running (or can be started).

        Also performs version guard — warns (does not stop) if version != 0.9.0.
        ``herdr --version`` outputs plain text (not JSON), so subprocess is used
        directly rather than _herdr_run().
        """
        if shutil.which("herdr") is None:
            return False

        # Version guard (plain-text output — not JSON).
        try:
            r = subprocess.run(
                _HERDR_CLI["version"], capture_output=True, text=True, timeout=5
            )
            ver_line = (r.stdout + r.stderr).strip()
            # Expected: "herdr 0.9.0"
            ver = ver_line.split()[-1] if ver_line else ""
            if ver and ver != _HERDR_VERIFIED_VERSION:
                self._warn(
                    f"herdr version mismatch: expected {_HERDR_VERIFIED_VERSION}, got {ver!r}. "
                    "CLI interface may have changed."
                )
        except Exception:
            pass

        return self._ensure_server()

    def server_running(self) -> bool:
        """True if the herdr server answers a ping right now.

        Unlike available(), this never starts the server — callers use it to
        tell 'already running' from 'we had to start it' for the user.
        """
        return _herdr_ping()

    def spawn(self, name: str, cmd: str, cwd: Optional[str] = None,
              env: Optional[dict] = None) -> bool:
        """Create a herdr tab named `name` and run `cmd`.

        Steps:
          1. Resolve workspace.
          2. tab create → root_pane.pane_id.
          3. pane rename <pane_id> <name>  (label does not auto-propagate).
          4. pane run <pane_id> <cmd>.
          5. Cache tab_id + pane_id.

        Returns False (without error) if a pane with this name already exists
        and still runs a live agent.  If the pane exists but holds nothing but
        an idle shell — the husk a herdr server restart leaves behind — the
        command is relaunched into that pane and True is returned.
        """
        self._guard("spawn", name)
        ws_id = self._workspace_id()
        if ws_id is None:
            self._warn(f"spawn {name!r}: could not resolve workspace")
            return False

        # Check for existing pane with this name.
        existing = _herdr_run("pane_list", ["--workspace", ws_id], timeout=10)
        if existing is not None:
            panes = existing.get("result", {}).get("panes", [])
            for pane in panes:
                if pane.get("label") != _pane_name(name):
                    continue
                existing_pane_id = pane.get("pane_id") or ""
                if not existing_pane_id or self._pane_has_live_process(existing_pane_id):
                    return False  # Live agent in there → no-op
                # Husk pane: herdr restored the label but not the process.
                # Relaunching in place keeps the tab (and its position) and is
                # what makes ./crewvia recover on its own after a server
                # restart — before this, spawn reported "already exists" and
                # start.sh went on to launch nothing at all.
                self._warn(
                    f"spawn {name!r}: pane {existing_pane_id} is an idle shell "
                    "(herdr restart?) — relaunching in place"
                )
                if _herdr_run("pane_run", [existing_pane_id, cmd], timeout=10) is None:
                    self._warn(f"spawn {name!r}: pane run failed on reused pane")
                    return False
                if not _wait_until_launched(
                        lambda: self._pane_process_state(existing_pane_id),
                        warn=self._warn, name=name):
                    return False
                self._write_cache(name, pane.get("tab_id") or "", existing_pane_id)
                return True

        # tab create.
        tab_args = ["--workspace", ws_id, "--label", _pane_name(name), "--no-focus"]
        if cwd:
            tab_args += ["--cwd", cwd]
        tab_data = _herdr_run("tab_create", tab_args, timeout=10)
        if tab_data is None:
            self._warn(f"spawn {name!r}: tab create failed")
            return False

        tab = tab_data.get("result", {}).get("tab", {})
        tab_id = tab.get("tab_id", "")
        root_pane = tab_data.get("result", {}).get("root_pane", {})
        pane_id = root_pane.get("pane_id", "")

        if not pane_id:
            self._warn(f"spawn {name!r}: could not get pane_id from tab create result")
            return False

        # pane rename (Phase 0: tab label does NOT propagate to pane label).
        _herdr_run("pane_rename", [pane_id, _pane_name(name)], timeout=5)

        # pane run.
        run_data = _herdr_run("pane_run", [pane_id, cmd], timeout=10)
        if run_data is None:
            self._warn(f"spawn {name!r}: pane run failed")
            return False

        # Cache ids.
        self._write_cache(name, tab_id, pane_id)
        return True

    def send(self, name: str, text: str) -> bool:
        """Send `text` to the named pane via pane run.

        Waits up to ``_SEND_PROMPT_TIMEOUT`` seconds for the ``❯`` prompt to
        appear before calling ``pane run``.  Phase 0 confirmed: ``pane run``
        submits reliably only after ``❯`` is visible; without it, Enter is
        dropped and the text sits in the input buffer unsent.

        If ``❯`` does not appear within the timeout (pane is busy with a long
        task), ``pane run`` is called anyway as a best-effort — the text is
        inserted but Enter may be dropped.

        Enter insurance: after ``pane run``, ``capture()`` is called once.
        ``_text_in_input_line()`` checks the **last 5 lines** for the first
        30 characters of ``text`` to detect whether Enter was dropped despite
        the wait (e.g., timeout fired or pane state changed mid-send).
        Searching the full screen caused a false-positive: a
        previously-executed command with the same text would appear in the
        scrollback and always trigger a spurious extra Enter.

        See ``_text_in_input_line()`` for the exact detection algorithm
        (Claude TUI ``❯`` lines vs. plain bash last-line fallback, 30-char
        prefix matching).

        Clears the input line (``ctrl+u``) immediately before ``pane_run``
        (t007, PR#189 レビュー指摘 F3): a caller that retries after
        `verify_sent()` reports the previous attempt as not-landed would
        otherwise type on top of the still-present text, concatenating
        messages ("ミッション開始…ミッション開始…"). Clearing first is a no-op
        when the line is already empty, so this is safe on a first attempt
        too.
        """
        self._guard("send", name)
        _SEND_PROMPT_TIMEOUT = 5.0   # seconds to wait for '❯'
        _SEND_PROMPT_INTERVAL = 0.5  # poll interval in seconds

        ids = self._resolve_ids(name)
        if ids is None:
            self._warn(f"send {name!r}: pane not found")
            return False
        pane_id = ids["pane_id"]

        # Wait for '❯' prompt so pane_run can submit with Enter (Phase 0).
        deadline = time.time() + _SEND_PROMPT_TIMEOUT
        while True:
            screen = self._capture_by_pane_id(pane_id)
            tail = screen.splitlines()[-5:] if screen else []
            if any(line.startswith("❯") for line in tail):
                break  # prompt ready — pane_run will type and press Enter
            if time.time() >= deadline:
                self._warn(
                    f"send {name!r}: '❯' not found after "
                    f"{_SEND_PROMPT_TIMEOUT:.0f}s (pane busy) — sending best-effort"
                )
                break
            time.sleep(_SEND_PROMPT_INTERVAL)

        # t007 F3: clear any leftover input before typing (see docstring).
        _herdr_run("pane_send_keys", [pane_id, "ctrl+u"], timeout=5)

        run_data = _herdr_run("pane_run", [pane_id, text], timeout=10)
        if run_data is None:
            self._warn(f"send {name!r}: pane run failed")
            return False

        # Enter insurance: check only the input line, not the full scrollback.
        time.sleep(0.1)
        screen = self._capture_by_pane_id(pane_id)
        if _text_in_input_line(screen, text):
            # Text still in input line — Enter was not submitted, append it.
            _herdr_run("pane_send_keys", [pane_id, "enter"], timeout=5)

        return True

    def _capture_by_pane_id(self, pane_id: str) -> str:
        """Internal: capture screen by pane_id directly (no name lookup).

        ``herdr pane read <pane> --source visible`` writes plain text (the
        pane's visible content) to stdout — NOT JSON.  Use _herdr_run_raw()
        so the raw stdout is returned as-is instead of being JSON-parsed into
        an empty dict.
        """
        text = _herdr_run_raw("pane_read", [pane_id], timeout=10)
        if text is None:
            return ""
        return text

    def capture(self, name: str) -> str:
        """Return the current visible screen contents of the named pane."""
        self._guard("capture", name)
        ids = self._resolve_ids(name)
        if ids is None:
            self._warn(f"capture {name!r}: pane not found")
            return ""
        return self._capture_by_pane_id(ids["pane_id"])

    def list(self, suffix: Optional[str] = None) -> List[str]:
        """Return names of live panes in the workspace, optionally filtered by suffix.

        Always queries herdr live (does not use cache) for accurate liveness.
        """
        ws_id = self._workspace_id()
        if ws_id is None:
            return []
        data = _herdr_run("pane_list", ["--workspace", ws_id], timeout=10)
        if data is None:
            return []
        panes = data.get("result", {}).get("panes", [])
        prefix = _pane_prefix()
        names = [_caller_name(p["label"]) for p in panes
                 if p.get("label") and p["label"].startswith(prefix)]
        if suffix:
            names = [n for n in names if n.endswith(suffix)]
        return names

    def kill(self, name: str, *, allow_foreign: bool = False) -> bool:
        """Close the tab for the named pane (terminates claude and children).

        Phase 0: tab close terminates all child processes via SIGHUP within 2s.
        Cache is deleted on success.

        `allow_foreign=True` lifts the identity backstop — see TmuxBackend.kill.
        """
        self._guard("kill", name)
        if allow_foreign:
            self._warn_forced_bypass(name)
        if not allow_foreign and name in DAEMON_PANE_NAMES:
            # The tab that was inspected is the tab that gets closed.  Calling
            # `_resolve_ids()` again here would re-resolve *by label*, and a
            # label is exactly what another checkout can attach to a different
            # tab while the /proc walk runs.
            tab_id, pane_pid, server = self._inspect_pane_full(name)
            if self._refuses_foreign_daemon(name, tab_id, pane_pid, server):
                return False
            if not tab_id:
                self._warn(
                    f"kill {name!r}: refused — the tab that was inspected "
                    f"could not be identified. Nothing was killed.")
                return False
        else:
            ids = self._resolve_ids(name)
            if ids is None:
                self._warn(f"kill {name!r}: pane not found")
                return False
            tab_id = ids.get("tab_id")
            if not tab_id:
                self._warn(f"kill {name!r}: tab_id not found in cache/live")
                return False

        data = _herdr_run("tab_close", [tab_id], timeout=10)
        if data is None:
            return False

        self._delete_cache(name)
        return True

    def _inspect_pane(self, name: str):
        """`(tab_id, pane_pid)` from one resolution — see _Backend."""
        self._guard("pid", name)
        ids = self._resolve_ids(name)
        if ids is None:
            self._warn(f"pid {name!r}: pane not found")
            return None, None
        pane_id = ids.get("pane_id")
        tab_id = ids.get("tab_id")
        if not pane_id:
            self._warn(f"pid {name!r}: pane_id not found in cache/live")
            return None, None

        data = _herdr_run("pane_process_info", [pane_id], timeout=10)
        if data is None:
            self._warn(f"pid {name!r}: pane process-info failed")
            return tab_id, None
        try:
            shell_pid = data["result"]["process_info"]["shell_pid"]
            return tab_id, int(shell_pid)
        except (KeyError, TypeError, ValueError) as e:
            self._warn(f"pid {name!r}: could not parse shell_pid: {e}")
            return tab_id, None

    def pid(self, name: str) -> Optional[int]:
        """Return the shell PID of the named pane via pane process-info."""
        return self._inspect_pane(name)[1]

    def attach(self, name: str) -> bool:
        """Attach / focus the named pane's tab (called when already inside herdr).

        If HERDR_ENV=1 (already inside herdr), use tab focus to bring the pane
        into view.  When HERDR_ENV is not set, the caller should have used
        attach_cmd() + exec instead — this path returns False.

        Returns True on success, False on error.
        """
        if os.environ.get("HERDR_ENV") == "1":
            ids = self._resolve_ids(name)
            if ids is None:
                self._warn(f"attach {name!r}: pane not found")
                return False
            tab_id = ids.get("tab_id")
            if not tab_id:
                return False
            data = _herdr_run("tab_focus", [tab_id], timeout=5)
            return data is not None
        # Not inside herdr — attach_cmd() + exec should have been used.
        return False

    def attach_cmd(self, name: str) -> Optional[List[str]]:
        """Return the argv to exec in order to enter herdr from outside.

        - Inside herdr (HERDR_ENV=1): return None → caller uses attach() (tab focus).
        - Outside herdr: pre-focus the target tab so the user lands on it when
          herdr opens, then return ['herdr'] so the bash caller can exec it.
        """
        if os.environ.get("HERDR_ENV") == "1":
            return None
        # Focus the target tab before returning — so exec 'herdr' lands on it.
        ids = self._resolve_ids(name)
        if ids:
            tab_id = ids.get("tab_id")
            if tab_id:
                _herdr_run("tab_focus", [tab_id], timeout=5)
        return ["herdr"]

    def state(self, name: str) -> str:
        """Return the agent state for the named pane.

        Uses ``herdr pane get <pane_id>`` and reads ``.result.pane.agent_status``.
        Cache is used first (same re-resolution as other verbs).

        Returns one of: "blocked", "working", "idle", "done", "unknown".
        Returns "unknown" on any error (fail-safe: caller skips notification).
        """
        pane_id = self._resolve_pane_id(name)
        if pane_id is None:
            self._warn(f"state {name!r}: pane not found")
            return "unknown"
        data = _herdr_run("pane_get", [pane_id], timeout=5)
        if data is None:
            self._warn(f"state {name!r}: pane get failed")
            return "unknown"
        try:
            status = data["result"]["pane"]["agent_status"]
            if status in ("blocked", "working", "idle", "done", "unknown"):
                return status
            # Completely unexpected value → treat as unknown (safe side).
            self._warn(f"state {name!r}: unexpected agent_status={status!r} → unknown")
            return "unknown"
        except (KeyError, TypeError):
            # agent_status field absent (older herdr or non-Claude pane) → unknown.
            return "unknown"


# ---------------------------------------------------------------------------
# Public Mux facade
# ---------------------------------------------------------------------------

class Mux:
    """Public facade — delegates to the selected backend.

    Usage:
      m = Mux()                        # auto-select backend
      m = Mux(backend=TmuxBackend())   # explicit backend (for testing)
    """

    def __init__(self, backend: Optional[_Backend] = None):
        if backend is not None:
            self._backend = backend
        else:
            cls = _select_backend()
            self._backend = cls()

    def available(self) -> bool:
        return self._backend.available()

    def server_running(self) -> bool:
        return self._backend.server_running()

    def spawn(self, name: str, cmd: str, cwd: Optional[str] = None,
              env: Optional[dict] = None) -> bool:
        return self._backend.spawn(name, cmd, cwd=cwd, env=env)

    def send(self, name: str, text: str) -> bool:
        return self._backend.send(name, text)

    def capture(self, name: str) -> str:
        return self._backend.capture(name)

    def list(self, suffix: Optional[str] = None) -> List[str]:
        return self._backend.list(suffix=suffix)

    def kill(self, name: str, *, allow_foreign: bool = False) -> bool:
        return self._backend.kill(name, allow_foreign=allow_foreign)

    def pid(self, name: str) -> Optional[int]:
        return self._backend.pid(name)

    def attach(self, name: str) -> bool:
        return self._backend.attach(name)

    def attach_cmd(self, name: str) -> Optional[List[str]]:
        return self._backend.attach_cmd(name)

    def state(self, name: str) -> str:
        return self._backend.state(name)

    def verify_sent(self, name: str, text: str) -> bool:
        """Return True if `text` has left the pane's active input line.

        `send()` on both backends is best-effort: TmuxBackend.send() never
        confirms delivery at all, and HerdrBackend.send() only patches a
        dropped Enter — neither actually proves the caller's message reached
        Claude.  This re-captures the pane and re-runs the same
        `_text_in_input_line()` heuristic `send()` uses internally for its
        Enter insurance, so callers (start.sh kickoff) can detect a message
        that never left the input line and retry instead of trusting the
        `True` that `send()` already returned.

        Uses a wider ``tail_lines`` window than send()'s own Enter insurance
        (see ``_VERIFY_SENT_TAIL_LINES`` / t007 F1) since a real pane has a
        multi-line TUI footer below the input line and a long kickoff message
        may wrap across several lines above it, both of which can push the
        ``❯`` prompt line out of a narrow tail window on smaller terminals.

        Returns True when `text` is NOT sitting unsent in the input line
        (i.e. it was submitted, or the pane never had it to begin with).
        Returns False when it is still stuck there, OR when the pane could
        not be captured at all (pane not found / backend error / timeout —
        t007 F2) — the caller should retry in either case. Capture failure
        must never be reported as "verified": `capture()` returns `""` both
        when the pane is genuinely empty and when it could not be reached at
        all, and there is no way to tell those apart here — so an empty
        capture is treated as "cannot confirm delivery", not as "landed".
        """
        screen = self.capture(name)
        if not screen:
            return False
        return not _text_in_input_line(screen, text, tail_lines=_VERIFY_SENT_TAIL_LINES)


# ---------------------------------------------------------------------------
# CLI entry point (for bash callers via lib_mux.sh)
# ---------------------------------------------------------------------------

def _cli_main(args: List[str]) -> int:
    if not args:
        print("Usage: lib_mux.py <verb> [args...]", file=sys.stderr)
        return 2

    verb = args[0]
    rest = args[1:]
    m = Mux()

    if verb == "available":
        return 0 if m.available() else 1

    elif verb == "server-running":
        return 0 if m.server_running() else 1

    elif verb == "spawn":
        if len(rest) < 2:
            print("Usage: lib_mux.py spawn <name> <cmd> [<cwd>]", file=sys.stderr)
            return 2
        name, cmd = rest[0], rest[1]
        cwd = rest[2] if len(rest) >= 3 else None
        return 0 if m.spawn(name, cmd, cwd=cwd) else 1

    elif verb == "send":
        if len(rest) < 2:
            print("Usage: lib_mux.py send <name> <text>", file=sys.stderr)
            return 2
        name, text = rest[0], " ".join(rest[1:])
        return 0 if m.send(name, text) else 1

    elif verb == "capture":
        if not rest:
            print("Usage: lib_mux.py capture <name>", file=sys.stderr)
            return 2
        output = m.capture(rest[0])
        sys.stdout.write(output)
        return 0

    elif verb == "list":
        suffix = rest[0] if rest else None
        names = m.list(suffix=suffix)
        for n in names:
            print(n)
        return 0

    elif verb == "kill":
        force = "--force" in rest
        rest = [a for a in rest if a != "--force"]
        if not rest:
            print("Usage: lib_mux.py kill <name> [--force]", file=sys.stderr)
            return 2
        return 0 if m.kill(rest[0], allow_foreign=force) else 1

    elif verb == "pid":
        if not rest:
            print("Usage: lib_mux.py pid <name>", file=sys.stderr)
            return 2
        p = m.pid(rest[0])
        if p is None:
            return 1
        print(p)
        return 0

    elif verb == "attach":
        if not rest:
            print("Usage: lib_mux.py attach <name>", file=sys.stderr)
            return 2
        return 0 if m.attach(rest[0]) else 1

    elif verb == "attach-cmd":
        if not rest:
            print("Usage: lib_mux.py attach-cmd <name>", file=sys.stderr)
            return 2
        cmd = m.attach_cmd(rest[0])
        if cmd is not None:
            # Print one argument per line so bash callers can reconstruct the argv.
            for arg in cmd:
                print(arg)
        # Always exit 0 — empty output means "use attach() instead".
        return 0

    elif verb == "state":
        if not rest:
            print("Usage: lib_mux.py state <name>", file=sys.stderr)
            return 2
        print(m.state(rest[0]))
        return 0

    elif verb == "verify-sent":
        if len(rest) < 2:
            print("Usage: lib_mux.py verify-sent <name> <text>", file=sys.stderr)
            return 2
        name, text = rest[0], " ".join(rest[1:])
        return 0 if m.verify_sent(name, text) else 1

    elif verb == "identity-ok":
        if not rest:
            print("Usage: lib_mux.py identity-ok <repo_root>", file=sys.stderr)
            return 2
        return 0 if repo_identity_ok(rest[0]) else 1

    else:
        print(f"Unknown verb: {verb!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(_cli_main(sys.argv[1:]))
    except MuxTestIsolationError as exc:
        # One readable line, not a traceback: the refusal is the message, and
        # every caller of this CLI reads stderr rather than a Python stack.
        print(str(exc), file=sys.stderr)
        sys.exit(MUX_TEST_ISOLATION_EXIT)

