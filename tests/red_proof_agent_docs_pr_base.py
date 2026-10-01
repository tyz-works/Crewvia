#!/usr/bin/env python3
"""red_proof_agent_docs_pr_base.py — 文書の書き換え (G4 / t016) のテストが、欠陥を戻すと赤になることの実証。

作業ツリーを**使い捨ての複製** (本番の queue・registry・.git は写さない) に写し、文書に欠陥を 1 つ注入して
`tests/test_agent_docs_take_pr_base_from_plan_pr_base.py` と構造ガードを走らせる。赤が「狙ったテスト名の FAILED」であること
(collection error・ImportError を赤と数えない) を確かめる。

    PYTHONDONTWRITEBYTECODE=1 python3 tests/red_proof_agent_docs_pr_base.py

注入する欠陥:
  D1  worker.md の PR base を、1 巡目の案 (`${CREWVIA_PR_BASE:-main}`。env が無いと黙って main) に戻す
  D2  worker.md の `--base "$PR_BASE"` を `--base main` に戻す
  D3  QA の diff の ref を常に `origin/` にする (TARGET_DIR の checkout に origin が無いと落ちる)
  D4  director.md の `plan pr-base` 失敗時の止め (`|| { ...; exit 1; }`) を外す
  D5  worker.md の stacked 判定を `main` との比較に戻す
  D6  verifier.md の diff を `main...HEAD` に戻す
  D7  QA の fetch の失敗を握りつぶす (`|| true`)
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
TARGET = "tests/test_agent_docs_take_pr_base_from_plan_pr_base.py"
GUARD = "tests/test_git_decisions_go_through_policy.py"
COPY_DIRS = ("agents", "skills", "scripts", "hooks", "tests", "config")
COPY_FILES = ("crewvia", "crewvia-stop")


def sub(path, pattern, repl, count=0, flags=0):
    def f(root):
        p = root / path
        text = p.read_text(encoding="utf-8")
        new, n = re.subn(pattern, repl, text, count=count, flags=flags)
        assert n >= 1, f"{path}: {pattern!r} が当たらない (注入に失敗)"
        p.write_text(new, encoding="utf-8")
    return f


DEFECTS = {
    "D1": (sub("agents/worker.md", r'PR_BASE="\$\(plan pr-base\)" \|\| \{[^\n]*\}', 'PR_BASE="${CREWVIA_PR_BASE:-main}"'),
           ["test_pr_create_in_the_worktree_uses_the_missions_pr_base", "test_pr_create_from_the_main_checkout_cwd_still_uses_the_missions_pr_base"]),
    "D2": (sub("agents/worker.md", r'--base "\$PR_BASE"', "--base main"),
           ["test_pr_create_in_the_worktree_uses_the_missions_pr_base", "test_adding_a_literal_to_a_real_file_turns_the_guard_red",
            "test_no_unlisted_git_decision_remains"]),
    "D3": (sub("skills/crewvia-qa/SKILL.md", r'DIFF_REF="\$\(plan pr-base --diff-ref\)"[^\n]*', 'DIFF_REF="origin/$(plan pr-base)"', count=1),
           ["test_qa_diff_of_a_target_dir_task_is_local_main_with_no_origin_remote"]),
    "D4": (sub("agents/director.md", r'(PR_BASE="\$\(plan pr-base --mission [^\n]*?\)") \|\| \{[^\n]*\}', r"\1"),
           ["test_director_reparent_stops_without_calling_gh_pr_edit_when_the_task_is_unknown"]),
    "D5": (sub("agents/worker.md", r'EXPECTED_BASE="\$\(plan pr-base --mission "\$MISSION" --task "\$TID"\)"[^\n]*', 'EXPECTED_BASE=main'),
           ["test_worker_stacked_check_compares_with_the_target_prs_task_not_its_own", "test_worker_stacked_check_stops_when_the_task_line_does_not_name_a_task"]),
    "D6": (sub("agents/verifier.md", r'git diff "\$\{DIFF_REF\}\.\.\.HEAD"', "git diff main...HEAD"),
           ["test_verifier_diff_uses_the_named_form_and_the_same_fetch_check", "test_no_unlisted_git_decision_remains",
            "test_no_agent_doc_tells_the_agent_to_default_to_main_in_a_command"]),
    "D7": (sub("skills/crewvia-qa/SKILL.md", r"(\|\| \{ echo \"\$\{DIFF_REF\} を取れない[^\n]*?\} ;;)", r"|| true ;;", count=1),
           ["test_qa_diff_stops_when_the_fetch_fails_and_does_not_print_a_diff"]),
}


def make_copy(dest: pathlib.Path):
    for d in COPY_DIRS:
        shutil.copytree(REPO / d, dest / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    for f in COPY_FILES:
        if (REPO / f).exists():
            shutil.copy2(REPO / f, dest / f)
    (dest / "knowledge").mkdir()
    for p in (REPO / "knowledge").glob("*.md"):
        shutil.copy2(p, dest / "knowledge" / p.name)


def run(root: pathlib.Path, tests: list[str]):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWVIA_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
                          cwd=root, env=env, capture_output=True, text=True)


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory(prefix="red-proof-agent-docs-") as td:
        base = pathlib.Path(td)
        clean = base / "clean"
        clean.mkdir()
        make_copy(clean)
        r = run(clean, [TARGET, GUARD])
        print(f"[baseline] {r.stdout.strip().splitlines()[-1]}")
        if r.returncode != 0:
            print(r.stdout[-3000:])
            return 1
        for key, (inject, expect) in DEFECTS.items():
            root = base / key
            root.mkdir()
            make_copy(root)
            inject(root)
            for d in root.rglob("__pycache__"):
                shutil.rmtree(d, ignore_errors=True)
            r = run(root, [TARGET, GUARD])
            failed = sorted(set(re.findall(r"^FAILED \S+?::(\w+)", r.stdout, flags=re.M)))
            errors = re.findall(r"^ERROR ", r.stdout, flags=re.M)
            hit = [t for t in expect if t in failed]
            good = r.returncode != 0 and not errors and bool(hit)
            ok &= good
            print(f"[{key}] {'RED (狙った FAILED: ' + ', '.join(hit) + ')' if good else 'NOT RED / 想定外'}  failed={failed} errors={len(errors)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
