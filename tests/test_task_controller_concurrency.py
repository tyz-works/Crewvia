"""独立プロセスでの並行 reserve (vNext 01c E1。受入条件): 同じ task に 2〜4 プロセス・20 回反復で、
active な Execution が 1 つだけ・attempt の重複 0。mock のロックではなく**実 flock**・**別プロセス**。

* `once`: 毎回 fresh な queue で、全員が同じ瞬間に reserve を 1 回打つ → 勝つのは**ちょうど 1 人**、残りは
  `TASK_ALREADY_RESERVED`。card の `execution_count` = 1・record 1 件・枠 1 つ
* `loop`: 同じ queue で reserve → start → reset を繰り返して取り合う → 勝った attempt が 1..K で重複なし・
  ID の重複なし・record は K 件・食い違いなし (`diagnose` が空)
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, ex, store

WORKER = pathlib.Path(__file__).with_name("task_controller_worker.py")
REPS = 20


def _spawn(q, mode, names, extra=()):
    start_at = time.time() + 0.6                      # 全プロセスが python の起動を終えてから同時に走る
    procs = [subprocess.Popen([sys.executable, str(WORKER), str(q), mode, name, str(start_at), *map(str, extra)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
             for name in names]
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=180)
        assert p.returncode == 0, stderr
        out.append(json.loads(stdout))
    return out


@pytest.mark.parametrize("nprocs", [2, 3, 4])
def test_exactly_one_reserve_wins_per_round_and_the_rest_are_refused_with_the_machine_readable_code(tmp_path, nprocs):
    for rep in range(REPS):
        q = h.seed(tmp_path / f"q{nprocs}-{rep}")
        names = [f"P{i}" for i in range(nprocs)]
        res = _spawn(q, "once", names)
        winners = [r for r in res if r["won"]]
        assert len(winners) == 1, (rep, res)
        assert all(r["code"] == ex.TASK_ALREADY_RESERVED for r in res if not r["won"]), res
        meta = h.read_meta(q)
        assert meta["execution_count"] == 1 and meta["worker"] == winners[0]["agent"]
        assert meta["current_execution_id"] == winners[0]["id"] and winners[0]["attempt"] == 1
        assert h.record_files(q) == [winners[0]["id"] + ".json"]                   # active な Execution は 1 つだけ
        slots = [p.name for p in (pathlib.Path(q) / "assignments").iterdir() if not p.name.endswith(".identity")]
        assert slots == [winners[0]["agent"]]
        active = [f for f in h.record_files(q) if h.read_record(q, f[:-5])["status"] in ex.ACTIVE_STATUSES]
        assert len(active) == 1
        assert h.diagnose_kinds(q) == []
        refusals = [r for r in h.audit_rows(q) if r["result"] == "refused:TASK_ALREADY_RESERVED"]
        assert len(refusals) == nprocs - 1 and all(r["execution_id"] == winners[0]["id"] for r in refusals)


@pytest.mark.parametrize("nprocs", [2, 4])
def test_repeated_contention_never_duplicates_an_attempt_or_an_id(tmp_path, nprocs):
    iterations = 20
    q = h.seed(tmp_path / "q")
    names = [f"L{i}" for i in range(nprocs)]
    res = _spawn(q, "loop", names, extra=(iterations,))
    wins = [w for r in res for w in r["wins"]]
    refused = sum(r["refused"] for r in res)
    assert len(wins) >= 5                                                            # 空虚でない: 実際に何度も取れた (全員が毎回勝つわけではない)
    assert refused > 0                                                              # 取り合いが本当に起きた
    attempts = sorted(w["attempt"] for w in wins)
    assert attempts == list(range(1, len(wins) + 1))                                # 重複 0・欠番 0
    ids = [w["id"] for w in wins]
    assert len(set(ids)) == len(ids)
    assert len(h.record_files(q)) == len(wins)                                      # 履歴が 1 件も欠けない
    meta = h.read_meta(q)
    assert meta["execution_count"] == len(wins) and meta["status"] == "pending"
    # 全 record が terminal (どの瞬間も active は 1 つだけだったので、最後に active は残らない)
    assert all(h.read_record(q, f[:-5])["status"] in ex.TERMINAL_EXECUTION_STATUSES for f in h.record_files(q))
    assert h.diagnose_kinds(q) == []
    assert not (pathlib.Path(q) / "assignments").exists() or not [
        p for p in (pathlib.Path(q) / "assignments").iterdir()]
    # 監査: reserve の行ごとに card の ID が入っている
    reserves = [r for r in h.audit_rows(q) if r["op"] == "pull" and r["result"] == "ok"]
    assert sorted(r["execution_id"] for r in reserves) == sorted(ids)
