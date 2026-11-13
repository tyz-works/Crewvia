"""done --pr は pr_number と一緒に pr_repo も下流の review / codex-review に伝える (t001 / PR #388 の P2)。
伝えないと review card が crewvia の同じ番号の PR を見る・承認が (repo, PR) の鍵で束ねられない。"""

from __future__ import annotations

import pytest

from test_resolve_pr import MISSION, _add_review_task, _pull_and_done, repo  # noqa: F401


def _set_repo(repo, tid, value):
    p = repo.plan("update", tid, "--pr-repo", value, "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr


def test_done_pr_propagates_pr_repo_to_downstream_review(repo):
    rv = _add_review_task(repo)
    _set_repo(repo, "t001", "tyz-works/irc-relay")
    _pull_and_done(repo, 5)
    card = repo.card_path(rv).read_text()
    assert "pr_number: 5" in card
    assert "pr_repo: tyz-works/irc-relay" in card


def test_no_pr_repo_upstream_writes_no_pr_repo(repo):
    rv = _add_review_task(repo)
    _pull_and_done(repo, 5)
    card = repo.card_path(rv).read_text()
    assert "pr_number: 5" in card and "pr_repo" not in card


def test_conflicting_downstream_pr_repo_is_not_overwritten_and_nothing_is_written(repo):
    rv = _add_review_task(repo)
    _set_repo(repo, "t001", "tyz-works/irc-relay")
    _set_repo(repo, rv, "tyz-works/other")
    done = _pull_and_done(repo, 5)
    card = repo.card_path(rv).read_text()
    assert "pr_repo: tyz-works/other" in card
    assert "pr_number" not in card
    assert "pr_repo が食い違う" in done.stderr
