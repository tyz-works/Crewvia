#!/usr/bin/env python3
"""tests/state_store_scenarios.py — lib_state_store のテスト共通の道具 (seed / 場面 / 不変条件)。

場面 (`SCENARIOS`) は設計 §2.2 の表の「複数ファイルを順に書くコマンド」を **lib の API だけで**
再現したもの (plan.sh はまだ lib を呼ばない — S2 は呼び出し元ゼロ)。各場面は設計の書く順序
(正本が先 / 派生値は正本より前 / projection は最後) をそのまま並べる。

`fork_and_crash()` は場面を子プロセスで走らせ、`FAULT_HOOK` の k 番目の呼び出しで自分に
SIGKILL を送る (= その点で落ちた状態を作る)。子は必ず SIGKILL か `os._exit` で終わる
(pytest の後始末に戻らない)。
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lib_state_store as store  # noqa: E402
import lib_task_cards as cards  # noqa: E402

MISSION = "m-crash"
AGENT = "Haruto"
GEN = "2026-09-30T10:00:00.000000Z"
NEW_GEN = "2026-09-30T11:00:00.000000Z"
PR = 123


def card(tid, status="pending", worker=None, started_at=None, **extra):
    meta = {
        "id": tid, "title": f"task {tid}", "skills": ["code"], "priority": "high",
        "status": status, "blocked_by": [], "target_dir": None, "worker": worker,
        "started_at": started_at, "completed_at": None,
    }
    meta.update(extra)
    return meta


def body(desc="do it"):
    return f"## Description\n{desc}\n\n## Result\n\n"


def seed(queue: pathlib.Path, scenario: str) -> None:
    """場面ごとの開始状態を、lib 自身で (原子的に) 作る。"""
    queue.mkdir(parents=True, exist_ok=True)
    with store.transaction(queue, op="seed", actor="test") as t:
        t.write_state({"active_missions": [MISSION], "default_mission": MISSION})
        t.write_mission(MISSION, {"title": "m", "slug": MISSION, "status": "in_progress",
                                  "created_at": "2026-09-30T00:00:00Z", "completed_at": None,
                                  "next_task_id": 3})
        if scenario == "pull":
            t.write_card(MISSION, "t001", card("t001"), body())
        elif scenario in ("done", "fail", "reset"):
            t.write_card(MISSION, "t001", card("t001", "in_progress", AGENT, GEN), body())
            t.write_card(MISSION, "t002", card("t002", blocked_by=["t001"]), body())
            t.publish_assignment(AGENT, MISSION, "t001", GEN)
        elif scenario == "add":
            t.write_card(MISSION, "t001", card("t001", "done", completed_at=GEN), body())
            t.write_card(MISSION, "t002", card("t002", "done", completed_at=GEN), body())
            t.write_mission(MISSION, {**t.load_mission(MISSION), "next_task_id": 3})
        elif scenario == "archive":
            t.write_card(MISSION, "t001", card("t001", "done", completed_at=GEN), body())
        else:
            raise ValueError(scenario)


# ---------------------------------------------------------------------------
# 場面 (設計 §2.1 / §2.2 の書く順序をそのまま)
# ---------------------------------------------------------------------------

def run_pull(queue):
    with store.transaction(queue, op="pull", actor=AGENT) as t:
        meta, b = t.load_card(MISSION, "t001")
        meta.update(status="in_progress", worker=AGENT, started_at=NEW_GEN)
        t.write_card(MISSION, "t001", meta, b)                    # 正本 = コミット点
        t.publish_assignment(AGENT, MISSION, "t001", NEW_GEN)     # identity → 本体
        t.record(MISSION, "t001", "pending", "in_progress", NEW_GEN)


def run_done(queue):
    with store.transaction(queue, op="done", actor=AGENT) as t:
        meta, b = t.load_card(MISSION, "t001")
        meta["pr_number"] = PR
        t.write_card(MISSION, "t001", meta, b)                    # D1 番号の永続化 (status は据え置き)
        dep, db = t.load_card(MISSION, "t002")
        dep["pr_number"] = PR
        t.write_card(MISSION, "t002", dep, db)                    # D2 派生値 (正本より前)
        meta.update(status="done", completed_at=NEW_GEN)
        t.write_card(MISSION, "t001", meta, b)                    # D3 コミット点
        t.retire_assignment(AGENT, MISSION, "t001", None)         # D4 projection
        t.record(MISSION, "t001", "in_progress", "done", GEN)


def run_fail(queue):
    with store.transaction(queue, op="fail", actor=AGENT) as t:
        meta, b = t.load_card(MISSION, "t001")
        meta["status"] = "failed"
        t.write_card(MISSION, "t001", meta, b)
        t.retire_assignment(AGENT, MISSION, "t001", None)
        t.record(MISSION, "t001", "in_progress", "failed", GEN)


def run_reset(queue):
    with store.transaction(queue, op="update", actor="director") as t:
        meta, b = t.load_card(MISSION, "t001")
        owner = meta["worker"]
        meta.update(status="pending", worker=None, started_at=None)
        t.write_card(MISSION, "t001", meta, b)                    # 旧所有者が card から消える
        t.retire_assignment(owner, MISSION, "t001", None)
        t.record(MISSION, "t001", "in_progress", "pending", GEN)


def run_add(queue):
    with store.transaction(queue, op="add", actor="director") as t:
        mission = t.load_mission(MISSION)
        n = int(mission["next_task_id"])
        tid = f"t{n:03d}"
        t.write_card(MISSION, tid, card(tid), body())
        mission["next_task_id"] = n + 1
        t.write_mission(MISSION, mission)
        t.record(MISSION, tid, None, "pending")


def run_archive(queue):
    with store.transaction(queue, op="archive", actor="director") as t:
        state = t.load_state()
        src = os.path.join(queue, "missions", MISSION)
        dst = os.path.join(queue, "archive", MISSION)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        store._fault("scenario:before_move", src)
        os.rename(src, dst)
        store._fault("scenario:after_move", dst)
        state["active_missions"] = [m for m in state["active_missions"] if m != MISSION]
        state["default_mission"] = None
        t.write_state(state)
        t.record(MISSION, None, None, None)


SCENARIOS = {
    "pull": run_pull, "done": run_done, "fail": run_fail,
    "reset": run_reset, "add": run_add, "archive": run_archive,
}

#: 落ちた後、次のロック取得で回復が見る範囲 (設計 §2.5)。
SCOPES = {
    "pull": store.Scope(cards=((MISSION, "t001"),), agents=(AGENT,)),
    "done": store.Scope(cards=((MISSION, "t001"),), agents=(AGENT,)),
    "fail": store.Scope(cards=((MISSION, "t001"),), agents=(AGENT,)),
    "reset": store.Scope(cards=((MISSION, "t001"),), agents=(AGENT,)),
    "add": store.Scope(add_missions=(MISSION,)),
    "archive": store.Scope(archive_slugs=(MISSION,)),
}


# ---------------------------------------------------------------------------
# 子プロセスで走らせて落とす
# ---------------------------------------------------------------------------

def points_of(queue, scenario):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 1
        try:
            points = []
            store.FAULT_HOOK = lambda p, path: points.append(p)
            SCENARIOS[scenario](queue)
            os.write(w, json.dumps(points).encode())
            code = 0
        finally:
            os._exit(code)
    os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 65536)
        if not chunk:
            break
        data += chunk
    os.close(r)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, "場面が最後まで走らなかった"
    return json.loads(data)


def fork_and_crash(queue, scenario, k):
    """`FAULT_HOOK` の k 番目 (0 始まり) の呼び出しで SIGKILL される子を走らせる。
    子が SIGKILL で死んだ (= 本当にその点で落ちた) ことを確かめて返す。"""
    pid = os.fork()
    if pid == 0:
        try:
            count = [0]

            def hook(point, path):
                if count[0] == k:
                    os.kill(os.getpid(), signal.SIGKILL)
                count[0] += 1
            store.FAULT_HOOK = hook
            SCENARIOS[scenario](queue)
        finally:
            os._exit(0)          # 落ちなかった (k が点の数以上)
    _, status = os.waitpid(pid, 0)
    return os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL


# ---------------------------------------------------------------------------
# 収束の検査
# ---------------------------------------------------------------------------

def read_meta(queue, slug, tid):
    text = cards.read_regular_text_or_unreadable(os.path.join(queue, "missions", slug, "tasks", f"{tid}.md"))
    assert not cards.is_unreadable(text), text
    meta, _b = cards.parse_frontmatter(text)
    return meta


def slot_text(queue, agent):
    p = pathlib.Path(queue) / "assignments" / agent
    return p.read_text().strip() if p.exists() else None


def identity(queue, agent):
    p = pathlib.Path(queue) / "assignments" / f"{agent}.identity"
    return json.loads(p.read_text()) if p.exists() else None


def assert_converged(queue, scenario):
    """回復の後: 壊れたファイル・正本と projection の食い違いが残らない。"""
    queue = pathlib.Path(queue)
    # 壊れたファイルが無い: 残っている card / mission / state はすべて読めて parse できる
    for path in queue.rglob("*.md"):
        cards.parse_frontmatter(path.read_text())
    for path in list(queue.rglob("mission.yaml")) + [queue / "state.yaml"]:
        cards.parse_yaml(path.read_text())
    for path in list((queue / "assignments").glob("*.identity")):
        json.loads(path.read_text())
    # 食い違いが無い: diagnose は残骸だけ出す。stale_tmp (kill された書き手の tmp) と
    # orphan_identity (retire が本体を消した後・identity を消す前で落ちた) は設計が「無害・
    # store-check が件数を出す」と決めた残骸 (§2.6 reap-orphan-assignment 行)。本体が無い
    # identity は classify が本体を先に読むので判定に使われず、次の publish が上書きする。
    findings = [f for f in store.diagnose(queue, store.Scope.everything(queue))
                if f.kind not in ("stale_tmp", "orphan_identity")]
    assert findings == [], findings
    for f in store.diagnose(queue, store.Scope.everything(queue)):
        if f.kind == "orphan_identity":
            assert not (queue / "assignments" / f.agent).exists()

    if scenario in ("pull",):
        meta = read_meta(queue, MISSION, "t001")
        if meta["status"] == "in_progress":
            assert slot_text(queue, AGENT) == f"{MISSION}:t001"
            assert identity(queue, AGENT)["started_at"] == meta["started_at"] == NEW_GEN
        else:
            assert meta["status"] == "pending" and slot_text(queue, AGENT) is None
    elif scenario in ("done", "fail", "reset"):
        meta = read_meta(queue, MISSION, "t001")
        if meta["status"] == "in_progress":
            assert slot_text(queue, AGENT) == f"{MISSION}:t001"
        else:
            assert slot_text(queue, AGENT) is None       # identity の残骸は上で許容済み (本体が無ければ無害)
        if scenario == "done" and meta["status"] == "done":
            dep = read_meta(queue, MISSION, "t002")
            assert meta["pr_number"] == dep["pr_number"] == PR      # 派生値は正本より前に書かれている
    elif scenario == "add":
        mission = cards.parse_yaml((queue / "missions" / MISSION / "mission.yaml").read_text())
        nums = [int(p.stem[1:]) for p in (queue / "missions" / MISSION / "tasks").glob("t*.md")]
        assert int(mission["next_task_id"]) > max(nums)
    elif scenario == "archive":
        state = cards.parse_yaml((queue / "state.yaml").read_text())
        if not (queue / "missions" / MISSION).exists():
            assert MISSION not in (state.get("active_missions") or [])
            assert (queue / "archive" / MISSION).is_dir()
