"""vNext 01b G3 fix 2 巡目 (PR #266): 外から来る名前は検証してからパスにする・利用者に見えるエラーは 1 か所の整形だけ。

1. `plan.sh pr-base` は mission slug と task id の**両方**を、ファイルシステムを見る前に検証する
   (`--mission ../archive/<slug>` で archive 済みの `target_dir` card を読んで main を返さない)。
   Resolver の CLI (`lib_git_policy.py`) も同じ関数 (`check_mission_slug` / `check_task_id`) を通る。
2. lint の診断・stderr・card の理由に、mission.yaml の無関係な欄の中身 (secret) が出ない。
   PyYAML の例外は「型名 + 行・列」だけにして出す (`lint_plan._yaml_error_location`)。
3. 整形は `format_policy_error` 1 か所。`e.detail` を自前で並べる経路が増えたら赤 (構造ガード)。
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

import pytest

from fixture_tree import REPO_ROOT
from test_git_policy_pull_and_pr_base_cutover import (  # noqa: F401  (repo は fixture)
    MISSION, AGENT, Repo, repo, assert_refused)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import lint_plan  # noqa: E402
import lib_git_policy  # noqa: E402

SECRET = "SENTINEL_SECRET_4419"

BAD_MISSIONS = [
    f"../archive/{MISSION}",   # active の外 (archive) へ
    "..", ".", "/etc", "a/b", "a\\b", "x\ny", f"{MISSION}/../{MISSION}",
]
BAD_TASKS = [
    "../../../archive/x/tasks/t001", "t001/../t001", "/etc/passwd", "t1x", "T001", "t001\n", " t001", "t",
    "..", ".",
]


def archive_copy_with_target_dir(repo: Repo):
    """active の mission を archive/ にも写し、その card に `target_dir` を付ける (旧実装では main を返して成功した)。"""
    dst = repo.queue / "archive" / MISSION
    shutil.copytree(repo.queue / "missions" / MISSION, dst)
    card = dst / "tasks" / "t001.md"
    text = card.read_text()
    assert "target_dir" in text or "---\n" in text
    card.write_text(re.sub(r"^---\n", "---\ntarget_dir: /tmp/elsewhere\n", text, count=1))
    return dst


# ---------------------------------------------------------------------------
# 1. 識別子の検証
# ---------------------------------------------------------------------------

def test_pr_base_does_not_read_an_archived_target_dir_card_through_a_traversal_slug(repo):
    """P2-1 の再現: `../archive/<slug>` は旧実装で main を返して exit 0 だった。"""
    archive_copy_with_target_dir(repo)
    p = repo.pr_base("--mission", f"../archive/{MISSION}", "--task", "t001", agent=None)
    assert_refused(p, "識別子")


@pytest.mark.parametrize("mission", BAD_MISSIONS)
def test_pr_base_refuses_a_bad_mission_slug_before_touching_the_filesystem(repo, mission):
    p = repo.pr_base("--mission", mission, "--task", "t001", agent=None)
    assert_refused(p)
    assert "識別子" in p.stderr or "mission と --task" in p.stderr, p.stderr


@pytest.mark.parametrize("task", BAD_TASKS)
def test_pr_base_refuses_a_bad_task_id_before_touching_the_filesystem(repo, task):
    p = repo.pr_base("--mission", MISSION, "--task", task, agent=None)
    assert_refused(p)
    assert "識別子" in p.stderr or "mission と --task" in p.stderr, p.stderr


def test_pr_base_task_argument_cannot_reach_another_missions_card(repo):
    other = repo.queue / "missions" / "other-mission"
    shutil.copytree(repo.queue / "missions" / MISSION, other)
    p = repo.pr_base("--mission", MISSION, "--task", "../../other-mission/tasks/t001", agent=None)
    assert_refused(p, "識別子")


def test_pr_base_refuses_a_bad_identifier_from_an_assignment(repo):
    """引数なしの形でも、assignment が指す値を同じ検査に通す (assignment は信頼しない)。"""
    assert repo.pull().returncode == 0
    (repo.queue / "assignments" / AGENT).write_text(f"../archive/{MISSION}:t001\n")
    assert_refused(repo.pr_base())


def test_resolver_cli_refuses_bad_names_with_the_same_functions(repo):
    for args in (["pr-base", "--mission", f"../archive/{MISSION}"],
                 ["resolve-task", "--mission", MISSION, "--task", "../t001", "--task-slug", "x",
                  "--repo-root", str(repo.root)],
                 ["resolve-task", "--mission", "a/b", "--task", "t001", "--task-slug", "x",
                  "--repo-root", str(repo.root)]):

        p = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "lib_git_policy.py"), *args,
                            "--queue", str(repo.queue)], capture_output=True, text=True)
        assert p.returncode == 2 and p.stdout == "", (args, p.returncode, p.stdout, p.stderr)
        assert "[invalid_value]" in p.stderr, p.stderr


@pytest.mark.parametrize("value", ["", "..", ".", "/abs", "a/b", "a\\b", "x\ny", None, 3])
def test_check_mission_slug_rejects(value):
    with pytest.raises(lib_git_policy.GitPolicyError):
        lib_git_policy.check_mission_slug(value)


@pytest.mark.parametrize("value", ["", "t", "t1x", "../t1", "t001\n", None, 1])
def test_check_task_id_rejects(value):
    with pytest.raises(lib_git_policy.GitPolicyError):
        lib_git_policy.check_task_id(value)


def test_check_functions_accept_real_identifiers():
    assert lib_git_policy.check_mission_slug(MISSION) == MISSION
    assert lib_git_policy.check_task_id("t027") == "t027"


# ---------------------------------------------------------------------------
# 2. 利用者に見える文字列に、無関係な欄の中身を出さない
# ---------------------------------------------------------------------------

# Resolver (`parse_yaml`) は通すが PyYAML は解析できない (閉じない flow)。
BROKEN_FOR_PYYAML = f"owner: [{SECRET}\n"
INVALID_GIT_BLOCK = "git:\n  mode: bogus\n"


def test_lint_diagnostic_for_a_yaml_parse_problem_has_only_kind_and_position(repo):
    """P2-2 の再現: `owner: [SENTINEL` を含む mission で、診断に行全体が出た。"""
    repo.add_to_mission_yaml(BROKEN_FOR_PYYAML)
    rows = lint_plan.check_git_policy(MISSION, str(repo.queue))
    assert rows and rows[0][0] == "FAIL", rows
    text = " ".join(r[2] for r in rows)
    assert SECRET not in text, text
    assert "行目" in text and "列目" in text, text


def test_every_yaml_document_loader_diagnostic_is_secret_free(repo):
    """`_load_yaml_document` は skill-permissions・timeout profile・deliverable_required も読む。族ごと 1 か所で直す。"""
    repo.add_to_mission_yaml(BROKEN_FOR_PYYAML)
    _data, problem = lint_plan._load_yaml_document(str(repo.mission_yaml))
    assert problem and SECRET not in problem, problem
    required, problem2 = lint_plan._mission_requires_deliverable(MISSION, str(repo.queue))
    assert required is False and problem2 and SECRET not in problem2, problem2


def test_yaml_error_location_never_uses_the_exception_text():
    import yaml
    for doc in (f"a: [{SECRET}\n", f"a: !{SECRET} x\n", f"a: 'unterminated {SECRET}\n",
                f"a: b\n\tc: {SECRET}\n", f"a: &{SECRET}\n  - x\n  : y\n"):
        with pytest.raises(yaml.YAMLError) as ei:
            yaml.safe_load(doc)
        out = lint_plan._yaml_error_location(ei.value)
        assert SECRET not in out, (doc, out)


def test_other_fields_never_reach_pr_base_pull_or_lint_output(repo):
    repo.add_to_mission_yaml(f"owner: {SECRET}\n" + INVALID_GIT_BLOCK)
    rows = lint_plan.check_git_policy(MISSION, str(repo.queue))
    assert rows[0][0] == "FAIL" and SECRET not in rows[0][2], rows
    p = repo.pr_base("--mission", MISSION, "--task", "t001", agent=None)
    assert_refused(p)
    assert SECRET not in p.stderr
    pull = repo.pull()
    assert pull.returncode == 1 and SECRET not in pull.stderr and SECRET not in pull.stdout
    assert SECRET not in repo.card_path().read_text()
    q = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "lib_git_policy.py"), "pr-base",
                        "--queue", str(repo.queue), "--mission", MISSION], capture_output=True, text=True)
    assert q.returncode == 2 and SECRET not in q.stderr + q.stdout


def test_a_parse_yaml_failure_on_another_line_shows_the_line_number_only(repo):
    repo.add_to_mission_yaml(f"{SECRET}-no-colon-line\n")
    rows = lint_plan.check_git_policy(MISSION, str(repo.queue))
    assert rows[0][0] == "FAIL" and SECRET not in rows[0][2], rows
    p = repo.pr_base("--mission", MISSION, "--task", "t001", agent=None)
    assert_refused(p)
    assert SECRET not in p.stderr


# ---------------------------------------------------------------------------
# 3. 構造ガード: 整形は 1 か所
# ---------------------------------------------------------------------------

def test_policy_error_text_is_formatted_in_one_place():
    """`GitPolicyError` の `.detail` を並べて文字列にするのは `format_policy_error` だけ。

    新しい経路 (診断・stderr・card の理由) が自前で `{e.detail}` を組み立てたら、ここが赤になる。
    """
    scripts = REPO_ROOT / "scripts"
    offenders = []
    for name in ("plan.sh", "lint_plan.py", "git-helpers.sh", "kai-review.sh", "worktree_gc.py"):
        for n, line in enumerate((scripts / name).read_text().splitlines(), 1):
            if re.search(r"\be\.detail\b", line):
                offenders.append(f"{name}:{n}: {line.strip()}")
    lib = (scripts / "lib_git_policy.py").read_text()
    uses = [m.start() for m in re.finditer(r"\be\.detail\b", lib)]
    assert len(uses) == 1, "lib_git_policy.py の `e.detail` は format_policy_error の 1 か所だけ"
    assert lib.rfind("def format_policy_error", 0, uses[0]) != -1
    assert not offenders, offenders
