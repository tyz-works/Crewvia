"""文書の `git fetch origin pull/N/head:...` は + 付き (force) でなければならない。

+ が無いと、force-push で PR の head が変わった後に同じ ref へ再 fetch したとき
rejected (non-fast-forward) になり、ref が古いまま残る (#394 レビュー P3)。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"git fetch origin [\"']?(?!\+)(?:refs/)?pull/")


def test_doc_fetches_of_pr_head_have_plus():
    offenders = []
    for sub in ("agents", "knowledge", "skills"):
        for p in (ROOT / sub).rglob("*.md"):
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if PATTERN.search(line):
                    offenders.append(f"{p.relative_to(ROOT)}:{n}")
    assert not offenders, offenders
