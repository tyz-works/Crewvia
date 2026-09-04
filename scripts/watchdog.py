#!/usr/bin/env python3
"""
scripts/watchdog.py — Crewvia Worker Watchdog v2

Monitors active Workers via three signal layers:
  - Tool layer   : registry/activity/<agent>/<task_id>.activity
  - Thought layer: registry/notifications/<agent>/ (Notification hook, M1)
  - Process layer: tmux pane_pid → pgrep -P child processes

Multi-level judgment per WorkerMonitor:
  alive     → no action
  warn      → POST /api/log type=alert (soft idle threshold)
  terminate → graceful shutdown (hard idle threshold or absolute max)
  kill      → cleanup only (tmux session already gone)

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
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Literal, Optional

# Import lib_mux — assumes watchdog.py lives in scripts/ alongside lib_mux.py
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))
from lib_mux import Mux  # noqa: E402
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

    def _observation_snapshot(self) -> dict:
        """★task_162 案C(観測専用). check() の判定には一切使わない — 呼び出し元は
        check() の戻り値やロジックを変更しない別経路のログ専用スナップショットである。

        無音区間の長さ(idle_seconds、_last_activity_mtime() をそのまま呼ぶだけで
        既存の計算方法を変えない)に加え、直近の notification(あれば)の種別・
        経過時間・「その後 activity/heartbeat の更新があったか(=解除されたか)」を
        記録する。「無音」と「人間待ちで無音」を区別するための情報(Picard指示)。
        """
        now = time.time()

        activity_file = (
            self.repo_root / "registry" / "activity" / self.agent_name
            / f"{self.task_id}.activity"
        )
        activity_mtime = activity_file.stat().st_mtime if activity_file.exists() else None

        hb_file = self.repo_root / "registry" / "heartbeats" / self.agent_name
        heartbeat_mtime = hb_file.stat().st_mtime if hb_file.exists() else None

        # notification を除いた「実活動」の最新mtime(通知の解除判定に使う。
        # _last_activity_mtime() は notification 自体を候補に含めるため、
        # ここでは意図的に別計算にしている — 通知が来ただけで「解除済み」と
        # 誤認しないようにするため)
        non_notification_candidates = [
            m for m in (activity_mtime, heartbeat_mtime) if m is not None
        ]
        non_notification_mtime = max(non_notification_candidates) if non_notification_candidates else None

        last_notif_type: Optional[str] = None
        last_notif_mtime: Optional[float] = None
        notif_dir = self.repo_root / "registry" / "notifications" / self.agent_name
        if notif_dir.exists():
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
            if newest_file is not None:
                last_notif_mtime = newest_mtime
                try:
                    payload = json.loads(newest_file.read_text())
                    last_notif_type = payload.get("notification_type")
                except Exception:
                    last_notif_type = "(unparseable)"

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

    def _has_child_processes(self) -> bool:
        """Return True if the mux pane has live child processes (Claude is active)."""
        name = self._mux_window_name()
        if not name:
            return False
        pane_pid = _mux.pid(name)
        if pane_pid is None:
            return False
        try:
            r = subprocess.run(["pgrep", "-P", str(pane_pid)], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Core check
    # ------------------------------------------------------------------

    def check(self) -> Literal["alive", "warn", "terminate", "kill"]:
        """
        Evaluate Worker health across all signal layers.

        Returns:
          "alive"     — Worker is healthy, no action needed
          "warn"      — Soft idle threshold exceeded; send alert to Taskvia
          "terminate" — Hard idle threshold or absolute max exceeded; graceful shutdown
          "kill"      — tmux session gone; cleanup only
        """
        now = time.time()

        # 1. 絶対上限チェック
        if now - self.started_at > self.max_threshold:
            return "terminate"

        # 2. tmux session 生存チェック
        target = self._tmux_window_target()
        if target is None:
            return "kill"

        # 3. 子プロセス生存チェック (pgrep -P <pane_pid>)
        if self._has_child_processes():
            return "alive"

        # 4. activity / heartbeat / notification の mtime チェック
        idle_seconds = now - self._last_activity_mtime()

        if idle_seconds > self.idle_threshold * 2:
            return "terminate"
        if idle_seconds > self.idle_threshold:
            return "warn"

        return "alive"


# ---------------------------------------------------------------------------
# Graceful terminate
# ---------------------------------------------------------------------------

def graceful_terminate(monitor: WorkerMonitor) -> None:
    """Send a shutdown message via mux, wait, then SIGTERM → SIGKILL."""
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

    # SIGTERM → wait → SIGKILL
    pane_pid = _mux.pid(name)
    if pane_pid is not None:
        _log(f"[terminate] SIGTERM → pid {pane_pid}")
        try:
            os.kill(pane_pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        time.sleep(KILL_DELAY)
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

_LOG_FILE: Optional[Path] = None
_OBSERVATION_LOG_FILE: Optional[Path] = None


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"[watchdog {ts}] {msg}"
    print(line, file=sys.stderr)
    if _LOG_FILE:
        try:
            with _LOG_FILE.open("a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _log_observation(monitor: "WorkerMonitor", check_result: str) -> None:
    """★task_162 案C(観測専用). idle秒数・通知状況を registry/watchdog-observations.jsonl
    へ追記するだけの関数。check() の戻り値・判定条件には一切関与しない
    (check_result は記録のためだけに受け取る — この関数の失敗や有無で check() の
    振る舞いが変わることは無い)。"""
    if _OBSERVATION_LOG_FILE is None:
        return
    try:
        snapshot = monitor._observation_snapshot()
        snapshot["check_result"] = check_result
        with _OBSERVATION_LOG_FILE.open("a") as f:
            f.write(json.dumps(snapshot) + "\n")
    except Exception:
        pass  # 観測ログの失敗で watchdog 本体を止めない


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(repo_root: Path, interval: int) -> None:
    global _LOG_FILE, _OBSERVATION_LOG_FILE
    registry_dir = repo_root / "registry"
    registry_dir.mkdir(exist_ok=True)
    _LOG_FILE = registry_dir / "watchdog.log"
    _OBSERVATION_LOG_FILE = registry_dir / "watchdog-observations.jsonl"

    taskvia_url = os.environ.get("TASKVIA_URL", "https://taskvia.vercel.app")
    taskvia_token = os.environ.get("TASKVIA_TOKEN", "")
    queue_dir = Path(os.environ.get("CREWVIA_QUEUE", str(repo_root / "queue")))

    # Track active monitors: (slug, task_id) → WorkerMonitor
    monitors: dict[tuple[str, str], WorkerMonitor] = {}

    _log(f"Starting Watchdog v2 (PID {os.getpid()}, interval={interval}s, repo={repo_root})")

    # Graceful exit on SIGTERM / SIGINT
    def _on_signal(signum, _frame):
        _log(f"Received signal {signum}, shutting down")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    while True:
        try:
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

            # Check each monitor
            for (slug, task_id), monitor in list(monitors.items()):
                status = monitor.check()
                agent = monitor.agent_name

                # ★task_162 案C(観測専用): check() の戻り値・分岐には一切影響しない
                # 独立した記録経路。この呼び出しを削除しても以下の判定ロジックは
                # 完全に同一に動作する。
                _log_observation(monitor, status)

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
                    _log(f"KILL: {agent}/{task_id} (mission={slug}) tmux window gone, cleanup only")
                    taskvia_alert(
                        taskvia_url, taskvia_token, agent,
                        f"KILL: {agent}/{task_id} tmux window が消失",
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
