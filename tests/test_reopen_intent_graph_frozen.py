"""PR-F intent の穴 3 (vNext 02 PR-F intent fix 3 / t065。Codex 指摘 P1 × 1)。

`reopen_intent_blockers` は intent に保存した `closure` だけでグラフを作っていた。回復が止まっている間に `update --blocked-by` で依存を
付け替える (無関係な done の B を I に依存させ、R を B に依存させる) と、B は保存した closure に無いので経路に現れず、回復は B を見ずに
I / A を戻して intent を消す → R が B の古い結果で I より先に pull できる。

二重の網:
  1. 未完の intent がある mission で、依存の形を変える書き込み (`add` / `update --blocked-by` / `release-dep`) を拒否する
     (exit 3・何も書かない・固定コード `REOPEN_INTENT_GRAPH_FROZEN`・出口つき)。
  2. 打ち直しの検査 (`reopen_intent_blockers`) は **今の mission の全 card から作ったグラフ**で、I から intent の対象への経路を調べる。
     1 を通り抜けた付け替え (手編集・旧形式の書き手) でも intent を残して止める。
テストは 1 と 2 を**別々に**赤にできる形にしてある (1 だけ / 2 だけを外した複製で、狙ったテストだけが落ちる)。
"""

from __future__ import annotations

import json
import re
import shutil

import pytest

from execution_e3_helpers import MISSION, last_error_code, run, set_card

from test_reopen_cascade_plan_sh import (  # noqa: E402
    CHAIN, SKILLS, _fork_reopen, _restore, build, count, finish_all, reopen, status, store_check,
)
from test_reopen_intent_recovery import ARGV, intents  # noqa: E402

# I(t001) → A(t002) → R(t003, review)。t004 = B (I とも A とも無関係な done の task)
SPEC = [*CHAIN, ("t004", [], ["code"])]
TIDS = ("t001", "t002", "t003", "t004")
FROZEN = "REOPEN_INTENT_GRAPH_FROZEN"


def _stopped_box(tmp_path, name="root", spec=SPEC, tids=TIDS):
    """全 task が done の mission で、reopen が intent を書いた直後 (まだ何も戻していない) に止まった box。"""
    box = build(tmp_path, spec, name=name)
    finish_all(box, tids)
    mf = box.queue / "missions" / MISSION / "mission.yaml"      # 場面作りで足した card の分だけ採番を進めておく (R-3 の回復が書かない)
    mf.write_text(re.sub(r"next_task_id: \d+", f"next_task_id: {len(spec) + 1}", mf.read_text()))
    saved = tmp_path / f"{name}-saved"
    shutil.copytree(box.queue, saved)
    for k in range(1, 400):
        _restore(box, saved)
        killed, _calls = _fork_reopen(box, ARGV, k)
        assert killed
        if intents(box):
            return box
    raise AssertionError("intent が書かれる点が無い")


def _stuck_box(tmp_path, name="root"):
    """完了できない intent (中間 A が skipped になった = 打ち直しが止まる) を抱えた box。"""
    box = _stopped_box(tmp_path, name)
    set_card(box, "t002", status="skipped")
    stalled = box.pull("Mika", "t001", skills=SKILLS)
    assert stalled.returncode == 1 and intents(box) == ["t001-1.json"], stalled.stderr
    return box


def _code(p):
    return last_error_code(p.stderr)


# ---------------------------------------------------------------------------
# 網 1: 依存の形を変える書き込みを拒否する
# ---------------------------------------------------------------------------

def _rewire(box):
    return run(box, "update", "t003", "--blocked-by", "t004", "--mission", MISSION, agent="Director")


def _add_with_dep(box):
    return run(box, "add", "新しい下流", "--skills", "code", "--blocked-by", "t001", "--mission", MISSION, agent="Director")


def _add_plain(box):
    return run(box, "add", "無関係な task", "--skills", "code", "--mission", MISSION, agent="Director")


def _release(box):
    return run(box, "release-dep", "t003", "--dep", "t002", "--mission", MISSION, agent="Director")


WRITERS = {"update --blocked-by": _rewire, "add --blocked-by": _add_with_dep, "add": _add_plain, "release-dep": _release}


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_dependency_shape_write_is_refused_while_an_intent_is_stuck(tmp_path, name):
    """Codex の再現 (付け替え) と、新しい下流を足す形 (add)・解除 (release-dep): exit 3・何も書かない・固定コード・出口。"""
    box = _stuck_box(tmp_path)
    before = box.snapshot()
    p = WRITERS[name](box)
    assert p.returncode == 3 and _code(p) == FROZEN, (name, p.returncode, p.stderr)
    assert "plan.sh reopen" in p.stderr, p.stderr                       # 出口が文面にある
    assert box.snapshot() == before, name                               # card・mission・intent・assignment が 1 バイトも変わらない
    assert intents(box) == ["t001-1.json"] and status(box, "t001") == "done"
    assert not (box.queue / "missions" / MISSION / "tasks" / "t005.md").exists(), name   # add が card を作っていない


def test_the_refusal_does_not_leak_card_contents(tmp_path):
    box = _stuck_box(tmp_path)
    set_card(box, "t004", title="SECRET-TITLE-65")
    p = _rewire(box)
    assert p.returncode == 3 and "SECRET-TITLE-65" not in p.stdout + p.stderr


def test_the_refusal_is_about_the_stuck_mission_only(tmp_path):
    """止めるのは依存の形を変える書き込みだけ。他の更新・読み取り・Director の出口 (status を直す update) は通す。"""
    box = _stuck_box(tmp_path)
    for argv in (("update", "t004", "--priority", "low"), ("update", "t004", "--description", "x"),
                 ("update", "t003", "--skills", "review,qa"), ("status",), ("store-check",)):
        p = run(box, *argv, "--mission", MISSION, agent="Director")
        assert p.returncode == 0, (argv, p.stderr)
    out = run(box, "update", "t002", "--status", "pending", "--mission", MISSION, agent="Director")
    assert out.returncode == 0, out.stderr                              # 止めている task を片付ける出口は塞がない


def test_after_the_director_clears_the_blocker_the_same_reopen_finishes_and_the_writers_work_again(tmp_path):
    box = _stuck_box(tmp_path)
    assert _rewire(box).returncode == 3
    set_card(box, "t002", status="pending")                              # 出口: 止めていた task を片付ける
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert intents(box) == [] and [status(box, t) for t in TIDS[:3]] == ["pending"] * 3 and status(box, "t004") == "done"
    assert _rewire(box).returncode == 0                                  # intent が無くなれば付け替えられる
    assert _add_plain(box).returncode == 0


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_completable_intent_is_finished_first_so_the_write_goes_through(tmp_path, name):
    """止まっているだけで**完了できる** intent は、書き込みコマンドの回復が先に完了する。その後の付け替えは安全なので通る
    (決定どおりの一覧で戻した後の mission に対する、普通の書き込み)。"""
    box = _stopped_box(tmp_path, "completable")
    p = WRITERS[name](box)
    if name == "release-dep":
        # 回復の後、R は pending (I の再完了待ち)。A は pending なので解除する failed の依存が無い (release-dep 自身の拒否)
        assert intents(box) == [] and p.returncode != 3, p.stderr
    else:
        assert p.returncode == 0, (name, p.stderr)
    assert intents(box) == [] and [status(box, t) for t in TIDS[:3]] == ["pending"] * 3


# ---------------------------------------------------------------------------
# 網 2: 打ち直しは今の mission 全体のグラフで経路を調べる
# ---------------------------------------------------------------------------

def _bypass_rewire(box):
    """網 1 を通らない付け替え (手編集・旧形式の書き手): B を I に依存させ、R を A でなく B に依存させる。"""
    set_card(box, "t004", blocked_by=["t001"])
    set_card(box, "t003", blocked_by=["t004"])


def test_a_rewire_that_bypassed_the_write_gate_still_stops_the_resume_and_keeps_the_intent(tmp_path):
    """Codex の再現そのもの (経路: I → B → R)。B は保存した closure に無い done の task = 古い I の結果の上の結果。"""
    box = _stopped_box(tmp_path)
    _bypass_rewire(box)
    before = box.snapshot()
    p = box.pull("Mika", "t001", skills=SKILLS)                            # 回復を走らせる pull
    assert p.returncode == 1 and _code(p) == "REOPEN_INTENT_INCOMPLETE", (p.returncode, p.stderr)
    assert "t004" in p.stderr and "PATH_CHANGED_WHILE_STALLED" in p.stderr, p.stderr
    assert box.snapshot() == before                                        # 何も書かない (I は done のまま・intent も残る)
    assert intents(box) == ["t001-1.json"] and [status(box, t) for t in TIDS] == ["done"] * 4
    out = store_check(box)
    assert re.search(r"reopen-intent-blocked.*task=t004.*PATH_CHANGED_WHILE_STALLED", out), out
    # R はまだ done なので古い B の上で走れない。intent の一覧は推測で増やさない
    listed = [t["id"] for t in json.loads((box.queue / "missions" / MISSION / "reopen-intents" / "t001-1.json").read_text())["tasks"]]
    assert "t004" not in listed


def test_the_exit_for_a_stopped_rewire_is_to_reset_the_new_middle_task_and_rerun_the_reopen(tmp_path):
    box = _stopped_box(tmp_path)
    _bypass_rewire(box)
    assert box.pull("Mika", "t001", skills=SKILLS).returncode == 1
    set_card(box, "t004", status="pending")                                # 出口: 決定の後に完了した task を pending に戻す
    p = reopen(box)
    assert p.returncode == 0, p.stderr
    assert intents(box) == [] and [status(box, t) for t in TIDS] == ["pending"] * 4
    assert count(box, "t001") == 1 and count(box, "t003") == 1


def test_a_new_pending_downstream_of_the_root_does_not_block_the_resume(tmp_path):
    """経路上にない新しい下流 (I に依存するだけ・どの対象にも届かない) は止める理由が無い (推測で一覧を増やさない)。"""
    box = _stopped_box(tmp_path)
    set_card(box, "t004", blocked_by=["t001"])                             # B は I の下流になったが、R への経路は無い
    p = box.pull("Mika", "t003", skills=SKILLS)                            # 回復だけ走らせる (R は I の再完了を待つので取れない)
    assert intents(box) == [], p.stderr
    assert [status(box, t) for t in TIDS[:3]] == ["pending"] * 3 and status(box, "t004") == "done"


@pytest.mark.parametrize("b_status", ["skipped", "failed"])
def test_a_rewired_middle_task_that_is_skipped_or_failed_blocks_with_the_entry_codes(tmp_path, b_status):
    """新しい経路上の中間も、入口と同じ拒否の規則 (skipped / failed の中間) に当たる。"""
    box = _stopped_box(tmp_path)
    _bypass_rewire(box)
    set_card(box, "t004", status=b_status)
    p = box.pull("Mika", "t001", skills=SKILLS)
    code = "CASCADE_SKIPPED_ON_PATH" if b_status == "skipped" else "CASCADE_FAILED_ON_PATH"
    assert p.returncode == 1 and "t004" in p.stderr and code in p.stderr, p.stderr
    assert intents(box) == ["t001-1.json"] and status(box, "t001") == "done"


def test_an_unreadable_card_anywhere_in_the_mission_stops_the_resume(tmp_path):
    """card が読めないと依存の辺が見えない (入口の `CASCADE_UNREADABLE_CARDS` と同じ向き)。壊れた intent と同じく消さずに止める。"""
    box = _stopped_box(tmp_path)
    (box.queue / "missions" / MISSION / "tasks" / "t004.md").write_text("not a card\n")
    before = box.snapshot()
    p = box.pull("Mika", "t001", skills=SKILLS)
    assert p.returncode == 1 and _code(p) == "REOPEN_INTENT_INCOMPLETE", p.stderr
    assert box.snapshot() == before and intents(box) == ["t001-1.json"]


# ---------------------------------------------------------------------------
# 族で洗う: 止まっている間に通している書き込みごとの理由 (設計 §4.3.1 の表の裏づけ)
# ---------------------------------------------------------------------------

def test_a_skills_change_that_makes_an_unlisted_done_task_a_target_is_caught_by_the_resume_check(tmp_path):
    """`update --skills` は通す書き込み。skills が変わると cascade の終点が増えうるが、増えた終点が done で一覧に無ければ
    打ち直しの検査 (PATH_CHANGED_WHILE_STALLED) が止める (古い結果を通さない)。一覧は推測で増やさない。"""
    spec = [*CHAIN, ("t004", ["t002"], ["code"])]                         # t004 = A の下流の code task (reviews では選ばれない)
    box = _stopped_box(tmp_path, "skills", spec=spec)
    listed = [t["id"] for t in json.loads((box.queue / "missions" / MISSION / "reopen-intents" / "t001-1.json").read_text())["tasks"]]
    assert "t004" not in listed
    # `update` は本体の前に回復を走らせて**完了できる** intent を先に完了する。skills の変更が打ち直しに届くのは、intent が別の理由で
    # 止まっている間に変わったときだけ → 場面は card を直接書いて作る (止まっている間の `update --skills` と同じ結果)
    set_card(box, "t004", skills=["review"])
    p = box.pull("Mika", "t001", skills=SKILLS)
    assert p.returncode == 1 and "t004" in p.stderr and "PATH_CHANGED_WHILE_STALLED" in p.stderr, p.stderr
    assert intents(box) == ["t001-1.json"] and status(box, "t001") == "done"
