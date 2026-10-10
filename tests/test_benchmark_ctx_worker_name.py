#!/usr/bin/env python3
"""benchmark-ctx.sh は不正な `--worker` 名を、何も起動する前に exit 1 で断る (PR #294 Codex P2)。

名前は指示文の `--agent <名前>` にクォートなしで貼られる。空白は別名で task を取らせ、
`;` や `$(...)` はシェルで実行されうる。検証は lib_agent_name の 1 つの定義を使う。

    python3 -m pytest tests/test_benchmark_ctx_worker_name.py -v
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "benchmark-ctx.sh"


def _run(tmp_path, name):
    marker = tmp_path / "side-effect"
    env = {k: v for k, v in os.environ.items() if k != "AGENT_NAME"}
    # claude / mux が呼ばれたら marker ができる shim を PATH の先頭に置く
    shim = tmp_path / "bin"
    shim.mkdir(exist_ok=True)
    for cmd in ("claude", "tmux", "herdr"):
        p = shim / cmd
        p.write_text(f"#!/bin/sh\ntouch {marker}\n")
        p.chmod(0o755)
    env["PATH"] = f"{shim}:{env['PATH']}"
    r = subprocess.run(["bash", str(SCRIPT), "--strategy", "A", "--worker", name, "--dry-run"],
                       capture_output=True, text=True, env=env, timeout=60, cwd=tmp_path)
    return r, marker


@pytest.mark.parametrize("name", [
    "Bench Worker", "a;touch x", "$(touch x)", "`id`", "a'b", " Luna", "x.restarting", "x.identity", ".hidden", "a/b",
])
def test_invalid_worker_name_exits_1_before_any_side_effect(tmp_path, name):
    r, marker = _run(tmp_path, name)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "invalid worker name" in r.stderr, r.stderr
    assert not marker.exists()
    assert "strategy=" not in r.stderr  # 最初のログ行より前に止まる


def test_valid_worker_name_passes_the_check(tmp_path):
    r, _ = _run(tmp_path, "BenchWorker")
    assert "invalid worker name" not in r.stderr, r.stderr
