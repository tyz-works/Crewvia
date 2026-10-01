#!/usr/bin/env python3
"""tests/task_controller_worker.py — Task Controller の並行テストの**独立プロセス**側 (test_task_controller_concurrency.py)。

    task_controller_worker.py <queue> <mode> <agent> <start_at_epoch> [iterations]

mode:
  once  start_at まで待ってから同じ task (t001) を 1 回だけ reserve する。結果 (勝ち / 拒否のコード) を出力
  loop  start_at まで待ってから、reserve → (勝てば) start → reset を iterations 回。勝った attempt と ID を出力
        (毎回 reserve から始めるので、取り合いが何度も起きる)

mock のロックではなく**実 flock**・**別プロセス**。plan.sh は使わない (E1 は呼び出し元ゼロの lib)。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import lib_execution as ex  # noqa: E402
import lib_state_store as store  # noqa: E402
import lib_task_controller as ctl  # noqa: E402

MISSION = "m-exec"


def _wait(start_at):
    while time.time() < start_at:
        time.sleep(0.0005)


def _reserve(queue, agent, stamp):
    with store.transaction(queue, op="pull", actor=agent) as t:
        t.recover(store.Scope(cards=((MISSION, "t001"),), agents=(agent,)))
        return ctl.reserve_task(t, MISSION, "t001", agent, now=stamp)


def once(queue, agent, start_at):
    _wait(start_at)
    try:
        ctx = _reserve(queue, agent, time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()))
        return {"agent": agent, "won": True, "id": ctx.execution_id, "attempt": ctx.attempt}
    except ex.ControllerError as e:
        return {"agent": agent, "won": False, "code": e.code}


def loop(queue, agent, start_at, n):
    _wait(start_at)
    wins, refused = [], 0
    for i in range(n):
        try:
            stamp = "2026-10-01T10:%02d:%02d.%06dZ" % (i % 60, os.getpid() % 60, int(time.time() * 1e6) % 1_000_000)
            ctx = _reserve(queue, agent, stamp)
        except ex.ControllerError as e:
            assert e.code == ex.TASK_ALREADY_RESERVED, e.code
            refused += 1
            continue
        wins.append({"id": ctx.execution_id, "attempt": ctx.attempt})
        with store.transaction(queue, op="pull2", actor=agent) as t:
            ctl.start_execution(t, MISSION, "t001", ctx.execution_id, None)
        with store.transaction(queue, op="update", actor=agent) as t:
            ctl.reset_task(t, MISSION, "t001")
    return {"agent": agent, "wins": wins, "refused": refused}


if __name__ == "__main__":
    queue, mode, agent, start_at = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    if mode == "once":
        out = once(queue, agent, start_at)
    else:
        out = loop(queue, agent, start_at, int(sys.argv[5]))
    print(json.dumps(out))
