#!/usr/bin/env python3
"""Telegram の経路 (PR-A) の**純粋関数**の網羅テスト。ネットワークにもファイルにも触れない。

設計 `knowledge/director-escalation-telegram.md` §2 (質問台帳・受け付ける条件・状態遷移) と §1-1 (受信側の状態)。
状態の扱い (札の発行・消費・期限・二重押し) は純粋関数 + 網羅で固定する (前のミッションの教訓)。

期待値の oracle はテスト内に**手で書いた表**で、実装の関数を呼び直さない (regression-test-must-prove-red)。

実行: python3 -m pytest tests/test_telegram_pure.py -q
"""

import itertools
import json
import os
import random
import stat
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent / "scripts"))

import lib_daemon_state as lds  # noqa: E402
import lib_telegram as t  # noqa: E402
from lib_task_cards import Unreadable  # noqa: E402

CHAT = "5550001"
NOW = 1_800_000_000.0
TOKEN = "123456:FAKE-TOKEN-abcdefghij"


def entry(**over):
    e = {"nonce": "9f3a1c", "question": "どうする?", "options": ["A: 続ける", "B: 止める"],
         "message_id": 4711, "status": "open", "created_at": NOW - 60, "expires_at": NOW + 3600,
         "forwarded": False}
    e.update(over)
    return e


def press(qid="q-1a2b3c4d", nonce="9f3a1c", index=0, *, chat=CHAT, sender=None, message_id=4711, update_id=9001):
    return {"update_id": update_id, "callback_query": {
        "id": "cb", "from": {"id": int(chat if sender is None else sender)},
        "message": {"message_id": message_id, "chat": {"id": int(chat)}},
        "data": f"{qid}.{nonce}.{index}"}}


# ---------------------------------------------------------------------------
# callback_data
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("data,expected", [
    ("q-1a2b3c4d.9f3a1c.0", ("q-1a2b3c4d", "9f3a1c", 0)),
    ("q-1a2b3c4d.9f3a1c.12", ("q-1a2b3c4d", "9f3a1c", 12)),
    ("q-1a2b3c4d.9f3a1c.1 ", None),            # 余りは拒否 (全体一致)
    (" q-1a2b3c4d.9f3a1c.1", None),
    ("q-1a2b3c4d.9f3a1c.1\n", None),
    ("q-1a2b3c4d.9f3a1c", None),
    ("q-1a2b3c4d.9f3a1c.-1", None),
    ("q-1a2b3c4D.9f3a1c.1", None),             # 大文字の hex は発行しない
    ("q-1a2b3c4.9f3a1c.1", None),
    ("q-1a2b3c4d.9f3a1.1", None),
    ("q-1a2b3c4d.9f3a1c.1.2", None),
    ("x-1a2b3c4d.9f3a1c.1", None),
    ("", None), (None, None), (123, None),
])
def test_callback_data_must_match_the_whole_string(data, expected):
    assert t.parse_callback_data(data) == expected


def test_callback_data_stays_under_telegrams_64_byte_limit():
    assert len(t.make_callback_data("q-1a2b3c4d", "9f3a1c", 3).encode()) <= 64


# ---------------------------------------------------------------------------
# 「未回答の質問がある」の定義と sweep (§2-3b)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,e,expected", [
    ("open・期限内・message_id あり", entry(), True),
    ("期限ちょうど", entry(expires_at=NOW), False),
    ("期限切れ", entry(expires_at=NOW - 1), False),
    ("message_id null・299 秒", entry(message_id=None, created_at=NOW - 299), True),
    ("message_id null・300 秒 = 残骸", entry(message_id=None, created_at=NOW - 300), False),
    ("answered", entry(status="answered"), False),
    ("withdrawn", entry(status="withdrawn"), False),
    ("expired", entry(status="expired"), False),
])
def test_unanswered_definition(name, e, expected):
    assert t.is_unanswered(e, NOW) is expected, name


def test_sweep_table():
    ledger = {
        "q-00000001": entry(expires_at=NOW),                                         # 期限 → expired・ボタンを消す
        "q-00000002": entry(message_id=None, created_at=NOW - 300),                  # 残骸 → withdrawn・消すボタン無し
        "q-00000003": entry(),                                                       # そのまま
        "q-00000004": entry(status="expired", closed_at=NOW - 8 * 86400),            # 7 日超 → 削除
        "q-00000005": entry(status="withdrawn", closed_at=NOW - 6 * 86400),          # 7 日未満 → 残る
        "q-00000006": entry(status="answered", closed_at=NOW - 9 * 86400,
                            answer={"kind": "choice", "index": 0, "at": NOW - 9 * 86400, "update_id": 1},
                            forwarded=False),                                        # 転送待ちは消さない
        "q-00000007": entry(status="answered", closed_at=NOW - 9 * 86400,
                            answer={"kind": "choice", "index": 0, "at": NOW - 9 * 86400, "update_id": 1},
                            forwarded=True),                                         # 転送済み・7 日超 → 削除
        "q-00000008": entry(message_id=None, created_at=NOW - 60),                   # null でも 5 分未満は残す
    }
    before = json.dumps(ledger, sort_keys=True)
    out = t.sweep_questions(ledger, NOW)
    assert json.dumps(ledger, sort_keys=True) == before, "純粋関数: 入力を書き換えない"
    assert out["q-00000001"]["status"] == "expired" and out["q-00000001"]["unbutton"] is True
    assert out["q-00000002"]["status"] == "withdrawn" and out["q-00000002"]["unbutton"] is False
    assert out["q-00000003"]["status"] == "open"
    assert "q-00000004" not in out
    assert out["q-00000005"]["status"] == "withdrawn"
    assert out["q-00000006"]["status"] == "answered"
    assert "q-00000007" not in out
    assert out["q-00000008"]["status"] == "open"
    assert lds.telegram_questions_problem(out) is None


def test_sweep_is_idempotent():
    ledger = {"q-00000001": entry(expires_at=NOW - 5), "q-00000002": entry(message_id=None, created_at=NOW - 999)}
    once = t.sweep_questions(ledger, NOW)
    assert t.sweep_questions(once, NOW) == once


# ---------------------------------------------------------------------------
# classify_update — §2-2 の全条件 AND の全直積 (手書きの oracle)
# ---------------------------------------------------------------------------

def test_classify_update_full_cross_product_against_a_handwritten_oracle():
    """callback の 5 条件 + 状態を 1 つずつ欠かした直積。`answer` になるのは**全部満たしたときだけ**。"""
    ledgers = {
        "open": entry(),
        "open_expired": entry(expires_at=NOW - 1),
        "open_null_message": entry(message_id=None),
        "answered": entry(status="answered", answer={"kind": "choice", "index": 1, "at": NOW - 5, "update_id": 5}),
        "withdrawn": entry(status="withdrawn"),
        "expired": entry(status="expired"),
    }
    n = 0
    for state, chat_ok, sender_ok, nonce_ok, msg_ok, idx in itertools.product(
            ledgers, (True, False), (True, False), (True, False), (True, False), (0, 1, 2)):
        ledger = {"q-1a2b3c4d": ledgers[state]}
        upd = press(nonce="9f3a1c" if nonce_ok else "000000", index=idx,
                    chat=CHAT if chat_ok else "999", sender=CHAT if sender_ok else "888",
                    message_id=4711 if msg_ok else 4712)
        d = t.classify_update(upd, ledger, CHAT, NOW)
        n += 1
        # --- oracle (手書き) ---
        if not chat_ok or not sender_ok:
            want = "ignore"                                  # 他 chat / 他人の押下は転送も返信もしない
        elif not nonce_ok:
            want = "reject"                                  # 札違い
        elif state == "open_null_message":
            want = "hold"                                    # message_id が null の open → offset を進めない
        elif not msg_ok or idx >= 2:
            want = "reject"                                  # 別メッセージのボタンの流用 / index 範囲外
        elif state == "open":
            want = "answer"
        else:
            want = "reject"                                  # 期限切れ・回答済み・取り下げ
        assert d.kind == want, (state, chat_ok, sender_ok, nonce_ok, msg_ok, idx, d.kind)
        if d.kind == "answer":
            assert d.answer == {"kind": "choice", "index": idx, "at": NOW, "update_id": 9001}
    assert n == 6 * 2 * 2 * 2 * 2 * 3


def test_unknown_question_and_malformed_callback_data():
    assert t.classify_update(press(qid="q-ffffffff"), {}, CHAT, NOW).kind == "reject"
    upd = press()
    upd["callback_query"]["data"] = "q-1a2b3c4d.9f3a1c.0.extra"
    assert t.classify_update(upd, {"q-1a2b3c4d": entry()}, CHAT, NOW).kind == "ignore"
    upd["callback_query"]["data"] = None
    assert t.classify_update(upd, {"q-1a2b3c4d": entry()}, CHAT, NOW).kind == "ignore"


def test_reject_replies_name_the_reason():
    ledger = {"q-1a2b3c4d": entry(status="answered", answer={"kind": "choice", "index": 1, "at": NOW, "update_id": 5})}
    d = t.classify_update(press(index=0), ledger, CHAT, NOW)
    assert d.kind == "reject" and "B: 止める" in d.reply        # 二重押し: 先に確定した選択肢を示す
    d = t.classify_update(press(), {"q-1a2b3c4d": entry(status="withdrawn")}, CHAT, NOW)
    assert "画面" in d.reply
    d = t.classify_update(press(), {"q-1a2b3c4d": entry(expires_at=NOW - 1)}, CHAT, NOW)
    assert "期限" in d.reply


@pytest.mark.parametrize("name,mutate,want", [
    ("返信 (正)", lambda m: None, "answer"),
    ("返信でない雑多なメッセージ", lambda m: m.pop("reply_to_message"), "ignore"),
    ("別メッセージへの返信", lambda m: m["reply_to_message"].update(message_id=1), "ignore"),
    ("他 chat", lambda m: m["chat"].update(id=1), "ignore"),
    ("他人の発言", lambda m: m["from"].update(id=1), "ignore"),
    ("テキストでない (画像など)", lambda m: m.pop("text"), "ignore"),
    ("空白だけ", lambda m: m.update(text="  \n "), "ignore"),
])
def test_text_reply_rules(name, mutate, want):
    msg = {"message_id": 7, "chat": {"id": int(CHAT)}, "from": {"id": int(CHAT)}, "text": "やっぱり先に #281 を見て",
           "reply_to_message": {"message_id": 4711}}
    mutate(msg)
    d = t.classify_update({"update_id": 9100, "message": msg}, {"q-1a2b3c4d": entry()}, CHAT, NOW)
    assert d.kind == want, name


def test_text_reply_to_a_closed_question_is_not_forwarded():
    for status in ("answered", "withdrawn", "expired"):
        msg = {"message_id": 7, "chat": {"id": int(CHAT)}, "from": {"id": int(CHAT)}, "text": "x",
               "reply_to_message": {"message_id": 4711}}
        ledger = {"q-1a2b3c4d": entry(status=status, answer={"kind": "choice", "index": 0, "at": NOW, "update_id": 1})}
        assert t.classify_update({"update_id": 1, "message": msg}, ledger, CHAT, NOW).kind == "ignore"


# ---------------------------------------------------------------------------
# update の種類 × 照合の結果 — 全セルの表 (設計 §2-3c。Codex P1 の族: 「照合できない」の理由を取り違えると答えが失われる)
# ---------------------------------------------------------------------------

#: 列: 照合の結果。それぞれの台帳を作る
OUTCOMES = ("match", "mismatch", "pending", "expired", "withdrawn", "answered")


def ledger_for(outcome, *, with_pending_elsewhere=False):
    """update の相手になる質問の台帳。message_id は常に 4711。`pending` は message_id が null の open が別にある形。"""
    answered = {"kind": "choice", "index": 0, "at": NOW - 5, "update_id": 1}
    e = {
        "match": entry(),
        "mismatch": None,                                   # 照合する質問が無い
        "pending": entry(message_id=None, created_at=NOW - 10),
        "expired": entry(status="expired", closed_at=NOW - 5),
        "withdrawn": entry(status="withdrawn", closed_at=NOW - 5),
        "answered": entry(status="answered", answer=answered, closed_at=NOW - 5),
    }[outcome]
    ledger = {} if e is None else {"q-1a2b3c4d": e}
    if with_pending_elsewhere:
        ledger["q-1a2b3c4e"] = entry(message_id=None, created_at=NOW - 10, nonce="bbbbbb")
    return ledger


def callback_for(outcome):
    nonce = "9f3a1c"
    qid = "q-ffffffff" if outcome == "mismatch" else "q-1a2b3c4d"
    return press(qid=qid, nonce=nonce, message_id=4711)


def reply_for(outcome, text="先に見て"):
    # 返信先: match / expired / withdrawn / answered は記録済みの message_id 4711。pending は「まだ記録されていない」ので別の番号 77
    reply_to = 77 if outcome in ("pending", "mismatch") else 4711
    return {"update_id": 9100, "message": {"message_id": 7, "chat": {"id": int(CHAT)}, "from": {"id": int(CHAT)},
                                           "text": text, "reply_to_message": {"message_id": reply_to}}}


#: 手書きの oracle: (種類, 結果) → (kind, offset を進めるか)。**Director に届くのは answer のときだけ** (forward される)。
#: Telegram への返信 (answerCallbackQuery) は callback の answer / reject のときだけ。
TABLE = {
    ("callback_query", "match"): ("answer", True),
    ("callback_query", "mismatch"): ("reject", True),
    ("callback_query", "pending"): ("hold", False),
    ("callback_query", "expired"): ("reject", True),
    ("callback_query", "withdrawn"): ("reject", True),
    ("callback_query", "answered"): ("reject", True),
    ("reply", "match"): ("answer", True),
    ("reply", "mismatch"): ("ignore", True),
    ("reply", "pending"): ("hold", False),                  # Codex P1: message_id の記録前に届いた返信を失わない
    ("reply", "expired"): ("ignore", True),                 # 閉じた質問への返信は無関係 (誤転送しない)。保留しない
    ("reply", "withdrawn"): ("ignore", True),
    ("reply", "answered"): ("ignore", True),
}


@pytest.mark.parametrize("kind,outcome", sorted(TABLE))
def test_update_kind_by_match_outcome_table(kind, outcome):
    ledger = ledger_for(outcome)
    upd = callback_for(outcome) if kind == "callback_query" else reply_for(outcome)
    d = t.classify_update(upd, ledger, CHAT, NOW)
    want_kind, want_advance = TABLE[(kind, outcome)]
    assert d.kind == want_kind, (kind, outcome, d.kind)
    assert (d.kind != "hold") is want_advance
    if kind == "callback_query":
        assert (d.callback_id is not None) is (want_kind in ("answer", "reject")), "待ち表示を止める返信は answer / reject だけ"
    else:
        assert d.callback_id is None


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_updates_that_can_never_be_an_answer_are_ignored_whatever_the_ledger_holds(outcome):
    """普通のメッセージ (返信でない)・編集・その他の update・他 chat・他人・テキストでない返信は、台帳がどうでも ignore。
    特に「保留の質問がある」ことが、これらを保留に変えてはいけない (offset が止まり続ける)。"""
    for pending_elsewhere in (False, True):
        ledger = ledger_for(outcome, with_pending_elsewhere=pending_elsewhere)
        updates = {
            "plain_message": {"update_id": 1, "message": {"message_id": 2, "chat": {"id": int(CHAT)}, "from": {"id": int(CHAT)}, "text": "雑談"}},
            "bot_command": {"update_id": 2, "message": {"message_id": 3, "chat": {"id": int(CHAT)}, "from": {"id": int(CHAT)}, "text": "/start"}},
            "edited_message": {"update_id": 3, "edited_message": {"message_id": 7, "chat": {"id": int(CHAT)}, "text": "x"}},
            "member_update": {"update_id": 4, "my_chat_member": {"chat": {"id": int(CHAT)}}},
            "empty": {"update_id": 5},
            "reply_other_chat": dict(reply_for(outcome), message=dict(reply_for(outcome)["message"], chat={"id": 999})),
            "reply_other_sender": dict(reply_for(outcome), message=dict(reply_for(outcome)["message"], **{"from": {"id": 888}})),
            "reply_without_text": {"update_id": 6, "message": {"message_id": 7, "chat": {"id": int(CHAT)}, "from": {"id": int(CHAT)},
                                                                 "photo": [{"file_id": "x"}], "reply_to_message": {"message_id": 77}}},
            "callback_other_chat": press(chat="999", sender="999"),
        }
        for name, upd in updates.items():
            assert t.classify_update(upd, ledger, CHAT, NOW).kind == "ignore", (outcome, pending_elsewhere, name)


def test_a_reply_to_a_closed_question_is_not_held_by_a_pending_one():
    """閉じた質問 (期限切れ・取り下げ・回答済み) のメッセージへの返信は、別に保留の質問があっても保留しない (offset が 5 分止まる)。"""
    for outcome in ("expired", "withdrawn", "answered"):
        ledger = ledger_for(outcome, with_pending_elsewhere=True)
        assert t.classify_update(reply_for(outcome), ledger, CHAT, NOW).kind == "ignore", outcome


def test_a_blank_reply_is_never_held():
    ledger = ledger_for("pending")
    assert t.classify_update(reply_for("pending", text="  \n "), ledger, CHAT, NOW).kind == "ignore"


def test_a_reply_is_held_only_while_the_pending_window_is_open():
    base = {"q-1a2b3c4e": entry(message_id=None, created_at=NOW - 10)}
    assert t.classify_update(reply_for("pending"), base, CHAT, NOW).kind == "hold"
    old = {"q-1a2b3c4e": entry(message_id=None, created_at=NOW - t.NULL_MESSAGE_GRACE_SECONDS)}
    assert t.classify_update(reply_for("pending"), old, CHAT, NOW).kind == "ignore", "猶予 (5 分) を過ぎた残骸は保留しない"
    expired = {"q-1a2b3c4e": entry(message_id=None, created_at=NOW - 10, expires_at=NOW - 1)}
    assert t.classify_update(reply_for("pending"), expired, CHAT, NOW).kind == "ignore"
    answered = {"q-1a2b3c4e": entry(status="answered", message_id=None, answer={"kind": "choice", "index": 0, "at": NOW, "update_id": 1})}
    assert t.classify_update(reply_for("pending"), answered, CHAT, NOW).kind == "ignore"


def test_other_update_kinds_are_ignored():
    for upd in ({"update_id": 1, "edited_message": {"text": "x"}}, {"update_id": 2}, {"update_id": 3, "message": {"text": "/start"}}):
        assert t.classify_update(upd, {"q-1a2b3c4d": entry()}, CHAT, NOW).kind == "ignore"


def test_answer_text_is_one_line_and_bounded():
    text = t.sanitize_answer_text("1 行目\r\n2 行目\x00\x07\x1b[31m" + "あ" * 600)
    assert "\n" not in text and "\x00" not in text and "\x1b" not in text
    assert text.endswith("…(truncated)") and len(text) <= 500 + len("…(truncated)")


# ---------------------------------------------------------------------------
# apply_answer — CAS: 先に確定した 1 つだけが有効
# ---------------------------------------------------------------------------

def test_first_answer_wins_and_is_never_overwritten():
    ledger = {"q-1a2b3c4d": entry()}
    first = {"kind": "choice", "index": 0, "at": NOW, "update_id": 1}
    second = {"kind": "choice", "index": 1, "at": NOW + 1, "update_id": 2}
    once = t.apply_answer(ledger, "q-1a2b3c4d", first, NOW)
    twice = t.apply_answer(once, "q-1a2b3c4d", second, NOW + 1)
    assert twice == once and twice["q-1a2b3c4d"]["answer"]["index"] == 0
    assert twice["q-1a2b3c4d"]["status"] == "answered" and twice["q-1a2b3c4d"]["forwarded"] is False
    assert ledger["q-1a2b3c4d"]["status"] == "open", "純粋関数: 入力を書き換えない"
    assert lds.telegram_questions_problem(twice) is None


@pytest.mark.parametrize("seed", range(40))
def test_random_press_sequences_yield_at_most_one_answer_and_never_change_it(seed):
    """ランダムな押下の列 (長さ 1〜12): 質問ごとに answer は高々 1 つで、確定後は不変。"""
    rng = random.Random(seed)
    ledger = {"q-1a2b3c4d": entry(), "q-1a2b3c4e": entry(message_id=4712, nonce="aaaaaa")}
    first_answer = {}
    for step in range(rng.randint(1, 12)):
        qid = rng.choice(["q-1a2b3c4d", "q-1a2b3c4e", "q-ffffffff"])
        e = ledger.get(qid) or entry()
        upd = press(qid=qid, nonce=rng.choice([e["nonce"], "000000"]), index=rng.choice([0, 1, 2]),
                    message_id=rng.choice([4711, 4712]), update_id=9000 + step)
        d = t.classify_update(upd, ledger, CHAT, NOW)
        if d.kind == "answer":
            ledger = t.apply_answer(ledger, d.qid, d.answer, NOW)
        for q, ent in ledger.items():
            if ent["status"] == "answered":
                first_answer.setdefault(q, ent["answer"])
                assert ent["answer"] == first_answer[q]
    assert lds.telegram_questions_problem(ledger) is None


# ---------------------------------------------------------------------------
# 受信側の状態 (§1-1) — 心拍の間隔と stale のしきい値
# ---------------------------------------------------------------------------

def receiver(**over):
    r = {"enabled": True, "checked_at": NOW, "reason": "ok", "bot_id": 123456, "chat_hash": "a" * 12}
    r.update(over)
    return r


def test_stale_threshold_is_derived_from_the_heartbeat_not_from_poll_interval():
    assert t.RECEIVER_HEARTBEAT_SECONDS == 30
    assert t.RECEIVER_STALE_SECONDS == 3 * t.RECEIVER_HEARTBEAT_SECONDS == 90
    # 心拍の間隔 + サイクル 1 回 (5 秒 + 処理時間 13 秒) は stale にならない (t014 P2-1)
    assert t.receiver_verdict(receiver(checked_at=NOW - (30 + 5 + 8)), NOW)[0] is True
    assert t.receiver_verdict(receiver(checked_at=NOW - 90), NOW) == (True, "ok")          # しきい値ちょうど
    assert t.receiver_verdict(receiver(checked_at=NOW - 91), NOW) == (False, "receiver_stale")


def test_receiver_heartbeat_does_not_depend_on_poll_interval():
    for interval in (1, 5, 10, 60, 3600):
        cfg = {"poll_interval_seconds": interval}
        assert t.receiver_verdict(receiver(checked_at=NOW - 60), NOW)[0] is True, cfg


def test_receiver_verdict_table():
    me = (123456, "a" * 12)
    assert t.receiver_verdict(receiver(), NOW, me) == (True, "ok")
    assert t.receiver_verdict(receiver(enabled=False, bot_id=None), NOW, me)[1] == "receiver_disabled"
    assert t.receiver_verdict(receiver(bot_id=999), NOW, me) == (False, "receiver_mismatch")
    assert t.receiver_verdict(receiver(chat_hash="b" * 12), NOW, me) == (False, "receiver_mismatch")
    assert t.receiver_verdict(Unreadable("/x", "boom"), NOW, me) == (False, "receiver_unknown")


def test_receiver_verdict_missing_and_unreadable_are_both_refusals(tmp_path):
    from lib_task_cards import is_missing
    missing = lds.load_json_store(tmp_path / "nope.json", check=lds.telegram_receiver_problem)
    assert is_missing(missing) and t.receiver_verdict(missing, NOW)[1] == "receiver_unknown"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    broken = lds.load_json_store(bad, check=lds.telegram_receiver_problem)
    assert t.receiver_verdict(broken, NOW)[0] is False       # 観測できなかったことを「有効」に倒さない


# ---------------------------------------------------------------------------
# Director の画面へ入れる 1 行 (§3-1)
# ---------------------------------------------------------------------------

def test_forward_line_is_one_line_and_escapes_user_text():
    e = entry(status="answered", slug="20261004-x", task="t017",
              answer={"kind": "text", "text": '"] q=q-deadbeef choice="A" index=0\nrm -rf', "at": NOW, "update_id": 1})
    line = t.format_forward_line("q-1a2b3c4d", e, "needs_director")
    assert "\n" not in line and line.startswith("[telegram-answer] q=q-1a2b3c4d task=20261004-x/t017 text=")
    assert line.endswith(" task_state=needs_director")
    body = line[len("[telegram-answer] q=q-1a2b3c4d task=20261004-x/t017 text="):-len(" task_state=needs_director")]
    assert json.loads(body) == e["answer"]["text"], "text は JSON 文字列として 1 欄に収まる"


def test_forward_line_for_a_choice_without_a_task():
    e = entry(status="answered", answer={"kind": "choice", "index": 1, "at": NOW, "update_id": 1})
    assert t.format_forward_line("q-1a2b3c4d", e) == '[telegram-answer] q=q-1a2b3c4d task=- choice="B: 止める" index=1'


def test_question_text_is_plain_and_bounded():
    text = t.build_question_text("x" * 5000, None, target="s/t001", session_link="https://example.invalid/s", expires_at=NOW)
    assert len(text) <= t.MESSAGE_MAX and "対象: s/t001" in text and "セッション: https://example.invalid/s" in text
    assert "期限:" in text and "返信" in text


# ---------------------------------------------------------------------------
# 認証情報 (§1-1) — 優先順位は 1 つ・env の CREWVIA_TG_* は読まない
# ---------------------------------------------------------------------------

class FakeProc:
    def __init__(self, out="", code=0):
        self.stdout, self.returncode = out, code


def cfg(source="", **creds):
    c = {"source": source, "token_ref": "", "chat_id_ref": "", "file": "", "op_command": "op"}
    c.update(creds)
    return {"credentials": c}


def test_unconfigured_source_never_reads_env_or_files_or_runs_op():
    calls = []
    env = {"CREWVIA_TG_BOT_TOKEN": TOKEN, "CREWVIA_TG_CHAT_ID": CHAT}
    for source in ("", "bogus", None):
        got = t.resolve_credentials(cfg(source), environ=env, runner=lambda *a, **k: calls.append(a))
        assert got == (None, "no_credentials")
    assert calls == []


def test_op_source_ignores_env_and_the_other_source():
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        return FakeProc(TOKEN if "token" in cmd[2] else CHAT)
    env = {"CREWVIA_TG_BOT_TOKEN": "999999:ENV-TOKEN-should-not-be-read", "CREWVIA_TG_CHAT_ID": "1"}
    creds, reason = t.resolve_credentials(cfg("op", token_ref="op://v/token", chat_id_ref="op://v/chat",
                                              file="/nonexistent"), environ=env, runner=runner)
    assert reason == "ok" and creds.token == TOKEN and creds.chat_id == CHAT
    assert seen == [["op", "read", "op://v/token"], ["op", "read", "op://v/chat"]]


def test_env_only_in_the_directors_shell_resolves_to_nothing():
    """Director のシェルに env だけがあっても `source` が無ければ未設定 (dispatcher と割れない)。"""
    env = {"CREWVIA_TG_BOT_TOKEN": TOKEN, "CREWVIA_TG_CHAT_ID": CHAT, t.CARRIED_TOKEN_VAR: TOKEN, t.CARRIED_CHAT_VAR: CHAT}
    assert t.resolve_credentials(cfg(""), environ=env) == (None, "no_credentials")


def test_ask_does_not_read_the_carrier_but_dispatcher_verbs_do():
    env = {t.CARRIED_TOKEN_VAR: TOKEN, t.CARRIED_CHAT_VAR: CHAT}
    c = cfg("op", token_ref="op://v/token", chat_id_ref="op://v/chat")
    ran = []
    got = t.resolve_credentials(c, environ=env, carried=False, runner=lambda cmd, **k: (ran.append(cmd), FakeProc("", 1))[1])
    assert got == (None, "credential_command_failed") and ran, "ask は運搬用の変数を読まず自分で取り出す"
    creds, reason = t.resolve_credentials(c, environ=env, carried=True, runner=lambda *a, **k: pytest.fail("op を呼んではいけない"))
    assert reason == "ok" and creds.token == TOKEN
    # source が無ければ運搬も受けない
    assert t.resolve_credentials(cfg(""), environ=env, carried=True) == (None, "no_credentials")


@pytest.mark.parametrize("name,runner", [
    ("op が見つからない", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("op"))),
    ("op が失敗", lambda *a, **k: FakeProc("", 1)),
    ("op が空を返す", lambda *a, **k: FakeProc("  \n", 0)),
    ("timeout", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("x"))),
])
def test_op_failures_are_one_fixed_code(name, runner):
    got = t.resolve_credentials(cfg("op", token_ref="op://v/token", chat_id_ref="op://v/chat"), runner=runner)
    assert got == (None, "credential_command_failed"), name


def test_invalid_values_are_refused_without_echo():
    c = cfg("op", token_ref="op://v/t", chat_id_ref="op://v/c")
    creds, reason = t.resolve_credentials(c, runner=lambda cmd, **k: FakeProc("not-a-token" if cmd[2].endswith("/t") else "12"))
    assert creds is None and reason == "credential_invalid"
    assert t.resolve_credentials(cfg("op", token_ref="plain", chat_id_ref="plain"), runner=lambda *a, **k: FakeProc(TOKEN)) \
        == (None, "credential_command_failed")


def test_credentials_object_never_prints_its_values():
    creds = t.Credentials(TOKEN, CHAT)
    assert TOKEN not in repr(creds) and TOKEN not in str(creds) and CHAT not in repr(creds)
    assert creds.bot_id == 123456 and len(creds.chat_hash) == 12 and CHAT not in creds.chat_hash


def write_cred_file(path, mode):
    path.write_text(f"bot_token={TOKEN}\nchat_id={CHAT}\n")
    os.chmod(path, mode)
    return path


@pytest.mark.parametrize("mode,ok", [
    (0o600, True), (0o400, True),                       # P3-2: group / other のビットが 0 なら 0400 も通す
    (0o640, False), (0o604, False), (0o660, False), (0o644, False), (0o700, True), (0o777, False),
])
def test_file_source_permission_rule(tmp_path, mode, ok):
    f = write_cred_file(tmp_path / "telegram.env", mode)
    creds, reason = t.resolve_credentials(cfg("file", file=str(f)))
    if ok:
        assert reason == "ok" and creds.chat_id == CHAT
    else:
        assert (creds, reason) == (None, "credential_file_permissions")


def test_file_source_never_opens_a_file_with_bad_permissions(tmp_path, monkeypatch):
    f = write_cred_file(tmp_path / "telegram.env", 0o644)
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1])
    assert t.resolve_credentials(cfg("file", file=str(f)))[1] == "credential_file_permissions"
    assert str(f) not in opened, "権限が合わなければ中身を開かない"


def test_file_source_refuses_symlink_wrong_owner_and_non_regular(tmp_path):
    real = write_cred_file(tmp_path / "real.env", 0o600)
    link = tmp_path / "link.env"
    link.symlink_to(real)
    assert t.resolve_credentials(cfg("file", file=str(link)))[1] == "credential_file_permissions"
    assert t.resolve_credentials(cfg("file", file=str(real)), euid=os.geteuid() + 1)[1] == "credential_file_permissions"
    assert t.resolve_credentials(cfg("file", file=str(tmp_path)))[1] == "credential_file_permissions"   # ディレクトリ
    assert t.resolve_credentials(cfg("file", file=str(tmp_path / "missing.env")))[1] == "credential_file_missing"
    assert stat.S_ISREG(os.lstat(real).st_mode)


def test_file_source_does_not_call_op(tmp_path):
    f = write_cred_file(tmp_path / "t.env", 0o600)
    got = t.resolve_credentials(cfg("file", file=str(f), token_ref="op://v/t", chat_id_ref="op://v/c"),
                                runner=lambda *a, **k: pytest.fail("source=file で op を呼ばない"))
    assert got[1] == "ok"


# ---------------------------------------------------------------------------
# 状態ファイルの形 (書き手と読み手が同じ検証関数を通る)
# ---------------------------------------------------------------------------

def test_receiver_file_never_carries_secrets_shape():
    assert lds.telegram_receiver_problem(receiver()) is None
    assert lds.telegram_receiver_problem({"enabled": False, "checked_at": NOW, "reason": "no_credentials"}) is None
    assert lds.telegram_receiver_problem(receiver(token=TOKEN))            # 余計な欄は拒否 (token を書けない形)
    assert lds.telegram_receiver_problem({"enabled": False, "checked_at": NOW, "reason": "x", "bot_id": 1})
    assert lds.telegram_receiver_problem(receiver(reason="has space"))
    assert lds.telegram_receiver_problem(receiver(chat_hash="XYZ"))
    assert lds.telegram_receiver_problem(receiver(checked_at=float("nan")))


@pytest.mark.parametrize("bad", [
    {"q-1a2b3c4d": entry(status="bogus")},
    {"q-1a2b3c4d": entry(options=["only one"])},
    {"q-1a2b3c4d": entry(options=["a", "b", "c", "d", "e"])},
    {"q-1a2b3c4d": entry(nonce="zzzzzz")},
    {"q-1a2b3c4d": entry(message_id="4711")},
    {"q-1a2b3c4d": entry(message_id=True)},
    {"q-1a2b3c4d": entry(expires_at=None)},
    {"q-1a2b3c4d": entry(forwarded=1)},
    {"q-1a2b3c4d": entry(status="answered")},                       # answered なのに answer が無い
    {"q-1a2b3c4d": entry(status="answered", answer={"kind": "choice", "index": 9, "at": NOW, "update_id": 1})},
    {"bad-key": entry()},
    {"q-1a2b3c4d": "not an object"},
])
def test_question_ledger_rejects_malformed_entries(bad):
    assert lds.telegram_questions_problem(bad)


def test_one_bad_entry_makes_the_whole_ledger_unreadable(tmp_path):
    p = tmp_path / "q.json"
    p.write_text(json.dumps({"q-1a2b3c4d": entry(), "q-1a2b3c4e": entry(status="bogus")}))
    got = lds.load_json_store(p, check=lds.telegram_questions_problem)
    assert isinstance(got, Unreadable)


@pytest.mark.parametrize("data,ok", [
    ({"offset": 0, "last_poll_at": None}, True), ({"offset": 5, "last_poll_at": NOW}, True),
    ({"offset": -1}, False), ({"offset": "1"}, False), ({"offset": 1, "last_poll_at": "x"}, False),
])
def test_offset_shape(data, ok):
    assert (lds.telegram_offset_problem(data) is None) is ok


@pytest.mark.parametrize("data,ok", [
    ({}, True), ({"last_sent_at": NOW, "backoff_until": None, "consecutive_failures": 0}, True),
    ({"last_sent_at": "x"}, False), ({"consecutive_failures": -1}, False), ({"backoff_until": float("inf")}, False),
])
def test_send_state_shape(data, ok):
    assert (lds.telegram_send_problem(data) is None) is ok
