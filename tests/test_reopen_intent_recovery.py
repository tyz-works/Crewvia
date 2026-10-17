"""reopen の cascade は intent に決定を書いて最後まで実行する (vNext 02 PR-F t059。設計 §4.3.1)。

旧版は「再実行が各 card・台帳の印から済んだ所を推測し直す」方式で、止まっている間に下流が動くと取りこぼした
(PR #308 Codex 2・3・5 巡目)。新方式は、戻す task の一覧・操作・順序を `queue/missions/<m>/reopen-intents/<root>-<n>.json` に
**1 度だけ**書き (= 決定のコミット点)、その一覧どおりに実行し、最後に intent を消す。止まったら **次に queue を書く plan.sh コマンド
(pull を含む)** がロックの直後に intent を最後まで実行してから本体に進む。

* 書き込みの全点で SIGKILL → 次のコマンド (同じ reopen / pull / 別の書き込みコマンド) の後の最終状態 (status・reopen_count・
  released_deps・台帳・mission) が、kill なしの 1 回実行と同じ (golden 比較)
* kill の後・回復の前に別の Worker の pull が来る形: pull 自体が先に回復する → 古い結果で pull される下流は 0 件
* Codex 5 巡目の 2 件 (止まっている間に B が done になる / legacy の実行中 B) が起きない
* intent が読めない・壊れている → その mission の pull が止まり (exit 1・固定コード)、他の mission と読み取り専用のコマンドは止まらない
* 同じ reopen の再実行は intent を完了して成功を報告する。違う reason / cascade は HISTORY_MISMATCH (何も書かない)
"""

from __future__ import annotations

import json
import shutil

import pytest

from execution_e3_helpers import MISSION, last_error_code, run, set_card

import lib_task_controller as ctl  # noqa: E402
from test_reopen_cascade_plan_sh import (  # noqa: E402
    CHAIN, REASON, SKILLS, _fork_reopen, _restore, _state, _released_matrix_box, build, count, finish, finish_all, ledger_files,
    reopen, status, store_check,
)

TIDS = ("t001", "t002", "t003")
ARGV = ["t001", "--mission", MISSION, "--reason", REASON, "--branch", "reuse"]


def intent_dir(box):
    return box.queue / "missions" / MISSION / "reopen-intents"


def intents(box):
    d = intent_dir(box)
    return sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) if d.is_dir() else []


def mission_file(box):
    return box.queue / "missions" / MISSION / "mission.yaml"


def next_command(box, how):
    """kill の後に打つ『次のコマンド』。reopen の再実行 / 別の Worker の pull (R を名指し) / 別の書き込みコマンド (update)。"""
    if how == "reopen":
        return reopen(box)
    if how == "pull":
        return box.pull("Mika", "t003", skills=SKILLS)
    if how == "update":
        return run(box, "update", "t002", "--priority", "low", "--mission", MISSION, agent="Director")
    raise AssertionError(how)


def clean_run_state(tmp_path, how, name, make_box):
    """kill なしの 1 回の実行 (+ 同じ次のコマンド) の最終状態。golden。"""
    box = make_box(tmp_path, name)
    assert reopen(box).returncode == 0
    if how != "reopen":
        next_command(box, how)
    return _state(box, TIDS)


def _chain_box(tmp_path, name):
    box = build(tmp_path, CHAIN, name=name)
    finish_all(box, TIDS)
    return box


@pytest.mark.parametrize("how", ["reopen", "pull", "update"])
def test_kill_at_every_write_then_the_next_command_ends_in_the_same_state_as_one_clean_run(tmp_path, how):
    golden = clean_run_state(tmp_path, how, "golden", lambda t, n: _released_matrix_box(t, "done", n))
    assert golden["t002"][0] == "pending" and "t002" not in golden["t003"][2], golden       # 前提: 1 回で A も R も戻り掃除される
    box = _released_matrix_box(tmp_path, "done", "victim")
    saved = tmp_path / "saved-queue"
    shutil.copytree(box.queue, saved)
    original = _state(box, TIDS)
    killed_points, intent_seen, recovered_by_other = 0, 0, 0
    for k in range(1, 400):
        _restore(box, saved)
        killed, _calls = _fork_reopen(box, ARGV, k)
        if not killed:
            break
        killed_points += 1
        st = {t: status(box, t) for t in TIDS}
        # どの時点でも「上流が pending なのに下流が done」を作らない (古い結果が通る側に倒れない)
        assert not (st["t001"] == "pending" and st["t002"] == "done"), (k, st)
        assert not (st["t002"] == "pending" and st["t003"] == "done"), (k, st)
        had_intent = bool(intents(box))
        intent_seen += had_intent
        if had_intent:
            assert "reopen-intent-incomplete" in store_check(box), k
        finished = status(box, "t001") == "pending" and not had_intent       # intent の削除まで済んだ後で止まった = 完了済み
        p = next_command(box, how)
        if how == "reopen":
            if finished:
                assert p.returncode == 3 and last_error_code(p.stderr) == "NOT_REOPENABLE", (k, p.stderr)
            else:
                assert p.returncode == 0, (k, p.stderr)
        elif had_intent:
            recovered_by_other += 1
            assert intents(box) == [], (k, how, "次のコマンドが intent を完了していない")
        if status(box, "t001") == "done":                      # intent を書く前に止まった: reopen は始まっていない
            assert not had_intent and _state(box, TIDS) == original, (k, how)
            assert reopen(box).returncode == 0
            if how != "reopen":
                next_command(box, how)
        assert _state(box, TIDS) == golden, (k, how)
        assert intents(box) == [] and "reopen-intent-incomplete" not in store_check(box), k
        for t in TIDS:
            assert len(ledger_files(box, t, "reopen")) == 1, (k, t)
    assert killed_points >= 12 and intent_seen >= 8, (killed_points, intent_seen)
    if how != "reopen":
        assert recovered_by_other >= 8, recovered_by_other


def test_the_mission_is_reactivated_by_the_recovery_not_only_by_the_first_run(tmp_path):
    """mission が done のとき、intent の実行は I の card より前に mission を戻す。回復を担う別のコマンドも同じ。"""
    box = _chain_box(tmp_path, "root")
    f = mission_file(box)
    f.write_text(f.read_text().replace("status: in_progress", "status: done"))
    saved = tmp_path / "saved-queue"
    shutil.copytree(box.queue, saved)
    mission_first = False
    for k in range(1, 400):
        _restore(box, saved)
        killed, _calls = _fork_reopen(box, ARGV, k)
        if not killed:
            break
        text = f.read_text()
        pending = [t for t in TIDS if status(box, t) == "pending"]
        if pending:
            assert "status: in_progress" in text, (k, pending)          # pending の task があるのに mission が done
        if intents(box) and not pending and "status: in_progress" in text:
            mission_first = True                                        # intent の後・mission の書き込みの直後 (card はまだ)
        run(box, "update", "t002", "--priority", "low", "--mission", MISSION, agent="Director")
        if status(box, "t001") == "done":
            assert reopen(box).returncode == 0
        assert [status(box, t) for t in TIDS] == ["pending"] * 3, k
        assert "status: in_progress" in f.read_text(), k
    assert mission_first


def test_a_pull_that_arrives_before_the_recovery_recovers_first_and_pulls_nothing_stale(tmp_path):
    """kill で R だけが pending に戻り (I・A は done) の窓。別の Worker の pull は、自分で intent を完了してから候補を選ぶ → R は
    I の再完了を待つ (古い結果で pull される下流が 0 件)。旧版は R が pull されて古い結果で done になりえた (Codex 2 巡目)。"""
    box = _chain_box(tmp_path, "root")
    saved = tmp_path / "saved-queue"
    shutil.copytree(box.queue, saved)
    window = None
    for k in range(1, 400):
        _restore(box, saved)
        killed, _calls = _fork_reopen(box, ARGV, k)
        assert killed, "窓の状態に届く前に reopen が最後まで走った"
        if [status(box, t) for t in TIDS] == ["done", "done", "pending"]:
            window = k
            break
    assert window is not None and intents(box)
    p = box.pull("Mika", "t003", skills=SKILLS)
    assert p.returncode != 0, "窓の R が古い結果の上で pull された"
    assert [status(box, t) for t in TIDS] == ["pending"] * 3
    assert [count(box, t) for t in TIDS] == [1, 1, 1] and intents(box) == []
    assert box.card("t003").get("execution_count") == 1            # R の新しい試行は発行されていない
    # I → A → R の順でしか完了できない
    finish(box, "t001")
    assert box.pull("Mika", "t003", skills=SKILLS).returncode != 0
    finish(box, "t002")
    finish(box, "t003")
    assert [status(box, t) for t in TIDS] == ["done"] * 3


@pytest.mark.parametrize("how", ["reopen", "pull", "update"])
def test_codex_round_5_case_1_a_downstream_finished_while_stopped_is_still_returned(tmp_path, how):
    """Codex 5 巡目: 止まっている間に下流 B が done になった。intent の一覧に B があるので、回復は B を (done から) 戻す。
    B は intent の作成時に実行中だった (reset の操作) が、止まっている間に done まで進んだ形。"""
    box = build(tmp_path, [("t001", [], ["code"]), ("t002", ["t001"], ["code"]), ("t003", ["t002"], ["code"])])
    finish_all(box, ("t001", "t002"))
    p = box.pull("Mika", "t003", skills=SKILLS)                     # B (t003, code) は実行中
    assert p.returncode == 0, p.stderr
    xid = json.loads(p.stdout)["execution_id"]
    saved = tmp_path / "saved-queue"
    shutil.copytree(box.queue, saved)
    argv = ARGV + ["--cascade", "all"]
    hit = None
    for k in range(1, 400):
        _restore(box, saved)
        killed, _calls = _fork_reopen(box, argv, k)
        if not killed:
            break
        if intents(box) and status(box, "t003") == "pending" and status(box, "t002") == "done" and status(box, "t001") == "done":
            hit = k                                                 # B は reset 済み・A と I は done のまま
            break
    assert hit is not None
    # 止まっている間に B が done になった。pull / done は自分でロックの直後に回復するので、この窓は Controller の外の書き手
    # (旧形式の書き手・手編集) でしか作れない — それでも一覧にある限り戻す
    set_card(box, "t003", status="done", worker=None, completed_at="2026-10-17T00:00:00Z")
    assert status(box, "t003") == "done"
    q = reopen(box, "t001", "--cascade", "all") if how == "reopen" else next_command(box, how)
    if how == "reopen":
        assert q.returncode == 0, q.stderr
    assert [status(box, t) for t in TIDS] == ["pending"] * 3, how
    assert intents(box) == []
    assert count(box, "t003") == 1                                   # done から戻したので reopen_count は 1


def test_codex_round_5_case_2_a_legacy_running_downstream_is_returned_by_the_recovery(tmp_path):
    """Codex 5 巡目: 旧形式 (試行の記録が無い) の実行中 B。intent の作成時点で『戻す task』に入っていれば回復も同じ操作をする。
    作成時に試行の記録が無ければ CASCADE_NO_EXECUTION / reset の対象 — どちらも intent の作成前に決まり、回復が推測しない。"""
    box = build(tmp_path, [("t001", [], ["code"]), ("t002", ["t001"], ["code"])])
    finish(box, "t001")
    from execution_e3_helpers import legacy_in_progress
    legacy_in_progress(box, "t002", "Mika")
    set_card(box, "t002", blocked_by=["t001"])
    p = reopen(box, "t001", "--cascade", "all")
    assert p.returncode == 0, p.stderr
    assert status(box, "t001") == "pending" and status(box, "t002") == "pending"
    assert intents(box) == []


# ---------------------------------------------------------------------------
# 同じ reopen の再実行・読めない intent
# ---------------------------------------------------------------------------

def _stopped_box(tmp_path, name="root"):
    """intent を書いた直後 (まだ何も戻していない) で止まった box。"""
    box = _chain_box(tmp_path, name)
    saved = tmp_path / f"{name}-saved"
    shutil.copytree(box.queue, saved)
    for k in range(1, 400):
        _restore(box, saved)
        killed, _calls = _fork_reopen(box, ARGV, k)
        assert killed
        if intents(box):
            return box
    raise AssertionError("intent が書かれる点が無い")


def test_rerun_with_the_same_arguments_completes_the_intent_and_reports_success(tmp_path):
    box = _stopped_box(tmp_path)
    assert status(box, "t001") == "done" and intents(box) == ["t001-1.json"]
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert "再実行で完了" in p.stdout and [status(box, t) for t in TIDS] == ["pending"] * 3
    assert [count(box, t) for t in TIDS] == [1, 1, 1] and intents(box) == []


@pytest.mark.parametrize("change", [dict(reason="別の理由"), dict(cascade="all"), dict(over_cap="了解")])
def test_rerun_with_different_arguments_is_refused_and_writes_nothing(tmp_path, change):
    box = _stopped_box(tmp_path)
    before = box.snapshot()
    extra, kw = [], {}
    if "cascade" in change:
        extra = ["--cascade", change["cascade"]]
    if "over_cap" in change:
        extra = ["--over-cap", change["over_cap"]]
    if "reason" in change:
        kw["reason"] = change["reason"]
    p = reopen(box, "t001", *extra, **kw)
    assert p.returncode == 3 and last_error_code(p.stderr) == "HISTORY_MISMATCH", p.stderr
    assert box.snapshot() == before and intents(box) == ["t001-1.json"]


def _corrupt_intent(box, text):
    (intent_dir(box) / intents(box)[0]).write_text(text)


@pytest.mark.parametrize("text", ["{", "[]", json.dumps({"version": 1}), "", json.dumps({"version": 2})])
def test_an_unreadable_intent_stops_the_pull_of_that_mission_and_nothing_else(tmp_path, text):
    box = _stopped_box(tmp_path)
    _corrupt_intent(box, text)
    before = box.snapshot()
    p = box.pull("Mika", "t003", skills=SKILLS)
    assert p.returncode == 1 and last_error_code(p.stderr) == "REOPEN_INTENT_INCOMPLETE", (p.returncode, p.stderr)
    assert box.snapshot() == before                                       # 何も書かない
    s = run(box, "status", "--mission", MISSION, agent="Director")           # 読み取り専用のコマンドは止まらない
    assert s.returncode == 0, s.stderr
    assert run(box, "store-check", "--mission", MISSION, agent="Director").returncode == 0
    assert "reopen-intent-incomplete" in store_check(box)
    u = run(box, "update", "t002", "--priority", "low", "--mission", MISSION, agent="Director")   # 凍結 (t067): 書き込みは止まる
    assert u.returncode == 3 and last_error_code(u.stderr) == "REOPEN_INTENT_GRAPH_FROZEN", u.stderr
    assert intents(box) == ["t001-1.json"]                                # 壊れた intent は消さない・直さない


def test_an_intent_whose_card_is_unreadable_stops_the_pull_and_a_fixed_card_lets_the_next_command_finish(tmp_path):
    box = _stopped_box(tmp_path)
    card = box.queue / "missions" / MISSION / "tasks" / "t002.md"
    good = card.read_text()
    card.write_text("---\n: : :\n")
    p = box.pull("Mika", "t001", skills=SKILLS)
    assert p.returncode == 1 and last_error_code(p.stderr) == "REOPEN_INTENT_INCOMPLETE", (p.returncode, p.stderr)
    card.write_text(good)
    q = box.pull("Mika", "t003", skills=SKILLS)                          # 直したら次の pull が intent を完了する
    assert intents(box) == [] and [status(box, t) for t in TIDS] == ["pending"] * 3, q.stderr


def test_another_mission_is_not_stopped_by_a_broken_intent(tmp_path):
    box = _stopped_box(tmp_path)
    _corrupt_intent(box, "{")
    other = "m-other"
    shutil.copytree(box.queue / "missions" / MISSION, box.queue / "missions" / other,
                    ignore=shutil.ignore_patterns("reopen-intents", "history", "executions"))
    for c in (box.queue / "missions" / other / "tasks").iterdir():
        c.unlink()
    state = box.queue / "state.yaml"
    state.write_text(state.read_text().replace("default_mission:", f"  - {other}\ndefault_mission:", 1))
    from pull_execution_helpers import Box  # noqa: F401
    import lib_state_store as store
    t = store.Txn(box.queue, op="seed", actor="test")
    meta, body = store.Txn(box.queue, op="x", actor="t").load_card(MISSION, "t002")
    t.write_card(other, "t009", {**meta, "id": "t009", "status": "pending", "blocked_by": [], "skills": ["code"],
                                 **{k: None for k in ("worker", "started_at", "completed_at")}}, body)
    p = box.plan("pull", "--agent", "Mika", "--skills", "code", agent="Mika")
    # 壊れた intent の mission は選ばれず、他の mission の t009 が取れる
    assert p.returncode == 0, (p.returncode, p.stderr)
    assert json.loads(p.stdout)["mission"] == other and json.loads(p.stdout)["id"] == "t009"
    assert intents(box) == ["t001-1.json"]                                # 壊れた intent は消さない・直さない
    assert [status(box, t) for t in TIDS] == ["done", "done", "done"]      # 壊れた mission の card は 1 つも動いていない


def test_store_check_reports_an_incomplete_intent_and_nothing_after_the_next_command(tmp_path):
    box = _stopped_box(tmp_path)
    out = store_check(box)
    assert "reopen-intent-incomplete" in out and "task=t001" in out
    assert "cascade-incomplete" not in out
    run(box, "update", "t002", "--priority", "low", "--mission", MISSION, agent="Director")
    assert "reopen-intent-incomplete" not in store_check(box)


def test_a_new_reopen_does_not_start_while_another_intent_is_unfinished_and_unreadable(tmp_path):
    """intent が壊れていて完了できない mission では、別の root の reopen も始めない (何も書かない・exit 1)。"""
    box = _stopped_box(tmp_path)
    _corrupt_intent(box, "{")
    before = box.snapshot()
    p = reopen(box, "t002")
    assert p.returncode == 1 and last_error_code(p.stderr) == "REOPEN_INTENT_INCOMPLETE", (p.returncode, p.stderr)
    assert box.snapshot() == before


def test_controller_refuses_a_new_reopen_while_an_intent_is_pending(tmp_path):
    """plan.sh が先に完了させるが、Controller を直に呼ばれても intent の上に重ねて書かない。"""
    box = _stopped_box(tmp_path)
    import lib_state_store as store
    with store.transaction(box.queue, op="reopen", actor="Director") as txn:
        with pytest.raises(ctl.ex.ControllerError) as e:
            ctl.reopen_task(txn, MISSION, "t001", kind="reopen", reason=REASON,
                            graph={"t001": [], "t002": ["t001"], "t003": ["t002"]}, unreadable=[])
    assert e.value.code == "REOPEN_INTENT_INCOMPLETE"


# ---------------------------------------------------------------------------
# t061: pull の 2 回目のロック (start) も、完了できない intent を見て止まる。ロックを取って task を進める遷移の表
# ---------------------------------------------------------------------------
#
#   遷移                                                  | 未完で完了できない intent がある mission で
#   pull の 1 回目のロック (候補選び・予約)                 | 止める (その mission を選ばない / 名指しなら exit 1)
#   pull の 2 回目のロック (start: reserved → running)      | 止める (t061。予約は書き換えない・JSON なし・REOPEN_INTENT_INCOMPLETE)
#   pull の worktree 失敗の出口 (needs_director に倒す)     | 通す (予約を閉じるだけで task を進めない。止めると予約が残る)
#   done / fail / needs-director / ready-for-verification /
#   verifying / verify-result (Worker の報告)               | 通す (作業を失わせない。報告は下流を pull 可能にしない = pull が止まっている)
#   retry / update --status・--reset / release-dep / add    | 通す (Director の出口を塞がない。task を走らせるのは pull だけ)
#   retire                                                 | 通す (窓を閉じる側。止めると誰も閉じない)
#   reopen (別の root)                                     | 止める (exit 1。Controller も重ねて始めない)
#   archive (その mission)                                 | 止める (半端な cascade を退避先に固めない)

def _single_pending_box(tmp_path):
    return build(tmp_path, [("t001", [], ["code"])])


def test_the_start_lock_of_a_pull_refuses_when_an_intent_appears_between_the_two_locks(tmp_path):
    box = _single_pending_box(tmp_path)
    box.hold()
    first = box.popen("pull", "--agent", "Ren", "--skills", SKILLS, "--mission", MISSION, "--task", "t001", agent="Ren")
    try:
        box.reached()                                            # 1 回目のロックの後・2 回目のロックの前
        assert box.card()["execution_status"] == "reserved"
        intent_dir(box).mkdir()
        (intent_dir(box) / "t009-1.json").write_text("{")        # この間に reopen が intent を書いて壊れた (回復できない)
        before = box.snapshot()
        box.go()
        stdout, stderr = first.communicate(timeout=60)
        assert first.returncode == 1 and stdout.strip() == "", (first.returncode, stdout, stderr)
        assert last_error_code(stderr) == "REOPEN_INTENT_INCOMPLETE", stderr
        meta = box.card()
        assert meta["status"] == "in_progress" and meta["execution_status"] == "reserved"       # start していない
        assert box.snapshot() == before, "start を止めたのに queue が変わった"
    finally:
        from pull_execution_helpers import kill_group
        kill_group(first) if first.poll() is None else None


def test_reports_and_the_directors_exits_still_pass_while_an_intent_cannot_be_completed(tmp_path):
    """表の『通す』行。Worker の done は作業を失わせない。update は t067 から凍結 (別テストで固定)。"""
    box = _single_pending_box(tmp_path)
    p = box.pull("Ren", "t001", skills=SKILLS)
    assert p.returncode == 0, p.stderr
    xid = json.loads(p.stdout)["execution_id"]
    intent_dir(box).mkdir()
    (intent_dir(box) / "t009-1.json").write_text("{")
    q = run(box, "done", "t001", "r", "--mission", MISSION, "--execution", xid, agent="Ren")
    assert q.returncode == 0, q.stderr
    u = run(box, "update", "t001", "--priority", "low", "--mission", MISSION, agent="Director")
    assert u.returncode == 3, u.stderr


def test_a_mission_with_an_unfinished_intent_cannot_be_retired_to_the_shelf(tmp_path):
    box = _stopped_box(tmp_path)
    _corrupt_intent(box, "{")
    before = box.snapshot()
    p = run(box, "archive", MISSION, agent="Director")
    assert p.returncode == 1 and last_error_code(p.stderr) == "REOPEN_INTENT_INCOMPLETE", (p.returncode, p.stderr)
    assert box.snapshot() == before and (box.queue / "missions" / MISSION).is_dir()
