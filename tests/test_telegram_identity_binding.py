#!/usr/bin/env python3
"""永続する Telegram の状態を bot・chat に結び付ける (PR #281 の Codex P1・2 巡目)。

質問台帳・offset・送信状態が「どの bot / どの chat のものか」を持たないと、認証情報を切り替えたとき (source の op ↔ file・
1Password 側でトークンを差し替える・別の bot / chat にする) に:
  * 前の bot の offset を新しい bot の `getUpdates` に使う (update_id は bot ごとの列。新しい bot の update を飛ばす)
  * 前の bot で出した open な質問が、新しい bot の返信と**照合されうる** (message_id は chat ごとの連番で衝突する)
識別子は秘密でない `bot_id` と `chat_hash` (receiver.json と同じもの)。設計 §2-5b の表の全セルを押さえる。

実行: python3 -m pytest tests/test_telegram_identity_binding.py -q
"""

import json
import sys
import time
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent / "scripts"))

import lib_daemon_state as lds  # noqa: E402
import lib_telegram as t  # noqa: E402
from telegram_fake_api import FakeBotApi, callback_update, reply_update  # noqa: E402

TOKEN_A, CHAT_A = "123456:FAKE-TOKEN-AAAAAAAAAA", "5550001"
TOKEN_B, CHAT_B = "654321:FAKE-TOKEN-BBBBBBBBBB", "5550002"
NOW = 1_900_000_000.0


def creds_a():
    return t.Credentials(TOKEN_A, CHAT_A)


def creds_b():
    return t.Credentials(TOKEN_B, CHAT_B)


@pytest.fixture
def apis():
    with FakeBotApi(token=TOKEN_A) as a, FakeBotApi(token=TOKEN_B) as b:
        yield a, b


@pytest.fixture
def reg(tmp_path):
    d = tmp_path / "registry" / "daemons"
    d.mkdir(parents=True)
    return d


def open_question(qid="q-0000000a", message_id=4711, identity=None, **over):
    e = {"nonce": "9f3a1c", "question": "どうする?", "options": ["A", "B"], "message_id": message_id, "status": "open",
         "created_at": NOW - 60, "expires_at": NOW + 3600, "forwarded": False}
    if identity is not None:
        e.update({"bot_id": identity.bot_id, "chat_hash": identity.chat_hash})
    e.update(over)
    return {qid: e}


def write(reg, name, data):
    (reg / name).write_text(json.dumps(data))


def read(reg, name):
    return json.loads((reg / name).read_text())


# ---------------------------------------------------------------------------
# 再現: 修正前の 2 つの取り違え
# ---------------------------------------------------------------------------

def test_a_reply_in_the_new_chat_is_not_matched_to_the_old_bots_question(reg, apis):
    """message_id は chat ごとの連番。新しい bot / chat の 4711 番への返信が、前の bot の質問 (message_id 4711) の答えになってはいけない。"""
    api_a, api_b = apis
    write(reg, t.QUESTIONS_FILE, open_question(identity=creds_a()))
    api_b.queue_update(reply_update(int(CHAT_B), 4711, "これは新しい bot の別の話題への返信"))
    res = t.poll_once(reg, creds_b(), api_base=api_b.url, now=NOW)
    ledger = read(reg, t.QUESTIONS_FILE)
    assert res["answered"] == 0
    assert ledger["q-0000000a"]["status"] == "withdrawn", "前の bot の open な質問は withdrawn (別の bot では答えを受けられない)"
    assert "answer" not in ledger["q-0000000a"]
    assert api_b.calls_of("editMessageReplyMarkup") == [], "別の bot の message_id のボタンを消そうとしない (別の bot では消せない)"


def test_the_old_bots_offset_is_not_used_for_the_new_bot(reg, apis):
    """update_id の列は bot ごと。前の bot の offset (9100) を新しい bot の getUpdates に使うと、新しい bot の 9001 を飛ばす。"""
    api_a, api_b = apis
    write(reg, t.OFFSET_FILE, {"offset": 9100, "last_poll_at": NOW - 100, "bot_id": creds_a().bot_id, "chat_hash": creds_a().chat_hash})
    ask = open_question(qid="q-0000000b", message_id=77, identity=creds_b())
    write(reg, t.QUESTIONS_FILE, ask)
    uid = api_b.queue_update(callback_update(int(CHAT_B), 77, "q-0000000b.9f3a1c.1", update_id=9001))
    res = t.poll_once(reg, creds_b(), api_base=api_b.url, now=NOW)
    assert api_b.calls_of("getUpdates")[0]["offset"] == 0, "他の bot の offset は捨てて最初から"
    assert res["answered"] == 1 and read(reg, t.QUESTIONS_FILE)["q-0000000b"]["status"] == "answered"
    assert read(reg, t.OFFSET_FILE)["bot_id"] == creds_b().bot_id and read(reg, t.OFFSET_FILE)["offset"] == uid + 1


# ---------------------------------------------------------------------------
# 表の各セル: 状態ファイル × (認証情報が変わった / 識別子の無い旧形式 / 同じ)
# ---------------------------------------------------------------------------

def test_a_same_bot_but_different_chat_is_also_a_different_identity(reg, apis):
    """bot_id が同じでも chat が違えば message_id の連番は別。chat_hash も見る。"""
    api_a, _ = apis
    other_chat = t.Credentials(TOKEN_A, CHAT_B)
    write(reg, t.QUESTIONS_FILE, open_question(identity=other_chat))
    t.poll_once(reg, creds_a(), api_base=api_a.url, now=NOW)
    assert read(reg, t.QUESTIONS_FILE)["q-0000000a"]["status"] == "withdrawn"


def test_a_question_without_an_identity_is_treated_as_someone_elses(reg, apis):
    """識別子導入前 (この PR の中で書かれた旧形式) は「今の認証情報のもの」と言えない → 取り下げ。別の bot の可能性を排除できない。"""
    api_a, _ = apis
    write(reg, t.QUESTIONS_FILE, open_question())
    api_a.queue_update(reply_update(int(CHAT_A), 4711, "旧形式の質問への返信"))
    res = t.poll_once(reg, creds_a(), api_base=api_a.url, now=NOW)
    led = read(reg, t.QUESTIONS_FILE)["q-0000000a"]
    assert res["answered"] == 0 and led["status"] == "withdrawn" and led["closed_reason"] == "identity_changed"
    assert led["unbutton"] is False


def test_an_offset_without_an_identity_is_discarded(reg, apis):
    api_a, _ = apis
    write(reg, t.OFFSET_FILE, {"offset": 9100, "last_poll_at": NOW - 100})
    write(reg, t.QUESTIONS_FILE, open_question(identity=creds_a()))
    t.poll_once(reg, creds_a(), api_base=api_a.url, now=NOW)
    assert api_a.calls_of("getUpdates")[0]["offset"] == 0
    assert read(reg, t.OFFSET_FILE)["bot_id"] == creds_a().bot_id


def test_the_same_identity_keeps_its_offset_and_its_questions(reg, apis):
    """対照: 認証情報が変わっていなければ何も捨てない (取り下げ過ぎ・offset の巻き戻しの検出)。"""
    api_a, _ = apis
    write(reg, t.OFFSET_FILE, dict(t.identity_of(creds_a()), offset=9100, last_poll_at=NOW - 100))
    write(reg, t.QUESTIONS_FILE, open_question(identity=creds_a()))
    res = t.poll_once(reg, creds_a(), api_base=api_a.url, now=NOW)
    assert api_a.calls_of("getUpdates")[0]["offset"] == 9100
    assert read(reg, t.QUESTIONS_FILE)["q-0000000a"]["status"] == "open" and res["identity_changed"] == []


def test_closed_questions_of_the_old_bot_are_left_alone(reg, apis):
    """answered / expired 等は触らない (answered の転送は台帳の中身だけで済む)。open だけを取り下げる。"""
    api_a, _ = apis
    old = creds_a()
    led = open_question("q-00000001", identity=old, status="answered", answer={"kind": "choice", "index": 0, "label": "A", "update_id": 5, "at": NOW - 5}, forwarded=False)
    led.update(open_question("q-00000002", identity=old, status="expired", closed_at=NOW - 10))
    led.update(open_question("q-00000003", identity=old))
    write(reg, t.QUESTIONS_FILE, led)
    res = t.poll_once(reg, creds_b(), api_base=api_b_url(apis), now=NOW)
    got = read(reg, t.QUESTIONS_FILE)
    assert got["q-00000001"]["status"] == "answered" and got["q-00000001"]["forwarded"] is False
    assert got["q-00000002"]["status"] == "expired"
    assert got["q-00000003"]["status"] == "withdrawn" and res["identity_changed"] == ["q-00000003"]


def api_b_url(apis):
    return apis[1].url


def test_a_button_of_the_old_bot_is_rejected_not_forwarded(reg, apis):
    """取り下げた後に押された古いボタン (同じ message_id・同じ nonce) は「不明な質問」。答えとして通らない。"""
    _, api_b = apis
    write(reg, t.QUESTIONS_FILE, open_question(identity=creds_a()))
    api_b.queue_update(callback_update(int(CHAT_B), 4711, "q-0000000a.9f3a1c.0", update_id=1))
    res = t.poll_once(reg, creds_b(), api_base=api_b.url, now=NOW)
    got = read(reg, t.QUESTIONS_FILE)["q-0000000a"]
    assert res["answered"] == 0 and "answer" not in got and got["status"] == "withdrawn"


def test_send_state_of_another_bot_is_not_inherited(reg, apis):
    """別の bot のバックオフ・レート記憶で新しい bot の送信を止めない (識別子の無い旧形式も同じ)。"""
    _, api_b = apis
    for stale in (dict(t.identity_of(creds_a()), backoff_until=NOW + 3600, last_sent_at=NOW),
                  {"backoff_until": NOW + 3600, "last_sent_at": NOW}):
        write(reg, t.SEND_FILE, stale)
        res = t.send_message(reg, creds_b(), "hello", api_base=api_b.url, now=NOW, sleep=lambda s: None, clock=lambda: NOW)
        assert res.ok, res
        assert read(reg, t.SEND_FILE)["bot_id"] == creds_b().bot_id
    assert len(api_b.calls_of("sendMessage")) == 2


def test_send_state_of_the_same_bot_still_backs_off(reg, apis):
    """対照: 同じ identity のバックオフは今までどおり効く。"""
    _, api_b = apis
    write(reg, t.SEND_FILE, dict(t.identity_of(creds_b()), backoff_until=NOW + 3600))
    res = t.send_message(reg, creds_b(), "hello", api_base=api_b.url, now=NOW, sleep=lambda s: None, clock=lambda: NOW)
    assert not res.ok and api_b.calls_of("sendMessage") == []


def test_identity_fields_are_validated_and_never_secret():
    ok = {"offset": 1, "last_poll_at": None, "bot_id": 123456, "chat_hash": "0123456789ab"}
    assert lds.telegram_offset_problem(ok) is None
    assert lds.telegram_offset_problem({"offset": 1}) is None, "識別子の無い旧形式は読める (使うかは lib_telegram.is_bound が決める)"
    assert lds.telegram_offset_problem(dict(ok, chat_hash=None)) and lds.telegram_offset_problem({"offset": 1, "bot_id": 1})
    assert lds.telegram_offset_problem(dict(ok, bot_id=True)) and lds.telegram_offset_problem(dict(ok, chat_hash="XYZ"))
    c = creds_a()
    assert TOKEN_A not in json.dumps(t.identity_of(c)) and CHAT_A not in json.dumps(t.identity_of(c))


def test_rebind_frees_the_open_question_limit_from_the_old_bots_questions(reg):
    """旧 bot の open な質問 8 件が、新しい bot の ask を上限で断らない (rebind が先)。"""
    old = {}
    for i in range(t.MAX_OPEN_QUESTIONS):
        old.update(open_question(f"q-0000000{i}", message_id=100 + i, identity=creds_a()))
    write(reg, t.QUESTIONS_FILE, old)
    got, withdrawn = t.rebind_ledger(old, t.identity_of(creds_b()), NOW)
    assert len(withdrawn) == t.MAX_OPEN_QUESTIONS and len(t.open_questions(got, NOW)) == 0


def test_rebind_is_pure_and_idempotent():
    led = open_question(identity=creds_a())
    before = json.dumps(led, sort_keys=True)
    once, w1 = t.rebind_ledger(led, t.identity_of(creds_b()), NOW)
    twice, w2 = t.rebind_ledger(once, t.identity_of(creds_b()), NOW)
    assert json.dumps(led, sort_keys=True) == before, "入力を書き換えない"
    assert w1 == ["q-0000000a"] and w2 == [] and twice == once, "2 回目は何も取り下げない (Director への通知は 1 回)"
