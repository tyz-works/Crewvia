#!/usr/bin/env python3
"""scripts/lib_verdict.py — plan_review.md の規定形式 verdict 抽出の単一実装。

t010 (mission 20260909-dead-config-sweep, QA t008 FINDING-1/2/3) の対応。

## 背景

plan_review.md の verdict 抽出は従来、以下の3層がそれぞれ独自の正規表現を
持っていた:
  - scripts/wait_for_plan_review.sh: `grep -E '^\\*\\*Verdict:\\*\\*...'`
  - scripts/normalize_plan_review_verdict.py: `CANON_RE` (`^...`, re.MULTILINE)
  - scripts/plan.sh cmd_review: `re.search(r'\\*\\*Verdict:\\*\\*...')`（アンカー無し）

3層とも次の穴を共有していた (QA t008 が実 plan.sh review をエンドツーエンドで
走らせて実測):
  - FINDING-1: コードフェンス (```) を除去していないため、フェンス内の
    書式例だけが「有効な verdict」として誤って採用される。
  - FINDING-2: ファイル内で最初に一致した行を採用するだけで、複数の判定が
    混在していても検出しない。フェンス内の書式例 approve が、後続の本物の
    revise を上書きしてしまう。
  - FINDING-3: `**Verdict:** approve | revise | reject` のように1行に複数の
    判定語が並ぶテンプレート行 (agents/plan_reviewer.md 由来) を、最初に
    一致した語 (approve) だけで確定させてしまう。

## 設計

判定 unit を1本に絞る (このミッションで繰り返し採用している「危険な結論は
allowlist・判定 unit は1つ」の原則をそのまま適用):
  1. まずコードフェンス (```...```) を全て取り除く。フェンス内の内容は
     判定の対象にしない (書式例として提示されただけの可能性が高いため)。
  2. 残った本文から `^\\*\\*Verdict:\\*\\*` で始まる行を全て集める。
  3. 各行について、行の値部分に approve/revise/reject のうち何種類の
     判定語が含まれるかを数える。2種類以上含まれる行 (テンプレート行の
     コピペ等) は「その行からは判定を読み取らない」として捨てる。
  4. 全行を通じて有効な判定語の集合を作る。集合の要素数が exactly 1 の
     場合だけ、その値を verdict として確定する。0件 (判定語なし) でも
     2件以上 (異なる判定が混在) でも判定不能として None を返す —
     「最初に見つかった approve を採る」という倒れ方は絶対にしない。

正常系 (`**Verdict:** approve` 1行だけ、フェンスなし) は現状と完全に同じ
挙動になる。既存の呼び出し元 (wait_for_plan_review.sh の grep 相当,
normalize_plan_review_verdict.py の CANON_RE 相当, plan.sh cmd_review の
re.search 相当) はいずれもこのモジュールの `extract_canonical_verdict()` に
置き換える。

Usage (CLI):
  python3 lib_verdict.py <plan_review.md path>
  → 有効な verdict が一意に確定すれば標準出力に書いて exit 0。
    判定不能 (0件 / 複数混在 / ファイルが読めない) なら何も出さず exit 1。
"""
from __future__ import annotations

import re
import sys

# コードフェンス (```...```、言語指定の有無を問わない) を除去する。
# DOTALL で複数行にまたがるフェンスも1つのブロックとして扱う。
# 閉じられていないフェンス (``` が奇数個) は保守的にそのまま残す —
# 「フェンス内かもしれない」だけで本文まで巻き込んで消してしまうと、
# 逆に正規の verdict が読めなくなる事故につながるため。
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# 規定形式の verdict 行 (値部分は行末までまるごと捕捉し、値の解釈は
# _parse_verdict_words() に委ねる)。ラベル自体の大文字小文字は区別しない
# 呼び出し元が過去に無かった (bash grep / plan.sh の re.search はどちらも
# 大文字小文字を区別していた) ため、ここでも区別しない (完全一致のみ)。
_VERDICT_LINE_RE = re.compile(r"^\*\*Verdict:\*\*[ \t]*(.*)$", re.MULTILINE)

# 判定語そのものは表記ゆれ (Approve / APPROVE 等) を許容する。大文字小文字を
# 区別しても実運用上の安全性は変わらず (許容語彙自体は3語で固定)、むしろ
# 表記ゆれで判定不能に落ちる方が Director の手間を増やすだけなので緩める。
_KNOWN_WORDS = ("approve", "revise", "reject")
_WORD_RE = re.compile(r"\b(" + "|".join(_KNOWN_WORDS) + r")\b", re.IGNORECASE)


def strip_code_fences(text: str) -> str:
    """コードフェンスで囲まれた範囲を除去したテキストを返す。"""
    return _FENCE_RE.sub("", text)


def _words_in_line(value: str) -> set[str]:
    """verdict 行の値部分に含まれる判定語 (小文字正規化済み) の集合を返す。"""
    return {m.group(1).lower() for m in _WORD_RE.finditer(value)}


def extract_canonical_verdict(text: str) -> str | None:
    """規定形式 `**Verdict:** approve|revise|reject` から verdict を1つ確定する。

    フェンス除去 → 各 verdict 行ごとに判定語を数える → 1行に2種類以上の
    判定語が混在する行は捨てる → 全行を通じて有効な判定語の集合を作る →
    集合の要素数が exactly 1 の場合だけ確定。それ以外 (0件 / 複数混在) は
    None (判定不能。呼び出し側は「待つ / 失敗」側に倒すこと)。
    """
    stripped = strip_code_fences(text)
    found: set[str] = set()
    for m in _VERDICT_LINE_RE.finditer(stripped):
        words = _words_in_line(m.group(1))
        if len(words) == 1:
            found |= words
        # len(words) == 0 (未知の判定語) / >= 2 (テンプレート行等、
        # FINDING-3) の行は判定材料として採用しない — 黙って捨てる。
    if len(found) == 1:
        return next(iter(found))
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: lib_verdict.py <plan_review.md>", file=sys.stderr)
        return 2
    path = argv[1]
    try:
        with open(path, encoding="utf-8") as f:
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
