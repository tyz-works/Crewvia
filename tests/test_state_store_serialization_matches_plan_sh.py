"""lib_state_store の直列化は plan.sh の今の直列化と**バイト単位で同じ** (設計 §3.2 直列化の行)。

S3 で plan.sh がこの lib を使うまで、`dump_yaml` / `serialize_frontmatter` / `save_state` の規則は
plan.sh と lib の 2 か所にある (§14-7「同じ規則の二重実装」の一時的な例外)。二重になっている間は
このテストが止め具: plan.sh の関数を **AST で取り出して実行**し、同じ入力で出力が一致することを
固定する。あわせて本物の plan.sh が書いた card / mission / state を読み戻して再直列化しても
1 バイトも変わらないことを確かめる (S3 の QA (t013) の前倒しの一部)。
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import subprocess

import pytest

import state_store_scenarios as sc       # scripts/ を sys.path に足す
import lib_state_store as store
import lib_task_cards as cards
from fixture_tree import copy_plan_tree

PLAN_SH = sc.REPO_ROOT / "scripts" / "plan.sh"
WANTED_FUNCS = {"dump_yaml", "_dump_kv", "_dump_scalar", "_dump_inline", "serialize_frontmatter", "save_state"}
WANTED_ASSIGNS = {"_NEEDS_QUOTE", "TASK_META_KEY_ORDER", "MISSION_KEY_ORDER"}


def _plan_namespace():
    text = PLAN_SH.read_text()
    src = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL).group(1)
    tree = ast.parse(src)
    picked = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCS:
            picked.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in WANTED_ASSIGNS for t in node.targets):
            picked.append(node)
    found = {n.name for n in picked if isinstance(n, ast.FunctionDef)} | \
            {t.id for n in picked if isinstance(n, ast.Assign) for t in n.targets}
    assert found == WANTED_FUNCS | WANTED_ASSIGNS, f"plan.sh から取り出せなかった: {(WANTED_FUNCS | WANTED_ASSIGNS) - found}"
    written = []
    ns = {"re": re, "STATE_FILE": "state.yaml", "_atomic_write": lambda path, text: written.append(text),
          "written": written}
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(PLAN_SH), "exec"), ns)
    return ns


@pytest.fixture
def plan():
    # テストごとに新しい名前空間: plan.sh の dump_yaml は key_order を書き換える (1 回で終わる CLI では
    # 無害)。持ち越すと、前のケースの未知のキーが次のケースの並びに混ざる。
    return _plan_namespace()


CARD_CORPUS = [
    {"id": "t001", "title": "plain", "skills": ["code", "bash"], "status": "pending"},
    {"id": "t001", "title": 'colon: "quotes" and \\ backslash', "skills": [], "worker": None},
    {"id": "t001", "title": "multi\nline\r\nreason\n", "needs_director_reason": "a\nb"},
    {"id": "t001", "title": "true", "priority": "123", "worker": "null", "started_at": "yes"},
    {"id": "t001", "title": "日本語のタイトル: 混在", "blocked_by": ["t002", "t003"], "pr_number": 42},
    {"id": "t001", "title": "x", "rework_count": 0, "max_rework": 3, "no_pr_waiver": "理由 # hash"},
    {"id": "t001", "title": "x", "qa_checkpoints": ["a: b", "c"], "required_evidence": [1, True, None, "s"]},
    {"id": "t001", "title": "x", "zzz_unknown_key": "kept after the ordered ones", "aaa": 1},
    {"id": "t001", "title": "", "skills": [""], "target_dir": "/tmp/x y"},
    {"id": "t001", "title": "x", "review": {"last_verdict": None, "cycle_count": 0, "reviewer": "a: b"}},
]
BODIES = ["", "no trailing newline", "with newline\n", "## Description\nd\n\n## Result\nr\n"]


@pytest.mark.parametrize("meta", CARD_CORPUS)
@pytest.mark.parametrize("body", BODIES)
def test_serialize_card_matches_plan_sh_byte_for_byte(plan, meta, body):
    assert store.serialize_card(dict(meta), body) == plan["serialize_frontmatter"](dict(meta), body)


@pytest.mark.parametrize("data", [
    {"title": "m", "slug": "s", "status": "drafting", "next_task_id": 3, "max_review_cycles": 3,
     "deliverable_required": True, "created_at": "2026-09-30T00:00:00Z", "completed_at": None,
     "review": {"last_verdict": None, "cycle_count": 0, "reviewed_at": None, "reviewer": None}},
    {"slug": "s", "extra": "after", "title": "colon: x", "next_task_id": 1},
    {},
])
def test_serialize_mission_matches_plan_sh(plan, data):
    assert store.serialize_mission(dict(data)) == plan["dump_yaml"](dict(data), key_order=plan["MISSION_KEY_ORDER"])


@pytest.mark.parametrize("state", [
    {"active_missions": [], "default_mission": None},
    {"active_missions": ["a", "20260930-x"], "default_mission": "a"},
    {"active_missions": None, "default_mission": "weird: name"},
    {"default_mission": "only"},
])
def test_serialize_state_matches_plan_sh(plan, state):
    plan["written"].clear()
    plan["save_state"](dict(state))
    assert plan["written"] == [store.serialize_state(dict(state))]


def test_key_order_tables_are_the_same_as_plan_sh(plan):
    assert store.TASK_META_KEY_ORDER == plan["TASK_META_KEY_ORDER"]
    assert store.MISSION_KEY_ORDER == plan["MISSION_KEY_ORDER"]


def test_real_plan_sh_output_round_trips_through_the_lib_unchanged(tmp_path):
    """本物の plan.sh (隔離コピー) が書いた card / mission / state を lib で読み戻して再直列化 → 同じバイト。"""
    plan_sh = copy_plan_tree(tmp_path)
    (tmp_path / "queue").mkdir()
    (tmp_path / "registry").mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CREWVIA_QUEUE": str(tmp_path / "queue"),
           "CREWVIA_REPO_ROOT": str(tmp_path), "CREWVIA_TASKVIA": "disabled", "CREWVIA_TASK_GRAPH": "0"}

    def run(*args):
        p = subprocess.run([str(plan_sh), *args], env=env, capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr
        return p.stdout

    run("init", "My mission: colon")
    run("add", 'Task: with "quotes" and: colon', "--skills", "code,bash", "--description", "d")
    run("add", "second 日本語", "--skills", "code", "--blocked-by", "t001", "--pr-number", "5", "--description", "d2")
    run("update", "t001", "--status", "in_progress", "--worker", "Haruto", "--priority", "high")
    files = sorted((tmp_path / "queue").rglob("*.md")) + sorted((tmp_path / "queue").rglob("mission.yaml")) + \
        [tmp_path / "queue" / "state.yaml"]
    assert len(files) == 4                                   # 空虚でない: card 2 + mission + state
    for p in files:
        raw = p.read_text()
        if p.suffix == ".md":
            meta, _body = cards.parse_frontmatter(raw)
            # parse_frontmatter は本文の空行を削るので、本文は書かれたままのバイトで渡す
            head = f"---\n{store.dump_yaml(meta, key_order=store.TASK_META_KEY_ORDER)}---\n\n"
            assert raw.startswith(head), p
            assert store.serialize_card(meta, raw[len(head):]) == raw, p
        elif p.name == "mission.yaml":
            assert store.serialize_mission(cards.parse_yaml(raw)) == raw, p
        else:
            assert store.serialize_state(cards.parse_yaml(raw)) == raw, p


def test_lib_dump_yaml_does_not_mutate_its_key_order_table():
    """plan.sh は渡された表に未知のキーを足す。長く生きるプロセスで使う lib は表を汚さない。"""
    before = list(store.TASK_META_KEY_ORDER)
    store.serialize_card({"id": "t001", "zzz_unknown": 1}, "b")
    store.serialize_mission({"slug": "s", "zzz_unknown": 1})
    assert store.TASK_META_KEY_ORDER == before
    assert "zzz_unknown" not in store.MISSION_KEY_ORDER
