"""`plan.sh resolve-pr` (t006): PR 番号から、その PR を作った task を `<mission>/<task_id>` で引く。

## 背景 (方針転換)

review Worker の stacked PR 判定は、以前は PR 本文の `task:` 行を shlex で解析していたが、3 巡のレビュー
でも fail-open の抜けを出し続けた (引用符・短縮フラグ・行継続形式 — `agents/worker.md` の正規テンプレート
そのものが素通りしていた)。`pr_number` は `plan.sh done --pr` で card の frontmatter に既に書かれているので、
review task が走る時点 (実装 task が done 済み) ではキューに聞けば済む、という方針に変えた (t006 の card)。

## このファイルが固定すること

1. `pr_number` を持ち skills が `PR_PROPAGATION_SKILLS` (codex-review / review) と重ならない card が
   1 件だけなら `<mission>/<task_id>` を返す。`deliverable: pr` の宣言は要らない (t006 retry / review P3-1。下の「古い
   mission」節)。
2. `propagate_pr_number()` が下流の codex-review / review の task にも同じ `pr_number` を書く (#29) が、
   それらは `PR_PROPAGATION_SKILLS` そのものなので候補に入らない — 候補が複数枚あっても一意に決まる
   (課題 3 の規則)。
3. 見つからない・2 件以上 (本物の曖昧) はどちらも exit 1・stdout 空 (`main` に倒さない pr-base と同じ思想)。
4. 退避済み (archive) の mission は見ない (`pr-base` と同じ規則)。
5. **repo で絞る** (t006 retry / review P2): `target_dir` が一致しない card は候補にならない。既定は `pull` と
   同じ規則 (`--target-dir` flag > `TARGET_DIR` env var > None = crewvia 本体)。
6. **`deliverable_required` の無い古い mission でも解決できる** (t006 retry / review P3-1): `deliverable` を
   宣言しないまま `done --pr` した card も候補になる (`PR_PROPAGATION_SKILLS` による除外は `deliverable` の
   有無に依存しない)。
7. 読み取り専用 (queue に 1 バイトも書かない)。
8. 引数の検証 (整数でない・0 以下は usage エラーとして exit 1 — `pr-base` と同じ `PULL_USAGE_EXIT_SUBCOMMANDS`)。

欠陥注入による red proof は `tests/red_proof_resolve_pr.py` (別パッチの赤を実証)。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys

import pytest

from fixture_tree import REPO_ROOT
from test_git_policy_pull_and_pr_base_cutover import AGENT, MISSION, Repo  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path)
    # `--deliverable pr` の検査 (`check_permission`) は `config/skill-permissions.yaml` と
    # `hooks/lib_skill_perms.py` を読む。`copy_plan_tree` は scripts/ しか写さないので、
    # この本物の 2 つだけここで足す。
    shutil.copytree(REPO_ROOT / "config", r.root / "config")
    (r.root / "hooks").mkdir()
    shutil.copy2(REPO_ROOT / "hooks" / "lib_skill_perms.py", r.root / "hooks" / "lib_skill_perms.py")
    return r


def assert_refused(p, needle=None):
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", "決められないとき stdout は空 (main に倒さない pr-base と同じ思想)"
    if needle:
        assert needle in p.stderr, p.stderr


def _add_review_task(repo, blocked_by="t001", skills="review"):
    p = repo.plan("add", "review it", "--mission", MISSION, "--skills", skills,
                  "--blocked-by", blocked_by, agent="Director")
    assert p.returncode == 0, p.stderr
    # "Added: <mission>/<id> — ..." の行から id を取り出す。
    for line in p.stdout.splitlines():
        if line.startswith("Added:"):
            return line.split()[1].split("/")[1]
    raise AssertionError(p.stdout)


def _pull_and_done(repo, pr_number, mission=MISSION, task_id="t001"):
    """`deliverable: pr` の task を pull → done --pr。自分の試行の execution id を名乗る (E3)。"""
    assert repo.plan("update", task_id, "--deliverable", "pr", "--mission", mission,
                     agent="Director").returncode == 0
    p = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--task", task_id,
                 "--mission", mission)
    assert p.returncode == 0, p.stderr
    execution_id = json.loads(p.stdout)["execution_id"]
    done = repo.plan("done", task_id, "finished", "--mission", mission, "--pr", str(pr_number),
                     "--execution", execution_id)
    assert done.returncode == 0, done.stderr
    return done


def _pull_and_done_without_deliverable(repo, pr_number, mission=MISSION, task_id="t001"):
    """`deliverable` を**宣言しないまま** pull → done --pr (`deliverable_required` の無い古い mission の形。
    t006 retry / review P3-1)。fixture の seed は mission.yaml に `deliverable_required` を書かないので、
    これが素の挙動 (`_pull_and_done` は明示的に `--deliverable pr` を足している側)。
    """
    p = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--task", task_id,
                 "--mission", mission)
    assert p.returncode == 0, p.stderr
    assert "deliverable" not in repo.card_path(task_id).read_text()
    execution_id = json.loads(p.stdout)["execution_id"]
    done = repo.plan("done", task_id, "finished", "--mission", mission, "--pr", str(pr_number),
                     "--execution", execution_id)
    assert done.returncode == 0, done.stderr
    return done


# ---------------------------------------------------------------------------
# 1 件だけ: その task を返す
# ---------------------------------------------------------------------------

def test_resolves_the_task_that_declared_deliverable_pr(repo):
    _pull_and_done(repo, 555)
    out = repo.plan("resolve-pr", "555", agent=None)
    assert (out.returncode, out.stdout) == (0, f"{MISSION}/t001\n"), out.stderr


def test_the_named_form_does_not_need_an_assignment_or_agent_name(repo):
    """読み取り専用で所有者を見ない (AGENT_NAME も assignment も無い shell から呼べる)。"""
    _pull_and_done(repo, 777)
    out = repo.plan("resolve-pr", "777", agent=None)
    assert (out.returncode, out.stdout) == (0, f"{MISSION}/t001\n")


# ---------------------------------------------------------------------------
# 伝播した下流 (codex-review / review) は候補にならない
# ---------------------------------------------------------------------------

def test_a_downstream_review_task_that_got_the_same_pr_number_by_propagation_is_not_a_candidate(repo):
    review_id = _add_review_task(repo)
    _pull_and_done(repo, 321)
    # 前提: 伝播が実際に起きていて、review task も同じ pr_number を持つ (候補が 2 枚ある状態を作る)。
    meta = repo.card_path(review_id).read_text()
    assert "pr_number: 321" in meta, meta
    out = repo.plan("resolve-pr", "321", agent=None)
    assert (out.returncode, out.stdout) == (0, f"{MISSION}/t001\n"), out.stderr


def test_a_downstream_codex_review_task_is_also_not_a_candidate(repo):
    review_id = _add_review_task(repo, skills="codex-review")
    _pull_and_done(repo, 909)
    assert "pr_number: 909" in repo.card_path(review_id).read_text()
    out = repo.plan("resolve-pr", "909", agent=None)
    assert (out.returncode, out.stdout) == (0, f"{MISSION}/t001\n")


# ---------------------------------------------------------------------------
# 古い mission (deliverable_required が無い。deliverable を宣言しないまま done --pr)
# ---------------------------------------------------------------------------

def test_resolves_without_a_deliverable_declaration_on_a_mission_without_deliverable_required(repo):
    """t006 retry / review P3-1: `deliverable == 'pr'` で絞っていた旧実装は、`deliverable_required` の無い
    mission (このテストの fixture がまさにそれ) の card を 0 件に見せ、review を全部 Director に上げる
    退行を起こしていた。`PR_PROPAGATION_SKILLS` による除外は `deliverable` の有無を見ないので解決できる。
    """
    assert "deliverable_required" not in repo.mission_yaml.read_text()
    _pull_and_done_without_deliverable(repo, 606)
    out = repo.plan("resolve-pr", "606", agent=None)
    assert (out.returncode, out.stdout) == (0, f"{MISSION}/t001\n"), out.stderr


def test_a_downstream_review_task_without_deliverable_declared_upstream_is_still_not_a_candidate(repo):
    """旧実装 (`deliverable == 'pr'`) はこの場合もたまたま正しかった (review 自身も deliverable を宣言しない
    ので最初から候補にならない) が、除外の理由が `PR_PROPAGATION_SKILLS` に変わっても壊れないことを固定する。"""
    review_id = _add_review_task(repo)
    _pull_and_done_without_deliverable(repo, 616)
    assert "pr_number: 616" in repo.card_path(review_id).read_text()
    out = repo.plan("resolve-pr", "616", agent=None)
    assert (out.returncode, out.stdout) == (0, f"{MISSION}/t001\n"), out.stderr


# ---------------------------------------------------------------------------
# repo で絞る (target_dir。t006 retry / review P2)
# ---------------------------------------------------------------------------

def test_a_same_numbered_pr_in_a_different_target_dir_repo_is_not_a_candidate(repo, tmp_path):
    """PR 番号は repo ごとに独立に採番されるので、crewvia 本体の #42 と TARGET_DIR の別 repo の #42 が
    衝突しうる。絞らなければ別の repo の task を間違って返す (t006 retry / review P2)。"""
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    p = repo.plan("add", "cross-repo impl", "--mission", MISSION, "--skills", "code",
                  "--deliverable", "pr", "--target-dir", str(other_repo), agent="Director")
    assert p.returncode == 0, p.stderr
    t002 = [line.split()[1].split("/")[1] for line in p.stdout.splitlines() if line.startswith("Added:")][0]
    assert repo.plan("update", t002, "--pr-number", "42", "--mission", MISSION,
                     agent="Director").returncode == 0
    _pull_and_done(repo, 42)   # t001 (crewvia 本体。target_dir なし) も #42 を持つ
    # 既定 (TARGET_DIR 無し) は crewvia 本体の task だけを見る。
    out = repo.plan("resolve-pr", "42", agent=None)
    assert (out.returncode, out.stdout) == (0, f"{MISSION}/t001\n"), out.stderr
    # --target-dir で絞ると、別 repo の task が返る。
    out = repo.plan("resolve-pr", "42", "--target-dir", str(other_repo), agent=None)
    assert (out.returncode, out.stdout) == (0, f"{MISSION}/{t002}\n"), out.stderr


def test_target_dir_env_var_scopes_like_the_flag(repo, tmp_path):
    """`--target-dir` が無ければ `TARGET_DIR` env を見る (`pull` と同じ規則)。"""
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    p = repo.plan("add", "cross-repo impl", "--mission", MISSION, "--skills", "code",
                  "--deliverable", "pr", "--target-dir", str(other_repo), agent="Director")
    assert p.returncode == 0, p.stderr
    t002 = [line.split()[1].split("/")[1] for line in p.stdout.splitlines() if line.startswith("Added:")][0]
    p = repo.plan("pull", "--agent", AGENT, "--skills", "code", "--task", t002, "--mission", MISSION,
                  extra_env={"TARGET_DIR": str(other_repo)})
    assert p.returncode == 0, p.stderr
    execution_id = json.loads(p.stdout)["execution_id"]
    done = repo.plan("done", t002, "finished", "--mission", MISSION, "--pr", "55",
                     "--execution", execution_id, extra_env={"TARGET_DIR": str(other_repo)})
    assert done.returncode == 0, done.stderr
    out = repo.plan("resolve-pr", "55", agent=None)
    assert_refused(out, "一意に決められません")   # TARGET_DIR なし (crewvia 本体) では見えない
    out = repo.plan("resolve-pr", "55", agent=None, extra_env={"TARGET_DIR": str(other_repo)})
    assert (out.returncode, out.stdout) == (0, f"{MISSION}/{t002}\n"), out.stderr


# ---------------------------------------------------------------------------
# 決められない: 0 件・2 件以上
# ---------------------------------------------------------------------------

def test_an_unknown_pr_number_is_refused(repo):
    _pull_and_done(repo, 1)
    assert_refused(repo.plan("resolve-pr", "999", agent=None), "一意に決められません")


def test_two_implementation_tasks_sharing_the_same_pr_number_is_ambiguous(repo):
    """director が手で同じ番号を 2 枚の deliverable: pr card に入れてしまった事故の形。推測せず exit 1。"""
    p = repo.plan("add", "second impl", "--mission", MISSION, "--skills", "code",
                 "--deliverable", "pr", agent="Director")
    assert p.returncode == 0, p.stderr
    t002 = [line.split()[1].split("/")[1] for line in p.stdout.splitlines() if line.startswith("Added:")][0]
    for tid in ("t001", t002):
        assert repo.plan("update", tid, "--deliverable", "pr", "--pr-number", "42",
                         "--mission", MISSION, agent="Director").returncode == 0
    assert_refused(repo.plan("resolve-pr", "42", agent=None), "一意に決められません")


# ---------------------------------------------------------------------------
# 退避済み (archive) の mission は見ない
# ---------------------------------------------------------------------------

def test_archived_missions_are_not_searched(repo):
    _pull_and_done(repo, 66)
    shutil.move(str(repo.queue / "missions" / MISSION), str(repo.queue / "archive" / MISSION))
    assert_refused(repo.plan("resolve-pr", "66", agent=None))


# ---------------------------------------------------------------------------
# 引数の検証・読み取り専用
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,needle", [
    ("abc", "整数で指定してください"), ("1.5", "整数で指定してください"),
    ("0", "正の整数にしてください"), ("-5", "正の整数にしてください"),
])
def test_rejects_a_non_positive_or_non_integer_pr_number(repo, raw, needle):
    p = repo.plan("resolve-pr", raw, agent=None)
    assert p.returncode == 1
    assert p.stdout == ""
    assert needle in p.stderr, p.stderr


def test_resolve_pr_writes_nothing(repo):
    _pull_and_done(repo, 11)

    def snap():
        return {str(p.relative_to(repo.queue)): p.read_bytes()
                for p in sorted(repo.queue.rglob("*")) if p.is_file() and "audit" not in p.parts}
    before = snap()
    repo.plan("resolve-pr", "11", agent=None)
    repo.plan("resolve-pr", "999999", agent=None)
    assert snap() == before
