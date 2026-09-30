"""S4 fix 2 巡目 (t037): 回復 (R-3 等) が**失敗**しても、「新規作成のつもり」の書き込みは既存を上書きしない。

回復は拒否を足さない設計 — 書き込みに失敗しても警告して本体へ進む。本体が「回復は済んだ (採番が進んだ・
枠が片付いた)」を前提に書くと、警告だけで回復対象のタスクを失う (Codex #261 1 巡目 P1)。

* add: next_task_id が実在の card より遅れ、mission.yaml が書けない (回復も本体の保存も失敗) → 既存の card は
  バイト単位で不変。
* init: 既存の mission.yaml は置き換えない (排他作成)。
* pull: 回復が失敗したとき、別の task を持つ Worker の枠を上書きしない。
* lib: `create_card` / `create_mission` は、在る・在るか確かめられない (EACCES) なら書かない。
"""

from __future__ import annotations

import os
import pathlib
import stat

import pytest

import state_store_scenarios  # noqa: F401  (scripts/ を sys.path に足す)
import lib_state_store as store
from test_projection_recovery_on_lock import Box, seed_running, seed_pending

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="権限で書けなくする検査は root では成立しない")

ADD = ["add", "NEXT", "--skills", "bash", "--deliverable", "none"]


def _set_next_task_id(box: Box, value: int) -> None:
    path = box.queue / "missions" / box.slug / "mission.yaml"
    text = path.read_text()
    lines = [f"next_task_id: {value}" if ln.startswith("next_task_id:") else ln for ln in text.splitlines()]
    path.write_text("\n".join(lines) + "\n")


class ReadOnly:
    """`path` (ディレクトリ) を書けなくする (読み・入るのはできる)。"""

    def __init__(self, path: pathlib.Path):
        self.path = path

    def __enter__(self):
        self.mode = stat.S_IMODE(self.path.stat().st_mode)
        self.path.chmod(0o500)

    def __exit__(self, *_):
        self.path.chmod(self.mode)


def _lagging_box(root) -> Box:
    """t001・t002 が在り、next_task_id=2 (t002 を書いた add が mission.yaml の前で落ちた後の状態)。"""
    box = seed_pending(root, tasks=2)
    _set_next_task_id(box, 2)
    return box


def test_add_with_a_failed_recovery_leaves_the_existing_card_byte_identical(tmp_path):
    box = _lagging_box(tmp_path / "b")
    card = box.queue / "missions" / box.slug / "tasks" / "t002.md"
    before = card.read_bytes()
    with ReadOnly(card.parent.parent):          # mission dir は書けない・tasks dir は書ける
        p = box.run(*ADD, expect=None)
    assert card.read_bytes() == before, f"t002 が上書きされた\n{p.stdout}\n{p.stderr}"
    assert p.returncode != 0                     # mission.yaml の保存は失敗する
    assert "回復" in p.stderr                    # 回復の失敗は警告される (拒否ではない)
    assert "上書きせず" in p.stderr              # 遅れた採番を見つけて未使用の id に進んだ


def test_add_after_the_cause_is_fixed_skips_every_existing_card_and_heals_the_numbering(tmp_path):
    box = _lagging_box(tmp_path / "b")
    with ReadOnly((box.queue / "missions" / box.slug)):
        box.run(*ADD, expect=None)               # t003 の card は書けたが next_task_id は進まない
    before = {tid: m["title"] for (_s, tid), m in box.cards().items()}
    box.run("add", "AFTER", *ADD[2:])
    after = {tid: m["title"] for (_s, tid), m in box.cards().items()}
    for tid, title in before.items():
        assert after[tid] == title
    assert len(after) == len(before) + 1 and "AFTER" in after.values()


def test_add_stops_without_writing_when_the_target_card_cannot_be_observed(tmp_path):
    box = _lagging_box(tmp_path / "b")
    tdir = box.queue / "missions" / box.slug / "tasks"
    mode = stat.S_IMODE(tdir.stat().st_mode)
    tdir.chmod(0o000)                            # lstat(tasks/t002.md) が EACCES = 在るか分からない
    try:
        p = box.run(*ADD, expect=None)
    finally:
        tdir.chmod(mode)
    assert p.returncode != 0
    assert sorted(f.name for f in tdir.glob("t*.md")) == ["t001.md", "t002.md"]


def test_init_does_not_replace_an_existing_mission_yaml(tmp_path):
    box = seed_pending(tmp_path / "b", tasks=1)
    mpath = box.queue / "missions" / box.slug / "mission.yaml"
    before = mpath.read_bytes()
    ns = box.namespace()
    txn = store.transaction(str(box.queue), op="t", actor="t")
    with txn as t:
        with pytest.raises(store.AlreadyExists):
            t.create_mission(box.slug, {"title": "X", "slug": box.slug})
    assert mpath.read_bytes() == before
    assert ns                                    # namespace が本物の plan.sh を読めている


def test_create_card_refuses_an_existing_card_and_an_unobservable_one(tmp_path):
    box = seed_pending(tmp_path / "b", tasks=1)
    card = box.queue / "missions" / box.slug / "tasks" / "t001.md"
    before = card.read_bytes()
    with store.transaction(str(box.queue), op="t", actor="t") as t:
        with pytest.raises(store.AlreadyExists) as e:
            t.create_card(box.slug, "t001", {"id": "t001", "status": "done"}, "x\n")
        assert e.value.state == "present"
        tdir = card.parent
        tdir.chmod(0o000)
        try:
            with pytest.raises(store.AlreadyExists) as e2:
                t.create_card(box.slug, "t001", {"id": "t001", "status": "done"}, "x\n")
            assert e2.value.state == "unobservable"
        finally:
            tdir.chmod(0o755)
        t.create_card(box.slug, "t009", {"id": "t009", "status": "pending"}, "new\n")    # 無ければ書く
    assert card.read_bytes() == before
    assert (card.parent / "t009.md").exists()


def test_pull_with_a_failed_recovery_does_not_overwrite_the_slot_of_a_busy_worker(tmp_path, monkeypatch):
    box = seed_running(tmp_path / "b", agent="Ren", tasks=1)
    box.run("add", "FREE", "--skills", "bash", "--deliverable", "none")
    slot = box.queue / "assignments" / "Ren"
    identity = box.queue / "assignments" / "Ren.identity"
    before = (slot.read_bytes(), identity.read_bytes(), {k: v for k, v in box.snapshot().items() if "tasks" in k})

    def boom(self, scope):
        raise store.StoreWriteError(str(box.queue / "assignments" / "Ren"), "write", 28)

    monkeypatch.setattr(store.Txn, "recover", boom)
    ns = box.namespace()
    monkeypatch.setenv("AGENT_NAME", "Ren")
    with pytest.raises(SystemExit) as e:
        ns["cmd_pull"](["--agent", "Ren", "--skills", "bash"])
    assert e.value.code == ns["PRECONDITION_UNMET"]
    assert (slot.read_bytes(), identity.read_bytes(),
            {k: v for k, v in box.snapshot().items() if "tasks" in k}) == before
