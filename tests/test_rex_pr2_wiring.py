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


# ---------------------------------------------------------------------------
# r1 A: start.sh は実効 backend を最初に 1 回だけ解決する (env > config mode: > auto)
# ---------------------------------------------------------------------------
#
# start.sh 本体は claude の起動まで進むので、「並列モード選択」のブロック (本物の
# start.sh から切り出す) だけを、偽の lib_mux.py / rex / herdr と一緒に bash で走らせる。
# lib_mux の単体では、export と検査が env と config で割れる事故 (QA F1) を捕まえられない。

def _mode_block():
    text = (SCRIPTS / "start.sh").read_text(encoding="utf-8")
    a = text.index('if [[ "${ROLE}" == "director" ]] && [[ -z "${CREWVIA_MUX_ENABLED:-}" ]]; then')
    b = text.index("export AGENT_NAME\nexport ROLE", a)
    return text[a:b]


def _run_mode_block(tmp_path, *, env_mux, config_mode, have=("rex", "herdr"), ensure_rc=0):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    log = tmp_path / "calls.log"
    (scripts / "lib_mux.py").write_text(
        "import os, sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + ' MUX=' + os.environ.get('CREWVIA_MUX', '') + '\\n')\n"
        f"sys.exit({ensure_rc} if sys.argv[1] == 'ensure-server' else 0)\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for b in have:
        f = bindir / b
        f.write_text("#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    cfg = tmp_path / "crewvia.yaml"
    cfg.write_text((f"mode: {config_mode}\n" if config_mode else "") + "other: 1\n")
    script = tmp_path / "block.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"SCRIPT_DIR={scripts}\nCONFIG_FILE={cfg}\nROLE=director\n"
        + _mode_block() +
        'echo "RESULT MUX=${CREWVIA_MUX:-} ENABLED=${CREWVIA_MUX_ENABLED:-}"\n')
    env = {"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin", "HOME": str(tmp_path)}
    if env_mux:
        env["CREWVIA_MUX"] = env_mux
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       env=env, stdin=subprocess.DEVNULL)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


@pytest.mark.parametrize("env_mux,config_mode,want_mux,want_calls", [
    # 本番の現状 (mode: herdr) でも env だけで rex に切り替わる。herdr の検査はしない
    ("rex", "herdr", "rex", ["ensure-server MUX=rex"]),
    # config が rex でも env=tmux なら tmux。rex の server は起動しない
    ("tmux", "rex", "tmux", []),
    # config が rex でも env=herdr なら herdr。rex の server は起動しない
    ("herdr", "rex", "herdr", ["available MUX=herdr", "available MUX=herdr"]),
    # env == config の既存の動作は変えない
    ("tmux", "tmux", "tmux", []),
    ("herdr", "herdr", "herdr", ["available MUX=herdr", "available MUX=herdr"]),
    # env 無しなら config
    (None, "rex", "rex", ["ensure-server MUX=rex"]),
])
def test_start_sh_resolves_the_effective_backend_once(tmp_path, env_mux, config_mode,
                                                      want_mux, want_calls):
    r, calls = _run_mode_block(tmp_path, env_mux=env_mux, config_mode=config_mode)
    assert r.returncode == 0, r.stderr
    assert f"RESULT MUX={want_mux} ENABLED=1" in r.stdout, r.stdout
    assert calls == want_calls
    # 実効でない backend の案内を出さない
    other = "herdr" if want_mux == "rex" else "rex"
    assert f"{other} 並列モード" not in r.stdout


def test_start_sh_rex_server_failure_exits_1_without_falling_back(tmp_path):
    r, calls = _run_mode_block(tmp_path, env_mux="rex", config_mode="herdr", ensure_rc=1)
    assert r.returncode == 1
    assert "rex server を使えません" in r.stderr
    assert "RESULT" not in r.stdout
    assert not [c for c in calls if c.startswith("available")]   # herdr に落ちない


# ---------------------------------------------------------------------------
# r1 B: 番号なしの信頼確認ダイアログ (実物の capture の形) と kickoff の誤報告
# ---------------------------------------------------------------------------

import lib_trust  # noqa: E402

#: 実 Claude (2.1.316) が未信頼 cwd で出した画面 (QA t002 F2 の capture)。選択肢に番号が無い。
REAL_TRUST_SCREEN = (
    " Accessing workspace:\n\n /tmp/qa/checkout/.claude/worktrees/x\n\n"
    " Quick safety check: Is this a project you created or one you trust? (Like your own\n"
    " code, a well-known open source project, or work from your team). If not, take a\n"
    " moment to review what's in this folder first.\n\n"
    " ❯ No, exit\n   Yes, I trust this folder\n\n"
    " Enter to confirm · Esc to cancel\n"
)


def test_unnumbered_trust_dialog_is_detected_by_both_detectors():
    assert lib_trust.screen_shows_trust_dialog(REAL_TRUST_SCREEN)
    assert lib_mux.rex_screen_state(REAL_TRUST_SCREEN) == "blocked"
    # カーソルが Yes 側でも同じ
    flipped = REAL_TRUST_SCREEN.replace("❯ No, exit\n   Yes", "  No, exit\n ❯ Yes")
    assert lib_trust.screen_shows_trust_dialog(flipped)
    assert lib_mux.rex_screen_state(flipped) == "blocked"


def test_numbered_trust_dialog_is_still_detected():
    assert lib_trust.screen_shows_trust_dialog(TRUST_SCREEN)
    assert lib_mux.rex_screen_state(TRUST_SCREEN) == "blocked"


@pytest.mark.parametrize("screen", [
    # 文言がパスにあるだけ・隣に権限確認メニュー (メニュー自体は blocked だが trust ではない)
    "Working directory: /tmp/No, exit\n" + PERMISSION_SCREEN,
    "cwd: /tmp/Yes, I trust this folder\n" + IDLE_SCREEN,
    "No, exit\n" + "x" * 200 + "\nYes, I trust this folder\n",
])
def test_trust_words_alone_are_not_a_trust_dialog(screen):
    assert not lib_trust.screen_shows_trust_dialog(screen)


@pytest.mark.parametrize("screen", [
    "cwd: /tmp/Yes, I trust this folder\n" + IDLE_SCREEN,
    "No, exit\nYes, I trust this folder\n" + IDLE_SCREEN,   # フッタ (Enter to confirm) が無い
])
def test_trust_words_alone_do_not_make_a_pane_blocked(screen):
    assert lib_mux.rex_screen_state(screen) != "blocked"


def test_start_sh_does_not_call_a_failed_send_verified():
    text = (SCRIPTS / "start.sh").read_text(encoding="utf-8")
    assert '[[ "$_SEND_OK" -eq 1 ]] && mux_verify_sent' in text
    assert "mux_send \"$WINDOW_NAME\" \"$KICKOFF_MSG\" >/dev/null || true" not in text


# ---------------------------------------------------------------------------
# r1 C / D
# ---------------------------------------------------------------------------

def test_rex_server_env_drops_every_herdr_variable(monkeypatch):
    for k in ("HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID",
              "HERDR_BIN_PATH", "HERDR_SOCKET_PATH", "HERDR_FUTURE_VAR"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("KEEP_ME", "1")
    env = lib_mux._rex_server_env()
    assert not [k for k in env if k.startswith("HERDR_")]
    assert env.get("KEEP_ME") == "1"


def _stop_script_in(tmp_path, repo_dir_name):
    """crewvia-stop.sh を、' を含む path の偽 checkout で --full --dry-run する。"""
    root = tmp_path / repo_dir_name
    (root / "scripts").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "crewvia.yaml").write_text("other: 1\n")
    # import できない lib_mux = backend を判定できない
    (root / "scripts" / "lib_mux.py").write_text("raise RuntimeError('boom')\n")
    (root / "scripts" / "crewvia-stop.sh").write_text(
        (SCRIPTS / "crewvia-stop.sh").read_text(encoding="utf-8"))
    return root


def test_stop_script_passes_the_path_by_argv_and_never_falls_back_to_herdr():
    text = (SCRIPTS / "crewvia-stop.sh").read_text(encoding="utf-8")
    assert "sys.path.insert(0, '${REPO_ROOT}" not in text
    assert 'sys.argv[1]' in text
    assert '-z "$_STOP_BACKEND"' in text


def test_stop_full_with_unresolvable_backend_stops_no_server(tmp_path):
    # 判定の python が失敗する (lib_mux が import できない) 偽 checkout。herdr は PATH に無くても
    # 「止めない」旨のメッセージで分かる。path に ' を含めても壊れない。
    root = _stop_script_in(tmp_path, "it's a checkout")
    r = subprocess.run(["bash", str(root / "scripts" / "crewvia-stop.sh"), "--full", "--dry-run"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    out = r.stdout + r.stderr
    assert "server は停止しません" in out, out
    assert "herdr server stop..." not in out
    assert "[dry-run] herdr server stop" not in out
