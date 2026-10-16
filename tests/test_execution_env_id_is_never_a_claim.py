"""E5 (mission 20261009-e5-env-and-backlog): plan.sh は env の CREWVIA_EXECUTION_ID を名乗りに使わない。名乗りは `--execution` だけ。

設計: `knowledge/execution.md` §20.8。主題は stale な `.crewvia-env` の再現 — Worker A が pull → 試行が置き換わる
(reset → Worker B が再 pull) → A が古い `.crewvia-env` (4 行目に A の ID) を source して `--execution` なしで done。
旧コードは env を読んで A の ID で照合し、拒否 (NOT_CURRENT) になるか、B の ID が書かれた新しい `.crewvia-env` を source した古い
Worker が B を名乗って通っていた。今は env を読まないので、どちらも「名乗りなし」= EXECUTION_REQUIRED (exit 3・何も書かない)。
**本番の queue / registry / mux には触れない** (`Box` が隔離する)。
"""

from __future__ import annotations

import shlex

import pytest

from execution_e3_helpers import MISSION, Box, last_error_code, report_argv, run, take

REPORTS = ["done", "fail", "needs-director", "ready-for-verification"]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001", "t002"))


def source_env_file(text):
    """`.crewvia-env` (export K=V の並び) を source した後の env を再現する。"""
    env = {}
    for line in text.splitlines():
        k, _, v = line.removeprefix("export ").partition("=")
        env[k] = shlex.split(v)[0] if v else ""
    return env


def test_a_new_pull_does_not_write_the_execution_id_to_crewvia_env(box):
    xid = take(box)
    assert xid not in box.env_file() and "CREWVIA_EXECUTION_ID" not in box.env_file()


def test_stale_crewvia_env_sourced_by_an_old_worker_cannot_name_the_replacement_attempt(box):
    """主題: 4 行の古い `.crewvia-env` (既存の worktree に残りうる形) を source して `--execution` なしで done。"""
    old = take(box, "Ren")
    box.reset()
    new = take(box, "Sora")
    assert new != old
    for ident, why in ((old, "A の古い ID"), (new, "B の新しい ID (別 Worker が上書きした後の ID)")):
        stale = source_env_file(f"export CREWVIA_MISSION_SLUG={MISSION}\nexport CREWVIA_TASK_ID=t001\n"
                                f"export CREWVIA_TASK_SLUG=x\nexport CREWVIA_EXECUTION_ID={ident}\n")
        before = box.snapshot(with_audit=False)
        p = run(box, "done", "t001", "r", "--mission", MISSION, agent="Ren", env=stale)
        assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_REQUIRED", (why, p.stdout, p.stderr)
        assert ident not in p.stderr                                        # 値は出さない
        assert box.snapshot(with_audit=False) == before            # card / record / 枠 / identity は不変
        assert box.card()["status"] == "in_progress"


@pytest.mark.parametrize("command", REPORTS)
@pytest.mark.parametrize("which", ["current", "stale"])
def test_env_alone_is_unnamed_for_every_report(box, command, which):
    xid = take(box)
    before = box.snapshot()
    p = run(box, *report_argv(command), env={"CREWVIA_EXECUTION_ID": xid if which == "current" else "ex-" + "2" * 32})
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_REQUIRED", p.stderr
    assert "CREWVIA_EXECUTION_ID" in p.stderr and "--execution" in p.stderr   # 無視したことを 1 行で言う
    assert box.snapshot() == before


def test_the_flag_alone_decides_even_when_env_disagrees(box):
    xid = take(box)
    p = run(box, *report_argv("done"), "--execution", xid, env={"CREWVIA_EXECUTION_ID": "ex-" + "3" * 32})
    assert p.returncode == 0, p.stderr
    assert box.card()["status"] == "done"


def test_an_empty_env_is_ignored_not_an_error(box):
    xid = take(box)
    p = run(box, *report_argv("done"), "--execution", xid, env={"CREWVIA_EXECUTION_ID": ""})
    assert p.returncode == 0, p.stderr
    assert "CREWVIA_EXECUTION_ID" not in p.stderr
