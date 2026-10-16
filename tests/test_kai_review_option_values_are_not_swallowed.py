"""t002 項目 1: kai-review.sh の値を取る option (--pr / --task / --mission / --model / --agent) は、値が無い・次の
option が続くときに次の語を値として食わない。使い方の誤りとして exit 1 (副作用の前。`--execution` と同じ扱い)。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

KAI_REVIEW = Path(__file__).resolve().parent.parent / "scripts" / "kai-review.sh"


def _run(*args):
    env = {k: v for k, v in os.environ.items() if k != "AGENT_NAME"}
    return subprocess.run(["bash", str(KAI_REVIEW), *args], capture_output=True, text=True, env=env, timeout=30)


@pytest.mark.parametrize("opt", ["--pr", "--task", "--mission", "--model", "--agent"])
def test_option_followed_by_another_option_is_refused(opt):
    r = _run(opt, "--dry-run")
    assert r.returncode == 1
    assert f"{opt} には値が要ります" in r.stderr


@pytest.mark.parametrize("opt", ["--pr", "--task", "--mission", "--model", "--agent"])
def test_option_with_no_value_at_the_end_is_refused(opt):
    r = _run(opt)
    assert r.returncode == 1
    assert f"{opt} には値が要ります" in r.stderr


def test_task_does_not_swallow_the_next_option():
    r = _run("--pr", "1", "--task", "--mission", "m-x")
    assert r.returncode == 1
    assert "--task には値が要ります" in r.stderr
