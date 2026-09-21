#!/usr/bin/env python3
"""
tests/test_retirement.py

t002 (mission 20260921-daemon-authority-and-mutual-watch):
kill 権限の watchdog への一元化 — 後始末と中断耐性。

## 直している欠陥 (RED 群が再現するもの)

**E1 — 後始末の担い手が居ない。**  watchdog は Worker プロセスを殺すが
`queue/` には一切触れない。task は `in_progress` のまま、
`queue/assignments/<agent>` も残る。掃除は dispatcher の vanished 検知が
Director に `plan.sh update --reset` を促すことにしか依存しておらず、
dispatcher が落ちている間に watchdog が Worker を殺すと**誰も気付かない
幽霊 task** が残る (`knowledge/daemon-authority.md` §2-3)。

**中断で宙に浮く。**  `graceful_terminate()` は
「send → 60s 待機 → SIGTERM → 10s 待機 → SIGKILL」の 70 秒間 main loop を
ブロックし、その間の状態はプロセスメモリにしか無い。ここで watchdog が
再起動されると SIGKILL は永久に来ず、task も assignment も残る (§4-1)。

## RED をどう「赤い」と示すか

RED 群は新 API を直接叩かない。`drive_full_termination()` という 1 つの
アダプタ越しに「この checkout が提供する方法で Worker を 1 体終了させる」だけを
行い、**終了後に queue がどうなっているか**を assert する。アダプタは
修正後なら retirement の phase machine を、修正前 (origin/main) なら
`watchdog.graceful_terminate()` を呼ぶ。つまり同じファイルを origin/main の
checkout にコピーして流せばそのまま対照実験になる
(`tests/watchdog-idle-e2e.sh` のシナリオ 4 と同じ考え方)。

    # RED (origin/main の scripts/ に対して):
    python3 -m pytest tests/test_retirement.py -v -k red

## テストケース

RED (修正前の実装では失敗する):
  test_red_terminated_worker_leaves_no_ghost_task
  test_red_interrupted_termination_is_resumable

安全側 (誤 kill に倒さないこと):
  test_identity_mismatch_discards_instead_of_killing
  test_unreadable_identity_does_not_authorise_kill
  test_repo_identity_failure_skips_without_discarding
  test_mux_unavailable_processes_nothing
  test_worker_that_picked_up_work_is_spared
  test_timeout_retirement_is_not_spared_by_its_own_assignment
  test_request_refused_when_no_spawn_identity

原子性・中断耐性:
  test_phase_is_durable_before_the_message_is_sent
  test_cycle_never_blocks
  test_recover_rebases_deadline_after_restart
  test_second_request_does_not_restart_the_machine

後始末の失敗:
  test_cleanup_failure_keeps_marker_and_notifies_once
  test_cleanup_failure_is_retried_until_it_succeeds
  test_refuses_to_start_when_plan_sh_is_missing

既存ガードの回帰 (弱めていないこと):
  test_mass_kill_guard_conditions_unchanged

t018 backlog の小修正:
  test_verdict_logger_forget_drops_state
  test_verdict_logger_logs_reason_change_within_same_verdict

t019 — 後始末の誤発火 (QA FAIL-1 / Codex P1 3 件。いずれも「Worker がまだ
生きている / task がもう別の状態なのに後始末が無条件に走る」が根):
  test_red_transient_list_failure_does_not_reset_a_live_worker
  test_red_cleanup_does_not_reopen_work_finished_during_the_grace_period
  test_red_cleanup_does_not_clear_a_successor_assignment
  test_red_failed_sigkill_does_not_declare_the_worker_terminated
  test_red_sigkill_waits_for_the_process_to_actually_exit
  test_repeated_sigkill_failure_is_reported_once
  test_empty_listing_with_a_live_backend_does_not_authorise_cleanup
  test_dead_recorded_pid_with_a_live_window_is_a_successor_not_a_death
  test_window_gone_is_concluded_when_the_backend_itself_is_down
    ↑ 最後の 2 本は逆向きの担保: 安全側に倒しすぎて永久に終わらない経路を
      作っていないこと / 後任を巻き込まないこと。
  (plan.sh 側の前提条件は tests/plan-update-expect.bats)

実行方法:
  python3 -m pytest tests/test_retirement.py -v
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import watchdog  # noqa: E402

AGENT = "Retiree"
WINDOW = f"{AGENT}-worker"
SLUG = "20260921-retiretest"
TASK_ID = "t001"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """生きているか。**ゾンビは死んだ扱い**。

    テストの Worker 役は subprocess.Popen の子なので、kill した直後は
    wait() されるまでゾンビとして残り `os.kill(pid, 0)` が成功し続ける。
    それを「生きている」と読むと、実際には殺せているのにテストが
    「殺せていない」と言い張る。/proc の state で見分ける。
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    try:
        state = stat[stat.rindex(")") + 2]
    except (ValueError, IndexError):
        return False
    return state != "Z"


class FakeMux:
    """window 名 → pane_pid の最小 mux。

    `list()` は **実プロセスの生死** を反映する。本物の kill を撃ったあとに
    窓が消えるところまで再現しないと、「殺したのに phase が進まない」類の
    バグをテストが素通りさせてしまう。本番の herdr / tmux には一切触れない。
    """

    def __init__(self, windows: dict = None, available: bool = True):
        self.windows = dict(windows or {})
        self.available_flag = available
        self.sent: list = []
        self.killed: list = []

    def available(self) -> bool:
        return self.available_flag

    def list(self, suffix=None):
        names = [n for n, pid in self.windows.items()
                 if pid is None or _pid_alive(pid)]
        if suffix:
            names = [n for n in names if n.endswith(suffix)]
        return names

    def pid(self, name):
        pid = self.windows.get(name)
        if pid is None or not _pid_alive(pid):
            return None
        return pid

    def send(self, name, text):
        self.sent.append((name, text))
        return name in self.list()

    def kill(self, name):
        self.killed.append(name)
        self.windows.pop(name, None)
        return True


class Sandbox:
    """使い捨ての crewvia repo。本番の queue/ registry/ には触れない。"""

    def __init__(self, root: Path):
        self.root = root
        self.registry = root / "registry"
        self.queue = root / "queue"
        self.scripts = root / "scripts"
        self.notes: list = []
        self.logs: list = []

    @property
    def task_file(self) -> Path:
        return self.queue / "missions" / SLUG / "tasks" / f"{TASK_ID}.md"

    @property
    def assignment_file(self) -> Path:
        return self.queue / "assignments" / AGENT

    def task_status(self) -> str:
        for line in self.task_file.read_text().splitlines():
            if line.startswith("status:"):
                return line.split(":", 1)[1].strip()
        return "?"

    def task_worker(self) -> str:
        for line in self.task_file.read_text().splitlines():
            if line.startswith("worker:"):
                return line.split(":", 1)[1].strip()
        return "?"

    def spawn_worker_process(self) -> int:
        """Worker 役の実プロセス。Claude ではないので殺しても失うものが無い。"""
        proc = subprocess.Popen(["sleep", "300"])
        self._procs.append(proc)
        return proc.pid

    def record_identity(self, target: str, pane_pid: int) -> None:
        """dispatcher の `_spawn_time_fallback()` 相当 (tmux backend の identity)。"""
        mux_dir = self.registry / "mux"
        mux_dir.mkdir(parents=True, exist_ok=True)
        (mux_dir / f"{target}.firstseen").write_text(str(time.time()))


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "repo"
    sb = Sandbox(root)
    sb._procs = []

    (root / "scripts").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "plan.sh", root / "scripts" / "plan.sh")

    mission = root / "queue" / "missions" / SLUG
    (mission / "tasks").mkdir(parents=True)
    (root / "queue" / "assignments").mkdir(parents=True)
    (root / "registry").mkdir(parents=True)

    (root / "queue" / "state.yaml").write_text(
        f"active_missions:\n  - {SLUG}\ndefault_mission: {SLUG}\n"
    )
    (mission / "mission.yaml").write_text(
        f'title: retirement test\nslug: {SLUG}\nstatus: in_progress\n'
        f'created_at: "2026-09-21T00:00:00Z"\ncompleted_at: null\nnext_task_id: 2\n'
    )
    (mission / "tasks" / f"{TASK_ID}.md").write_text(
        f"---\nid: {TASK_ID}\ntitle: retirement fixture\nskills: [code]\n"
        f"priority: high\nstatus: in_progress\nblocked_by: []\ntarget_dir: null\n"
        f'worker: {AGENT}\nstarted_at: "2026-09-21T00:00:00Z"\ncompleted_at: null\n'
        f"---\n\n## Description\nfixture\n\n## Result\n"
    )
    (root / "queue" / "assignments" / AGENT).write_text(f"{SLUG}:{TASK_ID}")

    # git checkout: repo_identity_ok() は .git の存在を確かめる
    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   capture_output=True)

    yield sb

    for proc in sb._procs:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def make_idle(sandbox):
    """「仕事を持っていない Worker」の状態にする。

    dispatcher が idle と判断する材料は `queue/assignments/<agent>` の不在
    そのものなので、no-task / blocked-stuck の retirement を組み立てる
    テストは assignment を消してからでないと前提が食い違う (executor は
    破壊ステップの直前に同じ材料を見直して、仕事を拾った Worker を守る)。
    """
    sandbox.assignment_file.unlink(missing_ok=True)


def make_executor(sandbox, mux, **kwargs):
    """本番の組み立て (`watchdog.make_retirement_executor`) と同じ形の executor。

    タイミング系だけテスト向けに縮める。phase の並びと guard は本番のまま。
    """
    import lib_retirement

    params = dict(
        registry_dir=sandbox.registry,
        repo_root=sandbox.root,
        mux=mux,
        repo_identity_check=lambda: True,
        log=sandbox.logs.append,
        notify=lambda msg: (sandbox.notes.append(msg), True)[1],
        plan_sh=sandbox.scripts / "plan.sh",
        queue_dir=sandbox.queue,
        grace_period=0,
        kill_delay=0,
    )
    params.update(kwargs)
    return lib_retirement.RetirementExecutor(**params)


# ---------------------------------------------------------------------------
# Adapter — "この checkout が提供する方法で Worker を 1 体終了させる"
# ---------------------------------------------------------------------------

def drive_full_termination(sandbox, mux, pane_pid, max_cycles: int = 20) -> str:
    """Worker を 1 体、終了まで運ぶ。戻り値はどちらの経路を通ったか。

    修正後: dispatcher が書くのと同じ retirement marker を 1 本置き、
            phase machine を settle するまで回す。
    修正前: watchdog.graceful_terminate() をそのまま呼ぶ (70 秒ブロックする
            ので待機時間だけ 0 に潰す)。後始末は誰もしない — それが RED。
    """
    if hasattr(watchdog, "make_retirement_executor"):
        ex = make_executor(sandbox, mux)
        assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
        for _ in range(max_cycles):
            ex.process_all()
            if not ex.has_marker(AGENT):
                return "retirement"
        raise AssertionError(f"retirement did not settle in {max_cycles} cycles")

    # --- pre-t002 path ---------------------------------------------------
    watchdog._mux = mux
    watchdog.TERMINATE_GRACE_PERIOD = 0
    watchdog.KILL_DELAY = 0
    monitor = watchdog.WorkerMonitor(
        task_id=TASK_ID,
        task_card={"worker": AGENT, "timeout": {"idle": 1, "max": 1}},
        profiles=watchdog.PROFILES,
        repo_root=sandbox.root,
    )
    watchdog.graceful_terminate(monitor)
    for _ in range(50):
        if not _pid_alive(pane_pid):
            break
        time.sleep(0.1)
    return "graceful_terminate"


def _legacy_monitor(sandbox):
    return watchdog.WorkerMonitor(
        task_id=TASK_ID,
        task_card={"worker": AGENT, "timeout": {"idle": 1, "max": 1}},
        profiles=watchdog.PROFILES,
        repo_root=sandbox.root,
    )


def interrupt_mid_termination(sandbox, mux, pane_pid) -> None:
    """終了処理を始め、その途中で**デーモンを本当に殺す**。

    修正後: 1 cycle 進めて (= shutdown 通知まで) executor を捨てる。
    修正前: graceful_terminate() を fork した子で走らせ、待機中に SIGKILL する。
            ブロッキング待機そのものが中断点なので、そこを実際に断ち切らないと
            「状態がメモリにしか無い」という欠陥を迂回してしまう。
    """
    import signal as _signal

    if hasattr(watchdog, "make_retirement_executor"):
        ex = make_executor(sandbox, mux, grace_period=3600)
        assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
        ex.process_all()
        del ex
        return

    child = os.fork()
    if child == 0:  # pragma: no cover — 子プロセス側
        try:
            watchdog._mux = mux
            watchdog.TERMINATE_GRACE_PERIOD = 3600
            watchdog.KILL_DELAY = 10
            watchdog.graceful_terminate(_legacy_monitor(sandbox))
        finally:
            os._exit(0)
    time.sleep(1.0)  # メッセージ送信は済み、SIGTERM 前
    os.kill(child, _signal.SIGKILL)
    os.waitpid(child, 0)


def resume_after_restart(sandbox, mux, max_cycles: int = 20) -> None:
    """デーモンを起動し直して、中断された終了処理を完結させる。

    修正前にはこれに当たる仕組みが存在しない。新しい watchdog は
    in_progress の task から monitor を作り直すだけで、前任が終了処理の
    どこまで進んでいたかを知る手段が無い (だから何もしないのが正しい再現)。
    """
    if not hasattr(watchdog, "make_retirement_executor"):
        return
    ex = make_executor(sandbox, mux)
    ex.recover()
    for _ in range(max_cycles):
        ex.process_all()
        if not ex.has_marker(AGENT):
            return


# ---------------------------------------------------------------------------
# RED — 修正前の実装ではここが失敗する
# ---------------------------------------------------------------------------

def test_red_terminated_worker_leaves_no_ghost_task(sandbox):
    """E1: Worker を終了させたら task は pending に戻り assignment は消える。

    修正前は「プロセスは死んだが task は in_progress のまま」になる。
    dispatcher が落ちていれば誰も掃除しないので、その task は永久に
    誰にも割り当てられない幽霊になる。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    drive_full_termination(sandbox, mux, pane_pid)

    assert not _pid_alive(pane_pid), "Worker プロセスが終了していない"
    assert sandbox.task_status() == "pending", (
        f"task が {sandbox.task_status()} のまま — 幽霊 task が残っている"
    )
    assert sandbox.task_worker() == "null"
    assert not sandbox.assignment_file.exists(), (
        "queue/assignments が残っている — 次の Worker が idle 判定されない"
    )


def test_red_interrupted_termination_is_resumable(sandbox):
    """中断耐性: 終了処理の途中でデーモンが落ちても、次の起動が完結させる。

    修正前の `graceful_terminate()` は「send → 60s → SIGTERM → 10s → SIGKILL」の
    70 秒間 main loop をブロックし、その進捗はプロセスメモリにしか存在しない。
    ここで落ちると SIGKILL は永久に来ず、再起動した watchdog は中断があった
    ことを知る手段を持たない (ディスクに 1 バイトも残っていない) ため、
    Worker は生き残り task は in_progress のまま宙に浮く。

    実際にデーモンを殺して再起動するところまでやる。モックした「中断」では
    「状態がメモリにしか無い」という欠陥の本体を迂回してしまう。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    interrupt_mid_termination(sandbox, mux, pane_pid)
    assert _pid_alive(pane_pid), (
        "前提が崩れた: 中断時点ではまだ猶予期間中で Worker は生きているはず"
    )

    resume_after_restart(sandbox, mux)

    assert not _pid_alive(pane_pid), (
        "再起動後も Worker が生き残っている — 中断した終了処理を誰も引き取れていない"
    )
    assert sandbox.task_status() == "pending", (
        f"task が {sandbox.task_status()} のまま宙に浮いている"
    )
    assert not sandbox.assignment_file.exists()


# ---------------------------------------------------------------------------
# 安全側 — 誤って殺さないこと
# ---------------------------------------------------------------------------

def test_identity_mismatch_discards_instead_of_killing(sandbox):
    """§3-3 F2: 同名の別 Worker に着弾させない。

    crewvia は Worker 名を再利用する。marker が元の Worker より長生きした
    場合、窓の名前が一致するというだけで殺すと、元気に働いている後継を
    殺すことになる。これは §5-2 が「いちばん危険」と名指ししている事故。
    """
    old_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, old_pid)
    mux = FakeMux({WINDOW: old_pid})
    make_idle(sandbox)

    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "no-task")

    # 元の Worker が自力終了し、Director が同じ名前で別の Worker を起動した
    successor_pid = sandbox.spawn_worker_process()
    mux.windows[WINDOW] = successor_pid

    for _ in range(5):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert _pid_alive(successor_pid), "同名の後継 Worker を殺してしまった"
    assert not ex.has_marker(AGENT), "marker が残り続けている"
    assert any("NOT retiring" in line for line in sandbox.logs), sandbox.logs
    # 殺していないのだから幽霊 task も無い → 復旧レシピを送ってはいけない
    assert sandbox.notes == [], f"不要な Director 通知: {sandbox.notes}"


def test_unreadable_identity_does_not_authorise_kill(sandbox):
    """identity が現在読めないときは「一致」ではなく「不一致」に倒す。"""
    import lib_retirement

    ok, why = lib_retirement.identity_matches(
        {"pane_pid": 1234, "created_at": None},
        {"pane_pid": None, "created_at": None},
    )
    assert ok is False and "unavailable" in why

    ok, why = lib_retirement.identity_matches({"pane_pid": None, "created_at": None},
                                              {"pane_pid": 99, "created_at": 1.0})
    assert ok is False, "照合できる記録が 1 つも無いのに一致と判定した"

    ok, _ = lib_retirement.identity_matches({"pane_pid": 99, "created_at": None},
                                            {"pane_pid": 99, "created_at": 1.0})
    assert ok is True


def test_repo_identity_failure_skips_without_discarding(sandbox):
    """R6: 自分の repo identity が怪しいときは何もしない。marker は捨てない。

    これは「相手が違う」ではなく「自分が信用できない」状態なので、
    marker を discard してしまうと復帰後にやり直しが効かない。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    make_idle(sandbox)

    ex = make_executor(sandbox, mux, repo_identity_check=lambda: False)
    assert ex.request(AGENT, WINDOW, "no-task")
    for _ in range(3):
        ex.process_all()

    assert _pid_alive(pane_pid)
    assert ex.has_marker(AGENT), "repo identity 失敗で marker を捨ててしまった"

    import lib_retirement
    prog = lib_retirement.read_json(
        lib_retirement.progress_path(sandbox.registry, AGENT))
    assert prog is None or prog["phase"] != lib_retirement.PHASE_DISCARDED


def test_mux_unavailable_processes_nothing(sandbox):
    """backend が答えられない cycle は 1 件も処理しない。

    ここを通してしまうと `mux.list()` の空が「全 Worker 消滅」と読まれ、
    生きている Worker の task を pending に戻す。watchdog の
    `_is_mass_kill()` と同じ向きに倒す。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "no-task", mission=SLUG, task_id=TASK_ID)

    mux.available_flag = False
    mux.windows.clear()
    for _ in range(5):
        assert ex.process_all() == []

    assert _pid_alive(pane_pid)
    assert sandbox.task_status() == "in_progress", "生きている task を戻してしまった"
    assert ex.has_marker(AGENT)


def test_worker_that_picked_up_work_is_spared(sandbox):
    """判定から実行までの間に仕事を拾った Worker は殺さない。

    t002 は「用済みと判断してから実際に閉じるまで」を 1 秒から猶予期間まで
    広げた。その間に Worker が plan.sh pull を通せば
    `queue/assignments/<agent>` が生まれる — dispatcher が idle と判断した
    まさにその材料なので、存在すれば要求の前提が失効している。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    sandbox.assignment_file.unlink()  # 要求時点では idle
    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "no-task")

    # 猶予期間中に Worker が task を拾った
    sandbox.assignment_file.write_text(f"{SLUG}:{TASK_ID}")

    for _ in range(5):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert _pid_alive(pane_pid), "仕事を拾った Worker を殺してしまった"
    assert not ex.has_marker(AGENT), "失効した marker が残っている"
    assert any("picked up a task" in line for line in sandbox.logs), sandbox.logs


def test_timeout_retirement_is_not_spared_by_its_own_assignment(sandbox):
    """逆方向: timeout terminate は assignment があるのが当たり前なので免除しない。

    ここを task_id 無しの経路と同じ扱いにすると、ハングした Worker が
    自分の assignment を盾に永久に生き残る。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    assert sandbox.assignment_file.exists()

    ex = make_executor(sandbox, mux)
    ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    for _ in range(10):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert not _pid_alive(pane_pid), "assignment があるだけで terminate を免れた"
    assert sandbox.task_status() == "pending"


def test_request_refused_when_no_spawn_identity(sandbox):
    """identity が取れない Worker には marker を書かない (fail closed)。

    書いても R7 で必ず discard されるだけで、その間 has_marker() が立つので
    再要求も止まる。§3-3 の「両方 null なら marker を書かない」。
    """
    mux = FakeMux({})  # pane_pid も created_at も無い
    ex = make_executor(sandbox, mux)

    assert ex.request("Ghost", "Ghost-worker", "no-task") is False
    assert not ex.has_marker("Ghost")
    assert any("refusing to request" in line for line in sandbox.logs), sandbox.logs


# ---------------------------------------------------------------------------
# 原子性・中断耐性
# ---------------------------------------------------------------------------

def test_phase_is_durable_before_the_message_is_sent(sandbox):
    """R1: 破壊的ステップの「前」に意図を書く。書けなければ送らない。"""
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    make_idle(sandbox)
    ex = make_executor(sandbox, mux, grace_period=3600)
    ex.request(AGENT, WINDOW, "no-task")

    order = []
    real_write = lib_retirement.write_json_atomic

    def tracking_write(path, data):
        if str(path).endswith(".progress.json"):
            order.append(f"write:{data.get('phase')}")
        return real_write(path, data)

    original_send = mux.send

    def tracking_send(name, text):
        order.append("send")
        return original_send(name, text)

    mux.send = tracking_send
    lib_retirement.write_json_atomic = tracking_write
    try:
        ex.process_all()
    finally:
        lib_retirement.write_json_atomic = real_write

    assert order == ["write:notified", "send"], order


def test_failed_progress_write_stops_before_acting(sandbox):
    """進捗を書けなかった cycle は何も送らない (R1 の裏面)。"""
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    make_idle(sandbox)
    ex = make_executor(sandbox, mux)
    ex.request(AGENT, WINDOW, "no-task")

    real_write = lib_retirement.write_json_atomic
    lib_retirement.write_json_atomic = lambda path, data: (
        real_write(path, data) if not str(path).endswith(".progress.json") else False
    )
    try:
        ex.process_all()
    finally:
        lib_retirement.write_json_atomic = real_write

    assert mux.sent == [], "進捗を永続化できていないのにメッセージを送った"
    assert _pid_alive(pane_pid)


def test_cycle_never_blocks(sandbox):
    """R2: 1 cycle の所要時間は待機で伸びない。

    旧実装は 1 体につき最大 70 秒 main loop を止め、N 体なら 70N 秒だった。
    """
    mux = FakeMux({})
    ex = make_executor(sandbox, mux, grace_period=60, kill_delay=10)
    for i in range(5):
        name = f"W{i}-worker"
        pid = sandbox.spawn_worker_process()
        mux.windows[name] = pid
        sandbox.record_identity(name, pid)
        assert ex.request(f"W{i}", name, "no-task")

    started = time.monotonic()
    ex.process_all()
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"1 cycle が {elapsed:.1f}s ブロックした"
    assert len(mux.sent) == 5, "5 体ぶんの shutdown が 1 cycle で出ていない"


def test_recover_rebases_deadline_after_restart(sandbox):
    """R3: 再起動時に deadline を now 起点で張り直す。

    deadline は絶対 epoch なので、張り直さないと再起動直後に全件が即
    エスカレーションする。「書いたが送れていなかった」場合、Worker は
    猶予を 1 秒も貰えないまま SIGTERM を受けることになる。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    make_idle(sandbox)
    ex = make_executor(sandbox, mux, grace_period=60)
    ex.request(AGENT, WINDOW, "no-task")
    ex.process_all()

    path = lib_retirement.progress_path(sandbox.registry, AGENT)
    stale = lib_retirement.read_json(path)
    stale["deadline"] = time.time() - 9999
    lib_retirement.write_json_atomic(path, stale)

    assert ex.recover() == [AGENT]
    revived = lib_retirement.read_json(path)
    assert revived["deadline"] > time.time() + 30
    assert revived["phase"] == lib_retirement.PHASE_NOTIFIED


def test_second_request_does_not_restart_the_machine(sandbox):
    """要求が繰り返し来ても phase は巻き戻らない。

    dispatcher は 5 秒ごとに同じ判定を下す。毎回 marker を上書きしていたら
    Worker は永久に「shutdown してね」と言われ続けるだけで、SIGTERM には
    決して到達しない。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    make_idle(sandbox)
    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "no-task") is True
    ex.process_all()

    first = lib_retirement.read_json(
        lib_retirement.progress_path(sandbox.registry, AGENT))
    assert ex.has_marker(AGENT) is True

    ex.process_all()
    second = lib_retirement.read_json(
        lib_retirement.progress_path(sandbox.registry, AGENT))
    assert second["deadline"] == first["deadline"], "deadline が張り直された"
    assert len(mux.sent) == 1, "shutdown メッセージが二度送られた"


# ---------------------------------------------------------------------------
# 後始末の失敗
# ---------------------------------------------------------------------------

def test_cleanup_failure_keeps_marker_and_notifies_once(sandbox):
    """後始末に失敗したら黙って捨てず、marker を残して Director に 1 度だけ言う。"""
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux,
                       run_command=lambda argv, env: (1, "plan.sh: lock timeout"))
    ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    for _ in range(6):
        ex.process_all()

    assert ex.has_marker(AGENT), "後始末できていないのに marker を消した"
    prog = lib_retirement.read_json(
        lib_retirement.progress_path(sandbox.registry, AGENT))
    assert prog["phase"] == lib_retirement.PHASE_CLEANUP_FAILED
    assert prog["cleanup_attempts"] >= 2, "再試行していない"
    assert len(sandbox.notes) == 1, f"Director への通知が重複: {sandbox.notes}"
    assert TASK_ID in sandbox.notes[0] and "--reset" in sandbox.notes[0]


def test_cleanup_failure_is_retried_until_it_succeeds(sandbox):
    """plan.sh が一時的にロックされていただけなら、次の cycle で回復する。"""
    calls = {"n": 0}
    real = None

    def flaky(argv, env):
        calls["n"] += 1
        if calls["n"] == 1:
            return 1, "lock timeout"
        return real(argv, env)

    import lib_retirement
    real = lib_retirement._default_run_command

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux, run_command=flaky)
    ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    for _ in range(10):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert not ex.has_marker(AGENT), "回復後も marker が残っている"
    assert sandbox.task_status() == "pending"
    assert not sandbox.assignment_file.exists()


def test_cleanup_survives_losing_the_request_marker(sandbox):
    """終端の後始末は request ファイルの生存に依存しない。

    Worker を殺し終えた後に request を失うと、「どの task を戻すべきか」を
    知る手段が無くなり、幽霊 task が誰にも気付かれないまま残る。
    phase を書く時点で mission/task_id を progress 側にも写しておく。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux)
    ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    # notified → sigterm_sent → terminated まで進めてから request を失う
    for _ in range(3):
        ex.process_all()
    lib_retirement.unlink_quiet(lib_retirement.request_path(sandbox.registry, AGENT))

    for _ in range(5):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert not _pid_alive(pane_pid)
    assert sandbox.task_status() == "pending", "request を失ったら後始末できなくなった"
    assert not sandbox.assignment_file.exists()


def test_request_lost_while_worker_still_alive_is_left_alone(sandbox):
    """逆に、Worker がまだ生きているうちに request を失ったら手を出さない。

    request が無いということは、次のステップを許可する identity の記録も
    無いということ。殺す根拠が無いので discarded に倒す。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux, grace_period=3600)
    ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    ex.process_all()  # → notified (猶予中なのでまだ生きている)

    lib_retirement.unlink_quiet(lib_retirement.request_path(sandbox.registry, AGENT))
    for _ in range(4):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert _pid_alive(pane_pid), "根拠を失ったのに殺した"
    assert sandbox.task_status() == "in_progress", "殺していないのに task を戻した"
    assert any("discarded" in line for line in sandbox.logs), sandbox.logs


def test_refuses_to_start_when_plan_sh_is_missing(sandbox):
    """後始末の道具が無いなら、そもそも殺し始めない (プランレビュー: fail closed)。

    先に殺してから plan.sh が無いと気付くのは、このタスクが潰そうとしている
    幽霊 task そのものを作る手順になる。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux, plan_sh=sandbox.scripts / "does-not-exist.sh")
    ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    for _ in range(3):
        ex.process_all()

    assert _pid_alive(pane_pid), "後始末できないのに殺した"
    assert mux.sent == []
    assert sandbox.task_status() == "in_progress"
    assert any("REFUSING to start" in line for line in sandbox.logs), sandbox.logs


def test_retirement_without_task_needs_no_cleanup(sandbox):
    """idle / no-task の Worker は task も assignment も持たない。

    この経路で Director に復旧レシピを送ると、存在しない幽霊 task の
    後始末を人間にさせることになる。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    make_idle(sandbox)
    ex = make_executor(sandbox, mux)
    ex.request(AGENT, WINDOW, "no-task")
    for _ in range(10):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert not _pid_alive(pane_pid)
    assert not ex.has_marker(AGENT)
    assert sandbox.notes == [], f"不要な Director 通知: {sandbox.notes}"


# ---------------------------------------------------------------------------
# t019 — 「後始末が誤発火したとき何が失われるか」
#
# QA FAIL-1 と Codex P1 3 件は同じ根を持つ: **Worker がまだ生きている /
# task がもう別の状態になっているのに、後始末が無条件に走る**。
# だからここで assert するのはフラグや phase 名ではなく、誤発火したときに
# 実際に失われるもの — 生きた Worker の task が in_progress のままであること、
# 完了済みの成果が pending に巻き戻らないこと、後任の assignment が残ること。
# (memory: fail-closed-guard-can-recreate-the-defect / recurring-defect-patterns)
# ---------------------------------------------------------------------------


class FlappingMux(FakeMux):
    """`available()` は True、`list()` だけが空を返す backend。

    tmux の `list()` は `returncode != 0` も例外も `[]` に潰し、herdr は
    `pane_list` の 10s タイムアウトを `[]` に潰す (`scripts/lib_mux.py`)。
    一方 outage ゲートの `available()` は tmux では binary が PATH にあるかしか
    見ない。つまり「available=True かつ list=[] かつ Worker は生きている」は
    本番で成立し得る組み合わせで、WSL のメモリ逼迫で 1 回タイムアウトすれば足りる。
    """

    def list(self, suffix=None):
        return []

    def server_running(self):
        return True


class NoPidMux(FakeMux):
    """pane_pid を答えられない backend (identity は mux cache の created_at のみ)。

    `pid()` が None を返すのは herdr の `pane_process_info` が失敗したときの
    実挙動。このとき retirement は「記録した pid を /proc で見る」という
    一番強い証拠を持てないので、窓の一覧だけが頼りになる。
    """

    def __init__(self, windows=None, available=True, server_up=True, listed=()):
        super().__init__(windows, available)
        self.server_up = server_up
        self.listed = list(listed)

    def pid(self, name):
        return None

    def list(self, suffix=None):
        return list(self.listed)

    def server_running(self):
        return self.server_up


def _set_task_field(sandbox, key: str, value: str) -> None:
    """frontmatter の 1 行を書き換える (plan.sh を通さない外部変更の模擬)。"""
    lines = sandbox.task_file.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            lines[i] = f"{key}: {value}"
            break
    else:
        raise AssertionError(f"frontmatter に {key}: が無い")
    sandbox.task_file.write_text("\n".join(lines) + "\n")


def _wait_gone(pid: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} が終了しない")


def _phase(sandbox) -> str:
    import lib_retirement
    prog = lib_retirement.read_json(
        lib_retirement.progress_path(sandbox.registry, AGENT))
    return (prog or {}).get("phase", "<no progress>")


def test_red_transient_list_failure_does_not_reset_a_live_worker(sandbox):
    """window list が一度空を返しただけで、生きた Worker の task を pending に戻さない。

    RED (QA FAIL-1 / Codex P1-3): `_start()` は `self._window_alive(target)` —
    すなわち `mux.list()` の結果 — だけで `PHASE_TERMINATED(window_gone=True)`
    を書き、次 cycle の `_settle_terminated()` が `plan.sh update --reset` を
    実行する。Worker は生きたまま作業を続けるので、pending に戻った同じ task を
    別の Worker が pull できてしまう (二重作業)。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FlappingMux({WINDOW: pane_pid})
    # 猶予期間は潰さない: エスカレーション自体は正当なので、ここで見たいのは
    # 「最初の 1 手で終端へ飛ぶか」だけ。
    ex = make_executor(sandbox, mux, grace_period=3600, kill_delay=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    for _ in range(2):
        ex.process_all()

    assert _pid_alive(pane_pid), "生きている Worker を死んだことにした"
    assert sandbox.task_status() == "in_progress", (
        f"生きた Worker の task が pending に戻された (logs={sandbox.logs})")
    assert sandbox.assignment_file.exists(), "生きた Worker の assignment を消した"
    assert _phase(sandbox) != lib_retirement.PHASE_TERMINATED, (
        "list() が空を返しただけで「終了済み」と記録した")


def test_red_cleanup_does_not_reopen_work_finished_during_the_grace_period(sandbox):
    """猶予期間中に Worker が仕事を終えていたら、その成果を pending に巻き戻さない。

    RED (Codex P1-1): `_settle_terminated()` は無条件に
    `plan.sh update --status pending --reset` を撃つ。shutdown メッセージを
    受け取った Worker が最後の `plan.sh done` を済ませてから抜けた場合、
    完了済みの task が pending に戻り、同じ仕事がもう一度配られる。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    ex.process_all()  # → notified

    # 猶予期間中に Worker が task を終わらせて自分で抜けた (plan.sh done 相当)
    _set_task_field(sandbox, "status", "done")
    sandbox.assignment_file.unlink()
    os.kill(pane_pid, signal.SIGKILL)
    _wait_gone(pane_pid)

    for _ in range(6):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert sandbox.task_status() == "done", (
        f"完了済みの task を pending に戻した (logs={sandbox.logs})")
    assert not ex.has_marker(AGENT), "settle できず marker が残り続けている"
    # 「たまたま通った」を弾く: 前提チェックを踏んだことまで見る
    assert any("no queue cleanup owed" in line for line in sandbox.logs), sandbox.logs
    assert sandbox.notes and "既に別の状態" in sandbox.notes[-1], sandbox.notes


def test_red_cleanup_does_not_clear_a_successor_assignment(sandbox):
    """遅れて走った後始末が、後任 Worker の assignment を消さない。

    RED (Codex P1-1): 人間が `--reset` して別 Worker に振り直した後に
    cleanup が走ると、plan.sh は「同じ task の別の実行」を区別できないため
    後任の assignment まで消える。plan.sh 側の content 一致チェックは
    「別 task の assignment」しか守らない。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux)

    # Worker は既に殺し終えていて、後始末だけが残っている状態
    lib_retirement.write_json_atomic(
        lib_retirement.progress_path(sandbox.registry, AGENT),
        lib_retirement.build_progress(
            None, lib_retirement.PHASE_TERMINATED,
            window_gone=True, pane_pid=pane_pid,
            mission=SLUG, task_id=TASK_ID, reason="timeout"))

    # その間に人間が reset → 後任 Worker が同じ task を pull した
    _set_task_field(sandbox, "worker", "Successor")
    sandbox.assignment_file.unlink(missing_ok=True)
    successor_assignment = sandbox.queue / "assignments" / "Successor"
    successor_assignment.write_text(f"{SLUG}:{TASK_ID}")

    ex.process_all()

    assert sandbox.task_worker() == "Successor", (
        f"後任の worker を消した (logs={sandbox.logs})")
    assert sandbox.task_status() == "in_progress", "後任が作業中の task を pending に戻した"
    assert successor_assignment.exists(), "後任の assignment を消した"
    assert any("no queue cleanup owed" in line for line in sandbox.logs), sandbox.logs
    assert not ex.has_marker(AGENT), "settle できず marker が残り続けている"


def test_red_failed_sigkill_does_not_declare_the_worker_terminated(sandbox):
    """SIGKILL が届かなかったら「終了した」と記録しない。

    RED (Codex P1-2): `_step_sigterm()` は `kill_process()` の戻り値を捨てて
    即 `PHASE_TERMINATED` を永続化する。`os.kill` が失敗しても次 cycle で
    task が pending に戻り marker も消えるが、Worker は生きたままになる。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux, kill_process=lambda pid, sig: False)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    for _ in range(4):  # start → notified → sigterm_sent → sigkill(失敗)
        ex.process_all()

    assert _pid_alive(pane_pid), "シグナルは届いていないのに Worker が消えた"
    assert sandbox.task_status() == "in_progress", (
        f"Worker が生きたまま task が pending に戻された (logs={sandbox.logs})")
    assert _phase(sandbox) == lib_retirement.PHASE_SIGTERM_SENT, (
        "届かなかったシグナルを「終了」として記録した")


def test_red_sigkill_waits_for_the_process_to_actually_exit(sandbox, monkeypatch):
    """シグナル送信の成功は即死を意味しない。死を確認するまで終端に進まない。

    RED (Codex P1-2): `kill_process()` が True を返しさえすれば、プロセスが
    まだ走っていても `PHASE_TERMINATED` が書かれ、次 cycle で task が
    pending に戻る。

    後半で「本当に死んだら収束する」ことも確かめる — 確認を足したせいで
    永久に終わらない経路を作っていないことの担保。
    """
    import lib_retirement

    monkeypatch.setattr(lib_retirement, "KILL_CONFIRM_WINDOW", 0.0, raising=False)

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    # 「送信は成功したがプロセスは死なない」— D 状態や権限違いの模擬
    ex = make_executor(sandbox, mux, kill_process=lambda pid, sig: True)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    for _ in range(4):
        ex.process_all()

    assert _pid_alive(pane_pid)
    assert sandbox.task_status() == "in_progress", (
        f"まだ動いている Worker の task を pending に戻した (logs={sandbox.logs})")
    assert _phase(sandbox) == lib_retirement.PHASE_SIGTERM_SENT

    # 本当に死ねば、同じ marker がそのまま後始末まで進む
    os.kill(pane_pid, signal.SIGKILL)
    _wait_gone(pane_pid)
    for _ in range(6):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert not ex.has_marker(AGENT), "死を確認したのに settle しない (居座り経路)"
    assert sandbox.task_status() == "pending"
    assert not sandbox.assignment_file.exists()


def test_repeated_sigkill_failure_is_reported_once(sandbox, monkeypatch):
    """死なない Worker は黙って諦めず、Director に 1 度だけ報告する。

    「判断が付かないから殺さない」に倒した結果が沈黙だと、Worker が居座った
    ことに誰も気付けない。再試行は続けつつ、通知は 1 回に抑える。
    """
    import lib_retirement

    monkeypatch.setattr(lib_retirement, "KILL_CONFIRM_WINDOW", 0.0, raising=False)

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux, kill_process=lambda pid, sig: False)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    for _ in range(10):
        ex.process_all()

    assert _pid_alive(pane_pid)
    assert sandbox.task_status() == "in_progress"
    assert len(sandbox.notes) == 1, f"Director への通知が重複/欠落: {sandbox.notes}"
    assert "SIGKILL" in sandbox.notes[0]


def test_empty_listing_with_a_live_backend_does_not_authorise_cleanup(sandbox):
    """pane_pid を記録できていない request では、空の window list を証拠にしない。

    backend が生きているのに一覧だけ空 = outage と全滅の区別が付かない状態。
    ここで後始末に進むと FAIL-1 と同じ事故が pid なし経路で再発する。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)  # created_at だけが identity
    mux = NoPidMux({WINDOW: pane_pid}, server_up=True, listed=[])
    ex = make_executor(sandbox, mux, grace_period=3600, kill_delay=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    for _ in range(2):
        ex.process_all()

    assert sandbox.task_status() == "in_progress", (
        f"証拠が無いのに後始末した (logs={sandbox.logs})")
    assert ex.has_marker(AGENT), "判断保留のはずなのに marker を捨てた"

    # 一覧が回復すれば通常どおり進む (保留は一時的で、行き止まりではない)
    mux.listed = [WINDOW]
    ex.process_all()
    assert _phase(sandbox) == "notified"


def test_dead_recorded_pid_with_a_live_window_is_a_successor_not_a_death(sandbox):
    """記録した pid が死んでいても、窓が生きている間は後始末に進まない。

    同じ窓名を名乗る後任が既に立っている状態。ここで「元の Worker は死んだ
    → task を pending に戻す」と読むと、後任が作業中の task を取り上げる
    (`knowledge/daemon-authority.md` §5-2)。窓が見えている間は identity
    guard が最終判断を持つ — 証拠の強さで pid を優先するのは、窓が見えない
    ときに限る。

    (t019 の最初の実装はここを踏み抜き、`scripts/test_retirement_authority.sh`
    の case4 が捕まえた。)
    """
    import lib_retirement

    old_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, old_pid)
    mux = FakeMux({WINDOW: old_pid})
    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    # 元の Worker が死に、同じ窓名で後任が立った
    os.kill(old_pid, signal.SIGKILL)
    _wait_gone(old_pid)
    new_pid = sandbox.spawn_worker_process()
    mux.windows[WINDOW] = new_pid

    ex.process_all()

    assert _pid_alive(new_pid), "後任を殺した"
    assert sandbox.task_status() == "in_progress", (
        f"後任が持っているかもしれない task を pending に戻した (logs={sandbox.logs})")
    assert _phase(sandbox) == lib_retirement.PHASE_DISCARDED
    assert any("NOT retiring" in line for line in sandbox.logs), sandbox.logs


def test_window_gone_is_concluded_when_the_backend_itself_is_down(sandbox):
    """backend ごと落ちているなら、空の一覧は outage ではなく全滅の証拠。

    「曖昧なら待つ」を無条件にすると、最後の Worker が死んだ瞬間に marker が
    永久に残り、このモジュールが潰そうとしている幽霊 task が再発する。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = NoPidMux({WINDOW: pane_pid}, server_up=False, listed=[])
    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    for _ in range(4):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert not ex.has_marker(AGENT), "全滅が確定しているのに marker が残った"
    assert sandbox.task_status() == "pending", "幽霊 task が残った"
    assert not sandbox.assignment_file.exists()


# ---------------------------------------------------------------------------
# 既存ガードの回帰
# ---------------------------------------------------------------------------

def test_worker_being_retired_is_not_re_monitored(sandbox):
    """終了処理中の Worker に monitor を作り直さない。

    task は後始末が終わるまで in_progress のままなので、monitor を毎 cycle
    作り直すと `started_at=now` で即 terminate 判定が再成立し、同じ終了に
    対して「TERMINATE」の宣言と Taskvia alert が cycle ごとに出る
    (e2e 実測で 1 回の終了に対し 12 回)。marker が既に意図の記録なので、
    marker がある間はそもそも監視対象から外す。
    """
    import lib_retirement

    import lib_retirement as _lr

    ex = make_executor(sandbox, FakeMux({}))
    card = {"worker": AGENT, "timeout": {"idle": 1, "max": 1}}
    W = watchdog.KILL_AUTHORITY_WATCHDOG

    assert watchdog.should_monitor(card, ex, W) is True, "marker が無いのに監視を外した"

    marker = _lr.request_path(sandbox.registry, AGENT)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")
    assert watchdog.should_monitor(card, ex, W) is False, (
        "retirement 中の Worker が監視対象に戻っている"
    )

    # ロールバック経路は旧挙動のまま — marker を消費しないので、監視を
    # 外すと誰もその Worker を止めなくなる。
    assert watchdog.should_monitor(card, ex, watchdog.KILL_AUTHORITY_DISPATCHER) is True
    # worker 名が未記録の task は判断材料が無い → 監視は続ける
    assert watchdog.should_monitor({"worker": None}, ex, W) is True


def test_mass_kill_guard_conditions_unchanged():
    """`_is_mass_kill()` の発火条件は t002 で変えていない。

    「全 monitor が kill」かつ「backend が使えない or 窓が 1 つも無い」の
    ときだけ True。片方だけでは False のまま。
    """
    all_kill = {("m", "t1"): "kill", ("m", "t2"): "kill"}
    mixed = {("m", "t1"): "kill", ("m", "t2"): "alive"}

    assert watchdog._is_mass_kill(all_kill, mux_available=False, mux_list_empty=False) is True
    assert watchdog._is_mass_kill(all_kill, mux_available=True, mux_list_empty=True) is True
    assert watchdog._is_mass_kill(all_kill, mux_available=True, mux_list_empty=False) is False
    assert watchdog._is_mass_kill(mixed, mux_available=False, mux_list_empty=True) is False
    assert watchdog._is_mass_kill({}, mux_available=False, mux_list_empty=True) is False


# ---------------------------------------------------------------------------
# t018 backlog の小修正
# ---------------------------------------------------------------------------

class _FakeMonitor:
    def __init__(self, agent="A", task_id="t1"):
        self.agent_name = agent
        self.task_id = task_id
        self.idle_threshold = 300
        self.max_threshold = 3600


def _detail(verdict, reason):
    return watchdog.CheckResult(
        verdict=verdict, reason=reason, idle_seconds=1.0,
        process_signal="idle_process", awaiting_human=False,
    )


def test_verdict_logger_forget_drops_state():
    """(a) monitors から外した Worker の判定状態も捨てる。

    残したままだと常駐で微増し、--reset 後に同じ agent/task が再監視された
    とき、最初の判定が古い判定と同値なら最大 10 cycle ログに出ない。
    """
    logger = watchdog.VerdictLogger()
    monitor = _FakeMonitor()
    logger.record(monitor, _detail("alive", "fresh"))
    assert logger._state
    logger.forget(monitor)
    assert logger._state == {}


def test_verdict_logger_logs_reason_change_within_same_verdict(monkeypatch):
    """(b) verdict が同じでも reason が変われば即 1 行出す。

    warn/soft_idle → warn/hard_idle_but_executing は「hard しきい値を越えた
    が実行中なので見逃している」への遷移で、5 分待たされてよい情報ではない。
    """
    lines = []
    monkeypatch.setattr(watchdog, "_log", lines.append)
    logger = watchdog.VerdictLogger()
    monitor = _FakeMonitor()

    logger.record(monitor, _detail("warn", "soft_idle"))
    logger.record(monitor, _detail("warn", "soft_idle"))
    assert len(lines) == 1, lines

    logger.record(monitor, _detail("warn", "hard_idle_but_executing"))
    assert len(lines) == 2, lines
    assert "hard_idle_but_executing" in lines[-1]
