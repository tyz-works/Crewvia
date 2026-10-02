#!/usr/bin/env python3
"""01c E4a (t016): `update --reset` / `plan.sh retire` / 退役 marker が Execution ID で試行を解放・照合する。

設計: `knowledge/execution.md` §2.1 (#8〜#13)・§2.2 (移行期の規則)・§4.2 / §4.4 (試行の終わらせ方・冪等)・§7 (E4a / E4b の分割)・
§16.7 / §17。受入条件:

1. 退役の全経路で、**同名の別 Worker・新しい試行を殺さない / 消さない**。旧形式の marker (`task_execution_id` なし) も読める
2. 実 `RetirementExecutor` + 実 plan.sh (隔離コピー) で、退役の依頼 → 実行 → 回収が通る
3. `--execution ""` (空の明示指定) は世代だけの経路・名乗りなしに倒さず exit 1 (retire `--started-at ""` と同じ)
4. Director の cutover review task の閉じ方 (`update --status in_progress --reset` → フラグなしの `done`) と
   Director の `reap-orphan-assignment` が E4a の後も通る
5. `update --reset` は試行を閉じる (reserved → released / running → failed `RESET_BY_DIRECTOR` / DETACHED は ABANDONED)

**本番の queue / registry / mux / デーモンには触れない** (`pull_execution_helpers.Box` が隔離する。mux は FakeMux、
Worker 役は `sleep` のプロセス)。
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import MISSION, OTHER_ID, XID_RE, Box, audit_rows_for, last_error_code, run, set_card, take

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lib_retirement as rt  # noqa: E402

AGENT = "Ren"
WINDOW = f"{AGENT}-worker"
TID = "t001"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001", "t002"))


def retire(box, *args, tid=TID, agent=AGENT, who="watchdog"):
    return run(box, "retire", tid, "--agent", agent, "--mission", MISSION, *args, agent=who)


def update_reset(box, tid=TID, *extra):
    return run(box, "update", tid, "--reset", "--mission", MISSION, *extra, agent="Director")


def state(box, tid=TID):
    m = box.card(tid)
    return (m.get("status"), m.get("worker"), m.get("started_at"), m.get("execution_status"),
            m.get("execution_end_code"))


def set_identity(box, agent=AGENT, **fields):
    """`queue/assignments/<agent>.identity` を書き換える (None の値は欄を消す)。旧コードの publish を作る道具。"""
    p = box.queue / "assignments" / f"{agent}.identity"
    data = json.loads(p.read_text())
    for k, v in fields.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    p.write_text(json.dumps(data, sort_keys=True) + "\n")


def become_successor(box, execution_id=OTHER_ID, started_at=None, agent=AGENT):
    """同名の後任が同じ task を pull し直した形を、card と枠に直接作る (pull は退役 marker があると拒否するので)。
    `started_at` を前の世代と同じにすれば、世代では区別できない後任になる。"""
    old = box.card()
    gen = started_at or "2026-10-02T09:00:00.000000Z"
    with e3.store.transaction(box.queue, op="seed", actor="test") as t:
        meta, body = t.load_card(MISSION, TID)
        meta = dict(meta)
        meta.update(status="in_progress", worker=agent, started_at=gen, current_execution_id=execution_id,
                    execution_status="running", execution_count=int(old.get("execution_count") or 1) + 1,
                    execution_reserved_at=gen)
        meta.pop("execution_end_code", None)
        t.write_card(MISSION, TID, meta, body)
        t.publish_assignment(agent, MISSION, TID, gen, execution_id=execution_id)
    return gen


# ---------------------------------------------------------------------------
# 1. plan.sh retire --execution
# ---------------------------------------------------------------------------

def test_retire_by_execution_closes_a_running_attempt_and_resets_the_task(box):
    xid = take(box)
    p = retire(box, "--execution", xid)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert state(box) == ("pending", None, None, "failed", "RETIRED")
    assert box.card()["current_execution_id"] == xid
    assert box.slot(AGENT) is None and box.identity(AGENT) is None
    rec = box.record(xid)
    assert (rec["status"], rec["end_code"]) == ("failed", "RETIRED")
    rows = [r for r in audit_rows_for(box, "retire") if r["result"] == "ok"]
    assert len(rows) == 1 and rows[0]["execution_id"] == xid and rows[0]["actor"] == "watchdog"


def test_retire_by_execution_releases_a_reserved_attempt(box):
    xid = take(box)
    set_card(box, execution_status="reserved")           # pull が start に進む前に落ちた形
    p = retire(box, "--execution", xid)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert state(box) == ("pending", None, None, "released", "RETIRED")
    assert box.record(xid)["status"] == "released"


def test_retire_needs_director_keeps_the_worker_and_stores_the_reason(box):
    xid = take(box)
    p = retire(box, "--execution", xid, "--outcome", "needs-director", "--reason", "stuck on a prompt")
    assert p.returncode == 0, (p.stdout, p.stderr)
    m = box.card()
    assert (m["status"], m["worker"], m["execution_status"], m["execution_end_code"]) == (
        "needs_director", AGENT, "failed", "RETIRED")
    assert m["needs_director_reason"] == "stuck on a prompt"
    assert box.slot(AGENT) is None                       # 枠は撤去 (Worker は死んでいる)


def test_retire_by_generation_still_works_on_an_active_attempt_and_closes_it(box):
    take(box)
    gen = box.card()["started_at"]
    p = retire(box, "--started-at", gen)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert state(box) == ("pending", None, None, "failed", "RETIRED")


def test_a_wrong_execution_is_refused_with_exit_3_and_nothing_is_written(box):
    take(box)
    before = box.snapshot()
    p = retire(box, "--execution", OTHER_ID)
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert box.snapshot() == before
    assert OTHER_ID not in p.stderr                      # 名乗られた値を出さない


def test_a_malformed_execution_is_refused_and_nothing_is_written(box):
    take(box)
    before = box.snapshot()
    p = retire(box, "--execution", "not-an-id")
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert box.snapshot() == before


def test_a_stale_attempt_cannot_retire_its_successor(box):
    """reset → 同名の Worker が再 pull。古い試行 X の退役は、新しい試行 Y を殺さない (card も枠も変わらない)。"""
    x = take(box)
    box.reset()
    y = take(box)
    assert x != y
    before = box.snapshot()
    p = retire(box, "--execution", x)
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert box.snapshot() == before
    assert state(box)[0] == "in_progress" and box.card()["current_execution_id"] == y
    assert box.slot(AGENT) == f"{MISSION}:{TID}"


def test_the_execution_id_decides_even_when_the_generation_is_identical(box):
    """世代 (started_at) では区別できない後任 (同じ値) でも、ID が違えば不一致 (§2.2: ID が優先)。"""
    x = take(box)
    gen_x = box.card()["started_at"]
    box.reset()
    y = take(box)
    become_successor(box, execution_id=y, started_at=gen_x)       # Y の世代を X と同じ値にする
    before = box.snapshot()
    p = retire(box, "--execution", x)
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert box.snapshot() == before
    p = retire(box, "--execution", x, "--started-at", gen_x)      # 世代が一致していても ID が違えば不一致
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert box.snapshot() == before
    assert state(box)[0] == "in_progress"


def test_the_execution_id_wins_over_a_stale_generation_in_the_other_direction(box):
    x = take(box)
    gen_x = box.card()["started_at"]
    box.reset()
    y = take(box)
    p = retire(box, "--execution", y, "--started-at", gen_x)      # 世代は古いが ID は今の試行
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert box.card()["current_execution_id"] == y and state(box)[3:] == ("failed", "RETIRED")


@pytest.mark.parametrize("argv", [
    ["--execution", ""],
    ["--execution", "   "],
    ["--execution", "", "--started-at", "2026-10-01T00:00:00Z"],
    ["--started-at", ""],
    [],
])
def test_an_empty_or_missing_claim_is_refused_with_exit_1_and_nothing_is_written(box, argv):
    """空の明示指定を世代だけの経路・名乗りなしに倒さない (01b G3)。`--execution ""` に有効な世代を添えても同じ。"""
    take(box)
    gen = box.card()["started_at"]
    if argv[-2:] == ["--started-at", "2026-10-01T00:00:00Z"]:
        argv = argv[:-1] + [gen]                         # 世代は今の card のもの。それでも空の --execution で止まる
    before = box.snapshot()
    p = retire(box, *argv)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert box.snapshot() == before


def test_a_resend_of_a_retire_that_already_closed_the_attempt_is_a_success_and_writes_nothing(box):
    """watchdog が card の書き込みの後・progress の更新の前に死んで打ち直す場合 (§4.4 の RETIRED 行)。"""
    xid = take(box)
    assert retire(box, "--execution", xid).returncode == 0
    before = box.snapshot()
    p = retire(box, "--execution", xid)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert "idempotent" in p.stdout
    assert box.snapshot() == before


def test_a_resend_after_a_different_end_is_refused(box):
    xid = take(box)
    assert run(box, "done", TID, "r", "--mission", MISSION, "--execution", xid).returncode == 0
    before = box.snapshot()
    p = retire(box, "--execution", xid)
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert box.snapshot() == before


def test_a_legacy_card_is_retired_by_generation_and_refuses_an_execution(box):
    e3.legacy_in_progress(box, "t002", AGENT)
    gen = box.card("t002")["started_at"]
    before = box.snapshot()
    assert retire(box, "--execution", OTHER_ID, tid="t002").returncode == 3
    assert box.snapshot() == before
    p = retire(box, "--started-at", gen, tid="t002")
    assert p.returncode == 0, (p.stdout, p.stderr)
    m = box.card("t002")
    assert m["status"] == "pending" and m["worker"] is None
    assert "current_execution_id" not in m and "execution_status" not in m      # 試行を発明しない


def test_a_card_taken_over_by_old_code_is_retired_by_generation_not_by_the_stale_id(box):
    """旧コードの pull が取り直した card (DETACHED (a))。X の欄は前の試行のもの —— X では退役できず、世代でだけ。
    X の欄は触らない (閉じるのは次の reserve / `update --close-execution`)。"""
    x = take(box)
    gen2 = "2026-10-02T08:00:00.000000Z"
    set_card(box, started_at=gen2)
    set_identity(box, started_at=gen2, execution_id=None)
    before = box.snapshot()
    assert retire(box, "--execution", x).returncode == 3
    assert box.snapshot() == before
    p = retire(box, "--started-at", gen2)
    assert p.returncode == 0, (p.stdout, p.stderr)
    m = box.card()
    assert (m["status"], m["worker"], m["started_at"]) == ("pending", None, None)
    assert (m["execution_status"], m.get("execution_end_code")) == ("running", None)   # X の欄はそのまま


def test_a_card_with_broken_execution_fields_is_not_retired(box):
    take(box)
    set_card(box, execution_status="bogus")
    gen = box.card()["started_at"]
    before = box.snapshot()
    for argv in (["--started-at", gen], ["--execution", box.card()["current_execution_id"]]):
        p = retire(box, *argv)
        assert p.returncode == 3, (p.stdout, p.stderr)
        assert box.snapshot() == before


def test_the_error_text_never_carries_card_content(box):
    secret = e3.SECRET
    take(box)
    set_card(box, title=secret, needs_director_reason=secret)
    p = retire(box, "--execution", OTHER_ID)
    assert p.returncode == 3 and secret not in p.stdout + p.stderr


# ---------------------------------------------------------------------------
# 2. update --reset が試行を閉じる
# ---------------------------------------------------------------------------

def test_reset_fails_a_running_attempt_as_reset_by_director(box):
    xid = take(box)
    p = update_reset(box)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert "Removed stale assignment" in p.stdout                 # 従来の出力
    assert state(box) == ("pending", None, None, "failed", "RESET_BY_DIRECTOR")
    assert box.slot(AGENT) is None and box.identity(AGENT) is None
    rec = box.record(xid)
    assert (rec["status"], rec["end_code"]) == ("failed", "RESET_BY_DIRECTOR")
    assert len([r for r in audit_rows_for(box, "update") if r["result"] == "ok"]) == 1      # 書き込みごとに 1 行


def test_reset_releases_a_reserved_attempt(box):
    take(box)
    set_card(box, execution_status="reserved")
    assert update_reset(box).returncode == 0
    assert state(box) == ("pending", None, None, "released", "RESET_BY_DIRECTOR")


def test_the_attempt_after_a_reset_is_a_new_one_and_the_old_id_is_stale(box):
    x = take(box)
    assert update_reset(box).returncode == 0
    y = take(box)
    assert y != x and box.card()["execution_count"] == 2
    p = run(box, "done", TID, "r", "--mission", MISSION, "--execution", x)
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_NOT_CURRENT"
    assert state(box)[0] == "in_progress"


def test_reset_closes_a_card_whose_attempt_was_left_by_old_code_as_abandoned(box):
    take(box)
    set_card(box, started_at="2026-10-02T08:00:00.000000Z")      # DETACHED (a)
    assert update_reset(box).returncode == 0
    assert state(box) == ("pending", None, None, "failed", "ABANDONED_OUTSIDE_CONTROLLER")


def test_reset_of_a_legacy_card_changes_the_task_only(box):
    e3.legacy_in_progress(box, "t002", AGENT)
    assert update_reset(box, "t002").returncode == 0
    m = box.card("t002")
    assert (m["status"], m["worker"], m["started_at"]) == ("pending", None, None)
    assert "current_execution_id" not in m


def test_reset_keeps_the_other_options_working_together(box):
    xid = take(box)
    p = update_reset(box, TID, "--priority", "low", "--status", "blocked")
    assert p.returncode == 0, (p.stdout, p.stderr)
    m = box.card()
    assert (m["status"], m["priority"], m["worker"]) == ("blocked", "low", None)
    assert (m["current_execution_id"], m["execution_status"], m["execution_end_code"]) == (
        xid, "failed", "RESET_BY_DIRECTOR")                       # 後から書く更新で試行の欄を巻き戻さない


def test_reset_of_a_card_with_broken_execution_fields_is_not_blocked(box):
    """壊れた card を片付ける出口は塞がない (新しい lib が今のコードより厳しくならない)。試行の欄は触らない。"""
    take(box)
    set_card(box, execution_status="bogus")
    p = update_reset(box)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert "試行の欄が壊れています" in p.stderr
    m = box.card()
    assert (m["status"], m["worker"], m["started_at"]) == ("pending", None, None)
    assert m["execution_status"] == "bogus"


def test_reset_does_not_remove_a_successors_slot_for_the_same_name(box):
    """reset は card の worker の枠を撤去する。同名の別 task を指す枠は消さない (今までと同じ警告)。"""
    take(box)
    with e3.store.transaction(box.queue, op="seed", actor="test") as t:
        t.write_card(MISSION, "t002", e3.sc.card("t002", "in_progress", AGENT, e3.sc.GEN), e3.sc.body())
        t.publish_assignment(AGENT, MISSION, "t002", e3.sc.GEN)
    assert update_reset(box).returncode == 0
    assert box.slot(AGENT) == f"{MISSION}:t002"


# ---------------------------------------------------------------------------
# 3. 今の運用が E4a の後も通る (plan review の追記)
# ---------------------------------------------------------------------------

def test_the_cutover_review_task_closing_flow_works_on_a_card_that_was_never_pulled(box):
    p = run(box, "update", "t002", "--status", "in_progress", "--reset", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, (p.stdout, p.stderr)
    p = run(box, "done", "t002", "reviewed", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert box.card("t002")["status"] == "done"


@pytest.mark.parametrize("before", ["running", "done"])
def test_the_cutover_review_task_closing_flow_works_on_a_card_with_an_earlier_attempt(box, before):
    xid = take(box)
    if before == "done":
        assert run(box, "done", TID, "r", "--mission", MISSION, "--execution", xid).returncode == 0
    p = run(box, "update", TID, "--status", "in_progress", "--reset", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, (p.stdout, p.stderr)
    m = box.card()
    assert (m["status"], m["worker"]) == ("in_progress", None)
    assert m["execution_status"] in ("failed", "completed")       # 前の試行は terminal のまま
    p = run(box, "done", TID, "reviewed", "--mission", MISSION, agent="Director")       # フラグなし
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert box.card()["status"] == "done"
    done_rows = [r for r in audit_rows_for(box, "done") if r["result"] == "ok"]
    assert done_rows[-1]["caller_check"] == "no_execution"


def _publish_orphan(box, agent=AGENT, task=TID):
    with e3.store.transaction(box.queue, op="seed", actor="test") as t:
        t.publish_assignment(agent, MISSION, task, "2026-10-02T07:00:00.000000Z")


@pytest.mark.parametrize("how", ["done", "retire", "reset"])
def test_the_directors_reap_of_an_orphan_slot_still_works_after_an_attempt_was_closed(box, how):
    xid = take(box)
    if how == "done":
        assert run(box, "done", TID, "r", "--mission", MISSION, "--execution", xid).returncode == 0
    elif how == "retire":
        assert retire(box, "--execution", xid).returncode == 0
    else:
        assert update_reset(box).returncode == 0
    if how == "done":
        pass                                                      # 手放した task を指す枠は、手書き・旧コードの取り残しで生じる
    _publish_orphan(box)
    before_card = box.card_text()
    p = run(box, "reap-orphan-assignment", AGENT, agent="Director")
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert box.slot(AGENT) is None
    assert box.card_text() == before_card                         # reap は projection だけ (card・試行に触れない)


def test_the_reap_leaves_a_live_attempts_slot_alone(box):
    take(box)
    p = run(box, "reap-orphan-assignment", AGENT, agent="Director")
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert box.slot(AGENT) == f"{MISSION}:{TID}"
    assert state(box)[3] == "running"


def test_the_dispatchers_reap_names_the_dispatcher_as_actor_in_the_audit_log(box):
    """dispatcher は subprocess の env に AGENT_NAME=dispatcher を入れる (§8)。ここでは同じ env で打って行を見る。"""
    xid = take(box)
    assert run(box, "done", TID, "r", "--mission", MISSION, "--execution", xid).returncode == 0
    _publish_orphan(box)
    p = run(box, "reap-orphan-assignment", AGENT, "--no-wait", agent="dispatcher")
    assert p.returncode == 0, (p.stdout, p.stderr)
    # reap は card を書かない: 回復 R-2 の行はあっても actor は dispatcher
    rows = [r for r in box.audit_rows() if r.get("op") == "reap-orphan-assignment"]
    assert all(r["actor"] == "dispatcher" for r in rows), rows


# ---------------------------------------------------------------------------
# 4. 退役 marker (lib_retirement)
# ---------------------------------------------------------------------------

def test_the_marker_reader_binds_only_an_active_attempt(box):
    xid = take(box)
    assert rt.read_task_execution_id(box.queue, MISSION, TID) == xid
    set_card(box, started_at="2026-10-02T08:00:00.000000Z")            # DETACHED (a): 古い ID を束縛しない
    assert rt.read_task_execution_id(box.queue, MISSION, TID) is None
    e3.legacy_in_progress(box, "t002", AGENT)
    assert rt.read_task_execution_id(box.queue, MISSION, "t002") is None          # legacy: 世代だけ
    assert rt.read_task_execution_id(box.queue, MISSION, "t999") is rt.UNKNOWN_EXECUTION_ID   # 読めない
    set_card(box, execution_status="bogus", started_at=None)
    assert rt.read_task_execution_id(box.queue, MISSION, TID) is rt.UNKNOWN_EXECUTION_ID     # 欄が壊れている


def test_a_terminal_attempt_is_not_bound(box):
    xid = take(box)
    assert run(box, "done", TID, "r", "--mission", MISSION, "--execution", xid).returncode == 0
    assert rt.read_task_execution_id(box.queue, MISSION, TID) is None


def test_assignment_verdict_prefers_the_execution_id_over_the_generation(box):
    xid = take(box)
    gen = box.card()["started_at"]
    verdict = rt.assignment_execution_verdict
    assert verdict(box.queue, AGENT, MISSION, TID, gen, xid)[0] == rt.EXEC_SAME
    assert verdict(box.queue, AGENT, MISSION, TID, gen, OTHER_ID)[0] == rt.EXEC_OTHER      # 世代が同じでも ID が違えば別
    assert verdict(box.queue, AGENT, MISSION, TID, "2026-01-01T00:00:00Z", xid)[0] == rt.EXEC_SAME   # ID が同じなら世代が違っても
    assert verdict(box.queue, AGENT, MISSION, TID, None, xid)[0] == rt.EXEC_SAME
    assert verdict(box.queue, AGENT, MISSION, TID, None, None)[0] == rt.EXEC_UNREADABLE     # 証拠なしは保留
    # 旧形式の枠 (identity に ID なし) は世代で読める (§2.2 の末尾)。世代が無ければ読めないに倒す
    set_identity(box, execution_id=None)
    assert verdict(box.queue, AGENT, MISSION, TID, gen, xid)[0] == rt.EXEC_SAME
    assert verdict(box.queue, AGENT, MISSION, TID, None, xid)[0] == rt.EXEC_UNREADABLE
    assert verdict(box.queue, AGENT, MISSION, TID, "2026-01-01T00:00:00Z", xid)[0] == rt.EXEC_OTHER


class FakeMux:
    def __init__(self, windows):
        self.windows = dict(windows)
        self.sent = []

    def available(self):
        return True

    def list(self, suffix=None):
        names = [n for n, pid in self.windows.items() if pid is None or _alive(pid)]
        return [n for n in names if not suffix or n.endswith(suffix)]

    def pid(self, name):
        pid = self.windows.get(name)
        return pid if pid is not None and _alive(pid) else None

    def send(self, name, text):
        self.sent.append((name, text))
        return name in self.list()

    def kill(self, name):
        self.windows.pop(name, None)
        return True


def _alive(pid):
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2] != "Z"


class Rig:
    """実 `RetirementExecutor` + 実 plan.sh (隔離コピー) + FakeMux + `sleep` の Worker 役。"""

    def __init__(self, box):
        self.box = box
        self.procs = []
        self.logs = []
        self.notes = []
        self.calls = []
        self.registry = box.root / "registry"
        self.pane_pid = self.spawn()
        mux_dir = self.registry / "mux"
        mux_dir.mkdir(parents=True, exist_ok=True)
        (mux_dir / f"{WINDOW}.firstseen").write_text(str(time.time()))
        self.mux = FakeMux({WINDOW: self.pane_pid})
        self.ex = rt.RetirementExecutor(
            registry_dir=self.registry, repo_root=box.root, mux=self.mux, repo_identity_check=lambda: True,
            log=self.logs.append, notify=lambda msg: (self.notes.append(msg), True)[1],
            plan_sh=box.plan_sh, queue_dir=box.queue, grace_period=0, kill_delay=0, run_command=self._run)

    def spawn(self):
        proc = subprocess.Popen(["sleep", "300"])
        self.procs.append(proc)
        return proc.pid

    def _run(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        e = dict(self.box.env)
        for k in ("CREWVIA_QUEUE", "CREWVIA_REPO_ROOT", "AGENT_NAME"):
            if k in env:
                e[k] = env[k]
        p = subprocess.run(argv, env=e, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def request(self, reason="timeout", **kw):
        return self.ex.request(AGENT, WINDOW, reason, mission=MISSION, task_id=TID, **kw)

    def marker(self):
        return json.loads(rt.request_path(self.registry, AGENT).read_text())

    def drive(self, cycles=20):
        for _ in range(cycles):
            self.ex.process_all()
            if not self.ex.has_marker(AGENT):
                return True
        return False

    def cleanup(self):
        for proc in self.procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


@pytest.fixture
def rig(box):
    r = Rig(box)
    yield r
    r.cleanup()


def _without_records(snapshot):
    return {k: v for k, v in snapshot.items() if "/executions/" not in k}


def _retire_calls(rig):
    return [c for c in rig.calls if len(c[0]) > 2 and c[0][2] == "retire"]


def test_the_marker_and_the_cleanup_are_bound_to_the_execution(rig):
    box = rig.box
    xid = take(box)
    assert rig.request()
    assert rig.marker().get("task_execution_id") == xid
    assert rig.drive(), rig.logs
    assert state(box) == ("pending", None, None, "failed", "RETIRED")
    assert box.slot(AGENT) is None
    (argv, env), = _retire_calls(rig)
    assert argv[3] == TID and "--execution" in argv and xid in argv
    assert "--started-at" not in argv                                # 両方は渡さない
    assert env["AGENT_NAME"] == "watchdog"
    assert not _alive(rig.pane_pid)
    rows = [r for r in audit_rows_for(box, "retire") if r["result"] == "ok"]
    assert rows and rows[-1]["actor"] == "watchdog" and rows[-1]["execution_id"] == xid


def test_the_progress_file_carries_the_execution_forward_after_the_request_is_gone(rig):
    xid = take(rig.box)
    assert rig.request()
    rig.ex.process_all()                                             # → notified (progress を書く)
    prog = json.loads(rt.progress_path(rig.registry, AGENT).read_text())
    assert prog.get("task_execution_id") == xid
    rt.request_path(rig.registry, AGENT).unlink()                    # request が先に消えても progress が証拠を持つ
    os.kill(rig.pane_pid, signal.SIGKILL)
    assert rig.drive(), rig.logs
    assert state(rig.box)[3:] == ("failed", "RETIRED")
    (argv, _env), = _retire_calls(rig)
    assert xid in argv


def test_an_old_format_marker_without_the_execution_is_read_and_cleans_up_by_generation(rig):
    """E4a は旧形式の marker (`task_execution_id` なし) を読める。確認の 0 件を merge 条件にしない理由 (§7)。"""
    box = rig.box
    take(box)
    assert rig.request()
    req_path = rt.request_path(rig.registry, AGENT)
    doc = json.loads(req_path.read_text())
    gen = doc["task_started_at"]
    del doc["task_execution_id"]
    req_path.write_text(json.dumps(doc))
    assert rig.drive(), rig.logs
    assert state(box) == ("pending", None, None, "failed", "RETIRED")      # 世代の照合でも試行は閉じる
    (argv, _env), = _retire_calls(rig)
    assert "--started-at" in argv and gen in argv and "--execution" not in argv


def test_a_marker_that_names_a_legacy_card_binds_the_generation_only(rig):
    box = rig.box
    e3.legacy_in_progress(box, "t002", AGENT)
    window = WINDOW
    assert rig.ex.request(AGENT, window, "timeout", mission=MISSION, task_id="t002")
    doc = rig.marker()
    assert doc["task_execution_id"] is None and doc["task_started_at"] == e3.sc.GEN
    assert rig.drive(), rig.logs
    (argv, _env), = _retire_calls(rig)
    assert "--started-at" in argv and "--execution" not in argv
    assert box.card("t002")["status"] == "pending"


@pytest.mark.parametrize("same_generation", [False, True])
def test_a_successor_with_the_same_name_is_not_retired_after_the_worker_died(rig, same_generation):
    """猶予期間のあいだに reset + 同名の後任の再 pull。後始末は後任の試行を殺さない・消さない。
    世代が (衝突して) 同じ値でも、ID が違えば後任のものとして守られる。"""
    box = rig.box
    xid = take(box)
    gen_x = box.card()["started_at"]
    assert rig.request()
    rig.ex.process_all()
    os.kill(rig.pane_pid, signal.SIGKILL)
    for _ in range(100):
        if not _alive(rig.pane_pid):
            break
        time.sleep(0.05)
    rig.ex.process_all()                                             # → terminated (後始末だけが残る)
    become_successor(box, started_at=gen_x if same_generation else None)
    before = _without_records(box.snapshot())
    assert rig.drive(), rig.logs
    # 後任の record を (card から) 作る回復 R-5 は projection の修復で、状態の書き換えではない
    assert _without_records(box.snapshot()) == before, "後任の card・枠を書き換えた"
    assert state(box)[0] == "in_progress" and box.card()["current_execution_id"] == OTHER_ID
    assert box.slot(AGENT) == f"{MISSION}:{TID}"
    assert any("no queue cleanup owed" in line for line in rig.logs), rig.logs
    assert xid != OTHER_ID


def test_a_successor_with_the_same_generation_is_not_signalled_at_the_guard(rig):
    """kill の前の guard (assignment の試行の照合)。ID が違えば世代が同じでも後任 —— SIGTERM を送らない。"""
    box = rig.box
    xid = take(box)
    gen_x = box.card()["started_at"]
    assert rig.request()
    become_successor(box, started_at=gen_x)                          # 先任が死ぬ前に、同じ世代の後任に入れ替わった
    assert rig.drive(), rig.logs
    assert _alive(rig.pane_pid), "後任の Worker に SIGTERM が届いた"
    assert state(box)[0] == "in_progress" and box.card()["current_execution_id"] == OTHER_ID
    assert not _retire_calls(rig)
    assert xid != OTHER_ID


def test_a_resend_of_the_cleanup_after_the_card_was_written_still_settles_the_marker(rig):
    """card を書いた retire の成功が watchdog に届かなかった (rc≠0 とみなされ打ち直し) 形: 2 回目は冪等の成功。"""
    box = rig.box
    xid = take(box)
    assert rig.request()
    rig.ex.process_all()
    os.kill(rig.pane_pid, signal.SIGKILL)
    for _ in range(100):
        if not _alive(rig.pane_pid):
            break
        time.sleep(0.05)
    first = {"n": 0}
    real = rig._run

    def flaky(argv, env):
        rc, out = real(argv, env)                                    # 本物の retire は card を書き終えている
        first["n"] += 1
        return (1, "lost the reply") if first["n"] == 1 else (rc, out)

    rig.ex.run_command = flaky
    assert rig.drive(cycles=30), rig.logs
    assert first["n"] >= 2
    assert state(box) == ("pending", None, None, "failed", "RETIRED") and box.card()["current_execution_id"] == xid


@pytest.mark.parametrize("reason", ["idle", "no-task", "target_dir_wait", "usage_limit"])
def test_task_less_retirements_need_no_queue_cleanup_and_touch_no_card(rig, reason):
    """task を持たない退役 (idle / no-task / TARGET_DIR 待機 / 利用枠切れの後始末) は plan.sh を呼ばず、card に触れない。"""
    box = rig.box
    take(box)
    with e3.store.transaction(box.queue, op="seed", actor="test") as t:      # idle: 枠が無い
        t.retire_assignment(AGENT, MISSION, TID, None)
    before = box.snapshot()
    assert rig.ex.request(AGENT, WINDOW, reason)
    assert "task_execution_id" not in rig.marker() or rig.marker()["task_execution_id"] is None
    assert rig.drive(), rig.logs
    assert not _retire_calls(rig)
    assert box.snapshot() == before
