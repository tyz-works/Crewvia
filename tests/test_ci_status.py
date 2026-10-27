"""PR の CI の見える化 (lib_ci_status・dispatcher の取得・plan.sh status / tasks.json・通知の 1 行)。
gh は必ず stub (本物の GitHub に飛ばさない)。設計: knowledge/ci-status.md"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import lib_ci_status as ci  # noqa: E402
import lib_daemon_watch  # noqa: E402

HEAD = "086c9f5e50a133f7cbdd789d5b9c36c4be6bd520"
NOW = 1_800_000_000.0


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def run_(name, status="COMPLETED", conclusion="SUCCESS", started=NOW - 600, completed=None):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "startedAt": iso(started) if started else None, "completedAt": iso(completed) if completed else None}


def view_json(rollup, head=HEAD):
    return json.dumps({"headRefOid": head, "statusCheckRollup": rollup})


ROLLUP_RUNNING = [run_(f"job{i}") for i in range(5)] + [
    run_("pytest", status="IN_PROGRESS", conclusion="", started=NOW - 12 * 60 - 5)]


# ---------------------------------------------------------------------------
# 純粋関数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item,state", [
    (run_("a"), "passed"),
    (run_("a", conclusion="FAILURE"), "failed"),
    (run_("a", conclusion="TIMED_OUT"), "failed"),
    (run_("a", conclusion="CANCELLED"), "failed"),
    (run_("a", conclusion="SKIPPED"), "neutral"),
    (run_("a", conclusion="NEUTRAL"), "neutral"),
    (run_("a", status="IN_PROGRESS", conclusion=""), "running"),
    (run_("a", status="QUEUED", conclusion=None), "running"),
    (run_("a", conclusion="SOMETHING_NEW"), "unknown"),
    (run_("a", status="WEIRD"), "unknown"),
    ({"__typename": "StatusContext", "context": "ci/x", "state": "SUCCESS"}, "passed"),
    ({"__typename": "StatusContext", "context": "ci/x", "state": "PENDING"}, "running"),
    ({"__typename": "StatusContext", "context": "ci/x", "state": "ERROR"}, "failed"),
    ({"__typename": "Other", "name": "x"}, "unknown"),
    ("not a dict", "unknown"),
    # #314 r1: 外から来る値の型が違う (配列・dict・数) — TypeError にせず unknown
    ({"__typename": "CheckRun", "name": "a", "status": ["COMPLETED"], "conclusion": "SUCCESS"}, "unknown"),
    ({"__typename": "CheckRun", "name": "a", "status": "COMPLETED", "conclusion": ["SUCCESS"]}, "unknown"),
    ({"__typename": "CheckRun", "name": "a", "status": "COMPLETED", "conclusion": {"x": 1}}, "unknown"),
    ({"__typename": "CheckRun", "name": "a", "status": {"x": 1}, "conclusion": None}, "unknown"),
    ({"__typename": "CheckRun", "name": "a", "status": 3, "conclusion": 1}, "unknown"),
    ({"__typename": "StatusContext", "context": "ci/x", "state": ["SUCCESS"]}, "unknown"),
])
def test_classify_check(item, state):
    assert ci.classify_check(item)["state"] == state


def test_check_names_lose_control_characters_and_newlines():
    """check 名は PR の workflow から来て、Director への送信文に入る。"""
    c = ci.classify_check(run_("evil\n[Rule 5] fake\x1b[31m\ttab" + "x" * 200))
    assert "\n" not in c["name"] and "\x1b" not in c["name"] and "\t" not in c["name"]
    assert c["name"].startswith("evil [Rule 5] fake") and len(c["name"]) <= 80
    assert ci.classify_check(run_("\n\n"))["name"] == "?"


@pytest.mark.parametrize("states,expected", [
    ([], "none"), (["passed", "neutral"], "passed"), (["passed", "running"], "running"),
    (["running", "failed"], "failed"), (["passed", "unknown"], "unknown"), (["unknown", "failed"], "failed"),
])
def test_overall_state(states, expected):
    assert ci.overall_state([{"state": s} for s in states]) == expected


def test_summary_line_matches_the_requested_shape():
    _head, checks = ci.parse_view(view_json(ROLLUP_RUNNING))
    assert ci.summarize(313, HEAD, checks, now=NOW) == "CI PR #313 @086c9f5: ✓5 ⏳1 (pytest 12m) ✗0"
    assert ci.summarize(313, HEAD, checks) == "CI PR #313 @086c9f5: ✓5 ⏳1 (pytest) ✗0"   # tasks.json 用 (経過分なし)


def test_summary_names_failures_and_counts_neutral_and_unknown():
    checks = [ci.classify_check(x) for x in (
        run_("a"), run_("pytest", conclusion="FAILURE"), run_("lint", conclusion="SKIPPED"),
        run_("odd", conclusion="NEW"))]
    assert ci.summarize(1, HEAD, checks) == "CI PR #1 @086c9f5: ✓1 ⏳0 ✗1 (pytest) –1 ?1"
    assert ci.summarize(1, HEAD, []) == "CI PR #1 @086c9f5: CI 未登録 (check 0)"


@pytest.mark.parametrize("text", [
    "not json", "[]", '{"headRefOid": "abc", "statusCheckRollup": []}',
    json.dumps({"headRefOid": HEAD, "statusCheckRollup": {}}), json.dumps({"statusCheckRollup": []}),
])
def test_parse_view_rejects_malformed(text):
    with pytest.raises(ValueError):
        ci.parse_view(text)


def test_parse_view_null_rollup_is_empty():
    assert ci.parse_view(json.dumps({"headRefOid": HEAD, "statusCheckRollup": None})) == (HEAD, [])


def test_parse_pr_list():
    assert ci.parse_pr_list("[]") is None
    assert ci.parse_pr_list('[{"number": 7, "isCrossRepository": false}]') == 7
    # fork の同名 branch は数えない
    assert ci.parse_pr_list('[{"number": 9, "isCrossRepository": true}]') is None
    assert ci.parse_pr_list('[{"number": 9, "isCrossRepository": true}, {"number": 7, "isCrossRepository": false}]') == 7
    for bad in ('[{"number": 1, "isCrossRepository": false}, {"number": 2, "isCrossRepository": false}]',
                '[{"number": "1", "isCrossRepository": false}]', '[{"number": 1}]', '{"number": 1}', "x",
                '[{"number": true, "isCrossRepository": false}]'):
        with pytest.raises(ValueError):
            ci.parse_pr_list(bad)


def _record(state="running", pr=313, observed=NOW, rollup=ROLLUP_RUNNING, reason=None):
    if state == "unknown":
        rec = ci._unknown(pr, reason or "gh_timeout", observed)
    elif state == "no_pr":
        rec = {"pr": None, "head": None, "state": "no_pr", "checks": [], "observed_at": observed, "summary": "no PR"}
    else:
        head, checks = ci.parse_view(view_json(rollup))
        rec = {"pr": pr, "head": head, "state": ci.overall_state(checks), "checks": checks,
               "observed_at": observed, "summary": ci.summarize(pr, head, checks)}
    assert ci.record_problem(rec) is None
    return rec


def test_format_line_marks_stale_records_and_shows_unknown_as_unknown():
    assert ci.format_line(_record(), NOW + 60) == "CI PR #313 @086c9f5: ✓5 ⏳1 (pytest 13m) ✗0"
    assert ci.format_line(_record(), NOW + 301).endswith("(5 分前)")
    assert ci.format_line(_record("unknown"), NOW) == "CI PR #313 unknown (gh_timeout)"
    assert ci.format_line(_record("unknown", pr=None, reason="no_branch"), NOW) == "CI unknown (no_branch)"
    assert ci.format_line(_record("no_pr"), NOW) is None
    assert ci.format_line(None, NOW) is None


def test_task_graph_field():
    assert ci.task_graph_ci(_record()) == {"state": "running", "summary": "CI PR #313 @086c9f5: ✓5 ⏳1 (pytest) ✗0"}
    assert ci.task_graph_ci(_record("unknown")) == {"state": "unknown", "summary": "CI PR #313 unknown"}
    assert ci.task_graph_ci(_record(rollup=[]))["state"] == "unknown"        # CI 未登録は 4 値の unknown に畳む
    assert ci.task_graph_ci(_record("no_pr")) is None


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(state="green"), lambda r: r.update(observed_at="x"), lambda r: r.update(pr=0),
    lambda r: r.update(head="abc"), lambda r: r.update(checks={}), lambda r: r.update(summary=None),
    lambda r: r["checks"].append({"name": 1, "state": "passed"}), lambda r: r.update(observed_at=float("nan")),
])
def test_record_problem_rejects_malformed(mutate):
    r = _record()
    mutate(r)
    assert ci.record_problem(r) is not None


# ---------------------------------------------------------------------------
# I/O: poll_cycle (gh は runner の stub)
# ---------------------------------------------------------------------------

class FakeGh:
    """`subprocess.run` の代わり。argv で答えを決め、呼ばれた argv を残す。"""

    def __init__(self, view=None, prs="[]", rc=0, timeout=False, fail_for=()):
        self.calls = []
        self.view = view if view is not None else view_json(ROLLUP_RUNNING)
        self.prs, self.rc, self.timeout, self.fail_for = prs, rc, timeout, fail_for

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        assert kw["timeout"] <= ci.GH_TIMEOUT_SECONDS and kw["stdin"] is subprocess.DEVNULL
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        if argv[1] == "pr" and argv[2] == "list":
            out = self.prs
        else:
            out = self.view
        rc = 1 if any(x in argv for x in self.fail_for) else self.rc
        return subprocess.CompletedProcess(argv, rc, out, "")


@pytest.fixture(autouse=True)
def _allow_stub_gh(monkeypatch):
    monkeypatch.setenv("CREWVIA_CI_ALLOW_GH_IN_TESTS", "1")


def task(id_="t001", pr=313, status="in_progress", mission="m", slug="do-it", target_dir=None):
    return {"mission": mission, "id": id_, "status": status, "pr_number": pr, "task_slug": slug,
            "target_dir": target_dir}


def poll(tmp_path, tasks, gh, now=NOW, **kw):
    kw.setdefault("branch_of", lambda t: f"task/{t['mission']}/{t['id']}-{t['task_slug']}")
    return ci.poll_cycle(tasks, registry_dir=tmp_path, repo_root=str(tmp_path), now=now, runner=gh, **kw)


def rec(tmp_path, id_="t001", mission="m"):
    return ci.read_record(tmp_path, mission, id_)


def test_pr_number_on_the_card_is_viewed_and_recorded(tmp_path):
    gh = FakeGh()
    assert poll(tmp_path, [task()], gh) == 1
    assert gh.calls == [["gh", "pr", "view", "313", "--json", "headRefOid,statusCheckRollup"]]
    r = rec(tmp_path)
    assert r["state"] == "running" and r["pr"] == 313 and r["head"] == HEAD and r["observed_at"] == NOW
    assert (tmp_path / "ci" / "m__t001.json").exists()


@pytest.mark.parametrize("rollup,state", [
    ([run_("a"), run_("b")], "passed"), ([run_("a"), run_("b", conclusion="FAILURE")], "failed"),
    (ROLLUP_RUNNING, "running"), ([], "none"),
])
def test_states_are_recorded(tmp_path, rollup, state):
    poll(tmp_path, [task()], FakeGh(view=view_json(rollup)))
    assert rec(tmp_path)["state"] == state


def test_without_pr_number_the_branch_from_the_resolver_finds_the_pr(tmp_path):
    gh = FakeGh(prs='[{"number": 42, "isCrossRepository": false}]')
    poll(tmp_path, [task(pr=None)], gh)
    assert gh.calls[0] == ["gh", "pr", "list", "--head", "task/m/t001-do-it", "--state", "open",
                           "--json", "number,isCrossRepository"]
    assert gh.calls[1][:4] == ["gh", "pr", "view", "42"]
    assert rec(tmp_path)["pr"] == 42


def test_branch_without_a_pr_is_no_pr_and_not_shown(tmp_path):
    gh = FakeGh(prs="[]")
    poll(tmp_path, [task(pr=None)], gh)
    assert len(gh.calls) == 1 and rec(tmp_path)["state"] == "no_pr"
    assert ci.format_line(rec(tmp_path), NOW) is None


def test_undecidable_branch_is_unknown_without_calling_gh(tmp_path):
    gh = FakeGh()
    poll(tmp_path, [task(pr=None)], gh, branch_of=lambda t: None)
    assert gh.calls == [] and rec(tmp_path)["state"] == "unknown" and rec(tmp_path)["reason"] == "no_branch"


@pytest.mark.parametrize("gh,reason", [
    (FakeGh(rc=1), "gh_exit_1"), (FakeGh(timeout=True), "gh_timeout"), (FakeGh(view="{broken"), "bad_json"),
    (FakeGh(view=json.dumps({"headRefOid": HEAD})), "bad_json"),
    (FakeGh(view=json.dumps({"headRefOid": "nope", "statusCheckRollup": []})), "bad_json"),
])
def test_gh_failures_are_unknown_never_green_or_no_ci(tmp_path, gh, reason):
    poll(tmp_path, [task()], gh)
    r = rec(tmp_path)
    assert r["state"] == "unknown" and r["reason"] == reason and r["pr"] == 313
    assert ci.task_graph_ci(r)["state"] == "unknown"


def test_pr_list_failures_are_unknown(tmp_path):
    poll(tmp_path, [task(pr=None)], FakeGh(prs="[1"))
    assert rec(tmp_path)["reason"] == "bad_pr_list"
    poll(tmp_path, [task(id_="t002", pr=None)], FakeGh(fail_for=("list",)))
    assert rec(tmp_path, "t002")["reason"] == "gh_exit_1"


def test_gh_missing_is_unknown(tmp_path):
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    poll(tmp_path, [task()], missing)
    assert rec(tmp_path)["reason"] == "gh_unavailable"


def test_interval_suppresses_repeat_queries(tmp_path):
    gh = FakeGh()
    poll(tmp_path, [task()], gh, poll_seconds=60)
    poll(tmp_path, [task()], gh, now=NOW + 59, poll_seconds=60)
    assert len(gh.calls) == 1
    poll(tmp_path, [task()], gh, now=NOW + 60, poll_seconds=60)
    assert len(gh.calls) == 2


def test_only_watched_statuses_are_polled(tmp_path):
    gh = FakeGh()
    poll(tmp_path, [task(status=s, id_=f"t00{i}", pr=10 + i) for i, s in enumerate(
        ("pending", "done", "needs_director", "in_progress", "ready_for_verification", "verifying"))], gh)
    assert len(gh.calls) == 3


def test_per_cycle_task_limit_and_oldest_first(tmp_path):
    gh = FakeGh()
    tasks = [task(id_=f"t{i:03d}", pr=100 + i) for i in range(1, 7)]
    poll(tmp_path, tasks, gh, max_tasks=4)
    assert len(gh.calls) == 4
    gh.calls.clear()
    poll(tmp_path, tasks, gh, now=NOW + 61, max_tasks=4)       # 記録の無い 2 件 (-inf) が先
    assert [c[3] for c in gh.calls[:2]] == ["105", "106"]


class Clock:
    """gh を 1 回呼ぶたびに `cost[i]` 秒進む時計 (呼び出しの回数に依らない)。"""

    def __init__(self, costs):
        self.t, self.costs = 0.0, list(costs)

    def __call__(self):
        return self.t

    def gh(self, view=None, prs="[]"):
        seen = []

        def run(argv, **kw):
            seen.append((argv[2], kw["timeout"]))
            self.t += self.costs.pop(0) if self.costs else 0
            return subprocess.CompletedProcess(argv, 0, prs if argv[2] == "list" else (view or view_json([])), "")
        run.seen = seen
        return run


def test_time_budget_stops_starting_new_queries(tmp_path):
    clock = Clock([5, 14.5, 10, 10])
    gh = clock.gh()
    poll(tmp_path, [task(id_=f"t00{i}", pr=i) for i in range(1, 5)], gh, clock=clock, budget=20)
    assert len(gh.seen) == 2           # 残り 0.5 秒 (< 1) で次を始めない


def test_timeout_is_capped_by_the_remaining_budget(tmp_path):
    clock = Clock([12, 0])
    gh = clock.gh()
    poll(tmp_path, [task(id_="t001", pr=1), task(id_="t002", pr=2)], gh, clock=clock, budget=20)
    assert [t for _, t in gh.seen] == [15, 8]


def test_list_and_view_share_one_deadline(tmp_path):
    """#314 r1: pr_number の無い task は list と view の 2 回。2 回目の timeout は 1 回目の後の残りで切る
    (同じ timeout を 2 回渡すと 1 task で予算の倍まで延びる)。"""
    clock = Clock([14, 0])
    gh = clock.gh(prs='[{"number": 7, "isCrossRepository": false}]')
    poll(tmp_path, [task(pr=None)], gh, clock=clock, budget=20)
    assert gh.seen == [("list", 15), ("view", 6)]
    assert rec(tmp_path)["pr"] == 7


def test_view_is_not_started_when_list_used_up_the_budget(tmp_path):
    clock = Clock([19.5])
    gh = clock.gh(prs='[{"number": 7, "isCrossRepository": false}]')
    poll(tmp_path, [task(pr=None)], gh, clock=clock, budget=20)
    assert [k for k, _ in gh.seen] == ["list"]
    r = rec(tmp_path)
    assert r["state"] == "unknown" and r["reason"] == "cycle_budget" and r["pr"] == 7


def test_same_pr_is_queried_once_per_cycle(tmp_path):
    gh = FakeGh()
    poll(tmp_path, [task(id_="t001"), task(id_="t002", status="verifying")], gh)
    assert len(gh.calls) == 1 and rec(tmp_path, "t002")["pr"] == 313


def test_changed_counts_only_display_changes(tmp_path):
    gh = FakeGh()
    assert poll(tmp_path, [task()], gh) == 1
    assert poll(tmp_path, [task()], gh, now=NOW + 120) == 0          # 経過分だけ変わっても再生成しない
    gh.view = view_json([run_("a")])
    assert poll(tmp_path, [task()], gh, now=NOW + 240) == 1


def test_target_dir_tasks_query_from_the_target_repo(tmp_path):
    target = tmp_path / "other"
    target.mkdir()
    seen = []

    def gh(argv, **kw):
        seen.append(kw["cwd"])
        return subprocess.CompletedProcess(argv, 0, view_json([]), "")
    poll(tmp_path, [task(target_dir=str(target))], gh)
    assert seen == [str(target)]
    poll(tmp_path, [task(id_="t002", target_dir=str(tmp_path / "gone"))], gh)
    assert rec(tmp_path, "t002")["reason"] == "target_dir_missing"


def test_broken_record_is_retaken_and_not_shown(tmp_path):
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "m__t001.json").write_text("{not json")
    assert rec(tmp_path) is None
    gh = FakeGh()
    poll(tmp_path, [task()], gh)
    assert len(gh.calls) == 1 and rec(tmp_path)["state"] == "running"


def test_malformed_check_types_in_gh_output_are_unknown_not_a_crash(tmp_path):
    bad = [{"__typename": "CheckRun", "name": "x", "status": "COMPLETED", "conclusion": ["SUCCESS"]}]
    poll(tmp_path, [task()], FakeGh(view=view_json(bad)))
    r = rec(tmp_path)
    assert r["state"] == "unknown" and r["checks"][0]["conclusion"] is None


def test_an_unexpected_exception_is_confined_to_its_task(tmp_path):
    """#314 r1: 1 つの task の想定外の例外で他の task の観測を止めない。"""
    def branch_of(t):
        if t["id"] == "t001":
            raise RuntimeError("boom")
        return "task/m/t002-do-it"
    warned = []
    gh = FakeGh(prs='[{"number": 9, "isCrossRepository": false}]')
    poll(tmp_path, [task(id_="t001", pr=None), task(id_="t002", pr=None)], gh, branch_of=branch_of,
         warn=warned.append)
    assert rec(tmp_path, "t001")["reason"] == "internal_error"
    assert rec(tmp_path, "t002")["state"] == "running"
    assert any("t001 failed: RuntimeError" in w for w in warned)


@pytest.mark.parametrize("mission,task_id", [
    ("../x", "t001"), ("a/b", "t001"), ("", "t001"), (".", "t001"), ("m\n", "t001"),
    ("m", "../t001"), ("m", "x001"), ("m", ""), ("m", None), (None, "t001"),
])
def test_record_names_are_checked_before_they_become_paths(tmp_path, mission, task_id):
    """#314 r1: mission / task id は card 由来。registry/ci の外を指すパスを作らない。"""
    with pytest.raises(ValueError):
        ci.record_path(tmp_path, mission, task_id)
    assert ci.read_record(tmp_path, mission, task_id) is None
    good = {"pr": None, "head": None, "state": "no_pr", "checks": [], "observed_at": NOW, "summary": "no PR"}
    assert ci.write_record(tmp_path, mission, task_id, good) is False
    gh, warned = FakeGh(), []
    poll(tmp_path, [task(id_=task_id, mission=mission), task(id_="t002")], gh, warn=warned.append)
    assert len(gh.calls) == 1 and rec(tmp_path, "t002")["state"] == "running"   # 不正な方は飛ばし、他は進む
    assert any("bad name" in w for w in warned)
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["m__t002.json"]


def test_gh_is_not_called_in_tests_without_the_stub_marker(tmp_path, monkeypatch):
    """テスト中 (`CREWVIA_MUX_TEST_ISOLATION`) は印が無ければ本物の gh を呼ばない。"""
    monkeypatch.delenv("CREWVIA_CI_ALLOW_GH_IN_TESTS")
    assert os.environ.get("CREWVIA_MUX_TEST_ISOLATION")
    gh = FakeGh()
    poll(tmp_path, [task()], gh)
    assert gh.calls == [] and rec(tmp_path)["reason"] == "gh_blocked_in_tests"


def test_ci_poll_seconds_config(tmp_path):
    cfg = tmp_path / "c.yaml"
    assert lib_daemon_watch.load_config(cfg, env={}).ci_poll_seconds == 60
    cfg.write_text("daemons:\n  ci_poll_seconds: 120\n")
    assert lib_daemon_watch.load_config(cfg, env={}).ci_poll_seconds == 120
    for bad in ("0", "-5", "abc"):
        cfg.write_text(f"daemons:\n  ci_poll_seconds: {bad}\n")
        assert lib_daemon_watch.load_config(cfg, env={}).ci_poll_seconds == 60
    # env では変えない (停止スイッチ・共有規則ではない)
    assert lib_daemon_watch.load_config(cfg, env={"CREWVIA_DAEMON_CI_POLL_SECONDS": "5"}).ci_poll_seconds == 60


# ---------------------------------------------------------------------------
# dispatcher の 1 サイクル (埋め込み python を exec。gh は PATH の stub)
# ---------------------------------------------------------------------------

DISPATCHER_SH = REPO / "scripts" / "dispatcher.sh"


class FakeMux:
    sent = []

    def __init__(self, *a, **kw):
        pass

    def list(self, *a, suffix=None, **kw):
        return ["Sora-director"] if suffix == "-director" else []

    def send(self, name, text):
        FakeMux.sent.append(text)
        return True

    def state(self, *a, **kw):
        return "unknown"

    def capture(self, *a, **kw):
        return "line1\nline2\nline3\nline4\nline5"

    def kill(self, *a, **kw):
        return False


GH_STUB = """#!/bin/bash
echo "$@" >> "$GH_CALLS"
case "$GH_MODE" in
  ok) cat "$GH_VIEW" ;;
  sleep) exec sleep 30 ;;
esac
"""


@pytest.fixture
def disp(tmp_path, monkeypatch):
    import lib_mux
    root = tmp_path / "repo"
    (root / "registry").mkdir(parents=True)
    queue = root / "queue"
    tdir = queue / "missions" / "m" / "tasks"
    tdir.mkdir(parents=True)
    (queue / "archive").mkdir()
    (queue / "missions" / "m" / "mission.yaml").write_text("slug: m\ntitle: M\nstatus: in_progress\n")
    (queue / "state.yaml").write_text("active_missions:\n  - m\ndefault_mission: m\n")
    (tdir / "t001.md").write_text(
        "---\nid: t001\ntitle: do it\nstatus: in_progress\nworker: Ren\nskills: [code]\npr_number: 313\n"
        "task_slug: do-it\n---\n\nbody\n")
    (root / "scripts").mkdir()
    plan = root / "scripts" / "plan.sh"
    plan.write_text(f'#!/bin/bash\necho "$@" >> {tmp_path}/plan_calls.txt\n'
                    f'echo "$CREWVIA_QUEUE|$CREWVIA_REPO_ROOT|$PWD" >> {tmp_path}/plan_env.txt\n')
    plan.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(GH_STUB)
    (bindir / "gh").chmod(0o755)
    (tmp_path / "view.json").write_text(view_json(ROLLUP_RUNNING))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_CALLS", str(tmp_path / "gh_calls.txt"))
    monkeypatch.setenv("GH_VIEW", str(tmp_path / "view.json"))
    monkeypatch.setenv("GH_MODE", "ok")
    monkeypatch.setattr(lib_mux, "Mux", FakeMux)
    monkeypatch.setattr(lib_mux, "repo_identity_ok", lambda *a, **kw: True)
    monkeypatch.setenv("CREWVIA_TASKVIA", "disabled")
    monkeypatch.setenv("TASKVIA_URL", "")
    FakeMux.sent = []

    src = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", DISPATCHER_SH.read_text(), re.DOTALL).group(1)
    src, n = re.subn(r"\n# --- CYCLE ENTRY POINT ---\n.*$", "", src, flags=re.S)
    assert n == 1
    monkeypatch.setattr(sys, "argv", ["dispatcher", str(queue), str(root / "registry"),
                                      str(tmp_path / "nc.json"), "300", "60", str(tmp_path / "d.log")])
    ns = {"__name__": "dispatcher_under_test"}
    exec(compile(src, "dispatcher.sh (embedded, test)", "exec"), ns)
    ns["_tmp"] = tmp_path
    ns["_root"] = root
    return ns


def test_dispatcher_cycle_records_ci_and_refreshes_the_task_graph(disp):
    disp["run_ci_poll"]()
    calls = (disp["_tmp"] / "gh_calls.txt").read_text().splitlines()
    assert calls == ["pr view 313 --json headRefOid,statusCheckRollup"]
    r = ci.read_record(disp["_root"] / "registry", "m", "t001")
    assert r["state"] == "running"
    assert (disp["_tmp"] / "plan_calls.txt").read_text().strip() == "task-graph"
    disp["run_ci_poll"]()                                   # 間隔の内: gh も task-graph も呼ばない
    assert len((disp["_tmp"] / "gh_calls.txt").read_text().splitlines()) == 1
    assert (disp["_tmp"] / "plan_calls.txt").read_text().count("task-graph") == 1


def test_dispatcher_task_graph_refresh_names_its_own_queue_not_the_ambient_one(disp, monkeypatch):
    """#314 r1 [agy]: ambient の CREWVIA_QUEUE / CREWVIA_REPO_ROOT が本番を指していても、plan.sh task-graph は
    dispatcher 自身の queue / repo を env で受け取る (隔離 dispatcher が本番の tasks.json を書かない)。"""
    monkeypatch.setenv("CREWVIA_QUEUE", "/nonexistent/production/queue")
    monkeypatch.setenv("CREWVIA_REPO_ROOT", "/nonexistent/production")
    disp["run_ci_poll"]()
    root = disp["_root"]
    assert (disp["_tmp"] / "plan_env.txt").read_text().splitlines() == [f"{root / 'queue'}|{root}|{root}"]


def test_dispatcher_cycle_moves_on_when_gh_hangs(disp, monkeypatch):
    monkeypatch.setenv("GH_MODE", "sleep")
    monkeypatch.setattr(ci, "GH_TIMEOUT_SECONDS", 1)
    t0 = time.monotonic()
    disp["run_ci_poll"]()
    assert time.monotonic() - t0 < 10
    r = ci.read_record(disp["_root"] / "registry", "m", "t001")
    assert r["state"] == "unknown" and r["reason"] == "gh_timeout"


def test_dispatcher_cycle_survives_an_exception(disp, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("x")
    monkeypatch.setattr(ci, "poll_cycle", boom)
    disp["run_ci_poll"]()                                   # 例外を外に出さない
    assert "ci poll failed: RuntimeError" in (disp["_tmp"] / "d.log").read_text()


def test_dispatcher_branch_comes_from_the_resolver(disp):
    import lib_git_policy
    policy = lib_git_policy.default_policy()
    expected = lib_git_policy.task_branch(policy, mission_slug="m", task_id="t001", task_slug="do-it")
    assert disp["_ci_branch_of"]({"mission": "m", "id": "t001", "task_slug": "do-it"}) == expected
    assert disp["_ci_branch_of"]({"mission": "m", "id": "t001", "task_slug": None}) is None


def test_rule5_line_helper(disp):
    disp["run_ci_poll"]()
    line = disp["ci_line_for"]("m", "t001")
    assert line.startswith(" CI PR #313 @086c9f5: ✓5 ⏳1 (pytest ")
    assert disp["ci_line_for"]("m", "t999") == ""


# ---------------------------------------------------------------------------
# 表示: plan.sh status / tasks.json / watchdog
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_tree(tmp_path, monkeypatch):
    from fixture_tree import copy_plan_tree
    root = tmp_path / "tree"
    copy_plan_tree(root)
    queue = root / "queue"
    tdir = queue / "missions" / "m" / "tasks"
    tdir.mkdir(parents=True)
    (queue / "archive").mkdir()
    (queue / "missions" / "m" / "mission.yaml").write_text("slug: m\ntitle: M\nstatus: in_progress\n")
    (queue / "state.yaml").write_text("active_missions:\n  - m\ndefault_mission: m\n")
    for tid, st in (("t001", "in_progress"), ("t002", "pending"), ("t003", "verifying")):
        (tdir / f"{tid}.md").write_text(
            f"---\nid: {tid}\ntitle: task {tid}\nstatus: {st}\nworker: Ren\nskills: [code]\n---\n\nbody\n")
    reg = root / "registry"
    now = time.time()
    ci.write_record(reg, "m", "t001", _record(observed=now))
    ci.write_record(reg, "m", "t002", _record(observed=now))               # pending には出さない
    ci.write_record(reg, "m", "t003", _record("unknown", observed=now - 600))
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWVIA_") or k == "CREWVIA_MUX_TEST_ISOLATION"}
    env.update(CREWVIA_QUEUE=str(queue), CREWVIA_REPO_ROOT=str(root), CREWVIA_TASKVIA="disabled",
               CREWVIA_TASK_GRAPH_FILE=str(tmp_path / "tasks.json"))
    env.pop("AGENT_NAME", None)
    return root, env, tmp_path


def _plan(root, env, *args):
    return subprocess.run(["bash", str(root / "scripts" / "plan.sh"), *args], env=env, capture_output=True,
                          text=True, timeout=60)


def test_plan_status_shows_the_ci_line(plan_tree):
    root, env, _ = plan_tree
    p = _plan(root, env, "status")
    assert p.returncode == 0, p.stderr
    assert re.search(r"🔄 t001 task t001 \(Ren\)\n\s+CI PR #313 @086c9f5: ✓5 ⏳1 \(pytest \d+m\) ✗0\n", p.stdout), p.stdout
    d = _plan(root, env, "status", "--mission", "m")
    assert d.returncode == 0, d.stderr
    lines = d.stdout.splitlines()
    i = next(n for n, ln in enumerate(lines) if " t001 " in ln)
    assert lines[i + 1].strip().startswith("CI PR #313 @086c9f5: ✓5 ⏳1 (pytest")
    j = next(n for n, ln in enumerate(lines) if " t003 " in ln)
    assert lines[j + 1].strip() == "CI PR #313 unknown (gh_timeout) (10 分前)"
    k = next(n for n, ln in enumerate(lines) if " t002 " in ln)
    assert "CI" not in lines[k + 1]


def test_tasks_json_gets_a_ci_field_without_changing_others(plan_tree):
    root, env, tmp = plan_tree
    env_off = dict(env)
    for f in (root / "registry" / "ci").iterdir():
        f.rename(tmp / f.name)
    p = _plan(root, env_off, "task-graph")
    assert p.returncode == 0, p.stderr
    before = json.loads((tmp / "tasks.json").read_text())
    for f in tmp.glob("m__*.json"):
        f.rename(root / "registry" / "ci" / f.name)
    p = _plan(root, env, "task-graph")
    assert p.returncode == 0, p.stderr
    after = json.loads((tmp / "tasks.json").read_text())
    by_id = {t["id"]: t for t in after["tasks"]}
    assert by_id["m:t001"]["ci"] == {"state": "running", "summary": "CI PR #313 @086c9f5: ✓5 ⏳1 (pytest) ✗0"}
    assert by_id["m:t003"]["ci"] == {"state": "unknown", "summary": "CI PR #313 unknown"}
    assert "ci" not in by_id["m:t002"]
    for t in after["tasks"]:
        t.pop("ci", None)
    assert after == before                                     # 既存の欄・形は変わらない


def test_watchdog_suppression_message_carries_the_ci_line(tmp_path):
    import watchdog
    ci.write_record(tmp_path / "registry", "m", "t001", _record(observed=time.time()))
    mon = watchdog.WorkerMonitor("t001", {"worker": "Ren"}, watchdog.PROFILES,
                                 tmp_path, mission_slug="m")
    assert mon._ci_note().startswith(" CI 待ちの状況: CI PR #313 @086c9f5: ✓5 ⏳1 (pytest ")
    mon2 = watchdog.WorkerMonitor("t009", {"worker": "Ren"}, watchdog.PROFILES,
                                  tmp_path, mission_slug="m")
    assert mon2._ci_note() == ""
