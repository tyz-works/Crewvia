"""vNext 01b G3 (cutover): plan.sh pull / git-helpers.sh が branch・path・base を Resolver から得て、`plan.sh pr-base` が PR base を返す。

設計は `knowledge/git-policy.md` §3 (CLI・`pr-base`)・§4 (一覧)・§5 (PR base の渡し方)。

固定すること:

1. policy 未指定の pull は今までと同じ (`.crewvia-env` は 3 行のまま。§5: PR base は env で渡さない)。
2. custom の `git:` (base_branch / pr_base を main 以外) で task branch の切り元と `plan.sh pr-base` の値が変わる。
3. `plan.sh pr-base` の拒否 (AGENT_NAME 空・assignment なし/形違い・card が別人/in_progress でない・`git:` が壊れている)
   はすべて exit 1・stdout 空。`target_dir` の task は mission の `pr_base` に関わらず `main` (範囲外・今の挙動)。
   名指しの形は所有者を見ない。退避済み (archive) の mission は exit 1。
4. 申し送り (Director / G2 レビュー t011): mission.yaml の**無関係な 1 行の字下げミス**で pull が止まるときの出口 =
   exit 1・stdout 空・card は `needs_director`・理由に「どのファイルの何行目か・直し方」 (行の中身は出さない)。
   lint は同じ入力で事前に FAIL。
5. `git-helpers.sh` の `crewvia_create_pr` は Resolver の pr_base を `--base` に渡す (取れなければ push も PR 作成もしない)。
6. worktree の登録を NUL 区切りのまま読む (改行を含む path の worktree が別の登録を偽造できない)。

使い捨ての git repo (bare の origin + clone) と隔離 queue の上で、本物の git-helpers.sh と lib_git_policy.py を使う。
本番の queue・`.claude/worktrees` には触れない。`gh` は stub。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys

import pytest

import state_store_scenarios as sc
from fixture_tree import copy_plan_tree, REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import lint_plan  # noqa: E402

MISSION = sc.MISSION
AGENT = sc.AGENT
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
SECRET = "SENTINEL_OTHER_FIELD_CONTENT_7731"


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV})
    if check:
        assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p


class Repo:
    def __init__(self, root: pathlib.Path):
        self.base = root
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
        # 本物の helper を置く (fixture の stub を上書き)。lib_git_policy.py は copy_plan_tree が lib として写している。
        (self.root / "scripts" / "git-helpers.sh").write_text(
            (REPO_ROOT / "scripts" / "git-helpers.sh").read_text())
        self.queue = self.root / "queue"
        (self.root / "registry").mkdir()
        sc.seed(self.queue, "pull")
        self.env = {"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8",
                    "CREWVIA_QUEUE": str(self.queue), "CREWVIA_REPO_ROOT": str(self.root),
                    "CREWVIA_TASKVIA": "disabled", "CREWVIA_TASK_GRAPH": "0", **GIT_ENV}
        self.wt = self.root / ".claude" / "worktrees" / MISSION / "t001-task-t001"

    # -- queue ---------------------------------------------------------------
    @property
    def mission_yaml(self) -> pathlib.Path:
        return self.queue / "missions" / MISSION / "mission.yaml"

    def add_to_mission_yaml(self, text: str):
        self.mission_yaml.write_text(self.mission_yaml.read_text() + text)

    def set_git(self, **fields):
        self.add_to_mission_yaml("git:\n" + "".join(f"  {k}: {v}\n" for k, v in fields.items()))

    def card_path(self, tid="t001") -> pathlib.Path:
        return self.queue / "missions" / MISSION / "tasks" / f"{tid}.md"

    def card(self):
        meta, _ = sc.cards.parse_frontmatter(self.card_path().read_text(), source="t001")
        return meta

    # -- commands --------------------------------------------------------------
    def plan(self, *args, agent=AGENT, cwd=None, extra_env=None):
        env = dict(self.env)
        if agent:
            env["AGENT_NAME"] = agent
        env.update(extra_env or {})
        return subprocess.run([str(self.root / "scripts" / "plan.sh"), *args], env=env,
                              capture_output=True, text=True, timeout=120, cwd=cwd or self.root)

    def pull(self):
        return self.plan("pull", "--agent", AGENT, "--skills", "code", "--task", "t001",
                         "--mission", MISSION)

    def pr_base(self, *args, **kw):
        return self.plan("pr-base", *args, **kw)

    def branch_off(self, name, filename):
        """origin に `name` の branch を作る (main とは違う commit を持つ)。"""
        git(self.root, "checkout", "-q", "-b", name, "main")
        (self.root / filename).write_text(name + "\n")
        git(self.root, "add", filename)
        git(self.root, "commit", "-q", "-m", name)
        git(self.root, "push", "-q", "origin", name)
        git(self.root, "checkout", "-q", "main")


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def head_of(wt):
    return git(wt, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# 1. policy 未指定: 今までと同じ
# ---------------------------------------------------------------------------

def test_default_pull_cuts_from_origin_main_and_env_file_has_no_extra_lines(repo):
    p = repo.pull()
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["worktree_path"] == str(repo.wt)
    assert head_of(repo.wt) == git(repo.root, "rev-parse", "origin/main").stdout.strip()
    assert git(repo.wt, "branch", "--show-current").stdout.strip() == f"task/{MISSION}/t001-task-t001"
    # §5: PR base は env で渡さない。.crewvia-env は G3 の前と同じ 3 行だけ (追加の行は無い)。
    lines = (repo.wt / ".crewvia-env").read_text().splitlines()
    assert [l.split("=")[0] for l in lines] == [
        "export CREWVIA_MISSION_SLUG", "export CREWVIA_TASK_ID", "export CREWVIA_TASK_SLUG"], lines
    assert "PR_BASE" not in (repo.wt / ".crewvia-env").read_text()


# ---------------------------------------------------------------------------
# 2. custom の git:
# ---------------------------------------------------------------------------

def test_custom_base_branch_is_where_the_task_branch_is_cut_from(repo):
    repo.branch_off("develop", "dev.txt")
    repo.set_git(mode="direct", base_branch="develop", pr_base="staging")
    p = repo.pull()
    assert p.returncode == 0, p.stderr
    assert head_of(repo.wt) == git(repo.root, "rev-parse", "origin/develop").stdout.strip()
    assert head_of(repo.wt) != git(repo.root, "rev-parse", "origin/main").stdout.strip()
    assert (repo.wt / "dev.txt").exists()


def test_custom_pr_base_is_what_pr_base_returns_from_any_cwd(repo):
    repo.set_git(base_branch="main", pr_base="staging")
    assert repo.pull().returncode == 0
    for cwd in (repo.root, repo.wt, repo.base):   # worktree・主 checkout・無関係な dir (cwd に依らない)
        p = repo.pr_base(cwd=cwd)
        assert (p.returncode, p.stdout) == (0, "staging\n"), (cwd, p.stderr)
    p = repo.pr_base("--diff-ref")
    assert p.stdout == "origin/staging\n"


def test_default_pr_base_is_main(repo):
    assert repo.pull().returncode == 0
    assert repo.pr_base().stdout == "main\n"
    assert repo.pr_base("--diff-ref").stdout == "origin/main\n"


def test_custom_task_branch_pattern_names_the_branch(repo):
    repo.set_git(task_branch_pattern='"work/{mission_slug}/{task_id}-{task_slug}"')
    p = repo.pull()
    assert p.returncode == 0, p.stderr
    assert git(repo.wt, "branch", "--show-current").stdout.strip() == f"work/{MISSION}/t001-task-t001"


# ---------------------------------------------------------------------------
# 3. plan.sh pr-base の拒否と target_dir・名指しの形
# ---------------------------------------------------------------------------

def assert_refused(p, needle=None):
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", "決められないとき stdout は空 (main に倒さない)"
    if needle:
        assert needle in p.stderr, p.stderr


def test_pr_base_refuses_without_agent_name(repo):
    repo.set_git(pr_base="staging")
    assert repo.pull().returncode == 0
    assert_refused(repo.pr_base(agent=None), "AGENT_NAME")


def test_pr_base_refuses_without_an_assignment(repo):
    repo.set_git(pr_base="staging")
    assert_refused(repo.pr_base())          # まだ pull していない


def test_pr_base_refuses_a_malformed_assignment(repo):
    assert repo.pull().returncode == 0
    (repo.queue / "assignments" / AGENT).write_text("not-a-valid-assignment\n")
    assert_refused(repo.pr_base(), "形が違います")


def test_pr_base_refuses_when_card_worker_is_someone_else(repo):
    repo.set_git(pr_base="staging")
    assert repo.pull().returncode == 0
    text = repo.card_path().read_text().replace(f"worker: {AGENT}", "worker: Someone")
    repo.card_path().write_text(text)
    assert_refused(repo.pr_base(), "食い違")


def test_pr_base_refuses_when_card_is_not_in_progress(repo):
    repo.set_git(pr_base="staging")
    assert repo.pull().returncode == 0
    text = repo.card_path().read_text().replace("status: in_progress", "status: done")
    repo.card_path().write_text(text)
    assert_refused(repo.pr_base(), "食い違")


@pytest.mark.parametrize("git_block", [
    "git:\n  mode: integration\n",
    "git:\n  pr_base: 'has space'\n",
    "git:\n  unknown_field: x\n",
])
def test_pr_base_refuses_a_broken_policy_instead_of_returning_main(repo, git_block):
    assert repo.pull().returncode == 0
    repo.add_to_mission_yaml(git_block)
    assert_refused(repo.pr_base(), "git policy")
    assert_refused(repo.pr_base("--mission", MISSION, "--task", "t001"), "git policy")


def test_target_dir_task_gets_main_whatever_the_mission_says(repo):
    repo.set_git(pr_base="staging")
    card = repo.card_path()
    card.write_text(card.read_text().replace("target_dir: null", f"target_dir: {repo.root}"))
    p = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--task", "t001",
                  "--mission", MISSION, "--target-dir", str(repo.root))
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["worktree_path"] is None
    assert repo.pr_base().stdout == "main\n"
    assert repo.pr_base("--diff-ref").stdout == "main\n", "TARGET_DIR の checkout に origin/main があるとは限らない (local の main)"
    named = repo.pr_base("--mission", MISSION, "--task", "t001", agent=None)
    assert named.stdout == "main\n"


def test_target_dir_pull_is_unchanged_no_worktree_no_env_file_and_helper_not_needed(repo):
    """target_dir の task (範囲外) は G3 でも worktree を作らず、helper も Resolver も呼ばない (helper が無くても通る)。"""
    repo.add_to_mission_yaml("git:\n  mode: integration\n")      # 壊れた policy でも target_dir の pull は止まらない
    (repo.root / "scripts" / "git-helpers.sh").unlink()
    card = repo.card_path()
    card.write_text(card.read_text().replace("target_dir: null", f"target_dir: {repo.root}"))
    p = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--task", "t001",
                  "--mission", MISSION, "--target-dir", str(repo.root))
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["worktree_path"] is None and out["target_dir"] == str(repo.root)
    assert not (repo.root / ".claude" / "worktrees").exists()
    assert not list(repo.root.rglob(".crewvia-env"))


def test_named_form_ignores_ownership_and_status(repo):
    repo.set_git(pr_base="staging")
    for status in ("pending", "done"):
        text = repo.card_path().read_text()
        repo.card_path().write_text(text.replace("status: pending", f"status: {status}"))
        p = repo.pr_base("--mission", MISSION, "--task", "t001", agent=None)   # AGENT_NAME も assignment も無い
        assert (p.returncode, p.stdout) == (0, "staging\n"), p.stderr
        repo.card_path().write_text(text)
    assert repo.pr_base("--mission", MISSION, "--task", "t001", "--diff-ref", agent=None).stdout == "origin/staging\n"


@pytest.mark.parametrize("args", [
    ["--mission", MISSION],                     # 片方だけ
    ["--task", "t001"],
    ["--mission", MISSION, "--task", "t099"],   # card が無い
    ["--mission", "no-such-mission", "--task", "t001"],
])
def test_named_form_refuses_what_it_cannot_decide(repo, args):
    assert_refused(repo.pr_base(*args, agent=None))


def test_archived_mission_is_refused_with_a_clear_reason(repo):
    """申し送り 2 (G2 レビュー): archive 済み mission の task は pull の対象外・`pr-base` は exit 1 (active だけを見る)。"""
    shutil.move(str(repo.queue / "missions" / MISSION), str(repo.queue / "archive" / MISSION))
    p = repo.pr_base("--mission", MISSION, "--task", "t001", agent=None)
    assert_refused(p, "退避済み")


def test_pr_base_writes_nothing(repo):
    repo.set_git(pr_base="staging")
    assert repo.pull().returncode == 0

    def snap():
        return {str(p.relative_to(repo.queue)): p.read_bytes()
                for p in sorted(repo.queue.rglob("*")) if p.is_file() and "audit" not in p.parts}
    before = snap()
    repo.pr_base()
    repo.pr_base("--mission", MISSION, "--task", "t001", "--diff-ref", agent=None)
    assert snap() == before


# ---------------------------------------------------------------------------
# 4. 申し送り 1: 無関係な字下げミス 1 行で pull が止まるときの出口
# ---------------------------------------------------------------------------

UNRELATED_INDENT_MISTAKE = f"owner: someone\n    {SECRET}: 1\n"


def assert_pull_stopped_cleanly(repo, p):
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    assert p.stdout.strip() == "", "止まった pull は JSON を出さない (Worker に主 checkout で作業させない)"
    meta = repo.card()
    assert meta["status"] == "needs_director", meta
    assert not (repo.queue / "assignments" / AGENT).exists(), "assignment が残っている"
    assert not repo.wt.exists(), "止まったのに worktree ができている"


def test_unrelated_indent_mistake_stops_the_pull_with_file_line_and_fix_and_leaks_no_line_content(repo):
    repo.add_to_mission_yaml(UNRELATED_INDENT_MISTAKE)
    p = repo.pull()
    assert_pull_stopped_cleanly(repo, p)
    reason = repo.card()["needs_director_reason"]     # card の 1 行目 (200 文字で切れる。全文は本文)
    assert "(P1)" in reason and "行目" in reason, reason
    card_text = repo.card_path().read_text()           # 要約 + 本文 (Director が読むもの)
    for text in (card_text, p.stderr):
        assert "mission.yaml" in text, "どのファイルか"
        assert "行目" in text, "何行目か"
        assert "plan.sh lint" in text and "--reset" in text, "直し方 (lint で確かめて、直したら reset で戻す)"
        assert SECRET not in text, "mission.yaml の行の中身は出さない"
    assert SECRET not in p.stdout


def test_lint_fails_the_same_input_before_any_pull(repo):
    repo.add_to_mission_yaml(UNRELATED_INDENT_MISTAKE)
    r = lint_plan.check_git_policy(MISSION, str(repo.queue))
    assert [lvl for lvl, _c, _m in r] == ["FAIL"]
    assert "行目" in r[0][2] and SECRET not in r[0][2]
    p = repo.plan("lint", MISSION, agent="Director")
    assert p.returncode != 0 and "git-policy" in p.stdout and SECRET not in p.stdout + p.stderr


@pytest.mark.parametrize("block,code", [
    ("git:\n  mode: integration\n", "unsupported_mode"),
    ("git:\n  worktree_root: elsewhere\n", "unsupported_value"),
    ("git:\n  base_branch: ''\n", "invalid_value"),
])
def test_unsupported_or_dangerous_policy_stops_pull_and_fails_lint(repo, block, code):
    repo.add_to_mission_yaml(block)
    assert_pull_stopped_cleanly(repo, repo.pull())
    assert code in repo.card()["needs_director_reason"]
    assert lint_plan.check_git_policy(MISSION, str(repo.queue))[0][0] == "FAIL"


def test_after_fixing_the_file_a_reset_task_pulls_normally(repo):
    """出口が閉じていないこと: 直して reset すれば同じ task が取れる (needs_director → pending → pull)。"""
    repo.add_to_mission_yaml(UNRELATED_INDENT_MISTAKE)
    assert_pull_stopped_cleanly(repo, repo.pull())
    repo.mission_yaml.write_text(repo.mission_yaml.read_text().replace(UNRELATED_INDENT_MISTAKE, ""))
    assert lint_plan.check_git_policy(MISSION, str(repo.queue)) == []
    r = repo.plan("update", "t001", "--status", "pending", "--reset", "--mission", MISSION, agent="Director")
    assert r.returncode == 0, r.stderr
    p = repo.pull()
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["worktree_path"] == str(repo.wt)


def test_lint_catches_a_disagreement_between_the_two_readers(repo, monkeypatch):
    """Resolver が読んだ値と本物の YAML パーサの値が割れたら FAIL (parse_yaml の読み違いを 2 つの読み手で拾う)。"""
    repo.set_git(pr_base="staging")
    real = lint_plan._GIT_POLICY.policy_from_text

    def other_reading(text, source="x"):
        import dataclasses
        return dataclasses.replace(real(text, source), pr_base="something-else")
    monkeypatch.setattr(lint_plan._GIT_POLICY, "policy_from_text", other_reading)
    r = lint_plan.check_git_policy(MISSION, str(repo.queue))
    assert r and r[0][0] == "FAIL" and "pr_base" in r[0][2]


def test_lint_fails_when_pyyaml_is_unavailable_instead_of_falling_back(repo, monkeypatch):
    repo.set_git(pr_base="staging")
    monkeypatch.setattr(lint_plan, "yaml", None)
    r = lint_plan.check_git_policy(MISSION, str(repo.queue))
    assert r and r[0][0] == "FAIL" and "PyYAML" in r[0][2]


def test_valid_custom_policy_passes_lint(repo):
    repo.set_git(mode="direct", base_branch="develop", pr_base="staging",
                 task_branch_pattern='"task/{mission_slug}/{task_id}-{task_slug}"')
    assert lint_plan.check_git_policy(MISSION, str(repo.queue)) == []


# ---------------------------------------------------------------------------
# 5. git-helpers.sh crewvia_create_pr / crewvia_remove_worktree
# ---------------------------------------------------------------------------

def _gh_stub(repo) -> pathlib.Path:
    bin_dir = repo.base / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = repo.base / "gh-args.log"
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> ' + str(log) + '\necho https://example.invalid/pr/1\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    repo.env["PATH"] = f"{bin_dir}:{repo.env['PATH']}"
    return log


def _call_helper(repo, snippet, cwd=None):
    return subprocess.run(["bash", "-c", f"source {repo.root}/scripts/git-helpers.sh && {snippet}"],
                          cwd=cwd or repo.root, env=repo.env, capture_output=True, text=True)


@pytest.mark.parametrize("fields,expect", [({}, "--base main"), ({"pr_base": "staging"}, "--base staging")])
def test_create_pr_passes_the_resolved_pr_base(repo, fields, expect):
    log = _gh_stub(repo)
    if fields:
        repo.set_git(**fields)
    assert repo.pull().returncode == 0
    branch = f"task/{MISSION}/t001-task-t001"
    r = _call_helper(repo, f'crewvia_create_pr "{MISSION}" "{branch}" "title" "body"', cwd=repo.wt)
    assert r.returncode == 0, r.stderr
    assert expect in log.read_text().splitlines()[0]
    assert f"--head {branch}" in log.read_text()


def test_create_pr_refuses_when_pr_base_cannot_be_resolved_and_does_not_push(repo):
    log = _gh_stub(repo)
    assert repo.pull().returncode == 0
    repo.add_to_mission_yaml("git:\n  mode: integration\n")
    branch = f"task/{MISSION}/t001-task-t001"
    r = _call_helper(repo, f'crewvia_create_pr "{MISSION}" "{branch}" "t" "b"', cwd=repo.wt)
    assert r.returncode == 1 and "PR base を決められません" in r.stderr
    assert not log.exists(), "gh を呼んでいない (main に倒さない)"
    assert git(repo.root, "ls-remote", "--heads", "origin", branch).stdout.strip() == "", "push もしていない"


def test_remove_worktree_resolves_the_same_path_as_create(repo):
    assert repo.pull().returncode == 0 and repo.wt.is_dir()
    r = _call_helper(repo, f'crewvia_remove_worktree "{MISSION}" t001 task-t001')
    assert r.returncode == 0, r.stderr
    assert not repo.wt.exists()


# ---------------------------------------------------------------------------
# 6. worktree の登録を NUL 区切りのまま読む
# ---------------------------------------------------------------------------

def test_worktree_lookup_is_not_forged_by_a_newline_in_another_worktrees_path(repo):
    """改行を含む path の worktree が `worktree <期待する path>` の行を偽造できない (旧: NUL を改行に直して読んでいた)。"""
    ours = repo.base / "ours"
    ours.mkdir()                                        # 登録されていない dir (期待は none)
    evil = repo.base / f"evil\nworktree {ours}\nbranch refs/heads/forged"
    git(repo.root, "worktree", "add", "-q", "-b", "evil-branch", str(evil), "main")
    r = _call_helper(repo, f'_crewvia_worktree_lookup "{ours}"')
    assert (r.returncode, r.stdout.strip()) == (0, "none"), (r.stdout, r.stderr)


def test_worktree_lookup_finds_a_registered_worktree_among_newline_paths(repo):
    weird = repo.base / "wei\nrd"
    git(repo.root, "worktree", "add", "-q", "-b", "weird-branch", str(weird), "main")
    mine = repo.base / "mine"
    git(repo.root, "worktree", "add", "-q", "-b", "mine-branch", str(mine), "main")
    r = _call_helper(repo, f'_crewvia_worktree_lookup "{mine}"')
    assert r.stdout.strip() == "registered refs/heads/mine-branch 0", (r.stdout, r.stderr)
    r = _call_helper(repo, '_crewvia_worktree_lookup "$1"'.replace('"$1"', f'"{weird}"'))
    assert r.stdout.strip() == "registered refs/heads/weird-branch 0", (r.stdout, r.stderr)


def test_lookup_failure_of_git_is_a_failure_not_none(repo):
    r = _call_helper(repo, f'cd {repo.base} && _crewvia_worktree_lookup "{repo.base}"', cwd=repo.base)
    assert r.returncode != 0, "git が読めない (repo の外) は none ではなく失敗"
