#!/usr/bin/env python3
"""tests/red_proof_resolve_pr.py — `plan.sh resolve-pr` (t006) の各規則を外した変異で、
狙ったテストが赤になる実証。

    python3 tests/red_proof_resolve_pr.py            # 全部 (約 1 分)
    python3 tests/red_proof_resolve_pr.py M01 M03     # 指定した変異だけ

やること (`tests/red_proof_git_policy_resolver.py` と同じ型):

1. リポジトリを丸ごと一時ディレクトリに写す (`.git` / `.claude` / `queue` / `registry` は除く。
   本番には触れない)。
2. **変異なし**で `tests/test_resolve_pr.py` が全部緑であることを確かめる (対照)。
3. 各変異 (`scripts/plan.sh` の `cmd_resolve_pr` の 1 か所を外す) を写しに当て、
   **狙ったテスト名が FAILED** になることを確かめる。collection error / import error は
   赤と数えない (変異が壊れているだけで、留め金の実証にならない)。

置換元は**ちょうど 1 回**現れなければ変異自体を失敗 (BROKEN) にする。
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
LIB = "scripts/plan.sh"
TEST = "tests/test_resolve_pr.py"

# (id, 説明, 置換元, 置換先, 赤になるべきテストの名前 (部分一致のどれか 1 つ以上が FAILED))
MUTATIONS = [
    ("M01", "PR_PROPAGATION_SKILLS の除外を外す (伝播した codex-review/review の card も候補にする)",
     "            if set(meta.get('skills') or []) & set(PR_PROPAGATION_SKILLS):\n"
     "                continue\n",
     "",
     ["test_a_downstream_review_task_that_got_the_same_pr_number_by_propagation_is_not_a_candidate",
      "test_a_downstream_codex_review_task_is_also_not_a_candidate",
      "test_a_downstream_review_task_without_deliverable_declared_upstream_is_still_not_a_candidate"]),
    ("M02", "2 件以上 (本物の曖昧) を拒否せず先頭を返す (fail-open)",
     "    if len(candidates) != 1:\n"
     "        die(f\"resolve-pr: PR #{pr_number} の task を一意に決められません ({len(candidates)} 件: {candidates})\")\n",
     "    if not candidates:\n"
     "        die(f\"resolve-pr: PR #{pr_number} の task を一意に決められません ({len(candidates)} 件: {candidates})\")\n",
     ["test_two_implementation_tasks_sharing_the_same_pr_number_is_ambiguous"]),
    ("M03", "退避済み (archive) の mission も探す",
     "    for slug in list(state.get('active_missions') or []):\n"
     "        for meta, _body in list_tasks(slug, quiet=True):\n",
     "    for slug, _bd in ([(s, None) for s in (state.get('active_missions') or [])] +\n"
     "                      [(s, os.path.join(ARCHIVE_DIR, s, 'tasks')) for s in\n"
     "                       (os.listdir(ARCHIVE_DIR) if os.path.isdir(ARCHIVE_DIR) else [])]):\n"
     "        for meta, _body in list_tasks(slug, quiet=True, base_dir=_bd):\n",
     ["test_archived_missions_are_not_searched"]),
    ("M04", "PR 番号が 0 以下でも通す (正の整数の検査を外す)",
     "    if pr_number <= 0:\n"
     "        die(f\"resolve-pr: PR 番号は正の整数にしてください: {pr_number}\")\n",
     "",
     ["test_rejects_a_non_positive_or_non_integer_pr_number"]),
    ("M05", "target_dir の絞り込みを外す (別 repo の同番号 PR を取り違える。review P2)",
     "            if (meta.get('target_dir') or None) != effective_target:\n"
     "                continue\n",
     "",
     ["test_a_same_numbered_pr_in_a_different_target_dir_repo_is_not_a_candidate",
      "test_target_dir_env_var_scopes_like_the_flag"]),
    ("M06", "deliverable: pr の宣言を再び要求する (deliverable_required の無い古い mission の退行を再発させる。review P3-1)",
     "            if meta.get('pr_number') != pr_number:\n"
     "                continue\n",
     "            if meta.get('pr_number') != pr_number or meta.get('deliverable') != 'pr':\n"
     "                continue\n",
     ["test_resolves_without_a_deliverable_declaration_on_a_mission_without_deliverable_required",
      "test_a_downstream_review_task_without_deliverable_declared_upstream_is_still_not_a_candidate"]),
    ("M07", "--target-dir が渡されて空のとき省略扱いに戻す (別 repo の同番号 PR の task を成功で返す。#339 の Codex P2)",
     "    if '--target-dir' in opts:\n"
     "        explicit_td = opts['--target-dir']\n"
     "        if not explicit_td.strip():\n"
     "            _usage_exit(\"resolve-pr: --target-dir に空の値は渡せません \"\n"
     "                        \"(省略すれば TARGET_DIR / crewvia 本体を見ます)\")\n"
     "        effective_target",
     "    explicit_td = opts.get('--target-dir')\n"
     "    if explicit_td:\n"
     "        effective_target",
     ["test_an_empty_target_dir_flag_is_refused_not_treated_as_omitted"]),
]


def _copy_tree(dest: pathlib.Path) -> None:
    """リポジトリを丸ごと写す (.git・.claude・queue・registry・logs は除く。本番の状態は写さない)。"""
    ignore = shutil.ignore_patterns(".git", ".claude", "queue", "registry", "logs", "__pycache__", "*.pyc",
                                    "node_modules")
    shutil.copytree(REPO, dest, ignore=ignore, dirs_exist_ok=True)


def _purge_pyc(root: pathlib.Path) -> None:
    for p in root.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWVIA_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(root: pathlib.Path, k_expr: str | None):
    _purge_pyc(root)
    cmd = [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider", "-rfE"]
    if k_expr:
        cmd += ["-k", k_expr]
    r = subprocess.run(cmd, cwd=root, env=_env(), capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    failed = set(re.findall(r"^FAILED \S+?::(\S+?)(?:\[| |$)", out, re.M))
    errors = re.findall(r"^ERROR .*$", out, re.M)
    return r.returncode, failed, errors, out


def main(argv) -> int:
    wanted = set(argv[1:])
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    results = []
    with tempfile.TemporaryDirectory(prefix="red-proof-resolve-pr-") as tmp:
        base = pathlib.Path(tmp) / "base"
        base.mkdir()
        _copy_tree(base)
        rc, failed, errors, out = _run(base, None)
        if rc != 0 or failed or errors:
            print("対照 (変異なし) が緑でない。変異の実証に進めない:\n" + out[-3000:])
            return 1
        m = re.search(r"(\d+) passed", out)
        print(f"対照 (変異なし): 緑 ({m.group(1) if m else '?'} passed)")

        for mid, desc, old, new, targets in selected:
            work = pathlib.Path(tmp) / mid
            shutil.copytree(base, work)
            lib = work / LIB
            src = lib.read_text()
            if src.count(old) != 1:
                results.append((mid, desc, "BROKEN", f"置換元が {src.count(old)} 回現れる (ちょうど 1 回でなければならない)"))
                shutil.rmtree(work, ignore_errors=True)
                continue
            lib.write_text(src.replace(old, new))
            k = " or ".join(targets)
            rc, failed, errors, out = _run(work, k)
            hit = sorted(f for f in failed if any(t in f for t in targets))
            if errors or "SyntaxError" in out or "ImportError" in out:
                results.append((mid, desc, "BROKEN", "collection error / import error (変異が壊れている)\n" + out[-1500:]))
            elif hit:
                results.append((mid, desc, "RED", ", ".join(hit)))
            else:
                results.append((mid, desc, "GREEN", "狙ったテストが赤にならない (留め金になっていない)"))
            shutil.rmtree(work, ignore_errors=True)

    print()
    for mid, desc, verdict, detail in results:
        print(f"{mid} {verdict:6} {desc}\n       -> {detail}")
    bad = [r for r in results if r[2] != "RED"]
    print(f"\n変異 {len(results)} 件: RED {len(results) - len(bad)} / それ以外 {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
