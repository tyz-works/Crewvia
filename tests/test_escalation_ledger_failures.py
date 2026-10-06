#!/usr/bin/env python3
"""台帳に書けないときの段階上げ (PR #283 Codex P2 / t029)。

欠陥: 送ってから台帳に書く順序だと、書き込みが失敗し続ける間、次のサイクルが古い段階を読んで同じ通知を毎回送る。
直し: 「送る」を先に台帳へ書き (sending)、書けなければ送らない。送った後の書き込みが失敗しても sending が残るので、
期限 (`SENDING_TIMEOUT_SECONDS`) までは同じ段階を送らない。不変条件 = **同じ試行・同じ段階の送信は、保存が壊れていても
どの列でも高々 1 回**。lib の `run_cycle()` を本物のまま、`write_ledger` の失敗を出来事として流す。

実行: python3 -m pytest tests/test_escalation_ledger_failures.py -q
"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import lib_escalation as E  # noqa: E402

EX = "ex-" + "a" * 32
META = {"id": "t017", "status": "needs_director", "needs_director_reason": "r"}


class Env:
    def __init__(self, tmp_path, monkeypatch, cfg=(300, 600)):
        self.reg = tmp_path / "registry"
        (self.reg / "daemons").mkdir(parents=True)
        self.cfg = cfg
        self.sent = []                  # [(段階, 時刻)]
        self.log = []                   # [(試行, 段階, 時刻, 成功したか)]
        self.attempt = EX
        self.resolved_at = []
        self.troubles = []
        self.now = 1000.0
        self.fail_write = lambda: False   # 呼び出しごとに「書き込みが失敗するか」
        self.director_ok = True
        self.telegram_ok = True
        real = E.write_ledger

        def flaky(registry_dir, ledger):
            return False if self.fail_write() else real(registry_dir, ledger)
        monkeypatch.setattr(E, "write_ledger", flaky)

    def cycle(self, dt=5.0, meta=META, execution=EX):
        self.now += dt
        self.attempt = execution
        if meta["status"] != "needs_director":
            self.resolved_at.append(self.now)
        m = dict(meta, current_execution_id=execution)
        return E.run_cycle(
            self.reg, [("m", m)], {"m": set()}, {"m": {"t017": m["status"]}}, {"m"}, {"m"}, self.now, self.cfg,
            director_live=lambda: True, telegram_available=lambda: True,
            send_director=lambda t: self._send(1, self.director_ok),
            send_telegram=lambda t: self._send(2, self.telegram_ok),
            execution_id_of=lambda x: x["current_execution_id"], log=lambda msg: None,
            trouble=self.troubles.append)

    def _send(self, stage, ok):
        self.sent.append((stage, self.now))
        self.log.append((self.attempt, stage, self.now, ok))
        return ok

    def stages(self):
        return [s for s, _ in self.sent]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def test_persistent_write_failure_never_floods(env):
    """Codex の再現: 台帳が読めるが書けない。3 サイクルどころか 200 サイクルでも 1 通も出ない (書けない = 見送り)。"""
    env.cycle()                                     # 台帳を作る (書ける)
    env.now += 700
    env.fail_write = lambda: True
    for _ in range(200):
        env.cycle()
    assert env.sent == []
    assert env.troubles, "書けないことは知らされる (見送りの理由)"


def test_write_fails_only_after_the_send_does_not_resend(env):
    env.cycle()
    env.now += 301
    calls = {"n": 0}

    def fail_after_first():
        calls["n"] += 1
        return calls["n"] >= 2                      # 送る前の書き込みは通り、結果の書き込みから壊れる
    env.fail_write = fail_after_first
    for _ in range(50):
        env.cycle()
    assert env.stages() == [1], "sending が残るので期限まで同じ段階を送り直さない"


def test_an_unrecorded_result_is_retried_once_after_the_timeout_when_storage_is_back(env):
    env.cfg = (300, 0)                              # 段階 1 だけ (期限後に段階 2 へ進む別の道を混ぜない)
    env.cycle()
    env.now += 301
    calls = {"n": 0}
    env.fail_write = lambda: (calls.__setitem__("n", calls["n"] + 1) or calls["n"] >= 2)
    env.cycle()
    assert env.stages() == [1]
    env.fail_write = lambda: False                  # 保存が戻った
    env.cycle(dt=E.SENDING_TIMEOUT_SECONDS - 100)
    assert env.stages() == [1], "期限前は送らない"
    env.cycle(dt=200)
    assert env.stages() == [1, 1], "期限後に失敗扱いで 1 度だけ送り直す"
    for _ in range(5):
        env.cycle()
    assert env.stages() == [1, 1]


def test_send_failure_is_recorded_and_retried_next_cycle(env):
    env.cycle()
    env.now += 301
    env.director_ok = False
    env.cycle()
    assert env.stages() == [1]
    entry = E.read_ledger(env.reg)["m/t017"]
    assert entry["stage_sent"] == 0 and entry["stage1_failed_at"] is not None and entry["sending_stage"] is None
    env.director_ok = True
    env.cycle()
    assert env.stages() == [1, 1] and E.read_ledger(env.reg)["m/t017"]["stage_sent"] == 1


def test_trouble_is_reported_without_depending_on_the_ledger(env):
    env.cycle()
    env.now += 301
    env.fail_write = lambda: True
    env.cycle()
    assert len(env.troubles) == 1 and "見送" in env.troubles[0]


@pytest.mark.parametrize("seed", range(300))
def test_random_events_never_send_a_stage_twice_per_attempt(tmp_path, monkeypatch, seed):
    """長さ 1〜40 の列 (保存の失敗 = 毎回 / 時々 / N サイクル続く・送信の失敗・試行の交代・解決・時間の経過を混ぜる)。"""
    rng = random.Random(seed)
    env = Env(tmp_path, monkeypatch, cfg=rng.choice([(300, 600), (0, 600), (300, 0), (10, 20)]))
    attempt = EX
    storm = 0
    for _ in range(rng.randint(1, 40)):
        roll = rng.random()
        if storm > 0:
            storm -= 1
            env.fail_write = lambda: True
        else:
            env.fail_write = (lambda: rng.random() < 0.3) if rng.random() < 0.4 else (lambda: False)
            if roll < 0.08:
                storm = rng.randint(2, 15)
        env.director_ok, env.telegram_ok = rng.random() < 0.8, rng.random() < 0.8
        if roll > 0.93:
            attempt = "ex-" + rng.choice("bcdef") * 32
        meta = dict(META, status=rng.choice(["needs_director"] * 9 + ["pending"]))
        env.cycle(dt=rng.choice([5, 5, 5, 60, 301, 600, 700, 2000]), meta=meta, execution=attempt)
    # 同じ (試行, 段階) の連続する 2 回の送信の間には、前の送信が失敗して記録された (= ok が False) か、
    # sending の期限が過ぎたか、解決を挟んで時計が作り直されたか (= その間に pending があった) のいずれかが要る。
    # 保存の失敗だけでは 2 回目は出ない (連投の不在)。
    last = {}
    for att, stage, at, ok in env.log:
        prev = last.get((att, stage))
        if prev is not None:
            p_at, p_ok = prev
            assert (not p_ok) or at - p_at >= E.SENDING_TIMEOUT_SECONDS or at - p_at >= 5 and _reset_between(env, att, p_at, at), \
                f"{att[:6]} 段階 {stage}: {p_at} → {at} で成功した通知が理由なく繰り返された"
        last[(att, stage)] = (at, ok)


def _reset_between(env, att, a, b):
    """その間に判断待ちでなくなった (台帳が消えた) サイクルがあったか。"""
    return any(a < t < b for t in env.resolved_at)
