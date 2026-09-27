#!/usr/bin/env python3
"""
tests/test_retirement_wait_and_timeout_notice.py — PR6 (mission 20260926-mechanize-guards-a / t021)

## 直している 2 つの穴

### 1. 前任の後始末待ちで、後任の最初の pull が必ず空振りする

Worker 名は使い回される。前任の退役が「kill は済み、queue の後始末だけ残っている」
(progress の phase が `terminated`、記録された pane_pid が死んでいる) の間に後任が起動すると、
後任の最初の `plan.sh pull` は前任の marker に当たって `retirement_reserved` で拒否される
(2026-09-23: watchdog が終えたのは後任の起動の 5 秒後)。

直し方: 拒否の既定は変えず、**その 3 条件を証明できたときだけ**、後始末が終わるのを最大 60 秒待って
判定からやり直す。待つのはキューロックの外。

### 2. watchdog の timeout 終了が Director に 1 回だけ、必要な情報つきで届く

以前は「終了しました (理由: timeout)」の 1 通だけで、どの上限か・どれだけか・`--reset` の要否が
無く、送れなくても再送されず、台帳も無いので 2 度目の settle があれば 2 通目が届いた。
今は request に判定の根拠を載せ、settle のときに **notify-once 台帳** (dispatcher と共有) に乗せて 1 通だけ送る。
届かなければ届くまで再送する。

## 方法

- 1 は **本物の plan.sh** を使い捨ての sandbox で回す (tests/test_retirement.py の fixture を再利用)。
- 2 は本物の `watchdog.make_notify_once` + 本物の `RetirementExecutor` + 本物の dispatcher.sh の
  埋め込み python (prune)。

実行: python3 -m pytest tests/test_retirement_wait_and_timeout_notice.py -v
"""

import ast
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import lib_daemon_state  # noqa: E402
import watchdog  # noqa: E402

# 使い捨て sandbox と本物の plan.sh を回す道具は test_retirement.py のものを共用する。
from test_retirement import (  # noqa: E402,F401  (sandbox は pytest fixture)
    AGENT, SLUG, TASK_ID, WINDOW, FakeMux, _add_pending_task, _plan, _status_of,
    make_executor, sandbox,
)
# dispatcher.sh の埋め込み python を 1 サイクル回す道具。
from test_dispatcher_notify_once import Harness, SLUG as NOTIFY_SLUG  # noqa: E402,F401


# ===========================================================================
# 1. plan.sh pull — 死んだ前任の後始末を待って取り直す
# ===========================================================================

def _retirements(sandbox) -> Path:
    d = sandbox.registry / "retirements"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dead_pid() -> int:
    """確かに死んでいる pid (子を作って reap する)。ESRCH になることを確かめて返す。"""
    proc = subprocess.Popen(["true"])
    proc.wait()
    try:
        os.kill(proc.pid, 0)
    except ProcessLookupError:
        return proc.pid
    pytest.skip("pid が再利用されていて、確かに死んでいる pid を用意できない")


def _put_marker(sandbox, *, phase="terminated", pane_pid="dead", request=True,
                progress=True, progress_text=None):
    d = _retirements(sandbox)
    pid = _dead_pid() if pane_pid == "dead" else pane_pid
    if request:
        (d / f"{AGENT}.json").write_text(json.dumps({
            "request_id": "req-1", "agent": AGENT, "reason": "timeout",
            "mission": SLUG, "task_id": "t001",
            "spawn_identity": {"pane_pid": None if progress else pid},
        }))
    if progress:
        text = progress_text if progress_text is not None else json.dumps({
            "request_id": "req-1", "phase": phase, "pane_pid": pid})
        (d / f"{AGENT}.progress.json").write_text(text)


def _clear_marker(sandbox):
    for f in _retirements(sandbox).glob(f"{AGENT}*"):
        f.unlink()


def _pull_argv(sandbox):
    return ["bash", str(sandbox.scripts / "plan.sh"), "pull", "--mission", SLUG,
            "--agent", AGENT, "--skills", "code"]


def _pull_env(sandbox):
    env = dict(os.environ)
    env.update(CREWVIA_QUEUE=str(sandbox.queue), CREWVIA_REPO_ROOT=str(sandbox.root),
               AGENT_NAME=AGENT)
    return env


def _start_pull(sandbox):
    return subprocess.Popen(_pull_argv(sandbox), env=_pull_env(sandbox),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _free_agent_and_add_task(sandbox):
    """Worker は前の task を終えていて、次の pending が 1 枚ある状態にする。"""
    done = _plan(sandbox, "done", TASK_ID, "前の task は完了", "--mission", SLUG)
    assert done.returncode == 0, done.stderr
    _add_pending_task(sandbox, "t002")


def test_pull_waits_for_the_dead_predecessors_cleanup_and_then_takes_the_task(sandbox):
    """前任の後始末が終わったら、待っていた pull は task を取る (空振りしない)。"""
    _free_agent_and_add_task(sandbox)
    _put_marker(sandbox)

    proc = _start_pull(sandbox)
    try:
        time.sleep(2.5)
        assert proc.poll() is None, (
            "後始末待ちなのに、待たずに返った (前任の marker で空振りする旧挙動):\n"
            + (proc.stdout.read() + proc.stderr.read()))
        assert _status_of(sandbox, "t002") == "pending", "待っている間に card を書き換えた"

        _clear_marker(sandbox)          # watchdog の後始末が終わった
        out, err = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, f"marker が消えても取れなかった: rc={proc.returncode}\n{out}{err}"
    assert json.loads(out.strip().splitlines()[-1])["id"] == "t002"
    assert _status_of(sandbox, "t002") == "in_progress"
    assert "後始末待ち" in err, "なぜ待っているのかが stderr に残っていない"


def test_waiting_pull_does_not_hold_the_queue_lock(sandbox):
    """待機はキューロックの外。待っている間も他の plan.sh (retire --no-wait) は通る。

    ロックを持ったまま待つと、dispatcher・全 Worker の plan.sh・watchdog が同期で叩く
    `retire --no-wait` (LOCK_BUSY = exit 4) が止まる。
    """
    _free_agent_and_add_task(sandbox)
    _put_marker(sandbox)
    proc = _start_pull(sandbox)
    try:
        time.sleep(1.5)
        assert proc.poll() is None, "pull が待っていない (テストの前提が崩れている)"
        started = time.monotonic()
        other = _plan(sandbox, "retire", TASK_ID, "--agent", "Someone", "--started-at",
                      "nope", "--mission", SLUG, "--no-wait", timeout=20)
        elapsed = time.monotonic() - started
        assert other.returncode != 4, (
            f"待機中の pull がキューロックを握っている (retire --no-wait が LOCK_BUSY): "
            f"{other.stdout}{other.stderr}")
        assert elapsed < 5, f"別の plan.sh が待たされた ({elapsed:.1f}s)"
        # 待たされずに **書ける** ことも見る (retire は読むだけ、update は書く)。
        upd = _plan(sandbox, "update", "t002", "--priority", "low", "--mission", SLUG, timeout=20)
        assert upd.returncode == 0, upd.stderr
    finally:
        _clear_marker(sandbox)
        proc.communicate(timeout=20)


def test_pull_rejudges_from_the_top_after_waiting(sandbox):
    """待った結果を「通ってよい」の証拠にしない: marker が別の退役に替わっていたら拒否する。"""
    _free_agent_and_add_task(sandbox)
    _put_marker(sandbox)
    live = subprocess.Popen(["sleep", "300"])
    proc = _start_pull(sandbox)
    try:
        time.sleep(1.5)
        assert proc.poll() is None
        _clear_marker(sandbox)
        # 後任自身の退役が始まった (pid は生きている・phase は notified)。
        _put_marker(sandbox, phase="notified", pane_pid=live.pid)
        out, err = proc.communicate(timeout=15)
    finally:
        live.kill()
        live.wait()
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 2, f"判定をやり直さず task を渡した: rc={proc.returncode}\n{out}{err}"
    assert _status_of(sandbox, "t002") == "pending"
    assert not sandbox.assignment_file.exists()
    assert "retirement_reserved" in err


@pytest.mark.parametrize("label, kwargs", [
    ("phase が terminated でない (kill がまだ)", dict(phase="notified")),
    ("phase が cleanup_failed (人間待ち。待っても終わらない)", dict(phase="cleanup_failed")),
    ("progress が無く request だけ", dict(progress=False)),
    ("progress が壊れている", dict(progress_text="{not json")),
    ("progress が JSON だが object でない", dict(progress_text="[1, 2]")),
    ("pid がどこにも記録されていない", dict(pane_pid=None)),
    ("pid が数でない", dict(pane_pid="abc")),
])
def test_pull_still_refuses_at_once_when_the_predecessor_is_not_proven_gone(sandbox, label, kwargs):
    """判断できない・証拠が欠けるときは、待たずに従来どおり拒否する。"""
    _free_agent_and_add_task(sandbox)
    _put_marker(sandbox, **kwargs)
    started = time.monotonic()
    pull = _plan(sandbox, "pull", "--mission", SLUG, "--agent", AGENT, "--skills", "code")
    elapsed = time.monotonic() - started
    assert pull.returncode == 2, f"{label}: rc={pull.returncode}\n{pull.stdout}{pull.stderr}"
    assert "retirement_reserved" in pull.stderr, label
    assert elapsed < 10, f"{label}: 待ってしまった ({elapsed:.1f}s)"
    assert "後始末待ち" not in pull.stderr, label
    assert _status_of(sandbox, "t002") == "pending"
    assert not sandbox.assignment_file.exists()


def test_pull_does_not_wait_when_the_recorded_pid_is_still_alive(sandbox):
    """pid が生きている marker は「前任の後始末待ち」ではない (自分自身の退役かもしれない)。"""
    _free_agent_and_add_task(sandbox)
    live = subprocess.Popen(["sleep", "300"])
    try:
        _put_marker(sandbox, phase="terminated", pane_pid=live.pid)
        started = time.monotonic()
        pull = _plan(sandbox, "pull", "--mission", SLUG, "--agent", AGENT, "--skills", "code")
        elapsed = time.monotonic() - started
    finally:
        live.kill()
        live.wait()
    assert pull.returncode == 2 and "retirement_reserved" in pull.stderr, pull.stderr
    assert elapsed < 10, f"生きている pid を後始末待ちと読んだ ({elapsed:.1f}s)"


def test_pull_stops_waiting_at_the_limit_and_refuses(sandbox):
    """後始末が終わらない marker で pull を永久に待たせない (上限で拒否に戻る)。

    上限は sandbox にコピーした plan.sh の定数を書き換えて縮める (本番の 60 秒は待たない)。
    書き換えが効いたことは assert で確かめる — 効いていなければこのテストは 60 秒かかる。
    """
    plan = sandbox.scripts / "plan.sh"
    text = plan.read_text()
    text, n = re.subn(r"^PREDECESSOR_CLEANUP_WAIT_SECONDS = 60$",
                      "PREDECESSOR_CLEANUP_WAIT_SECONDS = 3", text, flags=re.M)
    assert n == 1, "上限の定数を書き換えられなかった (このテストの前提が崩れている)"
    plan.write_text(text)

    _free_agent_and_add_task(sandbox)
    _put_marker(sandbox)                # 消されない = 後始末が終わらない
    started = time.monotonic()
    pull = _plan(sandbox, "pull", "--mission", SLUG, "--agent", AGENT, "--skills", "code")
    elapsed = time.monotonic() - started
    assert pull.returncode == 2, f"rc={pull.returncode}\n{pull.stdout}{pull.stderr}"
    assert 2.5 <= elapsed < 20, f"上限 (3 秒) で戻っていない: {elapsed:.1f}s"
    assert "retirement_reserved" in pull.stderr
    assert "待ちましたが" in pull.stderr, "待ったうえで諦めたことが理由に残っていない"
    assert _status_of(sandbox, "t002") == "pending"


def test_the_wait_limit_and_its_basis_are_written_next_to_the_constant():
    """60 秒という数の根拠が、定数の隣に残っていること (t021 の要件)。"""
    text = (REPO / "scripts" / "plan.sh").read_text()
    m = re.search(r"^PREDECESSOR_CLEANUP_WAIT_SECONDS = 60$", text, flags=re.M)
    assert m, "定数が無い / 値が変わった"
    before = text[:m.start()]
    assert "DEFAULT_CHECK_INTERVAL" in before[-1200:], "根拠 (watchdog の周期) が定数の隣に無い"


# ===========================================================================
# 2. watchdog の timeout 終了 → Director に 1 回だけ
# ===========================================================================

class Director:
    """Director の窓。届いた通知を貯める。`up=False` で「送れない」状態にできる。"""

    def __init__(self):
        self.up = True
        self.inbox = []

    def send(self, message):
        if not self.up:
            return False
        self.inbox.append(message)
        return True


def _notify_once_for(sandbox, director, logs=None):
    return watchdog.make_notify_once(sandbox.root, send=director.send,
                                     log=(logs.append if logs is not None else sandbox.logs.append))


def _timeout_executor(sandbox, director, **kw):
    """本番の組み立て (`watchdog.make_retirement_executor`) と同じ配線の executor。"""
    mux = FakeMux({WINDOW: sandbox.spawn_worker_process()})
    sandbox.record_identity(WINDOW, mux.windows[WINDOW])
    ex = make_executor(sandbox, mux, notify=director.send,
                       notify_once=_notify_once_for(sandbox, director), **kw)
    return ex, mux


def _run_timeout_retirement(sandbox, ex, detail, max_cycles=20):
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID, detail=detail)
    for _ in range(max_cycles):
        ex.process_all()
        if not ex.has_marker(AGENT):
            return
    raise AssertionError("retirement did not settle")


MAX_DETAIL = {"kind": "max", "observed_seconds": 3604, "limit_seconds": 3600}
IDLE_DETAIL = {"kind": "idle", "observed_seconds": 700, "limit_seconds": 600}


def _ledger(sandbox):
    path = sandbox.registry / "daemons" / "notified-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def test_timeout_termination_tells_the_director_once_with_what_it_needs(sandbox):
    director = Director()
    ex, _mux = _timeout_executor(sandbox, director)
    _run_timeout_retirement(sandbox, ex, MAX_DETAIL)

    assert len(director.inbox) == 1, director.inbox
    msg = director.inbox[0]
    assert TASK_ID in msg and SLUG in msg, "task id / mission が無い"
    assert "max=3600s" in msg and "3604s" in msg, f"どの上限か・経過時間が無い: {msg}"
    assert "不要" in msg and "--reset" in msg, f"reset の要否が無い: {msg}"
    assert sandbox.task_status() == "pending", "後始末 (pending に戻す) が済んでいない"

    key = f"timeout_{SLUG}_{TASK_ID}"
    entry = _ledger(sandbox)[key]
    assert entry["kind"] == "timeout" and entry["slug"] == SLUG and entry["task"] == TASK_ID
    assert entry["fp"], "fingerprint が無い"


def test_idle_timeout_names_the_idle_limit(sandbox):
    director = Director()
    ex, _mux = _timeout_executor(sandbox, director)
    _run_timeout_retirement(sandbox, ex, IDLE_DETAIL)
    assert len(director.inbox) == 1
    assert "無活動" in director.inbox[0] and "700s" in director.inbox[0], director.inbox[0]


def test_the_same_retirement_is_never_told_twice(sandbox):
    """settle の再実行 (送信のあと・marker を消す前にデーモンが落ちた) で 2 通目が出ない。"""
    director = Director()
    notify_once = _notify_once_for(sandbox, director)
    key, fp = f"timeout_{SLUG}_{TASK_ID}", "req-1"
    assert notify_once(key, fp, "timeout", SLUG, TASK_ID, "first") is True
    assert notify_once(key, fp, "timeout", SLUG, TASK_ID, "first again") is True
    assert director.inbox == ["first"], "同じ通知が 2 通届いた"
    # 別の退役 (= 新しい事象) は届く。
    assert notify_once(key, "req-2", "timeout", SLUG, TASK_ID, "second") is True
    assert director.inbox == ["first", "second"]


def test_a_second_timeout_of_the_same_task_is_a_new_event_and_is_told(sandbox):
    """同じ task が別の実行でもう一度 timeout したら、届く (fingerprint は退役ごと)。

    key を task に、fingerprint を定数にすると、2 度目以降の timeout が永久に黙る。
    """
    director = Director()
    ex, mux = _timeout_executor(sandbox, director)
    _run_timeout_retirement(sandbox, ex, MAX_DETAIL)
    assert len(director.inbox) == 1

    # Director が差し戻し、同名の後任が同じ task を pull し直し、また timeout した。
    card = sandbox.task_file.read_text()
    card = re.sub(r"^status:.*$", "status: in_progress", card, flags=re.M)
    card = re.sub(r"^worker:.*$", f"worker: {AGENT}", card, flags=re.M)
    card = re.sub(r"^started_at:.*$", 'started_at: "2026-09-21T01:00:00Z"', card, flags=re.M)
    sandbox.task_file.write_text(card)
    sandbox.publish_assignment("2026-09-21T01:00:00Z")
    mux.windows[WINDOW] = sandbox.spawn_worker_process()
    sandbox.record_identity(WINDOW, mux.windows[WINDOW])

    _run_timeout_retirement(sandbox, ex, IDLE_DETAIL)
    assert len(director.inbox) == 2, "2 度目の timeout が黙殺された"
    assert "無活動" in director.inbox[1]


def test_an_unreachable_director_is_retried_until_it_hears_once(sandbox):
    """Director が居ない間は台帳に書かず、戻ったら次のサイクルで 1 通だけ届く。"""
    director = Director()
    director.up = False
    ex, _mux = _timeout_executor(sandbox, director)
    _run_timeout_retirement(sandbox, ex, MAX_DETAIL)      # marker は消えた・通知は未達
    assert director.inbox == []
    assert _ledger(sandbox) == {}, "届いていないのに「伝えた」と記録した (戻っても届かなくなる)"
    assert sandbox.task_status() == "pending", "通知が届かないことで後始末が止まった"

    ex.process_all()                                       # まだ居ない
    assert director.inbox == []

    director.up = True
    ex.process_all()
    ex.process_all()
    ex.process_all()
    assert len(director.inbox) == 1, director.inbox
    assert f"timeout_{SLUG}_{TASK_ID}" in _ledger(sandbox)


def test_an_undeliverable_notice_is_given_up_after_the_limit(sandbox):
    director = Director()
    director.up = False
    clock = [1000.0]
    ex, _mux = _timeout_executor(sandbox, director, now=lambda: clock[0])
    _run_timeout_retirement(sandbox, ex, MAX_DETAIL)
    assert ex._owed_notices
    clock[0] += ex.OWED_NOTICE_GIVE_UP_SECONDS + 1
    director.up = True
    ex.process_all()
    assert not ex._owed_notices and director.inbox == [], "上限を過ぎても再送し続けている"
    assert any("giving up" in line for line in sandbox.logs)


def test_non_timeout_retirements_keep_the_plain_report_and_leave_the_ledger_alone(sandbox):
    """timeout 以外 (no-task 等) の報告は従来のまま。台帳にも触れない。"""
    from test_retirement import make_idle
    director = Director()
    make_idle(sandbox)
    mux = FakeMux({WINDOW: sandbox.spawn_worker_process()})
    sandbox.record_identity(WINDOW, mux.windows[WINDOW])
    ex = make_executor(sandbox, mux, notify=director.send,
                       notify_once=_notify_once_for(sandbox, director))
    assert ex.request(AGENT, WINDOW, "no_task")
    for _ in range(20):
        ex.process_all()
        if not ex.has_marker(AGENT):
            break
    assert _ledger(sandbox) == {}


def test_without_a_ledger_the_old_plain_report_is_still_sent(sandbox):
    """`notify_once` を配線していない呼び出し側 (旧テスト・他の caller) は従来どおり 1 通。"""
    director = Director()
    mux = FakeMux({WINDOW: sandbox.spawn_worker_process()})
    sandbox.record_identity(WINDOW, mux.windows[WINDOW])
    ex = make_executor(sandbox, mux, notify=director.send)
    _run_timeout_retirement(sandbox, ex, MAX_DETAIL)
    assert len(director.inbox) == 1 and "復旧作業は不要" in director.inbox[0]


def test_detail_is_carried_from_the_request_into_the_progress_file(sandbox):
    """request が消えたあとの settle でも、判定の根拠が手元に残っていること。"""
    director = Director()
    ex, _mux = _timeout_executor(sandbox, director, grace_period=3600)
    assert ex.request(AGENT, WINDOW, "timeout", mission=SLUG, task_id=TASK_ID,
                      detail=MAX_DETAIL)
    req = json.loads((sandbox.registry / "retirements" / f"{AGENT}.json").read_text())
    assert req["detail"] == MAX_DETAIL
    ex.process_all()
    prog = json.loads((sandbox.registry / "retirements" / f"{AGENT}.progress.json").read_text())
    assert prog["detail"] == MAX_DETAIL


def test_timeout_detail_names_the_limit_that_fired():
    monitor = watchdog.WorkerMonitor(
        task_id=TASK_ID, task_card={"worker": AGENT, "timeout": {"idle": 300, "max": 3600}},
        profiles=watchdog.PROFILES, repo_root=REPO)
    max_hit = watchdog.CheckResult("terminate", "max_exceeded", 5.0, "not_probed", False)
    idle_hit = watchdog.CheckResult("terminate", "hard_idle", 640.4, "idle", False)
    assert watchdog.timeout_detail(monitor, max_hit, 3604.2) == {
        "kind": "max", "observed_seconds": 3604, "limit_seconds": 3600}
    assert watchdog.timeout_detail(monitor, idle_hit, 900.0) == {
        "kind": "idle", "observed_seconds": 640, "limit_seconds": 600}


def test_the_watchdog_terminate_branch_hands_the_detail_to_the_request():
    """run() の terminate 分岐が、request に detail を渡していること (構造)。

    判定の根拠を渡し忘れると、通知は「種別を記録できていません」になる。
    """
    tree = ast.parse((REPO / "scripts" / "watchdog.py").read_text())
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run")
    calls = [n for n in ast.walk(run)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "request"]
    assert len(calls) == 1, "run() の中の retirement.request が 1 つでなくなった"
    assert any(kw.arg == "detail" for kw in calls[0].keywords), \
        "timeout の request に detail (どの上限か) を渡していない"


# ---------------------------------------------------------------------------
# 台帳は 2 人が書く — 書き込みは排他
# ---------------------------------------------------------------------------

def _hammer(path, prefix, n):
    for i in range(n):
        entry = {"fp": "x", "kind": "needs_director", "slug": "s", "task": f"{prefix}{i}"}
        while not lib_daemon_state.told_record(path, f"{prefix}_{i}", entry):
            time.sleep(0.01)        # ロックが取れなかった = 次のサイクルで再試行


def test_concurrent_ledger_writers_do_not_lose_each_others_entries(tmp_path):
    """dispatcher と watchdog が同時に書いても、どちらのエントリも消えない。

    原子的な置換 (temp + os.replace) だけでは「A が読む → B が書く → A が書く」で B の
    エントリが消える。消えたのが dispatcher のエントリなら、対処済みの通知が再送される。
    """
    path = str(tmp_path / "daemons" / "notified-state.json")
    procs = [multiprocessing.Process(target=_hammer, args=(path, f"p{k}", 40)) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    data = json.loads(Path(path).read_text())
    assert len(data) == 160, f"書き込みが失われた: {len(data)}/160"


def test_ledger_lock_is_taken_by_the_dispatcher_writers_too():
    """dispatcher の record_told / prune_told も同じロックの下 (構造)。"""
    src = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", (REPO / "scripts" / "dispatcher.sh").read_text(),
                    re.DOTALL).group(1)
    tree = ast.parse(src)
    for name in ("record_told", "prune_told"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        locks = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "told_lock"]
        saves = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "save_told"]
        assert locks and saves, f"{name}: told_lock か save_told が無い"
        inside = {id(c) for w in ast.walk(fn) if isinstance(w, ast.With) for c in ast.walk(w)}
        assert all(id(c) in inside for c in saves), \
            f"{name}: 台帳の書き込み (save_told) がロックの外にある"


def test_told_lock_reports_failure_instead_of_blocking_on_a_busy_lock(tmp_path):
    path = str(tmp_path / "notified-state.json")
    with lib_daemon_state.told_lock(path) as first:
        assert first is True
        started = time.monotonic()
        with lib_daemon_state.told_lock(path, wait=0.3) as second:
            assert second is False, "同じロックを二重に取れた"
        assert time.monotonic() - started < 3


# ---------------------------------------------------------------------------
# dispatcher の prune は、TTL 内の timeout 通知を捨てない
# ---------------------------------------------------------------------------

@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path / "repo", monkeypatch)


def _seed_ledger(h, entries):
    h.told_file.parent.mkdir(parents=True, exist_ok=True)
    h.told_file.write_text(json.dumps(entries))


def test_dispatcher_prune_keeps_fresh_timeout_notices_and_drops_stale_ones(h):
    """timeout 通知の task は後始末で pending に戻り、live key に現れない。

    prune が「状態を離れた」と読んで捨てると、crash 跨ぎの重複防止が効かない。TTL を過ぎたものは
    掃除される (台帳が育ち続けない)。
    """
    now = time.time()
    h.card("t001", "pending")
    _seed_ledger(h, {
        "timeout_fresh": {"fp": "a", "kind": "timeout", "slug": NOTIFY_SLUG, "task": "t001",
                          "at": now - 60},
        "timeout_stale": {"fp": "b", "kind": "timeout", "slug": NOTIFY_SLUG, "task": "t001",
                          "at": now - lib_daemon_state.TOLD_TIMEOUT_TTL_SECONDS - 60},
        "timeout_no_at": {"fp": "c", "kind": "timeout", "slug": NOTIFY_SLUG, "task": "t001"},
        "needs_director_gone": {"fp": "d", "kind": "needs_director", "slug": NOTIFY_SLUG,
                                "task": "t001"},
    })
    h.cycle()
    left = json.loads(h.told_file.read_text())
    assert "timeout_fresh" in left, "TTL 内の timeout 通知を prune した"
    assert "timeout_stale" not in left and "timeout_no_at" not in left \
        and "needs_director_gone" not in left, sorted(left)
