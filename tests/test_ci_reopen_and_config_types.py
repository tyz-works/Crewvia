"""CI 待ち P3: CLOSED → reopen の取り直し (A) と daemons: しきい値の型検査 (B)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lib_ci_status as ci  # noqa: E402
import lib_daemon_watch  # noqa: E402
from test_ci_status import FakeGh, NOW, HEAD, poll, rec, run_, task, _allow_stub_gh  # noqa: E402,F401


def view(state):
    return json.dumps({"headRefOid": HEAD, "statusCheckRollup": [run_("a")], "state": state})


# ---- (A) -------------------------------------------------------------------

def test_closed_pr_is_not_polled_before_the_recheck_interval(tmp_path):
    poll(tmp_path, [task()], FakeGh(view=view("CLOSED")))
    assert rec(tmp_path)["pr_state"] == "CLOSED"
    gh = FakeGh(view=view("OPEN"))
    poll(tmp_path, [task()], gh, now=NOW + ci.CLOSED_RECHECK_SECONDS - 1)
    assert gh.calls == []


def test_reopened_pr_is_polled_again_after_the_interval(tmp_path):
    poll(tmp_path, [task()], FakeGh(view=view("CLOSED")))
    gh = FakeGh(view=view("OPEN"))
    poll(tmp_path, [task()], gh, now=NOW + ci.CLOSED_RECHECK_SECONDS)
    assert len(gh.calls) == 1
    r = rec(tmp_path)
    assert r["pr_state"] == "OPEN" and r["state"] == "passed"


def test_still_closed_pr_stays_closed_and_waits_another_interval(tmp_path):
    poll(tmp_path, [task()], FakeGh(view=view("CLOSED")))
    t1 = NOW + ci.CLOSED_RECHECK_SECONDS
    gh = FakeGh(view=view("CLOSED"))
    poll(tmp_path, [task()], gh, now=t1)
    assert len(gh.calls) == 1
    gh2 = FakeGh(view=view("CLOSED"))
    poll(tmp_path, [task()], gh2, now=t1 + 60)
    assert gh2.calls == []


def test_merged_pr_is_never_polled_again(tmp_path):
    poll(tmp_path, [task()], FakeGh(view=view("MERGED")))
    gh = FakeGh(view=view("OPEN"))
    poll(tmp_path, [task()], gh, now=NOW + 10 * ci.CLOSED_RECHECK_SECONDS)
    assert gh.calls == []


def test_recheck_counts_against_the_per_cycle_budget(tmp_path):
    tasks = [task(id_=f"t{i:03d}", pr=100 + i) for i in range(6)]
    poll(tmp_path, tasks, FakeGh(view=view("CLOSED")), max_tasks=6)
    gh = FakeGh(view=view("CLOSED"))
    poll(tmp_path, tasks, gh, now=NOW + ci.CLOSED_RECHECK_SECONDS)
    assert len(gh.calls) == ci.MAX_TASKS_PER_CYCLE


# ---- (B) -------------------------------------------------------------------

KEYS_INT_ANY = ["stall_notify_seconds", "checkpoint_seconds", "flap_threshold", "dispatcher_stale_seconds"]
KEYS_POSITIVE = ["ci_poll_seconds", "hard_idle_suppressed_notify_seconds"]


def load(tmp_path, key, raw):
    lib_daemon_watch._WARNED_CONFIG.clear()
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"daemons:\n  {key}: {raw}\n")
    return getattr(lib_daemon_watch.load_config(cfg, env={}), key), getattr(lib_daemon_watch.WatchConfig(), key)


@pytest.mark.parametrize("key", KEYS_INT_ANY + KEYS_POSITIVE)
@pytest.mark.parametrize("raw", ["yes", "true", "false", '"900"', "900.5"])
def test_bool_string_float_are_rejected_with_one_warning(tmp_path, capsys, key, raw):
    got, default = load(tmp_path, key, raw)
    assert got == default
    assert capsys.readouterr().err.count(f"daemons.{key}") == 1


@pytest.mark.parametrize("key", KEYS_INT_ANY + KEYS_POSITIVE)
def test_plain_int_is_accepted(tmp_path, capsys, key):
    got, _ = load(tmp_path, key, "900")
    assert got == 900 and capsys.readouterr().err == ""


@pytest.mark.parametrize("key", KEYS_INT_ANY)
@pytest.mark.parametrize("raw", [0, -1])
def test_zero_and_negative_keep_their_meaning_where_allowed(tmp_path, key, raw):
    got, _ = load(tmp_path, key, str(raw))
    assert got == raw


@pytest.mark.parametrize("key", KEYS_POSITIVE)
@pytest.mark.parametrize("raw", [0, -1])
def test_zero_and_negative_fall_back_where_positive_only(tmp_path, key, raw):
    got, default = load(tmp_path, key, str(raw))
    assert got == default


def test_warning_is_emitted_once_across_repeated_loads(tmp_path, capsys):
    lib_daemon_watch._WARNED_CONFIG.clear()
    cfg = tmp_path / "c.yaml"
    cfg.write_text("daemons:\n  stall_notify_seconds: yes\n")
    for _ in range(3):
        lib_daemon_watch.load_config(cfg, env={})
    assert capsys.readouterr().err.count("stall_notify_seconds") == 1
