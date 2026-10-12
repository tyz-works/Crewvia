#!/usr/bin/env python3
"""hooks/lib_research_guard.py — research skill の構造的ガード (pre-tool-use.sh から呼ぶ)

  python3 lib_research_guard.py write <repo> <agent> <tool_path> <cwd> <skill_perms_yaml>
  python3 lib_research_guard.py bash  <command>        (コマンドは引数、無ければ stdin)

どちらも 1 行の JSON を出す: {"decision": "allow"|"deny", "reason": "..."}
**判断に迷う形は deny (fail closed)**。例外で落ちたときも呼び出し側 (hook) は deny に倒す。

(1) write: research の Write/Edit/MultiEdit/NotebookEdit が書いてよい範囲
    - <repo>/queue/missions/<自分の assignment の slug>/notes/<自分の tid>*.md  (上書き可)
    - skill-permissions.yaml の research_write_scope.obsidian_research_dir を指定したときだけ
      <その dir>/YYYYMMDD_*.md  (新規作成のみ・既定は無効)
    パスは realpath で解決して判定し、`..` や symlink で範囲外に出る形・既存の symlink は拒否する。

(2) bash: research の Bash は読み取り専用の allowlist (下の _check_segment)。
    リダイレクト・tee・sed -i・cp/mv/rm・インタプリタ (python3 -c 等) は通らない。
    plan.sh done / needs-director の `--result-file -` を引用符つき heredoc で渡す形は通す。
"""

from __future__ import annotations

import json
import os
import re
import sys

OBSIDIAN_NAME_RE = re.compile(r"^\d{8}_[A-Za-z0-9._-]+\.md$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TID_RE = re.compile(r"^t\d+$")


def _out(decision: str, reason: str) -> int:
    print(json.dumps({"decision": decision, "reason": reason}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- write scope


def _read_assignment(repo: str, agent: str) -> tuple[str, str] | None:
    if not agent or "/" in agent or agent.startswith("."):
        return None
    try:
        with open(os.path.join(repo, "queue", "assignments", agent)) as f:
            text = f.read().replace("\n", "").strip()
    except OSError:
        return None
    slug, sep, tid = text.partition(":")
    if not sep or not SLUG_RE.match(slug) or not TID_RE.match(tid):
        return None
    return slug, tid


def _obsidian_dir(yaml_path: str) -> str | None:
    try:
        with open(yaml_path) as f:
            for line in f:
                m = re.match(r"^\s+obsidian_research_dir:\s*(.*?)\s*(#.*)?$", line.rstrip("\n"))
                if m:
                    v = m.group(1).strip().strip('"').strip("'")
                    if not v:
                        return None
                    return os.path.realpath(os.path.expanduser(v))
    except OSError:
        return None
    return None


def _no_symlink_below(root: str, path: str) -> bool:
    """root (realpath 済み) から path までの既存の構成要素に symlink が無いこと。"""
    rel = os.path.relpath(path, root)
    cur = root
    for part in rel.split(os.sep):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return False
        if not os.path.lexists(cur):
            break
    return True


def check_write(repo: str, agent: str, raw_path: str, cwd: str, yaml_path: str) -> tuple[str, str]:
    if not raw_path:
        return "deny", "書き込み先が不明です"
    abspath = raw_path if os.path.isabs(raw_path) else os.path.join(cwd, raw_path)
    norm = os.path.normpath(abspath)  # `..` を字句的に畳む
    real = os.path.realpath(abspath)  # symlink を解決
    # 親 dir が「字句的な形」と「解決後」で一致しないもの (= 経路に symlink か `..` の抜け) は通さない
    if os.path.dirname(norm) != os.path.dirname(real) or os.path.islink(abspath):
        return "deny", "symlink や .. を経由するパスへの書き込みは許可されません"

    repo_real = os.path.realpath(repo)
    asg = _read_assignment(repo_real, agent)
    if asg:
        slug, tid = asg
        notes = os.path.join(repo_real, "queue", "missions", slug, "notes")
        if os.path.dirname(real) == notes and re.match(
            r"^" + re.escape(tid) + r"(?:[^0-9][A-Za-z0-9._-]*)?\.md$", os.path.basename(real)
        ):
            return "allow", f"research の調査メモ (queue/missions/{slug}/notes/{tid}*.md)"

    obs = _obsidian_dir(yaml_path)
    if obs and os.path.dirname(real) == obs and OBSIDIAN_NAME_RE.match(os.path.basename(real)):
        if os.path.lexists(abspath):
            return "deny", "obsidian の調査ノートは新規作成のみです (既存ファイルの上書きは不可)"
        return "allow", "research の調査ノート (obsidian 指定 dir への新規作成)"

    return "deny", (
        "research スキルが書けるのは queue/missions/<自分の mission>/notes/<自分の task id>*.md "
        "(と、設定したときだけ obsidian の YYYYMMDD_*.md への新規作成) だけです。"
    )


# ------------------------------------------------------------------ bash scope

_SIMPLE_READERS = {
    "cat", "head", "tail", "wc", "ls", "stat", "file", "realpath", "dirname", "basename",
    "diff", "cmp", "nl", "column", "sort", "uniq", "cut", "tr", "rev", "tac", "fold",
    "echo", "printf", "true", "false", "pwd", "date", "which", "type", "command", "test", "[",
    "grep", "egrep", "fgrep", "rg", "ugrep", "jq", "tree", "du", "df", "id", "whoami",
    "hostname", "uname", "env", "printenv", "sha256sum", "md5sum", "cksum", "less", "more",
    "ps", "pgrep", "seq", "expr", "sleep", "readlink", "numfmt", "paste", "comm", "join",
    "strings", "od", "xxd", "hexdump", "yq",
}
# 値を取って外へ出る・実行する形があるものは個別に拒否する引数
_DENY_ARGS = {
    "grep": ("--pre",), "rg": ("--pre", "--hostname-bin"), "ugrep": ("--pager", "--pre"),
    "less": ("-o", "-O", "--log-file", "--LOG-FILE"), "sort": ("-o", "--output"),
    "env": (), "tree": ("-o",), "jq": ("--rawfile", "--slurpfile"),
    "yq": ("-i", "--inplace"), "seq": (), "diff": (),
}
_GIT_READ = {
    "log", "show", "diff", "status", "rev-parse", "ls-files", "grep", "blame", "rev-list",
    "cat-file", "ls-tree", "merge-base", "describe", "shortlog", "name-rev", "show-ref",
    "for-each-ref", "ls-remote", "diff-tree", "diff-files", "diff-index", "count-objects",
    "whatchanged", "show-branch", "check-ignore", "var", "version", "help",
}
_GIT_DENY_ARGS = ("--output", "--ext-diff", "--textconv", "-O", "--open-files-in-pager")
_GH_READ = {
    ("pr", "view"), ("pr", "diff"), ("pr", "checks"), ("pr", "list"), ("pr", "status"),
    ("run", "list"), ("run", "view"), ("issue", "view"), ("issue", "list"),
    ("repo", "view"), ("release", "list"), ("release", "view"), ("workflow", "list"),
    ("workflow", "view"), ("search", "prs"), ("search", "issues"), ("search", "code"),
    ("search", "repos"), ("search", "commits"),
}
_PLAN_READ = {"status", "pull", "done", "needs-director", "pr-base", "store-check"}
_SAFE_SOURCES = {".crewvia-env"}


class _Reject(Exception):
    pass


def _strip_heredocs(cmd: str) -> str:
    """引用符つき heredoc の本文を落とす。引用符なし (展開される) heredoc は拒否。"""
    lines = cmd.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        # この行の中の heredoc 開始 (<<[-]'WORD' / <<[-]"WORD" / <<[-]WORD)。<<< は here-string。
        marks = re.findall(r"(?<!<)<<(?!<)(-?)\s*(\S+)", line)
        for dash, word in marks:
            if not (len(word) >= 3 and word[0] in "'\"" and word[-1] == word[0]):
                raise _Reject("引用符なしの heredoc は展開されるため拒否します (<<'EOF' の形にしてください)")
            term = word[1:-1]
            if not term:
                raise _Reject("heredoc の終端が空です")
            while i < len(lines):
                body = lines[i]
                i += 1
                if (body.strip() if dash else body) == term:
                    break
            else:
                raise _Reject("heredoc が閉じていません")
        # 開始語はそのまま残す (後段のリダイレクト検査は `<<` を入力側として許す)
    return "\n".join(out)


def _tokenize(cmd: str) -> list[list[str]]:
    """コマンドを区切り (; & | && || 改行) で分け、各区間を語の列にする。

    リダイレクトは語 `>`/`>>`/`<` ... の形で残さず、ここで検査して捨てる。
    $( ` <( >( は常に拒否。
    """
    segs: list[list[str]] = []
    cur: list[str] = []
    word = ""
    has_word = False
    i, n = 0, len(cmd)

    def flush_word() -> None:
        nonlocal word, has_word
        if has_word:
            cur.append(word)
        word, has_word = "", False

    def end_seg() -> None:
        nonlocal cur
        flush_word()
        if cur:
            segs.append(cur)
        cur = []

    while i < n:
        c = cmd[i]
        if c == "\\":
            if i + 1 < n:
                if cmd[i + 1] == "\n":
                    i += 2
                    continue
                word += cmd[i + 1]
                has_word = True
                i += 2
                continue
            raise _Reject("行末の \\ は拒否します")
        if c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise _Reject("閉じていない '")
            word += cmd[i + 1:j]
            has_word = True
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            buf = ""
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    buf += cmd[j + 1]
                    j += 2
                    continue
                if cmd[j] == "`" or cmd.startswith("$(", j):
                    raise _Reject("コマンド置換 ($( や `) は拒否します")
                buf += cmd[j]
                j += 1
            if j >= n:
                raise _Reject("閉じていない \"")
            word += buf
            has_word = True
            i = j + 1
            continue
        if c == "`" or cmd.startswith("$(", i) or cmd.startswith("<(", i) or cmd.startswith(">(", i):
            raise _Reject("コマンド置換・プロセス置換は拒否します")
        if c in " \t":
            flush_word()
            i += 1
            continue
        if c in "\n;":
            end_seg()
            i += 1
            continue
        if c == "|":
            end_seg()
            i += 2 if cmd.startswith("||", i) or cmd.startswith("|&", i) else 1
            continue
        if c == "&":
            if cmd.startswith("&&", i):
                end_seg()
                i += 2
                continue
            if cmd.startswith("&>", i):
                # &>/dev/null だけ許す
                m = re.match(r"&>\s*/dev/null(?![\w/.-])", cmd[i:])
                if not m:
                    raise _Reject("&> は /dev/null 宛てだけ許可します")
                flush_word()
                i += m.end()
                continue
            raise _Reject("バックグラウンド実行 (&) は拒否します")
        if c in "<>":
            # fd 番号つき (2>&1, 2>/dev/null) の fd 番号は直前の語として溜まっている
            if has_word and word.isdigit():
                word, has_word = "", False
            if cmd.startswith("<<<", i):
                flush_word()
                i += 3
                continue
            if cmd.startswith("<<", i):
                # heredoc 開始語は _strip_heredocs が検証済み。語ごと読み飛ばす
                m = re.match(r"<<-?\s*('[^']*'|\"[^\"]*\")", cmd[i:])
                if not m:
                    raise _Reject("heredoc の形を解釈できません")
                flush_word()
                i += m.end()
                continue
            if c == "<":
                flush_word()
                i += 1
                continue  # 入力リダイレクト: 後ろの語は通常の引数として検査される
            # c == ">"
            m = re.match(r">>?\s*(/dev/null)(?![\w/.-])", cmd[i:]) or re.match(r">&\s*(\d|-)(?!\d)", cmd[i:])
            if not m:
                raise _Reject("ファイルへのリダイレクト (>, >>) は拒否します")
            flush_word()
            i += m.end()
            continue
        word += c
        has_word = True
        i += 1
    end_seg()
    return segs


def _check_git(args: list[str]) -> None:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            i += 2
            continue
        if a in ("--no-pager", "-P", "--no-optional-locks", "--no-replace-objects"):
            i += 1
            continue
        break
    if i >= len(args):
        raise _Reject("git のサブコマンドがありません")
    sub, rest = args[i], args[i + 1:]
    for a in rest:
        if a in _GIT_DENY_ARGS or a.startswith("--output="):
            raise _Reject(f"git {sub} の {a} は書き込み・実行を伴うため拒否します")
    if sub in _GIT_READ:
        return
    if sub == "branch":
        if all(a in ("-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes") for a in rest):
            return
        raise _Reject("git branch は一覧表示 (-a -r -v --list --show-current) だけ許可します")
    if sub == "remote":
        if rest in ([], ["-v"], ["show"]) or (rest[:1] == ["show"] and len(rest) == 2):
            return
        raise _Reject("git remote は -v / show だけ許可します")
    if sub == "fetch":
        # .git の remote ref を更新するだけ (作業ツリーは変えない)。--upload-pack は任意実行になる
        if any(a.startswith(("--upload-pack", "-u")) for a in rest):
            raise _Reject("git fetch の --upload-pack は拒否します")
        return
    if sub == "worktree" and rest[:1] == ["list"]:
        return
    if sub == "stash" and rest[:1] == ["list"]:
        return
    if sub == "tag" and all(a in ("-l", "--list", "-n") for a in rest):
        return
    if sub == "config" and rest[:1] in (["--get"], ["--get-all"], ["--list"], ["-l"]):
        return
    raise _Reject(f"git {sub} は読み取り専用の allowlist にありません")


def _check_gh(args: list[str]) -> None:
    if len(args) >= 1 and args[0] == "api":
        rest = args[1:]
        for k, a in enumerate(rest):
            if a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field=", "--input=")):
                raise _Reject("gh api の -f/-F/--input は書き込みになりうるため拒否します")
            if a in ("-X", "--method"):
                if k + 1 >= len(rest) or rest[k + 1].upper() != "GET":
                    raise _Reject("gh api は GET だけ許可します")
            if a.startswith("--method=") and a.split("=", 1)[1].upper() != "GET":
                raise _Reject("gh api は GET だけ許可します")
            if a == "graphql":
                raise _Reject("gh api graphql は POST のため拒否します")
        return
    if len(args) >= 2 and (args[0], args[1]) in _GH_READ:
        return
    raise _Reject("この gh サブコマンドは読み取り専用の allowlist にありません")


_SED_SCRIPT_OK = re.compile(
    r"^(?:\$|\d+|/[^/]*/)(?:,(?:\$|\d+|/[^/]*/))?!?[pP=]$"
    r"|^(?:\d+|\$)?[pP=]$"
    r"|^s([^\\\n\w])(?:(?!\1).)*\1(?:(?!\1).)*\1[gIip0-9]*$"
)


def _check_sed(args: list[str]) -> None:
    scripts = []
    k = 0
    seen_script = False
    while k < len(args):
        a = args[k]
        if a in ("-n", "-E", "-r", "-s", "--quiet", "--silent", "--regexp-extended", "--posix", "-u", "-z"):
            k += 1
            continue
        if a == "-e" and k + 1 < len(args):
            scripts.append(args[k + 1])
            seen_script = True
            k += 2
            continue
        if a.startswith("-") and a != "-":
            raise _Reject(f"sed の {a} は拒否します (-i/-f/--in-place 等は書き込み・実行を伴う)")
        if not seen_script:
            scripts.append(a)
            seen_script = True
        k += 1  # 以降はファイル名
    if not scripts:
        raise _Reject("sed のスクリプトがありません")
    for s in scripts:
        for part in s.split(";") if not s.startswith("s") else [s]:
            if not _SED_SCRIPT_OK.match(part.strip()):
                raise _Reject("sed は 行指定の p と s///[gIp] だけ許可します (w / e / r 等は拒否)")


_AWK_BAD = re.compile(r"[>|]|\bsystem\b|\bgetline\b|\bclose\b|\bprintf?\s*\(?[^;]*\bsprintf\b")


def _check_awk(args: list[str]) -> None:
    for a in args:
        if a in ("-i", "--include", "-f", "--file", "-E") or a.startswith(("-i", "--in-place", "--file=")):
            raise _Reject(f"awk の {a} は拒否します")
    prog = next((a for a in args if not a.startswith("-")), "")
    if _AWK_BAD.search(prog):
        raise _Reject("awk のプログラムに > | system getline close があります (書き込み・実行の疑い)")


def _check_find(args: list[str]) -> None:
    for a in args:
        if a in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"):
            raise _Reject(f"find {a} は拒否します")


def _check_plan(args: list[str]) -> None:
    if not args or args[0] not in _PLAN_READ:
        raise _Reject("plan.sh は status / pull / done / needs-director / pr-base / store-check だけ許可します")


def _check_segment(words: list[str], depth: int = 0) -> None:
    if not words:
        return
    # 先頭の VAR=value は拒否 (LD_PRELOAD 等の経路を作らない)
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        raise _Reject("先頭の環境変数代入は拒否します")
    cmd, args = words[0], words[1:]
    base = os.path.basename(cmd)

    if cmd in ("cd", "pushd", "popd"):
        return
    if cmd in ("source", ".") and len(args) == 1 and os.path.basename(args[0]) in _SAFE_SOURCES:
        return
    if cmd in ("export", "unset") and all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*(=[^$`]*)?$", a) for a in args):
        return
    if cmd == "timeout" or cmd == "nice" or cmd == "time":
        rest = list(args)
        while rest and (rest[0].startswith("-") or re.match(r"^\d+[smhd]?$", rest[0])):
            rest.pop(0)
        if depth > 3 or not rest:
            raise _Reject("timeout/time の入れ子が深すぎます")
        _check_segment(rest, depth + 1)
        return
    if base == "plan.sh":
        _check_plan(args)
        return
    if cmd == "git":
        _check_git(args)
        return
    if cmd == "gh":
        _check_gh(args)
        return
    if cmd == "sed":
        _check_sed(args)
        return
    if cmd in ("awk", "gawk", "mawk"):
        _check_awk(args)
        return
    if cmd == "python3" and args[:2] == ["-m", "json.tool"]:
        return
    if cmd == "find":
        _check_find(args)
        return
    if cmd == "xargs":
        raise _Reject("xargs は任意コマンドを実行するため拒否します")
    if cmd in _SIMPLE_READERS:
        for a in args:
            for bad in _DENY_ARGS.get(cmd, ()):
                if a == bad or a.startswith(bad + "="):
                    raise _Reject(f"{cmd} の {a} は拒否します")
        if cmd == "env" and args:
            raise _Reject("env でのコマンド実行は拒否します")
        return
    raise _Reject(
        f"{cmd!r} は research の読み取り専用 allowlist にありません "
        "(python3 / bash -c / awk / cp / mv / rm / tee / curl 等は不可)"
    )


def check_bash(command: str) -> tuple[str, str]:
    try:
        text = _strip_heredocs(command)
        segs = _tokenize(text)
        if not segs:
            return "deny", "空のコマンドです"
        for seg in segs:
            _check_segment(seg)
    except _Reject as e:
        return "deny", f"research の Bash は読み取り専用です: {e}"
    return "allow", "research の読み取り専用 allowlist"


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "write" and len(argv) == 7:
        d, r = check_write(argv[2], argv[3], argv[4], argv[5], argv[6])
        return _out(d, r)
    if len(argv) >= 2 and argv[1] == "bash":
        cmd = argv[2] if len(argv) > 2 else sys.stdin.read()
        d, r = check_bash(cmd)
        return _out(d, r)
    return _out("deny", "usage error")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:  # fail closed
        _out("deny", f"research guard error: {type(e).__name__}")
        sys.exit(0)
