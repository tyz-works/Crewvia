"""01c E3 (t012): verifier-dispatcher.sh は `verifying` と、verifier への `verify-result` の指示文で、card の今の試行を名指しする。

設計: `knowledge/execution.md` §5.1 (verify-result は「どの試行を判定するか」を名指しする。verifier は持ち主ではない)・§5.4。

verifier-dispatcher.sh の python 本体は bash の heredoc の中で、import できない。**本物のソースから関数だけを取り出して**
(ast で `card_execution_id` と `mark_verifying`・正規表現の定義) 小さな名前空間で走らせる (写しを書かない)。
tmux・queue には触れない (`subprocess.Popen` は記録するだけの偽物)。
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO / "scripts" / "verifier-dispatcher.sh").read_text()
XID = "ex-" + "a" * 32


def _python_source():
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", SRC, re.DOTALL)
    assert m, "verifier-dispatcher.sh の python ヒアドキュメント (PYEOF) が見つからない"
    return m.group(1)


def _namespace(popen):
    tree = ast.parse(_python_source())
    wanted_funcs = {"card_execution_id", "mark_verifying"}
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in wanted_funcs)
            or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in
                                                  {"_EXECUTION_ID_RE", "PLAN_SH", "PLAN_SH_TIMEOUT_SECONDS"}
                                                  for t in n.targets))]
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert found == wanted_funcs, found
    module = ast.Module(body=body, type_ignores=[])

    class FakeSubprocess:
        PIPE = subprocess.PIPE
        TimeoutExpired = subprocess.TimeoutExpired
        Popen = staticmethod(popen)

    ns = {"re": re, "os": __import__("os"), "subprocess": FakeSubprocess, "Path": pathlib.Path,
          "QUEUE_DIR": pathlib.Path("/nonexistent-queue"), "_SCRIPTS_DIR": pathlib.Path("/nonexistent-scripts")}
    exec(compile(module, "verifier-dispatcher.sh", "exec"), ns)
    return ns


class RecordingPopen:
    calls: list = []

    def __init__(self, argv, env=None, **kw):
        type(self).calls.append((argv, env))
        self.returncode = 0
        self.pid = 1

    def communicate(self, timeout=None):
        return "", ""


@pytest.fixture
def ns():
    RecordingPopen.calls = []
    return _namespace(RecordingPopen)


@pytest.mark.parametrize("status", ["reserved", "running"])
def test_an_active_attempt_is_named(ns, status):
    assert ns["card_execution_id"]({"current_execution_id": XID, "execution_status": status}) == XID


@pytest.mark.parametrize("meta", [
    {},                                                                                    # legacy の card (欄なし)
    {"current_execution_id": XID, "execution_status": "completed"},                        # terminal の試行
    {"current_execution_id": XID, "execution_status": "failed"},
    {"current_execution_id": XID},                                                         # 状態が無い
    {"current_execution_id": "ex-123", "execution_status": "running"},                     # 形が違う
    {"current_execution_id": "EX-" + "a" * 32, "execution_status": "running"},
    {"current_execution_id": None, "execution_status": "running"},
    {"current_execution_id": ["x"], "execution_status": "running"},
])
def test_anything_else_is_not_named(ns, meta):
    """名乗って拒否されるより、名乗りなしで通す (E3 は名乗りなしを拒否しない)。壊れた値は名乗りに使わない。"""
    assert ns["card_execution_id"](meta) is None


def test_verifying_passes_the_execution_flag_only_when_there_is_an_id(ns):
    ns["mark_verifying"]("m", "t001", "V1", XID)
    argv, env = RecordingPopen.calls[-1]
    assert argv[-2:] == ["--execution", XID] and argv[2:4] == ["verifying", "t001"]
    assert env["AGENT_NAME"] == "verifier-dispatcher"
    ns["mark_verifying"]("m", "t001", "V1")
    argv, _ = RecordingPopen.calls[-1]
    assert "--execution" not in argv


def test_the_daemons_own_environment_never_leaks_a_claim_into_verifying(ns, monkeypatch):
    monkeypatch.setenv("CREWVIA_EXECUTION_ID", "ex-" + "9" * 32)
    ns["mark_verifying"]("m", "t001", "V1")
    _argv, env = RecordingPopen.calls[-1]
    assert "CREWVIA_EXECUTION_ID" not in env


def test_the_instruction_to_the_verifier_carries_the_execution_flag():
    """verifier への指示文 (verify-result) に `--execution <id>` が入る。指示文は dispatch() の中なので、組み立ての形を
    ソースで固定する (名乗りがあるときだけ付ける・名乗りがなければ今までの文面)。"""
    src = _python_source()
    assert 'exec_arg = f" --execution {xid}" if xid else ""' in src
    assert "<pass|fail|needs_human_review>\"" in src.replace("\\\"", '"') or "<pass|fail|needs_human_review>" in src
    assert "{exec_arg}" in src and "mark_verifying(slug, task_id, agent_name, xid)" in src
