#!/usr/bin/env python3
"""dispatcher の 1 サイクルで段階上げが動くこと (PR-B / 設計 §5・§9)。

本物の dispatcher.sh の埋め込み python を名前空間に読み込み (`tests/test_dispatcher_notify_once.py` の Harness)、
`run_telegram_cycle()` → `dispatch()` を回す。mux は FakeMux、Bot API は偽サーバー (`tests/telegram_fake_api.py`)。
「N 分たった」は台帳の `first_seen` を書き換えて表す (壁時計を触らない)。

再現する事故: t017 が needs_director のまま 10.5 時間止まり、後続の t006 が blocked のままだった
(修正前は最初の `[needs_director]` 1 通のあとは何も届かない)。

実行: python3 -m pytest tests/test_escalation_dispatcher_cycle.py -q
"""

import json
import re
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent / "scripts"))

from telegram_fake_api import FakeBotApi  # noqa: E402
from test_dispatcher_notify_once import DISPATCHER_SH, FakeMux, SLUG  # noqa: E402
from test_telegram_dispatcher_glue import CHAT, TOKEN, TgHarness  # noqa: E402
import lib_telegram  # noqa: E402

EX = "ex-" + "c" * 32


class EscHarness(TgHarness):
    def __init__(self, root, monkeypatch, carried=True, **kw):
        super().__init__(root, monkeypatch, carried=carried, **kw)
        self.carried = carried

    def cycle(self, **kw):
        FakeMux.sent = []
        if self.carried:    # 本番は dispatcher.sh がサイクルごとに前置代入で運ぶ。import 時に pop されるので毎回戻す
            self.monkeypatch.setenv("_CREWVIA_TG_RESOLVED_TOKEN", TOKEN)
            self.monkeypatch.setenv("_CREWVIA_TG_RESOLVED_CHAT_ID", CHAT)
        src = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", DISPATCHER_SH.read_text(), re.DOTALL).group(1)
        src, n = re.subn(r"\n# --- CYCLE ENTRY POINT ---\n.*$", "", src, flags=re.S)
        assert n == 1
        self.monkeypatch.setattr(sys, "argv", [
            "dispatcher", str(self.queue), str(self.registry), str(self.notify_cache), "300", "60", str(self.log)])
        ns = {"__name__": "dispatcher_under_test"}
        exec(compile(src, "dispatcher.sh (embedded, test)", "exec"), ns)
        self.ns = ns
        ns["run_telegram_cycle"]()
        ns["dispatch"]()
        return [m["message"] for m in FakeMux.sent]

    @property
    def ledger_path(self):
        return self.daemons / "escalation-state.json"

    def ledger(self):
        return json.loads(self.ledger_path.read_text())

    def age(self, seconds, key=None):
        data = self.ledger()
        for k, e in data.items():
            if key is None or k == key:
                e["first_seen"] -= seconds
        self.ledger_path.write_text(json.dumps(data))


@pytest.fixture
def api(monkeypatch):
    with FakeBotApi(token=TOKEN) as a:
        monkeypatch.setitem(lib_telegram.send_message.__kwdefaults__, "api_base", a.url)
        yield a


@pytest.fixture
def h(tmp_path, monkeypatch, api):
    h = EscHarness(tmp_path / "repo", monkeypatch)
    h.configure()
    return h


def stuck_with_follower(h):
    h.card("t017", "needs_director", needs_director_reason="Codex NEEDS FIX: race", current_execution_id=EX)
    h.card("t006", "pending", blocked_by=["t017"])


def escalations(msgs):
    return [m for m in msgs if m.startswith("[escalation]")]


def sends(api):
    return api.calls_of("sendMessage")


def test_the_incident_nothing_after_the_first_notice_before_this_pr_now_5_and_10_minutes(h, api):
    stuck_with_follower(h)
    first = h.cycle()
    assert [m for m in first if "[needs_director]" in m] and not escalations(first) and not sends(api)
    assert h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 0

    h.age(299)
    assert not escalations(h.cycle()) and not sends(api)

    h.age(2)                                                      # 301 秒 — 段階 1
    msgs = h.cycle()
    assert len(escalations(msgs)) == 1
    text = escalations(msgs)[0]
    assert "t017 (needs_director) — 止まって 5 分" in text and "後続 1 件が待機中: t006" in text
    assert "Codex NEEDS FIX: race" in text and not sends(api)
    for _ in range(3):                                            # 1 回だけ
        assert not escalations(h.cycle())

    h.age(300)                                                    # 601 秒 — 段階 2
    msgs = h.cycle()
    assert not escalations(msgs)
    (payload,) = sends(api)
    assert payload["chat_id"] == CHAT and "t017 (needs_director)" in payload["text"] and "parse_mode" not in payload
    assert payload["text"].startswith("🛑 crewvia: Director の判断待ちが続いています")
    for _ in range(3):
        h.cycle()
    assert len(sends(api)) == 1 and h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 2


def test_the_last_task_with_no_followers_still_escalates(h, api):
    h.card("t017", "needs_director", needs_director_reason="x", current_execution_id=EX)
    h.cycle()
    h.age(301)
    (text,) = escalations(h.cycle())
    assert "後続" not in text
    h.age(300)
    h.cycle()
    assert len(sends(api)) == 1


def test_needs_human_review_is_escalated_too(h, api):
    h.card("t017", "needs_human_review", needs_director_reason="x", current_execution_id=EX, worker="Ren")
    h.cycle()
    h.age(301)
    assert len(escalations(h.cycle())) == 1


def test_without_telegram_stage_one_still_arrives_and_nothing_else(tmp_path, monkeypatch, api):
    h = EscHarness(tmp_path / "repo", monkeypatch, carried=False)
    h.configure(source="")
    stuck_with_follower(h)
    h.cycle()
    h.age(301)
    assert len(escalations(h.cycle())) == 1
    h.age(600)
    assert not escalations(h.cycle()) and not sends(api)
    assert h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 1


def test_with_no_director_telegram_still_goes_at_10_minutes(h, api):
    stuck_with_follower(h)
    h.cycle()
    FakeMux.directors = []
    h.age(301)
    assert not escalations(h.cycle()) and not sends(api)          # 段階 1 は見送り (記録しない)
    assert h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 0
    h.age(300)
    h.cycle()
    assert len(sends(api)) == 1 and h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 2
    FakeMux.directors = ["Sora-director"]
    assert not escalations(h.cycle())                              # 戻っても段階 1 は出ない (1 回だけ)


def test_director_send_failure_is_retried_and_marks_stage_one_failed(h, api):
    stuck_with_follower(h)
    h.cycle()
    h.age(301)
    FakeMux.send_ok = False
    assert not escalations(h.cycle())
    entry = h.ledger()[f"{SLUG}/t017"]
    assert entry["stage_sent"] == 0 and entry["stage1_failed_at"] is not None
    FakeMux.send_ok = True
    assert len(escalations(h.cycle())) == 1


def test_telegram_failure_is_not_recorded_and_respects_the_backoff(h, api):
    stuck_with_follower(h)
    h.cycle()
    h.age(301)
    h.cycle()
    api.method_modes["sendMessage"] = "http500"
    h.age(300)
    h.cycle()
    assert len(sends(api)) == 1 and h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 1
    for _ in range(3):
        h.cycle()
    assert len(sends(api)) == 1, "バックオフの間は 5 秒ごとに叩かない"
    api.method_modes.clear()
    send_state = h.daemons / "telegram-send.json"
    data = json.loads(send_state.read_text())
    data["backoff_until"] = 0
    send_state.write_text(json.dumps(data))
    h.cycle()
    assert len(sends(api)) == 2 and h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 2


def test_resolving_the_card_clears_the_ledger_and_a_new_attempt_starts_over(h, api):
    stuck_with_follower(h)
    h.cycle()
    h.age(301)
    h.cycle()
    h.card("t017", "pending")
    h.cycle()
    assert f"{SLUG}/t017" not in h.ledger()
    h.card("t017", "needs_director", needs_director_reason="again", current_execution_id="ex-" + "d" * 32)
    h.cycle()
    assert h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 0
    h.age(301)
    assert len(escalations(h.cycle())) == 1                        # 新しい試行はまた 1 回


def test_unreadable_ledger_stays_silent_and_warns(h, api):
    stuck_with_follower(h)
    h.cycle()
    h.age(9999)
    h.ledger_path.write_text("{not json")
    for _ in range(2):
        assert not escalations(h.cycle())
    assert not sends(api) and "escalation-state" in h.log_text()
    h.ledger_path.unlink()                                          # 消すのが復旧手順
    h.cycle()
    assert h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 0


def test_a_corrupt_card_in_the_mission_blocks_escalation_until_it_is_readable(h, api):
    stuck_with_follower(h)
    h.cycle()
    h.age(9999)
    (h.tasks / "t099.md").write_text("---\nid: t099\n: : broken\n---\n")
    msgs = h.cycle()
    assert not escalations(msgs) and not sends(api)
    (h.tasks / "t099.md").unlink()
    assert len(escalations(h.cycle())) == 1                         # 戻ったら経過どおり (1 サイクル 1 段階)


def test_restart_keeps_the_clock_because_the_ledger_is_on_disk(h, api):
    stuck_with_follower(h)
    h.cycle()
    h.age(250)
    h.cycle()                                                       # 別の python (= dispatcher の再起動と同じ)
    h.age(60)
    assert len(escalations(h.cycle())) == 1


def test_at_most_three_telegram_notices_per_cycle(h, api):
    for i in range(1, 6):
        h.card(f"t0{i:02d}", "needs_director", needs_director_reason="x", current_execution_id="ex-" + str(i) * 32)
    h.cycle()
    h.age(301)
    h.cycle()
    h.age(300)
    h.cycle()
    assert len(sends(api)) == 3
    h.cycle()
    assert len(sends(api)) == 5


def test_token_never_appears_in_logs_ledger_or_director_messages(h, api):
    stuck_with_follower(h)
    h.cycle()
    h.age(301)
    msgs = h.cycle()
    h.age(300)
    msgs += h.cycle()
    blob = h.log_text() + h.ledger_path.read_text() + "".join(msgs) + "".join(m["message"] for m in FakeMux.sent)
    assert TOKEN not in blob and CHAT not in blob


def test_unwritable_ledger_sends_nothing_and_tells_the_director_once(h, api, monkeypatch):
    import lib_escalation
    stuck_with_follower(h)
    h.cycle()
    h.age(9999)
    monkeypatch.setattr(lib_escalation, "write_ledger", lambda *a, **k: False)
    got = []
    for _ in range(4):
        got += h.cycle()
    assert not sends(api) and not [m for m in got if "Director の判断待ちが続いています" in m]
    troubles = [m for m in got if m.startswith("[escalation] escalation-state に書けない")]
    assert len(troubles) == 1, "間引きは台帳ではなく notify cache (プロセスをまたぐ) にある"
