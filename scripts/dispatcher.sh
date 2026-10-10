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

# B2 / #26: この bash プロセスが起動した時点で、実際に読み込まれたコードの版
# (HEAD sha + 対象ファイルの digest) を 1 回だけ記録する。以降 disk が
# `git merge` で変わっても、この記録はこのプロセスが実際に持っているものを
# 指し続ける — 起動後に record し直すと「今の disk」を記録してしまい、
# 版ずれ検知が常に「ずれていない」を返す無意味なものになる。
# 失敗しても dispatcher 自体の起動は止めない (`|| true`) — バージョン記録は
# 運用上の便宜であり、dispatch を止める理由にはならない。
python3 "${SCRIPT_DIR}/lib_daemon_watch.py" record-version dispatcher \
  --repo-root "$REPO_ROOT" >> "$LOG_FILE" 2>&1 || log "record-version failed (non-fatal)"

# ---------------------------------------------------------------------------
# Telegram の認証情報 (PR-A / 設計 §1-1)。**起動時に 1 回** (respawn のたびにここを通る) 取り出し、
# この bash プロセスの**シェル変数**に持つ。export しない (argv にも bash の env にも出さない)。
# サイクルごとの python には、下の run_dispatch の **前置代入** (env コマンドを付けない) で
# その呼び出しにだけ渡す。python のサイクルごとに 1Password を呼ばない。
# 取り出せなかった間 (1Password のロック・op が PATH に無い・ファイルの権限) は、長い間隔
# TG_RESOLVE_RETRY_SECONDS (600 秒) でだけ取り直す (サイクル単位では呼ばない。t016 P3-1)。
# 未設定 (config の telegram.credentials.source が無い) なら何もしない。
# ---------------------------------------------------------------------------
TG_RESOLVE_RETRY_SECONDS=600
tg_token=""
tg_chat=""
tg_reason="no_credentials"
tg_resolved_at=0

_tg_resolve() {
  local out
  tg_token=""
  tg_chat=""
  out="$(python3 "${SCRIPT_DIR}/lib_telegram.py" resolve 2>/dev/null)" || out="credential_command_failed"
  tg_reason="$(printf '%s\n' "$out" | sed -n 1p)"
  [[ -n "$tg_reason" ]] || tg_reason="credential_command_failed"
  if [[ "$tg_reason" == "ok" ]]; then
    tg_token="$(printf '%s\n' "$out" | sed -n 2p)"
    tg_chat="$(printf '%s\n' "$out" | sed -n 3p)"
  fi
  tg_resolved_at="$(date +%s)"
}

_tg_maybe_retry() {
  # 設定されているのに取り出せていない (no_credentials = 未設定は対象外) ときだけ、長い間隔で取り直す。
  if [[ "$tg_reason" != "ok" && "$tg_reason" != "no_credentials" ]] \
     && (( $(date +%s) - tg_resolved_at >= TG_RESOLVE_RETRY_SECONDS )); then
    _tg_resolve
    log "telegram credentials retry: ${tg_reason}"
  fi
}

_tg_resolve
if [[ "$tg_reason" != "no_credentials" ]]; then
  log "telegram credentials: ${tg_reason}"
fi

# ---------------------------------------------------------------------------
# One dispatch cycle — implemented in Python for YAML / file parsing
# ---------------------------------------------------------------------------
run_dispatch() {
  # 認証情報は前置代入で python にだけ渡す (`env VAR=… python3` は env の argv に token が載るので使わない)。
  _CREWVIA_TG_RESOLVED_TOKEN="$tg_token" _CREWVIA_TG_RESOLVED_CHAT_ID="$tg_chat" \
  _CREWVIA_TG_RESOLVE_REASON="$tg_reason" \
  python3 - "$QUEUE_DIR" "$REGISTRY_DIR" "$NOTIFY_CACHE" "$NOTIFY_TTL" "$STATE_GRACE" "$LOG_FILE" <<'PYEOF'
import sys
import os
import re
import json
import shlex
import time
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

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

# Telegram (PR-A): dispatcher.sh が起動時に取り出した認証情報は前置代入でここに届く。**すぐ os.environ から外して**
# このモジュールの変数に持つ — 以降の subprocess (mux send・git 等) が継承しない。Telegram の通信だけは
# lib_telegram.run_cycle が `poll` のサブプロセスにだけ env で渡す。token をログ・例外文に出さない。
_TG_CARRIED = {k: os.environ.pop(k, '') for k in ('_CREWVIA_TG_RESOLVED_TOKEN', '_CREWVIA_TG_RESOLVED_CHAT_ID')}
_TG_RESOLVE_REASON = os.environ.pop('_CREWVIA_TG_RESOLVE_REASON', '') or 'no_credentials'

from lib_mux import Mux, repo_identity_ok  # noqa: E402
import lib_retirement  # noqa: E402
# 「依存が満たされた」の定義は crewvia の中で 1 箇所しかない (t010 / QA t002 の
# 指摘 F-2b)。ここに同じ規則のコピーを書き戻さないこと — plan.sh pull が割り当て
# る task と dispatcher が投げる task がズレると、痛むのは QA FAIL の直後だけで、
# その瞬間まで誰も気付かない。tests/test_task_graph.py がコピーの再発を見張る。
from lib_dep_rules import card_dependencies  # noqa: E402
# task の status の語彙・終端 / 手放した / 判断待ちの集合は 1 か所 (vNext 01a S1)。plan.sh /
# lint_plan.py も同じモジュールを読む。ここに status の集合を書き戻さないこと —
# tests/test_task_status_single_definition.py が AST で落とす。
from lib_task_status import (  # noqa: E402
    RELEASED_WORK_STATUSES, TERMINAL_STATUSES, WAITS_ON_DIRECTOR_STATUSES,
)
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
# デーモン側 JSON 状態ストア (「伝えた」台帳・拒否の記録) を読む入口は 1 つ (t026)。
# ここで `json.loads(text)` を書き足さないこと — 外側しか検証しない読み方が、同じ根の
# 欠陥を PR #214 で 3 回出した。再発防止は
# tests/test_daemon_state_reads_go_through_the_entry.py (この埋め込み python も走査する)。
from lib_daemon_state import (  # noqa: E402
    is_finite_number, job_since_state_problem, load_json_store, notify_cache_problem,
    rule5_state_problem, told_entry_problem, told_is_fresh_timeout, told_ledger_problem,
    told_lock, usage_limit_state_problem,
)
# 「利用枠切れ」の画面の同定 (C2 / t005)。watchdog と共有する 1 か所の定義 — ここに
# 画面の文言を照合するコードを書き足さないこと (位置と構造に束縛した判定が割れる)。
import lib_usage_limit  # noqa: E402
# Worker が起動された TARGET_DIR の記録と、「この Worker にこの task を回してよいか」の
# 判定 (t009 / #21)。定義はこのモジュールに 1 つだけ (`plan.sh pull` の target 照合と
# 同じ正規形)。ここに Worker と task の target_dir の比較を書き戻さないこと。
import lib_worker_target as _worker_target  # noqa: E402
# 割り当て文に貼る Worker 名の検証 (plan.sh pull --agent が受け付ける形と同じ定義。コピーしない)。
from lib_state_store import agent_name_problem as _agent_name_problem  # noqa: E402
# 「ペインの裏で何かが走っているか」の定義 (watchdog の idle 判定と共有、B1 / #27)。
# ここで /proc を読む分類を書き足さないこと — 2 か所に置くと答えが割れる。
from lib_pane_process import classify_process_tree  # noqa: E402
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
# `RELEASED_WORK_STATUSES` = 「Worker がその card をもう手放している」status。TERMINAL_STATUSES
# (= 依存が満たされた) とは問いが違う: `failed` は依存を満たさない (HELD) が、Worker は手放している。
# Worker の生死・Kai-codex の孤児判定はこちらを使う (t001 / backlog #13)。定義は lib_task_status。

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
PLAN_SH = REGISTRY_DIR.parent / 'scripts' / 'plan.sh'

# `plan.sh` の exit code (scripts/plan.sh の PRECONDITION_UNMET / LOCK_BUSY と同じ
# 値。plan.sh は import できる python モジュールではない (bash + heredoc) ので、
# watchdog 側の lib_retirement.py と同様にここでも値だけを複製する —— 「共有規則に
# env 停止スイッチを付けない」原則と同じ理由で、値そのものの複製は禁止していない
# (規則の定義が 2 箇所に分かれるのが問題であって、固定 exit code の複製ではない)。
PLAN_PRECONDITION_UNMET = 3
PLAN_LOCK_BUSY = 4
# lib_retirement.CLEANUP_COMMAND_TIMEOUT と同じ値 (watchdog → plan.sh retire の
# 呼び出しと同種の「短時間で終わるはずの queue ロック付き操作」)。
REAP_ORPHAN_COMMAND_TIMEOUT = 30

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

# t074 追補 (Director 実例, 2026-09-27 23:15〜23:45): Rule 5 の「裏に job があるので
# 黙る」判断 (worker_has_background_work) には、B1 の設計上、上限が無かった。Ren の
# `while pgrep -f "<script>" > /dev/null; do sleep 15; done` は Bash tool のラッパーの
# 子孫なので job と判定され続け (`pgrep -f` は Claude Code が全体を包む
# `bash -c '… eval …'` 自身の cmdline にも一致するため、赤の実証が終わった後も
# 自己一致で永遠に回った)、15 分以上 Rule 5 が黙り続けた。
#
# 「子孫の末端が sleep/pgrep/tail -f のような待ち系コマンドか」で判定する案 (族C
# 監査で検討した (a)) は採らない — 正当な CI 待ちループ (`while ...; do sleep 20;
# done` で `gh pr checks` を呼ぶパターン。本番で常用されている) も末端は同じ
# `sleep` になるため、"待ち系コマンドは job でない" にすると正当な長時間待ちを
# 常に誤検知することになり、B1 が消したかった偽陽性を作り直してしまう
# (memory `time-as-proxy-flips-false-positive-to-false-negative` と同じ形の罠:
# 「何をしているか」の代理指標を変えても、代理指標である限り穴が向きを変えて残る)。
#
# 代わりに、watchdog の絶対上限 (`max_threshold`, 既定 3600秒) と同じ考え方
# (「プロセス層はこの上限だけは抑制しない」) を Rule 5 にも 1 つ足す:
# 「裏に job がある」という理由で idle-with-task を黙らせてよいのは、その job が
# 継続的にそう判定され続けてから最大でもこの秒数まで — 超えたら、たとえ本物の
# job (シェル) が生きていても通常の idle-with-task 判定に進ませる (黙る方向には
# 倒さない安全弁。「本当に進んでいるか」を判定しようとはしない — それは
# classify_process_tree の責務ではない)。
#
# デフォルト 1800 秒 (30 分): 本番で観測されている正当な長時間実行 (pytest 一式
# が約 25 分。tests/CLAUDE.md) より長く、watchdog の絶対上限 (既定 3600 秒) より
# 十分短い — Director が watchdog の kill を待たずに気付けるようにする。
# Override via CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS for tests / tuning.
try:
    BACKGROUND_JOB_MAX_SECONDS = int(
        os.environ.get('CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS', '1800'))
except ValueError:
    _bad = os.environ.get('CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS')
    print(f"[dispatcher] WARNING: invalid CREWVIA_RULE5_BACKGROUND_JOB_MAX_SECONDS={_bad!r} "
          "— using default 1800", file=sys.stderr)
    BACKGROUND_JOB_MAX_SECONDS = 1800

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


def worker_holds_work(agent_name, all_tasks):
    """card の `worker` が `agent_name` で、手放されていない card が 1 枚でもあるか。

    「手放されていない」= `RELEASED_WORK_STATUSES` でも `pending` でもない。in_progress
    だけでなく needs_director / needs_human_review / blocked / verifying / ... を含む。
    pending を外すのは、`--reset` が worker を消すので pending に名前が残るのは取り残しで、
    Worker を生かす理由にならないため。

    以前の Rule 2 は `status == 'in_progress'` だけを見ていた。`plan.sh needs-director` が
    assignment を外すようになった (t001) ので、そのままでは判断待ちの Worker が
    「仕事なし」と読まれて退役の対象になる。倒す先は「殺さない」: 知らない status も
    保持に数える (allowlist ではなく除外側を数える)。

    `all_tasks` は `load_all_tasks()` の戻り (= `lib_task_cards` の入口を通った card)。
    ここで card を読み直さない。
    """
    return any(
        meta.get('worker') == agent_name
        and meta.get('status') not in RELEASED_WORK_STATUSES
        and meta.get('status') != 'pending'
        for _, meta in all_tasks
    )


def worker_waits_on_director(agent_name, all_tasks):
    """`agent_name` が needs_director の card を持っている (= Director の判断待ち) か。

    `needs-director` は assignment を外すので、assignment の有無だけでは「まだこの
    Worker は仕事を持っている」が読めなくなった。外す前は assignment が残っていたので
    busy 扱いになっていた —— その扱いをここで保つ (新しい task を割り当てない・Rule 5 を
    重ねて鳴らさない)。
    """
    return any(
        meta.get('worker') == agent_name and meta.get('status') in WAITS_ON_DIRECTOR_STATUSES
        for _, meta in all_tasks
    )


def codex_review_slot_busy(task_statuses_by_mission):
    """`queue/assignments/Kai-codex` が、いま走っているかもしれない run を指しているか。

    「同時 1 実行」の根拠は assignment の**有無**だった。`plan.sh needs-director` が撤去
    しない版 (t001 より前) や archive 前の取り残しでは、走っていない run の assignment が
    残り、以後の codex-review が恒久的に spawn されなかった (backlog #13)。

    指す task が手放し済み (`RELEASED_WORK_STATUSES`) か needs_director なら孤児で、塞がない
    (**読むだけ**。assignment を消すのは plan.sh の役目 —— 次の pull が上書きするか、
    `reap_kai_codex_orphan_assignment()` が毎サイクル掃除する。t009 / backlog #34)。

    塞ぐ側に倒すもの: assignment が読めない / `<mission>:<task>` の形でない / 指す task が
    見つからない (archive 済みなど) / 進行中の status。証明できない孤児は孤児と扱わない
    (誤って 2 つ目の run を走らせるより、Director に見える停止のほうが安い)。
    """
    raw = read_assignment(CODEX_REVIEW_AGENT)
    if is_missing(raw):
        return False
    if is_unreadable(raw):
        return True
    slug, _, task_id = raw.strip().partition(':')
    if not slug or not task_id:
        return True
    status = task_statuses_by_mission.get(slug, {}).get(task_id)
    if status in RELEASED_WORK_STATUSES or status in WAITS_ON_DIRECTOR_STATUSES:
        log(f"[codex-review] {CODEX_REVIEW_AGENT} の assignment は終わった task {slug}:{task_id} "
            f"(status={status}) を指す孤児 — spawn を塞がない")
        return False
    return True


def _referenced_task_status(slug, task_id, task_statuses_by_mission):
    """`slug:task_id` の status（不明なら `None`）。

    `task_statuses_by_mission` は `load_all_tasks(active_missions)` の戻りで、
    **active な mission だけ**を載せている。t117: 掃除の呼び出しが「active な
    ミッションが 0 件」「active が全部 done」の早期 return より前に動くように
    なったので、その 2 つの分岐では `task_statuses_by_mission` をまだ作って
    おらず、呼び出し側は空辞書 `{}` を渡す。さらに、参照先の mission がそもそも
    archive 済みなら `active_missions` に二度と載らないので、通常サイクルでも
    同じ穴がある。`.get(slug, {})` で空辞書に潰すと「終了していない」に誤読し、
    孤児が永久に残る（B3 が直すはずだった「ミッション終了後」の形そのもの）。

    mission が `task_statuses_by_mission` に無いときだけ、その mission の
    active dir → archive dir の順で card を直接読む（どちらにも無ければ
    判定不能 = `None`）。
    """
    statuses = task_statuses_by_mission.get(slug)
    if statuses is not None:
        return statuses.get(task_id)
    for base in (MISSIONS_DIR, ARCHIVE_DIR):
        task_file = base / slug / 'tasks' / f'{task_id}.md'
        if task_file.exists():
            meta, _ = read_task_card(task_file, task_id)
            return meta.get('status')
    return None


def _kai_codex_orphan_candidate(task_statuses_by_mission):
    """`assignment/Kai-codex` が、掃除 (`plan.sh reap-orphan-assignment`) を試す価値が
    あるかを安く判定する — 実際に消してよいかの最終判断ではない。

    `plan.sh` は 1 サイクルにつき最大 1 回しか起動したくない (孤児が無いサイクルが
    大多数)。このサイクルで既に読み込み済みの `task_statuses_by_mission` (無ければ
    `_referenced_task_status()` が card を直接読む。t117) だけを見て、subprocess を
    起動する価値があるかを判定する —— 実際の削除判定 (キューロックの中での読み
    直し・消してよい status の判定 (回復の R-2 と同じ 1 つの定義 `is_orphan_target`)・世代照合) は
    `plan.sh reap-orphan-assignment` 側 (cmd_reap_orphan_assignment) だけが行う。
    ここで「消してよい」と結論しない: 対象はあくまで `RELEASED_WORK_STATUSES`
    (`codex_review_slot_busy()` と同じ「終了した」の定義) で、`needs_director` は
    **含めない**。vNext 01a S4 で plan.sh 側は needs_director / worker の無い pending も
    消すようになったが (回復の R-2 と同じ集合)、この安い判定は**意図して狭いまま**にする:
    needs_director への遷移 (kai-review.sh → `plan.sh needs-director`) は自分で枠を撤去するので
    ここで候補に立てる意味が薄く、広げると毎サイクルの subprocess 起動が増えるだけで、
    dispatcher の restart も要る。
    """
    raw = read_assignment(CODEX_REVIEW_AGENT)
    if is_missing(raw) or is_unreadable(raw):
        return False
    slug, sep, task_id = raw.strip().partition(':')
    if not sep or not slug or not task_id:
        return False
    status = _referenced_task_status(slug, task_id, task_statuses_by_mission)
    return status in RELEASED_WORK_STATUSES


def reap_kai_codex_orphan_assignment(task_statuses_by_mission):
    """孤児化した `assignment/Kai-codex` があれば `plan.sh reap-orphan-assignment`
    で撤去する (t009 / backlog #34)。

    毎サイクル呼ばれる前提の安さ: `_kai_codex_orphan_candidate()` が False を返す
    大多数のサイクルでは subprocess を 1 本も起動しない。実際に起動するときも
    `--no-wait` (キューが混んでいれば待たず諦め、次のサイクルでまた判定し直す —
    watchdog → `plan.sh retire` (`lib_retirement.py`) と同じ理由: 5 秒ごとの
    ポーリングループを、混んだキュー 1 つで止めない)。

    ここで判定しないこと (すべて `plan.sh reap-orphan-assignment` 側の責務):
    実際に消してよいかどうかの最終判断 (キューロック内での読み直し・
    `needs_director` の除外・世代照合)。この関数はその subprocess を呼ぶかどうか
    と、結果をログに残すことだけを担う。

    `dispatch()` は「active なミッションが 0 件」「active が全部 done」の早期
    return より前でこの関数を呼ぶ (t117)。その 2 つの分岐では `task_statuses_by_
    mission` をまだ作っていないので `{}` が渡ってくる —— それでも参照先の
    status は `_kai_codex_orphan_candidate()` → `_referenced_task_status()` が
    card を直接読んで判定できる。
    """
    if not _kai_codex_orphan_candidate(task_statuses_by_mission):
        return
    if not PLAN_SH.is_file():
        log(f"WARNING: plan.sh not found at {PLAN_SH} — cannot reap orphan assignment")
        return
    argv = ['bash', str(PLAN_SH), 'reap-orphan-assignment', CODEX_REVIEW_AGENT, '--no-wait']
    # queue/registry のパスは env 経由で明示する (`os.environ` の継承任せにしない)。
    # このプロセス自身の QUEUE_DIR / REGISTRY_DIR は起動時の argv (bash 側で解決済み)
    # から来ており、周囲の env にある CREWVIA_QUEUE / CREWVIA_REPO_ROOT と必ずしも
    # 一致しない (テストが argv だけを差し替えて exec するとズレる。本番でも
    # 「このプロセスが実際に使っている値」を明示したほうが安全)。plan.sh は
    # `${CREWVIA_QUEUE:-...}` で env を優先するので、ここで明示すれば ambient な
    # env の値 (例: 実運用の本番 queue を指す開発者シェルの env) より必ず勝つ
    # (memory: qa-ambient-repo-root-points-at-production)。
    env = dict(os.environ)
    env['CREWVIA_QUEUE'] = str(QUEUE_DIR)
    env['CREWVIA_REPO_ROOT'] = str(REGISTRY_DIR.parent)
    # 監査ログの actor を `unknown` にしない (execution.md §8 の (2)。verifier-dispatcher.sh と同じ前例)。
    # Director のシェルから継いだ AGENT_NAME を、この subprocess の行に載せない。
    env['AGENT_NAME'] = 'dispatcher'
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=env,
            timeout=REAP_ORPHAN_COMMAND_TIMEOUT, cwd=str(REGISTRY_DIR.parent),
        )
    except Exception as e:  # noqa: BLE001 — a failed cleanup must never crash the daemon
        log(f"WARNING: plan.sh reap-orphan-assignment failed to run: {type(e).__name__}: {e}")
        return
    output = ((proc.stdout or '') + (proc.stderr or '')).strip()
    if proc.returncode == 0:
        last_line = output.splitlines()[-1] if output else ''
        log(f"[codex-review] {last_line}" if last_line
            else "[codex-review] reap-orphan-assignment: done (no output)")
    elif proc.returncode in (PLAN_LOCK_BUSY, PLAN_PRECONDITION_UNMET):
        # 何も書かれていない (キューが混んでいた / 前提が外れていた) — 次の
        # サイクルでまた判定し直すので、ここで騒がない。
        pass
    else:
        log(f"WARNING: plan.sh reap-orphan-assignment exited {proc.returncode}: {output[:300]}")


_target_record_memo = {}      # dispatch() が毎サイクルの先頭で空にする


def worker_target_record(agent_name):
    """`registry/workers/<agent>/target_dir.json` (検証済みの dict か `Unreadable`)。

    1 サイクルに 1 度だけ読む。読めない (壊れている) は毎サイクル警告すると 5 秒ごとに
    ログを埋めるので、通知スロットルに 1 回だけ出す。
    """
    if agent_name not in _target_record_memo:
        def _warn(msg, agent_name=agent_name):
            key = f"target_record_trouble_{agent_name}"
            if should_notify(key):
                log(f"WARNING: {agent_name}: {msg}")
                record_notify(key)
        _target_record_memo[agent_name] = _worker_target.load_record(
            REGISTRY_DIR, agent_name, warn=_warn)
    return _target_record_memo[agent_name]


def worker_may_take_task(agent_name, meta):
    """この Worker に task (`meta`) を割り当ててよいか — `(可否, 理由)`。判定は lib 1 つ。"""
    return _worker_target.worker_may_take(
        worker_target_record(agent_name), meta.get('target_dir'))


def worker_outstanding_assignment(agent_name, all_tasks):
    """割り当てメッセージを送ったが、まだ pull されていない task `(slug, task_id)` を返す (無ければ None)。

    #22: dispatcher は Worker に task A を送った 6 秒後に、別の task B (優先度が高い・
    unblock された) を同じ Worker に送り、Worker が両方を pull して `queue/assignments/<worker>` が
    上書きされた。送った時点では assignment ファイルはまだ無いので `is_idle` は真のままで、
    「送った」という事実は通知スロットル (`assign_<agent>_<slug>_<task>`) にしか残っていない。
    A がまだ pending で、その送信が TTL の内にあるあいだ、その Worker は「割り当て済み」として扱う。
    TTL を過ぎても pull されなければ (Worker が受け取っていない) この保護は外れ、A の再送に戻る
    (従来と同じ)。`plan.sh pull --task` 側の拒否 (別 task を持つ Worker) が最後の網。
    """
    cache = load_notify_cache()
    now = time.time()
    for slug, meta in all_tasks:
        if meta.get('status') != 'pending':
            continue
        sent_at = cache.get(f"assign_{agent_name}_{slug}_{meta.get('id')}")
        if sent_at is not None and now - sent_at <= NOTIFY_TTL:
            return slug, meta.get('id')
    return None


def worker_start_command(task_skills, target_dir, *, fresh):
    """Director がそのまま貼れる Worker 起動コマンド (`director.md` の起動手順と同じ形)。

    `fresh`: 同じ skill の Worker が (別の TARGET_DIR で) 生きているとき。registry-first の
    名前引きは同じ名前を返し、`start.sh` は「既に居る」で断るので、新しい名前を取らせる。
    名前は貼った時点で `assign-name.sh` が決める (dispatcher が registry を書き換えない)。
    """
    skills = ' '.join(shlex.quote(s) for s in sorted(task_skills))
    backend = os.environ.get('CREWVIA_MUX') or 'herdr'
    target = f"TARGET_DIR={shlex.quote(str(target_dir))} " if target_dir else ''
    return (
        f"cd {shlex.quote(str(REPO_ROOT))} && "
        f"AGENT_NAME=$(bash scripts/assign-name.sh {skills}{' --fresh' if fresh else ''}) "
        f"{target}CREWVIA_MUX_ENABLED=1 CREWVIA_MUX={shlex.quote(backend)} "
        f"bash scripts/start.sh worker {skills}"
    )


def sweep_stale_target_records(alive_workers):
    """生きていない Worker の古い target_dir 記録を片付ける (t009)。判定は lib 1 つ、例外は出さない。

    `alive_workers` は窓が有る **または** heartbeat が新しい Worker (`_alive_workers`)。窓の一覧だけを
    根拠にすると、mux が 1 人分だけ一時的に落とした回に、生きている Worker の記録を消しうる。
    """
    try:
        for name in _worker_target.sweep_stale_records(REGISTRY_DIR, set(alive_workers)):
            log(f"[target-record] swept stale TARGET_DIR record for retired Worker {name!r}")
    except Exception as e:
        log(f"WARNING: stale TARGET_DIR record sweep failed: {e!r}")


def dependency_gate(slug, meta, done_ids_by_mission, task_statuses_by_mission):
    """この pending task の依存判定 (`DependencyVerdict`: unmet / held)。

    規則は lib_dep_rules に 1 つだけ (plan.sh pull / task-graph と共有)。dispatch()
    はここを通す —— テストが「dispatcher はこの card を投げるのか」を本物のコードで
    直接問えるように、判定を dispatch() の外に出してある (tests/test_failed_dependency_hold.py)。
    failed の依存は held (Director が release-dep するまで投げない、t007)。
    """
    return card_dependencies(
        meta,
        done_ids_by_mission.get(slug, set()),
        task_statuses_by_mission.get(slug, {}),
    )


# ---------------------------------------------------------------------------
# Notification dedup cache
# ---------------------------------------------------------------------------

def load_notify_cache():
    """通知スロットル `{key: 最後に送った epoch 秒}`。

    読み取りと形の検証は入口 (`load_json_store`) の 1 つ (t026)。使えないとき
    (無い / 壊れている / 値が数でない・NaN・遠い未来) は `{}` = スロットルを失う =
    **もう一度送る** 側に倒す (冪等。次の `record_notify()` が正しい形で書き直す)。
    値を検証せずに信じると、`cache[key]` が `TypeError` でサイクルを落とすか、NaN・未来時刻で
    その key の通知を永久に遮る。
    """
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


def forget_notify(prefix, keep=None):
    """`prefix` で始まるスロットルを捨てる (`keep` だけは残す)。捨てる = 「送れる」側。"""
    cache = load_notify_cache()
    gone = [k for k in cache if k.startswith(prefix) and k != keep]
    if not gone:
        return
    for k in gone:
        del cache[k]
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

    読み取り・JSON・**各エントリの形** の検証は `load_json_store()` (1 つの入口)。
    ENOENT (まだ無い) は `{}`。それ以外の失敗は `Unreadable` のまま返し、
    呼び出し側が「使えない」として扱う (空とは別)。**壊れたエントリが 1 つでもあれば
    台帳全体が `Unreadable`** (t026 / Kai 3 巡目 P2): 外側だけ検証して内側を信じると、
    `{"bad": {"slug": []}}` で `prune_told()` が毎サイクル `TypeError` になり、
    prune もサイクルの残りも止まって、状態を離れて戻った task が永久に沈黙する。
    `Unreadable` のときの向きは **再送側** (`already_told` は False、`prune_told` は何もしない、
    `record_told` は作り直す)。
    """
    told = load_json_store(TOLD_FILE, check=told_ledger_problem)
    if is_missing(told):
        return {}
    if is_unreadable(told):
        _told_trouble(f"{TOLD_FILE} を使えない ({told.reason})")
    return told


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


def record_told(key, fp, kind, slug, task_id, throttle_key=None):
    """送れた通知を台帳に書く。台帳が壊れていたら作り直す (自己修復)。

    同じ key の fingerprint が変わった (A → B) ときは、以前の fingerprint のスロットル
    (`<key>#<旧fp>`) を捨てる (`throttle_key` = 今送った分は残す)。捨てないと、
    A → B → A で 3 回目の A が 1 回目の A の `<key>#<fp_A>` に NOTIFY_TTL のあいだ
    遮られ、**新しい事象の通知が遅れる** (t021 / Kai P2)。
    """
    entry = {'fp': fp, 'kind': kind, 'slug': slug, 'task': str(task_id)}
    # 書くものは、読み手が受け付ける形と同じでなければならない (t026)。読み手だけが
    # 厳しいと、書いたばかりの台帳を自分が「壊れている」と読み、永久に再送側へ倒れる。
    problem = told_entry_problem(key, entry)
    if problem:
        _told_trouble(f"台帳に書けない形のエントリ ({problem})")
        return False
    # 台帳の書き手は watchdog (timeout 通知) と 2 人 (t021)。read-modify-write の全体を
    # `told_lock()` の下に置く — 原子的な置換だけでは、相手が今書いたエントリを、
    # 古い読み取りの書き戻しで消しうる。取れなければ「書けなかった」= 再送側 (次サイクル)。
    with told_lock(TOLD_FILE) as held:
        if not held:
            _told_trouble(f"{TOLD_FILE} のロックを取れない")
            return False
        told = load_told()
        if is_unreadable(told):
            told = {}
        prev = told.get(key)
        if isinstance(prev, dict) and prev.get('fp') != fp:
            forget_notify(f'{key}#', keep=throttle_key)
        told[key] = entry
        return save_told(told)


def prune_told(live_keys, observed_slugs):
    """状態を離れた task の記録を捨てる。

    観測できた mission (`observed_slugs`) の task だけが対象。観測できなかった
    (破損カード・走査失敗) ものは「離れた」の証拠にならないので触らない。
    """
    # watchdog の timeout 通知 (kind=timeout) は TTL のあいだ対象外 — その task は後始末で
    # pending に戻っていて、live key に現れないのが普通 (`told_is_fresh_timeout()`)。
    with told_lock(TOLD_FILE) as held:
        if not held:
            return      # 次のサイクルでやり直す。prune は遅れてよい (欠落ではなく遅延)
        told = load_told()
        if is_unreadable(told):
            return
        stale = [k for k, e in told.items()
                 if isinstance(e, dict) and e.get('slug') in observed_slugs
                 and k not in live_keys and not told_is_fresh_timeout(e)]
        if not stale:
            return
        for k in stale:
            del told[k]
        save_told(told)
    # 台帳の記録だけでなくスロットル (`<key>#<fp>`) も捨てる。状態を離れて同じ理由で
    # 戻ったのは新しい事象で、fingerprint は前回と同じ。スロットルが残っていると
    # 台帳が「伝えていない」と言っても NOTIFY_TTL のあいだ遮られる (t021 / Kai P2)。
    #
    # 選んだ理由 (状態の「回」をスロットル key に入れる案を採らなかった): 回の識別子は
    # 台帳に持たせるしかなく、台帳が使えないとき (= 再送側に倒したいとき) に key が
    # 定まらず、連射防止のスロットルが働かなくなる。離脱時に捨てるなら、台帳が使える
    # ときだけ (離脱を観測できたときだけ) 捨てるので、倒す向きが変わらない。
    for k in stale:
        forget_notify(f'{k}#')


def clear_told_key(key):
    """台帳から 1 件だけ消す (`prune_told` と違い、queue の mission/task に紐づかない
    通知向け — B2 の main-checkout-drift はどの mission の slug にも属さないので
    `observed_slugs` 経由の prune では拾えない)。状態が「きれいになった」ときに
    呼ぶことで、次に同じ fingerprint の状態が来ても再通知される (解消後の再通知)。
    戻り値は「畳めたか」(t018): ロックが取れない・台帳が読めないは False。呼び出し側が、
    畳めていないのに自分の記録だけ消して台帳キーを孤児にしないための印。
    ロックが取れなければ何もしない (次のサイクルでやり直す。消し忘れの直接の害は
    「同じ状態が繰り返し起きたときにだけ再通知が 1 サイクル遅れる」だけで、
    通知そのものが消えるわけではない)。

    台帳の記録だけでなくスロットル (`<key>#<fp>`) も捨てる — `prune_told()` と同じ
    理由 (t021 / Kai P2): 消さないと、同じ fingerprint がもう一度起きたとき
    `already_told` は「伝えていない」を返すのに `should_notify(throttle_key)` が
    NOTIFY_TTL のあいだ遮り、解消後の再通知が最大 5 分遅れる。
    """
    with told_lock(TOLD_FILE) as held:
        if not held:
            return False
        told = load_told()
        if is_unreadable(told):
            return False
        if key in told:
            del told[key]
            save_told(told)
    forget_notify(f'{key}#')
    return True


def observed_missions(all_tasks, active_missions):
    """破損カードを 1 枚も含まない active mission (= 全 task を観測できた)。"""
    broken = {slug for slug, meta in all_tasks
              if meta.get('status') == CORRUPT_TASK_STATUS}
    return {slug for slug in active_missions if slug not in broken}


def notify_state_once(key, fp, kind, slug, task_id, build_msg, *, director_live=lambda: True):
    """状態ベースの通知を、状態が変わるまで 1 回だけ送る。

    順序 (安い判定を先に): 台帳が「伝えた」→ 何もしない / スロットル → 何もしない /
    Director 不在 → 記録せず見送る (戻ったらすぐ送る) / 送る。
    `director_live` は **呼び出せる値** (遅延評価)。mux への問い合わせは、台帳とスロットルを
    通り抜けて実際に送ろうとする通知があるときにだけ行う — 通知対象が 1 件も無い
    サイクル (= ほとんどのサイクル) で 5 秒ごとに `mux list` を叩かないため (t021 / QA t011 の P3)。
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
    if not director_live():
        log(f'WARNING: {kind} — Director 不在のため通知スキップ: {slug}/{task_id}')
        return False
    if tmux_send(_director_name(), build_msg()):
        record_notify(throttle_key)
        record_told(key, fp, kind, slug, task_id, throttle_key=throttle_key)
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


def pull_agent_flag(agent_name):
    """割り当て文の `plan pull` に付ける ` --agent <名前>` (先頭に空白つき)。付けられない名前なら空文字。

    pull が担当者なし (agent=null) で試行を始める事故は、Worker のシェルの AGENT_NAME が空だったことが原因。
    文で名前を明示すれば env に頼らない。シェルに貼るので、plan.sh pull が拒否する名前
    (agent_name_problem) や空白・引用符を含む名前は付けず従来の文にする — 壊れた文を送らない。
    """
    if not isinstance(agent_name, str) or not agent_name.strip() or agent_name != agent_name.strip():
        return ''
    if _agent_name_problem(agent_name) or shlex.quote(agent_name) != agent_name:
        return ''
    return f' --agent {agent_name}'


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


def sweep_stale_pane_records():
    """Drop `registry/mux/<name>.json` records whose pane no longer exists (t001).

    A Worker's pane closes without `kill()` — watchdog's retirement signals the
    shell pid and the mux closes the pane itself — so the record naming it
    outlives it.  This is the same shape as `sweep_spawn_grace_markers()` and
    lives beside it for the same reason: this daemon owns `registry/mux/`, and
    the sweep is keyed on the pane being *gone*, not on any one kill path
    succeeding.

    The decision is `lib_mux.reap_stale_pane_records()` and nothing here
    repeats it.  It asks the mux about the id each record names, and drops a
    record only on a definite "no such pane"; a mux that cannot be asked
    (down, timing out) leaves every record where it is, because the record is
    the proof `may_destroy_pane()` needs.  It never raises, and a failure here
    is a log line, never a failed cycle.

    Rollback: a mux outage is already safe (records are kept).  To stop the
    sweep, restart this daemon with `CREWVIA_MUX_RECORD_SWEEP=0`.
    """
    try:
        for name in _mux.reap_stale_records():
            log(f"[mux-record] swept stale spawn record for vanished pane {name!r}")
    except Exception as e:
        log(f"WARNING: stale spawn-record sweep failed: {e!r}")


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
        # 読み取りと JSON は入口の 1 つ (t026)。
        data = load_json_store(
            p, warn=lambda msg: log(f"WARNING: mux cache: {msg}"))
        if is_unreadable(data):
            return None
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
    # 読み取りと形の検証は入口 (`load_json_store`) の 1 つ (t026)。`since` が文字列だと
    # `now - since` が TypeError、list だと `.get` が AttributeError で、Rule 5 のサイクルが落ちる。
    entry = load_json_store(
        _state_json_path(name), check=rule5_state_problem,
        warn=lambda msg: log(f"WARNING: rule5 state entry: {msg}"))
    if is_unreadable(entry):
        # 倒す先はここだけ「空」でよい —— grace が最初からやり直しになる
        # = **通知が遅れる**側で、破壊も割り当ても起こらない
        # (knowledge/empty-vs-unobservable.md §2 の I)。
        return {}
    return entry


def _save_state_entry(name: str, state: str, since: float) -> None:
    """Persist state + since timestamp for Rule 5 grace tracking."""
    try:
        STATE_JSON_DIR.mkdir(parents=True, exist_ok=True)
        _state_json_path(name).write_text(
            json.dumps({'state': state, 'since': since}), encoding='utf-8'
        )
    except Exception as e:
        log(f'WARNING: cannot write state entry for {name!r}: {e}')


def _job_since_path(name: str) -> Path:
    return STATE_JSON_DIR / f'{name}.job-since.json'


def _load_job_since(name: str) -> tuple[Optional[float], bool]:
    """t074 追補: 「裏の job が連続して見え続けている」開始時刻。`_load_state_entry`
    の `since` (現在の条件 A/B の grace 計測。job があるあいだ毎サイクル
    `time.time()` へ書き直される — `test_grace_counts_from_the_end_of_the_job`)
    とは別物なので別ファイルに持つ (同じフィールドを 2 つの意味で使うと
    どちらかの意味が壊れる)。

    戻り値は `(job_since, reliable)`。**「無い」と「読めない」を区別する**
    (t082 P2, Codex review 4巡目): ファイルがまだ無い (`is_missing`) のは
    「この job を初めて見た」という正常な状態で、`reliable=True` /
    `job_since=None` (呼び出し側が `now` で新規に計る)。ファイルは**あるのに
    壊れている/形が合わない**のは異常な状態で、`reliable=False` を返す —
    これを None と同じに潰して「初めて見た」扱いにすると、`job_since` を
    保てない環境 (registry/mux が壊れている等) では**毎サイクル `now` を
    新規タイマーとして採用し続け、`BACKGROUND_JOB_MAX_SECONDS` が永久に
    切れない** (安全弁そのものが黙る側に壊れる)。呼び出し側は `reliable`
    が False なら上限判定を信用せず、通常の idle-with-task 判定に流すこと
    (「タイマーを確実に保てないときは通知を許す」)。
    """
    entry = load_json_store(
        _job_since_path(name), check=job_since_state_problem,
        warn=lambda msg: log(f"WARNING: rule5 job_since entry: {msg}"))
    if is_missing(entry):
        return None, True
    if is_unreadable(entry):
        return None, False
    value = entry.get('job_since')
    if is_finite_number(value):
        return value, True
    return None, False  # スキーマ検証済みのはずだが、念のため信用しない側に倒す


def _save_job_since(name: str, job_since: Optional[float]) -> bool:
    """job_since を書く。None は「job が消えた/条件から外れた」— ファイルごと消す
    (次に job が現れたときまた新しく計り直す)。

    戻り値は書き込みが**成功したか** (t082 P2)。呼び出し側は、新規タイマーの
    保存に失敗したら (今回計った `job_since` が次のサイクルで読み直せない)
    その事実を信用せず、上限判定を通常の idle-with-task 判定に譲ること
    (書き込み失敗を握り潰して「保存できた」ふりをすると `_load_job_since` の
    修正が意味を持たなくなる — 毎サイクル保存に失敗し続け、毎サイクル
    「初めて見た」と読み直して `now` を採用し、結局上限が切れない)。
    """
    path = _job_since_path(name)
    if job_since is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log(f'WARNING: cannot clear job_since entry for {name!r}: {e}')
            return False
        return True
    try:
        STATE_JSON_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'job_since': job_since}), encoding='utf-8')
    except Exception as e:
        log(f'WARNING: cannot write job_since entry for {name!r}: {e}')
        return False
    return True


def worker_has_background_work(target: str) -> bool:
    """True iff this Worker's pane has a tool / background job running under claude.

    B1 (#27): `run_in_background` の shell と Monitor は、生きている間 herdr の
    agent_status を `idle` / `done` にする (2026-09-27 実測: 裏の `sleep` が生きている
    50 秒間ずっと `done`、画面末尾は `1 shell` / `1 monitor`)。ツール呼び出しが止まる
    ので当然だが、Worker は待っているだけで停止していない。同じ瞬間、claude の直下には
    `bash -c ...` が生えていて、watchdog の idle 判定 (`classify_process_tree`) は
    `executing` と読む。**同じ定義を共有する** — 別の判定を dispatcher に置かない。

    t074 (Codex review 3巡目 P1): `classify_process_tree` の判定根拠はもう時刻
    (t016/t049) でも comm (t065) でもない。`lib_pane_process.py` docstring 参照
    — 本番の `npm exec @playwright/mcp` は npm の `process.title` 書き換えと
    `sh -c "..."` を挟む経路のせいで comm ベースの許可リストでは job と誤読され
    (偽陰性)、Rule 5 を永久に抑制し続ける欠陥があった。今は「Bash tool /
    Monitor のラッパー (shell snapshot を `source` する形) の子孫か」で区別する
    ので、呼び出し側 (ここ) は名前も時刻も意識する必要が無い。

    倒す向き (この判定は**通知**を止める側): 観測できない (`unknown` / pane pid が
    引けない / 例外) ときは False = 従来どおり通知する。誤って True にすると詰まった
    Worker の通知が黙るが、False の誤りは Director に 1 通余計に届くだけ。watchdog は
    逆向き (`unknown` は殺さない) — 殺す側の誤りの方が高くつくから、判定ごとに向きが違う。
    """
    try:
        pane_pid = _mux.pid(target)
        if pane_pid is None:
            return False
        return classify_process_tree(pane_pid) == 'executing'
    except Exception as e:  # 観測失敗 → 通知する側に倒す。黙って True にしない
        log(f'WARNING: Rule 5 — cannot classify pane process tree for {target!r}: {e!r}')
        return False


# ---------------------------------------------------------------------------
# 利用枠切れ (C2 / t005)
# ---------------------------------------------------------------------------
#
# Worker の画面が「利用枠切れ」(lib_usage_limit.detect) のとき、Rule 5 は idle-with-task を
# 出さない。代わりに:
#   1. Director に **1 回だけ** 「利用枠切れ (リセット予定 <時刻|不明>)」を伝える
#      (通知台帳 notified-state.json の作法。解消したら台帳から外す)
#   2. リセット予定 + RESUME_GRACE を過ぎたら、Worker に **1 回だけ** 再開を促す
#      (枠はアカウント単位なので起動し直しても回復しない。リセット後も自動では再開しない)
#   3. 促した後もなお利用枠切れなら、Director に **1 回だけ** 再通知する
# 抑制には上限がある (lib_usage_limit.excuse_deadline): それを過ぎても表示が続くなら通常の
# Rule 5 判定に戻す。画面が読めない・構造が合わないときは「利用枠切れではない」(従来どおり)。
USAGE_LIMIT_RESUME_GRACE = 120         # リセット予定からこの秒数後に再開を促す
USAGE_LIMIT_UNKNOWN_RESUME_AFTER = 5 * 3600 + 300   # 時刻不明: 見え始めからこの秒数後
USAGE_LIMIT_STILL_AFTER = 300          # 再開を促してからこの秒数経っても続くなら再通知
USAGE_LIMIT_RESUME_MESSAGE = (
    '利用枠がリセットされたはずです。中断していた作業をそのまま再開してください '
    '(まだ制限中なら、その旨だけ返してください)。'
)


def _usage_limit_path(name: str) -> Path:
    return STATE_JSON_DIR / f'{name}.usage-limit.json'


def _load_usage_limit(name: str) -> Optional[dict]:
    """記録が無い/読めない → None (新しい利用枠切れとして作り直す。読めないことで
    再開の促しが二重になるのは、`usage_limit_resume_<name>` のスロットルが止める)。"""
    entry = load_json_store(
        _usage_limit_path(name), check=usage_limit_state_problem,
        warn=lambda msg: log(f"WARNING: usage-limit entry: {msg}"))
    if is_missing(entry) or is_unreadable(entry):
        return None
    return entry


def _save_usage_limit(name: str, entry: Optional[dict]) -> bool:
    """記録を書く (None = 消す)。戻り値は成否 (t018 / P2)。呼び出し側は、記録を保てない
    (= 上限つきのタイマーを維持できない) なら利用枠切れとして扱わず通常の Rule 5 に戻すこと。
    書けないのに True を返すと毎サイクル新しい first_seen / reset_at になり、免除の期限が
    永遠に未来のまま Rule 5 だけが止まる (B1 の `_save_job_since` と同じ作法)。"""
    path = _usage_limit_path(name)
    try:
        if entry is None:
            path.unlink(missing_ok=True)
            return True
        STATE_JSON_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry), encoding='utf-8')
        return True
    except Exception as e:
        log(f'WARNING: cannot write usage-limit entry for {name!r}: {e}')
        return False


def _retire_usage_limit(name: str) -> bool:
    """利用枠切れが終わった (回復した) Worker の記録と台帳キーを畳む。台帳キーを先に消し、
    消せたときだけ記録を消す: 記録だけ消えて台帳キーが残ると、以後の健全な観測は「記録が無い」
    ので二度と消しに来ない (t018 / P2)。消せなければ記録が残り、次サイクルでやり直す。"""
    for k in (f'usage-limit_{name}', f'usage-limit-still_{name}'):
        if not clear_told_key(k):
            return False
    return _save_usage_limit(name, None)


def handle_usage_limit(name: str, target: str, assignment_file: Path) -> bool:
    """利用枠切れの Worker を Rule 5 の代わりに扱う。True = Rule 5 は黙る (この関数が扱った)。

    False は「利用枠切れではない」「上限を過ぎたので通常の判定に戻す」「観測できなかった」。
    """
    now = time.time()
    try:
        screen = _mux.capture(target)
    except Exception as e:
        log(f'WARNING: usage-limit — cannot capture {target!r}: {e!r}')
        return False   # 観測できない → 利用枠切れではない (従来どおり)
    if not lib_usage_limit.observable(screen):
        # capture の失敗は空文字列で返る。「見えなかった」観測は記録も台帳も触らない
        # (消すと次に読めたとき新しい deadline が付く — t018 / P1)。免除もしない。
        return False
    prev = _load_usage_limit(name)
    entry = lib_usage_limit.observe(prev, screen, now)
    key = f'usage-limit_{name}'
    still_key = f'usage-limit-still_{name}'
    if entry is None:
        if prev is not None or _usage_limit_path(name).exists():
            # 解消した: 記録と台帳を畳む (次に同じ状態が来たら再通知する)。記録があったときだけ
            # (通常の Worker で毎サイクル台帳のロックを取らない)。
            _retire_usage_limit(name)
        return False

    same = prev is not None and (lib_usage_limit.identity(prev.get('notice'))
                                 == lib_usage_limit.identity(entry['notice']))
    entry['resumed_at'] = prev.get('resumed_at') if same else None
    # 台帳の fp は通知行 + 見え始め: 同じ通知行 (相対時刻など) の次の利用枠切れは別の fp になり、
    # 前回の台帳キーが消し損ねで残っていても最初の通知が出る (t018 / P2)。
    ident = f'{lib_usage_limit.identity(entry["notice"])}@{int(entry["first_seen"])}'

    # 記録を保てない (書けない) なら上限つきのタイマーを維持できない → 通常の Rule 5 に戻す
    # (t018 / P2)。上限超えの場合も、同じ通知行の間は同じ first_seen を保たせるため保存する。
    if not _save_usage_limit(name, entry):
        return False
    if now > lib_usage_limit.excuse_deadline(entry):
        # 上限超え: 通常の Rule 5 判定へ (永久に黙らない)。
        return False

    # Rule 5 の grace は、利用枠切れが終わってから数え直す
    _save_state_entry(name, 'usage-limit', now)

    slug, task_id = '?', '?'
    raw = read_assignment(name)
    if not is_unreadable(raw):
        raw = raw.strip()
        if ':' in raw:
            slug, task_id = raw.split(':', 1)
        elif raw:
            task_id = raw

    def _director_live():
        return bool(_mux.list(suffix='-director'))

    reset_txt = lib_usage_limit.format_reset(entry)
    # 台帳の slug は mission ではなく '_daemon' — mission の slug を入れると、task が live key
    # に現れない (この key は task の key ではない) ため prune_told() が毎サイクル捨てて
    # 再通知になる (main-checkout-drift と同じ理由)。
    notify_state_once(
        key, ident, 'usage-limit', '_daemon', name,
        lambda: (f'[Rule 5] Worker {name} が利用枠切れです (リセット予定 {reset_txt}, '
                 f'task {task_id}, mission={slug})。枠はアカウント単位で、起動し直しても'
                 f'回復しません。リセット後にこの Worker へ再開を促します (自動)。'
                 f'表示: {entry["notice"][:120]}'),
        director_live=_director_live)

    # 再開の促し (1 回だけ)
    reset_at = entry.get('reset_at')
    resume_due = (reset_at + USAGE_LIMIT_RESUME_GRACE if isinstance(reset_at, (int, float))
                  else entry['first_seen'] + USAGE_LIMIT_UNKNOWN_RESUME_AFTER)
    if entry['resumed_at'] is None and now >= resume_due:
        resume_key = f'usage_limit_resume_{name}'
        if should_notify(resume_key) and tmux_send(target, USAGE_LIMIT_RESUME_MESSAGE):
            record_notify(resume_key)
            entry['resumed_at'] = now
    elif (entry['resumed_at'] is not None
          and now >= entry['resumed_at'] + USAGE_LIMIT_STILL_AFTER):
        notify_state_once(
            still_key, f'{ident}@{int(entry["resumed_at"])}', 'usage-limit', '_daemon', name,
            lambda: (f'[Rule 5] Worker {name} は再開を促した後も利用枠切れのままです '
                     f'(リセット予定 {reset_txt}, task {task_id}, mission={slug})。'
                     f'枠がまだ戻っていない可能性があります。表示: {entry["notice"][:120]}'),
            director_live=_director_live)
    # resumed_at を残す (書けなければ再開の促しが二重になりうる → 通常の Rule 5 に戻す)
    return _save_usage_limit(name, entry)


def check_rule5(name: str, target: str, assignment_file: Path, task_statuses_by_mission: dict,
                waits_on_director: bool = False) -> None:
    """Rule 5: detect blocked / idle-with-task and notify Director.

    A: state == "blocked"
    B: state in {"idle", "done"} AND assignment_file exists
       AND no tool / background job (run_in_background shell, Monitor) is running in
       the pane — such a Worker is waiting, not stopped (B1 / #27).  It is treated as
       'working': dedup keys cleared, grace timer restarted, so once the job ends the
       grace period counts from then.  A job that never ends is bounded by
       watchdog's absolute max, not by this rule.

    State is persisted to registry/mux/<name>.state.json for grace tracking.
    Notifications are deduped via the standard NOTIFY_TTL cache.

    tmux mode (state == "unknown") → skip entirely (safe side).

    t032 F4: once the escalating Worker goes idle after `plan.sh needs-director`,
    Rule 5 must not send a second notification ("[Rule 5] idle-with-task" /
    "blocked") for the same root cause, forever (every NOTIFY_TTL) — the
    needs_director notification owns it.  Same exclusivity idea as the
    failed+handoff_path block, which only fires for status=='failed'.

    t001 (#13): `plan.sh needs-director` now retires the assignment, so the
    assignment file no longer says "this Worker is parked on a needs_director
    task".  Condition B needs the file and stops firing by itself; condition A
    (pane blocked) used to be suppressed only through the assignment, so the
    caller now passes `waits_on_director` (the Worker's own card says so).
    The assignment-based check stays for assignments left by an older plan.sh.
    """
    # IMPORTANT: mux pane labels are '<name>-worker' (e.g. 'Omar-worker'), not
    # the bare agent name.  Use `target` (= window_target from
    # tmux_list_worker_windows) so the label lookup succeeds.  Using `name`
    # always returns 'unknown' (pane not found) and silently disables Rule 5.
    st = _mux.state(target)

    # C2 (t005): 利用枠切れの Worker は idle-with-task ではない — 別の扱い。
    # 観測に使うのは idle/done/blocked のとき (割り当てのある Worker) だけ。
    if st in ('idle', 'done', 'blocked') and (st == 'blocked' or assignment_file.exists()):
        if handle_usage_limit(name, target, assignment_file):
            return
    elif st == 'working' and _usage_limit_path(name).exists():
        # 記録を消してよい根拠は「回復を観測した」ことだけ: 動いている (working) を mux が
        # 返した。`unknown` は一時的な lookup / RPC の失敗でも返る (HerdrBackend.state) ので回復の
        # 証拠ではない — 消すと次に読めたとき同じ通知から新しい first_seen / reset_at が付き、
        # 免除が延びる (t020 / P1)。idle/done で割り当てなしの Worker も観測していないので保つ。
        _retire_usage_limit(name)

    # B1 (#27): idle/done with a live background job is 'working' for Rule 5.
    # Only condition B (assignment exists) is affected — 'blocked' still notifies.
    # The /proc scan runs only for the idle-with-assignment case.
    has_job = st in ('idle', 'done') and assignment_file.exists() and worker_has_background_work(target)
    if has_job:
        # t074 追補 (BACKGROUND_JOB_MAX_SECONDS 参照): 「裏に job がある」で黙るのは
        # 無期限ではない。job が連続して見え続けている時間を計り、上限を超えたら
        # 通常の idle-with-task 判定に進ませる (黙る方向には倒さない安全弁 —
        # Ren の pgrep 自己一致ループのように、ラッパーの子孫だが何十分も
        # 進んでいない job を Rule 5 が永久に黙らせないため)。
        now = time.time()
        job_since, reliable = _load_job_since(name)
        if job_since is None:
            job_since = now
            # t082 P2: 保存に失敗したら、今回計った job_since は次のサイクルで
            # 読み直せない (= 保てていない)。reliable を落とし、上限判定を
            # 信用しない側に倒す (握り潰して「保存できた」ふりをしない)。
            reliable = _save_job_since(name, job_since) and reliable
        if reliable and (now - job_since <= BACKGROUND_JOB_MAX_SECONDS):
            st = 'working'
        # else: 上限超え、またはタイマーを確実に保てない — st は 'idle'/'done'
        # のまま通常の判定に流す (黙る方向には倒さない)。job_since はクリア
        # しない (同じ長時間 job が続く限り、次サイクルも同じ経過時間を計り
        # 続け、通常の grace/NOTIFY_TTL の判定に任せる)。
    else:
        # job が消えた/条件から外れた → 連続計測をリセットする
        _save_job_since(name, None)

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
        if assigned_task_status in WAITS_ON_DIRECTOR_STATUSES or waits_on_director:
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


def spawn_kai_review(slug, meta, task_statuses_by_mission):
    """Background-spawn kai-review.sh for a codex-review task.

    Preconditions:
      - meta['skills'] contains 'codex-review'
      - task is unblocked and pending
      - caller has already checked notify dedup (via should_notify)

    Behavior:
      - Extracts pr_number from frontmatter; if absent, logs warning and returns.
      - Refuses to spawn while queue/assignments/Kai-codex points at a run that
        may still be in flight (`codex_review_slot_busy()`).  An assignment left
        behind by a run that already finished / escalated is an orphan and does
        not block (t001 / backlog #13).
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
    if codex_review_slot_busy(task_statuses_by_mission):
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
    except Exception as e:       # backstop: 想定外でも dispatch サイクルを落とさず「保留」に倒す
        return 'unreadable', Unreadable(REGISTRY_DIR, f'unexpected {type(e).__name__}: {e}')
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


def handle_codex_review(slug, meta, live_state_keys, task_statuses_by_mission):
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
                    f"手動差分レビューに切り替え、結果を報告してください (task は pending のままなので、"
                    f"先に plan.sh update {task_id} --status in_progress --reset --mission {slug} で開いてから "
                    f"plan.sh done {task_id} --mission {slug}。pending のままの done は拒否されます。01c E3)。"
                    f"codex-review を意図して再試行するなら "
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
                          director_live=director_live_for_state_notices)
        return
    if not meta.get('pr_number'):
        # t036: PR 番号の無い codex-review は spawn できない。以前は log() だけで、しかも
        # 呼び出し側が handle_codex_review() の後に無条件 continue するので no_worker の
        # Director 通知にも届かず、pending のまま誰にも知らされなかった (実装 Worker が
        # `plan.sh done --pr` を付け忘れると、Codex を通らずに merge されうる)。
        # 状態ベースなので 1 回だけ (番号が入れば状態を離れ、台帳から捨てられる)。
        # ここに来るのは依存が満たされて pending の task だけ — blocked のまま止めている
        # task は Director が意図して止めているので通知しない。
        key = f'review_no_pr_{slug}_{task_id}'
        live_state_keys.add(key)
        fp = fingerprint('review_no_pr')
        def build_msg():
            return (
                f"[review-no-pr] task {task_id} (mission={slug}): codex-review が ready なのに "
                f"pr_number がありません。kai-review.sh を起動できず、このままでは Codex を通らずに "
                f"merge されえます。実装 Worker が plan.sh done --pr を付け忘れた可能性があります。"
                f"PR 番号を入れて再開してください: "
                f"plan.sh update {task_id} --pr-number <N> --status pending --mission {slug}"
            )
        notify_state_once(key, fp, 'no-pr-number', slug, task_id, build_msg,
                          director_live=director_live_for_state_notices)
        return
    spawn_key = f'kai_spawn_{slug}_{task_id}'
    if should_notify(spawn_key):
        if spawn_kai_review(slug, meta, task_statuses_by_mission):
            record_notify(spawn_key)


_director_live_memo = []     # dispatch() が毎サイクルの先頭で空にする


def director_live_for_state_notices():
    """Director の窓が生きているか。**サイクル内で最初に必要になったときに 1 回だけ**問い合わせる。

    以前は needs_director / handoff のループの前で無条件に呼んでいたので、通知対象が
    無いサイクルでも mux への問い合わせが 5 秒ごとに増えていた。呼び出し側は
    `notify_state_once(director_live=director_live_for_state_notices)` と **関数のまま**
    渡す。1 サイクルの中で結果を使い回すのは、通知のたびに問い合わせを増やさないため。
    """
    if not _director_live_memo:
        _director_live_memo.append(bool(_mux.list(suffix='-director')))
    return _director_live_memo[0]


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
    _director_live_memo.clear()
    _target_record_memo.clear()
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

    # t009 / backlog #34, t117: Kai-codex の孤児 assignment の掃除は「active な
    # ミッションが 0 件」「active が全部 done」の早期 return より前に置く。
    # 以前は `load_all_tasks()` の後 (=両方の早期 return の後ろ) にあり、
    # 最後のミッションが完了した直後に assignment が孤児化すると、以後の
    # サイクルは毎回そのどちらかの return で抜けてしまい、掃除が二度と
    # 走らなかった (B3 が直すはずだった「ミッション終了後」の形そのもの)。
    # ここではまだ `task_statuses_by_mission` を作っていないので `{}` を渡す —
    # 参照先 mission の card は `_kai_codex_orphan_candidate()` が直接読む
    # (active dir / archive dir の両方。archive 済みミッションを指す孤児も
    # これで掃除できる)。
    reap_kai_codex_orphan_assignment({})

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
        task_statuses = task_statuses_by_mission.get(slug, {})
        verdict = dependency_gate(slug, meta, done_ids_by_mission, task_statuses_by_mission)
        unmet_deps = verdict.unmet
        if unmet_deps:
            # Suppress repeated output of the same blocked state to avoid
            # flooding the scrollback (same line every 5s → 40-line buffer fills
            # up with identical rows, hiding diagnostic send logs).
            # Log once on first detection, then at most once per NOTIFY_TTL
            # (default 300s) as a heartbeat so the state remains visible.
            _bkey = f"blocked_log_{slug}_{meta.get('id')}"
            if should_notify(_bkey):
                if verdict.held:
                    # failed の依存は誰も自動では解かない。ログにも出口を残す
                    # (plan.sh status にも同じ文面が出る)。
                    log(f"[held] task {meta.get('id')} (mission={slug}) — failed deps "
                        f"{verdict.held}: Director の判断待ち。"
                        f"`plan.sh release-dep {meta.get('id')} --mission {slug}` で解除")
                else:
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

    sweep_stale_target_records(_alive_workers)

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

        # Idle = no assignment file, and not waiting on the Director.
        #
        # t001 (#13): `plan.sh needs-director` now retires the assignment (so a
        # finished Kai-codex run no longer blocks every later codex-review).  A
        # Worker parked on a needs_director card is still not idle — before the
        # change its assignment file said so; now its card does.  Reading it as
        # idle would hand it a new task while the Director is deciding what to
        # do with the old one.
        assignment_file = ASSIGNMENTS_DIR / agent_name
        waits_on_director = worker_waits_on_director(agent_name, all_tasks)
        is_idle = not assignment_file.exists() and not waits_on_director

        # Rule 5 (herdr only): check agent state for blocked / idle-with-task.
        # Runs for ALL workers (busy and idle) before the is_idle gate below.
        check_rule5(agent_name, target, assignment_file, task_statuses_by_mission,
                    waits_on_director=waits_on_director)

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

        # #22: 割り当てメッセージを送ったばかりで、まだ pull されていない task がある
        # Worker には、別の task を重ねて送らない (assignment ファイルはまだ無いので
        # is_idle は真のまま)。理由と TTL は worker_outstanding_assignment() を参照。
        outstanding = worker_outstanding_assignment(agent_name, all_tasks)
        if outstanding:
            _okey = f"outstanding_log_{agent_name}_{outstanding[0]}_{outstanding[1]}"
            if should_notify(_okey):
                log(f"[assign] {agent_name}: task {outstanding[1]} (mission={outstanding[0]}) "
                    f"を送信済みで pull 待ち — 別の task は送らない")
                record_notify(_okey)
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
                # #21: Worker が起動された TARGET_DIR と task の target_dir が合わない
                # task は回さない (別 repo 用の Worker に crewvia 本体の task が回り、
                # 差し戻しても同じ Worker に再割り当てされた)。記録が読めない Worker には
                # target_dir 付きの task を回さない (保留)。判定は lib_worker_target 1 つ。
                may_take, why_not = worker_may_take_task(agent_name, meta)
                if not may_take:
                    _tkey = f"target_skip_{agent_name}_{slug}_{meta['id']}"
                    if should_notify(_tkey):
                        log(f"[target] {agent_name}: task {meta['id']} (mission={slug}) を割り当てない — {why_not}")
                        record_notify(_tkey)
                    continue
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
                    f"plan pull --task {task_id} --mission {slug}{pull_agent_flag(agent_name)} で取得後、"
                    f"作業→plan done で完了。"
                    f"pull の JSON の execution_id を、完了・失敗・差し戻しの報告の "
                    f"--execution に渡すこと (Bash は呼び出しごとに env が消えるので、報告のたびに --execution ex-… を付ける)。"
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
            # Also keep the worker alive if it still holds a card — in_progress,
            # needs_director, needs_human_review, blocked, verifying, ... (t001:
            # anything the Worker has not released; see worker_holds_work()).
            # Originally this only looked at in_progress, as defense in depth for
            # the window between plan.sh pull's save_task (task→in_progress) and
            # the assignment write.  needs-director now removes the assignment
            # too, so a Worker awaiting the Director's decision would otherwise
            # read as "no work" and be retired (memory:
            # assignment-removal-triggers-rule2-kill).
            has_in_progress = worker_holds_work(agent_name, all_tasks)
            # #21: skill は合うが TARGET_DIR が合わない task しか残っていない Worker。
            # そういう task は、この Worker が (blocked が解けても) 永久に取れない。
            # 待つ理由が無いので、no-task と同じく退役させる (C3 / t009)。旧実装は
            # 「退役させず待機 (Director に起動要求済み)」だったが、(a) 取れない task を待つ
            # Worker が mission 完了後も残り続け、(b) blocked な task には起動要求が
            # 実際には出ていなかった (起動要求は unblocked_pending だけを走査する)。
            # 自分の TARGET_DIR の task を待つ Worker は takeable_pending が空でないので、
            # この分岐に来ず Rule 2 のまま (#21 が守ろうとした挙動は変わらない)。
            takeable_pending = [(sl, m) for sl, m in matching_pending
                                if worker_may_take_task(agent_name, m)[0]]
            # 例外: Worker の TARGET_DIR の記録が「無い / 読めない」ときは、target_dir 付きの
            # task を取れないのは記録が無いからで、task が合わないと確定していない
            # (PR3 より前に起動した Worker は自分の task を待っているだけかもしれない)。
            # 観測できなかったものを根拠に破壊 (退役) しない — 従来どおり待機に倒す。
            _rec_known = not is_unreadable(worker_target_record(agent_name))
            if has_any and not takeable_pending and not has_in_progress and not _rec_known:
                _skey = f"target_only_{agent_name}"
                if should_notify(_skey):
                    log(f"[target] {agent_name}: 残っている task は TARGET_DIR が合わないものだけだが、"
                        f"TARGET_DIR の記録が無い/読めないため合わないと確定できない — 退役させず待機")
                    record_notify(_skey)
            elif not takeable_pending and not has_in_progress:
                # 残っている task が無い (not has_any) か、あっても TARGET_DIR が合わず
                # この Worker には永久に取れない (C3 / t009): どちらも「もう要らない」。
                if in_spawn_grace(target):
                    log(f"[spawn_grace] {agent_name}: within {SPAWN_GRACE_SECONDS}s spawn grace — skip shutdown")
                else:
                    notify_key = f"shutdown_{agent_name}"
                    if should_notify(notify_key):
                        if has_any:
                            log(f"[target] {agent_name}: 残っている task は TARGET_DIR が合わないものだけ "
                                f"(この Worker は永久に取れない) — 退役を依頼 (no-task)")
                        if retire_worker(agent_name, target, 'no-task'):
                            record_notify(notify_key)
            elif has_any and not has_in_progress:
                # Rule 2: all matching tasks are blocked (C3: "matching" = the ones this
                # Worker can actually take — a TARGET_DIR-mismatched task's blocker chain
                # says nothing about whether *this* Worker is stuck).  If the most recently
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
                    for s, m in takeable_pending
                    for dep in (m.get('blocked_by') or [])
                )
                if has_active_blocker:
                    log(
                        f"[Rule 2 skip] {agent_name}: blocker task is in_progress "
                        f"→ chain progressing, worker kept alive"
                    )
                else:
                    newest_mtime = max(_chain_newest_mtime(s, m) for s, m in takeable_pending)
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
        # no-op when Kai-codex may still be in flight (assignment points at a
        # live run — an orphan assignment does not count)
        # or when pr_number is missing (warning logged, Director escalates).
        if task_skills & CODEX_REVIEW_SKILLS:
            handle_codex_review(slug, meta, live_state_keys, task_statuses_by_mission)
            continue
        # can_handle: True if any alive worker (window exists OR heartbeat recent) has skills ⊇ task_skills
        # #21: skill に加えて TARGET_DIR の記録も合う Worker だけが「担当できる」。
        skill_ok = [
            name for name in _alive_workers
            if (workers.get(name) or {}).get('role', 'worker') == 'worker'
            and task_skills.issubset(set((workers.get(name) or {}).get('skills') or []))
        ]
        can_handle = any(worker_may_take_task(name, meta)[0] for name in skill_ok)
        if not can_handle:
            # Include slug to avoid collision when missions reuse t001, t002, etc.
            notify_key = f"no_worker_{slug}_{task_id}"
            if should_notify(notify_key):
                msg = (
                    f"要求スキル {sorted(task_skills)} の Worker を起動してください "
                    f"(task {task_id}, mission={slug})"
                )
                if skill_ok:
                    # skill は合う Worker が居るのに担当できない = TARGET_DIR が合わない。
                    msg += f" — 既存の Worker は担当できません: {worker_may_take_task(skill_ok[0], meta)[1]}"
                msg += (
                    f"。起動コマンド: "
                    + worker_start_command(task_skills, meta.get('target_dir'), fresh=bool(skill_ok))
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
    for slug, meta in all_tasks:
        if meta.get('status') not in WAITS_ON_DIRECTOR_STATUSES:
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
                             director_live=director_live_for_state_notices):
            log(f"[needs_director] {slug}/{task_id}: notified director (reason: {reason_line[:80]!r})")


    # Handoff detection: failed tasks with handoff_path → notify Director
    # t010 (#10): needs_director と同じく、状態が変わるまで 1 回だけ (台帳)。
    # 入力は status + handoff_path。以前は failed かつ handoff_path がある間ずっと
    # TTL ごとに再送された。
    # t023 (Kai 2 巡目 P2): **ここは all_tasks を使う** (ミッションを走査し直さない)。
    # 末尾の prune_told() は「observed_missions(all_tasks) で観測できた mission の、live に
    # 無い key を捨てる」ので、live key を集める側も同じスナップショットでなければならない。
    # 別の走査で live key を集めると、1 回目の走査 (all_tasks) は成功・2 回目が破損カード /
    # 走査失敗のとき、handoff key が 1 件も集まらないのに mission は「観測できた」扱いになり、
    # 台帳とスロットルを捨てる。読み取りが回復すると同じ failed task が再通知され、
    # 断続的な失敗のたびに通知洪水が戻る (「観測できなかった」を「もう無い」と読む型)。
    # 採らなかった案 (b) 「2 回目の失敗を pruning のガードに足す」: 走査を 2 回するせいで
    # 起きる欠陥を、2 回目の結果を見張る別のガードで塞ぐことになる。ガードは持ち場が増える
    # ぶん漏れる (t018)。スナップショットを 1 つにすれば、判断の材料が 1 つなので食い違えない
    # (needs_director / vanished 検知はすでに all_tasks を使っている)。走査も 1 回減る。
    for slug, meta in all_tasks:
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
                             director_live=director_live_for_state_notices):
            log(f"handoff detected: {slug}/{task_id} -> notified director")

    # PR-B: 判断待ちが続いたら 5 分で Director に再通知・10 分で Telegram (設計 §5)。all_tasks を使い回す。
    run_escalation_cycle(all_tasks, done_ids_by_mission, task_statuses_by_mission, active_missions)

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


# ---------------------------------------------------------------------------
# Main-checkout drift detection (t005 / B2 / #26)
# ---------------------------------------------------------------------------
#
# merge のたびに Director が手で「主 checkout を ff → 変わったファイルに応じて
# dispatcher / watchdog を restart」してきた (前 mission で 9 回)。忘れると
# merge 済みの修正が本番で動かず、誤診断の元になる
# (knowledge/dispatcher-restart-after-merge.md)。ここでは検知して Director に
# 1 回だけ知らせるところまでを行う — restart するかどうかは
# scripts/sync-main-checkout.sh を人が実行する (デーモンが自分で自分を
# restart したり、Worker が主 checkout を触ったりはしない、という mission の方針)。

#: 数分に 1 回でよい (`git fetch` は 5 秒ごとのサイクルでは重すぎる)。housekeeping の
#: 周期であって dispatcher と plan.sh が答えを揃えなければならない共有規則ではないので
#: env での調整を許す (memory: no-env-killswitch-for-shared-rule はそこが違う)。
DEFAULT_MAIN_CHECKOUT_DRIFT_INTERVAL = 180.0


def _parse_drift_interval(raw):
    """`CREWVIA_MAIN_CHECKOUT_DRIFT_INTERVAL` を検証する (t114 / PR#246 Codex 2巡目 P2-2)。

    このモジュールは `run_dispatch()` が毎サイクル python プロセスを丸ごと作り直して
    実行するので、この行はここに `float()` を素で置くだけで **module import 時 = 毎
    サイクル** 評価される。空・非数・`nan`・`inf`・負の値が入ると
    `check_main_checkout_drift()` の try/except より手前 (import 中) で例外が飛び、
    以降の `publish_agents()` / `dispatch()` まで含めてサイクル全体が毎回黙って
    死ぬ (bash の while ループ自体は健全な heartbeat を出し続けるので気付けない)。
    不正な値は既定値に戻し、警告だけ出して dispatch は止めない。
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not is_finite_number(value) or value < 0:
        log(f"WARNING: invalid CREWVIA_MAIN_CHECKOUT_DRIFT_INTERVAL={raw!r} "
            f"(finite・非負の秒数である必要があります) — "
            f"既定値 {DEFAULT_MAIN_CHECKOUT_DRIFT_INTERVAL} を使います")
        return DEFAULT_MAIN_CHECKOUT_DRIFT_INTERVAL
    return value


MAIN_CHECKOUT_DRIFT_INTERVAL = _parse_drift_interval(
    os.environ.get('CREWVIA_MAIN_CHECKOUT_DRIFT_INTERVAL', str(DEFAULT_MAIN_CHECKOUT_DRIFT_INTERVAL)))
DRIFT_CHECK_STATE = REGISTRY_DIR / 'daemons' / 'main-checkout-drift-check.json'


def drift_check_state_problem(data):
    if not is_finite_number(data.get('last_checked_at')):
        return f"'last_checked_at' is {data.get('last_checked_at')!r}, expected a finite timestamp"
    return None


def _drift_check_due(now):
    state = load_json_store(DRIFT_CHECK_STATE, check=drift_check_state_problem)
    if is_missing(state) or is_unreadable(state):
        # 読めない/まだ無い場合は「調べる」側に倒す — git fetch が少し早く走る
        # だけで、逆に倒すと壊れた台帳が検知そのものを恒久的に止めてしまう。
        return True
    return (now - state['last_checked_at']) >= MAIN_CHECKOUT_DRIFT_INTERVAL


def _mark_drift_checked(now):
    path = DRIFT_CHECK_STATE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps({'last_checked_at': now}))
        os.replace(tmp, path)
    except OSError as e:
        log(f"WARNING: cannot write {path}: {e}")


def check_main_checkout_drift():
    """主 checkout の版ずれを検知し、Director に 1 回だけ知らせる。

    2 つの独立した検知:
    (a) origin/main が主 checkout の HEAD より進んでいる (merge 済みの fix が
        まだ pull されていない)
    (b) dispatcher / watchdog が起動時に記録した版と、ディスク上の対象ファイルが
        一致しない (pull 済みだがそのデーモンが未 restart)

    どちらも「観測できない」(git fetch 失敗・版の記録が読めない) ときは通知しない
    — 誤報より沈黙の方が安く、確かめたい人は sync-main-checkout.sh --dry-run を
    いつでも自分で実行できる。全体を try/except で包むのは run_daemon_watch() と
    同じ理由: 安全網が dispatch サイクルを落としてはならない。
    """
    try:
        import lib_daemon_watch
        now = time.time()
        if not _drift_check_due(now):
            return
        _mark_drift_checked(now)

        origin_ahead = None
        if lib_daemon_watch.fetch_origin(REPO_ROOT):
            origin_ahead = lib_daemon_watch.commits_behind(REPO_ROOT)

        restart_flags = {
            name: lib_daemon_watch.restart_needed(REGISTRY_DIR, REPO_ROOT, name)
            for name in lib_daemon_watch.DAEMONS
        }
        drifted_daemons = sorted(n for n, v in restart_flags.items() if v)
        has_origin_drift = bool(origin_ahead)  # 検知の方向は None・0 とも False 扱いでよい
        # (誤報より沈黙が安い、上のdocstring参照)。

        # 台帳を畳んでよいのは「きれいだと確認できた」ときだけ (t114 / PR#246 Codex
        # 2巡目 P2-1)。origin_ahead=None (fetch 失敗) や restart_flags[name]=None
        # (版の記録が読めない) は「不明」であって「0」でも「ずれ無し」でもない。
        # 直す前は has_origin_drift/drifted_daemons が両方 False になった瞬間に
        # 台帳を消していたので、一時的な fetch 失敗が「解消した」と誤読され、
        # origin が実際は進んだままの状態で次に fetch が成功すると同じ drift の
        # fingerprint が「初めて」に見えて 2 回目の通知が出てしまう。
        all_confirmed_clean = (
            origin_ahead == 0
            and all(v is False for v in restart_flags.values())
        )

        if not has_origin_drift and not drifted_daemons:
            if all_confirmed_clean:
                # きれいな状態だと確認できた。前回の drift 通知が台帳に残っていれば
                # 畳んでおく — そうしないと、次にまったく同じ fingerprint の drift が
                # 起きたとき (滅多に無いが、同じ commit 数・同じ daemon の組の再発)
                # already_told に遮られて再通知されない。
                clear_told_key('main-checkout-drift')
            else:
                # 観測のどれかが不明 (fetch 失敗・版の記録が読めない)。過去に通知
                # 済みの記録があるなら、それを「解消した」と誤読して消してはいけない
                # — 消さずに保つのが安全側で、次に観測できたときにまた自然に畳まれる。
                log('[main-checkout-drift] some observations unknown '
                    f'(origin_ahead={origin_ahead!r} restart_flags={restart_flags!r}) — '
                    'leaving any existing notification record untouched')
            return

        fp = fingerprint(origin_ahead, drifted_daemons)

        def build_msg():
            lines = ['[main-checkout-drift] 主 checkout の同期が必要です。']
            if has_origin_drift:
                lines.append(f'- origin/main が {origin_ahead} commit 進んでいます (未 pull)。')
            if drifted_daemons:
                lines.append('- 稼働中で版がずれているデーモン: ' + ', '.join(drifted_daemons))
            for name, v in restart_flags.items():
                if v is None:
                    lines.append(
                        f'  ({name} は版の記録が無く比較できません — 次に restart '
                        f'されたときから記録されます)'
                    )
            lines.append(
                'scripts/sync-main-checkout.sh を実行してください '
                '(--dry-run で内容だけ先に確認できます)。'
            )
            return '\n'.join(lines)

        if notify_state_once('main-checkout-drift', fp, 'main-checkout-drift',
                             '_daemon', 'main-checkout', build_msg,
                             director_live=director_live_for_state_notices):
            log(f'main-checkout-drift detected: origin_ahead={origin_ahead} '
                f'restart_flags={restart_flags} -> notified director')
    except Exception as e:
        log(f'[main-checkout-drift] check failed: {e!r}')


# ---------------------------------------------------------------------------
# 段階上げ (PR-B / knowledge/director-escalation-telegram.md §5)
# ---------------------------------------------------------------------------
#
# 判断待ち (AWAITING_DECISION_STATUSES) が続いたら、5 分で Director に再通知・10 分で Telegram。
# 判定は lib_escalation.decide() (純粋関数・網羅テスト)。ここは入力を集めて送るだけ。
# 全体を try/except で包むのは run_telegram_cycle() と同じ理由: 安全網が dispatch サイクルを落としてはならない。

_TG_RUNTIME = {'creds': None}      # run_telegram_cycle() が今サイクルの認証情報を置く (env には出さない)


def _escalation_warn(msg):
    if should_notify('escalation_warn'):
        log(msg)
        record_notify('escalation_warn')


def _escalation_trouble(msg):
    """台帳に書けない (通知を見送った) ことを Director に知らせる。台帳に依存しない: 間引きは
    プロセスをまたぐ notify cache (should_notify。NOTIFY_TTL に 1 回) で、台帳とは別のファイル。
    Director 不在・送信失敗は log だけ (記録しないので戻ったら次の TTL 明けに届く)。"""
    _escalation_warn('WARNING: ' + msg)
    key = 'escalation_ledger_unwritable'
    if should_notify(key) and director_live_for_state_notices() and tmux_send(_director_name(), '[escalation] ' + msg):
        record_notify(key)


def run_escalation_cycle(all_tasks, done_ids_by_mission, task_statuses_by_mission, active_missions):
    try:
        import lib_escalation
        import lib_telegram
        now = time.time()
        cfg = lib_escalation.load_cfg(REPO_ROOT / 'config' / 'crewvia.yaml', warn=_escalation_warn)
        creds = _TG_RUNTIME['creds']
        registry_dir = REGISTRY_DIR / 'daemons'
        tg_config = lib_telegram.load_telegram_config()
        session_link = (os.environ.get('CREWVIA_DIRECTOR_SESSION_URL') or tg_config.get('session_link') or '').strip()

        def send_telegram(text):
            res = lib_telegram.send_message(registry_dir, creds, text)
            if not res.ok and res.warn:
                log(f'WARNING: telegram unreachable ({res.error})')
            return res.ok

        def execution_id_of(meta):
            ex = meta.get('current_execution_id')
            if isinstance(ex, str) and ex:
                return ex
            return 'fp:' + fingerprint('needs_director', (meta.get('needs_director_reason') or '').strip())

        lib_escalation.run_cycle(
            REGISTRY_DIR, all_tasks, done_ids_by_mission, task_statuses_by_mission, set(active_missions),
            observed_missions(all_tasks, active_missions), now, cfg,
            director_live=director_live_for_state_notices,
            telegram_available=lambda: creds is not None and lib_telegram.telegram_available(registry_dir, now, identity=lib_telegram.identity_of(creds)),
            send_director=lambda text: tmux_send(_director_name(), text),
            send_telegram=send_telegram,
            execution_id_of=execution_id_of, session_link=session_link, log=_escalation_warn,
            trouble=_escalation_trouble)
    except Exception as e:
        log(f'[escalation] cycle failed: {type(e).__name__}')


# ---------------------------------------------------------------------------
# Telegram の経路 (PR-A / knowledge/director-escalation-telegram.md §1・§1-1・§3)
# ---------------------------------------------------------------------------
#
# 1 サイクルに足すのは `lib_telegram.run_cycle()` の 1 呼び出しだけ (ロジックは lib 側・純粋関数 + 網羅テスト)。
# ネットワークは `poll` のサブプロセス (timeout 8) の中だけ — サイクルを塞がない。未設定で台帳が無ければ
# 何も読まず何も書かない。受信側の心拍 (telegram-receiver.json) の書き手はここだけ。
# 全体を try/except で包むのは run_daemon_watch() と同じ理由: 安全網が dispatch サイクルを落としてはならない。

TELEGRAM_DISABLED_KEY = 'telegram_receiver_disabled'


def run_telegram_cycle():
    try:
        import lib_telegram
        config = lib_telegram.load_telegram_config()
        creds, reason = lib_telegram.resolve_credentials(
            config, environ={'_CREWVIA_TG_RESOLVED_TOKEN': _TG_CARRIED['_CREWVIA_TG_RESOLVED_TOKEN'],
                             '_CREWVIA_TG_RESOLVED_CHAT_ID': _TG_CARRIED['_CREWVIA_TG_RESOLVED_CHAT_ID']},
            carried=True)
        _TG_RUNTIME['creds'] = creds
        if creds is None and reason == 'no_credentials' and _TG_RESOLVE_REASON != 'ok':
            reason = _TG_RESOLVE_REASON      # 起動時に取り出せなかった理由 (固定コード) を心拍に残す

        def forward(line):
            # Director 不在・送信失敗は False → forwarded=false のまま次のサイクルで再送 (§2-3)
            if not director_live_for_state_notices():
                return False
            return tmux_send(_director_name(), line)

        summary = lib_telegram.run_cycle(
            REGISTRY_DIR / 'daemons', QUEUE_DIR, config, reason, forward, creds=creds, log=log)
        receiver = summary.get('receiver') or {}
        if receiver.get('file_exists') and receiver.get('enabled') is False:
            # 「受信が無効になった」を Director に 1 回 (状態ベース。同じ理由コードの間は繰り返さない)
            rsn = receiver.get('reason', 'unknown')
            notify_state_once(
                TELEGRAM_DISABLED_KEY, fingerprint(rsn), 'telegram-receiver', '_daemon', 'telegram',
                lambda: ('[telegram] Telegram 受信が無効です (理由コード: ' + str(rsn) + ')。'
                         'dispatcher が起動時に認証情報を取り出せなかった可能性 (1Password のロック・op が PATH に無い・'
                         'ファイルの権限)。原因を直して lib_daemon_watch.py restart を行うか、config の '
                         'telegram.credentials を見直してください。この間 ask_user.sh ask は断られ (exit 4)、'
                         'AskUserQuestion を使ってください。'),
                director_live=director_live_for_state_notices)
        elif receiver.get('enabled') is True:
            told = load_told()
            if not is_unreadable(told) and TELEGRAM_DISABLED_KEY in told:
                clear_told_key(TELEGRAM_DISABLED_KEY)
        changed = summary.get('identity_changed') or []
        if changed:
            # 認証情報 (bot / chat) が変わり、前の bot で出した未回答の質問を取り下げた。ボタンは別の bot では消せない。
            notify_state_once(
                'telegram_identity_changed', fingerprint(sorted(changed)), 'telegram-identity', '_daemon', 'telegram',
                lambda: ('[telegram] 認証情報 (bot / chat) が変わったため、前の bot で出した未回答の質問 ' + str(len(changed)) +
                         ' 件 (' + ', '.join(sorted(changed)) + ') を取り下げました (別の bot ではボタンを消せません。'
                         '押されても転送されません)。必要なら質問し直してください。'),
                director_live=director_live_for_state_notices)
        poll = summary.get('poll') or {}
        # 回復 (読めた / 書けた) を観測したら一度だけ通知の台帳を畳む。`_daemon` slug は prune_told の対象外なので、畳まないと
        # 同じ障害の 2 回目が永久に黙る。観測していない poll (台帳が使えない・質問なしで offset を読まなかった) では畳まない。
        for ok_flag, told_key in (('offset_ok', 'telegram_offset_unreadable'), ('offset_write_ok', 'telegram_offset_unwritable'),
                                  ('offset_write_ok', 'telegram_offset_fallback_unwritable'), ('offset_fallback_ok', 'telegram_offset_fallback_unwritable')):
            if poll.get(ok_flag):
                told = load_told()
                if not is_unreadable(told) and told_key in told:
                    clear_told_key(told_key)
        if poll.get('offset_fallback_unwritable'):
            # 本体にも退避先にも書けない。退避先で継続できていない: 進捗も間引きも残らず、毎サイクル同じ update を読み直す。
            notify_state_once(
                'telegram_offset_fallback_unwritable', fingerprint(['fallback-unwritable']), 'telegram-offset', '_daemon', 'telegram',
                lambda: ('[telegram] offset を保存できません: registry/daemons/telegram-offset.json にも退避先 '
                         'telegram-offset-fallback.json にも書けません。同じ update を読み直し続け、間引き (poll_interval) も効きません。'
                         'どちらのパスも消して (消してよい。復旧手順)、権限・ディレクトリ化を直してください。'),
                director_live=director_live_for_state_notices)
        elif poll.get('offset_unwritable'):
            # offset の本体が書けない (パスがディレクトリ等)。退避先 (telegram-offset-fallback.json) で二重処理と間引きは保っている。
            notify_state_once(
                'telegram_offset_unwritable', fingerprint(['unwritable']), 'telegram-offset', '_daemon', 'telegram',
                lambda: ('[telegram] registry/daemons/telegram-offset.json に書けません (ディレクトリ・権限など)。'
                         '退避先 telegram-offset-fallback.json で受信は続けていますが、直らないと毎サイクル同じ状態です。'
                         'そのパスを消して (消してよい。復旧手順) ください。'),
                director_live=director_live_for_state_notices)
        elif poll.get('offset_unreadable'):
            # offset ファイルが読めない (壊れ / 型違い / 権限)。0 から読み直して受信は続く (重複は台帳が止める)。次の書き込みで直る。
            # 直らない (書き込みも失敗する) 場合に黙らないよう、同じ理由は 1 回だけ Director に知らせる。
            notify_state_once(
                'telegram_offset_unreadable', fingerprint([str(poll['offset_unreadable'])]), 'telegram-offset', '_daemon', 'telegram',
                lambda: ('[telegram] telegram-offset.json が読めません (' + str(poll['offset_unreadable']) + ')。0 から読み直して受信は続けています。'
                         '直らなければ registry/daemons/telegram-offset.json を消してください (消してよい。復旧手順)。'),
                director_live=director_live_for_state_notices)
        if poll.get('error') and should_notify('telegram_poll_warn'):
            log(f"WARNING: telegram poll failed ({poll.get('error')})")
            record_notify('telegram_poll_warn')
    except Exception as e:
        log(f'[telegram] cycle failed: {type(e).__name__}')


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
check_main_checkout_drift()
# Telegram は dispatch() の成否から独立させる (心拍 = `ask` が見る受信側の生存表明。dispatch が毎回落ちても止めない)
run_telegram_cycle()
publish_agents()
dispatch()
# t002: both are queue/registry bookkeeping, not dispatch decisions, and both
# must run on every cycle — including the early-return cycles dispatch() takes
# when there are no active missions.
sweep_spawn_grace_markers()
sweep_stale_pane_records()
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
  _tg_maybe_retry
  run_dispatch || log "dispatch cycle error (exit $?)"
  sleep 5
done
