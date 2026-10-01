#!/usr/bin/env python3
"""lib_git_policy.py — Git Policy Resolver (vNext 01b G2 / t008)。

task の branch 名・worktree の path・base・PR base を決める**唯一の場所**。設計は
`knowledge/git-policy.md` §2 (schema) / §3 (API)。

- **判断だけ**を持つ。git も gh も呼ばない (subprocess なし)。観測 (`git show-ref` / `git worktree list`) と
  副作用 (fetch / worktree add) は呼び出し元が持つ。
- mission.yaml の `git:` 欄の読み口は `lib_task_cards` の入口 (`read_regular_text_or_unreadable` +
  `parse_yaml`)。**読めない (`Unreadable`) を既定値に倒さない** (不変条件 1)。
- 拒否はすべて `GitPolicyError(code, field, detail)`。**黙って既定値に倒さない** (未実装 mode・未知の欄・
  不正な値・読み飛ばされた字下げ行)。`git:` キーが**無い**ときだけが既定値 (GIT-01。今ある全 mission の形)。
- **G2 は呼び出し元ゼロ** (R2)。plan.sh / git-helpers.sh が import するのは G3 (cutover。ユーザー承認が要る)。
  env の停止スイッチは付けない (不変条件 5)。

CLI (bash の呼び出し元向け。G3 から。出力は 1 行の JSON。拒否は exit 2・stderr に code と detail):

    python3 scripts/lib_git_policy.py resolve-task --queue <dir> --mission <slug> --task <tid> \\
        --task-slug <s> --repo-root <dir>
    python3 scripts/lib_git_policy.py pr-base --queue <dir> --mission <slug>
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_task_cards import (  # noqa: E402
    TASK_ID_RE,
    is_missing,
    is_unreadable,
    parse_yaml,
    read_regular_text_or_unreadable,
)

DEFAULT_MODE = "direct"
DEFAULT_BASE_BRANCH = "main"
DEFAULT_PR_BASE = "main"
DEFAULT_TASK_BRANCH_PATTERN = "task/{mission_slug}/{task_id}-{task_slug}"
DEFAULT_WORKTREE_ROOT = ".claude/worktrees"
#: remote 名は policy にしない (CI・skill-permissions・sync-main-checkout が origin 前提)。
REMOTE = "origin"

#: `git:` の下に書ける欄。これ以外は `unknown_key` (綴り間違いを黙って既定値にしない)。
POLICY_FIELDS = ("mode", "base_branch", "pr_base", "task_branch_pattern", "worktree_root")

#: pattern の置換子。`{mission_slug}` と `{task_id}` は必須 (task ごとに branch が 1 本、を pattern の側で保証する。
#: `{agent}` や試行ごとの branch は書けない)。
PLACEHOLDERS = ("mission_slug", "task_id", "task_slug")
REQUIRED_PLACEHOLDERS = ("mission_slug", "task_id")

ERROR_CODES = ("malformed", "unknown_key", "type", "invalid_value",
               "unsupported_mode", "unsupported_value", "unreadable")

MAX_BRANCH_LENGTH = 200

# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class GitPolicyError(Exception):
    """Policy を決められない。`code` は `ERROR_CODES` のどれか。

    `detail` には欄の値を入れてよい。mission.yaml の他の欄は入れない (理由が card に書かれる)。
    """

    def __init__(self, code: str, field: str, detail: str):
        assert code in ERROR_CODES, code
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(f"git policy [{code}] {field}: {detail}")


class MissionNotFound(GitPolicyError):
    """mission.yaml が**本当に無い** (ENOENT)。今の `load_mission` と同じく呼び出し元のエラーとして扱える。

    `code` は `unreadable` のまま (読めなかったことに変わりはない。既定値に倒さない)。
    """


# ---------------------------------------------------------------------------
# 値の型
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GitPolicy:
    mode: str
    base_branch: str
    pr_base: str
    task_branch_pattern: str
    worktree_root: str
    source: str  # "default" | "mission"


@dataclasses.dataclass(frozen=True)
class TaskBase:
    ref: str       # "origin/main" か、fallback のときは "main"
    fallback: bool  # local の branch に倒したか (呼び出し元が警告を出す)


# ---------------------------------------------------------------------------
# 文字列の検査 (branch 名の規則 = git check-ref-format より狭い許可集合。設計 §2.3)
# ---------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RESERVED_FIRST_COMPONENTS = ("refs", REMOTE)


def _check_text(value, field: str) -> str:
    """欄の値が文字列で、空でなく、前後に空白がなく、制御文字を含まない。"""
    if not isinstance(value, str):
        raise GitPolicyError("type", field, f"文字列ではない ({type(value).__name__})")
    if value == "":
        raise GitPolicyError("invalid_value", field, "空文字")
    if value != value.strip():
        raise GitPolicyError("invalid_value", field, f"前後に空白がある: {value!r}")
    if _CONTROL_RE.search(value):
        raise GitPolicyError("invalid_value", field, f"制御文字を含む: {value!r}")
    return value


def branch_name_problem(name: str):
    """`name` が許可集合に入らない理由 (入るなら None)。

    git の `check-ref-format` を**写さない**。より狭い許可集合で、その中だけを通す。狭い集合が git の
    規則の部分集合であることは、`tests/test_git_policy_resolver.py` が本物の
    `git check-ref-format --branch` に通して確かめている (推論で済ませない)。
    """
    if not isinstance(name, str) or name == "":
        return "空"
    if len(name) > MAX_BRANCH_LENGTH:
        return f"{MAX_BRANCH_LENGTH} 文字を超える"
    if name == "HEAD":
        return "HEAD は使えない"
    parts = name.split("/")
    if parts[0] in _RESERVED_FIRST_COMPONENTS:
        return f"最初の成分が {parts[0]!r} (base の解決と読み違える)"
    for part in parts:
        if part == "":
            return "先頭・末尾の / か連続した / がある"
        if not _COMPONENT_RE.fullmatch(part):
            return f"成分 {part!r} が [A-Za-z0-9][A-Za-z0-9._-]* に合わない"
        if part.endswith("."):
            return f"成分 {part!r} が . で終わる"
        if part.endswith(".lock"):
            return f"成分 {part!r} が .lock で終わる"
        if ".." in part:
            return f"成分 {part!r} に .. を含む"
    return None


def _check_branch(value, field: str) -> str:
    _check_text(value, field)
    problem = branch_name_problem(value)
    if problem:
        raise GitPolicyError("invalid_value", field, f"branch 名として使えない: {problem} ({value!r})")
    return value


def _check_component(value, field: str) -> str:
    """置換子に入る 1 つの値。`/` を含まない 1 成分で、branch 名の規則を満たす。"""
    _check_text(value, field)
    if "/" in value:
        raise GitPolicyError("invalid_value", field, f"/ を含む (1 成分でなければならない): {value!r}")
    problem = branch_name_problem(value)
    if problem:
        raise GitPolicyError("invalid_value", field, f"branch の成分として使えない: {problem} ({value!r})")
    return value


_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_PATTERN_LITERAL_RE = re.compile(r"[A-Za-z0-9._/-]*")
#: pattern の検査用の見本 (今の値と同じ形)。
_SAMPLE_VALUES = {"mission_slug": "20261001-sample", "task_id": "t001", "task_slug": "sample"}


def _check_pattern(value, field: str) -> str:
    _check_text(value, field)
    names = _PLACEHOLDER_RE.findall(value)
    for n in names:
        if n not in PLACEHOLDERS:
            raise GitPolicyError("invalid_value", field, f"未知の置換子 {{{n}}} (使えるのは {', '.join(PLACEHOLDERS)})")
    for need in REQUIRED_PLACEHOLDERS:
        if need not in names:
            raise GitPolicyError("invalid_value", field, f"必須の置換子 {{{need}}} が無い")
    literal = _PLACEHOLDER_RE.sub("", value)
    if "{" in literal or "}" in literal:
        raise GitPolicyError("invalid_value", field, f"対応しない波括弧がある: {value!r}")
    if not _PATTERN_LITERAL_RE.fullmatch(literal):
        raise GitPolicyError("invalid_value", field, f"置換子以外の部分に使えない文字がある: {value!r}")
    _check_branch(_render(value, _SAMPLE_VALUES), field)
    return value


def _render(pattern: str, values: dict) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], pattern)


def _check_worktree_root(value, field: str) -> str:
    _check_text(value, field)
    if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value):
        raise GitPolicyError("invalid_value", field, f"絶対パスは使えない: {value!r}")
    if value.startswith("~"):
        raise GitPolicyError("invalid_value", field, f"~ で始まる値は使えない: {value!r}")
    if ".." in re.split(r"[\\/]", value):
        raise GitPolicyError("invalid_value", field, f".. の成分は使えない: {value!r}")
    if value != DEFAULT_WORKTREE_ROOT:
        # hooks/pre-tool-use.sh・hooks/lib_main_repo_git_guard.py・scripts/worktree_gc.py が
        # `.claude/worktrees` を自分で知っている。別の値を通すと編集ガードと片付けが壊れる (§2.1)。
        raise GitPolicyError("unsupported_value", field,
                             f"既定値 {DEFAULT_WORKTREE_ROOT!r} 以外は未対応: {value!r}")
    return value


def _check_mode(value, field: str) -> str:
    _check_text(value, field)
    if value != DEFAULT_MODE:
        raise GitPolicyError("unsupported_mode", field,
                             f"mode {value!r} は未実装 (使えるのは {DEFAULT_MODE!r} だけ。direct に倒さない)")
    return value


_CHECKS = {
    "mode": _check_mode,
    "base_branch": _check_branch,
    "pr_base": _check_branch,
    "task_branch_pattern": _check_pattern,
    "worktree_root": _check_worktree_root,
}

_DEFAULTS = {
    "mode": DEFAULT_MODE,
    "base_branch": DEFAULT_BASE_BRANCH,
    "pr_base": DEFAULT_PR_BASE,
    "task_branch_pattern": DEFAULT_TASK_BRANCH_PATTERN,
    "worktree_root": DEFAULT_WORKTREE_ROOT,
}

# ---------------------------------------------------------------------------
# mission.yaml → GitPolicy
# ---------------------------------------------------------------------------


def default_policy() -> GitPolicy:
    return GitPolicy(source="default", **_DEFAULTS)


def _git_block_line_count(text: str):
    """トップレベルの `git:` 行の数と、その下の字下げ行 (空行・コメントを除く) の数。

    `parse_yaml` は `git:` の下の字下げ行のうち 2 字下げの `key: value` 以外を**黙って読み飛ばす**
    (lib_task_cards.py の「Deeply nested / orphaned indented line — skip silently」)。空行・字下げた
    コメントの後ろの行も読み飛ばされる。読めた欄の数とこの数が合わなければ、何かが落ちている。
    """
    heads = 0
    lines = text.splitlines()
    count = 0
    in_block = False
    for line in lines:
        if re.match(r"^git:", line):
            heads += 1
            in_block = True
            count = 0 if heads == 1 else count
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # 空行・コメントは数えない。ただし `parse_yaml` はここで子の走査を打ち切る (後ろが読み飛ばされる)
            continue
        if line[0] in (" ", "\t"):
            count += 1
            continue
        in_block = False
    return heads, count


_STRAY_GIT_RE = re.compile(r"^[ \t]+(?:-[ \t]+)?[\"']?git[\"']?[ \t]*:")


def _stray_git_key_line(text: str):
    """0 桁目でない `git` キーの行 (1 始まりの行番号)。無ければ None。コメント行は対象外。"""
    for n, line in enumerate(text.splitlines(), 1):
        if _STRAY_GIT_RE.match(line):
            return n
    return None


def policy_from_text(text: str, source: str = "<mission.yaml>") -> GitPolicy:
    """mission.yaml の全文から Policy を作る。純関数 (lint と `load_git_policy` の共通の芯)。

    `git:` キーが無ければ既定値。あれば全欄を検査して、1 つでも外れれば `GitPolicyError`。
    """
    # parse_yaml の例外は問題の行をそのまま含む (関係ない欄の中身が出る)。理由には行番号だけを使い、
    # 例外は連鎖させない (except の外で raise する。`__context__` に行の中身を残さない)。
    parse_error = None
    try:
        data = parse_yaml(text, source=source)
    except ValueError as e:
        m = re.search(r"malformed line (\d+)", str(e))
        parse_error = f"{m.group(1)} 行目が解析できない" if m else "解析できない"
    if parse_error is not None:
        raise GitPolicyError("malformed", "mission.yaml", f"読めない: {parse_error}")

    # `git` という名前のキーが 0 桁目以外にある (字下げ・タブ・入れ子・リスト項目・引用符つき) mission.yaml は、
    # parse_yaml が黙って読み飛ばすので「git: が無い」と区別できない。既定値に倒す前に停止する。
    stray = _stray_git_key_line(text)
    if stray is not None:
        raise GitPolicyError("malformed", "git",
                             f"{stray} 行目に字下げされた git キーがある (git: は 0 桁目に書く。"
                             "字下げた行は parse_yaml が読み飛ばす)")

    heads, indented = _git_block_line_count(text)
    if "git" not in data:
        # `git:` 行が 1 本もないときだけ既定値。行はあるのに key が無いことは起きない (parse_yaml が拾う) が、
        # 起きたら黙って既定値にしない。
        if heads:
            raise GitPolicyError("malformed", "git", "git: の行があるのに読めていない")
        return default_policy()

    block = data["git"]
    if not isinstance(block, dict) or not block:
        # `git:` だけ・`git: null`・スカラー・リストは、`parse_yaml` では None や別の型になる。
        # None を「欄なし」に倒すと `mode: integration` が黙って direct になる。
        raise GitPolicyError("malformed", "git",
                             f"git: の値が mapping ではない ({type(block).__name__})。"
                             "2 字下げの `key: value` で書く")
    if heads != 1:
        raise GitPolicyError("malformed", "git", f"トップレベルの git: が {heads} 個ある")
    if indented != len(block):
        raise GitPolicyError("malformed", "git",
                             f"git: の下の字下げ行 {indented} 行のうち {len(block)} 欄しか読めていない "
                             "(4 字下げ・空行・コメント・重複キーは読み飛ばされる)")

    for key in block:
        if key not in POLICY_FIELDS:
            raise GitPolicyError("unknown_key", key,
                                 f"未知の欄 {key!r} (使えるのは {', '.join(POLICY_FIELDS)})")

    values = dict(_DEFAULTS)
    for key, raw in block.items():
        values[key] = _CHECKS[key](raw, key)
    return GitPolicy(source="mission", **values)


def mission_yaml_path(slug: str, queue_dir: str) -> str:
    return os.path.join(queue_dir, "missions", slug, "mission.yaml")


def load_git_policy(slug: str, *, queue_dir: str) -> GitPolicy:
    """`<queue_dir>/missions/<slug>/mission.yaml` から Policy を読む。読めなければ拒否 (既定値に倒さない)。"""
    if (not isinstance(slug, str) or slug == "" or "/" in slug or "\\" in slug
            or slug in (".", "..") or _CONTROL_RE.search(slug)):
        raise GitPolicyError("invalid_value", "mission_slug", f"mission の slug として使えない: {slug!r}")
    path = mission_yaml_path(slug, queue_dir)
    text = read_regular_text_or_unreadable(path)
    if is_unreadable(text):
        if is_missing(text):
            raise MissionNotFound("unreadable", "mission.yaml", f"mission {slug!r} が無い")
        raise GitPolicyError("unreadable", "mission.yaml", f"読めない: {text.reason}")
    return policy_from_text(text, source=path)


# ---------------------------------------------------------------------------
# 解決 (純関数)
# ---------------------------------------------------------------------------


def _check_task_id(task_id) -> str:
    _check_text(task_id, "task_id")
    if not TASK_ID_RE.fullmatch(task_id):
        raise GitPolicyError("invalid_value", "task_id", f"tNNN の形ではない: {task_id!r}")
    return task_id


def _component_values(mission_slug, task_id, task_slug) -> dict:
    return {
        "mission_slug": _check_component(mission_slug, "mission_slug"),
        "task_id": _check_task_id(task_id),
        "task_slug": _check_component(task_slug, "task_slug"),
    }


def task_branch(policy: GitPolicy, *, mission_slug: str, task_id: str, task_slug: str) -> str:
    """task の branch 名。既定値では git-helpers.sh の `task/${mission_slug}/${task_id}-${task_slug}` と同じバイト。"""
    values = _component_values(mission_slug, task_id, task_slug)
    return _check_branch(_render(policy.task_branch_pattern, values), "task_branch")


def task_worktree_path(policy: GitPolicy, *, repo_root: str, mission_slug: str, task_id: str,
                       task_slug: str) -> str:
    """task の worktree の絶対パス。`repo_root` の外に出ない。

    既定値では git-helpers.sh の `${repo_root}/.claude/worktrees/${mission_slug}/${task_id}-${task_slug}` と
    同じバイト (`repo_root` を正規化しない。git-helpers は git が返した root をそのまま使う)。
    外に出ないことの検査だけは realpath で行う (symlink・`..` で外へ逃げる値を通さない)。
    """
    if not isinstance(repo_root, str) or not os.path.isabs(repo_root):
        raise GitPolicyError("invalid_value", "repo_root", f"絶対パスでない: {repo_root!r}")
    if _CONTROL_RE.search(repo_root):
        raise GitPolicyError("invalid_value", "repo_root", f"制御文字を含む: {repo_root!r}")
    values = _component_values(mission_slug, task_id, task_slug)
    root = repo_root.rstrip("/") or "/"
    path = "/".join([root.rstrip("/"), policy.worktree_root, values["mission_slug"],
                     f"{values['task_id']}-{values['task_slug']}"])
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    try:
        inside = os.path.commonpath([real_root, real_path]) == real_root and real_path != real_root
    except ValueError:
        inside = False
    if not inside:
        raise GitPolicyError("invalid_value", "worktree_path",
                             f"repo_root の外に出る: {path!r}")
    return path


def task_base(policy: GitPolicy, *, remote_tracking_exists: bool) -> TaskBase:
    """task の branch を切る base。

    **選ぶ規則**: `refs/remotes/origin/<base_branch>` があれば `origin/<base_branch>`、無ければ local の
    `<base_branch>` に倒す (`fallback=True`。呼び出し元が警告を出す。GIT-04 の現状維持)。
    観測 (`remote_tracking_exists`) は呼び出し元の `git show-ref` で、Resolver は git を呼ばない。
    """
    if remote_tracking_exists:
        return TaskBase(ref=f"{REMOTE}/{policy.base_branch}", fallback=False)
    return TaskBase(ref=policy.base_branch, fallback=True)


def pr_base(policy: GitPolicy) -> str:
    return policy.pr_base


def resolve_task(policy: GitPolicy, *, repo_root: str, mission_slug: str, task_id: str,
                 task_slug: str) -> dict:
    """CLI `resolve-task` の出力。base は両方の候補を返し、選ぶのは呼び出し元の `git show-ref`。"""
    return {
        "branch": task_branch(policy, mission_slug=mission_slug, task_id=task_id, task_slug=task_slug),
        "worktree_path": task_worktree_path(policy, repo_root=repo_root, mission_slug=mission_slug,
                                            task_id=task_id, task_slug=task_slug),
        "base_remote": f"{REMOTE}/{policy.base_branch}",
        "base_local": policy.base_branch,
        "pr_base": policy.pr_base,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lib_git_policy.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="verb", required=True)
    r = sub.add_parser("resolve-task")
    for a in ("--queue", "--mission", "--task", "--task-slug", "--repo-root"):
        r.add_argument(a, required=True)
    b = sub.add_parser("pr-base")
    for a in ("--queue", "--mission"):
        b.add_argument(a, required=True)
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        policy = load_git_policy(args.mission, queue_dir=args.queue)
        if args.verb == "resolve-task":
            out = resolve_task(policy, repo_root=args.repo_root, mission_slug=args.mission,
                               task_id=args.task, task_slug=args.task_slug)
        else:
            out = {"pr_base": pr_base(policy)}
    except GitPolicyError as e:
        print(f"lib_git_policy: [{e.code}] {e.field}: {e.detail}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
