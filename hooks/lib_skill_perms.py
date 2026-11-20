#!/usr/bin/env python3
"""hooks/lib_skill_perms.py — Skill-based permission checker for PreToolUse hook.

Usage:
    python3 hooks/lib_skill_perms.py <yaml_path> <skills_csv> <tool_signature>
    <tool_signature> | python3 hooks/lib_skill_perms.py <yaml_path> <skills_csv>   # stdin 形 (hook 用)

    hook (pre-tool-use.sh) は stdin 形を使う。tool_signature には command 全文が入り、argv は
    1 引数 128KB (MAX_ARG_STRLEN) で exec が E2BIG になるため。argv 形は lint_plan.py 等の互換のため残す。

Output (JSON, single line):
    {"decision": "allow"|"deny"|"none", "source": "skill:code:allow:Edit"}

    - allow: Tool is permitted by skill permissions (skip Taskvia)
    - deny:  Tool is forbidden by skill permissions (block or escalate)
    - none:  No matching rule; fall through to existing Taskvia flow

Performance target: <100ms including Python startup.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from fnmatch import fnmatch, translate
from functools import lru_cache
from hashlib import md5

try:
    import yaml
except ImportError:
    yaml = None


def _parse_yaml_fallback(path: str) -> dict:
    """Minimal YAML parser for flat skill-permissions structure.

    Handles only the specific schema used by skill-permissions.yaml:
    top-level keys with nested allow/deny string lists.
    """
    result: dict = {}
    current_section = None
    current_key = None

    with open(path) as f:
        for line in f:
            stripped = line.rstrip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            content = stripped.strip()

            if indent == 0 and content.endswith(":"):
                current_section = content[:-1]
                if current_section == "skills":
                    continue
                result[current_section] = {}
                current_key = None
            elif indent == 2 and content.endswith(":"):
                key = content[:-1]
                if current_section == "skills":
                    result.setdefault("skills", {})[key] = {}
                    current_key = ("skills", key)
                elif current_section:
                    result[current_section][key] = []
                    current_key = (current_section, key)
            elif indent == 4 and content.endswith(":"):
                sub_key = content[:-1]
                if current_key and current_key[0] == "skills":
                    result["skills"][current_key[1]][sub_key] = []
                    current_key = ("skills", current_key[1], sub_key)
            elif content.startswith("- "):
                value = content[2:].strip().strip('"').strip("'")
                if current_key:
                    if len(current_key) == 3:
                        result["skills"][current_key[1]][current_key[2]].append(value)
                    elif len(current_key) == 2 and current_key[0] != "skills":
                        result[current_key[0]][current_key[1]].append(value)
    return result


def load_config(yaml_path: str) -> dict:
    """Load and cache skill-permissions.yaml (mtime-based)."""
    cache_key = md5(yaml_path.encode()).hexdigest()
    cache_path = f"/tmp/crewvia_skill_perms_{cache_key}.json"

    try:
        file_mtime = os.path.getmtime(yaml_path)
        if os.path.exists(cache_path):
            cache_mtime = os.path.getmtime(cache_path)
            if cache_mtime >= file_mtime:
                with open(cache_path) as f:
                    return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    with open(yaml_path) as f:
        if yaml:
            config = yaml.safe_load(f)
        else:
            config = _parse_yaml_fallback(yaml_path)

    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="crewvia_sp_")
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(config, f)
        os.rename(tmp_path, cache_path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return config


_QUOTED_STRING_RE = re.compile(r"""(?:"(?:[^"\\]|\\.)*"|'[^']*')""")


def _strip_quoted_strings(cmd: str) -> str:
    """Remove single/double quoted strings from a command.

    This prevents false positives when dangerous keywords appear
    inside string literals (e.g., git commit -m "fix sudo issue").
    """
    return _QUOTED_STRING_RE.sub("", cmd)


def _extract_bash_command(tool_sig: str) -> str | None:
    """Extract the command string from a Bash tool signature."""
    if tool_sig.startswith("Bash(") and tool_sig.endswith(")"):
        return tool_sig[5:-1]
    return None


# Dangerous substrings to scan for in compound commands.
# These catch cases where fnmatch fails because a dangerous command
# is preceded by other commands (e.g., "cd /tmp && rm -rf /").
_GLOBAL_DENY_SUBSTRINGS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+-\S*r\S*f"), "rm -rf"),
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r"\|\s*(ba)?sh\b"), "pipe-to-shell"),
    (re.compile(r"\|\s*source\b"), "pipe-to-source"),
]

# 正規化した各文 (Bash(<前置きを外した 1 文>)) の先頭に当てる正規表現の deny。fnmatch の先頭一致では
# 引数の順番に左右される形 (gh のオプションの位置・gh api の URL / GraphQL の位置) を書けないので別に持つ。
# 目的: Worker が PR を merge しないこと (merge は Director だけ。Director は pre-tool-use.sh が
# global deny より前に通す)。config の _global.deny の "Bash(gh pr merge*)" と重複してよい (ラベルが違うだけ)。
_GH = r"^Bash\((?:\S*/)?gh\s+"
_GLOBAL_DENY_STATEMENT_REGEX: list[tuple[re.Pattern, str]] = [
    # gh pr merge (gh -R owner/repo pr merge … のようにオプションが前に付く形も)
    (re.compile(_GH + r"(?:-{1,2}\S+\s+(?:[^-\s]\S*\s+)?)*pr\s+merge\b"), "gh pr merge"),
    # gh api …/pulls/<N>/merge (-X PUT / --method PUT の有無・引数の順番を問わない)
    (re.compile(_GH + r"api\b.*/pulls/[^\s/\"']+/merge\b"), "gh api pulls/N/merge"),
    # gh api graphql の merge 系 mutation
    (re.compile(_GH + r"api\b.*\b(?:mergePullRequest|enablePullRequestAutoMerge)\b"), "gh api graphql merge"),
]


def _match_statement_regex(sigs: list) -> str | None:
    for sig in sigs:
        for regex, label in _GLOBAL_DENY_STATEMENT_REGEX:
            if regex.search(sig):
                return label
    return None


# ---------------------------------------------------------------------------
# _global.deny の照合の前の正規化 (t001 / mission 20261120-global-deny-normalize)
#
# fnmatch は tool signature 全体への先頭一致なので、(a) git の大域オプション (git -C x push …)、
# (b) 複合コマンドの 2 文目以降 (cd x && git push …)、(c) 前置き (env A=1 git push … /
# /usr/bin/git push … / command git push …) で素通りする。ここでは Bash のコマンドを
# 「文」に分け、各文から前置きと git の大域オプションを外した正規形を作り、元の文と正規形の
# 両方を同じ _global.deny のパターンに照合する (パターンの意味は変えない)。
#
# 研究用ガード (lib_research_guard.py) は git の大域オプションを「許可して外す」のではなく
# 「一律に拒否」する (allowlist 方式) ので、外す側の一覧は共有できない。ここが唯一の一覧。
#
# 設計の原則:
#  (A) 解析器は、確信が持てないときにテキストを飛ばさない。飛ばすのは「区切り語まで確実に解析でき、
#      終端行も見つかったヒアドキュメントの本文」だけ。区切り語が引用符の組み合わせ・空白入り・変数などで
#      解析に自信が無いときは、本文を飛ばさず通常のコードとして走査する (誤検出側に倒す)。
#      here-string (<<<) はひとまとまりの演算子として消費する。
#  (B) リダイレクト (N>f・>>f・<f・N>&M・&>f・>|f・<<<w。語の先頭・途中のどちらも) は語の並びから外す。
#      リダイレクト先の $( ) / バッククォートは中を見る。
#  (C) コマンドを引数に取るもの (env -S・bash/sh -c・eval・trap・watch・find -exec・xargs・nohup/timeout/
#      nice/time/command/exec…の後ろ) は、その引数を同じ解析にかける。殻の入れ子の深さ (_MAX_SHELL) と
#      置換の入れ子の深さ (_MAX_NEST) は別に数える。シェルが stdin から読むヒアドキュメント本文・
#      here-string (bash <<< '…') もコードとして見る。非クォートのヒアドキュメント本文の $( ) も見る。
#
# 塞がない形 (残した理由): 変数展開 ($GIT push)・glob (g*t)・alias・関数・ANSI-C 引用 ($'\x67it')・
# git の -c alias.x=push・オプション略記 (--forc)・python -c / perl -e / node -e など他言語の system()・
# ssh / su -c / script -c / flock -c / nsenter / chroot・コマンド置換の入れ子が _MAX_NEST を超える形・
# 殻の入れ子が _MAX_SHELL を超える形 (その中身は文字列全体としてだけ照合する)。静的な字面からは
# 何が実行されるか確定できず、塞ぐと誤検出が全員の作業を止める。パターン側の穴 (git push origin HEAD:main・
# +main・--delete など) はパターンの意味を変えない方針なのでここでは扱わない。
# ---------------------------------------------------------------------------

_MAX_NEST = 4   # ダブルクォート・ヒアドキュメント内の $( ) / `` の再帰の上限 (超えたら字として扱う)
_MAX_SHELL = 4  # bash -c / eval / env -S … の入れ子の上限 (超えた中身は文字列全体としてだけ照合する)
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_KEYWORDS = frozenset(["!", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "fi", "done",
                       "coproc"])
_SHELLS = frozenset(["bash", "sh", "dash", "zsh", "ksh"])
_DELIM_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_DELIM_END = frozenset(" \t\n;&|()<>")
# git の大域オプション。値を次の語から取るもの (--x=v・-ck=v の結合形は「- で始まる語は全部外す」で足りる)
_GIT_OPT_WITH_ARG = frozenset(["-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env",
                               "--super-prefix", "--attr-source"])
# 「後ろのコマンドを実行する」前置きコマンド: 名前 → (値を取る短いオプション, 値を取る長いオプション)
_PREFIX_OPTS = {
    "env": ("uCSa", frozenset(["--unset", "--chdir", "--split-string", "--argv0"])),
    "command": ("", frozenset()),
    "builtin": ("", frozenset()),
    "exec": ("a", frozenset()),
    "nohup": ("", frozenset()),
    "setsid": ("", frozenset()),
    "time": ("of", frozenset(["--output", "--format"])),
    "nice": ("n", frozenset(["--adjustment"])),
    "ionice": ("cn", frozenset(["--class", "--classdata"])),
    "timeout": ("sk", frozenset(["--signal", "--kill-after"])),
    "stdbuf": ("ioe", frozenset(["--input", "--output", "--error"])),
    "xargs": ("IEnLPsad", frozenset(["--arg-file", "--delimiter", "--max-args", "--max-lines",
                                    "--max-procs", "--max-chars", "--eof"])),
    "doas": ("uC", frozenset()),
}


class _ScanCtx:
    """1 つの文字列の走査で共有する状態。"""
    __slots__ = ("failed", "work", "limit")

    def __init__(self, n: int) -> None:
        self.failed: set = set()   # 終端行が見つからなかった (区切り語, tabs)。以後は探さない
        self.work = 0              # ヒアドキュメントの終端探しで読んだ文字数
        self.limit = 4 * n + 1000  # 超えたら探さず「見つからない」扱い (= 本文を飛ばさない側) = 線形を保つ


def _scan_dq(cmd: str, i: int, depth: int, out: list, ctx: _ScanCtx, buf: list, stop_quote: bool) -> int:
    """ダブルクォート (またはヒアドキュメント本文) の中を読む。$( ) と `` は再帰して中の文を out に積む。
    stop_quote なら閉じの " の位置、そうでなければ末尾を返す。buf には字面を足す。"""
    n = len(cmd)
    while i < n:
        d = cmd[i]
        if stop_quote and d == '"':
            return i
        if d == "\\" and i + 1 < n:
            buf.append(cmd[i + 1] if cmd[i + 1] in '\\"$`' else "\\" + cmd[i + 1])
            i += 2
            continue
        if depth < _MAX_NEST and d == "$" and cmd.startswith("(", i + 1):
            buf.append("$(")
            i = _scan(cmd, i + 2, depth + 1, out, ctx, ")")
            continue
        if depth < _MAX_NEST and d == "`":
            buf.append("`")
            i = _scan(cmd, i + 1, depth + 1, out, ctx, "`")
            continue
        buf.append(d)
        i += 1
    return n


def _scan(cmd: str, i: int, depth: int, out: list, ctx: _ScanCtx, closer: str | None = None) -> int:
    """cmd[i:] を走査し、文 (raw, words, extra) を out に積む。戻り値は読み終えた位置。

    引用符を尊重する。区切りは ; & | ( ) ` と改行 (引用符の外)。# コメントは読み飛ばす。
    リダイレクトは語から外す。ダブルクォートの中の $( ) と `` は再帰して中の文も積む (closer がその閉じ)。
    extra は「その文のコードとして読まれうる文字列」(here-string の語・ヒアドキュメント本文)。
    1 文字は高々 (再帰の深さ) 回しか読まない = 線形 (ヒアドキュメントの終端探しは ctx で予算を持つ)。
    """
    n = len(cmd)
    words: list[str] = []
    buf: list[str] = []
    has = False
    start = i
    paren = 0
    extra: list[str] = []
    pending: list[tuple[str, bool, bool, list]] = []  # (区切り語, <<- か, 引用符付きか, extra)
    redir: str | None = None  # リダイレクトの直後: 次の語は捨てる ("here" なら extra へ)

    def flush() -> None:
        nonlocal has, redir
        if has:
            w = "".join(buf)
            if redir is None:
                words.append(w)
            else:
                if redir == "here":
                    extra.append(w)
                redir = None
        buf.clear()
        has = False

    def boundary() -> None:
        """リダイレクト演算子の前: 数字だけの語は fd 番号なので捨て、他は語として確定する。"""
        nonlocal has
        if has and "".join(buf).isdigit():
            buf.clear()
            has = False
        else:
            flush()

    def end_stmt(end: int, nxt: int) -> None:
        nonlocal words, start, extra, redir
        flush()
        if words:
            out.append((cmd[start:end].strip(), words, extra))
        words = []
        extra = []
        redir = None
        start = nxt

    while i < n:
        c = cmd[i]
        if c == "\\":
            if i + 1 < n:
                if cmd[i + 1] != "\n":
                    buf.append(cmd[i + 1])
                    has = True
                i += 2
            else:
                i += 1
            continue
        if c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                j = n
            if buf and buf[-1] == "$" and i > 0 and cmd[i - 1] == "$":
                buf.pop()  # $'…' は $ ごと引用符
            buf.append(cmd[i + 1:j])
            has = True
            i = j + 1
            continue
        if c == '"':
            if buf and buf[-1] == "$" and i > 0 and cmd[i - 1] == "$":
                buf.pop()  # $"…"
            has = True
            i = _scan_dq(cmd, i + 1, depth, out, ctx, buf, True) + 1
            continue
        if c in " \t":
            flush()
            i += 1
            continue
        if c == "#" and not has:
            j = cmd.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "\n":
            end_stmt(i, i + 1)
            i += 1
            for delim, tabs, quoted, ex in pending:
                key = (delim, tabs)
                if key in ctx.failed or ctx.work > ctx.limit:
                    continue  # 終端行が無い: 飛ばさず通常のコードとして走査する
                j = i
                found = -1
                eol = i
                while j < n:
                    eol = cmd.find("\n", j)
                    eol = n if eol < 0 else eol
                    line = cmd[j:eol]
                    if (line.lstrip("\t") if tabs else line) == delim:
                        found = j
                        break
                    j = eol + 1
                ctx.work += (eol if found >= 0 else n) - i
                if found < 0:
                    ctx.failed.add(key)
                    continue
                body = cmd[i:found]
                if not quoted:  # 非クォートの本文では $( ) と `` が実行される
                    _scan_dq(body, 0, depth, out, _ScanCtx(len(body)), [], False)
                ex.append(body)
                i = min(n, eol + 1)
            pending = []
            start = i
            continue
        if c == "<" and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            boundary()
            j = i + 2
            tabs = j < n and cmd[j] == "-"
            if tabs:
                j += 1
            k = j
            while k < n and cmd[k] in " \t":
                k += 1
            delim = None
            quoted = False
            end = k
            if k < n and cmd[k] in "'\"":
                q = cmd[k]
                e = cmd.find(q, k + 1)
                if e > 0:
                    m = _DELIM_RE.fullmatch(cmd, k + 1, e)
                    if m and (e + 1 >= n or cmd[e + 1] in _DELIM_END):
                        delim, quoted, end = m.group(0), True, e + 1
            else:
                s = k + 1 if (k < n and cmd[k] == "\\") else k
                m = _DELIM_RE.match(cmd, s)
                if m and (m.end() >= n or cmd[m.end()] in _DELIM_END):
                    delim, quoted, end = m.group(0), s != k, m.end()
            if delim is not None:
                pending.append((delim, tabs, quoted, extra))
                i = end
            else:  # 区切り語に自信が無い: 本文は飛ばさない。演算子だけ消費して次の語 (区切り語) を捨てる
                redir = "plain"
                i = j
            continue
        if c in "<>":
            if cmd.startswith("(", i + 1):  # <( ) >( ) プロセス置換: ( が文を区切る
                flush()
                i += 1
                continue
            boundary()
            j = i + 1
            kind = "plain"
            if c == ">":
                if j < n and cmd[j] in ">&|":
                    j += 1
            elif cmd.startswith("<<", i):  # <<<
                j = i + 3
                kind = "here"
            elif j < n and cmd[j] in "&>":
                j += 1
            redir = kind
            i = j
            continue
        if c == "&" and cmd.startswith(">", i + 1):  # &> &>>
            boundary()
            j = i + 2
            if j < n and cmd[j] == ">":
                j += 1
            redir = "plain"
            i = j
            continue
        if c in ";&|`()":
            if c == closer and c == "`":
                end_stmt(i, i + 1)
                return i + 1
            if c == ")":
                if closer == ")" and paren == 0:
                    end_stmt(i, i + 1)
                    return i + 1
                paren = max(0, paren - 1)
            elif c == "(":
                paren += 1
            end_stmt(i, i + 1)
            i += 1
            continue
        buf.append(c)
        has = True
        i += 1
    end_stmt(n, n)
    return n


def _strip_git_globals(words: list[str]) -> list[str]:
    """['git', '-C', 'x', '-c', 'k=v', '--no-pager', 'push', …] → ['git', 'push', …]"""
    k = 1
    n = len(words)
    while k < n and words[k].startswith("-") and words[k] != "--":
        w = words[k]
        k += 2 if w in _GIT_OPT_WITH_ARG else 1
    return [words[0]] + words[k:]


def _skip_opts(words: list[str], k: int, short_arg: str, long_arg, vals: list) -> int:
    """words[k:] の先頭のオプションを読み飛ばして次の位置を返す。値を取るオプションの値は vals に積む
    (短いオプションは束ねられる: -iS 'x' / -Sx。-S の値だけ後で使う)。"""
    n = len(words)
    while k < n:
        w = words[k]
        if w == "--":
            return k + 1
        if w == "-":
            k += 1
            continue
        if not w.startswith("-"):
            return k
        if w.startswith("--"):
            name, eq, val = w.partition("=")
            if name in long_arg and not eq:
                val = words[k + 1] if k + 1 < n else ""
                k += 2
            else:
                k += 1
            if name == "--split-string":
                vals.append(val)
            continue
        taken = False
        for idx in range(1, len(w)):
            ch = w[idx]
            if ch in short_arg:
                rest = w[idx + 1:]
                if rest:
                    val = rest
                    k += 1
                else:
                    val = words[k + 1] if k + 1 < n else ""
                    k += 2
                if ch == "S":
                    vals.append(val)
                taken = True
                break
        if not taken:
            k += 1
    return k


def _strip_prefix(words: list[str]) -> tuple[list[str], list[str]]:
    """VAR=val・env・command・builtin・exec・nohup・time・nice・timeout・xargs・制御構文の語・パス指定を外す。
    戻り値は (外した語列, 「コードとして読まれる文字列」(env -S の引数))。"""
    words = list(words)
    inner: list[str] = []
    for _ in range(64):  # 前置きの積み重ねの上限 (無限ループ防止)
        if not words:
            return words, inner
        w0 = words[0]
        base = w0.rsplit("/", 1)[-1] if "/" in w0 else w0
        if w0 in _KEYWORDS or _ASSIGN_RE.match(w0):
            words = words[1:]
        elif base in _PREFIX_OPTS:
            short_arg, long_arg = _PREFIX_OPTS[base]
            vals: list[str] = []
            k = _skip_opts(words, 1, short_arg, long_arg, vals)
            if vals:  # env -S 'A=1 git push' rest…: 引数は分割されて残りの語の前に付く
                rest = " ".join(words[k:])
                inner.extend(v + " " + rest for v in vals)
            if base == "timeout":
                k += 1  # DURATION
            words = words[k:]
        else:
            break
    if words and "/" in words[0]:
        words[0] = words[0].rsplit("/", 1)[-1]
    return words, inner


def _emit(words: list[str], extra: list[str], sigs: dict, inner: list[str]) -> None:
    """1 文の語列 → 照合する sig (前置きを外したもの。git なら大域オプションも外したもの) と、
    その文が実行する別のコード文字列 (bash -c の引数・eval・env -S・trap・watch・find -exec・stdin) を積む。"""
    norm, pin = _strip_prefix(words)
    inner.extend(pin)
    if not norm:
        return
    head = norm[0]
    forms = [norm]
    if head == "git":
        forms.append(_strip_git_globals(norm))
    for form in forms:
        sigs[f"Bash({' '.join(form)})"] = None
    if head in _SHELLS:
        for k in range(1, len(norm)):
            t = norm[k]
            if t.startswith("-") and not t.startswith("--") and "c" in t[1:] and k + 1 < len(norm):
                inner.append(norm[k + 1])
                break
        inner.extend(extra)  # bash <<< '…' / bash <<EOF … : stdin から読むコード
    elif head in ("source", "."):
        inner.extend(extra)
    elif head == "eval" and len(norm) > 1:
        inner.append(" ".join(norm[1:]))
    elif head == "trap" and len(norm) > 1:
        inner.append(norm[1])
    elif head == "watch":
        k = _skip_opts(norm, 1, "n", frozenset(["--interval"]), [])
        if k < len(norm):
            inner.append(" ".join(norm[k:]))
    elif head == "find":
        k = 1
        while k < len(norm):
            if norm[k] in ("-exec", "-execdir", "-ok", "-okdir"):
                j = k + 1
                while j < len(norm) and norm[j] not in (";", "+"):
                    j += 1
                if j > k + 1:
                    inner.append(" ".join(norm[k + 1:j]))
                k = j
            k += 1


def _candidate_sigs(cmd: str) -> list[str]:
    """cmd の各文について 元の文・前置きを外した形・git の大域オプションも外した形 を Bash(...) にして返す。
    bash -c / eval / env -S などの引数は _MAX_SHELL 段まで中を見る (置換の深さとは別に数える)。"""
    sigs: dict[str, None] = {}
    work: list[tuple[str, int]] = [(cmd, 0)]
    while work:
        text, sd = work.pop()
        out: list = []
        _scan(text, 0, 0, out, _ScanCtx(len(text)))
        inner: list[str] = []
        for raw, words, extra in out:
            if raw:
                sigs[f"Bash({raw})"] = None
            _emit(words, extra, sigs, inner)
        for t in inner:
            if sd < _MAX_SHELL:
                work.append((t, sd + 1))
            else:
                sigs[f"Bash({t})"] = None  # 打ち切り: 全体だけは照合する
    return list(sigs)


@lru_cache(maxsize=8)
def _compile_deny(patterns: tuple) -> list:
    """fnmatch と同じ意味 (fnmatch.translate) で前もってコンパイルする。(literal prefix, 正規表現, pattern)。"""
    res = []
    for p in patterns:
        m = re.search(r"[*?\[]", p)
        prefix = p if m is None else p[:m.start()]
        res.append((prefix, re.compile(translate(p)).match, p))
    return res


def _match_global_deny(sigs: list, patterns: list) -> str | None:
    comp = _compile_deny(tuple(patterns))
    for sig in sigs:
        for prefix, match, pattern in comp:
            if sig.startswith(prefix) and match(sig):
                return pattern
    return None


def check_permission(config: dict, skills_csv: str, tool_sig: str) -> dict:
    """Check tool_sig against skill permissions.

    Returns {"decision": "allow"|"deny"|"none", "source": "..."}.
    """
    # 1. _global.deny — always checked, regardless of SKILLS
    global_deny = config.get("_global", {}).get("deny", [])
    for pattern in global_deny:
        if fnmatch(tool_sig, pattern):
            return {"decision": "deny", "source": f"_global:deny:{pattern}"}

    # 1a. 正規化した各文 (複合コマンドの 2 文目以降・git の大域オプション・前置きを外した形) も照合する
    cmd = _extract_bash_command(tool_sig)
    if cmd is not None:
        sigs = _candidate_sigs(cmd)
        hit = _match_global_deny(sigs, global_deny) if global_deny else None
        if hit is not None:
            return {"decision": "deny", "source": f"_global:deny:{hit}"}
        label = _match_statement_regex(sigs)
        if label is not None:
            return {"decision": "deny", "source": f"_global:deny:regex:{label}"}

    # 1b. Substring scan for compound commands (cd /tmp && rm -rf /)
    # Strip quoted strings first to avoid false positives on string literals
    # (e.g., git commit -m "fix sudo issue" && git push)
    cmd = _extract_bash_command(tool_sig)
    if cmd and re.search(r"[;&|]", cmd):
        cmd_stripped = _strip_quoted_strings(cmd)
        for regex, label in _GLOBAL_DENY_SUBSTRINGS:
            if regex.search(cmd_stripped):
                return {"decision": "deny", "source": f"_global:deny:substring:{label}"}

    # No skills → fall through (global deny was already checked above)
    if not skills_csv:
        return {"decision": "none", "source": "no_skills"}

    skills = [s.strip() for s in skills_csv.split(",") if s.strip()]
    if not skills:
        return {"decision": "none", "source": "no_skills"}

    skills_config = config.get("skills", {})
    known_skills = [s for s in skills if s in skills_config]
    if not known_skills:
        return {"decision": "none", "source": "unknown_skills"}

    # 2. Skill deny — union across all task skills (case-insensitive for SQL patterns)
    for skill in known_skills:
        deny_patterns = skills_config[skill].get("deny", [])
        for pattern in deny_patterns:
            if fnmatch(tool_sig, pattern):
                return {"decision": "deny", "source": f"skill:{skill}:deny:{pattern}"}
            if cmd and fnmatch(tool_sig.lower(), pattern.lower()):
                return {"decision": "deny", "source": f"skill:{skill}:deny:{pattern}(ci)"}

    # 3. Skill allow — union across all task skills
    for skill in known_skills:
        allow_patterns = skills_config[skill].get("allow", [])
        for pattern in allow_patterns:
            if fnmatch(tool_sig, pattern):
                return {"decision": "allow", "source": f"skill:{skill}:allow:{pattern}"}

    # 4. No match — fall through
    return {"decision": "none", "source": "no_match"}


def main():
    # 3 引数 (yaml skills) + stdin = tool_sig を stdin から読む (hook 用。
    # Linux は 1 引数 128KB で exec が E2BIG になるので大きな command は argv に載せない)。
    # 4 引数 (yaml skills tool_sig) は lint_plan.py 等の互換のため残す。
    if len(sys.argv) == 3:
        tool_sig = sys.stdin.read()
        if tool_sig.endswith("\n"):  # here-string が足す 1 個の改行
            tool_sig = tool_sig[:-1]
        sys.argv.append(tool_sig)
    if len(sys.argv) != 4:
        print(json.dumps({"decision": "none", "source": "usage_error"}))
        sys.exit(0)

    yaml_path, skills_csv, tool_sig = sys.argv[1], sys.argv[2], sys.argv[3]

    if not os.path.isfile(yaml_path):
        print(json.dumps({"decision": "none", "source": "yaml_not_found"}))
        sys.exit(0)

    config = load_config(yaml_path)
    result = check_permission(config, skills_csv, tool_sig)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
