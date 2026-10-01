#!/usr/bin/env python3
"""tests/red_proof_git_policy_resolver.py — Resolver の各拒否を外した変異で、狙ったテストが赤になる実証 (t008)。

    python3 tests/red_proof_git_policy_resolver.py            # 全部 (約 3〜4 分)
    python3 tests/red_proof_git_policy_resolver.py M01 M10    # 指定した変異だけ

やること:

1. `scripts/` と `tests/` だけを一時ディレクトリに写す (本番の worktree・queue・registry には触れない。
   テストは自分で tmp の queue と使い捨ての git repo を作る)。
2. **変異なし**で `tests/test_git_policy_resolver.py` が全部緑であることを確かめる (対照)。
3. 各変異 (`lib_git_policy.py` の拒否を 1 つ外す) を写しに当て、**狙ったテスト名が FAILED** になることを確かめる。
   赤は「狙ったテスト名の FAILED」だけを数える: collection error・ImportError・SyntaxError は赤と数えない
   (変異が壊れているだけで、拒否の留め金を実証していない)。
4. `PYTHONDONTWRITEBYTECODE=1` と `__pycache__` の掃除つき (古い pyc が緑を見せる)。

変異の対象は `lib_git_policy.py` の 1 か所の文字列で、置換元が**ちょうど 1 回**現れなければ変異自体を失敗にする
(Resolver を書き換えたのに変異が空振りしている、を見逃さない)。
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
LIB = "scripts/lib_git_policy.py"
TEST = "tests/test_git_policy_resolver.py"

# (id, 説明, 置換元, 置換先, 赤になるべきテストの名前 (部分一致のどれか 1 つ以上が FAILED))
MUTATIONS = [
    ("M01", "未実装 mode を通す (unsupported_mode を外す)",
     '    if value != DEFAULT_MODE:\n        raise GitPolicyError("unsupported_mode"',
     '    if False:\n        raise GitPolicyError("unsupported_mode"',
     ["test_unsupported_mode_is_refused_not_defaulted", "test_policy_rejections"]),
    ("M02", "`git:` が mapping でないとき既定値に倒す (git: null / スカラー / リスト)",
     '    if not isinstance(block, dict) or not block:\n',
     '    if not isinstance(block, dict) or not block:\n        return default_policy()\n',
     ["test_policy_rejections"]),
    ("M03", "字下げ行の数と読めた欄の数の突き合わせを外す (4 字下げ・空行・コメントの読み飛ばし)",
     '    if unparsed is not None:\n',
     '    if False:\n',
     ["test_policy_rejections", "test_a_rejection_never_degrades_to_default_even_when_other_fields_are_fine",
      "test_a_line_parse_yaml_skipped_is_never_concluded_as_no_git"]),
    ("M04", "未知の欄を通す (unknown_key を外す)",
     '        if key not in POLICY_FIELDS:\n',
     '        if False:\n',
     ["test_policy_rejections", "test_resolver_has_no_notion_of_target_dir_so_it_never_returns_a_worktree_path_for_it"]),
    ("M05", "文字列でない値を文字列に読み替える (type を外す)",
     '    if not isinstance(value, str):\n        raise GitPolicyError("type"',
     '    value = str(value)\n    if not isinstance(value, str):\n        raise GitPolicyError("type"',
     ["test_policy_rejections"]),
    ("M06", "制御文字の検査を外す (欄の値と branch 規則の両方)",
     '    if _CONTROL_RE.search(value):\n        raise GitPolicyError("invalid_value", field, f"制御文字を含む: {value!r}")\n',
     '',
     ["test_every_field_refuses_empty_whitespace_and_control_chars_with_invalid_value"]),
    ("M07", "空文字・前後の空白の検査と branch 規則の「空」を外す (2 層とも)",
     '    if value == "":\n        raise GitPolicyError("invalid_value", field, "空文字")\n'
     '    if value != value.strip():\n        raise GitPolicyError("invalid_value", field, f"前後に空白がある: {value!r}")\n',
     '    if value == "":\n        return value\n',
     ["test_every_field_refuses_empty_whitespace_and_control_chars_with_invalid_value"]),
    ("M08", "worktree_root の `..` / 絶対パス / ~ の検査を外す",
     '    if value.startswith("/") or re.match(r"^[A-Za-z]:[\\\\/]", value):',
     '    if False:',
     ["test_worktree_root_escape_is_refused", "test_policy_rejections"]),
    ("M09", "worktree_root を既定値以外も通す (unsupported_value を外す)",
     '    if value != DEFAULT_WORKTREE_ROOT:\n',
     '    if False:\n',
     ["test_worktree_root_other_than_default_is_unsupported", "test_policy_rejections"]),
    ("M10", "branch 名の「英数字で始まる」を外す",
     '_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")',
     '_COMPONENT_RE = re.compile(r"[A-Za-z0-9._-]+")',
     ["test_our_rule_is_a_subset_of_git_check_ref_format", "test_boundary_table_matches_the_design_and_real_git"]),
    ("M11", "branch 名の「`.lock` で終わらない」を外す",
     '        if part.endswith(".lock"):\n',
     '        if False:\n',
     ["test_our_rule_is_a_subset_of_git_check_ref_format", "test_boundary_table_matches_the_design_and_real_git"]),
    ("M12", "branch 名の「`.` で終わらない」を外す",
     '        if part.endswith("."):\n',
     '        if False:\n',
     ["test_boundary_table_matches_the_design_and_real_git", "test_invalid_branch_names_are_refused_for_base_and_pr_base"]),
    ("M13", "branch 名の「`..` を含まない」を外す",
     '        if ".." in part:\n',
     '        if False:\n',
     ["test_our_rule_is_a_subset_of_git_check_ref_format", "test_boundary_table_matches_the_design_and_real_git"]),
    ("M14", "branch 名の最初の成分 refs / origin の拒否を外す",
     '    if parts[0] in _RESERVED_FIRST_COMPONENTS:\n',
     '    if False:\n',
     ["test_boundary_table_matches_the_design_and_real_git", "test_invalid_task_branch_pattern_is_refused"]),
    ("M15", "branch 名の HEAD の拒否を外す",
     '    if name == "HEAD":\n',
     '    if False:\n',
     ["test_boundary_table_matches_the_design_and_real_git", "test_our_rule_is_a_subset_of_git_check_ref_format"]),
    ("M16", "pattern の必須の置換子 ({mission_slug} / {task_id}) の検査を外す",
     '        if need not in names:\n',
     '        if False:\n',
     ["test_invalid_task_branch_pattern_is_refused"]),
    ("M17", "pattern の未知の置換子 ({agent} 等) を通す",
     '        if n not in PLACEHOLDERS:\n',
     '        if False:\n',
     ["test_invalid_task_branch_pattern_is_refused"]),
    ("M18", "task_id が tNNN の形かの検査を外す",
     '    if not TASK_ID_RE.fullmatch(task_id):\n',
     '    if False:\n',
     ["test_task_id_must_be_tnnn"]),
    ("M19", "置換子に入る値の `/` の拒否を外す (path traversal)",
     '    if "/" in value:\n        raise GitPolicyError("invalid_value", field, f"/ を含む',
     '    if False:\n        raise GitPolicyError("invalid_value", field, f"/ を含む',
     ["test_path_components_cannot_traverse", "test_invalid_substituted_components_are_refused"]),
    ("M20", "worktree path が repo_root の外に出る検査 (realpath) を外す",
     '    if not inside:\n',
     '    if False:\n',
     ["test_worktree_path_must_stay_inside_repo_root_even_through_symlink"]),
    ("M21", "repo_root が絶対パスかの検査を外す",
     '    if not isinstance(repo_root, str) or not os.path.isabs(repo_root):\n',
     '    if False:\n',
     ["test_repo_root_must_be_absolute_and_symlinked_root_is_fine",
      "test_resolver_refuses_instead_of_falling_back_to_another_checkout"]),
    ("M22", "mission.yaml が読めない (Unreadable) とき既定値に倒す",
     '    if is_unreadable(text):\n',
     '    if is_unreadable(text):\n        return default_policy()\n',
     ["test_missing_mission_yaml_is_refused_and_distinguishable", "test_unreadable_mission_yaml_is_refused_not_defaulted",
      "test_mission_yaml_that_is_a_directory_or_not_utf8_is_refused"]),
    ("M23", "既定の branch の形を変える (現行との互換が崩れる)",
     'DEFAULT_TASK_BRANCH_PATTERN = "task/{mission_slug}/{task_id}-{task_slug}"',
     'DEFAULT_TASK_BRANCH_PATTERN = "task/{mission_slug}/{task_id}_{task_slug}"',
     ["test_default_branch_and_path_equal_the_real_git_helpers", "test_policy_defaults_when_git_key_is_absent"]),
    ("M24", "origin/<base> があっても local に倒す (base の選択を壊す)",
     '    if remote_tracking_exists:\n',
     '    if False:\n',
     ["test_task_base_prefers_remote_tracking_and_falls_back_to_local"]),
    ("M25", "mission slug を load 時に検査しない (`a/b` で別の dir を読む)",
     '    if (not isinstance(slug, str) or slug == "" or "/" in slug or "\\\\" in slug\n'
     '            or slug in (".", "..") or _CONTROL_RE.search(slug)):\n',
     '    if False:\n',
     ["test_load_refuses_a_slug_that_is_not_one_component"]),
    ("M26", "`git:` が 2 つあるのを通す (parse_yaml が上書きして飛ばした行の検出を外す)",
     '    if unparsed is not None:\n',
     '    if False:\n',
     ["test_policy_rejections"]),
    ("M30", "pattern の置換子の直後の区切りの検査を外す (別の task が同じ branch になる。Codex 3 巡目 P2-1)",
     '    _check_pattern_boundaries(value, field)\n',
     '',
     ["test_a_branch_pattern_without_a_separator_is_refused",
      "test_the_colliding_pair_from_the_review_is_refused_at_validation"]),
    ("M27", "字下げされた・孤立した git キーの検出を外す (既定値に倒れる。Codex 2 巡目 P2-1)",
     '    stray = _stray_git_key_line(text)\n    if stray is not None:\n',
     '    stray = None\n    if stray is not None:\n',
     ["test_an_indented_or_orphan_git_key_is_never_the_default_policy",
      "test_no_input_has_a_git_key_somewhere_and_the_default_policy"]),
    ("M28", "解析エラーの理由に parser の例外の全文 (行の中身) を入れる (Codex 2 巡目 P2-2)",
     '        parse_error = f"{m.group(1)} 行目が解析できない" if m else "解析できない"\n',
     '        parse_error = str(e)\n',
     ["test_parse_failure_reports_only_the_line_number", "test_no_error_path_leaks_other_fields_or_line_contents",
      "test_cli_stderr_never_carries_other_fields_or_line_contents"]),
    ("M29", "解析エラーに元の例外を連鎖させる (`__cause__` に行の中身が残る)",
     '        raise GitPolicyError("malformed", "mission.yaml", f"読めない: {parse_error}")\n',
     '        raise GitPolicyError("malformed", "mission.yaml", f"読めない: {parse_error}") from ValueError(text)\n',
     ["test_no_error_path_leaks_other_fields_or_line_contents"]),
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
    r = subprocess.run(cmd, cwd=root, env=_env(), capture_output=True, text=True, timeout=900)
    out = r.stdout + r.stderr
    failed = set(re.findall(r"^FAILED \S+?::(\S+?)(?:\[| |$)", out, re.M))
    errors = re.findall(r"^ERROR .*$", out, re.M)
    return r.returncode, failed, errors, out


def main(argv) -> int:
    wanted = set(argv[1:])
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    results = []
    with tempfile.TemporaryDirectory(prefix="red-proof-git-policy-") as tmp:
        base = pathlib.Path(tmp) / "base"
        base.mkdir()
        _copy_tree(base)
        rc, failed, errors, out = _run(base, None)
        if rc != 0 or failed or errors:
            print("対照 (変異なし) が緑でない。変異の実証に進めない:\n" + out[-3000:])
            return 1
        print(f"対照 (変異なし): 緑 ({re.search(r'(\d+) passed', out).group(1)} passed)")

        for mid, desc, old, new, targets in selected:
            work = pathlib.Path(tmp) / mid
            shutil.copytree(base, work)
            lib = work / LIB
            src = lib.read_text()
            if src.count(old) != 1:
                results.append((mid, desc, "BROKEN", f"置換元が {src.count(old)} 回現れる (ちょうど 1 回でなければならない)"))
                continue
            lib.write_text(src.replace(old, new))
            k = " or ".join(targets)
            rc, failed, errors, out = _run(work, k)
            hit = sorted(f for f in failed if any(t in f for t in targets))
            if errors or "SyntaxError" in out or "ImportError" in out:
                results.append((mid, desc, "BROKEN", "collection error / import error (変異が壊れている)"))
            elif hit:
                results.append((mid, desc, "RED", ", ".join(hit)))
            else:
                results.append((mid, desc, "GREEN", "狙ったテストが赤にならない (留め金になっていない)"))
            shutil.rmtree(work, ignore_errors=True)

    print()
    width = max(len(r[1]) for r in results) if results else 0
    for mid, desc, verdict, detail in results:
        print(f"{mid} {verdict:6} {desc}\n       -> {detail}")
    bad = [r for r in results if r[2] != "RED"]
    print(f"\n変異 {len(results)} 件: RED {len(results) - len(bad)} / それ以外 {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
