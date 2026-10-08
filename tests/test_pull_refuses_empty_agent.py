"""`plan.sh pull` は agent (--agent も AGENT_NAME も) が空なら exit 1 で断り、1 バイトも書かない。

20261006-kai-review-full-findings/t001 と Ren t026 で 2 回起きた: pull を打ったシェルだけ AGENT_NAME が空
(テストの `env -u AGENT_NAME` / `env -i` の癖) で、card が in_progress・worker=null・execution の agent=null・
監査 actor=unknown になり、dispatcher が「仕事なし」と見て退役させた (store-check の in_progress_without_worker)。
exit 2 は idle の意味 (Worker が無限に再試行する) なので使わない。
"""

from __future__ import annotations

import json

import pytest

from pull_execution_helpers import Box, MISSION


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root")


def _refused(box, p, before):
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == ""
    assert "--agent" in p.stderr and "AGENT_NAME" in p.stderr
    assert box.snapshot(with_audit=True) == before          # state / card / execution / assignment / 監査 / task-graph
    assert box.card()["status"] == "pending" and box.card().get("worker") is None


def test_no_agent_flag_and_no_agent_name_is_refused_and_writes_nothing(box):
    before = box.snapshot(with_audit=True)
    p = box.plan("pull", "--skills", "code", "--task", "t001", "--mission", MISSION)       # AGENT_NAME 無し
    _refused(box, p, before)


def test_auto_select_without_an_agent_is_refused_too(box):
    before = box.snapshot(with_audit=True)
    _refused(box, box.plan("pull", "--skills", "code", "--mission", MISSION), before)


@pytest.mark.parametrize("blank", ["", " ", "\t", "  \n "])
def test_blank_agent_is_refused_whether_it_comes_from_the_flag_or_the_env(box, blank):
    before = box.snapshot(with_audit=True)
    flag = box.plan("pull", "--agent", blank, "--skills", "code", "--task", "t001", "--mission", MISSION)
    _refused(box, flag, before)
    env = box.plan("pull", "--skills", "code", "--task", "t001", "--mission", MISSION, agent=blank or None)
    _refused(box, env, before)
    if blank:   # 空白だけの AGENT_NAME (proc_env は空文字を入れないので直接)
        import subprocess
        e = box.proc_env()
        e["AGENT_NAME"] = blank
        q = subprocess.run(box.argv("pull", "--skills", "code", "--task", "t001", "--mission", MISSION),
                           env=e, capture_output=True, text=True, timeout=120)
        _refused(box, q, before)


def test_agent_given_still_pulls_as_before(box):
    p = box.pull("Ren")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["id"] == "t001"
    assert box.card()["worker"] == "Ren"


def test_agent_name_env_alone_is_enough(box):
    p = box.plan("pull", "--skills", "code", "--task", "t001", "--mission", MISSION, agent="Ren")
    assert p.returncode == 0, p.stderr
    assert box.card()["worker"] == "Ren"


def _graph_files(box):
    d = box.root / "registry" / "task-graph"
    return {p.relative_to(box.root).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()} \
        if d.exists() else {}


def _with_task_graph(box):
    """隔離した Box の root の中で task-graph の生成を有効にする (既定は CREWVIA_TASK_GRAPH=0)。"""
    box.env.pop("CREWVIA_TASK_GRAPH", None)
    box.env.pop("CREWVIA_TASK_GRAPH_FILE", None)       # 既定 = <root>/registry/task-graph/tasks.json (本番に書かない)


def test_refusal_does_not_regenerate_the_task_graph(box):
    """die() だと末尾の dispatch が task-graph を再生成し、lock と tasks.json を書く (Codex P2)。"""
    _with_task_graph(box)
    before_graph = _graph_files(box)
    before = box.snapshot(with_audit=True)
    for args, agent in ((("--skills", "code", "--task", "t001"), None), (("--skills", "code"), None),
                        (("--agent", " ", "--skills", "code", "--task", "t001"), None)):
        _refused(box, box.plan("pull", *args, "--mission", MISSION, agent=agent), before)
    assert _graph_files(box) == before_graph


def test_task_graph_positive_control_a_real_pull_does_write_it(box):
    """上のテストが「そもそも書かれない設定」で緑になっていないことの対照。"""
    _with_task_graph(box)
    assert _graph_files(box) == {}
    p = box.pull("Ren")
    assert p.returncode == 0, p.stderr
    assert any(k.endswith("tasks.json") for k in _graph_files(box)), _graph_files(box)
