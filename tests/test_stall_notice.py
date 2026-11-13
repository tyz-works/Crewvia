#!/usr/bin/env python3
"""mission の停滞通知 (lib_stall。t007)。

事例 (2026-11-13): review が要修正で done → 後続を Director が手で blocked → 進行中も割り当てられる task も 0 件
なのに mission は done でない → 10h40m 無言で静止。`classify()` は純粋関数、`run_cycle()` は本物のまま
(通知台帳の代わりに `notify` を差し込む)。

実行: python3 -m pytest tests/test_stall_notice.py -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import lib_stall as S  # noqa: E402
from lib_task_cards import is_unreadable  # noqa: E402
from lib_task_status import TASK_STATUSES  # noqa: E402

SLUG = "m1"


def card(tid, status, **kw):
    meta = {"id": tid, "status": status, "skills": ["code"], "blocked_by": [], "released_deps": []}
    meta.update(kw)
    return meta


def classify(cards):
    done = {c["id"] for c in cards if c["status"] in ("done", "verified", "skipped")}
    statuses = {c["id"]: c["status"] for c in cards}
    return S.classify(cards, done, statuses)


# ---------------------------------------------------------------- classify

def test_review_done_and_followers_blocked_is_a_stall():
    cards = [
        card("t001", "done", completed_at="2026-11-13T01:00:00Z"),
        card("t002", "done", completed_at="2026-11-13T02:00:00Z"),
        card("t003", "blocked", blocked_by=["t002"], blocked_reason="要修正"),
        card("t005", "blocked", blocked_by=["t002"], blocked_reason="要修正"),
    ]
    stall = classify(cards)
    assert stall is not None
    assert [t for t, _s, _w in stall.stuck] == ["t003", "t005"]
    assert stall.last_done == "t002"
    assert "要修正" in stall.stuck[0][2]


@pytest.mark.parametrize("status", ["in_progress", "verifying", "ready_for_verification"])
def test_progressing_task_means_not_stalled(status):
    assert classify([card("t001", status), card("t002", "blocked")]) is None


def test_assignable_pending_means_not_stalled():
    assert classify([card("t001", "done"), card("t002", "pending", blocked_by=["t001"]),
                     card("t003", "blocked")]) is None


def test_pending_with_unmet_dependency_is_stuck():
    stall = classify([card("t001", "blocked"), card("t002", "pending", blocked_by=["t001"])])
    assert stall is not None
    why = dict((t, w) for t, _s, w in stall.stuck)
    assert "t001" in why["t002"]


def test_failed_dependency_is_held_and_says_release_dep():
    stall = classify([card("t001", "failed"), card("t002", "pending", blocked_by=["t001"])])
    assert stall is not None
    why = dict((t, w) for t, _s, w in stall.stuck)
    assert "release-dep" in why["t002"]


def test_pending_without_skills_is_stuck():
    stall = classify([card("t001", "pending", skills=[])])
    assert stall is not None


def test_all_done_is_not_a_stall():
    assert classify([card("t001", "done"), card("t002", "skipped")]) is None


def test_no_cards_is_not_a_stall():
    assert classify([]) is None


@pytest.mark.parametrize("status", ["needs_director", "needs_human_review"])
def test_only_awaiting_decision_is_left_to_escalation(status):
    assert classify([card("t001", "done"), card("t002", status)]) is None


def test_awaiting_decision_plus_blocked_is_still_a_stall():
    assert classify([card("t001", "needs_director"), card("t002", "blocked")]) is not None


def test_unknown_status_is_not_judged():
    assert classify([card("t001", "cancelled"), card("t002", "blocked")]) is None


def test_every_status_in_vocabulary_classifies_without_error():
    for status in TASK_STATUSES:
        classify([card("t001", status)])


def test_message_is_one_line_and_sanitised():
    stall = classify([card("t001", "blocked", blocked_reason="a\nb\x1b[31m c")])
    text = S.message(SLUG, stall, 1800)
    assert "\n" not in text and "\x1b" not in text and " " not in text
    assert "30 分" in text and "t001" in text
    assert "plan.sh" in text and "reopen" in text


def test_message_truncates_long_lists():
    stall = classify([card(f"t{n:03d}", "blocked") for n in range(1, 13)])
    assert "ほか 4 件" in S.message(SLUG, stall, 60)


def test_fingerprint_changes_with_status_and_set():
    a = classify([card("t001", "blocked")])
    b = classify([card("t001", "failed")])
    c = classify([card("t001", "blocked"), card("t002", "blocked")])
    assert len({S.fingerprint(SLUG, x) for x in (a, b, c)}) == 3
    assert S.fingerprint(SLUG, a) == S.fingerprint(SLUG, classify([card("t001", "blocked", blocked_reason="x")]))


# ---------------------------------------------------------------- run_cycle

class Env:
    def __init__(self, tmp_path):
        self.reg = tmp_path / "registry"
        (self.reg / "daemons").mkdir(parents=True)
        self.sent = []
        self.live = set()
        self.cards = [card("t001", "done"), card("t002", "blocked")]
        self.missions = [SLUG]
        self.observed = {SLUG}
        self.notify_ok = True

    def notify(self, key, fp, slug, text):
        if not self.notify_ok:
            return False
        self.sent.append((key, fp, slug, text))
        return True

    def run(self, now, threshold=900):
        all_tasks = [(SLUG, c) for c in self.cards]
        done = {SLUG: {c["id"] for c in self.cards if c["status"] in ("done", "verified", "skipped")}}
        statuses = {SLUG: {c["id"]: c["status"] for c in self.cards}}
        return S.run_cycle(self.reg, all_tasks, done, statuses, self.missions, self.observed, now, threshold,
                           notify=self.notify, live_keys=self.live)

    def ledger_path(self):
        return self.reg / "daemons" / S.LEDGER_NAME


def test_notifies_only_after_threshold(tmp_path):
    env = Env(tmp_path)
    assert env.run(1000) == []
    assert env.run(1000 + 899) == []
    assert env.run(1000 + 900) == [SLUG]
    assert len(env.sent) == 1
    assert env.sent[0][0] == S.told_key(SLUG)
    assert S.told_key(SLUG) in env.live


def test_threshold_zero_or_negative_disables_and_writes_nothing(tmp_path):
    env = Env(tmp_path)
    for th in (0, -1):
        assert env.run(1000, th) == []
        assert env.run(99999, th) == []
    assert env.sent == [] and not env.ledger_path().exists() and env.live == set()


def test_clock_resets_when_stall_resolves(tmp_path):
    env = Env(tmp_path)
    env.run(1000)
    env.cards[1]["status"] = "in_progress"
    env.run(1500)
    assert json.loads(env.ledger_path().read_text()) == {}
    env.cards[1]["status"] = "blocked"
    assert env.run(1600) == []                       # 時計は 1600 から
    assert env.run(1600 + 900) == [SLUG]


def test_state_change_restarts_the_clock(tmp_path):
    env = Env(tmp_path)
    env.run(1000)
    env.cards.append(card("t003", "blocked"))
    assert env.run(1900) == []                       # 集合が変わった → 新しい時計
    assert env.run(1900 + 900) == [SLUG]


def test_unobserved_mission_keeps_clock_and_sends_nothing(tmp_path):
    env = Env(tmp_path)
    env.run(1000)
    env.observed = set()
    assert env.run(5000) == []
    assert SLUG in json.loads(env.ledger_path().read_text())
    env.observed = {SLUG}
    assert env.run(5000) == [SLUG]                   # 時計は残っていたので即


def test_inactive_mission_is_ignored(tmp_path):
    env = Env(tmp_path)
    env.missions = []
    assert env.run(1000) == [] and env.run(9999) == [] and env.sent == []


def test_notify_failure_is_retried_next_cycle(tmp_path):
    env = Env(tmp_path)
    env.run(1000)
    env.notify_ok = False
    assert env.run(2000) == []
    env.notify_ok = True
    assert env.run(2005) == [SLUG]


def test_corrupt_ledger_is_rebuilt_not_trusted(tmp_path):
    env = Env(tmp_path)
    env.ledger_path().write_text("{not json")
    logs = []
    all_tasks = [(SLUG, c) for c in env.cards]
    S.run_cycle(env.reg, all_tasks, {SLUG: {"t001"}}, {SLUG: {"t001": "done", "t002": "blocked"}}, [SLUG], {SLUG},
                1000, 900, notify=env.notify, live_keys=env.live, log=logs.append)
    assert any("台帳が壊れている" in m for m in logs)
    assert env.sent == []                            # 時計は最初から = 通知が遅れる側
    assert not is_unreadable(S.read_ledger(env.reg))


def test_ledger_entry_with_bad_field_is_unreadable(tmp_path):
    env = Env(tmp_path)
    env.ledger_path().write_text(json.dumps({SLUG: {"fp": "x"}}))
    assert is_unreadable(S.read_ledger(env.reg))


def test_future_first_seen_is_reset(tmp_path):
    env = Env(tmp_path)
    env.run(1000)
    assert env.run(500) == []                        # 時計が戻った → 新しい時計
    assert json.loads(env.ledger_path().read_text())[SLUG]["first_seen"] == 500
