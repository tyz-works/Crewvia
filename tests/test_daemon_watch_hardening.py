#!/usr/bin/env python3
"""
tests/test_daemon_watch_hardening.py

PR #209 の Codex レビュー (1 巡目) が出した 5 件のうち、判定表の側で閉じる 4 件
(t036)。残る 1 件 — 「子プロセスの無いシェルを idle と同一視しない」— は実際に
走っているシェルでしか確かめられないので `tests/test_daemon_husk_respawn.py` に
ある。

## 4 件が共通して言っていること

**決定は、書き込む瞬間が直列化されていて初めて決定になる。** そして
**「見られなかった」は「無かった」ではない。**

  1. `restart()` と watch の判定が直列化されていない (P1)
     マーカーを読む → (maintenance がマーカーを書いて kill) → そのまま spawn、
     で手動 restart と並行して起動する。「kill の前にマーカーを書く」だけでは
     閉じない。マーカーを読んでから spawn するまでを一つのロックで囲む。
     → `test_a_restart_in_progress_stops_the_watcher_from_spawning` ほか

  2. pause マーカーの永続化に失敗しても restart が進む (P1)
     `write_json_atomic()` の戻り値を捨てていたので、ディスク full や権限
     エラーでも token を返し、保護が無いまま kill と spawn をしていた。
     → `test_restart_does_not_kill_when_the_pause_marker_cannot_be_persisted` ほか

  3. 自動 respawn でデーモンの設定が失われる (P1)
     `spawn_command()` が mux 関連 3 変数しか運ばないので、`CREWVIA_QUEUE` や
     `CREWVIA_KILL_AUTHORITY` を上書きして起動されたデーモンが respawn される
     と、別の queue を見に行く / kill 権限が食い違う、が起きる。
     → `test_spawn_command_carries_the_operational_overrides` ほか

  4. 読めなかった /proc エントリを「不在」として扱っている (P2)
     `scan_daemon_pids()` は cmdline の OSError をすべて「終了した」として
     skip していた。権限エラーは不在の証明にならない。
     → `test_scan_holds_when_a_proc_entry_cannot_be_read` ほか

実行方法:
  python3 -m pytest tests/test_daemon_watch_hardening.py -v
"""

import fcntl
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import lib_daemon_watch as dw  # noqa: E402
import lib_mux  # noqa: E402

# 判定表を駆動する偽物一式は既存の回帰テストと同じものを使う。別に持つと、
# 「どちらのフェイクで通したのか」を追う手間がそのまま欠陥の隠れ場所になる。
from test_daemon_mutual_watch import (  # noqa: E402,F401
    Clock,
    FakeMux,
    RecordingMux,
    make_watch,
    repo,
    write_peer_heartbeat,
)

#: watch は dispatcher (self) が watchdog (peer) を見る向きで固定して書く。
PEER = dw.DAEMON_WATCHDOG


def _dead_peer_watch(repo, **kw):
    """「相手は確実に死んでいる」状態の watch。ロック以外は何も止めない。"""
    clock = Clock()
    write_peer_heartbeat(repo, PEER, updated_at=clock.t - 10_000)
    watch, mux, clock = make_watch(repo, self_name=dw.DAEMON_DISPATCHER,
                                   clock=clock, **kw)
    return watch, mux, clock


def _foreign_lock_attempt(path) -> bool:
    """別プロセスのふりをして flock を試す。True = 取れた (= 誰も持っていない)。

    flock は open file description ごとなので、同じプロセスから開き直した fd
    でも既存のロックと衝突する。つまりこの関数が False を返すことは、外部の
    restart が待たされるのと同じ状態が本当に成立している証拠になる。
    """
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


# ---------------------------------------------------------------------------
# 1. maintenance と判定全体の直列化 (P1)
# ---------------------------------------------------------------------------

def test_a_restart_in_progress_stops_the_watcher_from_spawning(repo):
    """手動 restart が進行中なら、watch は spawn せず保留する。

    欠陥のあった順序:
        watcher: マーカーを読む → 無い
        operator: マーカーを書く → peer を kill
        watcher: そのまま spawn      ← 手動 spawn と並んで 2 つ起動する

    マーカーの読みと spawn が同じロックの中にあれば、watcher は
    「maintenance 中」を必ず見るか、さもなければロックが取れずに保留する。
    """
    watch, mux, _ = _dead_peer_watch(repo)
    lock = dw.lock_path(repo / "registry", PEER)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)          # ← maintenance が握っている状態
    try:
        verdict = watch.watch_peer()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert verdict.action == dw.ACTION_HOLD, verdict
    assert mux.spawned == [], \
        "watcher spawned while a maintenance transaction held the daemon lock"


def test_the_decision_and_the_spawn_happen_under_one_lock(repo):
    """spawn の瞬間にロックが握られていること。

    「マーカーを読んだ時点」と「spawn する時点」で別々にロックを取り直すと、
    その隙間に maintenance が丸ごと入れてしまう。spawn の中から外部ロックが
    取れないことが、隙間が無いことの証拠になる。
    """
    held = []

    class ProbingMux(FakeMux):
        def spawn(self, name, cmd, cwd=None, env=None):
            held.append(_foreign_lock_attempt(dw.lock_path(repo / "registry", name)))
            return super().spawn(name, cmd, cwd=cwd, env=env)

    watch, mux, _ = _dead_peer_watch(repo, mux=ProbingMux(windows=["Sora-director"]))
    verdict = watch.watch_peer()

    assert verdict.action == dw.ACTION_RESPAWNED, verdict
    assert held == [False], \
        "the daemon lock was not held at the moment of the spawn"


def test_a_pause_cannot_slip_in_while_a_decision_is_in_flight(repo):
    """maintenance 側も同じロックを通る。

    ロックが watch 側にしか無ければ、`pause()` は判定の途中でも成立してしまい、
    「マーカーを書いたのに相手はもう spawn を決めていた」が起こる。pause が
    待たされることをもって、両者が同じ直列化に乗っていることを確かめる。
    """
    reached_spawn = threading.Event()
    let_spawn_finish = threading.Event()

    class SlowMux(FakeMux):
        def spawn(self, name, cmd, cwd=None, env=None):
            reached_spawn.set()
            let_spawn_finish.wait(10)
            return super().spawn(name, cmd, cwd=cwd, env=env)

    watch, mux, _ = _dead_peer_watch(repo, mux=SlowMux(windows=["Sora-director"]))
    watcher = threading.Thread(target=watch.watch_peer, daemon=True)
    watcher.start()
    assert reached_spawn.wait(10), "the watcher never reached its spawn"

    outcome = []
    pauser = threading.Thread(
        target=lambda: outcome.append(dw.pause(repo / "registry", PEER,
                                               reason="maintenance")),
        daemon=True)
    pauser.start()
    pauser.join(1.0)
    still_waiting = pauser.is_alive()

    let_spawn_finish.set()
    watcher.join(10)
    pauser.join(10)

    assert still_waiting, \
        "pause() completed while the watcher was mid-spawn — the two are not serialized"
    assert outcome and outcome[0], "pause() failed once the lock was free"


def test_restart_holds_the_lock_across_kill_and_spawn(repo):
    """restart の kill → spawn は、外から割り込めない一区間であること。"""
    seen = []
    mux = RecordingMux(windows=[dw.DAEMON_DISPATCHER])
    lock = dw.lock_path(repo / "registry", dw.DAEMON_DISPATCHER)
    inner_kill, inner_spawn = mux.kill, mux.spawn
    mux.kill = lambda n: (seen.append(("kill", _foreign_lock_attempt(lock))),
                          inner_kill(n))[1]
    mux.spawn = lambda n, c, cwd=None, env=None: (
        seen.append(("spawn", _foreign_lock_attempt(lock))),
        inner_spawn(n, c, cwd=cwd, env=env))[1]

    assert dw.restart(dw.DAEMON_DISPATCHER, repo_root=repo, mux=mux,
                      log=lambda m: None) is True
    assert seen == [("kill", False), ("spawn", False)], seen


def test_the_lock_is_released_when_the_work_is_done(repo):
    """ロックは区間を出たら必ず手放す。持ったままだと相互監視が黙って止まる。"""
    watch, mux, _ = _dead_peer_watch(repo)
    watch.watch_peer()
    assert _foreign_lock_attempt(dw.lock_path(repo / "registry", PEER)) is True

    dw.restart(dw.DAEMON_DISPATCHER, repo_root=repo,
               mux=RecordingMux(windows=[dw.DAEMON_DISPATCHER]), log=lambda m: None)
    assert _foreign_lock_attempt(
        dw.lock_path(repo / "registry", dw.DAEMON_DISPATCHER)) is True


def test_each_daemon_has_its_own_lock(repo):
    """dispatcher の restart が watchdog の respawn を止めてはいけない。"""
    watch, mux, _ = _dead_peer_watch(repo)          # peer = watchdog
    other = dw.lock_path(repo / "registry", dw.DAEMON_DISPATCHER)
    other.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(other), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        verdict = watch.watch_peer()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert verdict.action == dw.ACTION_RESPAWNED, verdict


def test_the_launcher_also_starts_daemons_under_the_lock(repo):
    """3 人目の起動主体 — `./crewvia` — も同じ直列化に乗せる。

    watch と restart だけを囲っても、起動経路が 3 つある限り閉じない:
    相手が respawn を決めた直後に人が `./crewvia` を叩けば、やはり 2 つになる。
    """
    mux = FakeMux()
    lock = dw.lock_path(repo / "registry", dw.DAEMON_DISPATCHER)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        ok = dw.spawn_daemon(dw.DAEMON_DISPATCHER, repo_root=repo, mux=mux,
                             timeout=0.2, log=lambda m: None)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert ok is False
    assert mux.spawned == [], "the launcher spawned during someone else's transaction"


def test_start_sh_launches_the_daemons_through_the_locked_path():
    """start.sh は自前で mux_spawn せず、ロックを取る CLI を通ること。"""
    start_sh = (REPO_ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
    for name in (dw.DAEMON_DISPATCHER, dw.DAEMON_WATCHDOG):
        assert f"lib_daemon_watch.py\" spawn {name}" in start_sh, \
            f"start.sh does not launch {name} through lib_daemon_watch spawn"
        assert f'mux_spawn "{name}"' not in start_sh, \
            f"start.sh still spawns {name} outside the daemon lock"


def test_spawn_daemon_uses_the_shared_launch_command(repo):
    """起動経路が増えてもコマンドは 1 つ。"""
    mux = FakeMux()
    assert dw.spawn_daemon(dw.DAEMON_WATCHDOG, repo_root=repo, mux=mux,
                           log=lambda m: None) is True
    assert mux.spawned[0]["cmd"] == dw.spawn_command(dw.DAEMON_WATCHDOG, repo)


# ---------------------------------------------------------------------------
# 2. マーカーが残らなければ破壊的ステップに進まない (P1)
# ---------------------------------------------------------------------------

@pytest.fixture
def unwritable_repo(tmp_path):
    """`registry/daemons` の位置に **ファイル** が居る checkout。

    ディスク full や権限エラーを mock 無しで再現するための仕掛け。ここへの
    mkdir も書き込みも OSError になるので、`write_json_atomic()` は False を
    返す — 本番で起きる失敗と同じ経路をたどる。
    """
    root = tmp_path / "crewvia"
    (root / "registry").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "registry" / "daemons").write_text("not a directory", encoding="utf-8")
    return root


def test_pause_reports_failure_when_the_marker_cannot_be_persisted(unwritable_repo):
    """書けなかったのに token を返すのは「保護がある」という嘘になる。"""
    assert dw.pause(unwritable_repo / "registry", dw.DAEMON_DISPATCHER,
                    reason="maintenance") is None


def test_restart_does_not_kill_when_the_pause_marker_cannot_be_persisted(unwritable_repo):
    """約束した保護が無いなら、kill も spawn もしない。

    マーカーが残らないまま kill すると、相手デーモンから見えるのはただの
    「死んだ dispatcher」なので、手動 spawn と並んで respawn される。
    """
    mux = RecordingMux(windows=[dw.DAEMON_DISPATCHER])
    ok = dw.restart(dw.DAEMON_DISPATCHER, repo_root=unwritable_repo, mux=mux,
                    log=lambda m: None)
    assert ok is False
    assert mux.calls == [], \
        "restart killed the daemon although its pause marker was never persisted"


def test_pause_cli_exits_nonzero_when_the_marker_cannot_be_persisted(unwritable_repo):
    """CLI も黙って 0 を返さない — 人が見る唯一の合図なので。

    非ゼロなだけでは足りない: 例外が素通りしても非ゼロになる。断ったことが
    読める 1 行で出ていて、traceback ではないことまでを契約にする。
    """
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "lib_daemon_watch.py"), "pause",
         dw.DAEMON_DISPATCHER, "--repo-root", str(unwritable_repo)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode != 0, out
    assert out.stdout.strip() == "", \
        "a token was printed for a marker that was never written"
    assert "Traceback" not in out.stderr, out.stderr
    assert dw.DAEMON_DISPATCHER in out.stderr, out.stderr


def test_restart_cli_exits_nonzero_when_it_refuses(unwritable_repo):
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "lib_daemon_watch.py"), "restart",
         dw.DAEMON_DISPATCHER, "--repo-root", str(unwritable_repo)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode != 0, out
    assert "Traceback" not in out.stderr, out.stderr


def test_a_persisted_marker_is_readable_before_the_kill(repo):
    """成功経路: 書けたなら、kill の時点で確かに読める状態になっている。"""
    token = dw.pause(repo / "registry", PEER, reason="maintenance")
    assert token
    marker = dw.read_pause(repo / "registry", PEER)
    assert marker is not None and marker.get("token") == token


# ---------------------------------------------------------------------------
# 3. respawn でデーモンの設定を落とさない (P1)
# ---------------------------------------------------------------------------

#: 運用上の上書き = 「このデーモンがどこで何をするか」を決める非機密の変数。
_OPERATIONAL = {
    "CREWVIA_QUEUE": "/srv/other/queue",
    "CREWVIA_KILL_AUTHORITY": "dispatcher",
    "CREWVIA_TASKVIA": "disabled",
    "CREWVIA_NOTIFY_CACHE": "/tmp/qa-notify-cache.json",
    "CREWVIA_SPAWN_GRACE": "30",
    "CREWVIA_STATE_GRACE": "7",
    "CREWVIA_BENCH_MODE": "1",
}


def test_spawn_command_carries_the_operational_overrides(repo):
    """respawn されたデーモンは、起こされる前と同じ設定で走らないといけない。

    とくに致命的なのは 2 つ:
      - `CREWVIA_QUEUE`      別の queue を割り当て始める
      - `CREWVIA_KILL_AUTHORITY`  dispatcher と watchdog で kill 権限が食い違う。
        dispatcher 自身のコメントが「片方だけ戻すのは危険」と明記している状態。
    """
    cmd = dw.spawn_command(dw.DAEMON_DISPATCHER, repo, env=dict(_OPERATIONAL))
    for var, value in _OPERATIONAL.items():
        assert f"export {var}='{value}';" in cmd, f"{var} was dropped: {cmd}"


def test_spawn_command_carries_the_mutual_watch_settings(repo):
    """相互監視自身の設定も運ぶ。

    しきい値を緩めて起動した checkout (QA ハーネス等) で respawn が起きると、
    起こし直された側だけが既定のしきい値に戻り、相手を stale と読んで
    起こし返す — 相互監視が自分で flap を作る。
    """
    env = {
        "CREWVIA_DAEMON_MUTUAL_WATCH": "0",
        "CREWVIA_DAEMON_DISPATCHER_STALE_SECONDS": "600",
        "CREWVIA_DAEMON_FLAP_THRESHOLD": "9",
    }
    cmd = dw.spawn_command(dw.DAEMON_WATCHDOG, repo, env=env)
    for var, value in env.items():
        assert f"export {var}='{value}';" in cmd, f"{var} was dropped: {cmd}"


def test_spawn_command_does_not_carry_secrets(repo):
    """機密は **運ばない**。コマンド文字列は ps にもペインの履歴にも残る。

    「設定を失わない」を素朴に直すと os.environ を丸ごと運ぶことになり、
    (a) トークンが ps に出る (b) herdr server の古い env を引きずる、という
    既知の罠を 2 つ同時に踏む。運ぶものは allowlist で決める。
    """
    env = dict(_OPERATIONAL)
    env.update({
        "TASKVIA_TOKEN": "tv-secret-token",
        "NTFY_PASS": "hunter2",
        "NTFY_USER": "crewvia",
        "AGENT_NAME": "Ren",
        "TASK_ID": "t036",
        "HERDR_ENV": "1",
        "TMUX": "/tmp/tmux-1000/default,1,0",
    })
    cmd = dw.spawn_command(dw.DAEMON_DISPATCHER, repo, env=env)
    for leaked in ("tv-secret-token", "hunter2", "TASKVIA_TOKEN", "NTFY_PASS",
                   "NTFY_USER", "AGENT_NAME", "TASK_ID", "HERDR_ENV", "TMUX="):
        assert leaked not in cmd, f"{leaked} leaked into the launch command: {cmd}"


def test_the_carried_overrides_survive_a_shell_round_trip(repo):
    """export 文が本当にその値になることを bash に確かめさせる。

    文字列として含まれていることと、シェルが同じ値で起動することは別の主張。
    """
    env = {"CREWVIA_QUEUE": "/srv/q with space", "CREWVIA_KILL_AUTHORITY": "dispatcher"}
    cmd = dw.spawn_command(dw.DAEMON_WATCHDOG, repo, env=env)
    exports = cmd[:cmd.rindex("; ") + 2]
    out = subprocess.run(
        ["bash", "-c", exports + 'printf "%s|%s" "$CREWVIA_QUEUE" "$CREWVIA_KILL_AUTHORITY"'],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "/srv/q with space|dispatcher", out


def test_an_unset_override_is_not_exported_empty(repo):
    """未設定を空文字で運ぶと、既定値が効くはずの場所で空文字が勝ってしまう。"""
    cmd = dw.spawn_command(dw.DAEMON_DISPATCHER, repo, env={"CREWVIA_QUEUE": ""})
    assert "CREWVIA_QUEUE" not in cmd


def test_start_sh_and_the_respawn_share_one_launch_command(repo, monkeypatch):
    """start.sh が使う CLI 出力と、respawn が使う文字列が同一であること。"""
    monkeypatch.setenv("CREWVIA_QUEUE", "/srv/other/queue")
    monkeypatch.setenv("CREWVIA_MUX", "tmux")
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "lib_daemon_watch.py"), "spawn-cmd",
         dw.DAEMON_DISPATCHER, "--repo-root", str(repo)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == dw.spawn_command(dw.DAEMON_DISPATCHER, repo)


# ---------------------------------------------------------------------------
# 4. 読めなかったエントリを「不在」にしない (P2)
# ---------------------------------------------------------------------------

def _fake_proc(tmp_path, entries):
    """`{pid: ("cmdline bytes" | None, "stat text")}` から偽 /proc を作る。

    cmdline を None にすると **ディレクトリ** を置く。read すると EISDIR に
    なるので、root で走らせても必ず「読めない」を再現できる (chmod 000 は
    root では効かない)。
    """
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    for pid, (cmdline, stat) in entries.items():
        d = proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        if cmdline is None:
            (d / "cmdline").mkdir(exist_ok=True)
        else:
            (d / "cmdline").write_bytes(cmdline)
        if stat is None:
            (d / "stat").mkdir(exist_ok=True)
        else:
            (d / "stat").write_text(stat, encoding="utf-8")
    return proc


def test_scan_holds_when_a_proc_entry_cannot_be_read(tmp_path):
    """1 件でも読めなければ走査は未完了 — `[]` ではなく None を返す。

    `[]` は「探して、居なかった」という強い主張で、そのまま respawn の条件に
    なる。読めなかったエントリがあるスキャンにその資格は無い。
    """
    root = tmp_path / "mine"
    proc = _fake_proc(tmp_path, {
        101: (b"bash\x00" + str(root / "scripts" / "dispatcher.sh").encode() + b"\x00",
              "101 (bash) S 1 " + "0 " * 40),
        102: (None, "102 (bash) S 1 " + "0 " * 40),   # 読めない
    })
    assert dw.scan_daemon_pids(root, "watchdog", proc_root=proc) is None


def test_scan_still_treats_a_vanished_entry_as_gone(tmp_path):
    """逆側の過剰修正を止める: 消えたエントリ (ENOENT) は「居ない」でよい。

    走査中にプロセスが終わるのは日常なので、これを None にすると respawn が
    二度と起きない。
    """
    root = tmp_path / "mine"
    proc = tmp_path / "proc"
    (proc / "12345").mkdir(parents=True)          # cmdline ごと存在しない
    assert dw.scan_daemon_pids(root, "watchdog", proc_root=proc) == []


def test_an_unreadable_proc_entry_stops_the_respawn(repo, tmp_path):
    """判定への接続: 走査が未完了なら respawn しない。"""
    proc = _fake_proc(tmp_path, {777: (None, "777 (bash) S 1 " + "0 " * 40)})
    watch, mux, _ = _dead_peer_watch(
        repo, scan=lambda root, name: dw.scan_daemon_pids(root, name, proc_root=proc))
    verdict = watch.watch_peer()
    assert verdict.action == dw.ACTION_HOLD, verdict
    assert mux.spawned == []


def _idle_shell_stat(pid):
    """プロンプトで待っている対話シェルと同じ形の stat 行。

    state=S / pgrp=session=tpgid=pid / tty_nr≠0 — 実測 (`bash` を pty 上で
    起動したときの値) に合わせてある。ここを「いかにも idle」にしておかないと、
    このファイルのテストが *別の* 理由で緑になり、確かめたい 1 点を隠す。
    """
    return f"{pid} (bash) S 1 {pid} {pid} 34816 {pid} " + "0 " * 40


def test_live_children_holds_when_an_entry_cannot_be_read(tmp_path):
    """`_live_children()` も同じ — 読めない stat があれば None。"""
    proc = _fake_proc(tmp_path, {
        4242: (b"bash\x00", _idle_shell_stat(4242)),
        4243: (b"bash\x00", None),                # 読めない
    })
    assert lib_mux._live_children(4242, proc_root=str(proc)) is None


def test_live_children_still_answers_when_entries_merely_vanished(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "4242").mkdir()                        # stat が無い = 消えた
    assert lib_mux._live_children(4242, proc_root=str(proc)) == []


def test_a_pane_whose_children_cannot_be_counted_is_busy(tmp_path):
    """数え切れなかった pane は「使用中」に倒す (上書き起動しない)。

    stat は「プロンプトで待つシェル」そのもの。子を数えられなかった一点だけで
    busy に倒れることを確かめている。
    """
    proc = _fake_proc(tmp_path, {
        4242: (b"bash\x00", _idle_shell_stat(4242)),
        4243: (b"bash\x00", None),
    })
    assert lib_mux._pane_shell_is_idle(4242, proc_root=str(proc)) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
