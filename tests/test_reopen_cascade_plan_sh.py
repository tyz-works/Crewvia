"""`plan.sh reopen` と下流の無効化 (vNext 02 PR-F / t036。設計 §4)。

隔離 plan.sh + 隔離 queue (`pull_execution_helpers.Box`)。本番の queue / registry / config には触れない。

* 遷移元・`--branch reuse` 必須・archive 済み mission・上限・理由の検査
* `--cascade none|reviews|all` の 3 値・経路の閉包 (I → A → R で A も戻る)・対象外の完了済み下流は列挙だけ
* 実行中 / ready_for_verification / verifying の下流 (cascade なしで拒否・ありで試行を RESET_BY_DIRECTOR で閉じる)
* 経路上の skipped は拒否 (途中・末端・複数経路の片方 × 3 値)・何も書かない
* 書き込みの各点の kill → 次のコマンドの回復 (intent の実行) は `tests/test_reopen_intent_recovery.py`
* reopen → pull で previous_attempt・reopen → pull → done が同じ pr_number で閉じる
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import MISSION, Box, last_error_code, new_card, run, set_card

import lib_history as hist  # noqa: E402
import lib_state_store as store  # noqa: E402

SKILLS = "code,review,qa,codex-review"
REASON = "I の結果を見直す"


def finish(box, tid, agent="Ren", **done_extra):
    """pull → done。戻り値: 試行の ID。"""
    p = box.pull(agent, tid, skills=SKILLS)
    assert p.returncode == 0, (tid, p.stdout, p.stderr)
    xid = json.loads(p.stdout)["execution_id"]
    args = ["done", tid, "r", "--mission", MISSION, "--execution", xid]
    for k, v in done_extra.items():
        args += [k, v]
    q = run(box, *args, agent=agent)
    assert q.returncode == 0, (tid, q.stderr)
    return xid


def build(tmp_path, spec, name="root"):
    """spec: [(tid, blocked_by, skills)]。t001 は Box が作る。全部 done にして返す (依存順)。"""
    box = Box(tmp_path / name, tasks=("t001",))
    for tid, blocked, skills in spec:
        if tid == "t001":
            set_card(box, "t001", skills=skills, blocked_by=blocked)
        else:
            new_card(box, tid, "pending", blocked_by=blocked, skills=skills)
    return box


def finish_all(box, tids):
    for tid in tids:
        finish(box, tid)


def reopen(box, tid="t001", *extra, reason=REASON, branch="reuse", agent="Director"):
    args = ["reopen", tid, "--mission", MISSION]
    if reason is not None:
        args += ["--reason", reason]
    if branch is not None:
        args += ["--branch", branch]
    return run(box, *args, *extra, agent=agent)


def status(box, tid):
    return box.card(tid)["status"]


def count(box, tid):
    return box.card(tid).get("reopen_count") or 0


def ledger_dir(box, tid):
    return box.queue / "missions" / MISSION / "history" / tid


def ledger_files(box, tid, kind=None):
    d = ledger_dir(box, tid)
    names = sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) if d.is_dir() else []
    return [n for n in names if kind is None or f"-{kind}-" in n]


def ledger(box, tid, name):
    return hist.parse((ledger_dir(box, tid) / name).read_text(), source=name)


def store_check(box):
    p = run(box, "store-check", "--mission", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    return p.stdout


CHAIN = [("t001", [], ["code"]), ("t002", ["t001"], ["code"]), ("t003", ["t002"], ["review"])]


@pytest.fixture
def chain(tmp_path):
    """I(t001) → A(t002, code) → R(t003, review)。全部 done。"""
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002", "t003"))
    return box


# ---------------------------------------------------------------------------
# 入口の検査
# ---------------------------------------------------------------------------

def test_reopen_returns_a_done_task_to_pending_and_records_the_reason(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    xid = finish(box, "t001")
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    card = box.card()
    assert card["status"] == "pending" and card["worker"] is None and card["started_at"] is None
    assert card.get("completed_at") is None and card["reopen_count"] == 1
    assert card["current_execution_id"] == xid and card["execution_status"] == "completed"
    [name] = ledger_files(box, "t001", "reopen")
    fields, _ = ledger(box, "t001", name)
    assert fields["kind"] == "reopen" and fields["execution_id"] == xid and fields["reason"] == REASON
    assert fields["reopen_count"] in (None, 0) and not fields.get("root")
    assert REASON not in json.dumps(box.audit_rows(), ensure_ascii=False)       # 理由は台帳にだけ


def test_branch_reuse_is_required_and_nothing_is_written_without_it(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    before = box.snapshot()
    for branch in (None, "new", ""):
        p = reopen(box, branch=branch)
        assert p.returncode != 0 and "--branch" in p.stderr, (branch, p.stderr)
        assert box.snapshot() == before
    assert reopen(box, "t001", "--cascade", "bogus").returncode != 0
    assert box.snapshot() == before


def test_reopen_active_attempt_with_an_owner_points_at_reset_not_close_execution(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    set_card(box, status="in_progress", worker="Ren", execution_status="running", execution_end_code=None)
    p = reopen(box)                                  # (record の追従は回復の仕事なので snapshot は比べない)
    assert p.returncode == 3, p.stderr
    assert last_error_code(p.stderr) == "ATTEMPT_ACTIVE"
    assert "plan.sh update t001 --status pending --reset" in p.stderr
    assert "先に `plan.sh update t001 --close-execution`" not in p.stderr


def test_reopen_active_attempt_without_an_owner_points_at_close_execution(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    set_card(box, execution_status="running", execution_end_code=None)       # done のまま試行が running = DETACHED
    p = reopen(box)
    assert p.returncode == 3, p.stderr
    assert last_error_code(p.stderr) == "ATTEMPT_ACTIVE"
    assert "plan.sh update t001 --close-execution" in p.stderr


@pytest.mark.parametrize("src", ["pending", "failed", "needs_director", "in_progress"])
def test_reopen_from_other_statuses_is_refused_and_writes_nothing(tmp_path, src):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    set_card(box, status=src, worker=None)
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3, p.stderr
    assert last_error_code(p.stderr) in ("NOT_REOPENABLE", "ATTEMPT_ACTIVE")
    assert box.snapshot() == before and not ledger_files(box, "t001", "reopen")


@pytest.mark.parametrize("src", ["done", "verified", "skipped"])
def test_reopen_accepts_done_verified_and_skipped(tmp_path, src):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    set_card(box, status=src)
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert status(box, "t001") == "pending" and count(box, "t001") == 1


def test_reopen_of_a_task_without_attempt_fields_is_refused(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    new_card(box, "t002", "done")                   # 試行の欄なし (この設計より前の card)
    before = box.snapshot()
    p = reopen(box, "t002")
    assert p.returncode == 3 and last_error_code(p.stderr) == "NOT_REOPENABLE"
    assert box.snapshot() == before


def test_reopen_of_an_archived_mission_is_refused_with_exit_3(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    p = run(box, "archive", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    before = box.snapshot()
    for extra in ((), ):
        q = reopen(box)
        assert q.returncode == 3 and last_error_code(q.stderr) == "MISSION_ARCHIVED", q.stderr
    q = run(box, "reopen", "t001", "--reason", REASON, "--branch", "reuse", agent="Director")   # --mission なしでも
    assert q.returncode == 3 and last_error_code(q.stderr) == "MISSION_ARCHIVED", q.stderr
    assert box.snapshot() == before


def test_reason_is_checked_like_retry(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    before = box.snapshot()
    for bad in ("", "  ", "a\nb", "a b", "x" * 501):
        p = reopen(box, reason=bad)
        assert p.returncode != 0, bad
        assert box.snapshot() == before
    p = reopen(box, reason=None)
    assert p.returncode != 0 and box.snapshot() == before


def test_attempt_cap_applies_at_the_entrance_and_over_cap_passes(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    set_card(box, max_attempts=1)
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "ATTEMPT_CAP"
    assert box.snapshot() == before
    q = reopen(box, "t001", "--over-cap", "もう一度だけ")
    assert q.returncode == 0, q.stderr
    [name] = ledger_files(box, "t001", "reopen")
    assert ledger(box, "t001", name)[0]["over_cap"] == "もう一度だけ"


def test_an_unfinished_reopen_rerun_is_not_reopenable_once_complete(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    assert reopen(box).returncode == 0
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "NOT_REOPENABLE"
    assert box.snapshot() == before and count(box, "t001") == 1


def test_reopen_reactivates_a_done_mission(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001")
    mfile = box.queue / "missions" / MISSION / "mission.yaml"
    mfile.write_text(mfile.read_text().replace("status: in_progress", "status: done"))
    assert "status: done" in mfile.read_text()
    assert reopen(box).returncode == 0
    assert "status: in_progress" in mfile.read_text()


# ---------------------------------------------------------------------------
# cascade の 3 値・閉包
# ---------------------------------------------------------------------------

def test_cascade_reviews_closes_the_path_so_the_intermediate_task_returns_too(chain):
    p = reopen(chain)
    assert p.returncode == 0, p.stderr
    for tid in ("t001", "t002", "t003"):
        assert status(chain, tid) == "pending" and count(chain, tid) == 1, tid
    # 下流の台帳: reopen・root と root_reopen_count (= I の reopen_count + 1)
    for tid in ("t002", "t003"):
        [name] = ledger_files(chain, tid, "reopen")
        fields, _ = ledger(chain, tid, name)
        assert fields["root"] == "t001" and fields["root_reopen_count"] == 1 and fields["reason"] == REASON
    # I から遠い順 (R → A → I): 台帳の ended_at が I より前
    ended = {t: ledger(chain, t, ledger_files(chain, t, "reopen")[0])[0]["ended_at"] for t in ("t001", "t002", "t003")}
    assert ended["t003"] <= ended["t002"] <= ended["t001"]
    assert "t002" in p.stdout and "t003" in p.stdout


def test_cascade_none_changes_only_the_root_and_lists_the_stale_downstream(chain):
    p = reopen(chain, "t001", "--cascade", "none")
    assert p.returncode == 0, p.stderr
    assert status(chain, "t001") == "pending"
    assert status(chain, "t002") == "done" and status(chain, "t003") == "done"
    assert count(chain, "t002") == 0 and not ledger_files(chain, "t002", "reopen")
    assert "古い結果に基づく下流" in p.stdout and "t002" in p.stdout and "t003" in p.stdout


def test_cascade_reviews_leaves_a_non_review_branch_alone_and_lists_it(tmp_path):
    spec = CHAIN + [("t004", ["t001"], ["code"])]            # I → B (非 review・R への経路上にない)
    box = build(tmp_path, spec)
    finish_all(box, ("t001", "t002", "t003", "t004"))
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert status(box, "t004") == "done" and count(box, "t004") == 0
    assert "古い結果に基づく下流" in p.stdout and "t004" in p.stdout


def test_cascade_all_returns_every_finished_downstream(tmp_path):
    spec = CHAIN + [("t004", ["t001"], ["code"])]
    box = build(tmp_path, spec)
    finish_all(box, ("t001", "t002", "t003", "t004"))
    p = reopen(box, "t001", "--cascade", "all")
    assert p.returncode == 0, p.stderr
    assert all(status(box, t) == "pending" and count(box, t) == 1 for t in ("t001", "t002", "t003", "t004"))


def test_downstream_codex_review_skill_is_selected_too(tmp_path):
    spec = [("t001", [], ["code"]), ("t002", ["t001"], ["codex-review"])]
    box = build(tmp_path, spec)
    finish(box, "t001", **{"--no-pr": "n/a"})
    finish(box, "t002")
    assert reopen(box).returncode == 0
    assert status(box, "t002") == "pending" and count(box, "t002") == 1


def test_pending_downstream_is_left_alone_and_released_deps_of_the_root_is_removed(tmp_path):
    spec = [("t001", [], ["code"]), ("t002", ["t001"], ["code"])]
    box = build(tmp_path, spec)
    finish(box, "t001")
    set_card(box, "t002", released_deps=["t001"])
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    c = box.card("t002")
    assert c["status"] == "pending" and c.get("reopen_count") in (None, 0) and "released_deps" not in c
    assert not ledger_files(box, "t002")


# ---------------------------------------------------------------------------
# 実行中の下流
# ---------------------------------------------------------------------------

def running_downstream(box, tid="t003", agent="Mika"):
    p = box.pull(agent, tid, skills=SKILLS)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["execution_id"]


@pytest.fixture
def chain_with_running_review(tmp_path):
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002"))
    xid = running_downstream(box)
    return box, xid


def test_running_downstream_without_cascade_is_refused_and_writes_nothing(chain_with_running_review):
    box, _xid = chain_with_running_review
    before = box.snapshot()
    p = reopen(box, "t001", "--cascade", "none")
    assert p.returncode == 3 and last_error_code(p.stderr) == "CASCADE_ACTIVE_DOWNSTREAM", p.stderr
    assert "t003" in p.stderr and box.snapshot() == before and count(box, "t001") == 0


@pytest.mark.parametrize("state", ["in_progress", "ready_for_verification", "verifying"])
def test_running_downstream_is_reset_by_director_and_the_workers_next_report_is_refused(tmp_path, state):
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002"))
    xid = running_downstream(box)
    if state != "in_progress":
        set_card(box, "t003", status=state)
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    c = box.card("t003")
    assert c["status"] == "pending" and c["execution_status"] == "failed"
    assert c["execution_end_code"] == "RESET_BY_DIRECTOR" and c["worker"] is None
    assert status(box, "t001") == "pending" and status(box, "t002") == "pending"   # A も閉包で戻る
    q = run(box, "done", "t003", "r", "--mission", MISSION, "--execution", xid, agent="Mika")
    assert q.returncode == 3 and last_error_code(q.stderr) in ("EXECUTION_NOT_CURRENT", "EXECUTION_ALREADY_TERMINAL")
    [rname] = ledger_files(box, "t003", "reset")
    f, _ = ledger(box, "t003", rname)
    assert f["root"] == "t001" and f["root_reopen_count"] == 1 and f["reason"] == REASON


# ---------------------------------------------------------------------------
# 経路上の skipped は拒否
# ---------------------------------------------------------------------------

def skipped_box(tmp_path, spec, skipped, name="root"):
    box = build(tmp_path, spec, name)
    finish_all(box, [t for t, _b, _s in spec])                 # 依存順に done にしてから skipped に書き換える
    for tid in skipped:
        set_card(box, tid, status="skipped")
    return box


@pytest.mark.parametrize("cascade", ["reviews", "all"])
def test_skipped_in_the_middle_of_the_path_is_refused(tmp_path, cascade):
    box = skipped_box(tmp_path, CHAIN, {"t002"})               # I → A(skipped) → R(done)
    before = box.snapshot()
    p = reopen(box, "t001", "--cascade", cascade)
    assert p.returncode == 3 and last_error_code(p.stderr) == "CASCADE_SKIPPED_ON_PATH", p.stderr
    assert "t001 -> t002" in p.stderr
    assert box.snapshot() == before and count(box, "t001") == 0 and not ledger_files(box, "t001", "reopen")


def test_skipped_in_the_middle_is_not_a_problem_with_cascade_none(tmp_path):
    box = skipped_box(tmp_path, CHAIN, {"t002"})
    p = reopen(box, "t001", "--cascade", "none")
    assert p.returncode == 0, p.stderr
    assert status(box, "t001") == "pending" and status(box, "t003") == "done"


def test_skipped_at_the_end_of_the_path_is_refused(tmp_path):
    spec = [("t001", [], ["code"]), ("t002", ["t001"], ["review"])]
    box = skipped_box(tmp_path, spec, {"t002"})                # I → R(skipped, review)
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "CASCADE_SKIPPED_ON_PATH", p.stderr
    assert box.snapshot() == before
    assert reopen(box, "t001", "--cascade", "none").returncode == 0


def test_skipped_on_one_of_two_paths_is_refused(tmp_path):
    spec = [("t001", [], ["code"]), ("t002", ["t001"], ["code"]), ("t003", ["t001"], ["code"]),
            ("t004", ["t002", "t003"], ["review"])]
    box = skipped_box(tmp_path, spec, {"t003"})                # I → A → R と I → B(skipped) → R
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "CASCADE_SKIPPED_ON_PATH", p.stderr
    assert box.snapshot() == before


def test_skipped_off_the_path_to_any_selected_task_is_not_a_problem(tmp_path):
    spec = CHAIN + [("t004", ["t001"], ["code"])]
    box = skipped_box(tmp_path, spec, {"t004"})                # 非 review の葉が skipped (reviews では選ばれない)
    assert reopen(box).returncode == 0
    assert status(box, "t004") == "skipped"
    box2 = skipped_box(tmp_path, spec, {"t004"}, name="root2")
    assert reopen(box2, "t001", "--cascade", "all").returncode == 3     # all では選ばれて経路の末端になる


# ---------------------------------------------------------------------------
# 上限・試行の記録のない下流
# ---------------------------------------------------------------------------

def test_downstream_over_the_attempt_cap_is_refused_unless_over_cap(chain):
    set_card(chain, "t003", max_attempts=1)
    before = chain.snapshot()
    p = reopen(chain)
    assert p.returncode == 3 and last_error_code(p.stderr) == "ATTEMPT_CAP" and "t003" in p.stderr
    assert chain.snapshot() == before
    assert reopen(chain, "t001", "--over-cap", "了解").returncode == 0


def test_downstream_without_attempt_fields_cannot_be_written_to_the_ledger(tmp_path):
    spec = [("t001", [], ["code"])]
    box = build(tmp_path, spec)
    new_card(box, "t002", "done", blocked_by=["t001"], skills=["review"])
    finish(box, "t001")
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "CASCADE_NO_EXECUTION", p.stderr
    assert box.snapshot() == before


# ---------------------------------------------------------------------------
# roll forward・kill
# ---------------------------------------------------------------------------

def _fork_reopen(box, argv, kill_at):
    """fork した子で本物の `cmd_reopen(argv)` を走らせ、lib の書き込み点の `kill_at` 番目で SIGKILL。
    戻り値: (SIGKILL で落ちたか, 書き込み点の数)。"""
    ns = box.namespace()
    ns["SUBCOMMAND"] = "reopen"
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 3
        try:
            os.environ.update(box.proc_env("Director"))
            calls = [0]

            def hook(_point, _path):
                calls[0] += 1
                if calls[0] == kill_at:
                    os.kill(os.getpid(), signal.SIGKILL)

            store.FAULT_HOOK = hook
            try:
                ns["cmd_reopen"](argv)
            except SystemExit:
                pass
            os.write(w, str(calls[0]).encode())
            code = 0
        finally:
            os._exit(code)
    os.close(w)
    _pid, st = os.waitpid(pid, 0)
    data = os.read(r, 32).decode()
    os.close(r)
    return os.WIFSIGNALED(st) and os.WTERMSIG(st) == signal.SIGKILL, int(data or 0)


def _restore(box, saved):
    shutil.rmtree(box.queue)
    shutil.copytree(saved, box.queue)


# ---------------------------------------------------------------------------
# reopen → pull → done
# ---------------------------------------------------------------------------

def test_reopen_then_pull_shows_previous_attempt_and_done_closes_with_the_same_pr(tmp_path):
    box = build(tmp_path, [("t001", [], ["code"])])
    finish(box, "t001", **{"--pr": "123"})
    assert box.card()["pr_number"] == 123
    assert reopen(box).returncode == 0
    assert box.card()["pr_number"] == 123 and box.card()["task_slug"]
    p = box.pull("Ren", "t001", skills=SKILLS)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["attempt"] == 2 and data["previous_attempt"]["attempt"] == 1
    q = run(box, "done", "t001", "r2", "--pr", "123", "--mission", MISSION, "--execution", data["execution_id"], agent="Ren")
    assert q.returncode == 0, q.stderr
    c = box.card()
    assert c["status"] == "done" and c["pr_number"] == 123 and c["reopen_count"] == 1


# ---------------------------------------------------------------------------
# PR #308 Codex 1 巡目 (t042): pending の review・reset_set の上限・mission の再活性化の順
# ---------------------------------------------------------------------------

TERMINAL = ("done", "verified")
A_STATES = ("done", "verified", "pending", "in_progress")
R_STATES = ("pending", "blocked", "in_progress", "done", "verified")


def _table_box(tmp_path, a_state, r_state):
    """I(t001, done) → A(t002, code) → R(t003, review)。全部 done にしてから A / R の status だけを場面に合わせる
    (試行の欄は終わった試行のまま = 台帳に写せる)。"""
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002", "t003"))
    if a_state != "done":
        set_card(box, "t002", status=a_state)
    if r_state != "done":
        set_card(box, "t003", status=r_state)
    return box


@pytest.mark.parametrize("cascade", ["reviews", "all"])
@pytest.mark.parametrize("r_state", R_STATES)
@pytest.mark.parametrize("a_state", A_STATES)
def test_no_review_can_be_pulled_before_the_root_completes_again(tmp_path, a_state, r_state, cascade):
    """中間 A の status × R の status × cascade。reopen が成功したら、R を I の再完了より前に pull できる組み合わせは残らない
    (A を done のまま残すと、直接の依存だけを見る pull が R を古い結果で通す)。拒否されるなら何も書かない。"""
    box = _table_box(tmp_path, a_state, r_state)
    before = box.snapshot()
    p = reopen(box, "t001", "--cascade", cascade)
    if p.returncode != 0:
        assert p.returncode == 3, (a_state, r_state, p.stderr)
        assert box.snapshot() == before
        return
    assert status(box, "t001") == "pending"
    assert status(box, "t002") not in TERMINAL, (a_state, r_state, status(box, "t002"))
    if status(box, "t003") == "pending":
        q = box.pull("Mika", "t003", skills=SKILLS)
        assert q.returncode != 0, ("I が pending なのに R を pull できた", a_state, r_state, q.stdout[:200])
    assert status(box, "t003") not in TERMINAL


def test_pending_review_alone_closes_a_done_intermediate(tmp_path):
    """指摘 1 の再現そのもの: I(done) → A(done, code) → R(pending, review)。既定の reopen で A も戻る。"""
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002"))
    assert status(box, "t003") == "pending"
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert [status(box, t) for t in ("t001", "t002", "t003")] == ["pending"] * 3
    assert count(box, "t002") == 1 and count(box, "t003") == 0         # R は pending のまま (reopen_count を足さない)
    assert box.pull("Mika", "t003", skills=SKILLS).returncode != 0


@pytest.mark.parametrize("a_state", ["done", "verified", "pending"])
def test_cascade_none_lists_a_pending_downstream_that_could_run_on_a_stale_intermediate(tmp_path, a_state):
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002"))
    if a_state != "done":
        set_card(box, "t002", status=a_state)
    p = reopen(box, "t001", "--cascade", "none")
    assert p.returncode == 0, p.stderr
    assert status(box, "t002") == a_state and status(box, "t003") == "pending"      # none は何も戻さない
    listed = "再完了を待たずに" in p.stdout and "t003" in p.stdout.split("再完了を待たずに", 1)[1]
    assert listed == (a_state in TERMINAL), p.stdout


def test_cascade_none_with_a_running_intermediate_is_still_refused(tmp_path):
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001",))
    running_downstream(box, "t002")
    before = box.snapshot()
    p = reopen(box, "t001", "--cascade", "none")
    assert p.returncode == 3 and last_error_code(p.stderr) == "CASCADE_ACTIVE_DOWNSTREAM"
    assert box.snapshot() == before


# ---- 上限: reopen が pending に戻す全部の集合が同じ関数を通る ----

def _over_cap_box(tmp_path, which):
    """`which` の task だけ max_attempts=1 (execution_count=1 で上限)。running の下流 D(t004) を含む。"""
    spec = CHAIN + [("t004", ["t001"], ["review"])]
    box = build(tmp_path, spec)
    finish_all(box, ("t001", "t002"))
    running_downstream(box, "t004")                       # t004: 実行中の review (reset_set)
    finish(box, "t003")
    set_card(box, which, max_attempts=1)
    return box


@pytest.mark.parametrize("which", ["t001", "t002", "t003", "t004"])
def test_every_set_reopen_returns_to_pending_goes_through_the_same_cap_check(tmp_path, which):
    """I (t001)・中間 (t002)・末端の done (t003)・実行中の下流 = reset_set (t004) のどれが上限でも、--over-cap なしは
    ATTEMPT_CAP (exit 3・何も書かない)。--over-cap で通り、理由は戻した全 task の台帳に残る (上限の task のぶん)。"""
    box = _over_cap_box(tmp_path, which)
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "ATTEMPT_CAP", (which, p.stderr)
    assert which in p.stderr and box.snapshot() == before
    q = reopen(box, "t001", "--over-cap", "了解")
    assert q.returncode == 0, q.stderr
    assert [status(box, t) for t in ("t001", "t002", "t003", "t004")] == ["pending"] * 4
    for kind in ("reopen", "reset"):
        for name in ledger_files(box, which, kind):
            fields, _ = ledger(box, which, name)
            if fields.get("root") == "t001" or which == "t001":
                assert fields["over_cap"] == "了解", (which, kind, fields)
    others = [t for t in ("t001", "t002", "t003", "t004") if t != which]
    for t in others:                                       # 上限に達していない task の台帳には over_cap を書かない
        for name in ledger_files(box, t):
            assert ledger(box, t, name)[0].get("over_cap") is None, (which, t, name)


def test_reset_downstream_at_the_cap_keeps_the_over_cap_reason_in_its_reset_ledger(tmp_path):
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002"))
    running_downstream(box)
    set_card(box, "t003", max_attempts=1)
    assert reopen(box).returncode == 3
    assert reopen(box, "t001", "--over-cap", "了解").returncode == 0
    [name] = ledger_files(box, "t003", "reset")
    fields, _ = ledger(box, "t003", name)
    assert fields["over_cap"] == "了解" and fields["root"] == "t001"


# ---- mission の再活性化は I の card より前 ----

def _mission_file(box):
    return box.queue / "missions" / MISSION / "mission.yaml"


def _mark_mission_done(box):
    f = _mission_file(box)
    f.write_text(f.read_text().replace("status: in_progress", "status: done"))
    assert "status: done" in f.read_text()


def test_a_refused_rerun_does_not_touch_a_done_mission_when_the_root_never_reopened(tmp_path):
    """root が pending でも reopen の台帳が無い (reopen を書き終えた形でない) なら、拒否は mission を書き換えない。"""
    box = build(tmp_path, [("t001", [], ["code"])])
    _mark_mission_done(box)
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "NOT_REOPENABLE", p.stderr
    assert box.snapshot() == before and "status: done" in _mission_file(box).read_text()


# ---------------------------------------------------------------------------
# PR #308 Codex 2 巡目 (t046): 経路上の failed / 語彙の外の status・途中で止まった cascade の収束
# ---------------------------------------------------------------------------

import lib_task_status as _status  # noqa: E402

#: 中間 A の status の全語彙 (lib_task_status.TASK_STATUSES) + 語彙に無い手書きの値
ALL_A_STATES = tuple(sorted(_status.TASK_STATUSES)) + ("cancelled",)


def _vocab_box(tmp_path, a_state, released):
    """I(t001, done) → A(t002, code) → R(t003, review, pending)。A を `a_state` に、`released` なら R が A を解除済み。"""
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002"))
    if a_state != "done":
        set_card(box, "t002", status=a_state)
    if released:
        set_card(box, "t003", released_deps=["t002"])
    return box


def test_the_vocabulary_list_is_the_whole_vocabulary():
    assert set(ALL_A_STATES) - {"cancelled"} == set(_status.TASK_STATUSES)


@pytest.mark.parametrize("cascade", ["none", "reviews", "all"])
@pytest.mark.parametrize("released", [False, True])
@pytest.mark.parametrize("a_state", ALL_A_STATES)
def test_no_combination_leaves_r_pullable_before_the_root_completes_again(tmp_path, a_state, released, cascade):
    """中間 A の status (語彙の全部 + 語彙の外) × R が A を released_deps で解除しているか × cascade 3 値。
    reopen が成功した (none を除く) なら、I が pending の間に R を pull できる組み合わせは残らない。拒否なら何も書かない。
    none は R を戻さないので pull できてしまいうるが、その場合は出力に必ず「再完了を待たずに」と R を列挙する。
    例外は A が skipped (終端 = R は reopen の前から I と無関係に pull できた。reopen が変えた窓ではない)。"""
    box = _vocab_box(tmp_path, a_state, released)
    before = box.snapshot()
    p = reopen(box, "t001", "--cascade", cascade)
    if p.returncode != 0:
        # 語彙の外の status (cancelled) は store が card を読めず STATE_INVALID (exit 1)。他の拒否は exit 3
        assert p.returncode == (1 if a_state == "cancelled" else 3), (a_state, released, cascade, p.stderr)
        assert box.snapshot() == before, "拒否は何も書かない"
        return
    assert a_state != "cancelled"
    assert status(box, "t001") == "pending"
    pullable = status(box, "t003") == "pending" and box.pull("Mika", "t003", skills=SKILLS).returncode == 0
    if cascade == "none":
        if pullable and a_state != "skipped":
            assert "再完了を待たずに" in p.stdout and "t003" in p.stdout.split("再完了を待たずに", 1)[1], \
                (a_state, released, p.stdout)
    else:
        assert not pullable, ("I が pending なのに R を pull できた", a_state, released, cascade)


def test_the_positive_control_r_is_pullable_when_a_failed_and_released_under_a_done_root(tmp_path):
    """上の表の陽性対照: I が done のまま A が failed で R が A を解除していれば、R は本当に pull できる (= 表の assert は空振りでない)。"""
    box = _vocab_box(tmp_path, "failed", True)
    assert box.pull("Mika", "t003", skills=SKILLS).returncode == 0


@pytest.mark.parametrize("released", [False, True])
@pytest.mark.parametrize("cascade", ["reviews", "all"])
@pytest.mark.parametrize("a_state", ["failed"])
def test_failed_on_the_path_is_refused_with_the_route(tmp_path, a_state, released, cascade):
    box = _vocab_box(tmp_path, a_state, released)
    before = box.snapshot()
    p = reopen(box, "t001", "--cascade", cascade)
    assert p.returncode == 3 and last_error_code(p.stderr) == "CASCADE_FAILED_ON_PATH", p.stderr
    assert "t001 -> t002" in p.stderr and "t002" in p.stderr
    assert box.snapshot() == before and count(box, "t001") == 0 and not ledger_files(box, "t001", "reopen")


def test_a_status_outside_the_vocabulary_is_unreadable_and_nothing_is_written(tmp_path):
    """語彙に無い status (手書きの cancelled) は store が card を読めない = STATE_INVALID (exit 1)。何も書かず、通らない。"""
    box = _vocab_box(tmp_path, "cancelled", True)
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 1 and last_error_code(p.stderr) == "STATE_INVALID", p.stderr
    assert box.snapshot() == before


def test_failed_at_the_end_of_the_path_is_not_refused(tmp_path):
    """経路の端の failed (対象の review 自身が failed) は、それに依存する下流が無い = 走る下流が無いので拒否しない。"""
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002"))
    set_card(box, "t003", status="failed")
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert [status(box, t) for t in ("t001", "t002", "t003")] == ["pending", "pending", "failed"]


def test_failed_off_the_path_to_any_selected_task_is_not_a_problem(tmp_path):
    spec = CHAIN + [("t004", ["t001"], ["code"]), ("t005", ["t004"], ["code"])]
    box = build(tmp_path, spec)
    finish_all(box, ("t001", "t002", "t003", "t004"))
    set_card(box, "t004", status="failed")                    # reviews では t004 も t005 も選ばれない
    assert reopen(box).returncode == 0
    assert status(box, "t004") == "failed"


def test_released_upstream_other_than_the_root_is_dropped_when_that_upstream_returns_to_pending(tmp_path):
    """R が (I でなく) 中間 A を解除していて、A が cascade で戻るなら、その解除は根拠が消える。再び failed になっても自動で通さない。"""
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002", "t003"))
    set_card(box, "t003", released_deps=["t002"])
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert [status(box, t) for t in ("t001", "t002", "t003")] == ["pending"] * 3
    assert not box.card("t003").get("released_deps")
    assert "t003" in p.stdout


def test_reopen_output_tells_the_director_how_to_recover_from_a_stop(chain):
    p = reopen(chain)
    assert p.returncode == 0, p.stderr
    assert "plan.sh reopen t001" in p.stdout and "同じ引数で再実行" in p.stdout and "reopen-intent-incomplete" in p.stdout
    assert "intent の続きを完了" in p.stdout                  # 次に queue を書くコマンドが完了する
    # 書き始める前にも stderr に出る (途中で死ぬと成功時の出力は届かない)
    assert "同じ引数で再実行" in p.stderr


def _state(box, tids):
    mfile = box.queue / "missions" / MISSION / "mission.yaml"
    m = "done" if "status: done" in mfile.read_text() else "active"
    out = {"mission": m}
    for t in tids:
        c = box.card(t)
        out[t] = (c["status"], c.get("reopen_count") or 0, tuple(c.get("released_deps") or ()),
                  tuple(sorted(n.split("-")[1] for n in ledger_files(box, t))))
    return out


def _released_matrix_box(tmp_path, r_state, name):
    """I(t001, done) → A(t002, code, done) → R(t003, review)。R は A の release を持つ (A がかつて failed で解除された名残)。"""
    box = build(tmp_path, CHAIN, name=name)
    finish_all(box, ("t001", "t002"))
    if r_state == "done":
        finish(box, "t003")
    set_card(box, "t003", released_deps=["t002"])
    mfile = box.queue / "missions" / MISSION / "mission.yaml"
    mfile.write_text(mfile.read_text().replace("status: in_progress", "status: done"))
    return box


# ---------------------------------------------------------------------------
# PR #308 Codex 4 巡目 (t054): targets は 1 か所・壊れた card があれば拒否
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["in_progress", "ready_for_verification", "verifying"])
def test_a_running_non_review_downstream_closes_the_done_intermediate_before_it(tmp_path, state):
    """指摘 1 の再現そのもの: I(done) → A(done, code) → B(running, code)。既定の reviews で B は reset されるが、B 自身は選ばれていない。
    B を経路の終点に数えないと A が done のまま残り、B が I の再完了より前に pull し直せる。"""
    box = build(tmp_path, [("t001", [], ["code"]), ("t002", ["t001"], ["code"]), ("t003", ["t002"], ["code"])])
    finish_all(box, ("t001", "t002"))
    running_downstream(box, "t003")
    if state != "in_progress":
        set_card(box, "t003", status=state)
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert [status(box, t) for t in ("t001", "t002", "t003")] == ["pending"] * 3
    assert box.pull("Mika", "t003", skills=SKILLS).returncode != 0, "I が pending なのに B を pull し直せた"


def _corrupt_card(box, tid, secret):
    """card の `blocked_by` を list でない形にする (読み取りが `[破損]` に隔離する)。値に secret を仕込む。"""
    import re
    p = box.queue / "missions" / MISSION / "tasks" / f"{tid}.md"
    p.write_text(re.sub(r"^blocked_by: .*$", f"blocked_by: {secret}", p.read_text(), count=1, flags=re.M))


@pytest.mark.parametrize("cascade", [None, "none", "reviews", "all"])
def test_reopen_is_refused_when_a_mission_card_is_unreadable_and_writes_nothing(tmp_path, cascade):
    """指摘 2 の再現そのもの: I → A → R で A が壊れていると、graph から I → A・A → R の辺が消えて R が戻らずに成功していた。
    mission の card のどれかが読めないなら exit 3・何も書かない・壊れた card の id だけを出す (中身は出さない)。"""
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002", "t003"))
    _corrupt_card(box, "t002", "SECRET_BLOCKED_BY_VALUE")
    before = box.snapshot()
    extra = ["--cascade", cascade] if cascade else []
    p = reopen(box, "t001", *extra)
    assert p.returncode == 3 and last_error_code(p.stderr) == "CASCADE_UNREADABLE_CARDS", (p.returncode, p.stderr)
    assert "t002" in p.stderr and "SECRET_BLOCKED_BY_VALUE" not in p.stdout + p.stderr
    assert box.snapshot() == before
    assert status(box, "t001") == "done" and count(box, "t001") == 0


def test_an_unreadable_card_that_is_not_in_the_graph_of_the_root_still_refuses(tmp_path):
    """壊れた card が I と無関係に見えても拒否する (壊れていると blocked_by も読めない = 無関係かは分からない)。"""
    box = build(tmp_path, CHAIN + [("t004", [], ["code"])])
    finish_all(box, ("t001", "t002", "t003"))
    _corrupt_card(box, "t004", "x")
    before = box.snapshot()
    p = reopen(box)
    assert p.returncode == 3 and last_error_code(p.stderr) == "CASCADE_UNREADABLE_CARDS" and box.snapshot() == before


def test_the_same_reopen_succeeds_once_the_card_is_fixed(tmp_path):
    """拒否の出口は card を直すだけ (拒否は何も書かないので、直した後の同じコマンドが通る)。"""
    box = build(tmp_path, CHAIN)
    finish_all(box, ("t001", "t002", "t003"))
    good = (box.queue / "missions" / MISSION / "tasks" / "t002.md").read_text()
    _corrupt_card(box, "t002", "x")
    assert reopen(box).returncode == 3
    (box.queue / "missions" / MISSION / "tasks" / "t002.md").write_text(good)
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert [status(box, t) for t in ("t001", "t002", "t003")] == ["pending"] * 3


def test_retry_and_store_check_do_not_depend_on_the_whole_mission_graph(tmp_path):
    """族の確認: `retry` は task 1 枚だけを見る (graph を作らない) ので、他の card が壊れていても通る。`store-check` の
    reopen-intent-incomplete も graph を使わない (intent の dir を見るだけ)。どちらも `list_tasks()` の置き換えを踏まない。"""
    box = build(tmp_path, CHAIN)
    finish(box, "t001")
    p = box.pull("Ren", "t002", skills=SKILLS)
    xid = json.loads(p.stdout)["execution_id"]
    assert run(box, "fail", "t002", "--mission", MISSION, "--no-head", "x", "--execution", xid, agent="Ren").returncode == 0
    _corrupt_card(box, "t003", "x")
    q = run(box, "retry", "t002", "--mission", MISSION, "--reason", "やり直す", agent="Director")
    assert q.returncode == 0, q.stderr
    assert status(box, "t002") == "pending"
    run(box, "store-check", "--mission", MISSION, agent="Director")             # 落ちない (終了コードは壊れた card の報告で非 0 もありうる)
