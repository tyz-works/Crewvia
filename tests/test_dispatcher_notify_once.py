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
        self.ns = ns    # 直近のサイクルの名前空間 (関数を直接呼ぶテスト用)
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
    """Director が pending に戻し、同じ理由でまた落ちた = 新しい事象。黙ってはいけない。

    **スロットルキャッシュを消さない** (t021 / Kai P2)。以前のこのテストは離脱・再入の
    サイクルで `ttl_expired=True` (= キャッシュ削除) を使っており、離脱時に台帳の記録は
    捨てるのに `<key>#<fp>` のスロットルが NOTIFY_TTL 生き残る穴を隠して緑になっていた。
    """
    h.card("t001", "needs_director", needs_director_reason="same reason")
    assert len(about(h.cycle(), "t001")) == 1
    h.card("t001", "pending")
    h.cycle()                           # 状態を離れたことを、通知側が観測する
    h.card("t001", "needs_director", needs_director_reason="same reason")
    assert h.notify_cache.exists()      # スロットルは生きている (削除して緑にしていない)
    assert len(about(h.cycle(), "t001")) == 1


def test_needs_director_reason_A_then_B_then_A_is_told_each_time(h):
    """A → B → A: 3 回目の A は 1 回目の A と同じ fingerprint だが、新しい事象。"""
    h.card("t001", "needs_director", needs_director_reason="A")
    assert len(about(h.cycle(), "t001")) == 1
    h.card("t001", "needs_director", needs_director_reason="B")
    assert len(about(h.cycle(), "t001")) == 1
    h.card("t001", "needs_director", needs_director_reason="A")
    assert h.notify_cache.exists()
    msgs = about(h.cycle(), "t001")
    assert len(msgs) == 1 and "理由: A" in msgs[0]
    assert about(h.cycle(), "t001") == []       # 同じ状態が続くあいだは黙る


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
    h.cycle()                           # スロットルキャッシュは消さない (上のテストと同じ理由)
    h.card("t002", "failed", handoff_path="/tmp/handoff-a.md")
    assert h.notify_cache.exists()
    assert len(about(h.cycle(), "t002")) == 1


def test_handoff_path_A_then_B_then_A_is_told_each_time(h):
    h.card("t002", "failed", handoff_path="/tmp/handoff-a.md")
    assert len(about(h.cycle(), "t002")) == 1
    h.card("t002", "failed", handoff_path="/tmp/handoff-b.md")
    assert len(about(h.cycle(), "t002")) == 1
    h.card("t002", "failed", handoff_path="/tmp/handoff-a.md")
    assert len(about(h.cycle(), "t002")) == 1
    assert about(h.cycle(), "t002") == []


def test_throttle_still_holds_while_the_state_is_unchanged(h):
    """離脱時にスロットルを消す修正が、同じ状態の連射を許してはいけない (台帳が書けないとき)。"""
    (h.registry / "daemons").write_text("file")     # 台帳が使えない = スロットルだけが頼り
    h.card("t001", "needs_director", needs_director_reason="x")
    assert sum(len(about(h.cycle(), "t001")) for _ in range(5)) == 1


def test_other_tasks_are_independent(h):
    h.card("t001", "needs_director", needs_director_reason="a")
    assert len(about(h.cycle(), "t001")) == 1
    h.card("t003", "needs_director", needs_director_reason="b")
    msgs = h.cycle(ttl_expired=True)
    assert about(msgs, "t001") == [] and len(about(msgs, "t003")) == 1


# -- 観測できなかったときは、台帳もスロットルも捨てない (t023 / Kai 2 巡目 P2) -----------
#
# 「空 (もう無い)」と「観測不能 (見られなかった)」を同じに扱う型。prune_told() は
# 「観測できた mission の、live に無い key」を捨てる。live key を別の走査で集めていると、
# 走査 (all_tasks) は成功・その後の走査が失敗したとき、key が 1 件も集まらないのに
# mission は「観測できた」扱いになり、台帳とスロットルを捨てる。回復すると再通知される。

def handoff_throttle_keys(h, task_id):
    cache = json.loads(h.notify_cache.read_text()) if h.notify_cache.exists() else {}
    return [k for k in cache if k.startswith(f"handoff_{SLUG}_{task_id}#")]


def told_keys(h):
    return list(json.loads(h.told_file.read_text())) if h.told_file.exists() else []


def make_later_scans_fail(monkeypatch):
    """`list_task_cards` を「サイクルの 1 回目の走査だけ成功・2 回目以降は走査失敗」にする。

    dispatch() が mission を何回走査しても、1 サイクル内では最初の 1 回しか本物を返さない。
    走査を 1 つのスナップショットにまとめていれば、失敗する 2 回目は存在しない。
    """
    import lib_task_cards
    real = lib_task_cards.list_task_cards
    state = {"calls": 0}

    def flaky(tasks_dir, warn=None):
        state["calls"] += 1
        if state["calls"] == 1:
            return real(tasks_dir, warn=warn)
        return [lib_task_cards.scan_failure_task(tasks_dir, "listing error", "injected")]

    monkeypatch.setattr(lib_task_cards, "list_task_cards", flaky)
    return state


def test_handoff_ledger_and_throttle_survive_a_corrupt_card(h):
    """failed+handoff のカード自体が壊れて読めない = 観測不能。「もう failed でない」ではない。"""
    h.card("t002", "failed", handoff_path="/tmp/handoff-t002.md")
    assert len(about(h.cycle(), "t002")) == 1
    assert told_keys(h) == [f"handoff_{SLUG}_t002"]
    assert handoff_throttle_keys(h, "t002")

    h.card("t999", "failed", handoff_path="/tmp/handoff-t002.md")
    (h.tasks / "t999.md").replace(h.tasks / "t002.md")      # id がファイル名と食い違う = [破損]
    assert about(h.cycle(), "t002") == []
    assert told_keys(h) == [f"handoff_{SLUG}_t002"]         # 台帳は捨てない
    assert handoff_throttle_keys(h, "t002")                 # スロットルも捨てない

    h.card("t002", "failed", handoff_path="/tmp/handoff-t002.md")              # 読み取りが回復
    assert about(h.cycle(), "t002") == []                   # スロットルだけで黙っている
    assert about(h.cycle(ttl_expired=True), "t002") == []   # 台帳が黙らせている: 再通知されない


def test_handoff_ledger_and_throttle_survive_a_scan_failure(h):
    """mission の tasks/ を走査できない (非終端のプレースホルダが返る) = 観測不能。"""
    h.card("t002", "failed", handoff_path="/tmp/handoff-t002.md")
    assert len(about(h.cycle(), "t002")) == 1

    backup = h.tasks.with_name("tasks.bak")
    h.tasks.rename(backup)
    h.tasks.write_text("not a directory")                   # 走査失敗 (ENOTDIR)
    try:
        assert h.cycle() == []
        assert told_keys(h) == [f"handoff_{SLUG}_t002"]
        assert handoff_throttle_keys(h, "t002")
    finally:
        h.tasks.unlink()
        backup.rename(h.tasks)
    assert about(h.cycle(ttl_expired=True), "t002") == []   # 回復後に再通知されない


def test_handoff_detection_and_pruning_share_one_snapshot(h, monkeypatch):
    """Kai 2 巡目 P2 そのもの: 1 回目の走査は成功・2 回目は失敗するとき、台帳を捨てない。

    走査が 1 回なら 2 回目は存在せず、handoff 検知と pruning は同じ結果を見る。
    """
    h.card("t002", "failed", handoff_path="/tmp/handoff-t002.md")
    assert len(about(h.cycle(), "t002")) == 1

    calls = make_later_scans_fail(monkeypatch)
    assert about(h.cycle(), "t002") == []
    assert told_keys(h) == [f"handoff_{SLUG}_t002"]
    assert handoff_throttle_keys(h, "t002")
    assert calls["calls"] == 1, "mission は 1 サイクルに 1 回だけ走査する (スナップショットは 1 つ)"


def test_a_task_that_really_left_the_state_is_still_pruned(h):
    """観測できたうえで failed でなくなったものは、これまでどおり捨てる (捨てすぎの逆側)。"""
    h.card("t002", "failed", handoff_path="/tmp/handoff-t002.md")
    assert len(about(h.cycle(), "t002")) == 1
    h.card("t002", "pending")
    h.cycle()
    assert told_keys(h) == []
    assert handoff_throttle_keys(h, "t002") == []


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


# -- 安いスキップを高い判定より前に (t021 / QA t011 の P3) -------------------------

def count_director_lookups(h, monkeypatch):
    """`mux list -director` の呼び出し回数を数える口 (送信先の解決 `_director_name()` も含む)。"""
    calls = []
    orig = FakeMux.list

    def counting(self, *a, suffix=None, **kw):
        if suffix == "-director":
            calls.append(1)
        return orig(self, *a, suffix=suffix, **kw)
    monkeypatch.setattr(FakeMux, "list", counting)
    return calls


def test_an_idle_cycle_does_not_ask_the_mux_whether_a_director_is_live(h, monkeypatch):
    """通知対象が 1 件も無いサイクルで mux を叩かない (5 秒ごとに回るので、積もる)。"""
    calls = count_director_lookups(h, monkeypatch)
    h.cycle()
    assert calls == []


def test_a_cycle_with_only_already_told_states_does_not_ask_the_mux(h, monkeypatch):
    h.card("t001", "needs_director", needs_director_reason="x")
    h.card("t002", "failed", handoff_path="/tmp/handoff-t002.md")
    h.cycle()                                   # ここで 1 回だけ伝える
    calls = count_director_lookups(h, monkeypatch)
    assert h.cycle() == [] and calls == []      # 台帳が「伝えた」と言っている間は問い合わせない


def test_liveness_is_looked_up_once_per_cycle_however_many_notices(h, monkeypatch):
    h.card("t001", "needs_director", needs_director_reason="x")
    h.card("t002", "failed", handoff_path="/tmp/handoff-t002.md")
    calls = count_director_lookups(h, monkeypatch)
    assert len(h.cycle()) == 2
    assert len(calls) == 1 + 2      # 生存確認 1 回 + 送信先の解決 (送るたびに 1 回)


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


def write_refusal(h, task_id="t010", **overrides):
    """`record()` を通さずに記録を書く (壊れた/不正な値の記録を作るため)。"""
    body = {"mission": SLUG, "task": task_id, "pr": "214", "diff_bytes": 412345,
            "max_bytes": 307200, "refused_at": "2026-09-25T00:00:00Z"}
    body.update(overrides)
    p = lib_review_refusal.refusal_path(h.registry, SLUG, task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body))


# t021 (Kai P2): フィールドが「在る」だけで受理していた。値が不正な記録は、
#   - diff_bytes:null → describe() が TypeError → **dispatch サイクル全体が落ちる**
#     (後続タスクと通知が全 mission で止まる)
#   - pr が不正 → 拒否チェックを**迂回**して再 spawn ループが戻る
# のどちらかになる。読めない記録は「拒否されていないと証明できない」= spawn を保留。
INVALID_REFUSAL_VALUES = [
    {"diff_bytes": None},
    {"max_bytes": None},
    {"diff_bytes": "412345"},          # 文字列 (型が違う)
    {"diff_bytes": True},              # bool は int の亜種だが数ではない
    {"diff_bytes": -1},
    {"max_bytes": -5},
    {"diff_bytes": 1.5},
    {"pr": None},
    {"pr": ""},
    {"pr": "abc"},
    {"pr": "0"},
    {"pr": -3},
    {"pr": 214.5},
    {"pr": ["214"]},
]


@pytest.mark.parametrize("bad", INVALID_REFUSAL_VALUES, ids=lambda d: repr(d))
def test_invalid_refusal_values_hold_the_spawn_and_do_not_crash_the_cycle(h, bad):
    review_card(h)
    review_card(h, task_id="t012", pr="216")          # 後続のタスクも処理されなければならない
    write_refusal(h, **bad)
    msgs = h.cycle()                                  # 例外を出さない
    assert not any("--pr" in c and "214" in c for c in h.spawned)       # 保留 (「拒否されていない」に倒さない)
    assert len(about(msgs, "t010")) == 1 and "読めない" in about(msgs, "t010")[0]
    assert any("216" in c for c in h.spawned)         # 後続の task は止まらない


@pytest.mark.parametrize("bad", [{"mission": "some-other-mission"}, {"task": "t999"},
                                 {"mission": None}, {"task": 10}],
                         ids=lambda d: repr(d))
def test_a_refusal_record_for_another_task_is_not_accepted_as_this_ones(h, bad):
    """ファイルのコピー/取り違え。記録の自己申告 (mission/task) が置き場所と食い違えば別物。"""
    review_card(h)
    write_refusal(h, **bad)
    msgs = h.cycle()
    assert h.spawned == []
    assert len(about(msgs, "t010")) == 1 and "読めない" in about(msgs, "t010")[0]


def test_integer_pr_in_a_refusal_record_is_valid(h):
    review_card(h)
    write_refusal(h, pr=214)
    msgs = about(h.cycle(), "t010")
    assert h.spawned == [] and len(msgs) == 1 and "手動" in msgs[0]


def test_refusal_does_not_touch_other_review_tasks(h):
    review_card(h, task_id="t010")
    review_card(h, task_id="t012", pr="216")
    refuse(h, task_id="t010")
    h.cycle()
    assert len(h.spawned) == 1 and "216" in h.spawned[0]


# ---------------------------------------------------------------------------
# t036: pr_number の無い codex-review が ready なら、Director に 1 回だけ知らせる
# ---------------------------------------------------------------------------
#
# 実装 Worker が `plan.sh done --pr` を付け忘れると、codex-review は pending のまま
# spawn できない。以前は log() だけで、Director には何も届かなかった。

def no_pr_card(h, task_id="t010", status="pending", **fields):
    h.card(task_id, status, skills="[codex-review]", **fields)     # pr_number 欄なし


def test_ready_codex_review_without_pr_number_is_told_to_the_director_once(h):
    no_pr_card(h)
    msgs = about(h.cycle(), "t010")
    assert len(msgs) == 1
    m = msgs[0]
    assert "pr_number" in m and SLUG in m
    assert f"plan.sh update t010 --pr-number <N> --status pending --mission {SLUG}" in m
    assert h.spawned == [], "番号が無いので spawn はしない"
    for _ in range(3):
        assert about(h.cycle(ttl_expired=True), "t010") == []


def test_no_pr_notice_is_not_sent_for_a_blocked_codex_review(h):
    """blocked は Director が意図して止めている。通知しない。"""
    no_pr_card(h, status="blocked", blocked_reason="PR 番号待ち")
    assert about(h.cycle(), "t010") == []
    assert about(h.cycle(ttl_expired=True), "t010") == []


def test_no_pr_notice_is_not_sent_when_pr_number_is_set(h):
    review_card(h)
    assert about(h.cycle(), "t010") == []
    assert len(h.spawned) == 1


def test_no_pr_notice_is_sent_for_an_empty_pr_number_too(h):
    review_card(h, pr=None)                     # pr_number: null
    assert len(about(h.cycle(), "t010")) == 1


def test_no_pr_notice_is_told_again_when_the_state_is_left_and_re_entered(h):
    no_pr_card(h)
    assert len(about(h.cycle(), "t010")) == 1
    review_card(h)                              # Director が番号を入れた = 状態を離れる
    h.cycle()
    no_pr_card(h)                               # 番号を消してまた ready (新しい事象)
    assert h.notify_cache.exists()
    assert len(about(h.cycle(), "t010")) == 1


def test_no_pr_notice_waits_for_a_live_director_without_recording(h):
    no_pr_card(h)
    FakeMux.directors = []
    assert h.cycle() == []
    FakeMux.directors = ["Sora-director"]
    assert len(about(h.cycle(), "t010")) == 1   # 戻ったらすぐ届く (記録していない)


def test_no_pr_notice_is_per_task(h):
    no_pr_card(h, task_id="t010")
    no_pr_card(h, task_id="t011")
    msgs = h.cycle()
    assert len(about(msgs, "t010")) == 1 and len(about(msgs, "t011")) == 1


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


@pytest.mark.parametrize("bad", INVALID_REFUSAL_VALUES, ids=lambda d: repr(d))
def test_refusal_load_rejects_invalid_field_values(tmp_path, bad):
    from lib_task_cards import is_missing, is_unreadable
    body = {"mission": "m", "task": "t1", "pr": "9", "diff_bytes": 500, "max_bytes": 300}
    body.update(bad)
    p = lib_review_refusal.refusal_path(tmp_path, "m", "t1")
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(body))
    rec = lib_review_refusal.load(tmp_path, "m", "t1")
    assert is_unreadable(rec) and not is_missing(rec)


def test_refusal_load_rejects_a_record_that_names_another_task(tmp_path):
    from lib_task_cards import is_missing, is_unreadable
    lib_review_refusal.record(tmp_path, "m", "t1", "9", 500, 300)
    src = lib_review_refusal.refusal_path(tmp_path, "m", "t1")
    dst = lib_review_refusal.refusal_path(tmp_path, "m", "t2")
    dst.write_text(src.read_text())               # t1 の記録を t2 の置き場所にコピー
    rec = lib_review_refusal.load(tmp_path, "m", "t2")
    assert is_unreadable(rec) and not is_missing(rec)


def test_refusal_load_accepts_valid_int_and_str_pr(tmp_path):
    for pr in (9, "9"):
        p = lib_review_refusal.refusal_path(tmp_path, "m", "t1")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"mission": "m", "task": "t1", "pr": pr,
                                 "diff_bytes": 500, "max_bytes": 300}))
        rec = lib_review_refusal.load(tmp_path, "m", "t1")
        assert isinstance(rec, dict)
        assert lib_review_refusal.refused_for_pr(rec, 9)
        assert lib_review_refusal.describe(rec)      # 例外を出さない


@pytest.mark.parametrize("bad", ["", "a/b", ".."])
def test_refusal_path_rejects_unsafe_names(tmp_path, bad):
    with pytest.raises(ValueError):
        lib_review_refusal.refusal_path(tmp_path, bad, "t1")
