"""plugin が **ファイル全体を拒否する** 生成物を作らないこと。

t002 の QA が plugin を実走させて見つけた欠陥 (F-1a / F-1b / F-1c / F-2a) を
固定する。ここで守りたいのは 1 つだけ:

    crewvia 側の普通の状態が、plugin 側の「読めない」に化けないこと。

plugin の `load_config` は 3 つの理由でファイル *全体* を捨てる — 空の tasks、
循環依存、解決できない depends_on。**どれも 1 つの mission の事情で起きるのに、
巻き添えになるのは全 mission の DAG である。** 特に空は、最後の mission を
archive した直後という「ミッションとミッションの間の普通の状態」で必ず通る。

このファイルは 2 段で見る。

1. **plugin の契約を写した検証** (`_reject_reason`) — plugin が無い環境でも
   走る。plugin の `load_config` と同じ 3 つの拒否理由をそのまま書いてある。
2. **本物の plugin に読ませる** — `--demo --once` で実際に 1 フレーム描かせる。
   plugin が手元にある環境でだけ走る (`CREWVIA_TASK_GRAPH_PLUGIN` か
   `/tmp/herdr-task-graph/task_graph.py`)。`--demo` は socket を一切触らない
   ので、本番の herdr に接続する余地がない。

1 だけだと「写しが正しい」ことを誰も見ていないので、2 が要る。2 だけだと
plugin の無い CI で何も守られないので、1 が要る。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from test_task_graph import (  # noqa: F401  (sandbox は fixture として使う)
    MISSION,
    REPO_ROOT,
    _by_id,
    sandbox,
)

# ---------------------------------------------------------------------------
# plugin の在り処
# ---------------------------------------------------------------------------

PLUGIN = pathlib.Path(
    os.environ.get("CREWVIA_TASK_GRAPH_PLUGIN", "/tmp/herdr-task-graph/task_graph.py")
)

requires_plugin = pytest.mark.skipif(
    not PLUGIN.exists(),
    reason=(
        f"herdr-task-graph plugin が {PLUGIN} に無い "
        "(CREWVIA_TASK_GRAPH_PLUGIN で場所を指定できる)"
    ),
)


def run_plugin(config: pathlib.Path) -> subprocess.CompletedProcess:
    """本物の plugin に読ませて 1 フレーム描かせる。

    `--demo` は socket を探しにいかないので、本番の herdr に触れる経路が無い。
    """
    return subprocess.run(
        [sys.executable, str(PLUGIN), "--config", str(config),
         "--demo", "--once", "--width", "150", "--height", "30"],
        capture_output=True, text=True, timeout=60,
    )


# ---------------------------------------------------------------------------
# plugin の契約の写し — load_config が投げる 3 つの理由
# ---------------------------------------------------------------------------

def _reject_reason(graph: dict) -> str | None:
    """plugin の load_config がこのファイルを拒否する理由。通れば None。"""
    tasks = graph.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return "tasks must be a non-empty array"

    ids: set[str] = set()
    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            return "every task needs a non-empty string id"
        if task_id in ids:
            return f"duplicate task id: {task_id}"
        ids.add(task_id)
        if not isinstance(task.get("depends_on", []), list):
            return f"depends_on must be an array: {task_id}"

    for task in tasks:
        for dep in task.get("depends_on", []):
            if dep not in ids:
                return f"{task['id']} depends on missing task {dep}"

    # 循環 (Kahn) — plugin の topological_levels と同じ判定。
    indegree = {t["id"]: len(t.get("depends_on", [])) for t in tasks}
    children: dict[str, list[str]] = {t["id"]: [] for t in tasks}
    for task in tasks:
        for dep in task.get("depends_on", []):
            children[dep].append(task["id"])
    current = [tid for tid, n in indegree.items() if n == 0]
    visited = 0
    while current:
        nxt = []
        for tid in current:
            visited += 1
            for child in children[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    nxt.append(child)
        current = nxt
    if visited != len(tasks):
        return "dependency graph contains a cycle"
    return None


def assert_plugin_accepts(sandbox, graph_path=None):
    """生成物が plugin に受け入れられることを、写しと本物の両方で言う。"""
    path = pathlib.Path(graph_path or sandbox.graph)
    graph = json.loads(path.read_text())

    reason = _reject_reason(graph)
    assert reason is None, (
        f"plugin がファイル全体を拒否する ({reason}) — "
        f"1 つの mission の事情で全 mission の DAG が表示不能になる\n"
        f"{json.dumps(graph, ensure_ascii=False)[:600]}"
    )

    if PLUGIN.exists():
        r = run_plugin(path)
        assert r.returncode == 0, (
            f"本物の plugin が拒否した (rc={r.returncode}): {r.stderr.strip()[:400]}"
        )
        assert "Traceback" not in r.stderr, r.stderr[:400]
    return graph


# ---------------------------------------------------------------------------
# F-1b — task が 0 件
# ---------------------------------------------------------------------------

def test_no_active_missions_still_produces_a_readable_file(sandbox):
    """active mission が 0 件でも plugin が読めること。

    最後の mission を archive した直後の状態。**ミッションとミッションの間の
    普通の状態** なので、ここで壊れるのは許容できない。
    """
    (sandbox.queue / "state.yaml").write_text(
        "active_missions: []\ndefault_mission: null\n"
    )
    assert sandbox.run("task-graph").returncode == 0

    graph = assert_plugin_accepts(sandbox)
    assert len(graph["tasks"]) == 1
    node = graph["tasks"][0]
    assert node["status"] == "blocked", (
        "空のプレースホルダが ready / done に見えている — "
        "存在しない task が実行可能、あるいは完了済みに読める"
    )
    assert "active mission がありません" in node["title"]


def test_active_mission_with_zero_tasks_still_produces_a_readable_file(sandbox):
    """mission はあるが task がまだ 1 件も無い状態 (init 直後)。"""
    assert sandbox.run("task-graph").returncode == 0

    graph = assert_plugin_accepts(sandbox)
    assert len(graph["tasks"]) == 1
    assert graph["tasks"][0]["status"] == "blocked"
    assert MISSION in graph["tasks"][0]["title"], (
        f"どの mission が空なのか分からない: {graph['tasks'][0]['title']!r}"
    )


def test_the_placeholder_disappears_as_soon_as_a_task_exists(sandbox):
    """task が 1 件でもあればプレースホルダは出ないこと。

    出続けると、実在しない node が DAG に居座る。
    """
    assert sandbox.run("task-graph").returncode == 0
    assert len(sandbox.read_graph()["tasks"]) == 1

    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0
    ids = list(_by_id(sandbox.read_graph()))
    assert ids == [f"{MISSION}:t001"], ids


def test_an_emptied_queue_is_not_left_showing_the_old_graph(sandbox):
    """mission が消えたあとに、古い DAG が現在の姿として残らないこと。

    「空なら書かない / 前回の内容を残す」を選ぶとここが赤くなる。グラフは
    「今どうなっているか」を見るためのものなので、もう存在しない mission を
    現在として見せるのは、何も見せないより悪い。
    """
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    assert sandbox.run("task-graph").returncode == 0
    assert f"{MISSION}:t001" in _by_id(sandbox.read_graph())

    (sandbox.queue / "state.yaml").write_text(
        "active_missions: []\ndefault_mission: null\n"
    )
    assert sandbox.run("task-graph").returncode == 0

    ids = list(_by_id(sandbox.read_graph()))
    assert f"{MISSION}:t001" not in ids, (
        f"archive 済みの mission が現在の DAG に残っている: {ids}"
    )


# ---------------------------------------------------------------------------
# F-1a — 依存の循環
# ---------------------------------------------------------------------------

def test_a_cycle_does_not_take_the_whole_file_down(sandbox):
    """循環があっても plugin がファイル全体を拒否しないこと。"""
    sandbox.add_task("t001", "pending", ["t002"])
    sandbox.add_task("t002", "pending", ["t001"])
    assert sandbox.run("task-graph").returncode == 0

    graph = assert_plugin_accepts(sandbox)
    ids = {t["id"] for t in graph["tasks"]}
    assert ids == {f"{MISSION}:t001", f"{MISSION}:t002"}, (
        f"循環している task が消えている: {ids}"
    )


def test_a_self_dependency_does_not_take_the_whole_file_down(sandbox):
    """自己依存 (t001 → t001) も同じ経路で隔離されること。"""
    sandbox.add_task("t001", "pending", ["t001"])
    assert sandbox.run("task-graph").returncode == 0

    graph = assert_plugin_accepts(sandbox)
    node = _by_id(graph)[f"{MISSION}:t001"]
    assert node["depends_on"] == []
    assert "循環依存" in node["title"], node["title"]


def test_the_cut_edge_is_named_not_silently_dropped(sandbox):
    """落とした辺が title に残ること。黙って消すと依存が無いように見える。"""
    sandbox.add_task("t001", "pending", ["t002"])
    sandbox.add_task("t002", "pending", ["t001"])
    assert sandbox.run("task-graph").returncode == 0

    nodes = _by_id(sandbox.read_graph())
    marked = [n for n in nodes.values() if "循環依存" in n["title"]]
    assert marked, f"循環の印がどこにも無い: {[n['title'] for n in nodes.values()]}"
    for node in marked:
        assert f"{MISSION}:t00" in node["title"], (
            f"どの辺を落としたのか書かれていない: {node['title']!r}"
        )


def test_a_cycle_leaves_the_task_unrunnable_looking(sandbox):
    """循環している task が READY に見えないこと。

    辺を落としたせいで「依存なし = 実行可能」に化けると、DAG は *pull できない
    task* を次に動かせるものとして指すことになる。
    """
    sandbox.add_task("t001", "pending", ["t002"])
    sandbox.add_task("t002", "pending", ["t001"])
    assert sandbox.run("task-graph").returncode == 0

    nodes = _by_id(sandbox.read_graph())
    for task_id in ("t001", "t002"):
        node = nodes[f"{MISSION}:{task_id}"]
        assert node["status"] == "waiting", (
            f"{task_id} が {node['status']} に見えている — 実際には pull できない"
        )

    # 対照: 実際に pull が拒否することを突き合わせる
    r = sandbox.run("pull", "--agent", "Ren", "--skills", "code")
    assert '"id": "t001"' not in r.stdout and '"id": "t002"' not in r.stdout, r.stdout


def test_one_broken_mission_does_not_hide_the_others(sandbox):
    """循環のある mission 以外は今までどおり全部見えること。

    plugin 側の拒否は「ファイル全体」なので、ここを落とすと無関係な mission の
    DAG まで巻き添えで消える。
    """
    sandbox.add_task("t001", "pending", ["t002"])
    sandbox.add_task("t002", "pending", ["t001"])
    sandbox.add_mission("m-healthy")
    sandbox.add_task("t001", "done", [], mission="m-healthy")
    sandbox.add_task("t002", "pending", ["t001"], mission="m-healthy")
    assert sandbox.run("task-graph").returncode == 0

    graph = assert_plugin_accepts(sandbox)
    nodes = _by_id(graph)
    assert "m-healthy:t001" in nodes and "m-healthy:t002" in nodes
    # 健全な mission は辺も status もそのまま
    assert nodes["m-healthy:t002"]["depends_on"] == ["m-healthy:t001"]
    assert nodes["m-healthy:t002"]["status"] == "ready"
    assert "循環依存" not in nodes["m-healthy:t002"]["title"]


def test_a_healthy_graph_keeps_every_edge(sandbox):
    """循環が無いとき、辺を 1 本も落とさないこと。

    後退辺の判定が雑だと、健全な菱形 (t002 と t003 が t001 に依存し、t004 が
    両方に依存する) で辺が消える。消えた辺は DAG の上では「もう待っていない」
    と読めるので、静かに嘘になる。
    """
    sandbox.add_task("t001", "done", [])
    sandbox.add_task("t002", "pending", ["t001"])
    sandbox.add_task("t003", "pending", ["t001"])
    sandbox.add_task("t004", "pending", ["t002", "t003"])
    assert sandbox.run("task-graph").returncode == 0

    nodes = _by_id(assert_plugin_accepts(sandbox))
    assert nodes[f"{MISSION}:t002"]["depends_on"] == [f"{MISSION}:t001"]
    assert nodes[f"{MISSION}:t003"]["depends_on"] == [f"{MISSION}:t001"]
    assert nodes[f"{MISSION}:t004"]["depends_on"] == [
        f"{MISSION}:t002", f"{MISSION}:t003",
    ]
    assert not [n for n in nodes.values() if "循環依存" in n["title"]]


def test_only_the_cycle_edge_is_cut_not_the_whole_dependency_list(sandbox):
    """循環に関わらない辺は、循環している task のものでも残ること。"""
    sandbox.add_task("t001", "done", [])
    sandbox.add_task("t002", "pending", ["t001", "t003"])
    sandbox.add_task("t003", "pending", ["t002"])
    assert sandbox.run("task-graph").returncode == 0

    nodes = _by_id(assert_plugin_accepts(sandbox))
    remaining = set(nodes[f"{MISSION}:t002"]["depends_on"]) | set(
        nodes[f"{MISSION}:t003"]["depends_on"]
    )
    assert f"{MISSION}:t001" in remaining, (
        f"循環と無関係な t001 への依存まで落ちている: {remaining}"
    )


# ---------------------------------------------------------------------------
# F-1c — ペインを持たない実行者
# ---------------------------------------------------------------------------

def test_a_paneless_runner_gets_no_pane_match(sandbox):
    """codex-review は detached subprocess で走る = ペインが無い。

    `pane_match` は「この task が今どのペインに居るか」を教える欄なので、
    ペインが存在しない実行者について名前を書くのは事実として誤り。
    """
    sandbox.add_task("t001", "in_progress", [], worker="Kai-codex")
    # assignment は公開されている (kai-review.sh も plan.sh pull を通る)。
    # 置かないと「assignment が無いから出ない」で緑になり、ペイン不在の規則
    # そのものを何も見ていないテストになる。
    sandbox.assign("Kai-codex", "t001")
    path = sandbox.queue / "missions" / MISSION / "tasks" / "t001.md"
    path.write_text(path.read_text().replace("skills: [code]", "skills: [codex-review]"))
    assert sandbox.run("task-graph").returncode == 0

    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert "pane_match" not in node, (
        f"ペインを持たない実行者に pane_match が付いている: {node.get('pane_match')!r}"
    )


def test_a_normal_worker_still_gets_a_pane_match(sandbox):
    """対照: 普通の Worker には今までどおり付くこと。"""
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    sandbox.assign("Ren", "t001")
    assert sandbox.run("task-graph").returncode == 0
    assert _by_id(sandbox.read_graph())[f"{MISSION}:t001"]["pane_match"] == "Ren-worker"


# ---------------------------------------------------------------------------
# F-2a — 対応表の網羅
# ---------------------------------------------------------------------------

def test_cancelled_does_not_contradict_the_dependency_rule(sandbox):
    """`cancelled` の見え方が、下流の READY と矛盾しないこと。

    `cancelled` は DEAD_DEP_STATUSES 側 = 依存として「満たされた」扱いなので、
    下流は READY になる。上流を `blocked` と表示すると、**止まっている依存の
    下流が動ける** という読めない画面になる。
    """
    sandbox.add_task("t001", "cancelled", [])
    sandbox.add_task("t002", "pending", ["t001"])
    assert sandbox.run("task-graph").returncode == 0

    nodes = _by_id(assert_plugin_accepts(sandbox))
    assert nodes[f"{MISSION}:t002"]["status"] == "ready"
    assert nodes[f"{MISSION}:t001"]["status"] == "failed", (
        "cancelled が終端に見えていない — 下流の READY と食い違う"
    )
    assert "[中止]" in nodes[f"{MISSION}:t001"]["title"], (
        "cancelled が failed と区別できない"
    )

    # 対照: pull が実際に t002 を割り当てる
    r = sandbox.run("pull", "--agent", "Ren", "--skills", "code")
    assert '"id": "t002"' in r.stdout, r.stdout


def test_every_status_the_linter_accepts_is_in_the_mapping_table():
    """lint が許す status が、対応表から漏れていないこと。

    表に無い status は `[status不明]` + `blocked` に落ちる。crewvia が普通に
    書く status がそこに落ちるのは、まさに F-2a で起きたこと。
    """
    import ast
    import re

    plan_src = (REPO_ROOT / "scripts" / "plan.sh").read_text()
    m = re.search(
        r"^TASK_GRAPH_STATUS_MAP = \{(.*?)^\}$", plan_src, re.DOTALL | re.MULTILINE
    )
    assert m, "TASK_GRAPH_STATUS_MAP が plan.sh に無い"
    mapped = set(re.findall(r"^\s*'([a-z_]+)':", m.group(1), re.MULTILINE))

    lint_src = (REPO_ROOT / "scripts" / "lint_plan.py").read_text()
    m = re.search(r"^VALID_STATUSES = (\{.*?\})$", lint_src, re.DOTALL | re.MULTILINE)
    assert m, "VALID_STATUSES が lint_plan.py に無い"
    valid = ast.literal_eval(m.group(1))

    # `pending` は依存の状態で ready / waiting に分かれるので表には載らない。
    missing = valid - mapped - {"pending"}
    assert not missing, (
        f"lint が許すのに対応表に無い status: {sorted(missing)} — "
        f"[status不明] + blocked に落ちる"
    )


# ---------------------------------------------------------------------------
# 本物の plugin に読ませる — 全 status を一度に
# ---------------------------------------------------------------------------

@requires_plugin
def test_the_real_plugin_renders_a_graph_with_every_status(sandbox):
    """対応表の全行 + 循環 + dangling を 1 ファイルに入れて実際に描かせる。"""
    from test_task_graph import STATUS_ROWS

    for row in STATUS_ROWS:
        sandbox.add_task(row[0], row[1], row[2])
    sandbox.add_task("t090", "pending", ["t091"])   # 循環
    sandbox.add_task("t091", "pending", ["t090"])
    sandbox.add_task("t092", "pending", ["t404"])   # dangling
    assert sandbox.run("task-graph").returncode == 0

    assert_plugin_accepts(sandbox)
    r = run_plugin(sandbox.graph)
    assert "TASK GRAPH" in r.stdout, r.stdout[:400]
