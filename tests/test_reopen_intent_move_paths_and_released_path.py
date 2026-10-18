"""PR-F intent の穴 5 (vNext 02 PR-F intent fix 5 / t069。Codex 指摘 P1 × 1・P2 × 2)。

1. [P1] `released_deps` の掃除が、戻した task と I の ID しか消さない。I(done) → A(needs_director) → R(pending, released_deps=[A]) で
   reopen は通り、A と R の解除が残る → `fail A` で R が I より先に pull できる。消す集合は **I から終点への経路上の task を status を問わず**。
2. [P2] `plan.sh archive` は active_missions の intent しか回復・確認しない。inactive の mission を reopen して止まった後に退避すると、未完の
   intent ごと退避先に移り回復の経路が消える。
3. [P2] `init --force --mission <slug>` は既存の mission を退避してから置き換える → 未完の intent が退避先に移る。

mission dir を動かす・置き換える・消す経路の棚卸し (`_INTENT_FREEZE` の『通す』の行):
  | コマンド | 内部の mission dir / state.yaml の書き換え | 扱い |
  |---|---|---|
  | init (新しい slug) | 新しい mission dir を作る・state.yaml に足す | 通す (既存の intent に触れない) |
  | init --force (既存) | 既存を archive/<slug>.overwritten-<ts> へ移す | **止める** (未完の intent があれば) |
  | archive | mission dir を archive/ へ移す・state.yaml から外す | **止める** (active かを問わず回復 → 残れば止める) |
  | launch / review | mission.yaml の 1 欄だけ | 通す (dir を動かさない) |
  | reap-orphan-assignment / retire | registry の assignment だけ | 通す |
  | done / fail 等の報告 | card 1 枚 + mission の done 判定 (dir は動かさない) | 通す |
"""

from __future__ import annotations

import re

import pytest

from execution_e3_helpers import MISSION, run, set_card

from test_reopen_cascade_plan_sh import ALL_A_STATES, SKILLS, _vocab_box, reopen, status  # noqa: E402
from test_reopen_intent_graph_frozen import _code, _stopped_box, _stuck_box  # noqa: E402
from test_reopen_intent_recovery import intents  # noqa: E402

INCOMPLETE = "REOPEN_INTENT_INCOMPLETE"


# ---------------------------------------------------------------------------
# 1. 経路上の中間の解除は status を問わず消える
# ---------------------------------------------------------------------------

#: reopen の後に中間 A へ起こす操作 (None = 何もしない / fail = Worker の失敗報告相当 / retry = 打ち直し)
POST_OPS = {"none": None, "fail": "failed", "retry": "pending"}


@pytest.mark.parametrize("post_op", sorted(POST_OPS))
@pytest.mark.parametrize("cascade", ["reviews", "all"])
@pytest.mark.parametrize("released", [False, True])
@pytest.mark.parametrize("a_state", [s for s in ALL_A_STATES if s != "cancelled"])
def test_no_downstream_can_run_before_the_root_completes_again_whatever_the_intermediate_does_next(
        tmp_path, a_state, released, cascade, post_op):
    """I(done) → A(中間・status = 語彙の全部) → R(pending, review)。R が A を解除しているか × cascade × reopen の後に A を fail / retry させる。
    reopen が通ったなら、I が pending の間に R を pull できる組み合わせは残らない。拒否 (skipped / failed の中間) なら何も書かない。"""
    box = _vocab_box(tmp_path, a_state, released)
    before = box.snapshot()
    p = reopen(box, "t001", "--cascade", cascade)
    if p.returncode != 0:
        assert p.returncode == 3 and a_state in ("skipped", "failed"), (a_state, p.returncode, p.stderr)
        assert box.snapshot() == before
        return
    assert status(box, "t001") == "pending" and intents(box) == []
    if POST_OPS[post_op]:
        set_card(box, "t002", status=POST_OPS[post_op])
    assert status(box, "t003") == "pending"
    assert not (box.card("t003").get("released_deps") or []), "経路上の A の解除が残っている"
    pull = box.pull("Mika", "t003", skills=SKILLS)
    assert pull.returncode != 0, ("I が pending なのに R を pull できた", a_state, released, cascade, post_op)


def test_the_positive_control_the_old_release_would_have_let_r_run(tmp_path):
    """陽性対照: I が done のまま A が failed・R が A を解除していれば R は本当に pull できる (上の assert は空振りでない)。"""
    box = _vocab_box(tmp_path, "failed", True)
    assert box.pull("Mika", "t003", skills=SKILLS).returncode == 0


def test_the_codex_repro_needs_director_then_fail(tmp_path):
    """指摘の再現そのもの: A(needs_director) を R が解除済み → reopen → `fail A` → R。"""
    box = _vocab_box(tmp_path, "needs_director", True)
    assert reopen(box, "t001").returncode == 0
    assert box.card("t002")["status"] == "needs_director"                  # 経路の中間は戻さない (未完了)
    assert not (box.card("t003").get("released_deps") or [])               # 解除だけが消える
    set_card(box, "t002", status="failed")
    assert box.pull("Mika", "t003", skills=SKILLS).returncode != 0


def test_the_pairs_to_unrelease_are_part_of_the_intent_and_recovery_removes_the_same_ones(tmp_path):
    """止まった reopen を回復が完了するとき、入口が決めた (下流, 上流) の組を消す (今の card から推測し直さない)。"""
    box = _stopped_box(tmp_path)
    text = (box.queue / "missions" / MISSION / "reopen-intents" / "t001-1.json").read_text()
    assert '"unrelease"' in text
    set_card(box, "t003", released_deps=["t002"])                          # 止まっている間に付いた解除 (手編集)
    p = box.pull("Mika", "t001", skills=SKILLS)
    assert intents(box) == [], p.stderr
    # intent に書いた組に無い解除は触らない (決定の後に増えたものを推測で消さない)
    assert box.card("t003").get("released_deps") == ["t002"]


# ---------------------------------------------------------------------------
# 2・3. mission dir を動かす経路 (archive / init --force) は未完の intent があれば止まる
# ---------------------------------------------------------------------------

def _inactive(box):
    (box.queue / "state.yaml").write_text("active_missions: []\ndefault_mission: null\n")


def _tree_without_audit(box):
    return box.snapshot()


def _archive_dir(box):
    return box.queue / "archive"


def _nothing_moved(box):
    """退避先に何も無い (dir 自体は mission の準備で先に作られていてよい)。"""
    d = _archive_dir(box)
    return not d.exists() or not any(d.iterdir())


@pytest.mark.parametrize("active", [True, False])
def test_archive_refuses_a_mission_with_a_stuck_intent_active_or_not(tmp_path, active):
    box = _stuck_box(tmp_path)
    if not active:
        _inactive(box)
    before = _tree_without_audit(box)
    p = run(box, "archive", MISSION, agent="Director")
    assert p.returncode == 1 and _code(p) == INCOMPLETE, (p.returncode, p.stderr)
    assert _tree_without_audit(box) == before                              # 何も動かさない
    assert intents(box) == ["t001-1.json"] and _nothing_moved(box)


@pytest.mark.parametrize("active", [True, False])
def test_archive_completes_a_completable_intent_first_even_for_an_inactive_mission(tmp_path, active):
    """完了できる intent (止まっていない) は退避の前に最後まで実行される。退避先の card は戻った後の形。"""
    box = _stopped_box(tmp_path)
    if not active:
        _inactive(box)
    p = run(box, "archive", MISSION, agent="Director")
    assert p.returncode == 0, p.stderr
    moved = _archive_dir(box) / MISSION
    assert moved.is_dir() and not (moved / "reopen-intents").exists() or not list((moved / "reopen-intents").glob("*.json"))
    text = (moved / "tasks" / "t001.md").read_text()
    assert re.search(r"^status: pending$", text, re.M), text


@pytest.mark.parametrize("active", [True, False])
def test_init_force_refuses_to_replace_a_mission_with_a_stuck_intent(tmp_path, active):
    box = _stuck_box(tmp_path)
    if not active:
        _inactive(box)
    before = _tree_without_audit(box)
    p = run(box, "init", "置き換え", "--mission", MISSION, "--force", "--inactive", agent="Director")
    assert p.returncode == 1 and _code(p) == INCOMPLETE, (p.returncode, p.stderr)
    assert _tree_without_audit(box) == before
    assert intents(box) == ["t001-1.json"] and _nothing_moved(box)


def test_init_force_replaces_a_mission_whose_intent_can_be_completed(tmp_path):
    box = _stopped_box(tmp_path)
    _inactive(box)
    p = run(box, "init", "置き換え", "--mission", MISSION, "--force", "--inactive", agent="Director")
    assert p.returncode == 0, p.stderr
    kept = [d for d in _archive_dir(box).iterdir() if d.name.startswith(MISSION + ".overwritten-")]
    assert len(kept) == 1
    assert re.search(r"^status: pending$", (kept[0] / "tasks" / "t001.md").read_text(), re.M)


def test_init_of_a_new_slug_and_init_force_of_a_clean_mission_still_work(tmp_path):
    box = _stuck_box(tmp_path)
    p = run(box, "init", "別の mission", "--mission", "m-other", "--inactive", agent="Director")
    assert p.returncode == 0, p.stderr
    assert intents(box) == ["t001-1.json"]                                 # 他の mission の intent は無傷
    q = run(box, "init", "もう一度", "--mission", "m-other", "--force", "--inactive", agent="Director")
    assert q.returncode == 0, q.stderr
