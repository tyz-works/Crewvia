#!/usr/bin/env python3
"""
scripts/watchdog.py — Crewvia Worker Watchdog v2

Monitors active Workers via three signal layers:
  - Tool layer   : registry/activity/<agent>/<task_id>.activity
  - Thought layer: registry/heartbeats/<agent>, registry/notifications/<agent>/
  - Process layer: mux pane_pid → /proc プロセス木の分類

Multi-level judgment per WorkerMonitor:
  alive     → no action
  warn      → POST /api/log type=alert (soft idle threshold)
  terminate → graceful shutdown (hard idle threshold or absolute max)
  kill      → cleanup only (tmux session already gone)

判定材料の組み合わせ方 (t016):
  idle 秒数 (Tool + Thought 層の最新 mtime) が唯一の「働いていない」の根拠で、
  **常に評価される**。プロセス層は terminate を *抑制する方向にだけ* 効く。
  子プロセスの存在は「生存」の証拠にはならない — claude 本体も MCP サーバーも
  ハング中・入力待ち・承認待ちのあいだ生き続けるからである。詳細は
  knowledge/daemon-authority.md と classify_process_tree() の docstring 参照。

Usage:
  python3 scripts/watchdog.py [--interval <s>] [--repo-root <path>]
  python3 scripts/watchdog.py --version
  python3 scripts/watchdog.py --help
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Literal, NamedTuple, Optional

# Import lib_mux — assumes watchdog.py lives in scripts/ alongside lib_mux.py
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))
from lib_mux import Mux, repo_identity_ok  # noqa: E402
_mux = Mux()

__version__ = "2.0.0"

# ---------------------------------------------------------------------------
# Worker profiles — defaults when task frontmatter has no timeout field
# ---------------------------------------------------------------------------

PROFILES: dict[str, dict[str, int]] = {
    "feature_impl": {"idle": 300, "max": 3600},   # default: 5 min idle, 1 hr max
    "research":     {"idle": 600, "max": 7200},   # 10 min idle, 2 hr max
    "quick":        {"idle": 120, "max":  600},   # 2 min idle, 10 min max
}
DEFAULT_PROFILE = "feature_impl"

TERMINATE_GRACE_PERIOD = 60   # seconds to wait after sending graceful shutdown message
KILL_DELAY = 10               # seconds after SIGTERM before SIGKILL
DEFAULT_CHECK_INTERVAL = 30   # main loop interval in seconds
MASS_KILL_ALERT_BACKOFF_SECONDS = 300  # t020: min gap between mass-kill Taskvia alerts

# t016: プロセス層の分類しきい値。ペインのセッション (claude) 起動からこれ以上
# 遅れて始まった子孫が居れば「Bash tool が実行中」とみなす。
#
# 下限の根拠: MCP サーバーは claude 起動の 1-2 秒後に立ち上がる (本番実測
# 2026-09-21: claude et=119s に対し MCP 2 本が et=117s)。これを「実行中」と
# 誤読すると idle 判定が永久に抑止され、今回直した欠陥がそのまま再発する。
# 60 秒は実測値の 30 倍で、MCP の起動が多少遅れても誤読しない余裕がある。
# 上限側は緩くて構わない — 誤読の向きが「殺さない」だからである。
PROCESS_WORK_START_GRACE = 60

# 同じ判定が続く間、何 cycle ごとに要約を 1 行残すか (30s * 10 = 5 分)。
VERDICT_SUMMARY_EVERY = 10


# ---------------------------------------------------------------------------
# Minimal YAML / frontmatter parser (no external deps)
# ---------------------------------------------------------------------------

def _scalar(val: str):
    if val in ("null", "~"):
        return None
    if val in ("true", "True"):
        return True
    if val in ("false", "False"):
        return False
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return val[1:-1].replace('\\"', '"')
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    return val


def parse_yaml(text: str) -> dict:
    lines = text.splitlines()
    result: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([\w-]+):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).rstrip()
        if val == "":
            i += 1
            items: list = []
            sub: dict = {}
            while i < len(lines):
                lst = re.match(r"^\s+-\s*(.*)$", lines[i])
                if lst:
                    items.append(_scalar(lst.group(1).strip()))
                    i += 1
                else:
                    mm = re.match(r"^  ([\w-]+):\s*(.*)$", lines[i])
                    if mm:
                        sub[mm.group(1)] = _scalar(mm.group(2).rstrip())
                        i += 1
                    else:
                        break
            result[key] = items if items else (sub if sub else None)
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            result[key] = [_scalar(s.strip()) for s in inner.split(",")] if inner else []
            i += 1
        else:
            result[key] = _scalar(val)
            i += 1
    return result


def parse_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
    if end < 0:
        return {}, text
    meta = parse_yaml("\n".join(lines[1:end]))
    body = "\n".join(lines[end + 1:])
    return meta, body


# ---------------------------------------------------------------------------
# Process layer (t016)
# ---------------------------------------------------------------------------

ProcessSignal = Literal[
    "no_window",     # mux 窓が無い
    "not_probed",    # プロセス層を見るまでもなく判定が決まった (絶対上限など)
    "unknown",       # 窓はあるが pane pid が引けない → terminate を抑制する
    "no_process",    # 子プロセスが 1 つも無い
    "idle_process",  # claude と MCP サーバーだけ = 木が有るだけ
    "executing",     # セッション起動より十分後に始まった子孫が居る = tool 実行中
]


def _proc_stat(pid: int) -> Optional[tuple[int, int]]:
    """/proc/<pid>/stat から (ppid, starttime_ticks) を返す。読めなければ None。

    comm (field 2) は括弧で囲まれ、空白や ')' を含みうるので最後の ')' で
    切ってから split する (例: "1234 (sh -c (x)) S 1 ..." )。
    切った残りの先頭が field 3 なので、field N は rest[N - 3] になる:
      ppid = field 4 = rest[1] / starttime = field 22 = rest[19]
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    rest = raw[close + 1:].split()
    if len(rest) < 20:
        return None
    try:
        return int(rest[1]), int(rest[19])
    except ValueError:
        return None


def classify_process_tree(
    root_pid: int, grace_seconds: int = PROCESS_WORK_START_GRACE
) -> ProcessSignal:
    """mux ペインのプロセス木を 3 値に分類する。

    本番のペインは常にこの形をしている (2026-09-21 実測, Ren-worker):

        /bin/bash                      ← root_pid (pane_pid)
          claude --model ...           ← セッション。Worker が生きている限り常駐
            npm exec @playwright/mcp   ← MCP サーバー。claude の 1-2 秒後に起動
            npm exec chrome-devtools   ← 同上
            /bin/bash -c source ...    ← Bash tool の実行中だけ現れる

    旧実装の `pgrep -P <pane_pid>` は常に claude 1 件を返すため「子プロセスが
    居る = 作業中」が恒真になり、idle 判定に一度も到達しなかった。ここでは
    **いつ生えたか** で区別する:

      "executing"    … セッション起動から grace_seconds より後に始まった子孫が
                       居る = Bash tool が今まさに走っている。長時間のビルド /
                       学習 / CI 待ちで activity が stale になる正当なケース
      "idle_process" … claude と MCP サーバーだけ。木が有ること自体は
                       「働いている」の証拠にならない
      "no_process"   … 子が 1 つも無い (claude が落ちた / 素のシェル)

    基準時刻は **最も古い直下の子 (= claude) の起動時刻** にする。root 自身では
    なく子を基準にするのは、ペインの bash が Worker より先に (crewvia 起動時に)
    生まれていることがあり、それを基準にすると claude の起動自体が "executing"
    に見えてしまうからである。深さは問わないので、ペイン直下に後から生えた
    プロセスも拾える。

    起動時刻は /proc の starttime (boot からの tick) 同士で比較する。壁時計に
    依存しないので、NTP 補正やサスペンドの影響を受けない。
    """
    if _proc_stat(root_pid) is None:
        return "no_process"

    procs: dict[int, tuple[int, int]] = {}
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError:
        return "unknown"
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        st = _proc_stat(int(entry.name))
        if st is not None:
            procs[int(entry.name)] = st

    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in procs.items():
        children.setdefault(ppid, []).append(pid)

    direct = children.get(root_pid, [])
    if not direct:
        return "no_process"

    ticks_per_sec = os.sysconf("SC_CLK_TCK") or 100
    threshold_ticks = grace_seconds * ticks_per_sec
    session_start = min(procs[pid][1] for pid in direct)

    # root 配下を幅優先で走査 (root 自身は含めない)
    stack = list(direct)
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if procs[pid][1] - session_start > threshold_ticks:
            return "executing"
        stack.extend(children.get(pid, []))

    return "idle_process"


class CheckResult(NamedTuple):
    """check() の判定と、その根拠。ログ・観測ログはこれをそのまま書く。"""
    verdict: Literal["alive", "warn", "terminate", "kill"]
    reason: str
    idle_seconds: float
    process_signal: ProcessSignal
    awaiting_human: bool


# ---------------------------------------------------------------------------
# WorkerMonitor
# ---------------------------------------------------------------------------

class WorkerMonitor:
    """Monitors a single in-progress task / Worker."""

    def __init__(self, task_id: str, task_card: dict, profiles: dict[str, dict[str, int]],
                 repo_root: Path) -> None:
        timeout = task_card.get("timeout") or {}
        profile_name = task_card.get("worker_profile") or DEFAULT_PROFILE
        base = profiles.get(profile_name) or profiles[DEFAULT_PROFILE]

        self.task_id = task_id
        self.agent_name: str = str(task_card.get("worker") or os.environ.get("AGENT_NAME", "unknown"))
        self.idle_threshold: int = int(timeout.get("idle") or base["idle"])
        self.max_threshold: int = int(timeout.get("max") or base["max"])
        self.started_at: float = time.time()
        self.repo_root = repo_root

    # ------------------------------------------------------------------
    # Signal detection helpers
    # ------------------------------------------------------------------

    def _last_activity_mtime(self) -> float:
        """Return mtime of most recent activity signal across all layers."""
        candidates: list[float] = []

        # Tool layer: activity file
        activity_file = (
            self.repo_root / "registry" / "activity" / self.agent_name
            / f"{self.task_id}.activity"
        )
        if activity_file.exists():
            candidates.append(activity_file.stat().st_mtime)

        # Thought layer: heartbeat file
        hb_file = self.repo_root / "registry" / "heartbeats" / self.agent_name
        if hb_file.exists():
            candidates.append(hb_file.stat().st_mtime)

        # Thought layer: notification files (most recent)
        notif_dir = self.repo_root / "registry" / "notifications" / self.agent_name
        if notif_dir.exists():
            for f in notif_dir.iterdir():
                if f.is_file():
                    try:
                        candidates.append(f.stat().st_mtime)
                    except OSError:
                        pass

        return max(candidates) if candidates else self.started_at

    def _non_notification_mtime(self) -> Optional[float]:
        """notification を除いた「実活動」の最新 mtime。

        _last_activity_mtime() は notification 自体を候補に含むため、通知の
        解除判定 (_awaiting_human) には使えない — 通知が来ただけで「解除済み」に
        見えてしまう。そのための別計算。
        """
        candidates: list[float] = []
        activity_file = (
            self.repo_root / "registry" / "activity" / self.agent_name
            / f"{self.task_id}.activity"
        )
        if activity_file.exists():
            candidates.append(activity_file.stat().st_mtime)
        hb_file = self.repo_root / "registry" / "heartbeats" / self.agent_name
        if hb_file.exists():
            candidates.append(hb_file.stat().st_mtime)
        return max(candidates) if candidates else None

    def _newest_notification(self) -> tuple[Optional[float], Optional[str]]:
        """直近の notification の (mtime, notification_type)。無ければ (None, None)。"""
        notif_dir = self.repo_root / "registry" / "notifications" / self.agent_name
        if not notif_dir.exists():
            return None, None
        newest_file = None
        newest_mtime = -1.0
        for f in notif_dir.iterdir():
            if not f.is_file():
                continue
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m > newest_mtime:
                newest_mtime = m
                newest_file = f
        if newest_file is None:
            return None, None
        try:
            payload = json.loads(newest_file.read_text())
            notif_type = payload.get("notification_type")
        except Exception:
            notif_type = "(unparseable)"
        return newest_mtime, notif_type

    def _observation_snapshot(self) -> dict:
        """★task_162 案C(観測専用). check() の判定には一切使わない — 呼び出し元は
        check() の戻り値やロジックを変更しない別経路のログ専用スナップショットである。

        無音区間の長さ(idle_seconds、_last_activity_mtime() をそのまま呼ぶだけで
        既存の計算方法を変えない)に加え、直近の notification(あれば)の種別・
        経過時間・「その後 activity/heartbeat の更新があったか(=解除されたか)」を
        記録する。「無音」と「人間待ちで無音」を区別するための情報(Picard指示)。
        """
        now = time.time()
        non_notification_mtime = self._non_notification_mtime()
        last_notif_mtime, last_notif_type = self._newest_notification()

        cleared_since_notification: Optional[bool] = None
        if last_notif_mtime is not None:
            cleared_since_notification = (
                non_notification_mtime is not None and non_notification_mtime > last_notif_mtime
            )

        idle_seconds = now - self._last_activity_mtime()

        return {
            "ts": round(now, 3),
            "agent": self.agent_name,
            "task_id": self.task_id,
            "idle_seconds": round(idle_seconds, 1),
            "idle_threshold": self.idle_threshold,
            "max_threshold": self.max_threshold,
            "last_notification_type": last_notif_type,
            "last_notification_age_seconds": (
                round(now - last_notif_mtime, 1) if last_notif_mtime is not None else None
            ),
            "cleared_since_notification": cleared_since_notification,
        }

    def _mux_window_name(self) -> Optional[str]:
        """Return the mux window name for this agent, or None if not found."""
        # Try '<agent>-worker' first, then bare agent name.
        for candidate in (f"{self.agent_name}-worker", self.agent_name):
            if candidate in _mux.list():
                return candidate
        return None

    # Keep old name as alias so graceful_terminate (which calls it) still works.
    def _tmux_window_target(self) -> Optional[str]:
        return self._mux_window_name()

    def _process_signal(self) -> ProcessSignal:
        """プロセス層のシグナル。**生死の判定ではない** — classify_process_tree() 参照。"""
        name = self._mux_window_name()
        if not name:
            return "no_window"
        pane_pid = _mux.pid(name)
        if pane_pid is None:
            # 窓はあるのに pane pid が引けない = mux backend の不調。実行中か
            # ハング中かを見分ける材料が無いので "unknown" とし、terminate を
            # 抑制する側に倒す (fail closed)。_is_mass_kill() と同じ向き。
            return "unknown"
        return classify_process_tree(pane_pid)

    def _awaiting_human(self) -> bool:
        """直近の Notification が未解除か = 人間の入力/承認待ちで無音か。

        Notification hook は承認待ち・入力待ちで発火するが **1 回しか鳴らない**。
        その後は activity も heartbeat も止まるため、無音の理由が「ハング」でも
        「人間待ち」でも idle_seconds は同じように伸びる。両者を区別できるのは
        「最後の通知より後に実活動があったか」だけである。

        通知より後に activity / heartbeat が動いていれば解除済み = 待ちではない。
        解除済みの古い通知が永久に terminate を抑止しないよう、比較は必ず
        **通知を除いた** 実活動の mtime と行う (_last_activity_mtime() は通知
        自体を候補に含むので、これに使うと常に「解除済み」に見えてしまう)。
        """
        notif_mtime, _ = self._newest_notification()
        if notif_mtime is None:
            return False
        real_mtime = self._non_notification_mtime()
        return real_mtime is None or real_mtime <= notif_mtime

    # ------------------------------------------------------------------
    # Core check
    # ------------------------------------------------------------------

    def check(self) -> Literal["alive", "warn", "terminate", "kill"]:
        """check_detail() の判定だけを返す薄いラッパー (既存呼び出し互換)。"""
        return self.check_detail().verdict

    def check_detail(self) -> CheckResult:
        """
        Evaluate Worker health across all signal layers.

        Returns (verdict, reason, idle_seconds, process_signal, awaiting_human):
          "alive"     — Worker is healthy, no action needed
          "warn"      — Soft idle threshold exceeded; send alert to Taskvia
          "terminate" — Hard idle threshold or absolute max exceeded; graceful shutdown
          "kill"      — mux window gone; cleanup only

        t016: 旧実装はここで「子プロセスが居れば alive」と即返していたため、
        以下の idle 判定に一度も到達しなかった。idle は常に評価し、プロセス層と
        「人間待ち」は **terminate を warn に落とす方向にだけ** 効かせる。
        判断が付かないケース (process_signal == "unknown") も殺さない側に倒す。
        """
        now = time.time()
        idle_seconds = now - self._last_activity_mtime()

        # 1. 絶対上限チェック
        #    idle とは独立した天井。プロセス層では抑制しない — 長時間 task は
        #    task frontmatter の timeout.max で明示的に引き上げる運用のままにする
        #    (knowledge/daemon-authority.md §4-3 で started_at の起点見直しは
        #    backlog 送りと決まっている)。
        if now - self.started_at > self.max_threshold:
            return CheckResult("terminate", "max_exceeded", idle_seconds, "not_probed", False)

        # 2. mux 窓の生存チェック
        if self._tmux_window_target() is None:
            return CheckResult("kill", "window_gone", idle_seconds, "no_window", False)

        # 3. idle 判定 (常に評価する)
        process_signal = self._process_signal()
        awaiting_human = self._awaiting_human()

        if idle_seconds > self.idle_threshold * 2:
            # プロセス層が terminate を抑制するケース。理由をログで区別できるよう
            # 別々の reason にする ("実行中だから見送った" と "見えないから見送った"
            # は運用上まったく別の話なので、まとめると原因調査ができない)。
            suppressed_by_process = {
                "executing": "hard_idle_but_executing",
                "unknown": "hard_idle_but_process_unknown",
            }.get(process_signal)
            if suppressed_by_process:
                return CheckResult(
                    "warn", suppressed_by_process, idle_seconds, process_signal, awaiting_human,
                )
            if awaiting_human:
                return CheckResult(
                    "warn", "hard_idle_but_awaiting_human",
                    idle_seconds, process_signal, awaiting_human,
                )
            return CheckResult("terminate", "hard_idle", idle_seconds, process_signal, awaiting_human)

        if idle_seconds > self.idle_threshold:
            return CheckResult("warn", "soft_idle", idle_seconds, process_signal, awaiting_human)

        return CheckResult("alive", "active", idle_seconds, process_signal, awaiting_human)


# ---------------------------------------------------------------------------
# Mass-kill guard (t016)
# ---------------------------------------------------------------------------

def _is_mass_kill(results: dict, mux_available: bool, mux_list_empty: bool) -> bool:
    """True when "every monitored Worker reports kill" is a config error
    rather than N real Worker deaths.

    t020 (P1 fix): the original version returned True whenever every
    monitored Worker's check() came back "kill" — but with exactly one
    Worker monitored, its lone real death is *always* "100% kill" too.
    crewvia's normal operation has 1-2 in_progress Workers, so N=1/N=2 are
    the common case, not the edge case the original docstring assumed away.
    Falling into this branch skips `del monitors[...]`, so a genuinely
    vanished Worker was never cleaned up (the watchdog's whole purpose,
    defeated in its most common operating condition) and the Taskvia alert
    fired every cycle forever. t020's fix added two independent gates:
    `len(results) >= 2` and a direct backend signal (`not mux_available` or
    `mux_list_empty`).

    t024 (P2 fix — Seo caught their own t020 proposal being too conservative):
    ANDing `len(results) >= 2` with the backend signal reintroduces exactly
    the bug this whole guard exists to prevent, for the one case it doesn't
    cover — N=1 with the backend genuinely broken. There, `len(results) >= 2`
    forces False, so the lone live Worker (whose window merely *looks* gone
    because the backend is misconfigured, not because it actually died) goes
    through the normal kill path: `del monitors[...]`, then re-created next
    cycle from the still-in_progress task file with a fresh `started_at` —
    the exact "KILL every cycle, forever" symptom t016 was written to fix,
    now reproduced specifically at the most common Worker count. Comparing
    every (count, backend) combination against `len(results) >= 2` removed
    shows they agree everywhere except that one cell:

        case                        with len>=2   without len>=2
        N=1 real death, backend OK     False          False
        N=1, backend broken            False          True   <- only diff
        N=2 real death, backend OK     False          False
        N=2, backend broken            True           True
        N=3 real death, backend OK     False          False
        N=3, backend broken            True           True

    `len(results) >= 2` never helped tell a real death from a config error —
    a real death always leaves the backend healthy (mux_list_empty=False),
    so the backend signal alone already returns False for it regardless of
    count. The count gate only ever *suppressed* the one case where the
    backend signal is what actually matters. So it's gone: this now checks
    the backend signal alone, for any count >= 1 (still 0 for the N=0 case —
    "nothing monitored" is never a mass-kill, there's nothing to be wrong
    about yet).
    """
    if not results:
        return False
    if not all(status == "kill" for status in results.values()):
        return False
    return (not mux_available) or mux_list_empty


def _should_alert_mass_kill(
    last_alert_at: float, now: float, backoff_seconds: float = MASS_KILL_ALERT_BACKOFF_SECONDS
) -> bool:
    """True if enough time has passed since the last mass-kill Taskvia alert
    to send another one (t020: a persistent misconfiguration must not
    re-alert Taskvia every single watchdog cycle forever)."""
    return (now - last_alert_at) >= backoff_seconds


# ---------------------------------------------------------------------------
# Graceful terminate
# ---------------------------------------------------------------------------

def graceful_terminate(monitor: WorkerMonitor) -> None:
    """Send a shutdown message via mux, wait, then SIGTERM → SIGKILL.

    t003: guarded by repo_identity_ok() at entry AND again immediately before
    each destructive step (SIGTERM, SIGKILL). A single check at entry is not
    enough — TERMINATE_GRACE_PERIOD (60s) and KILL_DELAY (10s) are both long
    enough for this process's own repo_root (a worktree, in the scenario this
    guards against) to be removed mid-wait. Any check failing means this
    process can no longer prove it owns the mux workspace it is about to act
    on — it fails closed: skip the remaining steps, never kill.
    """
    if not repo_identity_ok(monitor.repo_root):
        _log(
            f"[terminate] REFUSING to act on {monitor.agent_name}/{monitor.task_id}: "
            f"self-identity check failed for repo_root={monitor.repo_root} "
            f"(missing or no longer a git checkout — likely a removed worktree). "
            f"Skipping shutdown message and kill entirely."
        )
        return

    name = monitor._mux_window_name()
    if not name:
        _log(f"[kill] {monitor.agent_name}/{monitor.task_id}: window already gone")
        return

    msg = "タイムアウトのため中断します。現在の状況を 1-2 行で記載して終了してください。"
    ok = _mux.send(name, msg)
    if ok:
        _log(f"[terminate] {monitor.agent_name}/{monitor.task_id}: sent shutdown message, "
             f"waiting {TERMINATE_GRACE_PERIOD}s for graceful exit")
    else:
        _log(f"[terminate] WARNING: mux send failed for {name!r}")

    # Wait grace period, checking if Worker exits on its own
    for _ in range(TERMINATE_GRACE_PERIOD):
        time.sleep(1)
        if monitor._mux_window_name() is None:
            _log(f"[terminate] {monitor.agent_name}/{monitor.task_id}: Worker exited gracefully")
            return

    # Re-verify immediately before SIGTERM — the 60s wait above is long
    # enough for the worktree behind repo_root to have been removed since
    # the entry check.
    if not repo_identity_ok(monitor.repo_root):
        _log(
            f"[terminate] REFUSING SIGTERM for {monitor.agent_name}/{monitor.task_id}: "
            f"self-identity check failed after grace period (repo_root={monitor.repo_root})"
        )
        return

    # SIGTERM → wait → SIGKILL
    pane_pid = _mux.pid(name)
    if pane_pid is not None:
        _log(f"[terminate] SIGTERM → pid {pane_pid}")
        try:
            os.kill(pane_pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        time.sleep(KILL_DELAY)
        # Re-verify once more immediately before SIGKILL — same rationale,
        # smaller window (KILL_DELAY=10s).
        if not repo_identity_ok(monitor.repo_root):
            _log(
                f"[terminate] REFUSING SIGKILL for {monitor.agent_name}/{monitor.task_id}: "
                f"self-identity check failed during KILL_DELAY wait (repo_root={monitor.repo_root})"
            )
            return
        try:
            os.kill(pane_pid, signal.SIGKILL)
            _log(f"[terminate] SIGKILL → pid {pane_pid}")
        except ProcessLookupError:
            pass
    else:
        _log(f"[terminate] WARNING: could not get pane pid for {name!r}")


# ---------------------------------------------------------------------------
# Taskvia reporting
# ---------------------------------------------------------------------------

def taskvia_alert(taskvia_url: str, taskvia_token: str,
                  agent_name: str, content: str) -> None:
    """POST type=alert to /api/log. Silent on error."""
    if not taskvia_token:
        return
    payload = json.dumps({
        "type": "alert",
        "agent": agent_name,
        "content": content,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {taskvia_token}",
    }
    try:
        req = urllib.request.Request(
            f"{taskvia_url}/api/log", data=payload, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Task scanning
# ---------------------------------------------------------------------------

def load_active_tasks(queue_dir: Path) -> list[tuple[str, str, dict]]:
    """
    Return list of (mission_slug, task_id, meta) for all in_progress tasks
    across active missions.
    """
    state_file = queue_dir / "state.yaml"
    if not state_file.exists():
        return []

    state = parse_yaml(state_file.read_text())
    active_missions = list(state.get("active_missions") or [])

    results: list[tuple[str, str, dict]] = []
    missions_dir = queue_dir / "missions"
    for slug in active_missions:
        tasks_dir = missions_dir / slug / "tasks"
        if not tasks_dir.exists():
            continue
        for fn in tasks_dir.iterdir():
            if not re.fullmatch(r"t\d+\.md", fn.name):
                continue
            try:
                meta, _ = parse_frontmatter(fn.read_text())
            except Exception:
                continue
            if meta.get("status") == "in_progress":
                results.append((slug, str(meta.get("id", fn.stem)), meta))

    return results


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# _LOG_FILE は「固定ファイルへの明示的な上書き」。テストが monkeypatch で使う。
# 本番は _LOG_DIR を設定し、日付ごとにローテートする (dispatcher.sh と同じ規約)。
_LOG_FILE: Optional[Path] = None
_LOG_DIR: Optional[Path] = None
_OBSERVATION_LOG_FILE: Optional[Path] = None


def _current_log_file() -> Optional[Path]:
    """今このタイミングで書くべきログファイル。

    _LOG_DIR 側は呼ばれるたびに日付を評価するので、常駐したまま日付を跨いでも
    自動で次の日のファイルに切り替わる (旧実装は run() で 1 度だけパスを決めて
    いたため、単一ファイルが無限に伸び続けていた)。
    """
    if _LOG_FILE is not None:
        return _LOG_FILE
    if _LOG_DIR is None:
        return None
    return _LOG_DIR / f"watchdog-{time.strftime('%Y%m%d')}.log"


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"[watchdog {ts}] {msg}"
    print(line, file=sys.stderr)
    path = _current_log_file()
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(line + "\n")
        except OSError:
            pass


class VerdictLogger:
    """判定結果を watchdog のログに残す (t016)。

    旧実装は warn / terminate / kill のときしか _log() を呼ばなかった。
    check() が「子プロセスが居れば alive」で即返していたため非 alive の判定が
    一度も起きず、結果として **30 秒ごとの判定が 1 行も残らなかった**
    (registry/watchdog.log は 2026-09-18 の起動行以降が空)。判定が記録されない
    限り QA も本番運用も「watchdog が何をどう判断したか」を検証できない。

    ただし全 Worker 分を毎 cycle 書くとログが肥大するので:
      - 判定が **変わった** 瞬間は必ず 1 行
      - 同じ判定が続く間は summary_every cycle ごとに 1 行 ("still ...")
    とする。これで「静かな時間も watchdog は生きていて alive と判定し続けて
    いた」ことが後から確認でき、かつ行数は有界に保たれる。
    """

    def __init__(self, summary_every: int = VERDICT_SUMMARY_EVERY) -> None:
        self.summary_every = summary_every
        self._state: dict[tuple[str, str], tuple[str, int]] = {}

    @staticmethod
    def _line(monitor: "WorkerMonitor", detail: CheckResult, repeats: int) -> str:
        head = (
            f"still {detail.verdict} ({repeats} cycles)"
            if repeats
            else detail.verdict
        )
        return (
            f"[verdict] {monitor.agent_name}/{monitor.task_id} {head} "
            f"idle={detail.idle_seconds:.0f}s idle_threshold={monitor.idle_threshold} "
            f"max_threshold={monitor.max_threshold} process={detail.process_signal} "
            f"awaiting_human={str(detail.awaiting_human).lower()} reason={detail.reason}"
        )

    def record(self, monitor: "WorkerMonitor", detail: CheckResult) -> None:
        key = (monitor.agent_name, monitor.task_id)
        previous = self._state.get(key)
        if previous is None or previous[0] != detail.verdict:
            self._state[key] = (detail.verdict, 0)
            _log(self._line(monitor, detail, repeats=0))
            return
        repeats = previous[1] + 1
        self._state[key] = (detail.verdict, repeats)
        if self.summary_every > 0 and repeats % self.summary_every == 0:
            _log(self._line(monitor, detail, repeats=repeats))

    def forget(self, monitor: "WorkerMonitor") -> None:
        """監視対象から外れた Worker の状態を捨てる (同名で再起動したら初回扱い)。"""
        self._state.pop((monitor.agent_name, monitor.task_id), None)


_LEGACY_POINTER_MARK = "[moved]"


def _leave_legacy_log_pointer(legacy: Path) -> None:
    """旧ログパスに「引っ越し先」を 1 行だけ残す。

    `registry/dispatcher.log` は dispatcher が `logs/dispatcher/` の日付別
    ファイルへ移行した後も残り続け、2026-09-21 には「2026-09-05 以降更新されて
    いない = ログ経路が壊れている」と誤読される原因になった (実際には新しい
    パスに正常に出ていた)。watchdog で同じ引っ越しをする以上、同じ誤読を
    仕込まないための一行。

    既にマーカーが書かれていれば何もしない (再起動のたびに伸ばさない)。
    """
    try:
        if not legacy.exists():
            return
        tail = legacy.read_text(errors="replace").rstrip().rsplit("\n", 1)[-1]
        if _LEGACY_POINTER_MARK in tail:
            return
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with legacy.open("a") as f:
            f.write(
                f"[watchdog {ts}] {_LEGACY_POINTER_MARK} このファイルはもう使われない。"
                f"以降のログは logs/watchdog/watchdog-YYYYMMDD.log を見ること。\n"
            )
    except OSError:
        pass


def _log_observation(
    monitor: "WorkerMonitor",
    check_result: str,
    detail: Optional[CheckResult] = None,
) -> None:
    """★task_162 案C(観測専用). idle秒数・通知状況を registry/watchdog-observations.jsonl
    へ追記するだけの関数。check() の戻り値・判定条件には一切関与しない
    (check_result は記録のためだけに受け取る — この関数の失敗や有無で check() の
    振る舞いが変わることは無い)。"""
    if _OBSERVATION_LOG_FILE is None:
        return
    try:
        snapshot = monitor._observation_snapshot()
        snapshot["check_result"] = check_result
        if detail is not None:
            # t016: 判定の根拠も残す。check_result だけだと「なぜ terminate
            # しなかったのか」(executing / awaiting_human による抑制) が
            # 後から追えない。
            snapshot["reason"] = detail.reason
            snapshot["process_signal"] = detail.process_signal
            snapshot["awaiting_human"] = detail.awaiting_human
        with _OBSERVATION_LOG_FILE.open("a") as f:
            f.write(json.dumps(snapshot) + "\n")
    except Exception:
        pass  # 観測ログの失敗で watchdog 本体を止めない


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _assert_repo_identity_or_exit(repo_root: Path) -> None:
    """t003: self-identity check, once per main-loop cycle.

    A watchdog started against a git worktree that has since been removed
    must stop monitoring (and, crucially, stop being *able* to kill
    anything) rather than keep running on inherited mux env pointing at a
    workspace it can no longer prove it owns. See repo_identity_ok() in
    lib_mux.py for the full rationale. This is the coarse, once-per-cycle
    half of the guard; graceful_terminate() re-checks immediately before
    each destructive step for the fine-grained half (a worktree can be
    removed mid-wait, inside a single cycle, after this check already
    passed).

    Exits the whole process (sys.exit(1)) rather than merely skipping the
    cycle: an invalid repo_root means every path derived from it (queue_dir,
    registry_dir, ...) is suspect too, not just the kill actions.
    """
    if repo_identity_ok(repo_root):
        return
    _log(
        f"FATAL: repo_root {repo_root} no longer exists or is not a "
        f"git checkout (worktree removed?). A stale watchdog must "
        f"not keep running against inherited mux env — exiting."
    )
    sys.exit(1)


def run(repo_root: Path, interval: int) -> None:
    global _LOG_DIR, _OBSERVATION_LOG_FILE
    registry_dir = repo_root / "registry"
    registry_dir.mkdir(exist_ok=True)
    _LOG_DIR = repo_root / "logs" / "watchdog"
    _OBSERVATION_LOG_FILE = registry_dir / "watchdog-observations.jsonl"
    _leave_legacy_log_pointer(registry_dir / "watchdog.log")

    taskvia_url = os.environ.get("TASKVIA_URL", "https://taskvia.vercel.app")
    taskvia_token = os.environ.get("TASKVIA_TOKEN", "")
    queue_dir = Path(os.environ.get("CREWVIA_QUEUE", str(repo_root / "queue")))

    # Track active monitors: (slug, task_id) → WorkerMonitor
    monitors: dict[tuple[str, str], WorkerMonitor] = {}

    # t020: last time the mass-kill CONFIG ERROR alert actually fired.
    # Without this, a persistent misconfiguration re-sends the Taskvia alert
    # every single cycle forever.
    last_mass_kill_alert_at = 0.0

    # t016: 判定結果を watchdog のログに残す。旧実装は非 alive のときしか
    # ログを書かず、その非 alive が一度も起きなかったため判定が 1 行も残って
    # いなかった。
    verdict_logger = VerdictLogger()

    # t016: log which mux backend got selected at startup. A silent
    # misconfiguration here (e.g. config/crewvia.yaml `mode:` failing to
    # parse because of a trailing inline comment) used to be invisible until
    # every live Worker started getting falsely reported as "window gone" —
    # this one line turns that into an immediate, obvious startup fact.
    _backend_name = type(_mux._backend).__name__
    _log(
        f"Starting Watchdog v2 (PID {os.getpid()}, interval={interval}s, "
        f"repo={repo_root}, mux_backend={_backend_name})"
    )
    if not _mux.available():
        _log(
            f"WARNING: mux backend ({_backend_name}) reports unavailable at startup. "
            f"Every Worker will look like its window is gone until this is fixed — "
            f"check CREWVIA_MUX / config/crewvia.yaml `mode:` and that the backend "
            f"(tmux / herdr) is actually running."
        )

    # Graceful exit on SIGTERM / SIGINT
    def _on_signal(signum, _frame):
        _log(f"Received signal {signum}, shutting down")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    while True:
        try:
            _assert_repo_identity_or_exit(repo_root)

            active_tasks = load_active_tasks(queue_dir)
            active_keys = {(slug, tid) for slug, tid, _ in active_tasks}

            # Remove monitors for tasks that are no longer in_progress
            for key in list(monitors.keys()):
                if key not in active_keys:
                    del monitors[key]

            # Add monitors for new in_progress tasks
            for slug, task_id, meta in active_tasks:
                key = (slug, task_id)
                if key not in monitors:
                    monitors[key] = WorkerMonitor(
                        task_id=task_id,
                        task_card=meta,
                        profiles=PROFILES,
                        repo_root=repo_root,
                    )

            # Evaluate every monitor's status up front (side-effect free) before
            # acting on any of them. This lets us tell "every single monitored
            # Worker's window looks gone in the same cycle" apart from an
            # isolated, real window closure — see the mass-kill guard below
            # (t016).
            details: dict[tuple[str, str], CheckResult] = {
                key: monitor.check_detail() for key, monitor in monitors.items()
            }
            results: dict[tuple[str, str], "Literal['alive', 'warn', 'terminate', 'kill']"] = {
                key: detail.verdict for key, detail in details.items()
            }

            # Cheap pre-check (no extra backend calls) before paying for the
            # corroborating _mux.available()/.list() probes below — only
            # bother when every monitored Worker already looks dead. t024:
            # deliberately just "all kill", no count threshold — see
            # _is_mass_kill()'s docstring for why a count gate here would
            # reintroduce the bug this guard exists to prevent.
            kill_count = sum(1 for s in results.values() if s == "kill")
            maybe_mass_kill = len(results) > 0 and kill_count == len(results)

            if maybe_mass_kill:
                mux_list_now = _mux.list()
                mux_available_now = _mux.available()
                # See _is_mass_kill() docstring (t020): count alone is never
                # enough — corroborate with a direct backend signal before
                # treating this as a config error instead of N real deaths.
                if _is_mass_kill(
                    results,
                    mux_available=mux_available_now,
                    mux_list_empty=(len(mux_list_now) == 0),
                ):
                    backend_name = type(_mux._backend).__name__
                    msg = (
                        f"CONFIG ERROR: all {len(results)} monitored Worker(s) report "
                        f"their mux window gone in the same cycle (mux_backend={backend_name}, "
                        f"mux.available()={mux_available_now}, mux.list()={mux_list_now!r}). "
                        f"This almost always means the mux backend is misconfigured "
                        f"(CREWVIA_MUX / config/crewvia.yaml `mode:` / backend not actually "
                        f"running), not that every Worker died at once. Skipping cleanup "
                        f"this cycle."
                    )
                    _log(msg)
                    # t020: throttle the outbound alert — the log line above still
                    # fires every cycle for local debugging, but Taskvia only hears
                    # about it at most once per MASS_KILL_ALERT_BACKOFF_SECONDS
                    # instead of every single interval forever.
                    now_ts = time.time()
                    if _should_alert_mass_kill(last_mass_kill_alert_at, now_ts):
                        taskvia_alert(taskvia_url, taskvia_token, "watchdog", msg)
                        last_mass_kill_alert_at = now_ts
                    else:
                        _log(
                            f"(mass-kill alert suppressed — backoff, "
                            f"{now_ts - last_mass_kill_alert_at:.0f}s since last)"
                        )
                    for key, monitor in monitors.items():
                        verdict_logger.record(monitor, details[key])
                        _log_observation(monitor, results[key], details[key])
                    time.sleep(interval)
                    continue

            # Check each monitor
            for (slug, task_id), monitor in list(monitors.items()):
                detail = details[(slug, task_id)]
                status = results[(slug, task_id)]
                agent = monitor.agent_name

                # t016: 判定そのものをログに残す (変化時 + 一定間隔の要約)。
                verdict_logger.record(monitor, detail)

                # ★task_162 案C(観測専用): check() の戻り値・分岐には一切影響しない
                # 独立した記録経路。この呼び出しを削除しても以下の判定ロジックは
                # 完全に同一に動作する。
                _log_observation(monitor, status, detail)

                if status == "alive":
                    pass  # healthy — no action

                elif status == "warn":
                    idle = time.time() - monitor._last_activity_mtime()
                    msg = (
                        f"WARN: {agent}/{task_id} (mission={slug}) idle {idle:.0f}s "
                        f"(threshold={monitor.idle_threshold}s)"
                    )
                    _log(msg)
                    taskvia_alert(taskvia_url, taskvia_token, agent, msg)

                elif status == "terminate":
                    elapsed = time.time() - monitor.started_at
                    _log(
                        f"TERMINATE: {agent}/{task_id} (mission={slug}) "
                        f"elapsed={elapsed:.0f}s"
                    )
                    taskvia_alert(
                        taskvia_url, taskvia_token, agent,
                        f"TERMINATE: {agent}/{task_id} タイムアウト (elapsed={elapsed:.0f}s)",
                    )
                    graceful_terminate(monitor)
                    del monitors[(slug, task_id)]

                elif status == "kill":
                    backend_name = type(_mux._backend).__name__
                    _log(
                        f"KILL: {agent}/{task_id} (mission={slug}) mux window gone "
                        f"(backend={backend_name}), cleanup only"
                    )
                    taskvia_alert(
                        taskvia_url, taskvia_token, agent,
                        f"KILL: {agent}/{task_id} mux window が消失 (backend={backend_name})",
                    )
                    del monitors[(slug, task_id)]

        except Exception as e:
            _log(f"ERROR in dispatch cycle: {e}")

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="watchdog.py",
        description="Crewvia Worker Watchdog v2 — multi-signal monitoring daemon",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"watchdog.py {__version__}",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_CHECK_INTERVAL,
        metavar="SECONDS",
        help=f"Check interval in seconds (default: {DEFAULT_CHECK_INTERVAL})",
    )
    parser.add_argument(
        "--repo-root", type=Path,
        default=Path(__file__).resolve().parent.parent,
        metavar="PATH",
        help="Repository root (default: parent of scripts/)",
    )
    args = parser.parse_args()
    run(repo_root=args.repo_root, interval=args.interval)


if __name__ == "__main__":
    main()
