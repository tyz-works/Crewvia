#!/usr/bin/env python3
"""tests/execution_e3_helpers.py — 01c E3 (done / fail / needs-director / ready-for-verification / verify-result の Controller 化) の
テスト共通の道具。`pull_execution_helpers.Box` (隔離 plan.sh + 隔離 queue + stub の git-helpers) の上に、
報告コマンドを打つ・card を作る・「何も書かなかった」を比べる道具を足す。**本番の queue / registry / mux には触れない**。
"""

from __future__ import annotations

import json
import re
import subprocess

import pull_execution_helpers as h
from pull_execution_helpers import Box, MISSION, SECRET  # noqa: F401

sc = h.sc
store = h.store

XID_RE = re.compile(r"ex-[0-9a-f]{32}")
OTHER_ID = "ex-" + "1" * 32          # 形は正しいが、どの card の試行でもない


def take(box, agent="Ren", tid="t001"):
    """pull して、発行された execution id を返す (JSON から。`.crewvia-env` は読まない)。"""
    p = box.pull(agent, tid)
    assert p.returncode == 0, (p.stdout, p.stderr)
    xid = json.loads(p.stdout)["execution_id"]
    assert XID_RE.fullmatch(xid)
    return xid


def run(box, *args, agent="Ren", env=None, timeout=120):
    """plan.sh を 1 回。`env` で環境変数を足す (None の値は消す)。"""
    e = box.proc_env(agent)
    for k, v in (env or {}).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return subprocess.run(box.argv(*args), env=e, capture_output=True, text=True, timeout=timeout)


def report_argv(command, tid="t001"):
    """報告コマンドの最小の引数 (`--mission` 付き)。verify-result は verdict 付き。"""
    table = {
        "done": ["done", tid, "r", "--mission", MISSION],
        "fail": ["fail", tid, "--no-head", "x", "--mission", MISSION],
        "needs-director": ["needs-director", tid, "why", "--mission", MISSION],
        "ready-for-verification": ["ready-for-verification", tid, "--mission", MISSION],
        "verify-pass": ["verify-result", tid, "pass", "--mission", MISSION],
        "verify-fail": ["verify-result", tid, "fail", "--mission", MISSION],
        "verify-nhr": ["verify-result", tid, "needs_human_review", "--mission", MISSION],
        "verifying": ["verifying", tid, "--verifier", "V1", "--mission", MISSION],
    }
    return list(table[command])


def last_error_code(stderr):
    return h.last_error_code(stderr)


def set_card(box, tid="t001", **updates):
    """card の欄を lib 経由で書き換える (None の値は欄を消す)。試行の欄も同じ道で動かせる (場面を作るため)。"""
    with store.transaction(box.queue, op="seed", actor="test") as t:
        meta, body = t.load_card(MISSION, tid)
        meta = dict(meta)
        for k, v in updates.items():
            if v is None:
                meta.pop(k, None)
            else:
                meta[k] = v
        t.write_card(MISSION, tid, meta, body)


def new_card(box, tid, status="pending", **kw):
    """sc.card で作った card を書く (旧形式 = 試行の欄なしの card も作れる)。"""
    with store.transaction(box.queue, op="seed", actor="test") as t:
        t.write_card(MISSION, tid, sc.card(tid, status, **kw), sc.body())


def legacy_in_progress(box, tid="t002", worker="Ren"):
    """E2 の前に pull された形: in_progress・worker・started_at・枠あり・**試行の欄なし**。"""
    with store.transaction(box.queue, op="seed", actor="test") as t:
        t.write_card(MISSION, tid, sc.card(tid, "in_progress", worker, sc.GEN), sc.body())
        t.publish_assignment(worker, MISSION, tid, sc.GEN)


def audit_rows_for(box, op=None):
    rows = box.audit_rows()
    return [r for r in rows if op is None or r["op"] == op]


def refusals(box, op=None):
    return [r for r in audit_rows_for(box, op) if str(r.get("result", "")).startswith("refused:")]


def bodies_unchanged(box, before):
    """queue のバイト列 (監査ログを除く) が `before` と同じか。何も書かなかったことの比較。"""
    return box.snapshot() == before
