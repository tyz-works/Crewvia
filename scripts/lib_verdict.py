#!/usr/bin/env python3
"""scripts/lib_verdict.py — plan_review.md の規定形式 verdict 抽出の単一実装。

t012 (mission 20260909-dead-config-sweep, QA t011 NEW-1/NEW-2 + 未閉塞の同型穴)
による全面的な作り直し。t010 版 (フェンス除去 + 判定語の集合を数える方式) は
QA t011 が新規 fail-open を 3 件実測したため破棄した。

## この機構が 7 回同じ形で破られてきた理由

plan_review.md の verdict 抽出は、これまで一貫して
「**ファイル全体を走査して**、verdict らしき行を見つけ、
  その行の**中に** approve という語が**含まれていれば** approve」
という形をしていた。この形には原理的に 2 つの無限集合が入り込む:

  1. **値の側**: `not approve` / `approve できません` / `pending — do not
     approve yet` のように、approve を含みながら意味が反転する表現は
     いくらでも作れる。否定形を列挙して塞ぐことはできない
     (この結論は normalize_plan_review_verdict.py 側で先に出ており、
     そちらは既に APPROVE_EXACT の完全一致 = allowlist に反転済み)。
  2. **場所の側**: ``` / ~~~ / 入れ子フェンス / 閉じ忘れフェンス /
     HTML コメント / 引用 と、「本文に見えるが本文でない領域」の記法も
     いくらでもある。除去ヒューリスティクスを足すたびに別の記法で抜けられ、
     t010 版ではフェンス除去の正規表現 (```` ```.*?``` ````) がペア位置ずれを
     起こして**本物の revise を消し approve だけを残す**という、
     除去しなかった頃より悪い挙動まで作った (QA t011 NEW-2)。

したがって「除去を賢くする」方向は本質的に袋小路である。

## 設計 (t012): 判定を読む場所も値も、閉じた文法に限定する

  1. **場所を 1 点に固定する** — ファイルの**最初の非空行だけ**を判定の
     対象にする。ファイル全体の走査をやめる。
     フェンスや HTML コメントが本文のどこにあっても、判定の対象領域に
     入り込む余地が原理的に無くなる (フェンス内・コメント内の行が
     「最初の非空行」になるには、その開始記号 ``` / ~~~ / <!-- 自身が
     さらに手前の非空行として現れるため、開始記号の行が読まれて不一致に
     なる)。フェンス記法の列挙も除去も一切不要になる。
  2. **値を完全一致の allowlist にする** — 行の値部分を前後の空白だけ
     取り除き、`approve` / `revise` / `reject` の**いずれか 1 語ちょうど**と
     完全一致する場合だけ採用する。部分一致・包含判定は一切しない。
     大文字小文字も区別する (config/plan-review-verdict.schema.json の
     enum が小文字のみであり、`APPROVE` を通すのは危険側への緩和になる。
     QA t011 NEW-3 で指摘された緩和をここで戻す)。
  3. **それ以外はすべて判定不能 (None)** — 呼び出し側は「待つ / 失敗」側に
     倒すこと。approve に倒れる経路をこのモジュールは一つも持たない。
  4. **拒否専用の追加スキャン** — 1 で確定した値と**異なる値**の完全一致
     verdict 行がファイル内の他の場所にもある場合は、判定不能に落とす
     (reviewer が自己矛盾している = 曖昧なので安全側)。
     これはファイル全体を見るが、**判定を「読む」のではなく「拒否する」
     ためだけに使う**: 出力は None 方向にしか動かないので、この走査が
     approve を生むことは原理的にありえない (単調に安全側)。
     フェンス記法の解釈は一切していない — 「値が完全一致の verdict 行か
     どうか」しか見ないため、記法を変えて抜けるという攻撃面が無い。

この形は「危険な結論は allowlist・判定 unit は 1 つ」という本ミッションの
原則を、**値と場所の両方**に適用したものである (t010 版は本数だけに適用し、
値と場所には適用していなかった)。

## 書式が守られなかった場合にどう回収するか

上記のとおり本モジュールは意図的に厳しい。規定形式を外した plan_review.md
(例: タイトル行が先にある / 値に註釈が付く / 判定が本文中にある) は
すべて判定不能になる。**その回収は scripts/review-plan.sh の構造化出力経路
(`claude --json-schema` + config/plan-review-verdict.schema.json) が行う** —
CLI 自身が enum 適合を保証した JSON の `verdict` フィールドという、
曖昧性が原理的に存在しない単一の判定 unit から verdict を取り、
plan_review.md の 1 行目に規定形式で書き戻す。
つまり散文の解釈を賢くするのではなく、
**散文を読む場所を 1 点に固定し、書式が外れたら構造化出力に委ねる**
というのが t012 の全体設計である。

Usage (CLI):
  python3 lib_verdict.py <plan_review.md path>
  → 有効な verdict が一意に確定すれば標準出力に書いて exit 0。
    判定不能 (規定形式でない / 値が完全一致でない / ファイルが読めない)
    なら何も出さず exit 1。
"""
from __future__ import annotations

import re
import sys

# 許容する verdict は この 3 語ちょうどのみ (完全一致 allowlist)。
# config/plan-review-verdict.schema.json の enum と同一・同順。
CANONICAL_VERDICTS = ("approve", "revise", "reject")

# 規定形式の verdict 行。行頭アンカー (re.match) で、インデントも引用符 (>) も
# 許容しない。値部分は行末までまるごと捕捉し、完全一致判定は呼び出し側で行う。
_VERDICT_LINE_RE = re.compile(r"\*\*Verdict:\*\*(.*)$")


def first_content_line(text: str) -> str | None:
    """先頭の空行を読み飛ばし、最初の非空行を返す。1 行も無ければ None。

    「非空」= 前後の空白 (改行・タブ・全角空白を含む) を除いて 1 文字以上残る行。

    F3 (t002, mission 20260912-verdict-ci-launcher, QA Finn 実測): 行の分割に
    `str.splitlines()` を使わないこと。splitlines() は `\n` 以外にも
    `\r` / `\x0b` / `\x0c` / `\x1c` / `\x1d` / `\x1e` / U+0085 / U+2028 /
    U+2029 の 9 種すべてで分割する。呼び出し元 (review-plan.sh の grep /
    wait_for_plan_review.sh) はいずれも `\n` だけを行区切りとみなすため、
    `**Verdict:** approve<CR>ではない。修正が必要です` のような入力で
    lib_verdict だけが1行目を `**Verdict:** approve` に短く区切ってしまい
    完全一致 allowlist を素通りしていた。`str.split("\n")` は `\n` だけで
    分割するため、他の行区切り文字は行の中身の一部として残り
    (`_canonical_value_of` の完全一致判定で弾かれる)、この食い違いが無くなる。
    """
    for line in text.split("\n"):
        if line.strip():
            return line
    return None


def _canonical_value_of(line: str) -> str | None:
    """1 行が完全一致の規定形式 verdict 行なら、その値を返す。でなければ None。"""
    m = _VERDICT_LINE_RE.match(line)
    if not m:
        return None
    value = m.group(1).strip()
    return value if value in CANONICAL_VERDICTS else None


def extract_canonical_verdict(text: str) -> str | None:
    """規定形式 `**Verdict:** approve|revise|reject` から verdict を 1 つ確定する。

    判定対象は**ファイルの最初の非空行だけ**。その行が
    `**Verdict:**` で始まり、続く値が前後の空白を除いて
    CANONICAL_VERDICTS のいずれかと完全一致する場合にのみ、その語を候補にする。
    それ以外は例外なく None (判定不能)。

    候補が決まった後、ファイル内の他の行に**異なる値**の完全一致 verdict 行が
    あれば None に落とす (reviewer の自己矛盾 = 曖昧 → 安全側)。同じ値の
    重複は矛盾ではないので許容する。この追加走査は結果を None 方向にしか
    動かさないため、判定を「1 箇所からのみ読む」性質は保たれている。
    """
    first = first_content_line(text)
    if first is None:
        return None
    verdict = _canonical_value_of(first)
    if verdict is None:
        return None

    for line in text.split("\n"):
        other = _canonical_value_of(line)
        if other is not None and other != verdict:
            return None  # 自己矛盾 — 判定不能に倒す
    return verdict


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: lib_verdict.py <plan_review.md>", file=sys.stderr)
        return 2
    path = argv[1]
    try:
        # F2 (t015, mission 20260912-verdict-ci-launcher, PR #199 QA(Finn) t003
        # FAIL-2 / Kai-codex P2): newline="" で開き、universal newlines による
        # 改行変換を無効にする。デフォルトの open() は \r 単体・\r\n・\n の
        # いずれも読み込み時点で \n に変換してしまうため、
        # `**Verdict:** approve\rNOT approved; revisions required` のような
        # バイト列は、extract_canonical_verdict() を文字列として直接呼んだ
        # 場合 (tests/test_lib_verdict.py) には 1 行のまま (完全一致せず
        # 判定不能) だが、**ファイル経由** (このCLI, ひいては
        # wait_for_plan_review.sh) では \r が \n に変換されて2行に分割され、
        # 1行目 `**Verdict:** approve` だけが読まれて approve と誤判定していた
        # (旧 docstring 22-30行の「ファイル経由では \r 単体は顕在化しない」は
        # 事実と逆だった — 実際には approve として顕在化する)。
        # newline="" にすると \r はそのまま文字列に残るため、
        # extract_canonical_verdict() 側の `text.split("\n")` は \r を行区切り
        # とみなさず (中間の \r は行の一部として残り完全一致で弾かれる)、
        # 正規の CRLF 行末 (`approve\r\n`) は `_canonical_value_of` の
        # `.strip()` が末尾の \r を空白として除去するため従来どおり approve
        # と読める。
        with open(path, encoding="utf-8", newline="") as f:
            content = f.read()
    except OSError as e:
        print(f"lib_verdict: cannot read {path}: {e}", file=sys.stderr)
        return 1

    verdict = extract_canonical_verdict(content)
    if verdict is None:
        return 1
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
