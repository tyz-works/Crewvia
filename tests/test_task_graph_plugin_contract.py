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
    STATUS_ROWS,
    _by_id,
    sandbox,
)

# ---------------------------------------------------------------------------
# plugin の在り処
# ---------------------------------------------------------------------------

PLUGIN = pathlib.Path(
    os.environ.get("CREWVIA_TASK_GRAPH_PLUGIN", "/tmp/herdr-task-graph/task_graph.py")
)

#: 古い版の plugin (0.1.1 / 0.2.0) の checkout の場所 (`os.pathsep` 区切り)。
#: 本番の `/tmp/herdr-task-graph` は動いている資産なので使わない — 手元の clone から
#: `git worktree add <scratchpad>/... <sha>` した別の木を指す。
OLD_PLUGINS = [
    pathlib.Path(p) / "task_graph.py"
    for p in os.environ.get("CREWVIA_TASK_GRAPH_OLD_PLUGINS", "").split(os.pathsep)
    if p
]

requires_plugin = pytest.mark.skipif(
    not PLUGIN.exists(),
    reason=(
        f"herdr-task-graph plugin が {PLUGIN} に無い "
        "(CREWVIA_TASK_GRAPH_PLUGIN で場所を指定できる)"
    ),
)


def run_plugin(config: pathlib.Path, plugin: pathlib.Path = None) -> subprocess.CompletedProcess:
    """本物の plugin に読ませて 1 フレーム描かせる。

    `--demo` は socket を探しにいかないので、本番の herdr に触れる経路が無い。
    """
    return subprocess.run(
        [sys.executable, str(plugin or PLUGIN), "--config", str(config),
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
        for key in ("label", "group"):     # 0.3.0 から。古い版は見ない
            if key in task and not isinstance(task[key], str):
                return f"{key} must be a string: {task_id}"

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
# F-1d — id が信用できないカード (Codex 4 巡目)
# ---------------------------------------------------------------------------
#
# plugin は id が空でも重複していても **ファイル全体** を捨てる。そして crewvia
# 側の id は frontmatter に書かれた自己申告で、誰も検証していなかった。tNNN.md を
# コピーして id 行を直し忘れる —— カードを作る一番ありふれたやり方 —— だけで、
# 同じ id が 2 つ並んだ JSON が publish され、健全な他 mission の DAG まで消える。
#
# 害は可視化だけに留まらない。`plan.sh pull` は割り当てた card を
# `save_task(slug, meta['id'], ...)` で書き戻すので、`t002.md` が `id: t001` を
# 名乗っていると **t001.md が t002 の内容で上書きされる**。つまり id の食い違いは
# 表示の問題ではなく、カードを 1 枚失う経路である。

def _card_path(sandbox, task_id, mission=MISSION):
    return sandbox.queue / "missions" / mission / "tasks" / f"{task_id}.md"


def _copy_card(sandbox, src_id, dst_id, mission=MISSION, **replace):
    """tNNN.md をコピーして id 行を直し忘れた、という一番ありふれた壊し方。

    `replace` で本文の一部を差し替えられる (どちらのカードが書かれたかを
    見分けるため)。
    """
    text = _card_path(sandbox, src_id, mission).read_text()
    for old, new in replace.items():
        text = text.replace(old, new)
    _card_path(sandbox, dst_id, mission).write_text(text)


def test_a_copied_card_does_not_make_the_whole_file_unreadable(sandbox):
    """id を直し忘れたコピーがあっても、plugin がファイル全体を捨てないこと。

    捨てられると、まったく無関係な mission の DAG まで見えなくなる。t010 の
    循環 / 空配列とまったく同じ型の巻き添えである。
    """
    sandbox.add_task("t001", "pending", [])
    sandbox.add_task("t003", "done", [])
    _copy_card(sandbox, "t001", "t002")          # t002.md の中身は `id: t001`
    sandbox.add_mission("m-healthy")
    sandbox.add_task("t001", "pending", [], mission="m-healthy")

    assert sandbox.run("task-graph").returncode == 0
    graph = assert_plugin_accepts(sandbox)

    ids = [t["id"] for t in graph["tasks"]]
    assert len(ids) == len(set(ids)), f"id が重複したまま publish された: {ids}"
    assert "m-healthy:t001" in ids, (
        f"壊れていない mission が巻き添えで消えている: {ids}"
    )
    assert f"{MISSION}:t003" in ids, f"同じ mission の健全な card が消えている: {ids}"


def test_the_copied_card_is_isolated_visibly_not_dropped(sandbox):
    """隔離したカードが、隔離されたと分かる形で残ること。

    黙って落とすと「なぜこの task が DAG から消えたのか」が誰にも分からない。
    """
    sandbox.add_task("t001", "pending", [])
    _copy_card(sandbox, "t001", "t002")

    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(assert_plugin_accepts(sandbox))

    assert f"{MISSION}:t002" in nodes, (
        f"id の食い違うカードが黙って消えている: {sorted(nodes)}"
    )
    title = nodes[f"{MISSION}:t002"]["title"]
    assert "破損" in title, f"隔離の印が出ていない: {title!r}"
    assert "id" in title.lower(), f"何が問題なのか読み取れない: {title!r}"


def test_a_card_with_an_untrustworthy_id_is_never_ready_or_done(sandbox):
    """隔離したカードが、実行可能にも完了済みにも見えないこと。

    どちらに倒れても嘘になる: ready なら存在しない仕事が動けると読め、done なら
    やっていない仕事が終わったと読める。
    """
    sandbox.add_task("t001", "done", [])
    _copy_card(sandbox, "t001", "t002")          # done を名乗るコピー

    assert sandbox.run("task-graph").returncode == 0
    node = _by_id(assert_plugin_accepts(sandbox))[f"{MISSION}:t002"]
    assert node["status"] not in ("ready", "done"), node


def test_an_isolated_card_does_not_look_like_a_finished_dependency(sandbox):
    """隔離したカードに依存する task が、動けると誤って表示されないこと。"""
    sandbox.add_task("t001", "done", [])
    _copy_card(sandbox, "t001", "t002")
    sandbox.add_task("t003", "pending", ["t002"])

    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(assert_plugin_accepts(sandbox))
    assert nodes[f"{MISSION}:t003"]["status"] == "waiting", (
        "隔離した card が『満たされた依存』に見えている"
    )


def test_pull_never_writes_one_card_over_another(sandbox):
    """id の食い違いで、別のカードが上書きされないこと (害の本体)。

    `pull` は `save_task(slug, meta['id'], ...)` で書き戻す。`t002.md` が
    `id: t001` を名乗っていると、割り当ての瞬間に **t001.md が t002 の内容で
    上書きされる** —— カードが 1 枚、誰にも気付かれずに消える。
    """
    sandbox.add_task("t001", "pending", [])
    # priority を上げて、ソートで必ずコピー側が先に選ばれるようにする
    # (優先度が同じだと安定ソートで t001.md が先になり、事故が再現しない)。
    _copy_card(
        sandbox, "t001", "t002",
        **{"priority: medium": "priority: high", "title: task t001": "title: task t002"},
    )
    before = _card_path(sandbox, "t001").read_text()

    r = sandbox.run("pull", "--agent", "Ren", "--skills", "code")
    assert r.returncode == 0, r.stderr

    after = _card_path(sandbox, "t001").read_text()
    assert "task t002" not in after, (
        "t001.md が t002.md の内容で上書きされた —— カードが 1 枚失われている\n"
        f"before:\n{before}\nafter:\n{after}"
    )
    assert '"id": "t001"' in r.stdout, r.stdout
    assert "task t002" not in r.stdout, (
        f"id を名乗り替えたカードがそのまま割り当てられている: {r.stdout}"
    )


def test_a_mission_listed_twice_does_not_make_the_whole_file_unreadable(sandbox):
    """state.yaml で同じ mission が 2 回並んでいても、plugin が読めること。

    `active_missions` は手で編集される (Director の復旧手順に入っている)。
    同じ slug が 2 行あると、すべての node が 2 回出力され、全部が重複 id に
    なる —— カード 1 枚の事故ではなく、ファイルが丸ごと読めなくなる。
    """
    sandbox.add_task("t001", "pending", [])
    sandbox.add_task("t002", "pending", ["t001"])
    (sandbox.queue / "state.yaml").write_text(
        f"active_missions:\n  - {MISSION}\n  - {MISSION}\ndefault_mission: {MISSION}\n"
    )

    assert sandbox.run("task-graph").returncode == 0
    graph = assert_plugin_accepts(sandbox)
    ids = sorted(t["id"] for t in graph["tasks"])
    assert ids == [f"{MISSION}:t001", f"{MISSION}:t002"], (
        f"同じ mission が 2 重に出ている: {ids}"
    )


def test_a_card_without_an_id_line_is_not_silently_dropped(sandbox):
    """id 行そのものが無いカードが、DAG から黙って消えないこと。

    ファイル名と食い違う id と違って、ここに矛盾は無い —— ファイル名が答えを
    持っている。だから隔離ではなく、ファイル名の id で普通に扱う。ただし
    「黙って消える」だけは許さない。
    """
    sandbox.add_task("t001", "pending", [])
    path = _card_path(sandbox, "t001")
    path.write_text(path.read_text().replace("id: t001\n", ""))

    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(assert_plugin_accepts(sandbox))
    assert f"{MISSION}:t001" in nodes, (
        f"id 行の無い card が DAG から消えている: {sorted(nodes)}"
    )
    assert nodes[f"{MISSION}:t001"]["status"] == "ready"


def test_healthy_cards_are_untouched_by_the_id_check(sandbox):
    """対照: 普通のカードには印も付かず、状態も変わらないこと。"""
    for row in STATUS_ROWS:
        sandbox.add_task(row[0], row[1], row[2])
    assert sandbox.run("task-graph").returncode == 0

    nodes = _by_id(assert_plugin_accepts(sandbox))
    for task_id, _status, _bb, expected, marker in STATUS_ROWS:
        node = nodes[f"{MISSION}:{task_id}"]
        assert node["status"] == expected, (task_id, node)
        assert "破損" not in node["title"], (task_id, node["title"])
        if marker:
            assert marker in node["title"], (task_id, node["title"])


# ---------------------------------------------------------------------------
# 隔離はひとつの経路に集まっていること
# ---------------------------------------------------------------------------
#
# plugin がファイル全体を捨てる理由は増えうる (t010 で 2 つ、ここで 1 つ)。
# 潰し方がバラバラの場所に書かれていると、次の 1 件が来たときに片方だけ直して
# 穴が開く。だから **publish の直前に 1 つのゲート** を置き、拒否理由の対処は
# すべてそこを通す。

GATE = "enforce_task_graph_contract"

#: ゲートが潰している拒否理由と、その担当。plugin の `load_config` が投げる
#: 理由がこれ以外に増えたら、ゲートに 1 行足すことになる。
GATE_STEPS = {
    "空の tasks": "task_graph_placeholder",
    "id が空 / 重複": "isolate_untrustworthy_ids",
    "依存の循環": "break_dependency_cycles",
    "解決できない depends_on": "drop_unresolvable_dependencies",
}


def _plan_functions():
    import ast

    from test_task_graph import _functions, _plan_py_source

    return _functions(ast.parse(_plan_py_source()))


def test_every_rejection_reason_is_handled_inside_one_gate():
    """拒否理由の対処が、ぜんぶ同じゲートの中から呼ばれていること。"""
    import ast

    funcs = _plan_functions()
    assert GATE in funcs, f"{GATE}() が plan.sh に無い"
    gate_src = ast.unparse(funcs[GATE])
    for reason, helper in GATE_STEPS.items():
        assert helper in gate_src, (
            f"『{reason}』の対処 ({helper}) がゲートの外にある — "
            f"次に同種の破綻が来たとき、片方だけ直して穴が開く"
        )


def test_nothing_else_neutralises_a_rejection_reason_on_its_own():
    """各 helper が、ゲート以外から呼ばれていないこと。

    ゲートを通らない経路が 1 本でも残っていると、「ここを通せば安全」が
    成り立たなくなる。
    """
    import ast

    from test_task_graph import _call_name

    funcs = _plan_functions()
    for helper in GATE_STEPS.values():
        callers = set()
        for name, fn in funcs.items():
            if name in (GATE, helper):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and _call_name(node) == helper:
                    callers.add(name)
        assert not callers, (
            f"{helper}() がゲートの外からも呼ばれている: {sorted(callers)}"
        )


def _gate():
    """plan.sh のゲートを、そのまま呼べる形で取り出す。

    ゲートは「どこから来た node でも、通れば plugin が受け取れる」ことを保証する
    位置に立っている。いまその保証に頼っている経路は list_tasks の手前で潰れて
    いるので、**ゲート自身を直接呼ばないと、この保証を誰も見ていない状態になる**。
    """
    from test_task_graph import _plan_py_source

    src = _plan_py_source()
    # 末尾の dispatch 表から先は「サブコマンドを 1 つ実行する」本体なので、
    # 定義だけを読み込む。ここより上に全部の関数定義がある。
    cut = src.index("\ndispatch = {")
    ns: dict = {"__name__": "plan_sh_defs"}
    argv = sys.argv
    sys.argv = ["plan.sh", str(REPO_ROOT / "queue"), "task-graph", str(REPO_ROOT)]
    try:
        exec(compile(src[:cut], "plan.sh", "exec"), ns)  # noqa: S102
    finally:
        sys.argv = argv
    return ns[GATE]


def test_the_gate_isolates_a_duplicate_id_whoever_produced_it():
    """重複 id がゲートに届いたら、node を消さずに衝突を解くこと。"""
    gate = _gate()
    nodes = gate(
        [
            {"id": "m:t001", "title": "先に来たほう", "depends_on": [], "status": "ready"},
            {"id": "m:t001", "title": "あとから来たほう", "depends_on": [], "status": "ready"},
        ],
        ["m"],
    )
    ids = [n["id"] for n in nodes]
    assert len(nodes) == 2, f"node が消えている: {nodes}"
    assert len(ids) == len(set(ids)), f"重複が残っている: {ids}"
    assert ids[0] == "m:t001", "先に来たほうの id まで振り替えられている"

    later = nodes[1]
    assert "id重複" in later["title"], later["title"]
    assert "あとから来たほう" in later["title"], later["title"]
    assert later["status"] == "blocked", (
        "どのカードなのか言えない node が ready / done に見えている"
    )
    assert _reject_reason({"title": "t", "tasks": nodes}) is None


def test_the_gate_isolates_an_unusable_id_whoever_produced_it():
    """id が空 / 文字列でない node も、消さずに隔離すること。"""
    gate = _gate()
    nodes = gate(
        [
            {"id": "", "title": "id が空", "depends_on": [], "status": "done"},
            {"id": None, "title": "id が無い", "depends_on": [], "status": "ready"},
        ],
        ["m"],
    )
    assert len(nodes) == 2, f"node が消えている: {nodes}"
    for node in nodes:
        assert isinstance(node["id"], str) and node["id"], node
        assert "id不正" in node["title"], node["title"]
        assert node["status"] == "blocked", node
    assert _reject_reason({"title": "t", "tasks": nodes}) is None


def test_the_gate_leaves_a_healthy_graph_exactly_as_it_is():
    """対照: 契約を満たしている入力に、ゲートが手を入れないこと。"""
    gate = _gate()
    original = [
        {"id": "m:t001", "title": "one", "depends_on": [], "status": "done"},
        {"id": "m:t002", "title": "two", "depends_on": ["m:t001"], "status": "ready"},
    ]
    nodes = gate(json.loads(json.dumps(original)), ["m"])
    assert nodes == original, nodes


def test_the_graph_builder_publishes_through_the_gate():
    """build_task_graph() がゲートを通ってから返すこと。"""
    import ast

    funcs = _plan_functions()
    assert GATE in ast.unparse(funcs["build_task_graph"]), (
        f"build_task_graph() が {GATE}() を通らずに publish している"
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


def test_label_and_pane_id_do_not_make_an_older_plugin_reject_the_file(sandbox):
    """`label` / `pane_id` を先に書いても、未対応の plugin (0.1.1) は壊れない。

    plugin の `load_config` は未知の欄を拒否しない。写し (`_reject_reason`) と、
    手元に plugin があれば本物の両方で確かめる。
    """
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    sandbox.add_task("t002", "pending", ["t001"])
    sandbox.assign("Ren", "t001")
    sandbox.record_pane("Ren", "wP:p80")
    assert sandbox.run("task-graph").returncode == 0
    graph = assert_plugin_accepts(sandbox)
    node = _by_id(graph)[f"{MISSION}:t001"]
    assert node["label"] == "t001" and node["pane_id"] == "wP:p80"


def test_group_does_not_make_an_older_plugin_reject_the_file(sandbox):
    """`group` を書いても、未対応の plugin (0.1.1 / 0.2.0) は壊れない。

    写し (`_reject_reason`) で常に確かめ、古い版の実物が手元にあれば
    (`CREWVIA_TASK_GRAPH_OLD_PLUGINS`) それにも読ませる。
    """
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    sandbox.add_task("t002", "pending", ["t001"])
    sandbox.add_mission("m-beta")
    sandbox.add_task("t001", "pending", [], mission="m-beta")
    assert sandbox.run("task-graph").returncode == 0
    graph = assert_plugin_accepts(sandbox)
    nodes = _by_id(graph)
    assert nodes[f"{MISSION}:t001"]["group"] == MISSION
    assert nodes["m-beta:t001"]["group"] == "m-beta"

    for old_plugin in OLD_PLUGINS:
        assert old_plugin.exists(), f"CREWVIA_TASK_GRAPH_OLD_PLUGINS の {old_plugin} が無い"
        r = run_plugin(sandbox.graph, old_plugin)
        assert r.returncode == 0, (
            f"{old_plugin.parent.name} が group 付きのファイルを拒否した: {r.stderr.strip()[:400]}"
        )
        assert "Traceback" not in r.stderr, r.stderr[:400]


def test_the_mirror_rejects_a_non_string_group():
    """対照: 写しが `group` の型を見ていること (見ないと上のテストは空振りになる)。"""
    node = {"id": "m:t001", "title": "t", "depends_on": [], "status": "ready"}
    assert _reject_reason({"tasks": [dict(node, group="m")]}) is None
    assert "group must be a string" in _reject_reason({"tasks": [dict(node, group=1)]})


@requires_plugin
def test_the_real_plugin_shows_the_mission_slug_of_each_task(sandbox):
    """本物の plugin (0.3.0 以上) の画面に、同じ tNNN の mission が区別して出ること。"""
    import re

    sandbox.add_task("t001", "pending", [])
    sandbox.add_mission("m-beta")
    sandbox.add_task("t001", "pending", [], mission="m-beta")
    assert sandbox.run("task-graph").returncode == 0
    assert_plugin_accepts(sandbox)
    out = run_plugin(sandbox.graph).stdout
    if "group" not in PLUGIN.read_text():
        pytest.skip(f"{PLUGIN} は group 未対応の版")
    assert re.search(r"t001.*m-beta", out), out[:1200]
    assert re.search(r"t001.*" + re.escape(MISSION[-12:]), out), out[:1200]
