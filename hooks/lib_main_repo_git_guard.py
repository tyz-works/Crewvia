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

使い方: python3 lib_main_repo_git_guard.py "<command>" "<main_repo_abs_path>"
  stdout に "DENY" または "OK" を1行出力する。
"""
import re
import sys


# `git <...破壊的動詞...>` — 制御演算子 (|;&) を挟まない範囲で動詞を探す。
# `git -C $X branch -D foo` / `git -C $X worktree add ...` のように git と動詞の
# 間にオプション (-C ${CREWVIA_REPO_ROOT} 等) が挟まるケースもあるため、
# `branch`/`worktree` と `-D`/`add` の間も同様に [^|;&]* で緩く許容する
# (実測: `\bgit\s+branch\b` のように git 直後の空白必須にすると
# `git -C ${CREWVIA_REPO_ROOT} branch -D x` を見逃していた)。
DESTRUCTIVE_VERB_RE = re.compile(
    r'\bgit\b[^|;&]*\b(?:checkout|switch|reset|merge|rebase|commit|clean|stash)\b'
    r'|\bgit\b[^|;&]*\bbranch\b[^|;&]*-D\b'
    r'|\bgit\b[^|;&]*\bworktree\b[^|;&]*\badd\b'
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
        return True
    return False


def decide(command: str, repo_abs: str) -> str:
    if '$(' in command or '`' in command:
        # コマンド置換はクォート内でも実行されるため、見逃しを避けて元の文字列で判定する
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
