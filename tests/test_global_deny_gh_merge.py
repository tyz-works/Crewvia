"""Worker の PR merge (gh pr merge / API 経由) を _global.deny が止める。Director は通る (t001 / 20261120-deny-worker-pr-merge)。

背景: main の ruleset は管理者の PR 経由 merge を例外にしており、Worker も同じ管理者アカウントで動く。
Worker が merge すると review も CI も通っていない PR が main に入る。merge は Director だけ。
AGENT_NAME が空の素のセッションも deny される (git push origin main* と同じ方針)。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
from lib_skill_perms import check_permission, load_config  # noqa: E402

YAML_PATH = str(REPO / "config" / "skill-permissions.yaml")


def _decision(cmd: str, skills: str = "code") -> str:
    return check_permission(load_config(YAML_PATH), skills, f"Bash({cmd})")["decision"]


DENIED = [
    "gh pr merge 12",
    "gh pr merge 12 --squash --match-head-commit abc",
    "gh pr merge --auto --squash",
    "gh -R tyz-works/crewvia pr merge 12",
    "gh --repo tyz-works/crewvia pr merge 12",
    "/usr/bin/gh pr merge 12",
    "env X=1 gh pr merge 12",
    "cd x && gh pr merge 12",
    "gh pr view 1; gh pr merge 1",
    "echo a | gh pr merge 1",
    "bash -c 'gh pr merge 12'",
    'PR=12; gh pr merge "$PR" --squash',
    "gh api repos/o/r/pulls/12/merge -X PUT",
    "gh api -X PUT repos/o/r/pulls/12/merge",
    "gh api --method PUT repos/o/r/pulls/12/merge -f merge_method=squash",
    "gh api repos/o/r/pulls/12/merge",
    "gh api -f merge_method=squash repos/o/r/pulls/12/merge",
    'gh api "repos/o/r/pulls/$PR/merge" -X PUT',
    "gh api /repos/{owner}/{repo}/pulls/12/merge -X PUT",
    "env A=1 gh api repos/o/r/pulls/12/merge -X PUT",
    "gh api graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\"x\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { enablePullRequestAutoMerge(input:{pullRequestId:\"x\"}) { clientMutationId } }'",
    "cd x && gh api graphql -f query='mutation { mergePullRequest(input:{}) {x} }'",
]

ALLOWED = [
    "gh pr view 12",
    "gh pr checks 12",
    "gh pr diff 12",
    "gh pr create --title t --body b",
    "gh pr update-branch 12",
    "gh pr comment 12 --body 'looks good'",
    "gh pr comment 12 --body 'gh pr merge later'",
    "gh pr list --state merged",
    "gh api repos/o/r/pulls/12",
    "gh api repos/o/r/pulls/12/files",
    "gh api repos/o/r/pulls/12/reviews",
    "gh api repos/o/r/pulls/12 -X GET",
    "gh api graphql -f query='query { viewer { login } }'",
    'git commit -m "gh pr merge の説明"',
    "echo gh pr merge 12",
    "git status && gh pr view 3",
]


@pytest.mark.parametrize("cmd", DENIED)
def test_worker_merge_forms_denied(cmd):
    assert _decision(cmd) == "deny", cmd
    assert _decision(cmd, skills="") == "deny", cmd     # skills に関係なく (global)


@pytest.mark.parametrize("cmd", ALLOWED)
def test_non_merge_gh_forms_not_denied(cmd):
    assert _decision(cmd) != "deny", cmd


# --- 本物の hooks/pre-tool-use.sh: Director は通る / Worker・素のセッションは deny -----------------

pytestmark_hook = pytest.mark.skipif(shutil.which("jq") is None, reason="jq が必要")


@pytest.fixture()
def hook_root(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(REPO / "hooks", root / "hooks")
    shutil.copytree(REPO / "config", root / "config")
    (root / "registry").mkdir()
    (root / "registry" / "workers.yaml").write_text(
        "workers:\n  - name: Sora\n    role: director\n    skills: []\n"
        "  - name: Luna\n    role: worker\n    skills: [code]\n", encoding="utf-8")
    (root / ".git").mkdir()
    return root


def _hook(root: Path, command: str, agent: str) -> str:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin", "CREWVIA_REPO": str(root),
           "CREWVIA_TASKVIA": "disabled", "TASKVIA_TOKEN": "", "SKILLS": "code", "AGENT_NAME": agent}
    r = subprocess.run(["bash", str(root / "hooks/pre-tool-use.sh")], input=payload,
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    out = r.stdout.strip()
    if not out:
        return "SILENT"
    j = json.loads(out.splitlines()[-1])["hookSpecificOutput"]
    return f'{j["permissionDecision"]}: {j["permissionDecisionReason"]}'


@pytestmark_hook
@pytest.mark.parametrize("cmd", ["gh pr merge 12 --squash --match-head-commit abc",
                                 "gh api repos/o/r/pulls/12/merge -X PUT"])
def test_hook_denies_worker_and_bare_session_but_not_director(hook_root, cmd):
    assert _hook(hook_root, cmd, "Luna").startswith("deny: Global permission denied")
    assert _hook(hook_root, cmd, "").startswith("deny: Global permission denied")
    assert not _hook(hook_root, cmd, "Sora").startswith("deny")
