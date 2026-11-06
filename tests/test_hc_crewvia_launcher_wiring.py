#!/usr/bin/env python3
"""tests/test_hc_crewvia_launcher_wiring.py

`./crewvia`（トップレベルの launcher）の mode: 読み取りと到達チェックに `hc` を
足した配線のテスト（t002）。`scripts/start.sh` の director 専用ゲート
（`tests/test_rex_pr2_wiring.py` の `_run_mode_block`）とは別の場所——`./crewvia`
自身が config の `mode:` を読んで `CREWVIA_MUX` を決める箇所——で、同じ形の事故
（未知の mode 扱いにされて黙って tmux に倒れる）が起きうることを確かめる。

本物の `hc` / `herdr` / `rex` には一切触れない（偽のバイナリを PATH に置くだけ）。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "crewvia"


def _mode_block() -> str:
    # `case "$SUBCMD" in` appears twice (the NEEDS_MUX classifier, then the final
    # subcommand dispatch) — `rindex` so the slice keeps the herdr/rex/hc
    # reachability checks, which sit *between* the two.
    text = LAUNCHER.read_text(encoding="utf-8")
    a = text.index("if [[ -n \"${CREWVIA_TMUX:-}\" ]]; then")
    b = text.rindex('case "$SUBCMD" in')
    return text[a:b]


def _run_mode_block(tmp_path, *, env_mux, config_mode, have=("rex", "herdr", "hc"),
                    subcmd="director"):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for b in have:
        f = bindir / b
        f.write_text("#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    log = tmp_path / "calls.log"
    (scripts / "lib_mux.py").write_text(
        "import os, sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + ' MUX=' + os.environ.get('CREWVIA_MUX', '') + '\\n')\n"
        "sys.exit(0)\n")
    cfg = tmp_path / "crewvia.yaml"
    cfg.write_text((f"mode: {config_mode}\n" if config_mode else "") + "other: 1\n")
    script = tmp_path / "block.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'SCRIPT_DIR={scripts.parent}\nCONFIG_FILE={cfg}\nSUBCMD={subcmd}\nNEEDS_MUX=1\n'
        "read_config() {\n"
        "  local key=\"$1\" default=\"${2:-}\"\n"
        "  if [[ -f \"$CONFIG_FILE\" ]]; then\n"
        "    local val\n"
        "    val=$(grep -E \"^${key}:\" \"$CONFIG_FILE\" 2>/dev/null | head -1 | "
        "sed 's/^[^:]*:[[:space:]]*//' | sed 's/[[:space:]]*#.*//' | tr -d '\"' | tr -d \"'\")\n"
        "    if [[ -n \"$val\" ]]; then echo \"$val\"; return; fi\n"
        "  fi\n"
        "  echo \"$default\"\n"
        "}\n"
        + _mode_block() +
        'echo "RESULT MUX=${CREWVIA_MUX:-}"\n')
    env = {"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin", "HOME": str(tmp_path)}
    if env_mux:
        env["CREWVIA_MUX"] = env_mux
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       env=env, stdin=subprocess.DEVNULL)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


def test_edited_launcher_parses():
    r = subprocess.run(["bash", "-n", str(LAUNCHER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_mode_hc_in_config_is_not_an_unknown_mode(tmp_path):
    """t002 で見つけた事故の再現: `mode: hc` の case 文に `hc` が無いと、
    `*)` の catch-all に落ちて「不明な mode」警告つきで `tmux` に強制される。"""
    r, calls = _run_mode_block(tmp_path, env_mux=None, config_mode="hc")
    assert r.returncode == 0, r.stderr
    assert "RESULT MUX=hc" in r.stdout, r.stdout
    assert "不明な mode" not in (r.stdout + r.stderr)


def test_mode_hc_runs_the_reachability_check(tmp_path):
    r, calls = _run_mode_block(tmp_path, env_mux=None, config_mode="hc")
    assert r.returncode == 0, r.stderr
    assert calls == ["available MUX=hc"]


def test_mode_hc_binary_missing_exits_with_a_clear_error(tmp_path):
    r, calls = _run_mode_block(tmp_path, env_mux=None, config_mode="hc", have=("rex", "herdr"))
    assert r.returncode == 1
    assert "hc バイナリが見つかりません" in r.stderr


@pytest.mark.parametrize("config_mode", ["tmux", "herdr", "rex", "inline"])
def test_other_modes_are_unaffected(tmp_path, config_mode):
    r, calls = _run_mode_block(tmp_path, env_mux=None, config_mode=config_mode)
    assert r.returncode == 0, r.stderr
    want = config_mode if config_mode != "inline" else ""
    assert f"RESULT MUX={want}" in r.stdout, r.stdout
    assert "不明な mode" not in (r.stdout + r.stderr)


def test_stop_script_does_not_stop_an_unrelated_herdr_server_for_hc(tmp_path):
    """`crewvia-stop.sh --full` は backend ごとの分岐が無いと catch-all の
    `herdr server stop` に落ちる — hc を使っているのに無関係な herdr server を
    止めてしまう事故の再現・固定。"""
    script = (REPO / "scripts" / "crewvia-stop.sh").read_text(encoding="utf-8")
    assert "hc server は停止しません" in script
    marker = script.index('"$_STOP_BACKEND" == "hc"')
    herdr_stop = script.index("herdr server stop 2>&1")
    assert marker < herdr_stop, \
        "hc の分岐が herdr server stop より後にあると catch-all に落ちてから判定される"
