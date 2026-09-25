#!/usr/bin/env bash
# dispatcher.sh — Crewvia Worker dispatcher daemon (bg, 5-second poll)
#
# Observes:
#   queue/missions/**/tasks/*.md   pending tasks
#   tmux list-windows              live Worker / Director windows
#   queue/assignments/<NAME>       busy (exists) / idle (absent) state
#
# Dispatch logic (every 5 s):
#   idle Worker + unblocked pending task with skill intersection
#     → tmux send-keys assign message to Worker window
#   unblocked pending task with NO matching live Worker
#     → notify crewvia:Sora-director to spawn a Worker
#   Worker with zero tasks for its skill set (blocked included)
#     → send shutdown message and kill the window
#   All active missions done
#     → notify crewvia:Sora-director
#
# Notification dedup: same key is suppressed for NOTIFY_TTL seconds.
# State-based notices (needs_director / failed+handoff / codex-review refused) are sent
# ONCE per state, not per NOTIFY_TTL: see the ledger (registry/daemons/notified-state.json)
# and knowledge/notify-once.md (t010).
# Standalone-safe: exits 0 silently when tmux is not available.
#
# IMPORTANT — Daemon restart after code changes:
#   This script embeds Python as a heredoc.  The Python code is compiled once
#   at process start and held in memory for the lifetime of the daemon.
#   Any changes to this file take effect ONLY after the daemon is restarted:
#
#     # Kill the running daemon
#     pkill -f "dispatcher.sh" || true
#     # Restart (start.sh manages the tmux window automatically)
#     bash scripts/dispatcher.sh &
#
#   Symptom of a stale daemon: new filters / fixes appear in the file but
#   the old behaviour persists — always restart after updating dispatcher.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
QUEUE_DIR="${CREWVIA_QUEUE:-${REPO_ROOT}/queue}"
REGISTRY_DIR="${REPO_ROOT}/registry"
LOG_DIR="${REPO_ROOT}/logs/dispatcher"
LOG_FILE="${LOG_DIR}/dispatcher-$(date +%Y%m%d).log"
# CREWVIA_NOTIFY_CACHE: isolated-QA escape hatch.  The default path is shared by
# every dispatcher on the machine, so a test run would both read production
# dedup keys (false PASS: a suppressed notification looks like "did not fire")
# and write its own into them.  Tests point this at their own sandbox.
NOTIFY_CACHE="${CREWVIA_NOTIFY_CACHE:-/tmp/dispatcher-notify-cache.json}"
NOTIFY_TTL=300  # seconds before repeating the same notification (5 min)

# Rule 5: state grace period in seconds (env > config > default 60).
# Read from config/crewvia.yaml mux.state_grace_seconds if env is absent.
_read_state_grace() {
  local cfg="${REPO_ROOT}/config/crewvia.yaml"
  if [[ -n "${CREWVIA_STATE_GRACE:-}" ]]; then
    echo "${CREWVIA_STATE_GRACE}"
    return
  fi
  if [[ -f "$cfg" ]]; then
    # Look for "  state_grace_seconds: <n>" under "mux:" section.
    awk '/^mux:/{in_mux=1} in_mux && /state_grace_seconds:/{print $2; exit}' "$cfg"
  fi
}
STATE_GRACE="$(_read_state_grace)"
STATE_GRACE="${STATE_GRACE:-60}"  # default 60 seconds

# Standalone-safe: silently exit when no mux backend (tmux / herdr) is available
if ! python3 "${SCRIPT_DIR}/lib_mux.py" available >/dev/null 2>&1; then
  exit 0
fi

mkdir -p "$REGISTRY_DIR"
mkdir -p "$LOG_DIR"

# t005: 相互監視の bash 側。heartbeat を **bash から** 書くために source する。
# この daemon の本体は「毎サイクル作り直される python」ではなく、このループを
# 回している bash 自身なので、相手 (watchdog) が probe すべき PID もここの $$
# である。詳細は scripts/lib_daemon_watch.sh の冒頭。
# shellcheck source=lib_daemon_watch.sh
source "${SCRIPT_DIR}/lib_daemon_watch.sh"

log() {
  local msg
  msg="[dispatcher $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
  echo "$msg" >&2
  echo "$msg" >> "$LOG_FILE"
}

log "Starting dispatcher (PID $$, queue=$QUEUE_DIR)"

# ---------------------------------------------------------------------------
# One dispatch cycle — implemented in Python for YAML / file parsing
# ---------------------------------------------------------------------------
run_dispatch() {
  python3 - "$QUEUE_DIR" "$REGISTRY_DIR" "$NOTIFY_CACHE" "$NOTIFY_TTL" "$STATE_GRACE" "$LOG_FILE" <<'PYEOF'
import sys
import os
import re
import json
import time
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

QUEUE_DIR      = Path(sys.argv[1])
REGISTRY_DIR   = Path(sys.argv[2])
NOTIFY_CACHE   = Path(sys.argv[3])
NOTIFY_TTL     = int(sys.argv[4])
STATE_GRACE    = int(sys.argv[5]) if len(sys.argv) > 5 else 60
# LOG_FILE is passed from bash (argv[6]) so the date-based path stays consistent
# within a single dispatch cycle.  The bash wrapper updates it each cycle for
# midnight rotation.
LOG_FILE       = Path(sys.argv[6]) if len(sys.argv) > 6 else REGISTRY_DIR / 'dispatcher.log'
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Import lib_mux from scripts/ (same dir as this script via REGISTRY_DIR.parent)
REPO_ROOT = REGISTRY_DIR.parent
_SCRIPTS_DIR = REPO_ROOT / 'scripts'
sys.path.insert(0, str(_SCRIPTS_DIR))
from lib_mux import Mux, repo_identity_ok  # noqa: E402
import lib_retirement  # noqa: E402
# 「依存が満たされた」の定義は crewvia の中で 1 箇所しかない (t010 / QA t002 の
# 指摘 F-2b)。ここに同じ規則のコピーを書き戻さないこと — plan.sh pull が割り当て
# る task と dispatcher が投げる task がズレると、痛むのは QA FAIL の直後だけで、
# その瞬間まで誰も気付かない。tests/test_task_graph.py がコピーの再発を見張る。
from lib_dep_rules import unmet_dependencies  # noqa: E402
# task カードの読み取りも 1 箇所しかない (Codex 5 巡目 P2)。parser・「識別子は
# ファイル名」・信用できないカードの隔離を plan.sh 側だけに入れた結果、同じ queue を
# 2 つの別のコードが別の規則で読む状態になり、`id` 行の無いカードで **この
# サイクルが KeyError で落ちて全 mission の割り当てが止まる** 経路ができていた。
# ここに frontmatter を直接読むコードを書き戻さないこと。
# 再発防止は tests/test_task_card_identity.py。
from lib_task_cards import (  # noqa: E402,F401
    CORRUPT_TASK_STATUS, Unreadable, is_missing, is_unreadable, list_task_cards,
    parse_frontmatter, read_regular_text_or_unreadable, read_task_card,
)
# codex-review が「差分が大きすぎる」で拒否した事実の記録 (t010 / #11)。書き手は
# kai-review.sh、読み手はここ。定義はこのモジュールに 1 つだけ。
import lib_review_refusal  # noqa: E402
_mux = Mux()

# t002: who may end a Worker process.  'watchdog' (default) = this daemon only
# writes retirement markers and watchdog executes them; 'dispatcher' = the
# pre-t002 behaviour where this daemon kills windows itself.
#
# The same variable gates watchdog.py.  Flipping only one of the two is what
# the atomic-migration rule forbids: dispatcher-only rollback means both
# daemons kill independently and, because crewvia reuses Worker names, a
# late-arriving kill lands on an innocent successor (§5-2); watchdog-only
# rollback means nobody closes an idle window at all (§5-1).
KILL_AUTHORITY = (os.environ.get('CREWVIA_KILL_AUTHORITY') or '').strip().lower()
KILL_AUTHORITY = 'dispatcher' if KILL_AUTHORITY == 'dispatcher' else 'watchdog'

#: How long a request marker may sit unconsumed before we say so.  Until t005
#: (mutual watch) lands, watchdog is a single point of failure for closing
#: Workers: if it is dead, markers pile up and idle Workers simply linger,
#: which the notify dedup would otherwise keep almost invisible.
RETIREMENT_STALE_SECONDS = 300

MISSIONS_DIR   = QUEUE_DIR / 'missions'
ARCHIVE_DIR    = QUEUE_DIR / 'archive'
STATE_FILE     = QUEUE_DIR / 'state.yaml'
ASSIGNMENTS_DIR = QUEUE_DIR / 'assignments'
WORKERS_FILE   = REGISTRY_DIR / 'workers.yaml'
# LOG_FILE is set above from sys.argv[6] (date-based path from bash wrapper).
# Bug2 fix: persistent flag to track all_done state across dispatch cycles
ALL_DONE_STATE_FILE = REGISTRY_DIR / 'dispatcher_all_done.flag'

PRIORITY_ORDER  = {'high': 0, 'medium': 1, 'low': 2}
TERMINAL_STATUSES = {'done', 'verified', 'skipped'}

# Skills that mark a task as Director-only (handled directly by the Director,
# not dispatchable to any Worker).  Tasks with these skills are excluded from
# the "no worker available" notification loop so the Director is not spammed.
DIRECTOR_ONLY_SKILLS = {'director-only'}

# Skills routed to the Codex reviewer (Kai-codex) via kai-review.sh.
# When a task with any of these skills becomes unblocked-pending, the dispatcher
# background-spawns kai-review.sh instead of sending "no worker" to the Director.
# The task must carry a pr_number field in its frontmatter; otherwise the
# dispatcher logs a warning and leaves the task alone (Director escalation).
CODEX_REVIEW_SKILLS = {'codex-review'}
CODEX_REVIEW_AGENT = 'Kai-codex'  # must match registry/workers.yaml entry
KAI_REVIEW_SH = REGISTRY_DIR.parent / 'scripts' / 'kai-review.sh'
KAI_SPAWN_LOG_DIR = REGISTRY_DIR.parent / 'logs' / 'kai-spawn'

# Rule 2: blocked-stuck threshold (seconds).  If an idle Worker's only matching
# tasks have been blocked for longer than this, the Worker is sent shutdown.
# Uses task file mtime as a proxy for last_status_change.
BLOCKED_STUCK_THRESHOLD = 600  # 10 minutes

# Rule 5: state persistence files live in registry/mux/<name>.state.json.
# Format: {"state": "<blocked|working|idle|done|unknown>", "since": <epoch_float>}
STATE_JSON_DIR = REGISTRY_DIR / 'mux'

# Spawn grace period (seconds).  A freshly-spawned Worker needs ~10-30s
# (measured) to boot its TUI, run kickoff, and call `plan.sh pull` — until
# that pull writes queue/assignments/<agent>, the Worker looks idle to
# shutdown_idle_workers()/dispatch() even though it is only just starting.
# The dispatcher's 5s poll loop can catch a Worker mid-boot well before the
# pull lands, sending "タスクなし、shutdown" and killing the window — which
# throws away the ~48KB spawn prompt (start.sh) and forces Director to
# respawn + re-send, wasting far more tokens than a truly-idle Worker sitting
# for an extra cycle would.  So this constant deliberately leans toward NOT
# killing (grace generous, not tight): 90s is 3x the observed 10-30s startup
# time, while still being short enough that a genuinely-idle Worker is
# reaped within ~1-2 minutes of spawn, not left lingering indefinitely.
# Override via CREWVIA_SPAWN_GRACE for tests / tuning.
# t015 F4 (Seo review, LOW): int() on a non-numeric override used to raise
# ValueError at import time, before log()/LOG_FILE exist — the whole
# dispatcher daemon died silently on a typo'd env var. Fall back to the
# documented default instead (stderr only; log() isn't defined yet here).
try:
    SPAWN_GRACE_SECONDS = int(os.environ.get('CREWVIA_SPAWN_GRACE', '90'))
except ValueError:
    _bad = os.environ.get('CREWVIA_SPAWN_GRACE')
    print(f"[dispatcher] WARNING: invalid CREWVIA_SPAWN_GRACE={_bad!r} — using default 90", file=sys.stderr)
    SPAWN_GRACE_SECONDS = 90

# Circuit breaker for Taskvia API calls
TASKVIA_CB_FAILURES = 0
TASKVIA_CB_THRESHOLD = 3        # consecutive failures to trip
TASKVIA_CB_BACKOFF = 60         # seconds to wait after tripping
TASKVIA_CB_LAST_TRIP = 0.0

# ---------------------------------------------------------------------------
# Bench mode (CREWVIA_BENCH_MODE=1): gate-file-based assignment guard
# ---------------------------------------------------------------------------
# When enabled, the dispatcher checks /tmp/crewvia-bench-gate-<agent> before
# assigning any task to an idle Worker.  If the gate file exists, assignment
# is skipped for that cycle — benchmark-ctx.sh holds the gate while applying
# its context strategy (B: /clear, C: restart) and removes it when ready.
# This prevents the dispatcher from racing ahead and assigning the next task
# before the strategy action has been applied.
BENCH_MODE = os.environ.get('CREWVIA_BENCH_MODE', '') == '1'
BENCH_STRATEGY_CONF = Path('/tmp/crewvia-bench-strategy.conf')

def bench_gate_active(agent_name: str) -> bool:
    """Return True if the benchmark gate file exists for this agent."""
    if not BENCH_MODE:
        return False
    gate = Path(f'/tmp/crewvia-bench-gate-{agent_name}')
    return gate.exists()

def bench_current_strategy() -> str:
    """Read the current strategy (A/B/C) from the strategy conf file."""
    try:
        return BENCH_STRATEGY_CONF.read_text().strip()
    except OSError:
        return ''

def bench_worker_restarting(agent_name: str) -> bool:
    """Return True if benchmark-ctx.sh is mid-restart for this Worker (Strategy C).

    benchmark-ctx.sh writes queue/assignments/<agent>.restarting before killing
    the tmux window and removes it once the new Worker is running.  While the
    flag is present the dispatcher must not try to assign a task — the Worker
    window does not exist yet.
    """
    if not BENCH_MODE:
        return False
    flag = ASSIGNMENTS_DIR / f'{agent_name}.restarting'
    return flag.exists()

# Agent publish throttle: only publish heartbeats every PUBLISH_INTERVAL seconds
PUBLISH_INTERVAL = 60
LAST_PUBLISH_TIME = 0.0
LAST_PUBLISHED_AGENTS = set()   # names published in the last cycle

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    line = f"[dispatcher {ts}] {msg}"
    print(line, file=sys.stderr)
    try:
        with LOG_FILE.open('a') as f:
            f.write(line + '\n')
    except OSError:
        pass


# t003: self-identity check, once per dispatch cycle (this Python process is
# spawned fresh every cycle by the bash wrapper — see run_dispatch() in
# dispatcher.sh — so this doubles as both the "once at startup" and "once
# per loop" guard). A dispatcher started against a git worktree that has
# since been removed must stop dispatching (and, crucially, stop being
# *able* to kill anything) rather than keep running on inherited mux env
# pointing at a workspace it can no longer prove it owns. See
# repo_identity_ok() in lib_mux.py for the full rationale. This is the
# coarse half of the guard; tmux_kill_window() re-checks immediately before
# the actual kill for the fine-grained half (a worktree can be removed
# mid-cycle, after this check already passed).
if not repo_identity_ok(REPO_ROOT):
    log(
        f"FATAL: repo_root {REPO_ROOT} no longer exists or is not a git "
        f"checkout (worktree removed?). A stale dispatcher must not keep "
        f"running against inherited mux env — skipping this cycle entirely."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Minimal YAML parser (scalar fields + inline/block lists, no external deps)
# ---------------------------------------------------------------------------

def _split_inline_list(s):
    out, cur, in_q = [], [], None
    for ch in s:
        if in_q:
            cur.append(ch)
            if ch == in_q:
                in_q = None
            continue
        if ch in ('"', "'"):
            in_q = ch
            cur.append(ch)
            continue
        if ch == ',':
            out.append(''.join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        out.append(''.join(cur).strip())
    return [x for x in out if x]


def _scalar(val):
    if val in ('null', '~'):
        return None
    if val in ('true', 'True'):
        return True
    if val in ('false', 'False'):
        return False
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return val[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    if re.fullmatch(r'-?\d+', val):
        return int(val)
    return val


def parse_yaml(text):
    """Parse a minimal subset of YAML used in this project."""
    lines = text.splitlines()
    result = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            i += 1
            continue
        m = re.match(r'^([\w-]+):\s*(.*)$', line)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).rstrip()
        if val == '':
            i += 1
            items = []
            while i < len(lines):
                lst = re.match(r'^\s+-\s*(.*)$', lines[i])
                if lst:
                    items.append(_scalar(lst.group(1).strip()))
                    i += 1
                else:
                    break
            result[key] = items if items else None
        elif val.startswith('[') and val.endswith(']'):
            inner = val[1:-1].strip()
            result[key] = [_scalar(s.strip()) for s in _split_inline_list(inner)] if inner else []
            i += 1
        else:
            result[key] = _scalar(val)
            i += 1
    return result


# ---------------------------------------------------------------------------
# Frontmatter parser for task .md files
# ---------------------------------------------------------------------------
#
# `parse_frontmatter` は lib_task_cards から来る (import 部を参照)。ここに独自の
# 実装を置いていたのが Codex 5 巡目 P2 の指摘で、上の `parse_yaml` との違いが
# そのまま欠陥だった: 読めない行を黙って捨てるので、半分だけ読めた
# `status: pending` がそのまま信じられ、**中身の分からないカードが dispatch
# される**。plan.sh 側の parser は同じ行で例外を投げてカードを隔離していた。
#
# なお `parse_yaml` (この上) は **カード以外の YAML 専用** として残してある。
# `state.yaml` / `workers.yaml` / `mission.yaml` は手で編集される経路があり、
# 1 行の typo で常駐デーモンが毎サイクル死ぬと Worker の割り当てと生存監視が
# まとめて止まる。カードのほうは list_task_cards() が例外を `[破損]` に変えて
# 吸収するので、厳格な parser でも落ちない。

# ---------------------------------------------------------------------------
# State / workers / tasks loading
# ---------------------------------------------------------------------------

def read_queue_text(path, what):
    """queue / registry のファイルを **種類を確かめてから** 読む。

    読めたら `str`、読めなければ `Unreadable` —— 空文字でも None でもない。
    カードだけでなく `state.yaml` / `workers.yaml` / `mission.yaml` にも同じ
    ガードを当てる (Codex 8 巡目 P2)。ここは常駐デーモンなので、上限の無い
    `read_text()` が書き手のいない FIFO に当たると **サイクルごと座り込み、
    全 mission の割り当てが止まる**。1 枚のカードで落ちないようにしてある
    のと同じ理由で、1 つの壊れたファイルでも止まらないようにする。

    倒す先は呼び出し側が決める。ここで返すのは「読めなかった」だけ
    (memory: fail-direction-is-per-judgment)。
    """
    return read_regular_text_or_unreadable(
        path, warn=lambda msg: log(f"WARNING: {what}: {msg}"))


def read_assignment(agent_name):
    """`assignments/<agent>` を **種類を確かめてから** 読む。

    読めたら `"<slug>:<task_id>"` (前後の空白は落とす)、無い = `Unreadable`
    (`is_missing()` が True)、読めない = `Unreadable`。

    このファイルは `assignment_file.exists()` で「busy かどうか」を判定する
    経路と対になっているが、**中身を読むのは別の話**である。t017 のガードは
    この直後のカード読み取りにしか入っておらず、assignment 本体は素の
    `read_text()` のままだった (Codex 9 巡目 P2)。`registry/assignments/` は
    Worker 名で引かれるだけの短いファイルで、書くのは plan.sh の
    `_atomic_write` だけだが、置き違えた FIFO 1 枚で `publish_agents()` が
    返らなくなり —— それは `dispatch()` の **前** に走るので —— 全 mission の
    割り当てが止まる。
    """
    return read_queue_text(ASSIGNMENTS_DIR / agent_name, 'assignment file')


def load_state():
    """active mission の一覧。読めなければ `Unreadable` を **そのまま返す**。

    t017 まではここで `{}` に潰していた。潰すと `dispatch()` の
    `if not active_missions: shutdown_idle_workers()` に落ちて、**pending の
    仕事が残っているのに idle Worker の退役が認可される** (Codex 9 巡目 P1)。
    `Path.exists()` も同じ穴を持つ —— `EACCES` で stat できないときも False に
    なるので、「無いことを観測した」と「観測できなかった」が同じ分岐に入る。
    だから存在確認は `read_queue_text()` の `ENOENT` 1 本に寄せる。
    """
    text = read_queue_text(STATE_FILE, 'state file')
    if is_missing(text):
        return {}          # 本当に無い = active mission ゼロ
    if is_unreadable(text):
        return text        # 観測できなかった —— 呼び出し側が「空」と読めない形
    return parse_yaml(text)


def load_workers():
    """Return dict {name: {'skills': [...], ...}} from registry/workers.yaml.

    `load_state()` と同じく、読めなかったときは `Unreadable` を返す。ここも
    空に潰すと「Worker が 1 人もいない」と見分けが付かず、`publish_agents()` が
    **全エージェントを Taskvia から DELETE する** (= 観測の失敗が撤去の根拠に
    なる) 側へ倒れる。
    """
    text = read_queue_text(WORKERS_FILE, 'workers file')
    if is_missing(text):
        return {}          # 本当に無い = Worker 0 人
    if is_unreadable(text):
        return text
    data = parse_yaml(text)
    workers = {}
    # workers.yaml has a top-level 'workers' block list
    # parse_yaml returns it as a list of scalars which isn't right.
    # We need a proper block-list-of-mappings parser.
    # Instead, parse manually (同じ text を使う — 2 回読むと、その間に置き換え
    # られたファイルで data と workers が別の姿から作られる)。
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('- name:'):
            name = stripped[len('- name:'):].strip().strip('"\'')
            current = name
            workers[name] = {'skills': [], 'task_count': 0}
        elif current and re.match(r'\s+skills:', line):
            m = re.search(r'\[([^\]]*)\]', line)
            if m:
                inner = m.group(1).strip()
                if inner:
                    workers[current]['skills'] = [s.strip() for s in inner.split(',')]
                else:
                    workers[current]['skills'] = []
        elif current and re.match(r'\s+task_count:', line):
            m = re.search(r'task_count:\s*(\d+)', line)
            if m:
                workers[current]['task_count'] = int(m.group(1))
        elif current and re.match(r'\s+role:', line):
            role = line.split(':', 1)[1].strip()
            workers[current]['role'] = role
    return workers


def list_tasks_for_mission(slug):
    """Return list of (meta, body) sorted by task number.

    読み取りの規則は `scripts/lib_task_cards.py` にある —— `plan.sh` の
    `list_tasks()` が読むのと同じ 1 つのモジュールで、これが「pull が受理する
    カードと、ここがスケジュールするカードが一致する」の中身である。

    ここに自前の走査・parse を書き戻さないこと。読めないカードは例外ではなく
    `[破損]` (`CORRUPT_TASK_STATUS`) のカードとして返ってくるので、1 枚の事故で
    このサイクルが落ちることはない —— 倒れる先は常に「そのカードだけが動かない」。
    """
    return list_task_cards(MISSIONS_DIR / slug / 'tasks',
                           warn=lambda msg: log(f"WARNING: {msg}"))


def load_all_tasks(active_missions):
    """Return (all_tasks, done_ids_by_mission, task_statuses_by_mission).

    all_tasks: list of (slug, meta)
    done_ids_by_mission: {slug: set of task IDs whose status is in TERMINAL_STATUSES}
    task_statuses_by_mission: {slug: {task_id: status}} for blocked_by dep checks
    """
    all_tasks = []
    done_ids_by_mission = {}
    task_statuses_by_mission = {}
    for slug in active_missions:
        tasks = list_tasks_for_mission(slug)
        done_ids = {m['id'] for m, _ in tasks if m.get('status') in TERMINAL_STATUSES}
        done_ids_by_mission[slug] = done_ids
        task_statuses_by_mission[slug] = {m['id']: m.get('status') for m, _ in tasks}
        for meta, _ in tasks:
            all_tasks.append((slug, meta))
    return all_tasks, done_ids_by_mission, task_statuses_by_mission


# ---------------------------------------------------------------------------
# Notification dedup cache
# ---------------------------------------------------------------------------

def load_notify_cache():
    if not NOTIFY_CACHE.exists():
        return {}
    try:
        return json.loads(NOTIFY_CACHE.read_text())
    except Exception:
        return {}


def should_notify(key):
    cache = load_notify_cache()
    if key not in cache:
        return True
    return time.time() - cache[key] > NOTIFY_TTL


def record_notify(key):
    cache = load_notify_cache()
    cache[key] = time.time()
    try:
        NOTIFY_CACHE.write_text(json.dumps(cache))
    except OSError as e:
        log(f"WARNING: cannot write notify cache: {e}")


# ---------------------------------------------------------------------------
# State-notice ledger (t010 / #10)
# ---------------------------------------------------------------------------
#
# `should_notify()` は NOTIFY_TTL のスロットルであって **受領確認ではない**。
# needs_director / failed+handoff_path のような *状態ベース* の通知は、状態が続く
# かぎり TTL ごとに永久に再送された (2026-09-25、同一内容が数十通届いてユーザーが
# デーモンを手で止めた)。スロットルを長くしても直らない — 永久に再送されること
# が問題であって、間隔が問題ではない。
#
# そこで「この状態については既に伝えた」を、スロットルとは別に registry に持つ。
# 通知内容を決める入力 (status / reason / handoff_path / 拒否の記録) を畳んだ
# fingerprint が変わったときだけ再通知する。状態を離れた (task が pending に戻った
# 等) ら記録を捨てる — 同じ理由でもう一度落ちたのは新しい事象だから。
#
# 置き場が「まだ無い」(ENOENT) は初回で普通。**置き場が使えない** (壊れている・
# 書けない) は普通ではない: 「通知すべきものが無い」ではなく起動失敗として log に
# 出し、スロットルだけの旧挙動 (再送側) に倒す。黙って通知を止める側には倒さない
# — 10 時間の全停止 (t027) を作ったのは「通知が来ない」の方である。
TOLD_FILE = REGISTRY_DIR / 'daemons' / 'notified-state.json'


def _told_trouble(msg):
    """台帳が使えないことを log に出す。5 秒ごとに回るので TTL に 1 回だけ。"""
    if should_notify('told_ledger_trouble'):
        log(f"WARNING: notified-state: {msg} — 同じ状態の通知が "
            f"NOTIFY_TTL={NOTIFY_TTL}s ごとに再送されます (スロットルだけに戻っています)")
        record_notify('told_ledger_trouble')


def load_told():
    """`{notify_key: {'fp', 'kind', 'slug', 'task'}}`、または `Unreadable`。

    ENOENT (まだ無い) は `{}`。それ以外の失敗は `Unreadable` のまま返し、
    呼び出し側が「使えない」として扱う (空とは別)。
    """
    text = read_regular_text_or_unreadable(TOLD_FILE)
    if is_missing(text):
        return {}
    if is_unreadable(text):
        _told_trouble(f"{TOLD_FILE} を読めない ({text.reason})")
        return text
    try:
        data = json.loads(text)
    except ValueError as e:
        _told_trouble(f"{TOLD_FILE} が壊れている (malformed JSON: {e})")
        return Unreadable(TOLD_FILE, f'malformed JSON ({e})')
    if not isinstance(data, dict):
        _told_trouble(f"{TOLD_FILE} が壊れている (JSON object でない)")
        return Unreadable(TOLD_FILE, 'not a JSON object')
    return data


def save_told(told):
    try:
        TOLD_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TOLD_FILE.with_name(f'.{TOLD_FILE.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(told, ensure_ascii=False, sort_keys=True))
        os.replace(tmp, TOLD_FILE)
        return True
    except OSError as e:
        _told_trouble(f"{TOLD_FILE} に書けない ({e})")
        return False


def fingerprint(*parts):
    """通知内容を決める入力の畳み込み。入力が変われば変わる、それだけが要件。"""
    import hashlib
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


def already_told(told, key, fp):
    """`told` は `load_told()` の結果。使えない台帳は「伝えていない」(再送側)。"""
    if is_unreadable(told):
        return False
    entry = told.get(key)
    return isinstance(entry, dict) and entry.get('fp') == fp


def record_told(key, fp, kind, slug, task_id):
    """送れた通知を台帳に書く。台帳が壊れていたら作り直す (自己修復)。"""
    told = load_told()
    if is_unreadable(told):
        told = {}
    told[key] = {'fp': fp, 'kind': kind, 'slug': slug, 'task': task_id}
    return save_told(told)


def prune_told(live_keys, observed_slugs):
    """状態を離れた task の記録を捨てる。

    観測できた mission (`observed_slugs`) の task だけが対象。観測できなかった
    (破損カード・走査失敗) ものは「離れた」の証拠にならないので触らない。
    """
    told = load_told()
    if is_unreadable(told):
        return
    stale = [k for k, e in told.items()
             if isinstance(e, dict) and e.get('slug') in observed_slugs
             and k not in live_keys]
    if not stale:
        return
    for k in stale:
        del told[k]
    save_told(told)


def observed_missions(all_tasks, active_missions):
    """破損カードを 1 枚も含まない active mission (= 全 task を観測できた)。"""
    broken = {slug for slug, meta in all_tasks
              if meta.get('status') == CORRUPT_TASK_STATUS}
    return {slug for slug in active_missions if slug not in broken}


def notify_state_once(key, fp, kind, slug, task_id, build_msg, *, needs_director_live=True):
    """状態ベースの通知を、状態が変わるまで 1 回だけ送る。

    順序 (安い判定を先に): 台帳が「伝えた」→ 何もしない / スロットル → 何もしない /
    Director 不在 → 記録せず見送る (戻ったらすぐ送る) / 送る。
    スロットルの key に fingerprint を含めるのは 2 つの理由: (1) 状態が変わったら
    残っているスロットルに遮られず届く、(2) 台帳に書けなくても直後のサイクルで
    同じ通知が飛ばない。
    """
    told = load_told()
    if already_told(told, key, fp):
        return False
    throttle_key = f'{key}#{fp}'
    if not should_notify(throttle_key):
        return False
    if not needs_director_live:
        log(f'WARNING: {kind} — Director 不在のため通知スキップ: {slug}/{task_id}')
        return False
    if tmux_send(_director_name(), build_msg()):
        record_notify(throttle_key)
        record_told(key, fp, kind, slug, task_id)
        return True
    log(f"{kind} detected but mux send failed: {slug}/{task_id} (will retry)")
    return False


# ---------------------------------------------------------------------------
# mux helpers (delegated to lib_mux.Mux via _mux instance)
# ---------------------------------------------------------------------------

def tmux_list_worker_windows():
    """Return list of {'window_target': str, 'agent_name': str} for *-worker windows.

    window_target is the bare window name (no session prefix) — _mux.send/kill
    accept names, not 'session:name' targets.
    """
    names = _mux.list(suffix='-worker')
    return [
        {'window_target': name, 'agent_name': name[:-len('-worker')]}
        for name in names
    ]


def _director_name():
    """Return the name of the live Director window, falling back to 'Sora-director'."""
    names = _mux.list(suffix='-director')
    return names[0] if names else 'Sora-director'


def tmux_send(target, message):
    """Send a message to a mux window (Enter-terminated, 2-step for Claude TUI).

    Returns True on success, False on failure.  Callers must check the return
    value before calling record_notify() — recording a failed send as
    'delivered' would suppress retries for NOTIFY_TTL seconds.
    """
    ok = _mux.send(target, message)
    if ok:
        log(f"→ [{target}] {message[:120]}")
    else:
        log(f"WARNING: mux send to {target!r} failed")
    return ok


def tmux_kill_window(target):
    """Kill a mux window.

    t002: only reachable under CREWVIA_KILL_AUTHORITY=dispatcher (the rollback
    path).  In the default configuration this daemon does not kill Workers at
    all — see retire_worker().  Kept, with its `.firstseen` unlink intact, so
    that the rollback is a true return to the previous behaviour; the new
    sweep in sweep_spawn_grace_markers() is idempotent with it.

    t015 (QA FAIL on PR#190): also unlinks the `.firstseen` spawn-grace
    marker (see _spawn_time_fallback) on a successful kill. crewvia reuses
    Worker names (Haruto / Seo / Arjun / ...), so on the tmux backend
    (no created_at cache) a stale `.firstseen` from this dead Worker would
    make the *next* Worker spawned under the same name look already past
    SPAWN_GRACE_SECONDS — zero grace, killed within one dispatch cycle
    (measured: 33s from spawn). Deleting it here lets the next spawn under
    this name record a fresh firstseen and get the full grace window again.
    Only done when the kill actually succeeded — if it failed the window
    may still be alive, and touching its grace marker could either extend
    or reset protection it hasn't earned yet either way.
    Herdr backend: unaffected — its created_at lives in `<target>.json`,
    rewritten by lib_mux.py on every spawn, never in `.firstseen`.
    Best-effort: a failed unlink is logged, never raised (dispatcher must
    not crash on a kill path).

    t003: re-checks repo_identity_ok() immediately before the actual kill —
    the single choke point all kill call sites (idle-worker shutdown, no-task
    shutdown, blocked-stuck shutdown) funnel through.  t002 N4: the old list
    named "vanished-worker cleanup" here, which was never true — the vanished
    Worker path only notifies the Director, it has never killed anything. The
    once-per-cycle check at module load time (see repo_identity_ok(REPO_ROOT)
    above) only proves this process's identity was valid when the cycle
    started; a long-running cycle can still straddle a worktree removal.
    Fails closed: a failed check skips the kill entirely, it does not retry
    or escalate.
    """
    if not repo_identity_ok(REPO_ROOT):
        log(
            f"REFUSING to kill window {target!r}: self-identity check failed "
            f"for repo_root={REPO_ROOT} (missing or no longer a git checkout "
            f"— likely a removed worktree). Skipping this kill entirely."
        )
        return
    ok = _mux.kill(target)
    if ok:
        log(f"killed window: {target}")
        firstseen = STATE_JSON_DIR / f'{target}.firstseen'
        try:
            firstseen.unlink(missing_ok=True)
        except OSError as e:
            log(f"WARNING: failed to remove spawn-grace marker {firstseen}: {e}")
    else:
        log(f"WARNING: mux kill {target!r} failed")


# Writer side only: this daemon calls request()/has_marker() and never
# process_all().  Executing a marker is watchdog's job — that separation is the
# whole point of t002, so the executor is deliberately not driven from here.
_retirement = lib_retirement.RetirementExecutor(
    registry_dir=REGISTRY_DIR,
    repo_root=REPO_ROOT,
    mux=_mux,
    repo_identity_check=lambda: repo_identity_ok(REPO_ROOT),
    log=lambda msg: log(msg),
    queue_dir=QUEUE_DIR,
)


def retire_worker(agent_name, target, reason):
    """Ask for `agent_name` to be retired.  Returns True if the ask was recorded.

    This is where t002 moved the boundary.  All three "this Worker has no work
    left" paths (idle shutdown, no-task shutdown, Rule 2 blocked-stuck) used to
    end in `tmux_send(...)` + `tmux_kill_window(...)` right here.  The
    judgement is still ours — it reads queue/, which only this daemon does —
    but the killing is not: watchdog owns process lifetime, and it is the only
    one of the two that can also finish the queue-side cleanup afterwards.

    The shutdown message travels inside the marker so that exactly one daemon
    sends it.  Sending it here and killing there would put the two ends of the
    same action in different processes with no shared clock.
    """
    if KILL_AUTHORITY == 'dispatcher':
        sent = tmux_send(target, lib_retirement.SHUTDOWN_MESSAGE)
        time.sleep(1)  # allow the message to land before killing
        tmux_kill_window(target)
        # Callers gate record_notify() on this.  Reporting the send result (not
        # an unconditional True) keeps the rollback path's dedup behaviour
        # identical to pre-t002, where a failed send was retried next cycle.
        return sent

    if _retirement.has_marker(agent_name):
        # Already asked.  Re-writing the request would restart watchdog's
        # escalation from phase one every 5 seconds, so the Worker would be
        # told to shut down forever and never actually be signalled.
        return False
    return _retirement.request(agent_name, target, reason)


def sweep_spawn_grace_markers():
    """Delete `.firstseen` markers whose window is gone (t002 F1).

    `tmux_kill_window()` used to do this as a side effect of a successful
    kill, and that was the only place it happened.  With the kill gone from
    this daemon nobody would unlink them any more, and a stale marker is not
    cosmetic: on the tmux backend (no created_at cache) the *next* Worker
    spawned under the same reused name reads as already past SPAWN_GRACE, so
    it gets zero grace and is shut down within one cycle — measured at 33
    seconds from spawn when this regressed before (t015 / PR #190).

    Keying on the window being absent rather than on a kill succeeding also
    closes three holes that existed before t002: markers left behind when
    watchdog terminated a Worker, when a window vanished on its own, and when
    benchmark-ctx.sh killed one directly — none of those went through
    tmux_kill_window(), so none of them ever cleaned up.

    A backend that transiently lists nothing unlinks everything, which only
    grants the next spawn its full grace again — the safe direction, the same
    one watchdog's mass-kill guard leans.
    """
    try:
        markers = list(STATE_JSON_DIR.glob('*.firstseen'))
    except OSError:
        return
    if not markers:
        return
    live = set(_mux.list())
    for marker in markers:
        target = marker.name[:-len('.firstseen')]
        if target in live:
            continue
        try:
            marker.unlink(missing_ok=True)
            log(f"[spawn_grace] swept stale marker for vanished window {target!r}")
        except OSError as e:
            log(f"WARNING: failed to sweep spawn-grace marker {marker}: {e}")


def warn_on_unconsumed_retirements():
    """Say so when retirement markers are not being executed (§5-3 N3).

    Between t002 and t005 watchdog is the only thing that closes a Worker.  A
    watchdog that is dead or misconfigured produces no error of its own — the
    symptom is just idle Workers that never go away, and this daemon's notify
    dedup means even the request is logged at most once per 5 minutes.  One
    explicit line per stale marker turns "nothing visibly happening" into a
    fact someone can act on, and names the rollback.
    """
    now = time.time()
    for agent in lib_retirement.list_agents(REGISTRY_DIR):
        prog = lib_retirement.read_json(lib_retirement.progress_path(REGISTRY_DIR, agent))
        if prog is not None:
            continue  # watchdog has picked it up
        req = lib_retirement.read_json(lib_retirement.request_path(REGISTRY_DIR, agent))
        if not req:
            continue
        age = now - float(req.get('requested_at') or now)
        if age < RETIREMENT_STALE_SECONDS:
            continue
        notify_key = f'retire_stale_{agent}'
        if should_notify(notify_key):
            msg = (
                f"Worker {agent} の retirement marker が {age:.0f}s 未処理です。"
                f"watchdog が停止している可能性があります (watchdog タブを確認、"
                f"必要なら kill + respawn)。暫定回避は両デーモンを "
                f"CREWVIA_KILL_AUTHORITY=dispatcher で再起動。"
            )
            if tmux_send(_director_name(), msg):
                record_notify(notify_key)
            log(f"[retire] WARNING: {agent}: request unconsumed for {age:.0f}s — watchdog may be down")


def _mux_created_at(window_target: str):
    """Return the spawn epoch (float) for `window_target` from the Herdr
    cache (registry/mux/<window_target>.json, written by lib_mux.py
    HerdrBackend._write_cache), or None if unavailable.

    None covers: tmux backend (no such cache), a missing file, or a
    corrupt/unparseable one — callers fall back to _spawn_time_fallback().
    """
    p = STATE_JSON_DIR / f'{window_target}.json'
    try:
        # registry/ の固定パスもガードを通す (t018)。tmux backend ではこの
        # ファイルが存在しないのが普通なので、ENOENT は警告を出さない。
        raw = read_queue_text(p, 'mux cache')
        if is_unreadable(raw):
            return None
        data = json.loads(raw)
        ts = data.get('created_at')
        if not ts:
            return None
        dt = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _spawn_time_fallback(window_target: str) -> float:
    """Backend-agnostic fallback spawn timestamp for `window_target`.

    Used when _mux_created_at() has nothing (tmux backend has no created_at
    cache; a Herdr cache can also be missing/stale).  Records the first
    dispatch cycle this process observed the window and reuses it afterward,
    so grace stays time-bounded instead of depending on a Herdr-only file —
    a Worker with no cache does not get indefinite protection, just the same
    SPAWN_GRACE_SECONDS window measured from first sighting.
    """
    p = STATE_JSON_DIR / f'{window_target}.firstseen'
    try:
        raw = read_queue_text(p, 'spawn-grace marker')
        if not is_unreadable(raw):
            return float(raw.strip())
    except Exception:
        pass
    now = time.time()
    try:
        STATE_JSON_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(str(now))
        return now
    except OSError as e:
        # t015 F3 (Seo review, LOW): if registry/mux is not writable, the old
        # code still returned `now` every cycle, so in_spawn_grace() was
        # always True — a Worker with no task would never be reaped
        # (indefinite grace, the exact "無期限延命" this feature is meant to
        # avoid). Lean the other way instead: report an already-expired
        # timestamp (epoch 0) so grace reads as False and normal idle
        # shutdown still applies, per PR#190's "倒す方向" design intent.
        log(f"WARNING: cannot persist spawn-grace marker {p}: {e} — treating grace as expired")
        return 0.0


def in_spawn_grace(window_target: str) -> bool:
    """True if `window_target` was spawned within SPAWN_GRACE_SECONDS.

    Guards shutdown_idle_workers() and dispatch()'s no-task branch from
    killing a Worker that has not had time to run `plan.sh pull` yet (see
    SPAWN_GRACE_SECONDS comment for rationale / trade-off).
    """
    created = _mux_created_at(window_target)
    if created is None:
        created = _spawn_time_fallback(window_target)
    return (time.time() - created) < SPAWN_GRACE_SECONDS


# ---------------------------------------------------------------------------
# Main dispatch logic
# ---------------------------------------------------------------------------

AGENT_PRESENCE_TTL = 600  # 10 minutes — heartbeat files older than this are ignored


def publish_agents():
    """Publish active agents to Taskvia /api/agents.

    Heartbeat publish: every PUBLISH_INTERVAL (60s), POST all active agents.
    Departure publish: immediately DELETE agents that disappeared since last cycle.

    Circuit breaker: after TASKVIA_CB_THRESHOLD consecutive failures, skip
    for TASKVIA_CB_BACKOFF seconds before retrying.
    """
    global TASKVIA_CB_FAILURES, TASKVIA_CB_LAST_TRIP
    global LAST_PUBLISH_TIME, LAST_PUBLISHED_AGENTS

    taskvia_url = os.environ.get('TASKVIA_URL', 'https://taskvia.vercel.app')
    taskvia_token = os.environ.get('TASKVIA_TOKEN', '')
    if os.environ.get('CREWVIA_TASKVIA') == 'disabled' or not taskvia_token:
        return

    # Circuit breaker: skip if tripped and still in backoff
    if TASKVIA_CB_FAILURES >= TASKVIA_CB_THRESHOLD:
        elapsed = time.time() - TASKVIA_CB_LAST_TRIP
        if elapsed < TASKVIA_CB_BACKOFF:
            return
        log(f"Taskvia circuit breaker: retrying after {TASKVIA_CB_BACKOFF}s backoff")
        TASKVIA_CB_FAILURES = 0

    now = time.time()
    workers = load_workers()
    if is_unreadable(workers):
        # 「誰がいるか」を観測できていない。ここで空として進むと、下の
        # departure publish が **全員を DELETE する** —— 観測の失敗が撤去の
        # 根拠になる形 (memory: evidence-for-destructive-decisions)。
        log(f"WARNING: workers.yaml を観測できない ({workers.reason}) — "
            f"このサイクルは Taskvia への publish を見送る")
        return

    # Collect heartbeat mtimes for all agents
    heartbeats_dir = REGISTRY_DIR / 'heartbeats'
    hb_mtimes = {}
    if heartbeats_dir.exists():
        for hb_file in heartbeats_dir.iterdir():
            if hb_file.is_file() and not hb_file.name.startswith('.'):
                try:
                    hb_mtimes[hb_file.name] = hb_file.stat().st_mtime
                except OSError:
                    pass

    # Build current active agent set
    agents_to_publish = []
    current_agent_names = set()

    for name, info in workers.items():
        role = info.get('role', 'worker')
        skills = info.get('skills') or []

        if role == 'director':
            mtime = hb_mtimes.get(name, now)
            last_seen = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            agents_to_publish.append({
                'name': name, 'role': role, 'skills': skills,
                'current_task_id': None, 'current_task_title': None,
                'last_seen': last_seen,
            })
            current_agent_names.add(name)
        else:
            mtime = hb_mtimes.get(name)
            if mtime is None or now - mtime > AGENT_PRESENCE_TTL:
                continue

            task_id = None
            task_title = None
            assignment = read_assignment(name)
            if not is_unreadable(assignment):
                try:
                    assignment = assignment.strip()
                    if ':' in assignment:
                        mission_slug, task_id = assignment.split(':', 1)
                        task_file = MISSIONS_DIR / mission_slug / 'tasks' / f'{task_id}.md'
                        if task_file.exists():
                            # カードの読み取りは lib_task_cards を通すこと
                            # (Codex 8 巡目 P2)。ここは列挙ではなく固定パスなので
                            # t016 のガードから漏れていた —— `read_text()` には
                            # 上限が無いので、割り当て済みカードが FIFO に
                            # 置き換わると **この関数が返らない**。publish_agents()
                            # は dispatch() より前に走るため、止まるのは全 mission の
                            # 割り当てである。read_task_card() は例外を投げず、
                            # 読めないカードは `[破損]` の title で返る。
                            meta, _ = read_task_card(task_file, task_id)
                            task_title = meta.get('title')
                except Exception:
                    pass

            last_seen = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            agents_to_publish.append({
                'name': name, 'role': role, 'skills': skills,
                'current_task_id': task_id, 'current_task_title': task_title,
                'last_seen': last_seen,
            })
            current_agent_names.add(name)

    endpoint = f'{taskvia_url}/api/agents'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {taskvia_token}',
    }

    any_failure = False

    # Immediate DELETE for departed agents
    departed = LAST_PUBLISHED_AGENTS - current_agent_names
    for name in departed:
        payload = json.dumps({'name': name}).encode('utf-8')
        try:
            req = urllib.request.Request(endpoint, data=payload, headers=headers, method='DELETE')
            with urllib.request.urlopen(req, timeout=5):
                pass
            log(f"Agent departed: {name} (deleted from Taskvia)")
        except Exception as e:
            any_failure = True
            log(f"WARNING: /api/agents DELETE failed for {name}: {e}")
            break

    # Throttled heartbeat POST (every PUBLISH_INTERVAL)
    if not any_failure and now - LAST_PUBLISH_TIME >= PUBLISH_INTERVAL:
        for agent in agents_to_publish:
            payload = json.dumps(agent).encode('utf-8')
            try:
                req = urllib.request.Request(endpoint, data=payload, headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=5):
                    pass
            except Exception as e:
                any_failure = True
                log(f"WARNING: /api/agents publish failed for {agent['name']}: {e}")
                break
        if not any_failure:
            LAST_PUBLISH_TIME = now

    # Update state and circuit breaker
    if not any_failure:
        LAST_PUBLISHED_AGENTS = current_agent_names
        if TASKVIA_CB_FAILURES > 0:
            log("Taskvia circuit breaker reset (publish succeeded)")
        TASKVIA_CB_FAILURES = 0
    else:
        TASKVIA_CB_FAILURES += 1
        if TASKVIA_CB_FAILURES >= TASKVIA_CB_THRESHOLD:
            TASKVIA_CB_LAST_TRIP = time.time()
            log(f"Taskvia circuit breaker TRIPPED after {TASKVIA_CB_FAILURES} consecutive failures. Backing off {TASKVIA_CB_BACKOFF}s.")


def was_all_done_last_cycle():
    """Return True if the previous dispatch cycle also saw all_done=True."""
    return ALL_DONE_STATE_FILE.exists()


def set_all_done_state(done):
    """Persist all_done state so the next cycle can detect transitions."""
    if done:
        try:
            ALL_DONE_STATE_FILE.touch()
        except OSError as e:
            log(f"WARNING: cannot write all_done state: {e}")
    else:
        try:
            ALL_DONE_STATE_FILE.unlink()
        except FileNotFoundError:
            pass


def _state_json_path(name: str) -> Path:
    return STATE_JSON_DIR / f'{name}.state.json'


def _load_state_entry(name: str) -> dict:
    """Load state persistence entry.  Returns {} on missing / corrupt file."""
    p = _state_json_path(name)
    try:
        raw = read_queue_text(p, 'rule5 state entry')
        if is_unreadable(raw):
            # 倒す先はここだけ「空」でよい —— grace が最初からやり直しになる
            # = **通知が遅れる**側で、破壊も割り当ても起こらない
            # (knowledge/empty-vs-unobservable.md §2 の I)。
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def _save_state_entry(name: str, state: str, since: float) -> None:
    """Persist state + since timestamp for Rule 5 grace tracking."""
    try:
        STATE_JSON_DIR.mkdir(parents=True, exist_ok=True)
        _state_json_path(name).write_text(
            json.dumps({'state': state, 'since': since}), encoding='utf-8'
        )
    except Exception as e:
        log(f'WARNING: cannot write state entry for {name!r}: {e}')


def check_rule5(name: str, target: str, assignment_file: Path, task_statuses_by_mission: dict) -> None:
    """Rule 5: detect blocked / idle-with-task and notify Director.

    A: state == "blocked"
    B: state in {"idle", "done"} AND assignment_file exists

    State is persisted to registry/mux/<name>.state.json for grace tracking.
    Notifications are deduped via the standard NOTIFY_TTL cache.

    tmux mode (state == "unknown") → skip entirely (safe side).

    t032 F4: cmd_needs_director leaves the assignment file in place (no
    retire_assignment call), so once the escalating Worker goes idle,
    condition B fires here too and Director gets both a
    "[needs_director]" and a "[Rule 5] idle-with-task" notification for the
    same root cause, forever (every NOTIFY_TTL). Fold into the
    needs_director notification instead — same exclusivity idea as the
    failed+handoff_path block, which only fires for status=='failed'.
    """
    # IMPORTANT: mux pane labels are '<name>-worker' (e.g. 'Omar-worker'), not
    # the bare agent name.  Use `target` (= window_target from
    # tmux_list_worker_windows) so the label lookup succeeds.  Using `name`
    # always returns 'unknown' (pane not found) and silently disables Rule 5.
    st = _mux.state(target)

    # unknown / working → no action.  tmux always returns unknown → skip.
    if st in ('unknown', 'working'):
        # If state returned to working, clear notify dedup keys so re-notification
        # fires when the worker gets stuck again later.
        if st == 'working':
            cache = load_notify_cache()
            for key in (f'blocked_{name}', f'idle_with_task_{name}'):
                cache.pop(key, None)
            try:
                NOTIFY_CACHE.write_text(json.dumps(cache), encoding='utf-8')
            except Exception:
                pass
            # Also reset state entry so grace timer restarts cleanly.
            _save_state_entry(name, 'working', time.time())
        return

    # Determine which condition applies.
    is_A = (st == 'blocked')
    is_B = (st in ('idle', 'done')) and assignment_file.exists()

    # t032 F4: if the assignment points at a task that already escalated to
    # needs_director, the needs_director block owns notifying Director for
    # it — suppress Rule 5 entirely for this worker/task pair.
    if is_A or is_B:
        assigned_task_status = None
        raw = read_assignment(name)
        if not is_unreadable(raw):
            try:
                a_slug, _, a_task_id = raw.strip().partition(':')
                if not a_task_id:
                    a_task_id = a_slug
                    a_slug = None
                if a_slug is not None:
                    assigned_task_status = task_statuses_by_mission.get(a_slug, {}).get(a_task_id)
            except Exception:
                assigned_task_status = None
        if assigned_task_status == 'needs_director':
            is_A = False
            is_B = False

    if not is_A and not is_B:
        # idle/done without assignment → not B.  Reset state entry.
        _save_state_entry(name, st, time.time())
        return

    # Load persisted state to check grace period.
    entry = _load_state_entry(name)
    prev_state = entry.get('state', '')
    since = entry.get('since', 0.0)
    now = time.time()

    # Normalize condition key to distinguish A vs B.
    condition = 'blocked' if is_A else 'idle-with-task'

    # If the condition changed (or is fresh), reset the timer.
    if prev_state != condition:
        _save_state_entry(name, condition, now)
        return  # grace period starts now; don't notify yet

    elapsed = now - since
    if elapsed < STATE_GRACE:
        return  # grace period not yet exceeded

    # Grace exceeded — notify Director if not already notified recently.
    notify_key = f'blocked_{name}' if is_A else f'idle_with_task_{name}'
    if not should_notify(notify_key):
        return

    # Collect task info from assignment file.
    task_id = '?'
    mission_slug = '?'
    try:
        raw = read_assignment(name)
        if not is_unreadable(raw):
            raw = raw.strip()
            if ':' in raw:
                mission_slug, task_id = raw.split(':', 1)
            else:
                task_id = raw
    except Exception:
        pass

    # Collect screen tail for context.
    screen_tail = ''
    try:
        screen = _mux.capture(target)  # use target (= '<name>-worker') not name
        if screen:
            lines = screen.splitlines()
            screen_tail = '\n'.join(lines[-5:]) if len(lines) >= 5 else '\n'.join(lines)
    except Exception:
        pass

    director = _director_name()
    director_live = bool(_mux.list(suffix='-director'))

    msg = (
        f'[Rule 5] Worker {name} が {condition} です '
        f'(task {task_id}, mission={mission_slug}, {elapsed:.0f}秒継続)。'
        f'画面末尾:\n{screen_tail}'
    )

    if director_live:
        sent = tmux_send(director, msg)
    else:
        log(f'WARNING: Rule 5 — Director 不在のため通知スキップ: {msg[:200]}')
        sent = False

    if sent:
        record_notify(notify_key)


def spawn_kai_review(slug, meta):
    """Background-spawn kai-review.sh for a codex-review task.

    Preconditions:
      - meta['skills'] contains 'codex-review'
      - task is unblocked and pending
      - caller has already checked notify dedup (via should_notify)

    Behavior:
      - Extracts pr_number from frontmatter; if absent, logs warning and returns.
      - Refuses to spawn while queue/assignments/Kai-codex exists (another run
        of the same agent is already in flight — either this task or another).
      - Launches nohup kai-review.sh in a detached process group so the 5s
        poll loop does not block waiting for the codex CLI to finish.
      - stdout/stderr go to logs/kai-spawn/<slug>-<task_id>-<epoch>.log so
        crashes are diagnosable after the fact.
    """
    task_id = meta.get('id', '?')
    pr = meta.get('pr_number')
    if not pr:
        # No PR number → cannot review.  Log once (dedup'd) and let the Director
        # notice via the standard "no worker" fallback (also dedup'd).
        _key = f'kai_no_pr_{slug}_{task_id}'
        if should_notify(_key):
            log(
                f"WARNING: codex-review task {slug}/{task_id} has no pr_number "
                f"in frontmatter — cannot spawn kai-review.sh. "
                f"Set it via `plan.sh update {task_id} --pr-number <N> --mission {slug}`."
            )
            record_notify(_key)
        return False
    # Only one Kai-codex run at a time.
    if (ASSIGNMENTS_DIR / CODEX_REVIEW_AGENT).exists():
        return False
    if not KAI_REVIEW_SH.exists():
        log(f"ERROR: kai-review.sh not found at {KAI_REVIEW_SH} — cannot spawn Codex review")
        return False

    try:
        KAI_SPAWN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"WARNING: cannot create kai-spawn log dir {KAI_SPAWN_LOG_DIR}: {e}")
        return False
    log_path = KAI_SPAWN_LOG_DIR / f"{slug}-{task_id}-{int(time.time())}.log"

    cmd = [
        'bash', str(KAI_REVIEW_SH),
        '--pr', str(pr),
        '--task', task_id,
        '--mission', slug,
        '--agent', CODEX_REVIEW_AGENT,
    ]
    try:
        # Detach: start_new_session=True + close stdin + redirect stdout/stderr
        # so the child survives the dispatcher's next cycle.  Do NOT wait().
        logf = open(log_path, 'ab')
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(REGISTRY_DIR.parent),
        )
        # Close in the parent — child inherits its own fd.
        logf.close()
        log(f"→ spawned kai-review.sh for {slug}/{task_id} (PR#{pr}, log={log_path.name})")
        return True
    except Exception as e:
        log(f"ERROR: failed to spawn kai-review.sh for {slug}/{task_id}: {e}")
        return False


def review_refusal_for(slug, meta):
    """このcodex-review task に、差分サイズ超過の拒否記録が効いているか (t010 / #11)。

    -> ('none', None)          記録が無い / 別の PR 番号 (= 出し直した) → 通常どおり spawn
       ('refused', rec)        拒否済み → 再 spawn しない
       ('unreadable', Unreadable)  記録が読めない・壊れている → **保留** (spawn しない)

    読めない記録を「拒否されていない」に倒さない: 壊れた記録 1 枚で
    「spawn → 拒否 → needs_director → pending → spawn」のループが戻るため。
    """
    task_id = meta.get('id', '?')
    try:
        rec = lib_review_refusal.load(REGISTRY_DIR, slug, task_id)
    except ValueError as e:      # ファイル名に使えない slug / id
        return 'unreadable', Unreadable(REGISTRY_DIR, str(e))
    if is_missing(rec):
        return 'none', None
    if is_unreadable(rec):
        return 'unreadable', rec
    if not lib_review_refusal.refused_for_pr(rec, meta.get('pr_number')):
        return 'none', None
    return 'refused', rec


def _refusal_clear_hint(slug, task_id):
    return (f"python3 {REPO_ROOT / 'scripts' / 'lib_review_refusal.py'} clear "
            f"--mission {slug} --task {task_id}")


def refusal_note(slug, meta):
    """needs_director 通知に添える 1 文 (拒否済みのときだけ)。無ければ ''。"""
    if not (set(meta.get('skills') or []) & CODEX_REVIEW_SKILLS):
        return '', None
    state, rec = review_refusal_for(slug, meta)
    if state != 'refused':
        return '', None
    task_id = meta.get('id', '?')
    note = (f" ※ codex-review は差分サイズ超過で拒否済み: "
            f"{lib_review_refusal.describe(rec)}。手動差分レビューに切り替えてください "
            f"(dispatcher はこの task を再 spawn しません。codex-review を意図して再試行するなら "
            f"{_refusal_clear_hint(slug, task_id)} 、PR を分割したなら "
            f"plan.sh update {task_id} --pr-number <新PR> --mission {slug})。")
    return note, (rec['pr'], rec['diff_bytes'], rec['max_bytes'])


def handle_codex_review(slug, meta, live_state_keys):
    """unblocked-pending な codex-review task: 拒否済みなら知らせて止め、そうでなければ spawn。"""
    task_id = meta['id']
    state, rec = review_refusal_for(slug, meta)
    if state != 'none':
        key = f'review_refused_{slug}_{task_id}'
        live_state_keys.add(key)
        if state == 'refused':
            fp = fingerprint('review_refused', rec['pr'], rec['diff_bytes'], rec['max_bytes'])
            def build_msg():
                return (
                    f"[review-refused] task {task_id} (mission={slug}): codex-review は拒否済みです "
                    f"({lib_review_refusal.describe(rec)})。差分が大きいと codex は途中を切り詰め、"
                    f"空の結果を信用できないため、dispatcher は再 spawn しません。"
                    f"手動差分レビューに切り替え、結果を plan.sh done {task_id} --mission {slug} "
                    f"で報告してください。codex-review を意図して再試行するなら "
                    f"{_refusal_clear_hint(slug, task_id)} 、PR を分割したなら "
                    f"plan.sh update {task_id} --pr-number <新PR> --mission {slug}。"
                )
        else:
            fp = fingerprint('review_refused', 'unreadable', rec.reason)
            def build_msg():
                return (
                    f"[review-refused] task {task_id} (mission={slug}): codex-review の拒否記録が"
                    f"読めない/壊れているため spawn を保留しています ({rec.reason})。"
                    f"記録を確認し、意図して再試行するなら "
                    f"{_refusal_clear_hint(slug, task_id)} 、そうでなければ手動差分レビューに"
                    f"切り替えてください。"
                )
        notify_state_once(key, fp, 'review-refused', slug, task_id, build_msg,
                          needs_director_live=director_live_for_state_notices())
        return
    spawn_key = f'kai_spawn_{slug}_{task_id}'
    if should_notify(spawn_key):
        if spawn_kai_review(slug, meta):
            record_notify(spawn_key)


def director_live_for_state_notices():
    return bool(_mux.list(suffix='-director'))


def shutdown_idle_workers():
    """Request retirement of idle Worker windows (D1).

    t002: this decides, it no longer executes.  See retire_worker().
    """
    windows = tmux_list_worker_windows()
    for window in windows:
        agent_name = window['agent_name']
        target = window['window_target']
        assignment_file = ASSIGNMENTS_DIR / agent_name
        is_idle = not assignment_file.exists()
        if is_idle:
            if in_spawn_grace(target):
                log(f"[spawn_grace] {agent_name}: within {SPAWN_GRACE_SECONDS}s spawn grace — skip shutdown")
                continue
            notify_key = f"shutdown_{agent_name}"
            if should_notify(notify_key):
                if retire_worker(agent_name, target, 'all-done'):
                    record_notify(notify_key)


def dispatch():
    state = load_state()
    if is_unreadable(state):
        # Codex 9 巡目 P1。`{}` に潰すと下の `if not active_missions` に落ちて
        # **退役を認可する**。何が active なのか観測できていない以上、割り当ても
        # 退役も結論できない —— このサイクルは丸ごと見送る。次の 5 秒後にもう
        # 一度読む (state.yaml は `os.replace` でしか書かれないので、原因が
        # 直れば自然に戻る)。
        log(f"WARNING: state.yaml を観測できない ({state.reason}) — "
            f"このサイクルは割り当ても退役も行わない")
        return

    workers = load_workers()
    if is_unreadable(workers):
        # 誰が idle なのかを決める材料が無い。同上、結論を出さない。
        log(f"WARNING: workers.yaml を観測できない ({workers.reason}) — "
            f"このサイクルは割り当ても退役も行わない")
        return

    active_missions = list(state.get('active_missions') or [])

    # Bug1 fix: even with no active missions, shut down lingering idle Workers
    if not active_missions:
        shutdown_idle_workers()
        set_all_done_state(False)
        return

    # Check if all active missions are done (empty list = all done)
    all_done = True
    for slug in active_missions:
        mfile = MISSIONS_DIR / slug / 'mission.yaml'
        # `exists()` での事前確認は置かない —— `EACCES` で stat できないときも
        # False になるので、「無い」と「観測できない」が同じ分岐に入る。
        # 欠損も `is_unreadable()` に含まれ、どちらも「完了ではない」に倒す。
        mtext = read_queue_text(mfile, 'mission file')
        if is_unreadable(mtext):
            # 読めなかった mission を「完了した」に数えない。この判定の先には
            # 全 idle Worker の shutdown があるので、観測の失敗はそこへ落として
            # はいけない (memory: evidence-for-destructive-decisions)。
            all_done = False
            break
        m = parse_yaml(mtext)
        if m.get('status') != 'done':
            all_done = False
            break

    if all_done:
        # Shut down all idle workers before notifying director
        shutdown_idle_workers()

        # Bug2 fix: notify only on False→True transition, not every cycle
        prev_all_done = was_all_done_last_cycle()
        if not prev_all_done:
            tmux_send(_director_name(), '全ミッション完了')
            log("→ all missions done — notified director (one-shot)")

        set_all_done_state(True)
        return

    # Missions still in progress — clear the all_done flag
    set_all_done_state(False)

    # Load all tasks
    all_tasks, done_ids_by_mission, task_statuses_by_mission = load_all_tasks(active_missions)
    # t010: 状態ベースの通知 (needs_director / handoff / review-refused) が今なお
    # 成り立っている key。サイクルの最後に、成り立たなくなった記録を捨てるのに使う。
    live_state_keys = set()

    # Unblocked pending tasks (eligible for assignment), sorted by priority
    unblocked_pending = []
    for slug, meta in all_tasks:
        if meta.get('status') != 'pending':
            continue
        done_ids = done_ids_by_mission.get(slug, set())
        task_statuses = task_statuses_by_mission.get(slug, {})
        bb = meta.get('blocked_by') or []
        # 規則は lib_dep_rules に 1 つだけ (plan.sh pull / task-graph と共有)。
        unmet_deps = unmet_dependencies(bb, done_ids, task_statuses)
        if unmet_deps:
            # Suppress repeated output of the same blocked state to avoid
            # flooding the scrollback (same line every 5s → 40-line buffer fills
            # up with identical rows, hiding diagnostic send logs).
            # Log once on first detection, then at most once per NOTIFY_TTL
            # (default 300s) as a heartbeat so the state remains visible.
            _bkey = f"blocked_log_{slug}_{meta.get('id')}"
            if should_notify(_bkey):
                log(f"[blocked] task {meta.get('id')} (mission={slug}) — unmet deps: "
                    f"{unmet_deps} (statuses: {[task_statuses.get(d) for d in unmet_deps]})")
                record_notify(_bkey)
            continue
        task_skills = set(meta.get('skills') or [])
        if not task_skills:
            log(f"WARNING: task {meta.get('id')} (mission={slug}) has no skills — dispatcher cannot assign it")
            continue
        if task_skills & DIRECTOR_ONLY_SKILLS:
            # Task is handled directly by the Director — skip Worker assignment
            # and suppress "no worker" notifications for it.
            continue
        unblocked_pending.append((slug, meta))
    unblocked_pending.sort(key=lambda c: PRIORITY_ORDER.get(c[1].get('priority', 'medium'), 1))

    # All pending tasks (including blocked), for shutdown eligibility check
    all_pending = [(slug, meta) for slug, meta in all_tasks if meta.get('status') == 'pending']

    # Live Worker windows (herdr pane list — used for assignment and Rule 5)
    windows = tmux_list_worker_windows()

    # Alive workers — used for "can_handle" check only.
    # OR condition: heartbeat fresh OR window exists (fix for heartbeat gap, PR #154 follow-up).
    #
    # Rationale:
    #   - heartbeat fresh alone (PR #154): handles herdr pane_list transient [] case.
    #     Workers write heartbeat every HEARTBEAT_INTERVAL s, independent of herdr state.
    #   - window exists alone: handles idle Workers whose heartbeat aged past
    #     AGENT_PRESENCE_TTL (600s) but are still running (herdr window present).
    #     Without this, a Worker idle for >10 min is misclassified as dead →
    #     false "no worker" notification fired (OBS-1 symptom).
    #   - Both stale + no window → truly dead → can_handle=False (correct).
    _window_agent_names: set = {w['agent_name'] for w in windows}
    _hb_dir = REGISTRY_DIR / 'heartbeats'
    # Seed with all window-present workers first: window exists → alive (OR condition, complete).
    # Fresh-spawn Workers write their first heartbeat only on their first tool invocation
    # (hooks/post-tool-use.sh line 79), so iterating heartbeat files alone misses them.
    # The heartbeat loop below unions in heartbeat-fresh Workers (e.g. herdr transient []).
    _alive_workers: set = set(_window_agent_names)
    if _hb_dir.exists():
        _now_hb = time.time()
        for _hb_file in _hb_dir.iterdir():
            if _hb_file.is_file() and not _hb_file.name.startswith('.'):
                try:
                    _hb_fresh = _now_hb - _hb_file.stat().st_mtime <= AGENT_PRESENCE_TTL
                    if _hb_fresh:  # heartbeat fresh → alive (union with window seed above)
                        _alive_workers.add(_hb_file.name)
                except OSError:
                    pass

    # Track which tasks were assigned this cycle to avoid double-dispatch.
    # Keyed as (slug, task_id) tuples so that missions reusing the same
    # task IDs (e.g. t001 in both mission-a and mission-b) do not block
    # each other within the same dispatch cycle.
    assigned_task_ids = set()  # set of (slug, task_id) tuples

    for window in windows:
        agent_name = window['agent_name']
        target = window['window_target']

        worker_info = workers.get(agent_name)
        if worker_info is None:
            log(f"unknown worker in tmux: {agent_name} — not in registry/workers.yaml, skipping")
            continue

        worker_skills = set(worker_info.get('skills') or [])

        # Idle = no assignment file
        assignment_file = ASSIGNMENTS_DIR / agent_name
        is_idle = not assignment_file.exists()

        # Rule 5 (herdr only): check agent state for blocked / idle-with-task.
        # Runs for ALL workers (busy and idle) before the is_idle gate below.
        check_rule5(agent_name, target, assignment_file, task_statuses_by_mission)

        # t025: a Worker whose retirement is already in flight is not a
        # candidate for anything.  Before t002 the judgement and the kill were
        # one second apart, so there was no window to assign into; now watchdog
        # gives the Worker a grace period, and for the idle / no-task / Rule 2
        # paths `queue/assignments/<agent>` stays absent for all of it.  This
        # loop would happily read that as "idle" and send it the next task.
        #
        # What follows is not merely wasted work.  The Worker pulls, so the
        # pane keeps the same pid and created_at, watchdog's identity guard
        # passes, and the escalation lands on a Worker doing *new* work — whose
        # task the cleanup, bound to the old execution, then leaves stranded
        # in_progress (Codex 4 巡目 P1-1).  watchdog re-checks the assignment
        # before it acts (`assignment_execution_verdict()`), but that is the
        # last line of defence; not creating the situation is this one.
        #
        # Skipping the whole iteration also covers the no-task / blocked-stuck
        # branches below, which would only call retire_worker() and be refused
        # for the same marker.
        if KILL_AUTHORITY != 'dispatcher' and _retirement.has_marker(agent_name):
            log(f"[retire] {agent_name}: retirement in flight — not assigning "
                f"any task this cycle")
            continue

        if not is_idle:
            continue  # Worker is busy; do not interrupt

        # BENCH_MODE: hold off assignment while benchmark-ctx.sh applies its
        # context strategy (B: /clear, C: restart).  Gate is held by the
        # orchestrator and released once the strategy action is complete.
        if bench_gate_active(agent_name):
            log(f"[bench] gate active for {agent_name} — skipping assignment (strategy={bench_current_strategy()})")
            continue

        # Strategy C: skip assignment while the Worker window is being killed
        # and re-launched.  The .restarting flag is written by benchmark-ctx.sh
        # before tmux kill-window and removed after the new window is ready.
        if bench_worker_restarting(agent_name):
            log(f"[bench] {agent_name} is restarting (Strategy C) — skipping assignment")
            continue

        # Find best unblocked pending task with skill match
        best = None
        for slug, meta in unblocked_pending:
            if (slug, meta['id']) in assigned_task_ids:
                continue
            task_skills = set(meta.get('skills') or [])
            # Defense-in-depth: skip director-only tasks even if the gate above
            # let them through (e.g. scalar-typed skills field edge case).
            if task_skills & DIRECTOR_ONLY_SKILLS:
                continue
            # Codex-review tasks go through kai-review.sh (spawned below in the
            # no-worker loop), never through a regular Worker window.  Even if a
            # human accidentally adds `codex-review` to a Worker's skills in
            # registry/workers.yaml, this guard keeps Claude Workers out of the
            # Codex path.
            if task_skills & CODEX_REVIEW_SKILLS:
                continue
            if task_skills.issubset(worker_skills):
                best = (slug, meta)
                break

        if best:
            slug, meta = best
            task_id = meta['id']
            # Include slug so task IDs that restart per-mission don't collide
            # (e.g. assign_Haruto_20260905-mission-a_t001 vs _20260905-mission-b_t001)
            notify_key = f"assign_{agent_name}_{slug}_{task_id}"
            if should_notify(notify_key):
                msg = (
                    f"タスク {task_id} (mission={slug}) を実行して。"
                    f"plan pull --task {task_id} --mission {slug} で取得後、"
                    f"作業→plan done で完了。"
                )
                if tmux_send(target, msg):
                    record_notify(notify_key)
                assigned_task_ids.add((slug, task_id))
        else:
            # No unblocked pending task for this worker.
            # If there are ZERO tasks (even blocked) matching this worker's skills
            # across all active missions → worker is no longer needed.
            matching_pending = [
                (slug, meta) for slug, meta in all_pending
                if bool(task_s := set(meta.get('skills') or [])) and task_s.issubset(worker_skills)
            ]
            has_any = bool(matching_pending)
            # Defense-in-depth: also keep the worker alive if it owns an
            # in_progress task.  plan.sh pull writes the assignment file before
            # the Taskvia sync, but there is still a narrow window between
            # save_task (task→in_progress) and the assignment file write where
            # the dispatcher could see is_idle=True + no pending tasks.
            has_in_progress = any(
                meta.get('worker') == agent_name
                for _, meta in all_tasks
                if meta.get('status') == 'in_progress'
            )
            if not has_any and not has_in_progress:
                if in_spawn_grace(target):
                    log(f"[spawn_grace] {agent_name}: within {SPAWN_GRACE_SECONDS}s spawn grace — skip shutdown")
                else:
                    notify_key = f"shutdown_{agent_name}"
                    if should_notify(notify_key):
                        if retire_worker(agent_name, target, 'no-task'):
                            record_notify(notify_key)
            elif has_any and not has_in_progress:
                # Rule 2: all matching tasks are blocked.  If the most recently
                # modified matching task file is older than BLOCKED_STUCK_THRESHOLD,
                # the blocker chain has not progressed — send shutdown (worker is stuck).
                # Uses file mtime as a proxy for last_status_change.
                def _task_mtime(slug, meta):
                    p = MISSIONS_DIR / slug / 'tasks' / f"{meta['id']}.md"
                    try:
                        return p.stat().st_mtime
                    except OSError:
                        return time.time()  # file missing → treat as fresh

                def _chain_newest_mtime(slug, meta):
                    """Max of pending task mtime AND its direct blockers' mtimes.

                    Fix (Rule 2 mtime race): when a blocker task just completed
                    (status→done), the blocker file mtime is fresh even if the
                    pending task file itself was written long ago.  Checking
                    blocker mtimes prevents false stuck-detection in the window
                    between blocker completion and the pending task becoming
                    unblocked (the next dispatcher cycle may lag by a few seconds).
                    """
                    mtimes = [_task_mtime(slug, meta)]
                    for dep_id in (meta.get('blocked_by') or []):
                        dep_file = MISSIONS_DIR / slug / 'tasks' / f"{dep_id}.md"
                        try:
                            mtimes.append(dep_file.stat().st_mtime)
                        except OSError:
                            pass
                    return max(mtimes)

                # Fix (Rule 2 mtime race): if any direct blocker task is
                # in_progress, the chain is actively progressing — do NOT kill
                # the worker, even if the pending task files have old mtimes.
                has_active_blocker = any(
                    task_statuses_by_mission.get(s, {}).get(dep) == 'in_progress'
                    for s, m in matching_pending
                    for dep in (m.get('blocked_by') or [])
                )
                if has_active_blocker:
                    log(
                        f"[Rule 2 skip] {agent_name}: blocker task is in_progress "
                        f"→ chain progressing, worker kept alive"
                    )
                else:
                    newest_mtime = max(_chain_newest_mtime(s, m) for s, m in matching_pending)
                    stuck_secs = time.time() - newest_mtime
                    if stuck_secs >= BLOCKED_STUCK_THRESHOLD:
                        # t015 F1 (Seo review, MEDIUM): this 3rd kill path had no
                        # spawn-grace guard at all. Director may pre-spawn a
                        # Worker for a task whose blocker is still pending with
                        # an old mtime (common in long missions) — that reads as
                        # "blocked stuck" from cycle one and this branch killed
                        # the Worker seconds after spawn, same real-world damage
                        # (lost ~48KB prompt) as the bug PR#190 itself fixes for
                        # the other two paths.
                        if in_spawn_grace(target):
                            log(f"[spawn_grace] {agent_name}: within {SPAWN_GRACE_SECONDS}s spawn grace — skip Rule 2 shutdown")
                        else:
                            notify_key = f"blocked_stuck_{agent_name}"
                            if should_notify(notify_key):
                                log(
                                    f"[Rule 2] {agent_name}: all matching tasks blocked for "
                                    f"{stuck_secs:.0f}s ≥ {BLOCKED_STUCK_THRESHOLD}s "
                                    f"— requesting retirement (blocked-stuck)"
                                )
                                if retire_worker(agent_name, target, 'blocked-stuck'):
                                    record_notify(notify_key)

    # Notify Sora about unblocked pending tasks that NO live worker can handle.
    # alive_workers uses OR condition: window seed + heartbeat-fresh union.
    #   - window seed covers fresh-spawn Workers (no heartbeat file until first tool call).
    #   - heartbeat union covers Workers whose herdr window transiently disappeared (PR #154).
    #   - Neither window nor fresh heartbeat → truly dead → can_handle=False (correct).
    # See: PR #154 (heartbeat-only fix), this PR (full OR: window seed + heartbeat union).
    for slug, meta in unblocked_pending:
        task_id = meta['id']
        task_skills = set(meta.get('skills') or [])
        # Defense-in-depth: director-only tasks must never trigger a Worker
        # startup request — skip them regardless of how they reached this loop.
        if task_skills & DIRECTOR_ONLY_SKILLS:
            continue
        # Codex-review path (Phase 2): background-spawn kai-review.sh instead
        # of asking the Director to start a Worker.  spawn_kai_review is a
        # no-op when Kai-codex is already in flight (assignment file exists)
        # or when pr_number is missing (warning logged, Director escalates).
        if task_skills & CODEX_REVIEW_SKILLS:
            handle_codex_review(slug, meta, live_state_keys)
            continue
        # can_handle: True if any alive worker (window exists OR heartbeat recent) has skills ⊇ task_skills
        can_handle = any(
            task_skills.issubset(set((workers.get(name) or {}).get('skills') or []))
            for name in _alive_workers
            if (workers.get(name) or {}).get('role', 'worker') == 'worker'
        )
        if not can_handle:
            # Include slug to avoid collision when missions reuse t001, t002, etc.
            notify_key = f"no_worker_{slug}_{task_id}"
            if should_notify(notify_key):
                msg = (
                    f"要求スキル {sorted(task_skills)} の Worker を起動してください "
                    f"(task {task_id}, mission={slug})"
                )
                if tmux_send(_director_name(), msg):
                    record_notify(notify_key)


    # Vanished worker detection: in_progress tasks whose worker tab has disappeared.
    # Condition: status==in_progress AND worker recorded AND tab absent AND heartbeat stale.
    # When all four conditions hold, the Worker process is truly gone — the task will
    # never complete on its own.  Notify Director with a recovery recipe.
    _hb_dir_vanish = REGISTRY_DIR / 'heartbeats'
    _now_vanish = time.time()
    for slug, meta in all_tasks:
        if meta.get('status') != 'in_progress':
            continue
        worker_name = meta.get('worker')
        if not worker_name:
            continue  # no worker recorded yet (pull not done) — skip
        # Condition C: worker tab absent from live windows
        if worker_name in _window_agent_names:
            continue  # tab exists → worker is running, no alert
        # Condition D: heartbeat stale or missing
        hb_file = _hb_dir_vanish / worker_name
        hb_stale = True
        if hb_file.exists():
            try:
                hb_stale = _now_vanish - hb_file.stat().st_mtime > AGENT_PRESENCE_TTL
            except OSError:
                hb_stale = True
        if not hb_stale:
            continue  # heartbeat fresh → worker may still be alive (transient mux glitch)
        task_id = meta.get('id', '?')
        notify_key = f'vanished_worker_{slug}_{task_id}'
        if should_notify(notify_key):
            msg = (
                f"task {task_id} (mission: {slug}) の worker {worker_name} の tab が消滅しています。"
                f"plan.sh update {task_id} --status pending --reset --mission {slug} で復旧してください。"
            )
            if tmux_send(_director_name(), msg):
                record_notify(notify_key)
                log(f"[vanished_worker] {slug}/{task_id}: worker {worker_name} tab gone + heartbeat stale — notified director")


    # needs_director detection: task escalated to needs_director → notify Director.
    # t027: previously nothing watched this transition (grep for 'needs_director' in
    # this file returned 0 hits) — a Codex NEEDS FIX or a Worker's `plan.sh
    # needs-director` call left the queue silently draining to empty with no one
    # waking the Director (measured: 10h28m full stop, 2026-09-21 19:53 UTC →
    # 2026-09-22 06:22 UTC). Reuses all_tasks already loaded above — no extra scan.
    #
    # t010 (#10): 状態ベースの通知は **状態が変わるまで 1 回だけ**。以前は
    # should_notify() (= NOTIFY_TTL のスロットル) だけで、対処済みの task でも 5 分ごと
    # に永久に再送されていた (2026-09-25 に数十通)。今は notify_state_once() が
    # 「この状態は既に伝えた」台帳 (registry/daemons/notified-state.json) を持ち、
    # status + reason (+ 拒否記録) が変わったときだけ再通知する。Director が
    # pending に戻して再び落ちた場合は、状態を離れた時点で台帳から捨てるので届く。
    #
    # t032 F5: _director_name() falls back to the literal 'Sora-director' when no
    # Director window is live, so with no guard this block called tmux_send()
    # unconditionally — 2 log lines (tmux_send's own WARNING + this block's own
    # "mux send failed") every 5s per needs_director task, for as long as no
    # Director is up. Match Rule 5's director_live guard: 1 log line, no send
    # attempt, no notify recorded (so it re-checks, and re-notifies promptly,
    # once a Director comes back).
    director_live_for_needs_director = director_live_for_state_notices()
    for slug, meta in all_tasks:
        if meta.get('status') != 'needs_director':
            continue
        task_id = meta.get('id', '?')
        notify_key = f'needs_director_{slug}_{task_id}'
        live_state_keys.add(notify_key)
        reason = (meta.get('needs_director_reason') or '').strip()
        reason_line = reason.splitlines()[0][:200] if reason else '(理由未記載)'
        task_file = MISSIONS_DIR / slug / 'tasks' / f'{task_id}.md'
        # codex-review が差分サイズ超過で拒否した task には、手動差分レビューへの
        # 切り替えと超過バイト数・PR 番号を添える (t010 / #11)。
        note, note_inputs = refusal_note(slug, meta)
        fp = fingerprint('needs_director', reason, note_inputs)

        def build_msg(slug=slug, task_id=task_id, reason=reason, reason_line=reason_line,
                      task_file=task_file, note=note):
            return (
                f'[needs_director] task {task_id} (mission={slug}) が needs_director です。'
                f'理由: {reason_line}'
                + ('…' if len(reason) > len(reason_line) else '')
                + f' (全文: {task_file})。'
                f'reason を読んで方針を決め、plan.sh update {task_id} --status pending --reset '
                f'--mission {slug} で差し戻してください。'
                + note
            )
        if notify_state_once(notify_key, fp, 'needs_director', slug, task_id, build_msg,
                             needs_director_live=director_live_for_needs_director):
            log(f"[needs_director] {slug}/{task_id}: notified director (reason: {reason_line[:80]!r})")


    # Handoff detection: failed tasks with handoff_path → notify Director
    # t010 (#10): needs_director と同じく、状態が変わるまで 1 回だけ (台帳)。
    # 入力は status + handoff_path。以前は failed かつ handoff_path がある間ずっと
    # TTL ごとに再送された。
    director_live_for_handoff = director_live_for_state_notices()
    for slug in active_missions:
        tasks_for_slug = list_tasks_for_mission(slug)
        for meta, _ in tasks_for_slug:
            if meta.get('status') != 'failed':
                continue
            handoff_path = meta.get('handoff_path')
            if not handoff_path:
                continue
            task_id = meta.get('id', '?')
            notify_key = f"handoff_{slug}_{task_id}"
            live_state_keys.add(notify_key)
            fp = fingerprint('failed', handoff_path)

            def build_msg(slug=slug, task_id=task_id, handoff_path=handoff_path):
                handoff_summary = ''
                try:
                    hp = Path(handoff_path)
                    if not hp.is_absolute():
                        # task_158: writer(worker.md)は crewvia_handoff_path 経由で常に絶対パスを
                        # 書くはずなので、ここに来るのは規約からの逸脱(回帰)。cwd(worktree)基準の
                        # 相対パスは main repo 側から見て別ファイルを指し handoff_summary が空に
                        # なる既知の壊れ方(task_158)なので、黙って空文字にせず明示的に警告する。
                        log(f"WARNING: handoff_path is not absolute (task_158 regression?): "
                            f"{slug}/{task_id} handoff_path={handoff_path!r}")
                        hp = REGISTRY_DIR.parent / handoff_path
                    hp_text = read_queue_text(hp, 'handoff file')
                    if not is_unreadable(hp_text):
                        handoff_summary = ' | '.join(hp_text.splitlines()[:10])
                    else:
                        log(f"WARNING: handoff file unreadable at resolved path: "
                            f"{slug}/{task_id} resolved={hp} ({hp_text.reason})")
                except Exception:
                    handoff_summary = '(読み取り失敗)'
                return (
                    f"タスク {task_id} (mission={slug}) が failed になりました。"
                    f"handoff_path: {handoff_path} — {handoff_summary[:200]}。"
                    f"plan.sh add で継続タスクを追加してください。"
                )
            if notify_state_once(notify_key, fp, 'handoff', slug, task_id, build_msg,
                                 needs_director_live=director_live_for_handoff):
                log(f"handoff detected: {slug}/{task_id} -> notified director")

    # t010: 状態を離れた task の「伝えた」記録を捨てる (Director が pending に戻し、
    # 同じ理由でまた落ちたのは新しい事象なので、届かなければならない)。
    prune_told(live_state_keys, observed_missions(all_tasks, active_missions))


def run_daemon_watch():
    """Is the watchdog still alive?  (t005)

    Only the *peer* is judged here.  This daemon's own heartbeat is written by
    the bash wrapper, deliberately: the wrapper is the process that endures,
    and a python cycle that throws on every pass must not be able to make a
    live dispatcher look dead to the watchdog.

    Everything is caught — the mutual watch is a safety net, and a safety net
    that can abort the dispatch cycle is a net that makes things worse.
    """
    try:
        import lib_daemon_watch
        watch = lib_daemon_watch.DaemonWatch(
            registry_dir=REGISTRY_DIR,
            repo_root=REPO_ROOT,
            self_name=lib_daemon_watch.DAEMON_DISPATCHER,
            mux=_mux,
            config=lib_daemon_watch.load_config(),
            log=log,
        )
        verdict = watch.watch_peer()
        # 'healthy' fires every 5 seconds; logging it would bury the log.  The
        # rest are all states a person may need to reconstruct afterwards.
        if verdict.action not in (lib_daemon_watch.ACTION_HEALTHY,
                                  lib_daemon_watch.ACTION_GRACE,
                                  lib_daemon_watch.ACTION_DISABLED):
            log(f"[daemon-watch] watchdog: {verdict.action} — {verdict.reason}")
    except Exception as e:
        log(f"[daemon-watch] cycle failed: {e!r}")


# --- CYCLE ENTRY POINT ---
# Everything below this marker runs a full dispatch cycle.  Tests that want to
# exercise a single helper (tests/test_orphan_daemon_guard.py) exec() the code
# above it and stop here, so keep the marker even if the calls change.
#
# t005: the mutual watch goes FIRST.  It is the watchdog's only observer, and
# a failure further down this cycle (a corrupt card, a half-deployed change)
# would otherwise take the observer down with it — at exactly the moment the
# system is least healthy and most in need of it.
run_daemon_watch()
publish_agents()
dispatch()
# t002: both are queue/registry bookkeeping, not dispatch decisions, and both
# must run on every cycle — including the early-return cycles dispatch() takes
# when there are no active missions.
sweep_spawn_grace_markers()
if KILL_AUTHORITY != 'dispatcher':
    warn_on_unconsumed_retirements()
PYEOF
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while true; do
  # Recompute log file path on each cycle so midnight date-rollover creates a
  # new file automatically (e.g. dispatcher-20260905.log → dispatcher-20260906.log).
  LOG_FILE="${LOG_DIR}/dispatcher-$(date +%Y%m%d).log"
  # t005: 生存表明は dispatch サイクルの成否から独立させる。`run_dispatch &&`
  # のように繋いでしまうと、python 側が毎回例外で落ちる状態 (壊れた card、
  # 中途半端な deploy) で heartbeat だけが止まり、この bash ループは元気なのに
  # watchdog からは死んで見える → respawn → dispatcher が 2 つ、になる。
  # そのため無条件・先頭で書く。
  daemon_beat dispatcher "$REPO_ROOT" "$REGISTRY_DIR"
  run_dispatch || log "dispatch cycle error (exit $?)"
  sleep 5
done
