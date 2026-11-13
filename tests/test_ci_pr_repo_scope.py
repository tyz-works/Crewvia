"""card の PR の持ち主の repo (`pr_repo`) を (repo, PR) の鍵で揃える (t001 / 20261113-ci-followups)。
取得・記録の束ね・closed・reviews・通知の台帳キー・ci-gate が同じ `lib_ci_status.pr_scope` を使う。
後方互換: 欄が無い = crewvia の repo、target_dir があれば従来どおり。gh は必ず stub。赤の実証: tests/red_proof_ci_pr_repo_scope.py"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import lib_ci_status as ci  # noqa: E402
from test_ci_status import FakeGh, HEAD, NOW, poll, rec, task  # noqa: E402,F401
from test_ci_follow_and_notify import GREEN, RED, _write, events, view_state  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _allow_stub_gh(monkeypatch):
    monkeypatch.setenv("CREWVIA_CI_ALLOW_GH_IN_TESTS", "1")


def t_repo(id_="t001", pr=5, repo="tyz-works/irc-relay", **kw):
    d = task(id_=id_, pr=pr, **kw)
    d["pr_repo"] = repo
    return d


# --- scope の定義 (1 か所) ---

def test_scope_is_repo_then_target_dir_then_crewvia():
    assert ci.pr_scope({"pr_repo": "Tyz-Works/Irc-Relay", "target_dir": "/x"}) == "repo:tyz-works/irc-relay"
    assert ci.pr_scope({"target_dir": "/x"}) == "dir:/x"
    assert ci.pr_scope({}) == "" and ci.pr_scope({"pr_repo": None, "target_dir": None}) == ""


@pytest.mark.parametrize("bad", ["irc-relay", "a/b/c", "-a/b", "a b/c", 5, ["a/b"], "../x"])
def test_bad_pr_repo_has_no_scope(bad):
    assert ci.pr_scope({"pr_repo": bad}) is None


# --- 取得 ---

def test_pr_repo_card_is_fetched_with_repo_flag(tmp_path):
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [t_repo()], gh)
    assert gh.calls == [["gh", "pr", "view", "5", "--json", "headRefOid,statusCheckRollup,state",
                         "--repo", "tyz-works/irc-relay"]]


def test_card_without_pr_repo_keeps_the_old_call(tmp_path):
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [task(pr=5)], gh)
    assert gh.calls == [["gh", "pr", "view", "5", "--json", "headRefOid,statusCheckRollup,state"]]


def test_same_number_in_two_repos_is_fetched_twice(tmp_path):
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [t_repo(id_="t001", repo="a/one"), t_repo(id_="t002", repo="a/two"), task(id_="t003", pr=5)], gh)
    assert len(gh.calls) == 3


def test_same_repo_same_number_is_fetched_once(tmp_path):
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [t_repo(id_="t001"), t_repo(id_="t002")], gh)
    assert len(gh.calls) == 1


def test_bad_pr_repo_is_never_fetched(tmp_path):
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [t_repo(repo="nope")], gh)
    assert gh.calls == []
    r = rec(tmp_path)
    assert r["state"] == "no_pr" and r["pr_state"] == "NOT_FOUND" and r["reason"] == "bad_pr_repo"


# --- 通知の束ね ---

def test_merged_pr_in_another_repo_does_not_silence_this_repos_pr(tmp_path):
    """#386 P3-1: 別 repo の同番号が MERGED でも、crewvia の open PR の通知は止まらない。"""
    _write(tmp_path, "t001", 5, GREEN)                               # crewvia #5 は open・緑
    _write(tmp_path, "t002", 5, GREEN, pr_state="MERGED")            # irc-relay #5 は MERGED
    evs, closed = events(tmp_path, [task(id_="t001", status="done", pr=5), t_repo(id_="t002", status="done")])
    assert [(e["kind"], e["pr"]) for e in evs] == [("ci-green", 5)]
    assert evs[0]["scope"] == "" and len(closed) == 1 and closed[0] == ci.pr_tag(5, "repo:tyz-works/irc-relay")


def test_merged_crewvia_pr_does_not_silence_other_repos_same_number(tmp_path):
    _write(tmp_path, "t001", 5, GREEN, pr_state="MERGED")
    _write(tmp_path, "t002", 5, GREEN)
    evs, closed = events(tmp_path, [task(id_="t001", status="done", pr=5), t_repo(id_="t002", status="done")])
    assert [e["scope"] for e in evs] == ["repo:tyz-works/irc-relay"] and closed == [ci.pr_tag(5, "")]


def test_events_of_two_repos_with_the_same_number_have_distinct_ledger_keys(tmp_path):
    _write(tmp_path, "t001", 5, GREEN)
    _write(tmp_path, "t002", 5, GREEN)
    evs, _ = events(tmp_path, [task(id_="t001", status="done", pr=5), t_repo(id_="t002", status="done")])
    keys = {ci.event_key(e) for e in evs}
    assert len(evs) == 2 and len(keys) == 2
    assert any(k.startswith("ci-ci-green-pr5-") or k.startswith("ci-green-pr5-") for k in keys)


def test_closed_prefix_matches_event_keys():
    ev = {"kind": "ci-green", "pr": 5, "scope": "repo:a/b", "head": HEAD}
    tag = ci.pr_tag(5, "repo:a/b")
    assert ci.event_key(ev).startswith(ci.closed_prefix("ci-green", tag))
    assert ci.event_key({"kind": "ci-green", "pr": 5, "head": HEAD}).startswith(
        ci.closed_prefix("ci-green", ci.pr_tag(5, "")))


def test_closed_identifiers_are_a_single_type_even_when_both_scopes_are_closed(tmp_path):
    """t013 / PR #388 の P1: 既定 repo の closed (旧: int) と他 repo の closed (str) が同じサイクルに混ざると、
    `sorted()` が `int < str` を比べて TypeError になっていた (レビューで再現済み)。鍵の型を 1 つ (pr_tag の文字列)
    に揃えて落ちないようにする。"""
    _write(tmp_path, "t001", 5, GREEN, pr_state="MERGED")                       # crewvia #5 (既定 scope)
    _write(tmp_path, "t002", 5, GREEN, pr_state="CLOSED")                       # irc-relay #5 (別 scope)
    evs, closed = events(tmp_path, [task(id_="t001", status="done", pr=5), t_repo(id_="t002", status="done")])
    assert evs == []
    assert sorted(closed) == sorted([ci.pr_tag(5, ""), ci.pr_tag(5, "repo:tyz-works/irc-relay")])
    assert all(isinstance(c, str) for c in closed)


def test_reviews_are_bound_per_repo(tmp_path):
    """別 repo の同番号の review の承認を、この repo の PR の承認に数えない。"""
    _write(tmp_path, "t001", 5, GREEN)
    ok = {"status": "done", "completed_at": "2026-11-13T00:00:00Z", "skills": ["codex-review"],
          "result": f"approved head: {HEAD}"}
    other = dict(t_repo(id_="t002", status="done"), **ok)
    evs, _ = events(tmp_path, [task(id_="t001", status="done", pr=5), other])
    assert [e["kind"] for e in evs] == ["ci-green"]


def test_bad_pr_repo_card_gives_no_event(tmp_path):
    _write(tmp_path, "t001", 5, RED)
    assert events(tmp_path, [t_repo(repo="nope", status="done")])[0] == []


def test_head_of_gets_the_scope_for_non_crewvia_repos(tmp_path):
    _write(tmp_path, "t001", 5, GREEN)
    _write(tmp_path, "t002", 6, GREEN)
    seen = []

    def head_of(pr, scope=""):
        seen.append((pr, scope))
        return HEAD, None
    ci.pr_events([task(id_="t001", status="done", pr=5), t_repo(id_="t002", pr=6, status="done")],
                 registry_dir=tmp_path, now=NOW + 5, head_of=head_of)
    assert sorted(seen) == [(5, ""), (6, "repo:tyz-works/irc-relay")]


def test_fetch_pr_head_passes_repo_args():
    calls = []

    def runner(argv, **kw):
        import subprocess
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"headRefOid": HEAD}), "")
    assert ci.fetch_pr_head(5, cwd="/", runner=runner, repo_args=["--repo", "a/b"]) == (HEAD, None)
    assert calls[0][-2:] == ["--repo", "a/b"]


def test_pr_repo_card_with_missing_target_dir_is_still_fetched(tmp_path):
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [t_repo(target_dir=str(tmp_path / "gone"))], gh)
    assert gh.calls and gh.calls[0][-2:] == ["--repo", "tyz-works/irc-relay"]


# --- 記録は観測した scope を持ち、読むときに今の scope と照合する (t013 / PR #388 の P1) ---

def test_record_carries_the_scope_it_was_observed_under(tmp_path):
    poll(tmp_path, [t_repo(repo="a/one")], FakeGh(view=view_state(GREEN, "OPEN")))
    assert rec(tmp_path)["scope"] == "repo:a/one"
    poll(tmp_path, [task(id_="t002", pr=6)], FakeGh(view=view_state(GREEN, "OPEN")))
    assert rec(tmp_path, "t002")["scope"] == ""


def test_read_record_hides_a_record_observed_under_a_different_scope():
    r = ci._view(5, lambda a: (view_state(GREEN, "OPEN"), None), NOW, scope="repo:a/b")
    assert ci.record_matches_card(r, 5, scope="repo:a/b") is True
    assert ci.record_matches_card(r, 5, scope="repo:a/c") is False
    assert ci.record_matches_card(r, 5) is True                      # scope を渡さなければ検査しない (後方互換)


def test_a_record_without_a_scope_field_is_not_rejected_by_scope():
    """欄が無い記録 (この検査より前に書かれたもの) は scope で弾かない — 一律取り直しにしない。"""
    legacy = ci._unknown(5, "gh_timeout", NOW)                        # scope を渡さない = 欄を持たない
    assert "scope" not in legacy
    assert ci.record_matches_card(legacy, 5, scope="repo:a/c") is True


def test_pr_repo_change_is_retaken_without_waiting_for_the_poll_interval(tmp_path):
    """pr_number を変えずに pr_repo だけ変えた card は、間隔を待たずに取り直す (古い repo の CI をそのまま
    この repo の状況として出さない)。"""
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [t_repo(repo="a/one")], gh, poll_seconds=60)
    assert len(gh.calls) == 1
    poll(tmp_path, [t_repo(repo="a/two")], gh, now=NOW + 1, poll_seconds=60)    # 間隔内 (1 秒後)
    assert len(gh.calls) == 2 and gh.calls[-1][-2:] == ["--repo", "a/two"]
    assert rec(tmp_path)["scope"] == "repo:a/two"


def test_pr_repo_cleared_is_retaken_without_waiting_for_the_poll_interval(tmp_path):
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [t_repo(repo="a/one", pr=5)], gh, poll_seconds=60)
    assert len(gh.calls) == 1
    poll(tmp_path, [task(pr=5)], gh, now=NOW + 1, poll_seconds=60)             # pr_repo を消した
    assert len(gh.calls) == 2 and rec(tmp_path)["scope"] == ""


def test_a_terminal_record_in_the_old_repo_does_not_block_the_new_scope(tmp_path):
    """NOT_FOUND / MERGED / CLOSED の記録があっても、card の pr_repo が変われば取り直す — `pr_closed` が
    scope の食い違う終端記録を終端として扱わない。"""
    _write(tmp_path, "t001", 5, GREEN, pr_state="MERGED", scope="repo:a/old")
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [t_repo(repo="a/new")], gh, poll_seconds=60)
    assert len(gh.calls) == 1
    r = rec(tmp_path)
    assert r["scope"] == "repo:a/new" and r["pr_state"] == "OPEN"


def test_bad_pr_repo_terminal_record_recovers_once_pr_repo_is_fixed(tmp_path):
    """bad_pr_repo は NOT_FOUND (終端) として記録される。pr_repo を直せば、終端のまま固まらず取り直す。"""
    poll(tmp_path, [t_repo(repo="nope")], FakeGh(view=view_state(GREEN, "OPEN")), poll_seconds=60)
    r = rec(tmp_path)
    assert r["reason"] == "bad_pr_repo" and r["pr_state"] == "NOT_FOUND" and r["scope"] is None
    gh = FakeGh(view=view_state(GREEN, "OPEN"))
    poll(tmp_path, [t_repo(repo="a/fixed")], gh, now=NOW + 1, poll_seconds=60)
    assert len(gh.calls) == 1
    r = rec(tmp_path)
    assert r["scope"] == "repo:a/fixed" and r["state"] == "passed"


def test_pr_closed_does_not_treat_a_different_scopes_terminal_record_as_closed():
    terminal = {"pr": 5, "head": None, "state": "no_pr", "checks": [], "observed_at": NOW,
                "summary": "no PR", "pr_state": "MERGED", "scope": "repo:a/old"}
    assert ci.pr_closed(terminal, 5, scope="repo:a/old") is True
    assert ci.pr_closed(terminal, 5, scope="repo:a/new") is False
    assert ci.pr_closed(terminal, 5) is True                          # scope を渡さなければ従来どおり
