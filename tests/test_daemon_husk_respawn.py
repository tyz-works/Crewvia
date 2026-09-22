#!/usr/bin/env python3
"""
tests/test_daemon_husk_respawn.py

「プロセスだけが死んで pane が残る」= husk からの respawn (t035 / PR #209 QA FAIL-1)。

## なぜ別ファイルなのか

`test_daemon_mutual_watch.py` は判定表を **偽物の mux と偽物の /proc** で駆動する。
それは判定表の網羅には正しいが、t006 QA が見つけた欠陥は偽物では絶対に出ない形を
していた:

    start.sh も TmuxBackend.spawn も HerdrBackend.spawn も、pane に **シェル** を
    置いてそこへコマンドを流し込む。だからデーモンは pane シェルの子であり、
    デーモンが死んでもシェルは生き残り、pane と label は残る。

つまり「窓一覧に名前がある」は本番では**常に真**で、それを生存の証拠に使っていた
判定は必ず hold に落ちていた。偽の FakeMux では spawn 後に窓が消えたり残ったりを
こちらで決められてしまうので、**この欠陥はテストが自分で隠せる**。

よってこのファイルは実プロセス (と、あれば実 tmux) だけで書く:

  - `test_real_husk_pane_does_not_block_respawn` — 実 tmux 窓で実デーモンを起こし、
    **プロセスだけ** を SIGKILL して、窓が残ったままであることを確認したうえで
    相手の 1 cycle を回す。欠陥が戻れば hold のまま respawn 0 回で赤くなる。
  - `test_tmux_pane_liveness_uses_real_processes` — husk 判別そのものを、実際に
    走っているシェル・シェルの子・非シェルに対して確かめる。

実行方法:
  python3 -m pytest tests/test_daemon_husk_respawn.py -v
"""

import os
import pty
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_daemon_watch as dw  # noqa: E402
import lib_mux  # noqa: E402

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="この回帰は実 tmux 窓でしか再現しない (偽 tmux では husk を作れない)",
)

#: 本物のプロセスを相手にする以上、待ちは実時間。短い poll を重ねる。
_POLL = 0.1
_TIMEOUT = 20.0


def _wait_for(predicate, *, timeout=_TIMEOUT, what=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(_POLL)
    pytest.fail(f"timed out after {timeout}s waiting for {what or predicate!r}")


# ---------------------------------------------------------------------------
# Fixtures — an isolated checkout with a stub daemon, and an isolated session
# ---------------------------------------------------------------------------

#: `scan_daemon_pids()` の needle は `<repo_root>/scripts/<script>` の絶対パスなので、
#: stub もその名前で置く。`exec` しないのが肝心で、exec すると argv からスクリプト名が
#: 消えて走査に掛からなくなる = テストが欠陥ではなく自分の細工で赤くなる。
_STUB_DAEMON = """#!/usr/bin/env bash
# t035 test stub: 本物の dispatcher.sh の代わりに、走査に掛かる形で生き続けるだけ。
while true; do
  sleep 1
done
"""


@pytest.fixture
def stub_repo(tmp_path):
    root = tmp_path / "crewvia"
    (root / "scripts").mkdir(parents=True)
    (root / "registry" / "daemons").mkdir(parents=True)
    (root / ".git").mkdir()          # repo_identity_ok() が見るのはこれだけ
    script = root / "scripts" / "dispatcher.sh"
    script.write_text(_STUB_DAEMON, encoding="utf-8")
    script.chmod(0o755)
    return root


@pytest.fixture
def tmux_session(monkeypatch):
    """A tmux session of our own, torn down whatever the test does.

    本番は herdr なので tmux 側は元から無関係だが、同じマシンで editorial-room 等の
    tmux サーバーが動いている。セッション名を毎回ユニークにして、消すのも自分のぶん
    だけにする。
    """
    name = f"crewvia-husk-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("CREWVIA_MUX", "tmux")
    monkeypatch.setenv("CREWVIA_TMUX_SESSION", name)
    try:
        yield name
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name],
                       capture_output=True, timeout=10)


def _scan(repo):
    return dw.scan_daemon_pids(repo, dw.DAEMON_DISPATCHER)


# ---------------------------------------------------------------------------
# 1. 本題 — husk pane が残る普通のクラッシュ
# ---------------------------------------------------------------------------

@requires_tmux
def test_real_husk_pane_does_not_block_respawn(stub_repo, tmux_session):
    """プロセスだけが死に、pane と label が残る — 本番のクラッシュの形。

    この状態で相手 (watchdog) の 1 cycle を回したとき、respawn されなければ
    相互監視は生存性を持たない。t006 QA の FAIL-1 はこれだった。
    """
    mux = lib_mux.Mux()
    cmd = dw.spawn_command(dw.DAEMON_DISPATCHER, stub_repo)
    assert mux.spawn(dw.DAEMON_DISPATCHER, cmd, cwd=str(stub_repo)) is True

    first = _wait_for(lambda: _scan(stub_repo), what="the stub dispatcher to start")
    assert len(first) == 1, first

    reports = []
    watch = dw.DaemonWatch(
        registry_dir=stub_repo / "registry",
        repo_root=stub_repo,
        self_name=dw.DAEMON_WATCHDOG,
        mux=lib_mux.Mux(),
        config=dw.WatchConfig(),
        notify=lambda msg: (reports.append(msg), True)[1],
    )

    # -- 対照: 生きているうちは絶対に起こさない -----------------------------
    verdict = watch.watch_peer()
    assert verdict.action == dw.ACTION_HOLD, verdict.reason
    assert _scan(stub_repo) == first, "a live daemon was respawned on top of"

    # -- プロセスだけを殺す (pane シェルには触らない) ------------------------
    os.kill(first[0], 9)
    _wait_for(lambda: _scan(stub_repo) == [], what="the stub dispatcher to die")

    # husk であることの証拠: 窓は残っている。ここが偽物では作れない部分。
    assert dw.DAEMON_DISPATCHER in mux.list(), (
        "the pane vanished with the process — this run did not reproduce a husk, "
        "so it proves nothing about the defect"
    )

    # -- 相互監視の 1 cycle ---------------------------------------------------
    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_RESPAWNED, (
        f"husk pane blocked the respawn: {verdict.action} — {verdict.reason}"
    )
    revived = _wait_for(lambda: _scan(stub_repo), what="the dispatcher to come back")
    assert len(revived) == 1, f"double start: {revived}"
    assert revived != first
    assert reports and "dispatcher" in reports[0]


@requires_tmux
def test_a_live_daemon_in_the_pane_still_refuses_the_spawn(stub_repo, tmux_session):
    """husk を認めても、pane に生きたプロセスが居る間は spawn が断る。

    §7-1 の最悪ケース (二重起動) に対する最後の防壁が、窓名の有無ではなく
    **その窓の中身の生死** になったことを固定する。判定層を素通りさせて
    backend の spawn だけを直接叩く。
    """
    mux = lib_mux.Mux()
    cmd = dw.spawn_command(dw.DAEMON_DISPATCHER, stub_repo)
    assert mux.spawn(dw.DAEMON_DISPATCHER, cmd, cwd=str(stub_repo)) is True
    running = _wait_for(lambda: _scan(stub_repo), what="the stub dispatcher to start")

    assert mux.spawn(dw.DAEMON_DISPATCHER, cmd, cwd=str(stub_repo)) is False
    time.sleep(1.0)
    assert _scan(stub_repo) == running, "spawn started a second one on a live pane"


# ---------------------------------------------------------------------------
# 2. husk 判別そのもの — 実プロセスに対して
# ---------------------------------------------------------------------------

@pytest.fixture
def reap():
    procs = []
    yield procs.append
    for p in procs:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


@pytest.fixture
def pty_shell():
    """本物の対話シェルを pty 上に起こす (終了はこの fixture が面倒を見る)。

    `subprocess.Popen(..., stdin=<pty slave>)` では制御端末を取れない —
    端末を **開いた** セッションリーダーだけが制御端末を得るので、fork 後に
    setsid して自分で開く `pty.fork()` を使う。これをやらないと tpgid が
    -1 のままになり、「プロンプトに居る対話シェル」ではなく「端末を持たない
    シェル」を相手にテストしてしまう。
    """
    spawned = []

    def _spawn():
        pid, fd = pty.fork()
        if pid == 0:                      # pragma: no cover - child never returns
            os.execvp("bash", ["bash"])
        spawned.append(pid)
        _wait_for(lambda: Path(f"/proc/{pid}/stat").exists(), what="the pty shell")
        time.sleep(0.5)                   # プロンプトを出し切るまで
        return pid, fd

    yield _spawn
    for pid in spawned:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except OSError:
            pass


def test_tmux_pane_liveness_uses_real_processes(reap, monkeypatch, pty_shell):
    """`_pane_has_live_process()` を実プロセス数種で確かめる。

    tmux は herdr の `pane process-info` に当たる問い合わせを持たないので、
    pane シェルの pid から `/proc` を読む。読めなければ「使用中」に倒す。
    """
    backend = lib_mux.TmuxBackend()

    # (a) プロンプトで待っているだけの対話シェル = husk
    idle_pid, _idle_fd = pty_shell()
    monkeypatch.setattr(backend, "pid", lambda name: idle_pid)
    assert backend._pane_has_live_process("dispatcher") is False

    # (b) 子プロセスを走らせているシェル = 使用中。
    #     末尾に `:` を足すのは bash の暗黙 exec 最適化を避けるため —
    #     `bash -c "sleep 30"` だと bash 自身が sleep に化けてしまい、
    #     「シェルが子を走らせている」形にならない。
    busy = subprocess.Popen(["bash", "-c", "sleep 30; :"])
    reap(busy)
    _wait_for(lambda: _has_child(busy.pid), what="the shell to fork its child")
    monkeypatch.setattr(backend, "pid", lambda name: busy.pid)
    assert backend._pane_has_live_process("dispatcher") is True

    # (c) そもそもシェルでない = 使用中 (idle と読んで上書きしてはいけない)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    reap(other)
    monkeypatch.setattr(backend, "pid", lambda name: other.pid)
    assert backend._pane_has_live_process("dispatcher") is True

    # (d) pane の pid が取れない = 見られなかった → 使用中に倒す
    monkeypatch.setattr(backend, "pid", lambda name: None)
    assert backend._pane_has_live_process("dispatcher") is True


def _has_child(pid):
    return bool(lib_mux._live_children(pid))


# ---------------------------------------------------------------------------
# 3. 「子が無いシェル」 ≠ 「プロンプトに居るシェル」 (t036 / PR #209 P2)
# ---------------------------------------------------------------------------
#
# comm と子プロセスだけを見ると、次のものが全部「idle」に見える:
#
#   - スクリプトを走らせているシェル (子を fork しない builtin だけの処理)
#   - `bash -c '...'` で走っているシェル
#   - パイプから読んでいる非対話シェル
#
# どれも実際には仕事をしていて、そこへ launch コマンドを送り込めば、コマンドは
# 入力として食われるか、走っている処理と混ざる。spawn() はそれを成功として
# 返していた。判定は「シェルであること」に加えて **どう起動されたか** と
# **端末の前景に居るか** まで見る。

def test_a_shell_running_a_script_is_not_idle(reap, tmp_path):
    """`bash script.sh` は子を持たなくても仕事中。argv がそれを示している。"""
    script = tmp_path / "busy.sh"
    script.write_text("while :; do :; done\n", encoding="utf-8")
    busy = subprocess.Popen(["bash", str(script)])
    reap(busy)
    time.sleep(0.3)
    assert lib_mux._pane_shell_is_idle(busy.pid) is False


def test_a_shell_running_a_builtin_loop_is_not_idle(reap):
    """`bash -c` も同じ。子は一つも生まれない。"""
    busy = subprocess.Popen(["bash", "-c", "while :; do :; done"])
    reap(busy)
    time.sleep(0.3)
    assert lib_mux._pane_shell_is_idle(busy.pid) is False


def test_a_non_interactive_shell_reading_a_pipe_is_not_idle(reap):
    """端末を持たないシェルは pane のプロンプトではない。

    `bash` を引数無しで起動しても、stdin がパイプなら中身を読んで実行して
    いる最中でありうる。argv の長さだけでは見分けが付かない。
    """
    busy = subprocess.Popen(["bash"], stdin=subprocess.PIPE)
    reap(busy)
    time.sleep(0.3)
    assert lib_mux._pane_shell_is_idle(busy.pid) is False


def test_a_real_interactive_shell_at_its_prompt_is_idle(pty_shell):
    """逆側の保証: 本物の husk は idle のままでないと respawn できなくなる。

    t035 が足した生存性 (クラッシュで残ったシェルからも起こし直す) を、
    厳しくした判定が潰していないことの確認。
    """
    pid, _fd = pty_shell()
    assert lib_mux._pane_shell_is_idle(pid) is True


def test_a_shell_with_a_background_job_is_not_idle(pty_shell):
    """前景に戻っていても、裏で走っている仕事があれば使用中。"""
    pid, fd = pty_shell()
    os.write(fd, b"sleep 30 &\n")
    _wait_for(lambda: _has_child(pid), what="the background job to start")
    assert lib_mux._pane_shell_is_idle(pid) is False


@requires_tmux
def test_spawn_does_not_claim_success_when_the_pane_swallows_the_command(
        stub_repo, tmux_session):
    """入力待ちのペインに送り込んだコマンドは、起動ではなく **文字列** になる。

    対話シェルが `read` で止まっている状態は、プロンプトに居るのと /proc 上で
    区別が付かない (state も tpgid も argv も同じ)。区別が付かない以上、
    spawn は「送った」ではなく「本当に何か走り出した」で答えなければならない。
    さもないと相互監視は respawn を 1 回成功として記録し、grace と flap
    カウンタを消費したうえで、実際には何も起きていない。
    """
    session = tmux_session
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n",
                    dw.DAEMON_DISPATCHER], capture_output=True, timeout=10)
    # ペインのシェルを入力待ちにする (人が read を打った状態と同じ)。
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{dw.DAEMON_DISPATCHER}",
                    "read -r _swallowed", "Enter"], capture_output=True, timeout=10)
    time.sleep(1.0)

    mux = lib_mux.Mux()
    started = mux.spawn(dw.DAEMON_DISPATCHER,
                        dw.spawn_command(dw.DAEMON_DISPATCHER, stub_repo),
                        cwd=str(stub_repo))

    assert _scan(stub_repo) == [], "the stub daemon started after all — bad fixture"
    assert started is False, \
        "spawn reported success although the pane swallowed the command as input"
