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

t025 — Codex 4 巡目 P1 2 件 + P2 1 件 (退役処理と「割り当て・並行書き込み」の
相互作用。identity も証拠の強度も通ったうえで、**猶予期間のあいだに外の世界が
動く**ことで壊れる):
  test_red_worker_that_pulled_another_task_during_the_grace_period_is_spared (P1-1)
  test_red_worker_that_finished_its_task_during_the_grace_period_is_spared   (P1-1)
  test_red_same_card_pulled_again_by_a_successor_is_a_different_execution    (P1-1)
  test_red_concurrent_requests_do_not_overwrite_each_other                   (P1-2)
  test_red_progress_is_bound_to_the_request_that_created_it                  (P1-2)
  test_red_plan_sh_retire_can_refuse_to_wait_for_the_queue_lock              (P2)
  test_red_cleanup_does_not_block_the_cycle_on_the_queue_lock                (P2)
  test_timeout_retirement_on_the_same_execution_still_terminates
    ↑ 逆向きの担保: 上の 3 本は全部「前提が外れたら殺さない」なので、これが
      無いと「何も殺さない」に倒しただけで全部緑になる。
  (dispatcher 側 — 退役中 Worker を割り当て対象から外す — は
   tests/test_dispatcher_retirement_exclusion.py。dispatcher.sh の heredoc
   python を exec() して本物の dispatch() を回している)

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
    ↑ 最後の 1 本は逆向きの担保: 後任を巻き込まないこと。
  (plan.sh 側の前提条件は tests/plan-assignment-identity.bats の retire 節)

t020 — Codex P1 3 件 (identity 束縛と証拠の強度。いずれも「名前は同じでも
中身が別物になりうる」「観測できないことは証拠にならない」が根):
  test_red_cleanup_does_not_reset_a_same_named_successors_execution  (P1-1)
  test_red_sigterm_goes_to_the_pid_that_passed_the_identity_check    (P1-3)
  test_red_first_step_records_the_pid_it_verified                    (P1-3 同型)
  test_red_failed_server_probe_is_not_proof_of_death                 (P1-2)
  test_unresolvable_retirement_is_escalated_to_the_director_once
    ↑ 逆向きの担保: P1-2 で増えた「保留」に出口があること。出口は自動 cleanup
      ではなく Director への 1 度きりの報告 (t019 が server_running() を証拠に
      使った動機はここにあった)。
  (plan.sh 側の世代チェックは tests/plan-assignment-identity.bats の retire 節)

t024 — Codex 3 巡目の残り 2 件。どちらも「証拠が無いこと」を「証拠がある」に
読み替えて破壊的な一手に進む、という 9 件に共通の形の最後の 2 つ:
  test_red_orphan_recovery_without_a_recorded_pid_does_not_assume_a_death
  test_red_cleanup_without_a_recorded_generation_is_escalated_not_guessed
  test_cleanup_is_bound_to_the_execution_by_a_single_plan_sh_call
    ↑ 逆向きの担保: 後始末が `plan.sh retire` 1 呼び出しで、渡すのが証拠だけで
      あることを引数で固定する。サイトごとの個別ガードが復活すると落ちる。
  (対応表と設計は knowledge/daemon-authority.md §6-5)

t026 — Codex 5 巡目 P1 2 件。t025 で入れた対策の当てが甘かった 2 箇所で、
どちらも「証明できていないものを証明できたことにして進む」という同じ形:
  test_red_missing_assignment_identity_sidecar_does_not_authorise_a_kill  (P1-1)
  test_red_missing_recorded_generation_does_not_authorise_a_kill          (P1-1)
  test_unprovable_retirement_is_escalated_once_and_can_be_released
    ↑ 逆向きの担保: hold に倒す経路が増えたので、「二度と終了できない Worker」
      を作っていないこと (報告 1 通 + marker を消せば次の退役は成立する)。
  test_unprovable_hold_releases_itself_when_the_worker_lets_go_of_the_card
    ↑ 同上。人間を呼ばずに済む曖昧さ (Worker が自分で assignment を手放した)
      は自動で解けること。
  test_red_pull_is_refused_while_a_retirement_is_in_flight                (P1-2)
  test_red_worker_that_pulls_inside_the_guard_is_not_signalled_on_the_new_task
                                                                          (P1-2)
  test_pull_is_allowed_again_once_the_retirement_marker_is_cleared
    ↑ 逆向きの担保: 「常に断る」に倒しただけでは緑にならないこと。
  (設計は knowledge/daemon-authority.md §6-7)

t033 — Codex 6 巡目 P1 / P2。どちらも t026 で入れた対策の「片側しか閉じて
いない」箇所:
  test_red_marker_is_not_created_while_a_pull_transaction_is_open        (P1)
    ↑ pull は予約をキューロックの中で見るのに、marker の作成はロックの外
      だった。予約チェックを通過した pull が走査で止まっている隙に marker が
      立つと、その pull が公開する assignment は退役の管轄外になる。
  test_request_is_only_delayed_by_a_live_pull_not_refused_forever
    ↑ 逆向きの担保: 直列化は「待たせる」だけで、退役が二度と始まらない
      Worker を作らないこと。
  test_red_unprovable_hold_retries_its_report_until_the_director_hears_it (P2)
    ↑ 通知 1 回の失敗で永久に隔離されないこと。
  test_red_deferred_cleanup_retries_its_report_until_the_director_hears_it
    ↑ 同型。世代が無くて後始末を保留した側にも同じ穴があった。
  (設計は knowledge/daemon-authority.md §6-8)

実行方法:
  python3 -m pytest tests/test_retirement.py -v
"""

import contextlib
import errno
import fcntl
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

    @property
    def assignment_identity_file(self) -> Path:
        return self.queue / "assignments" / (AGENT + ".identity")

    def publish_assignment(self, started_at: str) -> None:
        """`plan.sh pull` が公開するもの一式 — 本体 + 実行アイデンティティ。

        サイドカーを省くと `classify_assignment()` は「世代を証明できない」に
        倒れ、後始末は (正しく) 何もしない。本番の pull は必ず両方書くので、
        片方だけ置いた fixture は本番より弱い状態を試していることになる。
        """
        self.assignment_file.parent.mkdir(parents=True, exist_ok=True)
        self.assignment_file.write_text(f"{SLUG}:{TASK_ID}\n")
        self.assignment_identity_file.write_text(json.dumps({
            "mission": SLUG,
            "task": TASK_ID,
            "worker": AGENT,
            "started_at": started_at,
        }, ensure_ascii=False, sort_keys=True) + "\n")

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

    def task_started_at(self) -> str:
        for line in self.task_file.read_text().splitlines():
            if line.startswith("started_at:"):
                return line.split(":", 1)[1].strip().strip('"')
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
    # plan.sh は依存規則 (lib_dep_rules.py) を自分の側の scripts/ から読む。
    shutil.copy2(REPO / "scripts" / "lib_dep_rules.py",
                 root / "scripts" / "lib_dep_rules.py")

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
    sb.publish_assignment("2026-09-21T00:00:00Z")

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


def _set_progress_field(sandbox, key: str, value) -> None:
    """progress marker の 1 フィールドを外から書き換える (破損・手編集の模擬)。"""
    import lib_retirement
    path = lib_retirement.progress_path(sandbox.registry, AGENT)
    doc = lib_retirement.read_json(path)
    assert doc is not None, "progress marker が無い"
    doc[key] = value
    assert lib_retirement.write_json_atomic(path, doc)


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

    # Worker は既に殺し終えていて、後始末だけが残っている状態。世代は記録済み —
    # ここで見たいのは「世代を持っていてなお後任を巻き込まないか」なので、
    # 世代を落とすと後始末が保留に倒れてしまい、肝心の経路を通らない。
    original_started_at = sandbox.task_started_at()
    lib_retirement.write_json_atomic(
        lib_retirement.progress_path(sandbox.registry, AGENT),
        lib_retirement.build_progress(
            None, lib_retirement.PHASE_TERMINATED,
            window_gone=True, pane_pid=pane_pid,
            mission=SLUG, task_id=TASK_ID, reason="timeout",
            task_started_at=original_started_at))

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


# ---------------------------------------------------------------------------
# t020 — Codex P1 3 件。いずれも「名前は同じでも中身が別物になりうる」
# 「観測できないことは証拠にならない」の別の顔。
#
# P1-1 と P1-3 は TOCTOU なので、テストは**検査と使用の間に後任が現れる**状況を
# 実際に作る。フラグや phase 名ではなく、誤発火したときに失われるもの —
# 後任 Worker のプロセスと、後任が作業中の task — を assert する。
# ---------------------------------------------------------------------------


class SuccessorRaceMux(FakeMux):
    """`pid()` の **2 回目** で同名の後任に入れ替わる backend。

    窓名は使い回されるので、元の Worker が抜けた直後に同じ名前で後任が立つのは
    本番の通常動作 (`knowledge/daemon-authority.md` §5-2)。identity check と
    シグナル送信がそれぞれ独立に名前解決すると、その隙間がまるごと事故になる。
    `arm()` を呼んだ時点から数え直すので、「どのステップの中で後任が現れたか」を
    テスト側が正確に決められる。
    """

    def __init__(self, windows, successor_pid):
        super().__init__(windows)
        self.successor_pid = successor_pid
        self.armed = False
        self.pid_calls = 0

    def arm(self):
        self.armed = True
        self.pid_calls = 0

    def pid(self, name):
        if not self.armed:
            return super().pid(name)
        self.pid_calls += 1
        if self.pid_calls >= 2:
            self.windows[name] = self.successor_pid
        return super().pid(name)


def test_red_cleanup_does_not_reset_a_same_named_successors_execution(sandbox):
    """同名の後任が同じ task を実行中なら、遅れて走った後始末は何も書き換えない。

    RED (Codex P1-1): 当時の前提は `--expect-status in_progress` と
    `--expect-worker <agent>` の 2 つだけ (t024 で `plan.sh retire` に集約)。
    これは人間が `--reset` して**同じ名前の Worker**が同じ task を pull し直した
    後にちょうど両方とも成立する (crewvia は Worker 名を意図的に使い回す)。
    後任が作業中の task が pending に戻り、assignment も消える。区別が付くのは
    `started_at` — pull のたびに書き換わる「割り当ての世代」だけ。
    """
    first_started_at = sandbox.task_started_at()
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    ex.process_all()                      # → notified
    os.kill(pane_pid, signal.SIGKILL)     # Worker は shutdown を受けて抜けた
    _wait_gone(pane_pid)
    ex.process_all()                      # → terminated (後始末だけが残る)

    # その間に人間が reset し、**同名の** Worker が同じ task を pull し直した。
    # status も worker も元と寸分違わない状態に戻っている。
    second_started_at = '"2026-09-22T09:00:00Z"'
    assert second_started_at.strip('"') != first_started_at
    _set_task_field(sandbox, "started_at", second_started_at)
    sandbox.publish_assignment(second_started_at.strip('"'))

    ex.process_all()                      # 後始末が走る cycle

    assert sandbox.task_status() == "in_progress", (
        f"後任が作業中の task を pending に戻した (logs={sandbox.logs})")
    assert sandbox.task_worker() == AGENT
    assert sandbox.task_started_at() == second_started_at.strip('"'), (
        "後任の割り当てごと巻き戻した")
    assert sandbox.assignment_file.exists(), "後任の assignment を消した"
    assert any("no queue cleanup owed" in line for line in sandbox.logs), sandbox.logs
    assert not ex.has_marker(AGENT), "settle できず marker が残り続けている"


def test_red_sigterm_goes_to_the_pid_that_passed_the_identity_check(sandbox):
    """identity check を通した PID にだけシグナルを送る。

    RED (Codex P1-3): `_step_notified()` は `_guard()` で現在の PID を検証した
    あと、**もう一度** `mux.pid(target)` を呼んでその結果に SIGTERM を送っていた。
    2 つの呼び出しの間に同名の後任が現れると、request と一度も照合されていない
    PID が marker に永続化され、そのまま殺される。
    """
    import lib_retirement

    old_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, old_pid)
    successor_pid = sandbox.spawn_worker_process()
    mux = SuccessorRaceMux({WINDOW: old_pid}, successor_pid)

    killed = []
    ex = make_executor(
        sandbox, mux,
        kill_process=lambda pid, sig: (killed.append((pid, sig)), True)[1])
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    ex.process_all()
    assert _phase(sandbox) == lib_retirement.PHASE_NOTIFIED

    mux.arm()          # この cycle の中で後任が現れる
    ex.process_all()   # → sigterm_sent

    assert killed == [(old_pid, signal.SIGTERM)], (
        f"検証していない PID にシグナルを送った (killed={killed})")
    assert _pid_alive(successor_pid), "同名の後任を殺した"
    prog = lib_retirement.read_json(
        lib_retirement.progress_path(sandbox.registry, AGENT))
    assert prog["pane_pid"] == old_pid, "後任の PID を marker に書き込んだ"
    assert mux.pid_calls == 1, (
        "窓名を解決し直している — 名前は使い回されるので、検証と使用の間に"
        "別のインスタンスが入り込む余地が残る")


def test_red_first_step_records_the_pid_it_verified(sandbox):
    """最初の一手でも、marker に書く PID は identity check を通したものにする。

    RED (Codex P1-3 の同型): `_start()` も `_guard()` の後に
    `current_spawn_identity()` を呼び直していた。ここで後任の PID を書くと、
    以降のステップは全部その PID を正当な対象として扱う。
    """
    import lib_retirement

    old_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, old_pid)
    successor_pid = sandbox.spawn_worker_process()
    mux = SuccessorRaceMux({WINDOW: old_pid}, successor_pid)

    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    mux.arm()
    ex.process_all()   # → notified

    prog = lib_retirement.read_json(
        lib_retirement.progress_path(sandbox.registry, AGENT))
    assert prog["pane_pid"] == old_pid, "後任の PID を marker に書き込んだ"
    assert mux.pid_calls == 1


def test_red_failed_server_probe_is_not_proof_of_death(sandbox):
    """backend の probe が黙ったからといって、Worker が死んだことにはしない。

    RED (Codex P1-2): `_listing_is_authoritative()` は
    `server_running() == False` を「裏の取れた死」として扱っていた。しかし
    `TmuxBackend.server_running()` はタイムアウトでも例外でも False を返すし、
    HerdrBackend のそれは socket への ping であってプロセスの終了証明ではない。
    一覧と probe を同時に巻き込む outage が起きれば、生きている Worker の task を
    pending に戻す権限が出てしまう — 一覧ゲートが塞いだはずの欠陥が 1 段下で
    再発する (memory: fail-closed-guard-can-recreate-the-defect)。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)   # identity は created_at のみ
    # pid も一覧も probe も同時に answers を失った状態。Worker は生きている。
    mux = NoPidMux({WINDOW: pane_pid}, server_up=False, listed=[])
    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    for _ in range(4):
        ex.process_all()

    assert _pid_alive(pane_pid), "生きている Worker を殺した"
    assert sandbox.task_status() == "in_progress", (
        f"probe の沈黙を死亡証明にして task を pending に戻した (logs={sandbox.logs})")
    assert sandbox.assignment_file.exists(), "生きている Worker の assignment を消した"
    assert ex.has_marker(AGENT), "証拠が無いまま marker を捨てた"


def test_unresolvable_retirement_is_escalated_to_the_director_once(sandbox):
    """保留には必ず出口がある。自動で倒す先は常に「殺さない・書き換えない」側。

    逆向きの担保。P1-2 の修正で「曖昧なら待つ」経路が増えた以上、永久に終わらない
    marker を作っていないことを示す必要がある — t019 が `server_running()` を
    証拠に使った動機はそこにあった。ただしその出口は自動 cleanup ではなく
    Director への 1 度きりの報告。再起動を繰り返しても報告が増えないことまで見る。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = NoPidMux({WINDOW: pane_pid}, server_up=False, listed=[])

    clock = [time.time()]
    ex = make_executor(sandbox, mux, now=lambda: clock[0])
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    ex.process_all()
    assert not sandbox.notes, "まだ判断保留の範囲内なのに Director を呼んだ"

    clock[0] += lib_retirement.STALL_REPORT_AFTER + 1
    ex.process_all()

    assert len(sandbox.notes) == 1, f"報告が 1 回に収まっていない: {sandbox.notes}"
    note = sandbox.notes[0]
    assert "何も kill せず queue も書き換えていません" in note, note
    assert TASK_ID in note and "--reset" in note, "手作業の手順が書かれていない"

    # 報告は報告であって後始末ではない
    assert sandbox.task_status() == "in_progress"
    assert _pid_alive(pane_pid)

    # デーモンを何度回しても、再起動しても、2 通目は出ない
    clock[0] += lib_retirement.STALL_REPORT_AFTER * 3
    ex.process_all()
    make_executor(sandbox, mux, now=lambda: clock[0]).process_all()
    assert len(sandbox.notes) == 1, f"同じ marker で繰り返し報告した: {sandbox.notes}"

    # 報告を受けた人間が marker を片付けたら、受領証も残らない。
    # 残すと、次に同じ Worker が詰まったときの報告を黙らせてしまう。
    assert lib_retirement.stall_path(sandbox.registry, AGENT).exists()
    lib_retirement.request_path(sandbox.registry, AGENT).unlink()
    ex.process_all()
    assert not lib_retirement.stall_path(sandbox.registry, AGENT).exists()


# ---------------------------------------------------------------------------
# t024 — Codex 3 巡目の残り 2 件。どちらも「証拠が無いこと」を
# 「証拠がある」に読み替えて後始末を走らせてしまう欠陥。
# ---------------------------------------------------------------------------

def test_red_orphan_recovery_without_a_recorded_pid_does_not_assume_a_death(sandbox):
    """PID を記録できていない marker は、request を失っても死亡扱いにしない。

    RED (Codex 3 巡目 P1-1): `_orphaned()` は `process_alive(prog["pane_pid"])`
    だけを見ていた。`build_progress()` は `pane_pid` を必ず埋める (既定値 None)
    ので、**PID を答えられない backend** — herdr の `pane_process_info` が
    失敗し identity が created_at だけになった状態 — で始まった retirement は
    `process_alive(None) == False` により即 `terminated` に飛ぶ。猶予期間中に
    request が消える / 読めなくなるだけで、Worker が元気に動いていても次 cycle の
    後始末が task を pending に戻す。

    要求されるのは「記録済みの PID」と「積極的な終了の証拠」の両方。どちらも
    無いなら倒す先は保留であって、死亡ではない。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)          # identity は created_at のみ
    mux = NoPidMux({WINDOW: pane_pid}, listed=[WINDOW])  # 窓は見えるが pid は読めない

    clock = [time.time()]
    ex = make_executor(sandbox, mux, grace_period=3600, now=lambda: clock[0])
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    ex.process_all()
    assert _phase(sandbox) == lib_retirement.PHASE_NOTIFIED
    prog = lib_retirement.read_json(
        lib_retirement.progress_path(sandbox.registry, AGENT))
    assert prog["pane_pid"] is None, "この backend では PID は記録できないはず"

    # 猶予期間の途中で request が消える (手で片付けられた / 読めなくなった)。
    lib_retirement.request_path(sandbox.registry, AGENT).unlink()

    for _ in range(3):
        ex.process_all()

    assert _pid_alive(pane_pid), "生きている Worker を殺した"
    assert sandbox.task_status() == "in_progress", (
        f"PID 不在を死亡証明にして task を pending に戻した (logs={sandbox.logs})")
    assert sandbox.task_worker() == AGENT
    assert sandbox.assignment_file.exists(), "生きている Worker の assignment を消した"
    assert _phase(sandbox) != lib_retirement.PHASE_TERMINATED, (
        "証拠が無いまま終端 phase に進んだ — 次 cycle で queue が書き換わる")

    # 保留には出口がある: 自動の後始末ではなく Director への 1 度きりの報告。
    assert not sandbox.notes, "まだ判断保留の範囲内なのに Director を呼んだ"
    clock[0] += lib_retirement.STALL_REPORT_AFTER + 1
    ex.process_all()
    assert len(sandbox.notes) == 1, f"報告が 1 回に収まっていない: {sandbox.notes}"
    assert "何も kill せず queue も書き換えていません" in sandbox.notes[0]


def test_unparseable_recorded_pid_is_not_evidence_of_a_death(sandbox):
    """壊れた pane_pid は「読めない」であって「死んだ」ではない。

    同型の穴が 1 つ隣にある: `process_alive()` は解釈できない値に False を
    返す — 「そのプロセスは動いているか」への答えとしては正しく、「我々の
    Worker は終了したか」への答えとしては P1-1 と同じ間違いになる。
    marker は JSON なので、途中で切れた書き込みや手作業の編集で壊れうる。

    ここでは窓も一覧も生きているので、正しい振る舞いは「pid は当てにせず、
    裏の取れた一覧に従う」= まだ居るので何もしない。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    ex.process_all()                                   # → notified
    _set_progress_field(sandbox, "pane_pid", "corrupt")

    for _ in range(3):
        ex.process_all()

    assert _pid_alive(pane_pid), "生きている Worker を殺した"
    assert sandbox.task_status() == "in_progress", (
        f"壊れた pid を死亡証明にして task を pending に戻した (logs={sandbox.logs})")
    assert sandbox.assignment_file.exists()

    # 判定そのものも固定しておく: 破壊的な一手の直前に pid を問う側は、
    # 「解釈できない」を `process_alive()` の False に潰さず None で受け取る。
    assert lib_retirement.recorded_pid("not-a-pid") is None
    assert lib_retirement.recorded_pid(None) is None
    assert lib_retirement.recorded_pid("4321") == 4321


def test_red_cleanup_without_a_recorded_generation_is_escalated_not_guessed(sandbox):
    """世代を記録できなかった retirement は、前提を弱めてでも実行したりしない。

    RED (Codex 3 巡目 P1-2): request 時に card を読めないと
    `task_started_at` は記録されず、`_settle_terminated()` は
    `--expect-started-at` を**意図的に省いて** status と worker だけで reset して
    いた。その 2 つは、人間が差し戻して同名の後任が pull し直すと元の値に完全に
    戻る (crewvia は Worker 名を使い回す)。つまり世代を読めなかったときだけ、
    世代チェックが防ぐはずだった後任レースがそのまま復活する。

    証拠が無いときに倒す先は「弱めて実行」ではなく「実行を見送って Director に
    上げる」。後から card を読み直して埋めることもできない — そのとき読めるのは
    後任の世代だからである。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux)

    # request の瞬間だけ card が読めない (queue が別 mount / 一時的な I/O 失敗)。
    hidden = sandbox.task_file.parent / "hidden-during-request"
    sandbox.task_file.rename(hidden)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    hidden.rename(sandbox.task_file)

    req = lib_retirement.read_json(lib_retirement.request_path(sandbox.registry, AGENT))
    assert "task_started_at" not in req, "世代が読めていたらこのテストは前提が違う"

    # Worker は shutdown を待たずに自力で抜け、窓も消えた。
    #
    # t026 以降、世代を記録できていない retirement は `_guard()` を通れない
    # (証明できないものを証明できたことにしないのが t026 の修正である) ので、
    # この後始末に到達する道は「Worker が自分で終わっていた」経路だけになった。
    # このテストの主題は**到達のしかた**ではなく、そこから先の「世代が無い
    # まま前提を弱めて reset しないこと」なので、到達だけ書き換えている。
    os.kill(pane_pid, signal.SIGKILL)
    _wait_gone(pane_pid)
    ex.process_all()                      # → terminated (後始末だけが残る)

    # その間に人間が差し戻し、**同名の** Worker が同じ task を pull し直した。
    successor_started_at = "2026-09-22T09:00:00Z"
    _set_task_field(sandbox, "started_at", f'"{successor_started_at}"')
    sandbox.publish_assignment(successor_started_at)

    ex.process_all()                      # 後始末が走るはずだった cycle

    assert sandbox.task_status() == "in_progress", (
        f"世代の証拠が無いまま後任の task を pending に戻した (logs={sandbox.logs})")
    assert sandbox.task_worker() == AGENT
    assert sandbox.task_started_at() == successor_started_at, "後任の割り当てごと巻き戻した"
    assert sandbox.assignment_file.exists(), "後任の assignment を消した"

    # 見送りには出口がある: Director への 1 度きりの報告 + 手順。
    assert len(sandbox.notes) == 1, f"報告が 1 回に収まっていない: {sandbox.notes}"
    note = sandbox.notes[0]
    assert TASK_ID in note and "--reset" in note, f"手作業の手順が書かれていない: {note}"
    assert "registry/retirements" in note, f"marker の片付け方が書かれていない: {note}"

    # 何 cycle 回しても、再起動しても 2 通目は出ない (この曖昧さは自力では解けない)。
    for _ in range(3):
        ex.process_all()
    make_executor(sandbox, mux).process_all()
    assert len(sandbox.notes) == 1, f"同じ marker で繰り返し報告した: {sandbox.notes}"
    assert sandbox.task_status() == "in_progress"


def test_cleanup_is_bound_to_the_execution_by_a_single_plan_sh_call(sandbox):
    """後始末は `plan.sh retire` 1 呼び出し。判定は plan.sh の中 (単一ロック内)。

    逆向きの担保。証拠 (agent / 世代 / mission) を渡して結果を受け取るだけ、
    という形になっていることを引数で固定する。status や worker を個別に指定する
    形に戻ると、「どれを渡すか」の判断が呼び出し側ごとに分かれ、1 つ緩めた場所
    から同じ型の事故が再発する (3 巡で 9 件の P1 がまさにそれだった)。
    """
    started_at = sandbox.task_started_at()
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    calls = []

    def _run(argv, env):
        calls.append(list(argv))
        return 0, ""

    ex = make_executor(sandbox, mux, run_command=_run)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    ex.process_all()
    os.kill(pane_pid, signal.SIGKILL)
    _wait_gone(pane_pid)
    ex.process_all()          # → terminated
    ex.process_all()          # → 後始末

    assert len(calls) == 1, f"後始末が 1 呼び出しに収まっていない: {calls}"
    argv = calls[0]
    assert argv[2] == "retire", f"新 API を通っていない: {argv}"
    assert argv[3] == TASK_ID
    assert argv[4:] == ["--agent", AGENT, "--started-at", started_at,
                        "--mission", SLUG, "--no-wait"], (
        f"証拠以外のものを渡している: {argv}")
    # --no-wait は「何を主張するか」ではなく「待てるかどうか」の指定なので、
    # 前提を緩める種類のフラグではない (t025 / Codex 4 巡目 P2)。
    assert not any(a.startswith("--expect") for a in argv), (
        "サイト側のガードが二重に残っている — どちらが効いているか分からなくなる")


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


# ---------------------------------------------------------------------------
# t025 — Codex 4 巡目。退役処理と「割り当て・並行書き込み」の相互作用。
#
# これまでの 9 件は「誰を殺すか」の束縛 (identity) と「何を証拠とするか」の
# 強度だった。今回はそのどちらも通ったうえで、**退役処理が走っている最中に
# 外の世界が動く**ことで壊れる 3 件。
#
#   1. 猶予期間中に Worker が次の task を pull できてしまう (P1)
#   2. request の生成が原子的でない (P1)
#   3. queue ロック待ちが watchdog の監視ループを止める (P2)
# ---------------------------------------------------------------------------

import multiprocessing  # noqa: E402  (t025 群でのみ使う)


def _plan(sandbox, *args, timeout=60):
    """sandbox の中で本物の plan.sh を回す。"""
    env = dict(os.environ)
    env["CREWVIA_QUEUE"] = str(sandbox.queue)
    env["CREWVIA_REPO_ROOT"] = str(sandbox.root)
    # 本番の Worker は必ずこれを持っている。無いと `plan.sh done` は
    # assignment を撤去しないので、fixture が本番より弱い状態を試してしまう。
    env["AGENT_NAME"] = AGENT
    return subprocess.run(
        ["bash", str(sandbox.scripts / "plan.sh"), *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def _add_pending_task(sandbox, task_id: str) -> None:
    """同じ mission にもう 1 枚 pending の card を置く。"""
    (sandbox.queue / "missions" / SLUG / "tasks" / f"{task_id}.md").write_text(
        f"---\nid: {task_id}\ntitle: next task\nskills: [code]\n"
        f"priority: high\nstatus: pending\nblocked_by: []\ntarget_dir: null\n"
        f"worker: null\nstarted_at: null\ncompleted_at: null\n"
        f"---\n\n## Description\nnext\n\n## Result\n"
    )


def _status_of(sandbox, task_id: str) -> str:
    path = sandbox.queue / "missions" / SLUG / "tasks" / f"{task_id}.md"
    for line in path.read_text().splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "?"


class _reservation_lifted:
    """退役予約を一時的に外し、「退役中の Worker が task を握った」状態を作る。

    t026 で `plan.sh pull` が退役予約を見るようになったので、この状態は正面から
    (本物の pull で) は作れなくなった。それでも watchdog 側の guard は**最後の
    防波堤**として残っていなければならない: 予約が届かない経路は実際にありうる
    — 退役が決まる前に Worker へ届いていた指示、手で publish された assignment、
    別 checkout の古い plan.sh。予約をいったん外して同じ状態を作り、guard 単体
    でもその Worker に手を出さないことを確かめる。
    """

    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.moved: list = []

    def __enter__(self):
        for path in (self.sandbox.registry / "retirements").glob(f"{AGENT}*"):
            hidden = path.parent / (path.name + ".hidden")
            path.rename(hidden)
            self.moved.append((hidden, path))
        assert self.moved, "退役 marker が無い — テストの前提が崩れている"
        return self

    def __exit__(self, *exc):
        for hidden, path in self.moved:
            hidden.rename(path)
        return False


# -- 1. 猶予期間中の pull ----------------------------------------------------

def test_red_worker_that_pulled_another_task_during_the_grace_period_is_spared(sandbox):
    """猶予期間中に次の task を pull した Worker を殺してはならない。

    timeout retirement は task_id を持つので `_guard()` の assignment
    チェックを免除される。免除された結果、見ているのは pane identity だけ
    になる — ところが pane の PID も created_at も**同じ Worker が次の task
    を実行していても変わらない**。だから guard は素通りし、watchdog は
    「別の task を実行中の生きた Worker」に SIGTERM を撃つ。しかも後始末は
    元の task しか見ないので、新しい task は in_progress のまま宙に浮く。

    再現は本物の plan.sh で行う: 猶予期間中に `done` → `pull` を実際に通す。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    _add_pending_task(sandbox, "t002")

    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    assert ex.process_all() == [(AGENT, "notified")]

    # 猶予期間中に Worker が生き返り、自分で片を付けて次を取る。
    # (t026 以降、pull そのものは予約で断られる。ここで試しているのはその後ろの
    #  guard なので、予約が届かなかった場合を `_reservation_lifted` で作る。)
    done = _plan(sandbox, "done", TASK_ID, "grace 中に自力で完了", "--mission", SLUG)
    assert done.returncode == 0, done.stderr
    with _reservation_lifted(sandbox):
        pull = _plan(sandbox, "pull", "--task", "t002", "--mission", SLUG,
                     "--agent", AGENT, "--skills", "code")
    assert pull.returncode == 0, pull.stderr
    assert _status_of(sandbox, "t002") == "in_progress"

    # 猶予期間が切れる。
    ex.now = lambda: time.time() + 7200
    for _ in range(5):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert _pid_alive(pane_pid), "次の task を実行中の Worker を kill した"
    assert _status_of(sandbox, "t002") == "in_progress", "新しい task が宙に浮いた"
    assert sandbox.assignment_file.exists(), "新しい実行の assignment を消した"
    assert _status_of(sandbox, TASK_ID) == "done", "Worker 自身が書いた結末を巻き戻した"


def test_red_worker_that_finished_its_task_during_the_grace_period_is_spared(sandbox):
    """assignment がもう無い = その実行は終わっている。殺す対象が無い。

    上の一段手前。pull まで進んでいなくても、`plan.sh done` を通した時点で
    退役要求の前提 (この Worker はこの task で止まっている) は偽になる。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    ex.process_all()

    done = _plan(sandbox, "done", TASK_ID, "grace 中に自力で完了", "--mission", SLUG)
    assert done.returncode == 0, done.stderr

    ex.now = lambda: time.time() + 7200
    for _ in range(5):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert _pid_alive(pane_pid), "もう手を離した task を理由に Worker を kill した"
    assert _status_of(sandbox, TASK_ID) == "done", "Worker 自身が書いた結末を巻き戻した"


def test_red_same_card_pulled_again_by_a_successor_is_a_different_execution(sandbox):
    """同じ card でも世代が違えば別の実行。差し戻し → 再 pull を巻き込まない。"""
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    ex.process_all()

    # 人間が差し戻し、同名 Worker が同じ card を取り直す (= 新しい世代)。
    reset = _plan(sandbox, "update", TASK_ID, "--status", "pending", "--reset",
                  "--mission", SLUG)
    assert reset.returncode == 0, reset.stderr
    # pull は t026 の予約で断られる。ここで試すのはその後ろの guard なので、
    # 予約が届かなかった場合を作る (`_reservation_lifted` の docstring 参照)。
    with _reservation_lifted(sandbox):
        pull = _plan(sandbox, "pull", "--task", TASK_ID, "--mission", SLUG,
                     "--agent", AGENT, "--skills", "code")
    assert pull.returncode == 0, pull.stderr

    ex.now = lambda: time.time() + 7200
    for _ in range(5):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert _pid_alive(pane_pid), "同じ card の別の実行を巻き込んで kill した"
    assert _status_of(sandbox, TASK_ID) == "in_progress", "後任の実行を pending に戻した"


def test_timeout_retirement_on_the_same_execution_still_terminates(sandbox):
    """逆方向の担保: 本当に同じ実行を握ったままの Worker は従来どおり終了する。

    上の 3 本は全部「前提が外れたら殺さない」なので、これが無いと
    「何も殺さない」に倒しただけでも全部緑になってしまう。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    for _ in range(10):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert not _pid_alive(pane_pid), "同じ実行を握ったままの Worker が終了しなかった"
    assert sandbox.task_status() == "pending"


# -- 2. request の原子性 -----------------------------------------------------

def _request_in_child(root, agent, window, reason, queue, registry, out):
    """別プロセスから request() を 1 回叩く。戻り値を out に積む。"""
    import lib_retirement as lr

    ex = lr.RetirementExecutor(
        registry_dir=registry, repo_root=root,
        mux=FakeMux({window: os.getpid()}),
        repo_identity_check=lambda: True,
        queue_dir=queue,
    )
    out.put((reason, bool(ex.request(agent, window, reason))))


def test_red_concurrent_requests_do_not_overwrite_each_other(sandbox):
    """2 つの書き手を実際に競わせる。勝つのは 1 本だけ。

    `has_marker()` の確認と `request()` の書き込みが分かれており、
    `request()` は無条件に os.replace する。dispatcher と watchdog が同時に
    「marker 無し」を観測すると、後から書いた方が先の request を黙って
    差し替える。
    """
    make_idle(sandbox)
    sandbox.record_identity(WINDOW, os.getpid())

    ctx = multiprocessing.get_context("fork")
    out = ctx.Queue()
    procs = [
        ctx.Process(target=_request_in_child,
                    args=(sandbox.root, AGENT, WINDOW, f"racer-{i}",
                          sandbox.queue, sandbox.registry, out))
        for i in range(8)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    results = [out.get() for _ in range(len(procs))]

    winners = [reason for reason, ok in results if ok]
    assert len(winners) == 1, f"複数の書き手が「書いた」と答えた: {winners}"

    import lib_retirement as lr
    req = lr.read_json(lr.request_path(sandbox.registry, AGENT))
    assert req is not None, "request が 1 本も残っていない"
    assert req["reason"] == winners[0], (
        f"勝者 {winners[0]!r} の request が {req['reason']!r} に差し替えられている")


def test_red_progress_is_bound_to_the_request_that_created_it(sandbox):
    """差し替わった request の証拠を、前の retirement の progress に適用しない。

    watchdog が最初の request で progress を作った後に request が差し替わる
    と、progress は最初の request の PID と task 証拠を持つのに、後続ステップ
    は差し替え側の mission/task/世代を読む。後始末の義務が捨てられるか、
    **別の退役の証拠が適用される**。
    """
    import lib_retirement as lr

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    # t002 は **別の実行** — 同じ Worker 名が今まさに走らせている生きた task。
    _add_pending_task(sandbox, "t002")
    other = sandbox.queue / "missions" / SLUG / "tasks" / "t002.md"
    other.write_text(other.read_text()
                     .replace("status: pending", "status: in_progress")
                     .replace("worker: null", f"worker: {AGENT}")
                     .replace("started_at: null", 'started_at: "2099-01-01T00:00:00Z"'))

    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    ex.process_all()  # → notified。progress は t001 の PID と世代を持つ。

    # 旧コード相当の「無条件 os.replace」で request だけを別物に差し替える。
    replacement = lr.build_request(
        AGENT, WINDOW, "no-task",
        {"pane_pid": pane_pid, "created_at": None},
        mission=SLUG, task_id="t002", task_started_at="2099-01-01T00:00:00Z")
    lr.write_json_atomic(lr.request_path(sandbox.registry, AGENT), replacement)
    sandbox.publish_assignment("2099-01-01T00:00:00Z")  # t002 の assignment
    sandbox.assignment_file.write_text(f"{SLUG}:t002\n")
    sandbox.assignment_identity_file.write_text(json.dumps({
        "mission": SLUG, "task": "t002", "worker": AGENT,
        "started_at": "2099-01-01T00:00:00Z"}, ensure_ascii=False, sort_keys=True) + "\n")

    ex.now = lambda: time.time() + 7200
    for _ in range(6):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break

    assert _status_of(sandbox, "t002") == "in_progress", (
        "差し替えられた request の証拠で、別の実行 (t002) が巻き戻された")
    assert sandbox.assignment_file.exists(), "別の実行の assignment を撤去した"
    assert any("request" in line and "progress" in line for line in sandbox.logs), sandbox.logs


# -- 3. queue ロック待ちで監視ループを止めない -------------------------------

class _HeldLock:
    """queue ロックを別プロセスで握りっぱなしにする。"""

    def __init__(self, queue_dir):
        self.queue_dir = Path(queue_dir)
        self.proc = None

    def __enter__(self):
        script = (
            "import fcntl,sys,time\n"
            "f=open(sys.argv[1],'a+')\n"
            "fcntl.flock(f, fcntl.LOCK_EX)\n"
            "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
            "time.sleep(600)\n"
        )
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", script, str(self.queue_dir / ".lock")],
            stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout.readline().strip() == "locked"
        return self

    def __exit__(self, *exc):
        self.proc.kill()
        self.proc.wait(timeout=5)
        return False


def test_red_plan_sh_retire_can_refuse_to_wait_for_the_queue_lock(sandbox):
    """plan.sh に「待たずに諦める」取得手段があること。"""
    with _HeldLock(sandbox.queue):
        started = time.monotonic()
        res = _plan(sandbox, "retire", TASK_ID, "--agent", AGENT,
                    "--started-at", "2026-09-21T00:00:00Z",
                    "--mission", SLUG, "--no-wait", timeout=30)
        elapsed = time.monotonic() - started

    import lib_retirement as lr

    assert elapsed < 15, f"ロックを待ってしまった ({elapsed:.1f}s)"
    # 呼び出し側の定数で assert する — 片方だけ変えると
    # 「ロック待ち」が「plan.sh の異常」や「もう用は無い」に化ける。
    assert res.returncode == lr.PLAN_LOCK_BUSY, (
        f"rc={res.returncode} — ロック取得失敗は 1 (異常) でも 3 (前提不成立) でもない"
        f"専用の終了コードで返すこと\n{res.stdout}{res.stderr}")
    assert lr.PLAN_LOCK_BUSY not in (0, 1, lr.PLAN_PRECONDITION_UNMET)
    assert _status_of(sandbox, TASK_ID) == "in_progress", "1 バイトも書いてはならない"


def test_red_cleanup_does_not_block_the_cycle_on_the_queue_lock(sandbox):
    """queue が混んでいても 1 cycle は有界。

    `_settle_terminated()` は `_default_run_command()` を同期呼び出ししており、
    本物の plan.sh はブロッキングの排他 flock を使う。marker 1 件につき最大
    120 秒 (subprocess timeout)、全 Worker の監視と退役処理が止まる。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    with _HeldLock(sandbox.queue):
        deadline = time.monotonic() + 60
        settled = False
        while time.monotonic() < deadline:
            started = time.monotonic()
            ex.process_all()
            cycle = time.monotonic() - started
            assert cycle < 20, f"1 cycle が {cycle:.0f}s 止まった (ロック待ち)"
            if not ex.has_marker(AGENT):
                settled = True
                break
        assert not settled, "ロックを取れていないのに後始末を完了扱いにした"
        prog = read_json_file(sandbox, AGENT)
        assert prog.get("phase") == "terminated", (
            f"ロック待ちを後始末の失敗として扱っている: {prog.get('phase')}")

    # ロックが空けば次の cycle で普通に完了する (保留に出口がある)。
    for _ in range(10):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break
    assert not ex.has_marker(AGENT), "ロックが空いても後始末が再開されない"
    assert sandbox.task_status() == "pending"


def read_json_file(sandbox, agent):
    import lib_retirement as lr
    return lr.read_json(lr.progress_path(sandbox.registry, agent)) or {}


# ---------------------------------------------------------------------------
# t026 — Codex 5 巡目 P1 2 件。どちらも「証明できていないものを証明できたことに
# して破壊的な一手に進む」という、この module が 4 巡繰り返してきた形:
#   1. 世代を突き合わせられないケースを EXEC_SAME (= 続行可) に倒していた (P1-1)
#   2. guard の確認とシグナル送信の間に、Worker 自身が次の task を取れた (P1-2)
# ---------------------------------------------------------------------------


def _retirement_files(sandbox):
    return sorted((sandbox.registry / "retirements").glob(f"{AGENT}*"))


def _clear_marker(sandbox) -> None:
    """人間が `registry/retirements/<agent>.*` を消す = 退役予約の解除。"""
    for path in _retirement_files(sandbox):
        path.unlink()


def _drive(ex, cycles: int = 6) -> None:
    for _ in range(cycles):
        ex.process_all()


# -- 1. 世代の証拠が無いときに続行してはならない -----------------------------

def test_red_missing_assignment_identity_sidecar_does_not_authorise_a_kill(sandbox):
    """サイドカーを読めない = 世代を突き合わせられない。続行の根拠にはならない。

    RED (Codex 5 巡目 P1-1): `assignment_execution_verdict()` は
    `<agent>.identity` が無い / 読めないときに `EXEC_SAME` を返していた。
    「assignment の本文が同じ card を指しているのだから」という理由づけだが、
    本文は**同じ card を取り直した後任のものとバイト単位で同一**になる。
    crewvia は Worker 名をポジションとして使い回すので、pane の pid も
    created_at も同じまま。つまりこの fallback は、猶予期間中に reset →
    再 pull が起きたときに、**新しい実行に対する SIGTERM/SIGKILL を許可する**。
    しかも後始末は世代が違うことを理由にその実行の reset を拒むので、task は
    誰の管轄でもないまま in_progress で宙に浮く。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)

    # 旧 plan.sh が公開した assignment、あるいは手で置かれた assignment。
    sandbox.assignment_identity_file.unlink()

    _drive(ex)

    assert _pid_alive(pane_pid), (
        f"世代を証明できないまま Worker を kill した (logs={sandbox.logs})")
    assert sandbox.task_status() == "in_progress", "証拠が無いのに card を書き換えた"
    assert sandbox.assignment_file.exists(), "証拠が無いのに assignment を消した"
    assert ex.has_marker(AGENT), (
        "marker を捨てると次の cycle が同じ判断をやり直すだけで、"
        "退役が永久に成立しないまま誰も気付かない")


def test_red_missing_recorded_generation_does_not_authorise_a_kill(sandbox):
    """記録側に世代が無いときも同じ。「比べる相手が無い」は「一致した」ではない。

    RED (Codex 5 巡目 P1-1 の同型): request の瞬間に card を読めないと
    `task_started_at` は記録されない。その状態で `assignment_execution_verdict()`
    は `EXEC_SAME` ("no recorded generation to compare") を返していたので、
    guard は素通りし、Worker は殺される。証拠が無いまま殺しておいて後始末だけ
    人間に渡す、という順番そのものが逆である。
    """
    import lib_retirement

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    ex = make_executor(sandbox, mux)

    hidden = sandbox.task_file.parent / "hidden-during-request"
    sandbox.task_file.rename(hidden)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    hidden.rename(sandbox.task_file)

    req = lib_retirement.read_json(lib_retirement.request_path(sandbox.registry, AGENT))
    assert "task_started_at" not in req, "世代が読めていたらこのテストは前提が違う"

    _drive(ex)

    assert _pid_alive(pane_pid), (
        f"世代を記録できていないのに Worker を kill した (logs={sandbox.logs})")
    assert sandbox.task_status() == "in_progress"
    assert sandbox.assignment_file.exists()
    assert ex.has_marker(AGENT)


def test_unprovable_retirement_is_escalated_once_and_can_be_released(sandbox):
    """逆向きの担保: 黙って居座る Worker を作らないこと。

    discard に倒す経路を増やすと、「退役要求が永久に取り消され続けて Worker が
    居座る」という別の穴が開く。倒す先は discard (= marker を捨てて次の cycle で
    同じ判断をやり直す) ではなく **hold + Director への 1 度きりの報告** である
    こと、そしてその保留に**人間が開けられる出口**があることを固定する。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    sandbox.assignment_identity_file.unlink()

    _drive(ex, 4)
    make_executor(sandbox, mux).process_all()   # 再起動しても 2 通目は出ない

    assert len(sandbox.notes) == 1, f"報告が 1 回に収まっていない: {sandbox.notes}"
    note = sandbox.notes[0]
    assert AGENT in note
    assert "registry/retirements" in note, f"marker の片付け方が書かれていない: {note}"
    assert "--reset" not in note, (
        f"何も kill していないのに task の差し戻しを促している: {note}")

    # 出口: 人間が marker を消せば、証拠のそろった次の退役はふつうに成立する。
    _clear_marker(sandbox)
    assert not ex.has_marker(AGENT)
    sandbox.publish_assignment(sandbox.task_started_at())

    ex2 = make_executor(sandbox, mux)
    assert ex2.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    for _ in range(10):
        ex2.process_all()
        if not ex2.has_marker(AGENT):
            break
    assert not _pid_alive(pane_pid), (
        f"証拠がそろっても二度と終了できない Worker になっている (logs={sandbox.logs})")
    assert sandbox.task_status() == "pending"


def test_unprovable_hold_releases_itself_when_the_worker_lets_go_of_the_card(sandbox):
    """逆向きの担保その 2: 人間を呼ばずに済む曖昧さは、自分で片付けること。

    hold は「証明できない」から待っているだけなので、assignment が「この Worker
    はもう別のことをしている」と言い切った時点で前提は反証される。世代を読める
    必要はない。ここを自動で解かないと、自力で完了しただけの健全な Worker が
    人間の手作業を待って止まり続ける。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})

    ex = make_executor(sandbox, mux)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    sandbox.assignment_identity_file.unlink()
    _drive(ex, 3)
    assert ex.has_marker(AGENT), "hold になっていない — テストの前提が崩れている"
    assert len(sandbox.notes) == 1

    # Worker は止まっていなかった。自分で片を付ける (= assignment を手放す)。
    done = _plan(sandbox, "done", TASK_ID, "止まっていなかった", "--mission", SLUG)
    assert done.returncode == 0, done.stderr

    _drive(ex, 3)

    assert not ex.has_marker(AGENT), (
        f"曖昧さが解けても Worker が隔離されたまま (logs={sandbox.logs})")
    assert _pid_alive(pane_pid), "解放のついでに kill した"
    assert _status_of(sandbox, TASK_ID) == "done", "Worker 自身が書いた結末を巻き戻した"
    assert len(sandbox.notes) == 1, f"解放を 2 通目の報告にした: {sandbox.notes}"


# -- 2. 退役予約は plan.sh pull の側で効かせる -------------------------------

def test_red_pull_is_refused_while_a_retirement_is_in_flight(sandbox):
    """退役中の Worker は、自分で pull しても新しい task を受け取れない。

    RED (Codex 5 巡目 P1-2): t025 で入れた dispatcher 側の除外は「割り当て
    メッセージを送らない」だけで、**Worker 自身が `plan.sh pull` を叩く経路**も
    既に届いている指示も止められない。assignment を公開するのは plan.sh の
    キューロックの中なので、退役予約もそこで効かせるのが唯一の直列化点である。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    _add_pending_task(sandbox, "t002")

    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    assert ex.process_all() == [(AGENT, "notified")]

    done = _plan(sandbox, "done", TASK_ID, "grace 中に自力で完了", "--mission", SLUG)
    assert done.returncode == 0, done.stderr

    pull = _plan(sandbox, "pull", "--mission", SLUG, "--agent", AGENT, "--skills", "code")
    assert pull.returncode == 2, (
        f"退役中の Worker に task を渡した: rc={pull.returncode}\n{pull.stdout}{pull.stderr}")
    assert _status_of(sandbox, "t002") == "pending", "1 バイトも書かないはずが card を書き換えた"
    assert not sandbox.assignment_file.exists(), "退役中の Worker の assignment を公開した"
    assert "retire" in pull.stderr.lower(), (
        f"断った理由が読み取れない (原因調査ができない): {pull.stderr}")


class HookedMux(FakeMux):
    """`pid()` — guard が必ず通る mux 呼び出し — に割り込み点を作る。

    本番の `current_spawn_identity()` は mux backend の subprocess を叩くので、
    ここは数十〜数百ミリ秒ブロックしうる。その間に Worker が `plan.sh done` →
    `plan.sh pull` を通せる、というのが P1-2 の指摘である。実時間では再現でき
    ない幅なので、**その呼び出しそのもの**を割り込み点として固定する
    (memory: microsecond-race-fix-needs-structural-test)。
    """

    def __init__(self, windows: dict, hook):
        super().__init__(windows)
        self.hook = hook
        self.armed = False
        self.fired = 0

    def pid(self, name):
        if self.armed:
            self.armed = False
            self.fired += 1
            self.hook()
        return super().pid(name)


def test_red_worker_that_pulls_inside_the_guard_is_not_signalled_on_the_new_task(sandbox):
    """guard の途中で Worker が次の task を取っても、その実行にシグナルは飛ばない。

    RED (Codex 5 巡目 P1-2): `_guard()` は assignment を先に読み、そのあとに
    ブロックしうる `current_spawn_identity()` を呼んでいた。その隙に Worker が
    自分で done → pull を通すと、pane の pid は変わらないので R7 は通り、
    **新しい task を実行中の Worker に SIGTERM が飛ぶ**。その実行の後始末は
    古い request の管轄外なので、新しい task は誰の管轄でもないまま宙に浮く。
    """
    outcome = {}

    def worker_takes_the_next_task():
        outcome["done"] = _plan(sandbox, "done", TASK_ID, "guard の最中に完了",
                                "--mission", SLUG)
        outcome["pull"] = _plan(sandbox, "pull", "--task", "t002", "--mission", SLUG,
                                "--agent", AGENT, "--skills", "code")

    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = HookedMux({WINDOW: pane_pid}, worker_takes_the_next_task)
    _add_pending_task(sandbox, "t002")

    signals: list = []
    ex = make_executor(sandbox, mux,
                       kill_process=lambda pid, sig: (signals.append((pid, sig)), True)[1])
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    assert ex.process_all() == [(AGENT, "notified")]

    mux.armed = True          # 次の guard の途中で Worker が動く
    ex.process_all()
    assert mux.fired == 1, "割り込み点を通っていない — テストの前提が崩れている"

    assert outcome["done"].returncode == 0, outcome["done"].stderr
    assert outcome["pull"].returncode != 0, (
        f"退役中の Worker が新しい task を取れた: {outcome['pull'].stdout}")
    assert _status_of(sandbox, "t002") == "pending", "新しい task が宙に浮いた"
    assert signals == [], (
        f"もう手を離した Worker にシグナルを撃った: {signals} (logs={sandbox.logs})")


def test_pull_is_allowed_again_once_the_retirement_marker_is_cleared(sandbox):
    """逆向きの担保: 予約は退役のあいだだけ。marker が無ければ pull は通る。

    これが無いと「常に断る」に倒しただけで上の 2 本が緑になる。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    _add_pending_task(sandbox, "t002")

    ex = make_executor(sandbox, mux, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    assert _plan(sandbox, "done", TASK_ID, "完了", "--mission", SLUG).returncode == 0
    assert _plan(sandbox, "pull", "--task", "t002", "--mission", SLUG,
                 "--agent", AGENT, "--skills", "code").returncode == 2

    _clear_marker(sandbox)
    ok = _plan(sandbox, "pull", "--task", "t002", "--mission", SLUG,
               "--agent", AGENT, "--skills", "code")
    assert ok.returncode == 0, f"予約解除後も pull できない: {ok.stderr}"
    assert _status_of(sandbox, "t002") == "in_progress"
    assert sandbox.assignment_file.exists()


# -- 3. marker の作成そのものを pull と直列化する (t033 / Codex 6 巡目 P1) ---

def _queue_lock_is_held(sandbox) -> bool:
    """キューロックが他のプロセスに握られているか。plan.sh と同じ flock を見る。"""
    fh = open(sandbox.queue / ".lock", "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


@contextlib.contextmanager
def _pull_parked_inside_the_queue_lock(sandbox, *, skills="code"):
    """本物の `plan.sh pull` を、キューロックを握ったまま card 走査の途中で止める。

    止め方は名前付きパイプ。`list_tasks()` は `tNNN.md` を番号順に **全部
    open して read** するので、`t000.md` を FIFO にしておくと pull はそこで
    止まる。止まる位置は退役予約チェックの **後**、assignment 公開の **前** —
    指摘された interleaving の (2) そのものである。

    「止まった」ことは sleep で当て込まない。FIFO は読み手が現れるまで書き手側の
    `O_WRONLY|O_NONBLOCK` open が ENXIO で失敗するので、**その open が成功した
    こと**が「pull はキューロックの中で card を読みに来ている」の証明になる
    (memory: microsecond-race-fix-needs-structural-test)。
    """
    fifo = sandbox.queue / "missions" / SLUG / "tasks" / "t000.md"
    os.mkfifo(fifo)
    env = dict(os.environ)
    env["CREWVIA_QUEUE"] = str(sandbox.queue)
    env["CREWVIA_REPO_ROOT"] = str(sandbox.root)
    env["AGENT_NAME"] = AGENT
    # FIFO の card は **1 回しか読めない**。plan.sh は queue を書き換えたあと、
    # キューロックの外で task-graph を作り直す = card をもう一度読みに行くので、
    # 有効なままだと 2 回目の open が書き手を待って永久に止まる。ここで見たいのは
    # pull のトランザクションの張り方だけなので、付加機能は落としておく。
    env["CREWVIA_TASK_GRAPH"] = "0"
    proc = subprocess.Popen(
        ["bash", str(sandbox.scripts / "plan.sh"), "pull",
         "--mission", SLUG, "--agent", AGENT, "--skills", skills],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    wfd = None
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            wfd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as e:
            if e.errno != errno.ENXIO:
                raise
            if proc.poll() is not None:
                break
            time.sleep(0.005)
    if wfd is None:
        out, err = proc.communicate(timeout=30)
        raise AssertionError(
            f"pull が card 走査まで来ていない (rc={proc.returncode}) — "
            f"テストの前提が崩れている\n{out}{err}")
    # 解放は必ず finally の中で。ここから先で何が失敗しても、キューロックを
    # 握ったままの pull を残すとセッション全体が道連れになる。
    try:
        assert _queue_lock_is_held(sandbox), (
            "pull が card を読みに来ているのにキューロックを握っていない — "
            "トランザクションの張り方が変わっている")
        yield proc
    finally:
        # FIFO に「pull 対象にならない card」を流し込む。pull はそのまま走査を
        # 続け、本来の pending task を掴んでトランザクションを閉じる。
        try:
            os.write(wfd, (
                "---\nid: t000\ntitle: parked\nskills: [code]\n"
                "priority: low\nstatus: done\nblocked_by: []\ntarget_dir: null\n"
                "worker: null\nstarted_at: null\ncompleted_at: null\n"
                "---\n\n## Description\nparked\n\n## Result\n").encode())
        finally:
            os.close(wfd)
        try:
            proc.parked_output = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.parked_output = proc.communicate()
            raise


def test_red_marker_is_not_created_while_a_pull_transaction_is_open(sandbox):
    """pull のトランザクションが開いている間は、退役 marker を作らないこと。

    RED (Codex 6 巡目 P1): t026 は `plan.sh pull` に退役予約を見させたが、見る
    側だけをロックの中に入れた。**`RetirementExecutor.request()` は marker を
    キューロックを取らずに作る**ので、次の並びがそのまま残っている:

      1. pull が `retirement_reservation()` を通過する (marker はまだ無い)
      2. pull が card 走査で止まる           ← ここで FIFO が pull を止める
      3. marker が作られ、`_guard()` が「assignment 無し」を見て idle 退役と判断
      4. shutdown が飛ぶ
      5. pull が assignment を公開する → その実行は退役の管轄外で宙に浮く

    guard の中で mux 参照を前倒ししても閉じない: 読む順番の問題ではなく、
    **決定を書き込む瞬間が pull のトランザクションと直列化されていない**ことが
    原因だからである。直列化点はキューロックしかない。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    _add_pending_task(sandbox, "t002")
    make_idle(sandbox)   # dispatcher が idle と判断する材料をそろえる

    ex = make_executor(sandbox, mux)

    with _pull_parked_inside_the_queue_lock(sandbox) as pull:
        wrote = ex.request(AGENT, WINDOW, "idle")
        actions = ex.process_all()

        assert not wrote and not ex.has_marker(AGENT), (
            f"pull のトランザクションが開いているのに退役 marker を作った "
            f"(actions={actions}, logs={sandbox.logs})")
        assert mux.sent == [], (
            f"これから task を掴む Worker に shutdown を送った: {mux.sent}")

    assert pull.returncode == 0, f"pull が完走していない: {pull.parked_output}"
    # pull が「本当に危険な側の仕事をした」ことの確認。ここが空だと、marker を
    # 作らなかったのは pull が何もしなかったからで、テストが穴を隠している。
    assert _status_of(sandbox, "t002") == "in_progress"
    assert sandbox.assignment_file.exists(), (
        "pull が assignment を公開していない — テストの前提が崩れている")
    assert not ex.has_marker(AGENT), (
        f"task を握った Worker に退役 marker が残っている — この実行は誰の管轄でも"
        f"ない (logs={sandbox.logs})")


def test_request_is_only_delayed_by_a_live_pull_not_refused_forever(sandbox):
    """逆向きの担保: 直列化は「待たせる」だけ。ロックが空けば marker は作れる。

    これが無いと「キューロックを見たら常に諦める」に倒しただけで上が緑になり、
    退役が二度と始まらない Worker — このミッションが潰してきた「黙って居座る」
    側の穴 — ができる。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    _add_pending_task(sandbox, "t002")
    make_idle(sandbox)

    ex = make_executor(sandbox, mux)
    with _pull_parked_inside_the_queue_lock(sandbox):
        assert not ex.request(AGENT, WINDOW, "idle")

    assert ex.request(AGENT, WINDOW, "idle"), (
        f"ロックが空いても退役を要求できない (logs={sandbox.logs})")
    assert ex.has_marker(AGENT)


# -- 4. 届かなかった報告は、届くまで再試行する (t033 / Codex 6 巡目 P2) ------

def _flaky_notifier():
    """「通知先が落ちている / 復旧した」を切り替えられる notify。"""
    state = {"online": False}
    delivered: list = []

    def notify(message):
        if not state["online"]:
            return False
        delivered.append(message)
        return True

    return state, delivered, notify


def test_red_unprovable_hold_retries_its_report_until_the_director_hears_it(sandbox):
    """通知が一時的に落ちただけで、Worker を永久に隔離しないこと。

    RED (Codex 6 巡目 P2): `_unprovable()` は `_report()` が False を返しても
    (= 通知が届かなくても) `PHASE_UNPROVABLE` を永続化する。以降のサイクルは
    `_recheck_unprovable()` しか通らず report を再試行せず、`_check_stall()` は
    この phase の報告を明示的に抑制する。結果、**一時的な通知失敗で Worker が
    永久に隔離され、Director には復旧に必要な情報が届かない**。marker がある
    あいだ pull も dispatcher の割り当ても止まるので、外からは何も起きない。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    state, delivered, notify = _flaky_notifier()

    ex = make_executor(sandbox, mux, notify=notify)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    sandbox.assignment_identity_file.unlink()   # 世代が読めない → hold

    _drive(ex, 4)
    prog = _progress_of(sandbox)
    assert prog.get("phase") == "unprovable", (
        f"hold になっていない — テストの前提が崩れている: {prog}")
    assert delivered == [], "落ちている通知先に届いたことになっている"
    assert not prog.get("director_notified")

    state["online"] = True
    _drive(ex, 3)

    assert len(delivered) == 1, (
        f"通知が復旧しても Director に届かない "
        f"(delivered={delivered}, logs={sandbox.logs})")
    note = delivered[0]
    assert AGENT in note and "registry/retirements" in note, (
        f"隔離の解き方が書かれていない: {note}")
    assert "--reset" not in note, (
        f"何も kill していないのに task の差し戻しを促している: {note}")

    _drive(ex, 3)
    assert len(delivered) == 1, f"届いたあとも送り続けている: {delivered}"

    # 報告は報告でしかない。隔離も Worker も task もそのまま。
    assert ex.has_marker(AGENT), "報告のついでに隔離を解いた"
    assert _pid_alive(pane_pid), "報告のついでに kill した"
    assert sandbox.task_status() == "in_progress"


def test_red_deferred_cleanup_retries_its_report_until_the_director_hears_it(sandbox):
    """同型: 世代が無くて後始末を保留した側も、届くまで再試行すること。

    `_cleanup_deferred()` は `cleanup_deferred=True` を立てたあと二度と報告しない。
    `_unprovable()` と同じく通知の成否を見ていないので、**Worker は既に殺されて
    いるのに task が in_progress のまま誰にも知らされない** — この module が
    消すために書かれた幽霊 task そのものになる。1 箇所だけ直すと同じ形が残る
    (memory: crewvia-recurring-defect-patterns)。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    state, delivered, notify = _flaky_notifier()

    ex = make_executor(sandbox, mux, notify=notify)
    hidden = sandbox.task_file.with_suffix(".hidden")
    sandbox.task_file.rename(hidden)            # 世代を読めない状態で request
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    hidden.rename(sandbox.task_file)

    # Worker が自分で落ちる。pid が死んでいるので exit は「証明済み」となり、
    # guard を通らずに terminated へ入る — 世代が無いまま後始末を迫られる、
    # `_cleanup_deferred()` に実際に到達する唯一の並び。
    os.kill(pane_pid, signal.SIGKILL)
    for _ in range(200):
        if not _pid_alive(pane_pid):
            break
        time.sleep(0.01)
    assert not _pid_alive(pane_pid)

    _drive(ex, 8)
    prog = _progress_of(sandbox)
    assert prog.get("cleanup_deferred"), (
        f"後始末の保留になっていない — テストの前提が崩れている: {prog}")
    assert delivered == []

    state["online"] = True
    _drive(ex, 3)
    assert len(delivered) == 1, (
        f"通知が復旧しても Director に届かない "
        f"(delivered={delivered}, logs={sandbox.logs})")
    assert "--reset" in delivered[0], (
        f"kill 済みなのに task の戻し方が書かれていない: {delivered[0]}")

    _drive(ex, 3)
    assert len(delivered) == 1, f"届いたあとも送り続けている: {delivered}"


# -- 5. 旧形式の marker と、届いたあとの永続化失敗 (t034 / Codex 7巡目 P2) ----

def test_red_pre_upgrade_unprovable_marker_without_a_saved_message_still_gets_reported(sandbox):
    """`pending_report` を持たない旧形式の marker も、フォールバックで報告すること。

    RED (t034 / Codex 7巡目 P2-1): `_retry_pending_report()` は `pending_report`
    が無ければ何もせず None を返す。この fix (`_report_fields()`) より前の
    watchdog が書いた `unprovable` marker は `director_notified=False` だが
    `pending_report` を持たない。`recover()` はこの形式を移行しないし、
    `_check_stall()` はこの phase の報告を明示的に抑制するので、再起動後は
    **どこからも拾われず、Director に一度も知らされないまま Worker が隔離され
    続ける**。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    state, delivered, notify = _flaky_notifier()

    ex = make_executor(sandbox, mux, notify=notify)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    sandbox.assignment_identity_file.unlink()   # 世代が読めない → hold

    _drive(ex, 4)
    prog = _progress_of(sandbox)
    assert prog.get("phase") == "unprovable", (
        f"hold になっていない — テストの前提が崩れている: {prog}")
    assert not prog.get("director_notified")

    # 旧バージョンの watchdog が書いた marker を模す: pending_report が無い。
    import lib_retirement
    prog.pop("pending_report", None)
    lib_retirement.write_json_atomic(
        lib_retirement.progress_path(sandbox.registry, AGENT), prog)

    state["online"] = True
    _drive(ex, 3)

    assert len(delivered) == 1, (
        f"pending_report の無い旧形式 marker が report を再試行しない "
        f"(delivered={delivered}, logs={sandbox.logs})")
    note = delivered[0]
    assert AGENT in note
    assert "registry/retirements" in note, f"隔離の解き方が書かれていない: {note}"

    _drive(ex, 3)
    assert len(delivered) == 1, f"届いたあとも送り続けている: {delivered}"


def test_red_pre_upgrade_cleanup_deferred_marker_without_a_saved_message_still_gets_reported(sandbox):
    """同型: `cleanup_deferred` 側の旧形式 marker も同じ穴を持つ。

    RED (t034 / Codex 7巡目 P2-1 の同型): `_cleanup_deferred()` も
    `_retry_pending_report()` に頼っているので、`pending_report` の無い旧形式
    marker はここでも黙って再試行されない。1 箇所だけ直すと同じ形が残る
    (memory: crewvia-recurring-defect-patterns)。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    state, delivered, notify = _flaky_notifier()

    ex = make_executor(sandbox, mux, notify=notify)
    hidden = sandbox.task_file.with_suffix(".hidden")
    sandbox.task_file.rename(hidden)            # 世代を読めない状態で request
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    hidden.rename(sandbox.task_file)

    os.kill(pane_pid, signal.SIGKILL)
    for _ in range(200):
        if not _pid_alive(pane_pid):
            break
        time.sleep(0.01)
    assert not _pid_alive(pane_pid)

    _drive(ex, 8)
    prog = _progress_of(sandbox)
    assert prog.get("cleanup_deferred"), (
        f"後始末の保留になっていない — テストの前提が崩れている: {prog}")
    assert not prog.get("director_notified")

    # 旧バージョンの watchdog が書いた marker を模す: pending_report が無い。
    import lib_retirement
    prog.pop("pending_report", None)
    lib_retirement.write_json_atomic(
        lib_retirement.progress_path(sandbox.registry, AGENT), prog)

    state["online"] = True
    _drive(ex, 3)

    assert len(delivered) == 1, (
        f"pending_report の無い旧形式 marker が report を再試行しない "
        f"(delivered={delivered}, logs={sandbox.logs})")
    assert "--reset" in delivered[0], (
        f"kill 済みなのに task の戻し方が書かれていない: {delivered[0]}")

    _drive(ex, 3)
    assert len(delivered) == 1, f"届いたあとも送り続けている: {delivered}"


def test_red_delivered_report_is_not_resent_when_persisting_the_receipt_fails(sandbox):
    """届いた通知の受領証だけを再試行し、通知自体は再送しないこと。

    RED (t034 / Codex 7巡目 P2-2): `_retry_pending_report()` は `_report()` が
    成功したあと `_write_progress(..., director_notified=True, ...)` の戻り値を
    見ていない。永続化がディスク満杯などで落ちると、次の cycle も
    `director_notified=False` かつ `pending_report` が残ったままなので
    **同じ通知をもう一度送る** — Codex はメモリ上の再現で 3 回の呼び出しから
    3 通の配信を確認したと報告している。
    """
    pane_pid = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, pane_pid)
    mux = FakeMux({WINDOW: pane_pid})
    state, delivered, notify = _flaky_notifier()

    ex = make_executor(sandbox, mux, notify=notify)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID)
    sandbox.assignment_identity_file.unlink()   # 世代が読めない → hold

    _drive(ex, 4)
    prog = _progress_of(sandbox)
    assert prog.get("phase") == "unprovable", (
        f"hold になっていない — テストの前提が崩れている: {prog}")
    assert delivered == []

    state["online"] = True

    import lib_retirement
    real_write = lib_retirement.write_json_atomic

    def failing_write(path, data):
        if str(path).endswith(".progress.json"):
            return False
        return real_write(path, data)

    lib_retirement.write_json_atomic = failing_write
    try:
        ex.process_all()
    finally:
        lib_retirement.write_json_atomic = real_write

    assert len(delivered) == 1, "通知そのものが届いていない — テストの前提が崩れている"
    prog = _progress_of(sandbox)
    assert not prog.get("director_notified"), (
        "受領証の永続化が失敗しているのに director_notified が立っている — "
        "テストの前提が崩れている")

    _drive(ex, 3)

    assert len(delivered) == 1, (
        f"受領証の永続化が失敗しただけなのに通知を再送した: {delivered}")
    prog = _progress_of(sandbox)
    assert prog.get("director_notified"), "永続化が復旧しても受領証が書かれない"


def _progress_of(sandbox) -> dict:
    import lib_retirement
    return lib_retirement.read_json(
        lib_retirement.progress_path(sandbox.registry, AGENT)) or {}
