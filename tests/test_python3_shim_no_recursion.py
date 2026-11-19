"""scripts/bin/python3 shim が別 checkout の同じ shim を呼んで無限再帰しない (t001 / 20261119-ci-2604-unpin-shim-recursion)。

PATH に別の checkout の scripts/bin (= 同じ shim) が並び、python3.12 が無いホストで、
A が B を `-c` で版検査 → B が A を… と再帰した (#414 レビューの P3)。

【封じ込め — 消さないこと】再帰する shim を subprocess.run(timeout=) で走らせると、timeout で
殺されるのは直接の子だけで、孫の shim 連鎖が生き残って増え続ける (attempt 2 で約 9,500
プロセスになり rex.service の pids.max を埋めて crewvia 全体が 6 時間止まった)。そこで shim は
別 process group (start_new_session) で起動し、timeout したら group ごと SIGSTOP (増殖を止める)
→ SIGKILL する。将来 shim が退行しても CI runner を潰さないための備え。
"""
import os
import signal
import time
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parent.parent / "scripts" / "bin" / "python3"
TOOLS = ("sh", "dirname", "cmp")


def _make_path(tmp_path, copies, stub_version=None):
    """python3.12 の無い PATH を作る。copies 個の shim の複製 + (任意) 偽 python3 の dir。"""
    tools = tmp_path / "tools"
    tools.mkdir()
    for t in TOOLS:
        real = shutil.which(t)
        assert real, t
        (tools / t).symlink_to(real)
    dirs = []
    for i in range(copies):
        d = tmp_path / f"checkout{i}" / "scripts" / "bin"
        d.mkdir(parents=True)
        shutil.copy(SHIM, d / "python3")
        dirs.append(d)
    if stub_version is not None:
        s = tmp_path / "stub"
        s.mkdir()
        p = s / "python3"
        major, minor = stub_version
        # -c の版検査には版どおりに答え、それ以外は印を出す
        p.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ]; then\n'
            f"  [ {minor} -ge 12 ] && exit 0 || exit 1\n"
            "fi\n"
            'echo "stub-python3 $*"\n'
        )
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        dirs.append(s)
    dirs.append(tools)
    return ":".join(str(d) for d in dirs), dirs[0] / "python3"


def _kill_group(pgid):
    """group が空になるまで STOP → KILL を繰り返す (STOP で増殖を止めてから殺す)。"""
    for _ in range(10):
        try:
            os.killpg(pgid, signal.SIGSTOP)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        time.sleep(0.05)


def _run(path, first, timeout=10):
    env = {"PATH": path}
    p = subprocess.Popen(
        [str(first), "hello"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p.pid)
        p.communicate()
        pytest.fail("shim が timeout (再帰している)")
    finally:
        _kill_group(p.pid)
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


def test_two_shims_fall_through_to_real_python3(tmp_path):
    path, first = _make_path(tmp_path, copies=2, stub_version=(3, 14))
    r = _run(path, first)
    assert r.returncode == 0, r.stderr
    assert "stub-python3 hello" in r.stdout


def test_two_shims_and_nothing_else_exit_127_without_recursing(tmp_path):
    path, first = _make_path(tmp_path, copies=2)
    r = _run(path, first)  # 再帰すると TimeoutExpired
    assert r.returncode == 127
    assert "python3" in r.stderr


def test_old_python_is_not_used_silently(tmp_path):
    path, first = _make_path(tmp_path, copies=2, stub_version=(3, 10))
    r = _run(path, first)
    assert r.returncode == 127
