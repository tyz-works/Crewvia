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

# t008 (PR#188 レビュー指摘 F5, Seo 実測): ALT_PATTERNS の優先順
# (reject > revise > approve) は複数パターンが同時にヒットしたときにしか
# 効かない。「承認しない」「承認できない」「LGTM とは言えない」のように
# 「承認」系の語が否定文脈で単独ヒットすると、そのまま approve に化けていた。
# scope 内に否定語があり、かつ approve 以外の判定語が当たらない場合は
# 「判定不能」として安全側 (None) に倒す。
NEG_RE = re.compile(
    r"(しない|しません|できない|できません|ではない|ではありません|とは言えな|見送|不可|NG)",
    re.IGNORECASE,
)

# t008 追加 (QA t003 FINDING-2, 実運用で観測): 「修正後 GO」「条件付き GO」
# 「GO（ただし...が前提）」のような留保付き判定も、NEG_RE と同じ穴で無条件
# approve に化けていた。plan-reviewer が実際に書いた例:
#   ## 総合判定
#   **修正後 GO**
#   理由: ... Director が上記の Description 補記を行えば即実行可。
# 「修正してから GO」は実質 revise 相当であり、これが無条件 approve として
# launch まで通っていた。normalize の docstring が謳う「倒れる方向は必ず
# 待つ / 失敗側」から外れる退行 — 修正前は 600s タイムアウトして Director が
# 手で見ていたものが、この PR で「静かな誤 approve」に変わっていた。
HEDGE_RE = re.compile(r"(修正後|条件付|ただし|前提)")

# NEG_RE / HEDGE_RE は判定語 (verdict) と同じ「文」または「段落」の中に
# ある場合だけ approve を無効化する。scope はヘッダー行から 200 文字先まで
# 見るため、無関係な離れた文にある否定語・留保表現 (例: 別の話題の「ただし」)
# で誤爆しないようにするための局所化。区切りは句点 (。) または空行。
_UNIT_SPLIT_RE = re.compile(r"。|\n[ \t]*\n")


def _unit_around(scope: str, pos: int) -> str:
    """`scope` 内で index `pos` を含む「文/段落」単位を返す。"""
    start = 0
    for m in _UNIT_SPLIT_RE.finditer(scope):
        if m.start() >= pos:
            return scope[start:m.start()]
        start = m.end()
    return scope[start:]


def find_alt_verdict(content: str) -> str | None:
    m = SCOPE_RE.search(content)
    if not m:
        return None
    scope = content[m.start(): m.start() + SCOPE_WINDOW]
    for verdict, pattern in ALT_PATTERNS:
        vm = pattern.search(scope)
        if not vm:
            continue
        if verdict == "approve":
            # approve は否定文脈 (「承認しない」等) と留保文脈
            # (「修正後 GO」等) の両方で誤爆しやすい — 判定語と同じ文/段落に
            # 否定語または留保表現があれば判定不能扱いにして安全側に倒す。
            # 例外: 「GO。ただし Codex review は Director 判断」のように、
            # 句点で文が切れた後の別の話題は同じ unit に含まれないため
            # 誤爆しない。
            unit = _unit_around(scope, vm.start())
            if NEG_RE.search(unit) or HEDGE_RE.search(unit):
                return None
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
