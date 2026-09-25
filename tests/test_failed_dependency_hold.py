#!/usr/bin/env python3
"""failed な依存を持つ task は、Director が解除するまで誰も進めない (t007 / backlog #9)。

## 背景

QA が FAIL した直後、その QA に `blocked_by` している review / merge task が
**自動で unblock され**、レビュアーが `pull --task` で取り直して merge 寸前まで
進んだ。原因は `lib_dep_rules.DEAD_DEP_STATUSES` が failed と cancelled の両方を持っていた —— 「もう
完了しない」依存を *満たされた* 扱いにする規則が、fix task (進めてよい) と
review / merge task (進めてはいけない) を区別できない。`plan.sh pull` /
`plan.sh task-graph` / `dispatcher.sh` の 3 者が同じ規則を読むので、3 者そろって
同じ穴を通っていた。

## 直し方 (knowledge/failed-dependency-hold.md に選択肢の比較がある)

`failed` の依存は「満たされた」ではなく **保留 (held)**。Director が
`plan.sh release-dep` で明示的に解除したときだけ進む。`cancelled` は Director 自身が
下した判断なので従来どおり満たされた扱い。

## このファイルが固定すること

1. 規則そのもの (lib_dep_rules) の真理値表。
2. **pull / pull --task / task-graph / status / dispatcher の 5 者が、同じ依存パターンに
   同じ答えを出すこと** (どれか 1 者が別の答えを出したら赤)。
3. 規則のコピーが 3 者の側に戻っていないこと (形の固定)。
4. 保留が「永久保留」という別の outage にならないこと: 理由と解除コマンドが
   `plan.sh status` に出る / 解除すれば進める / 解除の対象を間違えても拒否される。
"""

from __future__ import annotations

import itertools
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
PLAN_SH = SCRIPTS_DIR / "plan.sh"
DISPATCHER_SH = SCRIPTS_DIR / "dispatcher.sh"
DEP_RULES_PY = SCRIPTS_DIR / "lib_dep_rules.py"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lib_dep_rules  # noqa: E402

MISSION = "m-hold"


# ---------------------------------------------------------------------------
# 1. 規則の真理値表
# ---------------------------------------------------------------------------

DONE = {"t001"}


def _unmet(deps, statuses, released=()):
    return lib_dep_rules.unmet_dependencies(deps, DONE, statuses, released)


def test_a_failed_dependency_is_unmet_not_satisfied():
    """バグの本体。`failed` の依存が満たされた扱いになっていないこと。"""
    assert _unmet(["t002"], {"t002": "failed"}) == ["t002"]


def test_a_failed_dependency_is_reported_as_held():
    held = lib_dep_rules.held_dependencies(["t002", "t003"], DONE,
                                           {"t002": "failed", "t003": "pending"})
    assert held == ["t002"], "held は failed のものだけ (pending は待つだけ)"


def test_cancelled_stays_satisfied():
    """`cancelled` は Director が自分で下した判断。保留にすると自分の判断で止まる。"""
    assert _unmet(["t002"], {"t002": "cancelled"}) == []
    assert lib_dep_rules.held_dependencies(["t002"], DONE, {"t002": "cancelled"}) == []


def test_an_explicit_release_satisfies_a_failed_dependency():
    assert _unmet(["t002"], {"t002": "failed"}, released=["t002"]) == []
    assert lib_dep_rules.held_dependencies(
        ["t002"], DONE, {"t002": "failed"}, ["t002"]) == []


def test_a_release_is_per_dependency_not_per_task():
    """t002 だけを解除しても、後から failed になった t003 は保留のまま。"""
    statuses = {"t002": "failed", "t003": "failed"}
    assert _unmet(["t002", "t003"], statuses, released=["t002"]) == ["t003"]


def test_releasing_a_dependency_that_has_not_failed_does_not_satisfy_it():
    """事前解除は「もし failed になっても待たない」の意味で、今の依存は待つ。"""
    assert _unmet(["t002"], {"t002": "pending"}, released=["t002"]) == ["t002"]
    assert _unmet(["t002"], {"t002": "in_progress"}, released=["t002"]) == ["t002"]


def test_a_dangling_dependency_is_still_unmet_even_if_released():
    assert _unmet(["t099"], {}, released=["t099"]) == ["t099"]


def test_card_dependencies_reads_blocked_by_and_released_deps_from_the_card():
    """3 者は meta を渡すだけ。released_deps を渡し忘れる経路が構造的に無いこと。"""
    meta = {"blocked_by": ["t002", "t003"], "released_deps": ["t002"]}
    v = lib_dep_rules.card_dependencies(meta, DONE, {"t002": "failed", "t003": "failed"})
    assert v.unmet == ["t003"]
    assert v.held == ["t003"]

    bare = lib_dep_rules.card_dependencies({}, DONE, {})
    assert (bare.unmet, bare.held) == ([], [])
    nulls = lib_dep_rules.card_dependencies(
        {"blocked_by": None, "released_deps": None}, DONE, {})
    assert (nulls.unmet, nulls.held) == ([], [])


# ---------------------------------------------------------------------------
# 5 者が同じ答えを出す — フィクスチャ
# ---------------------------------------------------------------------------

def _card(task_id, status, blocked_by=(), released=None, worker=None):
    lines = [
        "---",
        f"id: {task_id}",
        f"title: task {task_id}",
        "skills: [code]",
        "priority: medium",
        f"status: {status}",
        "blocked_by: [" + ", ".join(blocked_by) + "]",
    ]
    if released is not None:
        lines.append("released_deps: [" + ", ".join(released) + "]")
    lines += [
        "target_dir: null",
        f"worker: {worker or 'null'}",
        "started_at: null",
        "completed_at: null",
        "---",
        "",
        "## Description",
        "",
        "fixture",
        "",
        "## Result",
        "",
    ]
    return "\n".join(lines) + "\n"


def _mission_yaml(slug):
    return (
        f"title: fixture {slug}\nslug: {slug}\nstatus: in_progress\n"
        'created_at: "2026-01-01T00:00:00Z"\ncompleted_at: null\n'
        "next_task_id: 99\nmax_review_cycles: 3\n"
    )


class Sandbox:
    """CREWVIA_REPO_ROOT / CREWVIA_QUEUE を隔離した plan.sh の実行環境。"""

    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / ".git").mkdir()
        (self.root / "registry" / "mux").mkdir(parents=True)
        shutil.copy2(PLAN_SH, self.root / "scripts" / "plan.sh")
        for extra in ("lib_dep_rules.py", "lib_task_cards.py",
                      "lib_registry.py", "lint_plan.py"):
            src = SCRIPTS_DIR / extra
            if src.exists():
                shutil.copy2(src, self.root / "scripts" / extra)
        self.queue = self.root / "queue"
        self.tasks = self.queue / "missions" / MISSION / "tasks"
        self.tasks.mkdir(parents=True)
        (self.queue / "archive").mkdir()
        (self.queue / "missions" / MISSION / "mission.yaml").write_text(_mission_yaml(MISSION))
        (self.queue / "state.yaml").write_text(
            f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n")
        self.graph = self.root / "registry" / "task-graph" / "tasks.json"

    def card(self, task_id, status, blocked_by=(), released=None):
        (self.tasks / f"{task_id}.md").write_text(_card(task_id, status, blocked_by, released))

    def env(self):
        env = dict(os.environ)
        env.update(CREWVIA_REPO_ROOT=str(self.root), CREWVIA_QUEUE=str(self.queue),
                   CREWVIA_TASKVIA="disabled", TASKVIA_URL="", TASKVIA_TOKEN="")
        return env

    def run(self, *args):
        return subprocess.run(["bash", str(self.root / "scripts" / "plan.sh"), *args],
                              env=self.env(), capture_output=True, text=True)

    def graph_node(self, task_id):
        nodes = {t["id"]: t for t in json.loads(self.graph.read_text())["tasks"]}
        return nodes[f"{MISSION}:{task_id}"]


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


def _dispatcher_namespace(root):
    from test_task_card_identity import load_dispatcher_namespace
    return load_dispatcher_namespace(root)


# ---------------------------------------------------------------------------
# 5 者が同じ答えを出す — 依存パターンの網羅表
# ---------------------------------------------------------------------------
#
# (名前, 依存先カード [(id, status)], 下流の blocked_by, released_deps, 期待)
#   期待 = "ready"   … 誰でも進めてよい
#          "waiting" … 依存が終わるのを待つだけ (誰も何もしなくてよい)
#          "held"    … failed の依存。Director の判断待ち

PATTERNS = [
    ("done",                 [("t001", "done")],                 ["t001"], None,   "ready"),
    ("verified",             [("t001", "verified")],             ["t001"], None,   "ready"),
    ("skipped",              [("t001", "skipped")],              ["t001"], None,   "ready"),
    ("cancelled",            [("t001", "cancelled")],            ["t001"], None,   "ready"),
    ("pending",              [("t001", "pending")],              ["t001"], None,   "waiting"),
    ("in_progress",          [("t001", "in_progress")],          ["t001"], None,   "waiting"),
    ("verification_failed",  [("t001", "verification_failed")],  ["t001"], None,   "waiting"),
    ("needs_director",       [("t001", "needs_director")],       ["t001"], None,   "waiting"),
    ("blocked",              [("t001", "blocked")],              ["t001"], None,   "waiting"),
    ("dangling",             [],                                 ["t099"], None,   "waiting"),
    ("dangling+released",    [],                                 ["t099"], ["t099"], "waiting"),
    ("failed+cancelled",     [("t001", "failed"), ("t002", "cancelled")],
                                                                 ["t001", "t002"], None, "held"),
    ("failed",               [("t001", "failed")],               ["t001"], None,   "held"),
    ("failed+released",      [("t001", "failed")],               ["t001"], ["t001"], "ready"),
    ("failed+released-other", [("t001", "failed")],              ["t001"], ["t009"], "held"),
    ("pending+pre-released", [("t001", "pending")],              ["t001"], ["t001"], "waiting"),
    ("done+failed",          [("t001", "done"), ("t002", "failed")],
                                                                 ["t001", "t002"], None, "held"),
    ("pending+failed",       [("t001", "pending"), ("t002", "failed")],
                                                                 ["t001", "t002"], None, "held"),
    ("failed+failed, one released",
                             [("t001", "failed"), ("t002", "failed")],
                                                                 ["t001", "t002"], ["t001"], "held"),
    ("failed+failed, both released",
                             [("t001", "failed"), ("t002", "failed")],
                                                                 ["t001", "t002"], ["t001", "t002"], "ready"),
]

DOWNSTREAM = "t010"


def _build(sandbox, deps, blocked_by, released):
    for dep_id, dep_status in deps:
        sandbox.card(dep_id, dep_status)
    sandbox.card(DOWNSTREAM, "pending", blocked_by, released)


IDS = [p[0] for p in PATTERNS]


@pytest.mark.parametrize("name,deps,blocked_by,released,expected", PATTERNS, ids=IDS)
def test_normal_pull_agrees(tmp_path, name, deps, blocked_by, released, expected):
    """dispatcher が使うのと同じ「自動選択」の pull。downstream だけが pending。"""
    sb = Sandbox(tmp_path)
    _build(sb, deps, blocked_by, released)
    r = sb.run("pull", "--skills", "code", "--agent", "Ren", "--mission", MISSION)
    pulled = r.returncode == 0 and DOWNSTREAM in r.stdout
    # 依存先の pending / in_progress 等は他の pending を持つが、それらは
    # skills が code なので自動選択の候補になり得る。下流を選んだかだけを見る。
    if expected == "ready":
        assert pulled, f"[{name}] 進めるはずの task が pull されない: {r.stdout} {r.stderr}"
    else:
        assert not (pulled and json.loads(r.stdout).get("id") == DOWNSTREAM), (
            f"[{name}] {expected} のはずの task が自動選択された: {r.stdout}")


@pytest.mark.parametrize("name,deps,blocked_by,released,expected", PATTERNS, ids=IDS)
def test_pull_task_agrees(tmp_path, name, deps, blocked_by, released, expected):
    """`pull --task` (dispatcher の kickoff 経由 = 今回の事故の経路)。"""
    sb = Sandbox(tmp_path)
    _build(sb, deps, blocked_by, released)
    r = sb.run("pull", "--task", DOWNSTREAM, "--mission", MISSION,
               "--skills", "code", "--agent", "Ren")
    if expected == "ready":
        assert r.returncode == 0, f"[{name}] 進めるはずなのに拒否された: {r.stderr}"
        assert json.loads(r.stdout)["id"] == DOWNSTREAM
    else:
        assert r.returncode != 0, f"[{name}] {expected} のはずが pull できた: {r.stdout}"
        assert "blocked by unfinished" in r.stderr or "held" in r.stderr, r.stderr


GRAPH_STATUS = {"ready": "ready", "waiting": "waiting", "held": "blocked"}


@pytest.mark.parametrize("name,deps,blocked_by,released,expected", PATTERNS, ids=IDS)
def test_task_graph_agrees(tmp_path, name, deps, blocked_by, released, expected):
    sb = Sandbox(tmp_path)
    _build(sb, deps, blocked_by, released)
    r = sb.run("task-graph")
    assert r.returncode == 0, r.stderr
    node = sb.graph_node(DOWNSTREAM)
    assert node["status"] == GRAPH_STATUS[expected], (
        f"[{name}] DAG の表示が pull の可否と食い違う: {node}")
    if expected == "held":
        assert "t001" in node["title"] or "t002" in node["title"], (
            f"[{name}] 何が原因で保留なのかが DAG から読めない: {node['title']!r}")


@pytest.mark.parametrize("name,deps,blocked_by,released,expected", PATTERNS, ids=IDS)
def test_dispatcher_agrees(tmp_path, name, deps, blocked_by, released, expected):
    sb = Sandbox(tmp_path)
    _build(sb, deps, blocked_by, released)
    ns = _dispatcher_namespace(sb.root)
    all_tasks, done_ids, statuses = ns["load_all_tasks"]([MISSION])
    meta = next(m for _s, m in all_tasks if m["id"] == DOWNSTREAM)
    verdict = ns["dependency_gate"](MISSION, meta, done_ids, statuses)
    assert bool(verdict.unmet) == (expected != "ready"), (
        f"[{name}] dispatcher の判定が pull と食い違う: {verdict}")
    assert bool(verdict.held) == (expected == "held"), (
        f"[{name}] dispatcher が held を見分けていない: {verdict}")


@pytest.mark.parametrize("name,deps,blocked_by,released,expected", PATTERNS, ids=IDS)
def test_status_agrees(tmp_path, name, deps, blocked_by, released, expected):
    sb = Sandbox(tmp_path)
    _build(sb, deps, blocked_by, released)
    r = sb.run("status", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    line = next(l for l in r.stdout.splitlines() if f" {DOWNSTREAM} " in l)
    if expected == "ready":
        assert "(pending)" in line, f"[{name}] 進めるのに blocked と出ている: {line}"
    elif expected == "waiting":
        assert "blocked:" in line and "HELD" not in line, f"[{name}] {line}"
    else:
        assert "HELD" in line, f"[{name}] 保留と出ていない: {line}"


# ---------------------------------------------------------------------------
# 4. 永久保留にならない — 理由が見え、解除でき、間違えても拒否される
# ---------------------------------------------------------------------------

def test_status_says_why_and_how_to_release(sandbox):
    sandbox.card("t001", "failed")
    sandbox.card("t002", "pending", ["t001"])
    detail = sandbox.run("status", "--mission", MISSION).stdout
    assert "plan.sh release-dep t002" in detail, (
        f"解除コマンドが出ていない (= Director が何を打てばよいか分からない): {detail}")
    assert "t001" in detail

    summary = sandbox.run("status").stdout
    assert "t002" in summary and "t001" in summary, (
        f"既定の status (要約) に保留が出ない。Director が気付けない: {summary}")


def test_release_dep_lets_the_task_proceed_and_is_recorded(sandbox):
    sandbox.card("t001", "failed")
    sandbox.card("t002", "pending", ["t001"])
    assert sandbox.run("pull", "--task", "t002", "--mission", MISSION,
                       "--skills", "code", "--agent", "Ren").returncode != 0

    r = sandbox.run("release-dep", "t002", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert "t001" in r.stdout

    text = (sandbox.tasks / "t002.md").read_text()
    assert re.search(r"^released_deps: \[t001\]$", text, re.M), text
    assert re.search(r"^blocked_by: \[t001\]$", text, re.M), (
        "解除しても依存の辺は消えない (DAG に履歴が残る)")

    ok = sandbox.run("pull", "--task", "t002", "--mission", MISSION,
                     "--skills", "code", "--agent", "Ren")
    assert ok.returncode == 0, ok.stderr


def test_release_dep_defaults_to_the_currently_held_dependencies(sandbox):
    sandbox.card("t001", "failed")
    sandbox.card("t002", "done")
    sandbox.card("t003", "pending", ["t001", "t002"])
    assert sandbox.run("release-dep", "t003", "--mission", MISSION).returncode == 0
    assert "released_deps: [t001]" in (sandbox.tasks / "t003.md").read_text()


def test_release_dep_can_name_a_dependency_explicitly(sandbox):
    sandbox.card("t001", "failed")
    sandbox.card("t002", "failed")
    sandbox.card("t003", "pending", ["t001", "t002"])
    assert sandbox.run("release-dep", "t003", "--dep", "t002",
                       "--mission", MISSION).returncode == 0
    text = (sandbox.tasks / "t003.md").read_text()
    assert "released_deps: [t002]" in text
    still = sandbox.run("pull", "--task", "t003", "--mission", MISSION,
                        "--skills", "code", "--agent", "Ren")
    assert still.returncode != 0 and "t001" in still.stderr


def test_release_dep_is_idempotent_and_accumulates(sandbox):
    sandbox.card("t001", "failed")
    sandbox.card("t002", "failed")
    sandbox.card("t003", "pending", ["t001", "t002"])
    for dep in ("t001", "t001", "t002"):
        assert sandbox.run("release-dep", "t003", "--dep", dep,
                           "--mission", MISSION).returncode == 0
    assert "released_deps: [t001, t002]" in (sandbox.tasks / "t003.md").read_text()


def test_release_dep_rejects_a_dependency_the_task_does_not_have(sandbox):
    """存在しない依存の名指しを黙って受理しない (打ち間違いが解除に見えてしまう)。"""
    sandbox.card("t001", "failed")
    sandbox.card("t002", "pending", ["t001"])
    r = sandbox.run("release-dep", "t002", "--dep", "t009", "--mission", MISSION)
    assert r.returncode != 0
    assert "t009" in r.stderr
    assert "released_deps" not in (sandbox.tasks / "t002.md").read_text()


def test_release_dep_with_nothing_to_release_says_so(sandbox):
    sandbox.card("t001", "done")
    sandbox.card("t002", "pending", ["t001"])
    r = sandbox.run("release-dep", "t002", "--mission", MISSION)
    assert r.returncode != 0
    assert "nothing" in r.stderr.lower() or "保留" in r.stderr
    assert "released_deps" not in (sandbox.tasks / "t002.md").read_text()


def test_release_dep_refuses_a_task_that_is_not_pending(sandbox):
    """走り出した task の依存を解除しても意味が無い。黙って書き換えない。"""
    sandbox.card("t001", "failed")
    sandbox.card("t002", "in_progress", ["t001"])
    r = sandbox.run("release-dep", "t002", "--mission", MISSION)
    assert r.returncode != 0
    assert "pending" in r.stderr


def test_update_blocked_by_prunes_releases_of_dependencies_that_left(sandbox):
    """依存を付け替えたとき、古い解除が新しい依存の解除として残らない。"""
    sandbox.card("t001", "failed")
    sandbox.card("t002", "pending", ["t001"], released=["t001"])
    assert sandbox.run("update", "t002", "--blocked-by", "t003",
                       "--mission", MISSION).returncode == 0
    text = (sandbox.tasks / "t002.md").read_text()
    assert "t001" not in text.split("---")[1].replace("id: t002", ""), text


def test_release_survives_the_dependency_being_rerun(sandbox):
    """解除は「その task がもう一度 failed になっても待たない」ではなく、
    依存 1 つ (t001) に対する解除。t001 が done になれば普通に満たされる。"""
    sandbox.card("t001", "done")
    sandbox.card("t002", "pending", ["t001"], released=["t001"])
    assert sandbox.run("pull", "--task", "t002", "--mission", MISSION,
                       "--skills", "code", "--agent", "Ren").returncode == 0


def test_held_diagnostic_appears_when_nothing_else_is_pullable(sandbox):
    """自動選択で何も取れなかったとき、理由が「保留」と分かること。
    (blocked としか出ないと、Director は待てばよいのか動くべきなのか判断できない)"""
    sandbox.card("t001", "failed")
    sandbox.card("t002", "pending", ["t001"])
    r = sandbox.run("pull", "--skills", "code", "--agent", "Ren", "--mission", MISSION)
    combined = r.stdout + r.stderr
    assert "held" in combined.lower() or "保留" in combined, combined
    assert "release-dep" in combined, combined


# ---------------------------------------------------------------------------
# 5. アーカイブ済み mission の card に回帰が無い
# ---------------------------------------------------------------------------

def test_archived_missions_still_read_without_error(sandbox):
    """archive 済み mission に failed 依存が残っていても、status --all が落ちない。"""
    arch = sandbox.queue / "archive" / "old-mission"
    (arch / "tasks").mkdir(parents=True)
    (arch / "mission.yaml").write_text(_mission_yaml("old-mission").replace(
        "status: in_progress", "status: done"))
    (arch / "tasks" / "t001.md").write_text(_card("t001", "failed"))
    (arch / "tasks" / "t002.md").write_text(_card("t002", "pending", ["t001"]))
    sandbox.card("t001", "done")
    r = sandbox.run("status", "--all")
    assert r.returncode == 0, r.stderr
    assert "old-mission" in r.stdout

    d = sandbox.run("status", "--mission", "old-mission")
    assert d.returncode == 0, d.stderr
    assert "t002" in d.stdout


def test_archive_command_keeps_released_deps(sandbox):
    """archive は card をそのまま退避する。released_deps を落とすと復元時に保留が復活する。"""
    src = PLAN_SH.read_text()
    assert "released_deps" in src


# ---------------------------------------------------------------------------
# 3. 形の固定 — 規則のコピーが戻らない
# ---------------------------------------------------------------------------

def _code(path):
    return "\n".join(l for l in path.read_text(errors="replace").splitlines()
                     if not l.lstrip().startswith("#"))


def test_consumers_ask_the_card_not_the_raw_lists():
    """3 者が `card_dependencies(meta, ...)` を呼び、blocked_by を自前で解釈しない。

    `unmet_dependencies(blocked_by, ...)` を直接呼ぶと released_deps を渡し忘れられる
    (渡し忘れ = 常に保留 = 解除が効かない、という見えにくい壊れ方になる)。
    """
    plan = _code(PLAN_SH)
    disp = _code(DISPATCHER_SH)
    assert plan.count("card_dependencies(") >= 3, (
        "plan.sh の pull ×2 / task-graph が card_dependencies() を使っていない")
    assert "card_dependencies(" in disp, "dispatcher.sh が card_dependencies() を使っていない"
    for name, src in (("plan.sh", plan), ("dispatcher.sh", disp)):
        assert "unmet_dependencies(bb" not in src, (
            f"{name} が blocked_by を直接 unmet_dependencies() に渡している")


def test_the_failed_status_is_not_named_by_any_consumer():
    """終わらないと確定した status の並び (どの順でも) が本体の外に無いこと。

    探す文字列は本体の定数から組み立てる (このテストにリテラルを書かない)。
    """
    mod = lib_dep_rules
    dead = tuple(mod.DEAD_DEP_STATUSES) + tuple(mod.HELD_DEP_STATUSES)
    needles = {", ".join(repr(s) for s in order) for order in itertools.permutations(dead)}
    for glob in ("scripts/*.sh", "scripts/*.py", "hooks/*.sh"):
        for path in sorted(REPO_ROOT.glob(glob)):
            if path.resolve() == DEP_RULES_PY.resolve():
                continue
            text = path.read_text(errors="replace")
            assert not any(n in text for n in needles), (
                f"{path.name} に依存規則のコピーがある")


def test_dispatcher_gate_is_defined_before_the_cycle_entry_point():
    """テストが exec() できる位置 (CYCLE ENTRY POINT より前) に置かれていること。"""
    text = DISPATCHER_SH.read_text()
    assert text.index("def dependency_gate(") < text.index("# --- CYCLE ENTRY POINT ---")
