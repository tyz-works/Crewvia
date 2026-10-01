#!/usr/bin/env python3
"""Git の判断 (branch / base / worktree root) のリテラルが Resolver の外に**増えたら CI が赤**になること (vNext 01b G3 / t012)。

設計: `knowledge/git-policy.md` §6。形は 01a の `tests/test_queue_writes_go_through_the_store.py` と同じ —— **allowlist**。
表 (`ALLOWED`) に載っていない検出が 1 つでもあれば落ちる (denylist は新しい書き方で必ず穴が開く)。検出器は
`tests/git_decision_scan.py` (リテラルの形と、何を拾い何を拾わないかはそちらの docstring)。

## 表の作り

`(ファイル, 関数) → (件数, 理由)`。**鍵に断片の文字列を使わない** (memory write-guard-allowlist-key-is-function-and-count):
文言の直しで鍵が壊れるが、(関数, 件数) は「その関数に判断が増えた / 減った」を直接言う。

* 増えた = 新しい判断が足された。Resolver (`lib_git_policy.py`) から得ろ。寄せない理由があるなら理由つきで表に足す
* 減った / 該当が無い = 表が古い (直したのに残った)。表から外す (`test_no_allowlist_row_is_dead`)
* 理由の欄に「未調査」は書けない

## 対象

* コード: `scripts/*.py`・`scripts/*.sh`・`scripts/bin/*`・`hooks/*.sh`・`hooks/*.py`・トップの `crewvia`・`crewvia-stop`。
  除外は `EXCLUDED` (テスト・Resolver 自身。理由つき)。
* 文書: `agents/*.md`・`skills/*/SKILL.md` の **fenced code block の中だけ**。

## 空虚な PASS を防ぐ

1. 検査したファイル数・code block 数・拾った件数を出し (CI ログにも `guard_report` 経由で残す)、下限を assert する
2. 死んだ行を落とす
3. 陽性対照: **本物のコードから切り出した形**を検出器が拾う (`test_positive_controls_*`)
4. worktree で走らせても対象が 0 件にならない
5. 赤の実証: 本物のファイルに 1 行足すと赤になる (`test_adding_a_literal_to_a_real_file_turns_the_guard_red`)

    python3 -m pytest tests/test_git_decisions_go_through_policy.py -v
"""

from __future__ import annotations

import collections
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import git_decision_scan as scan  # noqa: E402
import guard_report  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 検査から外すもの (テストと Resolver 自身)。**理由つき**。
EXCLUDED = {
    "scripts/lib_git_policy.py": "判断を持つ唯一の場所 (Resolver) 自身",
}
EXCLUDED_PREFIXES = (
    ("scripts/test_", "scripts/ に置いてあるテスト (本番のコードではない。tests/ と同じ扱い)"),
    ("scripts/e2e_harness", "scripts/ に置いてある e2e ハーネス (テスト)"),
)

# ---------------------------------------------------------------------------
# 理由
# ---------------------------------------------------------------------------

R_MAIN_CHECKOUT = ("主 checkout を origin/main に追従させる手順 / 検出。task の policy ではなく、本番 crewvia の更新手順 "
                   "(crewvia の本番 branch は mission ごとに変わらない)。knowledge/git-policy.md §4.1")
R_HOOK_ROOT = ("hook が自分の worktree への編集を拒否しないための除外。worktree_root は Resolver が既定値に固定している "
               "(§2.1) 間は値が一致する。広げるときに寄せる (§10-5)")
R_DOC_DEFAULT = ("文書の code block に書いた**既定の形**の説明 (worktree の命名)。決めるのは pull (JSON の `worktree_path` と "
                 "`git branch --show-current` が正) と本文に明記してある。PR base の決め打ちではない (G4 / t016)")
R_DOC_INVESTIGATE = "調査の例 (`git log origin/task/...`)。task の base ではなく、task branch の remote の見方 (G4 / t016。knowledge/git-policy.md §4.2)"

#: 検出した判断のうち、**寄せない** (or G4 が書き換える) もの。`(ファイル, 関数) → (件数, 理由)`。
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("scripts/lib_daemon_watch.py", "fetch_origin"): (1, R_MAIN_CHECKOUT + " (`branch=\"main\"` の既定値)"),
    ("scripts/lib_daemon_watch.py", "commits_behind"): (1, R_MAIN_CHECKOUT + " (`ref=\"origin/main\"` の既定値)"),
    ("scripts/lib_daemon_watch.py", "changed_files_vs"): (1, R_MAIN_CHECKOUT + " (`ref=\"origin/main\"` の既定値)"),
    ("scripts/dispatcher.sh", "build_msg"): (1, R_MAIN_CHECKOUT + " (drift の通知文)"),
    ("scripts/sync-main-checkout.sh", "<top>"): (6, R_MAIN_CHECKOUT + " (ff 本体と報告)"),
    ("hooks/pre-tool-use.sh", "<top>"): (1, R_HOOK_ROOT + " (編集ガードの case パターン)"),
    ("hooks/lib_main_repo_git_guard.py", "has_main_repo_reference"): (1, R_HOOK_ROOT + " (git ガードの除外)"),
    ("agents/director.md", "<code block>"): (2, R_DOC_DEFAULT + " ×1・" + R_DOC_INVESTIGATE + " ×1"),
    ("agents/worker.md", "<code block>"): (2, R_DOC_DEFAULT + " (命名の説明・pull の JSON の例)"),
}

#: 検査した対象の下限 (G4 着手時に実測: code 58 ファイル・文書 7 本 133 block・拾った 16 件。G3 では文書の書き換え前で 22 件)。
#: 検出器が壊れて数件しか拾わなくなっても PASS しないための床。多少の増減で赤にならない余裕を持たせてある。
MIN_CODE_FILES = 50
MIN_DOC_FILES = 5
MIN_CODE_BLOCKS = 100
MIN_HITS = 12


def _excluded(rel: str) -> bool:
    return rel in EXCLUDED or any(rel.startswith(prefix) for prefix, _why in EXCLUDED_PREFIXES)


def code_targets() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for pattern in ("scripts/*.py", "scripts/*.sh", "scripts/bin/*", "hooks/*.sh", "hooks/*.py", "crewvia", "crewvia-stop"):
        found.extend(p for p in REPO_ROOT.glob(pattern) if p.is_file())
    return sorted(p for p in set(found) if not _excluded(p.relative_to(REPO_ROOT).as_posix()))


def doc_targets() -> list[pathlib.Path]:
    return sorted(list(REPO_ROOT.glob("agents/*.md")) + list(REPO_ROOT.glob("skills/*/SKILL.md")))


def collect() -> tuple[list[scan.Hit], dict]:
    hits: list[scan.Hit] = []
    blocks = 0
    for path in code_targets():
        hits.extend(scan.file_hits(path, path.relative_to(REPO_ROOT).as_posix()))
    for path in doc_targets():
        doc, n = scan.doc_hits(path.read_text(encoding="utf-8"), path.relative_to(REPO_ROOT).as_posix())
        hits.extend(doc)
        blocks += n
    return hits, {"code_files": len(code_targets()), "doc_files": len(doc_targets()), "code_blocks": blocks}


def _counts(hits) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for h in hits:
        counter[(h.file, h.function)] += 1
    return counter


def problems_for(hits) -> list[str]:
    counts = _counts(hits)
    problems = []
    for key, n in sorted(counts.items()):
        if key not in ALLOWED:
            examples = "; ".join(f"{h.what} @L{h.line}: {h.snippet}" for h in hits if (h.file, h.function) == key)[:300]
            problems.append(f"未登録の Git 判断: {key} × {n} ({examples})")
        elif ALLOWED[key][0] != n:
            problems.append(f"件数が変わった: {key} 表={ALLOWED[key][0]} 実際={n}")
    return problems


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------


def test_no_unlisted_git_decision_remains():
    """表に無い判断・件数が変わった (関数) があれば落ちる。成功しても件数を CI ログに残す (`guard_report`)。"""
    hits, cov = collect()
    problems = problems_for(hits)
    guard_report.record("git-decisions", code_files=cov["code_files"], doc_files=cov["doc_files"],
                        code_blocks=cov["code_blocks"], hits=len(hits),
                        allowlisted=sum(1 for h in hits if (h.file, h.function) in ALLOWED),
                        unlisted=len(problems))
    assert not problems, (
        "Resolver (scripts/lib_git_policy.py) を通らない Git の判断が増えた / 減った:\n  " + "\n  ".join(problems)
        + "\n  → branch / base / PR base / worktree root は lib_git_policy から得る (bash は `lib_git_policy.py "
          "resolve-task|pr-base`、plan.sh は `_GIT_POLICY`、文書は `plan pr-base`)。寄せない理由があるなら、理由つきで "
          "ALLOWED に足す (knowledge/git-policy.md §4・§6)")


def test_no_allowlist_row_is_dead():
    """表の行に該当が無い (直したのに残った) ときは落ちる。"""
    hits, _ = collect()
    seen = {(h.file, h.function) for h in hits}
    dead = sorted(k for k in ALLOWED if k not in seen)
    assert not dead, f"検出が 0 件になった表の行 (表から外す): {dead}"


def test_every_row_has_a_real_reason():
    for key, (n, why) in ALLOWED.items():
        assert n >= 1, key
        assert len(why) >= 20 and "未調査" not in why and "TODO" not in why, (key, why)
    for rel, why in EXCLUDED.items():
        assert len(why) >= 10, rel


def test_the_scan_is_not_vacuous():
    hits, cov = collect()
    print(f"[git-decision-guard] code_files={cov['code_files']} doc_files={cov['doc_files']} "
          f"code_blocks={cov['code_blocks']} hits={len(hits)}")
    assert cov["code_files"] >= MIN_CODE_FILES, f"検査したコードが {cov['code_files']} ファイル (下限 {MIN_CODE_FILES})"
    assert cov["doc_files"] >= MIN_DOC_FILES, f"検査した文書が {cov['doc_files']} 本 (下限 {MIN_DOC_FILES})"
    assert cov["code_blocks"] >= MIN_CODE_BLOCKS, f"検査した code block が {cov['code_blocks']} 個 (下限 {MIN_CODE_BLOCKS})"
    assert len(hits) >= MIN_HITS, f"拾った件数が {len(hits)} 件 (下限 {MIN_HITS})。検出器が壊れている"
    # python 側 (AST) と bash 側 (字句) と文書側の全部が生きている
    kinds = {("doc" if h.function == "<code block>" else "py" if h.file.endswith(".py") else "sh") for h in hits}
    assert kinds == {"doc", "py", "sh"}, kinds


def test_scan_targets_exist_in_a_worktree():
    """worktree で走らせても対象が 0 件にならない (除外をパス文字列で判定しない)。"""
    assert any(p.name == "plan.sh" for p in code_targets())
    assert any(p.name == "git-helpers.sh" for p in code_targets())
    assert any(p.name == "worker.md" for p in doc_targets())


def test_the_resolver_and_the_callers_hold_no_git_decision_literal_outside_the_table():
    """判断を Resolver に寄せた箇所 (G3 の「寄せる」行) に、リテラルが戻っていない。"""
    hits, _ = collect()
    for moved in ("scripts/git-helpers.sh", "scripts/plan.sh", "scripts/kai-review.sh", "scripts/worktree_gc.py",
                  "scripts/lint_plan.py"):
        assert [h for h in hits if h.file == moved] == [], (moved, [(h.what, h.snippet) for h in hits if h.file == moved])


# ---------------------------------------------------------------------------
# 陽性対照: 本物のコードから切り出した形を拾う
# ---------------------------------------------------------------------------

BASH_FORMS = [
    ('local base="origin/main"', "origin/<literal>"),                              # git-helpers.sh (G3 前) の base
    ("gh pr create \\\n    --title \"$t\" \\\n    --base main \\\n    --head x", "--base <literal>"),   # 複数行の gh pr create
    ('git fetch --force origin "main:${BASE_FETCH_LOCAL_REF}"', "main: refspec"),   # kai-review.sh (G3 前)
    ('git show-ref --verify --quiet "refs/remotes/origin/main"', "origin/<literal>"),
    ('git show-ref --verify "refs/heads/main"', "refs main"),
    ('"${_GUARD_REPO_REAL}"/.claude/worktrees/*)', ".claude/worktrees"),           # pre-tool-use.sh の case パターン
    ('git diff main...HEAD --name-only', "main..."),
    # G4: main 以外のリテラルも拾う (G3 の QA t013。`--base develop` は赤にならなかった)
    ("gh pr create --title t --base develop", "--base <literal>"),
    ("gh pr edit 12 --base=release/1.2", "--base <literal>"),
    ('gh pr edit 12 --base "develop"', "--base <literal>"),
    ('git fetch origin && git rev-parse origin/develop', "origin/<literal>"),
    ('git diff develop...HEAD --name-only', "<literal>..HEAD"),
    ('git log develop..HEAD --oneline', "<literal>..HEAD"),
    ('git log origin/release/1.2..HEAD --oneline', "origin/<literal>"),
    ('git log release..HEAD --oneline', "<literal>..HEAD"),               # verifier.md (G4 前) の `main..HEAD` は 2 ドットで拾えていなかった

    ('git worktree add -b "task/$m/$t-$s" "$p" "$base"', "task/$ branch"),
]


@pytest.mark.parametrize("src,what", BASH_FORMS, ids=[w + "-" + str(i) for i, (_s, w) in enumerate(BASH_FORMS)])
def test_positive_controls_bash(src, what):
    names = [h.what for h in scan.shell_hits(src + "\n", "x.sh")]
    assert what in names, (src, names)


def test_positive_control_literal_after_a_comment_with_a_heredoc_opener():
    """コメント中の `<<EOF` で残りを読み飛ばす盲点 (t035) の再発防止: その**後**のリテラルも拾う。"""
    src = "# see <<EOF in the docs\nbase=origin/main\n"
    assert [h.what for h in scan.shell_hits(src, "x.sh")] == ["origin/<literal>"]


def test_positive_control_python_heredoc_body_in_a_sh_file():
    src = "echo hi\npython3 - <<'PYEOF'\nref = 'origin/main'\nPYEOF\n"
    assert [h.what for h in scan.shell_hits(src, "x.sh")] == ["origin/<literal>"]


PY_FORMS = [
    ('def fetch_origin(repo_root, *, remote: str = "origin", branch: str = "main",\n                 timeout=3):\n    pass\n', '"main" (exact)'),
    ('def commits_behind(repo_root, *, ref: str = "origin/main"):\n    pass\n', "origin/<literal>"),
    ('def f(ref: str = "origin/develop"):\n    pass\n', "origin/<literal>"),          # G4: main 以外
    ("cmd = ['gh', 'pr', 'create', '--base develop']\n", "--base <literal>"),
    ("import os\nx = os.path.join(self.repo, '.claude', 'worktrees')\n", ".claude/worktrees (joined)"),
    ("p = '/tmp/x/.claude/worktrees/' + name\n", ".claude/worktrees"),
    ("b = f'task/{m}/{t}-{s}'\n", "task/ pattern"),
    ("ref = f'refs/remotes/origin/main'\n", "refs/remotes/origin/main"),
    ("ref = 'refs/heads/main'\n", "refs/heads/main"),
]


@pytest.mark.parametrize("src,what", PY_FORMS, ids=[f"{w}-{i}" for i, (_s, w) in enumerate(PY_FORMS)])
def test_positive_controls_python(src, what):
    names = [h.what for h in scan.python_hits(src, "x.py")]
    assert what in names, (src, names)


def test_positive_control_document_code_block_but_not_prose():
    md = ("説明: `git diff main...HEAD` を使う (地の文は対象外)。origin/main との diff。\n\n"
          "```bash\n# コメントの origin/main は拾わない\ngit diff main...HEAD --name-only\n```\n\n"
          "また地の文に --base main と書く。\n")
    hits, blocks = scan.doc_hits(md, "x.md")
    assert blocks == 1
    assert sorted(h.what for h in hits) == ["<literal>..HEAD", "main..."], hits   # 同じ 1 行を 2 つの形が拾う


@pytest.mark.parametrize("src", [
    '"""docstring: origin/main と .claude/worktrees と --base main"""\nx = 1\n',
    "def f():\n    '''docstring task/{a}/{b}'''\n    return 1\n",
    "msg = f'task/{tid}: timeout must be a dict'\n",       # lint のメッセージ (branch ではない)
    "name = 'main_branch_name'\n",
])
def test_negative_controls_python(src):
    assert scan.python_hits(src, "x.py") == []


@pytest.mark.parametrize("src", [
    'gh pr create --base "$PR_BASE"',
    "gh pr edit 1 --base \"${PR_BASE}\"",
    'git diff "${DIFF_REF}...HEAD" --name-only',
    'git log "${DIFF_REF}..HEAD" --oneline',
    'git diff "$DIFF_REF...HEAD"',
    'case "$DIFF_REF" in\n  origin/*)\n    git fetch origin "${DIFF_REF#origin/}" ;;\nesac',   # 文書の手順そのもの
    'PR_BASE="$(plan pr-base --mission $M --task $T)" || exit 1',
    'git push origin HEAD',
    'git merge-base HEAD origin_x',
])
def test_negative_controls_bash_env_and_placeholder_forms(src):
    """env 参照・変数・glob は通す (リテラルだけを拾う。G4 の検出対象の拡張が文書の正しい書き方を赤にしない)。"""
    assert scan.shell_hits(src + "\n", "x.sh") == [], scan.shell_hits(src + "\n", "x.sh")


def test_negative_controls_bash_comments_only():
    assert scan.shell_hits("# origin/main --base main .claude/worktrees\necho ok # origin/main\n", "x.sh") == []


# ---------------------------------------------------------------------------
# 赤の実証: 本物のファイルに 1 行足すと赤になる
# ---------------------------------------------------------------------------

def _hits_with_extra(rel: str, extra: str) -> list[scan.Hit]:
    """`rel` の本物の内容に `extra` を足した版を検出器に通し、他のファイルは本物のまま。"""
    hits, _ = collect()
    hits = [h for h in hits if h.file != rel]
    path = REPO_ROOT / rel
    text = path.read_text(encoding="utf-8") + extra
    if rel.endswith(".md"):
        doc, _n = scan.doc_hits(text, rel)
        return hits + doc
    if rel.endswith(".py"):
        return hits + scan.python_hits(text, rel)
    return hits + scan.shell_hits(text, rel)


@pytest.mark.parametrize("rel,extra", [
    ("scripts/git-helpers.sh", '\n_x() {\n  git worktree add -b "$b" "$p" origin/main\n}\n'),
    ("scripts/git-helpers.sh", "\n_y() {\n  gh pr create --title t --base main --head h\n}\n"),
    ("scripts/plan.sh", "\n_BASE = 'origin/main'\n"),
    ("scripts/worktree_gc.py", "\nROOT = '.claude/worktrees'\n"),
    ("scripts/kai-review.sh", '\nfoo() { git fetch origin "main:x"; }\n'),
    ("agents/worker.md", "\n```bash\ngh pr create --base main\n```\n"),
    ("skills/crewvia-qa/SKILL.md", "\n```bash\ngit diff main...HEAD\n```\n"),
    # G4: main 以外のリテラル (G3 では赤にならなかった) と、2 ドットの形・env 参照を書き換えた文書への書き戻し
    ("agents/worker.md", "\n```bash\ngh pr create --base develop\n```\n"),
    ("agents/director.md", "\n```bash\ngh pr edit 1 --base release/2\n```\n"),
    ("agents/verifier.md", "\n```bash\ngit log main..HEAD --oneline\n```\n"),
    ("agents/verifier.md", "\n```bash\ngit diff origin/develop...HEAD\n```\n"),
    ("agents/worker-codex.md", "\n```bash\ngit diff origin/main\n```\n"),
    ("skills/crewvia-qa/SKILL.md", "\n```bash\ngit diff develop...HEAD --name-only\n```\n"),
    ("scripts/plan.sh", "\n_BASE = 'origin/develop'\n"),
    ("scripts/git-helpers.sh", "\n_z() {\n  gh pr create --title t --base develop\n}\n"),
])
def test_adding_a_literal_to_a_real_file_turns_the_guard_red(rel, extra):
    baseline, _ = collect()
    assert problems_for(baseline) == [], "前提: 何も足さない状態は緑"
    assert problems_for(_hits_with_extra(rel, extra)), f"{rel} に実際の書き方を足したのに赤にならない: {extra!r}"
