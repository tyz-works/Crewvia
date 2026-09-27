#!/usr/bin/env python3
"""形の違う `blocked_by` を持つ card は、どの経路でも開始されない (PR #217 Kai-codex 2 巡目 P2)。

## 穴 (t025 が作った fail-open 回帰)

t025 は `released_deps` を検証するとき、`card_dependencies()` に truthiness フィルタ
(`[d for d in (meta.get('blocked_by') or []) if d]`) を入れた。これが **`blocked_by` 側の falsy な
要素を黙って落とす**: `blocked_by: [null]` の card が「依存なし」と判定され、pull も dispatch も
task を開始する。#9 が潰そうとした事故の型 (**依存が満たされていないのに下流が進む**) を逆向きから
作り直していて、修正前 (`null` が unmet に落ちていた) より悪い。

## 直し方

依存の宣言は「これが済むまで進めるな」という**制約**なので、読み違えて落とすと制約が消える。

* 読み取り (`lib_task_cards.blocked_deps_problem`): 形の違う card は `[破損]` に隔離する。
  pull も dispatch も拾わず、mission の完了にも数えない。出口は
  `plan.sh update <id> --blocked-by ...` (raw の card を書き直す)。
* 2 枚目の網 (`lib_dep_rules.declared_dependencies`): 隔離をすり抜けた値も**捨てずに unmet に残す**。

## このファイルが固定すること

1. 不正値の表 (下の `MALFORMED`) の全行について、**5 者すべて** (自動 pull / `pull --task` /
   task-graph / status / 実 dispatcher 1 サイクル) で task が開始されない。
2. 対照 (`CONTROLS`): 健全な `blocked_by` は従来どおり進む。緑が「全部止めている」せいでないことの証明。
3. 2 枚目の網は、隔離を通らない生の card でも落とさず・例外を出さず unmet にする。
4. 隔離が出口を塞がない: `plan.sh update --blocked-by` で直せば、同じ card が進める。
   `release-dep` は、読み違えたままの card を解除しない。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import lib_dep_rules  # noqa: E402
import lib_task_cards  # noqa: E402
from test_dispatcher_cycle_honours_hold import (  # noqa: E402
    _kickoffs_for, run_dispatch_cycle,
)
from test_failed_dependency_hold import (  # noqa: E402
    DOWNSTREAM, MISSION, Sandbox, _card,
)

# ---------------------------------------------------------------------------
# 不正値の表: (名前, `blocked_by` の生の YAML (改行を含んでよい))
# ---------------------------------------------------------------------------
#
# 「1 枚の card が、依存を宣言したつもりで、実際は何も宣言していない」形を網羅する。
# 要素が falsy / 型違い、list でない値、block list の空要素。

MALFORMED = [
    ("[null]",            "blocked_by: [null]"),
    ("[false]",           "blocked_by: [false]"),
    ("[0]",               "blocked_by: [0]"),
    ('[""]',              'blocked_by: [""]'),
    ('["   "]',           'blocked_by: ["   "]'),
    ("[123]",             "blocked_by: [123]"),
    ("[true]",            "blocked_by: [true]"),
    ("[t001, null]",      "blocked_by: [t001, null]"),      # 健全な依存 (done) + 空の要素
    ("mapping",           "blocked_by:\n  t001: false"),
    ("plain string",      "blocked_by: t001"),              # 反復すると 1 文字ずつ
    ("bool true",         "blocked_by: true"),
    ("bool false",        "blocked_by: false"),             # `or []` が「無い」に潰す
    ("int 123",           "blocked_by: 123"),
    ("int 0",             "blocked_by: 0"),                 # `or []` が「無い」に潰す
    ('empty string',      'blocked_by: ""'),
    ("block list, null",  "blocked_by:\n  - null"),
    ("block list, empty", "blocked_by:\n  - "),
]
MALFORMED_IDS = [m[0] for m in MALFORMED]

#: 対照: 依存の宣言として読める形。`expected` = 進めてよいか。
CONTROLS = [
    ("absent",            None,                       True),
    ("null",              "blocked_by: null",         True),
    ("bare key",          "blocked_by:",              True),
    ("empty list",        "blocked_by: []",           True),
    ("done dep",          "blocked_by: [t001]",       True),
    ("block list, done",  "blocked_by:\n  - t001",    True),
    ("pending dep",       "blocked_by: [t002]",       False),
    ("dangling dep",      "blocked_by: [t099]",       False),
]
CONTROL_IDS = [c[0] for c in CONTROLS]


def _put(sb, blocked_by_yaml, t002="pending"):
    """`t001` (done) / `t002` を置き、DOWNSTREAM を **`blocked_by` の生の YAML** で書く。

    `t002` は依存としては未完了 (pending / in_progress どちらも待つ)。dispatcher の 1 サイクルは
    idle Worker が 1 人なので、pending の `t002` に先に枠を取られると「下流に kickoff が飛ばない」
    が**間違った理由で**成立する。dispatcher 系は `t002="in_progress"` で枠を空けておく。
    """
    sb.card("t001", "done")
    sb.card("t002", t002)
    text = _card(DOWNSTREAM, "pending", ["PLACEHOLDER"])
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("blocked_by:"))
    lines[idx:idx + 1] = [] if blocked_by_yaml is None else blocked_by_yaml.split("\n")
    (sb.tasks / f"{DOWNSTREAM}.md").write_text("\n".join(lines) + "\n")


def _pulled_downstream(sb):
    """自動選択の pull が DOWNSTREAM を選んだか。t002 (pending, skills=code) が先に選ばれ得るので
    「下流を選んだか」だけを見る。"""
    started = []
    for _ in range(3):  # t002 が先に取られても、下流の順番までたどり着く
        r = sb.run("pull", "--skills", "code", "--agent", "Ren", "--mission", MISSION)
        if r.returncode != 0 or not r.stdout.strip().startswith("{"):
            break
        started.append(json.loads(r.stdout)["id"])
    return DOWNSTREAM in started


# ---------------------------------------------------------------------------
# 1. 5 者: 不正な blocked_by の task は、どれも開始しない
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,yaml_", MALFORMED, ids=MALFORMED_IDS)
def test_normal_pull_never_starts_it(tmp_path, name, yaml_):
    sb = Sandbox(tmp_path)
    _put(sb, yaml_)
    assert not _pulled_downstream(sb), f"[{name}] 自動選択の pull が、依存の宣言が不正な task を開始した"


@pytest.mark.parametrize("name,yaml_", MALFORMED, ids=MALFORMED_IDS)
def test_pull_task_never_starts_it(tmp_path, name, yaml_):
    """dispatcher の kickoff 経由 (`pull --task`) は、元の事故の経路。"""
    sb = Sandbox(tmp_path)
    _put(sb, yaml_)
    r = sb.run("pull", "--task", DOWNSTREAM, "--mission", MISSION,
               "--skills", "code", "--agent", "Ren")
    assert r.returncode != 0, f"[{name}] pull --task が不正な blocked_by の task を開始した: {r.stdout}"
    assert "started_at: null" in (sb.tasks / f"{DOWNSTREAM}.md").read_text(), (
        f"[{name}] 拒否したのに card が書き換わっている")


@pytest.mark.parametrize("name,yaml_", MALFORMED, ids=MALFORMED_IDS)
def test_task_graph_never_says_ready(tmp_path, name, yaml_):
    sb = Sandbox(tmp_path)
    _put(sb, yaml_)
    r = sb.run("task-graph")
    assert r.returncode == 0, r.stderr
    node = sb.graph_node(DOWNSTREAM)
    assert node["status"] != "ready", f"[{name}] DAG が、pull が拒否する task を READY と描いている: {node}"
    assert "[破損]" in node["title"], f"[{name}] なぜ止まっているのかが DAG から読めない: {node['title']!r}"


@pytest.mark.parametrize("name,yaml_", MALFORMED, ids=MALFORMED_IDS)
def test_status_shows_it_as_broken_with_a_reason(tmp_path, name, yaml_):
    sb = Sandbox(tmp_path)
    _put(sb, yaml_)
    r = sb.run("status", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    line = next(ln for ln in r.stdout.splitlines() if f" {DOWNSTREAM} " in ln)
    assert "(pending)" not in line, f"[{name}] 進める task のように出ている: {line}"
    assert "[破損]" in line, f"[{name}] 破損と出ていない: {line}"


@pytest.mark.parametrize("name,yaml_", MALFORMED, ids=MALFORMED_IDS)
def test_real_dispatch_cycle_never_sends_a_kickoff(tmp_path, name, yaml_):
    """本物の dispatch() を idle Worker 1 人で 1 サイクル回しても、kickoff は飛ばない。"""
    sb = Sandbox(tmp_path)
    _put(sb, yaml_, t002="in_progress")
    mux, log = run_dispatch_cycle(sb)
    sent = _kickoffs_for(mux, DOWNSTREAM)
    assert not sent, (f"[{name}] dispatcher が不正な blocked_by の task に kickoff を送った: "
                      f"{sent}\nlog:\n{log}")


# ---------------------------------------------------------------------------
# 2. 対照: 健全な blocked_by は従来どおり (5 者の緑が「全部止めている」せいではない証明)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,yaml_,ok", CONTROLS, ids=CONTROL_IDS)
def test_control_pull_task(tmp_path, name, yaml_, ok):
    sb = Sandbox(tmp_path)
    _put(sb, yaml_)
    r = sb.run("pull", "--task", DOWNSTREAM, "--mission", MISSION,
               "--skills", "code", "--agent", "Ren")
    assert (r.returncode == 0) == ok, f"[{name}] ok={ok} のはずが rc={r.returncode}: {r.stdout} {r.stderr}"


@pytest.mark.parametrize("name,yaml_,ok", CONTROLS, ids=CONTROL_IDS)
def test_control_real_dispatch_cycle(tmp_path, name, yaml_, ok):
    sb = Sandbox(tmp_path)
    _put(sb, yaml_, t002="in_progress")
    mux, log = run_dispatch_cycle(sb)
    assert bool(_kickoffs_for(mux, DOWNSTREAM)) == ok, (
        f"[{name}] ok={ok} のはずが kickoff={_kickoffs_for(mux, DOWNSTREAM)}\nlog:\n{log}")


# ---------------------------------------------------------------------------
# 3. 規則の 2 枚目の網: 隔離を通らない生の card でも、落とさず・例外を出さない
# ---------------------------------------------------------------------------

RAW_VALUES = [
    [None], [False], [0], [""], ["   "], [123], [True], ["t001", None],
    {"t001": False}, "t001", True, False, 0, 123, "", [[]], [{}],
]


@pytest.mark.parametrize("value", RAW_VALUES, ids=[repr(v) for v in RAW_VALUES])
def test_second_net_keeps_a_malformed_declaration_unmet(value):
    verdict = lib_dep_rules.card_dependencies(
        {"blocked_by": value}, {"t001"}, {"t001": "done"})
    assert verdict.unmet, f"blocked_by={value!r} が「依存なし」になった (fail-open): {verdict}"
    assert not verdict.held, "不正な宣言は failed の保留ではない (解除で外れてはいけない)"


@pytest.mark.parametrize("value", [None, [], ["t001"]], ids=["none", "empty", "done"])
def test_second_net_still_treats_a_real_absence_as_no_dependency(value):
    verdict = lib_dep_rules.card_dependencies(
        {"blocked_by": value}, {"t001"}, {"t001": "done"})
    assert not verdict.unmet, verdict


def test_second_net_does_not_shrink_the_declaration():
    """要素数は減らない (`[t001, null]` は 2 件の宣言のまま。片方だけが満たされる)。"""
    deps = lib_dep_rules.declared_dependencies(["t001", None, False, ""])
    assert len(deps) == 4 and deps[0] == "t001"


def test_a_malformed_declaration_cannot_be_released():
    """`released_deps` に何を書いても、不正な宣言は解除されない (解除は failed の依存だけ)。"""
    meta = {"blocked_by": [None], "released_deps": [None, "None", ""]}
    assert lib_dep_rules.card_dependencies(meta, set(), {}).unmet


@pytest.mark.parametrize("name,yaml_", MALFORMED, ids=MALFORMED_IDS)
def test_reader_quarantines_every_malformed_value(tmp_path, name, yaml_):
    sb = Sandbox(tmp_path)
    _put(sb, yaml_)
    meta, _body = lib_task_cards.read_task_card(sb.tasks / f"{DOWNSTREAM}.md", DOWNSTREAM)
    assert meta["status"] == lib_task_cards.CORRUPT_TASK_STATUS, f"[{name}] 隔離されていない: {meta}"
    assert "blocked_by" in meta["parse_error"], meta["parse_error"]


@pytest.mark.parametrize("name,yaml_,ok", CONTROLS, ids=CONTROL_IDS)
def test_reader_leaves_healthy_declarations_alone(tmp_path, name, yaml_, ok):
    sb = Sandbox(tmp_path)
    _put(sb, yaml_)
    meta, _body = lib_task_cards.read_task_card(sb.tasks / f"{DOWNSTREAM}.md", DOWNSTREAM)
    assert meta["status"] == "pending", f"[{name}] 健全な card が隔離された: {meta}"


# ---------------------------------------------------------------------------
# 4. 隔離は出口を塞がない
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,yaml_", [MALFORMED[0], MALFORMED[9], MALFORMED[12]],
                         ids=["[null]", "plain string", "bool false"])
def test_update_blocked_by_repairs_a_quarantined_card(tmp_path, name, yaml_):
    """`plan.sh status` が案内する直し方 (`update --blocked-by`) で、同じ card が進める。"""
    sb = Sandbox(tmp_path)
    _put(sb, yaml_)
    assert sb.run("pull", "--task", DOWNSTREAM, "--mission", MISSION,
                  "--skills", "code", "--agent", "Ren").returncode != 0
    r = sb.run("update", DOWNSTREAM, "--mission", MISSION, "--blocked-by", "t001")
    assert r.returncode == 0, f"[{name}] 直せない (出口が塞がっている): {r.stderr}"
    r = sb.run("pull", "--task", DOWNSTREAM, "--mission", MISSION,
               "--skills", "code", "--agent", "Ren")
    assert r.returncode == 0, f"[{name}] 直したのに進めない: {r.stderr}"


def test_update_with_an_empty_blocked_by_also_repairs(tmp_path):
    sb = Sandbox(tmp_path)
    _put(sb, "blocked_by: [null]")
    assert sb.run("update", DOWNSTREAM, "--mission", MISSION, "--blocked-by", "").returncode == 0
    assert sb.run("pull", "--task", DOWNSTREAM, "--mission", MISSION,
                  "--skills", "code", "--agent", "Ren").returncode == 0


def test_release_dep_refuses_to_release_a_card_it_cannot_read(tmp_path):
    """解除は保留を外す権限。何の依存を解除するのか読めない card では断り、直し方を言う。"""
    sb = Sandbox(tmp_path)
    sb.card("t001", "failed")
    _put_failed_with(sb, "blocked_by: [t001, null]")
    r = sb.run("release-dep", DOWNSTREAM, "--mission", MISSION)
    assert r.returncode != 0, r.stdout
    assert "update" in r.stderr and "--blocked-by" in r.stderr, r.stderr
    assert "released_deps" not in (sb.tasks / f"{DOWNSTREAM}.md").read_text(), "断ったのに書き換えている"


def _put_failed_with(sb, blocked_by_yaml):
    text = _card(DOWNSTREAM, "pending", ["PLACEHOLDER"])
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("blocked_by:"))
    lines[idx:idx + 1] = blocked_by_yaml.split("\n")
    (sb.tasks / f"{DOWNSTREAM}.md").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 5. 形: 依存の宣言を truthiness で落とす書き方が、消費側に戻っていない
# ---------------------------------------------------------------------------

import re  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
#: `blocked_by` を読む主体。ここに `if d` 型のフィルタがあると、`[null]` が「依存なし」になる。
CONSUMERS = ("plan.sh", "dispatcher.sh", "lib_dep_rules.py", "lib_task_cards.py",
             "taskvia-sync.sh", "watchdog.py", "verifier-dispatcher.sh")

#: 旧コードの実際の形 (`[d for d in (meta.get('blocked_by') or []) if d]`) に合わせてある。
#: 1 行の中で「blocked_by を反復する内包表記が、その変数の真偽で絞る」/
#: 「`filter(None, ... blocked_by ...)`」を拾う。
TRUTHY_FILTER = re.compile(
    r"for\s+(\w+)\s+in\b.*blocked_by.*\bif\s+\1\s*[\]\)]"
    r"|filter\(\s*None\s*,.*blocked_by")


def test_the_pattern_matches_the_real_old_call_shapes():
    """検出器自身の確認: t025 が入れた実際の 3 行を捕まえる (リテラルの見本だけでは証明にならない)。"""
    for line in (
        "            blocked_by = [d for d in (meta.get('blocked_by') or []) if d]",
        "        blocked_by =[d for d in (meta.get('blocked_by') or []) if d]",
        "    blocked_by = [d for d in (meta.get('blocked_by') or []) if d]",
        "    deps = list(filter(None, meta.get('blocked_by') or []))",
    ):
        assert TRUTHY_FILTER.search(line), f"検出器が旧コードの形を捕まえない: {line!r}"
    for line in (   # 誤検出しない: 別の理由で絞る内包表記
        "    stray = [d for d in wanted if d not in blocked_by]",
        "    kept = [d for d in (old_released or []) if d in new_blocked]",
    ):
        assert not TRUTHY_FILTER.search(line), f"誤検出: {line!r}"


@pytest.mark.parametrize("name", CONSUMERS)
def test_no_consumer_drops_falsy_dependencies(name):
    hits = [ln.strip() for ln in (SCRIPTS / name).read_text().splitlines()
            if TRUTHY_FILTER.search(ln)]
    assert not hits, (
        f"{name} が `blocked_by` の falsy な要素を黙って落としている: {hits}\n"
        f"`[null]` が「依存なし」になり、pull も dispatch も開始する (PR #217 2 巡目 P2)。"
        f"`lib_dep_rules.declared_dependencies()` を通すこと。")
