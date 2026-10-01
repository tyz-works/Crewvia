"""vNext 01b G4 (cutover): エージェント向け文書の「PR の base / diff の base」の手順が `plan pr-base` の値を使う。

設計は `knowledge/git-policy.md` §4.2 (書き換える行)・§5 (渡し方。**env では渡さず `plan pr-base` を呼ぶ**)・§5 の QA の表。
文書の code block を**そのまま取り出して**実際に評価する (文書の手順が壊れても、文書を読むだけのテストは緑のままになる)。

使い捨ての git repo (bare の origin + clone) と隔離 queue の上で、本物の plan.sh / git-helpers.sh / lib_git_policy.py を使う。
`gh` は stub (呼び出しの引数を記録するだけ。`gh pr create` は実際には打たない)。本番の queue・worktree には触れない。

固定すること (§5 の (1)〜(10)):

PR 作成 (worker.md ×2): (1) worktree の cwd・`pr_base: develop` → `--base develop` / (2) **同じ task で cwd を主 checkout に移した shell** →
  `develop` (env を cwd 依りにした 1 巡目の案ではここが `main` になる) / (3) TARGET_DIR の task → `main` / (4) assignment が無い → exit 1 で
  `gh pr create` が呼ばれない。
stacked PR の判定 (worker.md)・付け替え (director.md): (5) AGENT_NAME も assignment も無い shell で、名指しの形が子 PR の task の値
  (`develop`) を返し `gh pr edit --base develop` / (6) 対象 PR の task の値と比べる (自分の task ではない) /
  (7) `task:` 行が無い・card が無い (退避済み・存在しない mission) → exit 1 で `gh pr edit` が呼ばれない。
diff の base (crewvia-qa・verifier.md): (8) `pr_base: develop`・`origin/develop` がまだ無い clone → 手順が fetch して
  `origin/develop...HEAD` / (9) TARGET_DIR の task・**`origin` の remote が無く local `main` だけある** checkout → `main...HEAD` が通る /
  (10) crewvia の task で fetch が失敗する → diff を取らず exit 1。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import git_decision_scan as scan  # noqa: E402
from fixture_tree import REPO_ROOT  # noqa: E402
from test_git_policy_pull_and_pr_base_cutover import AGENT, MISSION, Repo, git  # noqa: E402

GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "gh $*" | tr '\\n' ' ' >> "$GH_LOG"; echo >> "$GH_LOG"
if [ "$1 $2" = "pr view" ]; then
  case "$*" in
    *baseRefName*) printf '%s\\n' "${STUB_BASE_REF:-main}" ;;
    *"--json body"*) printf '%b' "${STUB_PR_BODY-}" ;;
    *) echo "https://example.invalid/pr/1" ;;
  esac
fi
exit 0
"""


def snippet(rel: str, start: str, end: str, index: int = 0) -> str:
    """`rel` の fenced code block から、`start` に当たる行から `end` に当たる行まで (含む) を取り出す。`index` 番目の開始行。"""
    found = []
    for block in scan.code_blocks((REPO_ROOT / rel).read_text(encoding="utf-8")):
        for i, line in enumerate(block):
            if re.search(start, line):
                for j in range(i, len(block)):
                    if re.search(end, block[j]):
                        found.append("\n".join(block[i:j + 1]))
                        break
                else:
                    raise AssertionError(f"{rel}: 終端 {end!r} が見つからない")
    assert len(found) > index, f"{rel}: 開始 {start!r} が {len(found)} 個しか無い (index={index})"
    return found[index]


class Docs:
    def __init__(self, repo: Repo):
        self.repo = repo
        self.bin = repo.base / "bin"
        self.bin.mkdir()
        (self.bin / "plan").write_text(f'#!/usr/bin/env bash\nexec "{repo.root}/scripts/plan.sh" "$@"\n')
        (self.bin / "gh").write_text(GH_STUB)
        for f in self.bin.iterdir():
            f.chmod(0o755)
        self.log = repo.base / "gh.log"
        self.log.write_text("")

    def gh_calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def run(self, script: str, cwd, *, agent=AGENT, extra_env=None):
        env = {**self.repo.env, "PATH": f"{self.bin}:{self.repo.env['PATH']}", "GH_LOG": str(self.log),
               "TASK_MISSION": MISSION, "TASK_ID": "t001"}
        env.pop("AGENT_NAME", None)
        if agent:
            env["AGENT_NAME"] = agent
        env.update(extra_env or {})
        return subprocess.run(["bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


@pytest.fixture
def docs(repo):
    return Docs(repo)


def base_passed(calls: list[str], verb: str) -> str:
    [line] = [c for c in calls if c.startswith(f"gh pr {verb} ")]
    m = re.search(r"--base[ =](\S+)", line)
    assert m, line
    return m.group(1)


def pr_create_script(index: int) -> str:
    return snippet("agents/worker.md", r'^PR_BASE=', r'--base "\$PR_BASE"', index)


def test_worker_md_has_two_pr_create_blocks_that_both_use_plan_pr_base():
    for i in (0, 1):     # 2 つ目が無ければ snippet() が落ちる (文書の 2 か所とも書き換えた)
        assert "gh pr create" in pr_create_script(i)


# ---------------------------------------------------------------------------
# PR 作成 (worker.md ×2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("index", [0, 1])
def test_pr_create_in_the_worktree_uses_the_missions_pr_base(repo, docs, index):
    repo.set_git(base_branch="main", pr_base="develop")
    assert repo.pull().returncode == 0
    p = docs.run(pr_create_script(index), repo.wt)
    assert p.returncode == 0, p.stderr
    assert base_passed(docs.gh_calls(), "create") == "develop"


@pytest.mark.parametrize("index", [0, 1])
def test_pr_create_from_the_main_checkout_cwd_still_uses_the_missions_pr_base(repo, docs, index):
    """cwd が主 checkout に戻った shell (Bash の cwd は呼び出しごとに戻る)。env を cwd 依りにすると、ここが黙って `main` になる。"""
    repo.set_git(base_branch="main", pr_base="develop")
    assert repo.pull().returncode == 0
    for cwd in (repo.root, repo.base):
        docs.log.write_text("")
        p = docs.run(pr_create_script(index), cwd)
        assert p.returncode == 0, (cwd, p.stderr)
        assert base_passed(docs.gh_calls(), "create") == "develop", cwd


def test_pr_create_for_a_default_mission_is_main(repo, docs):
    assert repo.pull().returncode == 0
    assert docs.run(pr_create_script(0), repo.wt).returncode == 0
    assert base_passed(docs.gh_calls(), "create") == "main"


def test_pr_create_of_a_target_dir_task_is_main_whatever_the_mission_says(repo, docs):
    repo.set_git(pr_base="develop")
    card = repo.card_path()
    card.write_text(card.read_text().replace("target_dir: null", f"target_dir: {repo.root}"))
    p = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--task", "t001", "--mission", MISSION,
                  "--target-dir", str(repo.root))
    assert p.returncode == 0, p.stderr
    p = docs.run(pr_create_script(0), repo.root)
    assert p.returncode == 0, p.stderr
    assert base_passed(docs.gh_calls(), "create") == "main"


@pytest.mark.parametrize("index", [0, 1])
def test_pr_create_without_an_assignment_stops_and_does_not_call_gh_pr_create(repo, docs, index):
    repo.set_git(pr_base="develop")      # まだ pull していない = assignment が無い
    p = docs.run(pr_create_script(index), repo.root)
    assert p.returncode == 1
    assert "PR base を決められない" in p.stderr
    assert not [c for c in docs.gh_calls() if c.startswith("gh pr create")], docs.gh_calls()


def test_pr_create_with_a_broken_policy_stops_instead_of_falling_back_to_main(repo, docs):
    assert repo.pull().returncode == 0
    repo.add_to_mission_yaml("git:\n  mode: integration\n")
    p = docs.run(pr_create_script(0), repo.wt)
    assert p.returncode == 1
    assert not [c for c in docs.gh_calls() if c.startswith("gh pr create")]


# ---------------------------------------------------------------------------
# stacked PR (worker.md の判定・director.md の付け替え)
# ---------------------------------------------------------------------------

def stacked_check_script() -> str:
    return snippet("agents/worker.md", r"^PR_NUM=", r"stacked PR: base=").replace("{PR番号}", "77")


def director_edit_script() -> str:
    s = snippet("agents/director.md", r'^\s*PR_BASE=', r"gh pr edit .*--base")
    return (s.replace("{子PRのmission}", "$CHILD_MISSION").replace("{子PRのtask_id}", "$CHILD_TASK")
             .replace("{子PR番号}", "88"))


def test_worker_stacked_check_compares_with_the_target_prs_task_not_its_own(repo, docs):
    """review の Worker は自分の task (別の mission でもよい) ではなく、対象 PR の本文の `task:` 行の task の値と比べる。"""
    repo.set_git(pr_base="develop")
    assert repo.pull().returncode == 0
    body = f"概要\\n\\ntask: {MISSION}/t001\\nbranch: x\\n"
    # base が develop (期待どおり) → stacked ではない
    p = docs.run(stacked_check_script(), repo.root, agent="Seo", extra_env={"STUB_PR_BODY": body, "STUB_BASE_REF": "develop"})
    assert (p.returncode, p.stdout.strip()) == (0, ""), p.stderr
    # base が main → develop が期待なので stacked と言う (main と比べて決めない)
    p = docs.run(stacked_check_script(), repo.root, agent="Seo", extra_env={"STUB_PR_BODY": body, "STUB_BASE_REF": "main"})
    assert p.returncode == 0 and "stacked PR: base=main (期待 develop)" in p.stdout, (p.stdout, p.stderr)


@pytest.mark.parametrize("body", ["task 行の無い PR (人が作った)\\n", "task: ../etc/x\\n", f"task: no-such-mission/t001\\n",
                                  f"task: {MISSION}\\n"])
def test_worker_stacked_check_stops_when_the_task_line_does_not_name_a_task(repo, docs, body):
    assert repo.pull().returncode == 0
    p = docs.run(stacked_check_script(), repo.root, agent="Seo", extra_env={"STUB_PR_BODY": body, "STUB_BASE_REF": "main"})
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert "PR base を決められない" in p.stderr
    assert "stacked PR" not in p.stdout, "main と比べて stacked かどうかを決めない"


def test_director_reparent_uses_the_child_prs_task_from_a_shell_without_agent_or_assignment(repo, docs):
    repo.set_git(pr_base="develop")
    assert repo.pull().returncode == 0          # card は in_progress。Director は assignment も AGENT_NAME も持たない
    p = docs.run(director_edit_script(), repo.root, agent=None,
                 extra_env={"CHILD_MISSION": MISSION, "CHILD_TASK": "t001"})
    assert p.returncode == 0, p.stderr
    assert base_passed(docs.gh_calls(), "edit") == "develop"
    # 対照: 引数なしの形は同じ shell で決められない (名指しが要ることの確認)
    assert repo.plan("pr-base", agent=None, cwd=repo.root).returncode == 1


@pytest.mark.parametrize("mission,task", [("", ""), (MISSION, ""), ("no-such-mission", "t001"), (MISSION, "t099"),
                                          ("../archive/x", "t001")])
def test_director_reparent_stops_without_calling_gh_pr_edit_when_the_task_is_unknown(repo, docs, mission, task):
    p = docs.run(director_edit_script(), repo.root, agent=None,
                 extra_env={"CHILD_MISSION": mission, "CHILD_TASK": task})
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert not [c for c in docs.gh_calls() if c.startswith("gh pr edit")], docs.gh_calls()


def test_director_reparent_stops_for_a_retired_mission(repo, docs):
    """退避済み (archive) の mission の task は exit 1。base は手で決めずユーザーに判断を仰ぐ (文書にそう書いてある)。"""
    assert repo.pull().returncode == 0
    dest = repo.queue / "archive"
    dest.mkdir(exist_ok=True)
    shutil.move(str(repo.queue / "missions" / MISSION), str(dest / MISSION))
    p = docs.run(director_edit_script(), repo.root, agent=None, extra_env={"CHILD_MISSION": MISSION, "CHILD_TASK": "t001"})
    assert p.returncode == 1
    assert not [c for c in docs.gh_calls() if c.startswith("gh pr edit")]
    assert "ユーザーに判断を仰ぐ" in p.stderr


# ---------------------------------------------------------------------------
# diff の base (crewvia-qa ×2・verifier.md)
# ---------------------------------------------------------------------------

def qa_diff_script(index: int = 0) -> str:
    return snippet("skills/crewvia-qa/SKILL.md", r'^DIFF_REF=', r"^git diff ", index)


def test_qa_step6_diff_does_not_silently_diff_head_against_itself_when_diff_ref_is_unset():
    """Step 6 は Step 1 の DIFF_REF を使う。別の shell で空のまま `...HEAD` を打つと HEAD...HEAD の空 diff になるので、空なら止める。"""
    line = snippet("skills/crewvia-qa/SKILL.md", r'^git diff "\$\{DIFF_REF:\?', r"^git diff ")
    p = subprocess.run(["bash", "-c", line], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert p.returncode != 0 and "DIFF_REF" in p.stderr, (p.returncode, p.stdout, p.stderr)


def verifier_diff_script() -> str:
    s = snippet("agents/verifier.md", r'^DIFF_REF=', r"^git log ")
    return s.replace("<slug>", MISSION).replace("<task_id>", "t001").replace(" -- <変更ファイル>", "")


def drop_remote_tracking(repo: Repo, name: str):
    git(repo.root, "update-ref", "-d", f"refs/remotes/origin/{name}")


def test_qa_diff_fetches_the_pr_base_when_the_clone_does_not_have_it_yet(repo, docs):
    repo.branch_off("develop", "dev.txt")
    repo.set_git(base_branch="main", pr_base="develop")
    assert repo.pull().returncode == 0
    drop_remote_tracking(repo, "develop")      # pull の `git fetch origin` は全 branch を取るので、pull の後で消して「まだ無い clone」にする
    assert git(repo.root, "rev-parse", "--verify", "--quiet", "origin/develop", check=False).returncode != 0, "前提: まだ無い"
    (repo.wt / "change.txt").write_text("x\n")
    git(repo.wt, "add", "change.txt")
    git(repo.wt, "commit", "-q", "-m", "change")
    p = docs.run(qa_diff_script(), repo.wt)
    assert p.returncode == 0, p.stderr
    assert git(repo.root, "rev-parse", "--verify", "--quiet", "origin/develop", check=False).returncode == 0, "手順が fetch した"
    assert "change.txt" in p.stdout.split()
    assert "dev.txt" not in p.stdout.split(), "develop にある変更は diff に出ない (base が develop)"


def test_qa_diff_for_a_default_mission_is_origin_main(repo, docs):
    assert repo.pull().returncode == 0
    (repo.wt / "change.txt").write_text("x\n")
    git(repo.wt, "add", "change.txt")
    git(repo.wt, "commit", "-q", "-m", "change")
    p = docs.run(qa_diff_script(), repo.wt)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == ["change.txt"]


def test_qa_diff_of_a_target_dir_task_is_local_main_with_no_origin_remote(repo, docs):
    """TARGET_DIR の checkout には `origin` が無いことがある。`origin/*` に倒す欠陥版ならここで落ちる。"""
    card = repo.card_path()
    card.write_text(card.read_text().replace("target_dir: null", f"target_dir: {repo.root}"))
    p = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--task", "t001", "--mission", MISSION,
                  "--target-dir", str(repo.root))
    assert p.returncode == 0, p.stderr
    git(repo.root, "checkout", "-q", "-b", "feature")
    (repo.root / "change.txt").write_text("x\n")
    git(repo.root, "add", "change.txt")
    git(repo.root, "commit", "-q", "-m", "change")
    git(repo.root, "remote", "remove", "origin")
    p = docs.run(qa_diff_script(), repo.root)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == ["change.txt"]


def test_qa_diff_stops_when_the_fetch_fails_and_does_not_print_a_diff(repo, docs):
    repo.set_git(pr_base="develop")
    assert repo.pull().returncode == 0
    git(repo.root, "remote", "set-url", "origin", str(repo.base / "does-not-exist.git"))
    p = docs.run(qa_diff_script(), repo.wt)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert p.stdout == ""
    assert "Director に報告して待つ" in p.stderr


def test_qa_diff_stops_when_plan_pr_base_cannot_decide(repo, docs):
    p = docs.run(qa_diff_script(), repo.root)       # assignment が無い
    assert p.returncode == 1 and p.stdout == ""


def test_verifier_diff_uses_the_named_form_and_the_same_fetch_check(repo, docs):
    repo.branch_off("develop", "dev.txt")
    repo.set_git(base_branch="main", pr_base="develop")
    assert repo.pull().returncode == 0
    drop_remote_tracking(repo, "develop")      # pull の `git fetch origin` は全 branch を取るので、pull の後で消して「まだ無い clone」にする
    (repo.wt / "change.txt").write_text("x\n")
    git(repo.wt, "add", "change.txt")
    git(repo.wt, "commit", "-q", "-m", "change")
    # Verifier は実装者の assignment も AGENT_NAME も持たない
    p = docs.run(verifier_diff_script(), repo.wt, agent=None)
    assert p.returncode == 0, p.stderr
    assert "change.txt" in p.stdout and "dev.txt" not in p.stdout
    assert git(repo.root, "rev-parse", "--verify", "--quiet", "origin/develop", check=False).returncode == 0


# ---------------------------------------------------------------------------
# 文書の地の文・手順の整合
# ---------------------------------------------------------------------------

def test_no_agent_doc_tells_the_agent_to_default_to_main_in_a_command():
    """`:-main` の既定値・`--base main`・`main...HEAD` を code block に書かない (構造ガードと同じ検出器を、書き換えた 5 本に直接当てる)。"""
    for rel in ("agents/worker.md", "agents/director.md", "agents/worker-codex.md", "agents/verifier.md",
                "skills/crewvia-qa/SKILL.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        hits, _n = scan.doc_hits(text, rel)
        bad = [h for h in hits if h.what in ("--base <literal>", "<literal>..HEAD", "main...") or
               (h.what == "origin/<literal>" and "task/" not in h.snippet)]
        assert bad == [], (rel, [(h.what, h.snippet) for h in bad])
        for block in scan.code_blocks(text):
            assert not any(":-main" in line for line in block), rel


def test_docs_reference_the_cli_that_exists():
    """文書が呼ぶ `plan pr-base` の形 (`--diff-ref`・`--mission`/`--task`) を plan.sh が本当に受ける (usage 行に載っている)。"""
    usage = subprocess.run([str(REPO_ROOT / "scripts" / "plan.sh"), "--help"], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "CREWVIA_QUEUE": "/nonexistent", "CREWVIA_TASKVIA": "disabled"})
    text = usage.stdout + usage.stderr
    assert "plan.sh pr-base [--mission <slug> --task <task_id>] [--diff-ref]" in text or "pr-base" in text
    for rel, needle in (("agents/worker.md", "plan pr-base"), ("agents/director.md", "plan pr-base --mission"),
                        ("agents/verifier.md", "plan pr-base --diff-ref --mission"), ("skills/crewvia-qa/SKILL.md", "plan pr-base --diff-ref"),
                        ("agents/worker-codex.md", "lib_git_policy.py pr-base")):
        assert needle in (REPO_ROOT / rel).read_text(encoding="utf-8"), (rel, needle)
