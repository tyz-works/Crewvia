"""01c E3 (t012): rollback した先の plan.sh でも `--execution` 付きの報告が通る (knowledge/execution.md §16.4)。

E3 は Worker のプロンプト・kai-review.sh・verifier-dispatcher.sh・Director の手順に `--execution <id>` を足す。E3 を revert
すると plan.sh だけが戻るが、**起動済みの Worker のプロンプトや走っている kai-review.sh は `--execution` を付けたまま
done / needs-director 等を呼び続ける**。戻し先の plan.sh が unknown option で拒否すると、完了・失敗の報告が止まる。
だから **`--execution` を受け付けて読み捨てるだけの互換を E3 の前に別 commit (`e3-execution-flag-compat`) で入れた**。
この PR (E3) を revert しても、その commit は残る。

ここは「その commit の plan.sh」を git の履歴から取り出して走らせ、6 つの報告コマンドが `--execution` を拒否せず
今までどおりに動くことを示す。履歴に無い (CI の浅い clone) ときは skip する。
"""

from __future__ import annotations

import subprocess

import pytest

import pull_execution_helpers as h
from pull_execution_helpers import Box, MISSION
from fixture_tree import copy_plan_tree

COMPAT_MARKER = "e3-execution-flag-compat"
FAKE_ID = "ex-" + "0" * 32          # 旧コードは読み捨てる。形が正しければ何でもよい


def _compat_sha():
    p = subprocess.run(["git", "-C", str(h.REPO_ROOT), "log", "--all", "--format=%H", "-n", "1",
                        f"--grep={COMPAT_MARKER}"], capture_output=True, text=True)
    sha = p.stdout.strip()
    if p.returncode != 0 or not sha:
        pytest.skip(f"{COMPAT_MARKER} の commit が git の履歴に無い (浅い clone)")
    return sha


def _install_compat_plan(box, tmp_path):
    """互換 commit の scripts/ を、隔離 root に上書きして写す (戻し先の plan.sh の再現)。"""
    sha = _compat_sha()
    src = tmp_path / "compat-src"
    src.mkdir()
    packed = subprocess.run(["git", "-C", str(h.REPO_ROOT), "archive", sha, "scripts", "tests/fixtures"],
                            capture_output=True)
    if packed.returncode != 0:
        pytest.skip(f"{sha} を取り出せない")
    subprocess.run(["tar", "-x", "-C", str(src)], input=packed.stdout, check=True)
    box.plan_sh = copy_plan_tree(box.root, src)


@pytest.fixture
def old_box(tmp_path):
    box = Box(tmp_path / "root", tasks=("t001", "t002", "t003", "t004"))
    _install_compat_plan(box, tmp_path)
    return box


def ok(p):
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert "unknown option" not in p.stderr
    return p


def test_done_with_an_execution_flag_goes_through_on_the_rolled_back_plan_sh(old_box):
    ok(old_box.pull("Ren", "t002"))
    ok(old_box.plan("done", "t002", "r", "--no-pr", "x", "--execution", FAKE_ID, "--mission", MISSION, agent="Ren"))
    assert old_box.card("t002")["status"] == "done"


def test_fail_and_needs_director_with_an_execution_flag_go_through_on_the_rolled_back_plan_sh(old_box):
    ok(old_box.pull("Ren", "t003"))
    ok(old_box.plan("needs-director", "t003", "why", "--execution", FAKE_ID, "--mission", MISSION, agent="Ren"))
    assert old_box.card("t003")["status"] == "needs_director"
    ok(old_box.pull("Sora", "t004"))
    ok(old_box.plan("fail", "t004", "--no-head", "none", "--execution", FAKE_ID, "--mission", MISSION, agent="Sora"))
    assert old_box.card("t004")["status"] == "failed"


def test_the_verification_chain_with_an_execution_flag_goes_through_on_the_rolled_back_plan_sh(old_box):
    ok(old_box.pull("Ren", "t001"))
    ok(old_box.plan("ready-for-verification", "t001", "--execution", FAKE_ID, "--mission", MISSION, agent="Ren"))
    ok(old_box.plan("verifying", "t001", "--verifier", "V1", "--execution", FAKE_ID, "--mission", MISSION,
                    agent="verifier-dispatcher"))
    ok(old_box.plan("verify-result", "t001", "pass", "--execution", FAKE_ID, "--mission", MISSION, agent="V1"))
    assert old_box.card("t001")["status"] == "verified"
