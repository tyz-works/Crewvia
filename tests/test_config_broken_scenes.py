#!/usr/bin/env python3
"""壊れた local / 共通 config の場面 (デーモン / hook / review-plan)。
knowledge/machine-local-config.md §3, §6。PR-2 レビュー (#360 t014) の P2-1 / P2-2 / P3-1 / P3-2 に対応。

    python3 -m pytest tests/test_config_broken_scenes.py -v

- dispatcher: 通知の往復 (見送り・一度だけ・回復で clear・壊れ直したらまた通知) と、
  CREWVIA_MUX が env に無く `Mux()` 自体が投げる場面 (P2-1。traceback で終わらない)
- watchdog: import で死なない・何も終了させずに待つ
- hooks/pre-tool-use.sh: 壊れた config は deny / 読めれば config の ntfy モードを選ぶ (python は 1 回)
- review-plan.sh: 壊れたら exit 1
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import lib_config  # noqa: E402
import lib_mux  # noqa: E402
from fixture_tree import copy_plan_tree  # noqa: E402
from test_dispatcher_notify_once import FakeMux, Harness  # noqa: E402

GOOD = "mode: tmux\napproval_channel:\n  mode: ntfy\n  ntfy:\n    url: http://ntfy.invalid\n    topic: tp\n"
BROKEN = "mode: [unclosed\n"


def _env_without_overrides() -> dict:
    env = dict(os.environ)
    for k in ("CREWVIA_MUX", "CREWVIA_MUX_TEST_ISOLATION", "CREWVIA_APPROVAL_CHANNEL", "NTFY_URL",
              "NTFY_TOPIC", "NTFY_USER", "NTFY_PASS", "APPROVAL_TOKEN_TTL_SECONDS", "AGENT_NAME"):
        env.pop(k, None)
    return env


def _write_config(root: Path, common: str = GOOD, local: str | None = None) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "crewvia.yaml").write_text(common, encoding="utf-8")
    lp = root / "config" / "crewvia.local.yaml"
    if local is None:
        lp.unlink(missing_ok=True)
    else:
        lp.write_text(local, encoding="utf-8")


@pytest.fixture()
def h(tmp_path, monkeypatch):
    monkeypatch.delenv("CREWVIA_MUX_TEST_ISOLATION", raising=False)   # local を読む場面を試す
    monkeypatch.delenv("CREWVIA_MUX", raising=False)
    harness = Harness(tmp_path / "repo", monkeypatch)
    # Harness は subprocess.Popen を差し替える (git が呼べない)。local の置き場所は repo root そのもの
    monkeypatch.setattr(lib_config, "find_main_checkout", lambda r: Path(r))
    _write_config(harness.root)
    return harness


# --- dispatcher: 通知の往復 --------------------------------------------------

def _config_msgs(msgs):
    return [m for m in msgs if "[config]" in m]


def test_dispatcher_config_unreadable_is_told_once_and_cleared_on_recovery(h):
    assert _config_msgs(h.cycle()) == []                       # 読める: 何も送らない
    _write_config(h.root, local=BROKEN)
    first = _config_msgs(h.cycle())
    assert len(first) == 1
    assert "local_malformed" in first[0] or "malformed" in first[0]
    assert "mode: [unclosed" not in first[0]                    # 元の行・値は載せない
    for _ in range(3):                                          # 同じ壊れは一度だけ (スロットルが切れても)
        assert _config_msgs(h.cycle(ttl_expired=True)) == []
    assert "config を読めない" in h.log_text()                  # 見送りは log に残る

    _write_config(h.root, local=None)                           # 回復 → 台帳が畳まれる
    assert _config_msgs(h.cycle()) == []
    told = json.loads(h.told_file.read_text()) if h.told_file.exists() else {}
    assert "config_unreadable" not in json.dumps(told)

    _write_config(h.root, local=BROKEN)                         # 同じ障害の 2 回目が黙らない
    assert len(_config_msgs(h.cycle(ttl_expired=True))) == 1


def test_dispatcher_skips_assignments_while_config_is_unreadable(h):
    h.card("t001", "pending")
    _write_config(h.root, local=BROKEN)
    h.cycle()
    assert h.spawned == []
    assert not (h.queue / "assignments").exists() or not any((h.queue / "assignments").iterdir())


# --- P2-1: Mux() 自体が投げる (CREWVIA_MUX が env に無い) -----------------------

def test_dispatcher_module_survives_mux_config_unreadable(h, monkeypatch):
    def boom(*a, **kw):
        raise lib_mux.MuxConfigUnreadable("local_malformed (line 1)")
    monkeypatch.setattr(lib_mux, "Mux", boom)
    _write_config(h.root, local=BROKEN)
    h.cycle_error = None
    src = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", (SCRIPTS / "dispatcher.sh").read_text(), re.DOTALL).group(1)
    src = re.sub(r"\n# --- CYCLE ENTRY POINT ---\n.*$", "", src, flags=re.S)
    monkeypatch.setattr(sys, "argv", ["dispatcher", str(h.queue), str(h.registry),
                                      str(h.notify_cache), "300", "60", str(h.log)])
    ns = {"__name__": "dispatcher_under_test"}
    exec(compile(src, "dispatcher.sh (embedded, test)", "exec"), ns)    # 以前はここで MuxConfigUnreadable
    assert ns["_MUX_CONFIG_PROBLEM"] == "local_malformed (line 1)"
    assert ns["_mux"].list(suffix="-director") == []
    ns["dispatch"]()                                                    # gate が見送って静かに戻る


def test_dispatcher_whole_cycle_has_no_traceback_when_mux_config_unreadable(tmp_path):
    root = tmp_path / "repo"
    # lib_mux の Mux() は自分の scripts/ の親の config を読む: scripts を repo に写して同じ root にする
    copy_plan_tree(root)
    for d in ("registry", "queue/missions", "queue/archive"):
        (root / d).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _write_config(root, local=BROKEN)
    src = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", (SCRIPTS / "dispatcher.sh").read_text(), re.DOTALL).group(1)
    prog = tmp_path / "cycle.py"
    prog.write_text(src, encoding="utf-8")
    env = _env_without_overrides()
    env["CREWVIA_TASKVIA"] = "disabled"
    r = subprocess.run([sys.executable, str(prog), str(root / "queue"), str(root / "registry"),
                        str(tmp_path / "cache.json"), "300", "60", str(tmp_path / "d.log")],
                       capture_output=True, text=True, env=env, cwd=str(root), timeout=120)
    assert "Traceback" not in r.stderr, r.stderr[-2000:]
    assert r.returncode == 0, r.stderr[-2000:]
    assert "config unreadable" in r.stderr


# --- watchdog ------------------------------------------------------------------

def test_watchdog_imports_and_waits_when_mux_config_unreadable(tmp_path):
    root = tmp_path / "repo"
    copy_plan_tree(root)
    (root / "scripts" / "watchdog.py").write_bytes((SCRIPTS / "watchdog.py").read_bytes())
    (root / "registry").mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _write_config(root, local=BROKEN)
    env = _env_without_overrides()
    code = (
        "import sys; sys.path.insert(0, 'scripts'); import watchdog as w;"
        "print('PROBLEM', w._MUX_CONFIG_PROBLEM); print('MUX', w._mux)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                       cwd=str(root), timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]            # 以前は import で MuxConfigUnreadable
    assert "PROBLEM local_malformed" in r.stdout or "PROBLEM " in r.stdout and "None" not in r.stdout.split("PROBLEM")[1].split("\n")[0]
    assert "MUX None" in r.stdout
    _write_config(root, local=None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                       cwd=str(root), timeout=60)
    assert "PROBLEM None" in r.stdout


# --- hooks/pre-tool-use.sh -------------------------------------------------------

def _hook_tree(tmp_path: Path, local: str | None) -> Path:
    root = tmp_path / "repo"
    copy_plan_tree(root)
    (root / "hooks").mkdir()
    for f in (REPO / "hooks").iterdir():
        if f.is_file():
            shutil.copy(f, root / "hooks" / f.name)
    (root / "registry").mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _write_config(root, local=local)
    return root


def _run_hook(root: Path) -> subprocess.CompletedProcess:
    env = _env_without_overrides()
    env.update({"CREWVIA_REPO": str(root), "AGENT_NAME": "Wei", "CREWVIA_TASKVIA": "enabled",
                "TASKVIA_TOKEN": "tok-test", "TASKVIA_URL": "http://127.0.0.1:9", "TASK_ID": "t001"})
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x", "content": "x"}})
    return subprocess.run(["bash", str(root / "hooks" / "pre-tool-use.sh")], input=payload,
                          capture_output=True, text=True, env=env, cwd=str(root), timeout=60)


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が必要")
def test_pre_tool_use_denies_when_config_is_unreadable(tmp_path):
    r = _run_hook(_hook_tree(tmp_path, BROKEN))
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "config-unreadable" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が必要")
def test_approval_channel_reads_config_with_one_python_start(tmp_path):
    root = _hook_tree(tmp_path, None)
    shim = tmp_path / "bin"
    shim.mkdir()
    log = tmp_path / "py.log"
    real = shutil.which("python3")
    (shim / "python3").write_text(f'#!/bin/bash\necho x >> "{log}"\nexec "{real}" "$@"\n')
    (shim / "python3").chmod(0o755)
    env = _env_without_overrides()
    env.update({"CREWVIA_REPO": str(root), "PATH": f"{shim}:{env['PATH']}"})
    r = subprocess.run(
        ["bash", "-c", f'. "{root}/hooks/lib_approval_channel.sh"; load_ntfy_config; '
                       'echo "$NTFY_URL $NTFY_TOPIC $APPROVAL_TOKEN_TTL_SECONDS"; get_approval_channel_mode'],
        capture_output=True, text=True, env=env, timeout=60)
    assert r.stdout.split("\n")[:2] == ["http://ntfy.invalid tp 900", "ntfy"], r.stderr
    assert len(log.read_text().splitlines()) == 1             # 以前は 6 回


# --- review-plan.sh -------------------------------------------------------------

def test_review_plan_stops_on_unreadable_config(tmp_path):
    root = tmp_path / "repo"
    copy_plan_tree(root)
    (root / "scripts" / "review-plan.sh").write_bytes((SCRIPTS / "review-plan.sh").read_bytes())
    (root / "scripts" / "review-plan.sh").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _write_config(root, local=BROKEN)
    (root / "queue" / "missions" / "m1").mkdir(parents=True)
    r = subprocess.run(["bash", str(root / "scripts" / "review-plan.sh"), "m1"],
                       capture_output=True, text=True, env=_env_without_overrides(), cwd=str(root), timeout=60)
    assert r.returncode == 1
    assert "読めません" in r.stderr
