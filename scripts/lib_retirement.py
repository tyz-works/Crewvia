#!/usr/bin/env python3
"""
scripts/lib_retirement.py — Worker retirement marker protocol (t002).

Shared by the two daemons that used to kill Workers independently of each
other.  After t002 the split is:

    judgement ("this Worker has no work left")  → dispatcher.sh
    execution ("end this process")              → watchdog.py

dispatcher.sh writes a *request*; watchdog.py drives it through a phase
machine and executes it.  See `knowledge/daemon-authority.md` §3-3 for why a
marker file beat the two alternatives (task frontmatter has no file to write
to for an idle Worker; `queue/assignments/<agent>` carries an
"exists = busy" invariant that five other call sites depend on).

## Files

    registry/retirements/<agent>.json           the request: written once, never updated
    registry/retirements/<agent>.progress.json  the phase machine's state

**One writer per file.**  Splitting request from progress removes the need for
locking between the daemons: the request is written exactly once by whoever
asked for the retirement (dispatcher for the idle / no-task / blocked-stuck
paths, watchdog itself for its own timeout terminates) and is never modified
afterwards, while every subsequent update goes to the progress file, which only
watchdog writes.  Both are written temp + os.replace so a reader never observes
a half-written JSON document.

Both files are deleted by watchdog once the retirement settles.  The design
memo (§R4) had dispatcher do that deletion after notifying a human to run
`plan.sh update --reset` by hand; the task instead requires watchdog to *finish*
the cleanup itself and report it after the fact, so the whole lifecycle after
the request now belongs to one daemon.  That is what makes the cleanup survive
dispatcher being down, which was the point of E1.  Dispatcher's independent D4
detection is deliberately left in place as a backstop (R5).

## Phases (progress.json)

    notified      shutdown message sent; deadline = when to escalate to SIGTERM
    sigterm_sent  SIGTERM delivered;     deadline = when to escalate to SIGKILL
    terminated    process is gone; queue-side cleanup still owed
    discarded     identity re-check failed — deliberately NOT killed (R7)
    cleanup_failed terminated, but plan.sh reset kept failing; left for a human

The phase is written to disk *before* the destructive step it authorises
(R1), so a daemon restart mid-termination can always tell what it already
did.  No phase transition blocks: each watchdog cycle advances every marker
by at most one step and returns (R2).

## Instance identity (R7)

`repo_identity_ok()` answers "am I the right daemon?".  This module answers
"is the window in front of me still the same Worker instance the marker was
written for?".  crewvia reuses Worker names (Haruto / Seo / Arjun ...), so a
marker that outlives its Worker would otherwise land on an innocent
same-named successor — the single most damaging failure mode in
`knowledge/daemon-authority.md` §5-2.  Every destructive step re-checks, and
every ambiguous answer (mux unreachable, no usable recorded field) is a
*mismatch*, i.e. "do not kill".
"""

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

PHASE_NOTIFIED = "notified"
PHASE_SIGTERM_SENT = "sigterm_sent"
PHASE_TERMINATED = "terminated"
PHASE_DISCARDED = "discarded"
PHASE_CLEANUP_FAILED = "cleanup_failed"

#: Phases where a Worker may still be alive and the machine owes it a step.
IN_FLIGHT_PHASES = frozenset({PHASE_NOTIFIED, PHASE_SIGTERM_SENT, PHASE_TERMINATED})
#: Phases where nothing further will happen on its own.
SETTLED_PHASES = frozenset({PHASE_DISCARDED, PHASE_CLEANUP_FAILED})

#: Message dispatcher used to send itself before killing; now carried in the
#: marker so the *executor* sends it exactly once.
SHUTDOWN_MESSAGE = "タスクなし、shutdown"

_REQUEST_SUFFIX = ".json"
_PROGRESS_SUFFIX = ".progress.json"

#: created_at values come from two different sources (an ISO string rounded to
#: whole seconds on herdr, a float epoch on tmux), so compare with a tolerance
#: rather than for exact equality.
_CREATED_AT_TOLERANCE = 1.5

#: Seconds a SIGKILL step spends confirming that the process actually went
#: away.  Signal delivery is not death: `os.kill` returns as soon as the
#: signal is queued, and the terminal phase authorises a queue reset, so it
#: must not be reached on the strength of a syscall that merely succeeded.
#: Bounded on purpose (R2) — an unconfirmed kill simply stays in flight and is
#: re-checked next cycle rather than blocking the daemon.
KILL_CONFIRM_WINDOW = 1.0

#: SIGKILL attempts to make before telling the Director the Worker will not
#: die.  Retrying is right (a transient EPERM or a process in uninterruptible
#: sleep may clear), but retrying *silently* forever would turn "we refused to
#: assume a death" into a Worker squatting with nobody aware of it.
SIGKILL_REPORT_AFTER = 3

#: `plan.sh update` exit status for "the precondition did not hold".  Distinct
#: from 1 (plan.sh is broken → retry) so cleanup can tell the two apart.
PLAN_PRECONDITION_UNMET = 3


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def retirements_dir(registry_dir) -> Path:
    return Path(registry_dir) / "retirements"


def request_path(registry_dir, agent: str) -> Path:
    return retirements_dir(registry_dir) / f"{agent}{_REQUEST_SUFFIX}"


def progress_path(registry_dir, agent: str) -> Path:
    return retirements_dir(registry_dir) / f"{agent}{_PROGRESS_SUFFIX}"


def list_agents(registry_dir) -> list:
    """Every agent name with a request and/or a progress marker on disk."""
    d = retirements_dir(registry_dir)
    agents = set()
    try:
        entries = list(d.iterdir())
    except OSError:
        return []
    for p in entries:
        if not p.is_file():
            continue
        if p.name.endswith(_PROGRESS_SUFFIX):
            agents.add(p.name[: -len(_PROGRESS_SUFFIX)])
        elif p.name.endswith(_REQUEST_SUFFIX):
            agents.add(p.name[: -len(_REQUEST_SUFFIX)])
    return sorted(agents)


# ---------------------------------------------------------------------------
# Atomic JSON I/O
# ---------------------------------------------------------------------------

def write_json_atomic(path, data: dict) -> bool:
    """Write `data` as JSON via temp + os.replace.

    The other daemon polls these files on its own schedule and must never see
    a partially written document.  Returns False (never raises) on I/O error —
    callers decide what a failed write means; for destructive steps it always
    means "do not proceed" (R1: the intent must be durable *first*).
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def read_json(path) -> Optional[dict]:
    """Return the parsed document, or None if missing / unreadable / corrupt."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def unlink_quiet(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def process_alive(pid) -> bool:
    """True only while `pid` names a live, non-zombie process.

    Used to tell "this Worker instance is gone" apart from "the mux backend
    cannot answer right now" — a distinction the phase machine cannot get from
    the window list alone, because a Worker that exits between the cycle's
    window snapshot and the identity re-check looks like *a different
    instance wearing the same name* (recorded pane_pid, currently unreadable).
    Discarding on that reading would skip the queue cleanup and leave exactly
    the ghost task this module exists to prevent.  A pid is unambiguous: the
    kernel answers without the backend's involvement.

    A zombie counts as gone: it has exited, and only its parent's wait() is
    outstanding.
    """
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        # No /proc (or no permission to read it): fall back to signal 0, where
        # PermissionError proves the process exists.
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True  # cannot tell → assume alive (do not authorise cleanup)
    try:
        return stat[stat.rindex(")") + 2] != "Z"
    except (ValueError, IndexError):
        return True


# ---------------------------------------------------------------------------
# Spawn identity
# ---------------------------------------------------------------------------

def created_at_from_cache(registry_dir, window_target: str) -> Optional[float]:
    """Spawn epoch for `window_target`, read-only, backend agnostic.

    herdr writes `registry/mux/<target>.json` with an ISO `created_at` on
    every spawn; tmux has no such cache, so dispatcher's
    `_spawn_time_fallback()` records `registry/mux/<target>.firstseen`
    instead.  This reads whichever exists and **never creates either** —
    `registry/mux/` belongs to dispatcher, and watchdog calling this must not
    become a second writer there (`knowledge/daemon-authority.md` §3-4 F1).
    """
    mux_dir = Path(registry_dir) / "mux"
    data = read_json(mux_dir / f"{window_target}.json")
    if data:
        ts = data.get("created_at")
        if ts:
            try:
                dt = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                pass
    try:
        return float((mux_dir / f"{window_target}.firstseen").read_text().strip())
    except (OSError, ValueError):
        return None


def current_spawn_identity(registry_dir, mux, window_target: str) -> dict:
    """Snapshot of "which instance currently owns this window name".

    Both fields may independently be None (tmux has no created_at cache until
    dispatcher writes one; pane_pid is None when the backend is unreachable or
    the window is gone).  `identity_matches()` treats "both None" as a
    mismatch, so an unreachable backend can never authorise a kill.
    """
    try:
        pane_pid = mux.pid(window_target)
    except Exception:
        pane_pid = None
    return {
        "pane_pid": int(pane_pid) if pane_pid is not None else None,
        "created_at": created_at_from_cache(registry_dir, window_target),
    }


def identity_matches(recorded: Optional[dict], current: Optional[dict]) -> Tuple[bool, str]:
    """(matches, reason).  `reason` is empty on a match, human-readable otherwise.

    Fails closed in three distinct ways, all meaning "do not kill":
      - nothing was recorded, or nothing recorded is non-null → nothing to
        prove identity against;
      - nothing is currently readable → the backend cannot be queried, so a
        "match" would be vacuous;
      - any recorded non-null field disagrees with its current value → this is
        a different instance wearing the same window name.
    """
    if not isinstance(recorded, dict):
        return False, "no recorded spawn_identity"
    if not isinstance(current, dict):
        return False, "no current spawn_identity"
    if current.get("pane_pid") is None and current.get("created_at") is None:
        return False, "current spawn_identity unavailable (mux unreachable or window gone)"

    compared = 0
    for key in ("pane_pid", "created_at"):
        rec = recorded.get(key)
        if rec is None:
            continue
        cur = current.get(key)
        if cur is None:
            return False, f"{key}: recorded={rec!r} but currently unreadable"
        if key == "created_at":
            try:
                if abs(float(cur) - float(rec)) > _CREATED_AT_TOLERANCE:
                    return False, f"created_at: recorded={rec!r} current={cur!r}"
            except (TypeError, ValueError):
                return False, f"created_at: uncomparable recorded={rec!r} current={cur!r}"
        else:
            try:
                if int(cur) != int(rec):
                    return False, f"pane_pid: recorded={rec!r} current={cur!r}"
            except (TypeError, ValueError):
                return False, f"pane_pid: uncomparable recorded={rec!r} current={cur!r}"
        compared += 1

    if compared == 0:
        return False, "recorded spawn_identity has no non-null field to compare"
    return True, ""


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------

def build_request(agent: str, window_target: str, reason: str, identity: dict,
                  mission: Optional[str] = None, task_id: Optional[str] = None,
                  message: str = SHUTDOWN_MESSAGE) -> dict:
    """A dispatcher-side retirement request.

    `mission` / `task_id` are None for the idle / no-task / blocked-stuck
    paths — by definition those Workers hold neither an assignment nor an
    in_progress task, which is precisely why the request cannot live in task
    frontmatter (§3-3 案A).
    """
    return {
        "agent": agent,
        "window_target": window_target,
        "reason": reason,
        "message": message,
        "requested_at": time.time(),
        "mission": mission,
        "task_id": task_id,
        "spawn_identity": dict(identity),
    }


def build_progress(previous: Optional[dict], phase: str, **fields) -> dict:
    """Next progress document: carry forward what we knew, overwrite what changed.

    Merging rather than rebuilding matters for the fields a later phase still
    needs but no longer recomputes — `pane_pid` recorded at SIGTERM time is
    what proves, after a restart, which process the machine had already
    signalled.
    """
    doc = dict(previous or {})
    doc.update(fields)
    doc["phase"] = phase
    doc["updated_at"] = time.time()
    for key, default in (
        ("deadline", None),
        ("pane_pid", None),
        ("window_gone", False),
        ("discard_reason", None),
        ("cleanup_error", None),
        ("cleanup_attempts", 0),
        ("director_notified", False),
        ("sigkill_attempts", 0),
        ("sigkill_report_sent", False),
    ):
        doc.setdefault(key, default)
    return doc


# ---------------------------------------------------------------------------
# Guard verdicts
# ---------------------------------------------------------------------------

#: Destructive step may proceed.
GUARD_OK = "ok"
#: Something about *this daemon* is wrong (repo identity).  Change nothing and
#: retry next cycle — the marker is still valid, we just cannot act right now.
GUARD_SKIP = "skip"
#: Something about *the target* is wrong (different instance, unreadable
#: identity).  The marker can never become valid again; settle it as discarded
#: so it stops pointing at an innocent same-named successor.
GUARD_DISCARD = "discard"


def _default_run_command(argv: list, env: dict) -> Tuple[int, str]:
    import subprocess
    try:
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=120,
        )
    except Exception as e:  # noqa: BLE001 — a failed cleanup must never crash the daemon
        return 1, f"{type(e).__name__}: {e}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _default_kill_process(pid: int, sig: int) -> bool:
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return True      # already gone — the outcome we wanted
    except OSError:
        return False


class RetirementExecutor:
    """Drives retirement markers through their phases, one step per cycle.

    Every dependency that touches the outside world is injected so the phase
    machine can be unit-tested without a mux backend, a real Worker or a real
    queue.  The daemon passes the real ones.

    `repo_identity_check` has **no default on purpose**: it is the fail-closed
    guard that stops a watchdog running out of a removed worktree from killing
    anything, and a default would let a caller disable it by forgetting it.
    """

    def __init__(self, registry_dir, repo_root, mux, repo_identity_check, *,
                 log=None, notify=None, plan_sh=None, queue_dir=None,
                 grace_period: int = 60, kill_delay: int = 10,
                 run_command=None, kill_process=None, now=None) -> None:
        self.registry_dir = Path(registry_dir)
        self.repo_root = Path(repo_root)
        self.mux = mux
        self.repo_identity_check = repo_identity_check
        self.log = log or (lambda _msg: None)
        self.notify = notify
        self.plan_sh = Path(plan_sh) if plan_sh else None
        self.queue_dir = Path(queue_dir) if queue_dir else None
        self.grace_period = grace_period
        self.kill_delay = kill_delay
        self.run_command = run_command or _default_run_command
        self.kill_process = kill_process or _default_kill_process
        self.now = now or time.time
        self._live_windows: set = set()
        self._listing_authoritative = False
        self._unavailable_logged_at = 0.0
        self._waiting_logged_at: dict = {}

    # ------------------------------------------------------------------
    # Requesting (called by dispatcher via CLI, and by watchdog for W2)
    # ------------------------------------------------------------------

    def has_marker(self, agent: str) -> bool:
        """True if a retirement is already requested or in flight for `agent`.

        Both callers need this before asking for a new one: a second request
        written while the machine is mid-termination would reset the phase and
        restart the whole 70s escalation from the top, indefinitely.
        """
        return (request_path(self.registry_dir, agent).exists()
                or progress_path(self.registry_dir, agent).exists())

    def request(self, agent: str, window_target: str, reason: str,
                mission: Optional[str] = None, task_id: Optional[str] = None,
                message: str = SHUTDOWN_MESSAGE) -> bool:
        """Write a retirement request.  False = not written (and not owed).

        Refuses when the current window has no usable identity at all, because
        a marker without one can never pass R7 later — it would be written only
        to be discarded, and in the meantime it would block the `has_marker()`
        guard above.  Refusing is the fail-closed direction: the Worker
        lingers (§5-1) instead of a later kill landing on the wrong instance
        (§5-2).
        """
        identity = current_spawn_identity(self.registry_dir, self.mux, window_target)
        if identity.get("pane_pid") is None and identity.get("created_at") is None:
            self.log(
                f"[retire] {agent}: refusing to request retirement — no spawn identity "
                f"available for window {window_target!r} (mux unreachable or window gone). "
                f"Not writing a marker; will retry when identity is readable."
            )
            return False
        req = build_request(agent, window_target, reason, identity,
                            mission=mission, task_id=task_id, message=message)
        if not write_json_atomic(request_path(self.registry_dir, agent), req):
            self.log(f"[retire] {agent}: failed to write retirement request")
            return False
        self.log(
            f"[retire] {agent}: requested ({reason}) window={window_target} "
            f"mission={mission} task={task_id} pane_pid={identity.get('pane_pid')}"
        )
        return True

    # ------------------------------------------------------------------
    # Restart recovery (R3)
    # ------------------------------------------------------------------

    def recover(self) -> list:
        """Re-base in-flight deadlines once, at daemon startup.

        A deadline is an absolute epoch, so after a restart every in-flight
        marker would escalate immediately.  That is wrong in exactly the case
        that matters: if the previous daemon died between writing
        `phase=notified` and actually sending the message (R1 puts the write
        first), the Worker never got its 60 seconds.  Re-basing gives it back.

        Only the clock moves here.  Whether the marker is still legitimate —
        window gone, different instance — is decided by the normal cycle
        below, which applies the same guards to a recovered marker as to a
        fresh one.  Doing it in this order is what keeps §3-3 F2 closed: a
        recovered marker never skips the R7 re-check.
        """
        recovered = []
        for agent in list_agents(self.registry_dir):
            prog = read_json(progress_path(self.registry_dir, agent))
            if not prog or prog.get("phase") not in IN_FLIGHT_PHASES:
                continue
            if prog.get("phase") == PHASE_TERMINATED:
                continue  # no deadline to re-base; cleanup is owed, not a wait
            window = self.grace_period if prog.get("phase") == PHASE_NOTIFIED else self.kill_delay
            updated = build_progress(prog, prog["phase"], deadline=self.now() + window)
            if write_json_atomic(progress_path(self.registry_dir, agent), updated):
                self.log(
                    f"[retire] {agent}: resuming interrupted retirement at "
                    f"phase={prog['phase']} (deadline re-based to +{window}s)"
                )
                recovered.append(agent)
        return recovered

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------

    def process_all(self) -> list:
        """Advance every marker by at most one step.  Returns [(agent, action)].

        Bounded by design (R2): no step blocks, so the caller's cycle time
        does not grow with the number of Workers being retired.
        """
        agents = list_agents(self.registry_dir)
        if not agents:
            return []

        # Corroborate before trusting "the window is gone".  Without this a
        # transient backend outage reads as "every Worker died", and the
        # terminal phase would reset live Workers' tasks to pending — the
        # damaging direction.  Same reasoning, and same direction, as
        # watchdog's _is_mass_kill(): a backend that cannot answer is a
        # config problem, not N deaths.
        if not self.mux.available():
            now = self.now()
            if now - self._unavailable_logged_at > 300:
                self._unavailable_logged_at = now
                self.log(
                    f"[retire] mux backend unavailable — skipping {len(agents)} "
                    f"pending retirement(s) this cycle (nothing killed, nothing reset)"
                )
            return []

        self._live_windows = set(self.mux.list())
        self._listing_authoritative = self._listing_is_authoritative()
        actions = []
        for agent in agents:
            try:
                action = self._advance(agent)
            except Exception as e:  # noqa: BLE001 — one bad marker must not stop the rest
                self.log(f"[retire] {agent}: ERROR advancing retirement: {type(e).__name__}: {e}")
                action = "error"
            if action:
                actions.append((agent, action))
        return actions

    # ------------------------------------------------------------------
    # Phase machine
    # ------------------------------------------------------------------

    def _advance(self, agent: str) -> Optional[str]:
        req = read_json(request_path(self.registry_dir, agent))
        prog = read_json(progress_path(self.registry_dir, agent))

        if prog is None:
            if req is None:
                return None
            return self._start(agent, req)

        phase = prog.get("phase")
        if phase == PHASE_NOTIFIED:
            return self._step_notified(agent, req, prog)
        if phase == PHASE_SIGTERM_SENT:
            return self._step_sigterm(agent, req, prog)
        if phase in (PHASE_TERMINATED, PHASE_CLEANUP_FAILED):
            return self._settle_terminated(agent, req, prog)
        if phase == PHASE_DISCARDED:
            return self._settle_discarded(agent, prog)

        # Unknown / corrupt phase.  Settle it as discarded rather than
        # guessing which destructive step it was in the middle of.
        self.log(f"[retire] {agent}: unknown phase {phase!r} — discarding marker")
        self._write_progress(agent, prog, PHASE_DISCARDED,
                             discard_reason=f"unknown phase {phase!r}")
        return "discarded"

    # -- step helpers --------------------------------------------------

    def _window_alive(self, target: str) -> bool:
        return target in self._live_windows

    def _listing_is_authoritative(self) -> bool:
        """May "absent from this cycle's window list" be read as "it is gone"?

        A non-empty list proves the query worked.  An empty one proves
        nothing: **both backends turn a failed or timed-out query into `[]`**
        (`lib_mux.py` TmuxBackend.list returns [] on `returncode != 0` or on
        its 5s timeout; HerdrBackend.list returns [] when the workspace lookup
        or the 10s `pane_list` fails).  The `available()` gate in
        `process_all()` does not catch that — tmux's only checks that the
        binary is in PATH — which is exactly how a live Worker's task came to
        be reset in QA (t003 FAIL-1) while watchdog's own `_is_mass_kill()`
        was refusing to act on the very same empty list three seconds earlier.

        The one reading that rescues an empty list is a backend that is itself
        down: then there are no windows to list, so emptiness is the truth
        rather than an outage.  Without that exception the death of the last
        Worker would leave a marker nothing could ever settle — the ghost task
        this module exists to remove.

        A backend that cannot answer "are you up?" is treated as up, i.e. the
        list stays unauthoritative: the cost is waiting a cycle, the cost of
        the other direction is resetting a live Worker's task.
        """
        if self._live_windows:
            return True
        server_running = getattr(self.mux, "server_running", None)
        if server_running is None:
            return False
        try:
            return not server_running()
        except Exception:  # noqa: BLE001 — an unanswerable probe is not proof
            return False

    def _exit_evidence(self, req: Optional[dict], prog: Optional[dict],
                       target: str) -> Tuple[bool, str]:
        """(gone, why) — and `gone` is True only when the exit is *proven*.

        The recorded pane_pid is decisive on its own, and is asked first: if
        that pid is no longer a live process the instance is over, and the
        retirement should move to its terminal phase so the queue gets
        repaired.  Asking the identity guard first would instead read the very
        same situation — a pid we recorded, unreadable now — as "somebody else
        owns this window", discard the marker, and strand the task in_progress.

        Only when no pid was ever recorded does the window list get a vote,
        and then only a list we have reason to trust (see
        `_listing_is_authoritative()`).  "The backend did not mention it" is
        not a death certificate.
        """
        recorded = (prog or {}).get("pane_pid")
        if recorded is None:
            recorded = ((req or {}).get("spawn_identity") or {}).get("pane_pid")
        if recorded is not None:
            if process_alive(recorded):
                return False, f"recorded pane_pid {recorded} is still running"
            return True, f"recorded pane_pid {recorded} is gone"
        if self._window_alive(target):
            return False, f"window {target!r} is still listed"
        if self._listing_authoritative:
            return True, f"window {target!r} absent from a corroborated window list"
        return False, (
            "no pane_pid was recorded and the window list is empty without "
            "corroboration — an outage and an exit look identical"
        )

    def _instance_gone(self, req: dict, prog: Optional[dict], target: str) -> bool:
        """Has the Worker we were retiring finished exiting?  Proof only."""
        gone, _why = self._exit_evidence(req, prog, target)
        return gone

    def _log_waiting(self, agent: str, message: str) -> None:
        """Log a "cannot tell yet" once every 5 minutes per agent.

        These repeat every cycle for as long as the backend stays confused, and
        a line per 30s cycle would bury the events that matter.
        """
        now = self.now()
        if now - self._waiting_logged_at.get(agent, 0.0) < 300:
            return
        self._waiting_logged_at[agent] = now
        self.log(message)

    def _write_progress(self, agent: str, previous: Optional[dict], phase: str, **fields) -> bool:
        doc = build_progress(previous, phase, **fields)
        ok = write_json_atomic(progress_path(self.registry_dir, agent), doc)
        if not ok:
            self.log(f"[retire] {agent}: failed to persist phase={phase} — not proceeding")
        return ok

    def _guard(self, agent: str, req: dict, target: str) -> Tuple[str, str]:
        """R6 + R7 + premise re-check, immediately before every destructive step."""
        if not self.repo_identity_check():
            return GUARD_SKIP, (
                f"self-identity check failed for repo_root={self.repo_root} "
                f"(missing or no longer a git checkout — likely a removed worktree)"
            )

        # The premise of an idle retirement is "this Worker has no work".  It
        # was true when dispatcher wrote the marker; it is checked again here
        # because t002 widened the gap between deciding and acting from one
        # second to a full grace period.  A Worker that pulled a task in the
        # meantime writes queue/assignments/<agent> — the same file dispatcher
        # judged it idle by — so an existing assignment means the reason for
        # this retirement has expired.  Timeout retirements (which carry a
        # task_id) are exempt: they are *about* a Worker that holds a task.
        if not req.get("task_id") and self.queue_dir:
            try:
                if (self.queue_dir / "assignments" / agent).exists():
                    return GUARD_DISCARD, "Worker picked up a task after the request was written"
            except OSError:
                pass

        current = current_spawn_identity(self.registry_dir, self.mux, target)
        ok, why = identity_matches(req.get("spawn_identity"), current)
        if not ok:
            return GUARD_DISCARD, why
        return GUARD_OK, ""

    def _plan_sh_usable(self) -> bool:
        return bool(self.plan_sh) and self.plan_sh.is_file()

    def _start(self, agent: str, req: dict) -> Optional[str]:
        target = req.get("window_target") or f"{agent}-worker"

        # Did it end before we did anything?  Only asked while the window is
        # *not* listed.  A listed window is answered by the identity guard
        # below instead, even when the recorded pid is dead — that combination
        # means a same-named successor has taken the window over, and treating
        # it as "our Worker exited, clean up its task" would reset the task the
        # successor is working on (§5-2, the worst failure mode this module
        # has).  The later steps read a dead recorded pid the opposite way on
        # purpose: by then they have signalled that pid themselves, so its
        # death is their doing and the cleanup is theirs to finish (§6-2 (3)).
        if not self._window_alive(target):
            # The terminal phase authorises `plan.sh update --reset`, i.e.
            # "this task may be handed to somebody else".  Reaching it because
            # `mux.list()` hiccuped is how QA (t003 FAIL-1) saw a live
            # Worker's task reset while it kept working, so require proof.
            gone, evidence = self._exit_evidence(req, None, target)
            if gone:
                # Nothing was killed here; the queue-side cleanup is still
                # owed, so go to the terminal phase rather than dropping the
                # marker.
                self.log(f"[retire] {agent}: window {target!r} already gone before "
                         f"first step ({evidence})")
                self._write_progress(agent, None, PHASE_TERMINATED, window_gone=True,
                                     mission=req.get("mission"),
                                     task_id=req.get("task_id"),
                                     reason=req.get("reason"))
                return "terminated"

            # Missing from the list, but not proven gone.  Nothing can be sent
            # to a window we cannot see, and nothing may be cleaned up on a
            # death we cannot show, so let the marker stand and look again
            # next cycle.  This resolves on its own the moment the backend
            # answers properly — and while it does not, dispatcher's
            # independent D4 detection (R5) still reports the task.
            self._log_waiting(
                agent,
                f"[retire] {agent}: window {target!r} not listed but {evidence} — "
                f"waiting instead of assuming a death (nothing killed, nothing reset)")
            return "skipped"

        # Pre-flight (plan review, fail closed): a retirement that owes a
        # queue-side cleanup must not begin unless the tool that performs it
        # is actually there.  Killing first and only then discovering plan.sh
        # is missing is precisely the ghost-task state this task exists to
        # remove, so refuse to start at all and say why.
        if req.get("task_id") and not self._plan_sh_usable():
            self.log(
                f"[retire] {agent}: REFUSING to start — task {req.get('task_id')} needs a "
                f"plan.sh reset afterwards but plan_sh={self.plan_sh} is not usable. "
                f"Worker left running."
            )
            return "blocked"

        verdict, why = self._guard(agent, req, target)
        if verdict == GUARD_SKIP:
            self.log(f"[retire] {agent}: skipping this cycle — {why}")
            return "skipped"
        if verdict == GUARD_DISCARD:
            self.log(f"[retire] {agent}: NOT retiring — {why}")
            self._write_progress(agent, None, PHASE_DISCARDED, discard_reason=why)
            return "discarded"

        pane_pid = current_spawn_identity(self.registry_dir, self.mux, target).get("pane_pid")
        # R1: the intent is durable before the act.  If the write fails we
        # have not sent anything, and next cycle starts over cleanly.
        #
        # mission/task_id are copied in rather than read back from the request
        # at cleanup time: the terminal phase is where the queue gets
        # repaired, and it must not depend on a second file still being there.
        # Losing the request after the Worker is already dead would otherwise
        # mean nobody ever learns which task to reset.
        if not self._write_progress(agent, None, PHASE_NOTIFIED,
                                    deadline=self.now() + self.grace_period,
                                    pane_pid=pane_pid,
                                    mission=req.get("mission"),
                                    task_id=req.get("task_id"),
                                    reason=req.get("reason")):
            return "blocked"
        message = req.get("message") or SHUTDOWN_MESSAGE
        if not self.mux.send(target, message):
            self.log(f"[retire] {agent}: WARNING mux send to {target!r} failed")
        else:
            self.log(
                f"[retire] {agent}: sent shutdown message, "
                f"{self.grace_period}s to exit on its own"
            )
        return "notified"

    def _step_notified(self, agent: str, req: Optional[dict], prog: dict) -> Optional[str]:
        if req is None:
            return self._orphaned(agent, prog)
        target = req.get("window_target") or f"{agent}-worker"

        if self._instance_gone(req, prog, target):
            self.log(f"[retire] {agent}: exited on its own after the shutdown message")
            self._write_progress(agent, prog, PHASE_TERMINATED, window_gone=True)
            return "terminated"

        if self.now() < (prog.get("deadline") or 0):
            return None  # still inside its grace period

        verdict, why = self._guard(agent, req, target)
        if verdict == GUARD_SKIP:
            self.log(f"[retire] {agent}: REFUSING SIGTERM — {why}")
            return "skipped"
        if verdict == GUARD_DISCARD:
            self.log(f"[retire] {agent}: NOT sending SIGTERM — {why}")
            self._write_progress(agent, prog, PHASE_DISCARDED, discard_reason=why)
            return "discarded"

        pane_pid = self.mux.pid(target)
        if pane_pid is None:
            self.log(f"[retire] {agent}: window {target!r} alive but pane pid unreadable — waiting")
            return "skipped"
        if not self._write_progress(agent, prog, PHASE_SIGTERM_SENT,
                                    deadline=self.now() + self.kill_delay,
                                    pane_pid=int(pane_pid)):
            return "blocked"
        if self.kill_process(int(pane_pid), signal.SIGTERM):
            self.log(f"[retire] {agent}: SIGTERM → pid {pane_pid}")
        else:
            # Not fatal to the machine — the SIGKILL step re-signals after
            # kill_delay and, unlike here, refuses to conclude anything from a
            # signal it could not deliver.  Said out loud because a SIGTERM
            # that cannot be delivered usually means the pid is not ours.
            self.log(f"[retire] {agent}: WARNING SIGTERM to pid {pane_pid} was not "
                     f"delivered — escalating in {self.kill_delay}s")
        return "sigterm_sent"

    def _step_sigterm(self, agent: str, req: Optional[dict], prog: dict) -> Optional[str]:
        if req is None:
            return self._orphaned(agent, prog)
        target = req.get("window_target") or f"{agent}-worker"

        if self._instance_gone(req, prog, target):
            self.log(f"[retire] {agent}: exited after SIGTERM")
            self._write_progress(agent, prog, PHASE_TERMINATED, window_gone=True)
            return "terminated"

        if self.now() < (prog.get("deadline") or 0):
            return None

        verdict, why = self._guard(agent, req, target)
        if verdict == GUARD_SKIP:
            self.log(f"[retire] {agent}: REFUSING SIGKILL — {why}")
            return "skipped"
        if verdict == GUARD_DISCARD:
            self.log(f"[retire] {agent}: NOT sending SIGKILL — {why}")
            self._write_progress(agent, prog, PHASE_DISCARDED, discard_reason=why)
            return "discarded"

        pane_pid = prog.get("pane_pid") or self.mux.pid(target)
        if pane_pid is None:
            self.log(f"[retire] {agent}: no pane pid for SIGKILL — waiting")
            return "skipped"

        pid = int(pane_pid)
        attempts = int(prog.get("sigkill_attempts") or 0) + 1
        delivered = self.kill_process(pid, signal.SIGKILL)
        if delivered:
            self.log(f"[retire] {agent}: SIGKILL → pid {pid}")
        else:
            self.log(f"[retire] {agent}: WARNING SIGKILL to pid {pid} was not delivered "
                     f"(attempt {attempts})")

        # A delivered signal is not a death.  `os.kill` returns once the
        # signal is queued, and the phase we are about to write is the one
        # that authorises `plan.sh update --reset` — so it has to be behind
        # proof that the process is actually gone, not behind a syscall that
        # merely succeeded.  Confirmation is bounded (R2): what cannot be
        # shown this cycle is re-checked next cycle, with the marker still in
        # flight, rather than blocking the daemon.
        if self._await_exit(pid, KILL_CONFIRM_WINDOW if delivered else 0.0):
            # Written after the signal deliberately: the intent to SIGKILL was
            # already durable as "sigterm_sent with an expired deadline" (R1),
            # and a crash between the two leaves that same state, which this
            # cycle re-derives.
            self._write_progress(agent, prog, PHASE_TERMINATED, pane_pid=pid,
                                 sigkill_attempts=attempts)
            return "terminated"

        reported = bool(prog.get("sigkill_report_sent"))
        if attempts >= SIGKILL_REPORT_AFTER and not reported:
            reported = self._report(
                agent, prog,
                f"watchdog が Worker {agent} に SIGKILL を {attempts} 回送りましたが、"
                f"pid {pid} が終了しません。Worker は動き続けている可能性があるため、"
                f"task の後始末は保留しています (誤って別の Worker に配り直さないため)。"
                f"手で `kill -9 {pid}` を試すか、pane の状態を確認してください。",
            )
        self._write_progress(agent, prog, PHASE_SIGTERM_SENT,
                             deadline=self.now() + self.kill_delay,
                             pane_pid=pid, sigkill_attempts=attempts,
                             sigkill_report_sent=reported)
        return "sigkill_unconfirmed"

    def _await_exit(self, pid: int, window: float) -> bool:
        """True once `pid` is gone; polls for at most `window` seconds.

        Real time on purpose — `self.now` is the injectable clock the phase
        deadlines run on, and this is not a deadline but the physical gap
        between SIGKILL and the kernel finishing with the process, which a
        fake clock cannot shorten.  The window is small and only ever paid on
        a cycle that just killed something.
        """
        if not process_alive(pid):
            return True
        deadline = time.monotonic() + max(0.0, window)
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if not process_alive(pid):
                return True
        return not process_alive(pid)

    def _orphaned(self, agent: str, prog: dict) -> str:
        """Progress with no request: the request was deleted mid-flight.

        Which way this settles depends on whether the Worker is still there.
        If the recorded pid is gone we already did the damage, so the queue
        repair is still owed and dropping the marker would strand the task —
        the progress file carries mission/task_id for exactly this case.  If
        it is still running we have lost the identity record that authorises
        further steps, so the only safe move left is to leave it alone.
        """
        if not process_alive(prog.get("pane_pid")):
            self.log(
                f"[retire] {agent}: request marker vanished, but the Worker is already "
                f"gone — completing the cleanup we still owe"
            )
            self._write_progress(agent, prog, PHASE_TERMINATED, window_gone=True)
            return "terminated"
        self.log(f"[retire] {agent}: request marker vanished mid-flight — settling as discarded")
        self._write_progress(agent, prog, PHASE_DISCARDED,
                             discard_reason="request marker vanished")
        return "discarded"

    # -- terminal handling ---------------------------------------------

    def _settle_terminated(self, agent: str, req: Optional[dict], prog: dict) -> Optional[str]:
        """Finish the queue-side bookkeeping this daemon now owes (E1).

        The Worker is gone.  Historically this is where crewvia stopped:
        the task stayed `in_progress`, `queue/assignments/<agent>` stayed on
        disk, and the only thing that noticed was dispatcher's D4 telling a
        human to run `plan.sh update --reset` by hand — which never arrived
        at all if dispatcher was the daemon that was down.

        The reset goes through `plan.sh` rather than rewriting frontmatter
        here.  plan.sh owns that file, holds the lock, and already refuses to
        delete an assignment that has been reused for a different task.  Two
        of this repo's worst outages came from writing frontmatter from
        outside it (PR #181's multi-line reason, the literal "null" worker).
        """
        mission = (req or {}).get("mission") or prog.get("mission")
        task_id = (req or {}).get("task_id") or prog.get("task_id")

        if task_id and mission:
            if not self._plan_sh_usable():
                return self._cleanup_failed(
                    agent, prog, f"plan.sh not usable at {self.plan_sh}", mission, task_id)
            # Guarded, not unconditional.  A retirement takes a grace period
            # plus however long the cleanup is retried, and the task can move
            # underneath it: the Worker may have finished its last `plan.sh
            # done` before exiting, or a human may have reset the task and a
            # successor pulled it.  An unconditional `--reset` reopens
            # finished work and clears the successor's assignment — plan.sh's
            # own content check only rejects an assignment for a *different*
            # task, never a different *execution* of this one.  The
            # preconditions are evaluated inside plan.sh's queue lock, so the
            # decision and the write cannot be split by another writer, and
            # they are re-evaluated on every retry.
            argv = ["bash", str(self.plan_sh), "update", task_id,
                    "--status", "pending", "--reset", "--mission", mission,
                    "--expect-status", "in_progress", "--expect-worker", agent]
            env = dict(os.environ)
            if self.queue_dir:
                env["CREWVIA_QUEUE"] = str(self.queue_dir)
            env["CREWVIA_REPO_ROOT"] = str(self.repo_root)
            rc, output = self.run_command(argv, env)
            if rc == PLAN_PRECONDITION_UNMET:
                # Nothing is owed and nothing is broken: the task is no longer
                # the one this retirement was about.  Settle quietly rather
                # than retrying — a retry can only re-confirm the same answer.
                detail = output.strip()[:300] or "task no longer matches the retirement"
                self.log(f"[retire] {agent}: no queue cleanup owed for {mission}/{task_id} "
                         f"— {detail}")
                self._report(
                    agent, prog,
                    f"watchdog が Worker {agent} を終了しましたが、task {task_id} "
                    f"(mission={mission}) は既に別の状態になっていたため queue は"
                    f"変更していません ({detail})。確認だけお願いします。",
                )
            elif rc != 0:
                return self._cleanup_failed(
                    agent, prog, f"plan.sh update exited {rc}: {output.strip()[:400]}",
                    mission, task_id)
            else:
                self.log(
                    f"[retire] {agent}: cleaned up {mission}/{task_id} "
                    f"(status→pending, assignment removed)"
                )
                reason = (req or {}).get("reason") or prog.get("reason") or "unknown"
                self._report(
                    agent, prog,
                    f"watchdog が Worker {agent} を終了しました "
                    f"(理由: {reason})。"
                    f"task {task_id} (mission={mission}) は pending に戻し、"
                    f"assignment も削除済みです。復旧作業は不要です。",
                )
        else:
            self.log(f"[retire] {agent}: retired, no task to clean up")

        # Request first: a crash between the two unlinks leaves a settled
        # progress marker with no request, which _advance() treats as done
        # instead of starting a second termination.
        unlink_quiet(request_path(self.registry_dir, agent))
        unlink_quiet(progress_path(self.registry_dir, agent))
        return "cleaned"

    def _cleanup_failed(self, agent: str, prog: dict, why: str,
                        mission: Optional[str], task_id: Optional[str]) -> str:
        """Terminated but the queue reset would not go through.

        Kept, not dropped: the marker is the only record that this agent's
        task needs repairing, and it is retried every cycle in case plan.sh
        was merely locked.  The Director is told once, with the manual recipe,
        so a broken cleanup is loud rather than silent.  D4 still notices the
        same task independently at 600s (R5) — that duplication is deliberate.
        """
        attempts = int(prog.get("cleanup_attempts") or 0) + 1
        self.log(f"[retire] {agent}: cleanup attempt {attempts} failed — {why}")
        already = bool(prog.get("director_notified"))
        notified = already
        if not already:
            notified = self._report(
                agent, prog,
                f"watchdog が Worker {agent} を終了しましたが、後始末に失敗しました "
                f"({why})。plan.sh update {task_id} --status pending --reset "
                f"--mission {mission} を手で実行してください。",
            )
        self._write_progress(agent, prog, PHASE_CLEANUP_FAILED,
                             cleanup_error=why, cleanup_attempts=attempts,
                             director_notified=notified,
                             mission=mission, task_id=task_id)
        return "cleanup_failed"

    def _settle_discarded(self, agent: str, prog: dict) -> str:
        """R7 said no.  Drop the marker without touching the queue.

        Nothing was killed, so there is no ghost task to repair — a healthy
        Worker is wearing this name right now.  Sending D4's recovery recipe
        here would make a human reset a task that is being worked on.
        """
        self.log(
            f"[retire] {agent}: discarding marker "
            f"(reason: {prog.get('discard_reason')}) — Worker left alone"
        )
        unlink_quiet(request_path(self.registry_dir, agent))
        unlink_quiet(progress_path(self.registry_dir, agent))
        return "discarded_cleared"

    def _report(self, agent: str, prog: dict, message: str) -> bool:
        if self.notify is None:
            return False
        try:
            return bool(self.notify(message))
        except Exception as e:  # noqa: BLE001
            self.log(f"[retire] {agent}: director report failed: {type(e).__name__}: {e}")
            return False
