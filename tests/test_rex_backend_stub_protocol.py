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


def _wait_idle_login_shell(p, seconds=20.0):
    """`exec -a -bash bash` が login shell の起動 (profile / bashrc) を終え、stdin を待つまで待つ。

    以前は `time.sleep(0.4)` だった。login shell は起動中に profile / bashrc が子プロセス
    (`node -v` など) を作るので、CPU が混むと 0.4 秒では終わらず、本体は『子のいる shell』と
    正しく拒否する (xdist 下の CI で落ちた。負荷だけで再現する)。『子が居らず、2 回続けて
    同じ』を確かめてから進む。時間では待たない。
    """
    deadline = time.time() + seconds
    stable = 0
    while time.time() < deadline:
        idle = (lib_mux._process_argv(p.pid) == ["-bash"]
                and lib_mux._live_children(p.pid) == [])
        stable = stable + 1 if idle else 0
        if stable >= 6:          # 0.05s 間隔で 0.3 秒続けて子が居ない
            return
        time.sleep(0.05)
    raise AssertionError("the login shell never settled into an idle state")


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
    # 候補文は 1 画面からは入力と区別できない (False)。区別は _clear_input の ctrl+u。
    assert lib_mux._rex_input_is_empty(empty) is False
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


_LIVE = {"match": ["process", "block:AAA"], "out": json.dumps(
    {"last_exit": None, "child": {"pid": 4242, "name": "claude"}})}


def _blocks_rule(label="Zed-worker", block_id="block:AAA"):
    return {"match": ["block", "ls"], "out": json.dumps({"blocks": [
        {"block_id": block_id, "label": lib_mux._pane_name(label), "window_id": "window:W1"}]})}


def test_send_without_a_clean_input_box_sends_no_enter(fake):
    rule = "─" * 40
    dirty = f"{rule}\n❯ leftover\n  more\n{rule}\n"
    _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule(), _LIVE, {"match": ["capture"], "out": dirty},
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
    fake.set(_base_rules() + [_blocks_rule(), _LIVE, {"match": ["capture"], "out": clean},
                              {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])
    assert lib_mux.RexBackend().send("Zed-worker", "hello")
    kinds = [c[0] for c in fake.calls() if c[0] in ("send", "send-key")]
    # 候補文は ctrl+u で変わらない = 空。その確認の ctrl+u が先頭に 1 回入る。
    assert kinds == ["send-key", "send", "send-key"]


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
    (fake.checkout / "registry" / "mux" / f"{pname}.json").unlink()   # 1 回目の記録
    try:
        fake.set(_spawn_rules(sessions=[{"session_id": "s1", "label": label}], blocks=blocks,
                              procs={"block:X": shell, "block:Y": _proc(live.pid)}))
        b = lib_mux.RexBackend()
        assert not b.spawn("Zed-worker", "true")
        assert b.last_spawn_refusal == lib_mux.SPAWN_OCCUPIED
    finally:
        live.kill()
        live.wait()


def test_spawn_cannot_take_the_spawn_lock_means_no_spawn(fake, monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def no_lock(*a, **k):
        yield False

    monkeypatch.setattr(lib_mux, "_rex_spawn_lock", no_lock)
    fake.set(_spawn_rules(sessions=[], blocks=[]))
    assert not lib_mux.RexBackend().spawn("Zed-worker", "true")
    assert not any(c[0] in ("run", "new") for c in fake.calls())


def test_spawn_does_not_call_rex_under_the_record_lock(fake, monkeypatch):
    import contextlib
    held = []

    @contextlib.contextmanager
    def watch(*a, **k):
        held.append(1)
        try:
            yield True
        finally:
            held.pop()

    real = lib_mux._rex_run

    def spy(*a, **k):
        assert not held, "rex was called while the record lock was held"
        return real(*a, **k)

    monkeypatch.setattr(lib_mux, "_pane_record_lock", watch)
    monkeypatch.setattr(lib_mux, "_rex_run", spy)
    fake.set(_spawn_rules(sessions=[], blocks=[]))
    lib_mux.RexBackend().spawn("Zed-worker", "true")


# ---------------------------------------------------------------------------
# kill: 本人確認 + pidfd
# ---------------------------------------------------------------------------

@pytest.fixture
def victim(fake):
    """偽の rex が『block:AAA の子』と言う本物のプロセス (ppid == このテスト = 偽の server)。"""
    procs = []

    def make(argv=("sleep", "600")):
        p = subprocess.Popen(list(argv), start_new_session=True,
                             stdin=subprocess.PIPE)
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
    # 復元された shell の実測した cmdline `-bash` (引数なし)。子は持たない = 空。
    p = victim(("bash", "-c", "exec -a -bash bash"))
    _wait_idle_login_shell(p)
    assert lib_mux._process_argv(p.pid) == ["-bash"]
    _arm(fake, p, generation="inst-OLD:1", child_name="bash")
    seen = []
    real = lib_mux._pidfd_signal
    lib_mux_orig = lib_mux._pidfd_signal
    try:
        lib_mux._pidfd_signal = lambda fd, sig: seen.append(sig) or real(fd, sig)
        b = lib_mux.RexBackend()
        ok, why = b.may_destroy("Zed-worker")
        assert ok, why
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


def _fork_once_seam(root, kids, spawned):
    done = []

    def inject():
        if done:
            return
        done.append(1)
        os.kill(root.pid, signal.SIGUSR1)
        assert _wait(lambda: len(lib_mux._live_children(root.pid) or []) >= 2, 3)
        spawned.extend(c for c in lib_mux._live_children(root.pid) if c not in kids)
    return inject


def test_a_child_forked_between_enumeration_and_signal_is_caught_by_the_next_stage(monkeypatch):
    """本番の順序 (列挙 -> 信号) のまま: 列挙の直後に生まれた子は、その段では信号を受けないが、
    最終走査で残党として見つかり、次の段の列挙で捕まる。"""
    root, kids = _start_tree()
    spawned = []
    monkeypatch.setattr(lib_mux, "_rex_after_enumerate", _fork_once_seam(root, kids, spawned))
    try:
        assert lib_mux._rex_destroy_tree(
            root.pid, lib_mux._rex_start_time(root.pid),
            signals=(signal.SIGTERM, signal.SIGKILL), grace=lambda s: 3.0, warn=lambda m: None)
        assert spawned
        assert _wait(lambda: not any(_alive(k) for k in kids + spawned))
    finally:
        root.kill()
        root.wait()
        for k in kids + spawned:
            if _alive(k):
                os.kill(k, signal.SIGKILL)


def test_a_child_forked_during_the_last_stage_is_caught_by_the_final_scan_and_fails_the_kill(monkeypatch):
    """最終段で生まれた子は信号を受けていない。最終走査が見つけ、成功を名乗らない。"""
    root, kids = _start_tree()
    spawned = []
    monkeypatch.setattr(lib_mux, "_rex_after_enumerate", _fork_once_seam(root, kids, spawned))
    msgs = []
    try:
        assert not lib_mux._rex_destroy_tree(
            root.pid, lib_mux._rex_start_time(root.pid),
            signals=(signal.SIGKILL,), grace=lambda s: 3.0, warn=msgs.append)
        assert spawned and msgs
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

        calls = []

        def lying(root_pid, root_sid, known=()):
            tree = real(root_pid, root_sid, known)
            if not calls:       # 列挙の 1 回目だけ嘘をつく (最終走査は本物)
                tree[bystander.pid] = (root_pid, "1")    # 偽の starttime = 列挙後に pid が再利用された
            calls.append(1)
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


# ---------------------------------------------------------------------------
# Codex r1 の指摘の回帰 (P1×4 / P2×1)
# ---------------------------------------------------------------------------

def test_send_refuses_when_the_capture_is_unreadable_or_has_no_input_box(fake):
    """読めない / 入力欄が見えない画面は『空』ではない: 何も送らない。"""
    _worker_record(fake)
    for out, rc in (("", 1), ("", 0), ("$ \n", 0)):
        fake.set(_base_rules() + [_blocks_rule(), _LIVE, {"match": ["capture"], "out": out, "rc": rc},
                                  {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])
        assert not lib_mux.RexBackend().send("Zed-worker", "hello")
        calls = fake.calls()
        assert not any(c[0] == "send" for c in calls)
        assert not any(c[0] == "send-key" and "enter" in c for c in calls)


def _destroy_with_final_scan(final):
    """最終走査 (2 回目の scan) だけ `final(tree)` で差し替えて破壊を走らせる。"""
    root = subprocess.Popen(["sleep", "640"], start_new_session=True)
    real = lib_mux._rex_scan_tree
    calls = []

    def scan(root_pid, root_sid, known=()):
        calls.append(1)
        tree = real(root_pid, root_sid, known)
        return tree if len(calls) < 2 else final(tree)

    orig = lib_mux._rex_scan_tree
    lib_mux._rex_scan_tree = scan
    try:
        msgs = []
        ok = lib_mux._rex_destroy_tree(
            root.pid, lib_mux._rex_start_time(root.pid),
            signals=(signal.SIGKILL,), grace=lambda s: 3.0, warn=msgs.append)
        return ok, msgs
    finally:
        lib_mux._rex_scan_tree = orig
        root.kill()
        root.wait()


def test_a_surviving_session_member_without_ancestry_makes_the_kill_fail():
    """親の死で再親子化された生存者 (held への祖先鎖が無い) も残党: 成功と言わない。"""
    orphan = subprocess.Popen(["sleep", "641"], start_new_session=True)
    try:
        st = lib_mux._rex_start_time(orphan.pid)

        def final(tree):
            tree[orphan.pid] = (1, st)
            return tree

        ok, msgs = _destroy_with_final_scan(final)
        assert not ok and msgs
        assert _alive(orphan.pid)
    finally:
        orphan.kill()
        orphan.wait()


def test_a_failed_final_scan_is_unobserved_not_empty():
    ok, msgs = _destroy_with_final_scan(lambda tree: None)
    assert not ok and msgs


@pytest.fixture
def husk_proc():
    """子を持たない `-bash` (復元された login shell と同じ cmdline) の実プロセス。"""
    p = subprocess.Popen(["bash", "-c", "exec -a -bash bash"], start_new_session=True,
                         stdin=subprocess.PIPE)
    _wait_idle_login_shell(p)
    yield p
    p.kill()
    p.wait()


def _husk_rules(fake, husk, *, record_patch=None, after_hup=True, generation="inst-OLD:1"):
    """世代違いの記録 + 子の居ない shell。SIGHUP 後は殻が終わった (last_exit) と答える。"""
    path, rec = _worker_record(fake, block_id="block:X", generation=generation)
    if record_patch:
        rec.update(record_patch)
        path.write_text(json.dumps(rec))
    label, pname = lib_mux._rex_session_label(), lib_mux._pane_name("Zed-worker")
    shell = _proc(husk.pid, name="bash")
    ended = {"out": json.dumps({"last_exit": {"exit_code": 129}})}
    seq = [shell, shell, ended] if after_hup else [shell]
    rules = _spawn_rules(sessions=[{"session_id": "session:S1", "label": label}],
                         blocks=[{"block_id": "block:X", "label": pname, "window_id": "window:W1"}],
                         procs={"block:NEW": _proc(husk.pid)})
    rules.insert(0, {"match": ["process", "block:X"], "seq": seq})
    fake.set(rules + [{"match": ["capture"], "out": ""},
                      {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])


@pytest.mark.parametrize("patch", [{"checkout": "/somewhere/else"}, {"handle": "window:OTHER"}])
def test_husk_is_not_reused_for_a_foreign_checkout_or_another_window(fake, husk_proc, patch):
    _husk_rules(fake, husk_proc, record_patch=patch)
    b = lib_mux.RexBackend()
    assert not b.spawn("Zed-worker", "true")
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    assert not any(c[0] in ("send", "send-key", "run") for c in fake.calls())
    assert _alive(husk_proc.pid)


def test_husk_is_rebuilt_not_typed_into(fake, husk_proc):
    """F2: 殻へは 1 文字も打たない。SIGHUP で終え、通常の rex run で新しい block を作り、
    記録の block_id を新しいものに書き換える。block close はしない。"""
    _husk_rules(fake, husk_proc)
    assert lib_mux.RexBackend().spawn("Zed-worker", "exec claude", cwd="/tmp/it's a dir")
    calls = fake.calls()
    assert not any(c[0] in ("send", "send-key", "capture") for c in calls)
    assert not any(c[:2] == ["block", "close"] for c in calls)
    run = [c for c in calls if c[0] == "run"]
    assert len(run) == 1
    assert run[0][run[0].index("--cwd") + 1] == "/tmp/it's a dir"
    assert run[0][run[0].index("--") + 1:] == ["bash", "-c", "exec claude"]
    assert husk_proc.wait(timeout=5) == -signal.SIGHUP
    rec = json.loads((fake.checkout / "registry" / "mux" /
                      f"{lib_mux._pane_name('Zed-worker')}.json").read_text())
    assert rec["block_id"] == "block:NEW" and rec["window_id"] == "window:NEW"


def test_husk_that_does_not_end_after_sighup_is_not_relaunched(fake, husk_proc):
    _husk_rules(fake, husk_proc, after_hup=False)    # 終わったと言わない
    b = lib_mux.RexBackend()
    assert not b.spawn("Zed-worker", "exec claude")
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    assert not any(c[0] == "run" for c in fake.calls())


def test_husk_with_the_same_generation_record_is_refused_without_a_signal(fake, husk_proc):
    _husk_rules(fake, husk_proc, generation=f"inst-A:{SERVER_PID}")
    assert not lib_mux.RexBackend().spawn("Zed-worker", "exec claude")
    assert _alive(husk_proc.pid)
    assert not any(c[0] == "run" for c in fake.calls())


def test_spawn_that_cannot_write_its_record_is_not_a_success(fake, monkeypatch, capsys):
    child = subprocess.Popen(["sleep", "600"])
    try:
        label = lib_mux._rex_session_label()
        fake.set(_spawn_rules(sessions=[{"session_id": "session:S1", "label": label}], blocks=[],
                              procs={"block:NEW": _proc(child.pid)}))
        monkeypatch.setattr(lib_mux, "write_pane_record", lambda *a, **k: False)
        assert not lib_mux.RexBackend().spawn("Zed-worker", "exec sleep 600")
        err = capsys.readouterr().err
        assert "block:NEW" in err and "window:NEW" in err
    finally:
        child.kill()
        child.wait()


def test_spawn_is_not_managed_when_the_server_generation_changed_during_run(fake, capsys, monkeypatch):
    """r4 F2: `rex run` の間に server が入れ替わった (世代が違う) block は管理された spawn と報告しない。"""
    child = subprocess.Popen(["sleep", "600"])
    try:
        label = lib_mux._rex_session_label()
        fake.set(_spawn_rules(sessions=[{"session_id": "session:S1", "label": label}], blocks=[],
                              procs={"block:NEW": _proc(child.pid)}))
        real = lib_mux._rex_status

        def status_after_run():
            st = real()
            if st is not None and any(c[0] == "run" for c in fake.calls()):
                st = dict(st, instance_id="inst-B")      # run の後は別の世代
            return st

        monkeypatch.setattr(lib_mux, "_rex_status", status_after_run)
        assert lib_mux._rex_generation(real()) != lib_mux._rex_generation(
            dict(real(), instance_id="inst-B"))
        assert not lib_mux.RexBackend().spawn("Zed-worker", "exec sleep 600")
        err = capsys.readouterr().err
        assert "block:NEW" in err and "window:NEW" in err
        assert not (fake.checkout / "registry" / "mux" /
                    f"{lib_mux._pane_name('Zed-worker')}.json").exists()
    finally:
        child.kill()
        child.wait()


def test_clear_input_sends_ctrl_e_before_ctrl_u_in_one_call(fake):
    """r4 F1: カーソルが行頭だと ctrl+u は何も消さない。行末へ送ってから消す。"""
    rule = _rule()
    cand = f"{rule}\n❯ Fix the failing test in foo.py\n{rule}\n"
    _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule(), _LIVE, {"match": ["capture"], "out": cand},
                              {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])
    assert lib_mux.RexBackend().send("Zed-worker", "hello")
    first = [c for c in fake.calls() if c[0] == "send-key"][0]
    assert first[-2:] == ["ctrl+e", "ctrl+u"]


# ---------------------------------------------------------------------------
# Codex r2 の指摘の回帰 (F1 走査 / F3 process の正の証拠)
# ---------------------------------------------------------------------------

def test_scan_tree_treats_an_unreadable_entry_as_unobserved_but_skips_a_vanished_one(monkeypatch):
    """F1: 消えた entry (GONE) は飛ばす。読めない entry は None (未観測) で返す。"""
    root = subprocess.Popen(["sleep", "660"], start_new_session=True)
    other = subprocess.Popen(["sleep", "661"], start_new_session=True)
    try:
        real = lib_mux._proc_stat_fields
        mode = {}

        def fake_stat(n, *a, **k):
            if str(n) == str(other.pid) and mode:
                return mode["status"], ""
            return real(n, *a, **k)

        monkeypatch.setattr(lib_mux, "_proc_stat_fields", fake_stat)
        mode["status"] = lib_mux._PROC_GONE
        tree = lib_mux._rex_scan_tree(root.pid, str(root.pid))
        assert tree is not None and root.pid in tree and other.pid not in tree
        mode["status"] = lib_mux._PROC_UNREADABLE
        assert lib_mux._rex_scan_tree(root.pid, str(root.pid)) is None
    finally:
        for p in (root, other):
            p.kill()
            p.wait()


def test_destroy_refuses_when_a_scanned_entry_is_unreadable(monkeypatch):
    """F1: 走査が読めない entry を見つけたら、信号は 1 つも送らず失敗。"""
    root = subprocess.Popen(["sleep", "662"], start_new_session=True)
    other = subprocess.Popen(["sleep", "663"], start_new_session=True)
    try:
        real = lib_mux._proc_stat_fields
        monkeypatch.setattr(lib_mux, "_proc_stat_fields",
                            lambda n, *a, **k: (lib_mux._PROC_UNREADABLE, "")
                            if str(n) == str(other.pid) else real(n, *a, **k))
        msgs = []
        assert not lib_mux._rex_destroy_tree(
            root.pid, lib_mux._rex_start_time(root.pid),
            signals=(signal.SIGKILL,), grace=lambda s: 1.0, warn=msgs.append)
        assert msgs and _alive(root.pid)
    finally:
        for p in (root, other):
            p.kill()
            p.wait()


def _destroy_with_failing_child_pidfd(err, gone):
    root, kids = _start_tree()
    real_open = lib_mux._pidfd_open
    real_stat = lib_mux._proc_stat_fields
    victim_pid = kids[0]

    def open_(pid):
        if pid == victim_pid:
            lib_mux._PIDFD_OPEN_ERRNO = err
            return None
        return real_open(pid)

    lib_mux._pidfd_open = open_
    if gone:      # 開けない理由が『もう居ない』のとき /proc も居ないと答える
        lib_mux._proc_stat_fields = lambda n, *a, **k: (
            (lib_mux._PROC_GONE, "") if str(n) == str(victim_pid) else real_stat(n, *a, **k))
    try:
        msgs = []
        ok = lib_mux._rex_destroy_tree(
            root.pid, lib_mux._rex_start_time(root.pid),
            signals=(signal.SIGKILL,), grace=lambda s: 3.0, warn=msgs.append)
        return ok, msgs, root
    finally:
        lib_mux._pidfd_open = real_open
        lib_mux._proc_stat_fields = real_stat
        for k in kids:
            if _alive(k):
                os.kill(k, signal.SIGKILL)


def test_a_child_pidfd_that_cannot_be_opened_refuses_but_one_that_vanished_does_not():
    import errno as _errno
    ok, msgs, root = _destroy_with_failing_child_pidfd(_errno.EPERM, gone=False)
    try:
        assert not ok and _alive(root.pid)          # 開けない = 未観測 -> 何も殺さない
    finally:
        root.kill()
        root.wait()
    ok, msgs, root = _destroy_with_failing_child_pidfd(_errno.ESRCH, gone=True)
    try:
        assert ok                                    # 消えた子は対象外
    finally:
        root.kill()
        root.wait()


@pytest.mark.parametrize("answer", [
    {}, {"child": {}}, {"child": {"pid": "x", "name": "sleep"}}, {"child": {"pid": 0}},
    {"last_exit": None}, {"last_exit": {}}, {"last_exit": 0}, [], "x",
])
def test_process_answer_without_positive_evidence_is_unreadable_and_kill_keeps_the_record(fake, answer):
    """F3: `{}` や不正な child は『終わった』の証拠ではない。kill は記録を消さない。"""
    path, _rec = _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule(),
                              {"match": ["process", "block:AAA"], "out": json.dumps(answer)}])
    b = lib_mux.RexBackend()
    assert b._process("block:AAA")[0] == "unreadable"
    assert not b.kill("Zed-worker")
    assert path.exists()


# ---------------------------------------------------------------------------
# Codex r3 の指摘の回帰 (F1 走査の種 / F2 候補文の判定)
# ---------------------------------------------------------------------------

def _rule(label="x"):
    return "─" * 40


def test_clear_input_treats_a_suggestion_of_any_wording_as_empty(fake):
    """F2: 候補文は `Try "` で始まるとは限らない。ctrl+u で変わらない 1 行 = 候補文。"""
    rule = _rule()
    cand = f"{rule}\n❯ Fix the failing test in foo.py\n{rule}\n"
    _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule(), _LIVE, {"match": ["capture"], "out": cand},
                              {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])
    assert lib_mux.RexBackend().send("Zed-worker", "hello")
    kinds = [c[0] for c in fake.calls() if c[0] in ("send", "send-key")]
    assert kinds == ["send-key", "send", "send-key"]


def test_clear_input_does_not_mistake_real_text_starting_with_try_for_a_suggestion(fake):
    """F2: 本物の入力が `Try "` で始まっても、ctrl+u で消える → 入力。空になるまで送らない。"""
    rule = _rule()
    typed = f"{rule}\n❯ Try \"my own text\"\n{rule}\n"
    empty = f"{rule}\n❯ \n{rule}\n"
    _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule(), _LIVE,
                              {"match": ["capture"], "seq": [{"out": typed}, {"out": typed}, {"out": empty}]},  # 1 本目は ❯ 待ち
                              {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])
    assert lib_mux.RexBackend().send("Zed-worker", "hello")
    seq = [(c[0], "ctrl+u" in c) for c in fake.calls() if c[0] in ("send", "send-key")]
    # ctrl+u で消えたことを確かめてから send。候補文として素通しにしない。
    assert seq[0] == ("send-key", True)
    assert [k for k, _ in seq].index("send") > 0


def test_clear_input_refuses_when_one_line_text_is_unchanged_only_if_multiline(fake):
    """複数行は候補文ではない: ctrl+u で変わらなくても送らない。"""
    rule = _rule()
    multi = f"{rule}\n❯ line1\n  line2\n{rule}\n"
    _worker_record(fake)
    fake.set(_base_rules() + [_blocks_rule(), _LIVE, {"match": ["capture"], "out": multi},
                              {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])
    assert not lib_mux.RexBackend().send("Zed-worker", "hello")
    assert not any(c[0] == "send" for c in fake.calls())


def test_scan_with_no_live_seed_does_not_pull_in_a_recycled_pids_children():
    """F1: 種が空 (終わった pid は種にしない) なら、その pid の子は巻き込まない。"""
    parent = subprocess.Popen(["sh", "-c", "sleep 662 & wait"], start_new_session=True)
    try:
        time.sleep(0.3)
        assert lib_mux._rex_scan_tree(parent.pid, None, known=set()) == {}
        assert parent.pid in lib_mux._rex_scan_tree(parent.pid, None)
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        parent.wait()


def test_destroy_does_not_seed_the_scan_with_a_recycled_root_pid(monkeypatch):
    """F1: root が終わって pid が別のプロセスに回収されたら (starttime が変わる)、
    以降の走査の種に root を入れない。"""
    root = subprocess.Popen(["sleep", "663"], start_new_session=True)
    try:
        st = lib_mux._rex_start_time(root.pid)
        seen = []
        real_scan = lib_mux._rex_scan_tree
        real_st = lib_mux._rex_start_time
        recycled = {"on": False}

        def spy(rp, sid, known=None):
            seen.append((recycled["on"], None if known is None else set(known)))
            return real_scan(rp, sid, known=known)

        def fake_start(pid):
            if recycled["on"] and pid == root.pid:
                return "recycled"
            return real_st(pid)

        monkeypatch.setattr(lib_mux, "_rex_scan_tree", spy)
        monkeypatch.setattr(lib_mux, "_rex_start_time", fake_start)
        monkeypatch.setattr(lib_mux, "_rex_after_enumerate",
                            lambda: recycled.__setitem__("on", True))
        lib_mux._rex_destroy_tree(root.pid, st, signals=[signal.SIGKILL],
                                  grace=lambda s: 2.0, warn=lambda m: None)
        after = [k for flag, k in seen if flag]
        assert after, "the post-signal scan was not reached"
        assert all(k is not None and root.pid not in k for k in after), after
    finally:
        root.kill()
        root.wait()


# ---------------------------------------------------------------------------
# r6: 所有の検査は世代に依らず、通常 / husk の両方の前に置く (F1)
# ---------------------------------------------------------------------------

_NOT_OURS = [
    ("no record", None),
    ("another checkout", {"checkout": "/somewhere/else"}),
    ("another window handle", {"handle": "window:OTHER"}),
    ("no server bound", {"server": None}),
]


@pytest.mark.parametrize("label,patch", _NOT_OURS)
def test_kill_of_a_restarted_server_block_needs_ownership(fake, victim, label, patch):
    p = victim(("sleep", "600"))
    path = fake.checkout / "registry" / "mux" / f"{lib_mux._pane_name('Zed-worker')}.json"
    _arm(fake, p, generation="inst-OLD:1", child_name="bash")
    if patch is None:
        path.unlink()
    else:
        rec = json.loads(path.read_text())
        rec.update(patch)
        path.write_text(json.dumps(rec))
    b = lib_mux.RexBackend()
    assert not b.may_destroy("Zed-worker")[0]
    assert not b.kill("Zed-worker")
    assert p.poll() is None


@pytest.mark.parametrize("label,patch", _NOT_OURS)
def test_spawn_into_a_restarted_server_husk_needs_ownership(fake, husk_proc, label, patch):
    _husk_rules(fake, husk_proc, record_patch=patch if patch else None)
    path = fake.checkout / "registry" / "mux" / f"{lib_mux._pane_name('Zed-worker')}.json"
    if patch is None:
        path.unlink()
    assert not lib_mux.RexBackend().spawn("Zed-worker", "true")
    assert _alive(husk_proc.pid)
    assert not any(c[0] == "run" for c in fake.calls())


def test_spawn_without_a_child_writes_no_record(fake, capsys):
    label = lib_mux._rex_session_label()
    fake.set(_spawn_rules(sessions=[{"session_id": "session:S1", "label": label}], blocks=[],
                          procs={"block:NEW": {"out": json.dumps({"last_exit": None})}}))
    assert not lib_mux.RexBackend().spawn("Zed-worker", "true")
    err = capsys.readouterr().err
    assert "block:NEW" in err and "window:NEW" in err
    assert not (fake.checkout / "registry" / "mux" /
                f"{lib_mux._pane_name('Zed-worker')}.json").exists()


def test_spawn_with_unreadable_starttime_writes_no_record(fake, monkeypatch, capsys):
    child = subprocess.Popen(["sleep", "600"])
    try:
        label = lib_mux._rex_session_label()
        fake.set(_spawn_rules(sessions=[{"session_id": "session:S1", "label": label}], blocks=[],
                              procs={"block:NEW": _proc(child.pid)}))
        monkeypatch.setattr(lib_mux, "_rex_start_time", lambda pid: None)
        assert not lib_mux.RexBackend().spawn("Zed-worker", "true")
        assert "block:NEW" in capsys.readouterr().err
        assert not (fake.checkout / "registry" / "mux" /
                    f"{lib_mux._pane_name('Zed-worker')}.json").exists()
    finally:
        child.kill()
        child.wait()


def test_send_types_nothing_into_a_bare_shell_even_when_a_clean_box_is_on_screen(fake):
    """r8 F1: 画面に空の入力欄が残っていても、前景が素の shell / 不明 / child 無しなら送らない。"""
    rule = _rule()
    clean = f"{rule}\n❯ \n{rule}\n"
    _worker_record(fake)
    shell_child = subprocess.Popen(["sleep", "600"])      # comm=sleep; name is what rex reports
    try:
        for out in (
            {"last_exit": {"exit_code": 0}},                                     # no child
            {"last_exit": None, "child": {"pid": shell_child.pid, "name": "bash"}},   # bare shell name, cmdline sleep -> LIVE? see below
            {},                                                                  # unreadable
        ):
            if out.get("child"):
                continue          # bare-shell cmdline form is covered by test_husk_state_requires_a_bare_shell_cmdline
            fake.set(_base_rules() + [_blocks_rule(),
                                      {"match": ["process", "block:AAA"], "out": json.dumps(out)},
                                      {"match": ["capture"], "out": clean},
                                      {"match": ["send-key"], "out": ""}, {"match": ["send"], "out": ""}])
            assert not lib_mux.RexBackend().send("Zed-worker", "hello")
            assert not any(c[0] in ("send", "send-key") for c in fake.calls())
    finally:
        shell_child.kill()
        shell_child.wait()
