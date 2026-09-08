#!/usr/bin/env python3
"""scripts/normalize_plan_review_verdict.py <plan_review.md path>

plan-reviewer (Opus) が queue/missions/<slug>/plan_review.md に規定形式の
`**Verdict:** approve|revise|reject` を書かず、`## 総合判定: **GO**` のような
別表記で判定を書いてしまうことがある (t002, mission 20260908-launch-reliability。
実際に 20260908-codex-reviewer-phase3 で観測)。scripts/plan.sh の cmd_review /
scripts/wait_for_plan_review.sh はどちらも行頭 `**Verdict:**` の正規表現でしか
判定を読み取らないため、このフォーマット差だけで 600s タイムアウトしていた。

このスクリプトは:
  1. 既に規定形式の `**Verdict:**` 行があれば何もせず exit 0 (idempotent。
     何度呼んでも安全)。
  2. 無ければ、`## 総合判定` 見出し付近から既知の別表記パターンを探し、
     判定語を推定できた場合のみファイル冒頭に規定形式の `**Verdict:**` 行を
     追記して exit 0 (元の内容は一切変更せず、末尾に残す)。
  3. 規定形式も既知の別表記も見つからなければ **何もせず** exit 1 で返す。
     呼び出し側はこれを「判定なし」として扱うこと — 倒れる方向は必ず
     「待つ / 失敗」側であり、ここで当て推量の verdict を作ってはならない。

Usage: python3 normalize_plan_review_verdict.py <plan_review.md>
Exit codes:
  0 = 有効な verdict が存在する (既に規定形式、または今回正規化できた)
  1 = 判定不能 (規定形式なし、既知の別表記も見つからなかった)
  2 = 引数エラー / ファイルが読めない
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone

# scripts/plan.sh cmd_review と scripts/wait_for_plan_review.sh が期待する
# 正規表現と完全に同じ形式であること (どちらも `^\*\*Verdict:\*\*` を行頭で見る)。
CANON_RE = re.compile(r"^\*\*Verdict:\*\*\s*(approve|revise|reject)\b", re.MULTILINE | re.IGNORECASE)

# reject / revise はこれまでと同じ denylist 方式のまま (今回の変更対象外) —
# 「安全な結論 (reject/revise) は denylist、危険な結論 (approve) は allowlist」
# という設計原則 (t011) では、誤って厳しい側 (reject/revise) に倒れるのは
# 安全なので現状の検出方針を維持してよい。
# 「倒れる方向に注意」: 複数の判定語が同時にヒットした場合は最も安全側
# (reject > revise) を優先する。判定候補のリスト順がそのまま優先順位。
ALT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("reject", re.compile(r"(no-?go|reject|却下)", re.IGNORECASE)),
    ("revise", re.compile(r"(revise|要修正|差し戻し)", re.IGNORECASE)),
]

# 誤検知防止: 本文中のどこかに偶然 "go" 等が現れただけのケースを拾わないよう、
# 「総合判定」という見出し語の直後 (見出し行自体を含む) だけを探索対象にする。
# 実際の事故は `## 総合判定: **GO**` のように見出し行そのものに判定語があった。
SCOPE_RE = re.compile(r"総合判定[^\n]*")
SCOPE_WINDOW = 200  # 見出し以降、何文字まで判定語を探すか

# --- t011 (mission 20260908-launch-reliability, QA t009 FINDING-A) ---
# 旧実装は「approve キーワード (go/approve/承認/lgtm) が scope 内にあり、
# 同じ文/段落に否定語 (NEG_RE) や留保表現 (HEDGE_RE) が無ければ approve」
# という denylist 方式だった。だが日本語の否定形は
# 「GO は出せない」「承認する段階にない」「approve しかねる」のように
# 無数の言い回しがあり、列挙 (NEG_RE) で網羅するのは原理的に不可能。
# F5 (PR#188 t008) で「承認しない」を塞いだ後も、QA (t009) は語彙を変えた
# だけの同じ穴を 6 件実測で再現した。
#
# 対応: 判定方向を反転する (QA 提案・採用)。「approve キーワードがあり
# 否定語が無ければ approve」ではなく「判定 unit を整形した結果が既知の
# 短い肯定形そのものと完全一致する場合にだけ approve、それ以外は判定不能」
# にする。危険な結論 (approve) は allowlist、安全な結論 (reject/revise) は
# denylist、という non-symmetric な倒れ方 — 本ミッションで繰り返し出ている
# 「倒れる方向」の考え方をそのまま適用したもの。
#
# この方式なら「GO は出せない」のような未知の否定形も、単に allowlist の
# どの語とも完全一致しないため自動的に判定不能になる — 否定形を列挙する
# 必要が無くなる (NEG_RE / HEDGE_RE は不要になったため削除)。
APPROVE_EXACT = {"go", "承認", "lgtm", "approve", "問題なし"}

# 判定 unit の先頭に残る見出しラベル (`総合判定` 自体、コロン付き/無し両方)
# を取り除く。見出し行に値が同居する場合 (`総合判定: **GO**`) と、見出し行
# だけで値が次の段落にある場合 (`総合判定` → 空行 → `**修正後 GO**`) の
# 両方に対応するため、unit の先頭が「総合判定」で始まる場合だけ剥がす
# (先頭でなければ値そのものの unit なので何もしない)。
_LABEL_STRIP_RE = re.compile(r"^総合判定\s*[:：]?\s*")
# 完全一致判定の前に取り除く装飾記号 (Markdown 太字 **、見出し #、括弧、
# 句読点等)。前後の装飾だけを剥がし、語の中身はそのまま残す。
_DECORATION_RE = re.compile(r"^[\s#*`「『【(（]+|[\s*`」』】)）。.、,:：]+$")

# 判定語 (verdict) は同じ「文」または「段落」の中で完結しているものだけを
# 見る。scope はヘッダー行から 200 文字先まで見るため、無関係な離れた文
# (例: 別の話題の「ただし」) を跨いで誤爆しないようにするための局所化。
# 区切りは句点 (。) または空行。
_UNIT_SPLIT_RE = re.compile(r"。|\n[ \t]*\n")


def _iter_units(scope: str):
    """`scope` を「文/段落」単位に分割してイテレートする。"""
    start = 0
    for m in _UNIT_SPLIT_RE.finditer(scope):
        yield scope[start:m.start()]
        start = m.end()
    yield scope[start:]


def _canonical_unit(unit: str) -> str:
    """unit を完全一致判定用に整形する: 見出しラベル除去 → 前後の装飾記号除去。"""
    stripped = _LABEL_STRIP_RE.sub("", unit, count=1)
    stripped = _DECORATION_RE.sub("", stripped)
    return stripped.strip()


def find_alt_verdict(content: str) -> str | None:
    m = SCOPE_RE.search(content)
    if not m:
        return None
    scope = content[m.start(): m.start() + SCOPE_WINDOW]

    # reject / revise は既存の denylist 方式のまま (scope 全体を探索)。
    for verdict, pattern in ALT_PATTERNS:
        if pattern.search(scope):
            return verdict

    # approve だけ allowlist に反転 (t011) — unit 全体が既知の短い肯定形
    # そのものと完全一致する場合にだけ approve。
    for unit in _iter_units(scope):
        if _canonical_unit(unit).lower() in APPROVE_EXACT:
            return "approve"
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: normalize_plan_review_verdict.py <plan_review.md>", file=sys.stderr)
        return 2

    path = argv[1]
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        print(f"normalize_plan_review_verdict: cannot read {path}: {e}", file=sys.stderr)
        return 2

    if CANON_RE.search(content):
        return 0  # 既に規定形式 — 何もしない

    verdict = find_alt_verdict(content)
    if verdict is None:
        return 1  # 判定不能 — 呼び出し側は「判定なし」として扱う

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        f"**Verdict:** {verdict}\n"
        f"<!-- normalized by scripts/normalize_plan_review_verdict.py at {ts}; "
        f"original wording preserved below (t002) -->\n\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + content)
    except OSError as e:
        print(f"normalize_plan_review_verdict: cannot write {path}: {e}", file=sys.stderr)
        return 2

    print(f"normalize_plan_review_verdict: normalized to '{verdict}' in {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
