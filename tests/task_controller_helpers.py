#!/usr/bin/env python3
"""tests/task_controller_helpers.py — Task Controller (vNext 01c E1) のテスト共通の道具。

seed (lib 自身で原子的に作る)・ID 生成器・crash 注入 (`FAULT_HOOK` の k 番目で fork した子が自分に SIGKILL)・
状態の読み出し・収束の検査。plan.sh は使わない (E1 は呼び出し元ゼロ。lib の API だけで場面を作る)。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import signal
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lib_execution as ex  # noqa: E402
import lib_state_store as store  # noqa: E402
import lib_task_cards as cards  # noqa: E402
import lib_task_controller as ctl  # noqa: E402

MISSION = "m-exec"
AGENT = "Hana"
OTHER = "Ren"
T0 = "2026-10-01T10:00:00.000000Z"
T1 = "2026-10-01T11:00:00.000000Z"
T2 = "2026-10-01T12:00:00.000000Z"
SECRET = "SENTINEL_SECRET_do_not_leak"


def xid(n):
    """決定的な ID (`ex-` + 32 桁 16 進)。"""
    return "ex-%032x" % n


def ids(start=1):
    """ID 生成器の注入 (`id_factory`)。呼ぶたびに xid(start), xid(start+1) …"""
    counter = iter(range(start, 10_000))
    return lambda: xid(next(counter))


def card(tid, status="pending", worker=None, started_at=None, **extra):
    meta = {
        "id": tid, "title": f"task {tid}", "skills": ["code"], "priority": "high",
        "status": status, "blocked_by": [], "target_dir": None, "worker": worker,
        "started_at": started_at, "completed_at": None,
    }
    meta.update(extra)
    return meta


def body(desc="do it", result=""):
    return f"## Description\n{desc}\n\n## Result\n{result}\n"


def seed(queue, cards_=None, *, tids=("t001",)):
    """mission 1 つと card を lib で作る。`cards_` は `{tid: (meta, body)}`。無ければ `tids` を pending で。"""
    queue = pathlib.Path(queue)
    queue.mkdir(parents=True, exist_ok=True)
    with store.transaction(queue, op="seed", actor="test") as t:
        t.write_state({"active_missions": [MISSION], "default_mission": MISSION})
        t.write_mission(MISSION, {"title": "m", "slug": MISSION, "status": "in_progress",
                                  "created_at": "2026-10-01T00:00:00Z", "completed_at": None,
                                  "next_task_id": 9})
        for tid, (meta, b) in (cards_ or {t_: (card(t_), body()) for t_ in tids}).items():
            t.write_card(MISSION, tid, meta, b)
    return queue


def tx(queue, op="test", actor=AGENT, recover=None):
    """`store.transaction` の短縮。`recover` (Scope) を渡すと取得直後に回復を走らせる (plan.sh の `recover_before` 相当)。"""
    class _Ctx:
        def __enter__(self_inner):
            self_inner.cm = store.transaction(queue, op=op, actor=actor)
            self_inner.t = self_inner.cm.__enter__()
            if recover is not None:
                self_inner.t.recover(recover)
            return self_inner.t

        def __exit__(self_inner, *a):
            return self_inner.cm.__exit__(*a)
    return _Ctx()


def reserve(queue, agent=AGENT, tid="t001", now=T0, factory=None, op="pull"):
    with tx(queue, op=op, actor=agent or "x") as t:
        return ctl.reserve_task(t, MISSION, tid, agent, now=now, id_factory=factory)


def reserve_and_start(queue, agent=AGENT, tid="t001", now=T0, factory=None, git=None):
    ctx = reserve(queue, agent, tid, now, factory)
    with tx(queue, op="pull2", actor=agent) as t:
        ctl.start_execution(t, MISSION, tid, ctx.execution_id, git)
    return ctx


def caller(execution_id, source="flag"):
    return ctl.Caller(execution_id, source)


# ---------------------------------------------------------------------------
# 読み出し
# ---------------------------------------------------------------------------

def read_meta(queue, tid="t001"):
    text = cards.read_regular_text_or_unreadable(os.path.join(queue, "missions", MISSION, "tasks", f"{tid}.md"))
    assert not cards.is_unreadable(text), text
    meta, _b = cards.parse_frontmatter(text)
    return meta


def read_card_text(queue, tid="t001"):
    return pathlib.Path(queue, "missions", MISSION, "tasks", f"{tid}.md").read_text()


def read_record(queue, execution_id):
    p = pathlib.Path(queue, "missions", MISSION, "executions", f"{execution_id}.json")
    return json.loads(p.read_text()) if p.exists() else None


def record_files(queue):
    d = pathlib.Path(queue, "missions", MISSION, "executions")
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def slot_text(queue, agent=AGENT):
    p = pathlib.Path(queue, "assignments", agent)
    return p.read_text().strip() if p.exists() else None


def identity(queue, agent=AGENT):
    p = pathlib.Path(queue, "assignments", f"{agent}.identity")
    return json.loads(p.read_text()) if p.exists() else None


def audit_rows(queue):
    out = []
    for p in sorted(pathlib.Path(queue, "audit").glob("transitions-*.jsonl")) if pathlib.Path(queue, "audit").exists() else []:
        out += [json.loads(line) for line in p.read_text().splitlines()]
    return out


def snapshot(queue):
    """queue の全ファイル (audit/ と .lock を除く) の {相対パス: sha256}。「1 バイトも書かない」の検査。"""
    root = pathlib.Path(queue)
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not rel.startswith("audit/") and rel != ".lock":
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def diagnose_kinds(queue, ignore=("stale_tmp", "orphan_identity")):
    return [f for f in store.diagnose(queue, store.Scope.everything(queue)) if f.kind not in ignore]


def scope(tid="t001", agent=AGENT):
    return store.Scope(cards=((MISSION, tid),), agents=(agent,) if agent else ())


def raises_code(code, fn, *a, **kw):
    """`fn` が `ControllerError(code)` を投げることを確かめ、例外を返す。"""
    try:
        fn(*a, **kw)
    except ex.ControllerError as e:
        assert e.code == code, (e.code, code, str(e))
        return e
    raise AssertionError(f"ControllerError({code}) が投げられなかった")


# ---------------------------------------------------------------------------
# crash 注入 (lib の書き込みの各段で `FAULT_HOOK` が呼ばれる。k 番目で子が SIGKILL)
# ---------------------------------------------------------------------------

def points_of(queue, fn):
    """`fn(queue)` を子で最後まで走らせ、通った `FAULT_HOOK` の点の一覧を返す。"""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 1
        try:
            points = []
            store.FAULT_HOOK = lambda p, path: points.append(p)
            fn(queue)
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


def fork_and_crash(queue, fn, k):
    """k 番目 (0 始まり) の点で SIGKILL される子で `fn(queue)` を走らせる。本当に SIGKILL で死んだかを返す。"""
    pid = os.fork()
    if pid == 0:
        try:
            count = [0]

            def hook(point, path):
                if count[0] == k:
                    os.kill(os.getpid(), signal.SIGKILL)
                count[0] += 1
            store.FAULT_HOOK = hook
            fn(queue)
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    return os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL


def card_kinds_converged(queue, tids=("t001",)):
    """回復の後: 試行の欄を持つ card ごとに record が card に追従している (R-5 の収束)・identity は ACTIVE のときだけ ID を持つ。"""
    for tid in tids:
        meta = read_meta(queue, tid)
        if not ex.has_execution_fields(meta):
            continue
        assert ex.fields_problem(meta) is None, meta
        rec = read_record(queue, meta["current_execution_id"])
        assert rec is not None, f"{tid}: current の record が無い"
        assert ex.record_follows_card(rec, meta), (rec, meta)
        assert ex.record_problem(rec, meta, MISSION, tid) is None


def old_code_reset(queue, tid="t001", agent=AGENT):
    """旧コード (d887acf) の `update --reset` が card と枠にすることだけを写す: 試行の欄は触らない・status を pending・
    worker / started_at を null・枠を撤去。(旧コードの plan.sh を実際に打つ版は QA t005 の担当。ここは lib の単体用)"""
    import re
    p = pathlib.Path(queue, "missions", MISSION, "tasks", f"{tid}.md")
    text = p.read_text()
    text = re.sub(r"^status: .*$", "status: pending", text, flags=re.M)
    text = re.sub(r"^worker: .*$", "worker: null", text, flags=re.M)
    text = re.sub(r"^started_at: .*$", "started_at: null", text, flags=re.M)
    p.write_text(text)
    for name in (agent, agent + ".identity"):
        f = pathlib.Path(queue, "assignments", name)
        if f.exists():
            f.unlink()


def set_status(queue, status, tid="t001", extra=""):
    """card の status 行だけ書き換える (Director の `update --status X` / 旧コードの遷移の写し)。"""
    import re
    p = pathlib.Path(queue, "missions", MISSION, "tasks", f"{tid}.md")
    p.write_text(re.sub(r"^status: .*$", f"status: {status}{extra}", p.read_text(), flags=re.M))
