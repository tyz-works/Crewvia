#!/usr/bin/env python3
"""tests/test_dispatcher_main_checkout_drift.py — 主 checkout の版ずれ通知は
状態が変わるまで 1 回だけ、解消したら再通知できる (t005 / B2 / backlog #26)。

`check_main_checkout_drift()` は `lib_daemon_watch.fetch_origin()` /
`commits_behind()` / `restart_needed()` を呼ぶが、この一連のテストは
「通知の一度きり性・解消後の再通知・観測不能時に黙る」という **dispatcher 側の
判定** だけを対象にする。git そのものの挙動 (fetch / ff / digest 比較) は
`tests/test_main_checkout_sync.py` が実 git リポジトリで別に確かめる。ここでは
`lib_daemon_watch` の該当関数を monkeypatch して決定的にする — 本物の dispatcher.sh
の埋め込み python を `exec()` して呼ぶ (`tests/test_dispatcher_notify_once.py` と
同じ手: `# --- CYCLE ENTRY POINT ---` から下を切り、残りを名前空間で実行する)。

実行: python3 -m pytest tests/test_dispatcher_main_checkout_drift.py -v
"""

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT_DIR / "scripts"
DISPATCHER_SH = SCRIPTS_DIR / "dispatcher.sh"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_daemon_watch  # noqa: E402
import lib_mux  # noqa: E402


class FakeMux:
    directors = ["Sora-director"]
    sent = []

    def __init__(self, *a, **kw):
        pass

    def list(self, *a, suffix=None, **kw):
        return list(FakeMux.directors) if suffix == "-director" else []

    def send(self, name, text):
        if name not in FakeMux.directors:
            return False
        FakeMux.sent.append(text)
        return True

    def state(self, *a, **kw):
        return "unknown"

    def capture(self, *a, **kw):
        return ""

    def kill(self, *a, **kw):
        return False


class Harness:
    """1 つの fixture repo と、dispatcher.sh の埋め込み python を exec した名前空間。"""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.registry = root / "registry"
        self.queue = root / "queue"
        self.notify_cache = root / "notify-cache.json"
        self.log = root / "dispatcher.log"
        self.monkeypatch = monkeypatch

        root.mkdir(parents=True)
        self.registry.mkdir()
        (self.queue / "missions").mkdir(parents=True)
        (self.queue / "archive").mkdir()
        (self.queue / "state.yaml").write_text("active_missions: []\ndefault_mission: null\n")

        FakeMux.directors = ["Sora-director"]
        FakeMux.sent = []
        monkeypatch.setattr(lib_mux, "Mux", FakeMux)
        monkeypatch.setattr(lib_mux, "repo_identity_ok", lambda *a, **kw: True)
        monkeypatch.setenv("CREWVIA_TASKVIA", "disabled")
        monkeypatch.delenv("TASKVIA_TOKEN", raising=False)
        monkeypatch.setenv("TASKVIA_URL", "")
        # 検知そのものと通知の一度きり性を分けて見るため、既定では毎回 due にする。
        # スロットル自体の挙動は test_interval_throttle_skips_the_check_body が見る。
        monkeypatch.setenv("CREWVIA_MAIN_CHECKOUT_DRIFT_INTERVAL", "0")

    def run(self, *, fetch_ok=True, ahead=None, restart_flags=None, fetch_fn=None):
        """`check_main_checkout_drift()` を 1 回呼ぶ。lib_daemon_watch の 3 関数を
        monkeypatch で差し替えるので、本物の git には一切触れない。`fetch_fn` を渡すと
        `fetch_ok` の代わりにそれを使う (例外注入テスト用)。"""
        restart_flags = {} if restart_flags is None else restart_flags
        self.monkeypatch.setattr(
            lib_daemon_watch, "fetch_origin",
            fetch_fn if fetch_fn is not None else (lambda *a, **kw: fetch_ok))
        self.monkeypatch.setattr(lib_daemon_watch, "commits_behind", lambda *a, **kw: ahead)
        self.monkeypatch.setattr(
            lib_daemon_watch, "restart_needed",
            lambda registry_dir, repo_root, name: restart_flags.get(name))

        FakeMux.sent = []
        src = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", DISPATCHER_SH.read_text(),
                        re.DOTALL).group(1)
        src, n = re.subn(r"\n# --- CYCLE ENTRY POINT ---\n.*$", "", src, flags=re.S)
        assert n == 1, "CYCLE ENTRY POINT marker not found"
        self.monkeypatch.setattr(sys, "argv", [
            "dispatcher", str(self.queue), str(self.registry), str(self.notify_cache),
            "300", "60", str(self.log)])
        ns = {"__name__": "dispatcher_under_test"}
        exec(compile(src, "dispatcher.sh (embedded, test)", "exec"), ns)
        ns["check_main_checkout_drift"]()
        return list(FakeMux.sent)

    @property
    def told_file(self):
        return self.registry / "daemons" / "notified-state.json"


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path / "repo", monkeypatch)


# ---------------------------------------------------------------------------
# きれいな状態では通知しない
# ---------------------------------------------------------------------------

def test_no_drift_sends_nothing(h):
    assert h.run(fetch_ok=True, ahead=0, restart_flags={"dispatcher": False, "watchdog": False}) == []


def test_unknown_alone_does_not_notify(h):
    """観測できない (fetch 失敗・版の記録が無い) だけでは通知しない — 誤報より沈黙が安い。"""
    assert h.run(fetch_ok=False, ahead=None,
                restart_flags={"dispatcher": None, "watchdog": None}) == []


# ---------------------------------------------------------------------------
# origin ahead / restart-needed はそれぞれ単独でも通知の引き金になる
# ---------------------------------------------------------------------------

def test_origin_ahead_notifies(h):
    msgs = h.run(fetch_ok=True, ahead=3, restart_flags={"dispatcher": False, "watchdog": False})
    assert len(msgs) == 1
    assert "main-checkout-drift" in msgs[0]
    assert "3 commit" in msgs[0]
    assert "sync-main-checkout.sh" in msgs[0]


def test_restart_needed_alone_notifies(h):
    msgs = h.run(fetch_ok=True, ahead=0, restart_flags={"dispatcher": True, "watchdog": False})
    assert len(msgs) == 1
    assert "dispatcher" in msgs[0]


# ---------------------------------------------------------------------------
# 状態が変わるまで 1 回だけ
# ---------------------------------------------------------------------------

def test_same_state_is_told_once(h):
    assert len(h.run(fetch_ok=True, ahead=2, restart_flags={"dispatcher": False, "watchdog": False})) == 1
    for _ in range(3):
        assert h.run(fetch_ok=True, ahead=2, restart_flags={"dispatcher": False, "watchdog": False}) == []


def test_state_change_is_told_again(h):
    assert len(h.run(fetch_ok=True, ahead=2, restart_flags={"dispatcher": False, "watchdog": False})) == 1
    # 台帳が「伝えた」= 2 commit ahead。もう 1 commit 進むと fingerprint が変わる。
    assert len(h.run(fetch_ok=True, ahead=3, restart_flags={"dispatcher": False, "watchdog": False})) == 1


# ---------------------------------------------------------------------------
# 解消したら再通知できる (同じ状態が偶然また起きても黙らない)
# ---------------------------------------------------------------------------

def test_resolution_then_recurrence_notifies_again(h):
    assert len(h.run(fetch_ok=True, ahead=1, restart_flags={"dispatcher": False, "watchdog": False})) == 1
    # 解消 (sync 済み)。
    assert h.run(fetch_ok=True, ahead=0, restart_flags={"dispatcher": False, "watchdog": False}) == []
    # 同じ fingerprint (ahead=1, 同じ daemon の組) がもう一度起きても、
    # 解消を経ているので新しい事象として届く。
    assert len(h.run(fetch_ok=True, ahead=1, restart_flags={"dispatcher": False, "watchdog": False})) == 1


# ---------------------------------------------------------------------------
# 周期スロットル: 間隔内は本体を実行しない (git fetch を毎サイクル叩かない)
# ---------------------------------------------------------------------------

def test_interval_throttle_skips_the_check_body(tmp_path, monkeypatch):
    h = Harness(tmp_path / "repo", monkeypatch)
    monkeypatch.setenv("CREWVIA_MAIN_CHECKOUT_DRIFT_INTERVAL", "3600")
    assert len(h.run(fetch_ok=True, ahead=5, restart_flags={"dispatcher": False, "watchdog": False})) == 1
    # 間隔内の再呼び出しは、たとえ状態が変わっていても本体を実行しない (通知 0 件)。
    assert h.run(fetch_ok=True, ahead=99, restart_flags={"dispatcher": True, "watchdog": True}) == []


# ---------------------------------------------------------------------------
# 配線: 関数そのものではなく、毎サイクル実際に呼ばれる場所に置かれているか
# ---------------------------------------------------------------------------

def test_check_main_checkout_drift_is_wired_into_the_cycle_entry_point():
    """`Harness.run()` は `check_main_checkout_drift()` を直接呼ぶので、上のテスト
    群は関数自身の判定ロジックしか固定できない。本番で実際に毎サイクル呼ばれる
    保証は、`# --- CYCLE ENTRY POINT ---` から下にこの呼び出しがあることで担保する
    — ここだけは静的なテキスト検査になる。"""
    src = DISPATCHER_SH.read_text()
    m = re.search(r"\n# --- CYCLE ENTRY POINT ---\n(.*)\nPYEOF", src, re.DOTALL)
    assert m, "CYCLE ENTRY POINT marker not found"
    assert "check_main_checkout_drift()" in m.group(1), (
        "check_main_checkout_drift() is not called from the cycle entry point — "
        "the function's logic can be correct and still never run in production")


def test_never_raises_even_if_lib_daemon_watch_blows_up(h):
    """安全網が dispatch サイクルを落としてはならない (run_daemon_watch() と同じ規律)。"""
    def boom(*a, **kw):
        raise RuntimeError("git fetch exploded")
    assert h.run(fetch_fn=boom) == []   # 例外を飲み込んで、何も送らずに終わる
