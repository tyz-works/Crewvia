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
from fnmatch import fnmatch
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
# 塞がない形 (残した理由): 変数展開 ($GIT push)・alias・関数・xargs・eval の入れ子 2 段以上・
# bash -c の入れ子 2 段以上・非クォートのヒアドキュメント本文中の $( )・base64 等で組み立てた
# 文字列。静的な字面からは何が実行されるか確定できず、塞ぐと誤検出が全員の作業を止める。
# bash -c / sh -c / eval は 1 段だけ中を見る。
# ---------------------------------------------------------------------------

_MAX_NEST = 4  # クォート内の $( ) / `` の再帰の上限 (超えたら字として扱う)
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_KEYWORDS = frozenset(["!", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "fi", "done"])
_SHELLS = frozenset(["bash", "sh", "dash", "zsh", "ksh"])
# git の大域オプション。値を次の語から取るもの (--x=v の結合形は「- で始まる語は全部外す」で足りる)
_GIT_OPT_WITH_ARG = frozenset(["-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env",
                               "--super-prefix", "--attr-source"])


def _scan(cmd: str, i: int, depth: int, out: list, closer: str | None = None) -> int:
    """cmd[i:] を走査し、文 (raw, words) を out に積む。戻り値は読み終えた位置。

    引用符を尊重する。区切りは ; & | ( ) ` と改行 (引用符の外)。ヒアドキュメントの本文・# コメントは
    読み飛ばす。ダブルクォートの中の $( ) と `` は再帰して中の文も積む (closer がその閉じ)。
    1 文字は高々 (再帰の深さ) 回しか読まない = 線形。
    """
    n = len(cmd)
    words: list[str] = []
    buf: list[str] = []
    has = False
    start = i
    paren = 0
    heredocs: list[tuple[str, bool]] = []

    def flush() -> None:
        nonlocal has
        if has:
            words.append("".join(buf))
        buf.clear()
        has = False

    def end_stmt(end: int, nxt: int) -> None:
        nonlocal words, start
        flush()
        if words:
            out.append((cmd[start:end].strip(), words))
        words = []
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
            buf.append(cmd[i + 1:j])
            has = True
            i = j + 1
            continue
        if c == '"':
            has = True
            i += 1
            while i < n and cmd[i] != '"':
                d = cmd[i]
                if d == "\\" and i + 1 < n:
                    buf.append(cmd[i + 1] if cmd[i + 1] in '\\"$`' else "\\" + cmd[i + 1])
                    i += 2
                    continue
                if depth < _MAX_NEST and d == "$" and cmd.startswith("(", i + 1):
                    buf.append("$(")
                    i = _scan(cmd, i + 2, depth + 1, out, ")")
                    continue
                if depth < _MAX_NEST and d == "`":
                    buf.append("`")
                    i = _scan(cmd, i + 1, depth + 1, out, "`")
                    continue
                buf.append(d)
                i += 1
            i += 1
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
            for delim, tabs in heredocs:
                while i < n:
                    eol = cmd.find("\n", i)
                    eol = n if eol < 0 else eol
                    line = cmd[i:eol]
                    i = eol + 1
                    if (line.lstrip("\t") if tabs else line) == delim:
                        break
            heredocs = []
            start = i
            continue
        if c == "<" and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            j = i + 2
            tabs = j < n and cmd[j] == "-"
            if tabs:
                j += 1
            while j < n and cmd[j] in " \t":
                j += 1
            d: list[str] = []
            while j < n and cmd[j] not in " \t\n;&|()<>":
                if cmd[j] in "'\"\\":
                    j += 1
                    continue
                d.append(cmd[j])
                j += 1
            heredocs.append(("".join(d), tabs))
            flush()
            i = j
            continue
        if c == "&" and ((i > 0 and cmd[i - 1] == ">") or cmd.startswith(">", i + 1)):
            buf.append(c)
            has = True
            i += 1
            continue
        if c in ";&|`" or c in "()":
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


def _strip_prefix(words: list[str]) -> list[str]:
    """VAR=val・env・command・builtin・exec・nohup・time・nice・timeout・制御構文の語・パス指定を外す。"""
    words = list(words)
    for _ in range(64):  # 前置きの積み重ねの上限 (無限ループ防止)
        if not words:
            return words
        w0 = words[0]
        base = w0.rsplit("/", 1)[-1] if "/" in w0 else w0
        if w0 in _KEYWORDS or _ASSIGN_RE.match(w0):
            words = words[1:]
        elif base == "env":
            k = 1
            while k < len(words) and (words[k].startswith("-") or _ASSIGN_RE.match(words[k])):
                k += 2 if words[k] in ("-u", "-C", "-S", "--unset", "--chdir", "--split-string") else 1
            words = words[k:]
        elif base in ("command", "builtin", "exec", "nohup", "setsid", "time"):
            k = 1
            while k < len(words) and words[k].startswith("-") and words[k] != "--":
                k += 2 if (base == "exec" and words[k] == "-a") else 1
            if k < len(words) and words[k] == "--":
                k += 1
            words = words[k:]
        elif base == "nice":
            k = 1
            while k < len(words) and words[k].startswith("-"):
                k += 2 if words[k] in ("-n", "--adjustment") else 1
            words = words[k:]
        elif base == "timeout":
            k = 1
            while k < len(words) and words[k].startswith("-"):
                k += 2 if words[k] in ("-s", "-k", "--signal", "--kill-after") else 1
            words = words[k + 1:]  # DURATION
        else:
            break
    if words and "/" in words[0]:
        words[0] = words[0].rsplit("/", 1)[-1]
    return words


def _normalized_forms(words: list[str]) -> list[list[str]]:
    """1 文の語列 → 照合する語列 (前置きを外したもの。git なら大域オプションも外したもの)。"""
    norm = _strip_prefix(words)
    forms = [norm] if norm else []
    if norm and norm[0] == "git":
        forms.append(_strip_git_globals(norm))
    return forms


def _candidate_sigs(cmd: str) -> list[str]:
    """cmd の各文について 元の文・前置きを外した形・git の大域オプションも外した形 を Bash(...) にして返す。
    bash -c / sh -c / eval の引数は 1 段だけ中を見る。"""
    out: list[tuple[str, list[str]]] = []
    _scan(cmd, 0, 0, out)
    sigs: dict[str, None] = {}
    inner: list[str] = []
    for raw, words in out:
        if raw:
            sigs[f"Bash({raw})"] = None
        for form in _normalized_forms(words):
            sigs[f"Bash({' '.join(form)})"] = None
            head = form[0]
            if head in _SHELLS:
                for k in range(1, len(form)):
                    t = form[k]
                    if t.startswith("-") and not t.startswith("--") and "c" in t[1:] and k + 1 < len(form):
                        inner.append(form[k + 1])
                        break
            elif head == "eval" and len(form) > 1:
                inner.append(" ".join(form[1:]))
    for text in inner:  # 1 段だけ
        sub: list[tuple[str, list[str]]] = []
        _scan(text, 0, _MAX_NEST, sub)
        for raw, words in sub:
            if raw:
                sigs[f"Bash({raw})"] = None
            for form in _normalized_forms(words):
                sigs[f"Bash({' '.join(form)})"] = None
    return list(sigs)


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
    if cmd is not None and global_deny:
        for sig in _candidate_sigs(cmd):
            for pattern in global_deny:
                if fnmatch(sig, pattern):
                    return {"decision": "deny", "source": f"_global:deny:{pattern}"}

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
