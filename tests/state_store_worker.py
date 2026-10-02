#!/usr/bin/env python3
"""tests/state_store_worker.py — 並行テストの**独立プロセス**側 (test_state_store_concurrency.py が起動する)。

    state_store_worker.py <queue> <mode> <name> <iterations>

mode:
  counter  同じ card の `rework_count` を read-modify-write で +1 (ロックが無ければ更新が失われる)
  claim    同じ task を取り合う: pending なら自分が持つ (card → identity → assignment)、
           自分のものなら手放す (card → assignment 撤去)。取れた回数・手放した回数を出力する
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import lib_state_store as store  # noqa: E402

MISSION = "m-conc"


def counter(queue, name, n):
    for _ in range(n):
        with store.transaction(queue, op="counter", actor=name) as t:
            meta, body = t.load_card(MISSION, "t001")
            meta["rework_count"] = int(meta["rework_count"]) + 1
            t.write_card(MISSION, "t001", meta, body)
            t.record(MISSION, "t001", None, None)
    return {"name": name}


def claim(queue, name, n):
    claimed = released = 0
    for i in range(n):
        with store.transaction(queue, op="claim", actor=name) as t:
            t.recover(store.Scope(cards=((MISSION, "t001"),), agents=(name,)))
            meta, body = t.load_card(MISSION, "t001")
            if meta["status"] == "pending":
                gen = f"{name}-{i}"
                meta.update(status="in_progress", worker=name, started_at=gen)
                t.write_card(MISSION, "t001", meta, body)
                t.publish_assignment(name, MISSION, "t001", gen)
                t.record(MISSION, "t001", "pending", "in_progress", gen)
                claimed += 1
            elif meta["worker"] == name:
                gen = meta["started_at"]
                meta.update(status="pending", worker=None, started_at=None)
                t.write_card(MISSION, "t001", meta, body)
                t.retire_assignment(name, MISSION, "t001", None)   # E4b: 世代では名指ししない (card の持ち主の枠)
                t.record(MISSION, "t001", "in_progress", "pending", gen)
                released += 1
    return {"name": name, "claimed": claimed, "released": released}


if __name__ == "__main__":
    queue, mode, name, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    print(json.dumps({"counter": counter, "claim": claim}[mode](queue, name, n)))
