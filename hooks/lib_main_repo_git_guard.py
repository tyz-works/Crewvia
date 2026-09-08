#!/usr/bin/env python3
"""hooks/lib_main_repo_git_guard.py

t001 (mission 20260908-main-repo-protection): Bash 経由で主リポジトリ
($CREWVIA_REPO_ROOT) に対して破壊的な git 操作を行おうとしていないかを判定する。
hooks/pre-tool-use.sh の Bash git ガードから呼ばれる。

背景: PR#182 (t011/t014) の worktree scope guard は Edit/Write/MultiEdit/
NotebookEdit のみを対象としており、Bash 経由の
`cd $CREWVIA_REPO_ROOT && git checkout -b ...` のような操作は対象外だった。
2026-09-08 に docs Worker (worktree モードで作業中) が実際にこの経路で主
リポジトリのブランチを main から切り替える事故を起こした (Director が検知・
復旧、実害なし。Sofia でも同種の事故があり2回目)。

検証済み (Director, git 2.43.0。再調査不要): git の reference-transaction
hook は `git checkout <existing>` / `git switch <existing>` に一切発火しない
(HEAD は symbolic ref であり ref transaction を通らない)。「HEAD の移動そのもの
を止める」手段は git hook 経由では原理的に存在しないため、主防御はこの Bash
コマンド文字列ガードで行う。

判定方式: コマンド文字列に「主リポジトリへの参照」($CREWVIA_REPO_ROOT /
$CREWVIA_REPO の変数表記、または主リポジトリの絶対パス) と「破壊的 git 動詞」
(checkout/switch/reset/merge/rebase/commit/clean/stash/branch -D/worktree add)
が同居していたら DENY。読み取り系 (status/log/diff/show) は対象外。

主リポジトリへの参照が `.claude/worktrees/` へ続く場合は対象外とする — それは
別の独立した worktree checkout (別の .git を持つ) への参照であり、そこでの
通常の git 操作 (checkout -b 等) を誤ってブロックしないため。これが誤爆防止の
最重要ポイント (worktree Worker が一切作業できなくなる事故を防ぐ)。

判定前に、コマンド文字列内のクォート区間 (シングル/ダブル) の中身を空白で
マスクする。plan.sh done の Result 引数のように、事故を説明する文章の中で
偶然 "git checkout" や "$CREWVIA_REPO_ROOT" という文字列が引用されただけの
ケース (このタスク自身の Result がまさにそれに該当しうる) を誤検知しないため。
ただしコマンド置換 ($(...) / `...`) を含む場合、クォート内であっても実際に
シェルへ渡されて実行されるため、見逃しを避けるためマスクせず元のコマンド文字列
全体で判定する (フェイルセーフ: 検出漏れより誤検知の方が安全な方向)。

既知の限界 (worker.md の明文化が一次防御である前提は変わらない):
  - 変数に一度代入してから使う形 (`R=$CREWVIA_REPO_ROOT; cd $R && git ...`) は
    検出できない。
  - 相対パスでの到達 (`cd ../../.. && git checkout ...`) は検出できない。
  - クォートの対応を厳密にパースしていない (エスケープされたクォート等は
    考慮しない) ベストエフォートのヒューリスティックである。

t005 (Seo/Opus 5 レビュー [P1][P2] への対応。PR#186 の同 branch に追加):

[P1] 当初の判定は「参照」と「破壊的動詞」が同一コマンド文字列のどこかに
同居していれば DENY としていたが、これは「参照が main repo を操作対象に
している」ことを見ておらず、read-only な呼び出し (`cat $CREWVIA_REPO_ROOT/
knowledge/bash.md` 等) の隣に別コマンドとして破壊的 git 操作 (worktree 内・
main repo 非対象) があるだけで誤爆していた。加えて DESTRUCTIVE_VERB_RE の
`[^|;&]*` に改行が含まれておらず、複数行スクリプトでは行をまたいだ誤マッチ
(1 行目の `git` と 3 行目の `stash` が「同一コマンド」として結合される等) も
発生していた。Seo 実測 (本番同形 env) で 5 パターンの誤爆を確認、修正案の
「検出 14/14 を維持したまま誤爆だけ消える」ことも実測で裏取り済み。

修正:
  - `[^|;&]*` → `[^|;&\n]*` (改行を制御演算子と同格に扱う)
  - has_main_repo_reference() を「参照の直前が操作対象を指定する構文
    (`cd `/`pushd `/`git -C `/`--git-dir=`/`--work-tree=`) の場合のみ真」に
    変更。QA (t003) が確認した検出 14 ケースはすべて `cd <ref> &&` 形か
    `git -C <ref>` 形であり、この条件を満たす。

[P2] decide() は `$(` またはバッククォートがコマンド文字列に 1 つでもあれば
クォートマスクを丸ごとスキップしていたが、シングルクォート内のバッククォート
はシェル的に完全に無害 (展開されない) であるにも関わらず区別していなかった。
結果、事故コマンドをシングルクォートで引用しただけの報告コマンド
(plan done t003 '(バッククォート)cd $CREWVIA_REPO_ROOT && git checkout -b x(バッククォート) を確認')
まで誤って DENY していた (PR#183 [P2] と同型の自己矛盾)。

修正: コマンド置換記法 (`$(` / バッククォート) が **シングルクォート区間の
外** (裸の位置、またはダブルクォート内 — ダブルクォート内でもシェルは実際に
展開・実行するため「外」扱いにする) にある場合だけフェイルセーフ (マスク
省略) に倒す。シングルクォート区間の外に実在する場合のみ意味があるため、
検出漏れは生まれない。

使い方: python3 lib_main_repo_git_guard.py "<command>" "<main_repo_abs_path>"
  stdout に "DENY" または "OK" を1行出力する。
"""
import re
import sys


# `git <...破壊的動詞...>` — 制御演算子 (|;&) および改行を挟まない範囲で動詞を
# 探す。改行を除外するのは t005 の修正: bash では改行は `;` と同格の区切りだが
# 正規表現上は素通りするため、除外しないと複数行スクリプトが事実上ひとつの窓に
# なり、無関係な行の動詞まで拾ってしまう。
# `git -C $X branch -D foo` / `git -C $X worktree add ...` のように git と動詞の
# 間にオプション (-C ${CREWVIA_REPO_ROOT} 等) が挟まるケースもあるため、
# `branch`/`worktree` と `-D`/`add` の間も同様に [^|;&\n]* で緩く許容する
# (実測: `\bgit\s+branch\b` のように git 直後の空白必須にすると
# `git -C ${CREWVIA_REPO_ROOT} branch -D x` を見逃していた)。
DESTRUCTIVE_VERB_RE = re.compile(
    r'\bgit\b[^|;&\n]*\b(?:checkout|switch|reset|merge|rebase|commit|clean|stash)\b'
    r'|\bgit\b[^|;&\n]*\bbranch\b[^|;&\n]*-D\b'
    r'|\bgit\b[^|;&\n]*\bworktree\b[^|;&\n]*\badd\b'
)

# t005 [P1]: 主リポジトリへの参照は、その直前が「操作対象を指定する構文」の
# 場合のみ「main repo を操作対象にしている」とみなす。単に文字列中のどこかに
# 参照が出現するだけ (read-only コマンドの引数、他コマンドの一部等) では
# 真としない。クォート文字 (`'`/`"`) が参照の直前に挟まるケース
# (`cd "$CREWVIA_REPO_ROOT"` 等) も許容する。
_OPERAND_PREFIX_RE = re.compile(
    r'(?:\bcd|\bpushd)[ \t]+[\'"]?$'
    r'|\bgit[ \t]+-C[ \t]+[\'"]?$'
    r'|--git-dir=[\'"]?$'
    r'|--work-tree=[\'"]?$'
)


def mask_quotes(command: str) -> str:
    """シングル/ダブルクォート区間の中身を空白に置換する (長さ・オフセットは維持)。

    エスケープされたクォート (`\\"` 等) は考慮しないベストエフォート実装。
    クォートの外側にある実際のシェル構文 (&&, ;, 裸の git コマンド等) は
    そのまま残るため、正規表現による検出対象として引き続き機能する。
    """
    out = list(command)
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n and command[j] != quote:
                j += 1
            end = min(j + 1, n)
            for k in range(i, end):
                out[k] = ' '
            i = end
        else:
            i += 1
    return ''.join(out)


def has_main_repo_reference(command: str, repo_abs: str) -> bool:
    ref_patterns = [
        re.escape(repo_abs),
        r'\$\{?CREWVIA_REPO_ROOT\}?',
        r'\$\{?CREWVIA_REPO\}?',
    ]
    ref_re = re.compile('(?:' + '|'.join(ref_patterns) + ')')
    for m in ref_re.finditer(command):
        tail = command[m.end():]
        if tail.startswith('/.claude/worktrees'):
            continue  # 別の独立した worktree checkout への参照 — 対象外
        head = command[:m.start()]
        if not _OPERAND_PREFIX_RE.search(head):
            continue  # main repo を操作対象にしていない (t005 [P1]) — 対象外
        return True
    return False


def _has_command_substitution_outside_single_quotes(command: str) -> bool:
    """`$(` または `` ` `` がシングルクォート区間の外にあるかを判定する (t005 [P2])。

    シングルクォート区間の中は展開が一切発生しない安全な位置なので対象外とする。
    ダブルクォート区間の中はシェルが実際にコマンド置換を展開・実行するため、
    「外」として扱う (裸の位置と同様にフェイルセーフの対象とする)。
    エスケープされたクォート等は考慮しないベストエフォート実装 (mask_quotes と
    同じ前提)。
    """
    in_single = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "'":
            in_single = not in_single
        elif not in_single and (ch == '`' or command[i:i + 2] == '$('):
            return True
        i += 1
    return False


def decide(command: str, repo_abs: str) -> str:
    if _has_command_substitution_outside_single_quotes(command):
        # クォート外 (またはダブルクォート内) のコマンド置換はクォートマスクを
        # 素通りして実際にシェルへ渡されるため、見逃しを避けて元の文字列で判定する
        check_cmd = command
    else:
        check_cmd = mask_quotes(command)

    if not has_main_repo_reference(check_cmd, repo_abs):
        return "OK"
    if DESTRUCTIVE_VERB_RE.search(check_cmd):
        return "DENY"
    return "OK"


def main() -> int:
    if len(sys.argv) != 3:
        print("OK")
        return 0
    command, repo_abs = sys.argv[1], sys.argv[2]
    print(decide(command, repo_abs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
