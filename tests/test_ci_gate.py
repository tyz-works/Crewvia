"""PR の CI の門 (`lib_ci_status.gate_verdict` と `plan.sh ci-gate`)。設計: knowledge/ci-wait-operations.md §2.2。

固定すること: 通る条件は全部 AND (記録の head == PR の現 head・全体 passed・必須 check `ci-ok` が passed・記録が新しい)。
それ以外 (running / failed / unknown / none / 古い / head 違い / 読めない / 取り直せない) は全部「通さない」(fail-closed)。
誰もまだ呼ばない (挙動は変わらない) ので、呼び出し元が増えたらこのテストが赤になる。
gh は stub (本物の GitHub に飛ばさない)。赤の実証は `tests/red_proof_ci_gate.py`。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
import sys

import pytest

from fixture_tree import REPO_ROOT
from test_git_policy_pull_and_pr_base_cutover import AGENT, MISSION, Repo  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lib_ci_status as ci  # noqa: E402

HEAD = "086c9f5e50a133f7cbdd789d5b9c36c4be6bd520"
OTHER = "1111111111111111111111111111111111111111"
NOW = 1_800_000_000.0


def chk(name, state="passed"):
    return {"name": name, "state": state, "conclusion": None, "started_at": None, "completed_at": None}


def rec(checks=None, *, state=None, head=HEAD, observed=NOW - 30, **extra):
    checks = [chk("pytest"), chk("ci-ok")] if checks is None else checks
    state = ci.overall_state(checks) if state is None else state
    return {"pr": 313, "head": head, "state": state, "checks": checks, "observed_at": observed,
            "summary": "s", **extra}


def verdict(record, pr_head=HEAD, now=NOW, **kw):
    return ci.gate_verdict(record, pr_head=pr_head, now=now, **kw)


# ---------------------------------------------------------------------------
# 純粋関数
# ---------------------------------------------------------------------------

def test_green_record_with_required_check_passes():
    assert verdict(rec()) == (True, "ok")


@pytest.mark.parametrize("record,reason", [
    (rec([chk("pytest"), chk("ci-ok", "running")]), "running"),
    (rec([chk("pytest", "failed"), chk("ci-ok")]), "failed"),
    (rec([chk("pytest"), chk("ci-ok", "unknown")]), "unknown:check"),
    (rec([]), "none"),
    (rec([chk("pytest")]), "no_required_check"),
    (rec([chk("pytest"), chk("ci-ok", "neutral")]), "no_required_check"),   # skipped は緑と数えない
    (rec([chk("ci-ok"), chk("ci-ok", "failed")], state="passed"), "no_required_check"),
    (rec([chk("ci-OK")]), "no_required_check"),
    (rec(head=OTHER), "head_mismatch"),
    (rec(head=None), "head_mismatch"),
    (rec(observed=NOW - 601), "stale"),
    (rec(observed=NOW + 5), "stale"),                                          # 未来の時刻
    ({"pr": 313, "head": None, "state": "unknown", "checks": [], "observed_at": NOW, "summary": "s",
      "reason": "gh_timeout"}, "unknown:gh_timeout"),
    ({"pr": None, "head": None, "state": "no_pr", "checks": [], "observed_at": NOW, "summary": "s"}, "unknown:no_pr"),
])
def test_everything_but_a_fresh_green_head_match_is_refused(record, reason):
    assert verdict(record) == (False, reason)


def test_boundary_of_freshness():
    assert verdict(rec(observed=NOW - 600))[0] is True
    assert verdict(rec(observed=NOW - 600.5)) == (False, "stale")


@pytest.mark.parametrize("record", [None, {}, [], "x", rec(state="bogus"), {**rec(), "checks": "x"},
                                    {k: v for k, v in rec().items() if k != "summary"},
                                    {**rec(), "observed_at": float("nan")}, {**rec(), "head": "abc"}])
def test_unreadable_record_is_refused(record):
    assert verdict(record) == (False, "unknown:unreadable")


@pytest.mark.parametrize("pr_head", [None, "", "abc", HEAD.upper(), 5])
def test_pr_head_that_could_not_be_fetched_is_refused(pr_head):
    assert verdict(rec(), pr_head=pr_head) == (False, "unknown:no_pr_head")


@pytest.mark.parametrize("now", [None, float("nan"), True, "x"])
def test_bad_clock_is_refused(now):
    assert verdict(rec(), now=now) == (False, "unknown:bad_clock")


def test_reason_from_a_record_is_sanitized():
    r = rec(state="unknown", checks=[], reason="bad\nreason\x1b[31m")
    ok, reason = verdict(r)
    assert not ok and "\n" not in reason and "\x1b" not in reason


def test_fetch_pr_head_failures_are_reasons_not_exceptions(monkeypatch, tmp_path):
    monkeypatch.setenv("CREWVIA_MUX_TEST_ISOLATION", "1")
    monkeypatch.setenv("CREWVIA_CI_ALLOW_GH_IN_TESTS", "1")

    class P:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def runner(out, rc=0):
        return lambda *a, **k: P(rc, out)

    f = lambda r: ci.fetch_pr_head(1, cwd=str(tmp_path), runner=r)  # noqa: E731
    assert f(runner(json.dumps({"headRefOid": HEAD}))) == (HEAD, None)
    assert f(runner("", 1)) == (None, "gh_exit_1")
    assert f(runner("not json")) == (None, "bad_json")
    assert f(runner("[]")) == (None, "bad_head")
    assert f(runner(json.dumps({"headRefOid": "zz"}))) == (None, "bad_head")


@pytest.mark.structural
def test_nobody_calls_the_gate_yet():
    """B は『誰も呼ばない』PR。呼び出し元を足すのは C (dispatcher の通知)。足すなら許可表を意図して広げる。"""
    allowed = {"scripts/lib_ci_status.py", "scripts/plan.sh"}
    hits = set()
    for p in list((REPO_ROOT / "scripts").rglob("*")) + list((REPO_ROOT / "hooks").rglob("*")):
        if p.is_file() and p.suffix in {".py", ".sh"} or p.name == "plan.sh":
            try:
                text = p.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            if re.search(r"gate_verdict|ci-gate", text):
                hits.add(str(p.relative_to(REPO_ROOT)))
    assert hits <= allowed, hits - allowed


# ---------------------------------------------------------------------------
# plan.sh ci-gate (gh は PATH の stub)
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path)
    shutil.copytree(REPO_ROOT / "config", r.root / "config")
    shutil.copytree(REPO_ROOT / "hooks", r.root / "hooks")
    return r


def put_gh(repo, head=HEAD, rc=0):
    d = repo.base / "bin"
    d.mkdir(exist_ok=True)
    g = d / "gh"
    g.write_text(f'#!/bin/sh\nif [ {rc} -ne 0 ]; then exit {rc}; fi\n'
                 f'echo \'{json.dumps({"headRefOid": head})}\'\n')
    g.chmod(g.stat().st_mode | stat.S_IXUSR)
    repo.env["PATH"] = f"{d}:{os.environ['PATH']}"


def done_with_pr(repo, pr=313):
    p = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--task", "t001", "--mission", MISSION)
    assert p.returncode == 0, p.stderr
    ex = json.loads(p.stdout)["execution_id"]
    d = repo.plan("done", "t001", "ok", "--mission", MISSION, "--pr", str(pr), "--execution", ex)
    assert d.returncode == 0, d.stderr


def write_rec(repo, record):
    assert ci.write_record(repo.root / "registry", MISSION, "t001", record)


def snapshot(root: pathlib.Path):
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts}


def gate(repo, *a):
    return repo.plan("ci-gate", *a, agent=None)


def test_cli_passes_with_the_current_head_in_the_line(repo):
    done_with_pr(repo)
    put_gh(repo)
    import time
    write_rec(repo, rec(observed=time.time() - 5))
    before = snapshot(repo.root / "queue"), snapshot(repo.root / "registry")
    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (
        0, f"ci-gate: PASS PR #313 head={HEAD}\nci-gate: approved_head no_approval\n"), p.stderr
    assert (snapshot(repo.root / "queue"), snapshot(repo.root / "registry")) == before    # 読み取り専用


def test_cli_passes_with_a_card_in_an_archived_mission(repo):
    """archive 済み mission の card でも記録と承認 head が引ける (t002 / 実例: 2026-11-13 PR #385)。

    mission 完了の条件 (`agents/director.md` §11) は『全 task が done』だけで PR の merge を含まないので、
    Director が archive してから merge するのが普通の順序。card が active mission 側に無くなっても
    門の記録・承認 head の両方を読めること。"""
    import time
    done_with_pr(repo)
    put_gh(repo)
    write_rec(repo, rec(observed=time.time() - 5))
    a = repo.plan("archive", MISSION)
    assert a.returncode == 0, a.stderr
    assert not (repo.queue / "missions" / MISSION).exists()
    assert (repo.queue / "archive" / MISSION / "tasks" / "t001.md").exists()
    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (
        0, f"ci-gate: PASS PR #313 head={HEAD}\nci-gate: approved_head no_approval\n"), p.stderr


def test_cli_prefers_the_newest_record_across_active_and_archive(repo):
    """同じ PR 番号の card が active mission と archive mission の両方にあっても、
    `observed_at` が最も新しい記録で判定する (active が優先でも archive が優先でもない)。"""
    import time
    done_with_pr(repo)                                           # active mission の t001 → PR #313
    put_gh(repo)
    write_rec(repo, rec(observed=time.time() - 500, state="failed",
                         checks=[chk("pytest"), chk("ci-ok", "failed")]))   # 古い記録: 赤

    # 2 つ目の mission を直接 archive に置く (同じ PR 番号。mission.yaml は gate に要らない)
    archived_tasks = repo.queue / "archive" / "mission2" / "tasks"
    archived_tasks.mkdir(parents=True)
    (archived_tasks / "t001.md").write_text(
        "---\nid: t001\ntitle: other mission, same PR\nstatus: done\npr_number: 313\n---\n\nbody\n")
    assert ci.write_record(repo.root / "registry", "mission2", "t001", rec(observed=time.time() - 5))  # 新しい記録: 緑

    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (
        0, f"ci-gate: PASS PR #313 head={HEAD}\nci-gate: approved_head no_approval\n"), p.stderr


def test_cli_is_not_fooled_by_an_unreadable_card_in_the_archived_mission(repo):
    """archive の別 mission に読めない card があっても、緑へは倒れない (正しい記録のまま判定する)。"""
    import time
    done_with_pr(repo)
    put_gh(repo)
    write_rec(repo, rec(observed=time.time() - 500, state="failed",
                         checks=[chk("pytest"), chk("ci-ok", "failed")]))

    broken_tasks = repo.queue / "archive" / "broken-mission" / "tasks"
    broken_tasks.mkdir(parents=True)
    (broken_tasks / "t001.md").write_text("not frontmatter at all")      # 壊れた card ([破損] に隔離される)

    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (1, "ci-gate: DENY PR #313 failed\n")


@pytest.mark.parametrize("gh_head,reason", [(OTHER, "head_mismatch")])
def test_cli_refuses_when_the_pr_moved_on(repo, gh_head, reason):
    import time
    done_with_pr(repo)
    put_gh(repo, head=gh_head)
    write_rec(repo, rec(observed=time.time() - 5))
    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (1, f"ci-gate: DENY PR #313 {reason}\n")


def test_cli_refuses_when_gh_cannot_answer(repo):
    import time
    done_with_pr(repo)
    put_gh(repo, rc=1)
    write_rec(repo, rec(observed=time.time() - 5))
    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (1, "ci-gate: DENY PR #313 unknown:no_pr_head\n") or \
        p.stdout == "ci-gate: DENY PR #313 unknown:gh_exit_1\n"
    assert p.returncode == 1


def test_cli_refuses_without_a_record_or_with_a_broken_one(repo):
    done_with_pr(repo)
    put_gh(repo)
    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (1, "ci-gate: DENY PR #313 unknown:no_record\n")
    d = repo.root / "registry" / "ci"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{MISSION}__t001.json").write_text("{broken")
    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (1, "ci-gate: DENY PR #313 unknown:no_record\n")


def test_cli_ignores_a_record_of_another_pr_and_another_repo(repo):
    import time
    done_with_pr(repo, pr=313)
    put_gh(repo)
    write_rec(repo, rec(observed=time.time() - 5))
    assert gate(repo, "314").stdout == "ci-gate: DENY PR #314 unknown:no_record\n"
    other = repo.base / "other"
    other.mkdir()
    assert gate(repo, "313", "--target-dir", str(other)).stdout == "ci-gate: DENY PR #313 unknown:no_record\n"


@pytest.mark.parametrize("args", [[], ["x"], ["0"], ["-3"], ["313", "314"], ["313", "--target-dir", " "],
                                  ["313", "--bogus"]])
def test_cli_usage_errors_are_exit_1_and_stdout_empty(repo, args):
    p = gate(repo, *args)
    assert p.returncode == 1 and not p.stdout.startswith("ci-gate: PASS")
