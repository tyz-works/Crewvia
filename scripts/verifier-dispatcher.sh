#!/usr/bin/env bash
# verifier-dispatcher.sh — Verifier Dispatcher daemon
#
# Polls every 30s for ready_for_verification tasks and assigns idle Verifiers.
#
# Idle Verifier detection:
#   registry/workers.yaml  — workers with 'verify' skill
#   queue/assignments/<name> — absent = idle, present = busy
#   tmux list-windows        — crewvia:<name>-verifier windows
#
# Self-verify prohibition: never assigns the same agent that was the task worker.
#
# Notification dedup: same key suppressed for NOTIFY_TTL seconds (default 60s).
# Standalone-safe: exits 0 silently when tmux is not available.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
QUEUE_DIR="${CREWVIA_QUEUE:-${REPO_ROOT}/queue}"
REGISTRY_DIR="${REPO_ROOT}/registry"
LOG_FILE="${REGISTRY_DIR}/verifier-dispatcher.log"
NOTIFY_CACHE="/tmp/verifier-dispatcher-notify-cache.$$.json"
NOTIFY_TTL="${NOTIFY_TTL:-60}"
POLL_INTERVAL="${VERIFIER_POLL_INTERVAL:-30}"

# Standalone-safe: silently exit when no mux backend (tmux / herdr) is available
if ! python3 "${SCRIPT_DIR}/lib_mux.py" available >/dev/null 2>&1; then
  exit 0
fi

mkdir -p "$REGISTRY_DIR"

log() {
  local msg
  msg="[verifier-dispatcher $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
  echo "$msg" >&2
  echo "$msg" >> "$LOG_FILE"
}

log "Starting verifier-dispatcher (PID $$, poll=${POLL_INTERVAL}s, notify_ttl=${NOTIFY_TTL}s)"

# ---------------------------------------------------------------------------
# One dispatch cycle — implemented in Python for YAML / file parsing
# ---------------------------------------------------------------------------
run_dispatch() {
  python3 - "$QUEUE_DIR" "$REGISTRY_DIR" "$NOTIFY_CACHE" "$NOTIFY_TTL" <<'PYEOF'
import sys
import os
import re
import json
import time
import subprocess
from pathlib import Path
from datetime import datetime, timezone

QUEUE_DIR      = Path(sys.argv[1])
REGISTRY_DIR   = Path(sys.argv[2])
NOTIFY_CACHE   = Path(sys.argv[3])
NOTIFY_TTL     = int(sys.argv[4])

# Import lib_mux from scripts/ (same dir as this script via REGISTRY_DIR.parent)
_SCRIPTS_DIR = REGISTRY_DIR.parent / 'scripts'
sys.path.insert(0, str(_SCRIPTS_DIR))
from lib_mux import Mux  # noqa: E402
# デーモン側 JSON 状態ストアを読む入口は 1 つ (t026)。ここで `json.loads` を書き足さない。
from lib_daemon_state import load_json_store, notify_cache_problem  # noqa: E402
from lib_task_status import accepts as status_accepts  # noqa: E402  (語彙・許可遷移は 1 か所。vNext 01a S1)
# task カードの読み取りは crewvia の中で 1 箇所しかない (Codex 5 巡目 P2)。
# ここに frontmatter を直接読むコードを書き戻さないこと — plan.sh が受理する
# カードとここが拾うカードが、静かにズレる。
from lib_task_cards import (  # noqa: E402
    is_missing, is_unreadable, list_task_cards, read_regular_text_or_unreadable,
)
_mux = Mux()

MISSIONS_DIR    = QUEUE_DIR / 'missions'
STATE_FILE      = QUEUE_DIR / 'state.yaml'
ASSIGNMENTS_DIR = QUEUE_DIR / 'assignments'
WORKERS_FILE    = REGISTRY_DIR / 'workers.yaml'
LOG_FILE        = REGISTRY_DIR / 'verifier-dispatcher.log'


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    line = f"[verifier-dispatcher {ts}] {msg}"
    print(line, file=sys.stderr)
    try:
        with LOG_FILE.open('a') as f:
            f.write(line + '\n')
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Minimal YAML parser (mirrors dispatcher.sh — no external deps)
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
    if val in ('null', '~', ''):
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
# State / workers / tasks loading
# ---------------------------------------------------------------------------

def _read_queue_text(path, what):
    """queue / registry のファイルを種類を確かめてから読む。

    読めたら `str`、読めなければ `Unreadable` (t018)。判定の本体は
    `lib_task_cards.read_regular_text_or_unreadable()` に 1 つだけ
    (Codex 8 巡目 P2)。常駐デーモンなので、上限の無い `read_text()` が書き手の
    いない FIFO に当たると **検証の割り当てがサイクルごと止まる**。
    """
    return read_regular_text_or_unreadable(
        path, warn=lambda msg: log(f"WARNING: {what}: {msg}"))


def load_state():
    text = _read_queue_text(STATE_FILE, 'state file')
    if is_missing(text):
        return {}          # 本当に無い = active mission ゼロ
    if is_unreadable(text):
        return text        # 観測できなかった —— 空として扱えない形で返す
    return parse_yaml(text)


def load_workers():
    """Return dict {name: {'skills': [...], 'role': str}} from registry/workers.yaml."""
    text = _read_queue_text(WORKERS_FILE, 'workers file')
    if is_missing(text):
        return {}          # 本当に無い = Worker 0 人
    if is_unreadable(text):
        return text        # 観測できなかった
    workers = {}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('- name:'):
            name = stripped[len('- name:'):].strip().strip('"\'')
            current = name
            workers[name] = {'skills': [], 'role': 'worker'}
        elif current and re.match(r'\s+skills:', line):
            m = re.search(r'\[([^\]]*)\]', line)
            if m:
                inner = m.group(1).strip()
                workers[current]['skills'] = [s.strip().strip('"\'') for s in inner.split(',')] if inner else []
        elif current and re.match(r'\s+role:', line):
            role = line.split(':', 1)[1].strip().strip('"\'')
            workers[current]['role'] = role
    return workers


def list_tasks_for_mission(slug):
    """Return list of (meta, body, path) sorted by task number.

    読み取りの規則は scripts/lib_task_cards.py に 1 つだけ (plan.sh / dispatcher.sh
    と同じもの)。読めないカードは例外ではなく `[破損]` のカードとして返るので、
    1 枚の事故でこのループが止まることはない。
    """
    tdir = MISSIONS_DIR / slug / 'tasks'
    cards = list_task_cards(tdir, warn=lambda msg: log(f"WARNING: {msg}"))
    return [(meta, body, tdir / f"{meta['id']}.md") for meta, body in cards]


# ---------------------------------------------------------------------------
# Task status change (verifying) — 書くのは plan.sh だけ (S5 / t020)
# ---------------------------------------------------------------------------

PLAN_SH = _SCRIPTS_DIR / 'plan.sh'
PLAN_SH_TIMEOUT_SECONDS = 120


_EXECUTION_ID_RE = re.compile(r'ex-[0-9a-f]{32}')


def card_execution_id(meta):
    """card の**今の試行**の ID (`ex-<32hex>`)。名乗りに使うので、**active (reserved / running) な試行のときだけ**返す
    (legacy の card・terminal の試行・形が違う値は None = 名乗らない。名乗って拒否されるより、名乗りなしで通す方が
    E3 の方針 (execution.md §5.2) に合う。これは照合の根拠ではなく「どの試行を検証に出すか」の名指し)。"""
    xid = meta.get('current_execution_id')
    if (isinstance(xid, str) and _EXECUTION_ID_RE.fullmatch(xid)
            and meta.get('execution_status') in ('reserved', 'running')):
        return xid
    return None


def mark_verifying(slug, task_id, verifier, execution_id=None):
    """`plan.sh verifying` で card を ready_for_verification → verifying にする。

    以前はここが card を丸ごと読み、ロックなしで書き戻していた (読んでから書くまでの間に done /
    verify-result が進めた status を古い内容で巻き戻せた)。今は queue のロックの中で plan.sh が
    読み直し、元の status が違えば何も書かずに拒否する (exit 2)。

    失敗は例外 (呼び出し側が 1 件ずつ受けてログに落とす。倒れる先は「その task だけが割り当たらない」)。
    queue はこのデーモンの QUEUE_DIR に向ける (`CREWVIA_QUEUE`)。plan.sh は bash の下で python を
    起こすので、タイムアウトは**プロセスグループごと**殺す (bash だけ殺すと下の python が孤児になる)。
    """
    env = dict(os.environ, CREWVIA_QUEUE=str(QUEUE_DIR), AGENT_NAME='verifier-dispatcher')
    env.pop('CREWVIA_EXECUTION_ID', None)      # 名乗りは引数 (`--execution`) だけ。このデーモンの env から別 task の ID を継がない
    claim = ['--execution', execution_id] if execution_id is not None else []
    proc = subprocess.Popen(
        ['bash', str(PLAN_SH), 'verifying', task_id, '--verifier', verifier, '--mission', slug, *claim],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=PLAN_SH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
        proc.communicate()
        raise RuntimeError(f"plan.sh verifying timed out after {PLAN_SH_TIMEOUT_SECONDS}s")
    if proc.returncode != 0:
        raise RuntimeError(f"plan.sh verifying exit {proc.returncode}: {(err or out).strip()[:300]}")


# ---------------------------------------------------------------------------
# Notification dedup cache (mirrors dispatcher.sh)
# ---------------------------------------------------------------------------

def load_notify_cache():
    # dispatcher.sh と同じ。読み取りと形の検証は入口 1 つ (t026)。使えないときは `{}`
    # = スロットルを失う = もう一度送る側 (冪等)。
    cache = load_json_store(NOTIFY_CACHE, check=notify_cache_problem)
    return {} if is_unreadable(cache) else cache


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
# mux helpers (delegated to lib_mux.Mux via _mux instance)
# ---------------------------------------------------------------------------

def tmux_list_verifier_windows():
    """Return list of {'window_target', 'agent_name'} for *-verifier windows."""
    names = _mux.list(suffix='-verifier')
    return [
        {'window_target': name, 'agent_name': name[:-len('-verifier')]}
        for name in names
    ]


def _director_name():
    """Return the name of the live Director window, falling back to 'Sora-director'."""
    names = _mux.list(suffix='-director')
    return names[0] if names else 'Sora-director'


def tmux_send(target, message):
    """Send message to mux window (2-step via lib_mux: message then Enter)."""
    ok = _mux.send(target, message)
    if ok:
        log(f"→ [{target}] {message[:120]}")
    else:
        log(f"WARNING: mux send to {target!r} failed")


# ---------------------------------------------------------------------------
# Main dispatch logic
# ---------------------------------------------------------------------------

def dispatch():
    state = load_state()
    if is_unreadable(state):
        log(f"WARNING: state.yaml を観測できない ({state.reason}) — "
            f"このサイクルは何も割り当てない")
        return
    active_missions = list(state.get('active_missions') or [])
    if not active_missions:
        return

    # Collect all ready_for_verification tasks across active missions
    rfv_tasks = []
    for slug in active_missions:
        tasks = list_tasks_for_mission(slug)
        for meta, _, path in tasks:
            if status_accepts('verifying', meta.get('status')):   # 検証を始められる status
                rfv_tasks.append((slug, meta, path))

    if not rfv_tasks:
        return

    log(f"found {len(rfv_tasks)} ready_for_verification task(s)")

    # Load workers with verify skill (exclude directors)
    all_workers = load_workers()
    if is_unreadable(all_workers):
        log(f"WARNING: workers.yaml を観測できない ({all_workers.reason}) — "
            f"このサイクルは何も割り当てない")
        return
    verify_workers = {
        name for name, info in all_workers.items()
        if 'verify' in (info.get('skills') or [])
        and info.get('role') != 'director'
    }

    # Find live verifier tmux windows
    verifier_windows = tmux_list_verifier_windows()
    live_verifier_map = {w['agent_name']: w for w in verifier_windows}

    # Track assigned verifiers this cycle to avoid double-dispatch
    assigned_verifiers = set()

    for slug, meta, task_path in rfv_tasks:
        task_id = meta.get('id', '?')
        task_worker = meta.get('worker') or ''

        # Find an idle verifier: verify skill + live window + idle + not the worker
        chosen = None
        for agent_name, window in live_verifier_map.items():
            if agent_name == task_worker:
                log(f"skip {agent_name}: self-verify prohibited (task worker={task_worker})")
                continue
            if agent_name not in verify_workers:
                log(f"skip {agent_name}: not in verify skill set")
                continue
            if agent_name in assigned_verifiers:
                continue  # already assigned this cycle
            assignment_file = ASSIGNMENTS_DIR / agent_name
            if assignment_file.exists():
                log(f"skip {agent_name}: busy (assignment file present)")
                continue
            chosen = (agent_name, window)
            break

        if chosen:
            agent_name, window = chosen
            log(f"assigning: task {task_id} (mission={slug}) → verifier {agent_name}")
            try:
                xid = card_execution_id(meta)
                mark_verifying(slug, task_id, agent_name, xid)
                # verify-result は「どの試行を判定するか」を名指しする (verifier は試行の持ち主ではない。execution.md §5.1)
                exec_arg = f" --execution {xid}" if xid else ""
                msg = (
                    f"タスク {task_id} (mission={slug}) の検証をしてください。"
                    f"plan.sh verify-result {task_id} <pass|fail|needs_human_review>"
                    f"{exec_arg} [--notes \"...\"] で結果を記録してください。"
                )
                tmux_send(window['window_target'], msg)
                assigned_verifiers.add(agent_name)
            except Exception as e:
                log(f"ERROR: failed to assign task {task_id} to {agent_name}: {e}")
        else:
            # No idle verifier available — notify Director (dedup)
            notify_key = f"no_verifier_{task_id}"
            if should_notify(notify_key):
                has_any_verifier = bool(verify_workers)
                if has_any_verifier:
                    reason = "全 Verifier がビジー中または同一 worker"
                else:
                    reason = "verify スキルを持つ Verifier が未登録"
                msg = (
                    f"Verifier Dispatcher: task {task_id} (mission={slug}) が"
                    f" ready_for_verification だが idle Verifier がいない ({reason})。"
                    f"verify スキルの Verifier を起動してください。"
                )
                tmux_send(_director_name(), msg)
                record_notify(notify_key)
                log(f"no idle verifier for task {task_id}: notified director")


dispatch()
PYEOF
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while true; do
  run_dispatch || log "dispatch cycle error (exit $?)"
  sleep "$POLL_INTERVAL"
done
