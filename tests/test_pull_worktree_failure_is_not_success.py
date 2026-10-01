"""GIT-05 (vNext 01b G1): crewvia 本体の task で worktree を作れない `plan.sh pull` は成功扱いにしない。

修正前は WARNING を出して exit 0・`worktree_path: null` で、Worker が主 checkout で作業を始めた。
修正後は card を `needs_director` に送り、exit 1・stdout は空 (knowledge/git-policy.md §1.2〜§1.4)。
正当な再 pull (その task の branch で登録済みの worktree) は再利用し、`.crewvia-env` を書き直す (W2)。

使い捨ての git repo (bare の origin + clone) と隔離 queue の上で、**本物の** git-helpers.sh を使う。
本番の `.claude/worktrees` や queue には触れない。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

import state_store_scenarios as sc
from fixture_tree import copy_plan_tree, REPO_ROOT

MISSION = sc.MISSION
AGENT = sc.AGENT
BRANCH = f"task/{MISSION}/t001-task-t001"


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if check:
        assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p


class Repo:
    def __init__(self, root: pathlib.Path):
        self.origin = root / "origin.git"
        self.root = root / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.root)], check=True,
                       capture_output=True)
        (self.root / "README").write_text("x\n")
        git(self.root, "add", "README")
        git(self.root, "commit", "-q", "-m", "init")
        git(self.root, "branch", "-M", "main")
        git(self.root, "push", "-q", "origin", "main")
        copy_plan_tree(self.root)
        # 本物の helper を置く (fixture の stub を上書き)
        (self.root / "scripts" / "git-helpers.sh").write_text(
            (REPO_ROOT / "scripts" / "git-helpers.sh").read_text())
        self.queue = self.root / "queue"
        (self.root / "registry").mkdir()
        sc.seed(self.queue, "pull")
        self.env = {"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8",
                    "CREWVIA_QUEUE": str(self.queue), "CREWVIA_REPO_ROOT": str(self.root),
                    "CREWVIA_TASKVIA": "disabled", "CREWVIA_TASK_GRAPH": "0",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.wt = self.root / ".claude" / "worktrees" / MISSION / "t001-task-t001"

    def plan(self, *args, agent=AGENT):
        env = dict(self.env)
        if agent:
            env["AGENT_NAME"] = agent
        return subprocess.run([str(self.root / "scripts" / "plan.sh"), *args], env=env,
                              capture_output=True, text=True, timeout=120)

    def pull(self):
        return self.plan("pull", "--agent", AGENT, "--skills", "code", "--task", "t001",
                         "--mission", MISSION)

    def card(self):
        meta, _ = sc.cards.parse_frontmatter(
            (self.queue / "missions" / MISSION / "tasks" / "t001.md").read_text(), source="t001")
        return meta

    def slot(self):
        return (self.queue / "assignments" / AGENT).exists()

    def reset(self):
        p = self.plan("update", "t001", "--status", "pending", "--reset", "--mission", MISSION,
                      agent="Director")
        assert p.returncode == 0, p.stderr


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def assert_exit_into_needs_director(repo, p, tag):
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert p.stdout.strip() == "", "失敗した pull は JSON を出さない (Worker に主 checkout で作業させない)"
    meta = repo.card()
    assert meta["status"] == "needs_director", meta
    assert f"({tag})" in meta["needs_director_reason"], meta["needs_director_reason"]
    assert not repo.slot(), "assignment が残っている"


def test_w0_creates_worktree_and_env(repo):
    p = repo.pull()
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["worktree_path"] == str(repo.wt)
    assert repo.wt.is_dir()
    env_text = (repo.wt / ".crewvia-env").read_text()
    assert "CREWVIA_TASK_ID=t001" in env_text
    assert git(repo.wt, "branch", "--show-current").stdout.strip() == BRANCH


def test_a_unwritable_parent_dir_fails_closed(repo):
    parent = repo.root / ".claude" / "worktrees"
    parent.mkdir(parents=True)
    parent.chmod(0o500)
    try:
        p = repo.pull()
    finally:
        parent.chmod(0o700)
    assert_exit_into_needs_director(repo, p, "W5")
    assert "WARNING: worktree creation skipped" not in p.stderr


def test_b_foreign_directory_at_the_path_fails_closed_and_is_kept(repo):
    repo.wt.mkdir(parents=True)
    (repo.wt / "someone-elses-work").write_text("keep me\n")
    p = repo.pull()
    assert_exit_into_needs_director(repo, p, "W4")
    assert (repo.wt / "someone-elses-work").read_text() == "keep me\n", "dir を消さない"


def test_c_branch_checked_out_in_another_worktree_fails_closed(repo, tmp_path):
    git(repo.root, "worktree", "add", "-q", "-b", BRANCH, str(tmp_path / "elsewhere"), "main")
    p = repo.pull()
    assert_exit_into_needs_director(repo, p, "W5")


def test_w3_path_registered_for_a_different_branch_fails_closed(repo):
    repo.wt.parent.mkdir(parents=True)
    git(repo.root, "worktree", "add", "-q", "-b", "other-branch", str(repo.wt), "main")
    p = repo.pull()
    assert_exit_into_needs_director(repo, p, "W3")
    assert git(repo.wt, "branch", "--show-current").stdout.strip() == "other-branch"


def test_w2_legitimate_repull_reuses_the_worktree_and_rewrites_env(repo):
    first = repo.pull()
    assert first.returncode == 0, first.stderr
    (repo.wt / "wip.txt").write_text("previous attempt\n")
    (repo.wt / ".crewvia-env").write_text("# stale\n")
    repo.reset()
    assert repo.card()["status"] == "pending"
    again = repo.pull()
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["worktree_path"] == str(repo.wt)
    assert "reusing registered worktree" in again.stderr
    assert "CREWVIA_TASK_ID=t001" in (repo.wt / ".crewvia-env").read_text()
    assert (repo.wt / "wip.txt").exists(), "再利用は中身を捨てない"
    assert repo.card()["status"] == "in_progress"


def test_w6_fetch_failure_warns_and_continues_on_success(repo):
    git(repo.root, "remote", "set-url", "origin", str(repo.root / "nowhere.git"))
    p = repo.pull()
    assert p.returncode == 0, p.stderr
    assert "git fetch origin failed" in p.stderr, "成功時にも git の警告を流す"


def test_n2_missing_helper_fails_closed(repo):
    (repo.root / "scripts" / "git-helpers.sh").unlink()
    p = repo.pull()
    assert_exit_into_needs_director(repo, p, "N2")


def _replace_helper(repo, body: str):
    (repo.root / "scripts" / "git-helpers.sh").write_text(body)


def test_n6_helper_succeeds_with_empty_stdout_fails_closed(repo):
    _replace_helper(repo, "crewvia_create_worktree() { return 0; }\n")
    p = repo.pull()
    assert_exit_into_needs_director(repo, p, "N6")


def test_n7_env_file_not_writable_fails_closed(repo):
    wt = repo.root / "fake-wt"
    _replace_helper(repo, f"crewvia_create_worktree() {{ mkdir -p {wt}; chmod 500 {wt}; echo {wt}; }}\n")
    try:
        p = repo.pull()
    finally:
        wt.chmod(0o700)
    assert_exit_into_needs_director(repo, p, "N7")


def test_cas_when_the_card_was_reset_meanwhile_pull_writes_nothing(repo):
    plan = repo.root / "scripts" / "plan.sh"
    _replace_helper(
        repo,
        f"crewvia_create_worktree() {{ AGENT_NAME=Director {plan} update t001 --status pending --reset "
        f"--mission {MISSION} >&2; echo 'crewvia_create_worktree: W5: boom' >&2; return 1; }}\n")
    p = repo.pull()
    assert p.returncode == 1 and p.stdout.strip() == ""
    meta = repo.card()
    assert meta["status"] == "pending", "他者が戻した card を pull が上書きしてはいけない"
    assert "書き換えませんでした" in p.stderr


def test_failed_task_is_not_handed_out_again(repo):
    repo.wt.mkdir(parents=True)
    assert repo.pull().returncode == 1
    again = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--mission", MISSION)
    assert again.returncode == 2, (again.stdout, again.stderr)


def test_target_dir_task_keeps_todays_behaviour(repo):
    path = repo.queue / "missions" / MISSION / "tasks" / "t001.md"
    path.write_text(path.read_text().replace("target_dir: null", f"target_dir: {repo.root}"))
    (repo.root / "scripts" / "git-helpers.sh").unlink()
    p = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--task", "t001",
                  "--mission", MISSION, "--target-dir", str(repo.root))
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["worktree_path"] is None
    assert not repo.wt.exists()
