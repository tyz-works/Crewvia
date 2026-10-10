#!/usr/bin/env python3
"""貼った Worker 名は `plan.sh pull` の引数解析で同じ名前として受理される (往復の性質)。

PR #294 は 3 回、「貼った名前が pull で同じ名前として受理されるか」の族で Codex の P2 を受けた
(State Store の import・クォート・先頭の `-`)。拒否する形を足すのをやめ、通す形を許可リストにして、
検証が通した名前ごとに「指示文 → shlex.split → 本物の parse_opts」の往復を確かめる。
plan.sh は読み込むだけで変更しない。

    python3 -m pytest tests/test_agent_name_pull_roundtrip.py -v
"""

from __future__ import annotations

import pathlib
import random
import shlex
import sys

import pytest
import yaml

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(REPO / "scripts"))

import lib_agent_name  # noqa: E402
from task_graph_publisher_harness import load_plan_namespace  # noqa: E402
from test_assignment_routing import _cycle, repo  # noqa: E402,F401

PULL_SPEC = {'--mission': 'value', '--skills': 'value', '--agent': 'value',
             '--target-dir': 'value', '--task': 'value'}  # plan.sh の cmd_pull と同じ option 集合


@pytest.fixture(scope="module")
def plan_ns(tmp_path_factory):
    root = tmp_path_factory.mktemp("roundtrip")
    ns = load_plan_namespace(REPO / "scripts" / "plan.sh", str(root / "queue"), str(REPO))
    ns["SUBCOMMAND"] = "pull"
    return ns


def _pull_args(name):
    """dispatcher が割り当て文に貼る形 (pull_agent_flag と同じ連結) から plan pull の引数を取り出す。"""
    sentence = f"plan pull --task t001 --mission m1 --agent {name} で取得後、作業"
    words = shlex.split(sentence.split(" で取得後")[0])
    return words[words.index("pull") + 1:]


def _production_names():
    pool = yaml.safe_load((REPO / "config" / "worker-names.yaml").read_text())["names"]
    reg = yaml.safe_load((REPO / "registry" / "workers.yaml").read_text()) or {}
    registered = [w["name"] for w in reg.get("workers", [])]
    return [str(n) for n in pool + registered + ["Kai-codex"]]


BOUNDARY = ["a", "Z", "0", "a-", "a.", "a_", "a-b", "a.b", "a_b", "A" * 200, "a" * 64 + "-" + "b" * 64,
            "-worker", "--help", "-x", "-", "--", ".a", "_a", " a", "a ", "a b", "a;b", "a$(x)", "a`x`",
            "a'b", 'a"b', "a|b", "a&b", "a>b", "a*b", "a\\b", "a=b", "a\nb", "日本", "Ωmega", ""]


def _random_names(n=3000, seed=294):
    rng = random.Random(seed)
    alphabet = "abcXYZ019-_. ;$()'\"`|&<>*\\=/\n~#!"
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12))) for _ in range(n)]


def _accepted_by_pull(ns, name):
    """本物の parse_opts + require_valid_agent_name を通して agent が元の名前と一致するか。"""
    try:
        opts, _ = ns["parse_opts"](_pull_args(name), PULL_SPEC)
        agent = opts.get("--agent", "").strip()
        ns["require_valid_agent_name"](agent)
    except (SystemExit, ValueError, ns["UsageExit"]):
        return False
    return agent == name


def test_every_production_worker_name_is_pasteable():
    names = _production_names()
    assert len(names) > 30, names
    bad = {n: lib_agent_name.shell_pasteable_agent_name_problem(n) for n in names
           if lib_agent_name.shell_pasteable_agent_name_problem(n)}
    assert not bad, bad
    # `-worker` を付ける前の名前で窓名も作る (x-worker)。窓名の側も許可リストに収まる
    assert all(lib_agent_name.shell_pasteable_agent_name_problem(n + "-worker") is None for n in names)


@pytest.mark.parametrize("batch", ["production", "boundary", "random"])
def test_names_the_check_accepts_round_trip_through_the_real_pull_parser(plan_ns, batch):
    names = {"production": _production_names, "boundary": lambda: BOUNDARY, "random": _random_names}[batch]()
    accepted = 0
    for name in names:
        if lib_agent_name.shell_pasteable_agent_name_problem(name) is None:
            accepted += 1
            assert _accepted_by_pull(plan_ns, name), f"検証は通すが pull が同じ名前で受理しない: {name!r}"
    assert accepted > 0


def test_names_the_check_rejects_get_no_agent_flag_in_the_dispatcher(repo):
    """検証が拒否する名前は dispatcher が `--agent` を付けない (従来の文)。通す名前は付ける (本物の dispatcher)。"""
    _, ns = _cycle(repo)
    for name in BOUNDARY + _random_names(300):
        ok = lib_agent_name.shell_pasteable_agent_name_problem(name) is None
        assert ns["pull_agent_flag"](name) == (f" --agent {name}" if ok else ""), repr(name)


def test_the_old_check_would_have_failed_the_round_trip(plan_ns):
    """赤の実証の印: クォートだけを見る旧検証は `-worker` を通すが、pull は受理しない。"""
    import shlex as _s
    old = lambda n: bool(n) and n == n.strip() and _s.quote(n) == n  # noqa: E731
    assert old("-worker") and not _accepted_by_pull(plan_ns, "-worker")
    assert old("--help") and not _accepted_by_pull(plan_ns, "--help")
