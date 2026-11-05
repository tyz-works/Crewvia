#!/usr/bin/env python3
"""dispatcher.sh の IRC 中継の差し込み (t007)。本物のコードを dispatcher.sh から**そのまま切り出して**確かめる。

* python 側: `_irc_notices()` / `run_irc_cycle()` (状態ベースの通知・回復で clear・未設定なら何もしない)。dispatcher.sh の埋め込み python から
  IRC のブロックだけを切り出して、スタブの名前空間で exec する (全体を exec するとサイクルが回ってしまう)。
* bash 側: `_irc_resolve` / `_irc_maybe_retry` (起動時に 1 回・失敗の間は長い間隔でだけ取り直す・token は export しない)。
  認証情報の運搬は前置代入で、`env` コマンドを使わない (静的 + 実行時)。

実行: python3 -m pytest tests/test_irc_dispatcher_glue.py -q
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
REPO = THIS_DIR.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(THIS_DIR))

import lib_irc_relay as L  # noqa: E402
from lib_task_cards import is_unreadable  # noqa: E402
from irc_fake_relay import FakeRelay  # noqa: E402

DISPATCHER = (SCRIPTS / "dispatcher.sh").read_text(encoding="utf-8")
TOKEN = "G" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# 静的: 差し込みの形
# ---------------------------------------------------------------------------

def test_dispatcher_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPTS / "dispatcher.sh")], check=True)


def run_dispatch_body():
    m = re.search(r"^run_dispatch\(\) \{\n(.*?)\n  python3 - ", DISPATCHER, re.S | re.M)
    assert m
    return m.group(1)


def test_token_is_carried_by_prefix_assignment_not_env_command_and_not_exported():
    body = run_dispatch_body()
    assert '_CREWVIA_IRC_RESOLVED_TOKEN="$irc_token"' in body
    assert "env " not in body.replace("# ", "#") or not re.search(r"^\s*env\s", body, re.M)
    assert not re.search(r"\bexport\b[^\n]*irc_token", DISPATCHER)
    assert not re.search(r"\benv\b\s+_CREWVIA_IRC", DISPATCHER)
    assert "declare -x irc_token" not in DISPATCHER


def test_python_pops_the_carried_variables_from_os_environ_immediately():
    assert "os.environ.pop('_CREWVIA_IRC_RESOLVED_TOKEN', '')" in DISPATCHER
    assert "os.environ.pop('_CREWVIA_IRC_RESOLVE_REASON', '')" in DISPATCHER
    pop_at = DISPATCHER.index("os.environ.pop('_CREWVIA_IRC_RESOLVED_TOKEN'")
    assert pop_at < DISPATCHER.index("from lib_mux import Mux")          # subprocess を使う import より前


def test_irc_cycle_runs_right_after_telegram_and_before_ci_poll_and_the_loop_retries_credentials():
    tail = DISPATCHER[DISPATCHER.index("run_daemon_watch()\ncheck_main_checkout_drift()"):]
    assert tail.index("run_telegram_cycle()\n") < tail.index("run_irc_cycle()\n") < tail.index("run_ci_poll()\n")
    loop = DISPATCHER[DISPATCHER.index("while true; do"):]
    assert "  _irc_maybe_retry\n" in loop


def test_irc_secrets_are_not_added_to_the_respawn_env_allowlist():
    watch = (SCRIPTS / "lib_daemon_watch.py").read_text(encoding="utf-8")
    assert "IRC" not in watch


# ---------------------------------------------------------------------------
# bash: 認証情報の取り出し
# ---------------------------------------------------------------------------

def bash_block():
    start = DISPATCHER.index("IRC_RESOLVE_RETRY_SECONDS=600")
    end = DISPATCHER.index("_irc_resolve\nif [[")
    return DISPATCHER[start:end]


def run_bash(tmp_path, stub_output, script):
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    stub = scripts / "lib_irc_relay.py"
    stub.write_text("import sys\nsys.stdout.write(" + repr(stub_output) + ")\n"
                    "open(sys.argv[0] + '.calls', 'a').write('x')\n")
    prog = f'SCRIPT_DIR="{scripts}"\nlog() {{ echo "LOG: $*"; }}\n{bash_block()}\n{script}\n'
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env={**os.environ, "PATH": os.environ["PATH"]})


def calls(tmp_path):
    f = tmp_path / "scripts" / "lib_irc_relay.py.calls"
    return len(f.read_text()) if f.exists() else 0


def test_resolve_keeps_token_in_a_shell_variable_only(tmp_path):
    proc = run_bash(tmp_path, f"ok\n{TOKEN}\n", '_irc_resolve\necho "reason=$irc_reason"\n[ "$irc_token" = "' + TOKEN + '" ] && echo held\nenv | grep -c irc_token || true')
    assert "reason=ok" in proc.stdout and "held" in proc.stdout
    assert proc.stdout.strip().endswith("0")                                  # export されていない
    assert TOKEN not in proc.stdout.replace("held", "")


def test_resolve_failure_clears_token_and_keeps_the_fixed_code(tmp_path):
    proc = run_bash(tmp_path, "credential_file_permissions\n", '_irc_resolve\necho "reason=$irc_reason token=[$irc_token]"')
    assert "reason=credential_file_permissions token=[]" in proc.stdout


def test_retry_only_after_the_interval_and_never_for_ok_or_unset(tmp_path):
    # no_credentials / ok は取り直さない
    for out in ("no_credentials\n", f"ok\n{TOKEN}\n"):
        proc = run_bash(tmp_path, out, "_irc_resolve\nirc_resolved_at=0\n_irc_maybe_retry\n_irc_maybe_retry")
        assert proc.returncode == 0
    assert calls(tmp_path) == 2                                               # 起動時の取り出し 1 回ずつ + 取り直し無し
    (tmp_path / "scripts" / "lib_irc_relay.py.calls").unlink()
    # 失敗の間: 間隔内は呼ばない・過ぎたら 1 回だけ
    proc = run_bash(tmp_path, "credential_command_failed\n",
                    "_irc_resolve\n_irc_maybe_retry\n_irc_maybe_retry\nirc_resolved_at=$(( $(date +%s) - 601 ))\n_irc_maybe_retry")
    assert calls(tmp_path) == 2 and proc.stdout.count("irc credentials retry") == 1


# ---------------------------------------------------------------------------
# python: 通知と 1 サイクル
# ---------------------------------------------------------------------------

def python_block():
    start = DISPATCHER.index("IRC_NOTIFY_KEYS = (")
    end = DISPATCHER.index("# Everything below this marker runs a full dispatch cycle.")
    return DISPATCHER[start:end]


class Ns:
    """dispatcher の名前空間のスタブ。notify_state_once の呼び出しを記録する。"""

    def __init__(self, tmp_path, monkeypatch, carried=TOKEN, resolve_reason="ok", mux=None):
        from test_irc_relay_poll_deliver import FakeMux
        self.notified = []
        self.cleared = []
        self.told = {}
        self.logs = []
        self.registry = tmp_path / "registry"
        (self.registry / "daemons").mkdir(parents=True)
        self.root = tmp_path
        (tmp_path / "config").mkdir()
        monkeypatch.setenv("CREWVIA_REPO_ROOT", str(tmp_path))
        self.mux = mux or FakeMux()

        def notify_state_once(key, fp, kind, slug, task_id, build_msg, *, director_live=lambda: True):
            self.notified.append((key, fp, build_msg()))
            return True

        def clear_told_key(key):
            self.cleared.append(key)
            self.told.pop(key, None)
            return True

        self.ns = {
            "REGISTRY_DIR": self.registry, "_mux": self.mux, "log": self.logs.append, "time": time,
            "fingerprint": lambda *p: hashlib.sha256(json.dumps(p, default=str).encode()).hexdigest()[:16],
            "notify_state_once": notify_state_once, "clear_told_key": clear_told_key,
            "load_told": lambda: self.told, "is_unreadable": is_unreadable,
            "director_live_for_state_notices": lambda: True,
            "_IRC_CARRIED": {"_CREWVIA_IRC_RESOLVED_TOKEN": carried}, "_IRC_RESOLVE_REASON": resolve_reason,
        }
        exec(python_block(), self.ns)

    def config(self, text):
        (self.root / "config" / "crewvia.yaml").write_text(text, encoding="utf-8")

    def cycle(self):
        self.ns["run_irc_cycle"]()

    def files(self):
        return sorted(p.name for p in (self.registry / "daemons").iterdir() if not p.name.endswith(".lock"))


def cfg_text(url, instance="wsl", extra=""):
    return (f"irc:\n  enabled: true\n  url: {url}\n  instance: {instance}\n{extra}  credentials:\n    source: file\n"
            f"    file: /unused\n")


@pytest.fixture
def relay():
    with FakeRelay(token=TOKEN) as r:
        yield r


def test_unset_config_does_nothing_at_all(tmp_path, monkeypatch):
    ns = Ns(tmp_path, monkeypatch, carried="", resolve_reason="no_credentials")
    ns.config("telegram:\n  poll_interval_seconds: 10\n")
    ns.cycle()
    assert ns.files() == [] and ns.notified == [] and ns.cleared == [] and ns.mux.sent == [] and ns.mux.list_calls == 0


def test_enabled_but_missing_irc_block_defaults_to_disabled(tmp_path, monkeypatch):
    ns = Ns(tmp_path, monkeypatch)
    ns.cycle()                                                                 # config ファイル自体が無い
    assert ns.files() == [] and ns.notified == []


def test_bad_url_notifies_once_with_the_fixed_code_only(tmp_path, monkeypatch):
    ns = Ns(tmp_path, monkeypatch)
    ns.config(cfg_text("http://8.8.8.8:8765"))
    ns.cycle()
    assert [n[0] for n in ns.notified] == ["irc_receiver_disabled"]
    assert "url_not_allowed" in ns.notified[0][2] and "8.8.8.8" not in ns.notified[0][2]
    assert ns.files() == ["irc-receiver.json"]


def test_credential_failure_at_startup_surfaces_the_startup_reason(tmp_path, monkeypatch):
    ns = Ns(tmp_path, monkeypatch, carried="", resolve_reason="credential_command_failed")
    ns.config(cfg_text("http://127.0.0.1:1"))
    ns.cycle()
    assert [n[0] for n in ns.notified] == ["irc_receiver_disabled"] and "credential_command_failed" in ns.notified[0][2]
    assert not (ns.registry / "daemons" / "irc-state.json").exists()


def test_end_to_end_delivery_logs_and_does_not_notify_on_success(tmp_path, monkeypatch, relay):
    ns = Ns(tmp_path, monkeypatch)
    ns.config(cfg_text(relay.url))
    relay.add(text="@Sora hello")
    ns.cycle()
    assert len(ns.mux.sent) == 1 and ns.mux.sent[0][1].startswith(L.FRAME_PREFIX)
    assert ns.notified == [] and any("delivered 1" in m for m in ns.logs)
    assert TOKEN not in json.dumps(ns.logs) and TOKEN not in json.dumps(ns.notified)


def test_unauthorized_notifies_immediately_and_recovery_clears(tmp_path, monkeypatch, relay):
    ns = Ns(tmp_path, monkeypatch)
    ns.config(cfg_text(relay.url))
    relay.token = "different-" + TOKEN
    ns.cycle()
    assert [n[0] for n in ns.notified] == ["irc_relay_unauthorized"]
    assert TOKEN not in ns.notified[0][2]
    # 回復: 台帳にキーがあるときだけ clear する (毎サイクル台帳を書かない)
    relay.token = TOKEN
    ns.told["irc_relay_unauthorized"] = {"fp": "x"}
    state_file = ns.registry / "daemons" / "irc-state.json"
    s = json.loads(state_file.read_text())
    s["last_poll_at"] = 0
    state_file.write_text(json.dumps(s))
    ns.notified.clear()
    ns.cycle()
    assert ns.notified == [] and ns.cleared == ["irc_relay_unauthorized"]
    ns.cleared.clear()
    ns.cycle()
    assert ns.cleared == []


def test_unreachable_is_quiet_until_six_in_a_row(tmp_path, monkeypatch):
    ns = Ns(tmp_path, monkeypatch)
    ns.config(cfg_text("http://127.0.0.1:9"))
    for i in range(6):
        if i:
            state_file = ns.registry / "daemons" / "irc-state.json"
            s = json.loads(state_file.read_text())
            s["last_poll_at"] = 0
            state_file.write_text(json.dumps(s))
        ns.cycle()
        assert (len(ns.notified) == 1) is (i == 5), i
    assert ns.notified[0][0] == "irc_relay_unreachable" and "6 回連続" in ns.notified[0][2]


def test_unreadable_state_notifies_and_blocks(tmp_path, monkeypatch, relay):
    ns = Ns(tmp_path, monkeypatch)
    ns.config(cfg_text(relay.url))
    (ns.registry / "daemons" / "irc-state.json").write_text("garbage")
    ns.cycle()
    assert [n[0] for n in ns.notified] == ["irc_receiver_disabled"] and "state_unreadable" in ns.notified[0][2]
    assert relay.requests == [] and ns.mux.sent == []


def test_gap_and_identity_change_are_notified(tmp_path, monkeypatch, relay):
    ns = Ns(tmp_path, monkeypatch)
    ns.config(cfg_text(relay.url))
    relay.add(text="@Sora one")
    relay.gap = True
    ns.cycle()
    assert "irc_relay_gap" in [n[0] for n in ns.notified]
    ns.notified.clear()
    relay.gap = False
    ns.config(cfg_text(relay.url, instance="other"))
    ns2 = ns
    sent_before = len(ns.mux.sent)
    relay.add(instance="wsl", text="@Sora two")
    state_file = ns.registry / "daemons" / "irc-state.json"
    s = json.loads(state_file.read_text())
    s["pending"] = [dict(L.pending_entry({"id": 1, "ts": int(time.time()), "kind": "dm", "channel": None, "sender": "a",
                                           "account": None, "name": "sora", "text": "old", "truncated": False,
                                           "suppressed_before": 0}, "7f3a9c21"))]
    state_file.write_text(json.dumps(s))
    ns2.cycle()
    keys = [n[0] for n in ns2.notified]
    assert "irc_identity_changed" in keys
    assert not any("本文=\"old\"" in text for _, text in ns2.mux.sent[sent_before:])      # 旧 identity の未配達は出ない


def test_cycle_failure_is_swallowed_with_type_name_only(tmp_path, monkeypatch):
    ns = Ns(tmp_path, monkeypatch)
    ns.config(cfg_text("http://127.0.0.1:1"))
    ns.ns["_mux"] = None
    ns.ns["lib_irc_relay"] = None
    import lib_irc_relay as real

    def boom(*a, **k):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(real, "run_cycle", boom)
    ns.cycle()
    assert any("irc] cycle failed: RuntimeError" in m for m in ns.logs) and TOKEN not in json.dumps(ns.logs)


def test_gap_notice_is_cleared_by_a_later_successful_poll_without_gap(tmp_path, monkeypatch, relay):
    """Codex P2: gap の台帳は gap でない成功で消える (消えないと次の独立した欠落が黙る)。"""
    ns = Ns(tmp_path, monkeypatch)
    ns.config(cfg_text(relay.url))
    ns.told["irc_relay_gap"] = {"fp": "x"}
    relay.gap = False
    ns.cycle()
    assert "irc_relay_gap" not in ns.told
