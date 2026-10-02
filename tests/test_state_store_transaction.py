"""transaction / Txn / recover / 監査ログの単体テスト (原案 STATE-01/02/04/06 / §10.1)。"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

import state_store_scenarios as sc   # scripts/ を sys.path に足す (lib より先)
import lib_state_store as store

M, A, G = sc.MISSION, sc.AGENT, sc.GEN


@pytest.fixture
def q(tmp_path):
    return tmp_path / "queue"


def audit_rows(q):
    rows = []
    for p in sorted((q / "audit").glob("transitions-*.jsonl")):
        rows += [json.loads(line) for line in p.read_text().splitlines()]
    return rows


# ---- ロック -------------------------------------------------------------------

def test_lock_failure_is_explicit_and_writes_nothing(q):
    q.mkdir()
    (q / ".lock").mkdir()                  # ロックファイルを開けない
    with pytest.raises(store.LockFailed):
        with store.transaction(q, op="x", actor="t") as t:
            t.write_state({"active_missions": [], "default_mission": None})
    assert not (q / "state.yaml").exists() and not (q / "audit").exists()


def test_nonblocking_busy_raises_lockbusy_and_writes_nothing(q):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time; sys.path.insert(0,'scripts'); import lib_state_store as s\n"
         "with s.transaction(sys.argv[1], op='h', actor='h'):\n"
         "    print('held', flush=True); time.sleep(30)", str(q)],
        stdout=subprocess.PIPE, text=True, cwd=sc.REPO_ROOT, start_new_session=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(store.LockBusy):
            with store.transaction(q, op="x", actor="t", nonblocking=True) as t:
                t.write_state({"active_missions": ["nope"], "default_mission": None})
        assert not (q / "state.yaml").exists()
        assert audit_rows(q) == []
    finally:
        os.killpg(holder.pid, 9)
        holder.wait()
    # 保持者が死ねば取れる (flock は fd と一緒に消える)
    with store.transaction(q, op="x", actor="t", nonblocking=True):
        pass


def test_nested_transaction_raises_immediately_instead_of_deadlocking(q):
    done = []

    def go():
        with store.transaction(q, op="outer", actor="t"):
            with pytest.raises(store.NestedTransaction):
                with store.transaction(q, op="inner", actor="t"):
                    pass
            done.append("raised")
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(10)
    assert not th.is_alive(), "入れ子が待って止まった (deadlock)"
    assert done == ["raised"]
    # 外側を抜けたら取り直せる (入れ子の記録が残らない)
    with store.transaction(q, op="again", actor="t"):
        pass


def test_second_thread_waits_for_the_first_not_nested_error(q):
    order = []
    started = threading.Event()

    def first():
        with store.transaction(q, op="a", actor="t"):
            started.set()
            import time
            time.sleep(0.3)
            order.append("first")

    def second():
        started.wait()
        with store.transaction(q, op="b", actor="t"):
            order.append("second")
    ts = [threading.Thread(target=first), threading.Thread(target=second)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert order == ["first", "second"]


def test_lock_released_after_exception_and_no_body_audit_row(q):
    with pytest.raises(RuntimeError):
        with store.transaction(q, op="boom", actor="t") as t:
            t.record(M, "t001", "pending", "in_progress")
            raise RuntimeError("x")
    assert audit_rows(q) == []                    # 例外で抜けたら本体の行は書かない
    with store.transaction(q, op="ok", actor="t", nonblocking=True):
        pass


def test_systemexit_from_a_refusal_also_skips_the_body_row(q):
    with pytest.raises(SystemExit):
        with store.transaction(q, op="done", actor="t") as t:
            t.record(M, "t001", "in_progress", "done")
            raise SystemExit(3)                   # plan.sh の die()
    assert audit_rows(q) == []


# ---- 読み直し -----------------------------------------------------------------

def test_reads_inside_the_lock_see_what_a_previous_holder_wrote(q):
    sc.seed(q, "pull")
    with store.transaction(q, op="a", actor="t") as t:
        meta, b = t.load_card(M, "t001")
        meta["title"] = "changed"
        t.write_card(M, "t001", meta, b)
    with store.transaction(q, op="b", actor="t") as t:
        assert t.load_card(M, "t001")[0]["title"] == "changed"


def test_load_card_not_found_vs_unreadable(q):
    sc.seed(q, "pull")
    with store.transaction(q, op="a", actor="t") as t:
        with pytest.raises(store.CardNotFound):
            t.load_card(M, "t099")
        p = q / "missions" / M / "tasks" / "t001.md"
        p.write_text("---\ntitle: [unterminated\n")
        with pytest.raises(store.CardUnreadable):
            t.load_card(M, "t001")
        p.write_bytes(b"\xff\xfe not utf8")
        with pytest.raises(store.CardUnreadable):
            t.load_card(M, "t001")
        p.unlink()
        os.mkfifo(p)                               # 通常ファイルでない — 待たずに拒否
        with pytest.raises(store.CardUnreadable):
            t.load_card(M, "t001")


def test_card_id_field_disagreeing_with_filename_is_unreadable_and_unwritable(q):
    sc.seed(q, "pull")
    with store.transaction(q, op="a", actor="t") as t:
        (q / "missions" / M / "tasks" / "t002.md").write_text(
            "---\nid: t001\ntitle: x\nstatus: pending\n---\n\nbody\n")
        with pytest.raises(store.CardUnreadable):
            t.load_card(M, "t002")
        with pytest.raises(store.InvalidName):
            t.write_card(M, "t002", sc.card("t001"), "b")


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "x\0y"])
def test_invalid_slug_and_agent_names_are_rejected_before_any_write(q, bad):
    with store.transaction(q, op="a", actor="t") as t:
        with pytest.raises(store.InvalidName):
            t.load_card(bad, "t001")
        with pytest.raises(store.InvalidName):
            t.publish_assignment(bad, M, "t001", G)
    assert not (q / "assignments").exists()


def test_state_missing_is_default_but_unreadable_is_an_error(q):
    with store.transaction(q, op="a", actor="t") as t:
        assert t.load_state() == {"active_missions": [], "default_mission": None}
        (q / "state.yaml").write_text("this line has no colon\n")
        with pytest.raises(store.StoreReadError):
            t.load_state()
        (q / "state.yaml").unlink()
        os.mkfifo(q / "state.yaml")
        with pytest.raises(store.StoreReadError):
            t.load_state()


# ---- assignment (projection) -----------------------------------------------------

X1 = "ex-" + "1" * 32
X2 = "ex-" + "2" * 32


def test_an_identity_without_an_id_is_not_this_attempts_slot_and_is_never_removed(q):
    """E4b: 世代 (started_at) では照合しない。ID の無い identity (E2 より前・旧コードの publish) は、同じ世代の値でも
    名指しした試行のものとは言えない (SUCCESSOR・撤去しない)。ID なしの名指し (None = いま card が示す実行) は枠が task を指せば MINE。"""
    with store.transaction(q, op="a", actor="t") as t:
        t.publish_assignment(A, M, "t001", G)                       # execution_id なし
        assert t.classify_assignment(A, M, "t001", X1) == store.ASSIGN_SUCCESSOR
        assert t.retire_assignment(A, M, "t001", X1) == store.ASSIGN_SUCCESSOR
        assert (q / "assignments" / A).exists()
        assert t.classify_assignment(A, M, "t001", None) == store.ASSIGN_MINE
        os.unlink(q / "assignments" / f"{A}.identity")
        assert t.classify_assignment(A, M, "t001", X1) == store.ASSIGN_UNVERIFIABLE   # identity が無い


def test_publish_writes_identity_before_body_and_retire_uses_classify(q):
    order = []
    with store.transaction(q, op="a", actor="t") as t:
        store.FAULT_HOOK = lambda p, path: order.append(os.path.basename(path)) if p == "atomic:replaced" else None
        try:
            t.publish_assignment(A, M, "t001", G, execution_id=X1)
        finally:
            store.FAULT_HOOK = None
        assert order == [f"{A}.identity", A]
        assert t.classify_assignment(A, M, "t001", X1) == store.ASSIGN_MINE
        assert t.classify_assignment(A, M, "t001", X2) == store.ASSIGN_SUCCESSOR
        assert t.classify_assignment(A, M, "t002", None) == store.ASSIGN_OTHER_TASK
        # 後任 (別の試行) は消さない
        assert t.retire_assignment(A, M, "t001", X2) == store.ASSIGN_SUCCESSOR
        assert (q / "assignments" / A).exists()
        assert t.retire_assignment(A, M, "t001", X1) == store.ASSIGN_MINE
        assert not (q / "assignments" / A).exists() and not (q / "assignments" / f"{A}.identity").exists()
        assert t.classify_assignment(A, M, "t001", X1) == store.ASSIGN_ABSENT


def test_unreadable_slot_is_unverifiable_not_absent(q):
    with store.transaction(q, op="a", actor="t") as t:
        (q / "assignments").mkdir()
        os.mkfifo(q / "assignments" / A)
        assert t.classify_assignment(A, M, "t001", None) == store.ASSIGN_UNVERIFIABLE
        assert t.retire_assignment(A, M, "t001", None) == store.ASSIGN_UNVERIFIABLE   # 消さない


# ---- 監査ログ -----------------------------------------------------------------

def test_body_row_fields_and_no_content_leak(q):
    sc.seed(q, "pull")
    secret = "SECRET-RESULT-BODY sk-abc123"
    monkey_env = os.environ.copy()
    os.environ["TASKVIA_TOKEN"] = "tok-xyz"
    try:
        with store.transaction(q, op="pull", actor=A) as t:
            meta, _b = t.load_card(M, "t001")
            meta.update(status="in_progress", worker=A, started_at=G)
            t.write_card(M, "t001", meta, f"## Description\n{secret}\n\n## Result\n{secret}\n")
            t.publish_assignment(A, M, "t001", G)
            t.record(M, "t001", "pending", "in_progress", G)
    finally:
        os.environ.clear(); os.environ.update(monkey_env)
    rows = [r for r in audit_rows(q) if r["op"] == "pull"]
    assert len(rows) == 1
    r = rows[0]
    assert set(r) == {"ts", "txn_id", "op", "mission", "task", "actor", "pid", "from_status", "to_status",
                      "generation", "execution_id", "result", "files"}
    assert (r["mission"], r["task"], r["actor"], r["from_status"], r["to_status"], r["generation"]) == \
        (M, "t001", A, "pending", "in_progress", G)
    assert r["execution_id"] is None and r["result"] == "ok" and r["pid"] == os.getpid()
    assert sorted(r["files"]) == sorted([f"missions/{M}/tasks/t001.md",
                                         f"assignments/{A}.identity", f"assignments/{A}"])
    raw = "".join(p.read_text() for p in (q / "audit").glob("*.jsonl"))
    assert "SECRET" not in raw and "sk-abc123" not in raw and "tok-xyz" not in raw


def test_audit_log_is_utc_dated_and_appends(q):
    for i in range(3):
        with store.transaction(q, op=f"o{i}", actor="t") as t:
            t.record(M, None, None, None)
    files = list((q / "audit").glob("transitions-*.jsonl"))
    assert len(files) == 1 and len(files[0].read_text().splitlines()) == 3
    import re
    assert re.fullmatch(r"transitions-\d{8}\.jsonl", files[0].name)


def test_audit_failure_does_not_stop_the_transition_and_is_visible(q, capsys):
    sc.seed(q, "pull")
    (q / "audit").write_text("not a directory")          # audit/ を作れない
    with store.transaction(q, op="pull", actor=A) as t:
        meta, b = t.load_card(M, "t001")
        meta["status"] = "in_progress"
        t.write_card(M, "t001", meta, b)
        t.record(M, "t001", "pending", "in_progress")
    assert sc.read_meta(q, M, "t001")["status"] == "in_progress"      # 遷移は止まらない
    assert t.audit_failures == 1
    err = capsys.readouterr().err
    assert "audit log を書けませんでした" in err                       # 黙って捨てない


# ---- recover ------------------------------------------------------------------

def recover(q, scope):
    with store.transaction(q, op="next", actor="t") as t:
        return t.recover(scope)


def results(reps):
    return [r.result for r in reps]


def test_recovery_writes_nothing_when_consistent(q):
    sc.seed(q, "done")
    before = {p: p.read_bytes() for p in q.rglob("*") if p.is_file()}
    assert recover(q, sc.SCOPES["done"]) == []
    after = {p: p.read_bytes() for p in q.rglob("*") if p.is_file() and p.parent.name != "audit"}
    assert {p: b for p, b in before.items() if p.parent.name != "audit"} == after


def test_r1_never_writes_the_authoritative_card(q):
    sc.seed(q, "pull")
    with store.transaction(q, op="x", actor="t") as t:
        meta, b = t.load_card(M, "t001")
        meta.update(status="in_progress", worker=A, started_at=G)
        t.write_card(M, "t001", meta, b)
    card = q / "missions" / M / "tasks" / "t001.md"
    before = card.read_bytes()
    assert results(recover(q, sc.SCOPES["pull"])) == ["repaired:R-1"]
    assert card.read_bytes() == before
    assert results(recover(q, sc.SCOPES["pull"])) == []              # 冪等


def _own(q, tid, agent, gen=G, status="in_progress"):
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, tid, sc.card(tid, status, agent, gen), sc.body())


def test_r1_refused_when_owner_holds_two_in_progress_cards_even_across_missions(q):
    sc.seed(q, "pull")
    _own(q, "t001", A)
    with store.transaction(q, op="x", actor="t") as t:            # 名指ししない別 mission (一時停止中)
        t.write_mission("m-other", {"title": "o", "slug": "m-other", "status": "in_progress",
                                    "next_task_id": 2})
        t.write_card("m-other", "t001", sc.card("t001", "in_progress", A, "g2"), sc.body())
    reps = recover(q, sc.SCOPES["pull"])
    assert results(reps) == ["reported:duplicate_owner"]
    assert "m-other/t001" in reps[0].detail and f"{M}/t001" in reps[0].detail
    assert not (q / "assignments" / A).exists()                    # 書かない
    rows = [r for r in audit_rows(q) if r["op"] == "recover"]
    assert [r["result"] for r in rows] == ["reported:duplicate_owner"]


def test_r1_refused_when_any_card_is_unreadable_owner_unprovable(q):
    sc.seed(q, "pull")
    _own(q, "t001", A)
    (q / "missions" / M / "tasks" / "t009.md").write_text("garbage without frontmatter")
    reps = recover(q, sc.SCOPES["pull"])
    assert results(reps) == ["reported:owner_unprovable"]
    assert f"{M}/t009" in reps[0].detail
    assert not (q / "assignments" / A).exists()


def test_recovery_reports_but_does_not_raise_or_add_refusals(q, capsys):
    """表に無い食い違いは書かずに報告する。raise しない (出口を減らさない)。"""
    sc.seed(q, "pull")
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t001", sc.card("t001", "ready_for_verification", A, G), sc.body())
    reps = recover(q, sc.SCOPES["pull"])
    assert results(reps) == ["reported:holding_without_assignment"]
    assert "reported: reported:holding_without_assignment" in capsys.readouterr().err
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t001", sc.card("t001", "in_progress", None, None), sc.body())
    assert results(recover(q, sc.SCOPES["pull"])) == ["reported:in_progress_without_worker"]
    assert not (q / "assignments").exists()


def test_r2_does_not_touch_blocked_card_assignment(q):
    """R-2 を「holding でない status すべて」にしない (Worker が動いている blocked を消さない)。"""
    sc.seed(q, "done")
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t001", sc.card("t001", "blocked", A, G, blocked_reason="wait"), sc.body())
    reps = recover(q, sc.SCOPES["done"])
    assert results(reps) == ["reported:assignment_on_non_orphan_status"]
    assert (q / "assignments" / A).exists()


@pytest.mark.parametrize("status", ["done", "verified", "skipped", "failed", "needs_director"])
def test_r2_removes_orphan_for_finished_statuses(q, status):
    sc.seed(q, "done")
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t001", sc.card("t001", status, A, G), sc.body())
    assert results(recover(q, sc.SCOPES["done"])) == ["repaired:R-2"]
    assert not (q / "assignments" / A).exists()


def test_r2_does_not_remove_a_successors_assignment(q):
    """後任が同じ task を pull し直していれば (card は in_progress) 消さない。"""
    sc.seed(q, "done")
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t001", sc.card("t001", "in_progress", A, "successor-gen"), sc.body())
    reps = recover(q, sc.SCOPES["done"])
    assert not any(r.repaired for r in reps)
    assert (q / "assignments" / A).exists()


def test_r2_then_r1_when_slot_points_at_a_finished_task(q):
    sc.seed(q, "done")
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t001", sc.card("t001", "done", A, G), sc.body())          # 旧 task は終了
        t.write_card(M, "t002", sc.card("t002", "in_progress", A, "g2"), sc.body())  # 新 task を pull 済み
    reps = recover(q, store.Scope(cards=((M, "t002"),), agents=(A,)))
    assert results(reps) == ["repaired:R-2", "repaired:R-1"]
    assert sc.slot_text(q, A) == f"{M}:t002" and sc.identity(q, A)["started_at"] == "g2"


def test_r1_not_written_when_slot_points_at_another_live_task(q):
    sc.seed(q, "done")
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t002", sc.card("t002", "in_progress", A, "g2"), sc.body())
    reps = recover(q, store.Scope(cards=((M, "t002"),), agents=(A,)))
    assert "reported:agent_slot_busy" in results(reps) or "reported:duplicate_owner" in results(reps)
    assert sc.slot_text(q, A) == f"{M}:t001"


def test_unreadable_slot_is_neither_removed_nor_overwritten(q):
    sc.seed(q, "pull")
    _own(q, "t001", A)
    (q / "assignments").mkdir()
    os.mkfifo(q / "assignments" / A)
    assert results(recover(q, sc.SCOPES["pull"])) == ["reported:assignment_unverifiable"]
    assert (q / "assignments" / A).exists()


def test_r3_advances_next_task_id_and_reports_leftover(q):
    sc.seed(q, "add")
    with store.transaction(q, op="x", actor="t") as t:
        t.write_card(M, "t003", sc.card("t003"), sc.body())          # add が card だけ書いて落ちた
    reps = recover(q, sc.SCOPES["add"])
    assert results(reps) == ["repaired:R-3"] and "t003" in reps[0].detail
    assert recover(q, sc.SCOPES["add"]) == []
    with store.transaction(q, op="x", actor="t") as t:
        assert int(t.load_mission(M)["next_task_id"]) == 4


def test_r4_drops_archived_slug_and_moves_default(q):
    sc.seed(q, "archive")
    os.rename(q / "missions" / M, _mk(q / "archive") / M)
    reps = recover(q, sc.SCOPES["archive"])
    assert results(reps) == ["repaired:R-4"]
    with store.transaction(q, op="x", actor="t") as t:
        st = t.load_state()
    assert st["active_missions"] == [] and st["default_mission"] is None
    assert recover(q, sc.SCOPES["archive"]) == []


def _mk(p):
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_r4_does_nothing_if_mission_still_exists_or_archive_missing(q):
    sc.seed(q, "archive")
    assert recover(q, sc.SCOPES["archive"]) == []                      # missions/ がまだ在る
    import shutil
    shutil.rmtree(q / "missions" / M)                                    # どちらにも無い: 推測しない
    assert recover(q, sc.SCOPES["archive"]) == []


def test_recovery_rows_are_written_immediately_even_if_the_body_then_dies(q):
    """P2-b: 回復の行は with の出口を待たない。本体が die (SystemExit) しても残る。"""
    sc.seed(q, "pull")
    _own(q, "t001", A)
    with pytest.raises(SystemExit):
        with store.transaction(q, op="pull", actor=A) as t:
            t.recover(sc.SCOPES["pull"])
            t.record(M, "t001", "pending", "in_progress")
            raise SystemExit(3)
    rows = audit_rows(q)
    assert [r["result"] for r in rows if r["op"] == "recover"] == ["repaired:R-1"]
    assert [r for r in rows if r["op"] == "pull"] == []                 # 本体の行だけが無い
    rec = [r for r in rows if r["op"] == "recover"][0]
    assert sorted(rec["files"]) == sorted([f"assignments/{A}.identity", f"assignments/{A}"])


def test_recovery_survives_the_process_dying_right_after_its_write(q):
    """修復の書き込みの後・行の追記の前で落ちても、行が欠けるのは最大 1 行で状態は正しい。"""
    sc.seed(q, "pull")
    _own(q, "t001", A)
    script = (
        "import sys, os, signal; sys.path.insert(0,'scripts'); import lib_state_store as s\n"
        "def hook(p, path):\n"
        "    if p == 'audit:begin': os.kill(os.getpid(), signal.SIGKILL)\n"
        "s.FAULT_HOOK = hook\n"
        f"with s.transaction(sys.argv[1], op='n', actor='t') as t:\n"
        f"    t.recover(s.Scope(cards=(('{M}','t001'),), agents=('{A}',)))\n")
    p = subprocess.run([sys.executable, "-c", script, str(q)], cwd=sc.REPO_ROOT)
    assert p.returncode == -9
    assert sc.slot_text(q, A) == f"{M}:t001"                            # 修復は済んでいる
    assert audit_rows(q) == []                                          # 行は欠けた (許容)
    assert recover(q, sc.SCOPES["pull"]) == []                          # 再回復は何もしない


def test_recovery_scope_does_not_read_outside_it(q):
    """範囲外の壊れた card は回復に影響しない (1 枚の事故で全体を止めない)。"""
    sc.seed(q, "pull")
    (q / "missions" / M / "tasks" / "t077.md").write_text("garbage")
    assert recover(q, sc.SCOPES["pull"]) == []                          # pull は t001 だけ名指し・pending


def test_diagnose_reports_without_writing_and_counts_residue(q):
    sc.seed(q, "pull")
    _own(q, "t001", A)
    (q / "missions" / M / "tasks" / ".t005.md.tmp.zzz").write_text("half")
    before = {p: p.read_bytes() for p in q.rglob("*") if p.is_file()}
    found = store.diagnose(q)
    kinds = sorted(f.kind for f in found)
    assert kinds == ["stale_tmp", "would-repair:R-1"]
    assert {p: p.read_bytes() for p in q.rglob("*") if p.is_file()} == before     # 書かない
    assert not (q / "audit").exists()


def test_locked_update_json_rereads_and_never_flattens_unreadable(tmp_path):
    p, lock = tmp_path / "map.json", tmp_path / "map.lock"
    store.locked_update_json(p, lock, lambda d: {**d, "a": 1})
    store.locked_update_json(p, lock, lambda d: {**d, "b": 2})
    assert json.loads(p.read_text()) == {"a": 1, "b": 2}
    p.write_text("{not json")
    with pytest.raises(store.StoreReadError):
        store.locked_update_json(p, lock, lambda d: {"x": 1})
    assert p.read_text() == "{not json"                                   # 上書きしない
