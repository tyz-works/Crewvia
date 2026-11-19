#!/usr/bin/env python3
"""
tests/test_post_tool_use_big_input_summary.py

hooks/post-tool-use.sh の TOOL_INPUT_SUMMARY は `echo "$TOOL_INPUT" | head -c 80` で作っていた。
pipefail 下で tool 入力が大きいと echo が SIGPIPE (141) で落ち、hook が crash して Taskvia へのログが
出なかった (#424 が pre-tool-use.sh:163 で直したのと同じ形)。bash の ${var:0:80} で切り出す。

本物の post-tool-use.sh を、curl を記録するだけの stub に差し替えて走らせる。
赤の実証: 代入を元のパイプに戻すと、curl が呼ばれずこのテストが落ちる。
"""
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq が必要")


def _run(tmp_path: Path, tool_input: dict):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    curl = bindir / "curl"
    curl.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = "-d" ] && printf "%s\\n" "$2" >> "'
                    + str(log) + '"; shift; done\nexit 0\n')
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    root = tmp_path / "repo"
    shutil.copytree(REPO / "hooks", root / "hooks")
    (root / "registry").mkdir()
    env = {"HOME": str(tmp_path), "PATH": f"{bindir}:/usr/local/bin:/usr/bin:/bin",
           "CREWVIA_REPO_ROOT": str(root), "TASKVIA_URL": "http://127.0.0.1:9",
           "TASKVIA_TOKEN": "dummy", "AGENT_NAME": "Haruto"}
    payload = json.dumps({"tool_name": "Bash", "tool_input": tool_input})
    r = subprocess.run(["bash", str(root / "hooks/post-tool-use.sh")], input=payload,
                       capture_output=True, text=True, env=env, timeout=120)
    posts = [json.loads(x) for x in log.read_text().splitlines() if x.startswith('{"type"')] \
        if log.exists() else []
    return r, posts


@pytest.mark.parametrize("size", [100, 200_000, 1_000_000])
def test_work_log_is_posted_with_an_80_char_summary_for_any_input_size(tmp_path, size):
    r, posts = _run(tmp_path, {"command": "x" * size})
    assert r.returncode == 0, r.stderr[-400:]
    work = [p for p in posts if p.get("type") == "work"]
    assert work, ("work ログが投稿されない (hook が crash した)", r.stderr[-400:])
    content = work[0]["content"]
    assert content.startswith("Bash: ")
    assert len(content) == len("Bash: ") + 80
