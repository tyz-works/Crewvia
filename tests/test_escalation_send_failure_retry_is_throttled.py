#!/usr/bin/env python3
"""段階上げの送信が失敗し続けても、再試行は間引き間隔に 1 回以下 (PR #283 の P3)。

事故の型: 段階 1 (Director への mux send) が失敗すると台帳は stage_sent=0 のまま。R7 は次のサイクル (約 5 秒) にまた当たり、
送れない間じゅう 5 秒ごとに再送を試みていた。設計 §5-4 は間隔を should_notify に任せる想定だったが、実装には無かった。
段階 2 (Telegram) は `telegram_available` のバックオフが間隔を持つので、ここで一緒に固定する。

本物の dispatcher 1 サイクル (tests/test_escalation_dispatcher_cycle.py の EscHarness)。mux は FakeMux (本物に届かない)。
「N 分たった」は台帳の時刻を書き換えて表す。

実行: python3 -m pytest tests/test_escalation_send_failure_retry_is_throttled.py -q
"""

import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent / "scripts"))

from test_dispatcher_notify_once import FakeMux, SLUG  # noqa: E402
from test_escalation_dispatcher_cycle import (  # noqa: E402,F401
    EX, EscHarness, api, escalations, h, sends, stuck_with_follower,
)
import lib_escalation  # noqa: E402

N_CYCLES = 30


@pytest.fixture
def attempts(monkeypatch):
    """Director への段階 1 の送信の**試行**数 (成功・失敗を問わない)。FakeMux は成功しか記録しない。"""
    calls = []
    real = FakeMux.send

    def counting(self, name, text):
        if text.startswith("[escalation]"):
            calls.append(text)
        return real(self, name, text)

    monkeypatch.setattr(FakeMux, "send", counting)
    return calls


def test_stage1_send_failure_is_attempted_at_most_once_per_interval(h, api, attempts):
    stuck_with_follower(h)
    h.cycle()
    h.age(301)
    FakeMux.send_ok = False
    for _ in range(N_CYCLES):
        h.cycle()
    assert len(attempts) == 1, f"{N_CYCLES} サイクルで {len(attempts)} 回試みた (間引きが効いていない)"
    assert not sends(api)

    h.age_failure(lib_escalation.STAGE1_RETRY_SECONDS)             # 間隔が過ぎたら 1 回だけ試す
    for _ in range(N_CYCLES):
        h.cycle()
    assert len(attempts) == 2

    FakeMux.send_ok = True                                          # 直ったら次の間隔で届く
    h.age_failure(lib_escalation.STAGE1_RETRY_SECONDS)
    assert len(escalations(h.cycle())) == 1
    assert h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 1


def test_stage1_throttle_does_not_hold_back_stage2(h, api, attempts):
    stuck_with_follower(h)
    h.cycle()
    h.age(301)
    FakeMux.send_ok = False
    h.cycle()
    assert len(attempts) == 1 and not sends(api)
    h.age(300)                                                      # 段階 1 の間引き中でも 10 分で Telegram は出る
    h.cycle()
    assert len(sends(api)) == 1 and h.ledger()[f"{SLUG}/t017"]["stage_sent"] == 2


def test_stage2_send_failure_is_attempted_at_most_once_per_backoff(h, api):
    stuck_with_follower(h)
    h.cycle()
    h.age(301)
    h.cycle()
    api.method_modes["sendMessage"] = "http500"
    h.age(300)
    for _ in range(N_CYCLES):
        h.cycle()
    assert len(sends(api)) == 1, "Telegram の送信失敗はバックオフの間 5 秒ごとに叩かない"
