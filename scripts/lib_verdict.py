#!/usr/bin/env python3
"""scripts/lib_verdict.py — plan_review.md の規定形式 verdict 判定の単一実装。

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
     いくらでも作れる。否定形を列挙して塞ぐことはできない。
  2. **場所の側**: ``` / ~~~ / 入れ子フェンス / 閉じ忘れフェンス /
     HTML コメント / 引用 と、「本文に見えるが本文でない領域」の記法も
     いくらでもある。除去ヒューリスティクスを足すたびに別の記法で抜けられる
     (QA t011 NEW-2)。

したがって「除去を賢くする」方向は本質的に袋小路である。

## 設計 (t012): 判定を読む場所も値も、閉じた文法に限定する

  1. **場所を 1 点に固定する** — ファイルの**最初の非空行だけ**を判定の
     対象にする。
  2. **値を完全一致の allowlist にする** — 行の値部分を前後の空白だけ
     取り除き、`approve` / `revise` / `reject` の**いずれか 1 語ちょうど**と
     完全一致する場合だけ採用する。大文字小文字も区別する
     (config/plan-review-verdict.schema.json の enum が小文字のみ)。

## t018 (mission 20260912-verdict-ci-launcher, PR #199 fix 3): 判定不能を 2 つに分ける

t012〜t015 の本モジュールは「有効な 1 語」以外をすべて None (判定不能) に
まとめていた。scripts/review-plan.sh は判定不能を「プローズに verdict が
書かれていない → 構造化出力 (`claude --json-schema`) で救済してよい」と
扱うため、次の 2 つが**区別できないまま同じ救済経路に流れていた**
(Kai-codex t004 2 回目 P1 / QA t016 Finn FAIL-A 実測):

  - 本当に書かれていない (`## 総合判定: GO` のような別表記だけ)
  - 書かれているのに規定形式を外している・自己矛盾している
    (1 行目 `**Verdict:** revise` + 本文に `**Verdict:** approve` /
     `**Verdict:** revise (重大な指摘あり)` / `**Verdict:** REVISE` …)

後者を structured=approve で救済すると、reviewer がプローズに revise/reject と
書いていても approve が消費される。t015 で plan.sh の plan_review.md 読み直しを
やめた (plan_review.verdict への束縛) ことで、読み直しが偶然持っていた
「自己矛盾なら refund」の安全弁も消え、1 行目 revise/reject が approve として
消費される回帰になっていた。

そこで判定結果を 3 状態にする (Director 設計判断 1-4):

  - **VALID** — 「verdict 行の兆候」を持つ行がファイル全体でちょうど 1 行
    だけあり、それが最初の非空行で、値が正規の 1 語と完全一致する。
  - **NO_SIGN** — 「verdict 行の兆候」を持つ行がファイルのどこにも無い
    (ファイル自体が無い場合を含む)。**構造化出力による救済が許されるのは
    この状態だけ。**
  - **VIOLATION** — 兆候が 1 つ以上あるのに VALID ではない (1 行目以外にある /
    値に余計な文字がある / 大文字 / 兆候が複数ある — 値が同じでも)、または
    兆候の有無そのものを確認できない (読めない・UTF-8 として解釈できない)。
    構造化出力の値に関係なく fail-closed にすること。

### 「verdict 行の兆候」の定義 — 記法を除去して狭めるのではなく、広く拾う

兆候の判定は**値を読むためではなく、救済を拒否するためだけ**に使う。
出力を NO_SIGN から VIOLATION の方向 (= 安全側) にしか動かさないため、
広く拾いすぎても approve は生まれない。したがって記法を列挙して
「どこが本文か」を解釈することはしない。代わりに行全体を正規化して、
その中に `verdict:` という並びが現れるかだけを見る:

  1. Unicode 互換分解 (NFKD) — 全角 `Ｖｅｒｄｉｃｔ：` を半角に揃える
  2. casefold — `VERDICT` / `Verdict` / `verdict` を同一視する
  3. 空白 (改行以外の行区切り文字を含む)・`*`・`_`・`` ` ``・`\\`・
     書式文字 (Cf, ゼロ幅空白など)・結合文字 (Mn) を取り除く
  4. 残った文字列に `verdict:` が含まれていれば兆候あり

これは Director の最低要件 (行頭・引用・リスト・フェンス内を問わず
大文字小文字を区別せず `**verdict:**` を含む行) の**上位集合**である。
`Verdict: revise` (太字なし) / `**Verdict**: revise` / `__Verdict:__ revise` /
全角・ゼロ幅文字入りも兆候として扱う — これらは Director 定義のままだと
「兆候なし → 構造化出力で救済」に流れ、1 行目に revise と書いた reviewer の
approve が消費される (k3/k4 と同じ型) ためである。
`verdict` の直後にコロンが無い言及 (`(verdict が revise/reject の場合のみ記載)`
のような雛形の注記) は兆候にしない — 正常系の plan_review.md を
誤って VIOLATION にしないため。

### 意図的に扱わない残余

`## 総合判定: NO-GO` のような verdict 語を使わない別表記、他言語、
同形異字 (キリル文字の е 等) は兆候にならず NO_SIGN として構造化出力に
委ねられる。Director 設計判断「兆候なしの別表記 + structured=approve → ready
(救済は残る)」の範囲であり、この場合は構造化出力が唯一の判定 unit になる。

## 呼び出し側の約束

scripts/review-plan.sh / scripts/wait_for_plan_review.sh は CLI の終了コードを
**allowlist で**解釈すること:

  - 終了コード 0 かつ標準出力が正規の 1 語 → VALID
  - 終了コード 10 かつ標準出力が空 → NO_SIGN (救済してよい唯一の状態)
  - **それ以外はすべて VIOLATION 扱い** — 20 はもちろん、Python の未捕捉例外
    (終了コード 1)、スクリプト自体が無い (python3 の終了コード 2) なども含む。
    t015 までは「0 以外 = 判定不能 = 救済可」だったため、lib_verdict.py が
    落ちるだけで救済経路に入れていた。

extract_canonical_verdict() は VALID のときだけ値を返し、NO_SIGN と VIOLATION を
どちらも None にする (互換用)。**救済するかどうかの判断には使わないこと** —
その判断には classify_verdict() の状態を使う。

Usage (CLI):
  python3 lib_verdict.py <plan_review.md path>
  → VALID     : 標準出力に verdict を書いて exit 0
    NO_SIGN   : 何も出さず exit 10
    VIOLATION : 何も出さず exit 20 (理由は標準エラー)
    引数の誤り: exit 2
"""
from __future__ import annotations

import re
import sys
import unicodedata

# 許容する verdict は この 3 語ちょうどのみ (完全一致 allowlist)。
# config/plan-review-verdict.schema.json の enum と同一・同順。
CANONICAL_VERDICTS = ("approve", "revise", "reject")

STATE_VALID = "valid"
STATE_NO_SIGN = "no_sign"
STATE_VIOLATION = "violation"

EXIT_VALID = 0
EXIT_USAGE = 2
EXIT_NO_SIGN = 10
EXIT_VIOLATION = 20

# 規定形式の verdict 行。行頭アンカー (re.match) で、インデントも引用符 (>) も
# 許容しない。値部分は行末までまるごと捕捉し、完全一致判定は呼び出し側で行う。
_VERDICT_LINE_RE = re.compile(r"\*\*Verdict:\*\*(.*)$")

# 兆候判定で無視する記法文字 (空白・書式文字・結合文字は別途除去する)。
_SIGN_IGNORED_CHARS = frozenset("*_`\\")
_SIGN_TOKEN = "verdict:"


def first_content_line(text: str) -> str | None:
    """先頭の空行を読み飛ばし、最初の非空行を返す。1 行も無ければ None。

    「非空」= 前後の空白 (改行・タブ・全角空白を含む) を除いて 1 文字以上残る行。

    F3 (t002, mission 20260912-verdict-ci-launcher, QA Finn 実測): 行の分割に
    `str.splitlines()` を使わないこと。splitlines() は `\\n` 以外にも
    `\\r` / `\\x0b` / `\\x0c` / `\\x1c` / `\\x1d` / `\\x1e` / U+0085 / U+2028 /
    U+2029 の 9 種すべてで分割する。`str.split("\\n")` は `\\n` だけで
    分割するため、他の行区切り文字は行の中身の一部として残り
    (`_canonical_value_of` の完全一致判定で弾かれる)。
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


def is_verdict_sign(line: str) -> bool:
    """1 行が「verdict 行の兆候」を持つか (定義はモジュール docstring 参照)。

    救済を拒否する方向にしか使わないため、記法を解釈せず広く拾う。
    """
    folded = unicodedata.normalize("NFKD", line).casefold()
    squeezed = "".join(
        ch
        for ch in folded
        if not (
            ch.isspace()
            or ch in _SIGN_IGNORED_CHARS
            or unicodedata.category(ch) in ("Cf", "Mn")
        )
    )
    return _SIGN_TOKEN in squeezed


def classify_verdict(text: str) -> tuple[str, str | None, str]:
    """plan_review.md の本文を 3 状態に分類する。

    戻り値: (state, verdict, reason)
      state   — STATE_VALID / STATE_NO_SIGN / STATE_VIOLATION
      verdict — STATE_VALID のときだけ正規の 1 語、それ以外は None
      reason  — 人間向けの説明 (ログ用)
    """
    lines = text.split("\n")
    sign_lines = [i for i, line in enumerate(lines) if is_verdict_sign(line)]
    if not sign_lines:
        return STATE_NO_SIGN, None, "no verdict-line sign anywhere in the file"

    # 兆候のある行は空白だけの行ではないので、最初の非空行は必ず存在する。
    first_idx = next(i for i, line in enumerate(lines) if line.strip())
    human_lines = ", ".join(str(i + 1) for i in sign_lines)

    if len(sign_lines) != 1:
        return (
            STATE_VIOLATION,
            None,
            f"{len(sign_lines)} verdict-line signs (lines {human_lines}); exactly one, "
            f"on the first non-blank line, is allowed",
        )
    if sign_lines[0] != first_idx:
        return (
            STATE_VIOLATION,
            None,
            f"the only verdict-line sign is on line {human_lines}, not on the first "
            f"non-blank line (line {first_idx + 1})",
        )
    verdict = _canonical_value_of(lines[first_idx])
    if verdict is None:
        return (
            STATE_VIOLATION,
            None,
            f"line {first_idx + 1} is a verdict-line sign but not exactly "
            f"'**Verdict:** approve|revise|reject'",
        )
    return STATE_VALID, verdict, f"canonical verdict on line {first_idx + 1}"


def extract_canonical_verdict(text: str) -> str | None:
    """VALID のときだけ verdict を返す。NO_SIGN も VIOLATION も None。

    互換用。None を「救済してよい」と解釈してはならない — 救済の可否は
    classify_verdict() の状態 (STATE_NO_SIGN のときだけ) で判断すること。
    """
    state, verdict, _reason = classify_verdict(text)
    return verdict if state == STATE_VALID else None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: lib_verdict.py <plan_review.md>", file=sys.stderr)
        return EXIT_USAGE
    path = argv[1]
    try:
        # F2 (t015, mission 20260912-verdict-ci-launcher, PR #199 QA(Finn) t003
        # FAIL-2 / Kai-codex P2): newline="" で開き、universal newlines による
        # 改行変換を無効にする。デフォルトの open() は \r 単体・\r\n・\n の
        # いずれも読み込み時点で \n に変換してしまうため、
        # `**Verdict:** approve\rNOT approved; revisions required` のような
        # バイト列がファイル経由でだけ 2 行に分割され、1 行目
        # `**Verdict:** approve` だけが読まれて approve と誤判定していた。
        # newline="" にすると \r はそのまま文字列に残り、`text.split("\n")` は
        # \r を行区切りとみなさない。正規の CRLF 行末 (`approve\r\n`) は
        # `_canonical_value_of` の `.strip()` が末尾の \r を除去するため
        # 従来どおり approve と読める。
        with open(path, encoding="utf-8", newline="") as f:
            content = f.read()
    except FileNotFoundError:
        # ファイルが無い = 兆候がどこにも無い (reviewer が書かなかった)。
        # 構造化出力による救済 (plan_review.md の新規作成) を許す唯一の
        # 「読めない」ケース。
        print(f"lib_verdict: {STATE_NO_SIGN}: {path} does not exist", file=sys.stderr)
        return EXIT_NO_SIGN
    except (OSError, UnicodeDecodeError) as e:
        # t018: 存在するのに読めない / UTF-8 として解釈できない場合は、兆候が
        # 無いことを確認できない。t015 まではここで UnicodeDecodeError が
        # 未捕捉のまま終了コード 1 になり、呼び出し側の「0 以外 = 救済可」に
        # 流れていた。
        print(
            f"lib_verdict: {STATE_VIOLATION}: cannot read {path} ({e}) — "
            f"cannot confirm the absence of verdict-line signs",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    state, verdict, reason = classify_verdict(content)
    if state == STATE_VALID:
        print(verdict)
        return EXIT_VALID
    print(f"lib_verdict: {state}: {path}: {reason}", file=sys.stderr)
    if state == STATE_NO_SIGN:
        return EXIT_NO_SIGN
    return EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main(sys.argv))
