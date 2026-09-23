"""queue → herdr-task-graph の tasks.json 生成を固定する。

このファイルが守りたいのは 3 つある。

1. **「依存が満たされた」の定義が 1 箇所しか無いこと。**
   crewvia の pull は `failed` / `cancelled` の依存を *満たされた扱い* にする。
   DAG 側がこの規則を自前で持つと、QA が FAIL した直後 —— 「次に何が動ける
   のか」を最も知りたい瞬間 —— にだけ、実際は dispatch される task を WAIT と
   表示する。規則は `unmet_dependencies()` ただ 1 つから来なければならない。

2. **生成がキューロックの内側に入らないこと。**
   plan.sh は全 Worker と両デーモンが叩く中枢である。ロック保持中に全 mission
   の走査を足すと、その分だけ全員の pull が待たされる。

3. **生成の失敗も、生成の有無も、本体の動作を変えないこと。**
   付加機能が plan.sh の終了コードを変えたら、Worker とデーモンがまとめて
   止まる。停止スイッチ 1 つで完全に無効化できることも併せて固定する。

構造 (1, 2) は AST で、挙動 (3 とマッピング) は実際に plan.sh を走らせて見る。
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLAN_SH = REPO_ROOT / "scripts" / "plan.sh"

#: LOCK_BUSY (plan.sh): ロックを取れず 1 バイトも書かずに引き返した終了コード。
LOCK_BUSY = 4


# ---------------------------------------------------------------------------
# plan.sh に埋め込まれた python 本体
# ---------------------------------------------------------------------------

def _plan_py_source() -> str:
    text = PLAN_SH.read_text()
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    assert m, "plan.sh の python ヒアドキュメント (PYEOF) が見つからない"
    return m.group(1)


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(_plan_py_source(), filename=str(PLAN_SH))


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    out: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            out.setdefault(node.name, node)
    return out


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


# ---------------------------------------------------------------------------
# 1. 依存判定の規則は 1 箇所から来る
# ---------------------------------------------------------------------------

def test_unmet_dependency_rule_exists_as_one_helper(tree):
    """READY / WAIT の判定が `unmet_dependencies()` に閉じていること。"""
    assert "unmet_dependencies" in _functions(tree), (
        "依存判定のヘルパー unmet_dependencies() が無い。"
        "pull と task-graph が別々に規則を持つと、QA FAIL 直後にだけ嘘をつく DAG になる。"
    )


def test_pull_does_not_reimplement_the_dependency_rule(tree):
    """cmd_pull が自前で `dep not in done_ids` を書いていないこと。

    ここが赤くなるのは、pull 側に規則がコピーで残ったとき —— つまり片方だけ直して
    もう片方が古いままになれる状態に戻ったとき。
    """
    funcs = _functions(tree)
    assert "cmd_pull" in funcs
    src = ast.unparse(funcs["cmd_pull"])
    assert "done_ids" in src, "前提が変わっている (cmd_pull が done_ids を使っていない)"
    assert "not in done_ids" not in src, (
        "cmd_pull が依存判定を自前で持っている。"
        "unmet_dependencies() を呼ぶ形に寄せること"
    )
    assert "unmet_dependencies" in src, "cmd_pull が unmet_dependencies() を使っていない"


def test_task_graph_uses_the_same_helper(tree):
    """生成側も同じヘルパーを使うこと。"""
    funcs = _functions(tree)
    assert "build_task_graph" in funcs, "build_task_graph() が無い"
    src = ast.unparse(funcs["build_task_graph"])
    assert "unmet_dependencies" in src, (
        "build_task_graph() が unmet_dependencies() を使っていない。"
        "READY の導出を自前で書くと pull と分裂する"
    )


# ---------------------------------------------------------------------------
# 2. 生成はロックの外
# ---------------------------------------------------------------------------

def test_generation_is_never_called_inside_a_function(tree):
    """`maybe_refresh_task_graph()` の呼び出しはモジュール直下だけ。

    with_lock() に渡されるのは必ず関数 (`_do` クロージャ) なので、「どの関数の
    内側でも呼ばれていない」を示せれば、ロックの内側に入っていないことも同時に
    示せる。将来 with_lock の形が変わっても崩れない言い方で固定する。
    """
    #: 生成機構そのもの。この中で互いを呼ぶのは組み立てであって、呼び出し口ではない。
    GENERATOR = {"maybe_refresh_task_graph", "refresh_task_graph", "build_task_graph"}
    #: 生成を始めてよい関数 (明示的なサブコマンドだけ)。
    ALLOWED_ENTRYPOINTS = {"cmd_task_graph"}

    inside: list[str] = []

    def walk(node: ast.AST, owner: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and _call_name(child) in GENERATOR:
                if owner is not None and owner not in (GENERATOR | ALLOWED_ENTRYPOINTS):
                    inside.append(f"{owner} → {_call_name(child)}")
            nxt = child.name if isinstance(child, ast.FunctionDef) else owner
            walk(child, nxt)

    walk(tree, None)
    assert not inside, (
        "task-graph の生成が関数の内側から呼ばれている "
        f"({inside}) — キューロックの内側に入りうる"
    )

    # 自動の呼び出し口はモジュール直下ただ 1 箇所。with_lock() に渡されるのは
    # 必ず関数なので、「関数の内側に 1 つも無い」でロックの外を言い切れる。
    top_level = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == "maybe_refresh_task_graph"
    ) - len(inside)
    assert top_level >= 1, "maybe_refresh_task_graph() がどこからも呼ばれていない"


def test_every_subcommand_is_classified(tree):
    """dispatch の全サブコマンドが「生成を呼ぶ / 呼ばない」に分類されていること。

    新しいサブコマンドを足したとき、分類を忘れると DAG がその遷移だけ映さなく
    なる (cycle 3 の指摘: `ready-for-verification` が抜けていた)。
    """
    src = _plan_py_source()
    ns: dict[str, object] = {}
    for name in ("QUEUE_MUTATING_SUBCOMMANDS", "QUEUE_READONLY_SUBCOMMANDS"):
        m = re.search(rf"^{name} = (\{{.*?\}})$", src, re.DOTALL | re.MULTILINE)
        assert m, f"{name} が plan.sh に無い"
        # set リテラルのみを読む (ast.literal_eval なのでコード実行は起きない)
        ns[name] = ast.literal_eval(m.group(1))

    dispatch_m = re.search(r"^dispatch = \{(.*?)^\}$", src, re.DOTALL | re.MULTILINE)
    assert dispatch_m, "dispatch テーブルが見つからない"
    keys = set(re.findall(r"'([a-z-]+)':", dispatch_m.group(1)))
    assert keys, "dispatch のキーを読み取れなかった"

    classified = set(ns["QUEUE_MUTATING_SUBCOMMANDS"]) | set(ns["QUEUE_READONLY_SUBCOMMANDS"])
    assert keys - classified == set(), f"分類されていないサブコマンド: {sorted(keys - classified)}"
    assert classified - keys == set(), f"dispatch に無いサブコマンド: {sorted(classified - keys)}"
    overlap = set(ns["QUEUE_MUTATING_SUBCOMMANDS"]) & set(ns["QUEUE_READONLY_SUBCOMMANDS"])
    assert not overlap, f"両方に入っているサブコマンド: {sorted(overlap)}"


# ---------------------------------------------------------------------------
# 挙動テスト — 隔離した queue / repo root で plan.sh を実走させる
# ---------------------------------------------------------------------------

MISSION = "m-alpha"

#: 対応表の全行を 1 つの mission に並べたフィクスチャ。
#: (task_id, crewvia status, blocked_by, 期待する plugin status, title に出る印)
STATUS_ROWS = [
    ("t001", "done", [], "done", None),
    ("t002", "verified", [], "done", None),
    ("t003", "skipped", [], "done", "[skip]"),
    ("t004", "in_progress", [], "running", None),
    ("t005", "verifying", [], "running", None),
    ("t006", "failed", [], "failed", None),
    ("t007", "verification_failed", [], "failed", "[検証NG]"),
    ("t008", "blocked", [], "blocked", "[停止]"),
    ("t009", "needs_director", [], "blocked", "[要判断]"),
    ("t010", "needs_human_review", [], "blocked", "[要判断]"),
    ("t011", "ready_for_verification", [], "blocked", "[要判断]"),
    ("t012", "pending", [], "ready", None),          # 依存なし → 実行可能
    ("t013", "pending", ["t004"], "waiting", None),  # 依存が in_progress → 依存待ち
    ("t014", "pending", ["t006"], "ready", None),    # 依存が failed → crewvia は dispatch する
]


def _task_md(task_id: str, status: str, blocked_by, *, title=None, worker=None) -> str:
    bb = "[" + ", ".join(blocked_by) + "]"
    return (
        "---\n"
        f"id: {task_id}\n"
        f"title: {title or ('task ' + task_id)}\n"
        "skills: [code]\n"
        "priority: medium\n"
        f"status: {status}\n"
        f"blocked_by: {bb}\n"
        "target_dir: null\n"
        f"worker: {worker if worker else 'null'}\n"
        "started_at: null\n"
        "completed_at: null\n"
        "---\n"
        "\n"
        "## Description\n\n"
        "fixture\n\n"
        "## Result\n\n"
    )


def _mission_yaml(slug: str, next_id: int = 99) -> str:
    return (
        f"title: fixture {slug}\n"
        f"slug: {slug}\n"
        "status: in_progress\n"
        'created_at: "2026-01-01T00:00:00Z"\n'
        "completed_at: null\n"
        f"next_task_id: {next_id}\n"
        "max_review_cycles: 3\n"
    )


@pytest.fixture
def sandbox(tmp_path):
    """CREWVIA_REPO_ROOT / CREWVIA_QUEUE の両方を隔離した実行環境。"""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(PLAN_SH, root / "scripts" / "plan.sh")
    for extra in ("lib_registry.py", "lint_plan.py"):
        src = REPO_ROOT / "scripts" / extra
        if src.exists():
            shutil.copy2(src, root / "scripts" / extra)

    queue = root / "queue"
    (queue / "missions" / MISSION / "tasks").mkdir(parents=True)
    (queue / "archive").mkdir(parents=True)
    (queue / "missions" / MISSION / "mission.yaml").write_text(_mission_yaml(MISSION))
    (queue / "state.yaml").write_text(
        f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n"
    )

    class Sandbox:
        def __init__(self):
            self.root = root
            self.queue = queue
            self.graph = root / "registry" / "task-graph" / "tasks.json"

        def env(self, **overrides):
            env = dict(os.environ)
            env.update(
                CREWVIA_REPO_ROOT=str(root),
                CREWVIA_QUEUE=str(queue),
                CREWVIA_TASKVIA="disabled",
                TASKVIA_URL="",
                TASKVIA_TOKEN="",
            )
            for k, v in overrides.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = v
            return env

        def add_task(self, *args, mission=MISSION, **kwargs):
            tdir = queue / "missions" / mission / "tasks"
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / f"{args[0]}.md").write_text(_task_md(*args, **kwargs))

        def add_mission(self, slug):
            (queue / "missions" / slug / "tasks").mkdir(parents=True, exist_ok=True)
            (queue / "missions" / slug / "mission.yaml").write_text(_mission_yaml(slug))
            state = (queue / "state.yaml").read_text()
            state = state.replace(
                "default_mission:", f"  - {slug}\ndefault_mission:"
            )
            (queue / "state.yaml").write_text(state)

        def run(self, *args, env=None, script=None):
            return subprocess.run(
                ["bash", str(script or (root / "scripts" / "plan.sh")), *args],
                env=env if env is not None else self.env(),
                capture_output=True,
                text=True,
            )

        def read_graph(self):
            return json.loads(self.graph.read_text())

    return Sandbox()


def _by_id(graph):
    return {t["id"]: t for t in graph["tasks"]}


# --- 対応表 ---------------------------------------------------------------

def test_status_mapping_covers_every_crewvia_status(sandbox):
    for row in STATUS_ROWS:
        sandbox.add_task(row[0], row[1], row[2])
    r = sandbox.run("task-graph")
    assert r.returncode == 0, r.stderr

    nodes = _by_id(sandbox.read_graph())
    for task_id, crewvia_status, _bb, expected, marker in STATUS_ROWS:
        node = nodes[f"{MISSION}:{task_id}"]
        assert node["status"] == expected, (
            f"{crewvia_status} → {node['status']} (期待: {expected})"
        )
        if marker:
            assert node["title"].startswith(marker), (
                f"{crewvia_status} の title に {marker} が無い: {node['title']!r}"
            )


def test_human_wait_is_distinguishable_from_dependency_wait_and_blocked(sandbox):
    """(a) 依存待ち / (b) 人間の判断待ち / (c) 明示的な停止 が見分けられること。"""
    for row in STATUS_ROWS:
        sandbox.add_task(row[0], row[1], row[2])
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())

    dependency_wait = nodes[f"{MISSION}:t013"]   # (a)
    human_wait = nodes[f"{MISSION}:t009"]        # (b)
    explicit_block = nodes[f"{MISSION}:t008"]    # (c)

    rendered = {
        (dependency_wait["status"], dependency_wait["title"][:6]),
        (human_wait["status"], human_wait["title"][:6]),
        (explicit_block["status"], explicit_block["title"][:6]),
    }
    assert len(rendered) == 3, f"3 つが見分けられない: {rendered}"
    assert dependency_wait["status"] == "waiting"
    assert human_wait["status"] == "blocked"
    assert explicit_block["status"] == "blocked"


def test_failed_dependency_does_not_make_downstream_look_blocked(sandbox):
    """QA FAIL 直後、crewvia が実際に dispatch する task は READY に見えること。

    ここが赤いままだと、このミッションのゴール「並列実行可能なタスクが見える」が
    まさにそれを見たい瞬間にだけ逆を表示する。
    """
    sandbox.add_task("t001", "failed", [])
    sandbox.add_task("t002", "pending", ["t001"])
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())
    assert nodes[f"{MISSION}:t002"]["status"] == "ready"

    # 同じ規則で pull が実際に割り当てることを突き合わせる (対照)
    r = sandbox.run("pull", "--agent", "Ren", "--skills", "code")
    assert r.returncode == 0, r.stderr
    assert '"id": "t002"' in r.stdout


def test_corrupted_task_is_surfaced_and_does_not_abort_generation(sandbox):
    sandbox.add_task("t001", "pending", [])
    (sandbox.queue / "missions" / MISSION / "tasks" / "t002.md").write_text(
        "id: t002\nthis file has no frontmatter delimiter\n"
    )
    sandbox.add_task("t003", "done", [])
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())
    assert set(nodes) == {f"{MISSION}:t001", f"{MISSION}:t002", f"{MISSION}:t003"}
    assert nodes[f"{MISSION}:t002"]["status"] == "failed"
    assert "破損" in nodes[f"{MISSION}:t002"]["title"]


# --- id の修飾 / mission またぎ ----------------------------------------------

def test_ids_are_qualified_per_mission(sandbox):
    sandbox.add_task("t001", "done", [])
    sandbox.add_task("t002", "pending", ["t001"])
    sandbox.add_mission("m-beta")
    sandbox.add_task("t001", "pending", [], mission="m-beta")
    sandbox.add_task("t002", "pending", ["t001"], mission="m-beta")

    assert sandbox.run("task-graph").returncode == 0
    graph = sandbox.read_graph()
    ids = [t["id"] for t in graph["tasks"]]
    assert len(ids) == len(set(ids)), f"id が衝突している: {ids}"
    assert set(ids) == {
        "m-alpha:t001", "m-alpha:t002", "m-beta:t001", "m-beta:t002",
    }
    nodes = _by_id(graph)
    assert nodes["m-alpha:t002"]["depends_on"] == ["m-alpha:t001"]
    assert nodes["m-beta:t002"]["depends_on"] == ["m-beta:t001"]
    # m-beta:t001 は pending・依存なし → READY、m-alpha:t002 は依存 done → READY
    assert nodes["m-beta:t002"]["status"] == "waiting"
    assert nodes["m-alpha:t002"]["status"] == "ready"


def test_dangling_dependency_is_visible_but_not_emitted(sandbox):
    sandbox.add_task("t001", "pending", ["t404"])
    assert sandbox.run("task-graph").returncode == 0
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert "depends_on" not in node or node["depends_on"] == []
    assert "t404" in node["title"]
    assert node["status"] == "waiting"


# --- pane_match -------------------------------------------------------------

def test_pane_match_points_at_the_worker_pane(sandbox):
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    sandbox.add_task("t002", "done", [], worker="Ren")
    sandbox.add_task("t003", "pending", [], worker="null")
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())
    assert nodes[f"{MISSION}:t001"]["pane_match"] == "Ren-worker"
    # 完了済みの worker 欄は履歴であって、今そのペインが居る場所ではない
    assert "pane_match" not in nodes[f"{MISSION}:t002"]
    # `worker: null` が文字列 "null" として入る既知の事故を拾わない
    assert "pane_match" not in nodes[f"{MISSION}:t003"]


# --- plan.sh への接続 --------------------------------------------------------

def _mtime(path: pathlib.Path):
    return path.stat().st_mtime_ns if path.exists() else None


def test_mutating_subcommand_refreshes_and_readonly_does_not(sandbox):
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0
    before = _mtime(sandbox.graph)
    assert before is not None

    time.sleep(0.01)
    r = sandbox.run("status")
    assert r.returncode == 0, r.stderr
    assert _mtime(sandbox.graph) == before, "status (読むだけ) が生成を呼んでいる"

    time.sleep(0.01)
    r = sandbox.run("update", "t001", "--priority", "high")
    assert r.returncode == 0, r.stderr
    assert _mtime(sandbox.graph) != before, "update (queue を書き換える) が生成を呼んでいない"


def test_pull_and_done_refresh_the_graph(sandbox):
    sandbox.add_task("t001", "pending", [])
    r = sandbox.run("pull", "--agent", "Ren", "--skills", "code")
    assert r.returncode == 0, r.stderr
    nodes = _by_id(sandbox.read_graph())
    assert nodes[f"{MISSION}:t001"]["status"] == "running"
    assert nodes[f"{MISSION}:t001"]["pane_match"] == "Ren-worker"

    r = sandbox.run("done", "t001", "done: https://example.invalid/pr/1")
    assert r.returncode == 0, r.stderr
    nodes = _by_id(sandbox.read_graph())
    assert nodes[f"{MISSION}:t001"]["status"] == "done"


def test_lock_busy_retire_writes_nothing(sandbox):
    """`retire --no-wait` がロックを取れなかった経路では生成を呼ばないこと。

    watchdog が監視ループの中から同期で叩く経路なので、何も書いていない実行の
    あとに全 mission を走査し直すのは、混んでいる瞬間に足す純粋な無駄。
    """
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0
    before = _mtime(sandbox.graph)

    holder = subprocess.Popen(
        [
            "python3", "-c",
            "import fcntl,sys,time;"
            "f=open(sys.argv[1],'a+');fcntl.flock(f,fcntl.LOCK_EX);"
            "print('held',flush=True);time.sleep(10)",
            str(sandbox.queue / ".lock"),
        ],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        time.sleep(0.01)
        r = sandbox.run(
            "retire", "t001", "--agent", "Ren",
            "--started-at", "2026-01-01T00:00:00.000000Z", "--no-wait",
        )
        assert r.returncode == LOCK_BUSY, (r.returncode, r.stdout, r.stderr)
        assert _mtime(sandbox.graph) == before, "ロックを取れなかった実行が生成を呼んでいる"
    finally:
        holder.kill()
        holder.wait()


# --- 停止スイッチ / 失敗の封じ込め --------------------------------------------

def test_kill_switch_writes_nothing_and_logs_nothing(sandbox):
    sandbox.add_task("t001", "pending", [])
    r = sandbox.run("update", "t001", "--priority", "high",
                    env=sandbox.env(CREWVIA_TASK_GRAPH="0"))
    assert r.returncode == 0, r.stderr
    assert not sandbox.graph.exists(), "停止中なのにファイルが書かれた"
    assert "task-graph" not in r.stderr, f"停止中にログが出ている: {r.stderr!r}"
    assert "task-graph" not in r.stdout, f"停止中にログが出ている: {r.stdout!r}"


def test_generation_failure_does_not_change_exit_code(sandbox):
    """出力先が書けなくても plan.sh は成功し、失敗は 1 行だけ残ること。"""
    sandbox.add_task("t001", "pending", [])
    blocker = sandbox.root / "blocker"
    blocker.write_text("not a directory\n")
    env = sandbox.env(CREWVIA_TASK_GRAPH_FILE=str(blocker / "tasks.json"))

    r = sandbox.run("update", "t001", "--priority", "high", env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "task-graph" in r.stderr, f"失敗が黙って捨てられている: {r.stderr!r}"

    # queue 側の変更は通常どおり適用されている
    body = (sandbox.queue / "missions" / MISSION / "tasks" / "t001.md").read_text()
    assert "priority: high" in body


def test_output_follows_crewvia_repo_root_not_script_location(sandbox, tmp_path):
    """worktree 側の plan.sh を叩いても、生成物は本体の registry に出ること。

    ここが REPO_ROOT (= スクリプトの位置) に落ちると、Director が開いている
    ファイルは Worker の pull / done では一切更新されない。
    """
    worktree = tmp_path / "wt"
    (worktree / "scripts").mkdir(parents=True)
    shutil.copy2(PLAN_SH, worktree / "scripts" / "plan.sh")

    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0
    before_mtime, before_bytes = _mtime(sandbox.graph), sandbox.graph.read_bytes()

    # worktree 側の plan.sh で queue を書き換える
    time.sleep(0.01)
    sandbox.add_task("t002", "in_progress", [], worker="Ren")
    r = sandbox.run("update", "t001", "--priority", "high",
                    script=worktree / "scripts" / "plan.sh")
    assert r.returncode == 0, r.stderr
    assert not (worktree / "registry" / "task-graph" / "tasks.json").exists(), \
        "worktree 側に出てしまっている"
    # 本体側が mtime と中身の両方で更新されていること
    assert _mtime(sandbox.graph) != before_mtime, "本体側の mtime が進んでいない"
    assert sandbox.graph.read_bytes() != before_bytes, "本体側の中身が更新されていない"
    assert f"{MISSION}:t002" in _by_id(sandbox.read_graph())


def test_foreign_queue_does_not_overwrite_the_repo_graph(sandbox, tmp_path):
    """`CREWVIA_QUEUE` が `<root>/queue` でない実行は本体の生成物に触らないこと。"""
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0
    before = sandbox.graph.read_bytes()

    foreign = tmp_path / "foreign-queue"
    (foreign / "missions").mkdir(parents=True)
    (foreign / "archive").mkdir(parents=True)
    (foreign / "state.yaml").write_text("active_missions: []\ndefault_mission: null\n")
    r = sandbox.run("init", "foreign-mission", env=sandbox.env(CREWVIA_QUEUE=str(foreign)))
    assert r.returncode == 0, r.stderr
    assert sandbox.graph.read_bytes() == before, "別のキューが本体の生成物を上書きした"


def test_generated_json_shape_matches_the_plugin_contract(sandbox):
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0
    graph = sandbox.read_graph()
    assert set(graph) == {"title", "tasks"}
    assert isinstance(graph["title"], str) and graph["title"]
    allowed_keys = {"id", "title", "depends_on", "status", "pane_match"}
    allowed_status = {"done", "running", "blocked", "ready", "waiting", "failed"}
    for node in graph["tasks"]:
        assert set(node) <= allowed_keys, f"未知のキー: {set(node) - allowed_keys}"
        assert node["status"] in allowed_status, node["status"]
