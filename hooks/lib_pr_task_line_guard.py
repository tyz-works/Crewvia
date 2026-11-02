#!/usr/bin/env python3
"""hooks/lib_pr_task_line_guard.py

t002 (mission 20261102-pr-task-line), 4 巡目: `gh pr create` / `gh pr new` の
`--body` (または短縮形 `-b`) に `task: <mission>/<task>` 行が無ければ deny する
判定を、bash の sed/grep によるシェル構文解析から Python へ移した。

背景 (3 巡とも指摘が同じ系統だった): bash 側の sed/grep 実装は 1 巡目「2 パス sed が
アポストロフィで対を誤る」→ 2 巡目「1 回の交替パターンにしたが sed は**行単位**で
正規表現を評価するため、複数行にまたがるクォート区間 (`--body "...\n...\n..."` の
ような複数行 `--body` 値そのもの) を正しく扱えない」と、同じ「sed/grep でシェルを
解析しようとしている」ことに起因する穴が出続けた。加えて短縮フラグ `-b` やエイリアス
`gh pr new` を素通りしてしまう、`grep -qF` の無アンカー一致で task id の接頭辞衝突
(`t1` が `t10` の行を「ある」と誤判定する) 等、文字列マッチならではの穴もあった。

本実装は:
  - クォート区間のマスクに `lib_main_repo_git_guard.mask_quotes()` を再利用する
    (このファイル自身が複数行のクォート区間を正しく扱うことを Director が実測確認済み
    — 文字単位でスキャンし、行の区切りを特別扱いしないため)。クォートマスクを
    ここで独自に書き直さない (この repo の「規則は 1 か所」の原則。lib_dep_rules.py 等
    と同じ)
  - マスク済み文字列上で、クォートの**外**にある制御演算子 (`;` `&` `|` `&&` `||` 改行)
    の位置を見つけ、その同じ文字位置で**元の** (マスクしていない) コマンド文字列を
    「単純コマンド」の断片に分割する (mask_quotes はクォート区間を空白に置換するだけで
    文字列長・オフセットを変えないため、マスク済み文字列で見つけた位置がそのまま元の
    文字列の位置になる)
  - 各断片を `shlex.split()` で実際の argv にトークナイズする。これにより:
    * `--body` / `-b` の**引数の値だけ**を見られる (`--body-file` のような紛らわしい
      別フラグとの文字列レベルでの混同が起きない)
    * `gh pr create` と `gh pr new` のどちらも同じロジックで扱える
    * task: 行の照合を `--body` の値そのものに対して行えるので、アンカー付き正規表現の
      ような小細工なしに `t1` と `t10` を取り違えない
  - **パースできないとき (閉じていない引用符等で `shlex.split` が `ValueError` を
    投げたとき) は deny しない (fail-open)**。`hooks/lib_main_repo_git_guard.py` が
    主リポジトリ破壊的操作の判定で fail-open に倒しているのと同じ設計判断 — 解析器の
    限界で誤って deny するより、ガードが効かない側に倒れるほうが安全 (Director の
    設計制約「解決できないときは deny しない」と同型)

既知の穴 (意図的にスコープ外。黙って見逃していない。knowledge/pr-task-line-guard.md 参照):
  - `--body-file` / ヒアドキュメント / 標準入力で本文を渡す形は検出できない
    (`--body` / `-b` の**インライン引数**の形だけを見る。見つからなければ判定自体を
    スキップして素通りする)
  - `bash -c "gh pr create ..."` のようにクォートごとネストしたインタプリタへ丸投げする
    形は、外側のクォートが 1 つの引数としてトークナイズされてしまうため検出できない
    (見逃し方向のリスク。誤検知を避けることを優先した Director の判断)
  - `gh pr edit --body` で後から行を消すケースは対象外 (生成時点のガードのみ)
  - シェル変数の展開・コマンド置換の結果は見ない (静的なトークン化であり、実行時の
    値は分からない。例えば `--body "$MSG"` の `$MSG` の中身は判定できない)

使い方: python3 lib_pr_task_line_guard.py "<command>" "<mission_slug>" "<task_id>"
  mission_slug / task_id のいずれかが空文字列なら (呼び出し側で assignment を解決
  できなかった場合) 無条件に "OK" を返す (fail-open)。
  stdout に "DENY" または "OK" を1行出力する。
"""
import re
import shlex
import sys

from lib_main_repo_git_guard import mask_quotes

# クォートの外にある制御演算子 (&& / || を先に試す。改行も ; と同格の区切り)。
_CONTROL_OP_RE = re.compile(r'&&|\|\||[;&|\n]')

_BODY_EQ_PREFIX = '--body='


def _split_simple_commands(command: str):
    """クォートの外にある制御演算子で、元の文字列を「単純コマンド」の断片に分割する。

    mask_quotes() はクォート区間を空白に置換するだけで長さ・オフセットを変えないため、
    マスク済み文字列上で見つけた制御演算子の位置は、そのまま元の文字列の同じ位置を指す。
    """
    masked = mask_quotes(command)
    pos = 0
    for m in _CONTROL_OP_RE.finditer(masked):
        yield command[pos:m.start()]
        pos = m.end()
    yield command[pos:]


def _is_gh_pr_create_or_new(tokens: list) -> bool:
    for i in range(len(tokens) - 2):
        if tokens[i] == 'gh' and tokens[i + 1] == 'pr' and tokens[i + 2] in ('create', 'new'):
            return True
    return False


def _find_inline_body_value(tokens: list):
    """`--body` / `-b` の**インライン引数**の値を返す。

    見つからなければ None (`--body-file` 等しか無い場合もここに含まれる — 既知の穴)。
    """
    for i, tok in enumerate(tokens):
        if tok in ('--body', '-b'):
            return tokens[i + 1] if i + 1 < len(tokens) else ''
        if tok.startswith(_BODY_EQ_PREFIX):
            return tok[len(_BODY_EQ_PREFIX):]
    return None


def _has_task_line(body_value: str, mission: str, task: str) -> bool:
    pattern = re.compile(
        r'task:[ \t]+' + re.escape(mission) + '/' + re.escape(task) + r'(?![0-9A-Za-z])'
    )
    return pattern.search(body_value) is not None


def decide(command: str, mission: str, task: str) -> str:
    if not mission or not task:
        return "OK"  # assignment を解決できなかった (fail-open)
    try:
        for segment in _split_simple_commands(command):
            tokens = shlex.split(segment)
            if not _is_gh_pr_create_or_new(tokens):
                continue
            body_value = _find_inline_body_value(tokens)
            if body_value is None:
                continue  # --body (-b) が無い (--body-file 等) — 既知の穴、素通り
            if not _has_task_line(body_value, mission, task):
                return "DENY"
        return "OK"
    except ValueError:
        # shlex が「閉じていない引用符」等でトークナイズできなかった (fail-open)
        return "OK"


def main() -> int:
    if len(sys.argv) != 4:
        print("OK")
        return 0
    command, mission, task = sys.argv[1], sys.argv[2], sys.argv[3]
    print(decide(command, mission, task))
    return 0


if __name__ == "__main__":
    sys.exit(main())
