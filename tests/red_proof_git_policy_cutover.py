#!/usr/bin/env python3
"""red_proof_git_policy_cutover.py — vNext 01b G3 の赤の実証 (t012)。**本番に触れない**。

追加したテストが「欠陥を戻すと赤になる」ことを、**使い捨ての複製**の上で確かめる。赤は「狙ったテスト名の FAILED」で、
collection error・ImportError は赤と数えない (memory red-proof-catches-tests-green-for-the-wrong-reason)。

1. `guard`: 構造ガード (`tests/test_git_decisions_go_through_policy.py`) に**実際の書き方**を 1 行ずつ足す。
   `git-helpers.sh` に `--base main` / `origin/main` のリテラル、`worker.md` の code block に `--base main`、
   `plan.sh` の Python に `'origin/main'`、`worktree_gc.py` に `'.claude/worktrees'`。足す前の複製は緑であること (対照)。
2. `pre-g3`: G3 の本番コード (git-helpers.sh / plan.sh / kai-review.sh / worktree_gc.py / lint_plan.py / lib_git_policy.py) を
   **G3 前 (PRE_G3_SHA = G2 の merge commit) に戻した複製**で、G3 の新テストを走らせる。狙ったテストが FAILED になる。
   比較元は固定した commit (stacked PR で red proof が失効しないように `origin/main` ではなく sha。memory
   red-proof-scripts-go-stale-on-stacked-prs)。

使い方:
    python3 tests/red_proof_git_policy_cutover.py            # 両方 (約 3 分)
    python3 tests/red_proof_git_policy_cutover.py guard      # 構造ガードだけ
    python3 tests/red_proof_git_policy_cutover.py pre-g3     # G3 前に戻した複製だけ

終了コード: 全部期待どおりなら 0。
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
#: G2 の merge commit (G3 前の main)。履歴に残る固定点。
PRE_G3_SHA = "bd4485a"
G3_PRODUCTION_FILES = ("scripts/git-helpers.sh", "scripts/plan.sh", "scripts/kai-review.sh", "scripts/worktree_gc.py",
                       "scripts/lint_plan.py", "scripts/lib_git_policy.py")

GUARD_TEST = "tests/test_git_decisions_go_through_policy.py"
GUARD_NAME = "test_no_unlisted_git_decision_remains"

#: (ラベル, 対象ファイル, 足す内容)。どれも**本物のコードの書き方**。
GUARD_INJECTIONS = [
    ("git-helpers.sh に `gh pr create --base main` を戻す", "scripts/git-helpers.sh",
     "\n_red_proof_pr() {\n  gh pr create --title t --body b \\\n    --base main \\\n    --head x\n}\n"),
    ("git-helpers.sh に `local base=\"origin/main\"` を戻す", "scripts/git-helpers.sh",
     '\n_red_proof_base() {\n  local base="origin/main"\n  echo "$base"\n}\n'),
    ("kai-review.sh に `\"main:${REF}\"` の fetch を戻す", "scripts/kai-review.sh",
     '\n_red_proof_fetch() { git fetch --force origin "main:${BASE_FETCH_LOCAL_REF}"; }\n'),
    ("worker.md の code block に `--base main`", "agents/worker.md",
     "\n```bash\ngh pr create --title t --base main\n```\n"),
    ("SKILL.md の code block に `git diff main...HEAD`", "skills/crewvia-qa/SKILL.md",
     "\n```bash\ngit diff main...HEAD --name-only\n```\n"),
    ("plan.sh の Python に `'origin/main'`", "scripts/plan.sh",
     "\nREF = 'origin/main'\n"),   # 末尾は python heredoc の外だが、下の _inject_plan_sh が heredoc の中に入れる
    ("worktree_gc.py に `'.claude/worktrees'`", "scripts/worktree_gc.py",
     "\nROOT = os.path.join('/x', '.claude/worktrees')\n"),
]

#: G3 前に戻した複製で FAILED になるはずのテスト (名前の一部。parametrize の id を含む)。
PRE_G3_EXPECTED_FAILED = [
    "test_custom_base_branch_is_where_the_task_branch_is_cut_from",
    "test_custom_pr_base_is_what_pr_base_returns_from_any_cwd",
    "test_custom_task_branch_pattern_names_the_branch",
    "test_pr_base_refuses_without_agent_name",
    "test_target_dir_task_gets_main_whatever_the_mission_says",
    "test_named_form_ignores_ownership_and_status",
    "test_unrelated_indent_mistake_stops_the_pull_with_file_line_and_fix_and_leaks_no_line_content",
    "test_lint_fails_the_same_input_before_any_pull",
    "test_unsupported_or_dangerous_policy_stops_pull_and_fails_lint",
    "test_create_pr_passes_the_resolved_pr_base",
    "test_create_pr_refuses_when_pr_base_cannot_be_resolved_and_does_not_push",
    "test_worktree_lookup_is_not_forged_by_a_newline_in_another_worktrees_path",
    "test_git_policy_callers_are_exactly_the_cutover_set",
]
PRE_G3_TEST_FILES = ("tests/test_git_policy_pull_and_pr_base_cutover.py", "tests/test_git_policy_resolver.py",
                     GUARD_TEST)


def sh(*cmd, cwd=None, env=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed: {cmd}\n{p.stdout}\n{p.stderr}")
    return p


def make_copy(dest: pathlib.Path) -> None:
    """追跡されているファイル + 未追跡の新規ファイルを `dest` に写す (.git は写さない)。"""
    names = set(sh("git", "ls-files", cwd=REPO).stdout.splitlines())
    names |= set(sh("git", "ls-files", "--others", "--exclude-standard", cwd=REPO).stdout.splitlines())
    for name in sorted(names):
        src = REPO / name
        if not src.is_file():
            continue
        out = dest / name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)


def pytest_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWVIA_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CREWVIA_HERDR_SOCK"] = "/nonexistent/sock"
    return env


def run_pytest(copy: pathlib.Path, *args) -> subprocess.CompletedProcess:
    # __pycache__ を残さない (古い pyc が緑を見せる)
    for cache in copy.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    return sh(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args, cwd=copy, env=pytest_env(), check=False)


def failed_names(output: str) -> list[str]:
    return re.findall(r"^FAILED (\S+)", output, re.M)


def inject(copy: pathlib.Path, rel: str, extra: str) -> None:
    path = copy / rel
    text = path.read_text(encoding="utf-8")
    if rel == "scripts/plan.sh":
        # plan.sh の Python 本体は heredoc の中 (PYEOF の前) に入れないと Python として読まれない
        text = text.replace("\nPYEOF\n", extra + "\nPYEOF\n", 1) if "\nPYEOF\n" in text else text + extra
    else:
        text += extra
    path.write_text(text, encoding="utf-8")


def red_proof_guard() -> bool:
    ok = True
    with tempfile.TemporaryDirectory(prefix="red-proof-guard-") as td:
        base = pathlib.Path(td) / "base"
        make_copy(base)
        p = run_pytest(base, GUARD_TEST, "-k", GUARD_NAME)
        print(f"[guard] 対照 (何も足さない複製): rc={p.returncode} (緑であること)")
        if p.returncode != 0:
            print(p.stdout[-1500:])
            return False
        for label, rel, extra in GUARD_INJECTIONS:
            work = pathlib.Path(td) / "w"
            if work.exists():
                shutil.rmtree(work)
            shutil.copytree(base, work)
            inject(work, rel, extra)
            r = run_pytest(work, GUARD_TEST, "-k", GUARD_NAME)
            names = failed_names(r.stdout)
            red = r.returncode != 0 and any(GUARD_NAME in n for n in names) and "ERROR" not in r.stdout.split("short test summary")[0][-300:]
            print(f"[guard] {'赤 OK ' if red else '赤にならない'}: {label} (rc={r.returncode}, FAILED={names})")
            ok &= red
    return ok


def red_proof_pre_g3() -> bool:
    with tempfile.TemporaryDirectory(prefix="red-proof-pre-g3-") as td:
        copy = pathlib.Path(td) / "pre"
        make_copy(copy)
        for rel in G3_PRODUCTION_FILES:
            old = sh("git", "show", f"{PRE_G3_SHA}:{rel}", cwd=REPO).stdout
            (copy / rel).write_text(old, encoding="utf-8")
        r = run_pytest(copy, *PRE_G3_TEST_FILES)
        out = r.stdout
        collection_errors = re.findall(r"^ERROR .*", out, re.M)
        failed = failed_names(out)
        print(f"[pre-g3] G3 前の本番コードに戻した複製: rc={r.returncode} FAILED={len(failed)} 件 collection-error={len(collection_errors)} 件")
        ok = r.returncode != 0 and not collection_errors
        for name in PRE_G3_EXPECTED_FAILED:
            hit = [f for f in failed if name in f]
            print(f"[pre-g3] {'赤 OK ' if hit else '赤にならない'}: {name} ({len(hit)} 件)")
            ok &= bool(hit)
        return ok


def main(argv) -> int:
    which = argv[1] if len(argv) > 1 else "all"
    results = []
    if which in ("all", "guard"):
        results.append(("guard", red_proof_guard()))
    if which in ("all", "pre-g3"):
        results.append(("pre-g3", red_proof_pre_g3()))
    for name, ok in results:
        print(f"== {name}: {'全部期待どおり' if ok else '期待と違う (上を見る)'}")
    return 0 if all(ok for _n, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
