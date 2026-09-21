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

    registry/retirements/<agent>.json           written+deleted by dispatcher only
    registry/retirements/<agent>.progress.json  written+deleted by watchdog only

**One writer per file.**  Splitting request from progress is what removes the
need for locking between the two daemons — neither ever updates a file the
other owns.  Both are written temp + os.replace so a reader never observes a
half-written JSON document.

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
