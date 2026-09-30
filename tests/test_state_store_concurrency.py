"""独立プロセスでの並行 (原案 §10.2 / STATE-02): 同じ card への同時更新で card が壊れず、
projection が二重にならない。mock のロックではなく**実 flock**・**別プロセス**で確かめる。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import state_store_scenarios as sc       # scripts/ を sys.path に足す (lib より先)
import lib_state_store as store

WORKER = pathlib.Path(__file__).with_name("state_store_worker.py")
MISSION = "m-conc"
PROCS = 6
ITER = 25


def _seed(q):
    with store.transaction(q, op="seed", actor="t") as t:
        t.write_state({"active_missions": [MISSION], "default_mission": MISSION})
        t.write_mission(MISSION, {"title": "c", "slug": MISSION, "status": "in_progress", "next_task_id": 2})
        t.write_card(MISSION, "t001", sc.card("t001", rework_count=0), sc.body())


def _run(q, mode, names, n):
    procs = [subprocess.Popen([sys.executable, str(WORKER), str(q), mode, name, str(n)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=True)
             for name in names]
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=120)
        assert p.returncode == 0, stderr
        out.append(json.loads(stdout))
    return out


def test_concurrent_read_modify_write_loses_no_update_and_never_corrupts(tmp_path):
    q = tmp_path / "q"
    _seed(q)
    names = [f"W{i}" for i in range(PROCS)]
    _run(q, "counter", names, ITER)
    meta = sc.read_meta(q, MISSION, "t001")
    assert meta["rework_count"] == PROCS * ITER                      # 1 件も失われていない
    # 監査ログ: 全行が完全な JSON で、行数 = トランザクション数 (行が混ざらない)
    rows = [json.loads(line) for p in (q / "audit").glob("*.jsonl") for line in p.read_text().splitlines()]
    assert len([r for r in rows if r["op"] == "counter"]) == PROCS * ITER
    assert {r["actor"] for r in rows if r["op"] == "counter"} == set(names)
    assert store.diagnose(q) == []


def test_concurrent_claims_never_double_the_projection(tmp_path):
    q = tmp_path / "q"
    _seed(q)
    names = [f"C{i}" for i in range(PROCS)]
    res = _run(q, "claim", names, ITER)
    claimed = sum(r["claimed"] for r in res)
    released = sum(r["released"] for r in res)
    assert claimed >= PROCS                                            # 空虚でない: 実際に取り合った
    assert claimed - released in (0, 1)                                # 取った数 - 手放した数 = いま持っている数
    meta = sc.read_meta(q, MISSION, "t001")
    slots = [p.name for p in (q / "assignments").iterdir()
             if not p.name.endswith(".identity") and p.read_text().strip() == f"{MISSION}:t001"] \
        if (q / "assignments").exists() else []
    if meta["status"] == "in_progress":
        assert slots == [meta["worker"]]                               # 持っているのは 1 人だけ・枠は 1 つ
        assert sc.identity(q, meta["worker"])["started_at"] == meta["started_at"]
    else:
        assert slots == [] and claimed == released
    assert store.diagnose(q) == []                                     # 食い違い・残骸なし
    rows = [json.loads(line) for p in (q / "audit").glob("*.jsonl") for line in p.read_text().splitlines()]
    assert len([r for r in rows if r["op"] == "claim" and r["to_status"] == "in_progress"]) == claimed
