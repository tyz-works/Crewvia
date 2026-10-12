#!/usr/bin/env python3
"""tests/red_proof_research_bash_guard.py — research の Bash ガード (t005 / t007) の赤の実証。

t005 の項目 1〜5・t007 (Codex 2 巡目) の項目 6〜9・t009 (Codex 3 巡目) の項目 10〜12 を 1 つずつ戻した複製 (hooks/ と config/skill-permissions.yaml と
tests/test_research_skill_guards.py だけを一時 dir に写したもの) で同じテストを走らせ、
狙ったテストが **assert で** 赤になることを確かめる。本番の worktree・queue には触れない。

  python3 tests/red_proof_research_bash_guard.py        (約 15 分)

各変異は置換元がちょうど 1 回現れることを確かめる (0 回なら BROKEN = 注入点が消えた)。
項目 1 の heredoc の文字検査は、外しても字句解析が本文の行をコマンドとして検査するので deny のまま
(二重に守られている)。赤の実証は「拒否の理由が heredoc の案内 (--result-file) であること」のテストで取る。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEST_FILE = REPO / "tests" / "test_research_skill_guards.py"
GUARD_REL = "hooks/lib_research_guard.py"
HOOK_REL = "hooks/pre-tool-use.sh"

# (名前, [(ファイル, 置換元, 置換先)], 赤になるべきテスト名の断片 / None = 単独では緑の想定)
MUTATIONS = [
    ("1a heredoc の文字検査を外す", [
        (GUARD_REL, '    ("<<", "heredoc', '    ("<<<<<<", "heredoc'),
    ], "test_heredoc_is_rejected_up_front_with_guidance"),
    ("1b done / needs-director の --result-file 検査を外す", [
        (GUARD_REL, '        return  # usage を出すだけで何も書かない\n', '        return  # usage を出すだけで何も書かない\n    return\n'),
    ], "test_plan_result_file_must_be_own_notes"),
    ("2a $ の文字検査を外す", [
        (GUARD_REL, '    ("$", "$ (変数展開', '    ("$$$$$$", "$ (変数展開'),
    ], "test_writes_and_unknown_forms_are_denied_by_the_lib"),
    ("2b # の文字検査を外す", [
        (GUARD_REL, '    ("#", "# (コメント)', '    ("####", "# (コメント)'),
    ], "test_writes_and_unknown_forms_are_denied_by_the_lib"),
    ("2c export / command / env を表に足す (状態変更・ラッパー)", [
        (GUARD_REL, '    "echo": Spec(any_args=True),', '    "echo": Spec(any_args=True),\n    "export": Spec(any_args=True),\n    "command": Spec(any_args=True),\n    "env": Spec(any_args=True),'),
    ], "test_codex_reproductions_are_denied"),
    # 相対パスは cd の後ろで cwd 不明 = 拒否のまま (二重に守られている)。赤は cd の後ろの絶対パスの source で取る
    ("2d 先頭以外の source を許す", [
        (GUARD_REL, '    if index != 0 or depth != 0:\n        raise _Reject("source は', '    if depth != 0:\n        raise _Reject("source は'),
    ], "test_source_is_bound_to_the_own_worktree_env_file"),
    ("3 awk / python3 / xxd を表に戻す (言語を持つツール)", [
        (GUARD_REL, '    "echo": Spec(any_args=True),', '    "echo": Spec(any_args=True),\n    "awk": Spec(any_args=True),\n    "python3": Spec(any_args=True),\n    "xxd": Spec(any_args=True),'),
    ], "test_codex_reproductions_are_denied"),
    ("4a 列挙外の短いオプションを読み飛ばす (denylist 相当に戻す)", [
        (GUARD_REL, '            raise _Reject(f"{label} の -{c} は allowlist にありません ({a!r})")', '            break'),
    ], "test_every_unlisted_option_is_denied_in_split_and_attached_form"),
    ("4b 列挙外の長いオプションを通す", [
        (GUARD_REL, '                raise _Reject(f"{label} の --{name} は allowlist にありません")', '                pass'),
    ], "test_every_unlisted_option_is_denied_in_split_and_attached_form"),
    ("4c gh api に -X / -f / -F を足す", [
        (GUARD_REL, 'GH_SPECS[("api",)] = Spec(short_arg="q",', 'GH_SPECS[("api",)] = Spec(short_arg="qXfF",'),
    ], "test_codex_reproductions_are_denied"),
    ("4d - で始まる値を受け取る (取り違えの防止を外す)", [
        (GUARD_REL, '    if w.value.startswith("-"):\n        raise _Reject(f"{label} の値が - で始まっています', '    if False:\n        raise _Reject(f"{label} の値が - で始まっています'),
    ], "test_writes_and_unknown_forms_are_denied_by_the_lib"),
    ("4e 先頭が glob の語を通す", [
        (GUARD_REL, '            if w.glob_at == 0:\n                raise _Reject(f"{label}: 先頭が glob', '            if False:\n                raise _Reject(f"{label}: 先頭が glob'),
    ], "test_glob_words_cannot_become_options"),
    ("5 card を TASK_ID の fallback 経路からだけ引く (P2 を戻す)", [
        (HOOK_REL, '_RG_CARDS="${_TASK_FILE:-}"\ncase "${AGENT_NAME:-}" in', '_RG_CARDS="${_TASK_FILE:-}"\ncase "/" in'),
    ], "test_card_skills_count_even_when_task_id_is_already_set"),
    # ---- t007 (Codex 2 巡目 P1×4)
    ("6a git の束ね・結合値の拒否を外す (strict を無効に)", [
        (GUARD_REL, 'return Spec(short=short, long=_w(long), long_arg=_w(long_arg), long_opt=_w(long_opt), strict=True, **kw)',
         'return Spec(short=short, long=_w(long), long_arg=_w(long_arg), long_opt=_w(long_opt), strict=False, **kw)'),
    ], "test_git_specs_are_per_subcommand_and_strict"),
    ("6b strict のうち束ねの拒否だけを外す", [
        (GUARD_REL, '        if spec.strict and len(a) > 2:', '        if False:'),
    ], "test_every_unlisted_option_is_denied_in_split_and_attached_form"),
    # 戻しても -LOecho は strict (束ねの拒否) が止めるので、赤は「git grep が表に無い」と DENIED の git grep -n で取る
    ("6c git grep を表に戻す", [
        (GUARD_REL, '    ("version",): _git(max_pos=0),', '    ("version",): _git(max_pos=0),\n    ("grep",): _git(short="nLO"),'),
    ], "test_git_specs_are_per_subcommand_and_strict"),
    ("6d git の大域オプション (--no-pager / -C) を読み飛ばす", [
        (GUARD_REL, '    if sub.startswith("-"):\n        # -c / -C',
         '    if sub in ("--no-pager", "-P", "-C"):\n        return _check_git(words[2 if sub == "-C" else 1:], ctx)\n    if sub.startswith("-"):\n        # -c / -C'),
    ], "test_git_global_options_are_denied"),
    ("7a 引用符つきの数字を fd として読み捨てる (c35cb83 の drop_fd_number に戻す)", [
        (GUARD_REL, '        if c in "<>":\n            raise _Reject("リダイレクトは',
         '        if c == ">" and word.isdigit() and _ALLOWED_REDIRECT.match("2" + cmd[i:]):\n'
         '            i += _ALLOWED_REDIRECT.match("2" + cmd[i:]).end() - 1\n'
         '            word, has_word, glob_at = "", False, -1\n'
         '            continue\n'
         '        if c in "<>":\n            raise _Reject("リダイレクトは'),
    ], "test_codex_reproductions_are_denied"),
    ("7b > /dev/null 一般を通す (2>/dev/null と 2>&1 以外)", [
        (GUARD_REL, '        if c in "<>":\n            raise _Reject("リダイレクトは',
         '        if c == ">" and cmd.startswith(">/dev/null", i):\n'
         '            flush_word()\n'
         '            i += len(">/dev/null")\n'
         '            continue\n'
         '        if c in "<>":\n            raise _Reject("リダイレクトは'),
    ], "test_only_two_exact_redirect_tokens"),
    ("8a plan.sh を basename で判定する (c35cb83 に戻す)", [
        (GUARD_REL, '        if not _is_trusted_path(cmd, ctx, ("scripts", "plan.sh")):', '        if ".." in cmd.split("/"):'),
    ], "test_codex_reproductions_are_denied"),
    ("8b 字面の検査を外し realpath だけで判定する (symlink 経由を通す)", [
        (GUARD_REL, '        if lexical == os.path.join(root, *rel):', '        if real == os.path.join(root_real, *rel):'),   # t009 で .crewvia-env と共通 (10c と同じ注入点)
    ], "test_plan_sh_must_resolve_to_the_trusted_script"),
    ("8c 同じコマンドの中の cd の行き先を cwd として追跡する", [
        (GUARD_REL, '            ctx["cd_seen"] = True\n            ctx["cwd"] = None',
         '            ctx["cd_seen"] = True\n            ctx["cwd"] = args[-1].value if args else None'),
    ], "test_cd_in_the_same_command_does_not_vouch_for_a_relative_plan_sh"),
    ("9 git fetch に --prune / --prune-tags / -p を戻す", [
        (GUARD_REL, '    ("fetch",): _git(long="no-prune no-prune-tags", max_pos=2, check=_check_git_fetch),', '    ("fetch",): _git(short="p", long="no-prune no-prune-tags prune prune-tags", max_pos=2, check=_check_git_fetch),'),
    ], "test_codex_reproductions_are_denied"),
    ("9b git fetch の --no-prune --no-prune-tags 必須を外す (t016)", [
        (GUARD_REL, '    if not {"--no-prune", "--no-prune-tags"} <= keys:', '    if False:'),
    ], "test_git_fetch_only_three_forms"),
    # ---- t009 (Codex 3 巡目 P1×3)
    ("10a source の場所を検査しない (4e8a822 の形: どの cwd の .crewvia-env でも可)", [
        (GUARD_REL, '    if os.path.basename(path) != ".crewvia-env" or not _is_trusted_path(path, ctx, (".crewvia-env",)):',
         '    if os.path.basename(path) != ".crewvia-env":'),
    ], "test_source_is_bound_to_the_own_worktree_env_file"),
    ("10b スラッシュ無しの source .crewvia-env (PATH 探索) を通す", [
        (GUARD_REL, '    if "/" not in path:\n        raise _Reject(', '    if "/" not in path:\n        return\n        raise _Reject('),
    ], "test_codex_reproductions_are_denied"),
    # 字面だけで判定する (worktree 側の realpath の検査を外す) = 自分の worktree に置いた symlink の .crewvia-env を通す。
    # (8b の「字面を外す」は source では効かない: 信頼する .crewvia-env を指す symlink は中身が同じ)
    ("10c source / plan.sh の realpath の検査を外す (symlink の .crewvia-env を通す)", [
        (GUARD_REL, '    return own_worktree_file(real, root_real) and real == os.path.join(root_real, under)', '    return True'),
    ], "test_source_is_bound_to_the_own_worktree_env_file"),
    ("11 printf を表に戻す", [
        (GUARD_REL, '    "echo": Spec(any_args=True),', '    "echo": Spec(any_args=True),\n    "printf": Spec(),'),
    ], "test_codex_reproductions_are_denied"),
    ("12a git diff の必須フラグの検査を外す", [
        (GUARD_REL, '        check=_require_no_ext_programs),   # --no-index', '        ),   # --no-index'),
    ], "test_codex_reproductions_are_denied"),
    ("12b git diff に --no-index を戻す", [
        (GUARD_REL, '"ignore-blank-lines no-ext-diff no-textconv compact-summary', '"ignore-blank-lines no-ext-diff no-textconv no-index compact-summary'),
    ], "test_diffing_git_needs_no_ext_diff_and_no_textconv"),
    ("12c git show の必須フラグの検査を外す", [
        (GUARD_REL, '        long_opt="color decorate unified abbrev pretty word-diff find-renames",\n        check=_require_no_ext_programs),',
         '        long_opt="color decorate unified abbrev pretty word-diff find-renames"),'),
    ], "test_codex_reproductions_are_denied"),
    ("12d git log の差分系オプションでも必須フラグを求めない", [
        (GUARD_REL, '    if any(k not in _LOG_NO_DIFF for k, _ in opts):', '    if False:'),
    ], "test_codex_reproductions_are_denied"),
    ("12e git blame の --no-textconv の要求を外す", [
        (GUARD_REL, '                     long_arg="date", check=_require_no_textconv),', '                     long_arg="date"),'),
    ], "test_default_git_runs_textconv_and_the_required_flags_stop_it"),
    ("13a find の述語の値の glob 検査を先頭だけに戻す", [
        (GUARD_REL, '                if v.glob_at >= 0:\n                    raise _Reject(f"find {a} の値に', '                if v.glob_at == 0:\n                    raise _Reject(f"find {a} の値に'),
    ], "test_writes_and_unknown_forms_are_denied_by_the_lib"),
    ("13b find の始点の glob 検査を外す", [
        (GUARD_REL, '        if w.glob_at >= 0:\n            raise _Reject("find の始点に', '        if False:\n            raise _Reject("find の始点に'),
    ], "test_writes_and_unknown_forms_are_denied_by_the_lib"),
]


def _prepare(root: Path) -> None:
    shutil.copytree(REPO / "hooks", root / "hooks")
    (root / "config").mkdir()
    shutil.copy(REPO / "config" / "skill-permissions.yaml", root / "config" / "skill-permissions.yaml")
    (root / "tests").mkdir()
    shutil.copy(TEST_FILE, root / "tests" / TEST_FILE.name)


def _apply(root: Path, edits) -> str | None:
    for rel, old, new in edits:
        p = root / rel
        text = p.read_text()
        n = text.count(old)
        if n != 1:
            return f"BROKEN: {rel} の置換元が {n} 回 ({old[:50]!r})"
        p.write_text(text.replace(old, new))
    return None


def _run(root: Path) -> tuple[int, set[str], str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for k in list(env):
        if k.startswith("CREWVIA_") or k in ("AGENT_NAME", "TASK_ID", "SKILLS"):
            env.pop(k)
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(root),
         str(root / "tests" / TEST_FILE.name)],
        capture_output=True, text=True, cwd=str(root), env=env, timeout=900,
    )
    failed = set(re.findall(r"^FAILED \S+::(\w+)", p.stdout, re.M))
    return p.returncode, failed, p.stdout[-400:]


def main() -> int:
    results = []
    with tempfile.TemporaryDirectory(prefix="rp-research-guard-") as base:
        base_p = Path(base)
        clean = base_p / "clean"
        _prepare(clean)
        rc, failed, tail = _run(clean)
        print(f"[clean] rc={rc} failed={sorted(failed)}")
        if rc != 0:
            print(tail)
            return 1
        for i, (name, edits, expect) in enumerate(MUTATIONS):
            root = base_p / f"m{i}"
            _prepare(root)
            broken = _apply(root, edits)
            if broken:
                results.append((name, broken, False))
                continue
            rc, failed, _ = _run(root)
            if expect is None:
                ok = rc == 0
                verdict = "GREEN (想定どおり: 後段の層が止める)" if ok else f"RED {sorted(failed)}"
            else:
                ok = rc != 0 and any(f.startswith(expect) for f in failed)
                verdict = f"RED {sorted(failed)[:6]}{' …' if len(failed) > 6 else ''} ({len(failed)} 件)" if rc else "GREEN"
            results.append((name, verdict, ok))
    print()
    for name, verdict, ok in results:
        print(f"{'OK ' if ok else 'NG '} {name}: {verdict}")
    return 0 if all(ok for _, _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
