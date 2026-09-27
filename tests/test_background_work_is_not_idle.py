#!/usr/bin/env python3
"""
tests/test_background_work_is_not_idle.py — 裏の shell / monitor は「止まっている」ではない (B1 / #27)

## 背景

長いテストを `run_in_background` / Monitor で待っている Worker は、ツール呼び出しが止まる。
2026-09-27 に自分の pane で実測した (裏の `sleep` が生きている間):

    herdr agent_status : `idle` (Monitor のときは `done`)   ← どちらも Rule 5 の通知対象
    画面末尾           : `1 shell` / `1 monitor`
    claude の直下      : `bash -c ... eval 'sleep 300'` が生える (後から生えた子)
    classify_process_tree(pane_pid) : 終始 `executing`、終わると `idle_process`

その結果、dispatcher の Rule 5 が `[Rule 5] idle-with-task` を約 2 分おきに Director に送り続けた
(mission 20260926-mechanize-guards-a では Director が手でツールを 1 回使わせて回避した)。
watchdog はプロセス層 (t016) が既に hard idle を terminate にしない — ここではそれを
**実プロセス木で** 通しで確かめ、上限 (max) が裏の子があっても効くことを固定する。

## t065 (Codex review 2巡目, PR#238 P2): 判定根拠を時刻からプロセスの同定に移す

t016 (`grace_seconds`) → t049 (`min_start_epoch`) はどちらも「いつ生えたか」という
時刻を代理指標にしていたため、`plan.sh pull` の後で初めて起動する MCP サーバー
(遅延起動の Playwright ブラウザ等) を本物の job と区別できず、job が全部終わった
後も Rule 5 を永久に抑制し続ける偽陰性が残った。`lib_pane_process.classify_process_tree()`
はもう時刻を見ない — comm (実行ファイル名) でシェル (job) と既知インフラを区別する。
このファイルのテストもそれに合わせてある: `PROCESS_WORK_START_GRACE` は無くなった。

## 方法

* プロセス木は本物 (`sh` の親子)。MCP / ブラウザ相当は `_fake_binary()` で
  `/bin/sh` を `node` / `chrome` 等の名前の symlink 越しに起動し、comm (実行体名)
  だけを模擬する (実バイナリの中身は問わない — 分類は comm しか見ない)。
* dispatcher は **本物の** dispatcher.sh の埋め込み python を `exec()` して `check_rule5()` を呼ぶ
  (`tests/test_dispatcher_notify_once.py` の Harness)。差し替えるのは mux (状態と pane pid) だけ。
* watchdog は本物の `WorkerMonitor.check_detail()`。差し替えるのは mux の pane pid 解決だけで、
  プロセス層は実物を通す (`_process_signal` をスタブしない)。

fail の向き (memory: fail-direction-is-per-judgment) はテスト名に出す:
  * Rule 5 (通知を止める判定): 観測できない → **通知する**側
  * watchdog (殺す判定):        観測できない → **殺さない**側 (既存: test_watchdog_idle.py)

実行: python3 -m pytest tests/test_background_work_is_not_idle.py -v
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lib_mux  # noqa: E402
import lib_pane_process  # noqa: E402
import watchdog  # noqa: E402
from test_dispatcher_notify_once import FakeMux, Harness, SLUG  # noqa: E402
from test_watchdog_idle import (  # noqa: E402
    WINDOW, _FakeMux as _WatchdogFakeMux, _make_monitor, _write_activity,
)

AGENT = "sofia"
TARGET = f"{AGENT}-worker"


# ---------------------------------------------------------------------------
# 実プロセス木 (pane 相当)。1 テストファイルで 2 本だけ立てて使い回す
# ---------------------------------------------------------------------------

def _pgid_of(pid: int) -> Optional[int]:
    """/proc/<pid>/stat の field 5 (pgid)。読めなければ None (= 既に居ない)。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    rest = raw[close + 1:].split()
    if len(rest) < 3:
        return None
    try:
        return int(rest[2])
    except ValueError:
        return None


def _members_of_group(pgid: int) -> list:
    """この pgid に属する現存 pid の一覧 (テストの「孤児が残っていないか」の判定に使う)。"""
    members = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return members
    for entry in entries:
        if entry.name.isdigit() and _pgid_of(int(entry.name)) == pgid:
            members.append(int(entry.name))
    return members


def _kill_pane_tree(proc: subprocess.Popen) -> None:
    """t049 (Codex review, PR#238 P3): root だけでなく group ごと kill する。

    root の `sh` 自身しか kill しないと、非対話シェルの背後の子 (孫の `sh` /
    `sleep`) は同じ group のまま孤児になり、自分の `sleep 300` の寿命 (最大 5分)
    だけ生き残って標準出力の fd を掴み続ける (mutation script を繰り返すたびに
    残骸が積み上がり、EOF 待ちの呼び出し側を遅延させうる)。spawn 側が
    `start_new_session=True` (setsid) で自分専用の process group を持つ前提。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass  # 既に居ない (group ごと消えた) — teardown の目的は既に達成
    proc.wait()


def _fake_binary(directory, name: str, real: str = "/bin/sh") -> str:
    """`real` への symlink を `name` という名前で作り、そのパスを返す。

    /proc/<pid>/stat の comm (実行体名) は execve に渡したパス自身の
    ベース名になる (実行ファイルの実体の名前ではない — 本ファイル内で実測
    済み: `/bin/sleep` への symlink `chrome_test_marker` を実行すると
    comm は `chrome_test_mar` [15 文字打ち切り] になる)。これを使って
    node / chrome 等の永続インフラの comm を、実バイナリを入れずに模擬する。
    `real="/bin/sh"` を使えば、模擬した名前のままシェルとして機能する
    (comm は symlink 名のまま、中身は sh なので `-c` でさらに子を生やせる) —
    「MCP サーバー (node) が後からブラウザ (chrome) を起動する」形を作れる。
    """
    p = Path(directory) / name
    if not p.exists():
        p.symlink_to(real)
    return str(p)


@pytest.fixture(scope="module")
def panes(tmp_path_factory):
    """{"busy": pid, "quiet": pid} — どちらも本番のペインと同じ形の木。

    busy   root sh ─ node(t=0, MCP 相当) ─ sh(t=0, 裏の job)
    quiet  root sh ─ node(t=0, MCP 相当)                       … 裏の job 無し

    MCP 相当は `_fake_binary()` で comm を `node` にする (t065: 分類は comm を
    見るので、真の MCP サーバーでなくても「同定される既知インフラ」であれば
    形が揃う)。**quiet にシェルを一切含めないこと** — シェル (comm) が見つかった
    時点で job と判定するので、中身が空でも `sh -c "..."` のような wrapper を
    quiet 側に置くと、それだけで誤って job 側に倒れてしまう。
    各木は `start_new_session=True` で自分専用の process group を持つ
    (setsid)。teardown は `_kill_pane_tree()` で group ごと落とす (P3)。
    """
    fake_bin_dir = tmp_path_factory.mktemp("fake-bin-panes")
    node_bin = _fake_binary(fake_bin_dir, "node", real="/bin/sleep")
    busy = subprocess.Popen(
        ["sh", "-c", f'{node_bin} 300 & sh -c "sleep 300 & wait" & wait'],
        start_new_session=True)
    quiet = subprocess.Popen(
        ["sh", "-c", f'{node_bin} 300 & wait'],
        start_new_session=True)
    time.sleep(3.5)  # 遅れて生える子が出そろうまで
    try:
        yield {"busy": busy.pid, "quiet": quiet.pid}
    finally:
        for p in (busy, quiet):
            _kill_pane_tree(p)


def test_the_fixture_trees_classify_as_intended(panes):
    """前提: 木の形が意図どおり。ここが崩れると以降の緑・赤は何の証明にもならない。"""
    assert lib_pane_process.classify_process_tree(panes["busy"]) == "executing"
    assert lib_pane_process.classify_process_tree(panes["quiet"]) == "idle_process"


def _direct_children(pid):
    """`_proc_stat` を直接使って ppid==pid の pid 一覧を返す (テスト専用ヘルパー)。"""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = lib_pane_process._proc_stat(int(entry.name))
        except OSError:
            continue
        if stat is not None and stat[0] == pid:
            out.append(int(entry.name))
    return out


def test_an_unreadable_intermediate_pid_falls_to_unknown_not_idle(monkeypatch):
    """族A監査 (t049): `_proc_stat` が「消滅」以外の理由 (EACCES 等) である pid を
    読めないと、それを None に潰して静かにスキップする実装では、その pid を親に
    持つ子孫 (それ自体は読める) が誰からも「値」として指されなくなり、
    children から永久に辿り着けなくなる。生きている裏 job のサブツリーが
    まるごと `idle_process` に見えてしまう欠陥を、実プロセス木 + `_proc_stat`
    への読み取り失敗の注入で固定する (`unknown` を返し、静かに `idle_process`
    へ倒さないこと)。
    """
    root = subprocess.Popen(
        ["sh", "-c", 'sleep 300 & sh -c "sleep 300 & wait" & wait'],
        start_new_session=True)
    try:
        time.sleep(0.5)  # 孫の裏 job まで出そろうまで

        # root の直下の子のうち、自分自身がさらに子を持つ方が wrapper (裏 job の親)。
        direct = _direct_children(root.pid)
        wrapper_pid = next(pid for pid in direct if _direct_children(pid))

        real_proc_stat = lib_pane_process._proc_stat

        def flaky_proc_stat(pid):
            if pid == wrapper_pid:
                raise PermissionError(13, "Permission denied (test injection)")
            return real_proc_stat(pid)

        monkeypatch.setattr(lib_pane_process, "_proc_stat", flaky_proc_stat)

        assert lib_pane_process.classify_process_tree(root.pid) == "unknown"
    finally:
        _kill_pane_tree(root)


# ---------------------------------------------------------------------------
# dispatcher Rule 5
# ---------------------------------------------------------------------------

class PaneMux(FakeMux):
    """herdr が返す agent_status と pane pid を、テストが決める。"""

    pane_state = "idle"
    pane_pid = None
    pid_raises = None

    def state(self, *a, **kw):
        return PaneMux.pane_state

    def pid(self, name):
        if PaneMux.pid_raises is not None:
            raise PaneMux.pid_raises
        return PaneMux.pane_pid


class Rule5:
    """`check_rule5()` を、grace 経過済みの idle-with-task から 1 回呼ぶ口。"""

    def __init__(self, h: Harness, monkeypatch):
        monkeypatch.setattr(lib_mux, "Mux", PaneMux)
        PaneMux.pane_state, PaneMux.pane_pid, PaneMux.pid_raises = "idle", None, None
        self.h = h
        h.cycle()   # 本物の埋め込み python の名前空間を作る (Worker 窓は無いので何も送らない)
        self.ns = h.ns
        self.assignment = self.ns["ASSIGNMENTS_DIR"] / AGENT
        self.assignment.parent.mkdir(parents=True, exist_ok=True)
        self.assignment.write_text(f"{SLUG}:t001\n")

    def age_state(self, state="idle-with-task", seconds=600):
        """Rule 5 の grace (60 秒) を経過済みにする。"""
        self.ns["_save_state_entry"](AGENT, state, time.time() - seconds)

    def entry(self):
        return json.loads(
            (self.h.registry / "mux" / f"{AGENT}.state.json").read_text())

    def run(self):
        FakeMux.sent = []
        self.ns["check_rule5"](AGENT, TARGET, self.assignment, {})
        return [m["message"] for m in FakeMux.sent]


@pytest.fixture
def r5(tmp_path, monkeypatch):
    return Rule5(Harness(tmp_path / "repo", monkeypatch), monkeypatch)


@pytest.mark.parametrize("herdr_state", ["idle", "done"])
def test_a_live_background_job_is_not_idle_with_task(r5, panes, herdr_state):
    """裏の shell (`idle`) / Monitor (`done`) が生きている間は Rule 5 は黙る。"""
    PaneMux.pane_state, PaneMux.pane_pid = herdr_state, panes["busy"]
    r5.age_state()
    assert r5.run() == []


def test_the_same_pane_without_a_background_job_is_still_notified(r5, panes):
    """対照: 裏の job が無ければ従来どおり通知する (常に黙る実装を弾く)。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["quiet"]
    r5.age_state()
    msgs = r5.run()
    assert len(msgs) == 1 and "idle-with-task" in msgs[0]


def test_a_background_job_restarts_the_grace_and_clears_the_dedup_key(r5, panes):
    """裏の job 中は 'working' 扱い: grace を測り直し、通知済みの印を外す。"""
    r5.ns["record_notify"](f"idle_with_task_{AGENT}")
    assert f"idle_with_task_{AGENT}" in r5.ns["load_notify_cache"]()
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.age_state()
    assert r5.run() == []
    assert r5.entry()["state"] == "working"
    assert time.time() - r5.entry()["since"] < 30          # 測り直された
    assert f"idle_with_task_{AGENT}" not in r5.ns["load_notify_cache"]()


def test_grace_counts_from_the_end_of_the_job(r5, panes):
    """job が終わったあとの grace は、終わった時点から数える (終わった瞬間に通知しない)。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.age_state()
    assert r5.run() == []                                   # job 中
    PaneMux.pane_pid = panes["quiet"]                       # job が終わった
    assert r5.run() == []                                   # 状態が変わった最初の cycle は grace の起点
    assert r5.entry()["state"] == "idle-with-task"
    r5.age_state()                                          # そのまま grace が過ぎた
    assert len(r5.run()) == 1                               # 本当に止まっているなら通知する


def test_blocked_is_still_notified_with_a_background_job(r5, panes):
    """`blocked` (承認・質問待ち) は裏の job があっても通知する — 待っているのは人間。"""
    PaneMux.pane_state, PaneMux.pane_pid = "blocked", panes["busy"]
    r5.age_state("blocked")
    msgs = r5.run()
    assert len(msgs) == 1 and "blocked" in msgs[0]


def test_no_assignment_means_the_process_tree_is_not_even_read(r5, panes):
    """assignment が無ければ条件 B ではない — /proc の走査は idle-with-task のときだけ。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", panes["busy"]
    r5.assignment.unlink()
    calls = []
    real = r5.ns["classify_process_tree"]
    r5.ns["classify_process_tree"] = lambda *a, **kw: calls.append(a) or real(*a, **kw)
    r5.run()
    assert calls == []


# -- fail の向き: 観測できない → 通知する側 ------------------------------------

def test_unobservable_pane_pid_falls_to_the_notifying_side(r5):
    """pane pid が引けない (mux 不調) → 通知する。誤って黙るより 1 通余計な方が安い。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", None
    r5.age_state()
    assert len(r5.run()) == 1


def test_a_classifier_error_falls_to_the_notifying_side_and_is_logged(r5):
    """分類が例外 → 通知する。黙って握りつぶさず WARNING を残す。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", 12345
    PaneMux.pid_raises = OSError("herdr unreachable")
    r5.age_state()
    assert len(r5.run()) == 1
    assert "cannot classify pane process tree" in r5.h.log_text()


def test_a_vanished_pane_process_falls_to_the_notifying_side(r5):
    """pane pid はあるが /proc に無い (`no_process`) → 裏の job は見えない → 通知する。"""
    PaneMux.pane_state, PaneMux.pane_pid = "idle", 2 ** 22
    r5.age_state()
    assert len(r5.run()) == 1


# ---------------------------------------------------------------------------
# P2 2巡目 (Codex review, PR#238): 判定根拠を時刻からプロセスの同定に移す
# ---------------------------------------------------------------------------

def test_a_job_that_starts_at_the_same_instant_as_mcp_is_still_caught(tmp_path):
    """偽陽性側の赤の実証: 裏 job が MCP と**同時に** (しきい値の前後を問わず)
    始まっても、シェルとして同定されればその場で job と分かる。t049 までの
    時刻ベースの判定は「着手直後 (しきい値の枠内)」に始まった job を区別
    できなかったが、同定ベースなら「いつ生えたか」自体を見ないのでこの区別が
    そもそも要らないことを固定する。
    """
    node_bin = _fake_binary(tmp_path, "node", real="/bin/sleep")
    root = subprocess.Popen(
        ["sh", "-c", f'{node_bin} 300 & sh -c "sleep 300 & wait" & wait'],
        start_new_session=True)
    try:
        time.sleep(0.05)  # MCP (node) と job (sh) がほぼ同時に生えた直後
        assert lib_pane_process.classify_process_tree(root.pid) == "executing"
    finally:
        _kill_pane_tree(root)


def test_a_late_starting_mcp_browser_does_not_suppress_rule_5_forever(tmp_path):
    """偽陰性側の赤の実証 (今回の主眼): `plan.sh pull` の後で初めて起動する MCP
    サーバー / ブラウザ (遅延起動の Playwright ブラウザ等) は、時刻ベースの
    判定 (t049 の `min_start_epoch`) だと「assignment より後に生えた」という
    理由だけで無条件に executing 扱いになり、**job が全部終わった後も Rule 5
    を永久に抑制し続けた** (偽陰性 — Director が気付けない)。

    同定ベースでは、遅れて生えた MCP/ブラウザだけが残る木は job (シェル) を
    一切含まないので idle_process と判定され、Rule 5 が正しく機能することを
    固定する。木は「MCP (node) が起動し、しばらくしてブラウザ (chrome) だけを
    起動する。job (シェル) は無い」形 — job がとっくに終わった Worker を模す。

    遅延は shell の `sleep N;` では作らない — `sleep` は外部コマンドなので
    フォークされ、そのプロセス自身が (シェルでも既知インフラでもない)
    同定不能な子として一瞬混ざり込み、テストの意図と無関係に "executing" に
    倒れてしまう (このテスト自身が実測して踏んだ)。ここでは builtin の `read`
    で FIFO から 1 行読むまで node は止まり、書き込まれたら node が chrome を
    **新しい子プロセスとして** fork する (`exec` で同じ pid に入れ替えると
    chrome の /proc starttime が node の fork 時刻のままになり、「assignment
    より後に生えた」を再現できなくなる — この違いも実測して踏んだ)。
    """
    fifo_path = tmp_path / "signal"
    os.mkfifo(fifo_path)
    node_bin = _fake_binary(tmp_path, "node", real="/bin/sh")
    chrome_bin = _fake_binary(tmp_path, "chrome", real="/bin/sleep")
    root = subprocess.Popen(
        ["sh", "-c", f'{node_bin} -c "read x < {fifo_path}; {chrome_bin} 300 & wait" & wait'],
        start_new_session=True)
    try:
        time.sleep(0.3)  # node (MCP 相当) だけが立った状態。ブラウザはまだ
        assert lib_pane_process.classify_process_tree(root.pid) == "idle_process"

        with open(fifo_path, "w") as f:
            f.write("go\n")  # 遅延起動のブラウザを今始める合図
        time.sleep(0.3)  # chrome (遅延起動のブラウザ相当) が生えた後
        assert lib_pane_process.classify_process_tree(root.pid) == "idle_process"
    finally:
        _kill_pane_tree(root)


def test_a_late_starting_mcp_browser_lets_rule_5_notify_via_check_rule5(
    tmp_path, monkeypatch
):
    """上と同じ木を、本物の `check_rule5()` 経由で確かめる — 受入条件の
    「MCP の永続プロセスだけを持ち、job は全部終わった Worker に Rule 5 が
    発火すること」そのもの。**ブラウザは assignment file を書いた後で初めて
    起動する** (finding が明示的に要求する「plan.sh pull の後で初めて起動する
    Playwright ブラウザ」の経路そのもの) — 旧実装 (t049 の `min_start_epoch`:
    「assignment より後に生えた子孫は無条件 executing」) だと、このブラウザは
    job が無くても永久に executing と誤読され、Rule 5 が黙り続けた (偽陰性)。
    同定ベースでは「いつ生えたか」を見ないので、assignment の前後に関わらず
    ブラウザは infra のまま — 通知が正しく発火することを固定する。
    """
    fifo_path = tmp_path / "signal"
    os.mkfifo(fifo_path)
    node_bin = _fake_binary(tmp_path, "node", real="/bin/sh")
    chrome_bin = _fake_binary(tmp_path, "chrome", real="/bin/sleep")
    root = subprocess.Popen(
        ["sh", "-c", f'{node_bin} -c "read x < {fifo_path}; {chrome_bin} 300 & wait" & wait'],
        start_new_session=True)
    try:
        time.sleep(0.3)  # node だけが立った状態。ブラウザはまだ

        h = Harness(tmp_path / "repo", monkeypatch)
        monkeypatch.setattr(lib_mux, "Mux", PaneMux)
        PaneMux.pane_state, PaneMux.pane_pid, PaneMux.pid_raises = "idle", root.pid, None
        h.cycle()
        assignment = h.ns["ASSIGNMENTS_DIR"] / AGENT
        assignment.parent.mkdir(parents=True, exist_ok=True)
        assignment.write_text(f"{SLUG}:t001\n")  # ここが「plan.sh pull」の時刻
        h.ns["_save_state_entry"](AGENT, "idle-with-task", time.time() - 600)  # grace 経過済み

        # assignment を書いた**後**で初めてブラウザ (chrome) を起動する —
        # 遅延起動の Playwright ブラウザの経路そのもの
        with open(fifo_path, "w") as f:
            f.write("go\n")
        time.sleep(0.3)  # chrome が生えるまで

        FakeMux.sent = []
        h.ns["check_rule5"](AGENT, TARGET, assignment, {})
        msgs = [m["message"] for m in FakeMux.sent]
        assert len(msgs) == 1 and "idle-with-task" in msgs[0]
    finally:
        _kill_pane_tree(root)


def test_an_unidentified_persistent_process_is_treated_as_a_job_not_infra(tmp_path):
    """族C監査 (t065): 既知インフラの一覧に無い永続プロセスをどう倒すか。
    未知の物を infra 側に倒す (= 黙り続ける) と、新種の MCP サーバー / 未対応の
    ブラウザが現れるたびに同じ偽陰性が起こりうる。同定できないものは job 側
    (executing) に倒し、偽陽性 (1 回余計に通知する) の方を選ぶことを固定する。
    """
    unknown_bin = _fake_binary(tmp_path, "totally-unknown-binary", real="/bin/sleep")
    root = subprocess.Popen(
        ["sh", "-c", f'{unknown_bin} 300 & wait'],
        start_new_session=True)
    try:
        time.sleep(0.3)
        assert lib_pane_process.classify_process_tree(root.pid) == "executing"
    finally:
        _kill_pane_tree(root)


# ---------------------------------------------------------------------------
# P3 (Codex review, PR#238): pane tree の teardown は group ごと
# ---------------------------------------------------------------------------

def test_kill_pane_tree_leaves_no_orphans_behind():
    """root の `sh` だけを kill すると、孫の `sh` / `sleep` が同じ group のまま
    孤児になり、自分の `sleep 300` の寿命 (最大 5分) だけ生き残って標準出力の fd を
    掴み続ける (mutation script を繰り返すたびに残骸が積み上がる)。
    `_kill_pane_tree()` が group ごと落とし、孤児を残さないことを固定する。
    """
    proc = subprocess.Popen(
        ["sh", "-c", 'sleep 300 & sh -c "sleep 2; sleep 300 & wait" & wait'],
        start_new_session=True)
    time.sleep(3.5)  # 孫の裏 job まで出そろうまで
    pgid = os.getpgid(proc.pid)

    # 前提: 木の形が意図どおり (root + MCP 相当 + wrapper + 裏 job の 4 プロセス)
    assert len(_members_of_group(pgid)) == 4

    _kill_pane_tree(proc)
    time.sleep(0.3)  # SIGKILL の反映を待つ
    assert _members_of_group(pgid) == []


# ---------------------------------------------------------------------------
# watchdog — 実プロセス木を通しで (`_process_signal` をスタブしない)
# ---------------------------------------------------------------------------

def test_watchdog_does_not_terminate_a_worker_with_a_live_background_job(
    tmp_path, monkeypatch, panes
):
    """hard idle でも、裏の job が生きている間は terminate しない (warn)。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["busy"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)          # 300 * 2 を大きく超える
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("warn", "hard_idle_but_executing")
    assert detail.process_signal == "executing"


def test_watchdog_still_terminates_the_same_silence_without_a_job(
    tmp_path, monkeypatch, panes
):
    """対照: 同じ無音でも裏の job が無ければ terminate (常に見送る実装を弾く)。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["quiet"]))
    monitor = _make_monitor(tmp_path, idle=300)
    _write_activity(tmp_path, age_seconds=3000)
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("terminate", "hard_idle")


def test_the_absolute_max_still_applies_with_a_live_background_job(
    tmp_path, monkeypatch, panes
):
    """裏で止まったままの job を持つ Worker が永久に残らない — max は裏の job があっても効く。"""
    monkeypatch.setattr(watchdog, "_mux", _WatchdogFakeMux(WINDOW, panes["busy"]))
    monitor = _make_monitor(tmp_path, idle=300, max_threshold=60)
    _write_activity(tmp_path, age_seconds=3000)
    monitor.started_at = time.time() - 120
    detail = monitor.check_detail()
    assert (detail.verdict, detail.reason) == ("terminate", "max_exceeded")


def test_watchdog_and_dispatcher_share_one_classifier():
    """「裏で何かが走っているか」の定義は 1 つ (lib_pane_process) — コピーが生えたら赤。"""
    assert watchdog.classify_process_tree is lib_pane_process.classify_process_tree
    src = (SCRIPTS_DIR / "dispatcher.sh").read_text()
    assert "from lib_pane_process import classify_process_tree" in src
    for name in ("dispatcher.sh", "watchdog.py"):
        text = (SCRIPTS_DIR / name).read_text()
        assert "/proc/" not in text.replace("lib_pane_process", ""), (
            f"{name} が /proc を直接読んでいる — 分類は lib_pane_process に 1 つだけ")
