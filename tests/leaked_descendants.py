#!/usr/bin/env python3
"""tests/leaked_descendants.py — テストが起こした子孫プロセスを、テストの終わりまでに残させない (t029 / backlog #32)。

## 何を止めるか

FIFO のテストは plan.sh を `subprocess.run(timeout=)` で走らせる。返らなければ赤にする作りだが、
タイムアウトで kill されるのは直接の子 (bash) だけで、plan.sh の下の `python3 - <queue> ...` は
孤児になり、書き手のいない FIFO を open したまま **永久に** 待つ (`wchan = wait_for_partner`)。
誰も回収せず、全 pytest を回すたびに数個ずつ溜まった (2026-09-26 に 22 個・287MB)。
WSL のメモリは 6〜8GB でスラッシングの前例がある。`tests/proc_group.py` がその直し方 (グループごと殺す)
で、ここはそれを **仕組みで強制する** 側 — 直し忘れたテストが次に出たら、その 1 本が赤くなる。

## 仕組み

1. セッションの印を `os.environ` に置く (`CREWVIA_PYTEST_SESSION=<pid>-<hex>`)。子孫は継承する。
2. 各テストの前に「いま居るプロセス」の一覧を取り (`snapshot`)、後で **増えていて生きているもの** を探す。
3. 「このセッションの子孫」の判定は 2 つの OR:
   - 環境に印がある (`/proc/<pid>/environ`)
   - cmdline か cwd が、このセッションの pytest 一時ディレクトリ (basetemp) を指している
     (`env -i` 等で環境を捨てた子孫も、sandbox の中で動く限り捕まる)
4. 見つけたら **kill して**、そのテストを ERROR にする (残すと次のテストに溜まる)。メッセージに
   pid・ppid・状態・wchan・cmdline を出す。
5. セッションの終わりに、もう一度印で探す (module / session スコープの fixture が残したもののため)。

## 本番を誤って数えない・殺さない

判定は「祖先が pytest か」を **環境の印 + 自分の basetemp** で行う。本番の plan.sh / dispatcher /
watchdog は、このセッションの環境を継承していないので印を持たず、basetemp も指さない。
別セッションの pytest (同時に走る別の Worker) も、印がセッションごとに違うので数えない。
同じ uid のプロセスだけを見る。ゾンビ (`Z`) は死んでいるので数えない (memory:
killed-subprocess-zombie-looks-alive)。pytest 自身は数えない。

## 倒す向き

- 環境が読めない同 uid のプロセス (`dumpable=0` 等) は **「無い」にしない**: 観測できなかった数として
  メッセージに残す (落とす根拠にはしないが、黙って捨てない)。
- `/proc` が無い環境 (macOS) ではガードは動けない。**動かないことを警告として出す**
  (黙って全部通さない)。
"""

from __future__ import annotations

import os
import pathlib
import signal
import time
import uuid
import warnings

import pytest

MARKER_VAR = "CREWVIA_PYTEST_SESSION"

#: kill したあと死ぬのを待つ / 増えたプロセスが自然に終わるのを待つ上限 (秒)。
GRACE_SECONDS = 1.5
_POLL_SECONDS = 0.05

_PROC = pathlib.Path("/proc")


def available() -> bool:
    return (_PROC / "self" / "stat").exists()


class Survivor:
    """生き残りの 1 件 (kill する前に読んだ観測)。"""

    def __init__(self, pid: int, ppid: int, state: str, age_s: float, wchan: str, cmdline: str, via: str):
        self.pid, self.ppid, self.state, self.age_s = pid, ppid, state, age_s
        self.wchan, self.cmdline, self.via = wchan, cmdline, via

    def describe(self) -> str:
        return (f"pid={self.pid} ppid={self.ppid} state={self.state} age={self.age_s:.0f}s "
                f"wchan={self.wchan or '-'} via={self.via}\n      {self.cmdline[:300]}")


class Scan:
    """1 回の走査の結果。`unobservable` は「同 uid なのに環境が読めなかった」pid の数。"""

    def __init__(self):
        self.survivors: list[Survivor] = []
        self.unobservable = 0


def _read_stat(pid: int):
    """(state, ppid, starttime_ticks) — 読めなければ None。`comm` は括弧の中に空白を含みうる。"""
    try:
        raw = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    rp = raw.rfind(")")
    if rp < 0:
        return None
    rest = raw[rp + 2:].split()
    try:
        return rest[0], int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None


def _read_bytes(path: pathlib.Path):
    try:
        return path.read_bytes()
    except OSError:
        return None


def pids() -> set[int]:
    out = set()
    try:
        names = os.listdir(_PROC)
    except OSError:
        return out
    for name in names:
        if name.isdigit():
            out.add(int(name))
    return out


def snapshot() -> set[int]:
    """この時点で居るプロセスの pid 一覧 (pid の再利用は 1 テストの長さでは起きない前提)。"""
    return pids()


def _uptime() -> float:
    try:
        return float((_PROC / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _belongs(pid: int, marker: bytes, basetemp: bytes):
    """(所属する理由 | None, 観測できたか)。同 uid の pid の environ が読めなければ観測できていない。"""
    base = _PROC / str(pid)
    environ = _read_bytes(base / "environ")
    if environ is None:
        observed = False
    else:
        observed = True
        if marker in environ.split(b"\0"):
            return "env-marker", True
    cmdline = _read_bytes(base / "cmdline")
    if cmdline is not None and basetemp and basetemp in cmdline:
        return "cmdline-in-basetemp", True
    try:
        cwd = os.readlink(base / "cwd").encode()
    except OSError:
        cwd = None
    if cwd is not None and basetemp and (cwd == basetemp or cwd.startswith(basetemp + b"/")):
        return "cwd-in-basetemp", True
    return None, observed


def scan(exclude: set[int], marker_value: str, basetemp: str) -> Scan:
    """`exclude` に無い、生きていて、このセッションの子孫と判定できるプロセスを集める。"""
    result = Scan()
    marker = f"{MARKER_VAR}={marker_value}".encode()
    base = basetemp.encode()
    me, uid = os.getpid(), os.getuid()
    boot_now = _uptime()
    hz = os.sysconf("SC_CLK_TCK")
    for pid in sorted(pids() - exclude):
        if pid == me:
            continue
        stat = _read_stat(pid)
        if stat is None:
            if (_PROC / str(pid)).exists():
                result.unobservable += 1   # 居るのに stat が読めない / 読み取れない形 —— 「無い」にしない
            continue                       # (居なければ走査中に死んだだけ)
        state, ppid, start = stat
        if state in ("Z", "X"):
            continue                       # 死んでいる (回収待ちのゾンビ)
        try:
            if (_PROC / str(pid)).stat().st_uid != uid:
                continue                   # 別ユーザーのプロセスは見ない
        except OSError:
            continue
        why, observed = _belongs(pid, marker, base)
        if why is None:
            if not observed:
                result.unobservable += 1
            continue
        cmd = _read_bytes(_PROC / str(pid) / "cmdline") or b""
        wchan = (_read_bytes(_PROC / str(pid) / "wchan") or b"").decode(errors="replace")
        result.survivors.append(Survivor(
            pid, ppid, state, max(0.0, boot_now - start / hz), wchan,
            cmd.replace(b"\0", b" ").decode(errors="replace").strip(), why))
    return result


def kill_all(survivors: list[Survivor]) -> None:
    """判定済みの生き残りだけを SIGKILL する。死ぬ (ゾンビ含む) のを `GRACE_SECONDS` まで待つ。"""
    for s in survivors:
        try:
            os.kill(s.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        alive = [s for s in survivors if (_read_stat(s.pid) or ("Z",))[0] not in ("Z", "X")]
        if not alive:
            return
        time.sleep(_POLL_SECONDS)


def settle(exclude: set[int], marker_value: str, basetemp: str) -> Scan:
    """増えたプロセスが自然に終わるのを `GRACE_SECONDS` まで待ってから、残ったものを返す。

    テストの後片付けで殺されたばかりのプロセスが、まだ終わり切っていないだけの場合を残りに数えない。
    """
    deadline = time.monotonic() + GRACE_SECONDS
    result = scan(exclude, marker_value, basetemp)
    while result.survivors and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        result = scan(exclude, marker_value, basetemp)
    return result


def format_failure(where: str, result: Scan) -> str:
    lines = [f"{where}: このセッションのテストが子孫プロセスを {len(result.survivors)} 個残した (kill 済み)。",
             "  テストの後片付け漏れ。plan.sh のような bash の下の子孫は `subprocess.run(timeout=)` では "
             "bash しか殺されない —— `tests/proc_group.py` の `run_in_own_group()` / `kill_group()` を使う。"]
    lines += [f"  - {s.describe()}" for s in result.survivors]
    if result.unobservable:
        lines.append(f"  (環境が読めず観測できなかった同 uid のプロセス: {result.unobservable} 個。数には入れていない)")
    return "\n".join(lines)


class LeakGuard:
    """pytest プラグイン。`install()` が登録する。fixture と sessionfinish を持つ。"""

    def __init__(self, marker_value: str):
        self.marker_value = marker_value
        self.basetemp = ""
        self.checked_tests = 0          # 実際に走査したテストの数 (0 件で PASS にしない)
        self.leaks: list[str] = []

    @pytest.fixture(autouse=True)
    def _crewvia_no_leaked_descendants(self, request, tmp_path_factory):
        # autouse は関数スコープの fixture の中で最初に立つので、最後に降ろされる —— テストの
        # 他の fixture (worker_pane など) が自分で片付けたあとの状態を見る。
        self.basetemp = str(tmp_path_factory.getbasetemp())
        before = snapshot()
        yield
        result = settle(before, self.marker_value, self.basetemp)
        self.checked_tests += 1
        if result.survivors:
            message = format_failure(request.node.nodeid, result)
            kill_all(result.survivors)
            self.leaks.append(message)
            pytest.fail(message, pytrace=False)

    def pytest_terminal_summary(self, terminalreporter):
        """検査した件数を毎回出す（0 件で PASS にしない）。"""
        terminalreporter.write_line(
            f"[leaked-descendants] 検査したテスト: {self.checked_tests} 件 / "
            f"残された子孫を検出したテスト: {len(self.leaks)} 件")

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        """module / session スコープの fixture が残したもののための最後の網。"""
        if not available():
            return
        result = scan(set(), self.marker_value, self.basetemp)
        if result.survivors:
            message = format_failure("session finish", result)
            kill_all(result.survivors)
            self.leaks.append(message)
            print("\n" + message)
            session.exitstatus = int(pytest.ExitCode.TESTS_FAILED)


def install(config) -> LeakGuard | None:
    """conftest の `pytest_configure` から呼ぶ。`/proc` が無ければ警告して何もしない。"""
    if not available():
        warnings.warn(
            "tests/leaked_descendants.py: /proc が無いので、テストが残した子孫プロセスの検査は動かない "
            "(黙って通さないためにこの警告を出す)", pytest.PytestWarning)
        return None
    marker_value = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    os.environ[MARKER_VAR] = marker_value
    guard = LeakGuard(marker_value)
    config.pluginmanager.register(guard, "crewvia-leak-guard")
    return guard
