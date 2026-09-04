#!/usr/bin/env python3
"""Mux backend abstraction — spawn / send / capture / list / kill / pid / attach.

Backends:
  TmuxBackend  — wraps current tmux CLI calls verbatim (Phase 1)
  HerdrBackend — herdr terminal workspace manager (Phase 2)

Backend selection (highest priority first):
  1. CREWVIA_MUX env  ("tmux" | "herdr")
  2. config/crewvia.yaml  `mode: tmux|herdr|inline`
  3. Auto: use tmux if `tmux` is in PATH

Usage as module:
  from lib_mux import Mux
  m = Mux()
  m.spawn("Omar-worker", "claude ...", cwd="/path/to/repo")
  m.send("Omar-worker", "タスクなし、shutdown")
  screen = m.capture("Omar-worker")
  workers = m.list(suffix="-worker")
  m.kill("Omar-worker")
  pid = m.pid("Omar-worker")
  m.attach("Sora-director")
  ok = m.available()

CLI usage (for bash callers):
  python3 lib_mux.py available            # exit 0 = available
  python3 lib_mux.py spawn <name> <cmd> [<cwd>]
  python3 lib_mux.py send  <name> <text>
  python3 lib_mux.py capture <name>       # prints raw screen text
  python3 lib_mux.py list [<suffix>]      # one name per line
  python3 lib_mux.py kill <name>
  python3 lib_mux.py pid  <name>          # prints integer PID
  python3 lib_mux.py attach <name>
"""

import json
import os
import shutil
import socket
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


def _config_mode() -> Optional[str]:
    """Read `mode:` key from config/crewvia.yaml relative to this script's repo root.

    Returns "tmux", "herdr", "inline", or None if not found / unreadable.
    """
    script_dir = Path(__file__).parent
    config_path = script_dir.parent / "config" / "crewvia.yaml"
    try:
        text = config_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("mode:") and not stripped.startswith("#"):
                value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
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

    # Legacy compat: CREWVIA_TMUX=1 → tmux
    crewvia_tmux = os.environ.get("CREWVIA_TMUX", "")
    if crewvia_tmux == "1":
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

    def kill(self, name: str) -> bool:
        raise NotImplementedError

    def pid(self, name: str) -> Optional[int]:
        raise NotImplementedError

    def attach(self, name: str) -> bool:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError


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
        Returns True on success, False if the window already existed or on error.

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
    """

    BACKEND_NAME = "tmux"

    def _target(self, name: str) -> str:
        return f"{_session()}:{name}"

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def spawn(self, name: str, cmd: str, cwd: Optional[str] = None,
              env: Optional[dict] = None) -> bool:
        """Create a tmux window named `name` and run `cmd`.

        Mirrors start.sh L468-481:
          - has-session → new-session (if session missing) → new-window
          - send-keys <cmd> + Enter
        Returns False (without error) if window already exists.
        """
        session = _session()
        try:
            # Check / create session
            has = subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True, timeout=5,
            )
            if has.returncode != 0:
                r = subprocess.run(
                    ["tmux", "new-session", "-d", "-s", session, "-n", name],
                    capture_output=True, timeout=5,
                )
                if r.returncode != 0:
                    self._warn(f"new-session failed for {session!r}: {r.stderr.decode()}")
                    return False
            else:
                # Session exists — check if window already exists
                existing = subprocess.run(
                    ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
                    capture_output=True, text=True, timeout=5,
                )
                if existing.returncode == 0 and name in existing.stdout.splitlines():
                    # Window already present → no-op, return False per spec
                    return False
                r = subprocess.run(
                    ["tmux", "new-window", "-t", session, "-n", name],
                    capture_output=True, timeout=5,
                )
                if r.returncode != 0:
                    self._warn(f"new-window failed for {name!r}: {r.stderr.decode()}")
                    return False

            target = self._target(name)
            subprocess.run(
                ["tmux", "send-keys", "-t", target, cmd],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Enter"],
                capture_output=True, timeout=5,
            )
            return True
        except Exception as e:
            self._warn(f"spawn {name!r} failed: {e}")
            return False

    def send(self, name: str, text: str) -> bool:
        """Send `text` + Enter to the named window.

        Uses 2-step send-keys with 0.1 s sleep between text and Enter to work
        around Claude TUI's bracketed paste handling (same as dispatcher.sh).
        """
        target = self._target(name)
        try:
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
            self._warn(f"send to {name!r} failed: {e}")
            return False

    def capture(self, name: str) -> str:
        """Return the current pane contents via capture-pane -p."""
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
            names = [line.strip() for line in r.stdout.splitlines() if line.strip()]
            if suffix:
                names = [n for n in names if n.endswith(suffix)]
            return names
        except Exception as e:
            self._warn(f"list failed: {e}")
            return []

    def kill(self, name: str) -> bool:
        """Kill the named window."""
        target = self._target(name)
        try:
            r = subprocess.run(
                ["tmux", "kill-window", "-t", target],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception as e:
            self._warn(f"kill {name!r} failed: {e}")
            return False

    def pid(self, name: str) -> Optional[int]:
        """Return the shell PID of the pane (display-message #{pane_pid})."""
        target = self._target(name)
        try:
            r = subprocess.run(
                ["tmux", "display-message", "-p", "-t", target, "#{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                self._warn(f"display-message failed for {name!r}: {r.stderr}")
                return None
            raw = r.stdout.strip()
            if not raw:
                return None
            return int(raw)
        except (ValueError, Exception) as e:
            self._warn(f"pid {name!r} failed: {e}")
            return None

    def attach(self, name: str) -> bool:
        """Attach to the session.

        If $TMUX is set (already inside tmux), use switch-client.
        Otherwise use attach-session.
        Returns True on success, False on error.
        """
        session = _session()
        try:
            if os.environ.get("TMUX"):
                r = subprocess.run(
                    ["tmux", "switch-client", "-t", session],
                    capture_output=True, timeout=5,
                )
            else:
                r = subprocess.run(
                    ["tmux", "attach-session", "-t", self._target(name)],
                    timeout=60,
                )
            return r.returncode == 0
        except Exception as e:
            self._warn(f"attach {name!r} failed: {e}")
            return False


# ---------------------------------------------------------------------------
# HerdrBackend (Phase 2)
# ---------------------------------------------------------------------------

# Cache directory relative to repo root (script's parent's parent).
_HERDR_CACHE_DIR_NAME = Path("registry") / "mux"

# Verified herdr version.
_HERDR_VERIFIED_VERSION = "0.8.2"

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

_HERDR_SOCK_PATH = Path.home() / ".config" / "herdr" / "herdr.sock"


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
      IMPORTANT — callers must capture() and wait for '❯' before calling send().
      pane run with '❯' present submits reliably; without it, Enter is dropped
      (Phase 0 finding).  send() adds an Enter insurance: if capture() after
      pane run still shows the text in the input line, one pane send-keys enter
      is appended.

    Herdr CLI subcommands are collected in _HERDR_CLI table (module level) so
    a CLI rename requires editing only that table.
    """

    BACKEND_NAME = "herdr"

    def __init__(self) -> None:
        # Resolve cache root lazily to support test PATH overrides.
        self._cache_root: Optional[Path] = None

    def _cache_dir(self) -> Path:
        """Return the registry/mux/ path relative to the script's repo root."""
        if self._cache_root is None:
            script_dir = Path(__file__).parent
            self._cache_root = script_dir.parent / _HERDR_CACHE_DIR_NAME
        return self._cache_root

    def _cache_path(self, name: str) -> Path:
        return self._cache_dir() / f"{name}.json"

    def _write_cache(self, name: str, tab_id: str, pane_id: str) -> None:
        try:
            self._cache_dir().mkdir(parents=True, exist_ok=True)
            entry = {
                "tab_id": tab_id,
                "pane_id": pane_id,
                "backend": "herdr",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._cache_path(name).write_text(json.dumps(entry, indent=2), encoding="utf-8")
        except Exception as e:
            self._warn(f"cache write failed for {name!r}: {e}")

    def _read_cache(self, name: str) -> Optional[dict]:
        try:
            return json.loads(self._cache_path(name).read_text(encoding="utf-8"))
        except Exception:
            return None

    def _delete_cache(self, name: str) -> None:
        try:
            p = self._cache_path(name)
            if p.exists():
                p.unlink()
        except Exception as e:
            self._warn(f"cache delete failed for {name!r}: {e}")

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
            # Cache stale — drop and re-resolve.
            self._delete_cache(name)

        # Live lookup via pane list.
        ws_id = self._workspace_id()
        if ws_id is None:
            return None
        data = _herdr_run("pane_list", ["--workspace", ws_id], timeout=10)
        if data is None:
            return None
        panes = data.get("result", {}).get("panes", [])
        for pane in panes:
            if pane.get("label") == name:
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
            self._delete_cache(name)

        # Live lookup.
        ws_id = self._workspace_id()
        if ws_id is None:
            return None
        data = _herdr_run("pane_list", ["--workspace", ws_id], timeout=10)
        if data is None:
            return None
        panes = data.get("result", {}).get("panes", [])
        for pane in panes:
            if pane.get("label") == name:
                return {
                    "tab_id": pane.get("tab_id"),
                    "pane_id": pane.get("pane_id"),
                    "backend": "herdr",
                }
        return None

    # ------------------------------------------------------------------
    # Server / workspace helpers
    # ------------------------------------------------------------------

    def _ensure_server(self) -> bool:
        """Ensure herdr server is running.  Start it if not, wait up to 10s."""
        if _herdr_ping():
            return True
        # Start server (daemon-izes automatically — Phase 0 confirmed).
        try:
            subprocess.run(
                _HERDR_CLI["server_start"],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

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

        Also performs version guard — warns (does not stop) if version != 0.8.2.
        """
        if shutil.which("herdr") is None:
            return False

        # Version guard.
        data = _herdr_run("version", [], timeout=5)
        if data is not None:
            # herdr --version outputs plain text like "herdr 0.8.2"
            pass
        try:
            r = subprocess.run(
                _HERDR_CLI["version"], capture_output=True, text=True, timeout=5
            )
            ver_line = (r.stdout + r.stderr).strip()
            # Expected: "herdr 0.8.2"
            ver = ver_line.split()[-1] if ver_line else ""
            if ver and ver != _HERDR_VERIFIED_VERSION:
                self._warn(
                    f"herdr version mismatch: expected {_HERDR_VERIFIED_VERSION}, got {ver!r}. "
                    "CLI interface may have changed."
                )
        except Exception:
            pass

        return self._ensure_server()

    def spawn(self, name: str, cmd: str, cwd: Optional[str] = None,
              env: Optional[dict] = None) -> bool:
        """Create a herdr tab named `name` and run `cmd`.

        Steps:
          1. Resolve workspace.
          2. tab create → root_pane.pane_id.
          3. pane rename <pane_id> <name>  (label does not auto-propagate).
          4. pane run <pane_id> <cmd>.
          5. Cache tab_id + pane_id.

        Returns False (without error) if a pane with this name already exists.
        """
        ws_id = self._workspace_id()
        if ws_id is None:
            self._warn(f"spawn {name!r}: could not resolve workspace")
            return False

        # Check for existing pane with this name.
        existing = _herdr_run("pane_list", ["--workspace", ws_id], timeout=10)
        if existing is not None:
            panes = existing.get("result", {}).get("panes", [])
            if any(p.get("label") == name for p in panes):
                return False  # Already exists → no-op

        # tab create.
        tab_args = ["--workspace", ws_id, "--label", name, "--no-focus"]
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
        _herdr_run("pane_rename", [pane_id, name], timeout=5)

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

        CALLER CONTRACT: capture() and wait for '❯' before calling send().
        Phase 0 confirmed: pane run submits reliably only after '❯' appears.
        Without '❯', the text is inserted but Enter is dropped.

        Enter insurance: after pane run, capture() is called once.  If `text`
        is still visible in the input line (meaning Enter was not submitted),
        one `pane send-keys enter` is appended.
        """
        ids = self._resolve_ids(name)
        if ids is None:
            self._warn(f"send {name!r}: pane not found")
            return False
        pane_id = ids["pane_id"]

        run_data = _herdr_run("pane_run", [pane_id, text], timeout=10)
        if run_data is None:
            self._warn(f"send {name!r}: pane run failed")
            return False

        # Enter insurance: check if text remains in the input line.
        time.sleep(0.1)
        screen = self._capture_by_pane_id(pane_id)
        if text in screen:
            # Text is still in input — append Enter.
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
        names = [p.get("label") for p in panes if p.get("label")]
        if suffix:
            names = [n for n in names if n.endswith(suffix)]
        return names

    def kill(self, name: str) -> bool:
        """Close the tab for the named pane (terminates claude and children).

        Phase 0: tab close terminates all child processes via SIGHUP within 2s.
        Cache is deleted on success.
        """
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

    def pid(self, name: str) -> Optional[int]:
        """Return the shell PID of the named pane via pane process-info."""
        ids = self._resolve_ids(name)
        if ids is None:
            self._warn(f"pid {name!r}: pane not found")
            return None
        pane_id = ids["pane_id"]

        data = _herdr_run("pane_process_info", [pane_id], timeout=10)
        if data is None:
            self._warn(f"pid {name!r}: pane process-info failed")
            return None
        try:
            shell_pid = data["result"]["process_info"]["shell_pid"]
            return int(shell_pid)
        except (KeyError, TypeError, ValueError) as e:
            self._warn(f"pid {name!r}: could not parse shell_pid: {e}")
            return None

    def attach(self, name: str) -> bool:
        """Attach / focus the named pane's tab.

        If HERDR_ENV=1 (already inside herdr), use tab focus.
        Otherwise, herdr does not support nested attach: print a hint to stderr
        and return False (start.sh continues regardless of return value).
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
        else:
            print(
                f"[mux:herdr] herdr を実行すると {name!r} の画面に入れます。",
                file=sys.stderr,
            )
            return False


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

    def spawn(self, name: str, cmd: str, cwd: Optional[str] = None,
              env: Optional[dict] = None) -> bool:
        return self._backend.spawn(name, cmd, cwd=cwd, env=env)

    def send(self, name: str, text: str) -> bool:
        return self._backend.send(name, text)

    def capture(self, name: str) -> str:
        return self._backend.capture(name)

    def list(self, suffix: Optional[str] = None) -> List[str]:
        return self._backend.list(suffix=suffix)

    def kill(self, name: str) -> bool:
        return self._backend.kill(name)

    def pid(self, name: str) -> Optional[int]:
        return self._backend.pid(name)

    def attach(self, name: str) -> bool:
        return self._backend.attach(name)


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
        if not rest:
            print("Usage: lib_mux.py kill <name>", file=sys.stderr)
            return 2
        return 0 if m.kill(rest[0]) else 1

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

    else:
        print(f"Unknown verb: {verb!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv[1:]))
