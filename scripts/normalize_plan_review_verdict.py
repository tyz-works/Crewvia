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

# 既知の別表記 → 正規判定語のマッピング。
# 「倒れる方向に注意」: 複数の判定語が同時にヒットした場合は最も安全側
# (reject > revise > approve) を優先する。判定候補のリスト順がそのまま優先順位。
ALT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("reject", re.compile(r"(no-?go|reject|却下)", re.IGNORECASE)),
    ("revise", re.compile(r"(revise|要修正|差し戻し)", re.IGNORECASE)),
    ("approve", re.compile(r"(\bgo\b|approve|承認|lgtm)", re.IGNORECASE)),
]

# 誤検知防止: 本文中のどこかに偶然 "go" 等が現れただけのケースを拾わないよう、
# 「総合判定」という見出し語の直後 (見出し行自体を含む) だけを探索対象にする。
# 実際の事故は `## 総合判定: **GO**` のように見出し行そのものに判定語があった。
SCOPE_RE = re.compile(r"総合判定[^\n]*")
SCOPE_WINDOW = 200  # 見出し以降、何文字まで判定語を探すか


def find_alt_verdict(content: str) -> str | None:
    m = SCOPE_RE.search(content)
    if not m:
        return None
    scope = content[m.start(): m.start() + SCOPE_WINDOW]
    for verdict, pattern in ALT_PATTERNS:
        if pattern.search(scope):
            return verdict
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
