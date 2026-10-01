#!/usr/bin/env python3
"""red_proof_agent_docs_pr_base.py — 文書の書き換え (G4 / t016) のテストが、欠陥を戻すと赤になることの実証。

作業ツリーを**使い捨ての複製** (本番の queue・registry・.git は写さない) に写し、文書に欠陥を 1 つ注入して
`tests/test_agent_docs_take_pr_base_from_plan_pr_base.py` と構造ガードを走らせる。赤が「狙ったテスト名の FAILED」で、
**落ちた理由がテストの assert (`AssertionError` / `assert ...`) で、欠陥ごとに決めた assert の識別子 (`why`) を含む**ことを runner が機械的に確かめる
(collection error・ImportError・文書の切り出しの失敗 (`SnippetNotFound`) で落ちたものは赤と数えない。t029)。

    PYTHONDONTWRITEBYTECODE=1 python3 tests/red_proof_agent_docs_pr_base.py

注入する欠陥:
  D1  worker.md の PR base を、1 巡目の案 (`${CREWVIA_PR_BASE:-main}`。env が無いと黙って main) に戻す
  D2  worker.md の `--base "$PR_BASE"` を `--base main` に戻す
  D3  QA の diff の ref を常に `origin/` にする (TARGET_DIR の checkout に origin が無いと落ちる)
  D4  director.md の `plan pr-base` 失敗時の止め (`|| { ...; exit 1; }`) を外す
  D5  worker.md の stacked 判定を `main` との比較に戻す
  D6  verifier.md の diff を `main...HEAD` に戻す
  D7  QA の fetch の失敗を握りつぶす (`|| true`)
  D8  QA / verifier の fetch を、設定の refspec 任せの `git fetch origin <branch>` に戻す (--single-branch の clone で古い / 無い ref を見る)
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


def sub(path, pattern, repl, count=0, flags=0, extra=None):
    """`path` に欠陥を注入する。`extra=(path, pattern, repl)` は同じ欠陥の 2 か所目 (同じ手順の写し)。"""
    def f(root):
        for pth, pat, rp in ((path, pattern, repl), *([extra] if extra else [])):
            p = root / pth
            text = p.read_text(encoding="utf-8")
            new, n = re.subn(pat, rp, text, count=count, flags=flags)
            assert n >= 1, f"{pth}: {pat!r} が当たらない (注入に失敗)"
            p.write_text(new, encoding="utf-8")
    return f


# 各欠陥: (注入, [(落ちるべきテスト名, 落ちた理由に要求する正規表現)])。理由は pytest の `FAILED <id> - <reason>` の 1 行目。
# `AssertionError` で始まらない理由 (SnippetNotFound・ImportError・collection error …) は、テスト名が合っていても赤と数えない。
DEFECTS = {
    "D1": (sub("agents/worker.md", r'PR_BASE="\$\(plan pr-base\)" \|\| \{[^\n]*\}', 'PR_BASE="${CREWVIA_PR_BASE:-main}"'),
           [("test_pr_create_in_the_worktree_uses_the_missions_pr_base", r"'main' == 'develop'"),
            # 次の assert は cwd のメッセージが付き、1 行目に値が出ない
            ("test_pr_create_from_the_main_checkout_cwd_still_uses_the_missions_pr_base", None)]),
    "D2": (sub("agents/worker.md", r'--base "\$PR_BASE"', "--base main"),
           [("test_pr_create_in_the_worktree_uses_the_missions_pr_base", r"'main' == 'develop'"),
            ("test_adding_a_literal_to_a_real_file_turns_the_guard_red", None),
            ("test_no_unlisted_git_decision_remains", None)]),
    "D3": (sub("skills/crewvia-qa/SKILL.md", r'DIFF_REF="\$\(plan pr-base --diff-ref\)"[^\n]*', 'DIFF_REF="origin/$(plan pr-base)"', count=1),
           [("test_qa_diff_of_a_target_dir_task_is_local_main_with_no_origin_remote", None)]),
    "D4": (sub("agents/director.md", r'(PR_BASE="\$\(plan pr-base --mission [^\n]*?\)") \|\| \{[^\n]*\}', r"\1"),
           [("test_director_reparent_stops_without_calling_gh_pr_edit_when_the_task_is_unknown", None)]),
    "D5": (sub("agents/worker.md", r'EXPECTED_BASE="\$\(plan pr-base --mission "\$MISSION" --task "\$TID"\)"[^\n]*', 'EXPECTED_BASE=main'),
           [("test_worker_stacked_check_compares_with_the_target_prs_task_not_its_own", None),
            ("test_worker_stacked_check_stops_when_the_task_line_does_not_name_a_task", None)]),
    "D6": (sub("agents/verifier.md", r'git diff "\$\{DIFF_REF\}\.\.\.HEAD"', "git diff main...HEAD"),
           [("test_no_unlisted_git_decision_remains", None),
            ("test_no_agent_doc_tells_the_agent_to_default_to_main_in_a_command", None)]),
    "D7": (sub("skills/crewvia-qa/SKILL.md", r"(\|\| \{ echo \"\$\{DIFF_REF\} を取れない[^\n]*?\} ;;)", r"|| true ;;", count=1),
           [("test_qa_diff_stops_when_the_fetch_fails_and_does_not_print_a_diff", None)]),
    "D8": (sub("skills/crewvia-qa/SKILL.md", r'"\+refs/heads/\$\{DIFF_REF#origin/\}:refs/remotes/\$\{DIFF_REF\}"', '"${DIFF_REF#origin/}"', count=1, extra=(
                "agents/verifier.md", r'"\+refs/heads/\$\{DIFF_REF#origin/\}:refs/remotes/\$\{DIFF_REF\}"', '"${DIFF_REF#origin/}"')),
           [("test_diff_base_fetch_reaches_the_remote_tip_in_a_clone_whose_fetch_refspec_is_narrowed",
             r"手順が取れた branch を拒否した|origin の先端でない")]),
}


def is_assertion(reason: str) -> bool:
    """pytest は assert の失敗を、メッセージ無しなら `assert 0 == 1`、ありなら `AssertionError: ...` と書く。どちらもテストの assert。"""
    return reason.startswith("AssertionError") or reason.startswith("assert ")


def failed_reasons(out: str) -> dict[str, list[str]]:
    """`-rf` の short summary (`FAILED path::name[param] - reason`) から {name: [reason の 1 行目]}。reason が無い行は ''。"""
    found: dict[str, list[str]] = {}
    for m in re.finditer(r"^FAILED \S+?::(\w+)(?:\[[^\]]*\])?(?: - (.*))?$", out, flags=re.M):
        found.setdefault(m.group(1), []).append((m.group(2) or "").strip())
    return found


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
    env["COLUMNS"] = "500"      # `FAILED <id> - <reason>` の reason を切り詰めさせない
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf", *tests],
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
            reasons = failed_reasons(r.stdout)           # {テスト名: [理由, ...]} (parametrize は名前でまとめる)
            errors = re.findall(r"^ERROR ", r.stdout, flags=re.M)
            hit, miss = [], []
            for test, why in expect:
                ok_reasons = [x for x in reasons.get(test, []) if is_assertion(x) and (why is None or re.search(why, x))]
                (hit if ok_reasons else miss).append(test)
            not_assert = sorted({f"{n}: {x[:60]}" for n, xs in reasons.items() for x in xs if not is_assertion(x)})
            good = r.returncode != 0 and not errors and bool(hit) and not not_assert
            ok &= good
            detail = f"failed={sorted(reasons)} errors={len(errors)}"
            if not_assert:
                detail += f" assert 以外で落ちた={not_assert}"
            print(f"[{key}] {'RED (狙った assert で FAILED: ' + ', '.join(hit) + ')' if good else 'NOT RED / 想定外'}  {detail}")
            if miss and good:
                print(f"      (注: 次は落ちなかった / 理由が違った: {', '.join(miss)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
