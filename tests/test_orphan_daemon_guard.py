#!/usr/bin/env python3
"""
tests/test_orphan_daemon_guard.py

Regression テスト: 孤児 watchdog / dispatcher の自己同一性チェック (t003)

## 背景

herdr server は起動時 env を全ペインへ永続継承する(既知の落とし穴)。そのため
「worktree で試しに起動した watchdog.py / dispatcher.sh」が、本番 herdr
workspace を env 継承したまま生き残る経路が構造的に存在する。両プロセスとも
生存監視の結果として Worker pane を kill する権限を持つため、自分の出自
(worktree) が既に削除されているのに本番 pane を誤 kill しうる。

## 修正内容

`scripts/lib_mux.py` に `repo_identity_ok(repo_root)` を追加し、両デーモンの
kill 経路にこれを差し込んだ:

  - `scripts/watchdog.py`
    - `_assert_repo_identity_or_exit()` — 毎メインループ冒頭 (粗い判定)
    - `graceful_terminate()` — SIGTERM 直前・SIGKILL 直前にそれぞれ再チェック
      (きめ細かい判定。TERMINATE_GRACE_PERIOD=60s / KILL_DELAY=10s の待機中に
      worktree が消される可能性を捕捉する)
  - `scripts/dispatcher.sh` (heredoc 埋め込み python)
    - モジュールロード時 (= 1 dispatch cycle ごと。python3 プロセスは cycle
      ごとに使い捨てなので、これが「起動時」と「ループ毎」を兼ねる)
    - `tmux_kill_window()` — 実際の kill 直前 (3 箇所ある呼び出し元すべてが
      通る唯一の choke point)

倒れる方向は必ず「kill しない (見逃す)」側— チェック失敗時は静かに諦める。

## 隔離方針 (本番を巻き込まない)

- registry は env override 不可 (既知の落とし穴) なので、対象コードが読む
  repo_root 相当のパスは毎回 `tempfile.mkdtemp()` で作った使い捨てディレクトリ。
- notify cache は本物の `/tmp/dispatcher-notify-cache.json` を一切使わない
  (dispatcher 側は tmux_kill_window() 単体を exec するだけで dispatch() 本体
  は呼ばない — 通知ロジックに触れない)。
- 実 tmux / herdr には一切触れない。`_mux` はどちらのテストでも FakeMux に
  差し替える。os.kill / time.sleep もモックし、本物のシグナルは一切送らない。
- dispatcher.sh 側は heredoc に埋め込まれた python を実際に `exec()` して
  検証する(コピー実装ではなく本物のコードパス)。末尾の `publish_agents()` /
  `dispatch()` 呼び出しだけを切り離して実行しないようにしている — それ以外は
  すべて本番と同一のソーステキスト。

実行方法:
  python3 -m pytest tests/test_orphan_daemon_guard.py -v
"""

import re
import shutil
import signal
import sys
import tempfile
import types
from pathlib import Path

import pytest

REPO_ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_mux  # noqa: E402
import watchdog  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_fake_repo() -> Path:
    """A throwaway dir that looks like a valid git checkout to repo_identity_ok()."""
    tmp = Path(tempfile.mkdtemp(prefix="crewvia-orphan-guard-test-"))
    (tmp / ".git").mkdir()
    return tmp


# ---------------------------------------------------------------------------
# 1. lib_mux.repo_identity_ok() — pure unit tests
# ---------------------------------------------------------------------------

class TestRepoIdentityOk:
    def test_valid_git_checkout_is_ok(self):
        repo = _make_fake_repo()
        try:
            assert lib_mux.repo_identity_ok(repo) is True
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_linked_worktree_dot_git_file_is_ok(self):
        """A linked worktree has `.git` as a *file* (pointer to the main repo's
        gitdir), not a directory — repo_identity_ok() must accept both."""
        repo = Path(tempfile.mkdtemp(prefix="crewvia-orphan-guard-test-"))
        try:
            (repo / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
            assert lib_mux.repo_identity_ok(repo) is True
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_deleted_directory_is_not_ok(self):
        """The core scenario this whole task exists for: the worktree was
        `git worktree remove`d (or otherwise deleted) out from under a still
        -running daemon."""
        repo = _make_fake_repo()
        shutil.rmtree(repo)
        assert lib_mux.repo_identity_ok(repo) is False

    def test_directory_without_git_is_not_ok(self):
        """Directory exists but was recreated as a plain (non-git) dir —
        must not be treated as a valid identity either."""
        tmp = Path(tempfile.mkdtemp(prefix="crewvia-orphan-guard-test-"))
        try:
            assert lib_mux.repo_identity_ok(tmp) is False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_nonexistent_path_is_not_ok(self):
        assert lib_mux.repo_identity_ok("/nonexistent/path/does/not/exist") is False


# ---------------------------------------------------------------------------
# 2. watchdog.py — _assert_repo_identity_or_exit() (coarse, loop-top guard)
# ---------------------------------------------------------------------------

class TestWatchdogLoopTopGuard:
    def test_valid_repo_root_does_not_exit(self):
        repo = _make_fake_repo()
        try:
            watchdog._assert_repo_identity_or_exit(repo)  # must not raise
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_missing_repo_root_exits(self):
        repo = _make_fake_repo()
        shutil.rmtree(repo)
        with pytest.raises(SystemExit) as exc_info:
            watchdog._assert_repo_identity_or_exit(repo)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# 3. watchdog.py — graceful_terminate() (fine-grained, pre-kill re-check)
# ---------------------------------------------------------------------------

class FakeMux:
    """Stand-in for lib_mux.Mux — no real tmux/herdr backend touched."""

    def __init__(self, window_name="Sofia-worker"):
        self._window_name = window_name
        self.sent = []
        self.window_present = True

    def list(self):
        return [self._window_name] if self.window_present else []

    def send(self, name, msg):
        self.sent.append((name, msg))
        return True

    def pid(self, name):
        return 999999  # never a real PID — os.kill is mocked in every test below


def _make_monitor(repo_root: Path, fake_mux: FakeMux) -> "watchdog.WorkerMonitor":
    monitor = watchdog.WorkerMonitor(
        task_id="t999",
        task_card={"worker": "Sofia"},
        profiles=watchdog.PROFILES,
        repo_root=repo_root,
    )
    return monitor


class TestGracefulTerminateGuard:
    def setup_method(self):
        self._orig_mux = watchdog._mux
        self._orig_grace = watchdog.TERMINATE_GRACE_PERIOD
        self._orig_delay = watchdog.KILL_DELAY
        self._orig_kill = watchdog.os.kill
        self._orig_sleep = watchdog.time.sleep

    def teardown_method(self):
        watchdog._mux = self._orig_mux
        watchdog.TERMINATE_GRACE_PERIOD = self._orig_grace
        watchdog.KILL_DELAY = self._orig_delay
        watchdog.os.kill = self._orig_kill
        watchdog.time.sleep = self._orig_sleep

    def test_entry_check_blocks_when_repo_already_gone(self):
        """repo_root already invalid before graceful_terminate() is even
        called — must send no shutdown message and issue no signal at all."""
        repo = _make_fake_repo()
        shutil.rmtree(repo)  # gone before the call

        fake_mux = FakeMux()
        watchdog._mux = fake_mux
        killed = []
        watchdog.os.kill = lambda pid, sig: killed.append(sig)

        monitor = _make_monitor(repo, fake_mux)
        watchdog.graceful_terminate(monitor)

        assert fake_mux.sent == [], "must not send shutdown message once identity is invalid"
        assert killed == [], "must not signal the pane once identity is invalid"

    def test_removed_during_grace_period_blocks_sigterm(self):
        """repo_root is valid at entry (so the shutdown message *is* sent),
        but gets removed while graceful_terminate() is waiting out
        TERMINATE_GRACE_PERIOD — the pre-SIGTERM re-check must catch this."""
        repo = _make_fake_repo()
        watchdog.TERMINATE_GRACE_PERIOD = 2
        watchdog.KILL_DELAY = 1

        fake_mux = FakeMux()
        fake_mux.window_present = True  # stays "present" so the wait loop runs to completion
        watchdog._mux = fake_mux
        killed = []
        watchdog.os.kill = lambda pid, sig: killed.append(sig)

        sleep_calls = {"n": 0}

        def fake_sleep(_seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] == 1:
                # simulate the worktree being removed mid-wait
                shutil.rmtree(repo, ignore_errors=True)

        watchdog.time.sleep = fake_sleep

        monitor = _make_monitor(repo, fake_mux)
        watchdog.graceful_terminate(monitor)

        assert len(fake_mux.sent) == 1, "shutdown message should have been sent (identity was valid at entry)"
        assert killed == [], "SIGTERM must be refused once identity became invalid mid-wait"

    def test_removed_during_kill_delay_blocks_sigkill(self):
        """repo_root survives through SIGTERM but is removed during the
        KILL_DELAY wait — the pre-SIGKILL re-check must catch this."""
        repo = _make_fake_repo()
        watchdog.TERMINATE_GRACE_PERIOD = 2
        watchdog.KILL_DELAY = 1

        fake_mux = FakeMux()
        fake_mux.window_present = True
        watchdog._mux = fake_mux
        killed = []
        watchdog.os.kill = lambda pid, sig: killed.append(sig)

        sleep_calls = {"n": 0}

        def fake_sleep(_seconds):
            sleep_calls["n"] += 1
            # sleep calls 1..TERMINATE_GRACE_PERIOD are the grace-period wait;
            # call TERMINATE_GRACE_PERIOD + 1 is the KILL_DELAY wait after
            # SIGTERM. Remove the repo during that one.
            if sleep_calls["n"] == watchdog.TERMINATE_GRACE_PERIOD + 1:
                shutil.rmtree(repo, ignore_errors=True)

        watchdog.time.sleep = fake_sleep

        monitor = _make_monitor(repo, fake_mux)
        watchdog.graceful_terminate(monitor)

        assert signal.SIGTERM in killed, "SIGTERM should have fired (identity valid through grace period)"
        assert signal.SIGKILL not in killed, "SIGKILL must be refused once identity became invalid during KILL_DELAY"

    def test_positive_control_full_termination_when_identity_stays_valid(self):
        """Control case: if the repo_root never becomes invalid, the guard
        must not get in the way — both SIGTERM and SIGKILL fire as before."""
        repo = _make_fake_repo()
        try:
            watchdog.TERMINATE_GRACE_PERIOD = 2
            watchdog.KILL_DELAY = 1

            fake_mux = FakeMux()
            fake_mux.window_present = True
            watchdog._mux = fake_mux
            killed = []
            watchdog.os.kill = lambda pid, sig: killed.append(sig)
            watchdog.time.sleep = lambda _s: None

            monitor = _make_monitor(repo, fake_mux)
            watchdog.graceful_terminate(monitor)

            assert len(fake_mux.sent) == 1
            assert killed == [signal.SIGTERM, signal.SIGKILL]
        finally:
            shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. dispatcher.sh — the real heredoc-embedded python, exec()'d in isolation
# ---------------------------------------------------------------------------

DISPATCHER_SH = SCRIPTS_DIR / "dispatcher.sh"


class FakeDispatcherMux:
    """Stand-in for lib_mux.Mux inside the dispatcher heredoc — no real
    tmux/herdr backend touched."""

    def __init__(self):
        self.killed = []

    def kill(self, target):
        self.killed.append(target)
        return True

    # dispatch()/publish_agents() are never invoked by these tests, so no
    # other Mux methods need to exist here.


def _load_dispatcher_namespace(repo_root: Path) -> dict:
    """exec() the real python embedded in dispatcher.sh's heredoc, with the
    trailing `publish_agents()` / `dispatch()` full-cycle calls stripped off
    (this is what makes it a targeted unit of tmux_kill_window() instead of
    running an actual dispatch cycle against a fake queue) and lib_mux
    replaced by a fake Mux (repo_identity_ok itself stays real — that's the
    function under test).

    Returns the resulting namespace dict, or raises SystemExit if the
    module-level self-identity guard fires (used by the negative test below).
    """
    text = DISPATCHER_SH.read_text()
    m = re.search(r"<<'PYEOF'\n(.*)\nPYEOF", text, re.S)
    assert m, "could not locate the python heredoc in dispatcher.sh — has the marker changed?"
    src = m.group(1)
    src, n = re.subn(r"\npublish_agents\(\)\ndispatch\(\)\s*$", "", src)
    assert n == 1, "could not strip the trailing publish_agents()/dispatch() call — dispatcher.sh structure changed?"

    fake_lib_mux = types.ModuleType("lib_mux")
    fake_lib_mux.Mux = FakeDispatcherMux
    fake_lib_mux.repo_identity_ok = lib_mux.repo_identity_ok  # real function under test
    sys.modules["lib_mux"] = fake_lib_mux

    queue_dir = repo_root / "queue"
    registry_dir = repo_root / "registry"
    (registry_dir / "mux").mkdir(parents=True, exist_ok=True)
    queue_dir.mkdir(parents=True, exist_ok=True)

    argv = [
        "dispatcher-embedded",
        str(queue_dir),
        str(registry_dir),
        str(repo_root / "notify-cache.json"),
        "300",
        "60",
        str(repo_root / "dispatcher.log"),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        ns = {"__name__": "dispatcher_under_test"}
        exec(compile(src, str(DISPATCHER_SH) + " (embedded, test)", "exec"), ns)
        return ns
    finally:
        sys.argv = old_argv
        del sys.modules["lib_mux"]


class TestDispatcherKillWindowGuard:
    def test_module_level_guard_exits_when_repo_root_already_gone(self):
        """The coarse, once-per-cycle check: if REPO_ROOT (REGISTRY_DIR.parent)
        is not a valid git checkout, the whole embedded script must refuse to
        load past that point (sys.exit(1)) rather than proceed into
        dispatch()/tmux_kill_window() territory at all."""
        repo = Path(tempfile.mkdtemp(prefix="crewvia-orphan-guard-test-"))
        try:
            # deliberately no .git — repo_identity_ok() must return False
            with pytest.raises(SystemExit) as exc_info:
                _load_dispatcher_namespace(repo)
            assert exc_info.value.code == 1
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_tmux_kill_window_proceeds_with_valid_identity(self):
        repo = _make_fake_repo()
        try:
            ns = _load_dispatcher_namespace(repo)
            ns["tmux_kill_window"]("Test-worker")
            fake_mux = ns["_mux"]
            assert fake_mux.killed == ["Test-worker"]
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_tmux_kill_window_refuses_once_repo_root_removed(self):
        """The fine-grained, pre-kill choke point inside tmux_kill_window()
        itself: repo_root was valid when the process started (module-level
        guard passed), but gets removed before this particular kill call —
        must be refused."""
        repo = _make_fake_repo()
        ns = _load_dispatcher_namespace(repo)
        shutil.rmtree(repo)  # removed after module load, before the kill call

        ns["tmux_kill_window"]("Test-worker")
        fake_mux = ns["_mux"]
        assert fake_mux.killed == [], "kill must be refused once repo_root no longer exists"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
