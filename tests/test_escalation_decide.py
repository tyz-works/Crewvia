#!/usr/bin/env python3
"""段階上げの判定 `lib_escalation.decide()` — 手書きの oracle 表 + 全直積の不変条件 + ランダムな列 (設計 §5-4)。

* 表は規則表の**行そのもの**を手で書いたもの。期待値を実装の式から導かない (regression-test-must-prove-red)。
* 全直積は不変条件だけを見る (表の再実装をしない)。前提 (R1〜R4b を通過した行) は設計 §5-4 のとおり。
* ランダムな列 (長さ 1〜12) は Director の在・不在と送信失敗 (段階 1・2) を混ぜて畳み込む。

実行: python3 -m pytest tests/test_escalation_decide.py -q
"""

import itertools
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import lib_escalation as E  # noqa: E402

NOW = 10_000.0
EX = "ex-" + "a" * 32
OTHER_EX = "ex-" + "b" * 32


def card(status="needs_director", ex=EX, observable=True):
    return E.CardView("m", "t017", status, ex, observable)


def entry(first_seen=NOW - 100, stage=0, failed=None, ex=EX, sending=None, sending_at=None):
    return {"execution_id": ex, "first_seen": first_seen, "stage_sent": stage,
            "stage_sent_at": None if stage == 0 else first_seen + 1, "stage1_failed_at": failed,
            "sending_stage": sending, "sending_at": sending_at}


def kind(decision):
    return decision.ledger_update[0]


# (名前, card, entry, cfg, director_live, telegram_available, 期待 action, 期待 ledger 種別, 期待 stage_sent | None)
D, T = 300, 600
ROWS = [
    ("R1 判断待ちでない・台帳あり → 消す", card("pending"), entry(), (D, T), True, True, "none", "delete", None),
    ("R1 判断待ちでない・台帳なし → 何もしない", card("done"), None, (D, T), True, True, "none", "keep", None),
    ("R2 観測できない → 鳴らさず保つ", card(observable=False), entry(NOW - 9999), (D, T), True, True, "none", "keep", None),
    ("R3 台帳なし → 時計を始める", card(), None, (D, T), True, True, "none", "set", 0),
    ("R4 別の試行 → 時計をやり直す", card(ex=OTHER_EX), entry(NOW - 9999, 1), (D, T), True, True, "none", "set", 0),
    ("R4b 時計が戻った → first_seen だけ直す", card(), entry(NOW + 50, 1), (D, T), True, True, "none", "set", 1),
    ("R5 段階 2 済み → 3 回目は無い", card(), entry(NOW - 9999, 2), (D, T), True, True, "none", "keep", None),
    ("R6 段階 1 済み・10 分・Telegram 可 → 段階 2", card(), entry(NOW - 600, 1), (D, T), True, True, "telegram_notice", "set", 2),
    ("R6 段階 1 前でも Director 不在なら段階 2 (P1-1)", card(), entry(NOW - 600, 0), (D, T), False, True, "telegram_notice", "set", 2),
    ("R6 段階 1 が無効 (d<=0) なら待たない", card(), entry(NOW - 600, 0), (0, T), True, True, "telegram_notice", "set", 2),
    ("R6 段階 1 の送信が失敗済みなら待たない", card(), entry(NOW - 600, 0, failed=NOW - 300), (D, T), True, True,
     "telegram_notice", "set", 2),
    ("R6 段階 1 を経ていない在席の Director を飛ばさない", card(), entry(NOW - 600, 0), (D, T), True, True,
     "director_renotice", "set", 1),
    ("R6 Telegram 不可 → R7 に落ちて段階 1 (P2-2)", card(), entry(NOW - 700, 0), (D, T), True, False,
     "director_renotice", "set", 1),
    ("R6 Telegram 不可・段階 1 済み → 見送り", card(), entry(NOW - 700, 1), (D, T), True, False, "none", "keep", None),
    ("R7 5 分ちょうど・在席 → 段階 1", card(), entry(NOW - 300, 0), (D, T), True, True, "director_renotice", "set", 1),
    ("R7 5 分に 1 秒足りない → 何もしない", card(), entry(NOW - 299, 0), (D, T), True, True, "none", "keep", None),
    ("R7 Director 不在・10 分未満 → 見送り (記録しない)", card(), entry(NOW - 400, 0), (D, T), False, True, "none", "keep", None),
    ("R7 段階 1 は 1 回だけ", card(), entry(NOW - 400, 1), (D, T), True, True, "none", "keep", None),
    ("R7 needs_human_review も対象", card("needs_human_review"), entry(NOW - 300, 0), (D, T), True, True,
     "director_renotice", "set", 1),
    ("R5b 段階 1 を送る途中 (期限内) → 送らない", card(), entry(NOW - 400, 0, sending=1, sending_at=NOW - 5), (D, T), True, True,
     "none", "keep", None),
    ("R5b 段階 2 を送る途中 (期限内) → 送らない", card(), entry(NOW - 700, 1, sending=2, sending_at=NOW - 5), (D, T), True, True,
     "none", "keep", None),
    ("R5b 段階 1 の sending が期限切れ → 失敗扱いで 1 度だけ送り直せる", card(), entry(NOW - 900, 0, sending=1, sending_at=NOW - 601),
     (D, T), True, False, "director_renotice", "set", 1),
    ("R5b 段階 1 の sending が期限切れ・10 分超 → 失敗扱いで段階 2 に進める", card(), entry(NOW - 900, 0, sending=1, sending_at=NOW - 601),
     (D, T), True, True, "telegram_notice", "set", 2),
    ("R5b sending の時刻が未来 (壊れた記録) は期限切れ扱い", card(), entry(NOW - 400, 0, sending=1, sending_at=NOW + 99), (D, T), True, True,
     "director_renotice", "set", 1),
    ("cfg 両方無効 → 何も鳴らさない", card(), entry(NOW - 99999, 0), (0, 0), True, True, "none", "keep", None),
    ("cfg 段階 1 だけ", card(), entry(NOW - 99999, 1), (D, 0), True, True, "none", "keep", None),
    ("cfg 段階 2 だけ・10 分", card(), entry(NOW - 600, 0), (0, T), True, True, "telegram_notice", "set", 2),
    ("10 分ちょうどで段階 2 (境界)", card(), entry(NOW - 600, 1), (D, T), True, True, "telegram_notice", "set", 2),
    ("10 分に 1 秒足りない → 段階 2 は出ない", card(), entry(NOW - 599, 1), (D, T), True, True, "none", "keep", None),
]


@pytest.mark.parametrize("name,c,e,cfg,dl,ta,action,ledger,stage", ROWS, ids=[r[0] for r in ROWS])
def test_oracle_table(name, c, e, cfg, dl, ta, action, ledger, stage):
    got = E.decide(c, e, NOW, cfg, dl, ta)
    assert got.action == action
    assert kind(got) == ledger
    if stage is not None:
        assert got.ledger_update[1]["stage_sent"] == stage


def test_new_event_resets_first_seen_and_clears_failure_mark():
    got = E.decide(card(ex=OTHER_EX), entry(NOW - 9999, 2, failed=NOW - 1), NOW, (D, T), True, True)
    assert got.ledger_update[1] == {"execution_id": OTHER_EX, "first_seen": NOW, "stage_sent": 0,
                                    "stage_sent_at": None, "stage1_failed_at": None,
                                    "sending_stage": None, "sending_at": None}


def test_apply_failure_marks_only_stage_one_and_keeps_the_first_mark():
    e = entry(NOW - 300, 0)
    assert E.apply_failure(e, "telegram_notice", NOW) == e
    marked = E.apply_failure(e, "director_renotice", NOW)
    assert marked["stage1_failed_at"] == NOW and marked["stage_sent"] == 0
    assert E.apply_failure(marked, "director_renotice", NOW + 50)["stage1_failed_at"] == NOW


# ---------------------------------------------------------------------------
# 全直積の不変条件
# ---------------------------------------------------------------------------

CFGS = [(300, 600), (0, 600), (300, 0), (0, 0), (-5, 600), (1, 2), (600, 601)]
STATUSES = ["needs_director", "needs_human_review", "pending", "in_progress", "done"]


def population():
    for status, observable, led, stage, failed, cfg, dl, ta in itertools.product(
            STATUSES, (True, False), ("none", "same", "other", "future"), (0, 1, 2), (None, 1),
            CFGS, (True, False), (True, False)):
        d, t = cfg
        edges = {0, 1, d - 1, d, d + 1, t - 1, t, t + 1, 99999}
        for elapsed in sorted(edges):
            if led == "none":
                e = None
            else:
                ex = OTHER_EX if led == "other" else EX
                first = NOW + 77 if led == "future" else NOW - elapsed
                e = entry(first, stage, failed=None if failed is None else NOW - 1, ex=ex)
            yield card(status, observable=observable), e, cfg, dl, ta, elapsed


def test_invariants_over_the_full_product():
    n = 0
    for c, e, cfg, dl, ta, elapsed in population():
        n += 1
        d, t = cfg
        got = E.decide(c, e, NOW, cfg, dl, ta)
        awaiting = c.status in ("needs_director", "needs_human_review")
        if not awaiting:
            assert got.action == "none"
            assert kind(got) == ("delete" if e is not None else "keep")
            continue
        if got.action == "telegram_notice":
            assert t > 0 and ta and e is not None and NOW - e["first_seen"] >= t
        if got.action == "director_renotice":
            assert d > 0 and dl and e["stage_sent"] == 0
        if kind(got) == "set" and e is not None and e["execution_id"] == c.execution_id:
            assert got.ledger_update[1]["stage_sent"] >= e["stage_sent"]      # 単調増加
        if got.action != "none":
            assert kind(got) == "set"
        passed = (c.observable and e is not None and e["execution_id"] == c.execution_id and e["first_seen"] <= NOW)
        if not passed:
            continue
        elapsed_now = NOW - e["first_seen"]
        if dl is False and t > 0 and elapsed_now >= t and ta and e["stage_sent"] < 2:
            assert got.action == "telegram_notice"                              # P1-1 の回帰
        if (not ta) and d > 0 and dl and e["stage_sent"] == 0 and elapsed_now >= d:
            assert got.action == "director_renotice"                            # P2-2 の回帰
    assert n > 5000


# ---------------------------------------------------------------------------
# ランダムな列 (長さ 1〜12)
# ---------------------------------------------------------------------------

def simulate(seed):
    rng = random.Random(seed)
    cfg = rng.choice(CFGS)
    ledger = None
    now = 1000.0
    sent = {"director_renotice": 0, "telegram_notice": 0}
    events = 0
    for _ in range(rng.randint(1, 12)):
        now += rng.choice([0, 1, 60, 299, 300, 301, 600, 5000])
        status = rng.choice(["needs_director"] * 5 + ["pending"])
        ex = rng.choice([EX] * 6 + [OTHER_EX])
        dl, ta = rng.random() < 0.6, rng.random() < 0.6
        got = E.decide(card(status, ex=ex, observable=rng.random() < 0.9), ledger, now, cfg, dl, ta)
        if got.action == "none":
            ok = True
        else:
            ok = rng.random() < 0.7
            assert (got.action != "director_renotice") or dl
            assert (got.action != "telegram_notice") or ta
        if got.action != "none":
            if ok:
                sent[got.action] += 1
        if got.action != "none" and not ok:
            ledger = E.apply_failure(ledger, got.action, now)
            continue
        if kind(got) == "set":
            if ledger is not None and ledger["execution_id"] == got.ledger_update[1]["execution_id"]:
                assert got.ledger_update[1]["stage_sent"] >= ledger["stage_sent"]
            if ledger is None or ledger["execution_id"] != got.ledger_update[1]["execution_id"]:
                sent = {"director_renotice": 0, "telegram_notice": 0}             # 新しい事象
            ledger = got.ledger_update[1]
        elif kind(got) == "delete":
            ledger = None
            sent = {"director_renotice": 0, "telegram_notice": 0}
        events += 1
        assert sent["director_renotice"] <= 1 and sent["telegram_notice"] <= 1, "同じ試行で同じ段階は 2 回出ない"
    return events


@pytest.mark.parametrize("seed", range(400))
def test_random_sequences_never_repeat_a_stage(seed):
    simulate(seed)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("d,t,want", [
    (None, None, (300.0, 600.0)),
    (300, 600, (300.0, 600.0)),
    (100, 200, (100.0, 200.0)),
    (600, 300, (300.0, 600.0)),          # 逆順 → 既定値
    (300, 300, (300.0, 600.0)),          # 同値も拒否
    (0, 600, (0.0, 600.0)),              # 段階 1 無効なら順序の検証は無い
    (-1, 5, (-1.0, 5.0)),
    (300, 0, (300.0, 0.0)),
    ("abc", 600, (300.0, 600.0)),
    (float("nan"), 600, (300.0, 600.0)),
    (float("inf"), 600, (300.0, 600.0)),
    (True, 600, (300.0, 600.0)),
])
def test_cfg_validation(d, t, want):
    warnings = []
    assert E.parse_cfg(d, t, warnings.append) == want
    if want == (300.0, 600.0) and (d, t) not in ((None, None), (300, 600)):
        assert warnings


def test_cfg_env_overrides_config_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("escalation:\n  director_after_seconds: 111\n  telegram_after_seconds: 222\n")
    assert E.load_cfg(p, {}) == (111.0, 222.0)
    assert E.load_cfg(p, {E.ENV_DIRECTOR_AFTER: "50", E.ENV_TELEGRAM_AFTER: "70"}) == (50.0, 70.0)
    assert E.load_cfg(tmp_path / "missing.yaml", {}) == (300.0, 600.0)


# ---------------------------------------------------------------------------
# 台帳の形・文面
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"nokey": entry()}, {"m/t1": []}, {"m/t1": dict(entry(), stage_sent=3)},
    {"m/t1": dict(entry(), first_seen="x")}, {"m/t1": dict(entry(), execution_id="")},
    {"m/t1": dict(entry(), stage1_failed_at=float("nan"))},
])
def test_ledger_shape_rejects_bad_entries(bad):
    assert E.escalation_state_problem(bad)


def test_ledger_shape_accepts_what_decide_writes():
    assert E.escalation_state_problem({"m/t1": entry(), "m/t2": entry(stage=2, failed=5)}) is None


def test_notice_text_has_the_design_items_and_omits_empty_lines():
    lines = E.notice_lines("HEAD", "m", "t017", "needs_director", 600, ["t006", "t007"], "理由\n二行目", "https://s")
    assert lines[:3] == ["HEAD", "mission: m", "task: t017 (needs_director) — 止まって 10 分"]
    assert "後続 2 件が待機中: t006, t007" in lines and "理由: 理由" in lines and lines[-1] == "セッション: https://s"
    bare = E.notice_lines("HEAD", "m", "t1", "needs_director", 60, None, "")
    assert not any(l.startswith(("後続", "セッション")) for l in bare) and "理由: (理由未記載)" in bare
    assert len(E.telegram_text(["x" * 5000])) <= E.TELEGRAM_TEXT_MAX
