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
    registry/retirements/<agent>.stalled        receipt: "the Director has been told"

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
    cleanup_failed terminated, but the queue repair did not happen; left for a
                  human.  Two ways in: plan.sh kept failing (retried every
                  cycle), or the assignment generation was never recorded, so
                  no automatic repair is allowed at all (t024 — not retried,
                  since re-asking can only reach the same answer)

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

The check is only worth what the *use* is bound to, which is what t020
(Codex P1-1 / P1-3) is about.  Two rules follow from it:

  - **Act on what you verified.**  A step never resolves `window_target`
    again after its guard has passed: window names are reused, so a second
    `mux.pid()` can return a same-named successor the guard never saw.  The
    pid comes out of the guard's own snapshot (`_verified_pid()`).
  - **Bind the queue cleanup to the assignment, not the name.**  `status` and
    `worker` both return to their old values when a human resets a task and
    the same Worker name pulls it again, so the retirement also records the
    task's `started_at` — the generation `plan.sh pull` rewrites on every
    execution — and asserts it under plan.sh's lock.

t024 turned that binding into a single call.  The cleanup is
`plan.sh retire <task> --agent <name> --started-at <generation> --mission
<slug>`: this module hands over the evidence and reads the answer, and does
not get to choose which preconditions are asserted.  Every one of the nine P1s
these three review rounds produced was a caller deciding to assert less, so
the choice is gone from the interface.  **No generation, no cleanup** — the
marker goes to `cleanup_failed` and a human is told once, because a generation
cannot be recovered afterwards (re-reading the card returns the successor's).

## Evidence (t020, Codex P1-2)

Nothing here concludes a death from a *failure to observe* one.  Both mux
backends collapse a timeout into an empty list, and `server_running()` is a
5s subprocess on tmux and a socket ping on herdr — an outage big enough to
empty the listing empties the probe too, and two failures that corroborate
each other are not evidence.  So proof of an exit is a recorded pane_pid that
`/proc` says is gone, or absence from a listing that returned *something*.
"Recorded" means a pid we can actually read (`recorded_pid()`): an absent or
corrupt one is no evidence at all, and reading it as a death was the third
round's P1-1.

## The world keeps moving (t025)

The three rounds above are all about *who* is being ended and *what* counts as
proof.  Both can be satisfied and the retirement still be wrong, because a
retirement is no longer instantaneous: t002 put a grace period between the
decision and the act, and things happen in it.

  - **A Worker is not a process.**  A timeout retirement names one execution
    of one card, and the pane cannot tell you which execution is running: a
    Worker that finishes the timed-out task and pulls the next one keeps its
    pid and its created_at.  `assignment_execution_verdict()` asks the
    assignment instead — the thing `plan.sh pull` republishes, with its
    generation, inside the queue lock — and **only a positive match lets the
    retirement proceed**.  An assignment that says the Worker moved on is a
    discard; a generation we cannot read at all is a hold, because a discard
    there would be re-decided identically every cycle (`_unprovable()`,
    t026).  Upstream, neither dispatcher nor `plan.sh pull` will hand a task
    to a Worker that has a marker, so the situation mostly does not arise;
    this is the backstop for the times it does.
  - **The request file is the race, so creating it is the decision.**  Two
    daemons write requests and both check `has_marker()` first.  A check apart
    from the write is not a decision, so the create is atomic
    (`write_json_exclusive()`), and every request carries a `request_id` the
    progress file copies.  A progress file and a request that disagree about
    that id are two different retirements, and their evidence never mixes —
    the progress finishes on its own (`_read_pair()`), the request waits its
    turn.

    The same sentence decides *where* the create happens (t033).  `plan.sh
    pull` reads this file — `retirement_reservation()` — to decide whether a
    Worker may be given work, and it reads it inside the queue lock, together
    with the assignment it publishes.  A decision written outside that lock is
    not serialised with the decisions it is supposed to override: a pull that
    has already passed the reservation check and is still scanning cards will
    publish an assignment the marker knows nothing about, and the guard, asked
    in between, sees an idle Worker and ends it.  So the create takes the
    queue lock too (`queue_transaction()`), without waiting, and a busy queue
    simply means "not this cycle".
  - **Nothing in the cycle may wait on the queue lock.**  The cleanup runs
    inside watchdog's monitoring loop, and plan.sh's lock is a blocking
    exclusive flock: one busy queue used to suspend every Worker's monitoring
    for the subprocess timeout, per marker, on every retry.  `plan.sh retire
    --no-wait` gives up instead and exits `PLAN_LOCK_BUSY`, which is read as
    "ask again next cycle" — not as a failure, which would burn an attempt and
    wake a human for a lock that was about to be released.

All three fail toward "kill nothing, rewrite nothing", and all three have the
same exit as everything else here: `_check_stall()` tells the Director once.

Every other reading waits.  Waiting has one exit, and it is not automatic:
`_check_stall()` escalates a retirement that has not moved in
`STALL_REPORT_AFTER` seconds to the Director, once, and changes nothing.  The
automatic direction is always "kill nothing, rewrite nothing" — resolving an
ambiguity by rewriting the queue is how the last three defects in this module
were built (memory: fail-closed-guard-can-recreate-the-defect).
"""

import contextlib
import fcntl
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# queue / registry を読むガードは crewvia に 1 つしかない (t017/t018)。
# 素の `read_text()` は上限を持たないので、置き違えた FIFO 1 枚で退役処理
# —— watchdog のサイクルの中 —— が無期限に座り込む。
from lib_task_cards import (  # noqa: E402
    is_missing, is_unreadable, read_regular_text_or_unreadable,
)
# JSON の状態ストアを読む入口も 1 つ (t026)。ここで `json.loads` を書き足さない。
from lib_daemon_state import load_json_store  # noqa: E402

# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

PHASE_NOTIFIED = "notified"
PHASE_SIGTERM_SENT = "sigterm_sent"
PHASE_TERMINATED = "terminated"
PHASE_DISCARDED = "discarded"
PHASE_CLEANUP_FAILED = "cleanup_failed"
#: Stopped because this retirement cannot be shown to be about the execution
#: that is running now.  Nothing killed, nothing rewritten, marker kept — see
#: `RetirementExecutor._unprovable()` for why this is a hold and not a discard.
PHASE_UNPROVABLE = "unprovable"

#: Phases where a Worker may still be alive and the machine owes it a step.
IN_FLIGHT_PHASES = frozenset({PHASE_NOTIFIED, PHASE_SIGTERM_SENT, PHASE_TERMINATED})
#: Phases where nothing further will happen on its own.
SETTLED_PHASES = frozenset({PHASE_DISCARDED, PHASE_CLEANUP_FAILED, PHASE_UNPROVABLE})

#: Message dispatcher used to send itself before killing; now carried in the
#: marker so the *executor* sends it exactly once.
SHUTDOWN_MESSAGE = "タスクなし、shutdown"

_REQUEST_SUFFIX = ".json"
_PROGRESS_SUFFIX = ".progress.json"
#: Deliberately not a `.json` suffix: `list_agents()` keys off those two, and a
#: third `.json` file would be read as a retirement for an agent named
#: "<agent>.stall".
_STALL_SUFFIX = ".stalled"

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

#: `plan.sh --no-wait` exit status for "the queue lock was held by somebody
#: else, so nothing was written".  Kept distinct from 3 on purpose: 3 means
#: "nothing is owed any more" and settles the marker, while this one means
#: "ask again next cycle" and must not consume the cleanup obligation.
PLAN_LOCK_BUSY = 4

#: Ceiling on the cleanup subprocess.  The cleanup runs *inside* watchdog's
#: monitoring cycle, so its worst case is the cycle's worst case (R2).  The
#: lock wait — the one unbounded thing plan.sh used to do — is gone via
#: `--no-wait`; this is the belt for everything else (a stalled interpreter, a
#: filesystem that stops answering).  120s was worse than the 70s termination
#: stall this module was written to remove.
CLEANUP_COMMAND_TIMEOUT = 30

#: plan.sh's queue lock, relative to the queue directory.  Writing a retirement
#: request takes this same lock — see `queue_transaction()`.
QUEUE_LOCK_NAME = ".lock"

#: How long a retirement may stay unsettled before the Director hears about it
#: once.  Every fail-closed branch in this module ends in "wait and look again
#: next cycle", and some of those waits cannot resolve on their own: a request
#: whose Worker left no recorded pid, written while the backend was the last
#: thing listing that window, has nothing left that can ever supply proof.
#: Waiting forever would be the silent half of the ghost task this module
#: exists to remove — so the wait gets an exit, and that exit is a human, not
#: an automatic queue rewrite (t020, Codex P1-2).  Generous on purpose: the
#: normal escalation is 70s and a slow cleanup retries for a few minutes, so
#: anything still in flight at this age is genuinely stuck.
STALL_REPORT_AFTER = 1800


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def retirements_dir(registry_dir) -> Path:
    return Path(registry_dir) / "retirements"


def request_path(registry_dir, agent: str) -> Path:
    return retirements_dir(registry_dir) / f"{agent}{_REQUEST_SUFFIX}"


def progress_path(registry_dir, agent: str) -> Path:
    return retirements_dir(registry_dir) / f"{agent}{_PROGRESS_SUFFIX}"


def stall_path(registry_dir, agent: str) -> Path:
    """Receipt for "the Director has already been told this one is stuck"."""
    return retirements_dir(registry_dir) / f"{agent}{_STALL_SUFFIX}"


def list_agents(registry_dir) -> list:
    """Every agent name with any retirement file on disk.

    Includes the stall receipt, which outlives its marker whenever a human
    clears the request by hand — which is precisely what the escalation asks
    them to do.  Left behind, it would silence the *next* stall for the same
    agent; listed here, the cycle that finds neither a request nor a progress
    file sweeps it up (`_check_stall`).  Callers all tolerate an agent with no
    request and no progress (dispatcher's `warn_on_unconsumed_retirements()`
    skips a missing request; `_advance()` returns None).
    """
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
        elif p.name.endswith(_STALL_SUFFIX):
            agents.add(p.name[: -len(_STALL_SUFFIX)])
        elif p.name.endswith(_REQUEST_SUFFIX):
            agents.add(p.name[: -len(_REQUEST_SUFFIX)])
    return sorted(agents)


# ---------------------------------------------------------------------------
# The queue transaction
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def queue_transaction(queue_dir):
    """plan.sh's queue lock, taken **without waiting**.  Yields `(held, why)`.

    `plan.sh pull` publishes an assignment and checks
    `retirement_reservation()` inside this lock, so this is the only place a
    retirement request can be serialised against a pull.  Writing the request
    outside it left the reservation check and the write in different critical
    sections, which is not a decision at all:

        pull: reservation check passes (no marker yet)
        pull: … still scanning cards …
        here: marker created; the guard sees no assignment → "idle" → shutdown
        pull: publishes the assignment

    and the task that pull just published belongs to no retirement.  Reading
    the assignment later, or earlier, or twice does not close it — the write
    has to happen where the reservation is read.

    **Non-blocking on purpose.**  Both callers are daemons: dispatcher must
    not stall its dispatch cycle behind a busy queue, and watchdog must not
    stall its monitoring cycle, which is the whole reason `plan.sh retire`
    grew `--no-wait` (see `PLAN_LOCK_BUSY`).  "Could not take it" is not a
    failure here either — the Worker simply is not retired this cycle and the
    caller asks again, which is the fail-closed direction (§5-1: a Worker
    lingers) rather than the damaging one (§5-2: a kill lands on work).

    **Nothing inside may call a subprocess.**  Everything this holds the lock
    for is a local file read or an `os.replace` of a small document; a mux
    query or a `plan.sh` invocation in here would suspend every Worker's
    `plan.sh` call for its timeout.
    """
    path = Path(queue_dir) / QUEUE_LOCK_NAME
    try:
        os.makedirs(path.parent, exist_ok=True)
        fh = open(path, "a+")
    except OSError as e:
        yield False, f"cannot open the queue lock {path}: {e}"
        return
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False, (f"the queue lock {path} is held by another process "
                          f"(a plan.sh transaction is in flight)")
            return
        try:
            yield True, ""
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


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


#: `write_json_exclusive()` outcomes.
WROTE_CREATED = "created"
WROTE_EXISTS = "exists"
WROTE_ERROR = "error"


def write_json_exclusive(path, data: dict) -> str:
    """Create `path` with `data`, **only if it does not exist yet**.

    Returns one of `WROTE_CREATED` / `WROTE_EXISTS` / `WROTE_ERROR`.

    The request file is the token two daemons contend for: dispatcher writes
    one for an idle Worker, watchdog writes one for a timeout, and both decide
    to by reading `has_marker()` first.  A check that is separate from the
    write is not a decision — both can read "no marker" and then both write,
    and the second `os.replace` silently discards the first retirement while
    watchdog may already have built a progress file against it (Codex 4 巡目
    P1-2).  Only the filesystem can settle that race, so the create *is* the
    check.

    Written to a temp file first and then hard-linked into place, rather than
    `O_EXCL` + write: the loser of the race must not be able to observe a
    zero-length or half-written request, because `read_json()` answers None
    for a corrupt document and `_advance()` reads None as "no request" — which
    is a *different* branch, not a retry.  `os.link` publishes a document that
    is already complete, and fails with EEXIST if somebody got there first.
    `O_EXCL` remains as the fallback for filesystems without hard links.
    """
    path = Path(path)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    tmp = path.with_name(f".{path.name}.new.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
    except OSError:
        unlink_quiet(tmp)
        return WROTE_ERROR
    try:
        try:
            os.link(tmp, path)
            return WROTE_CREATED
        except FileExistsError:
            return WROTE_EXISTS
        except OSError:
            pass  # no hard-link support — fall through
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return WROTE_EXISTS
        except OSError:
            return WROTE_ERROR
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            return WROTE_CREATED
        except OSError:
            return WROTE_ERROR
    finally:
        unlink_quiet(tmp)


def read_json(path) -> Optional[dict]:
    """Return the parsed document, or None if missing / unreadable / corrupt."""
    # 「None = 無い / 読めない / 壊れている」を潰した形は従来のまま (この関数の契約)。
    # 区別が要る呼び出し側は `load_json_store()` を直接使う (`read_pause_state()` 参照)。
    # 読み取り・JSON・「object であること」は入口の 1 つ (t026)。
    data = load_json_store(path)
    return None if is_unreadable(data) else data


def unlink_quiet(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def recorded_pid(value) -> Optional[int]:
    """The pid a marker recorded, or None when it recorded none we can use.

    `process_alive()` answers False for anything it cannot parse, which is the
    right answer to "is this process running" and the wrong one to "did our
    Worker exit" — an absent or corrupt pid is no evidence at all, and reading
    it as a death is the shape of Codex 3 巡目 P1-1.  Callers that are about
    to authorise something destructive ask this first and hold when it is
    None, rather than handing a `None` straight to `process_alive()`.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
# Assignment identity
# ---------------------------------------------------------------------------

#: Returned by `read_task_started_at()` when the card could not be read at all.
#: Distinct from `None`, which is the card saying "no execution has started" —
#: a real value worth asserting later.  Collapsing the two would let an
#: unreadable card be recorded as `started_at: null` and then match a card that
#: genuinely has none.
UNKNOWN_STARTED_AT = object()


def read_task_started_at(queue_dir, mission: str, task_id: str):
    """`started_at` from a task card, or `UNKNOWN_STARTED_AT`.

    This is the *assignment generation*: `plan.sh pull` rewrites it on every
    execution, so it is what separates "the assignment this retirement is
    about" from "another run of the same task, by a Worker with the same
    name".  status and worker both come back to their old values on a reset +
    re-pull, which is why neither can play this role (Codex P1-1).

    Read with a deliberately small parser rather than plan.sh: this runs on
    every retirement request, inside watchdog's cycle, and must not be able to
    block on the queue lock.  Only the frontmatter is scanned, and only for one
    key whose value is a quoted scalar.
    """
    if not queue_dir or not mission or not task_id:
        return UNKNOWN_STARTED_AT
    path = Path(queue_dir) / "missions" / str(mission) / "tasks" / f"{task_id}.md"
    # カードも固定パスのガードを通す (Codex 9 巡目 P2)。ここは watchdog の
    # サイクルの中で退役要求ごとに走るので、上限の無い `read_text()` が
    # 書き手のいない FIFO に当たると **退役処理全体が返らない**。
    text = read_regular_text_or_unreadable(path)
    if is_unreadable(text):
        return UNKNOWN_STARTED_AT
    in_frontmatter = False
    for line in text.splitlines():
        if line.strip() == "---":
            if in_frontmatter:
                break            # end of frontmatter, key absent
            in_frontmatter = True
            continue
        if not in_frontmatter or not line.startswith("started_at:"):
            continue
        raw = line.split(":", 1)[1].strip().strip('"').strip("'").strip()
        return None if raw.lower() in ("", "null", "none", "~") else raw
    return None


#: `assignment_execution_verdict()` answers.
EXEC_SAME = "same"        # the assignment still names the execution we recorded
EXEC_OTHER = "other"      # it names a different task, or a different generation
EXEC_ABSENT = "absent"    # nothing is published — that execution has ended
EXEC_UNREADABLE = "unreadable"  # the assignment is there but cannot be read


def assignment_execution_verdict(queue_dir, agent: str, mission, task_id,
                                 generation) -> Tuple[str, str]:
    """Does `queue/assignments/<agent>` still name the execution we recorded?

    Returns `(verdict, detail)`.

    This is the second half of instance identity, and the half the pane cannot
    supply.  `identity_matches()` answers "is this the same *process*", which
    a Worker that finishes one task and pulls the next passes trivially — same
    pane, same pid, same created_at.  A timeout retirement is not about a
    process, though; it is about **one execution of one card**.  Between the
    shutdown message and the escalation the Worker can finish that card and
    take another, and killing it then both ends live work and strands the new
    task, whose cleanup nobody is holding evidence for (Codex 4 巡目 P1-1).

    The assignment is the right thing to ask because `plan.sh pull` publishes
    it inside the queue lock together with the card, and publishes the
    generation beside it (`<agent>.identity`).  Read-only and deliberately
    small: this runs inside watchdog's cycle and must not take the queue lock.

    **Only a positive match returns `EXEC_SAME`.**  Every way of failing to
    read the generation — none recorded, no sidecar, a sidecar without one —
    comes back `EXEC_UNREADABLE`, because the one thing left to compare is a
    string that a reset + re-pull reproduces exactly (Codex 5 巡目 P1-1).
    """
    if not queue_dir or not agent or not task_id:
        return EXEC_UNREADABLE, "no assignment to compare against"
    base = Path(queue_dir) / "assignments" / str(agent)
    text = read_regular_text_or_unreadable(base)
    if is_missing(text):
        return EXEC_ABSENT, f"queue/assignments/{agent} does not exist"
    if is_unreadable(text):
        return EXEC_UNREADABLE, f"queue/assignments/{agent} unreadable: {text.reason}"
    published = text.strip()

    expected = f"{mission}:{task_id}"
    if published != expected:
        return EXEC_OTHER, (f"assignment names {published!r}, not {expected!r} "
                            f"— the Worker has moved on")
    if generation is None:
        # Nothing was recorded to compare against, so nothing here can be
        # matched.  The card match above is *not* the remaining evidence it
        # looks like: a reset + re-pull of the same card by the same name
        # republishes a byte-identical assignment, which is the one case the
        # generation exists to separate (Codex 5 巡目 P1-1).
        return EXEC_UNREADABLE, "no generation was recorded for this retirement"

    identity = read_json(base.with_name(base.name + ".identity"))
    current = (identity or {}).get("started_at")
    if identity is None or current is None:
        # `plan.sh pull` always writes the sidecar next to the assignment, so
        # this is an old or hand-made state.  That makes it unusual, not
        # proven: the assignment in front of us may still be a successor's.
        return EXEC_UNREADABLE, (f"queue/assignments/{agent}.identity is missing "
                                 f"or carries no started_at")
    if str(current).strip() != str(generation).strip():
        return EXEC_OTHER, (f"assignment generation {current!r} != recorded "
                            f"{generation!r} — same card, different execution")
    return EXEC_SAME, ""


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
    raw = read_regular_text_or_unreadable(mux_dir / f"{window_target}.firstseen")
    if is_unreadable(raw):
        return None
    try:
        return float(raw.strip())
    except ValueError:
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
                  message: str = SHUTDOWN_MESSAGE,
                  task_started_at=UNKNOWN_STARTED_AT) -> dict:
    """A dispatcher-side retirement request.

    `mission` / `task_id` are None for the idle / no-task / blocked-stuck
    paths — by definition those Workers hold neither an assignment nor an
    in_progress task, which is precisely why the request cannot live in task
    frontmatter (§3-3 案A).

    `task_started_at` is the assignment generation this retirement is bound to.
    The key is **omitted entirely** when it could not be read, so that a later
    cleanup can tell "this task had no started_at" apart from "we never found
    out".  Neither one authorises an automatic cleanup — see
    `RetirementExecutor._bound_generation()` — but keeping them distinguishable
    is what stops a fabricated `null` from matching a genuinely unstarted card.
    """
    req = {
        # A name for *this* retirement, minted here and carried by the progress
        # file so the two can be told apart from a later one for the same
        # agent.  Names are reused, agents are reused, and even mission/task
        # can repeat; nothing else in the document is unique to one retirement
        # (Codex 4 巡目 P1-2).
        "request_id": uuid.uuid4().hex,
        "agent": agent,
        "window_target": window_target,
        "reason": reason,
        "message": message,
        "requested_at": time.time(),
        "mission": mission,
        "task_id": task_id,
        "spawn_identity": dict(identity),
    }
    if task_started_at is not UNKNOWN_STARTED_AT:
        req["task_started_at"] = task_started_at
    return req


def _carried_from_request(req: dict) -> dict:
    """Fields the progress file must keep after the request is gone.

    The terminal phase is where the queue gets repaired, and it must not
    depend on a second file still being on disk — losing the request after the
    Worker is already dead would otherwise mean nobody ever learns which task
    to reset, or which *execution* of it this retirement was bound to.
    `task_started_at` is carried by key presence for the same reason
    `build_request()` omits it: absent means "never found out", and that has
    to stay distinguishable from a recorded `None`.
    """
    carried = {
        # Carried first: it is what binds every later step to the request that
        # authorised it, and the binding has to survive the request going away.
        "request_id": req.get("request_id"),
        "mission": req.get("mission"),
        "task_id": req.get("task_id"),
        "reason": req.get("reason"),
    }
    if "task_started_at" in req:
        carried["task_started_at"] = req["task_started_at"]
    return carried


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
        ("request_id", None),
        ("deadline", None),
        ("pane_pid", None),
        ("window_gone", False),
        ("discard_reason", None),
        ("cleanup_error", None),
        ("cleanup_attempts", 0),
        ("cleanup_deferred", False),
        ("director_notified", False),
        ("pending_report", None),
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
#: We cannot tell whether the target is still the execution this retirement is
#: about.  Unlike a discard, re-deciding would reach the same dead end every
#: cycle, so the marker is held and a human is told once (`_unprovable()`).
GUARD_UNPROVABLE = "unprovable"


def _default_run_command(argv: list, env: dict) -> Tuple[int, str]:
    import subprocess
    try:
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True,
            timeout=CLEANUP_COMMAND_TIMEOUT,
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
        self._listing_authority: Optional[bool] = None
        self._unavailable_logged_at = 0.0
        self._waiting_logged_at: dict = {}
        # (agent, request_id) of retirements whose report has already reached
        # the Director this process's lifetime.  `_retry_pending_report()`
        # checks this *before* re-sending, so a persistence failure after a
        # successful delivery retries only the receipt write, never the
        # notification (t034 / Codex 7巡目 P2-2).
        self._delivered_reports: set = set()

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

        Also refuses while a `plan.sh` transaction is open.  The marker is what
        `plan.sh pull` reads to decide whether this Worker may be given work,
        so creating it *is* the decision, and a decision taken outside the lock
        that publishes assignments is not serialised with them at all
        (`queue_transaction()` has the interleaving).  Refusing costs a cycle;
        the alternative costs an execution nobody owns.
        """
        # Outside the lock on purpose: this goes to the mux backend, i.e. a
        # subprocess, and nothing may hold the queue lock across one.
        identity = current_spawn_identity(self.registry_dir, self.mux, window_target)
        if identity.get("pane_pid") is None and identity.get("created_at") is None:
            self.log(
                f"[retire] {agent}: refusing to request retirement — no spawn identity "
                f"available for window {window_target!r} (mux unreachable or window gone). "
                f"Not writing a marker; will retry when identity is readable."
            )
            return False
        if self.queue_dir is None:
            # Without the queue there is no lock to serialise against, and a
            # marker written anyway could land in the middle of somebody's
            # pull.  The same executor could not check the idle premise
            # (`_guard()` skips it) or clean up afterwards either, so this is
            # a misconfiguration, not a mode.
            self.log(
                f"[retire] {agent}: refusing to request retirement — no queue_dir, so the "
                f"marker cannot be serialised against plan.sh (would race with pull)"
            )
            return False

        with queue_transaction(self.queue_dir) as (held, why):
            if not held:
                # Not a failure and nothing is owed: the Worker is simply not
                # retired this cycle.  Rate-limited because dispatcher asks
                # every few seconds and a busy queue would otherwise fill the
                # log with one line per cycle per Worker.
                self._log_waiting(
                    agent,
                    f"[retire] {agent}: not writing a retirement request this cycle "
                    f"— {why}. Asking again next cycle ({reason})")
                return False
            return self._write_request(agent, window_target, reason, identity,
                                       mission, task_id, message)

    def _write_request(self, agent: str, window_target: str, reason: str,
                       identity: dict, mission: Optional[str],
                       task_id: Optional[str], message: str) -> bool:
        """The request itself.  **Runs under the queue lock; no subprocesses.**

        Reading the card here rather than before taking the lock is not
        incidental: `plan.sh pull` rewrites `started_at` and publishes the
        assignment in one transaction, so a generation read outside the lock
        can name an execution that the assignment published a moment later has
        already replaced.
        """
        # Read here rather than taken from the caller: every caller would have
        # to remember, and the one that forgot would hand the cleanup to a
        # human for no reason (Codex P1-1).  One place decides.  This is also
        # the *only* moment the generation can be read — by cleanup time the
        # card may belong to a successor.
        started_at = read_task_started_at(self.queue_dir, mission, task_id)
        if task_id and started_at is UNKNOWN_STARTED_AT:
            # The Worker can still be retired — the reason for that is the
            # timeout, not the card — but the *queue* cleanup afterwards will
            # be deferred to a human, because nothing left can bind it to this
            # execution (`_cleanup_deferred()`).  Said out loud here, where the
            # cause (an unreadable card, a missing queue_dir) is still visible;
            # by cleanup time all that is left is the absent key.
            self.log(
                f"[retire] {agent}: WARNING could not read started_at for "
                f"{mission}/{task_id} (queue_dir={self.queue_dir}) — the Worker will "
                f"still be retired, but the queue cleanup will be handed to the "
                f"Director instead of run automatically"
            )
        if progress_path(self.registry_dir, agent).exists():
            # A retirement is already under way and its request has been
            # consumed or lost.  Writing a fresh request now would hand the
            # phase machine a second document to read, and the two would
            # disagree about which execution is being ended.
            self.log(f"[retire] {agent}: a retirement is already in flight "
                     f"— not writing a second request ({reason})")
            return False
        req = build_request(agent, window_target, reason, identity,
                            mission=mission, task_id=task_id, message=message,
                            task_started_at=started_at)
        # Create-if-absent, atomically.  The queue lock serialises this against
        # `plan.sh`; this keeps the *two daemons* from overwriting each other,
        # which the lock does not help with — they can both hold it, one after
        # the other, and a separate check followed by an unconditional replace
        # let the loser overwrite the winner (Codex 4 巡目 P1-2).
        outcome = write_json_exclusive(request_path(self.registry_dir, agent), req)
        if outcome == WROTE_EXISTS:
            self.log(f"[retire] {agent}: a retirement request already exists "
                     f"— leaving it alone (not writing {reason!r})")
            return False
        if outcome != WROTE_CREATED:
            self.log(f"[retire] {agent}: failed to write retirement request")
            return False
        self.log(
            f"[retire] {agent}: requested ({reason}) window={window_target} "
            f"mission={mission} task={task_id} pane_pid={identity.get('pane_pid')} "
            f"request_id={req['request_id']}"
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
        self._listing_authority = self._listing_authority_for_cycle()
        actions = []
        for agent in agents:
            try:
                action = self._advance(agent)
            except Exception as e:  # noqa: BLE001 — one bad marker must not stop the rest
                self.log(f"[retire] {agent}: ERROR advancing retirement: {type(e).__name__}: {e}")
                action = "error"
            if action:
                actions.append((agent, action))
            try:
                if self._check_stall(agent):
                    actions.append((agent, "stall_reported"))
            except Exception as e:  # noqa: BLE001 — reporting must not break the machine
                self.log(f"[retire] {agent}: ERROR checking for a stalled retirement: "
                         f"{type(e).__name__}: {e}")
        return actions

    # ------------------------------------------------------------------
    # Phase machine
    # ------------------------------------------------------------------

    @staticmethod
    def _same_retirement(req: Optional[dict], prog: Optional[dict]) -> bool:
        """Do this request and this progress file describe the same retirement?

        Compared by `request_id` and nothing else.  A missing id on either side
        is *not* a mismatch: markers written before ids existed are still
        legitimate mid-flight retirements, and treating them as foreign would
        orphan every one of them at upgrade time.
        """
        rid = (req or {}).get("request_id")
        pid_ = (prog or {}).get("request_id")
        if rid is None or pid_ is None:
            return True
        return str(rid) == str(pid_)

    def _read_pair(self, agent: str) -> Tuple[Optional[dict], Optional[dict], bool]:
        """`(request, progress, foreign)` — and evidence never crosses retirements.

        When the request on disk is not the one this progress file was built
        from, the request is handed back as `None`: every step then runs on the
        progress file's own evidence (its pid, its task, its generation), which
        is what `_orphaned()` already does correctly for "the request vanished
        mid-flight".  Mixing the two instead — progress from one retirement,
        mission/task/generation from another — is what lets a timeout's cleanup
        obligation be dropped, or another retirement's evidence be applied to a
        live execution (Codex 4 巡目 P1-2).

        The foreign request is left on disk untouched.  It is somebody's live
        ask; once this progress settles it gets its own turn through `_start()`.
        """
        req = read_json(request_path(self.registry_dir, agent))
        prog = read_json(progress_path(self.registry_dir, agent))
        if req is None or prog is None or self._same_retirement(req, prog):
            return req, prog, False
        self._log_waiting(
            agent,
            f"[retire] {agent}: the retirement request on disk "
            f"(request_id={req.get('request_id')!r}) is not the one this progress "
            f"file was built from ({prog.get('request_id')!r}) — finishing the "
            f"progress marker on its own evidence and leaving the request alone")
        return None, prog, True

    def _advance(self, agent: str) -> Optional[str]:
        req, prog, foreign = self._read_pair(agent)

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
            return self._settle_terminated(agent, req, prog, keep_request=foreign)
        if phase == PHASE_DISCARDED:
            return self._settle_discarded(agent, prog, keep_request=foreign)
        if phase == PHASE_UNPROVABLE:
            return self._recheck_unprovable(agent, req, prog)

        # Unknown / corrupt phase.  Settle it as discarded rather than
        # guessing which destructive step it was in the middle of.
        self.log(f"[retire] {agent}: unknown phase {phase!r} — discarding marker")
        self._write_progress(agent, prog, PHASE_DISCARDED,
                             discard_reason=f"unknown phase {phase!r}")
        return "discarded"

    # -- step helpers --------------------------------------------------

    def _window_alive(self, target: str) -> bool:
        return target in self._live_windows

    def _listing_authority_for_cycle(self) -> Optional[bool]:
        """May "absent from this cycle's window list" be read as "it is gone"?

        `True` = yes, `None` = unknown.  There is deliberately no `False`:
        nothing here ever proves a Worker is *alive*, only that this cycle's
        listing is or is not usable as a death certificate.

        A non-empty list proves the query worked.  An empty one proves
        nothing: **both backends turn a failed or timed-out query into `[]`**
        (`lib_mux.py` TmuxBackend.list returns [] on `returncode != 0` or on
        its 5s timeout; HerdrBackend.list returns [] when the workspace lookup
        or the 10s `pane_list` fails).  The `available()` gate in
        `process_all()` does not catch that — tmux's only checks that the
        binary is in PATH — which is exactly how a live Worker's task came to
        be reset in QA (t003 FAIL-1) while watchdog's own `_is_mass_kill()`
        was refusing to act on the very same empty list three seconds earlier.

        t019 rescued the empty list with a second probe: `server_running()`
        returning False was read as "the backend is down, so emptiness is the
        truth".  That probe cannot carry the weight (Codex P1-2).
        `TmuxBackend.server_running()` returns False on a timeout or an
        exception as readily as on a dead server, and HerdrBackend's is a ping
        on a socket — neither is evidence that any Worker *process* exited.
        A single outage that took out the listing would take out the probe
        too, and the two failures would then corroborate each other into
        authority to reset a live Worker's task: the same defect the listing
        gate was added to close, one layer down (memory:
        fail-closed-guard-can-recreate-the-defect).

        So an empty list stays `None` — unknown — and cleanup requires
        positive evidence of an exit.  The concern t019 had was real, though:
        without an exit, the death of the last Worker leaves a marker nothing
        can ever settle, and a marker nobody looks at is the silent half of a
        ghost task.  The exit is `_check_stall()`, which escalates to the
        Director once.  A human is the right terminator for an ambiguity, and
        the automatic direction stays "kill nothing, rewrite nothing".
        """
        return True if self._live_windows else None

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
        `_listing_authority_for_cycle()`).  "The backend did not mention it" is
        not a death certificate.
        """
        # `recorded_pid()`, not the raw field: a corrupt value is not a pid we
        # can question, so it must fall through to the (corroborated) window
        # list rather than answer "gone" via `process_alive()`'s parse failure.
        recorded = recorded_pid((prog or {}).get("pane_pid"))
        if recorded is None:
            recorded = recorded_pid(((req or {}).get("spawn_identity") or {}).get("pane_pid"))
        if recorded is not None:
            if process_alive(recorded):
                return False, f"recorded pane_pid {recorded} is still running"
            return True, f"recorded pane_pid {recorded} is gone"
        if self._window_alive(target):
            return False, f"window {target!r} is still listed"
        if self._listing_authority is True:
            return True, f"window {target!r} absent from a corroborated window list"
        return False, (
            "no pane_pid was recorded and the window list is empty without "
            "corroboration — an outage and an exit look identical"
        )

    def _instance_gone(self, req: dict, prog: Optional[dict], target: str) -> bool:
        """Has the Worker we were retiring finished exiting?  Proof only."""
        gone, _why = self._exit_evidence(req, prog, target)
        return gone

    def _check_stall(self, agent: str) -> bool:
        """Escalate a retirement that has been unable to move for too long.

        This is the exit every "wait and look again next cycle" branch needs.
        Most of them do resolve on their own — the backend answers, the pid
        dies, plan.sh unlocks.  One class cannot: a request whose Worker never
        had a pane_pid recorded, whose window stopped being listed, and whose
        listing is therefore permanently unusable as evidence (see
        `_listing_authority_for_cycle()`).  Nothing will ever arrive to settle
        it, and the marker meanwhile blocks `has_marker()`, so dispatcher stops
        asking too.  Silence there is the ghost task this module exists to
        remove, just with the daemons keeping quiet about it.

        The escalation is a message, **not** a queue rewrite.  An ambiguity
        this old is exactly the thing a person should look at: if the Worker is
        alive, an automatic reset hands its task to somebody else, and if it is
        dead, a human `plan.sh update --reset` costs one command.  Automatic
        always means "kill nothing, rewrite nothing".

        Reported once, and durably so: a watchdog that restarts every few
        minutes must not turn one stuck marker into a stream of alerts.
        """
        req, prog, _foreign = self._read_pair(agent)
        if req is None and prog is None:
            unlink_quiet(stall_path(self.registry_dir, agent))
            return False
        # These two own their own message to the Director, with what to do,
        # and retry it every cycle until it is delivered
        # (`_retry_pending_report()`); a stall report about the same marker
        # would only add noise.  The suppression is safe *because* of that
        # retry: before it, a phase whose report had failed was silenced here
        # too, and the marker went quiet with the Worker still quarantined.
        if (prog or {}).get("phase") in (PHASE_CLEANUP_FAILED, PHASE_UNPROVABLE):
            return False
        if stall_path(self.registry_dir, agent).exists():
            return False

        since = (req or {}).get("requested_at") or (prog or {}).get("updated_at")
        try:
            age = self.now() - float(since)
        except (TypeError, ValueError):
            return False
        if age < STALL_REPORT_AFTER:
            return False

        phase = (prog or {}).get("phase") or "requested (no step taken yet)"
        mission = (req or {}).get("mission") or (prog or {}).get("mission")
        task_id = (req or {}).get("task_id") or (prog or {}).get("task_id")
        target = (req or {}).get("window_target") or f"{agent}-worker"
        if task_id and mission:
            task_line = f"task {task_id} (mission={mission}) は in_progress のままです。"
            recipe = (f"死んでいた場合の後始末: plan.sh update {task_id} --status pending "
                      f"--reset --mission {mission} を手で実行し、")
        else:
            task_line = "この Worker は task を持っていないので、queue の後始末は不要です。"
            recipe = "死んでいた場合は "
        message = (
            f"watchdog が Worker {agent} の終了処理を {age / 60:.0f} 分進められていません "
            f"(phase={phase}, window={target})。Worker が生きているのか既に死んだのか "
            f"裏が取れないため、**何も kill せず queue も書き換えていません**。{task_line}\n"
            f"確認してください: 窓 {target} が実在するか / pane のプロセスが生きているか。\n"
            f"{recipe}registry/retirements/{agent}.* を削除してください。"
        )
        reported = self._report(agent, prog or {}, message)
        self.log(f"[retire] {agent}: retirement stalled for {age:.0f}s at phase={phase} "
                 f"— escalated to the Director (director_reached={reported})")
        # Written whether or not the notifier worked: the log line above is the
        # fallback record, and retrying a broken notifier every cycle forever
        # is the noise this receipt exists to prevent.
        write_json_atomic(stall_path(self.registry_dir, agent), {
            "agent": agent,
            "reported_at": self.now(),
            "phase": phase,
            "age_seconds": round(age, 1),
            "director_reached": reported,
        })
        return True

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

    def _guard(self, agent: str, req: dict, target: str) -> Tuple[str, str, dict]:
        """R6 + R7 + premise re-check, immediately before every destructive step.

        Returns `(verdict, why, identity)`, where `identity` is **the very
        snapshot the verdict was reached on**.  Callers must take the pid they
        act on from it rather than resolving `target` again: window names are
        reused, so a second `mux.pid(target)` can hand back a same-named
        successor that this guard never saw, and that pid would then be
        persisted and signalled without ever having been checked against the
        request (Codex P1-3).

        The order of the checks is part of the guarantee.  The one call here
        that can block — resolving the window's identity through the mux
        backend — runs **first**, so that the assignment is the *last* thing
        read before the caller acts on the answer.  Reading the assignment
        first left a subprocess-long gap between "this Worker still holds that
        execution" and the signal, and a Worker that called `plan.sh pull`
        inside that gap was signalled while running a task this retirement
        knows nothing about (Codex 5 巡目 P1-2).  What actually closes that
        door is the retirement reservation `plan.sh pull` now honours — no
        assignment can be published to a Worker with a marker, inside the same
        queue lock that publishes it.  This ordering is what keeps the door
        from standing open in the meantime; neither alone is the fix.
        """
        if not self.repo_identity_check():
            return GUARD_SKIP, (
                f"self-identity check failed for repo_root={self.repo_root} "
                f"(missing or no longer a git checkout — likely a removed worktree)"
            ), {}

        current = current_spawn_identity(self.registry_dir, self.mux, target)
        ok, why = identity_matches(req.get("spawn_identity"), current)
        if not ok:
            return GUARD_DISCARD, why, current

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
                    return (GUARD_DISCARD,
                            "Worker picked up a task after the request was written",
                            current)
            except OSError:
                pass

        # The premise of a *timeout* retirement is "this Worker is stuck on
        # this execution".  The exemption above is about the assignment merely
        # existing, which is normal here — but *which* execution it names is
        # not, and the pane cannot answer that question: a Worker that finishes
        # the timed-out task and pulls the next one keeps the same pid and the
        # same created_at, so R7 passes and the escalation lands on live work,
        # stranding the new task nobody holds evidence for (Codex 4 巡目 P1-1).
        #
        # Everything except a positive match stops here, and the two ways of
        # stopping are not interchangeable:
        #
        #   - the assignment *says* the Worker moved on (another task, or
        #     another generation of this one, or none at all) → discard.  The
        #     marker goes away and the timeout is re-decided from the card on a
        #     later cycle if the Worker really is stuck.
        #   - we simply cannot read the generation → hold.  A discard here
        #     would be re-requested and re-discarded every cycle, forever,
        #     because the card says the same thing next time (`_unprovable()`).
        if req.get("task_id") and self.queue_dir:
            verdict, detail = assignment_execution_verdict(
                self.queue_dir, agent, req.get("mission"), req.get("task_id"),
                self._bound_generation(req, None))
            if verdict == EXEC_UNREADABLE:
                return GUARD_UNPROVABLE, (
                    f"cannot prove the assignment still names the execution this "
                    f"retirement is about ({detail})"), current
            if verdict != EXEC_SAME:
                return GUARD_DISCARD, (
                    f"the assignment no longer names the execution this retirement "
                    f"is about ({verdict}: {detail})"), current
        return GUARD_OK, "", current

    def _unprovable(self, agent: str, req: Optional[dict], prog: Optional[dict],
                    why: str, target: str) -> Optional[str]:
        """The guard could not bind this retirement to a running execution.

        Discarding is the usual answer to "the premise no longer holds", and
        it is the right one when the assignment *says* the Worker moved on:
        the marker goes away and the timeout gets re-decided from the card.
        Here there is nothing to re-decide with.  The card will say exactly
        the same thing next cycle, so a discard is re-requested and
        re-discarded for as long as the Worker stays stuck — a silent
        livelock, which is the ghost this module exists to remove, only
        quieter.  So the marker is kept.

        Keeping it is also what protects the Worker: `has_marker()` stays
        raised, so watchdog stops re-firing the timeout, dispatcher stops
        offering it work, and `plan.sh pull` refuses on the same file.  It is
        quarantined rather than killed, and a human opens the quarantine by
        deleting `registry/retirements/<agent>.*`.

        Told once, and deliberately **without** the `--reset` recipe: a human
        following it would roll back a task that may be under way right now
        (`_settle_discarded()`).  What they are asked for is the one thing
        only they can supply — looking at what the Worker is actually doing.

        The message names the phase it stopped at, and says "from here on"
        rather than "nothing": reached from `_step_sigterm()` a SIGTERM has
        already gone out, and a report that claimed otherwise would send the
        reader looking for the wrong thing.
        """
        previous = prog or {}
        if previous.get("phase") == PHASE_UNPROVABLE:
            return None   # already held and already reported
        stopped_at = previous.get("phase") or "requested (no step taken yet)"
        self.log(f"[retire] {agent}: HOLDING this retirement at phase={stopped_at} "
                 f"— {why}. Nothing further killed, nothing reset; the marker stays "
                 f"until a human clears registry/retirements/{agent}.*")
        mission = (req or {}).get("mission") or previous.get("mission")
        task_id = (req or {}).get("task_id") or previous.get("task_id")
        task_line = (f"task {task_id} (mission={mission}) は in_progress のままです。"
                     if task_id and mission else "")
        reported = self._report_fields(
            agent, previous,
            f"watchdog は Worker {agent} の退役を途中で止めました "
            f"(phase={stopped_at})。この退役が「どの実行を終わらせるものだったか」"
            f"を証明できないためです ({why})。証拠が無いまま進めると、同名の "
            f"Worker が別の実行をしている場合にそれを巻き込むので、**ここから先は"
            f"何も kill せず、queue も書き換えていません**。{task_line}\n"
            f"確認してください: 窓 {target} の Worker がいま何を実行しているか / "
            f"queue/assignments/{agent} と隣の .identity サイドカーが揃っているか。\n"
            f"この marker がある間 {agent} は新しい task を pull できず、dispatcher "
            f"からも割り当てられません。解除は registry/retirements/{agent}.* を"
            f"削除してください。",
        )
        carried = _carried_from_request(req) if (prog is None and req) else {}
        if not self._write_progress(agent, previous, PHASE_UNPROVABLE,
                                    unprovable_reason=why,
                                    **reported, **carried):
            return "blocked"
        return "unprovable"

    def _recheck_unprovable(self, agent: str, req: Optional[dict],
                            prog: dict) -> Optional[str]:
        """A held retirement is released as soon as the assignment settles it.

        The hold exists because nothing could be proved; it does not have to
        outlive that.  If the assignment later says outright that this Worker
        moved on — it is gone, or it names another task — the premise is
        falsified without the generation entering into it, and the marker can
        be dropped exactly as `_guard()` would have dropped it.  That is the
        common recovery: the Worker was never stuck, it finished, and nobody
        had to touch anything.

        **Only those two answers release it.**  A generation that becomes
        readable and matching would mean quietly resuming a retirement the
        Director has already been told was stopped, and re-deciding that
        behind their back is the churn the hold exists to prevent.

        Read-only up to the two things it writes — `discarded`, which kills
        nothing and rewrites nothing, and the retry of a report that never got
        out.  The retry comes first: "the Director has already been told" is
        the premise the hold rests on, and while it is false this Worker is
        quarantined with nobody able to act on it.
        """
        delivered = self._retry_pending_report(agent, prog)
        if delivered:
            return delivered
        mission = (req or {}).get("mission") or prog.get("mission")
        task_id = (req or {}).get("task_id") or prog.get("task_id")
        if not (task_id and self.queue_dir):
            return None
        verdict, detail = assignment_execution_verdict(
            self.queue_dir, agent, mission, task_id,
            self._bound_generation(req, prog))
        if verdict not in (EXEC_OTHER, EXEC_ABSENT):
            return None
        why = f"held, then settled by the assignment ({verdict}: {detail})"
        self.log(f"[retire] {agent}: releasing the held retirement — {why}")
        self._write_progress(agent, prog, PHASE_DISCARDED, discard_reason=why)
        return "discarded"

    @staticmethod
    def _verified_pid(req: dict, identity: dict) -> Optional[int]:
        """The pid this step is allowed to act on, or None.

        Prefers the pid recorded in the request, which is the one the guard
        just compared against; falls back to the guard's own snapshot for the
        backends that record only a `created_at` (herdr's `pane_process_info`
        can fail while the window is perfectly listable).  Either way the value
        comes out of an identity check that has already passed — it is never a
        fresh resolution of a reusable window name.
        """
        recorded = (req.get("spawn_identity") or {}).get("pane_pid")
        if recorded is None:
            recorded = (identity or {}).get("pane_pid")
        if recorded is None:
            return None
        try:
            return int(recorded)
        except (TypeError, ValueError):
            return None

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
                                     **_carried_from_request(req))
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

        verdict, why, identity = self._guard(agent, req, target)
        if verdict == GUARD_SKIP:
            self.log(f"[retire] {agent}: skipping this cycle — {why}")
            return "skipped"
        if verdict == GUARD_UNPROVABLE:
            return self._unprovable(agent, req, None, why, target)
        if verdict == GUARD_DISCARD:
            self.log(f"[retire] {agent}: NOT retiring — {why}")
            self._write_progress(agent, None, PHASE_DISCARDED, discard_reason=why)
            return "discarded"

        # From the guard's snapshot, not a second lookup: this pid is what the
        # later phases signal, and re-resolving the window name here is how a
        # same-named successor's pid used to get persisted (Codex P1-3).
        pane_pid = self._verified_pid(req, identity)
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
                                    **_carried_from_request(req)):
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

        verdict, why, identity = self._guard(agent, req, target)
        if verdict == GUARD_SKIP:
            self.log(f"[retire] {agent}: REFUSING SIGTERM — {why}")
            return "skipped"
        if verdict == GUARD_UNPROVABLE:
            return self._unprovable(agent, req, prog, why, target)
        if verdict == GUARD_DISCARD:
            self.log(f"[retire] {agent}: NOT sending SIGTERM — {why}")
            self._write_progress(agent, prog, PHASE_DISCARDED, discard_reason=why)
            return "discarded"

        # The pid the guard just vouched for.  Asking `mux.pid(target)` again
        # here was Codex P1-3: a Worker that exits between the two calls hands
        # the window name to a same-named successor, whose pid would then be
        # written to the marker and SIGTERM'd without ever being compared to
        # the request.
        pane_pid = self._verified_pid(req, identity)
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

        verdict, why, identity = self._guard(agent, req, target)
        if verdict == GUARD_SKIP:
            self.log(f"[retire] {agent}: REFUSING SIGKILL — {why}")
            return "skipped"
        if verdict == GUARD_UNPROVABLE:
            return self._unprovable(agent, req, prog, why, target)
        if verdict == GUARD_DISCARD:
            self.log(f"[retire] {agent}: NOT sending SIGKILL — {why}")
            self._write_progress(agent, prog, PHASE_DISCARDED, discard_reason=why)
            return "discarded"

        # The recorded pid first: it is the process this machine already sent
        # SIGTERM to, so finishing the job on any other pid would be a second,
        # unannounced kill.  The fallback is still the guard's own snapshot,
        # never a fresh resolution of the window name (Codex P1-3).
        pane_pid = prog.get("pane_pid") or self._verified_pid(req, identity)
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

        Three outcomes, and which one applies turns entirely on what the
        **recorded pid** says — not on its absence.

        * recorded pid is gone → the damage is already done, the queue repair
          is still owed, and dropping the marker would strand the task.  The
          progress file carries mission/task_id for exactly this case.
        * recorded pid is alive → we have lost the identity record that
          authorises further steps; leave the Worker alone.
        * **no pid was ever recorded** → we know nothing, and that is not a
          death.  `build_progress()` fills `pane_pid` with `None` by default,
          so `process_alive(prog["pane_pid"])` answers `False` for a marker
          that simply never had one — which is the normal shape when the
          backend could only supply a `created_at` (herdr's
          `pane_process_info` failing while the window lists fine).  Reading
          that `False` as "already gone" sent such a retirement straight to
          the terminal phase, and the next cycle reset a live Worker's task
          (Codex 3 巡目 P1-1).

        The last case is deliberately a hold, not a discard: discarding would
        drop the only record that this task may still need repairing.  The
        exit from the hold is `_check_stall()`, which escalates once — the
        same shape every other "cannot tell yet" branch here uses, and the
        same direction: kill nothing, rewrite nothing.
        """
        recorded = recorded_pid(prog.get("pane_pid"))
        if recorded is None:
            self._log_waiting(
                agent,
                f"[retire] {agent}: request marker vanished and no usable pane_pid was "
                f"ever recorded — waiting instead of assuming a death (nothing killed, "
                f"nothing reset)")
            return "skipped"
        if process_alive(recorded):
            self.log(f"[retire] {agent}: request marker vanished mid-flight but "
                     f"pid {recorded} is still running — settling as discarded")
            self._write_progress(agent, prog, PHASE_DISCARDED,
                                 discard_reason="request marker vanished")
            return "discarded"
        self.log(
            f"[retire] {agent}: request marker vanished, but recorded pid {recorded} "
            f"is gone — completing the cleanup we still owe"
        )
        self._write_progress(agent, prog, PHASE_TERMINATED, window_gone=True)
        return "terminated"

    @staticmethod
    def _bound_generation(req: Optional[dict], prog: Optional[dict]) -> Optional[str]:
        """The assignment generation this retirement is bound to, or None.

        `None` means "no evidence", and there is no way to acquire it later:
        re-reading the card now would return whatever generation is current,
        which is precisely the successor's when there is one.  A recorded
        `null` is not evidence either — a card with no `started_at` names no
        execution to end.

        Request first, then progress: the request is where `request()` wrote
        it, and the progress file carries it forward for the case where the
        request is gone by the time the cleanup runs.  Key *presence* is what
        distinguishes "never found out" from a recorded value, which is why
        `build_request()` omits the key rather than storing a sentinel.
        """
        for source in ((req or {}), (prog or {})):
            if "task_started_at" not in source:
                continue
            value = source["task_started_at"]
            if value is None:
                return None
            text = str(value).strip()
            return text or None
        return None

    def _cleanup_deferred(self, agent: str, prog: dict, mission: str,
                          task_id: str) -> Optional[str]:
        """Terminated, but nothing may be rewritten: the generation is unknown.

        Without the generation the only preconditions left are status and
        worker, and both come back to their original values when a human
        resets the task and a same-named Worker pulls it again — crewvia
        reuses names by design.  Asserting only those would reset the
        successor's live execution, which is the exact race the generation
        check exists to prevent; weakening the premise *is* the defect, not a
        fallback (Codex 3 巡目 P1-2).

        So: no automatic cleanup, ever, for this marker.  A human is told
        once — with what to check, how to repair the task and how to clear the
        marker — and the marker is kept, because it is the only record that
        this task may still be a ghost.  Re-running the check every cycle
        would only re-confirm the same answer, so later cycles say nothing —
        except to finish delivering the one message, which is the entire
        value of this branch.  The Worker here is already dead, so a report
        that never arrives leaves the task `in_progress` with nobody aware of
        it: the same shape as the hold above, and the reason both go through
        `_report_fields()` rather than each deciding for itself.
        """
        why = (f"{mission}/{task_id} の実行世代 (started_at) を記録できていないため、"
               f"自動の後始末は行いません")
        if prog.get("cleanup_deferred"):
            return self._retry_pending_report(agent, prog)
        self.log(
            f"[retire] {agent}: {why} — 世代を後から読み直しても、そこにあるのは"
            f"後任の世代なので埋め合わせにならない。Director に上げて保留する"
        )
        reported = self._report_fields(
            agent, prog,
            f"watchdog が Worker {agent} を終了しましたが、その実行の世代 "
            f"(started_at) を記録できていませんでした。前提を弱めて自動で "
            f"reset すると、同名の後任が作業中の task を巻き戻す恐れがあるため、"
            f"**queue は何も書き換えていません**。\n"
            f"task {task_id} (mission={mission}) が in_progress のまま取り残されて"
            f"いないか確認し、必要なら plan.sh update {task_id} --status pending "
            f"--reset --mission {mission} を手で実行してください。"
            f"そのうえで registry/retirements/{agent}.* を削除してください。",
        )
        self._write_progress(agent, prog, PHASE_CLEANUP_FAILED,
                             cleanup_error=why, cleanup_deferred=True,
                             **reported,
                             mission=mission, task_id=task_id)
        return "cleanup_deferred"

    # -- terminal handling ---------------------------------------------

    def _settle_terminated(self, agent: str, req: Optional[dict], prog: dict,
                           keep_request: bool = False) -> Optional[str]:
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

        One call, not a set of fields (t024).  `plan.sh retire` takes the
        evidence — which mission, which task, which worker, which *generation*
        — and decides for itself, inside the queue lock, whether anything is
        owed.  This module hands over the evidence and reads the answer; it
        does not get to choose which preconditions to assert.  That choice was
        the shape of all nine P1s three rounds of review produced: wherever a
        caller could decide to assert less, some path eventually did.
        """
        mission = (req or {}).get("mission") or prog.get("mission")
        task_id = (req or {}).get("task_id") or prog.get("task_id")

        if task_id and mission:
            # Asked before plan.sh is even looked at: without the generation
            # there is no call to make, so a missing plan.sh is not the thing
            # to report, and reporting both would tell the Director twice
            # about one marker.  See `_bound_generation()`.
            generation = self._bound_generation(req, prog)
            if generation is None:
                return self._cleanup_deferred(agent, prog, mission, task_id)
            if not self._plan_sh_usable():
                return self._cleanup_failed(
                    agent, prog, f"plan.sh not usable at {self.plan_sh}", mission, task_id)
            # --no-wait: this call happens inside watchdog's monitoring cycle,
            # and plan.sh's lock is a *blocking* exclusive flock.  A busy queue
            # used to suspend every Worker's monitoring for the subprocess
            # timeout, once per marker and again on every retry — worse than
            # the 70s termination stall this module was written to remove
            # (Codex 4 巡目 P2).  Nothing is lost by giving up: the marker stays
            # in flight and the next cycle asks again.
            argv = ["bash", str(self.plan_sh), "retire", task_id,
                    "--agent", agent, "--started-at", generation,
                    "--mission", mission, "--no-wait"]
            env = dict(os.environ)
            if self.queue_dir:
                env["CREWVIA_QUEUE"] = str(self.queue_dir)
            env["CREWVIA_REPO_ROOT"] = str(self.repo_root)
            rc, output = self.run_command(argv, env)
            if rc == PLAN_LOCK_BUSY:
                # Not a failure: nothing was written and the obligation still
                # stands.  Deliberately *not* routed through _cleanup_failed(),
                # which would burn an attempt, tell the Director, and move the
                # phase to cleanup_failed — for a lock that is about to be
                # released anyway.  `_check_stall()` is the exit if the queue
                # never frees up.
                self._log_waiting(
                    agent,
                    f"[retire] {agent}: queue lock busy — deferring the cleanup of "
                    f"{mission}/{task_id} to a later cycle (nothing written)")
                return None
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
                    agent, prog, f"plan.sh retire exited {rc}: {output.strip()[:400]}",
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
        # instead of starting a second termination.  `keep_request` is the one
        # exception: the request on disk belongs to a *different* retirement,
        # so deleting it here would silently drop somebody else's ask.
        if not keep_request:
            unlink_quiet(request_path(self.registry_dir, agent))
        unlink_quiet(progress_path(self.registry_dir, agent))
        unlink_quiet(stall_path(self.registry_dir, agent))
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

    def _settle_discarded(self, agent: str, prog: dict, keep_request: bool = False) -> str:
        """R7 said no.  Drop the marker without touching the queue.

        Nothing was killed, so there is no ghost task to repair — a healthy
        Worker is wearing this name right now.  Sending D4's recovery recipe
        here would make a human reset a task that is being worked on.
        """
        self.log(
            f"[retire] {agent}: discarding marker "
            f"(reason: {prog.get('discard_reason')}) — Worker left alone"
        )
        if not keep_request:   # see _settle_terminated()
            unlink_quiet(request_path(self.registry_dir, agent))
        unlink_quiet(progress_path(self.registry_dir, agent))
        unlink_quiet(stall_path(self.registry_dir, agent))
        return "discarded_cleared"

    def _report(self, agent: str, prog: dict, message: str) -> bool:
        if self.notify is None:
            return False
        try:
            return bool(self.notify(message))
        except Exception as e:  # noqa: BLE001
            self.log(f"[retire] {agent}: director report failed: {type(e).__name__}: {e}")
            return False

    def _report_fields(self, agent: str, prog: dict, message: str) -> dict:
        """Report, and hand back the progress fields that record what happened.

        The phases that report *once* — `unprovable`, `cleanup_deferred` —
        used to persist `director_notified=<whatever _report returned>` and
        then never look at it again.  A notifier that was down for one cycle
        therefore turned "tell the Director, then hold" into "hold", silently:
        the marker keeps the Worker quarantined (`has_marker()` stops pull and
        dispatch) while the one message that explains why never arrives.  That
        is the ghost this module exists to remove, with the daemons keeping
        quiet about it.

        So a report that did not get through is kept as an obligation.  The
        message is stored verbatim rather than rebuilt later: the inputs it was
        composed from (the request, the guard's reason) may be gone by the time
        it is retried, and a reconstructed message would drift from the one the
        decision was actually made on.  Dedup starts **after** delivery, which
        is what `director_notified` is now allowed to mean.
        """
        ok = self._report(agent, prog, message)
        return {"director_notified": ok, "pending_report": None if ok else message}

    def _fallback_report(self, agent: str, prog: dict) -> str:
        """Rebuild a notification from persisted evidence when no verbatim one survived.

        `_report_fields()` started saving the exact message so a retry never
        has to guess.  A marker written by an older watchdog has
        `director_notified=False` with no `pending_report` at all — before
        this fix, `_retry_pending_report()` gave up silently on that shape and
        the Worker stayed quarantined forever, with `recover()` not migrating
        the field and `_check_stall()` deliberately staying quiet about these
        two phases (t034 / Codex 7巡目 P2-1). Everything the original message
        was built from except the guard's exact prose is still on the progress
        file, so this reconstructs an equivalent one instead of giving up.
        """
        phase = prog.get("phase") or "requested (no step taken yet)"
        mission = prog.get("mission")
        task_id = prog.get("task_id")
        task_line = (f"task {task_id} (mission={mission}) は in_progress のままです。"
                     if task_id and mission else
                     "この Worker がどの task を持っていたかは記録から読み取れません。")
        # `cleanup_deferred` means the Worker is already dead and the queue was
        # deliberately left untouched (see `_cleanup_deferred()`) — the reader
        # needs the manual reset recipe, not "go look at the window". A bare
        # `PHASE_CLEANUP_FAILED` without the flag never reaches this path: its
        # own retry (`_cleanup_failed()`) resends every cycle unconditionally.
        if prog.get("cleanup_deferred"):
            reason = prog.get("cleanup_error") or "実行世代 (started_at) を記録できていません"
            recipe = (f"plan.sh update {task_id} --status pending --reset "
                      f"--mission {mission} を手で実行してください。"
                      if task_id and mission else "")
            return (
                f"watchdog が Worker {agent} を終了しましたが、後始末を保留して"
                f"います ({reason})。この marker を書いた watchdog は元の通知本文を"
                f"残さないバージョンだったため、永続化された証拠から再構成した"
                f"通知です。**queue は何も書き換えていません**。{task_line}\n"
                f"{recipe}"
                f"そのうえで registry/retirements/{agent}.* を削除してください。"
            )
        reason = prog.get("unprovable_reason") or prog.get("discard_reason")
        reason_line = f" 理由: {reason}。" if reason else ""
        return (
            f"watchdog は Worker {agent} の退役を phase={phase} で保留したまま"
            f"止まっています。{reason_line}この marker を書いた watchdog は元の"
            f"通知本文を残さないバージョンだったため、永続化された証拠から再構成"
            f"した通知です。**この通知自体は何も kill せず queue も書き換えて"
            f"いません**。{task_line}\n"
            f"確認してください: 窓 {agent}-worker の Worker がいま何を実行しているか。\n"
            f"解除は registry/retirements/{agent}.* を削除してください。"
        )

    def _retry_pending_report(self, agent: str, prog: dict) -> Optional[str]:
        """Deliver a report an earlier cycle could not get out.  Changes nothing else.

        Delivery and persistence are retried independently: `identity` records
        in memory, for this process's lifetime, that the message already
        reached the Director, so a `_write_progress()` failure right after
        (disk full, etc.) retries only the receipt write on later cycles —
        never a second send of the same notification (t034 / Codex 7巡目
        P2-2).
        """
        if prog.get("director_notified"):
            return None
        identity = (agent, prog.get("request_id"))
        if identity not in self._delivered_reports:
            message = prog.get("pending_report") or self._fallback_report(agent, prog)
            if not self._report(agent, prog, message):
                self._log_waiting(
                    agent,
                    f"[retire] {agent}: still cannot reach the Director about this held "
                    f"retirement (phase={prog.get('phase')}) — retrying every cycle. "
                    f"The log line at the time of the decision is the fallback record.")
                return None
            self.log(f"[retire] {agent}: delivered the report that earlier cycles could not "
                     f"send (phase={prog.get('phase')})")
            self._delivered_reports.add(identity)
        if not self._write_progress(agent, prog, prog.get("phase"),
                                    director_notified=True, pending_report=None):
            self._log_waiting(
                agent,
                f"[retire] {agent}: delivered the report but could not persist the "
                f"receipt (phase={prog.get('phase')}) — will not resend, retrying "
                f"only the write.")
            return None
        self._delivered_reports.discard(identity)
        return "report_delivered"
