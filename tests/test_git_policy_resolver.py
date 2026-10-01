#!/usr/bin/env python3
"""tests/test_git_policy_resolver.py — Git Policy Resolver (`scripts/lib_git_policy.py`。vNext 01b G2 / t008)。

設計は `knowledge/git-policy.md` §2 (schema) / §3 (API) / §11.1 (自前の規則を本物と突き合わせる)。

何を固定するか:

1. 原案 §10.4 の単体テスト 9 項目 (policy 未指定 / origin/main の有無 / custom base / 現行と一致 /
   invalid mode / path traversal / invalid branch component / target_dir / worktree 失敗)。
   target_dir と worktree 失敗は G1 / G3 の範囲なので、Resolver の側は「target_dir に worktree path を返さない」
   「失敗を黙って別の path に倒さず拒否する」の形で固定する。
2. 互換性: **G3 前の git-helpers.sh (tests/fixtures/git-helpers-pre-g3.sh に凍結) と G3 後の git-helpers.sh (本物)** に、
   plan.sh の `_slugify` (本文から取り出して実行) が作った同じ入力を与えて、branch / path が**バイト単位で一致**する表
   (生成。50 通り以上。それぞれ使い捨ての clone)。G3 後の helper は Resolver から値を得るので、これが
   「policy 未指定の mission で修正前後の pull が同じ branch・同じ path で worktree を作る」の本体。
3. §2.2 の拒否の各行。
4. §2.3 の branch 名の規則が `git check-ref-format --branch` の**部分集合**であること (本物の git に通す)。
5. `git:` 付きの mission.yaml を `Txn.write_mission` で書き戻してもバイトが変わらない (原案 §14-17)。
6. 呼び出し元が cutover の集合と**ちょうど**一致する (G2 の「呼び出し元ゼロ」は G3 で外した。01a S2 の
   `test_state_store_has_no_callers_yet` と同じ作法で、増えたら赤)。

テストは `CREWVIA_QUEUE` / `CREWVIA_REPO_ROOT` を触らない。queue は tmp に作り、git は使い捨ての bare origin + clone。
本番の queue・`.claude/worktrees` には触れない。
"""

from __future__ import annotations

import inspect
import itertools
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_git_policy as gp  # noqa: E402
import lib_state_store as store  # noqa: E402
from lib_task_cards import parse_yaml  # noqa: E402

GIT_ENV_BASE = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git_env(home: pathlib.Path) -> dict:
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "LC_ALL": "C.UTF-8"}
    env.update(GIT_ENV_BASE)
    return env


def _mission_text(git_lines: str = "") -> str:
    return "title: Demo\nslug: 20261001-demo\nstatus: active\n" + git_lines


def _git_block(**fields) -> str:
    return "git:\n" + "".join(f"  {k}: {v}\n" for k, v in fields.items())


def _policy(**fields) -> gp.GitPolicy:
    return gp.policy_from_text(_mission_text(_git_block(**fields)))


# ---------------------------------------------------------------------------
# §10.4-1 policy 未指定 → direct / main (GIT-01)
# ---------------------------------------------------------------------------

def test_policy_defaults_when_git_key_is_absent():
    p = gp.policy_from_text(_mission_text())
    assert (p.mode, p.base_branch, p.pr_base, p.task_branch_pattern, p.worktree_root, p.source) == (
        "direct", "main", "main", "task/{mission_slug}/{task_id}-{task_slug}", ".claude/worktrees", "default")
    assert p == gp.default_policy()


def test_policy_defaults_for_each_missing_field_but_source_is_mission():
    p = _policy(base_branch="develop")
    assert p.source == "mission"
    assert p.base_branch == "develop"
    assert (p.mode, p.pr_base, p.task_branch_pattern, p.worktree_root) == (
        "direct", "main", gp.DEFAULT_TASK_BRANCH_PATTERN, ".claude/worktrees")


def test_a_mission_written_today_without_git_is_default(tmp_path):
    q = tmp_path / "queue"
    (q / "missions" / "20261001-demo").mkdir(parents=True)
    (q / "missions" / "20261001-demo" / "mission.yaml").write_text(_mission_text())
    assert gp.load_git_policy("20261001-demo", queue_dir=str(q)) == gp.default_policy()


# ---------------------------------------------------------------------------
# §10.4-2/3 base の解決 (origin/main の有無・custom base / pr base)
# ---------------------------------------------------------------------------

def test_task_base_prefers_remote_tracking_and_falls_back_to_local():
    p = gp.default_policy()
    assert gp.task_base(p, remote_tracking_exists=True) == gp.TaskBase(ref="origin/main", fallback=False)
    assert gp.task_base(p, remote_tracking_exists=False) == gp.TaskBase(ref="main", fallback=True)


def test_custom_base_and_pr_base_are_resolved():
    p = _policy(base_branch="develop", pr_base='"release/2026.10"')
    assert gp.task_base(p, remote_tracking_exists=True).ref == "origin/develop"
    assert gp.task_base(p, remote_tracking_exists=False) == gp.TaskBase(ref="develop", fallback=True)
    assert gp.pr_base(p) == "release/2026.10"
    # base_branch と pr_base は独立 (片方だけ変えてももう片方は既定値のまま)
    assert gp.pr_base(_policy(base_branch="develop")) == "main"
    assert gp.task_base(_policy(pr_base="develop"), remote_tracking_exists=True).ref == "origin/main"


def test_resolve_task_returns_both_base_candidates_without_choosing():
    p = _policy(base_branch="develop")
    out = gp.resolve_task(p, repo_root="/tmp/r", mission_slug="20261001-m", task_id="t001", task_slug="x")
    assert out["base_remote"] == "origin/develop" and out["base_local"] == "develop"
    assert out["branch"] == "task/20261001-m/t001-x"
    assert out["worktree_path"] == "/tmp/r/.claude/worktrees/20261001-m/t001-x"
    assert out["pr_base"] == "main"


def test_custom_task_branch_pattern():
    p = _policy(task_branch_pattern='"work/{task_id}/{mission_slug}"')
    assert gp.task_branch(p, mission_slug="20261001-m", task_id="t007", task_slug="x") == "work/t007/20261001-m"


# ---------------------------------------------------------------------------
# §10.4-4 既定の branch 名と worktree path が現行と一致 (本物の git-helpers.sh / _slugify と突き合わせる)
# ---------------------------------------------------------------------------

def _load_slugify():
    """pull が task_slug を作る式。01c E2 で plan.sh の `_slugify` のコピーを消し、式の置き場は
    `lib_execution.slugify_title` の 1 か所になった (reserve が card に固定する)。plan.sh が pull の中で呼ぶのも同じ関数。"""
    src = (SCRIPTS / "plan.sh").read_text()
    assert "def _slugify(" not in src, "plan.sh に task_slug の式のコピーが戻っている"
    sys.path.insert(0, str(SCRIPTS))
    import lib_execution
    return lib_execution.slugify_title


SLUGIFY = _load_slugify()

TITLES = [
    "G2: Git Policy Resolver (呼び出し元ゼロ)", "全角ＡＢＣ１２３ title", "記号!@#$%^&*()_+ だけ", "",
    "   ", "---", "a", "A B", "Hello, World!", "tabs\tand\nnewlines", "日本語だけのタイトル",
    "x" * 39, "x" * 40, "x" * 41, "x" * 100, ("word-" * 20), "ends with dash-" + "y" * 30,
    "UPPER lower MiXeD", "dots.in.title", "under_score_title", "slash/in/title", "back\\slash",
    "quote'and\"double", "emoji 😀 title", "123 numbers 456", "-leading dash", "trailing dash-",
    "git check-ref-format --branch", "..", ".lock", "refs/heads/x", "HEAD", "origin/main", "@{-1}",
    "a  b   c", "ＡＢＣ", "ß straße", "tNNN t001",
]

MISSION_SLUGS = [
    "20261001-vnext-01b-git-policy", "20260930-ops-gaps-c", "20260905-slug.badinit-20260905T063231Z",
    "obs-t024-a", "t037-probe-orphan", "test-qa-1788612749", "20260830-20260830-crewvia-3bug-fix",
    "a", "A", "0", "m.1", "m_1", "m-1", "a" * 60, "UPPER", "x.y-z_w",
    # git-helpers.sh が作れない slug (Resolver も拒否する側) と、git は通すが §2.3 が狭める slug (`_x`)
    "a..b", "x.lock", "x.", "has space", "a~b", "a:b", "_x",
]


def _compat_cases():
    cases = []
    for i, title in enumerate(TITLES):
        cases.append(("20261001-compat", f"t{i + 1:03d}", title))
    for j, ms in enumerate(MISSION_SLUGS):
        cases.append((ms, f"t{j + 101:03d}", "Fixed Title For Mission Slugs"))
    return cases


def _make_clone(base, name):
    """使い捨ての bare origin + clone。"""
    home = base / f"home-{name}"
    home.mkdir()
    env = _git_env(home)
    origin = base / f"origin-{name}.git"
    work = base / f"clone-{name}"

    def run(*args, cwd):
        subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True)

    run("git", "init", "--bare", "-b", "main", str(origin), cwd=base)
    run("git", "clone", str(origin), str(work), cwd=base)
    (work / "README").write_text("x\n")
    run("git", "add", "README", cwd=work)
    run("git", "commit", "-m", "init", cwd=work)
    run("git", "branch", "-M", "main", cwd=work)
    run("git", "push", "-u", "origin", "main", cwd=work)
    return {"root": work, "env": env}


@pytest.fixture(scope="module")
def clone(tmp_path_factory):
    """G3 後の (本物の) git-helpers.sh を走らせる使い捨ての clone。queue は tmp に作る (mission.yaml は都度置く)。"""
    base = tmp_path_factory.mktemp("gitpolicy-compat-new")
    c = _make_clone(base, "new")
    queue = base / "queue"
    (queue / "missions").mkdir(parents=True)
    c["queue"] = queue
    c["env"] = {**c["env"], "CREWVIA_QUEUE": str(queue)}
    return c


@pytest.fixture(scope="module")
def pre_g3_clone(tmp_path_factory):
    """G3 前の git-helpers.sh (凍結した複製) を走らせる使い捨ての clone。"""
    return _make_clone(tmp_path_factory.mktemp("gitpolicy-compat-old"), "old")


PRE_G3_HELPER = REPO / "tests" / "fixtures" / "git-helpers-pre-g3.sh"


def _run_helper(helper, clone, mission, tid, task_slug):
    """`crewvia_create_worktree`。(rc, 作られた path, その worktree の branch, base を作ったときの警告を含む stderr)。"""
    cmd = f'source {helper} && crewvia_create_worktree "$1" "$2" "$3"'
    r = subprocess.run(["bash", "-c", cmd, "x", mission, tid, task_slug], cwd=clone["root"],
                       env=clone["env"], capture_output=True, text=True)
    if r.returncode != 0:
        return r.returncode, None, None, r.stderr
    path = r.stdout.strip().splitlines()[-1]
    b = subprocess.run(["git", "-C", path, "branch", "--show-current"], env=clone["env"],
                       capture_output=True, text=True, check=True).stdout.strip()
    return 0, path, b, r.stderr


def _real_helper(clone, mission, tid, task_slug):
    """G3 後の本物の `crewvia_create_worktree`。mission.yaml (`git:` なし) を queue に置いてから呼ぶ。"""
    d = clone["queue"] / "missions" / mission
    if not d.exists():
        d.mkdir(parents=True)
        (d / "mission.yaml").write_text(f"mission: {mission}\n")
    return _run_helper(SCRIPTS / "git-helpers.sh", clone, mission, tid, task_slug)[:3]


def test_compat_table_has_at_least_fifty_cases():
    assert len(_compat_cases()) >= 50


@pytest.mark.parametrize("mission,tid,title", _compat_cases(),
                         ids=[f"{m[:18]}-{t}-{i}" for i, (m, t, _) in enumerate(_compat_cases())])
def test_default_branch_and_path_equal_the_pre_g3_git_helpers(clone, pre_g3_clone, mission, tid, title):
    """policy 未指定の mission で、G3 前の helper と G3 後の helper が**同じ branch・同じ path** で worktree を作る。

    G3 前の helper (凍結) が作れる入力は G3 後も作れて、バイト単位で同じ。G3 後が拒否してよいのは設計 §2.3 で
    git より狭くした入力だけ。逆に G3 後が作れる入力は G3 前も作れる (新しく通す入力を増やさない)。
    """
    task_slug = SLUGIFY(title, tid)
    old_rc, old_path, old_branch, _ = _run_helper(PRE_G3_HELPER, pre_g3_clone, mission, tid, task_slug)
    new_rc, new_path, new_branch = _real_helper(clone, mission, tid, task_slug)
    if new_rc == 0:
        assert old_rc == 0, f"G3 後だけが作れる (入力を広げた): {mission} {tid} {task_slug!r}"
        # path は clone のルートが違うので、ルートを除いた相対部分が同じ (ルートより後ろは式の出力そのもの)
        assert new_path[len(str(clone["root"])):] == old_path[len(str(pre_g3_clone["root"])):]
        assert new_branch == old_branch
    else:
        assert old_rc != 0 or mission in NARROWER_THAN_GIT, \
            f"G3 前は作れたのに G3 後が拒否した: {mission} {tid} {task_slug!r}"


#: git は通すが §2.3 の許可集合 (狭い側) が拒否する mission slug (`_x`: 英数字で始まらない、`x.`: 成分が . で終わる)。理由は設計 §2.3 の表。
NARROWER_THAN_GIT = {"_x", "x."}


def test_compat_every_generated_slug_is_a_valid_component():
    for title in TITLES:
        s = SLUGIFY(title, "t001")
        assert gp.branch_name_problem(f"x/{s}") is None, (title, s)


def test_default_pattern_literals_match_the_frozen_pre_g3_helper():
    """G3 前の式 (凍結した複製のリテラル) が Resolver の既定値と同じ。G3 後の helper にはこの式が残っていない。"""
    src = PRE_G3_HELPER.read_text()
    assert 'local branch="task/${mission_slug}/${task_id}-${task_slug}"' in src
    assert '${repo_root}/.claude/worktrees/${mission_slug}/${task_id}-${task_slug}' in src
    live = (SCRIPTS / "git-helpers.sh").read_text()
    assert 'local branch="task/' not in live and '/.claude/worktrees/${mission_slug}' not in live
    assert gp.DEFAULT_TASK_BRANCH_PATTERN == "task/{mission_slug}/{task_id}-{task_slug}"
    assert gp.DEFAULT_WORKTREE_ROOT == ".claude/worktrees"


# ---------------------------------------------------------------------------
# §10.4-5 invalid mode (GIT-02)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["integration", "Direct", "stacked", "direct ", "none"])
def test_unsupported_mode_is_refused_not_defaulted(mode):
    with pytest.raises(gp.GitPolicyError) as ei:
        _policy(mode=f'"{mode}"')
    assert ei.value.code in ("unsupported_mode", "invalid_value")
    if mode == "integration":
        assert ei.value.code == "unsupported_mode"


# ---------------------------------------------------------------------------
# §10.4-6 path traversal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("root", ["/abs/worktrees", "..", "../escape", "a/../b", "~/wt", "~", "C:\\x", "C:/x"])
def test_worktree_root_escape_is_refused(root):
    with pytest.raises(gp.GitPolicyError) as ei:
        _policy(worktree_root=f'"{root}"')
    assert ei.value.code == "invalid_value"


@pytest.mark.parametrize("root", ["worktrees", ".claude/worktrees/", "./.claude/worktrees", "other/place"])
def test_worktree_root_other_than_default_is_unsupported(root):
    with pytest.raises(gp.GitPolicyError) as ei:
        _policy(worktree_root=f'"{root}"')
    assert ei.value.code == "unsupported_value"


@pytest.mark.parametrize("mission,slug", [
    ("..", "x"), ("a/b", "x"), ("../escape", "x"), ("m", "../x"), ("m", "a/b"), ("m", ".."), ("m", ""),
    ("", "x"), ("/abs", "x"), ("m", "/abs"),
])
def test_path_components_cannot_traverse(mission, slug):
    p = gp.default_policy()
    with pytest.raises(gp.GitPolicyError):
        gp.task_worktree_path(p, repo_root="/tmp/r", mission_slug=mission, task_id="t001", task_slug=slug)
    with pytest.raises(gp.GitPolicyError):
        gp.task_branch(p, mission_slug=mission, task_id="t001", task_slug=slug)


def test_worktree_path_must_stay_inside_repo_root_even_through_symlink(tmp_path):
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    (root / ".claude").mkdir(parents=True)
    outside.mkdir()
    (root / ".claude" / "worktrees").symlink_to(outside)
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.task_worktree_path(gp.default_policy(), repo_root=str(root), mission_slug="20261001-m",
                              task_id="t001", task_slug="x")
    assert ei.value.field == "worktree_path"


def test_repo_root_must_be_absolute_and_symlinked_root_is_fine(tmp_path):
    p = gp.default_policy()
    with pytest.raises(gp.GitPolicyError):
        gp.task_worktree_path(p, repo_root="relative/root", mission_slug="m", task_id="t001", task_slug="x")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    # root が symlink でも、返す path は渡された root のまま (git-helpers.sh も git が返した root をそのまま使う)
    out = gp.task_worktree_path(p, repo_root=str(link) + "/", mission_slug="20261001-m", task_id="t001",
                                task_slug="x")
    assert out == f"{link}/.claude/worktrees/20261001-m/t001-x"


# ---------------------------------------------------------------------------
# §10.4-7 invalid branch component
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["a b", "a\tb", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "a@{b",
                                 "", " main", "main ", "-main", "_main", ".main", "main.", "main.lock"])
def test_invalid_branch_names_are_refused_for_base_and_pr_base(bad):
    for f in ("base_branch", "pr_base"):
        with pytest.raises(gp.GitPolicyError):
            _policy(**{f: json.dumps(bad)})


@pytest.mark.parametrize("component", ["a b", "a/b", "-a", ".a", "a.", "a.lock", "a..b", "x" * 201, "a\x00b",
                                       "a\nb", "é", "日本語"])
def test_invalid_substituted_components_are_refused(component):
    p = gp.default_policy()
    for kw in ({"mission_slug": component, "task_slug": "x"}, {"mission_slug": "m", "task_slug": component}):
        with pytest.raises(gp.GitPolicyError):
            gp.task_branch(p, task_id="t001", **kw)


@pytest.mark.parametrize("tid", ["", "001", "T001", "t", "t1x", "t001/", "../t001", "t 001"])
def test_task_id_must_be_tnnn(tid):
    with pytest.raises(gp.GitPolicyError):
        gp.task_branch(gp.default_policy(), mission_slug="m", task_id=tid, task_slug="x")


@pytest.mark.parametrize("pattern,why", [
    ("task/{task_id}", "mission_slug が無い"),
    ("task/{mission_slug}", "task_id が無い"),
    ("task/{mission_slug}/{task_id}/{agent}", "未知の置換子 agent"),
    ("task/{mission_slug}/{task_id}-{attempt}", "未知の置換子 attempt"),
    ("task/{mission_slug}/{task_id}-{}", "空の置換子"),
    ("task/{mission_slug}/{task_id}{", "対応しない {"),
    ("task/{mission_slug}/{task_id}}", "対応しない }"),
    ("task/{mission_slug}/{task_id} x", "空白"),
    ("task/{mission_slug}/{task_id}~", "~"),
    ("refs/{mission_slug}/{task_id}", "最初の成分が refs"),
    ("origin/{mission_slug}/{task_id}", "最初の成分が origin"),
    ("/{mission_slug}/{task_id}", "先頭の /"),
    ("{mission_slug}//{task_id}", "連続した /"),
    ("{mission_slug}/{task_id}/", "末尾の /"),
    ("-{mission_slug}/{task_id}", "先頭が -"),
    ("{mission_slug}/{task_id}.", "末尾が ."),
    ("{mission_slug}/{task_id}.lock", ".lock で終わる"),
])
def test_invalid_task_branch_pattern_is_refused(pattern, why):
    with pytest.raises(gp.GitPolicyError) as ei:
        _policy(task_branch_pattern=json.dumps(pattern))
    assert ei.value.code == "invalid_value", why
    assert ei.value.field == "task_branch_pattern"


# ---------------------------------------------------------------------------
# §10.4-8 target_dir / §10.4-9 worktree 失敗 — Resolver の側の形
# ---------------------------------------------------------------------------

def test_resolver_has_no_notion_of_target_dir_so_it_never_returns_a_worktree_path_for_it():
    """target_dir の task を worktree なしで扱う判断は plan.sh (G1 / G3)。Resolver は target_dir を受け取らず、
    worktree path は常に `<repo_root>/<worktree_root>/...` の下になる。"""
    for fn in (gp.task_branch, gp.task_worktree_path, gp.task_base, gp.pr_base, gp.resolve_task):
        assert "target_dir" not in inspect.signature(fn).parameters, fn.__name__
    p = gp.policy_from_text(_mission_text() + "target_dir: /somewhere/else\n")
    out = gp.task_worktree_path(p, repo_root="/tmp/r", mission_slug="20261001-m", task_id="t001", task_slug="x")
    assert out.startswith("/tmp/r/.claude/worktrees/") and "/somewhere/else" not in out
    # `git:` の下に target_dir を書いても通らない (policy の欄ではない)
    with pytest.raises(gp.GitPolicyError) as ei:
        _policy(target_dir="/somewhere/else")
    assert ei.value.code == "unknown_key"


def test_resolver_refuses_instead_of_falling_back_to_another_checkout():
    """worktree の path を決められないときに、主 checkout (repo_root そのもの) や cwd を返さない。"""
    p = gp.default_policy()
    for kw in ({"repo_root": "relative"}, {"repo_root": ""}, {"repo_root": "/tmp/r", "task_slug": ""},
               {"repo_root": "/tmp/r", "mission_slug": ""}):
        args = {"repo_root": "/tmp/r", "mission_slug": "m", "task_id": "t001", "task_slug": "x", **kw}
        with pytest.raises(gp.GitPolicyError):
            gp.task_worktree_path(p, **args)


# ---------------------------------------------------------------------------
# §2.2 検証の規則 (fail closed)
# ---------------------------------------------------------------------------

REJECTIONS = [
    ("git:\n", "malformed"),                                    # git: だけ (parse_yaml では None)
    ("git: null\n", "malformed"),
    ("git: ~\n", "malformed"),
    ("git: direct\n", "malformed"),                             # スカラー
    ("git: {mode: integration}\n", "malformed"),                # flow mapping も読めない
    ("git: [a, b]\n", "malformed"),                             # リスト
    ("git:\n  - mode\n", "malformed"),                          # block list
    ("git:\n# only a comment\n", "malformed"),
    ("git:\n  mode: direct\n    pr_base: develop\n", "malformed"),   # 4 字下げは parse_yaml が黙って読み飛ばす
    ("git:\n  mode: direct\n\n  pr_base: develop\n", "malformed"),   # 空行の後ろ
    ("git:\n  mode: direct\n  # c\n  pr_base: develop\n", "malformed"),  # 字下げたコメントの後ろ
    ("git:\n  mode: direct\n\tpr_base: develop\n", "malformed"),   # タブ字下げ
    ("git:\n  mode: direct\n  mode: direct\n", "malformed"),   # 重複キー
    ("git:\n  mode: direct\ngit:\n  pr_base: develop\n", "malformed"),   # git: が 2 つ
    ("git:\ngit:\n  pr_base: develop\n", "malformed"),   # 先の git: が空 (字下げ行の数は合ってしまう。git: の数で拾う)
    ("git:\n  mode integration\n", "malformed"),               # コロンなし (読み飛ばされる)
    ("\"git\":\n  mode: integration\n", "malformed"),          # 0 桁目でも引用符つきのキーは parse_yaml が読めない
    ("git\t:\n  mode: integration\n", "malformed"),             # コロンの前のタブ
    ("\ufeffgit:\n  mode: integration\n", "malformed"),         # BOM つきの先頭キー
    ("{git: {mode: integration}}\n", "malformed"),               # 文書全体が flow 形式
    ("git:\n  pr-base: develop\n", "unknown_key"),
    ("git:\n  baseBranch: develop\n", "unknown_key"),
    ("git:\n  mode: direct\n  remote: upstream\n", "unknown_key"),
    ("git:\n  pr_base: 123\n", "type"),
    ("git:\n  pr_base: true\n", "type"),
    ("git:\n  pr_base: null\n", "type"),
    ("git:\n  mode: 1\n", "type"),
    ("git:\n  base_branch: \"\"\n", "invalid_value"),
    ("git:\n  pr_base: \" main\"\n", "invalid_value"),
    ("git:\n  pr_base: \"main \"\n", "invalid_value"),
    ("git:\n  pr_base: 'a\x07b'\n", "invalid_value"),
    ("git:\n  pr_base: 'a\x7fb'\n", "invalid_value"),
    ("git:\n  mode: integration\n", "unsupported_mode"),
    ("git:\n  worktree_root: elsewhere\n", "unsupported_value"),
    ("git:\n  worktree_root: ../x\n", "invalid_value"),
    ("git:\n  worktree_root: /x\n", "invalid_value"),
]


@pytest.mark.parametrize("text,code", REJECTIONS)
def test_policy_rejections(text, code):
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.policy_from_text(_mission_text(text))
    assert ei.value.code == code, (text, ei.value)


@pytest.mark.parametrize("field", list(gp.POLICY_FIELDS))
@pytest.mark.parametrize("bad,label", [
    ('""', "empty"), ('" "', "blank"), ('" x"', "leading space"), ('"x "', "trailing space"),
    # 制御文字は**生の文字**で書く (`parse_yaml` は `\\u0001` を解釈しない。ただの文字列になり制御文字ではない)。
    # \n・\x0b・\x0c・\x1c-\x1e は `splitlines` が行を割るのでここでは使えない。
    ("'x\x01y'", "SOH"), ("'x\x07y'", "BEL"), ("'x\x7fy'", "DEL"), ("'x\x00y'", "NUL"),
])
def test_every_field_refuses_empty_whitespace_and_control_chars_with_invalid_value(field, bad, label):
    """欄ごとに同じ拒否コード。ある層 (branch 規則・既定値との比較) が偶然拒否しても、コードが違えば赤になる
    (層が重なっている所で、片方を外しても緑のまま、を作らない)。"""
    with pytest.raises(gp.GitPolicyError) as ei:
        _policy(**{field: bad})
    assert ei.value.code == "invalid_value", (field, label, ei.value)
    assert ei.value.field == field


def test_a_rejection_never_degrades_to_default_even_when_other_fields_are_fine():
    text = _mission_text("git:\n  mode: direct\n  base_branch: develop\n  pr_base: main\n    task_branch_pattern: x\n")
    with pytest.raises(gp.GitPolicyError):
        gp.policy_from_text(text)


def test_error_detail_does_not_leak_other_mission_fields():
    text = "title: SECRET-TITLE\nnotes: SECRET-NOTES\ngit:\n  pr_base: 123\n"
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.policy_from_text(text)
    assert "SECRET" not in str(ei.value)


# ---- 「git という語が mission.yaml にあるのに policy なし (既定値)」になる入力を 0 に (Codex 2 巡目 P2-1) ----

#: `parse_yaml` は字下げ行を黙って読み飛ばす。0 桁目の `git:` しか見ない検査はこれをすり抜け、
#: `mode: integration` が黙って direct になった。字下げ・タブ・入れ子・リスト項目・引用符つきの `git` キーは全部停止。
INDENTED_GIT = [
    "title: Demo\n git:\n   mode: integration\n",
    "title: Demo\n\tgit:\n\t\tmode: integration\n",
    "title: Demo\nreview:\n  git:\n    mode: integration\n",
    "title: Demo\nreview:\n  - git: x\n",
    "title: Demo\n  - git: x\n",
    "  git:\n    mode: integration\n",
    "title: Demo\n git: {mode: integration}\n",
    "title: Demo\n  git: null\n",
    "title: Demo\n  git:\n",
    "title: Demo\n  \"git\": x\n",
    "title: Demo\n  'git': x\n",
    "title: Demo\n    git:\n      pr_base: develop\n",
    "git:\n  mode: direct\nnotes:\n  git:\n    pr_base: develop\n",   # 本物の git: があっても迷子の git: は拒否
    "title: Demo\n git :\n",                                         # `git :` (コロンの前の空白)
]

#: `git` という語を含むが「git キー」ではない入力は、今までどおり既定値 (誤って拒否しない側の確認)。
GIT_WORD_BUT_NO_KEY = [
    "title: git workflow\n",
    "title: Demo\nnotes: git: not a key\n",
    "title: Demo\ndescription: use git\n",
    "title: Demo\nreview:\n  gitlab: x\n",
    "title: Demo\n# git:\n",
    "title: Demo\n  # git: x\n",
    "title: Demo\ngithub: x\n",
    "title: Demo\ngit-policy: x\n",
]


@pytest.mark.parametrize("text", INDENTED_GIT)
def test_an_indented_or_orphan_git_key_is_never_the_default_policy(text):
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.policy_from_text(text)
    assert ei.value.code == "malformed", (text, ei.value)
    assert ei.value.field == "git"


@pytest.mark.parametrize("text", GIT_WORD_BUT_NO_KEY)
def test_the_word_git_without_a_git_key_is_still_the_default(text):
    assert gp.policy_from_text(text) == gp.default_policy()


def test_no_input_has_a_git_key_somewhere_and_the_default_policy():
    """全拒否例・全字下げ例で「git キーの行がある」なら、既定値が返らない (族 (1) の網羅)。"""
    key = re.compile(r"^[ \t]*(?:-[ \t]+)?[\"']?git[\"']?[ \t]*:", re.M)
    inputs = [_mission_text(t) for t, _ in REJECTIONS] + INDENTED_GIT + [
        _mission_text(_git_block(mode="direct")), _mission_text(_git_block(base_branch="develop"))]
    n = 0
    for text in inputs:
        if not key.search(text):
            continue
        n += 1
        try:
            p = gp.policy_from_text(text)
        except gp.GitPolicyError:
            continue
        assert p.source == "mission", f"git キーがあるのに既定値: {text!r}"
    assert n >= 40, n


# ---- エラーに mission の他の欄・行の中身を出さない (Codex 2 巡目 P2-2) ----

SECRET = "SECRET-7f3a"
#: 解析できない行 (コロンが無い)・関係ない欄・字下げの孤児。どれも git の欄ではない。
LEAK_WRAPPERS = [
    ("plain", lambda body: f"notes: {SECRET}\n" + body),
    ("block", lambda body: f"review:\n  {SECRET}-key: {SECRET}-val\n" + body),
    ("trailer", lambda body: body + f"tail: {SECRET}\n"),
    ("garbage", lambda body: f"{SECRET} garbage line without colon\n" + body),
    ("garbage-after", lambda body: body + f"{SECRET} garbage after\n"),
]


def _all_surfaces(exc: BaseException) -> str:
    import traceback
    parts = [str(exc), repr(exc), repr(exc.args), getattr(exc, "detail", ""), getattr(exc, "field", ""),
             getattr(exc, "code", ""), "".join(traceback.format_exception(exc))]
    for chained in (exc.__cause__, exc.__context__):
        if chained is not None:
            parts.append("".join(traceback.format_exception(chained)))
    return "\n".join(parts)


@pytest.mark.parametrize("wrap_name,wrap", LEAK_WRAPPERS, ids=[n for n, _ in LEAK_WRAPPERS])
@pytest.mark.parametrize("body,code", REJECTIONS + [(t, "malformed") for t in INDENTED_GIT],
                         ids=[f"rej{i}" for i in range(len(REJECTIONS) + len(INDENTED_GIT))])
def test_no_error_path_leaks_other_fields_or_line_contents(wrap_name, wrap, body, code):
    text = wrap(body if body in INDENTED_GIT else _mission_text(body))
    try:
        gp.policy_from_text(text)
    except gp.GitPolicyError as e:
        assert SECRET not in _all_surfaces(e), (wrap_name, body, e)
        assert e.__cause__ is None and e.__context__ is None, "parser の例外 (行の中身を含む) を連鎖させない"
    else:
        pytest.fail(f"拒否されるはずの入力が通った: {wrap_name} {body!r}")


def test_parse_failure_reports_only_the_line_number():
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.policy_from_text(f"title: x\n{SECRET} no colon here\nnotes: y\n")
    e = ei.value
    assert e.code == "malformed"
    assert "2" in e.detail, "行番号は出す"
    assert SECRET not in _all_surfaces(e)


def test_cli_stderr_never_carries_other_fields_or_line_contents(tmp_path):
    q = tmp_path / "queue"
    for name, text in {
        "20261001-garbage": f"title: x\n{SECRET} no colon here\ngit:\n  mode: direct\n",
        "20261001-indent": f"notes: {SECRET}\n git:\n   mode: integration\n",
        "20261001-bad": f"notes: {SECRET}\ngit:\n  pr_base: 123\n",
        "20261001-mode": f"notes: {SECRET}\ngit:\n  mode: integration\n",
    }.items():
        d = q / "missions" / name
        d.mkdir(parents=True)
        (d / "mission.yaml").write_text(text)
        for verb in (["pr-base", "--queue", str(q), "--mission", name],
                     ["resolve-task", "--queue", str(q), "--mission", name, "--task", "t001", "--task-slug", "x",
                      "--repo-root", "/tmp/r"]):
            r = _cli(*verb)
            assert r.returncode == 2 and r.stdout == "", (name, r)
            assert SECRET not in r.stdout + r.stderr, (name, r.stderr)
    # 読めない mission.yaml (UTF-8 でない) の理由にも中身を出さない
    d = q / "missions" / "20261001-bin"
    d.mkdir(parents=True)
    (d / "mission.yaml").write_bytes(f"notes: {SECRET}\n".encode() + b"\xff\xfe\n")
    r = _cli("pr-base", "--queue", str(q), "--mission", "20261001-bin")
    assert r.returncode == 2 and "unreadable" in r.stderr and SECRET not in r.stderr


def test_unparsable_mission_yaml_is_malformed_not_default():
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.policy_from_text("title: x\nthis is not yaml\n")
    assert ei.value.code == "malformed"


def test_all_error_codes_are_the_documented_set():
    assert set(gp.ERROR_CODES) == {"malformed", "unknown_key", "type", "invalid_value", "unsupported_mode",
                                    "unsupported_value", "unreadable"}


# ---- 読めない (Unreadable) は既定値に倒さない (不変条件 1) ----

def test_missing_mission_yaml_is_refused_and_distinguishable(tmp_path):
    with pytest.raises(gp.MissionNotFound) as ei:
        gp.load_git_policy("20261001-none", queue_dir=str(tmp_path))
    assert ei.value.code == "unreadable"


def test_unreadable_mission_yaml_is_refused_not_defaulted(tmp_path):
    d = tmp_path / "missions" / "20261001-x"
    d.mkdir(parents=True)
    f = d / "mission.yaml"
    f.write_text(_mission_text())
    f.chmod(0)
    try:
        if os.access(f, os.R_OK):
            pytest.skip("root などで chmod 0 が効かない環境")
        with pytest.raises(gp.GitPolicyError) as ei:
            gp.load_git_policy("20261001-x", queue_dir=str(tmp_path))
        assert ei.value.code == "unreadable" and not isinstance(ei.value, gp.MissionNotFound)
    finally:
        f.chmod(0o644)


def test_mission_yaml_that_is_a_directory_or_not_utf8_is_refused(tmp_path):
    (tmp_path / "missions" / "20261001-dir" / "mission.yaml").mkdir(parents=True)
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.load_git_policy("20261001-dir", queue_dir=str(tmp_path))
    assert ei.value.code == "unreadable"
    d = tmp_path / "missions" / "20261001-bin"
    d.mkdir(parents=True)
    (d / "mission.yaml").write_bytes(b"title: \xff\xfe\n")
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.load_git_policy("20261001-bin", queue_dir=str(tmp_path))
    assert ei.value.code == "unreadable"


@pytest.mark.parametrize("slug", ["", ".", "..", "a/b", "../x", "a\\b", "a\x00b"])
def test_load_refuses_a_slug_that_is_not_one_component(tmp_path, slug):
    """読める mission.yaml が**実在する**位置の slug (入れ子の dir・queue の外) でも、slug の検査で拒否する
    (検査が無ければ読めてしまう = ENOENT で偶然拒否されるのと区別する)。"""
    q = tmp_path / "queue"
    for rel in ("missions/a/b", "x", "missions/a\\b"):
        d = q / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "mission.yaml").write_text(_mission_text())
    (q / "missions" / "mission.yaml").write_text(_mission_text())   # slug "" / "." の指す先
    (tmp_path / "queue" / "mission.yaml").write_text(_mission_text())   # slug ".." の指す先
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.load_git_policy(slug, queue_dir=str(q))
    assert ei.value.code == "invalid_value" and ei.value.field == "mission_slug"


# ---------------------------------------------------------------------------
# §2.3 branch 名の規則 — 境界表と、本物の `git check-ref-format --branch` との部分集合
# ---------------------------------------------------------------------------

#: 設計 §2.3 の境界表。(名前, git が通す, この規則が通す)
BOUNDARY = [
    ("release.", False, False), ("-release", False, False), ("task/demo/t001-work.", False, False),
    ("a/b.lock", False, False), ("a.lock/b", False, False), ("a/.b", False, False),
    ("a..b", False, False), ("a//b", False, False), ("/a", False, False), ("a/", False, False),
    ("a@{b", False, False), ("HEAD", False, False),
    ("a/-b", True, False), ("_a", True, False), ("a/_b", True, False), ("@", True, False), ("a@b", True, False),
    ("origin/main", True, False), ("refs/heads/x", True, False), ("a./b", True, False),
    ("main", True, True), ("develop", True, True), ("release/2026.10", True, True),
    ("task/20261001-x/t001-y", True, True),
]


def _git_accepts(name: str, env) -> bool:
    r = subprocess.run(["git", "check-ref-format", "--branch", name], env=env, capture_output=True, text=True)
    return r.returncode == 0


@pytest.fixture(scope="module")
def plain_env(tmp_path_factory):
    return _git_env(tmp_path_factory.mktemp("home"))


@pytest.mark.parametrize("name,git_ok,ours_ok", BOUNDARY)
def test_boundary_table_matches_the_design_and_real_git(name, git_ok, ours_ok, plain_env):
    assert _git_accepts(name, plain_env) is git_ok, f"設計の表と git 2.x の結果が違う: {name!r}"
    assert (gp.branch_name_problem(name) is None) is ours_ok


def _clause_probes():
    """規則の各条項の境界を突く合成例 (1 文字足す・引く)。"""
    out = []
    for good in ("a", "a1", "a.b", "a-b", "a_b", "a/b", "A/B", "0/1", "a.b.c", "a-.b", "a/b-c.d_e"):
        out += [good, good + ".", good + ".lock", "." + good, "-" + good, "_" + good, good + "/", "/" + good,
                good + "//x", good + "..x", good + "@", good + "{", good + "~", good + "^", good + ":",
                good + "?", good + "*", good + "[", good + "\\", good + " ", good + "\t", good + "\x7f",
                good + ".lock/x", "x/" + good + ".lock", "x/." + good, "refs/" + good, "origin/" + good,
                "HEAD/" + good, good + "/HEAD"]
    out += ["HEAD", "head", "@", "refs", "origin", "a" * 200, "a" * 201, "a/" * 100 + "a"]
    return out


def _generated_names():
    rng = random.Random(20261001)
    wide = "abcXYZ019._-/@{}~^:?*[\\ \x01\x7f"
    narrow = "abAB09._-/"
    names = []
    for alphabet, n in ((wide, 1500), (narrow, 2500)):
        for _ in range(n):
            names.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6))))
    # 1〜3 文字の全列挙 (narrow)
    for k in (1, 2, 3):
        names += ["".join(t) for t in itertools.product(narrow, repeat=k)]
    return names


def test_our_rule_is_a_subset_of_git_check_ref_format(plain_env):
    """規則が通した名前は、本物の git も通す。1 件でも外れたら赤 (設計 §2.3 の手順 1〜3)。"""
    names = list(dict.fromkeys(_clause_probes() + _generated_names()))
    accepted = 0
    checked = 0
    violations = []
    for name in names:
        checked += 1
        if gp.branch_name_problem(name) is None:
            accepted += 1
            if not _git_accepts(name, plain_env):
                violations.append(name)
    assert violations == [], f"規則が通したのに git が拒否: {violations[:20]}"
    # 空虚な PASS を防ぐ: 通した件数と検査件数に下限
    assert checked >= 3000, checked
    assert accepted >= 300, f"規則が通した名前が少なすぎる ({accepted} 件)。部分集合の確認が空になっている"
    print(f"[subset] checked={checked} accepted_by_rule={accepted}")


def test_the_subset_check_can_fail(plain_env):
    """陽性対照: 「英数字で始まる」を外した規則はこの検査で捕まる (検査が何も守っていない状態ではない)。"""
    loose = re.compile(r"[A-Za-z0-9._-]+")

    def loose_ok(name):
        return all(loose.fullmatch(p) and not p.endswith(".") and not p.endswith(".lock") and ".." not in p
                   for p in name.split("/"))

    escaped = [n for n in dict.fromkeys(_clause_probes() + _generated_names())
               if loose_ok(n) and n and n != "HEAD" and not _git_accepts(n, plain_env)]
    assert escaped, "緩めた規則が git に拒否される名前を通さない = 生成した入力が弱い"


def test_every_real_mission_slug_and_task_id_passes_the_rule(plain_env):
    """既存の名前を拒否しない側の確認 (設計 §2.3 の手順 4)。2026-10-01 時点の queue/archive の全 slug。"""
    slugs = REAL_MISSION_SLUGS
    assert len(slugs) >= 50
    for s in slugs:
        for tid in ("t001", "t042", "t999", "t1000"):
            b = gp.task_branch(gp.default_policy(), mission_slug=s, task_id=tid, task_slug=SLUGIFY("A title: 日本語", tid))
            assert _git_accepts(b, plain_env), b
    assert all(gp.branch_name_problem(s) is None for s in slugs)


REAL_MISSION_SLUGS = """
20260722-smoke-test-mission 20260824-macos-wsl 20260825-backlog-m1 20260825-backlog-m2 20260825-backlog-m3
20260825-nasne-epg 20260825-pr103-merge 20260825-task-count-fix 20260827-epgget-decrypt-phase-a
20260828-dispatcher-director-only 20260828-epgget-decrypt-phase-b 20260828-hook-error-recurrence
20260828-plan-sh-target-dir 20260830-20260830-crewvia-3bug-fix 20260830-20260830-phase-b-v4-xml-extrac
20260830-help 20260902-crewvia-improvements 20260903-plan-update-extend 20260903-pr-review-127-128
20260903-target-dir-settings-revert 20260904-crewvia-launcher-herdr 20260904-herdr-phase1
20260904-herdr-phase2 20260904-herdr-phase3 20260904-herdr-rule5 20260904-herdr-spike
20260905-child-session-marker 20260905-crewvia-backlog 20260905-dispatcher-idle-worker-fix
20260905-dispatcher-notify-fix 20260905-dispatcher-notify-reliability 20260905-help
20260905-notify-observation 20260905-qa-gate 20260905-slug.badinit-20260905T063231Z
20260906-heartbeat-gap-fix 20260907-codex-reviewer-phase1 20260907-crewvia-backlog-5
20260907-skill-model-mapping 20260907-tmux-name-cleanup 20260908-codex-reviewer-phase3
20260908-launch-reliability 20260908-main-repo-protection 20260909-dead-config-sweep
20260909-safety-gate-hardening 20260912-verdict-ci-launcher 20260921-daemon-authority-and-mutual-watch
20260924-task-graph-integration 20260925-ops-gap-fixes 20260925-pytest-workspace-leak
20260925-task-graph-usable 20260926-mechanize-guards-a 20260927-mechanize-guards-b 20260927-qaprobe
20260929-task-graph-pages 20260930-ops-gaps-c 20260930-vnext-01a-state-store 20261001-vnext-01b-git-policy
obs-t024-a observe-only-A observe-only-B t037-probe-orphan t041-probe-b4 t041-probe-b5 test-notify-pilot
test-qa-1788612749 test-qa-1788612815 test-qa-bl1-1788612851 test-qa-bl2-1788612865 test-qa-bl3-1788612880
20260912-minerva-stage0-1
""".split()


def test_mission_slug_outside_the_rule_is_refused_not_rewritten():
    """`init` は slug を検査しない。規則の外の slug の mission は Resolver が置換後に拒否する (黙って直さない)。"""
    p = gp.default_policy()
    for bad in ("has space", "日本語", "a:b", "a~b", "_x", "-x", ".x", "x.", "x.lock"):
        with pytest.raises(gp.GitPolicyError):
            gp.task_branch(p, mission_slug=bad, task_id="t001", task_slug="x")


# ---------------------------------------------------------------------------
# §2.4 mission.yaml の往復でバイトが変わらない (原案 §14-17)
# ---------------------------------------------------------------------------

CANONICAL = ("title: Demo\nslug: 20261001-demo\nstatus: active\ngit:\n  mode: direct\n  base_branch: develop\n"
             "  pr_base: release/2026.10\n"
             "  task_branch_pattern: \"work/{task_id}/{mission_slug}\"\n  worktree_root: .claude/worktrees\n")


def test_serialize_round_trip_keeps_git_block_bytes():
    assert store.serialize_mission(parse_yaml(CANONICAL)) == CANONICAL
    assert gp.policy_from_text(CANONICAL).task_branch_pattern == "work/{task_id}/{mission_slug}"


def test_write_mission_through_the_store_keeps_git_block_bytes(tmp_path):
    q = tmp_path / "queue"
    (q / "missions" / "20261001-demo").mkdir(parents=True)
    path = q / "missions" / "20261001-demo" / "mission.yaml"
    path.write_text(CANONICAL)
    # 他の欄を変えて書き戻す (plan.sh が mission.yaml を更新する形) — git: の下は 1 バイトも変わらない
    with store.transaction(str(q), op="test", actor="pytest") as txn:
        data = parse_yaml(path.read_text())
        data["next_task_id"] = 7
        txn.write_mission("20261001-demo", data)
    after = path.read_text()
    assert after.startswith(CANONICAL.split("git:\n")[0])
    assert "git:" + CANONICAL.split("git:", 1)[1] in after.replace("next_task_id: 7\n", "")
    assert gp.load_git_policy("20261001-demo", queue_dir=str(q)).pr_base == "release/2026.10"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "lib_git_policy.py"), *args], capture_output=True,
                          text=True, env={"PATH": os.environ["PATH"]})


def test_cli_resolve_task_and_pr_base(tmp_path):
    q = tmp_path / "queue"
    (q / "missions" / "20261001-demo").mkdir(parents=True)
    (q / "missions" / "20261001-demo" / "mission.yaml").write_text(CANONICAL)
    r = _cli("resolve-task", "--queue", str(q), "--mission", "20261001-demo", "--task", "t003",
             "--task-slug", "abc", "--repo-root", "/tmp/r")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out == {"branch": "work/t003/20261001-demo", "worktree_path": "/tmp/r/.claude/worktrees/20261001-demo/t003-abc",
                   "base_remote": "origin/develop", "base_local": "develop", "pr_base": "release/2026.10"}
    assert len(r.stdout.strip().splitlines()) == 1
    r = _cli("pr-base", "--queue", str(q), "--mission", "20261001-demo")
    assert r.returncode == 0 and json.loads(r.stdout) == {"pr_base": "release/2026.10"}


def test_cli_rejection_is_exit_2_with_code_on_stderr_and_empty_stdout(tmp_path):
    q = tmp_path / "queue"
    (q / "missions" / "20261001-bad").mkdir(parents=True)
    (q / "missions" / "20261001-bad" / "mission.yaml").write_text(_mission_text("git:\n  mode: integration\n"))
    r = _cli("pr-base", "--queue", str(q), "--mission", "20261001-bad")
    assert r.returncode == 2 and r.stdout == "" and "unsupported_mode" in r.stderr
    r = _cli("pr-base", "--queue", str(q), "--mission", "20261001-missing")
    assert r.returncode == 2 and r.stdout == "" and "unreadable" in r.stderr


# ---------------------------------------------------------------------------
# 呼び出し元ゼロ (G3 で外す。01a S2 の test_state_store_has_no_callers_yet と同じ作法)
# ---------------------------------------------------------------------------

NAME = "lib_git_policy"
#: 名前を出してよいファイル (repo 相対)。テストと knowledge/ は走査の対象外。
#: G3 (cutover。ユーザー承認済みの PR) で呼び出し元になった集合。**ここに足すのは cutover (= ユーザー承認が要る PR) だけ**。
ALLOWED_CALLERS = {
    "scripts/lib_git_policy.py",   # 自身 (docstring・CLI)
    "scripts/plan.sh",             # pull の worktree 失敗の分類・`pr-base`
    "scripts/git-helpers.sh",      # crewvia_create_worktree / remove / create_pr が CLI を呼ぶ
    "scripts/kai-review.sh",       # diff base (pr-base)
    "scripts/worktree_gc.py",      # 片付けの根 (DEFAULT_WORKTREE_ROOT)
    "scripts/lint_plan.py",        # mission.yaml の git: の検査
}
SCAN_DIRS = ("scripts", "hooks", "agents", "config", "skills")
SCAN_FILES = ("crewvia", "crewvia-stop")


def _candidates():
    files = []
    for d in SCAN_DIRS:
        base = REPO / d
        if not base.is_dir():
            continue
        # 文書 (.md) は対象にしない (ガードを説明する文書が自分で引っかかる型)
        # テスト (scripts/ に置いてある test_*.sh・e2e ハーネス) は呼び出し元ではなく検証する側なので対象にしない
        files += [p for p in base.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".md", ".pyc")
                  and not p.name.startswith(("test_", "e2e_harness"))]
    files += [REPO / f for f in SCAN_FILES if (REPO / f).is_file()]
    return files


def test_git_policy_callers_are_exactly_the_cutover_set():
    scanned = 0
    offenders = []
    seen = set()
    for p in _candidates():
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        scanned += 1
        rel = p.relative_to(REPO).as_posix()
        if NAME in text:
            (seen.add(rel) if rel in ALLOWED_CALLERS else offenders.append(rel))
    assert scanned >= 60, f"走査したファイルが少なすぎる ({scanned} 件)"
    assert offenders == [], (f"lib_git_policy を呼ぶコードが増えた (cutover か確認。ユーザー承認が要る): "
                             f"{offenders}")
    assert seen == ALLOWED_CALLERS


def test_the_caller_detector_finds_each_way_of_calling_it():
    for s in ("import lib_git_policy", "from lib_git_policy import task_branch", "import lib_git_policy as g",
              "importlib.import_module('lib_git_policy')", 'python3 "$SCRIPT_DIR/lib_git_policy.py" pr-base',
              "python3 -m lib_git_policy", "__import__(\"lib_git_policy\")"):
        assert NAME in s, s
    assert NAME not in "import lib_task_cards"


def test_the_resolver_runs_no_subprocess_and_imports_no_git_tooling():
    """判断だけを持つ: subprocess / os.system / shutil.which を import も呼びもしない。"""
    src = (SCRIPTS / "lib_git_policy.py").read_text()
    import ast
    tree = ast.parse(src)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not (imported & {"subprocess", "shutil", "pty", "multiprocessing"}), imported
    assert "os.system" not in src and "os.popen" not in src and "os.exec" not in src


# ---------------------------------------------------------------------------
# G2 fix 3 巡目 (t026): branch pattern の境界・parse_yaml が飛ばした行
# ---------------------------------------------------------------------------

#: 飛ばされる形を列挙して塞ぐのをやめ、「parse_yaml が結果に反映しなかった行が 1 行でもあれば停止」にした。
#: 列挙に無い形 (flow 形式・git を含まない迷子の行) もここで止まる。
FLOW_AND_UNLISTED_SKIPPED_LINES = [
    "title: Demo\n  {git: {mode: integration}}\n",
    "title: Demo\n  {\"git\": {mode: integration}}\n",
    "title: Demo\n  [git, mode]\n",
    "title: Demo\n  just a stray line\n",
    "title: Demo\n    deeply: nested\n",
    "title: Demo\nreview:\n  a: 1\n    b: 2\n",
    "git:\n  mode: direct\n\n  base_branch: develop\n",
]


@pytest.mark.parametrize("text", FLOW_AND_UNLISTED_SKIPPED_LINES)
def test_a_line_parse_yaml_skipped_is_never_concluded_as_no_git(text):
    with pytest.raises(gp.GitPolicyError) as ei:
        gp.policy_from_text(text)
    assert ei.value.code == "malformed", (text, ei.value)
    assert ei.value.field == "git"
    assert "integration" not in str(ei.value) and "stray line" not in str(ei.value)  # 行の中身は出さない


def test_a_mission_yaml_with_nothing_skipped_is_still_the_default():
    """誤って拒否しない側: 今ある mission.yaml の形 (review: の mapping・コメント・空行) は既定値のまま。"""
    text = ("title: \"x\"\nslug: 20261001-demo\n# comment\n\nnext_task_id: 3\nreview:\n  last_verdict: approve\n"
            "  cycle_count: 1\n  reviewer: null\ntags:\n  - a\n  - b\n")
    assert gp.policy_from_text(text) == gp.default_policy()


# ---- branch pattern の境界 (別の task が同じ branch にならない) ----

COLLIDING_PATTERNS = [
    "task/{mission_slug}/{task_id}{task_slug}",   # t100 + 0fix と t1000 + fix
    "task/{mission_slug}/{task_slug}{task_id}",
    "task/{mission_slug}{task_id}",
    "task/{mission_slug}-{task_id}",              # mission 自身が - を含む
    "task/{mission_slug}/{task_id}{task_slug}-x",
    "task/{task_id}-{mission_slug}{task_slug}",
    "task/{mission_slug}/{task_id}-{task_slug}-extra",   # slug の後ろの - は境界にならない
]


@pytest.mark.parametrize("pattern", COLLIDING_PATTERNS)
def test_a_branch_pattern_without_a_separator_is_refused(pattern):
    with pytest.raises(gp.GitPolicyError) as ei:
        _policy(task_branch_pattern=json.dumps(pattern))
    assert ei.value.code == "invalid_value"
    assert ei.value.field == "task_branch_pattern"


@pytest.mark.parametrize("pattern", [
    "task/{mission_slug}/{task_id}-{task_slug}",     # 既定値
    "task/{mission_slug}/{task_id}",
    "work/{task_id}/{mission_slug}",
    "{mission_slug}/{task_id}-{task_slug}",
    "t/{mission_slug}/{task_slug}/{task_id}",
    "x-{mission_slug}/{task_id}-{task_slug}",
])
def test_a_branch_pattern_with_separators_is_accepted(pattern):
    assert _policy(task_branch_pattern=json.dumps(pattern)).task_branch_pattern == pattern


def test_the_colliding_pair_from_the_review_is_refused_at_validation():
    """指摘の組: mission demo の t100 (slug 0fix) と t1000 (slug fix) が同じ branch になる pattern は設定の段階で拒否される。"""
    pattern = "task/{mission_slug}/{task_id}{task_slug}"
    a = pattern.replace("{mission_slug}", "demo").replace("{task_id}", "t100").replace("{task_slug}", "0fix")
    b = pattern.replace("{mission_slug}", "demo").replace("{task_id}", "t1000").replace("{task_slug}", "fix")
    assert a == b  # 前提: 実際に衝突する
    with pytest.raises(gp.GitPolicyError):
        _policy(task_branch_pattern=json.dumps(pattern))


def test_every_accepted_pattern_maps_distinct_tasks_to_distinct_branches_and_paths(tmp_path):
    """受理される pattern では、(mission, task_id, slug) が違えば branch も worktree path も違う (総当たり)。"""
    missions = ["demo", "demo-t1", "20261001-a", "a-b", "a"]
    ids = ["t1", "t10", "t100", "t1000", "t11"]
    slugs = ["fix", "0fix", "1", "x-1", "t1-fix", "10"]
    patterns = ["task/{mission_slug}/{task_id}-{task_slug}", "work/{task_id}/{mission_slug}/{task_slug}",
                "{mission_slug}/{task_slug}/{task_id}"]
    for pattern in patterns:
        pol = _policy(task_branch_pattern=json.dumps(pattern))
        branches, paths = {}, {}
        for m, i, s in itertools.product(missions, ids, slugs):
            key = (m, i, s)
            b = gp.task_branch(pol, mission_slug=m, task_id=i, task_slug=s)
            w = gp.task_worktree_path(pol, repo_root=str(tmp_path), mission_slug=m, task_id=i, task_slug=s)
            assert branches.setdefault(b, key) == key, (pattern, b, key, branches[b])
            assert paths.setdefault(w, key) == key, (pattern, w, key, paths[w])
