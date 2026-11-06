#!/usr/bin/env python3
"""tests/test_hc_backend.py

HcBackend (knowledge/herdr-core-mux-backend.md, t002) を **偽の `hc`** (PATH 上
の stub, tests/hc_harness.py) で確かめる。CI で常に走る — 本物の hc には一切
触れない。

固定する契約 (設計 §3〜§8):
  - 識別が要る動詞 (`pid` / `state` / `record_existence` / `kill` の daemon 分岐)
    は `address`（spawn 時の handle）を使い、`name` の使い回しに惑わされない
  - `record_existence`: `not_found` だけが GONE。`finished` / `name_taken` /
    `usage` / 未知の kind はすべて UNOBSERVED。世代 (`core.id` + `holder.instance`)
    が前後で変わっていたら UNOBSERVED
  - `available()`: `hello.generation` が `_HC_GENERATIONS` に無ければ False
    (fail-closed。herdr の「警告だけで続行」ではない)
  - `state()`: `reported` が `true` でなければ常に `"unknown"`（`idle` に倒さない）
  - `spawn()`: argv は `bash -lc "export PATH=...; " + cmd`（`scripts/bin` と
    `$HOME/.local/bin` を含む）で hc server 自身の素の PATH を補う
  - `list()`: `hc connect` の `terminal.list` を `{"all": false}` で呼び、
    `labels.crewvia_name` で名前を復元する（CLI `hc ls` は使わない）

  python3 -m pytest tests/test_hc_backend.py -v
"""

import json
import sys
from pathlib import Path

import pytest

from hc_harness import FakeHc, connect_error, connect_result, err_json, ok, \
    status_json, terminal_json
from rex_harness import make_checkout

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lib_mux  # noqa: E402


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = FakeHc(tmp_path, monkeypatch)
    checkout = make_checkout(tmp_path)
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: checkout)
    monkeypatch.setenv("CREWVIA_REPO_ROOT", str(checkout))
    f.checkout = checkout
    return f


def _status_rule(**kw):
    return {"match": ["server", "status"], **ok(out=status_json(**kw))}


def backend():
    return lib_mux.HcBackend()


# ---------------------------------------------------------------------------
# available() — version pinning (design doc §4.2; mutation target)
# ---------------------------------------------------------------------------

def test_available_true_when_generation_matches(fake):
    fake.set([
        {"match": ["server", "start"], **ok()},
        connect_result("hello", {"generation": 1}),
    ])
    assert backend().available() is True


def test_available_false_on_generation_mismatch(fake, capsys):
    """RED proof target #2: removing this check must turn this test red."""
    fake.set([
        {"match": ["server", "start"], **ok()},
        connect_result("hello", {"generation": 2}),
    ])
    assert backend().available() is False
    assert "generation" in capsys.readouterr().err


def test_available_false_when_hello_unreadable(fake):
    fake.set([
        {"match": ["server", "start"], **ok()},
        connect_result("hello", {"no_generation_field": True}),
    ])
    assert backend().available() is False


def test_available_false_when_server_start_fails(fake):
    fake.set([{"match": ["server", "start"], **ok(rc=1, err="boom")}])
    assert backend().available() is False


def test_available_false_when_binary_missing(fake, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")
    assert backend().available() is False


def test_server_running_true_only_on_clean_status(fake):
    fake.set([_status_rule()])
    assert backend().server_running() is True
    fake.set([{"match": ["server", "status"], **ok(rc=1)}])
    assert backend().server_running() is False


def test_server_identity_from_core_and_holder(fake):
    fake.set([_status_rule(core_id="core-X", instance="inst-Y")])
    assert backend().server_identity() == ("core-X", "inst-Y")


def test_server_identity_none_when_status_unreadable(fake):
    fake.set([{"match": ["server", "status"], **ok(rc=1)}])
    assert backend().server_identity() is None


# ---------------------------------------------------------------------------
# state() — reported:false must never become idle (design doc §7.3; mutation
# target #3)
# ---------------------------------------------------------------------------

def test_state_reported_false_is_unknown_even_when_status_present(fake):
    """RED proof target #3: collapsing `reported` into "always trust status"
    must turn this red — a program mid-launch (`reported: false`) must not be
    read as idle."""
    fake.set([{"match": ["get"], **ok(out=terminal_json(
        reported=False, status={"state": "idle"}))}])
    assert backend().state("Omar-worker") == "unknown"


def test_state_reported_true_idle(fake):
    fake.set([{"match": ["get"], **ok(out=terminal_json(
        reported=True, status={"state": "idle"}))}])
    assert backend().state("Omar-worker") == "idle"


def test_state_reported_true_each_known_value(fake):
    for value in ("working", "done", "blocked", "error"):
        fake.set([{"match": ["get"], **ok(out=terminal_json(
            reported=True, status={"state": value}))}])
        assert backend().state("Omar-worker") == value


def test_state_unrecognised_status_value_is_unknown(fake):
    fake.set([{"match": ["get"], **ok(out=terminal_json(
        reported=True, status={"state": "something-new"}))}])
    assert backend().state("Omar-worker") == "unknown"


def test_state_status_null_is_unknown(fake):
    fake.set([{"match": ["get"], **ok(out=terminal_json(
        reported=False, status=None))}])
    assert backend().state("Omar-worker") == "unknown"


def test_state_pane_not_found_is_unknown(fake):
    fake.set([{"match": ["get"], **err_json("not_found")}])
    assert backend().state("Omar-worker") == "unknown"


# ---------------------------------------------------------------------------
# pid() / _inspect_pane() — address is the handle, finished -> None pid
# ---------------------------------------------------------------------------

def test_pid_is_none_when_finished(fake):
    fake.set([{"match": ["get"], **ok(out=terminal_json(
        address="t9", state="finished", pid=1234))}])
    assert backend().pid("Omar-worker") is None


def test_pid_is_pid_when_live(fake):
    fake.set([{"match": ["get"], **ok(out=terminal_json(
        address="t9", state="live", pid=4242))}])
    assert backend().pid("Omar-worker") == 4242


def test_pid_none_when_get_fails(fake):
    fake.set([{"match": ["get"], **err_json("not_found")}])
    assert backend().pid("Omar-worker") is None


def test_inspect_pane_uses_recorded_handle_not_name(fake):
    """Once a record exists, `_target()` resolves by `address`, not by the
    (reusable) `name` — the call to `hc get` must carry the address."""
    lib_mux.write_pane_record("Omar-worker", "hc", "tOLD",
                              server=("core-A", "inst-A"))
    fake.set([{"match": ["get", "tOLD"], **ok(out=terminal_json(
        address="tOLD", state="live", pid=555))}])
    b = backend()
    assert b.pid("Omar-worker") == 555
    calls = fake.raw_calls()
    assert any(c[:2] == ["get", "tOLD"] for c in calls)
    assert not any(c[:2] == ["get", "omar-worker"] for c in calls)


def test_inspect_pane_falls_back_to_lowercased_name_without_a_record(fake):
    target = lib_mux._pane_name("Omar-worker").lower()
    fake.set([{"match": ["get", target], **ok(out=terminal_json(
        address="t1", state="live", pid=1))}])
    assert backend().pid("Omar-worker") == 1


# ---------------------------------------------------------------------------
# record_existence() — only not_found is GONE (design doc §3.1/§5; mutation
# target #1)
# ---------------------------------------------------------------------------

def _record(handle="t5", endpoint="core-A", generation="inst-A"):
    return {"handle": handle, "backend": "hc",
            "server": {"endpoint": endpoint, "generation": generation}}


def test_record_existence_not_found_is_gone(fake):
    """RED proof target #1a: a kill-authorisation record must only be ended
    on a definite `not_found` — nothing else may report PANE_GONE."""
    fake.set([
        _status_rule(core_id="core-A", instance="inst-A"),
        {"match": ["get", "t5"], **err_json("not_found")},
    ])
    assert backend().record_existence("Omar-worker", _record()) == lib_mux.PANE_GONE


@pytest.mark.parametrize("kind", [
    "finished", "name_taken", "usage", "unavailable", "needs_update",
])
def test_record_existence_other_kinds_are_unobserved(fake, kind):
    """RED proof target #1b: any error kind other than `not_found` — a
    *live* terminal just rejecting the request — must never be read as GONE.
    Mistaking `finished` or `name_taken` for GONE would drop the kill
    authorisation for a terminal that is still there."""
    fake.set([
        _status_rule(core_id="core-A", instance="inst-A"),
        {"match": ["get", "t5"], **err_json(kind)},
    ])
    assert backend().record_existence("Omar-worker", _record()) == lib_mux.PANE_UNOBSERVED


def test_record_existence_live_or_finished_terminal_is_exists(fake):
    fake.set([
        _status_rule(core_id="core-A", instance="inst-A"),
        {"match": ["get", "t5"], **ok(out=terminal_json(address="t5", state="finished"))},
    ])
    assert backend().record_existence("Omar-worker", _record()) == lib_mux.PANE_EXISTS


def test_record_existence_generation_changed_before_asking_is_unobserved(fake):
    fake.set([_status_rule(core_id="core-A", instance="inst-DIFFERENT")])
    assert backend().record_existence("Omar-worker", _record()) == lib_mux.PANE_UNOBSERVED


def test_record_existence_generation_changed_after_asking_is_unobserved(fake):
    fake.set([
        {"match": ["server", "status"],
         "seq": [ok(out=status_json(core_id="core-A", instance="inst-A")),
                 ok(out=status_json(core_id="core-A", instance="inst-B"))]},
        {"match": ["get", "t5"], **err_json("not_found")},
    ])
    assert backend().record_existence("Omar-worker", _record()) == lib_mux.PANE_UNOBSERVED


def test_record_existence_malformed_record_is_unobserved(fake):
    assert backend().record_existence("Omar-worker", {}) == lib_mux.PANE_UNOBSERVED
    assert backend().record_existence(
        "Omar-worker", {"handle": "t5", "server": {}}) == lib_mux.PANE_UNOBSERVED


# ---------------------------------------------------------------------------
# spawn() — PATH export (design doc §6; mutation target #4) and name_taken
# ---------------------------------------------------------------------------

def test_spawn_wraps_cmd_with_path_export_including_local_bin(fake):
    """RED proof target #4: dropping the PATH prefix must turn this red —
    without it, hc's own bare server PATH cannot find `claude` / `hc`."""
    fake.set([
        _status_rule(),
        {"match": ["run"], **ok(out=json.dumps({"address": "t1", "state": "live"}))},
    ])
    assert backend().spawn("Omar-worker", "echo hi", cwd="/tmp") is True
    [call] = [c for c in fake.raw_calls() if c[0] == "run"]
    assert "--" in call
    script = call[-1]
    assert script.startswith('export PATH="')
    assert "scripts/bin" in script
    assert "$HOME/.local/bin" in script
    assert script.endswith("echo hi")
    assert call[-3:-1] == ["bash", "-lc"]


def test_spawn_lowercases_the_hc_name_and_keeps_the_label(fake):
    fake.set([
        _status_rule(),
        {"match": ["run"], **ok(out=json.dumps({"address": "t1", "state": "live"}))},
    ])
    assert backend().spawn("Omar-worker", "true") is True
    [call] = [c for c in fake.raw_calls() if c[0] == "run"]
    assert "--name" in call
    assert call[call.index("--name") + 1] == lib_mux._pane_name("Omar-worker").lower()
    assert "--label" in call
    assert call[call.index("--label") + 1] == f"crewvia_name={lib_mux._pane_name('Omar-worker')}"


def test_spawn_writes_a_record_bound_to_the_server(fake):
    fake.set([
        _status_rule(core_id="core-A", instance="inst-A"),
        {"match": ["run"], **ok(out=json.dumps({"address": "t7", "state": "live"}))},
    ])
    assert backend().spawn("Omar-worker", "true") is True
    record = lib_mux.read_pane_record("Omar-worker")
    assert record["handle"] == "t7"
    assert record["backend"] == "hc"
    assert record["server"] == {"endpoint": "core-A", "generation": "inst-A"}


def test_spawn_name_taken_is_occupied_refusal(fake):
    fake.set([_status_rule(), {"match": ["run"], **err_json("name_taken")}])
    b = backend()
    assert b.spawn("Omar-worker", "true") is False
    assert b.last_spawn_refusal == lib_mux.SPAWN_OCCUPIED


def test_spawn_other_error_is_unreadable_refusal(fake):
    fake.set([_status_rule(), {"match": ["run"], **err_json("spawn_failed")}])
    b = backend()
    assert b.spawn("Omar-worker", "true") is False
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE


def test_spawn_does_not_retry_an_old_finished_terminal_with_the_same_name(fake):
    """Design doc §3.0: hc never refuses a spawn because of an old, finished
    terminal under the same name — there is no husk-reuse branch to test."""
    fake.set([
        _status_rule(),
        {"match": ["run"], **ok(out=json.dumps({"address": "t2", "state": "live"}))},
    ])
    assert backend().spawn("Omar-worker", "true") is True
    assert lib_mux.read_pane_record("Omar-worker")["handle"] == "t2"


# ---------------------------------------------------------------------------
# send / keys / capture
# ---------------------------------------------------------------------------

def test_send_passes_text_after_a_double_dash(fake):
    fake.set([{"match": ["send"], **ok(out=json.dumps(
        {"started": True, "status": "working", "turn": 1}))}])
    assert backend().send("Omar-worker", "-not-a-flag") is True
    [call] = [c for c in fake.raw_calls() if c[0] == "send"]
    assert call[call.index("--") + 1] == "-not-a-flag"


def test_send_failure_is_false(fake):
    fake.set([{"match": ["send"], **err_json("finished")}])
    assert backend().send("Omar-worker", "hi") is False


def test_keys_unknown_name_refuses_without_calling_hc(fake):
    b = backend()
    assert b.keys("Omar-worker", ["pgdn"]) is False
    assert fake.raw_calls() == []


def test_keys_translates_to_lowercase_herdr_style_names(fake):
    fake.set([{"match": ["keys"], **ok(out=json.dumps({"status": None, "turn": 0}))}])
    assert backend().keys("Omar-worker", ["Enter", "ESCAPE"]) is True
    [call] = [c for c in fake.raw_calls() if c[0] == "keys"]
    tail = call[call.index("--") + 1:]
    assert tail == ["enter", "esc"]


def test_capture_returns_text_field(fake):
    fake.set([{"match": ["read"], **ok(out=json.dumps(
        {"complete": True, "how": "history", "text": "hello\n"}))}])
    assert backend().capture("Omar-worker") == "hello\n"


def test_capture_requests_the_configured_line_window(fake):
    fake.set([{"match": ["read"], **ok(out=json.dumps(
        {"complete": True, "how": "history", "text": ""}))}])
    backend().capture("Omar-worker")
    [call] = [c for c in fake.raw_calls() if c[0] == "read"]
    assert call[call.index("--lines") + 1] == str(lib_mux._HC_CAPTURE_LINES)


def test_capture_on_error_is_empty_string(fake):
    fake.set([{"match": ["read"], **err_json("not_found")}])
    assert backend().capture("Omar-worker") == ""


# ---------------------------------------------------------------------------
# list() — terminal.list(all=false), labels.crewvia_name (design doc §3.3/§3.5)
# ---------------------------------------------------------------------------

def test_list_calls_terminal_list_with_all_false(fake):
    fake.set([{"connect": True, "method": "terminal.list",
              "result": {"hidden_finished": 0, "terminals": []}}])
    assert backend().list() == []


def test_list_reads_names_from_labels_not_hc_name(fake):
    label = lib_mux._pane_name("Omar-worker")
    fake.set([connect_result("terminal.list", {"hidden_finished": 0, "terminals": [
        {"name": label.lower(), "labels": {"crewvia_name": label}, "state": "live"},
        {"name": "shell", "labels": {}, "state": "live"},
    ]})])
    assert backend().list() == ["Omar-worker"]


def test_list_filters_by_suffix(fake):
    worker = lib_mux._pane_name("Omar-worker")
    director = lib_mux._pane_name("Sora-director")
    fake.set([connect_result("terminal.list", {"hidden_finished": 0, "terminals": [
        {"name": worker.lower(), "labels": {"crewvia_name": worker}, "state": "live"},
        {"name": director.lower(), "labels": {"crewvia_name": director}, "state": "live"},
    ]})])
    assert backend().list(suffix="-worker") == ["Omar-worker"]


def test_list_on_connect_failure_is_empty(fake):
    fake.set([connect_error("terminal.list", "internal")])
    assert backend().list() == []


# ---------------------------------------------------------------------------
# kill()
# ---------------------------------------------------------------------------

def test_kill_ordinary_worker_uses_target_and_drops_record(fake):
    lib_mux.write_pane_record("Omar-worker", "hc", "t5",
                              server=("core-A", "inst-A"))
    fake.set([{"match": ["kill", "t5"], **ok(out=terminal_json(
        address="t5", state="finished"))}])
    assert backend().kill("Omar-worker") is True
    assert lib_mux.read_pane_record("Omar-worker") is None


def test_kill_not_found_is_treated_as_success_and_drops_record(fake):
    lib_mux.write_pane_record("Omar-worker", "hc", "t5",
                              server=("core-A", "inst-A"))
    fake.set([{"match": ["kill", "t5"], **err_json("not_found")}])
    assert backend().kill("Omar-worker") is True
    assert lib_mux.read_pane_record("Omar-worker") is None


def test_kill_other_error_is_false_and_keeps_record(fake):
    lib_mux.write_pane_record("Omar-worker", "hc", "t5",
                              server=("core-A", "inst-A"))
    fake.set([{"match": ["kill", "t5"], **err_json("unavailable")}])
    assert backend().kill("Omar-worker") is False
    assert lib_mux.read_pane_record("Omar-worker") is not None


def test_kill_daemon_pane_without_a_matching_record_is_refused(fake, capsys):
    """dispatcher / watchdog panes go through `may_destroy_pane()` — a daemon
    name with no record for this checkout must be refused, never destroyed
    bare (CLAUDE.md 不変条件: `registry/mux/<name>.json` is the kill authority)."""
    target = lib_mux._pane_name("dispatcher").lower()
    fake.set([{"match": ["get", target], **ok(out=terminal_json(
        address="t9", state="live", pid=4242))}])
    assert backend().kill("dispatcher") is False
    assert "refused" in capsys.readouterr().err


def test_kill_daemon_pane_with_force_bypasses_the_guard(fake):
    target = lib_mux._pane_name("dispatcher").lower()
    fake.set([{"match": ["kill", target], **ok(out=terminal_json(
        address="t9", state="finished"))}])
    assert backend().kill("dispatcher", allow_foreign=True) is True


# ---------------------------------------------------------------------------
# attach / attach_cmd / verify_sent / key column / backend selection
# ---------------------------------------------------------------------------

def test_attach_is_always_false(fake):
    assert backend().attach("Omar-worker") is False


def test_attach_cmd_execs_hc_attach_with_the_target(fake):
    target = lib_mux._pane_name("Omar-worker").lower()
    assert backend().attach_cmd("Omar-worker") == ["hc", "attach", target]


def test_attach_cmd_prefers_the_recorded_address(fake):
    lib_mux.write_pane_record("Omar-worker", "hc", "t3",
                              server=("core-A", "inst-A"))
    assert backend().attach_cmd("Omar-worker") == ["hc", "attach", "t3"]


def test_verify_sent_falls_back_to_text_in_input_line(fake):
    fake.set([{"match": ["read"], **ok(out=json.dumps(
        {"complete": True, "how": "history", "text": "❯ still here"}))}])
    assert lib_mux.Mux(backend=backend()).verify_sent("Omar-worker", "still here") is False


def test_verify_sent_true_when_text_left_the_input_line(fake):
    fake.set([{"match": ["read"], **ok(out=json.dumps(
        {"complete": True, "how": "history", "text": "❯ \n> working on it"}))}])
    assert lib_mux.Mux(backend=backend()).verify_sent("Omar-worker", "still here") is True


def test_hc_key_names_and_the_other_backends_are_unchanged():
    assert lib_mux._KEY_COLUMNS["hc"] == lib_mux._KEY_COLUMNS["herdr"]
    assert lib_mux._KEY_COLUMNS["tmux"] == 0
    assert lib_mux._KEY_COLUMNS["rex"] == 2


def test_hc_is_selected_only_when_asked_for(monkeypatch):
    monkeypatch.setenv("CREWVIA_MUX", "hc")
    assert lib_mux._select_backend() is lib_mux.HcBackend
    monkeypatch.setenv("CREWVIA_MUX", "tmux")
    assert lib_mux._select_backend() is lib_mux.TmuxBackend
    monkeypatch.delenv("CREWVIA_MUX", raising=False)
    monkeypatch.setattr(lib_mux, "_config_mode", lambda config_path=None: "hc")
    assert lib_mux._select_backend() is lib_mux.HcBackend


def test_mux_verify_sent_dispatches_to_hc_backend(fake, monkeypatch):
    fake.set([{"match": ["read"], **ok(out=json.dumps(
        {"complete": True, "how": "history", "text": ""}))}])
    called = {}

    def fake_verify_sent(name, text):
        called["seen"] = (name, text)
        return True

    b = backend()
    monkeypatch.setattr(b, "verify_sent", fake_verify_sent)
    assert lib_mux.Mux(backend=b).verify_sent("Omar-worker", "hi") is True
    assert called["seen"] == ("Omar-worker", "hi")
