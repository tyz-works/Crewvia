#!/usr/bin/env python3
"""
tests/test_dispatcher_notify_once.py — 状態ベースの通知は、状態が変わるまで 1 回だけ (t010 / #10 + #11)

## 背景

`should_notify()` は `NOTIFY_TTL` (300 秒) の **スロットルであって受領確認ではない**。
`status == needs_director` / `status == failed` + `handoff_path` のような **状態ベース**
の通知は、状態が続くかぎり 5 分ごとに永久に再送された (2026-09-25、同一内容の通知が
数十通届き、ユーザーがデーモンを手で止めた)。

修正: 「この状態については既に伝えた」という記録 (`registry/daemons/notified-state.json`)
を、スロットルとは別に持つ。通知内容を決める入力 (status / reason / handoff_path /
拒否の記録) が変わったときだけ再通知する。

#11: `kai-review.sh` が差分サイズ超過で拒否した codex-review task は、pending に
戻されても **再 spawn しない** (同じ PR は何度やっても同じ大きさ)。

## 方法

複製ではなく **本物の** dispatcher.sh の埋め込み python を `exec()` して、実際の
`dispatch()` を呼ぶ (`# --- CYCLE ENTRY POINT ---` から下を切る。
`tests/test_orphan_daemon_guard.py` と同じ手)。mux だけを FakeMux に差し替える。

「TTL が過ぎた」は notify cache (/tmp のスロットル) を消して表す —— スロットルが
再送を許した状況でも、状態ベースの通知は黙っていなければならない。

実行: python3 -m pytest tests/test_dispatcher_notify_once.py -v
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT_DIR / "scripts"
DISPATCHER_SH = SCRIPTS_DIR / "dispatcher.sh"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_mux  # noqa: E402
import lib_review_refusal  # noqa: E402

SLUG = "20260925-notify-once"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

class FakeMux:
    """dispatcher が mux に対してすること (list / send / state / capture) だけを持つ。"""

    directors = ["Sora-director"]
    send_ok = True
    sent = []           # class-level: exec のたびに作り直される _mux から共有する

    def __init__(self, *a, **kw):
        pass

    def list(self, *a, suffix=None, **kw):
        if suffix == "-director":
            return list(FakeMux.directors)
        return []

    def send(self, name, text):
        if not FakeMux.send_ok or name not in FakeMux.directors:
            return False
        FakeMux.sent.append({"target": name, "message": text})
        return True

    def state(self, *a, **kw):
        return "unknown"

    def capture(self, *a, **kw):
        return ""

    def kill(self, *a, **kw):
        return False


class Harness:
    """1 つの fixture repo と、そこに対して dispatch() を 1 サイクル回す口。"""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.registry = root / "registry"
        self.queue = root / "queue"
        self.tasks = self.queue / "missions" / SLUG / "tasks"
        self.notify_cache = root / "notify-cache.json"
        self.log = root / "dispatcher.log"
        self.spawned = []
        self.monkeypatch = monkeypatch

        root.mkdir(parents=True)
        (root / ".git").mkdir()
        self.registry.mkdir()
        (root / "scripts").mkdir()
        (root / "scripts" / "kai-review.sh").write_text("#!/bin/bash\n")
        (self.registry / "workers.yaml").write_text(
            "workers:\n  - name: sofia\n    skills: [bash]\n    task_count: 0\n")
        self.tasks.mkdir(parents=True)
        (self.queue / "archive").mkdir()
        (self.queue / "state.yaml").write_text(
            f"active_missions:\n  - {SLUG}\ndefault_mission: {SLUG}\n")
        (self.queue / "missions" / SLUG / "mission.yaml").write_text(
            f'title: "notify once"\nslug: {SLUG}\nstatus: in_progress\n'
            "created_at: 2026-09-25T00:00:00Z\ncompleted_at: null\nnext_task_id: 99\n")

        FakeMux.directors = ["Sora-director"]
        FakeMux.send_ok = True
        FakeMux.sent = []
        monkeypatch.setattr(lib_mux, "Mux", FakeMux)
        monkeypatch.setattr(lib_mux, "repo_identity_ok", lambda *a, **kw: True)
        monkeypatch.setenv("CREWVIA_TASKVIA", "disabled")
        monkeypatch.delenv("TASKVIA_TOKEN", raising=False)
        monkeypatch.setenv("TASKVIA_URL", "")

        def fake_popen(cmd, *a, **kw):
            self.spawned.append(cmd)

            class _P:
                pid = 0
            return _P()
        monkeypatch.setattr(subprocess, "Popen", fake_popen)

    # -- task cards ---------------------------------------------------------
    def card(self, task_id, status, skills="[bash]", **fields):
        lines = ["---", f"id: {task_id}", f"title: {task_id}", f"skills: {skills}",
                 "priority: high", f"status: {status}", "blocked_by: []"]
        for k, v in fields.items():
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        lines += ["---", "", "## Description", "x", ""]
        (self.tasks / f"{task_id}.md").write_text("\n".join(lines))

    # -- one cycle ------------------------------------------------------------
    def cycle(self, *, ttl_expired=False):
        """1 サイクル回し、そのサイクルで Director に届いたメッセージを返す。

        ttl_expired=True: NOTIFY_TTL が過ぎた状況 (= スロットルは再送を許す)。
        """
        if ttl_expired and self.notify_cache.exists():
            self.notify_cache.unlink()
        FakeMux.sent = []
        self.spawned.clear()
        src = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", DISPATCHER_SH.read_text(),
                        re.DOTALL).group(1)
        src, n = re.subn(r"\n# --- CYCLE ENTRY POINT ---\n.*$", "", src, flags=re.S)
        assert n == 1, "CYCLE ENTRY POINT marker not found"
        self.monkeypatch.setattr(sys, "argv", [
            "dispatcher", str(self.queue), str(self.registry), str(self.notify_cache),
            "300", "60", str(self.log)])
        ns = {"__name__": "dispatcher_under_test"}
        exec(compile(src, "dispatcher.sh (embedded, test)", "exec"), ns)
        ns["dispatch"]()
        return [m["message"] for m in FakeMux.sent]

    def log_text(self):
        return self.log.read_text() if self.log.exists() else ""

    @property
    def told_file(self):
        return self.registry / "daemons" / "notified-state.json"


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path / "repo", monkeypatch)


def about(msgs, task_id):
    return [m for m in msgs if f"task {task_id} " in m or f"タスク {task_id} " in m]


# ---------------------------------------------------------------------------
# #10: needs_director は状態が変わるまで 1 回だけ
# ---------------------------------------------------------------------------

def test_needs_director_is_told_once_even_after_the_throttle_expires(h):
    h.card("t001", "needs_director", needs_director_reason="race in assign loop")
    assert len(about(h.cycle(), "t001")) == 1
    # スロットルが再送を許しても、同じ状態については黙っている。
    for _ in range(3):
        assert about(h.cycle(ttl_expired=True), "t001") == []


def test_needs_director_is_told_again_when_the_reason_changes(h):
    h.card("t001", "needs_director", needs_director_reason="first")
    assert len(about(h.cycle(), "t001")) == 1
    h.card("t001", "needs_director", needs_director_reason="second, different")
    msgs = about(h.cycle(), "t001")     # スロットルを消していないのに届く: 別の状態
    assert len(msgs) == 1 and "second, different" in msgs[0]
    assert about(h.cycle(ttl_expired=True), "t001") == []


def test_needs_director_left_and_re_entered_with_same_reason_is_told_again(h):
    """Director が pending に戻し、同じ理由でまた落ちた = 新しい事象。黙ってはいけない。"""
    h.card("t001", "needs_director", needs_director_reason="same reason")
    assert len(about(h.cycle(), "t001")) == 1
    h.card("t001", "pending")
    h.cycle(ttl_expired=True)           # 状態を離れたことを、通知側が観測する
    h.card("t001", "needs_director", needs_director_reason="same reason")
    assert len(about(h.cycle(ttl_expired=True), "t001")) == 1


def test_handoff_is_told_once_even_after_the_throttle_expires(h):
    h.card("t002", "failed", handoff_path="/tmp/handoff-t002.md")
    assert len(about(h.cycle(), "t002")) == 1
    for _ in range(3):
        assert about(h.cycle(ttl_expired=True), "t002") == []


def test_handoff_is_told_again_when_the_handoff_path_changes(h):
    h.card("t002", "failed", handoff_path="/tmp/handoff-a.md")
    assert len(about(h.cycle(), "t002")) == 1
    h.card("t002", "failed", handoff_path="/tmp/handoff-b.md")
    assert len(about(h.cycle(), "t002")) == 1


def test_handoff_left_and_re_entered_is_told_again(h):
    h.card("t002", "failed", handoff_path="/tmp/handoff-a.md")
    assert len(about(h.cycle(), "t002")) == 1
    h.card("t002", "pending")
    h.cycle(ttl_expired=True)
    h.card("t002", "failed", handoff_path="/tmp/handoff-a.md")
    assert len(about(h.cycle(ttl_expired=True), "t002")) == 1


def test_other_tasks_are_independent(h):
    h.card("t001", "needs_director", needs_director_reason="a")
    assert len(about(h.cycle(), "t001")) == 1
    h.card("t003", "needs_director", needs_director_reason="b")
    msgs = h.cycle(ttl_expired=True)
    assert about(msgs, "t001") == [] and len(about(msgs, "t003")) == 1


# -- 送れなかった通知は「伝えた」に数えない ------------------------------------

def test_a_failed_send_is_not_recorded_as_told(h):
    h.card("t001", "needs_director", needs_director_reason="x")
    FakeMux.send_ok = False
    assert h.cycle() == []
    FakeMux.send_ok = True
    assert len(about(h.cycle(), "t001")) == 1


def test_no_director_is_not_recorded_as_told(h):
    h.card("t001", "needs_director", needs_director_reason="x")
    FakeMux.directors = []
    assert h.cycle() == []
    FakeMux.directors = ["Sora-director"]
    assert len(about(h.cycle(), "t001")) == 1


# -- 記録の置き場: 「無い」と「使えない」を分ける ---------------------------------

def test_missing_store_is_a_normal_first_run_and_is_created(h):
    assert not h.told_file.exists()
    h.card("t001", "needs_director", needs_director_reason="x")
    assert len(about(h.cycle(), "t001")) == 1
    assert h.told_file.exists()
    assert "WARNING: notified-state" not in h.log_text()


def test_unwritable_store_degrades_to_the_throttle_and_says_so(h):
    """置き場を作れない/書けない = 起動失敗。「通知すべきものが無い」ではない。

    再送は止められない (スロットルだけに戻る) が、黙って壊れず、ログに出す。
    そして 5 秒ごとの連射にはならない (スロットルは効く)。
    """
    (h.registry / "daemons").write_text("i am a file, not a directory")
    h.card("t001", "needs_director", needs_director_reason="x")
    assert len(about(h.cycle(), "t001")) == 1
    assert about(h.cycle(), "t001") == []                       # スロットルは効いている
    assert len(about(h.cycle(ttl_expired=True), "t001")) == 1   # 見えない間は再送側に倒す
    assert "notified-state" in h.log_text()


def test_corrupt_store_is_reported_then_repaired_by_the_next_successful_record(h):
    """壊れた台帳は「無い」ではない: ログに出す。書けるなら次の記録で作り直す (自己修復)。"""
    h.told_file.parent.mkdir(parents=True)
    h.told_file.write_text("{ this is not json")
    h.card("t001", "needs_director", needs_director_reason="x")
    assert len(about(h.cycle(), "t001")) == 1        # 読めない間は再送側に倒す = 送る
    assert "notified-state" in h.log_text()          # 起動失敗として声を出す
    json.loads(h.told_file.read_text())              # 直っている
    assert about(h.cycle(ttl_expired=True), "t001") == []


def test_broken_store_write_never_causes_a_send_per_cycle(h):
    """記録が書けなくても、直後のサイクルで同じ通知が再度飛んではいけない。"""
    (h.registry / "daemons").write_text("file")
    h.card("t001", "needs_director", needs_director_reason="x")
    total = sum(len(about(h.cycle(), "t001")) for _ in range(5))
    assert total == 1


# ---------------------------------------------------------------------------
# #11: サイズ超過で拒否された codex-review task は再 spawn しない
# ---------------------------------------------------------------------------

def refuse(h, task_id="t010", pr="214", diff_bytes=412345, max_bytes=307200):
    lib_review_refusal.record(h.registry, SLUG, task_id, pr, diff_bytes, max_bytes)


def review_card(h, task_id="t010", status="pending", pr="214", **fields):
    h.card(task_id, status, skills="[codex-review]", pr_number=pr, **fields)


def test_a_codex_review_task_without_refusal_is_spawned(h):
    review_card(h)
    h.cycle()
    assert len(h.spawned) == 1 and "--pr" in h.spawned[0]


def test_a_refused_codex_review_task_is_not_respawned(h):
    review_card(h)
    refuse(h)
    for _ in range(3):
        h.cycle(ttl_expired=True)
        assert h.spawned == []


def test_refusal_tells_the_director_to_review_by_hand_once(h):
    review_card(h)
    refuse(h)
    msgs = about(h.cycle(), "t010")
    assert len(msgs) == 1
    m = msgs[0]
    assert "手動" in m and "差分レビュー" in m
    assert "412345" in m and "307200" in m          # 超過バイト数の材料
    assert "214" in m                               # PR 番号
    assert "lib_review_refusal.py clear" in m       # 意図して再試行する経路
    assert about(h.cycle(ttl_expired=True), "t010") == []


def test_needs_director_after_refusal_carries_the_same_instructions(h):
    """kai-review.sh が拒否 → plan.sh needs-director に倒した直後の通知。"""
    refuse(h)
    review_card(h, status="needs_director",
                needs_director_reason="NEEDS FIX: diff is 412345 bytes (> 307200)")
    msgs = about(h.cycle(), "t010")
    assert len(msgs) == 1
    assert "手動" in msgs[0] and "412345" in msgs[0] and "214" in msgs[0]
    assert about(h.cycle(ttl_expired=True), "t010") == []


def test_clearing_the_refusal_lets_the_director_retry_on_purpose(h):
    review_card(h)
    refuse(h)
    h.cycle()
    assert h.spawned == []
    assert lib_review_refusal.clear(h.registry, SLUG, "t010") is True
    h.cycle(ttl_expired=True)
    assert len(h.spawned) == 1


def test_a_different_pr_number_is_not_covered_by_the_old_refusal(h):
    """PR を分割して出し直した = 別の PR。拒否は効かない。"""
    review_card(h, pr="215")
    refuse(h, pr="214")
    h.cycle()
    assert len(h.spawned) == 1


def test_a_recreated_task_is_not_covered_by_the_old_refusal(h):
    review_card(h, task_id="t011")
    refuse(h, task_id="t010")
    h.cycle()
    assert len(h.spawned) == 1


def test_unreadable_refusal_holds_the_spawn(h):
    """壊れた記録を「拒否されていない」に倒すと、壊れた記録 1 枚でループが戻る。"""
    review_card(h)
    p = lib_review_refusal.refusal_path(h.registry, SLUG, "t010")
    p.parent.mkdir(parents=True)
    p.write_text("{ not json")
    msgs = about(h.cycle(), "t010")
    assert h.spawned == []
    assert len(msgs) == 1 and "読めない" in msgs[0]


def test_refusal_does_not_touch_other_review_tasks(h):
    review_card(h, task_id="t010")
    review_card(h, task_id="t012", pr="216")
    refuse(h, task_id="t010")
    h.cycle()
    assert len(h.spawned) == 1 and "216" in h.spawned[0]


# ---------------------------------------------------------------------------
# lib_review_refusal (record / load / clear)
# ---------------------------------------------------------------------------

def test_refusal_record_round_trip(tmp_path):
    lib_review_refusal.record(tmp_path, "m", "t1", "9", 500, 300)
    rec = lib_review_refusal.load(tmp_path, "m", "t1")
    assert rec["pr"] == "9" and rec["diff_bytes"] == 500 and rec["max_bytes"] == 300
    assert lib_review_refusal.refused_for_pr(rec, 9)
    assert not lib_review_refusal.refused_for_pr(rec, 10)


def test_refusal_absent_is_missing_not_unreadable_generic(tmp_path):
    from lib_task_cards import is_missing, is_unreadable
    rec = lib_review_refusal.load(tmp_path, "m", "t1")
    assert is_unreadable(rec) and is_missing(rec)


@pytest.mark.parametrize("body", ["{", "[]", '{"mission": "m"}'])
def test_refusal_malformed_is_unreadable_but_not_missing(tmp_path, body):
    from lib_task_cards import is_missing, is_unreadable
    p = lib_review_refusal.refusal_path(tmp_path, "m", "t1")
    p.parent.mkdir(parents=True)
    p.write_text(body)
    rec = lib_review_refusal.load(tmp_path, "m", "t1")
    assert is_unreadable(rec) and not is_missing(rec)


@pytest.mark.parametrize("bad", ["", "a/b", ".."])
def test_refusal_path_rejects_unsafe_names(tmp_path, bad):
    with pytest.raises(ValueError):
        lib_review_refusal.refusal_path(tmp_path, bad, "t1")
