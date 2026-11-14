"""IRC 配達前の判定は Rex の program_status を先に見て延期する (t001)。"""
import json
import sys
from pathlib import Path

import pytest

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS.parent / "scripts"))
sys.path.insert(0, str(THIS))
import lib_irc_relay as L  # noqa: E402
import lib_mux  # noqa: E402
from test_irc_relay_poll_deliver import FakeMux, READY, ent, st, deliver  # noqa: E402

LONG = (THIS / "fixtures" / "irc" / "busy-long-todo-spinner-out-of-window.txt").read_text(encoding="utf-8")
DRAFT = READY.replace("❯ \n", "❯ typing\n")
DIALOG = "Do you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel\n"


class StatusMux(FakeMux):
    def __init__(self, status, **kw):
        super().__init__(**kw)
        self.status = status
        self.status_calls = []

    def agent_status(self, name):
        self.status_calls.append(name)
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


def run(status, screen):
    mux = StatusMux(status, screens={"Sora-director": screen})
    new, stats = deliver(st(ent(41)), mux)
    return mux, new, stats


def test_fixture_is_misread_as_ready_by_screen_alone():
    # 今の main の見逃しの実証: スピナーが窓の外 → 画面だけだと ready
    assert L.classify_pane(LONG) == "ready"


def test_working_defers_even_when_screen_says_ready():
    mux, new, stats = run("working", LONG)
    assert mux.sent == [] and len(new["pending"]) == 1
    assert stats["deferred"] == {"busy": 1} and new["pending"][0]["attempts"] == 0


def test_blocked_defers_as_dialog():
    mux, new, stats = run("blocked", READY)
    assert mux.sent == [] and stats["deferred"] == {"dialog": 1}


@pytest.mark.parametrize("status", ["idle", None])
def test_idle_and_none_follow_the_screen(status):
    mux, new, _ = run(status, READY)
    assert len(mux.sent) == 1 and new["pending"] == []
    mux, new, stats = run(status, DRAFT)
    assert mux.sent == [] and stats["deferred"] == {"draft": 1}


def test_exception_follows_the_screen():
    mux, new, _ = run(RuntimeError("boom"), READY)
    assert len(mux.sent) == 1


def test_idle_never_overrides_screen_busy_or_dialog():
    assert L._pane_state(StatusMux("idle", screens={"w": L.__dict__ and (Path(THIS / "fixtures/irc/busy-screen-sora.txt").read_text(encoding="utf-8"))}), "w") == "busy"
    assert L._pane_state(StatusMux("idle", screens={"w": DIALOG}), "w") == "dialog"


def test_mux_without_agent_status_is_unchanged():
    mux = FakeMux()
    new, _ = deliver(st(ent(41)), mux)
    assert len(mux.sent) == 1 and new["pending"] == []


# --- RexBackend.agent_status ---------------------------------------------
BLOCK = "block:01abc"
PID, NAME = 4321, "2.1.325"
FG = {"foreground": {"pid": PID, "name": NAME}, "child": {"pid": PID, "name": NAME}}


def rec(state):
    return {"state": state, "app": "claude-code", "effective_app": "claude-code",
            "owner": {"pid": PID, "name": NAME}}


def ok(obj):
    return lib_mux.RexResult(0, json.dumps(obj), "", False)


@pytest.fixture
def backend(monkeypatch):
    b = lib_mux.RexBackend()
    monkeypatch.setattr(b, "_resolve", lambda name: {"block_id": BLOCK})
    monkeypatch.setattr(b, "_guard", lambda verb, name: None)
    monkeypatch.setattr(b, "capture", lambda name: pytest.fail("screen must not be read"))
    monkeypatch.setattr(b, "_capture_block", lambda bid: pytest.fail("screen must not be read"))
    return b


def fake(monkeypatch, status, proc=None):
    def runner(args, timeout=None):
        return status if args[2] == "program_status" else (proc or ok(FG))
    monkeypatch.setattr(lib_mux, "_rex_run", runner)


@pytest.mark.parametrize("state,expected", [("working", "working"), ("blocked", "blocked"),
                                            ("idle", "idle"), ("done", "idle"),
                                            ("error", None), ("weird", None)])
def test_rex_agent_status_mapping(backend, monkeypatch, state, expected):
    fake(monkeypatch, ok({"records": [rec(state)]}))
    assert backend.agent_status("Sora-director") == expected


def test_rex_agent_status_none_cases(backend, monkeypatch):
    fake(monkeypatch, ok({"records": []}))
    assert backend.agent_status("x") is None
    fake(monkeypatch, lib_mux.RexResult(None, "", "", True))
    assert backend.agent_status("x") is None
    fake(monkeypatch, None)
    assert backend.agent_status("x") is None
    fake(monkeypatch, ok({"records": [rec("working")]}),
         proc=ok({"foreground": {"pid": 1, "name": "bash"}}))
    assert backend.agent_status("x") is None          # owner 不一致
    monkeypatch.setattr(backend, "_resolve", lambda name: None)
    assert backend.agent_status("x") is None          # pane_not_found
    def boom(name): raise RuntimeError("x")
    monkeypatch.setattr(backend, "_resolve", boom)
    assert backend.agent_status("x") is None


def test_other_backends_default_none():
    assert lib_mux.TmuxBackend().agent_status("x") is None
    assert lib_mux.HerdrBackend().agent_status("x") is None
