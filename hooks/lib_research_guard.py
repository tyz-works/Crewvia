#!/usr/bin/env python3
"""hooks/lib_research_guard.py — research skill の構造的ガード (pre-tool-use.sh から呼ぶ)

  python3 lib_research_guard.py write <repo> <agent> <tool_path> <cwd> <skill_perms_yaml>
  python3 lib_research_guard.py bash [--repo R --agent A --cwd C] [<command>]   (コマンドは引数、無ければ stdin)

どちらも 1 行の JSON を出す: {"decision": "allow"|"deny", "reason": "..."}
**判断に迷う形は deny (fail closed)**。例外で落ちたときも呼び出し側 (hook) は deny に倒す。

(1) write: research の Write/Edit/MultiEdit/NotebookEdit が書いてよい範囲
    - <repo>/queue/missions/<自分の assignment の slug>/notes/<自分の tid>*.md  (上書き可)
    - skill-permissions.yaml の research_write_scope.obsidian_research_dir を指定したときだけ
      <その dir>/YYYYMMDD_*.md  (新規作成のみ・既定は無効)
    パスは realpath で解決して判定し、`..` や symlink で範囲外に出る形・既存の symlink は拒否する。

(2) bash: research の Bash は「解釈が要らない形」だけを通す読み取り専用の allowlist (t005)。
    $ / バッククォート / # / heredoc / 状態を変える builtin・ラッパー / 言語を持つツール (awk・python3 等)
    は拒否し、残りはコマンドごとのオプション allowlist (SPECS / GIT_SPECS / GH_SPECS / PLAN_SPECS) で検査する。
    plan.sh done / needs-director の結果は --result-file <自分の notes/<tid>*.md> だけ (--repo/--agent/--cwd が要る)。
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


def _own_notes(repo: str, agent: str, path: str) -> tuple[str, str] | None:
    """path (realpath で解決する) が自分の assignment の notes/<tid>*.md なら (slug, tid)。"""
    real = os.path.realpath(path)
    repo_real = os.path.realpath(repo)
    asg = _read_assignment(repo_real, agent)
    if not asg:
        return None
    slug, tid = asg
    notes = os.path.join(repo_real, "queue", "missions", slug, "notes")
    if os.path.dirname(real) == notes and re.match(
        r"^" + re.escape(tid) + r"(?:[^0-9][A-Za-z0-9._-]*)?\.md$", os.path.basename(real)
    ):
        return slug, tid
    return None


def check_write(repo: str, agent: str, raw_path: str, cwd: str, yaml_path: str) -> tuple[str, str]:
    if not raw_path:
        return "deny", "書き込み先が不明です"
    abspath = raw_path if os.path.isabs(raw_path) else os.path.join(cwd, raw_path)
    norm = os.path.normpath(abspath)  # `..` を字句的に畳む
    real = os.path.realpath(abspath)  # symlink を解決
    # 親 dir が「字句的な形」と「解決後」で一致しないもの (= 経路に symlink か `..` の抜け) は通さない
    if os.path.dirname(norm) != os.path.dirname(real) or os.path.islink(abspath):
        return "deny", "symlink や .. を経由するパスへの書き込みは許可されません"

    asg = _own_notes(repo, agent, real)
    if asg:
        slug, tid = asg
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
#
# 方針 (t005): bash の文法を部分的に解釈して危ない形を探すのをやめ、**解釈が要らない形だけ**
# を通す。展開・置換・heredoc・コメント・状態を変える builtin は文字や語の段階で拒否し、
# 残った「コマンド名 + 引数」の並びを、コマンドごとのオプション allowlist で検査する。
#   - 文字で拒否: `$` (変数・コマンド置換すべて) / バッククォート / `#` / `<<` (heredoc・here-string)
#   - 引用符の外で拒否: ( ) { } ! (サブシェル・グループ・ブレース展開・否定)
#   - glob (* ? [) は「先頭が - でない字そのもの」で始まる語だけ (展開しても別のオプションにならない)
#   - コマンド名は下の表のどれかに完全一致 (パス指定は plan.sh だけ)。export / env / command /
#     exec / eval / xargs / 先頭の X=… などは表に無いので拒否 = PATH / LD_PRELOAD は変えられない
#   - オプションは表に列挙したものだけ。結合形 (-ovictim / -XDELETE) も 1 字ずつ表と照合する。
#     値を次の語から取るオプションの値が - で始まるものは拒否 (取り違えで別オプションを見逃さない)


class _Reject(Exception):
    pass


class _Word:
    __slots__ = ("value", "glob_at")

    def __init__(self, value: str, glob_at: int) -> None:
        self.value = value
        self.glob_at = glob_at  # 引用符の外の glob 文字 (* ? [) が最初に現れる位置。無ければ -1

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"_Word({self.value!r}, {self.glob_at})"


_FORBIDDEN_TEXT = (
    ("<<", "heredoc / here-string (<<) は拒否します。結果は Write でメモに書き、--result-file にそのパスを渡してください"),
    ("$", "$ (変数展開・コマンド置換) は拒否します"),
    ("`", "バッククォート (コマンド置換) は拒否します"),
    ("#", "# (コメント) は拒否します (引用符の中も一律)"),
    ("\r", "CR を含むコマンドは拒否します"),
    ("\x00", "NUL を含むコマンドは拒否します"),
)
_GLOB_CHARS = "*?["
_REJECT_UNQUOTED = "(){}!"


def _tokenize(cmd: str) -> list[list[_Word]]:
    """区切り (; | || && |& 改行) で区間に分け、各区間を語の列にする。

    リダイレクトは /dev/null 宛ての出力・fd の複製 (2>&1)・入力 (< file) だけを認め、語からは除く。
    """
    for needle, why in _FORBIDDEN_TEXT:
        if needle in cmd:
            raise _Reject(why)
    segs: list[list[_Word]] = []
    cur: list[_Word] = []
    word = ""
    has_word = False
    glob_at = -1
    pending_in = False  # 直前が `<` (次の語は読む入力ファイル。引数ではない)
    i, n = 0, len(cmd)

    def flush_word() -> None:
        nonlocal word, has_word, glob_at, pending_in
        if has_word:
            if pending_in:
                if glob_at >= 0:
                    raise _Reject("入力リダイレクト先に glob は使えません")
                pending_in = False
            else:
                cur.append(_Word(word, glob_at))
        word, has_word, glob_at = "", False, -1

    def end_seg() -> None:
        nonlocal cur
        flush_word()
        if pending_in:
            raise _Reject("入力リダイレクト (<) の先がありません")
        if cur:
            segs.append(cur)
        cur = []

    def drop_fd_number() -> None:
        # `2>&1` の 2 は直前の語として溜まっている (引用符の無い数字だけ)
        nonlocal word, has_word, glob_at
        if has_word and word.isdigit() and glob_at < 0:
            word, has_word, glob_at = "", False, -1

    while i < n:
        c = cmd[i]
        if c == "\\":
            if i + 1 >= n:
                raise _Reject("行末の \\ は拒否します")
            if cmd[i + 1] == "\n":
                i += 2
                continue
            word += cmd[i + 1]
            has_word = True
            i += 2
            continue
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
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    nxt = cmd[j + 1]
                    if nxt == "\n":
                        j += 2
                        continue
                    word += nxt if nxt in '\\"' else "\\" + nxt
                    j += 2
                    continue
                word += cmd[j]
                j += 1
            if j >= n:
                raise _Reject('閉じていない "')
            has_word = True
            i = j + 1
            continue
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
            i += 2 if cmd.startswith(("||", "|&"), i) else 1
            continue
        if c == "&":
            if cmd.startswith("&&", i):
                end_seg()
                i += 2
                continue
            if cmd.startswith("&>", i):
                m = re.match(r"&>>?[ \t]*/dev/null(?=[ \t\n;|&]|$)", cmd[i:])
                if not m or pending_in:
                    raise _Reject("&> は /dev/null 宛てだけ許可します")
                flush_word()
                i += m.end()
                continue
            raise _Reject("バックグラウンド実行 (&) は拒否します")
        if c == "<":
            drop_fd_number()
            if cmd.startswith(("<>", "<&", "<("), i) or pending_in:
                raise _Reject("<> / <& / <( は拒否します")
            flush_word()
            pending_in = True
            i += 1
            continue
        if c == ">":
            drop_fd_number()
            m = re.match(r">>?[ \t]*/dev/null(?=[ \t\n;|&]|$)", cmd[i:]) or re.match(r">&[12](?=[ \t\n;|&]|$)", cmd[i:])
            if not m or pending_in:
                raise _Reject("ファイルへのリダイレクト (>, >>) は拒否します (/dev/null と 2>&1 だけ可)")
            flush_word()
            i += m.end()
            continue
        if c in _REJECT_UNQUOTED:
            raise _Reject(f"引用符の外の {c} (サブシェル・グループ・ブレース展開・否定) は拒否します")
        if c in _GLOB_CHARS and glob_at < 0:
            glob_at = len(word)
        word += c
        has_word = True
        i += 1
    end_seg()
    return segs


# ---- コマンドごとのオプション表


class Spec:
    """1 つのコマンド (または git / gh / plan.sh のサブコマンド) が受け付ける引数の形。

    short:     値を取らない短いオプションの文字
    short_arg: 値を取る短いオプションの文字 (直後に結合 -k2 か、次の語)
    long:      値を取らない長いオプション (--x)
    long_arg:  値を取る長いオプション (--x=v か --x v)
    long_opt:  値が任意の長いオプション (--x か --x=v。次の語は取らない)
    numeric:   -5 のような数字だけのオプションを認める (head / tail / grep / git log)
    digits:    結合形の中の数字を認める (git の -U3 / -n5)
    max_pos:   位置引数の上限 (None = 無制限)。上限があるコマンドの位置引数は glob 不可
    any_args:  引数を一切検査しない (echo のように、どんな引数でも読み書きも実行もしないもの)
    check:     追加の検査 (opts, positionals, ctx) -> None (拒否は _Reject)
    """

    def __init__(self, short="", short_arg="", long=(), long_arg=(), long_opt=(), numeric=False,
                 digits=False, max_pos=None, any_args=False, check=None):
        self.short = short
        self.short_arg = short_arg
        self.long = frozenset(long)
        self.long_arg = frozenset(long_arg)
        self.long_opt = frozenset(long_opt)
        self.numeric = numeric
        self.digits = digits
        self.max_pos = max_pos
        self.any_args = any_args
        self.check = check

    def all_long(self) -> frozenset:
        return self.long | self.long_arg | self.long_opt


def _w(s: str) -> list[str]:
    return s.split()


_GREP = Spec(
    short="nirRlLcvwxEFGPohHsqzaIZTUb", short_arg="eABCmfdD", numeric=True,
    long=_w("recursive dereference-recursive line-number ignore-case no-ignore-case files-with-matches "
            "files-without-match count invert-match word-regexp line-regexp extended-regexp fixed-strings "
            "basic-regexp perl-regexp only-matching no-filename with-filename no-messages quiet silent null "
            "null-data text byte-offset initial-tab"),
    long_arg=_w("include exclude exclude-dir regexp after-context before-context context max-count file label "
                "binary-files directories devices"),
    long_opt=_w("color colour"),
)
_RG = Spec(
    short="nilcvwFSuUoNHsLxa.P0IqpVb", short_arg="egtTABCmMEjr", numeric=False,
    long=_w("hidden no-ignore no-ignore-vcs no-ignore-parent files files-with-matches files-without-match count "
            "count-matches line-number no-line-number ignore-case smart-case case-sensitive fixed-strings "
            "word-regexp line-regexp invert-match only-matching no-heading heading with-filename no-filename "
            "follow json vimgrep multiline multiline-dotall pcre2 null no-messages column no-column trim "
            "unrestricted sort-files stats binary text no-config pretty byte-offset passthru"),
    long_arg=_w("glob iglob type type-not max-count max-depth after-context before-context context regexp sort sortr "
                "color colors max-columns threads replace encoding max-filesize"),
)
_UGREP = Spec(
    short="nirRlLcvwxEFGPohHsqIbjSUkTWYy%+", short_arg="eABCmfdDgtMNK", numeric=True,
    long=_w("recursive dereference-recursive line-number ignore-case smart-case files-with-matches "
            "files-without-match count invert-match word-regexp line-regexp extended-regexp fixed-strings "
            "basic-regexp perl-regexp only-matching no-filename with-filename no-messages quiet silent null "
            "text byte-offset initial-tab hidden ignore-files column-number line-buffered binary-files"),
    long_arg=_w("include exclude exclude-dir include-dir glob iglob regexp after-context before-context context "
                "max-count file label file-type max-depth min-depth"),
    long_opt=_w("color colour"),
)


def _check_sed(opts, pos, ctx) -> None:
    scripts = [v for k, v in opts if k in ("-e", "--expression")]
    files = pos
    if not scripts:
        if not pos:
            raise _Reject("sed のスクリプトがありません")
        if pos[0].glob_at >= 0:
            raise _Reject("sed のスクリプトに glob は使えません")
        scripts, files = [pos[0].value], pos[1:]
    del files
    for s in scripts:
        for part in [s] if s.startswith("s") else s.split(";"):
            if not _SED_SCRIPT_OK.match(part.strip()):
                raise _Reject("sed は 行指定の p / = と s///[gIip] だけ許可します (w / e / r 等は拒否)")


_SED_SCRIPT_OK = re.compile(
    r"^(?:\$|\d+|/[^/\n]*/)(?:,(?:\$|\d+|/[^/\n]*/))?!?[pP=]$"
    r"|^(?:\d+|\$)?[pP=]$"
    r"|^s([^\\\n\w])(?:(?!\1).)*\1(?:(?!\1).)*\1[gIip0-9]*$"
)


def _check_find(words: list[_Word]) -> None:
    """find は「始点 + 述語」の文法なので専用に検査する (述語の allowlist)。"""
    noarg = set(_w("-print -print0 -prune -empty -ls -not -o -a -or -and -true -false -follow -depth "
                   "-readable -executable -writable -xdev -mount -nouser -nogroup -quit"))
    witharg = set(_w("-name -iname -path -ipath -wholename -iwholename -regex -iregex -type -xtype -maxdepth "
                     "-mindepth -newer -mtime -mmin -atime -amin -ctime -cmin -size -perm -user -group -printf "
                     "-samefile -links -inum -regextype -newermt -uid -gid -lname -ilname -used"))
    i = 0
    while i < len(words) and words[i].value in ("-L", "-P", "-H"):
        i += 1
    seen_pred = False
    while i < len(words):
        w = words[i]
        a = w.value
        if a in ("(", ")", "!", ","):
            seen_pred = True
            i += 1
            continue
        if a.startswith("-"):
            if w.glob_at >= 0:
                raise _Reject("find の述語に glob は使えません")
            seen_pred = True
            if a in noarg:
                i += 1
                continue
            if a in witharg:
                if i + 1 >= len(words):
                    raise _Reject(f"find {a} の値がありません")
                v = words[i + 1]
                if v.glob_at == 0:
                    raise _Reject(f"find {a} の値が引用符の無い glob で始まっています ('...' で囲んでください)")
                i += 2
                continue
            raise _Reject(f"find {a} は allowlist にありません (-exec -delete -fprint* -ok 等は不可)")
        if seen_pred:
            raise _Reject(f"find の述語の後ろの {a!r} は解釈できません")
        i += 1  # 始点


def _check_git_fetch(opts, pos, ctx) -> None:
    for w in pos:
        if ":" in w.value or w.value.startswith("+"):
            raise _Reject("git fetch の refspec (src:dst / +) はローカルの ref を書き換えるため拒否します")


def _check_no_ext_transport(opts, pos, ctx) -> None:
    for w in pos:
        if "::" in w.value:
            raise _Reject("git の <transport>::<address> 形式は拒否します")


def _require_one_of(*names: str):
    def check(opts, pos, ctx) -> None:
        if not any(k in names for k, _ in opts):
            raise _Reject(f"このサブコマンドは {' / '.join(names)} のどれかが必要です (読み取りの形だけ許可)")
    return check


def _require_list_if_positional(opts, pos, ctx) -> None:
    if pos and not any(k in ("-l", "--list") for k, _ in opts):
        raise _Reject("名前を渡すと作成になるため、位置引数があるときは -l / --list が必要です")


SPECS: dict[str, Spec] = {
    "cat": Spec(short="nAvetsbETu", long=_w("number number-nonblank show-all show-ends show-tabs show-nonprinting squeeze-blank")),
    "head": Spec(short="qvz", short_arg="nc", numeric=True, long=_w("quiet silent verbose zero-terminated"), long_arg=_w("lines bytes")),
    "tail": Spec(short="qvz", short_arg="nc", numeric=True, long=_w("quiet silent verbose zero-terminated"), long_arg=_w("lines bytes")),
    "wc": Spec(short="lcwmL", long=_w("lines bytes chars words max-line-length")),
    "ls": Spec(short="laAhRtrSd1FiGgnopsuUcCXvxL", long=_w("all almost-all human-readable recursive reverse directory classify full-time"), long_opt=_w("color")),
    "stat": Spec(short="Lft", short_arg="c", long=_w("dereference terse file-system"), long_arg=_w("format printf")),
    "file": Spec(short="bLih", long=_w("brief mime mime-type mime-encoding dereference")),
    "realpath": Spec(short="emsqPLz", long=_w("canonicalize-existing canonicalize-missing no-symlinks strip quiet zero logical physical"), long_arg=_w("relative-to relative-base")),
    "dirname": Spec(short="z", long=_w("zero")),
    "basename": Spec(short="az", short_arg="s", long=_w("multiple zero"), long_arg=_w("suffix")),
    "diff": Spec(short="uqrNwbBiyEtTsapZ", short_arg="UxXIW", long=_w("brief recursive new-file ignore-all-space ignore-space-change ignore-blank-lines ignore-case side-by-side text report-identical-files minimal strip-trailing-cr suppress-common-lines ignore-trailing-space expand-tabs no-dereference"), long_arg=_w("exclude label width ignore-matching-lines"), long_opt=_w("unified context color")),
    "cmp": Spec(short="lsb", short_arg="in", long=_w("verbose silent quiet print-bytes"), max_pos=4),
    "nl": Spec(short="p", short_arg="bnwsvi"),
    "sort": Spec(short="nrufbdghiMRVsz", short_arg="ktS", long=_w("numeric-sort reverse unique ignore-case human-numeric-sort version-sort general-numeric-sort month-sort stable zero-terminated ignore-leading-blanks dictionary-order random-sort"), long_arg=_w("key field-separator")),
    "uniq": Spec(short="cdDuiz", short_arg="fsw", long=_w("count repeated unique ignore-case zero-terminated"), long_arg=_w("skip-fields skip-chars check-chars"), max_pos=1),  # 2 つ目の位置引数は出力ファイル
    "cut": Spec(short="sz", short_arg="dfcb", long=_w("complement only-delimited zero-terminated"), long_arg=_w("delimiter fields characters bytes output-delimiter")),
    "tr": Spec(short="cdsCt", long=_w("complement delete squeeze-repeats truncate-set1"), max_pos=2),
    "rev": Spec(),
    "tac": Spec(short="br", short_arg="s"),
    "column": Spec(short="tnx", short_arg="sc"),
    "paste": Spec(short="sz", short_arg="d"),
    "comm": Spec(short="123iz", long=_w("nocheck-order check-order"), max_pos=2),
    "echo": Spec(any_args=True),
    "printf": Spec(),  # オプション無し (-v で変数を書く形は拒否)
    "pwd": Spec(short="LP", max_pos=0),
    "date": Spec(short="uR", short_arg="d", long=_w("utc rfc-email"), long_arg=_w("date"), max_pos=1),
    "which": Spec(short="a"),
    "printenv": Spec(),  # echo "$X" の代わり (env の値を読むだけ。オプション無し)
    "true": Spec(max_pos=0),
    "false": Spec(max_pos=0),
    "sleep": Spec(max_pos=2),
    "cd": Spec(short="LP", max_pos=1),
    "grep": _GREP,
    "egrep": _GREP,
    "fgrep": _GREP,
    "rg": _RG,
    "ugrep": _UGREP,
    "jq": Spec(short="rcnsejSaCMR", long=_w("raw-output compact-output null-input exit-status sort-keys tab ascii-output join-output raw-input monochrome-output color-output raw-output0 seq stream"), long_arg=_w("indent")),
    "tree": Spec(short="adfiplsughDFCnAJ", short_arg="LIP", long=_w("noreport dirsfirst gitignore")),
    "du": Spec(short="shacLxb", short_arg="d", long=_w("summarize human-readable apparent-size total"), long_arg=_w("max-depth")),
    "df": Spec(short="hTiPlak"),
    "readlink": Spec(short="femnqsvz", long=_w("canonicalize canonicalize-existing canonicalize-missing no-newline quiet silent verbose zero")),
    "sha256sum": Spec(short="bctwz", long=_w("binary check tag text warn strict quiet status zero")),
    "md5sum": Spec(short="bctwz", long=_w("binary check tag text warn strict quiet status zero")),
    "ps": Spec(short="efaxlwuHjMTyAN", short_arg="opCUgGt", long_arg=_w("sort format pid ppid")),
    "pgrep": Spec(short="flaxvcnoi", short_arg="uUgPGst"),
    "od": Spec(short="bcdfiloxsv", short_arg="AtNjSw"),
    "strings": Spec(short="afw", short_arg="nte"),
    "sed": Spec(short="nErszu", short_arg="el", long=_w("quiet silent regexp-extended posix separate null-data unbuffered debug sandbox"), long_arg=_w("expression line-length"), check=_check_sed),
}

_GIT_READ_SPEC = Spec(
    short="pszrtlivwcaEFhHqobuWIMCnmdk", short_arg="eLSG", numeric=True, digits=True,
    long=_w(
        "oneline stat shortstat numstat name-only name-status patch no-patch graph all reverse first-parent "
        "no-merges merges follow abbrev-commit no-color cached staged ignore-all-space ignore-space-change "
        "full-diff raw summary dirstat exit-code quiet source topo-order date-order left-right cherry-pick "
        "cherry-mark boundary branches tags remotes ancestry-path simplify-by-decoration no-abbrev full-index "
        "check no-renames minimal patience histogram no-walk parents children walk-reflogs invert-grep all-match "
        "regexp-ignore-case extended-regexp fixed-strings perl-regexp no-ext-diff no-textconv no-prefix "
        "function-context pickaxe-all pickaxe-regex compact-summary root no-commit-id count "
        "show-toplevel git-dir symbolic-full-name verify is-inside-work-tree absolute-git-dir show-prefix "
        "git-common-dir show-cdup others modified deleted exclude-standard error-unmatch full-name stage ignored "
        "directory unmerged line-number ignore-case word-regexp files-with-matches files-without-match heading "
        "break untracked invert-match only-matching show-function null column porcelain line-porcelain "
        "incremental show-email show-name show-number full-tree long object-only is-ancestor fork-point "
        "octopus independent always exact-match heads hash head exists verbose human-readable non-matching "
        "no-index list ahead-behind no-ahead-behind short-stat batch-check batch symref get-url refs branch "
        "short-sha"
    ),
    long_arg=_w("since until after before author committer grep max-count skip date format diff-filter sort "
                "contains merged no-merged points-at match exclude max-depth"),
    long_opt=_w("color decorate word-diff unified relative find-renames find-copies dirstat-by-file abbrev pretty "
                "short abbrev-ref untracked-files ignored-mode porcelain-v dirty broken"),
)
GIT_SPECS: dict[tuple, Spec] = {(s,): _GIT_READ_SPEC for s in _w(
    "log show diff status rev-parse ls-files grep blame rev-list cat-file ls-tree merge-base describe shortlog "
    "name-rev show-ref for-each-ref diff-tree diff-files diff-index count-objects whatchanged show-branch "
    "check-ignore version var"
)}
GIT_SPECS[("ls-remote",)] = Spec(short="qht", long=_w("heads tags refs quiet get-url symref"), long_arg=_w("sort"), check=_check_no_ext_transport)
GIT_SPECS[("fetch",)] = Spec(short="qvp", long=_w("prune prune-tags tags no-tags all quiet verbose dry-run"), long_arg=_w("depth"), check=lambda o, p, c: (_check_git_fetch(o, p, c), _check_no_ext_transport(o, p, c)))
GIT_SPECS[("branch",)] = Spec(short="arvl", long=_w("all remotes verbose list show-current"), long_arg=_w("contains merged no-merged sort format points-at"), check=_require_list_if_positional)
GIT_SPECS[("tag",)] = Spec(short="ln", digits=True, long=_w("list"), long_arg=_w("contains sort points-at merged no-merged"), check=_require_list_if_positional)
GIT_SPECS[("remote",)] = Spec(short="v", long=_w("verbose"), max_pos=0)
GIT_SPECS[("remote", "show")] = Spec(short="n", max_pos=1)
GIT_SPECS[("worktree", "list")] = Spec(short="vz", long=_w("porcelain verbose"), max_pos=0)
GIT_SPECS[("stash", "list")] = Spec(long=_w("oneline stat"), max_pos=0)
GIT_SPECS[("config",)] = Spec(short="lz", long=_w("get get-all get-regexp list show-origin show-scope local global system name-only null"), max_pos=2,
                              check=_require_one_of("--get", "--get-all", "--get-regexp", "--list", "-l"))
# git の大域オプションで通すもの (-c / --exec-path / --git-dir / --work-tree / --config-env 等は不可)
_GIT_GLOBAL_NOARG = ("--no-pager", "-P", "--no-optional-locks", "--no-replace-objects")

_GH_COMMON = Spec(
    short="cv", short_arg="qtRLsbBHAlSaue",
    long=_w("log log-failed comments name-only patch exit-status required verbose"),
    long_arg=_w("json jq template repo limit state branch workflow base head author label search assignee user status "
                "event commit attempt job created color sort order owner language match"),
)
GH_SPECS: dict[tuple, Spec] = {k: _GH_COMMON for k in (
    ("pr", "view"), ("pr", "diff"), ("pr", "checks"), ("pr", "list"), ("pr", "status"),
    ("run", "list"), ("run", "view"), ("issue", "view"), ("issue", "list"),
    ("repo", "view"), ("release", "list"), ("release", "view"), ("workflow", "list"),
    ("workflow", "view"), ("search", "prs"), ("search", "issues"), ("search", "code"),
    ("search", "repos"), ("search", "commits"),
)}


def _check_gh_api(opts, pos, ctx) -> None:
    if len(pos) != 1:
        raise _Reject("gh api は API パスを 1 つだけ渡してください")
    if pos[0].value == "graphql":
        raise _Reject("gh api graphql は POST のため拒否します")


# gh api は「パス + --jq / -q / --paginate」だけ (-X / --method / -f / -F / --field / --raw-field /
# --input / -H は結合形も含め、列挙していないので拒否される)
GH_SPECS[("api",)] = Spec(short_arg="q", long=_w("paginate"), long_arg=_w("jq"), check=_check_gh_api)


def _check_plan_result(opts, pos, ctx) -> None:
    if any(k in ("-h", "--help") for k, _ in opts):
        return  # usage を出すだけで何も書かない
    files = [v for k, v in opts if k == "--result-file"]
    if not files:
        raise _Reject("plan.sh done / needs-director の結果は --result-file <自分の notes/<tid>*.md> で渡してください "
                      "(位置引数の文字列・--result-file - は不可)")
    if len(files) != 1:
        raise _Reject("--result-file は 1 つだけ")
    path = files[0]
    if not os.path.isabs(path):
        if ctx.get("cd_seen"):
            raise _Reject("cd の後ろでは --result-file を絶対パスで渡してください")
        path = os.path.join(ctx.get("cwd") or "", path)
    if not ctx.get("repo") or not _own_notes(ctx["repo"], ctx.get("agent", ""), path):
        raise _Reject("--result-file に渡せるのは queue/missions/<自分の slug>/notes/<自分の tid>*.md だけです")


_PLAN_HELP = dict(short="h", long=_w("help"))
PLAN_SPECS: dict[tuple, Spec] = {
    ("status",): Spec(**_PLAN_HELP | {"long": _w("help all")}, long_arg=_w("mission"), max_pos=0),
    ("pull",): Spec(**_PLAN_HELP, long_arg=_w("mission skills agent target-dir task"), max_pos=0),
    ("done",): Spec(**_PLAN_HELP, long_arg=_w("mission result-file pr no-pr execution"), max_pos=1, check=_check_plan_result),
    ("needs-director",): Spec(**_PLAN_HELP, long_arg=_w("mission result-file execution"), max_pos=1, check=_check_plan_result),
    ("pr-base",): Spec(**_PLAN_HELP | {"long": _w("help diff-ref")}, long_arg=_w("mission task"), max_pos=0),
    ("store-check",): Spec(**_PLAN_HELP, long_arg=_w("mission"), max_pos=0),
}


def all_specs():
    """(コマンドの先頭の語の列, Spec) をすべて列挙する (テストが表から全組み合わせを作るための入口)。"""
    for name, spec in SPECS.items():
        yield [name], spec
    for key, spec in GIT_SPECS.items():
        yield ["git", *key], spec
    for key, spec in GH_SPECS.items():
        yield ["gh", *key], spec
    for key, spec in PLAN_SPECS.items():
        yield ["./scripts/plan.sh", *key], spec


def _value_word(words: list[_Word], i: int, label: str) -> str:
    if i >= len(words):
        raise _Reject(f"{label} の値がありません")
    w = words[i]
    if w.value.startswith("-"):
        raise _Reject(f"{label} の値が - で始まっています (別のオプションの取り違えを防ぐため拒否)")
    if w.glob_at >= 0:
        raise _Reject(f"{label} の値に引用符の無い glob は使えません")
    return w.value


def _parse(spec: Spec, words: list[_Word], label: str):
    opts: list[tuple[str, str | None]] = []
    pos: list[_Word] = []
    i = 0
    only_pos = False
    while i < len(words):
        w = words[i]
        a = w.value
        if only_pos or a == "-" or not a.startswith("-"):
            if w.glob_at == 0:
                raise _Reject(f"{label}: 先頭が glob の語 {a!r} は拒否します (展開でオプションになりうる)")
            if a.startswith("-") and w.glob_at >= 0:
                raise _Reject(f"{label}: - で始まる glob {a!r} は拒否します")
            pos.append(w)
            i += 1
            continue
        if a == "--":
            only_pos = True
            i += 1
            continue
        if spec.numeric and re.fullmatch(r"-\d+", a):
            opts.append(("-N", a[1:]))
            i += 1
            continue
        if w.glob_at >= 0 and not (a.startswith("--") and "=" in a[:w.glob_at]):
            raise _Reject(f"{label}: オプション {a!r} に引用符の無い glob は使えません")
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            if name in spec.long_arg:
                if not eq:
                    val = _value_word(words, i + 1, f"{label} --{name}")
                    i += 1
            elif name in spec.long_opt:
                pass
            elif name in spec.long and not eq:
                pass
            else:
                raise _Reject(f"{label} の --{name} は allowlist にありません")
            opts.append(("--" + name, val if eq or name in spec.long_arg else None))
            i += 1
            continue
        j = 1
        while j < len(a):
            c = a[j]
            if c in spec.short or (spec.digits and c.isdigit()):
                opts.append(("-" + c, None))
                j += 1
                continue
            if c in spec.short_arg:
                rest = a[j + 1:]
                if not rest:
                    rest = _value_word(words, i + 1, f"{label} -{c}")
                    i += 1
                opts.append(("-" + c, rest))
                break
            raise _Reject(f"{label} の -{c} は allowlist にありません ({a!r})")
        i += 1
    if spec.max_pos is not None:
        if len(pos) > spec.max_pos:
            raise _Reject(f"{label} の位置引数が多すぎます (出力先になりうる形は拒否)")
        if any(p.glob_at >= 0 for p in pos):
            raise _Reject(f"{label} の位置引数に glob は使えません (個数が変わると出力先になりうる)")
    return opts, pos


def _run_spec(spec: Spec, words: list[_Word], label: str, ctx: dict) -> None:
    if spec.any_args:
        return
    opts, pos = _parse(spec, words, label)
    if spec.check:
        spec.check(opts, pos, ctx)


def _check_git(words: list[_Word], ctx: dict) -> None:
    i = 0
    while i < len(words):
        a = words[i].value
        if words[i].glob_at >= 0:
            raise _Reject("git の大域オプション・サブコマンドに glob は使えません")
        if a == "-C":
            _value_word(words, i + 1, "git -C")
            i += 2
            continue
        if a.startswith("-C") and len(a) > 2:
            i += 1
            continue
        if a in _GIT_GLOBAL_NOARG:
            i += 1
            continue
        if a.startswith("-"):
            raise _Reject(f"git の大域オプション {a} は拒否します (-c / --exec-path / --git-dir 等)")
        break
    if i >= len(words):
        raise _Reject("git のサブコマンドがありません")
    sub = words[i].value
    nxt = words[i + 1].value if i + 1 < len(words) else None
    if (sub, nxt) in GIT_SPECS:
        _run_spec(GIT_SPECS[(sub, nxt)], words[i + 2:], f"git {sub} {nxt}", ctx)
        return
    if (sub,) in GIT_SPECS:
        _run_spec(GIT_SPECS[(sub,)], words[i + 1:], f"git {sub}", ctx)
        return
    raise _Reject(f"git {sub} は読み取り専用の allowlist にありません")


def _check_gh(words: list[_Word], ctx: dict) -> None:
    vals = [w.value for w in words]
    if vals[:1] == ["api"]:
        _run_spec(GH_SPECS[("api",)], words[1:], "gh api", ctx)
        return
    if tuple(vals[:2]) in GH_SPECS:
        _run_spec(GH_SPECS[tuple(vals[:2])], words[2:], f"gh {vals[0]} {vals[1]}", ctx)
        return
    raise _Reject("この gh サブコマンドは読み取り専用の allowlist にありません")


def _check_plan(words: list[_Word], ctx: dict) -> None:
    sub = words[0].value if words else ""
    if (sub,) not in PLAN_SPECS:
        raise _Reject("plan.sh は status / pull / done / needs-director / pr-base / store-check だけ許可します")
    _run_spec(PLAN_SPECS[(sub,)], words[1:], f"plan.sh {sub}", ctx)


_TIMEOUT_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")


def _check_segment(words: list[_Word], index: int, ctx: dict, depth: int = 0) -> None:
    head = words[0]
    if head.glob_at >= 0:
        raise _Reject("コマンド名に glob は使えません")
    cmd, args = head.value, words[1:]
    if cmd in ("source", ".") and index == 0 and depth == 0 and [w.value for w in args] == [".crewvia-env"]:
        return  # 固定形 1 つだけ: 先頭の区間の `source .crewvia-env`
    if cmd == "timeout" and depth == 0:
        if len(args) < 2 or args[0].glob_at >= 0 or not _TIMEOUT_DURATION.match(args[0].value):
            raise _Reject("timeout は `timeout <秒数> <許可されたコマンド>` の形だけ許可します")
        _check_segment(args[1:], index, ctx, depth + 1)
        return
    if cmd == "plan" or (os.path.basename(cmd) == "plan.sh" and ".." not in cmd.split("/")):
        _check_plan(args, ctx)
        return
    if cmd == "git":
        _check_git(args, ctx)
        return
    if cmd == "gh":
        _check_gh(args, ctx)
        return
    if cmd == "find":
        _check_find(args)
        return
    if cmd in SPECS:
        _run_spec(SPECS[cmd], args, cmd, ctx)
        if cmd == "cd":
            ctx["cd_seen"] = True
        return
    raise _Reject(
        f"{cmd!r} は research の読み取り専用 allowlist にありません "
        "(python3 / awk / bash -c / env / command / export / cp / mv / rm / tee / curl 等は不可)"
    )


def check_bash(command: str, repo: str = "", agent: str = "", cwd: str = "") -> tuple[str, str]:
    ctx = {"repo": repo, "agent": agent, "cwd": cwd, "cd_seen": False}
    try:
        segs = _tokenize(command)
        if not segs:
            return "deny", "空のコマンドです"
        for k, seg in enumerate(segs):
            _check_segment(seg, k, ctx)
    except _Reject as e:
        return "deny", f"research の Bash は読み取り専用です: {e}"
    return "allow", "research の読み取り専用 allowlist"


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "write" and len(argv) == 7:
        d, r = check_write(argv[2], argv[3], argv[4], argv[5], argv[6])
        return _out(d, r)
    if len(argv) >= 2 and argv[1] == "bash":
        rest = argv[2:]
        ctx = {"repo": "", "agent": "", "cwd": ""}
        while rest[:1] and rest[0] in ("--repo", "--agent", "--cwd") and len(rest) >= 2:
            ctx[rest[0][2:]] = rest[1]
            rest = rest[2:]
        if len(rest) > 1:
            return _out("deny", "usage error")
        cmd = rest[0] if rest else sys.stdin.read()
        d, r = check_bash(cmd, **ctx)
        return _out(d, r)
    return _out("deny", "usage error")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:  # fail closed
        _out("deny", f"research guard error: {type(e).__name__}")
        sys.exit(0)
