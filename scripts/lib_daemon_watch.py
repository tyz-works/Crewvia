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
  python3 scripts/lib_daemon_watch.py restart <name>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, NamedTuple, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib_mux import Mux, repo_identity_ok  # noqa: E402
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


# ---------------------------------------------------------------------------
# Process identity — PID plus generation
# ---------------------------------------------------------------------------

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
        except OSError:
            continue  # exited between listing and read — genuinely not there
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
# Launch command — one source of truth, shared with start.sh
# ---------------------------------------------------------------------------

#: Everything the launched daemon needs in order to talk to the *same* mux as
#: its launcher.  CREWVIA_MUX alone decides the *backend* but not the
#: *destination*: a daemon that inherits only the mode falls back to the
#: default session / workspace name, and then every mux verb it performs lands
#: somewhere nobody is looking — it would spawn its own peer into a session no
#: one is attached to and report to a Director that isn't there.  The QA
#: harness hit this for real (t006 QA FINDING-2); production is invisible to
#: it only because production uses the default names.
_SPAWN_ENV_VARS = ("CREWVIA_MUX", "CREWVIA_TMUX_SESSION", "CREWVIA_HERDR_WORKSPACE")


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
        body = f"bash '{scripts / 'dispatcher.sh'}'"
    elif name == DAEMON_WATCHDOG:
        body = f"python3 '{scripts / 'watchdog.py'}'"
    else:
        raise ValueError(f"unknown daemon: {name!r}")
    return f"cd '{repo_root}' && {prefix}{body}"


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

    def stale_seconds_for(self, name: str) -> int:
        return (self.dispatcher_stale_seconds if name == DAEMON_DISPATCHER
                else self.watchdog_stale_seconds)


_CONFIG_KEYS = (
    "dispatcher_stale_seconds", "watchdog_stale_seconds", "flap_window_seconds",
    "flap_threshold", "respawn_grace_seconds", "pause_report_after_seconds",
    "hold_report_after_seconds",
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

def pause(registry_dir, name: str, *, reason: str = "", now=None) -> str:
    """Suppress respawn of `name`; returns the token that can lift it.

    Needed because the documented restart recipe is "kill, then spawn": in the
    gap between the two the daemon genuinely is dead, and without a marker the
    peer is right to respawn it — landing a second copy next to the one the
    operator is about to start by hand.

    The token binds the marker to *this* restart.  Two overlapping restarts
    would otherwise have the first one's `resume` lift the second one's
    protection, which is the same "name is not identity" mistake the rest of
    this module is built around.
    """
    token = uuid.uuid4().hex
    path = pause_path(registry_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {
        "daemon": name,
        "token": token,
        "reason": reason,
        "created_at": float(now if now is not None else time.time()),
        "by_pid": os.getpid(),
    })
    return token


def resume(registry_dir, name: str, *, token: Optional[str] = None,
           force: bool = False) -> bool:
    """Lift the marker.  False when it belongs to someone else."""
    path = pause_path(registry_dir, name)
    marker = read_json(path)
    if marker is None:
        return True  # nothing to lift — idempotent
    if not force and token is not None and marker.get("token") != token:
        return False
    if not force and token is None:
        return False
    unlink_quiet(path)
    return True


def read_pause(registry_dir, name: str) -> Optional[dict]:
    return read_json(pause_path(registry_dir, name))


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

        marker = read_pause(self.registry_dir, peer)
        if marker is not None:
            self._maybe_report_stale_pause(marker, now)
            return Verdict(ACTION_PAUSED,
                           f"{peer} is paused for maintenance "
                           f"({marker.get('reason') or 'no reason given'})")

        state = self._read_state()
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
# Restart helper
# ---------------------------------------------------------------------------

def restart(name: str, *, repo_root=None, mux=None, reason: str = "manual restart",
            log: Callable[[str], None] = print) -> bool:
    """Stop and start `name` without its peer racing us into a double start.

    Order is load-bearing: **pause first**, then kill.  The recipe people
    actually run is "kill, then spawn", and in the gap the daemon really is
    dead — a peer watching at that moment is correct to respawn it, and the
    operator's own spawn a second later lands on top.  Pausing first closes
    the gap; resuming last reopens the watch only once the new one is up.
    """
    repo_root = Path(repo_root) if repo_root is not None else _SCRIPTS_DIR.parent
    mux = mux if mux is not None else Mux()
    registry_dir = repo_root / "registry"

    token = pause(registry_dir, name, reason=reason)
    try:
        mux.kill(name)
        ok = bool(mux.spawn(name, spawn_command(name, repo_root), cwd=str(repo_root)))
        if not ok:
            log(f"[daemon-watch] restart {name}: spawn reported no start")
        return ok
    finally:
        # Even on failure: leaving the marker behind would silently disable
        # mutual watch for this daemon, and the stale-pause report is a
        # 30-minute detour compared with just not leaking it.
        resume(registry_dir, name, token=token)


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

    _sub("restart", "pause \u2192 kill \u2192 spawn \u2192 resume")

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    registry_dir = repo_root / "registry"

    if args.cmd == "spawn-cmd":
        print(spawn_command(args.name, repo_root))
        return 0

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
        print(pause(registry_dir, args.name, reason=args.reason))
        return 0

    if args.cmd == "resume":
        ok = resume(registry_dir, args.name, token=args.token, force=args.force)
        if not ok:
            print(f"refused: the marker on {args.name} was created by another run "
                  f"(pass --force to lift it anyway)", file=sys.stderr)
        return 0 if ok else 1

    if args.cmd == "restart":
        return 0 if restart(args.name, repo_root=repo_root) else 1

    return 2  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":
    sys.exit(main())
