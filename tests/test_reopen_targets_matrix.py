"""reopen の「経路を閉じる終点 (targets)」を総当たりで固める (vNext 02 PR-F fix 4 / t054。PR #308 Codex 4 巡目)。

Controller の API だけで回す速い層 (plan.sh は打たない)。場面は 小さなグラフ × 各ノードの状態 (`lib_task_status` の語彙の全部 +
「まだ一度も走っていない pending」) × 各ノードの skills (review 系 / それ以外) × `--cascade` 3 値。1 つの不変条件で見る:

* 拒否 (`ControllerError`) なら queue は 1 バイトも変わらない
* 成功し、`cascade` が none 以外なら、**「I の下流で、cascade が選ぶ task (status を問わない) と、reopen の前に実行中だった task」への
  経路上の task は、どれも終端 (done / verified / skipped) のまま残らない**。かつ、それらの pending は I の再完了より前に pull
  できない (`lib_dep_rules.unmet_dependencies` が空でない)。期待する集合は本番の `_reopen_targets` を使わず、このファイルで
  グラフから独立に導く
* `--cascade none` で成功したら I の card だけが変わる

加えて、孤児の読めない card がある mission の reopen は `CASCADE_UNREADABLE_CARDS` で断る (何も書かない)・途中で止まった reopen を
再実行しても同じ不変条件が成り立つ (前の実行で戻した下流が経路の終点に数えられる)。
"""

from __future__ import annotations

import itertools
import os
import pathlib
import re
import shutil
import tempfile

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, T0, T1, T2, ex, store, ctl

import lib_dep_rules as dep  # noqa: E402
import lib_task_cards as cards_lib  # noqa: E402
import lib_task_status as status_vocab  # noqa: E402

REASON = "I の結果を見直す"
TERMINAL = status_vocab.TERMINAL_STATUSES

#: 状態 = (形, status)。形: done = 終わった試行の card / running = 実行中の試行 (record・枠つき) / fresh = 一度も走っていない pending。
DONE_FORM = ("done", "verified", "skipped", "pending", "failed", "needs_director", "blocked", "verification_failed")
RUNNING_FORM = ("in_progress", "ready_for_verification", "verifying", "needs_human_review")
FRESH_FORM = ("pending", "blocked")
STATES = ([("done", s) for s in DONE_FORM] + [("running", s) for s in RUNNING_FORM] + [("fresh", s) for s in FRESH_FORM])

#: I = t001。他は名前順に t002 ... (blocked_by は I を根にした DAG)
GRAPHS = {
    "chain": {"t002": ["t001"], "t003": ["t002"]},                                  # I → A → R
    "long": {"t002": ["t001"], "t003": ["t002"], "t004": ["t003"]},                 # I → A → B → R
    "diamond": {"t002": ["t001"], "t003": ["t001"], "t004": ["t002", "t003"]},      # I → {A, B} → R
}
SKILLS = {"review": ["review"], "code": ["code"]}
CASCADES = ("none", "reviews", "all")


def test_the_state_table_covers_the_whole_status_vocabulary():
    assert {s for _form, s in STATES} == set(status_vocab.TASK_STATUSES)


# ---------------------------------------------------------------------------
# 場面の部品 (palette: グラフ × skills ごとに 1 回だけ本物の Controller で作り、場面ごとに必要な card だけ書き写す)
# ---------------------------------------------------------------------------

class Palette:
    """`graph` (I 以外の `{tid: blocked_by}`) と `skills` (`{tid: [skill]}`) の、各 task の 3 つの形のファイル一式。"""

    def __init__(self, root, graph, skills):
        self.graph = {"t001": [], **graph}
        self.skills = {"t001": ["code"], **skills}
        self.tids = sorted(self.graph)
        self.files = {}                 # (形, tid) -> {相対パス: bytes}
        self.shared = {}                # mission.yaml / state.yaml
        for form in ("done", "running", "fresh"):
            self._build(pathlib.Path(root) / form, form)

    def _build(self, queue, form):
        nodes = {t: (h.card(t, blocked_by=list(self.graph[t]), skills=list(self.skills[t])), h.body()) for t in self.tids}
        h.seed(queue, nodes)
        if form != "fresh":
            factory = h.ids(100 if form == "done" else 5000)      # 形ごとに ID が重ならない
            for t in self.tids:
                agent = f"Ag{t}"
                h.reserve_and_start(queue, agent=agent, tid=t, now=T0, factory=factory)
                if form == "done":
                    xid = h.read_meta(queue, t)["current_execution_id"]
                    with h.tx(queue, op="done", actor=agent) as txn:
                        ctl.complete_execution(txn, MISSION, t, h.caller(xid), to_status="done",
                                               meta_updates={"completed_at": "2026-10-16T00:00:00Z"},
                                               body=h.body(result="r"), now=T1, result_replaced=True)
        base = pathlib.Path(queue)
        for t in self.tids:
            mine = {}
            card = base / "missions" / MISSION / "tasks" / f"{t}.md"
            mine[card.relative_to(base).as_posix()] = card.read_bytes()
            meta = h.read_meta(queue, t)
            xid = meta.get("current_execution_id")
            if xid:
                for p in (base / "missions" / MISSION / "executions" / f"{xid}.json",):
                    mine[p.relative_to(base).as_posix()] = p.read_bytes()
                for p in (base / "missions" / MISSION / "history" / t).glob("*") if (base / "missions" / MISSION / "history" / t).is_dir() else ():
                    mine[p.relative_to(base).as_posix()] = p.read_bytes()
                for name in (f"Ag{t}", f"Ag{t}.identity"):
                    p = base / "assignments" / name
                    if p.exists():
                        mine[p.relative_to(base).as_posix()] = p.read_bytes()
            self.files[(form, t)] = mine
        for rel in ("state.yaml", f"missions/{MISSION}/mission.yaml"):
            self.shared[rel] = (base / rel).read_bytes()

    def materialize(self, queue, assignment):
        """`assignment` = {tid: (形, status)}。I は done の形で固定 (reopen の対象)。"""
        queue = pathlib.Path(queue)
        if queue.exists():
            shutil.rmtree(queue)
        expected = dict(self.shared)
        for t in self.tids:
            form, status = assignment[t]
            for rel, data in self.files[(form, t)].items():
                if rel.endswith(f"/tasks/{t}.md"):
                    native = {"done": "done", "running": "in_progress", "fresh": "pending"}[form]
                    if status != native:
                        data = re.sub(rb"^status: .*$", b"status: " + status.encode(), data, count=1, flags=re.M)
                expected[rel] = data
        for rel, data in expected.items():
            _write(queue / rel, data)
        return expected


def _write(path, data):
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _tree(queue):
    """queue の通常ファイル {相対パス: bytes} (audit/ と .lock を除く。`h.snapshot` と同じ範囲)。"""
    out = {}
    for dirpath, _dirs, files in os.walk(queue):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, queue).replace(os.sep, "/")
            if not rel.startswith("audit/") and rel != ".lock":
                with open(full, "rb") as f:
                    out[rel] = f.read()
    return out


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    """総当たりの時間のため fsync を省く (耐久性はこのテストの対象ではない。書き込みの順序は別のテストが見る)。"""
    monkeypatch.setattr(os, "fsync", lambda fd: None)


# ---------------------------------------------------------------------------
# 期待する集合 (本番の実装を使わず、グラフと場面から導く)
# ---------------------------------------------------------------------------

def descendants(graph, root):
    out, todo = set(), [root]
    while todo:
        n = todo.pop()
        for t, deps in graph.items():
            if n in deps and t not in out:
                out.add(t)
                todo.append(t)
    return out


def expected_targets(graph, skills, assignment, cascade):
    """I の下流のうち、cascade が選ぶ task (status を問わない) と reopen の前に実行中だった task。none は空。"""
    if cascade == "none":
        return set()
    out = set()
    for t in descendants(graph, "t001"):
        selected = cascade == "all" or "review" in skills[t]
        if selected or assignment[t][1] in status_vocab.ASSIGNMENT_HOLDING_STATUSES:
            out.add(t)
    return out


def path_nodes(graph, targets):
    """I から targets のどれかへの依存の経路上にある task (targets 自身を含む)。"""
    down = descendants(graph, "t001")
    up, todo = set(targets), list(targets)
    while todo:
        n = todo.pop()
        for d in graph[n]:
            if d in down and d not in up:
                up.add(d)
                todo.append(d)
    return up & down


def read_cards(queue):
    tasks = cards_lib.list_task_cards(os.path.join(queue, "missions", MISSION, "tasks"))
    return {m["id"]: m for m, _b in tasks}


def run_reopen(queue, graph, cascade, *, unreadable=()):
    with h.tx(queue, op="reopen", actor="Director") as txn:
        return ctl.reopen_task(txn, MISSION, "t001", kind="reopen", reason=REASON, cascade=cascade,
                               graph={t: list(d) for t, d in graph.items()}, unreadable=list(unreadable), now=T2)


def check_invariant(queue, graph, skills, assignment, cascade):
    """成功した reopen の後の不変条件。破れていれば説明の文字列を返す (正しければ None)。"""
    metas = read_cards(queue)
    if metas["t001"]["status"] != "pending":
        return "I が pending に戻っていない"
    if cascade == "none":
        for t in graph:
            if t != "t001" and metas[t]["status"] != assignment[t][1]:
                return f"--cascade none なのに {t} の status が変わった ({assignment[t][1]} → {metas[t]['status']})"
        return None
    targets = expected_targets(graph, skills, assignment, cascade)
    statuses = {t: m["status"] for t, m in metas.items()}
    done_ids = {t for t, s in statuses.items() if s in TERMINAL}
    for t in path_nodes(graph, targets):
        if statuses[t] in TERMINAL:
            return f"{t} が経路上なのに終端 ({statuses[t]}) のまま残った"
    for t in targets:
        if statuses[t] != "pending":
            continue
        unmet = dep.unmet_dependencies(graph[t], done_ids, statuses, metas[t].get("released_deps") or ())
        if not unmet:
            return f"{t} (pending) が I の再完了より前に pull できる"
    return None


# ---------------------------------------------------------------------------
# 総当たり
# ---------------------------------------------------------------------------

def _scenarios(graph_name, cascade):
    graph = {"t001": [], **GRAPHS[graph_name]}
    others = [t for t in sorted(graph) if t != "t001"]
    # skills が効くのは cascade=reviews の選択だけ (all は全部・none は何も選ばない)。他の 2 値で skills を総当たりしても同じ場面を
    # 8 回走らせるだけなので 1 通り (全部 code) にする
    combos = itertools.product(SKILLS, repeat=len(others)) if cascade == "reviews" else [("code",) * len(others)]
    for skill_names in combos:
        skills = {t: SKILLS[n] for t, n in zip(others, skill_names)}
        yield graph, skills, others


@pytest.mark.parametrize("cascade", CASCADES)
@pytest.mark.parametrize("graph_name", sorted(GRAPHS))
def test_reopen_never_leaves_a_stale_path_to_a_target(tmp_path, graph_name, cascade):
    shm = pathlib.Path("/dev/shm")                       # 総当たりは書き込みが多い。tmpfs があれば使う
    scratch = pathlib.Path(tempfile.mkdtemp(dir=shm if shm.is_dir() and os.access(shm, os.W_OK) else tmp_path))
    try:
        _run_matrix(scratch, tmp_path, graph_name, cascade)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_matrix(scratch, tmp_path, graph_name, cascade):
    root = scratch / "scn"
    failures, ok, refused = [], 0, 0
    for n, (graph, skills, others) in enumerate(_scenarios(graph_name, cascade)):
        palette = Palette(scratch / f"palette{n}", graph={t: d for t, d in graph.items() if t != "t001"}, skills=skills)
        for states in itertools.product(STATES, repeat=len(others)):
            assignment = {"t001": ("done", "done"), **dict(zip(others, states))}
            before = palette.materialize(root, assignment)
            try:
                run_reopen(root, graph, cascade)
            except ex.ControllerError as e:
                refused += 1
                if _tree(root) != before:
                    failures.append((skills, assignment, "拒否したのに queue が変わった", e.code))
                continue
            ok += 1
            problem = check_invariant(root, graph, skills, assignment, cascade)
            if problem:
                failures.append((skills, assignment, problem))
    assert not failures, f"{len(failures)} 件の場面で不変条件が破れた。最初の 3 件: {failures[:3]}"
    assert ok and refused, (ok, refused)         # 拒否も成功も場面に含まれる (検査が空振りしていない)


# ---------------------------------------------------------------------------
# 途中で止まった reopen の再実行: 前の実行で戻した下流も経路の終点に数える
# ---------------------------------------------------------------------------

CRASH_SCENARIOS = {
    # B は review でない実行中の task。reset されて pending (台帳の印つき) になるが、skills では選ばれない
    "running-code-leaf": ({"t002": ["t001"], "t003": ["t002"]}, {"t002": ["code"], "t003": ["code"]},
                          {"t002": ("done", "done"), "t003": ("running", "in_progress")}),
    "pending-review-leaf": ({"t002": ["t001"], "t003": ["t002"]}, {"t002": ["code"], "t003": ["review"]},
                            {"t002": ("done", "done"), "t003": ("fresh", "pending")}),
    "diamond-with-running-leaf": ({"t002": ["t001"], "t003": ["t001"], "t004": ["t002", "t003"]},
                                  {"t002": ["code"], "t003": ["code"], "t004": ["code"]},
                                  {"t002": ("done", "done"), "t003": ("done", "done"), "t004": ("running", "in_progress")}),
}


@pytest.mark.parametrize("name", sorted(CRASH_SCENARIOS))
def test_a_rerun_after_a_kill_at_every_write_still_closes_every_path(tmp_path, name):
    graph_spec, skills, states = CRASH_SCENARIOS[name]
    graph = {"t001": [], **graph_spec}
    assignment = {"t001": ("done", "done"), **states}
    palette = Palette(tmp_path / "palette", graph=graph_spec, skills=skills)
    root, saved = tmp_path / "root", tmp_path / "saved"
    palette.materialize(root, assignment)
    shutil.copytree(root, saved)

    def attempt(queue):
        run_reopen(queue, graph, "reviews")

    points = h.points_of(root, attempt)
    assert len(points) >= 8, points                                  # 注入口が生きている (下限)
    window_seen = False
    for k in range(len(points)):
        shutil.rmtree(root)
        shutil.copytree(saved, root)
        assert h.fork_and_crash(root, attempt, k), k
        mid = {t: m["status"] for t, m in read_cards(root).items()}
        # 危険な窓: reset された下流は pending なのに、その手前の完了済みの中間はまだ done
        window_seen = window_seen or (mid["t001"] == "done" and any(
            mid[t] == "pending" and assignment[t][0] != "fresh" for t in states) and any(mid[t] == "done" for t in states))
        if mid["t001"] == "pending":                                 # I の card まで書けていた = 完了済み (再実行は断られる)
            with pytest.raises(ex.ControllerError) as refused:
                run_reopen(root, graph, "reviews")
            assert refused.value.code == ex.NOT_REOPENABLE
        else:
            run_reopen(root, graph, "reviews")                       # 同じコマンドの再実行 (roll forward)
        problem = check_invariant(root, graph, skills, assignment, "reviews")
        assert problem is None, (name, k, problem)
    if name != "pending-review-leaf":
        assert window_seen, "止まった状態で『下流は pending・中間は done』の窓を通っていない (場面が空振り)"
