#!/usr/bin/env python3
"""Rex 配線 (rex-mux-backend.md §11 PR-2) のテスト。

本番の既定 socket の server は起動も停止も接続もしない。偽の `rex` (FakeRex) か
純粋関数だけを使う。本物の server が要る確認 (再起動後の husk) は
`test_rex_backend_real_server.py` 側。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from rex_harness import FakeRex, make_checkout, status_json

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import lib_mux  # noqa: E402
import lib_daemon_watch  # noqa: E402

REPO = SCRIPTS.parent


# ---------------------------------------------------------------------------
# state(): capture ベース (§10.2-4)
# ---------------------------------------------------------------------------

RULE = "─" * 40
IDLE_SCREEN = f"some output\n{RULE}\n❯ \n{RULE}\n  ? for shortcuts\n"
WORKING_SCREEN = ("✻ Thinking… (12s · esc to interrupt)\n\n"
                  f"{RULE}\n❯ \n{RULE}\n")
PERMISSION_SCREEN = ("Bash command\n  ls\n\nDo you want to proceed?\n"
                     "❯ 1. Yes\n  2. No, and tell Claude what to do differently\n\n"
                     "Esc to cancel\n")
TRUST_SCREEN = ("Do you trust the files in this folder?\n"
                "❯ 1. Yes, I trust this folder\n  2. No, exit\n\nEnter to confirm\n")


@pytest.mark.parametrize("screen,expected", [
    (IDLE_SCREEN, "idle"),
    (WORKING_SCREEN, "working"),
    (PERMISSION_SCREEN, "blocked"),
    (TRUST_SCREEN, "blocked"),
    # 承認ダイアログの下にスピナーが残っていても blocked が勝つ
    (WORKING_SCREEN + PERMISSION_SCREEN, "blocked"),
])
def test_screen_state_recognises_the_shapes(screen, expected):
    assert lib_mux.rex_screen_state(screen) == expected


@pytest.mark.parametrize("screen", [
    "", "   \n\n", None, 42,
    "just a shell prompt\n$ ",
    # task の出力が選択肢風の行を引用しただけ (フッタが無い) → blocked にしない
    "grep result:\n❯ 1. Yes\nnothing else\n",
    # 入力欄が無い (起動前・全画面表示) → idle と言い切らない
    "Loading...\n",
])
def test_screen_state_unreadable_or_unrecognised_is_unknown(screen):
    assert lib_mux.rex_screen_state(screen) == "unknown"


def test_state_uses_the_capture_and_an_empty_capture_is_unknown(monkeypatch):
    b = lib_mux.RexBackend()
    monkeypatch.setattr(b, "capture", lambda name: PERMISSION_SCREEN)
    assert b.state("Zed-worker") == "blocked"
    monkeypatch.setattr(b, "capture", lambda name: "")   # 読めなかった = idle ではない
    assert b.state("Zed-worker") == "unknown"


# ---------------------------------------------------------------------------
# server の env: このマシン固有の穴 (§10.2-7)
# ---------------------------------------------------------------------------

def test_server_env_drops_session_variables_and_ssh_triggers(monkeypatch):
    for k, v in {"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22", "SSH_CLIENT": "x",
                 "SSH_TTY": "/dev/pts/9", "CREWVIA_MUX": "rex",
                 "CLAUDE_CODE_X": "1", "TASKVIA_TOKEN": "secret",
                 "HERDR_ENV": "1", "KEEP_ME": "yes"}.items():
        monkeypatch.setenv(k, v)
    env = lib_mux._rex_server_env()
    for gone in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "CREWVIA_MUX",
                 "CLAUDE_CODE_X", "TASKVIA_TOKEN", "HERDR_ENV"):
        assert gone not in env, gone
    assert env["KEEP_ME"] == "yes" and "PATH" in env


def test_server_command_default_is_rex_server_start(monkeypatch):
    assert lib_mux._rex_server_command(None) == ["rex", "server", "start"]


def test_server_command_explicit_endpoint_uses_run_listen(monkeypatch):
    monkeypatch.setattr(lib_mux, "_test_isolation_active", lambda: False)
    cmd = lib_mux._rex_server_command("unix:///tmp/x/s.sock")
    assert cmd[:5] == ["rex", "server", "run", "--listen", "unix:///tmp/x/s.sock"]
    assert "--restore=false" not in cmd            # 本番は復元する (§10.2-2)


def test_server_command_under_isolation_has_own_state_dir_and_no_restore(monkeypatch):
    monkeypatch.setattr(lib_mux, "_test_isolation_active", lambda: True)
    cmd = lib_mux._rex_server_command("unix:///tmp/x/s.sock")
    assert "--state-dir" in cmd and "/tmp/x/state" in cmd and "--restore=false" in cmd


def test_start_server_launches_nothing_when_the_endpoint_is_refused(monkeypatch):
    monkeypatch.setattr(lib_mux, "_rex_endpoint",
                        lambda: (False, None, "refused for the test"))

    def boom(*a, **k):
        raise AssertionError("a server was launched")
    monkeypatch.setattr(lib_mux.subprocess, "Popen", boom)
    assert lib_mux._rex_start_server() == "refused for the test"


def test_start_server_passes_the_cleaned_env(monkeypatch):
    monkeypatch.setattr(lib_mux, "_rex_endpoint", lambda: (True, None, ""))
    monkeypatch.setattr(lib_mux.shutil, "which", lambda n: "/usr/bin/rex")
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    seen = {}

    def fake_popen(cmd, **kw):
        seen.update(cmd=cmd, **kw)
    monkeypatch.setattr(lib_mux.subprocess, "Popen", fake_popen)
    assert lib_mux._rex_start_server() is None
    assert seen["cmd"] == ["rex", "server", "start"]
    assert "SSH_CONNECTION" not in seen["env"]
    assert seen["start_new_session"] is True


# ---------------------------------------------------------------------------
# config rex.endpoint
# ---------------------------------------------------------------------------

def _cfg(tmp_path, text):
    p = tmp_path / "crewvia.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_config_without_rex_block_uses_rex_default(tmp_path):
    assert lib_mux._rex_config_endpoint(_cfg(tmp_path, "mode: rex\n")) == (True, None, "")


def test_config_endpoint_is_read_from_the_rex_block_only(tmp_path):
    other = "telegram:\n  endpoint: unix:///tmp/not-me.sock\nmode: rex\n"
    assert lib_mux._rex_config_endpoint(_cfg(tmp_path, other)) == (True, None, "")
    mine = "mode: rex\nrex:\n  endpoint: unix:///tmp/me.sock  # note\n"
    assert lib_mux._rex_config_endpoint(_cfg(tmp_path, mine)) == (
        True, "unix:///tmp/me.sock", "")


def test_config_endpoint_over_108_bytes_or_not_unix_is_refused(tmp_path):
    long = "unix:///tmp/" + "a" * 120 + ".sock"
    ok, ep, why = lib_mux._rex_config_endpoint(_cfg(tmp_path, f"rex:\n  endpoint: {long}\n"))
    assert not ok and ep is None and "108" in why
    ok, ep, why = lib_mux._rex_config_endpoint(
        _cfg(tmp_path, "rex:\n  endpoint: http://127.0.0.1:9\n"))
    assert not ok and "unix://" in why


def test_unreadable_config_is_refused_not_defaulted(tmp_path):
    d = tmp_path / "crewvia.yaml"
    d.mkdir()                                   # 通常ファイルでない = 読めない
    ok, ep, why = lib_mux._rex_config_endpoint(d)
    assert not ok and "could not be read" in why


# ---------------------------------------------------------------------------
# ensure-server
# ---------------------------------------------------------------------------

def _status(revision=9):
    return {"endpoint": "unix:///x", "pid": 99, "instance_id": "i", "api_revision": revision}


def test_ensure_server_uses_a_running_server_and_only_warns(monkeypatch):
    monkeypatch.setattr(lib_mux, "_rex_status", lambda timeout=5.0: _status())
    monkeypatch.setattr(lib_mux, "_rex_start_server",
                        lambda: pytest.fail("must not start a second server"))
    ok, msg = lib_mux.rex_ensure_server()
    assert ok and "WARNING" in msg and "already running" in msg


def test_ensure_server_rejects_an_unmeasured_api_revision(monkeypatch):
    monkeypatch.setattr(lib_mux, "_rex_status", lambda timeout=5.0: _status(revision=10))
    ok, msg = lib_mux.rex_ensure_server()
    assert not ok and "api_revision 10" in msg


def test_ensure_server_starts_one_and_verifies(monkeypatch):
    answers = iter([None, None, _status()])
    monkeypatch.setattr(lib_mux, "_rex_status", lambda timeout=5.0: next(answers))
    monkeypatch.setattr(lib_mux, "_rex_start_server", lambda: None)
    monkeypatch.setattr(lib_mux.time, "sleep", lambda s: None)
    ok, msg = lib_mux.rex_ensure_server()
    assert ok and msg == "rex server started"


def test_ensure_server_reports_a_launch_failure_and_a_server_that_never_answers(monkeypatch):
    monkeypatch.setattr(lib_mux, "_rex_status", lambda timeout=5.0: None)
    monkeypatch.setattr(lib_mux, "_rex_start_server", lambda: "no rex binary")
    assert lib_mux.rex_ensure_server() == (False, "no rex binary")
    monkeypatch.setattr(lib_mux, "_rex_start_server", lambda: None)
    ticks = iter([0.0, 0.0, 99.0])
    monkeypatch.setattr(lib_mux.time, "time", lambda: next(ticks))
    monkeypatch.setattr(lib_mux.time, "sleep", lambda s: None)
    ok, msg = lib_mux.rex_ensure_server(wait_seconds=1.0)
    assert not ok and "did not answer" in msg


def _cli(args, env_extra):
    env = {**os.environ, **env_extra}
    env.pop("AGENT_NAME", None)
    return subprocess.run([sys.executable, str(SCRIPTS / "lib_mux.py"), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_cli_ensure_server_against_a_fake_running_server(tmp_path, monkeypatch):
    fake = FakeRex(tmp_path, monkeypatch)
    fake.set([{"match": ["server", "status"],
               "out": status_json(pid=os.getpid()), "rc": 0}])
    r = _cli(["ensure-server"], {"CREWVIA_MUX": "rex",
                                 "CREWVIA_MUX_TEST_ISOLATION": "1",
                                 "CREWVIA_REX_SOCK": fake.sock})
    assert r.returncode == 0, r.stderr
    assert "already running" in r.stderr
    # 起動系の verb は 1 回も呼ばれない (既に居る server を作り直さない)
    assert not [c for c in fake.calls() if c[:2] in (["server", "start"], ["server", "run"])]


def test_cli_ensure_server_fails_closed_on_an_unmeasured_revision(tmp_path, monkeypatch):
    fake = FakeRex(tmp_path, monkeypatch)
    fake.set([{"match": ["server", "status"],
               "out": status_json(pid=os.getpid(), revision=3), "rc": 0}])
    r = _cli(["ensure-server"], {"CREWVIA_MUX": "rex",
                                 "CREWVIA_MUX_TEST_ISOLATION": "1",
                                 "CREWVIA_REX_SOCK": fake.sock})
    assert r.returncode == 1 and "api_revision 3" in r.stderr


# ---------------------------------------------------------------------------
# デーモン: respawn のコマンド / dispatcher の起動案内
# ---------------------------------------------------------------------------

def test_daemon_respawn_command_carries_the_rex_destination(tmp_path):
    root = make_checkout(tmp_path)
    cmd = lib_daemon_watch.spawn_command(
        "dispatcher", root,
        env={"CREWVIA_MUX": "rex", "CREWVIA_REX_SESSION": "alt"})
    assert "export CREWVIA_MUX='rex'" in cmd
    assert "export CREWVIA_REX_SESSION='alt'" in cmd
    # テスト専用の socket 上書きは、設定されていればそのまま運ばれる (herdr の SOCK と同じ)
    cmd2 = lib_daemon_watch.spawn_command(
        "watchdog", root, env={"CREWVIA_MUX": "rex", "CREWVIA_REX_SOCK": "/tmp/t/s"})
    assert "export CREWVIA_REX_SOCK='/tmp/t/s'" in cmd2


def test_dispatcher_does_not_default_the_worker_start_command_to_herdr():
    text = (SCRIPTS / "dispatcher.sh").read_text(encoding="utf-8")
    assert "os.environ.get('CREWVIA_MUX') or 'herdr'" not in text
    assert "_lib_mux._select_backend().BACKEND_NAME" in text


def test_rex_server_env_denies_ssh_triggers_for_the_tmux_attach_husk():
    # ~/.bashrc の tmux 自動 attach の条件 (SSH_CONNECTION あり・HERDR_ENV なし) を server が持たない
    assert {"SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"} <= lib_mux._REX_SERVER_ENV_DENY_EXTRA


# ---------------------------------------------------------------------------
# 入口スクリプト: rex の分岐がある (herdr 専用の箇所は触っていない)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,needles", [
    ("crewvia", ["rex)", "ensure-server", "CREWVIA_MUX=tmux|herdr|rex"]),
    ("scripts/start.sh", ['"rex"', "ensure-server", "mode: rex"]),
    ("scripts/crewvia-stop.sh", ["rex server は停止しません"]),
    ("scripts/setup-new-env.sh", ["command -v rex"]),
])
def test_entry_scripts_know_rex(path, needles):
    text = (REPO / path).read_text(encoding="utf-8")
    for n in needles:
        assert n in text, (path, n)


@pytest.mark.parametrize("path", ["crewvia", "scripts/start.sh", "scripts/crewvia-stop.sh",
                                  "scripts/setup-new-env.sh", "scripts/dispatcher.sh"])
def test_edited_shell_scripts_parse(path):
    r = subprocess.run(["bash", "-n", str(REPO / path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
