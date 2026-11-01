#!/usr/bin/env python3
"""tests/test_rex_backend_stub_protocol.py

RexBackend を **偽の `rex` (PATH 上の stub)** で確かめる。**CI で常に走る** — CI の ubuntu に本物の
rex は入らない (Linux は testing wave 限定で配布が gated)。本物を使う版は
`test_rex_backend_real_server.py` (rex が無ければ skip)。

kill の本人確認・pidfd・子孫の列挙は、偽の rex に「この pid が block の子だ」と言わせ、
**本物の子プロセス**を pidfd で止めて確かめる (rex の挙動ではなく crewvia 側の判定を見るため)。

固定する契約 (knowledge/rex-mux-backend.md §11 PR-1):
  - 全呼び出しが `-S` / `--autostart=false` / timeout / `stdin=DEVNULL`。空の宛先では呼ばない
  - `block close` は破壊にも殻の掃除にも使わない (stub に来たら失敗)
  - 本番の宛先・本番の socket はテストから到達できない
  - api_revision が想定外・status が読めない → available() False
  - record_existence: exit 3 + not found だけが GONE。曖昧 (exit 3 + ambiguous)・server 無し (exit 4)・
    問い合わせ中の世代変化は UNOBSERVED
  - kill: 記録との照合 (世代・checkout・child pid / starttime・handle=window_id)・pidfd が無ければ拒否・
    TERM/HUP を無視するプロセスも KILL まで・子孫 (列挙後の fork を含む)・pid 再利用は信号を受けない

  python3 -m pytest tests/test_rex_backend_stub_protocol.py -v
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from rex_harness import FakeRex, make_checkout, ok, status_json

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lib_mux  # noqa: E402

SERVER_PID = os.getpid()      # 偽の server。テストが起こす子は ppid == この pid


def _alive(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return text.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def _wait(cond, seconds=6.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = FakeRex(tmp_path, monkeypatch)
    checkout = make_checkout(tmp_path)
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: checkout)
    monkeypatch.setenv("CREWVIA_REPO_ROOT", str(checkout))
    f.checkout = checkout
    return f


def _base_rules(*, instance="inst-A", revision=9):
    return [{"match": ["server", "status"],
             "out": status_json(pid=SERVER_PID, instance_id=instance,
                                revision=revision), "rc": 0}]


# ---------------------------------------------------------------------------
# 呼び出しの作法
# ---------------------------------------------------------------------------

def test_every_call_carries_endpoint_autostart_false_timeout_and_devnull(fake, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(cmd, **kw):
        seen.append((cmd, kw))
        return real(cmd, **kw)

    monkeypatch.setattr(lib_mux.subprocess, "run", spy)
    fake.set(_base_rules())
    assert lib_mux.RexBackend().available()
    assert lib_mux._rex_run(["server", "status", "--json"]) is not None
    assert seen
    for cmd, kw in seen:
        assert cmd[0] == "rex" and cmd[1:3] == ["-S", f"unix://{fake.sock}"]
        assert "--autostart=false" in cmd
        assert kw["stdin"] == subprocess.DEVNULL
        assert kw["timeout"] and kw["timeout"] > 0


def test_empty_selector_value_or_argument_never_reaches_rex(fake, capsys):
    fake.set(_base_rules())
    for args in (["send", "-b", "", "x"], ["send", "-b"], ["send-key", "-s", ""],
                 ["capture", "-b", ""], ["send", ""]):
        assert lib_mux._rex_run(args) is None
    assert fake.calls() == []
    assert "refusing a rex call" in capsys.readouterr().err


def test_timeout_is_reported_as_timed_out_not_as_an_answer(fake, monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(lib_mux.subprocess, "run", boom)
    res = lib_mux._rex_run(["server", "status", "--json"])
    assert res.timed_out and res.rc is None
    assert lib_mux._rex_status_from(res) is None


# ---------------------------------------------------------------------------
# 本番の宛先・本番の socket
# ---------------------------------------------------------------------------

def test_default_production_session_is_refused_under_test_isolation(fake, monkeypatch):
    monkeypatch.setenv("CREWVIA_REX_SESSION", "crewvia")
    b = lib_mux.RexBackend()
    for verb in (lambda: b.spawn("Zed-worker", "true"), lambda: b.send("Zed-worker", "x"),
                 lambda: b.kill("Zed-worker"), lambda: b.capture("Zed-worker"),
                 lambda: b.keys("Zed-worker", ["up"])):
        with pytest.raises(lib_mux.MuxTestIsolationError):
            verb()
    assert fake.calls() == []


def test_bare_production_pane_name_is_refused_under_test_isolation(fake, monkeypatch):
    monkeypatch.setenv("CREWVIA_MUX_PANE_PREFIX", "")
    with pytest.raises(lib_mux.MuxTestIsolationError):
        lib_mux.RexBackend().spawn("dispatcher", "true")
    assert fake.calls() == []


def test_without_a_test_socket_rex_is_never_executed_under_test_isolation(fake, monkeypatch):
    """CREWVIA_REX_SOCK が無い = 既定の socket = 本番の server。何も実行しない。"""
    monkeypatch.delenv("CREWVIA_REX_SOCK")
    b = lib_mux.RexBackend()
    assert lib_mux._rex_run(["server", "status", "--json"]) is None
    assert not b.available() and not b.server_running()
    assert b.list() == []
    with pytest.raises(lib_mux.MuxTestIsolationError):
        b.spawn("Zed-worker", "true")
    assert fake.calls() == []


def test_test_socket_override_is_not_honoured_outside_a_test_run(fake, monkeypatch, capsys):
    """CREWVIA_REX_SOCK が本番で立っていたら available() は False (ping 先と bind 先のずれを作らない)。"""
    monkeypatch.delenv("CREWVIA_MUX_TEST_ISOLATION")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    fake.set(_base_rules())
    assert not lib_mux.RexBackend().available()
    assert "test-only override" in capsys.readouterr().err
    assert fake.calls() == []


def test_socket_path_over_108_bytes_is_unavailable(fake, monkeypatch):
    monkeypatch.setenv("CREWVIA_REX_SOCK", "/tmp/" + "x" * 120 + "/s.sock")
    fake.set(_base_rules())
    assert not lib_mux.RexBackend().available()
    assert fake.calls() == []


# ---------------------------------------------------------------------------
# 版の検査 (§7)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,rule", [
    ("revision 8", {"match": ["server", "status"], "out": status_json(revision=8)}),
    ("revision 10", {"match": ["server", "status"], "out": status_json(revision=10)}),
    ("exit 1", {"match": ["server", "status"], "rc": 1, "err": "rex server is not running"}),
    ("not json", {"match": ["server", "status"], "out": "hello"}),
    ("json list", {"match": ["server", "status"], "out": "[1]"}),
    ("no instance_id", {"match": ["server", "status"],
                        "out": json.dumps({"endpoint": "e", "pid": 1, "api_revision": 9})}),
    ("revision as string", {"match": ["server", "status"],
                            "out": json.dumps({"endpoint": "e", "pid": 1, "instance_id": "i", "api_revision": "9"})}),
    ("pid bool", {"match": ["server", "status"],
                  "out": json.dumps({"endpoint": "e", "pid": True, "instance_id": "i", "api_revision": 9})}),
])
def test_unexpected_or_unreadable_status_makes_rex_unavailable(fake, label, rule):
    fake.set([rule])
    b = lib_mux.RexBackend()
    assert not b.available(), label
    assert not b.spawn("Zed-worker", "true")
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    assert b.server_identity() is None or label == "revision 8" or label == "revision 10"
    assert not any(c[0] in ("run", "new") for c in fake.calls())


def test_good_status_is_available_and_identity_is_instance_and_pid(fake):
    fake.set(_base_rules(instance="inst-Z"))
    b = lib_mux.RexBackend()
    assert b.available() and b.server_running()
    assert b.server_identity() == ("unix:///fake/s.sock", f"inst-Z:{SERVER_PID}")


# ---------------------------------------------------------------------------
# record_existence (§4 / QA 4)
# ---------------------------------------------------------------------------

def _rec(generation=f"inst-A:{SERVER_PID}"):
    return {"backend": "rex", "block_id": "block:AAA", "window_id": "window:W1",
            "session_id": "session:S1", "server": {"endpoint": "unix:///fake/s.sock",
                                                   "generation": generation}}


def test_record_existence_classification(fake):
    b = lib_mux.RexBackend()
    insp = ["block", "inspect"]
    cases = [
        ("exists", {"match": insp, "out": "{}", "rc": 0}, lib_mux.PANE_EXISTS),
        ("not found", {"match": insp, "rc": 3, "err": 'Error: block target "block:AAA" not found'},
         lib_mux.PANE_GONE),
        ("ambiguous (exit 3)", {"match": insp, "rc": 3, "err": 'Error: block target "x" is ambiguous'},
         lib_mux.PANE_UNOBSERVED),
        ("server down (exit 4)", {"match": insp, "rc": 4, "err": "no Rex server is running"},
         lib_mux.PANE_UNOBSERVED),
        ("exit 3 other text", {"match": insp, "rc": 3, "err": "permission"}, lib_mux.PANE_UNOBSERVED),
        ("exit 1", {"match": insp, "rc": 1, "err": "boom"}, lib_mux.PANE_UNOBSERVED),
    ]
    for label, rule, want in cases:
        fake.set(_base_rules() + [rule])
        assert b.record_existence("Zed-worker", _rec()) == want, label


def test_record_existence_other_generation_is_not_asked_at_all(fake):
    fake.set(_base_rules(instance="inst-B") + [
        {"match": ["block", "inspect"], "rc": 3, "err": 'block target "x" not found'}])
    assert lib_mux.RexBackend().record_existence("Zed-worker", _rec()) == lib_mux.PANE_UNOBSERVED
    assert not any(c[:2] == ["block", "inspect"] for c in fake.calls())


@pytest.mark.parametrize("block_present", [True, False])
def test_server_replaced_during_the_question_is_unobserved_either_way(fake, block_present):
    """§11 QA 4 競合: 問い合わせの間に instance_id が変わる → block が居ても居なくても UNOBSERVED。"""
    inst = {"match": ["server", "status"], "seq": [
        {"out": status_json(pid=SERVER_PID, instance_id="inst-A")},
        {"out": status_json(pid=SERVER_PID, instance_id="inst-B")}]}
    answer = ({"match": ["block", "inspect"], "out": "{}"} if block_present else
              {"match": ["block", "inspect"], "rc": 3, "err": 'block target "x" not found'})
    fake.set([inst, answer])
    assert lib_mux.RexBackend().record_existence("Zed-worker", _rec()) == lib_mux.PANE_UNOBSERVED


def test_status_unreadable_after_the_question_is_unobserved(fake):
    inst = {"match": ["server", "status"], "seq": [
        {"out": status_json(pid=SERVER_PID, instance_id="inst-A")}, {"rc": 4, "err": "gone"}]}
    fake.set([inst, {"match": ["block", "inspect"], "rc": 3, "err": 'block target "x" not found'}])
    assert lib_mux.RexBackend().record_existence("Zed-worker", _rec()) == lib_mux.PANE_UNOBSERVED


def test_record_without_block_id_or_server_is_unobserved_and_asks_nothing(fake):
    fake.set(_base_rules())
    b = lib_mux.RexBackend()
    for rec in ({}, {"server": {"generation": "g"}}, {"block_id": "", "server": {"generation": "g"}},
                {"block_id": "block:A"}):
        assert b.record_existence("Zed-worker", rec) == lib_mux.PANE_UNOBSERVED
    assert fake.calls() == []


# ---------------------------------------------------------------------------
# send の作法 (§6)
# ---------------------------------------------------------------------------

def test_input_box_reading():
    rule = "─" * 40
    empty = f"{rule}\n❯ Try \"how does x work?\"\n{rule}\n  footer\n"
    plain = f"{rule}\n❯ \n{rule}\n"
    one = f"{rule}\n❯ hello\n{rule}\n"
    multi = f"{rule}\n❯ 行1\n  行2\n{rule}\n"
    assert lib_mux._rex_input_is_empty(empty) is True
    assert lib_mux._rex_input_is_empty(plain) is True
    assert lib_mux._rex_input_is_empty(one) is False
    assert lib_mux._rex_input_is_empty(multi) is False
    assert lib_mux._rex_input_is_empty("$ ls\nfile\n") is None


def _worker_record(fake, *, block_id="block:AAA", child=None, generation=f"inst-A:{SERVER_PID}"):
    rec = {"handle": "window:W1", "tab_id": "window:W1", "pane_id": block_id,
           "backend": "rex", "checkout": str(fake.checkout.resolve()),
           "created_at": "2026-11-01T00:00:00Z",
           "server": {"endpoint": "unix:///fake/s.sock", "generation": generation},
           "block_id": block_id, "window_id": "window:W1", "session_id": "session:S1",
           "instance_id": "inst-A"}
    if child:
        rec["child"] = child
    path = fake.checkout / "registry" / "mux" / f"{lib_mux._pane_name('Zed-worker')}.json"
    path.write_text(json.dumps(rec))
    return path, rec


def _blocks_rule(label="Zed-worker", block_id="block:AAA"):
    return {"match": ["block", "ls"], "out": json.dumps({"blocks": [
        {"block_id": block_id, "label": lib_mux._pane_name(label), "window_id": "window:W1"}]})}


def test_send_without_a_clean_input_box_sends_no_enter(fake):
    rule = "─" * 40
    dirty = f"{rule}\n❯ leftover\n  more\n{rule}\n"
    _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule(), {"match": ["capture"], "out": dirty},
                              {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])
    assert not lib_mux.RexBackend().send("Zed-worker", "hello")
    calls = fake.calls()
    assert not any(c[0] == "send" for c in calls)
    assert not any(c[0] == "send-key" and "enter" in c for c in calls)
    assert any(c[0] == "send-key" and "ctrl+u" in c for c in calls)


def test_send_to_a_clean_box_types_then_presses_enter_once(fake):
    rule = "─" * 40
    clean = f"{rule}\n❯ Try \"x\"\n{rule}\n"
    _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule(), {"match": ["capture"], "out": clean},
                              {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])
    assert lib_mux.RexBackend().send("Zed-worker", "hello")
    kinds = [c[0] for c in fake.calls() if c[0] in ("send", "send-key")]
    assert kinds == ["send", "send-key"]


def test_send_over_the_limit_or_empty_is_false_and_sends_nothing(fake):
    _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule()])
    b = lib_mux.RexBackend()
    assert not b.send("Zed-worker", "")
    assert not b.send("Zed-worker", "あ" * 20000)       # 60000 bytes
    assert not any(c[0] in ("send", "send-key") for c in fake.calls())


def test_send_to_a_name_without_a_record_or_with_empty_block_id_calls_nothing(fake):
    fake.set(_base_rules())
    b = lib_mux.RexBackend()
    assert not b.send("Nobody-worker", "x")
    _worker_record(fake, block_id="")
    assert not b.send("Zed-worker", "x")
    assert not b.keys("Zed-worker", ["up"])
    assert b.capture("Zed-worker") == ""
    assert not any(c[0] in ("send", "send-key", "capture") for c in fake.calls())


def test_a_block_whose_label_changed_is_not_reached(fake, capsys):
    """記録の block が別の label になっていたら、同じ名前の別 block にも届かない。"""
    _worker_record(fake)
    fake.set(_base_rules() + [{"match": ["block", "ls"], "out": json.dumps({"blocks": [
        {"block_id": "block:AAA", "label": "renamed", "window_id": "window:W1"},
        {"block_id": "block:BBB", "label": lib_mux._pane_name("Zed-worker"), "window_id": "window:W2"}]})}])
    b = lib_mux.RexBackend()
    assert b._resolve("Zed-worker") is None
    assert not b.send("Zed-worker", "x")
    assert not any(c[0] in ("send", "send-key") for c in fake.calls())


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_excludes_the_initial_shell_and_keeps_duplicates(fake, capsys):
    p = lib_mux._pane_prefix()
    fake.set(_base_rules() + [
        {"match": ["ls", "--json"], "head": "ls", "out": json.dumps({"sessions": [
            {"session_id": "session:S1", "label": lib_mux._rex_session_label()}]})},
        {"match": ["block", "ls"], "out": json.dumps({"blocks": [
            {"block_id": "b1", "label": "shell", "window_id": "w"},
            {"block_id": "b2", "label": p + "Ann-worker", "window_id": "w"},
            {"block_id": "b3", "label": p + "Ann-worker", "window_id": "w"},
            {"block_id": "b4", "label": p + "Bo-reviewer", "window_id": "w"},
            {"block_id": "b5", "label": "foreign", "window_id": "w"}]})}])
    b = lib_mux.RexBackend()
    assert sorted(b.list()) == ["Ann-worker", "Ann-worker", "Bo-reviewer"]
    assert b.list(suffix="-reviewer") == ["Bo-reviewer"]
    assert "ambiguous" in capsys.readouterr().err


def test_list_with_two_sessions_of_the_same_label_is_empty(fake):
    label = lib_mux._rex_session_label()
    fake.set(_base_rules() + [{"match": ["ls", "--json"], "head": "ls", "out": json.dumps({"sessions": [
        {"session_id": "s1", "label": label}, {"session_id": "s2", "label": label}]})}])
    assert lib_mux.RexBackend().list() == []


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------

def _spawn_rules(*, sessions, blocks, procs=None, run_ok=True):
    rules = _base_rules() + [{"match": ["ls", "--json"], "head": "ls", "out": json.dumps({"sessions": sessions})}]
    rules.append({"match": ["block", "ls"], "out": json.dumps({"blocks": blocks})})
    for bid, proc in (procs or {}).items():
        rules.append({"match": ["process", bid], **proc})
    if run_ok:
        rules.append({"match": ["run"], "out": json.dumps(
            {"window_id": "window:NEW", "block_ids": ["block:NEW"]})})
    rules.append({"match": ["window", "rename"], "out": ""})
    return rules


def _proc(pid, name="sleep"):
    return {"out": json.dumps({"last_exit": None, "child": {"pid": pid, "name": name}})}


def test_spawn_creates_a_window_and_writes_a_record_bound_to_the_child(fake):
    child = subprocess.Popen(["sleep", "600"])
    try:
        label = lib_mux._rex_session_label()
        fake.set(_spawn_rules(sessions=[{"session_id": "session:S1", "label": label}], blocks=[],
                              procs={"block:NEW": _proc(child.pid)}))
        assert lib_mux.RexBackend().spawn("Zed-worker", "exec sleep 600", cwd="/tmp")
        run = [c for c in fake.calls() if c[0] == "run"][0]
        assert run[run.index("--label") + 1] == lib_mux._pane_name("Zed-worker")
        assert "--focus=false" in run and "--json" in run and "-s" in run
        assert run[run.index("--") + 1:] == ["bash", "-c", "exec sleep 600"]
        assert any(c[:2] == ["window", "rename"] for c in fake.calls())
        rec = json.loads((fake.checkout / "registry" / "mux" /
                          f"{lib_mux._pane_name('Zed-worker')}.json").read_text())
        assert rec["backend"] == "rex" and rec["handle"] == "window:NEW"
        assert rec["block_id"] == "block:NEW" and rec["window_id"] == "window:NEW"
        assert rec["session_id"] == "session:S1"
        assert rec["child"] == {"pid": child.pid, "starttime": lib_mux._rex_start_time(child.pid)}
        assert rec["server"]["generation"] == f"inst-A:{SERVER_PID}"
        assert not any(c[:2] == ["block", "close"] for c in fake.calls())
    finally:
        child.kill()
        child.wait()


def test_spawn_creates_the_session_when_there_is_none(fake):
    fake.set(_spawn_rules(sessions=[], blocks=[], procs={"block:NEW": _proc(os.getpid())}) +
             [{"match": ["new"], "out": json.dumps({"session_id": "session:S9"})}])
    assert lib_mux.RexBackend().spawn("Zed-worker", "true")
    assert any(c[0] == "new" for c in fake.calls())


def test_spawn_refuses_two_sessions_with_the_label(fake):
    label = lib_mux._rex_session_label()
    fake.set(_spawn_rules(sessions=[{"session_id": "s1", "label": label},
                                    {"session_id": "s2", "label": label}], blocks=[]))
    b = lib_mux.RexBackend()
    assert not b.spawn("Zed-worker", "true")
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    assert not any(c[0] in ("run", "new") for c in fake.calls())


def test_spawn_refuses_two_live_blocks_with_the_label(fake):
    label, pname = lib_mux._rex_session_label(), lib_mux._pane_name("Zed-worker")
    fake.set(_spawn_rules(
        sessions=[{"session_id": "s1", "label": label}],
        blocks=[{"block_id": "block:X", "label": pname, "window_id": "w1"},
                {"block_id": "block:Y", "label": pname, "window_id": "w2"}],
        procs={"block:X": _proc(1), "block:Y": _proc(1)}))
    b = lib_mux.RexBackend()
    assert not b.spawn("Zed-worker", "true")
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    assert not any(c[0] == "run" for c in fake.calls())


def test_spawn_does_not_count_a_dead_shell_but_counts_a_live_block(fake):
    """殻 (child 無し・last_exit 有り) は数えない。生きた 1 つは SPAWN_OCCUPIED。"""
    label, pname = lib_mux._rex_session_label(), lib_mux._pane_name("Zed-worker")
    shell = {"out": json.dumps({"last_exit": {"exit_code": 0}})}
    blocks = [{"block_id": "block:X", "label": pname, "window_id": "w1"},
              {"block_id": "block:Y", "label": pname, "window_id": "w2"}]
    # 殻 + 殻 -> 新規作成できる
    fake.set(_spawn_rules(sessions=[{"session_id": "s1", "label": label}], blocks=blocks,
                          procs={"block:X": shell, "block:Y": shell,
                                 "block:NEW": _proc(os.getpid())}))
    assert lib_mux.RexBackend().spawn("Zed-worker", "true")
    # 殻 + 生きた sleep -> OCCUPIED
    live = subprocess.Popen(["sleep", "600"])
    try:
        fake.set(_spawn_rules(sessions=[{"session_id": "s1", "label": label}], blocks=blocks,
                              procs={"block:X": shell, "block:Y": _proc(live.pid)}))
        b = lib_mux.RexBackend()
        assert not b.spawn("Zed-worker", "true")
        assert b.last_spawn_refusal == lib_mux.SPAWN_OCCUPIED
    finally:
        live.kill()
        live.wait()


def test_spawn_cannot_take_the_record_lock_means_no_spawn(fake, monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def no_lock(*a, **k):
        yield False

    monkeypatch.setattr(lib_mux, "_pane_record_lock", no_lock)
    fake.set(_spawn_rules(sessions=[], blocks=[]))
    assert not lib_mux.RexBackend().spawn("Zed-worker", "true")
    assert not any(c[0] in ("run", "new") for c in fake.calls())


# ---------------------------------------------------------------------------
# kill: 本人確認 + pidfd
# ---------------------------------------------------------------------------

@pytest.fixture
def victim(fake):
    """偽の rex が『block:AAA の子』と言う本物のプロセス (ppid == このテスト = 偽の server)。"""
    procs = []

    def make(argv=("sleep", "600")):
        p = subprocess.Popen(list(argv), start_new_session=True)
        procs.append(p)
        return p

    make.procs = procs
    yield make
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass
        p.wait()


def _arm(fake, p, *, record=True, record_child=None, generation=f"inst-A:{SERVER_PID}",
         instance="inst-A", child_name="sleep"):
    st = lib_mux._rex_start_time(p.pid)
    if record:
        _worker_record(fake, child=record_child if record_child is not None
                       else {"pid": p.pid, "starttime": st}, generation=generation)
    fake.set(_base_rules(instance=instance) + [
        _blocks_rule(),
        {"match": ["process", "block:AAA"], "out": json.dumps(
            {"last_exit": None, "child": {"pid": p.pid, "name": child_name}})}])
    return st


def test_normal_kill_matches_the_record_and_takes_the_process_down(fake, victim):
    p = victim()
    _arm(fake, p)
    b = lib_mux.RexBackend()
    assert b.may_destroy("Zed-worker")[0]
    assert b.kill("Zed-worker")
    assert p.wait(timeout=5) is not None
    assert not (fake.checkout / "registry" / "mux" / f"{lib_mux._pane_name('Zed-worker')}.json").exists()
    assert not any(c[:2] == ["block", "close"] or c[0] == "kill" for c in fake.calls())


@pytest.mark.parametrize("label,kwargs", [
    ("record generation differs, process not an empty shell", {"generation": "inst-OLD:1"}),
    ("record child pid differs", {"record_child": {"pid": 1, "starttime": "1"}}),
    ("record child starttime differs", {"record_child": "STARTTIME"}),
    ("record has no child", {"record_child": {}}),
])
def test_worker_kill_is_refused_when_the_record_does_not_vouch(fake, victim, label, kwargs):
    p = victim()
    kw = dict(kwargs)
    if kw.get("record_child") == "STARTTIME":
        kw["record_child"] = {"pid": p.pid, "starttime": "1"}
    _arm(fake, p, **kw)
    b = lib_mux.RexBackend()
    assert not b.may_destroy("Zed-worker")[0], label
    assert not b.kill("Zed-worker"), label
    assert _alive(p.pid), label


def test_worker_kill_is_refused_without_a_record_or_for_a_foreign_checkout(fake, victim):
    p = victim()
    _arm(fake, p, record=False)
    assert not lib_mux.RexBackend().kill("Zed-worker")      # 記録が無ければ block にも届かない
    assert _alive(p.pid)
    path, rec = _worker_record(fake, child={"pid": p.pid, "starttime": lib_mux._rex_start_time(p.pid)})
    rec["checkout"] = "/another/checkout"
    path.write_text(json.dumps(rec))
    assert not lib_mux.RexBackend().kill("Zed-worker")
    assert _alive(p.pid)


def test_handle_must_be_the_window_id_not_the_block_id():
    """§11 QA 3: handle に block_id を渡す実装は赤 — provenance は record.handle == window_id で比べる。"""
    rec = {"backend": "rex", "checkout": lib_mux._record_checkout_identity(), "handle": "window:W1",
           "server": {"endpoint": "e", "generation": "g"}, "child": {"pid": 5, "starttime": "9"}}
    args = dict(server_before=("e", "g"), server_after=("e", "g"), pid=5, starttime="9",
                ppid=7, server_pid=7)
    assert lib_mux.rex_worker_provenance(rec, window_id="window:W1", **args)[0]
    ok_, why = lib_mux.rex_worker_provenance(rec, window_id="block:AAA", **args)
    assert not ok_ and "handle" in why
    rec2 = dict(rec, handle="block:AAA")
    assert not lib_mux.rex_worker_provenance(rec2, window_id="window:W1", **args)[0]


def test_provenance_refuses_a_server_that_changed_between_the_two_reads():
    rec = {"backend": "rex", "checkout": lib_mux._record_checkout_identity(), "handle": "w",
           "server": {"endpoint": "e", "generation": "g1"}, "child": {"pid": 5, "starttime": "9"}}
    base = dict(window_id="w", pid=5, starttime="9", ppid=7, server_pid=7)
    assert lib_mux.rex_worker_provenance(rec, server_before=("e", "g1"), server_after=("e", "g1"), **base)[0]
    assert not lib_mux.rex_worker_provenance(rec, server_before=("e", "g1"), server_after=("e", "g2"), **base)[0]
    assert not lib_mux.rex_worker_provenance(rec, server_before=("e", "g1"), server_after=None, **base)[0]
    assert not lib_mux.rex_worker_provenance(rec, server_before=("e", "g1"), server_after=("e", "g1"),
                                             **dict(base, ppid=8))[0]


def test_server_changing_while_the_process_is_pinned_refuses_the_kill(fake, victim):
    p = victim()
    _arm(fake, p)
    fake.set(fake_rules_with_changing_instance(fake, p))
    assert not lib_mux.RexBackend().kill("Zed-worker")
    assert _alive(p.pid)


def fake_rules_with_changing_instance(fake, p):
    return [{"match": ["server", "status"], "seq": [
        {"out": status_json(pid=SERVER_PID, instance_id="inst-A")},
        {"out": status_json(pid=SERVER_PID, instance_id="inst-B")}]},
        _blocks_rule(),
        {"match": ["process", "block:AAA"], "out": json.dumps(
            {"last_exit": None, "child": {"pid": p.pid, "name": "sleep"}})}]


def test_pidfd_unavailable_means_no_kill_and_no_pid_addressed_signal(fake, victim, monkeypatch):
    p = victim()
    _arm(fake, p)
    sent = []
    real_kill = os.kill
    monkeypatch.setattr(os, "kill", lambda *a: sent.append(a) or real_kill(*a))
    monkeypatch.setattr(lib_mux, "_pidfd_open", lambda pid: None)
    assert not lib_mux.RexBackend().kill("Zed-worker")
    assert not lib_mux.RexBackend().kill("Zed-worker", allow_foreign=True)
    assert _alive(p.pid) and sent == []


def test_unsupported_machine_has_no_pidfd(monkeypatch):
    monkeypatch.setattr(lib_mux.platform, "machine", lambda: "riscv64")
    assert lib_mux._pidfd_open(os.getpid()) is None


def test_kill_works_without_os_pidfd_open_like_the_uv_build(fake, victim, monkeypatch):
    """uv 版 python 3.12 は os.pidfd_open が無い。ctypes の syscall だけで動くこと。"""
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    p = victim()
    _arm(fake, p)
    assert lib_mux.RexBackend().kill("Zed-worker")
    assert p.wait(timeout=5) is not None


def test_a_process_that_ignores_hup_and_term_is_taken_down_by_kill(fake, victim, monkeypatch):
    script = ("import signal,time,sys\n"
              "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
              "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
              "sys.stdout.write('r'); sys.stdout.flush()\n"
              "time.sleep(600)\n")
    p = subprocess.Popen([sys.executable, "-c", script], start_new_session=True, stdout=subprocess.PIPE)
    try:
        assert p.stdout.read(1) == b"r"
        monkeypatch.setattr(lib_mux, "_rex_grace", lambda sig: 0.4)
        _arm(fake, p)
        assert lib_mux.RexBackend().kill("Zed-worker")
        assert p.wait(timeout=5) == -signal.SIGKILL
    finally:
        p.kill()
        p.wait()


def test_kill_that_cannot_end_the_process_does_not_report_success(fake, victim, monkeypatch):
    p = victim()
    _arm(fake, p)
    monkeypatch.setattr(lib_mux, "_pidfd_signal", lambda fd, sig: "ok")     # 信号が効かない
    monkeypatch.setattr(lib_mux, "_rex_grace", lambda sig: 0.2)
    assert not lib_mux.RexBackend().kill("Zed-worker")
    assert _alive(p.pid)
    assert (fake.checkout / "registry" / "mux" / f"{lib_mux._pane_name('Zed-worker')}.json").exists()


def test_force_still_requires_a_direct_child_of_the_server(fake, victim):
    p = victim()
    _arm(fake, p)
    fake.set([{"match": ["server", "status"],
               "out": status_json(pid=SERVER_PID + 12345)}] + fake_rules_tail(p))
    b = lib_mux.RexBackend()
    assert not b.kill("Zed-worker", allow_foreign=True)
    assert _alive(p.pid)


def fake_rules_tail(p):
    return [_blocks_rule(), {"match": ["process", "block:AAA"], "out": json.dumps(
        {"last_exit": None, "child": {"pid": p.pid, "name": "sleep"}})}]


def test_nothing_running_in_the_block_is_left_alone_and_the_record_ends(fake):
    _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule(),
                              {"match": ["process", "block:AAA"], "out": json.dumps({"last_exit": {"exit_code": 0}})}])
    assert lib_mux.RexBackend().kill("Zed-worker")
    assert not any(c[:2] == ["block", "close"] for c in fake.calls())


# ---------------------------------------------------------------------------
# 再起動後の husk (§5.3 / QA 3b)
# ---------------------------------------------------------------------------

def test_restored_empty_shell_is_taken_with_sighup_only(fake, victim):
    """世代違いの記録 + 子の居ない shell -> SIGHUP だけで終わる (TERM/KILL は打たない)。"""
    p = victim(("sleep", "600"))
    # 偽の rex は『これは bash だ』と言う。実体の sleep は子を持たない = 空。
    _arm(fake, p, generation="inst-OLD:1", child_name="bash")
    seen = []
    real = lib_mux._pidfd_signal
    lib_mux_orig = lib_mux._pidfd_signal
    try:
        lib_mux._pidfd_signal = lambda fd, sig: seen.append(sig) or real(fd, sig)
        b = lib_mux.RexBackend()
        assert b.may_destroy("Zed-worker")[0]
        assert b.kill("Zed-worker")
    finally:
        lib_mux._pidfd_signal = lib_mux_orig
    assert seen == [signal.SIGHUP]
    assert p.wait(timeout=5) is not None


def test_restored_shell_with_a_child_is_not_taken(fake, victim):
    """③ 復元された shell が子を持つ (利用者が処理を始めた) なら拒否。"""
    parent = subprocess.Popen(["bash", "-c", "sleep 600 & wait"], start_new_session=True)
    try:
        assert _wait(lambda: lib_mux._live_children(parent.pid) not in (None, []))
        _arm(fake, parent, generation="inst-OLD:1", child_name="bash")
        b = lib_mux.RexBackend()
        assert not b.may_destroy("Zed-worker")[0]
        assert not b.kill("Zed-worker")
        assert _alive(parent.pid)
    finally:
        for c in lib_mux._live_children(parent.pid) or []:
            os.kill(c, signal.SIGKILL)
        parent.kill()
        parent.wait()


def test_restored_shell_that_is_not_a_shell_name_is_not_taken(fake, victim):
    p = victim()
    _arm(fake, p, generation="inst-OLD:1", child_name="claude")
    assert not lib_mux.RexBackend().kill("Zed-worker")
    assert _alive(p.pid)


# ---------------------------------------------------------------------------
# 子孫 (§11 QA 3c)
# ---------------------------------------------------------------------------

def _tree_script():
    return textwrap.dedent("""
        import os, signal, subprocess, sys, time
        kids = [subprocess.Popen(["sleep", "620"]),
                subprocess.Popen(["sleep", "621"], start_new_session=True)]   # setsid した子
        def fork_one(*_):
            kids.append(subprocess.Popen(["sleep", "622"]))
        signal.signal(signal.SIGUSR1, fork_one)
        sys.stdout.write("r\\n" + " ".join(str(k.pid) for k in kids) + "\\n"); sys.stdout.flush()
        while True:
            time.sleep(1)
    """)


def _start_tree():
    p = subprocess.Popen([sys.executable, "-c", _tree_script()], start_new_session=True,
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "r"
    kids = [int(x) for x in p.stdout.readline().split()]
    return p, kids


def test_destroy_tree_takes_every_descendant_even_when_the_parent_dies_first():
    root, kids = _start_tree()
    try:
        ok_ = lib_mux._rex_destroy_tree(
            root.pid, lib_mux._rex_start_time(root.pid),
            signals=(signal.SIGKILL,), grace=lambda s: 3.0, warn=lambda m: None)
        assert ok_
        assert _wait(lambda: not any(_alive(k) for k in kids))
        assert not _alive(root.pid)
    finally:
        root.kill()
        root.wait()
        for k in kids:
            if _alive(k):
                os.kill(k, signal.SIGKILL)


def test_a_child_forked_between_enumeration_and_signal_is_caught_by_the_rescan(monkeypatch):
    root, kids = _start_tree()
    spawned = []

    def inject():
        os.kill(root.pid, signal.SIGUSR1)
        assert _wait(lambda: len(lib_mux._live_children(root.pid) or []) >= 2, 3)
        # 3 本目 (sleep 622) の pid を控える
        spawned.extend(c for c in lib_mux._live_children(root.pid) if c not in kids)

    monkeypatch.setattr(lib_mux, "_rex_after_enumerate", inject)
    try:
        assert lib_mux._rex_destroy_tree(
            root.pid, lib_mux._rex_start_time(root.pid),
            signals=(signal.SIGKILL,), grace=lambda s: 3.0, warn=lambda m: None)
        assert spawned
        assert _wait(lambda: not any(_alive(k) for k in kids + spawned))
    finally:
        root.kill()
        root.wait()
        for k in kids + spawned:
            if _alive(k):
                os.kill(k, signal.SIGKILL)


def test_a_recycled_pid_in_the_tree_gets_no_signal(monkeypatch):
    """列挙時の starttime と pidfd を開いた後の starttime が違えば (pid 再利用)、信号を送らない。"""
    root = subprocess.Popen(["sleep", "630"], start_new_session=True)
    bystander = subprocess.Popen(["sleep", "631"], start_new_session=True)
    try:
        real = lib_mux._rex_scan_tree

        def lying(root_pid, root_sid, known=()):
            tree = real(root_pid, root_sid, known)
            tree[bystander.pid] = (root_pid, "1")        # 偽の starttime = 列挙後に pid が再利用された
            return tree

        monkeypatch.setattr(lib_mux, "_rex_scan_tree", lying)
        assert lib_mux._rex_destroy_tree(
            root.pid, lib_mux._rex_start_time(root.pid),
            signals=(signal.SIGKILL,), grace=lambda s: 3.0, warn=lambda m: None)
        assert root.wait(timeout=5) is not None
        assert _alive(bystander.pid)
    finally:
        bystander.kill()
        bystander.wait()
        root.kill()
        root.wait()


def test_destroy_tree_refuses_a_recycled_root_pid():
    root = subprocess.Popen(["sleep", "632"], start_new_session=True)
    try:
        msgs = []
        assert not lib_mux._rex_destroy_tree(
            root.pid, "1", signals=(signal.SIGKILL,), grace=lambda s: 0.2, warn=msgs.append)
        assert _alive(root.pid) and msgs
    finally:
        root.kill()
        root.wait()


def test_descendants_that_survive_make_the_kill_fail(monkeypatch):
    root, kids = _start_tree()
    try:
        real = lib_mux._pidfd_signal
        # 子 (sleep 620) にだけ信号を握りつぶす
        victim_pid = kids[0]
        original_open = lib_mux._pidfd_open
        fds = {}

        def open_(pid):
            fd = original_open(pid)
            fds[fd] = pid
            return fd

        monkeypatch.setattr(lib_mux, "_pidfd_open", open_)
        monkeypatch.setattr(lib_mux, "_pidfd_signal",
                            lambda fd, sig: "ok" if fds.get(fd) == victim_pid else real(fd, sig))
        msgs = []
        assert not lib_mux._rex_destroy_tree(
            root.pid, lib_mux._rex_start_time(root.pid),
            signals=(signal.SIGKILL,), grace=lambda s: 0.5, warn=msgs.append)
        assert _alive(victim_pid) and msgs
    finally:
        root.kill()
        root.wait()
        for k in kids:
            if _alive(k):
                os.kill(k, signal.SIGKILL)


# ---------------------------------------------------------------------------
# 選択・設定・キー
# ---------------------------------------------------------------------------

def test_rex_is_selected_only_when_asked_for(monkeypatch, tmp_path):
    monkeypatch.setenv("CREWVIA_MUX", "rex")
    assert lib_mux._select_backend() is lib_mux.RexBackend
    monkeypatch.setenv("CREWVIA_MUX", "tmux")
    assert lib_mux._select_backend() is lib_mux.TmuxBackend
    monkeypatch.setenv("CREWVIA_MUX", "herdr")
    assert lib_mux._select_backend() is lib_mux.HerdrBackend
    monkeypatch.delenv("CREWVIA_MUX")
    cfg = tmp_path / "crewvia.yaml"
    cfg.write_text("mode: rex   # trying it\n")
    assert lib_mux._config_mode(cfg) == "rex"
    monkeypatch.setattr(lib_mux, "_config_mode", lambda *a, **k: "rex")
    assert lib_mux._select_backend() is lib_mux.RexBackend
    monkeypatch.setattr(lib_mux, "_config_mode", lambda *a, **k: None)
    assert lib_mux._select_backend() is lib_mux.TmuxBackend       # 何も選ばなければ従来どおり


def test_rex_key_names_and_the_other_backends_are_unchanged():
    assert lib_mux.translate_keys(["Up", "Enter", "ESC", "tab"], "rex") == (
        ["up", "enter", "escape", "tab"], [])
    assert lib_mux.translate_keys(["Escape"], "herdr") == (["esc"], [])
    assert lib_mux.translate_keys(["Escape"], "tmux") == (["Escape"], [])
    assert lib_mux.translate_keys(["1"], "rex") == ([], ["1"])


def test_state_is_unknown_and_attach_cmd_is_not_claimed(fake):
    b = lib_mux.RexBackend()
    assert b.state("Zed-worker") == "unknown"
    assert b.attach_cmd("Zed-worker") is None
