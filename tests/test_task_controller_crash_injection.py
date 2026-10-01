"""Task Controller の crash 注入 (vNext 01c E1。受入条件): 各トランザクションの書き込み点で SIGKILL し、次のロック取得
(= 回復 R-1 / R-2 / R-5) で収束する。正本 (card) から projection (record・identity・枠) が再生成できる。

lib は書き込みの各段 (tmp 作成・書き込み・fsync・replace・親 dir fsync・unlink・監査ログ) で `FAULT_HOOK` を呼ぶ。
その k 番目で fork した子が自分に SIGKILL を送る (子は SIGKILL か `os._exit` でしか終わらない)。全点 × REPS 回。

各点で確かめること (execution.md §1.4 / §9.5):

  1. **回復の前でも** card は健全 (試行の欄の整合・`attempt_view` が計算できる)。card がコミット点
  2. 次のロック取得 (別プロセス = この pytest) で `recover()` → 収束: record が card に追従・`diagnose` が空
  3. 回復は冪等 (2 回目は修復 0)・修復は監査に `repaired:R-n` で残る
  4. **同じ操作の再送**が、どの点で落ちても §4.4 の表どおり (まだ起きていなければ成功・済んでいれば冪等の成功か
     `TASK_ALREADY_RESERVED`)。再送の後も収束
  5. reserve の手順 0: **前の試行 X の終端は、Y を書く前に必ず card と record X にある** (回復を待たずに成り立つ)
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

import task_controller_helpers as h
from task_controller_helpers import MISSION, AGENT, OTHER, T0, T1, T2, ex, store, ctl

REPS = 20
HOLD = store._ASSIGNMENT_HOLDING_STATUSES
RECOVERY_SCOPE = store.Scope(cards=((MISSION, "t001"),), agents=(AGENT, OTHER))


# ---------------------------------------------------------------------------
# 開始状態 (seed) と場面 (run)。どれも lib の API だけで作る。ID は固定
# ---------------------------------------------------------------------------

def seed_pending(q):
    h.seed(q)


def seed_reserved(q):
    h.seed(q)
    h.reserve(q, factory=h.ids())


def seed_running(q):
    h.seed(q)
    h.reserve_and_start(q, factory=h.ids())


def seed_ready(q):
    seed_running(q)
    with h.tx(q, op="ready") as t:
        ctl.mark_task(t, MISSION, "t001", h.caller(h.xid(1)), command="ready-for-verification",
                      to_status="ready_for_verification")


def seed_detached(q):
    """X (running) の card を旧コードが reset で手放した (DETACHED (b)・pending)。"""
    seed_running(q)
    h.old_code_reset(q)


def seed_finished_detached(q):
    """X (running) のまま task だけ旧コードが done にした (DETACHED (b)・終わった task)。"""
    seed_running(q)
    h.set_status(q, "done")


X = h.xid(1)


def run_reserve(q):
    h.reserve(q, factory=h.ids())


def run_reserve_next(q):
    h.reserve(q, agent=OTHER, now=T1, factory=h.ids(2))


def run_start(q):
    with h.tx(q, op="pull2") as t:
        ctl.start_execution(t, MISSION, "t001", X, {"worktree": "/abs/wt", "head_at_start": "abc"})


def run_done(q):
    with h.tx(q, op="done") as t:
        ctl.complete_execution(t, MISSION, "t001", h.caller(X), to_status="done", meta_updates={"completed_at": T2})


def run_verify_pass(q):
    with h.tx(q, op="verify-result") as t:
        ctl.complete_execution(t, MISSION, "t001", h.caller(X), to_status="verified")


def run_fail(q):
    with h.tx(q, op="fail") as t:
        ctl.fail_execution(t, MISSION, "t001", h.caller(X), ex.WORKER_FAILED, to_status="failed")


def run_needs_director(q):
    with h.tx(q, op="needs-director") as t:
        ctl.fail_execution(t, MISSION, "t001", h.caller(X), ex.NEEDS_DIRECTOR, to_status="needs_director",
                           meta_updates={"needs_director_reason": "r"})


def run_verify_fail(q):
    with h.tx(q, op="verify-result") as t:
        ctl.fail_execution(t, MISSION, "t001", h.caller(X), ex.VERIFICATION_REJECTED, to_status="pending")


def run_reset(q):
    with h.tx(q, op="update", actor="director") as t:
        ctl.reset_task(t, MISSION, "t001")


def run_retire_release(q):
    with h.tx(q, op="retire", actor="watchdog") as t:
        ctl.release_execution(t, MISSION, "t001", h.caller(X), ex.RETIRED)


def run_g1(q):
    with h.tx(q, op="pull2") as t:
        ctl.fail_execution(t, MISSION, "t001", h.caller(X), ex.WORKSPACE_CREATE_FAILED, to_status="needs_director",
                           meta_updates={"needs_director_reason": "r"})


def run_abandon(q):
    with h.tx(q, op="update", actor="director") as t:
        ctl.abandon_detached_execution(t, MISSION, "t001")


#: 名前 → (seed, run, 再送してよい結果の例外コード (無ければ必ず成功), 最後の card の状態の検査)
SCENARIOS = {
    "reserve": (seed_pending, run_reserve, {ex.TASK_ALREADY_RESERVED}),
    "reserve_after_detached": (seed_detached, run_reserve_next, {ex.TASK_ALREADY_RESERVED}),
    "start": (seed_reserved, run_start, set()),
    "done": (seed_running, run_done, set()),
    "verify_pass": (seed_ready, run_verify_pass, set()),
    "fail": (seed_running, run_fail, set()),
    "needs_director": (seed_running, run_needs_director, set()),
    "verify_fail": (seed_ready, run_verify_fail, set()),
    "reset_running": (seed_running, run_reset, set()),
    "reset_reserved": (seed_reserved, run_reset, set()),
    "retire_released": (seed_reserved, run_retire_release, set()),
    # G1 は compare-and-set (E2): 書いた後の再送は「CAS が外れた」= 拒否 (§4.4 の表に G1 の冪等の行は無い。何も書かない)
    "g1": (seed_reserved, run_g1, {ex.EXECUTION_NOT_CURRENT}),
    "abandon_detached": (seed_finished_detached, run_abandon, {ex.INVALID_TRANSITION}),
}


@pytest.fixture(scope="module")
def seeds(tmp_path_factory):
    """場面ごとの開始状態を 1 回だけ作り (反復のたびに複製)、点の一覧を dry-run で取る。"""
    out = {}
    for name, (seed_fn, run_fn, _ok) in SCENARIOS.items():
        root = tmp_path_factory.mktemp(f"seed-{name}")
        seed_fn(root / "q")
        probe = tmp_path_factory.mktemp(f"probe-{name}")
        shutil.copytree(root / "q", probe / "q")
        out[name] = (root / "q", h.points_of(probe / "q", run_fn))
    return out


#: 点の数の下限 (書き込みの段が減ったら注入口が壊れている。空虚な PASS を防ぐ)
MIN_POINTS = {"reserve": 26, "reserve_after_detached": 40, "start": 14, "done": 20, "verify_pass": 14, "fail": 20,
              "needs_director": 20, "verify_fail": 20, "reset_running": 20, "reset_reserved": 20,
              "retire_released": 20, "g1": 20, "abandon_detached": 16}


def test_every_scenario_has_enough_points(seeds):
    for name, (_q, points) in seeds.items():
        assert len(points) >= MIN_POINTS[name], (name, len(points))
    assert {"atomic:begin", "atomic:tmp_created", "atomic:written", "atomic:synced", "atomic:replaced",
            "atomic:dir_synced", "audit:begin", "audit:appended"} <= set(seeds["reserve"][1])
    assert {"remove:begin", "remove:unlinked", "remove:dir_synced"} <= set(seeds["done"][1])
    # record の書き込みが点として数えられている (executions/ への書き込みが lib の外に出ていない)
    assert seeds["reserve"][1].count("atomic:replaced") >= 4                      # card・record・identity・本体


def _assert_card_is_sound(q):
    meta = h.read_meta(q)
    assert ex.fields_problem(meta) is None, meta
    if ex.has_execution_fields(meta):
        ex.attempt_view(meta, HOLD)                                               # 計算できる (ValueError にならない)


def _converged(q):
    h.card_kinds_converged(q)
    findings = h.diagnose_kinds(q)
    # 回復は DETACHED の欄を閉じない (推測で terminal にしない。execution.md §4.2)。Director の `abandon_detached_execution`
    # が済むまで、終わった task に残る active な欄の報告は消えない (済めば消える)
    if h.read_meta(q).get("execution_status") in ex.ACTIVE_STATUSES:
        # pending に戻された DETACHED (b) も同じ (旧コードの reset の跡。次の reserve の手順 0 か abandon が閉じるまで報告のまま)
        findings = [f for f in findings if f.kind not in (
            "reported:execution_active_on_finished_task", "reported:execution_active_on_non_holding_status")]
    assert findings == [], findings


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_crash_at_every_point_converges_after_the_next_lock_and_a_resend_gives_the_designed_answer(
        scenario, seeds, tmp_path):
    seed_q, points = seeds[scenario]
    run_fn, ok_codes = SCENARIOS[scenario][1], SCENARIOS[scenario][2]
    checked = 0
    for k, point in enumerate(points):
        for rep in range(REPS):
            q = tmp_path / f"{k}-{rep}" / "q"
            shutil.copytree(seed_q, q)
            died = h.fork_and_crash(q, run_fn, k)
            assert died, f"{scenario}[{k}={point}] 子が SIGKILL で落ちていない (注入口が効いていない)"

            _assert_card_is_sound(q)                                              # 回復の前でも card は健全 (コミット点)
            if scenario == "reserve_after_detached":
                _assert_previous_attempt_is_closed_before_the_next_id_exists(q)

            with h.tx(q, op="next", actor="test") as t:
                first = t.recover(RECOVERY_SCOPE)
            _converged(q)
            with h.tx(q, op="next2", actor="test") as t:
                second = t.recover(RECOVERY_SCOPE)
            assert [r for r in second if r.repaired] == [], (scenario, k, point, second)   # 冪等
            lines = "".join(row_text for row_text in
                            (p.read_text() for p in (q / "audit").glob("transitions-*.jsonl")))
            for r in first:
                if r.repaired:
                    assert f'"result": "{r.result}"' in lines, (scenario, k, point, r)

            # 同じ操作の再送: まだ起きていなければ成功・済んでいれば冪等の成功 (または設計の拒否コード)
            try:
                run_fn(q)
            except ex.ControllerError as e:
                assert e.code in ok_codes, (scenario, k, point, e.code)
            with h.tx(q, op="next3", actor="test") as t:
                t.recover(RECOVERY_SCOPE)
            _converged(q)
            checked += 1
    assert checked == len(points) * REPS


def _assert_previous_attempt_is_closed_before_the_next_id_exists(q):
    """reserve の手順 0 (§1.4): card に新しい ID (Y) が現れた時点で、X の終端は card にも record X にも既にある
    (回復を待たない)。Y がまだ無い点では、X が card の current なので R-5 の範囲 (回復の後に収束する)。"""
    meta = h.read_meta(q)
    if meta["current_execution_id"] != X:
        rec = h.read_record(q, X)
        assert rec is not None and rec["status"] == "failed" and rec["end_code"] == "ABANDONED_OUTSIDE_CONTROLLER"
    elif meta["execution_status"] == "failed":
        assert meta["execution_end_code"] == "ABANDONED_OUTSIDE_CONTROLLER"       # card に先に入った (record は R-5 が合わせる)
    kinds = [f.kind for f in store.diagnose(q, store.Scope.everything(q))]
    assert "reported:execution_record_superseded_active" not in kinds


def test_a_detached_attempt_closed_by_the_next_reserve_is_never_left_active_in_a_superseded_record(seeds, tmp_path):
    """欠陥版 (Y を書いてから X を凍結) では、Y の card の直後に落ちると X が current から外れ、record X は active の
    まま二度と扱われない (Codex P2-2)。全点で、X の record は回復の前後とも active のままにならない。"""
    seed_q, points = seeds["reserve_after_detached"]
    for k in range(len(points)):
        q = tmp_path / f"s{k}" / "q"
        shutil.copytree(seed_q, q)
        h.fork_and_crash(q, run_reserve_next, k)
        rec = h.read_record(q, X)
        meta = h.read_meta(q)
        if meta["current_execution_id"] != X:
            assert rec["status"] in ex.TERMINAL_EXECUTION_STATUSES, (k, points[k], rec["status"])
