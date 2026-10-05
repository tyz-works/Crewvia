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
