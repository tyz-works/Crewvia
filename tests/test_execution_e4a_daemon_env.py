#!/usr/bin/env python3
"""01c E4a (t016): デーモンが plan.sh を呼ぶときの `AGENT_NAME` (監査ログの `actor`。execution.md §8 の (2))。

- dispatcher の `reap-orphan-assignment` は subprocess の env に `AGENT_NAME=dispatcher` を入れる。**Director のシェルから継いだ
  `AGENT_NAME` を載せない** (継いだ名前の行になると、Director の操作とデーモンの操作が監査で区別できない)
- watchdog (lib_retirement) の `plan.sh retire` は `AGENT_NAME=watchdog` (`test_execution_e4a_retire_reset.py` が実 plan.sh で固定)

dispatcher.sh は heredoc の python を `exec()` して本物の関数を呼ぶ (ロジックを複製しない)。**本番の queue / registry / mux には触れない**。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import subprocess  # noqa: E402

from test_dispatcher_retirement_exclusion import FakeMux, _load_dispatcher  # noqa: E402
from test_needs_director_releases_assignment import _assign, _put_card, _tasks_dir  # noqa: E402
from test_reap_orphan_assignment import CODEX, SLUG, _dispatcher_root  # noqa: E402


def _audit_rows(root):
    rows = []
    for f in sorted((root / "queue" / "audit").glob("transitions-*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return rows


def _orphan_root(tmp_path):
    root = _dispatcher_root(tmp_path)
    _put_card(_tasks_dir(root), "t011", "done", aged=True, worker=CODEX, skills="codex-review", pr=4)
    _assign(root, CODEX, f"{SLUG}:t011\n")
    return root


def test_dispatcher_sets_the_actor_for_the_reap(tmp_path, monkeypatch):
    """Director のシェルから継いだ AGENT_NAME があっても、reap の subprocess には `dispatcher` が入る。"""
    monkeypatch.setenv("AGENT_NAME", "Director")
    root = _orphan_root(tmp_path)
    ns = _load_dispatcher(root, FakeMux([]))
    seen = []

    def spy_run(argv, **kw):
        seen.append((list(argv), dict(kw.get("env") or {})))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    ns["subprocess"] = types.SimpleNamespace(**{**vars(subprocess), "run": spy_run})
    ns["reap_kai_codex_orphan_assignment"]({SLUG: {"t011": "done"}})
    reap = [(a, e) for a, e in seen if "reap-orphan-assignment" in a]
    assert len(reap) == 1, seen
    assert reap[0][1]["AGENT_NAME"] == "dispatcher"


def test_the_real_reap_through_the_dispatcher_removes_the_orphan_and_logs_the_dispatcher(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "Director")
    monkeypatch.setenv("CREWVIA_TASKVIA", "disabled")
    root = _orphan_root(tmp_path)
    ns = _load_dispatcher(root, FakeMux([]))
    ns["reap_kai_codex_orphan_assignment"]({SLUG: {"t011": "done"}})
    assert not (root / "queue" / "assignments" / CODEX).exists()
    rows = [r for r in _audit_rows(root) if r.get("actor") not in (None, "test")]
    assert rows, "監査ログに行が無い"
    assert all(r["actor"] == "dispatcher" for r in rows), rows
