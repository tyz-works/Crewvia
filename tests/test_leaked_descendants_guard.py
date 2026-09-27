#!/usr/bin/env python3
"""tests/test_leaked_descendants_guard.py — テストが子孫プロセスを残したら落とす仕組み自身の検査 (t029 / backlog #32)。

`tests/leaked_descendants.py` (構造ガード) と `tests/proc_group.py` (正しい書き方) を検査する。
ガードが「何も検出しないのに緑」になっていないことを、次の 3 層で示す。

1. **判定** — 印を持つ孤児は数える。印も basetemp も持たない他人のプロセス・ゾンビ・テスト前から居たものは数えない
   (本番の plan.sh を誤って数えたり殺したりしない)
2. **本物の欠陥** — `subprocess.run(timeout=)` で bash だけ殺すと FIFO を open して待つ python が孤児で残る、
   という backlog #32 の形そのものを **内側の pytest** で再現し、ガードが落とすこと。同じ形を
   `run_in_own_group()` で書いたら通ること (陰性対照)
3. **ヘルパー** — `run_in_own_group()` がタイムアウトで木ごと殺し、`TimeoutExpired` を上げること

内側の pytest は tmp_path の中の conftest / テストを使う (このファイルの下で本物の pytest を走らせる)。
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import time

import pytest

import leaked_descendants
from proc_group import kill_group, kill_tree, run_in_own_group

TESTS_DIR = pathlib.Path(__file__).resolve().parent

pytestmark = pytest.mark.skipif(
    not leaked_descendants.available(), reason="/proc が無い環境ではガード自体が動かない")


def _alive(pid: int) -> bool:
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return raw[raw.rfind(")") + 2:].split()[0] not in ("Z", "X")


def _wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _marker() -> str:
    return os.environ[leaked_descendants.MARKER_VAR]


def _spawn_orphan(env=None, cwd=None) -> int:
    """`sh -c 'sleep 60 & wait'` の親を殺して、sleep を孤児にする。孤児の pid を返す。"""
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE, text=True,
        env=env, cwd=cwd, start_new_session=True)
    child = int(proc.stdout.readline())
    proc.stdout.close()
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    return child


@pytest.fixture
def basetemp(tmp_path_factory) -> str:
    return str(tmp_path_factory.getbasetemp())


# --- 1. 判定 ---------------------------------------------------------------------------


def test_scan_counts_an_orphan_that_carries_the_session_marker(basetemp):
    before = leaked_descendants.snapshot()
    pid = _spawn_orphan()
    try:
        result = leaked_descendants.scan(before, _marker(), basetemp)
        found = {s.pid: s for s in result.survivors}
        assert pid in found, f"印を継承した孤児を数えていない: {list(found)}"
        assert found[pid].via == "env-marker"
        assert found[pid].ppid == 1 or found[pid].ppid != os.getpid()
    finally:
        os.kill(pid, signal.SIGKILL)
    assert _wait_dead(pid)


def test_scan_does_not_count_a_process_with_neither_marker_nor_basetemp(basetemp):
    """別セッション / 本番のプロセスに相当する: 環境を捨て、basetemp の外で動く。"""
    before = leaked_descendants.snapshot()
    pid = _spawn_orphan(env={"PATH": "/usr/bin:/bin"}, cwd="/")
    try:
        result = leaked_descendants.scan(before, _marker(), basetemp)
        assert pid not in {s.pid for s in result.survivors}, (
            "印も basetemp も持たない (= このセッションの子孫と言えない) プロセスを数えた。"
            "本番の plan.sh / デーモンを kill しうる")
        # 別セッションの印は、セッションごとに違う値なので数えない
        other = leaked_descendants.scan(before, "0-deadbeef", basetemp)
        assert pid not in {s.pid for s in other.survivors}
    finally:
        os.kill(pid, signal.SIGKILL)
    assert _wait_dead(pid)


def test_scan_counts_a_descendant_that_dropped_its_env_but_lives_in_basetemp(basetemp, tmp_path):
    """`env -i` で印を捨てた子孫も、sandbox (basetemp の中) で動く限り捕まる。"""
    before = leaked_descendants.snapshot()
    pid = _spawn_orphan(env={"PATH": "/usr/bin:/bin"}, cwd=str(tmp_path))
    try:
        result = leaked_descendants.scan(before, _marker(), basetemp)
        found = {s.pid: s for s in result.survivors}
        assert pid in found, "env を捨てた子孫を basetemp で捕まえられない"
        assert found[pid].via == "cwd-in-basetemp"
    finally:
        os.kill(pid, signal.SIGKILL)
    assert _wait_dead(pid)


def test_scan_ignores_processes_that_were_there_before(basetemp):
    pid = _spawn_orphan()
    try:
        before = leaked_descendants.snapshot()      # 孤児は「前から居た」側に入る
        result = leaked_descendants.scan(before, _marker(), basetemp)
        assert pid not in {s.pid for s in result.survivors}
    finally:
        os.kill(pid, signal.SIGKILL)
    assert _wait_dead(pid)


def test_scan_does_not_count_a_zombie(basetemp):
    """kill 済みで回収待ちのゾンビは死んでいる (memory: killed-subprocess-zombie-looks-alive)。"""
    before = leaked_descendants.snapshot()
    proc = subprocess.Popen(["sleep", "60"])          # 親 (このプロセス) が wait しない = ゾンビになる
    try:
        os.kill(proc.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while _alive(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pathlib.Path(f"/proc/{proc.pid}/stat").exists(), "ゾンビが作れていない (前提が崩れた)"
        result = leaked_descendants.scan(before, _marker(), basetemp)
        assert proc.pid not in {s.pid for s in result.survivors}
    finally:
        proc.wait()


def test_scan_does_not_count_the_pytest_process_itself(basetemp):
    result = leaked_descendants.scan(set(), _marker(), basetemp)
    assert os.getpid() not in {s.pid for s in result.survivors}


# --- 2. 本物の欠陥を内側の pytest で ---------------------------------------------------------

_INNER_CONFTEST = """\
import leaked_descendants

def pytest_configure(config):
    leaked_descendants.install(config)
"""

#: backlog #32 の形そのもの: bash の下の python が FIFO の open() で待つ。plan.sh の
#: `python3 - <queue> ...` に当たる。
_INNER_TESTS = '''\
import os
import subprocess
import sys

from proc_group import run_in_own_group

BLOCKED = "import sys; open(sys.argv[1])"      # 書き手のいない FIFO の open() は返らない


def _fifo(tmp_path):
    p = tmp_path / "state.yaml"
    os.mkfifo(p)
    return p


def _cmd(fifo):
    return ["bash", "-c", f'{sys.executable} -c "{BLOCKED}" {fifo}; true']


def test_plain_run_leaves_the_python_child(tmp_path):
    try:
        subprocess.run(_cmd(_fifo(tmp_path)), timeout=2)
    except subprocess.TimeoutExpired:
        pass


def test_run_in_own_group_leaves_nothing(tmp_path):
    try:
        run_in_own_group(_cmd(_fifo(tmp_path)), timeout=2)
    except subprocess.TimeoutExpired:
        pass
'''


def _inner_pytest(tmp_path: pathlib.Path, *node_ids: str) -> subprocess.CompletedProcess:
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "conftest.py").write_text(_INNER_CONFTEST, encoding="utf-8")
    (inner / "test_inner.py").write_text(_INNER_TESTS, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TESTS_DIR)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return run_in_own_group(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
         "--rootdir", str(inner), "--basetemp", str(inner / "bt"),
         *[f"{inner / 'test_inner.py'}::{n}" for n in node_ids]],
        cwd=str(inner), env=env, timeout=120)


def test_guard_fails_the_test_that_leaves_the_fifo_python_and_kills_it(tmp_path):
    proc = _inner_pytest(tmp_path, "test_plain_run_leaves_the_python_child")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"孤児の python を残したテストが緑になった (ガードが空振り):\n{out}"
    assert "子孫プロセスを" in out and "test_plain_run_leaves_the_python_child" in out, out
    assert "state.yaml" in out, f"残ったプロセスの cmdline が出ていない: {out}"
    assert "1 error" in out, f"残した 1 本が ERROR になっていない: {out}"
    assert "検査したテスト: 1 件 / 残された子孫を検出したテスト: 1 件" in out, f"検査件数が出ていない: {out}"
    # kill 済みであること: FIFO を開いたまま待つ python がもう居ない
    still = subprocess.run(["pgrep", "-f", str(tmp_path / "inner")], capture_output=True, text=True)
    live = [p for p in still.stdout.split() if _alive(int(p))]
    assert not live, f"ガードが残った子孫を kill していない: {live}"


def test_guard_passes_the_same_shape_written_with_run_in_own_group(tmp_path):
    """陰性対照: 同じ形でも木ごと殺していれば緑。ガードが「何でも落とす」ものでないこと。"""
    proc = _inner_pytest(tmp_path, "test_run_in_own_group_leaves_nothing")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"木ごと殺しているのに落ちた:\n{out}"
    assert "1 passed" in out, out
    assert "検査したテスト: 1 件 / 残された子孫を検出したテスト: 0 件" in out, (
        f"陰性対照が『検査 0 件で緑』になっていないことを件数で確かめる: {out}")


# --- 3. ヘルパー -------------------------------------------------------------------------------


def test_run_in_own_group_kills_the_whole_tree_on_timeout(tmp_path):
    marker_file = tmp_path / "pids"
    cmd = ["bash", "-c", f"sleep 60 & echo $! > {marker_file}; wait"]
    with pytest.raises(subprocess.TimeoutExpired):
        run_in_own_group(cmd, timeout=1.5)
    child = int(marker_file.read_text().strip())
    assert _wait_dead(child), "タイムアウトしたのに孫の sleep が生きている (bash しか殺していない)"


def test_run_in_own_group_returns_a_completed_process_like_subprocess_run():
    done = run_in_own_group(["bash", "-c", "echo out; echo err >&2; exit 3"], timeout=10)
    assert (done.returncode, done.stdout, done.stderr) == (3, "out\n", "err\n")


def test_run_in_own_group_sweeps_a_child_left_after_bash_exits(tmp_path):
    """bash が先に正常終了しても、残った孫を放置しない。"""
    marker_file = tmp_path / "pids"
    # 孫が stdout / stderr のパイプを持ったままだと communicate が EOF を待つので、パイプは渡さない。
    done = run_in_own_group(
        ["bash", "-c", f"sleep 60 >/dev/null 2>&1 & echo $! > {marker_file}"], timeout=10)
    assert done.returncode == 0
    child = int(marker_file.read_text().strip())
    assert _wait_dead(child), "bash が終わったあとに孫が残った"


def _spawn_shell_with_background_job(tmp_path, name):
    """`set -m` (job control) の bash が裏の仕事を **別のプロセスグループ** に置く形。(shell, 裏の仕事の pid)。"""
    marker_file = tmp_path / name
    proc = subprocess.Popen(
        ["bash", "-c", f"set -m; sleep 60 >/dev/null 2>&1 & echo $! > {marker_file}; wait"],
        start_new_session=True)
    deadline = time.monotonic() + 5
    while not marker_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child = int(marker_file.read_text().strip())
    assert os.getpgid(child) != os.getpgid(proc.pid), "前提: 子が別グループに居ない"
    return proc, child


def test_kill_tree_reaches_a_child_in_another_process_group(tmp_path):
    """対話シェルの裏の仕事は別のプロセスグループに居る (job control)。`kill_group` では届かない相手。"""
    # 対照: kill_group では届かない (この前提が崩れたら kill_tree が要る理由が無い)
    shell_a, child_a = _spawn_shell_with_background_job(tmp_path, "a")
    try:
        kill_group(shell_a)
        shell_a.wait()
        assert _alive(child_a), "前提: kill_group では別グループの子に届かないはず"
    finally:
        os.kill(child_a, signal.SIGKILL)
    assert _wait_dead(child_a)

    shell_b, child_b = _spawn_shell_with_background_job(tmp_path, "b")
    try:
        kill_tree(shell_b.pid)
        shell_b.wait()
        assert _wait_dead(child_b), "kill_tree が別グループの子孫に届いていない"
    finally:
        if _alive(child_b):
            os.kill(child_b, signal.SIGKILL)


def test_kill_group_is_a_noop_when_the_group_is_gone():
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    kill_group(proc)        # 例外を出さない
