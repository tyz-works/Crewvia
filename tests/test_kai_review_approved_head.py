"""kai-review.sh は LGTM (findings なし/全て P3) の経路でだけ `approved_head:` を書く
(t001, mission 20261113-approval-gate-holes)。

旧実装は Result の全文を組み立てても `approved_head:` 行を一度も書かなかったため、codex-review が
LGTM で done しても `lib_ci_status.approved_head()` が読む「Result の最後の非空行」に承認 head が無く、
`plan.sh ci-gate` は常に `no_approval` を返し、dispatcher の `merge-ready` は永久に出なかった
(実例: PR #385)。

ここでは `tests/test_kai_review_keeps_full_codex_findings_in_the_card.py` の fixture
(`run_kai_review`) を再利用し、偽の `gh` を差し替えて以下を検査する:
  1. done を呼ぶ直前に取り直した PR の現 head が、レビュー開始時に控えた head と一致すれば
     `approved_head: <sha>` が Result の最後の非空行になる。
  2. 食い違えば (レビュー中に push があった想定) `approved_head:` は書かず、再レビューが要る旨を残す。
  3. 現 head を確認できなければ (gh が失敗) 同じく `approved_head:` は書かない。
  4. needs-director (要修正) の経路では `approved_head:` を書かない。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from test_kai_review_keeps_full_codex_findings_in_the_card import (
    _assert_base_resolved,
    _findings,
    run_kai_review,
)

CLEAN = json.dumps({"findings": []})

# gh pr view <PR> --json headRefName --jq '.headRefName'  → $5 = headRefName
# gh pr view <PR> --json headRefOid  --jq '.headRefOid'   → $5 = headRefOid
GH_MATCHES_FETCHED_HEAD = """#!/bin/bash
if [[ "$5" == "headRefOid" ]]; then
  git -C "$CREWVIA_REPO_ROOT" for-each-ref --format='%(objectname)' 'refs/kai-review-fetch/pr-1-*' | head -1
  exit 0
fi
echo feat
"""

GH_HEAD_CHANGED = """#!/bin/bash
if [[ "$5" == "headRefOid" ]]; then
  echo ffffffffffffffffffffffffffffffffffffffff
  exit 0
fi
echo feat
"""

GH_HEAD_UNKNOWN = """#!/bin/bash
if [[ "$5" == "headRefOid" ]]; then
  exit 1
fi
echo feat
"""


def _origin_pr_head(tmp_path):
    return subprocess.run(
        ["git", "--git-dir", str(tmp_path / "origin.git"), "rev-parse", "refs/pull/1/head"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_approved_head_written_when_head_matches(tmp_path):
    p, capture = run_kai_review(tmp_path, CLEAN, extra_exes={"gh": GH_MATCHES_FETCHED_HEAD})
    _assert_base_resolved(p)
    text = (capture / "done.txt").read_text()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[-1] == f"approved_head: {_origin_pr_head(tmp_path)}", (text, p.stdout, p.stderr)


def test_no_approved_head_when_head_changed_during_review(tmp_path):
    p, capture = run_kai_review(tmp_path, CLEAN, extra_exes={"gh": GH_HEAD_CHANGED})
    _assert_base_resolved(p)
    text = (capture / "done.txt").read_text()
    assert "approved_head:" not in text, text
    assert "再レビュー" in text.splitlines()[-1] or "再レビュー" in text


def test_no_approved_head_when_current_head_cannot_be_confirmed(tmp_path):
    p, capture = run_kai_review(tmp_path, CLEAN, extra_exes={"gh": GH_HEAD_UNKNOWN})
    _assert_base_resolved(p)
    text = (capture / "done.txt").read_text()
    assert "approved_head:" not in text, text
    assert "再レビュー" in text


def test_needs_director_path_never_gets_approved_head(tmp_path):
    p, capture = run_kai_review(tmp_path, _findings(1, 50), extra_exes={"gh": GH_MATCHES_FETCHED_HEAD})
    _assert_base_resolved(p)
    text = (capture / "needs-director.txt").read_text()
    assert "approved_head:" not in text, text
    assert not (capture / "done.txt").exists()
