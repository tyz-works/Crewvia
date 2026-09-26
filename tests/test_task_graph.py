"""queue → herdr-task-graph の tasks.json 生成を固定する。

このファイルが守りたいのは 3 つある。

1. **「依存が満たされた」の定義が 1 箇所しか無いこと。**
   crewvia の pull は `cancelled` の依存を *満たされた扱い* にし、`failed` の依存は
   Director が `release-dep` するまで *保留* にする (t007)。DAG 側がこの規則を
   自前で持つと、QA が FAIL した直後 —— 「次に何が動けるのか」を最も知りたい
   瞬間 —— にだけ、pull が拒否する task を READY と表示する (あるいはその逆)。
   規則は `lib_dep_rules.card_dependencies()` ただ 1 つから来なければならない。

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
import itertools
import json
import os
import pathlib
import re
import fcntl
import shutil
import socket
import subprocess
import sys
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

DEP_RULES_PY = REPO_ROOT / "scripts" / "lib_dep_rules.py"


def _dep_rules():
    """規則の本体モジュールを読み込む。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("lib_dep_rules", DEP_RULES_PY)
    assert spec and spec.loader, f"{DEP_RULES_PY} を読めない"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unmet_dependency_rule_exists_as_one_helper():
    """依存判定の本体が scripts/lib_dep_rules.py にあること。

    規則を読む主体は 3 つある (plan.sh pull / plan.sh task-graph /
    dispatcher.sh)。置き場が 1 つでなくなると、ズレは QA FAIL の直後だけ、
    つまり誰も疑わない瞬間に現れる。
    """
    mod = _dep_rules()
    assert callable(mod.unmet_dependencies)
    assert mod.DEAD_DEP_STATUSES, "DEAD_DEP_STATUSES が空"


def test_plan_sh_takes_the_rule_from_the_module_instead_of_defining_it(tree):
    """plan.sh が規則を自前で定義し直していないこと。"""
    assert "unmet_dependencies" not in _functions(tree), (
        "plan.sh が unmet_dependencies() を自前で定義している。"
        "規則は scripts/lib_dep_rules.py から取ること"
    )
    src = _plan_py_source()
    assert "lib_dep_rules" in src, "plan.sh が lib_dep_rules を読んでいない"
    assert "unmet_dependencies = " in src, (
        "plan.sh の名前空間に unmet_dependencies が束縛されていない"
    )


#: 規則のコピーが残っていないかを見に行く範囲。
_RULE_SCAN_GLOBS = ("scripts/*.sh", "scripts/*.py", "hooks/*.sh", "tests/*.py")


def test_no_script_keeps_its_own_copy_of_the_rule():
    """DEAD_DEP_STATUSES の中身を直書きした箇所が、本体以外に無いこと。

    dispatcher.sh には PR #212 のあとも同じ規則の独自コピーが残っていた
    (QA t002 の指摘 F-2b)。「今は一致している」は、片方だけ直せる形が残って
    いるかぎり保証ではない。テスト側の写し (「dispatcher.sh と同じロジック」)
    も同罪で、そちらは *本物を直しても緑のまま* になるぶん質が悪い。

    探す文字列は本体の DEAD_DEP_STATUSES から組み立てる。このテスト自身に
    リテラルを書かないので、規則の中身が変わっても探し先は自動で追従する。
    """
    mod = _dep_rules()
    dead = tuple(mod.DEAD_DEP_STATUSES) + tuple(mod.HELD_DEP_STATUSES)
    # 「終わらないと確定した status を並べたタプル」は、順序を入れ替えても同じコピー。
    needles = {", ".join(repr(s) for s in order)
               for order in itertools.permutations(dead)}

    offenders = []
    for glob in _RULE_SCAN_GLOBS:
        for path in sorted(REPO_ROOT.glob(glob)):
            if path.resolve() == DEP_RULES_PY.resolve():
                continue
            text = path.read_text(errors="replace")
            if any(needle in text for needle in needles if "," in needle):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, (
        f"依存規則のコピーが残っている: {offenders} — "
        f"scripts/lib_dep_rules.py の card_dependencies() を呼ぶこと"
    )


def test_dispatcher_uses_the_shared_rule():
    """dispatcher.sh が規則を import して呼んでいること。"""
    src = (REPO_ROOT / "scripts" / "dispatcher.sh").read_text()
    assert "from lib_dep_rules import card_dependencies" in src, (
        "dispatcher.sh が共有の依存規則を import していない"
    )
    assert "return card_dependencies(" in src, (
        "dispatcher.sh の dependency_gate() が card_dependencies() を呼んでいない"
    )
    assert "dependency_gate(slug, meta," in src, (
        "dispatch() が dependency_gate() を通していない"
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
        "card_dependencies() を呼ぶ形に寄せること"
    )
    assert "card_dependencies" in src, "cmd_pull が card_dependencies() を使っていない"


def test_task_graph_uses_the_same_helper(tree):
    """生成側も同じヘルパーを使うこと。"""
    funcs = _functions(tree)
    assert "build_task_graph" in funcs, "build_task_graph() が無い"
    src = ast.unparse(funcs["build_task_graph"])
    assert "card_dependencies" in src, (
        "build_task_graph() が card_dependencies() を使っていない。"
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
    # 依存が failed → Director の判断待ち。pull / dispatch は拒否するので READY と
    # 出してはいけない。plugin に「判断待ち」は無いので blocked に畳んで印で見分ける。
    ("t014", "pending", ["t006"], "blocked", "[保留"),
    # `cancelled` は DEAD_DEP_STATUSES 側の終端 (Director 自身の判断)。`blocked` に
    # 畳むと「依存先は blocked なのに下流は READY」という、依存規則と食い違う画面になる。
    ("t015", "cancelled", [], "failed", "[中止]"),
    ("t016", "pending", ["t015"], "ready", None),    # 依存が cancelled → dispatch する
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
    # lib_dep_rules.py / lib_task_cards.py は必須 (plan.sh が起動時に読む、
    # 依存規則と task カード読み取りの本体。どちらもフォールバックを持たない)。
    # 残りは、その subcommand を使うときだけ要る補助。
    # lib_mux.py / lib_daemon_state.py は pane_id を解決するときだけ遅延で読まれる
    # (`task_graph_pane_id`)。
    for extra in ("lib_dep_rules.py", "lib_task_cards.py",
                  "lib_registry.py", "lint_plan.py",
                  "lib_mux.py", "lib_daemon_state.py"):
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

        def assign(self, worker, task_id, mission=MISSION):
            """`plan.sh pull` が公開するのと同じ assignment を置く。

            pane_match は card の `worker` 欄だけでは決まらない (名前は使い
            回されるので、履歴の欄から「今どのペインに居るか」は言えない)。
            「いま公開されている assignment がこの task を指している」が
            もう一方の条件なので、fixture でも同じ事実を置く。
            """
            adir = queue / "assignments"
            adir.mkdir(parents=True, exist_ok=True)
            (adir / worker).write_text(f"{mission}:{task_id}\n")

        def record_pane(self, worker, pane_id="w1:p9", *, backend="herdr",
                        generation=None, body=None):
            """`start.sh` が Worker 起動時に書く spawn 記録を置く。

            既定の `generation` は **生きているプロセス** (この pytest 自身) の
            `<pid>:<starttime>` — herdr の server の世代が持つのと同じ形。
            `body` を渡すと、その文字列をそのまま書く (壊れた記録の再現用)。
            """
            path = _mux().pane_record_path(f"{worker}-worker", repo_root=root)
            path.parent.mkdir(parents=True, exist_ok=True)
            if body is not None:
                path.write_text(body)
                return path
            record = {
                "handle": pane_id, "tab_id": pane_id, "pane_id": pane_id,
                "backend": backend,
                "server": {"endpoint": "/nonexistent/herdr.sock",
                           "generation": generation or _live_generation()},
            }
            path.write_text(json.dumps(record))
            return path

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


def _mux():
    """記録のパスを本番と同じ規則 (`CREWVIA_MUX_PANE_PREFIX` 込み) で引く。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import lib_mux
    return lib_mux


def _generation_of(pid: int) -> str:
    """herdr の server の世代と同じ形: `<pid>:<starttime>`。"""
    stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    return f"{pid}:{stat[stat.rindex(')') + 2:].split()[19]}"


def _live_generation() -> str:
    return _generation_of(os.getpid())


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


def test_failed_dependency_shows_downstream_as_held_not_ready(sandbox):
    """QA FAIL 直後、pull が拒否する task は READY に見えないこと (t007)。

    以前は failed の依存を「満たされた」扱いにして READY と出し、実際に review task
    が自動で進んだ。表示 (この DAG) と pull の可否が同じ規則から来ること。
    詳細な突き合わせは tests/test_failed_dependency_hold.py。
    """
    sandbox.add_task("t001", "failed", [])
    sandbox.add_task("t002", "pending", ["t001"])
    assert sandbox.run("task-graph").returncode == 0
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t002"]
    assert node["status"] == "blocked"
    assert "[保留: t001 が failed]" in node["title"]

    # 同じ規則で pull が実際に拒否することを突き合わせる (対照)
    r = sandbox.run("pull", "--agent", "Ren", "--skills", "code")
    assert '"id": "t002"' not in r.stdout, (r.stdout, r.stderr)
    assert "release-dep" in (r.stdout + r.stderr)

    # Director が解除したら、DAG も pull も同時に進める側へ変わる
    assert sandbox.run("release-dep", "t002").returncode == 0
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t002"]
    assert node["status"] == "ready"
    r = sandbox.run("pull", "--agent", "Ren", "--skills", "code")
    assert r.returncode == 0 and '"id": "t002"' in r.stdout


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
    sandbox.assign("Ren", "t001")
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())
    assert nodes[f"{MISSION}:t001"]["pane_match"] == "Ren-worker"
    # 完了済みの worker 欄は履歴であって、今そのペインが居る場所ではない
    assert "pane_match" not in nodes[f"{MISSION}:t002"]
    # `worker: null` が文字列 "null" として入る既知の事故を拾わない
    assert "pane_match" not in nodes[f"{MISSION}:t003"]


#: 「終わったのに worker 欄が残る」status。cmd_fail は completion を記録しつつ
#: worker を残す (撤去するのは assignment だけ) ので、どれも TERMINAL_STATUSES
#: には入らないまま Worker 名を持ち続ける。
FINISHED_BUT_KEEPS_WORKER = ["failed", "cancelled", "verification_failed", "corrupted"]


@pytest.mark.parametrize("status", FINISHED_BUT_KEEPS_WORKER)
def test_a_finished_task_never_points_at_a_pane(sandbox, status):
    """終了した task の worker 欄は、生きたペインの宛先ではない。

    crewvia は Worker 名を使い回すので、Ren が次の task に移ったあと、この
    node は **無関係な task のペイン** を指す。`plan.sh fail` が AGENT_NAME
    無しで呼ばれると assignment すら残るので、status 側でも必ず弾く。
    """
    sandbox.add_task("t001", status, [], worker="Ren")
    sandbox.assign("Ren", "t001")  # 撤去され損ねた assignment
    assert sandbox.run("task-graph").returncode == 0
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert "pane_match" not in node, (
        f"終了した task ({status}) に pane_match が付いている: "
        f"{node.get('pane_match')!r}"
    )


def test_pane_match_follows_the_live_assignment_not_the_card(sandbox):
    """名前の使い回し — assignment が別の task を指していれば出さない。

    Ren は前の card (t001) の worker 欄に名前を残したまま、今は t002 に就いて
    いる。card だけを根拠にすると、t001 の node が「今の Ren のペイン」を
    指してしまう。
    """
    sandbox.add_task("t001", "needs_director", [], worker="Ren")
    sandbox.add_task("t002", "in_progress", [], worker="Ren")
    sandbox.assign("Ren", "t002")
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())
    assert "pane_match" not in nodes[f"{MISSION}:t001"], (
        "assignment が指していない task に pane_match が付いている: "
        f"{nodes[f'{MISSION}:t001'].get('pane_match')!r}"
    )
    assert nodes[f"{MISSION}:t002"]["pane_match"] == "Ren-worker"


def test_a_waiting_worker_still_points_at_its_pane(sandbox):
    """判断待ち・検証待ちは「終わった」ではない — ペインはまだそこに居る。

    needs_director は Director が **いちばんペインに飛びたい** 状態なので、
    終了状態を弾くついでに巻き添えで消していないことを対照で見る。
    """
    for i, status in enumerate(
        ["in_progress", "verifying", "ready_for_verification",
         "needs_director", "needs_human_review"],
        start=1,
    ):
        task_id = f"t00{i}"
        sandbox.add_task(task_id, status, [], worker=f"W{i}")
        sandbox.assign(f"W{i}", task_id)
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())
    for i in range(1, 6):
        assert nodes[f"{MISSION}:t00{i}"]["pane_match"] == f"W{i}-worker"


def test_a_worker_parked_on_needs_director_points_at_its_pane_without_an_assignment(sandbox):
    """`plan.sh needs-director` は assignment を外す (t001 / backlog #13)。

    それでも判断待ちの node は Director が **いちばんペインに飛びたい** 状態のまま。
    「assignment が指している」を要求し続けると、実運用 (assignment が無い) では
    この node だけ pane_match が出ない —— 上のテストは fixture が assignment を
    置いているので、この回帰を見逃す。
    """
    sandbox.add_task("t001", "needs_director", [], worker="Ren")
    sandbox.record_pane("Ren", "wP:p80")
    assert sandbox.run("task-graph").returncode == 0
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert node["pane_match"] == "Ren-worker"
    assert node["pane_id"] == "wP:p80"


@pytest.mark.parametrize(
    "status", ["in_progress", "verifying", "ready_for_verification", "needs_human_review"])
def test_no_assignment_is_not_enough_for_the_other_pane_statuses(sandbox, status):
    """assignment の不在を許すのは needs_director だけ。ほかの status は従来どおり
    「公開中の assignment がこの task を指している」を要求する (名前の使い回しの防御)。"""
    sandbox.add_task("t001", status, [], worker="Ren")
    assert sandbox.run("task-graph").returncode == 0
    assert "pane_match" not in _by_id(sandbox.read_graph())[f"{MISSION}:t001"]


def test_an_unreadable_assignment_does_not_count_as_absent_for_needs_director(sandbox):
    """「無い」(ENOENT) と「読めない」は別。読めない assignment は、判断待ちでも出さない側に倒す。"""
    sandbox.add_task("t001", "needs_director", [], worker="Ren")
    (sandbox.queue / "assignments" / "Ren").mkdir(parents=True)     # 通常ファイルでない
    assert sandbox.run("task-graph").returncode == 0
    assert "pane_match" not in _by_id(sandbox.read_graph())[f"{MISSION}:t001"]


def test_needs_director_through_the_real_plan_sh_keeps_the_pane_link(sandbox):
    """実 plan.sh の `needs-director` を通した後でも、node が Worker のペインを指す。"""
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    sandbox.assign("Ren", "t001")
    sandbox.record_pane("Ren", "wP:p81")
    r = sandbox.run("needs-director", "t001", "NEEDS FIX: fixture", "--mission", MISSION,
                    env=sandbox.env(AGENT_NAME="Ren"))
    assert r.returncode == 0, r.stderr
    assert not (sandbox.queue / "assignments" / "Ren").exists(), "前提: assignment は外れている"
    assert sandbox.run("task-graph").returncode == 0
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert node["pane_match"] == "Ren-worker"
    assert node["pane_id"] == "wP:p81"


# --- label (P-2) / pane_id (P-3) -----------------------------------------------

def test_every_node_carries_a_short_label_and_keeps_its_qualified_id(sandbox):
    """`label` は mission を落とした task id。`id` は一意性と依存解決のために残す。"""
    sandbox.add_task("t001", "done", [])
    sandbox.add_task("t002", "pending", ["t001"])
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())
    assert set(nodes) == {f"{MISSION}:t001", f"{MISSION}:t002"}
    assert nodes[f"{MISSION}:t001"]["label"] == "t001"
    assert nodes[f"{MISSION}:t002"]["label"] == "t002"
    assert nodes[f"{MISSION}:t002"]["depends_on"] == [f"{MISSION}:t001"]


def test_every_node_carries_its_mission_slug_as_group(sandbox):
    """複数 mission を並べると `label` (`tNNN`) は重複する。`group` が区別を担う。"""
    sandbox.add_task("t001", "done", [])
    sandbox.add_mission("m-beta")
    sandbox.add_task("t001", "pending", [], mission="m-beta")
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())
    assert nodes[f"{MISSION}:t001"]["group"] == MISSION
    assert nodes["m-beta:t001"]["group"] == "m-beta"
    # 対照: 同じ label でも group で区別できる (これが無いと画面で見分けがつかない)
    assert nodes[f"{MISSION}:t001"]["label"] == nodes["m-beta:t001"]["label"] == "t001"


def test_the_placeholder_has_no_group_because_it_belongs_to_no_mission(sandbox):
    """task 0 件の placeholder は mission に属さないので `group` を持たない。"""
    assert sandbox.run("task-graph").returncode == 0
    (node,) = sandbox.read_graph()["tasks"]
    assert "表示する task なし" in node["title"]
    assert "group" not in node


def test_pane_id_is_written_from_the_spawn_record(sandbox):
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    sandbox.assign("Ren", "t001")
    sandbox.record_pane("Ren", "wP:p80")
    r = sandbox.run("task-graph")
    assert r.returncode == 0, r.stderr
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert node["pane_id"] == "wP:p80"
    assert node["pane_match"] == "Ren-worker"   # 従来動作は残す


def _dead_generation() -> str:
    """終わって回収済みのプロセスの世代 — もう /proc に無い。"""
    p = subprocess.Popen(["true"])
    p.wait()
    return _generation_of_dead(p.pid)


def _generation_of_dead(pid: int) -> str:
    return f"{pid}:123456789"


#: (id, 記録の置き方, 理由)。どれも `pane_id` を書かず、`pane_match` は残る。
UNUSABLE_RECORDS = [
    ("no-record", lambda sb: None, "記録が無い"),
    ("broken-json", lambda sb: sb.record_pane("Ren", body="{not json"), "壊れた JSON"),
    ("empty-file", lambda sb: sb.record_pane("Ren", body=""), "書き込み途中の空ファイル"),
    ("json-array", lambda sb: sb.record_pane("Ren", body="[1, 2]"), "object でない"),
    ("tmux-backend", lambda sb: sb.record_pane("Ren", backend="tmux"), "herdr でない"),
    ("empty-pane-id", lambda sb: sb.record_pane("Ren", ""), "pane_id が空"),
    ("dead-server", lambda sb: sb.record_pane("Ren", generation=_dead_generation()),
     "記録の server がもう居ない (再起動後の古い記録)"),
    ("pid-reused",
     lambda sb: sb.record_pane(
         "Ren", generation=f"{os.getpid()}:{int(_live_generation().split(':')[1]) + 1}"),
     "pid は生きているが starttime が違う (pid の使い回し)"),
    ("malformed-generation", lambda sb: sb.record_pane("Ren", generation="abc"),
     "世代の形が違う"),
    ("pid-not-digits", lambda sb: sb.record_pane("Ren", generation="../1:2"),
     "世代に pid でないものが入っている"),
]


@pytest.mark.parametrize("place", [u[1] for u in UNUSABLE_RECORDS],
                         ids=[u[0] for u in UNUSABLE_RECORDS])
def test_pane_id_is_omitted_when_the_record_cannot_be_trusted(sandbox, place):
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    sandbox.assign("Ren", "t001")
    place(sandbox)
    r = sandbox.run("task-graph")
    assert r.returncode == 0, r.stderr
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert "pane_id" not in node, node
    assert node["pane_match"] == "Ren-worker"       # 生成は落ちず、従来動作が残る


def test_pane_id_needs_the_live_assignment_and_a_live_status(sandbox):
    """pane_match と同じ AND: 記録があっても、就いていない Worker には書かない。"""
    sandbox.record_pane("Ren", "wP:p80")
    sandbox.record_pane("Old", "wP:p81")
    # 名前の使い回し: assignment は別の task を指している
    sandbox.add_task("t001", "needs_director", [], worker="Ren")
    sandbox.add_task("t002", "in_progress", [], worker="Ren")
    sandbox.assign("Ren", "t002")
    # 終わった task の worker 欄 (履歴)
    sandbox.add_task("t003", "failed", [], worker="Old")
    sandbox.assign("Old", "t003")
    assert sandbox.run("task-graph").returncode == 0
    nodes = _by_id(sandbox.read_graph())
    assert "pane_id" not in nodes[f"{MISSION}:t001"]
    assert nodes[f"{MISSION}:t002"]["pane_id"] == "wP:p80"
    assert "pane_id" not in nodes[f"{MISSION}:t003"]


def test_pane_id_is_never_written_for_a_paneless_executor(sandbox):
    """codex-review はペインを持たない。同名の記録が残っていても書かない。"""
    task = sandbox.queue / "missions" / MISSION / "tasks" / "t001.md"
    sandbox.add_task("t001", "in_progress", [], worker="Kai-codex")
    task.write_text(task.read_text().replace("skills: [code]", "skills: [codex-review]"))
    sandbox.assign("Kai-codex", "t001")
    sandbox.record_pane("Kai-codex", "wP:p1")
    assert sandbox.run("task-graph").returncode == 0
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert "pane_id" not in node and "pane_match" not in node


def test_reading_the_record_takes_no_lock_and_never_touches_herdr(sandbox):
    """生成は `.records.lock` を待たず、herdr の socket に接続もしない。

    生成器は `retire --no-wait` (watchdog が同期で叩く) を含む全経路から呼ばれる。
    ロックを待つと Worker の生死を誰も見ていない時間ができ、herdr に触れると
    「plugin が無くても・herdr でなくても何も起きない」が崩れる。
    """
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    sandbox.assign("Ren", "t001")
    record = sandbox.record_pane("Ren", "wP:p80")

    lock = record.parent / ".records.lock"
    lock.touch()
    sock_path = sandbox.root / "h.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)
    server.setblocking(False)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)          # 書き手 (spawn) が握っている状態
        started = time.monotonic()
        r = sandbox.run("task-graph", env=sandbox.env(CREWVIA_HERDR_SOCK=str(sock_path)))
        elapsed = time.monotonic() - started
    try:
        server.accept()
        connected = True
    except BlockingIOError:
        connected = False
    finally:
        server.close()

    assert r.returncode == 0, r.stderr
    assert elapsed < 5, f"記録のロックを待っている ({elapsed:.1f}s)"
    assert not connected, "生成が herdr の socket に接続した"
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert node["pane_id"] == "wP:p80"


def test_a_missing_lib_mux_costs_the_pane_id_and_nothing_else(sandbox):
    """pane_id の解決が壊れても、図は描かれ、終了コードは変わらない。"""
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    sandbox.assign("Ren", "t001")
    sandbox.record_pane("Ren", "wP:p80")
    (sandbox.root / "scripts" / "lib_mux.py").unlink()
    r = sandbox.run("task-graph")
    assert r.returncode == 0, r.stderr
    node = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]
    assert "pane_id" not in node and node["pane_match"] == "Ren-worker"
    assert node["label"] == "t001"
    assert "pane_id を解決できない" in r.stderr   # 黙って落とさない


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
    # 本物の worktree には scripts/ が丸ごと在る。plan.sh は依存規則と task カードの
    # 読み取りを自分の側の scripts/ から読むので、ここでも一緒に置く。
    for _extra in ("lib_dep_rules.py", "lib_task_cards.py"):
        shutil.copy2(REPO_ROOT / "scripts" / _extra, worktree / "scripts" / _extra)

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
    allowed_keys = {"id", "label", "group", "title", "depends_on", "status",
                    "pane_match", "pane_id"}
    allowed_status = {"done", "running", "blocked", "ready", "waiting", "failed"}
    for node in graph["tasks"]:
        assert set(node) <= allowed_keys, f"未知のキー: {set(node) - allowed_keys}"
        assert node["status"] in allowed_status, node["status"]


# ---------------------------------------------------------------------------
# 4. 並行する publish — 古いスナップショットが新しい姿を巻き戻さないこと
# ---------------------------------------------------------------------------
#
# 原子的な置換が防ぐのは「半端な JSON を読まれること」だけで、「読むのが先・
# 書くのが後」の入れ替わりは防がない。そして巻き戻った表示は一瞬では消えない:
# 巻き戻される側が *最後の queue 変更* だった場合、次に誰かが queue を触るまで
# 誤った running が居座る。「次のコマンドが直す」は、次のコマンドがある場合の
# 話でしかない。

HARNESS = REPO_ROOT / "tests" / "task_graph_publisher_harness.py"

#: 「新しい側が先に publish し終える」ための猶予。直列化されていれば、この間
#: 新しい側はロック待ちのまま何も書かない (= 待ち切って先に進むのが正しい)。
NEWER_PUBLISHER_GRACE = 3.0


def _wait_for_file(path: pathlib.Path, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while not path.exists():
        assert time.time() < deadline, f"{what} が {timeout}s 以内に現れなかった"
        time.sleep(0.01)


def test_a_stale_snapshot_never_overwrites_a_newer_one(sandbox, tmp_path):
    """queue を先に読んだ publish が、あとから来ても新しい姿を上書きしないこと。

    harness 側 (古い読み取り) を publish の直前で止め、そのあいだに t001 を
    done にして通常の `plan.sh task-graph` (新しい読み取り) を走らせる。
    最後に harness を解放する — つまり **古い方が最後に書こうとする**。
    直列化されていれば、古い方は新しい方より先に書き終えているか、新しい方が
    そのあとで読み直すかのどちらかになり、最終形は done でなければならない。
    """
    sandbox.add_task("t001", "in_progress", [], worker="Ren")
    assert sandbox.run("task-graph").returncode == 0
    assert _by_id(sandbox.read_graph())[f"{MISSION}:t001"]["status"] == "running"

    reached, go = tmp_path / "reached", tmp_path / "go"
    plan = sandbox.root / "scripts" / "plan.sh"
    stale = subprocess.Popen(
        [sys.executable, str(HARNESS),
         "--plan", str(plan), "--queue", str(sandbox.queue),
         "--repo-root", str(sandbox.root),
         "--reached", str(reached), "--go", str(go)],
        env=sandbox.env(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    newer = None
    try:
        # 古い側が queue を読み終え、publish の直前で止まった
        _wait_for_file(reached, 10.0, "harness の到達印")

        # 世界が進む: t001 が終わり、新しい読み取りが publish しに来る
        sandbox.add_task("t001", "done", [])
        newer = subprocess.Popen(
            ["bash", str(plan), "task-graph"],
            env=sandbox.env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            newer.wait(timeout=NEWER_PUBLISHER_GRACE)
        except subprocess.TimeoutExpired:
            pass  # 直列化されていれば、ここでロック待ちしているのが正しい
    finally:
        go.write_text("go")
        out, err = stale.communicate(timeout=60)
        assert stale.returncode == 0, f"harness が失敗した: {err or out}"
        if newer is not None:
            n_out, n_err = newer.communicate(timeout=60)
            assert newer.returncode == 0, f"新しい側が失敗した: {n_err or n_out}"

    status = _by_id(sandbox.read_graph())[f"{MISSION}:t001"]["status"]
    assert status == "done", (
        "古いスナップショットが新しい姿を上書きした "
        f"(status={status!r}) — 次に queue を触る者が居なければ、この誤った "
        "running は無期限に残る"
    )


#: 要求者が「ロックを待ち切れなかった」に入るまでの上限。本番は 10 秒だが、
#: 待ち時間そのものは仕組みではないので harness 側で短くして演じさせる。
REQUESTER_LOCK_WAIT = 0.05

#: 要求者が要求を置く地点まで進むのを待つ時間。直っていれば要求者はここで
#: pending lock を待って止まる (= 帰ってこない) ので、終了は待てない。
REQUESTER_GRACE = 2.0


def _publisher(sandbox, tmp_path, *, gate, name, lock_wait=None):
    """harness を 1 つ起動する (本番の refresh_task_graph() をそのまま走らせる)。"""
    argv = [
        sys.executable, str(HARNESS),
        "--plan", str(sandbox.root / "scripts" / "plan.sh"),
        "--queue", str(sandbox.queue), "--repo-root", str(sandbox.root),
        "--gate", gate,
    ]
    if gate != "none":
        argv += ["--reached", str(tmp_path / f"{name}.reached"),
                 "--go", str(tmp_path / f"{name}.go")]
    if lock_wait is not None:
        argv += ["--lock-wait", str(lock_wait)]
    return subprocess.Popen(
        argv, env=sandbox.env(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )


def test_a_request_placed_at_the_moment_of_release_is_never_orphaned(sandbox, tmp_path):
    """解放の *直前* に置かれた読み直しの要求が、誰にも拾われずに残らないこと。

    取りこぼしはこの一瞬でしか起きない。保持者が「要求は無い」と確認したあと、
    まだロックを解放しきらないうちに、待ち切れなかった側が要求を置く。保持者は
    もう見に来ないし、要求者はもう待っていない。置かれた要求は次に誰かが queue
    を触るまで誰にも消費されず、**最後の queue 変更 (t002) が無期限に見えない
    まま残る** —— 「次のコマンドが直す」は、次のコマンドがある場合の話でしかない。

    直っていれば出口は 2 つしかなく、どちらかが必ず成立する:
      (a) 要求者が解放されたロックを取って自分で publish する
      (b) 保持者が要求を見て読み直す
    このシナリオが踏ませるのは (a) —— 保持者は確認と解放を一区間で終えるので、
    要求者は「確認済みで解放前」に要求を置けず、必ず解放後に置いて自分で publish
    することになる。
    """
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0
    assert f"{MISSION}:t002" not in _by_id(sandbox.read_graph())

    holder = _publisher(sandbox, tmp_path, gate="release", name="holder")
    requester = None
    try:
        # 保持者は publish を終え、「要求は無い」と確認し、解放の直前で止まった
        _wait_for_file(tmp_path / "holder.reached", 10.0, "保持者の解放直前の印")

        # 世界が進む。**これが最後の queue 変更** — 誰も拾わなければ永久に映らない
        sandbox.add_task("t002", "in_progress", [], worker="Ren")
        sandbox.assign("Ren", "t002")

        requester = _publisher(sandbox, tmp_path, gate="none", name="requester",
                               lock_wait=REQUESTER_LOCK_WAIT)
        try:
            # 直っていれば要求者はここで止まる (要求を置く区間が保持者と排他)。
            # 直っていなければ要求だけ置いて先に帰る — どちらでも次に進める。
            requester.wait(timeout=REQUESTER_GRACE)
        except subprocess.TimeoutExpired:
            pass
    finally:
        (tmp_path / "holder.go").write_text("go")
        h_out, h_err = holder.communicate(timeout=60)
        assert holder.returncode == 0, f"保持者が失敗した: {h_err or h_out}"
        if requester is not None:
            r_out, r_err = requester.communicate(timeout=60)
            assert requester.returncode == 0, f"要求者が失敗した: {r_err or r_out}"

    nodes = _by_id(sandbox.read_graph())
    assert f"{MISSION}:t002" in nodes, (
        "解放の直前に置かれた読み直しの要求が誰にも拾われていない — "
        f"最後の queue 変更 (t002) が publish されないまま残った: {sorted(nodes)}"
    )
    leftover = pathlib.Path(str(sandbox.graph) + ".pending")
    assert not leftover.exists(), (
        f"消費されないまま残った読み直しの要求がある: {leftover}"
    )


def test_the_holder_picks_up_a_request_placed_while_it_was_publishing(sandbox, tmp_path):
    """publish の最中に置かれた要求は、保持者が読み直して消費すること (出口 b)。

    要求者はロックを取れないまま帰るので、拾えるのは保持者しか居ない。保持者が
    「自分の読み取りより後に置かれた要求」を読み直しの合図として扱えていないと、
    ここで t002 が落ちる。
    """
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0

    holder = _publisher(sandbox, tmp_path, gate="publish", name="holder")
    requester = None
    try:
        # 保持者は queue を読み終え、生成物を書く直前で止まった
        _wait_for_file(tmp_path / "holder.reached", 10.0, "保持者の publish 直前の印")

        sandbox.add_task("t002", "in_progress", [], worker="Ren")
        sandbox.assign("Ren", "t002")

        requester = _publisher(sandbox, tmp_path, gate="none", name="requester",
                               lock_wait=REQUESTER_LOCK_WAIT)
        r_out, r_err = requester.communicate(timeout=60)
        assert requester.returncode == 0, f"要求者が失敗した: {r_err or r_out}"
        # 要求者は 1 バイトも書いていない (ロックを取れていないので)
        assert f"{MISSION}:t002" not in _by_id(sandbox.read_graph())
    finally:
        (tmp_path / "holder.go").write_text("go")
        h_out, h_err = holder.communicate(timeout=60)
        assert holder.returncode == 0, f"保持者が失敗した: {h_err or h_out}"

    nodes = _by_id(sandbox.read_graph())
    assert f"{MISSION}:t002" in nodes, (
        "publish 中に置かれた要求を保持者が読み直していない — "
        f"要求者の変更 (t002) が落ちた: {sorted(nodes)}"
    )
    assert not pathlib.Path(str(sandbox.graph) + ".pending").exists()


def test_the_release_is_decided_inside_the_pending_lock(tree):
    """「要求が無いことの確認」と「本ロックの解放」が同じ区間にあること。

    受け渡しが成立する根拠はこの入れ子だけである。解放が区間の外に出た瞬間、
    「確認済みで解放前」という中途半端な状態が外から観測できるようになり、
    そこに置かれた要求は誰にも拾われない。挙動テストはその一瞬を狙って開けて
    いるが、構造としても固定しておく (順序が崩れても、狙う一瞬は残るため)。
    """
    funcs = _functions(tree)
    assert "refresh_task_graph" in funcs
    inside = []
    for node in ast.walk(funcs["refresh_task_graph"]):
        if not isinstance(node, ast.With):
            continue
        if not any(isinstance(i.context_expr, ast.Call)
                   and _call_name(i.context_expr) == "task_graph_pending_lock"
                   for i in node.items):
            continue
        inside += [
            c for c in ast.walk(node)
            if isinstance(c, ast.Call) and _call_name(c) == "release_task_graph_lock"
        ]
    assert inside, (
        "release_task_graph_lock() が task_graph_pending_lock() の区間の中から "
        "呼ばれていない — 確認と解放のあいだに置かれた要求を誰も拾えなくなる"
    )


def test_an_unwritable_destination_is_reported_as_a_failure_not_as_contention(sandbox):
    """書き先が壊れているときの 1 行が、混雑ではなく失敗として出ること。

    直列化のロックは生成物と同じディレクトリに作るので、書き先が壊れていると
    「ロックを用意できない」が publish より先に起きる。これを「待ち切れなかった」
    と同じ扱いに畳むと、operator が受け取る唯一の 1 行が嘘の診断になり、
    存在しない混雑を追いかけることになる。
    """
    sandbox.add_task("t001", "pending", [])
    blocker = sandbox.root / "blocker"
    blocker.write_text("not a directory\n")
    env = sandbox.env(CREWVIA_TASK_GRAPH_FILE=str(blocker / "tasks.json"))

    r = sandbox.run("update", "t001", "--priority", "high", env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "生成に失敗しました" in r.stderr, (
        f"書き先の失敗が失敗として出ていない: {r.stderr!r}"
    )
    assert "待ち切れ" not in r.stderr, (
        f"書き先の失敗が publish の混雑として誤診されている: {r.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 5. 手動コマンドにも foreign-queue ガードが掛かること
# ---------------------------------------------------------------------------

def _foreign_queue(path: pathlib.Path, slug: str = "m-foreign") -> pathlib.Path:
    """本体の registry とは無関係な、隔離テスト用の queue を 1 つ作る。"""
    (path / "missions" / slug / "tasks").mkdir(parents=True)
    (path / "archive").mkdir(parents=True)
    (path / "missions" / slug / "mission.yaml").write_text(_mission_yaml(slug))
    (path / "missions" / slug / "tasks" / "t001.md").write_text(
        _task_md("t001", "pending", [])
    )
    (path / "state.yaml").write_text(
        f"active_missions:\n  - {slug}\ndefault_mission: {slug}\n"
    )
    return path


def test_manual_task_graph_refuses_a_foreign_queue(sandbox, tmp_path):
    """`CREWVIA_QUEUE` だけ別に向けた手動実行が、本体の生成物を上書きしないこと。

    自動経路 (maybe_refresh_task_graph) は既に守られているのに、手動の
    `plan.sh task-graph` だけ素通りしていた。隔離 QA は本体の queue を汚さない
    ために `CREWVIA_QUEUE` を付け替えて走るので、そこで 1 回叩かれるだけで
    Director が見ているグラフがテスト用の queue に化ける。
    """
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0
    before = sandbox.graph.read_bytes()

    foreign = _foreign_queue(tmp_path / "foreign-queue")
    r = sandbox.run("task-graph", env=sandbox.env(CREWVIA_QUEUE=str(foreign)))
    assert r.returncode != 0, "隔離 queue からの手動生成が黙って通った"
    assert sandbox.graph.read_bytes() == before, "隔離 queue が本体の生成物を上書きした"
    assert "CREWVIA_TASK_GRAPH_FILE" in r.stderr, (
        f"書き先の指定方法が案内されていない: {r.stderr!r}"
    )


def test_manual_task_graph_accepts_a_foreign_queue_with_an_explicit_destination(
    sandbox, tmp_path
):
    """書き先を明示すれば、隔離 queue からの手動生成は通ること。

    ガードが守るのは「本体の生成物」であって、隔離した実行そのものではない。
    ここが塞がると、QA が自分の queue のグラフを目で見る手段が無くなる。
    """
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0
    before = sandbox.graph.read_bytes()

    foreign = _foreign_queue(tmp_path / "foreign-queue")
    out = tmp_path / "explicit" / "tasks.json"
    r = sandbox.run(
        "task-graph",
        env=sandbox.env(CREWVIA_QUEUE=str(foreign), CREWVIA_TASK_GRAPH_FILE=str(out)),
    )
    assert r.returncode == 0, r.stderr
    assert "m-foreign:t001" in _by_id(json.loads(out.read_text()))
    assert sandbox.graph.read_bytes() == before, "本体の生成物が巻き込まれた"


# ---------------------------------------------------------------------------
# 印のロックを保持したまま止まっている実行が、plan.sh を止めないこと
# ---------------------------------------------------------------------------
#
# 印のロック (`<生成物>.pending.lock`) は、保持時間がファイル 1 つの読み書きと
# flock の解放だけの、非常に短いロックである。だからといって **期限なしで待つ**
# と、保持したまま生きて止まっている実行が 1 つ居るだけで、以降の queue 変更
# コマンドが全て無期限に待つ。可視化は付加機能なので、倒す先は「グラフが少し
# 古くなる」でなければならず、「plan.sh が待たされる」であってはならない。
#
# とくに `retire --no-wait` は watchdog が **同期で** 叩く。watchdog は Worker の
# 生死を見る唯一の主体なので、ここが止まると監視そのものが止まる。

#: ロックを握ったまま止まっている実行を演じる。引数は「握るロックのパス …,
#: 握り終えた印のパス」。**プロセスは生きたまま止まる** (死ぬと flock が外れて
#: しまい、再現したい状況にならない)。
_LOCK_STALLER_SRC = """
import fcntl, pathlib, sys, time
paths, ready = sys.argv[1:-1], pathlib.Path(sys.argv[-1])
held = []
for p in paths:
    f = open(p, 'a+')
    fcntl.flock(f, fcntl.LOCK_EX)
    held.append(f)
ready.write_text('held')
time.sleep(600)
"""

#: 止まっている実行が居るときに plan.sh が返ってくるまでの上限 (テスト側の判定)。
#: 本番の上限 (TASK_GRAPH_PENDING_LOCK_WAIT_SECONDS) より十分大きく取る —
#: ここで見たいのは「有限で返る」であって、秒数の当てっこではない。
PENDING_STALL_BUDGET = 25.0

#: watchdog が `retire --no-wait` を叩くときの subprocess タイムアウト。
#: plan.sh はこれより **内側** で返らなければ、監視を止めたことになる。
WATCHDOG_RETIRE_TIMEOUT = 30.0


def _stall_holding(tmp_path, *lock_paths, name="staller"):
    """指定のロックを握ったまま止まるプロセスを起こし、握り終えるまで待つ。"""
    ready = tmp_path / f"{name}.held"
    for p in lock_paths:
        pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _LOCK_STALLER_SRC, *map(str, lock_paths), str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _wait_for_file(ready, 10.0, f"{name} がロックを握った印")
    except BaseException:
        proc.kill()
        proc.wait(timeout=10)
        raise
    return proc


def test_a_stalled_pending_lock_does_not_block_the_publisher(sandbox, tmp_path):
    """印のロックを握ったまま止まっている実行が居ても、publish 側が返ること。

    ここで止まるのは **本ロックを取れた側** である。印のロックを期限なしで待つと、
    その実行は本ロックを握ったまま永久に止まる。つまり止まるのは 1 コマンドでは
    なく、**以降の全ての queue 変更コマンド** である (全員が本ロックの 10 秒を
    払ったうえで、同じ印のロックで無期限に詰まる)。

    直っていれば、上限を過ぎた時点で publish 側は手を引いて返る。**そのとき
    既にある要求の印を消さないこと** も併せて固定する: 消してよいのは「無い」と
    確認できたときだけで、確認できていない以上、消せば要求者の最後の変更が
    そのまま落ちる。残しておけば次に queue を触った実行が拾う。
    """
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0

    pending = pathlib.Path(str(sandbox.graph) + ".pending")
    pending_lock = pathlib.Path(str(sandbox.graph) + ".pending.lock")

    # 先に置かれていた要求。手を引くときに巻き添えで消えてはいけない。
    pending.write_text("999999 1\n")
    before = pending.read_text()

    staller = _stall_holding(tmp_path, pending_lock, name="pending-only")
    try:
        started = time.monotonic()
        r = subprocess.run(
            ["bash", str(sandbox.root / "scripts" / "plan.sh"),
             "update", "t001", "--priority", "high"],
            env=sandbox.env(), capture_output=True, text=True,
            timeout=PENDING_STALL_BUDGET,
        )
        elapsed = time.monotonic() - started
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"印のロックを握ったまま止まっている実行が居るだけで、plan.sh が "
            f"{PENDING_STALL_BUDGET}s 以内に返らなかった — 可視化のための "
            f"任意機能が本体を止めている"
        )
    finally:
        staller.kill()
        staller.wait(timeout=10)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert elapsed < PENDING_STALL_BUDGET, elapsed
    assert pending.exists(), (
        "手を引くときに、確認できていない要求の印を消してしまっている — "
        "要求者の最後の queue 変更がそのまま落ちる"
    )
    assert pending.read_text() == before, "印の中身が書き換わっている"
    assert r.stderr.strip(), "手を引いたことが 1 行も報告されていない"


def test_a_stalled_pending_lock_does_not_block_the_requester(sandbox, tmp_path):
    """本ロックも印のロックも握られたまま止まっているとき、要求者が返ること。

    要求者は本ロックを待ち切れずに引き返す側である。引き返す途中で要求の印を
    置きに行き、そこで印のロックを **期限なしで** 待つ。Codex が指したのは
    この経路で、本ロックの上限 (10 秒) を払い終えたあとに、上限の無い待ちが
    続いてしまう。
    """
    sandbox.add_task("t001", "pending", [])
    assert sandbox.run("task-graph").returncode == 0

    staller = _stall_holding(
        tmp_path,
        str(sandbox.graph) + ".lock",
        str(sandbox.graph) + ".pending.lock",
        name="both",
    )
    requester = _publisher(sandbox, tmp_path, gate="none", name="requester",
                           lock_wait=REQUESTER_LOCK_WAIT)
    try:
        try:
            out, err = requester.communicate(timeout=PENDING_STALL_BUDGET)
        except subprocess.TimeoutExpired:
            requester.kill()
            requester.communicate(timeout=10)
            pytest.fail(
                f"要求者が {PENDING_STALL_BUDGET}s 以内に返らなかった — "
                f"印のロックの待ちに上限が無い"
            )
    finally:
        staller.kill()
        staller.wait(timeout=10)

    assert requester.returncode == 0, f"要求者が失敗した: {err or out}"


def test_retire_no_wait_returns_inside_the_watchdogs_timeout(sandbox, tmp_path):
    """`retire --no-wait` が watchdog の 30 秒の内側で返ること (本番の定数で実測)。

    watchdog は Worker の生死を見る唯一の主体で、この呼び出しを **同期で** 行う。
    ここが subprocess タイムアウトまで持っていかれると、その間 **誰も Worker を
    見ていない**。だからこのシナリオだけは待ち時間を harness で短くせず、本番の
    上限 (本ロック + 印のロック) をそのまま払わせて測る。
    """
    sandbox.add_task("t001", "pending", [])
    r = sandbox.run("pull", "--agent", "Ren", "--skills", "code")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    card = (sandbox.queue / "missions" / MISSION / "tasks" / "t001.md").read_text()
    m = re.search(r"^started_at:\s*(\S+)\s*$", card, re.MULTILINE)
    assert m, f"pull が started_at を書いていない: {card!r}"
    generation = m.group(1).strip('"\'')

    staller = _stall_holding(
        tmp_path,
        str(sandbox.graph) + ".lock",
        str(sandbox.graph) + ".pending.lock",
        name="retire",
    )
    try:
        started = time.monotonic()
        r = subprocess.run(
            ["bash", str(sandbox.root / "scripts" / "plan.sh"),
             "retire", "t001", "--agent", "Ren",
             "--started-at", generation, "--no-wait"],
            env=sandbox.env(), capture_output=True, text=True,
            timeout=WATCHDOG_RETIRE_TIMEOUT,
        )
        elapsed = time.monotonic() - started
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"retire --no-wait が watchdog の {WATCHDOG_RETIRE_TIMEOUT}s の "
            f"タイムアウトまで返らなかった — その間 Worker の生死を誰も見ていない"
        )
    finally:
        staller.kill()
        staller.wait(timeout=10)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert elapsed < WATCHDOG_RETIRE_TIMEOUT, elapsed
    # 後始末そのものは、グラフの都合に一切引きずられずに成立していること。
    card = (sandbox.queue / "missions" / MISSION / "tasks" / "t001.md").read_text()
    assert re.search(r"^status:\s*pending\s*$", card, re.MULTILINE), card
    print(f"[evidence] retire --no-wait elapsed={elapsed:.2f}s "
          f"(watchdog timeout={WATCHDOG_RETIRE_TIMEOUT}s)")
