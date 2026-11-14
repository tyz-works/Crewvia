"""RexBackend.state() は program_status (OSC 7501) を第一に読み、使えなければ画面判定へ (t003)。

偽の rex は `lib_mux._rex_run` の差し替えで作る (呼ばれた引数列も見る)。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lib_mux  # noqa: E402

BLOCK = "block:01abc"
PID, NAME = 4321, "2.1.325"
FG = {"foreground": {"pid": PID, "name": NAME}, "child": {"pid": PID, "name": NAME}}

RULE = "─" * 40
IDLE_SCREEN = f"hello\n{RULE}\n❯ \n{RULE}\n  status\n"
SPINNER_SCREEN = f"✽ Catapulting… (1m 24s)\n{RULE}\n❯ \n{RULE}\n  status\n"


def rec(state="working", app="claude-code", owner=(PID, NAME), **kw):
    r = {"state": state, "app": app, "effective_app": app,
         "updated_at": "2026-11-14T13:58:47+09:00"}
    if owner is not None:
        r["owner"] = {"pid": owner[0], "name": owner[1]}
    r.update(kw)
    return r


def ok(obj):
    return lib_mux.RexResult(0, json.dumps(obj), "", False)


TIMEOUT = lib_mux.RexResult(None, "", "", True)


@pytest.fixture(autouse=True)
def _reset_log():
    lib_mux._PROGRAM_STATUS_LOGGED.clear()


@pytest.fixture
def backend(monkeypatch):
    b = lib_mux.RexBackend()
    monkeypatch.setattr(b, "_resolve", lambda name: {"block_id": BLOCK})
    monkeypatch.setattr(b, "_guard", lambda verb, name: None)   # 隔離の関門は別テスト
    return b


def fake_rex(monkeypatch, status, proc=ok(FG), calls=None):
    def run(args, timeout=None):
        if calls is not None:
            calls.append((list(args), timeout))
        method = args[2] if args[:2] == ["block", "call"] else None
        if method == "program_status":
            return status
        if method == "process":
            return proc
        raise AssertionError(args)
    monkeypatch.setattr(lib_mux, "_rex_run", run)


def screen(monkeypatch, b, text):
    monkeypatch.setattr(b, "capture", lambda name: text)
    monkeypatch.setattr(b, "_capture_block", lambda block_id: text)


@pytest.mark.parametrize("state,expected", [
    ("working", "working"), ("blocked", "blocked"), ("idle", "idle"),
    ("done", "idle"),
])
def test_mapping(backend, monkeypatch, state, expected):
    fake_rex(monkeypatch, ok({"records": [rec(state)]}))
    screen(monkeypatch, backend, "")        # 画面に頼っていれば unknown になる
    assert backend.state("Zed-worker") == expected


def test_blocked_with_kind_is_blocked(backend, monkeypatch):
    fake_rex(monkeypatch, ok({"records": [rec("blocked", kind="question")]}))
    assert backend.state("Zed-worker") == "blocked"


def test_program_status_wins_over_the_screen(backend, monkeypatch):
    fake_rex(monkeypatch, ok({"records": [rec("idle")]}))
    screen(monkeypatch, backend, SPINNER_SCREEN)    # 画面だけなら working
    assert backend.state("Zed-worker") == "idle"


def test_two_calls_with_short_timeout(backend, monkeypatch):
    calls = []
    fake_rex(monkeypatch, ok({"records": [rec()]}), calls=calls)
    backend.state("Zed-worker")
    assert [c[0][2] for c in calls] == ["program_status", "process"]
    assert all(c[1] is not None and c[1] <= 5 for c in calls)


FALLBACK_CASES = {
    "records empty": (ok({"records": []}), ok(FG)),
    "state error": (ok({"records": [rec("error")]}), ok(FG)),
    "state unknown value": (ok({"records": [rec("sleeping")]}), ok(FG)),
    "other app only": (ok({"records": [rec(app="cargo")]}), ok(FG)),
    "child record only": (ok({"records": [rec(id="build/test")]}), ok(FG)),
    "two claude records": (ok({"records": [rec(), rec("idle")]}), ok(FG)),
    "owner mismatch pid": (ok({"records": [rec(owner=(PID + 1, NAME))]}), ok(FG)),
    "owner mismatch name": (ok({"records": [rec(owner=(PID, "2.1.0"))]}), ok(FG)),
    "owner missing": (ok({"records": [rec(owner=None)]}), ok(FG)),
    "owner null": (ok({"records": [dict(rec(), owner=None)]}), ok(FG)),
    "foreground missing": (ok({"records": [rec()]}), ok({"child": FG["child"]})),
    "process unreadable": (ok({"records": [rec()]}), lib_mux.RexResult(1, "", "x", False)),
    "process timeout": (ok({"records": [rec()]}), TIMEOUT),
    "not json": (lib_mux.RexResult(0, "oops", "", False), ok(FG)),
    "not an object": (ok([1]), ok(FG)),
    "records not a list": (ok({"records": {}}), ok(FG)),
    "record not a dict": (ok({"records": ["x"]}), ok(FG)),
    "state wrong type": (ok({"records": [rec(state=5)]}), ok(FG)),
    "state missing": (ok({"records": [{"app": "claude-code", "owner": {"pid": PID, "name": NAME}}]}), ok(FG)),
    "pid is bool": (ok({"records": [rec(owner=(True, NAME))]}), ok(FG)),
    "api missing (rc 1)": (lib_mux.RexResult(1, "", "unknown method", False), ok(FG)),
    "timeout": (TIMEOUT, ok(FG)),
    "rex not run": (None, ok(FG)),
}


@pytest.mark.parametrize("case", sorted(FALLBACK_CASES))
def test_unusable_program_status_falls_back_to_the_screen(backend, monkeypatch, case):
    status, proc = FALLBACK_CASES[case]
    fake_rex(monkeypatch, status, proc)
    screen(monkeypatch, backend, SPINNER_SCREEN)
    assert backend.state("Zed-worker") == "working"        # #405 の画面判定
    screen(monkeypatch, backend, IDLE_SCREEN)
    assert backend.state("Zed-worker") == "idle"
    screen(monkeypatch, backend, "")
    assert backend.state("Zed-worker") == "unknown"


def test_pane_not_found_falls_back(monkeypatch):
    b = lib_mux.RexBackend()
    monkeypatch.setattr(b, "_guard", lambda verb, name: None)
    monkeypatch.setattr(b, "_resolve", lambda name: None)
    monkeypatch.setattr(b, "capture", lambda name: SPINNER_SCREEN)
    assert b.state("Zed-worker") == "working"


def test_fallback_is_logged_once_per_reason(backend, monkeypatch, capsys):
    fake_rex(monkeypatch, ok({"records": []}))
    screen(monkeypatch, backend, IDLE_SCREEN)
    for _ in range(3):
        backend.state("Zed-worker")
    err = capsys.readouterr().err
    assert err.count("program_status not used") == 1
    assert "no_record" in err


def test_effective_app_is_used_when_app_is_empty(backend, monkeypatch):
    r = rec("working")
    r["app"] = ""
    fake_rex(monkeypatch, ok({"records": [r]}))
    assert backend.state("Zed-worker") == "working"


def test_idle_record_is_not_discarded_for_age(backend, monkeypatch):
    # idle の Claude Code は報告をし直さない (実測)。古い updated_at でも採用する。
    fake_rex(monkeypatch, ok({"records": [rec("done", updated_at="2020-01-01T00:00:00+09:00")]}))
    assert backend.state("Zed-worker") == "idle"


USAGE_SCREEN = (f"⚠ Usage limit reached · continuing automatically at 6pm\n"
                f"{RULE}\n❯ \n{RULE}\n  status\n")


@pytest.mark.parametrize("state,reason", [("error", "status_error"),
                                          ("sleeping", "status_unknown_value")])
def test_error_and_unknown_value_read_the_screen_not_silence(
        backend, monkeypatch, capsys, state, reason):
    fake_rex(monkeypatch, ok({"records": [rec(state)]}))
    # 入力欄が空 = idle (Rule 5 が通知する側)。unknown (無音) に倒さない。
    screen(monkeypatch, backend, IDLE_SCREEN)
    assert backend.state("Zed-worker") == "idle"
    assert reason in capsys.readouterr().err


def test_error_with_usage_limit_screen_reaches_usage_limit_path(backend, monkeypatch):
    import lib_usage_limit
    fake_rex(monkeypatch, ok({"records": [rec("error")]}))
    screen(monkeypatch, backend, USAGE_SCREEN)
    assert backend.state("Zed-worker") == "idle"
    assert lib_usage_limit.detect(USAGE_SCREEN)


def test_fallback_reuses_the_resolved_block(monkeypatch):
    b = lib_mux.RexBackend()
    monkeypatch.setattr(b, "_guard", lambda verb, name: None)
    n = []
    monkeypatch.setattr(b, "_resolve", lambda name: n.append(name) or {"block_id": BLOCK})
    seen = []
    monkeypatch.setattr(b, "_capture_block", lambda bid: seen.append(bid) or IDLE_SCREEN)
    fake_rex(monkeypatch, ok({"records": []}))
    assert b.state("Zed-worker") == "idle"
    assert len(n) == 1 and seen == [BLOCK]


def test_fallback_keeps_the_test_isolation_guard(monkeypatch):
    # 解決済みの block id を直に使っても、capture と同じ関門を通る
    b = lib_mux.RexBackend()
    monkeypatch.setattr(b, "_resolve", lambda name: {"block_id": BLOCK})
    monkeypatch.setattr(b, "_capture_block", lambda bid: IDLE_SCREEN)
    fake_rex(monkeypatch, ok({"records": []}))
    with pytest.raises(lib_mux.MuxTestIsolationError):
        b.state("Zed-worker")
