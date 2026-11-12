"""承認 head の比較と `merge-ready` (PR-D1)。設計: knowledge/ci-wait-operations.md §2.5・§2.2-6。赤の実証: tests/red_proof_ci_approved_head.py

固定すること: ①承認 head を Result から読む関数は 1 つで、末尾の 1 行の形を fullmatch する(本文の sha を推測しない)
②review 系 task が全部終端で、done 時刻が最大のものの承認 head == PR の現 head のときだけ `merge-ready`
③そうでなければ `ci-green` + 固定語 (`no_approval` / `approved_head_mismatch`) ④緑を知らせる前に現 head を取り直す
(取り直せない・動いていれば出さない) ⑤`plan.sh ci-gate` の 2 行目。`gate_verdict` 自体は承認を見ない。gh は stub。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import lib_ci_status as ci  # noqa: E402
from test_ci_follow_and_notify import (GREEN, HEAD2, _allow_stub_gh, cycle, d, view_state)  # noqa: E402,F401
from test_ci_gate import HEAD, OTHER, NOW, chk, done_with_pr, gate, put_gh, rec as gate_rec, repo, write_rec  # noqa: E402,F401
from test_ci_status import FakeMux, disp, task  # noqa: E402,F401


def appr(sha=HEAD, status="done", at="2026-11-13T10:00:00Z", text=None):
    return {"status": status, "completed_at": at, "result": text if text is not None else f"所見: 問題なし\napproved_head: {sha}"}


# ---------------------------------------------------------------------------
# approved_head: 末尾の 1 行だけを読む
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    f"approved_head: {HEAD}", f"x\napproved_head: {HEAD}", f"x\napproved_head: {HEAD}\n\n  \n", f"x\r\napproved_head: {HEAD}\r\n",
])
def test_approved_head_reads_the_last_line(text):
    assert ci.approved_head(text) == HEAD


@pytest.mark.parametrize("text", [
    None, 5, "", "\n\n", "approved_head: " + HEAD[:39], "approved_head: " + HEAD + "0", "approved_head: " + HEAD.upper(),
    f"approved_head: {HEAD} ok", f" approved_head: {HEAD}", f"approved_head:  {HEAD}", f"approved_head:{HEAD}",
    f"approved_head: {HEAD}\n要修正です", f"- approved_head: {HEAD}", f"see {HEAD}", f"`approved_head: {HEAD}`",
    "approved_head: " + "g" * 40,
])
def test_approved_head_never_guesses(text):
    assert ci.approved_head(text) is None


# ---------------------------------------------------------------------------
# approval_verdict
# ---------------------------------------------------------------------------

def test_matching_latest_review_is_approved():
    assert ci.approval_verdict([appr()], pr_head=HEAD) == (True, "ok")


@pytest.mark.parametrize("reviews,reason", [
    ([], "no_approval"),
    (None, "no_approval"),
    ([appr(status="in_progress")], "no_approval"),
    ([appr(), appr(status="blocked", at=None)], "no_approval"),                   # 1 本でも終端でなければ
    ([appr(status="failed")], "no_approval"),
    ([appr(text="所見だけ")], "no_approval"),
    ([appr(OTHER)], "approved_head_mismatch"),
    # done 時刻が最大のもの (古い承認は効かない)
    ([appr(HEAD, at="2026-11-13T10:00:00Z"), appr(OTHER, at="2026-11-13T11:00:00Z")], "approved_head_mismatch"),
    ([appr(HEAD, at="2026-11-13T10:00:00Z"), appr(text="要修正", at="2026-11-13T11:00:00Z")], "no_approval"),
    # 同時刻は全部が承認していること
    ([appr(HEAD), appr(text="要修正")], "no_approval"),
    ([appr(HEAD), appr(OTHER)], "approved_head_mismatch"),
    ([appr(HEAD, at=None), appr(OTHER, at="2026-11-13T11:00:00Z")], "approved_head_mismatch"),
])
def test_everything_else_is_not_approved(reviews, reason):
    assert ci.approval_verdict(reviews, pr_head=HEAD) == (False, reason)


def test_newer_approval_wins_over_an_older_rejection():
    reviews = [appr(text="要修正", at="2026-11-13T10:00:00Z"), appr(HEAD, at="2026-11-13T11:00:00Z")]
    assert ci.approval_verdict(reviews, pr_head=HEAD) == (True, "ok")


def test_a_bad_current_head_is_never_approved():
    assert ci.approval_verdict([appr()], pr_head=None) == (False, "unknown:no_pr_head")


def test_gate_verdict_still_ignores_approval():
    """gate_verdict は CI の緑だけを見る関数のまま (承認の語を受け取らない)。"""
    import inspect
    assert list(inspect.signature(ci.gate_verdict).parameters) == ["record", "pr_head", "now", "max_age"]


# ---------------------------------------------------------------------------
# pr_events
# ---------------------------------------------------------------------------

def review_task(id_="t002", status="done", at="2026-11-13T10:00:00Z", result=None, skills=("codex-review",), pr=313):
    t = task(id_=id_, status=status, pr=pr)
    t.update(skills=list(skills), completed_at=at, result=f"x\napproved_head: {HEAD}" if result is None else result)
    return t


def write_green(tmp_path, id_="t001", pr=313, head=HEAD):
    r = {"pr": pr, "head": head, "state": "passed", "checks": [chk("pytest"), chk("ci-ok")], "observed_at": NOW,
         "summary": "s", "pr_state": "OPEN"}
    assert ci.write_record(tmp_path, "m", id_, r)


def events(tmp_path, tasks, head_of=None):
    return ci.pr_events(tasks, registry_dir=tmp_path, now=NOW + 5, head_of=head_of)[0]


def test_green_with_a_matching_approval_is_merge_ready(tmp_path):
    write_green(tmp_path)
    evs = events(tmp_path, [task(status="done"), review_task()])
    assert [(e["kind"], e["head"], e["reason"]) for e in evs] == [("merge-ready", HEAD, None)]
    msg = ci.event_message(evs[0])
    assert msg.startswith("[merge-ready] PR #313") and HEAD[:12] in msg and "--match-head-commit" in msg


@pytest.mark.parametrize("tasks,reason", [
    ([task(status="done")], "no_approval"),
    ([task(status="done"), review_task(result="要修正")], "no_approval"),
    ([task(status="done"), review_task(status="in_progress")], "no_approval"),
    ([task(status="done"), review_task(result=f"approved_head: {OTHER}")], "approved_head_mismatch"),
    ([task(status="done"), review_task(skills=("code",))], "no_approval"),                 # review 系ではない task の承認は数えない
    ([task(status="done"), review_task(pr=999)], "no_approval"),                            # 別の PR の review
])
def test_green_without_a_matching_approval_stays_ci_green_with_the_reason(tmp_path, tasks, reason):
    write_green(tmp_path)
    evs = events(tmp_path, tasks)
    assert [(e["kind"], e["reason"]) for e in evs] == [("ci-green", reason)]
    assert reason in ci.event_message(evs[0]) and "承認ではありません" in ci.event_message(evs[0])


@pytest.mark.parametrize("skill", ["review", "codex-review", "qa"])
def test_every_review_kind_counts(tmp_path, skill):
    write_green(tmp_path)
    assert [e["kind"] for e in events(tmp_path, [task(status="done"), review_task(skills=(skill,))])] == ["merge-ready"]


def test_the_current_head_is_fetched_again_before_announcing(tmp_path):
    write_green(tmp_path)                                                              # 記録は HEAD
    tasks = [task(status="done"), review_task()]
    assert events(tmp_path, tasks, head_of=lambda pr: (HEAD2, None)) == [], "記録の head と現 head が違えば緑と言わない"
    assert events(tmp_path, tasks, head_of=lambda pr: (None, "gh_timeout")) == [], "取り直せなければ出さない"
    assert [e["kind"] for e in events(tmp_path, tasks, head_of=lambda pr: (HEAD, None))] == ["merge-ready"]


def test_approval_is_compared_with_the_refetched_head_not_the_record(tmp_path):
    write_green(tmp_path, head=HEAD2)
    tasks = [task(status="done"), review_task()]                                       # 承認は HEAD、記録も現 head も HEAD2
    evs = events(tmp_path, tasks, head_of=lambda pr: (HEAD2, None))
    assert [(e["kind"], e["reason"]) for e in evs] == [("ci-green", "approved_head_mismatch")]


def test_fetch_is_not_made_for_prs_that_are_not_green(tmp_path):
    ci.write_record(tmp_path, "m", "t001", {"pr": 313, "head": HEAD, "state": "failed", "checks": [chk("ci-ok", "failed")],
                                            "observed_at": NOW, "summary": "s", "pr_state": "OPEN"})
    calls = []
    events(tmp_path, [task(status="done")], head_of=lambda pr: calls.append(pr) or (HEAD, None))
    assert calls == []


def test_event_keys_differ_by_kind_and_head():
    keys = {ci.event_key({"kind": k, "pr": 313, "head": h}) for k in ("ci-green", "merge-ready") for h in (HEAD, HEAD2)}
    assert len(keys) == 4


# ---------------------------------------------------------------------------
# dispatcher の 1 サイクル
# ---------------------------------------------------------------------------

def add_review(d, result, *, status="done", skills="codex-review", at="2026-11-13T10:00:00Z"):
    card = d["_root"] / "queue" / "missions" / "m" / "tasks" / "t002.md"
    card.write_text(f"---\nid: t002\ntitle: review\nstatus: {status}\nworker: Ren\nskills: [{skills}]\npr_number: 313\n"
                    f"task_slug: review\ncompleted_at: '{at}'\n---\n\n## Description\nd\n\n## Result\n{result}\n")


def sent():
    return [m for m in FakeMux.sent if m.startswith(("[ci-", "[merge-ready]"))]


def kinds():
    return [m.split("]")[0] + "]" for m in sent()]


def test_dispatcher_announces_merge_ready_once_with_a_matching_approval(d):
    add_review(d, f"問題なし\napproved_head: {HEAD}")
    cycle(d, view_state(GREEN, "OPEN"))
    cycle(d, view_state(GREEN, "OPEN"))
    assert kinds() == ["[merge-ready]"]


def test_dispatcher_keeps_ci_green_without_an_approval_and_upgrades_when_it_arrives(d):
    add_review(d, "要修正", status="in_progress")
    cycle(d, view_state(GREEN, "OPEN"))
    assert kinds() == ["[ci-green]"] and "no_approval" in sent()[0]
    add_review(d, f"直しました\napproved_head: {HEAD}")
    cycle(d, view_state(GREEN, "OPEN"))
    assert kinds() == ["[ci-green]", "[merge-ready]"]


def test_dispatcher_does_not_announce_a_head_pushed_after_the_review(d):
    add_review(d, f"問題なし\napproved_head: {HEAD}")
    cycle(d, view_state(GREEN, "OPEN", head=HEAD2))
    assert kinds() == ["[ci-green]"] and "approved_head_mismatch" in sent()[0]


def test_dispatcher_refetches_the_head_and_stays_silent_when_the_pr_moved(d):
    add_review(d, f"問題なし\napproved_head: {HEAD}")
    (d["_tmp"] / "view.json").write_text(view_state(GREEN, "OPEN"))
    d["run_ci_poll"]()                                                                  # 記録は HEAD の緑
    (d["_tmp"] / "view.json").write_text(view_state(GREEN, "OPEN", head=HEAD2))         # その後 PR が進んだ
    d["run_ci_notify"]()
    assert sent() == [], "古い head の緑を知らせない (#377 レビュー P3-1)"


def test_dispatcher_stays_silent_when_gh_cannot_refetch(d):
    add_review(d, f"問題なし\napproved_head: {HEAD}")
    (d["_tmp"] / "view.json").write_text(view_state(GREEN, "OPEN"))
    d["run_ci_poll"]()
    (d["_tmp"] / "view.json").write_text("{garbage")                                    # 取り直しの gh が読めない答えを返す
    d["run_ci_notify"]()
    assert sent() == []


# ---------------------------------------------------------------------------
# plan.sh ci-gate の 2 行目
# ---------------------------------------------------------------------------

def add_review_card(repo, result, *, status="done", at="2026-11-13T10:00:00Z", skills="codex-review"):
    from test_git_policy_pull_and_pr_base_cutover import MISSION
    c = repo.card_path("t002")
    c.write_text(f"---\nid: t002\ntitle: review\nstatus: {status}\nskills: [{skills}]\npr_number: 313\n"
                 f"task_slug: review\ncompleted_at: '{at}'\n---\n\n## Description\nd\n\n## Result\n{result}\n")


def passing_gate(repo):
    done_with_pr(repo)
    put_gh(repo)
    write_rec(repo, gate_rec(observed=time.time() - 5))


@pytest.mark.parametrize("result,line", [
    (f"ok\napproved_head: {HEAD}", "ok"),
    ("要修正", "no_approval"),
    (f"ok\napproved_head: {OTHER}", "approved_head_mismatch"),
])
def test_cli_prints_the_approval_line_and_keeps_exit_zero(repo, result, line):
    passing_gate(repo)
    add_review_card(repo, result)
    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (0, f"ci-gate: PASS PR #313 head={HEAD}\nci-gate: approved_head {line}\n"), p.stderr


def test_cli_uses_the_newest_review_and_ignores_unfinished_ones_as_no_approval(repo):
    passing_gate(repo)
    add_review_card(repo, f"ok\napproved_head: {HEAD}", status="in_progress")
    assert gate(repo, "313").stdout.endswith("approved_head no_approval\n")


def test_cli_deny_has_no_approval_line(repo):
    done_with_pr(repo)
    put_gh(repo, head=OTHER)
    write_rec(repo, gate_rec(observed=time.time() - 5))
    add_review_card(repo, f"ok\napproved_head: {HEAD}")
    p = gate(repo, "313")
    assert (p.returncode, p.stdout) == (1, "ci-gate: DENY PR #313 head_mismatch\n")
