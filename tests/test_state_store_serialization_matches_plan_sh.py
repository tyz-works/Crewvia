"""lib_state_store の直列化は **cutover 前の plan.sh** の直列化と**バイト単位で同じ** (設計 §3.2 直列化の行)。

S3 (t012) で plan.sh は自前の `dump_yaml` / `serialize_frontmatter` / `save_state` を捨て、この lib を
使うようになった (原案 §14-7 二重実装の禁止)。「バイトが変わらない」の根拠は、cutover 前 (a1f6957) の
plan.sh の関数を**その版で実行して得た出力**を golden として固定したもの
(`tests/fixtures/state_store_serialization_golden.json`)。plan.sh の関数は今は無いので、AST で取り出して
比べる方式は使えない — 旧版の出力を写した golden との比較に置き換えた。

あわせて次を確かめる:
- plan.sh に直列化のコピーが**戻っていない** (AST。関数名・`_NEEDS_QUOTE` 表・`open(..., 'w')` で queue を書く形)
- 本物の plan.sh が書いた card / mission / state を lib で読み戻して再直列化しても 1 バイトも変わらない
"""

from __future__ import annotations

import ast
import json
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
GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "state_store_serialization_golden.json")
                    .read_text(encoding="utf-8"))

#: cutover 前に plan.sh が持っていた直列化の名前。plan.sh に**戻ってはいけない**。
FORMER_PLAN_SH_SERIALIZERS = {
    "dump_yaml", "_dump_kv", "_dump_scalar", "_dump_inline", "serialize_frontmatter",
    "_NEEDS_QUOTE", "TASK_META_KEY_ORDER", "MISSION_KEY_ORDER",
}


def _plan_tree() -> ast.Module:
    text = PLAN_SH.read_text()
    src = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL).group(1)
    return ast.parse(src)


def test_golden_is_not_vacuous():
    """golden が空・少数でないこと (0 件で全部 PASS しない)。"""
    assert len(GOLDEN["cards"]) == 40
    assert len(GOLDEN["missions"]) == 3
    assert len(GOLDEN["states"]) == 4
    assert all(c["expected"].startswith("---\n") for c in GOLDEN["cards"])


@pytest.mark.parametrize("case", GOLDEN["cards"], ids=lambda c: f"{c['meta'].get('title', '')[:12]!r}-{len(c['body'])}")
def test_serialize_card_matches_pre_cutover_plan_sh_byte_for_byte(case):
    assert store.serialize_card(dict(case["meta"]), case["body"]) == case["expected"]


@pytest.mark.parametrize("case", GOLDEN["missions"])
def test_serialize_mission_matches_pre_cutover_plan_sh(case):
    assert store.serialize_mission(dict(case["data"])) == case["expected"]


@pytest.mark.parametrize("case", GOLDEN["states"])
def test_serialize_state_matches_pre_cutover_plan_sh(case):
    assert store.serialize_state(dict(case["state"])) == case["expected"]


def test_plan_sh_no_longer_carries_a_copy_of_the_serializer():
    """plan.sh のモジュール直下の定義・代入に、旧直列化の名前が無い (コピーが戻ったら赤)。"""
    tree = _plan_tree()
    defined = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    assert len(tree.body) > 200, "plan.sh の python 本体を読めていない (空虚な PASS)"
    assert defined & FORMER_PLAN_SH_SERIALIZERS == set(), (
        "plan.sh に直列化のコピーが戻っている: " + ", ".join(sorted(defined & FORMER_PLAN_SH_SERIALIZERS)))
    # 陽性対照: 検出器が旧 plan.sh (golden の生成元と同じ形) の名前を拾えること
    sample = ast.parse("def dump_yaml(data, key_order=None):\n    pass\n_NEEDS_QUOTE = set()\n")
    names = {n.name for n in sample.body if isinstance(n, ast.FunctionDef)} | \
            {t.id for n in sample.body if isinstance(n, ast.Assign) for t in n.targets}
    assert names & FORMER_PLAN_SH_SERIALIZERS == {"dump_yaml", "_NEEDS_QUOTE"}


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
    """旧 plan.sh の dump_yaml は渡された表に未知のキーを足した。長く生きるプロセスで使う lib は表を汚さない。"""
    before = list(store.TASK_META_KEY_ORDER)
    store.serialize_card({"id": "t001", "zzz_unknown": 1}, "b")
    store.serialize_mission({"slug": "s", "zzz_unknown": 1})
    assert store.TASK_META_KEY_ORDER == before
    assert "zzz_unknown" not in store.MISSION_KEY_ORDER
