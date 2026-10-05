#!/usr/bin/env python3
"""読めない offset ファイルでも受信は止まらない (PR #281 の Codex P2・5 巡目)。

修正前: `_receive_updates` が読めない offset を `{}` に置き換えた後 `offset_state['offset']` を引き、KeyError。
ファイルが壊れている限り毎サイクル同じ所で落ち、押下が永久に受信されなかった。

不変条件: offset が「壊れ / 型違い / 権限なし / 負の値」のどれでも、押下は 1 サイクルで受信され、offset は正しい形で上書きされる。
無い (ENOENT) は通常運用で `offset_unreadable` を出さない。

実行: python3 -m pytest tests/test_telegram_unreadable_offset_still_receives.py -q
"""

import json
import os
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
SCRIPTS = THIS_DIR.parent / "scripts"
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(SCRIPTS))

import lib_telegram as t  # noqa: E402
from telegram_fake_api import FakeBotApi, callback_update  # noqa: E402

TOKEN, CHAT = "123456:FAKE-TOKEN-abcdefghij", "5550001"
NOW = 1_900_000_000.0
QID = "q-000000aa"


def creds():
    return t.Credentials(TOKEN, CHAT)


@pytest.fixture
def api():
    with FakeBotApi(token=TOKEN) as a:
        yield a


@pytest.fixture
def reg(tmp_path):
    d = tmp_path / "registry" / "daemons"
    d.mkdir(parents=True)
    ident = creds()
    ledger = {QID: {"nonce": "9f3a1c", "question": "どうする?", "options": ["A", "B"], "message_id": 77, "status": "open",
                    "created_at": NOW - 600, "expires_at": NOW + 3600, "forwarded": False,
                    "bot_id": ident.bot_id, "chat_hash": ident.chat_hash}}
    (d / t.QUESTIONS_FILE).write_text(json.dumps(ledger))
    return d


def _break_garbage(path):
    path.write_text("{not json")


def _break_wrong_type(path):
    path.write_text(json.dumps({"offset": "12", "last_poll_at": None}))


def _break_negative(path):
    path.write_text(json.dumps({"offset": -1, "last_poll_at": None}))


def _break_list(path):
    path.write_text("[1, 2]")


def _break_directory(path):
    path.mkdir()        # 通常ファイルでない (read_regular_text_or_unreadable が断る)


BREAKS = [_break_garbage, _break_wrong_type, _break_negative, _break_list, _break_directory]


@pytest.mark.parametrize("brk", BREAKS, ids=lambda f: f.__name__)
def test_unreadable_offset_does_not_stop_the_press_from_being_received(reg, api, brk):
    path = reg / t.OFFSET_FILE
    brk(path)
    uid = api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1"))
    res = t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert res["error"] is None, res
    assert res["answered"] == 1
    assert json.loads((reg / t.QUESTIONS_FILE).read_text())[QID]["status"] == "answered"
    assert res.get("offset_unreadable"), "壊れていることが見えない (黙って直し続けている)"
    assert api.calls[0][1]["offset"] == 0, "読めない offset は 0 から読み直す"


@pytest.mark.parametrize("brk", [_break_garbage, _break_wrong_type, _break_negative, _break_list], ids=lambda f: f.__name__)
def test_next_write_repairs_the_offset_file(reg, api, brk):
    brk(reg / t.OFFSET_FILE)
    uid = api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1"))
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    fixed = json.loads((reg / t.OFFSET_FILE).read_text())
    assert fixed["offset"] == uid + 1
    again = t.poll_once(reg, creds(), api_base=api.url, now=NOW + 60)
    assert "offset_unreadable" not in again, "直った後は出さない"


def test_unreadable_offset_replay_does_not_forward_twice(reg, api):
    """0 から読み直しても、すでに answered の質問は CAS で変わらない (at-least-once の重複は転送されない)。"""
    api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1"))
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    before = json.loads((reg / t.QUESTIONS_FILE).read_text())
    _break_garbage(reg / t.OFFSET_FILE)
    # open が無いと通信しない → 別の open な質問を足して再受信させる
    ledger = dict(before)
    ledger["q-000000bb"] = dict(before[QID], status="open", message_id=78)
    ledger["q-000000bb"].pop("answer", None)
    ledger["q-000000bb"]["forwarded"] = False
    (reg / t.QUESTIONS_FILE).write_text(json.dumps(ledger))
    t.poll_once(reg, creds(), api_base=api.url, now=NOW + 60)
    after = json.loads((reg / t.QUESTIONS_FILE).read_text())
    assert after[QID] == before[QID], "再受信で answered の質問が書き換わった"


def test_missing_offset_is_normal_and_silent(reg, api):
    api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1"))
    res = t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert res["answered"] == 1 and "offset_unreadable" not in res


@pytest.mark.skipif(os.geteuid() == 0, reason="root は権限で拒否されない")
def test_permission_denied_offset_still_receives(reg, api):
    path = reg / t.OFFSET_FILE
    path.write_text(json.dumps({"offset": 5, "last_poll_at": None}))
    path.chmod(0o000)
    try:
        api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1"))
        res = t.poll_once(reg, creds(), api_base=api.url, now=NOW)
        assert res["answered"] == 1 and res.get("offset_unreadable") == "EACCES"
    finally:
        path.chmod(0o600)


# --- 同じ族: 送信状態 (telegram-send.json) が読めない ---------------------------------------------------------

def test_unreadable_send_state_is_rewritten_by_the_next_send_and_availability_says_no(reg, api):
    """`telegram_available` は読めない送信状態を「使えない」と答える (黙って True にしない)。送る側は 1 回送って書き直す。"""
    ident = creds()
    (reg / t.RECEIVER_FILE).write_text(json.dumps({"enabled": True, "checked_at": NOW, "reason": "ok",
                                                   "bot_id": ident.bot_id, "chat_hash": ident.chat_hash}))
    (reg / t.SEND_FILE).write_text("{not json")
    assert t.telegram_available(reg, NOW) is False
    res = t.send_message(reg, ident, "hi", api_base=api.url, now=NOW)
    assert res.ok, res.error
    assert json.loads((reg / t.SEND_FILE).read_text())["bot_id"] == ident.bot_id     # 正しい形に直った
    assert t.telegram_available(reg, NOW + 60) is True
