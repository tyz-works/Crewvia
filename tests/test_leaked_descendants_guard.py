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

import kill_budget
import leaked_descendants
from proc_group import descendants, kill_group, kill_tree, run_in_own_group

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


def _wait_observable(pid: int, seconds: float = 5.0) -> bool:
    """`/proc/<pid>/environ` が **空でなく** 読めるようになるまで待つ。

    exec の最中は environ が 0 バイトで読める (実測 2.7%、1ms 未満で解消)。その隙に走査すると
    印を持つ子孫を観測できず、判定そのものを見たいテストが揺れる (2026-09-27 の flaky)。
    ガード側は空を「観測できなかった」に倒すので黙って消えることはもう無いが、**判定を見る
    テストは観測できる状態になってから走査する**。
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if pathlib.Path(f"/proc/{pid}/environ").read_bytes():
                return True
        except OSError:
            pass
        time.sleep(0.005)
    return False


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
        assert _wait_observable(pid), "孤児の environ が読めるようにならない (前提が崩れた)"
        result = leaked_descendants.scan(before, _marker(), basetemp)
        found = {s.pid: s for s in result.survivors}
        assert pid in found, (
            f"印を継承した孤児を数えていない: {list(found)} "
            f"(観測できなかった同 uid のプロセス: {result.unobservable} 個)")
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
        # 観測できない隙に走査すると「数えなかった」が **観測できなかっただけ** になり、
        # このテストが理由なく緑になる (0 件で PASS にしない / tests/CLAUDE.md)。
        assert _wait_observable(pid), "他人役の environ が読めるようにならない (前提が崩れた)"
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
        assert _wait_observable(pid), "子孫の environ が読めるようにならない (前提が崩れた)"
        result = leaked_descendants.scan(before, _marker(), basetemp)
        found = {s.pid: s for s in result.survivors}
        assert pid in found, (
            f"env を捨てた子孫を basetemp で捕まえられない "
            f"(観測できなかった同 uid のプロセス: {result.unobservable} 個)")
        assert found[pid].via == "cwd-in-basetemp"
    finally:
        os.kill(pid, signal.SIGKILL)
    assert _wait_dead(pid)


def test_an_empty_environ_is_not_counted_as_observed(monkeypatch):
    """environ が 0 バイトで読めるのは **観測の失敗** (`knowledge/empty-vs-unobservable.md` の O)。

    「読めた・印が無い」に潰すと、exec の最中の子孫が survivors にも unobservable にも入らず
    黙って消える。実測では孤児を起こした直後の 1 読みで 300 回中 8 回 (2.7%) が空だった。
    実物のレースは時刻依存なので、ここは読み取りを差し替えて判定そのものを固定する。
    """
    monkeypatch.setattr(
        leaked_descendants, "_read_bytes",
        lambda path: b"" if path.name == "environ" else None)
    why, observed = leaked_descendants._belongs(
        os.getpid(), f"{leaked_descendants.MARKER_VAR}=no-such-session".encode(), b"/nonexistent")
    assert why is None
    assert observed is False, (
        "空の environ を「観測できた」にすると、exec 中の印つき子孫を黙って見逃す")


def test_a_readable_environ_does_not_mask_a_failed_cmdline_read(monkeypatch):
    """environ は読めて (印が無い) も、その先の cmdline が読めなければ「確認できた」にしない。

    env を捨てた子孫の basetemp 判定は cmdline / cwd に頼る (`_dir_in_cmdline` /
    cwd-in-basetemp)。environ と同じ exec 遷移の隙が cmdline にも起こりうるので、ここが
    読めないだけで「basetemp 配下ではないと確認できた」に倒してはいけない
    (旧実装は environ の読み取りにしか `observed` を連動させていなかった)。
    """
    monkeypatch.setattr(
        leaked_descendants, "_read_bytes",
        lambda path: b"PATH=/usr/bin\0" if path.name == "environ" else None)
    why, observed = leaked_descendants._belongs(
        os.getpid(), f"{leaked_descendants.MARKER_VAR}=no-such-session".encode(), b"/nonexistent")
    assert why is None
    assert observed is False, (
        "environ は読めたが cmdline が読めなかったのに『観測できた』にした")


def test_a_readable_environ_and_cmdline_do_not_mask_a_failed_cwd_read(monkeypatch):
    """cwd の readlink が失敗しても、同様に『観測できた』にしない (上と同じ理由の cwd 版)。"""
    monkeypatch.setattr(
        leaked_descendants, "_read_bytes",
        lambda path: b"PATH=/usr/bin\0" if path.name == "environ" else b"cat\0")

    def _flaky_readlink(path):
        if str(path).endswith("/cwd"):
            raise OSError("denied")
        return "/"

    monkeypatch.setattr(os, "readlink", _flaky_readlink)
    why, observed = leaked_descendants._belongs(
        os.getpid(), f"{leaked_descendants.MARKER_VAR}=no-such-session".encode(), b"/nonexistent")
    assert why is None
    assert observed is False, (
        "cwd の readlink が失敗したのに『観測できた』にした")


def test_cmdline_match_requires_a_path_boundary_not_a_bare_prefix():
    """`/tmp/pytest-1` は `/tmp/pytest-10/test.py` の前方一致であって同じディレクトリではない。

    素の部分文字列一致 (`basetemp in cmdline`) だと、別の pytest セッションの basetemp
    (`pytest-10`) 配下で動く若いプロセスを、自分の basetemp (`pytest-1`) 配下の子孫と
    誤認し、殺す対象に入れてしまう。
    """
    basetemp = b"/tmp/pytest-1"
    colliding = b"/usr/bin/python3\0/tmp/pytest-10/test.py\0"
    assert not leaked_descendants._dir_in_cmdline(colliding, basetemp), (
        "接頭辞が一致するだけの別ディレクトリ (pytest-10) を basetemp (pytest-1) 配下と誤認した")

    real_subpath = b"/usr/bin/python3\0/tmp/pytest-1/test.py\0"
    assert leaked_descendants._dir_in_cmdline(real_subpath, basetemp), (
        "本物の basetemp 配下の引数を検出できていない")

    exact_arg = b"/tmp/pytest-1"
    assert leaked_descendants._dir_in_cmdline(exact_arg, basetemp), (
        "引数がディレクトリそのものと完全一致する形 (境界が終端) を拒否している"
    )


def test_cmdline_match_rejects_a_prefix_directory_sharing_only_a_suffix():
    """`/backup/tmp/pytest-1` は `/tmp/pytest-1` と末尾が同じだけの別ディレクトリ。

    旧実装は一致の**後ろ**の境界しか見ておらず、前の境界を見ていなかった (2 巡目 codex
    finding 2、直接呼び出しで再現済み)。前の境界も見ないと、こういう別ディレクトリ配下の
    プロセスを basetemp 配下と誤認して kill 対象に入れてしまう。
    """
    basetemp = b"/tmp/pytest-1"
    haystack = b"/usr/bin/python3\0/backup/tmp/pytest-1/job.py\0"
    assert not leaked_descendants._dir_in_cmdline(haystack, basetemp), (
        "接頭ディレクトリが違うだけの別パスを basetemp 配下と誤認した")


def test_cmdline_match_normalizes_dot_dot_before_comparing():
    """`/tmp/pytest-1/../pytest-2/job.py` は文字面に basetemp を含むが実体は隣の pytest-2 配下。"""
    basetemp = b"/tmp/pytest-1"
    haystack = b"/usr/bin/python3\0/tmp/pytest-1/../pytest-2/job.py\0"
    assert not leaked_descendants._dir_in_cmdline(haystack, basetemp), (
        "`..` を正規化せずに部分文字列一致させ、隣の別ディレクトリを basetemp 配下と誤認した")


def test_cmdline_match_still_finds_a_real_subpath_that_only_resolves_after_dot_dot_collapses():
    """逆方向: 文字面には basetemp が現れなくても、正規化すれば本物の basetemp 配下なら検出する。

    見逃す向きの誤りも同じ根っこ (正規化しない部分文字列一致) から起こる —— このガードは
    「見逃さない」側に倒す設計 (`empty-vs-unobservable.md`) なので、こちらも直す。
    """
    basetemp = b"/tmp/pytest-1"
    haystack = b"/usr/bin/python3\0/tmp/pytest-2/../pytest-1/job.py\0"
    assert leaked_descendants._dir_in_cmdline(haystack, basetemp), (
        "正規化すれば本物の basetemp 配下になる引数を検出できていない")


def test_cmdline_match_ignores_a_trailing_slash_on_either_side():
    assert leaked_descendants._dir_in_cmdline(b"/tmp/pytest-1/\0", b"/tmp/pytest-1"), (
        "引数側の末尾スラッシュを理由に検出を落とした")
    assert leaked_descendants._dir_in_cmdline(b"/tmp/pytest-1\0", b"/tmp/pytest-1/"), (
        "basetemp 側の末尾スラッシュを理由に検出を落とした")


def test_cmdline_match_finds_the_value_of_an_equals_form_argument():
    """`--rootdir=<basetemp>/...` のような `=` 付き引数の値側も検出できること。"""
    basetemp = b"/tmp/pytest-1"
    assert leaked_descendants._dir_in_cmdline(b"pytest\0--rootdir=/tmp/pytest-1/sub\0", basetemp), (
        "`=` 付き引数の値側にある basetemp 配下のパスを検出できていない")
    assert not leaked_descendants._dir_in_cmdline(b"pytest\0--rootdir=/tmp/pytest-10/sub\0", basetemp), (
        "`=` 付き引数でも別ディレクトリ (pytest-10) を basetemp (pytest-1) 配下と誤認した")


def test_scan_matches_a_descendant_via_a_symlinked_basetemp(tmp_path):
    """basetemp 自体がシンボリックリンク経由で渡されても、実体化して検出できること。

    `/proc/<pid>/cwd` はカーネルが常に正規化済み (シンボリックリンク解決済み) のパスを
    返す。`tmp_path_factory` の basetemp がシンボリックリンク越しの表記のまま比較されると、
    正規化済みの cwd と文字面で食い違い、黙って見逃す (族B: 観測対象と比較対象の同一性が
    ずれる)。`scan()` は basetemp を 1 回だけ `os.path.realpath` してこれを吸収する。
    """
    real_dir = tmp_path / "real-basetemp"
    real_dir.mkdir()
    link_dir = tmp_path / "link-basetemp"
    link_dir.symlink_to(real_dir)

    before = leaked_descendants.snapshot()
    pid = _spawn_orphan(env={"PATH": "/usr/bin:/bin"}, cwd=str(real_dir))
    try:
        assert _wait_observable(pid), "前提が崩れた"
        result = leaked_descendants.scan(before, _marker(), str(link_dir))
        found = {s.pid: s for s in result.survivors}
        assert pid in found, (
            "basetemp がシンボリックリンク経由でも、実体化して cwd と一致させられていない "
            f"(観測できなかった同 uid のプロセス: {result.unobservable} 個)")
    finally:
        os.kill(pid, signal.SIGKILL)
    assert _wait_dead(pid)


def test_settle_retries_while_unobservable_remains(monkeypatch):
    """survivors が 0 でも unobservable が残っている間は再試行する。

    exec 直後の空 environ のような一過性の観測失敗を、1 回読めなかっただけで
    「クリーンな結果 (unobservable=1 のまま)」として確定させない。
    """
    calls: list[int] = []

    def fake_scan(exclude, marker_value, basetemp):
        calls.append(1)
        result = leaked_descendants.Scan()
        result.unobservable = 0 if len(calls) > 1 else 1
        return result

    monkeypatch.setattr(leaked_descendants, "scan", fake_scan)
    monkeypatch.setattr(leaked_descendants, "GRACE_SECONDS", 1.0)
    monkeypatch.setattr(leaked_descendants, "_POLL_SECONDS", 0.01)
    result = leaked_descendants.settle(set(), "marker", "/tmp")
    assert len(calls) >= 2, "unobservable が残っているのに再試行しなかった (1 回で確定させた)"
    assert result.unobservable == 0


class _FakeSession:
    def __init__(self):
        self.exitstatus = 0


def test_sessionfinish_reports_but_does_not_fail_on_unobservable_only(monkeypatch, capsys):
    """survivors が 0 で unobservable だけが残った session finish は、report はするが落とさない。

    `scan`/`settle` のどちらを呼ぶ実装でも同じ偽の結果を返すよう両方差し替える —— 呼び出し方の
    詳細に依らず「survivors が無くても unobservable を報告する」という振る舞いだけを固定する。
    """
    def fake_result(exclude, marker_value, basetemp):
        result = leaked_descendants.Scan()
        result.unobservable = 2
        return result

    monkeypatch.setattr(leaked_descendants, "settle", fake_result)
    monkeypatch.setattr(leaked_descendants, "scan", fake_result)

    guard_obj = leaked_descendants.LeakGuard("marker-that-matches-nothing")
    session = _FakeSession()
    guard_obj.pytest_sessionfinish(session, 0)

    assert guard_obj.unobservable_only == 1, "unobservable だけの session finish を報告していない"
    assert session.exitstatus == 0, "確認できない観測失敗だけを理由に session を失敗にした"
    assert "観測でき" in capsys.readouterr().out, "unobservable の報告が標準出力に出ていない"


@pytest.mark.skipif(not leaked_descendants.pidfd_supported(), reason="このカーネルは pidfd_open が使えない")
def test_scan_binds_a_working_pidfd_for_each_real_survivor(basetemp):
    """scan() が返す survivor は、観測した瞬間に束縛した pidfd 経由で本物に届く (finding 3)。"""
    before = leaked_descendants.snapshot()
    pid = _spawn_orphan()
    try:
        assert _wait_observable(pid), "孤児の environ が読めるようにならない (前提が崩れた)"
        result = leaked_descendants.scan(before, _marker(), basetemp)
        found = {s.pid: s for s in result.survivors}
        assert pid in found, f"印を継承した孤児を数えていない: {list(found)}"
        survivor = found[pid]
        assert survivor.pidfd is not None, "本物の子孫なのに pidfd を束縛できなかった"
        signal.pidfd_send_signal(survivor.pidfd, signal.SIGKILL)
        os.close(survivor.pidfd)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
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


# --- 1b. UTF-8 でない comm (4巡目 codex review P2-1) ------------------------------------
#
# Linux のプロセス名 (`comm`、`prctl(PR_SET_NAME)` で書き換えられる) は NUL と `/` を除く
# 任意のバイト列を取れる。`_read_stat` / `_ppid_and_start` / `descendants` はどれも
# `/proc/<pid>/stat` の `comm` を含む行を `read_text()` (str) でデコードしており、
# `b'bad-\xff'` のような不正なバイト列 1 つで `UnicodeDecodeError` (`OSError` のサブクラス
# ではない) を投げて走査全体を落とす。`scan()` は所有者を確かめる**前**に `_read_stat` を
# 呼ぶので、無関係な 1 プロセスがいるだけで session-finish の後片付けが動かなくなる。

_RAW_COMM_CHILD = (
    "import ctypes, time, sys\n"
    "libc = ctypes.CDLL(None, use_errno=True)\n"
    "libc.prctl(15, bytes.fromhex(sys.argv[1]), 0, 0, 0)\n"  # 15 = PR_SET_NAME
    "time.sleep(30)\n"
)

#: b"bad-\xff" — 0xff は単独では UTF-8 として不正な継続バイト。コマンドラインへ生バイト列を
#: そのまま埋め込めないので 16 進数で渡し、子の中で `bytes.fromhex` する。
_BAD_COMM_HEX = "6261642dff"


def _spawn_with_raw_comm(name_hex: str) -> subprocess.Popen:
    """`comm` (`/proc/<pid>/stat` の `(...)`) を `name_hex` の生バイト列にした子プロセスを立てる。

    `prctl(PR_SET_NAME)` は root 権限なしにプロセス自身の comm を書き換えられる。exec は comm
    を実行ファイル名にリセットしてしまうので、exec せず既存の Python プロセスのまま prctl する。
    """
    return subprocess.Popen([sys.executable, "-c", _RAW_COMM_CHILD, name_hex])


def _wait_for_comm(pid: int, name_hex: str, seconds: float = 5.0) -> None:
    expected = bytes.fromhex(name_hex)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            raw = pathlib.Path(f"/proc/{pid}/stat").read_bytes()
        except OSError:
            raw = b""
        if expected[:15] in raw:
            return
        time.sleep(0.02)
    raise AssertionError("comm がまだ書き換わっていない (prctl(PR_SET_NAME) が失敗した疑い)")


def test_read_stat_survives_a_non_utf8_process_name():
    """`leaked_descendants._read_stat` は comm が UTF-8 として不正でも落ちない。"""
    child = _spawn_with_raw_comm(_BAD_COMM_HEX)
    try:
        _wait_for_comm(child.pid, _BAD_COMM_HEX)
        stat = leaked_descendants._read_stat(child.pid)
        assert stat is not None, "UTF-8 でない comm を持つだけで stat が読めなくなった"
        state, ppid, start = stat
        assert ppid == os.getpid()
        assert start > 0
    finally:
        child.kill()
        child.wait()


def test_ppid_and_start_survives_a_non_utf8_process_name():
    """`kill_budget._ppid_and_start` も同じ族 (`_read_stat` と同じ解析コードの複製)。"""
    child = _spawn_with_raw_comm(_BAD_COMM_HEX)
    try:
        _wait_for_comm(child.pid, _BAD_COMM_HEX)
        got = kill_budget._ppid_and_start(child.pid)
        assert got is not None, "UTF-8 でない comm を持つだけで stat が読めなくなった"
        assert got[0] == os.getpid()
    finally:
        child.kill()
        child.wait()


def test_descendants_survives_a_non_utf8_process_name():
    """`proc_group.descendants` も同じ族 —— 無関係な UTF-8 でない comm のプロセスに走査中に
    当たっても /proc 全体の走査が落ちない (落ちれば FIFO テストの後片付け `kill_tree` を
    道連れにする)。UTF-8 でない comm を持つプロセス自身も、自分の子として正しく数えられる
    (黙って取りこぼさない) ことも確かめる。"""
    child = _spawn_with_raw_comm(_BAD_COMM_HEX)
    try:
        _wait_for_comm(child.pid, _BAD_COMM_HEX)
        assert child.pid in descendants(os.getpid())
    finally:
        child.kill()
        child.wait()


def test_scan_survives_a_bystander_with_a_non_utf8_process_name(basetemp):
    """finding P2-1 の再現形そのもの: 無関係な UTF-8 でない comm のプロセスが 1 つ居るだけで
    `scan()` 全体が落ち、本物の孤児 (印付き) が見つからなくなる、を直したことの確認。"""
    before = leaked_descendants.snapshot()
    bystander = _spawn_with_raw_comm(_BAD_COMM_HEX)
    try:
        _wait_for_comm(bystander.pid, _BAD_COMM_HEX)
        pid = _spawn_orphan()
        try:
            assert _wait_observable(pid), "孤児の environ が読めるようにならない (前提が崩れた)"
            result = leaked_descendants.scan(before, _marker(), basetemp)
            assert pid in {s.pid for s in result.survivors}, \
                "UTF-8 でない comm のプロセスに巻き込まれて本物の孤児を見失った"
        finally:
            os.kill(pid, signal.SIGKILL)
        assert _wait_dead(pid)
    finally:
        bystander.kill()
        bystander.wait()


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
