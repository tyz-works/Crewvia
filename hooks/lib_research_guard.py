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
    t007: git の表はサブコマンドごと・束ね不可・値は --name=<値> だけ・大域オプション不可、fetch は 3 形だけ。
    リダイレクトは 2>/dev/null と 2>&1 の完全一致だけ。plan.sh は本 repo か自分の worktree の実体だけ。
    t009: source は自分の worktree (か repo root) の .crewvia-env をパスで指す形だけ。printf は外した。
    差分を出す git (diff / show / 差分系オプション付きの log / blame) は --no-ext-diff --no-textconv 必須。
    守るもの・対象外 (脅威モデル) は knowledge/research-skill-guards.md の先頭。
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
#   - 引用符の外で拒否: ( ) { } ! (サブシェル・グループ・ブレース展開・否定)、2>/dev/null と 2>&1 以外の > <
#   - glob (* ? [) は「先頭が - でない字そのもの」で始まる語だけ (展開しても別のオプションにならない)
#   - コマンド名は下の表のどれかに完全一致 (パス指定は信頼する場所の plan.sh だけ)。export / env / command /
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
# 許すリダイレクトはこの 2 つの完全一致のトークンだけ (直後は区切りか末尾)
_ALLOWED_REDIRECT = re.compile(r"2>(?:/dev/null|&1)(?=[ \t\n;|&]|$)")
_REJECT_UNQUOTED = "(){}!"


def _tokenize(cmd: str) -> list[list[_Word]]:
    """区切り (; | || && |& 改行) で区間に分け、各区間を語の列にする。

    リダイレクトは引用符の外にある完全一致のトークン `2>/dev/null` と `2>&1` だけ (直前が空白・
    直後が区切り) を認め、語からは除く (t007)。それ以外の `>` `<` は引用符の外に現れたら拒否する。
    fd 番号を「溜まった語」から判定しない (`'2'>/dev/null` の 2 は uniq の位置引数になる)。
    """
    for needle, why in _FORBIDDEN_TEXT:
        if needle in cmd:
            raise _Reject(why)
    segs: list[list[_Word]] = []
    cur: list[_Word] = []
    word = ""
    has_word = False
    glob_at = -1
    i, n = 0, len(cmd)

    def flush_word() -> None:
        nonlocal word, has_word, glob_at
        if has_word:
            cur.append(_Word(word, glob_at))
        word, has_word, glob_at = "", False, -1

    def end_seg() -> None:
        nonlocal cur
        flush_word()
        if cur:
            segs.append(cur)
        cur = []

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
            raise _Reject("バックグラウンド実行 (&) と &> は拒否します")
        if c == "2" and not has_word and i > 0 and cmd[i - 1] in " \t":
            m = _ALLOWED_REDIRECT.match(cmd, i)
            if m:
                i = m.end()
                continue
        if c in "<>":
            raise _Reject("リダイレクトは引用符の外の 2>/dev/null と 2>&1 だけ許可します "
                          "(> / >> / < / &> / 1> / 引用符・\\ 付きの fd 番号は不可。読むファイルは引数で渡す)")
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
    strict:    git 用 (t007)。短いオプションは `-X` の 2 文字だけ (束ね・結合値は不可。数字だけの -N は numeric
               のとき可)、値を取るオプションは `--name=value` の形だけ (short_arg は持てない)
    """

    def __init__(self, short="", short_arg="", long=(), long_arg=(), long_opt=(), numeric=False,
                 digits=False, max_pos=None, any_args=False, check=None, strict=False):
        if strict and (short_arg or digits):
            raise ValueError("strict な表は値を取る短いオプション・結合形の数字を持てない")
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
        self.strict = strict

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
                if v.glob_at >= 0:
                    raise _Reject(f"find {a} の値に引用符の無い glob は使えません ('...' で囲んでください)")
                i += 2
                continue
            raise _Reject(f"find {a} は allowlist にありません (-exec -delete -fprint* -ok 等は不可)")
        if seen_pred:
            raise _Reject(f"find の述語の後ろの {a!r} は解釈できません")
        if w.glob_at >= 0:
            raise _Reject("find の始点に引用符の無い glob は使えません (展開で述語になりうる)")
        i += 1  # 始点


_FETCH_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/@-]*$")


def _check_git_fetch(opts, pos, ctx) -> None:
    """git fetch は `git fetch --no-prune --no-prune-tags [origin [<ref>]]` の 3 形だけ (t007 / t016)。

    オプションは --no-prune と --no-prune-tags だけで、両方が揃っているときだけ通す
    (git config の fetch.prune / remote.<name>.prune / fetch.pruneTags が true でも ref・tag を消さない)。
    --prune / --tags / --force 等はすべて列挙外で拒否。
    ref は `:` `+` `*` `^` を含まない名前 (字の集合で絞る) = ローカルの ref を書き換える refspec にならない。
    """
    keys = {k for k, _ in opts}
    if not {"--no-prune", "--no-prune-tags"} <= keys:
        raise _Reject("git fetch には --no-prune --no-prune-tags を付けてください "
                      "(git config の prune 設定で ref・tag が消えるのを防ぎます)")
    if pos and pos[0].value != "origin":
        raise _Reject("git fetch の remote は origin だけ許可します (git fetch / git fetch origin / git fetch origin <ref>)")
    if len(pos) == 2 and not _FETCH_REF_RE.match(pos[1].value):
        raise _Reject("git fetch origin <ref> の ref に : + * ^ 等は使えません (ローカルの ref を書き換える形になりうる)")


def _check_no_ext_transport(opts, pos, ctx) -> None:
    for w in pos:
        if "::" in w.value:
            raise _Reject("git の <transport>::<address> 形式は拒否します")


def _check_date_format_only(opts, pos, ctx) -> None:
    """date の位置引数は `+FORMAT` 1 つだけ。数字の位置引数 (date 101212002026 = 時計の設定) は拒否 (t013)。
    -s / --set は表に無い (オプションとして解釈されず拒否される)。"""
    for w in pos:
        if not w.value.startswith("+"):
            raise _Reject("date の位置引数は +FORMAT だけ許可します (時計の設定になりうる形は拒否)")


def _require_one_of(*names: str):
    def check(opts, pos, ctx) -> None:
        if not any(k in names for k, _ in opts):
            raise _Reject(f"このサブコマンドは {' / '.join(names)} のどれかが必要です (読み取りの形だけ許可)")
    return check


# 差分を出す git は外部 diff (GIT_EXTERNAL_DIFF / diff.<driver>.command) と textconv (diff.<driver>.textconv)
# を既定で起動しうる (t009。Codex 3 巡目: `git diff --no-index /dev/null x` が GIT_EXTERNAL_DIFF を起動した)。
# 列挙で --ext-diff / --textconv を外すだけでは止まらないので、無効にするフラグが両方付いた形だけ通す。
_NO_EXT_PROGRAMS = ("--no-ext-diff", "--no-textconv")
# git log のうち差分を出さないオプション。これ以外 (-p / --stat / --follow / --cherry-pick …) が 1 つでもあれば
# 差分の機構が動くとみなし、_NO_EXT_PROGRAMS を求める (列挙外は「差分あり」に倒す = fail closed)
_LOG_NO_DIFF = frozenset(
    ["-N", "--no-patch", "--no-ext-diff", "--no-textconv"]
    + ["--" + n for n in _w(
        "oneline graph all reverse first-parent no-merges merges abbrev-commit no-color topo-order date-order "
        "left-right boundary branches tags remotes ancestry-path simplify-by-decoration no-abbrev walk-reflogs "
        "invert-grep all-match regexp-ignore-case extended-regexp fixed-strings perl-regexp source parents "
        "children since until after before author committer grep max-count skip date format "
        "color decorate abbrev pretty")]
)


def _require_no_ext_programs(opts, pos, ctx) -> None:
    keys = {k for k, _ in opts}
    if not all(f in keys for f in _NO_EXT_PROGRAMS):
        raise _Reject("差分を出す git は --no-ext-diff --no-textconv を付けてください "
                      "(外部 diff・textconv のプログラムを起動しない形だけ許可します)")


def _require_no_ext_programs_if_diff(opts, pos, ctx) -> None:
    if any(k not in _LOG_NO_DIFF for k, _ in opts):
        _require_no_ext_programs(opts, pos, ctx)


def _require_no_textconv(opts, pos, ctx) -> None:
    if not any(k == "--no-textconv" for k, _ in opts):
        raise _Reject("git blame は既定で textconv を起動するので --no-textconv を付けてください")


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
    "pwd": Spec(short="LP", max_pos=0),
    "date": Spec(short="uR", short_arg="d", long=_w("utc rfc-email"), long_arg=_w("date"), max_pos=1, check=_check_date_format_only),
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

def _git(short="", long="", long_arg="", long_opt="", **kw) -> Spec:
    return Spec(short=short, long=_w(long), long_arg=_w(long_arg), long_opt=_w(long_opt), strict=True, **kw)


# git はサブコマンドごとに「通すオプション + 値の取り方」を持つ (t007。共有の表は使わない: git grep の -L は
# 値を取らないが git log の -L は取る、のように同じ字でも引数の個数がサブコマンドで違う)。
# git grep は外した (grep -r / rg で代わりになる。-O / --open-files-in-pager が pager を起動する)。
# pager・外部コマンドを起動しうるもの (-O / --open-files-in-pager / --ext-diff / --textconv / -c /
# --exec-path / --upload-pack / --filters ...) は列挙しない。短いオプションの束ね (-LOecho) は Spec.strict が拒否。
GIT_SPECS: dict[tuple, Spec] = {
    ("log",): _git(
        short="p", numeric=True,
        long="oneline stat shortstat numstat name-only name-status patch no-patch graph all reverse first-parent "
             "no-merges merges follow abbrev-commit no-color topo-order date-order left-right cherry-pick cherry-mark "
             "boundary branches tags remotes ancestry-path simplify-by-decoration no-abbrev full-diff raw summary "
             "no-renames walk-reflogs invert-grep all-match regexp-ignore-case extended-regexp fixed-strings "
             "perl-regexp no-ext-diff no-textconv source parents children compact-summary root "
             "ignore-all-space ignore-space-change",
        long_arg="since until after before author committer grep max-count skip date format diff-filter",
        long_opt="color decorate unified abbrev pretty find-renames word-diff dirstat",
        check=_require_no_ext_programs_if_diff),
    ("show",): _git(
        short="ps",
        long="oneline stat shortstat numstat name-only name-status patch no-patch no-color abbrev-commit raw summary "
             "no-renames no-ext-diff no-textconv source compact-summary first-parent ignore-all-space "
             "ignore-space-change",
        long_arg="format diff-filter date",
        long_opt="color decorate unified abbrev pretty word-diff find-renames",
        check=_require_no_ext_programs),
    ("diff",): _git(
        short="wb",
        long="stat shortstat numstat name-only name-status patch no-patch cached staged no-color raw summary "
             "exit-code quiet no-renames minimal patience histogram ignore-all-space ignore-space-change "
             "ignore-blank-lines no-ext-diff no-textconv compact-summary no-prefix full-index check "
             "merge-base function-context",
        long_arg="diff-filter",
        long_opt="color unified word-diff find-renames dirstat abbrev relative",
        check=_require_no_ext_programs),   # --no-index は列挙しない (repo の外のファイルを比べる形)
    ("status",): _git(short="sb", long="short branch long show-stash ahead-behind no-ahead-behind",
                      long_opt="porcelain untracked-files ignored"),
    ("rev-parse",): _git(short="q", long="show-toplevel git-dir absolute-git-dir git-common-dir is-inside-work-tree "
                                     "is-inside-git-dir show-prefix show-cdup verify quiet symbolic-full-name symbolic",
                         long_opt="abbrev-ref short"),
    ("ls-files",): _git(short="comdsz", long="cached others modified deleted stage ignored directory unmerged "
                                            "exclude-standard error-unmatch full-name",
                        long_arg="exclude"),
    ("blame",): _git(short="wsln", long="porcelain line-porcelain incremental show-email show-name show-number root "
                                       "show-stats no-textconv",
                     long_arg="date", check=_require_no_textconv),
    ("rev-list",): _git(numeric=True, long="count all left-right first-parent no-merges merges reverse topo-order "
                                           "date-order cherry-pick cherry-mark boundary ancestry-path",
                        long_arg="max-count since until after before author grep skip"),
    ("merge-base",): _git(long="is-ancestor fork-point all octopus independent"),
    ("describe",): _git(long="tags all always long exact-match contains first-parent", long_opt="abbrev"),
    ("ls-tree",): _git(short="rtdlz", long="name-only name-status full-name full-tree long object-only",
                       long_opt="abbrev"),
    ("cat-file",): _git(short="ptse", max_pos=2),
    ("show-ref",): _git(short="q", long="heads tags verify quiet", long_opt="hash abbrev"),
    ("for-each-ref",): _git(long_arg="format sort count contains no-contains merged no-merged points-at"),
    ("name-rev",): _git(long="name-only tags all", long_arg="refs exclude"),
    ("shortlog",): _git(short="sne", long="summary numbered email", long_arg="since until"),
    ("check-ignore",): _git(short="vn", long="verbose non-matching no-index"),
    ("version",): _git(max_pos=0),
    ("ls-remote",): _git(short="qht", long="heads tags refs quiet get-url symref", long_arg="sort",
                         check=_check_no_ext_transport),
    ("fetch",): _git(long="no-prune no-prune-tags", max_pos=2, check=_check_git_fetch),
    ("branch",): _git(short="arvl", long="all remotes verbose list show-current",
                      long_arg="contains merged no-merged sort format points-at",
                      check=_require_list_if_positional),
    ("tag",): _git(short="l", long="list", long_arg="contains sort points-at merged no-merged",
                   check=_require_list_if_positional),
    ("remote",): _git(short="v", long="verbose", max_pos=0),
    ("remote", "show"): _git(short="n", max_pos=1),
    ("worktree", "list"): _git(short="vz", long="porcelain verbose", max_pos=0),
    ("stash", "list"): _git(long="oneline stat", max_pos=0),
    ("config",): _git(short="lz", long="get get-all get-regexp list show-origin show-scope local global system "
                                     "name-only null",
                      max_pos=2, check=_require_one_of("--get", "--get-all", "--get-regexp", "--list", "-l")),
}

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
                if not eq and spec.strict:
                    raise _Reject(f"{label} --{name} の値は --{name}=<値> の形で渡してください (次の語は値にしない)")
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
        if spec.strict and len(a) > 2:
            raise _Reject(f"{label}: 短いオプションの束ね・結合値 {a!r} は拒否します (-X を 1 つずつ。値は --name=<値>)")
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
    if not words:
        raise _Reject("git のサブコマンドがありません")
    if words[0].glob_at >= 0 or (len(words) > 1 and words[1].glob_at >= 0):
        raise _Reject("git のサブコマンドに glob は使えません")
    sub = words[0].value
    if sub.startswith("-"):
        # -c / -C / --git-dir / --work-tree / --exec-path / --no-pager ... 大域オプションは一律に拒否 (t007)
        raise _Reject(f"git の大域オプション {sub} は拒否します (-C の代わりに cd してから git を呼ぶ)")
    nxt = words[1].value if len(words) > 1 else None
    if (sub, nxt) in GIT_SPECS:
        _run_spec(GIT_SPECS[(sub, nxt)], words[2:], f"git {sub} {nxt}", ctx)
        return
    if (sub,) in GIT_SPECS:
        _run_spec(GIT_SPECS[(sub,)], words[1:], f"git {sub}", ctx)
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


def _trusted_root() -> str:
    """plan.sh を信頼する repo: CREWVIA_REPO_ROOT、無ければこの lib の位置 (hooks/..)。"""
    return os.environ.get("CREWVIA_REPO_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_trusted_path(cmd: str, ctx: dict, rel: tuple[str, ...]) -> bool:
    """パスの語が「本 repo の <rel>」か「自分の worktree の <rel>」を指すか (t007 / t009)。

    rel は ("scripts", "plan.sh") か (".crewvia-env",)。
    素の `plan` / `plan.sh` / `.crewvia-env` (alias・PATH で解決される) は不可。パスは cwd に対して解決し、
    (1) 字面 (normpath) が信頼する場所のどれかと一致し、(2) realpath もその場所の realpath と一致すること。
    plan.sh は自分の位置 (BASH_SOURCE の dirname を論理パスで) から lib を読むので、realpath だけを
    見ると「信頼する plan.sh への symlink を別の dir に置く」形で別の lib を読ませられる。字面も見て塞ぐ。
    """
    if "/" not in cmd or ".." in cmd.split("/") or cmd.startswith("~"):
        return False
    cwd = ctx.get("cwd")
    if not os.path.isabs(cmd) and not cwd:
        return False  # 同じコマンドの中で cd した後 (cwd 不明) の相対パス
    lexical = os.path.normpath(cmd if os.path.isabs(cmd) else os.path.join(cwd, cmd))
    real = os.path.realpath(lexical)
    root_lex = os.path.normpath(os.path.abspath(_trusted_root()))
    root_real = os.path.realpath(root_lex)
    for root in {root_lex, root_real}:
        if lexical == os.path.join(root, *rel):
            return real == os.path.join(root_real, *rel)
    asg = _read_assignment(root_real, ctx.get("agent", ""))
    if not asg:
        return False
    slug, tid = asg
    wt_re = re.compile(r"^" + re.escape(tid) + r"(?:-[^/]*)?$")

    def own_worktree_file(path: str, root: str) -> bool:
        wt = path
        for _ in rel:
            wt = os.path.dirname(wt)
        return (path == os.path.join(wt, *rel)
                and os.path.dirname(wt) == os.path.join(root, ".claude", "worktrees", slug)
                and bool(wt_re.match(os.path.basename(wt))))

    if not any(own_worktree_file(lexical, r) for r in {root_lex, root_real}):
        return False
    under = os.path.relpath(lexical, root_lex if lexical.startswith(root_lex + os.sep) else root_real)
    return own_worktree_file(real, root_real) and real == os.path.join(root_real, under)


def _check_source(args: list[_Word], index: int, depth: int, ctx: dict) -> None:
    """source / . は自分の worktree (か本 repo) の .crewvia-env をパスで指す形だけ (t009)。

    - 先頭の区間・timeout の外・引数はファイル 1 つだけ (`source f x` は位置引数を書き換える)
    - スラッシュ無しの `.crewvia-env` は PATH を探す (bash の sourcepath) ので不可。`./.crewvia-env` か絶対パス
    - 字面と realpath の両方が信頼する場所 = symlink 不可 (plan.sh と同じ _is_trusted_path)。
      index 0 なので同じコマンドの cd の後ろにはならない (cwd は hook の cwd)
    """
    if index != 0 or depth != 0:
        raise _Reject("source は コマンドの先頭で `source ./.crewvia-env` (か絶対パス) の形だけ許可します")
    if len(args) != 1 or args[0].glob_at >= 0:
        raise _Reject("source の引数は .crewvia-env のパス 1 つだけです")
    path = args[0].value
    if "/" not in path:
        raise _Reject("`source .crewvia-env` は PATH を探すので不可です。`source ./.crewvia-env` と書いてください")
    if os.path.basename(path) != ".crewvia-env" or not _is_trusted_path(path, ctx, (".crewvia-env",)):
        raise _Reject("source できるのは自分の worktree (か本 repo) の .crewvia-env だけです "
                      "(別の場所・symlink・.. は不可)")


_TIMEOUT_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")


def _check_segment(words: list[_Word], index: int, ctx: dict, depth: int = 0) -> None:
    head = words[0]
    if head.glob_at >= 0:
        raise _Reject("コマンド名に glob は使えません")
    cmd, args = head.value, words[1:]
    if cmd in ("source", "."):
        _check_source(args, index, depth, ctx)
        return
    if cmd == "timeout" and depth == 0:
        if len(args) < 2 or args[0].glob_at >= 0 or not _TIMEOUT_DURATION.match(args[0].value):
            raise _Reject("timeout は `timeout <秒数> <許可されたコマンド>` の形だけ許可します")
        _check_segment(args[1:], index, ctx, depth + 1)
        return
    if os.path.basename(cmd) in ("plan.sh", "plan"):
        if not _is_trusted_path(cmd, ctx, ("scripts", "plan.sh")):
            raise _Reject("plan.sh は本 repo (CREWVIA_REPO_ROOT) か自分の worktree の scripts/plan.sh を "
                          "パスで指したものだけ許可します (素の plan・別の場所の plan.sh・symlink・.. は不可)")
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
            # 同じコマンドの中の cd の後ろの cwd は字面から確定できない (パイプの中の cd はサブシェル、
            # `false && cd x;`・`timeout 5 cd x` は効かない、CDPATH・失敗)。追跡せず「不明」にする
            # = 後ろの相対パスの plan.sh / --result-file は拒否 (絶対パスで渡す)
            ctx["cd_seen"] = True
            ctx["cwd"] = None
        return
    raise _Reject(
        f"{cmd!r} は research の読み取り専用 allowlist にありません "
        "(python3 / awk / bash -c / env / command / export / cp / mv / rm / tee / curl 等は不可)"
    )


def check_bash(command: str, repo: str = "", agent: str = "", cwd: str = "") -> tuple[str, str]:
    ctx = {"repo": repo, "agent": agent, "cwd": cwd or os.getcwd(), "cd_seen": False}
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
