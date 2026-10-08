#!/usr/bin/env python3
"""offset の本体が**読めるが書けない**とき、退避先の新しい進捗を使う (PR #289 の Codex P2)。

修正前: `read_offset_effective` は本体が**読めない**ときだけ退避先を見た。本体が読めるが置換できない
(immutable・sticky dir の所有者違い 等) と `store_offset` は退避先に進捗を書くのに、以後の poll / run_cycle は古い本体の
offset と last_poll_at を読み続け、同じ update を読み直し・間引きも効かなかった。

規則: 本体と退避先が両方読めたら offset の大きい方 (同じなら last_poll_at の新しい方)。

実行: python3 -m pytest tests/test_telegram_readable_but_unwritable_offset_uses_fallback.py -q
"""

import json
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent / "scripts"))

import lib_telegram as t  # noqa: E402
from telegram_fake_api import FakeBotApi, callback_update  # noqa: E402

TOKEN, CHAT = "123456:FAKE-TOKEN-abcdefghij", "5550001"
NOW = 1_900_000_000.0
QID, QID2 = "q-000000aa", "q-000000bb"


def creds():
    return t.Credentials(TOKEN, CHAT)


def entry(qid, message_id):
    ident = creds()
    return {"nonce": "9f3a1c", "question": "どうする?", "options": ["A", "B"], "message_id": message_id, "status": "open",
            "created_at": NOW - 600, "expires_at": NOW + 3600, "forwarded": True,
            "bot_id": ident.bot_id, "chat_hash": ident.chat_hash}


def offset_doc(offset, last_poll_at):
    ident = creds()
    return {"offset": offset, "last_poll_at": last_poll_at, "bot_id": ident.bot_id, "chat_hash": ident.chat_hash}


@pytest.fixture
def api():
    with FakeBotApi(token=TOKEN) as a:
        yield a


@pytest.fixture
def reg(tmp_path):
    d = tmp_path / "registry" / "daemons"
    d.mkdir(parents=True)
    (d / t.QUESTIONS_FILE).write_text(json.dumps({QID: entry(QID, 77), QID2: entry(QID2, 78)}))
    return d


@pytest.fixture
def primary_replace_fails(monkeypatch):
    """本体 (telegram-offset.json) の置換だけが失敗する。退避先・他のファイルは本物の書き込み。"""
    real = t._write_json_locked

    def fake(path, data):
        if Path(path).name == t.OFFSET_FILE:
            return False
        return real(path, data)

    monkeypatch.setattr(t, "_write_json_locked", fake)


def test_readable_but_unwritable_primary_does_not_reprocess_the_same_update(reg, api, primary_replace_fails):
    """(a) 本体は読める (offset 5) が書けない。2 回目の poll は退避先の新しい offset で getUpdates を呼ぶ。"""
    (reg / t.OFFSET_FILE).write_text(json.dumps(offset_doc(5, NOW - 100)))
    uid = api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1"))
    first = t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert first["answered"] == 1 and first.get("offset_unwritable") is True
    assert (reg / t.OFFSET_FALLBACK_FILE).is_file()
    assert json.loads((reg / t.OFFSET_FILE).read_text())["offset"] == 5, "本体は古いまま (書けない)"

    second = t.poll_once(reg, creds(), api_base=api.url, now=NOW + 60)
    assert api.calls_of("getUpdates")[1]["offset"] == uid + 1, "古い本体の offset で同じ update を読み直した"
    assert second["processed"] == 0 and second["rejected"] == 0


def test_readable_but_unwritable_primary_keeps_the_throttle(reg, primary_replace_fails):
    """(a') 間引きの根拠 last_poll_at も退避先の新しい値を読む。"""
    (reg / t.OFFSET_FILE).write_text(json.dumps(offset_doc(5, NOW - 1000)))
    t.store_offset(reg, offset_doc(6, NOW), {})
    state, primary = t.read_offset_effective(reg)
    assert state["offset"] == 6 and state["last_poll_at"] == NOW
    assert primary["offset"] == 5


def test_directory_in_place_of_the_primary_still_works(reg, api):
    """(b) 本体が通常ファイルでない (dir)。読めないので退避先を使う (従来どおり)。"""
    (reg / t.OFFSET_FILE).mkdir()
    uid = api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1"))
    first = t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert first["answered"] == 1 and first.get("offset_unwritable") is True
    t.poll_once(reg, creds(), api_base=api.url, now=NOW + 60)
    assert api.calls_of("getUpdates")[1]["offset"] == uid + 1


def test_stale_fallback_with_a_smaller_offset_loses_to_the_primary(reg):
    """(c) 退避先の削除に失敗して古い退避先が残っても、本体の新しい値が勝つ。"""
    (reg / t.OFFSET_FILE).write_text(json.dumps(offset_doc(20, NOW)))
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps(offset_doc(7, NOW + 500)))   # last_poll_at が新しくても offset が小さい
    state, primary = t.read_offset_effective(reg)
    assert state["offset"] == 20 and state["last_poll_at"] == NOW


def test_equal_offsets_take_the_newer_last_poll_at(reg):
    (reg / t.OFFSET_FILE).write_text(json.dumps(offset_doc(9, NOW)))
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps(offset_doc(9, NOW + 30)))
    assert t.read_offset_effective(reg)[0]["last_poll_at"] == NOW + 30
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps(offset_doc(9, None)))
    assert t.read_offset_effective(reg)[0]["last_poll_at"] == NOW


def test_unreadable_fallback_or_missing_primary_behave_as_before(reg):
    assert t.is_missing(t.read_offset_effective(reg)[0])
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps(offset_doc(9, NOW)))
    assert t.is_missing(t.read_offset_effective(reg)[0]), "本体が無いだけなら退避先は見ない"
    (reg / t.OFFSET_FILE).write_text(json.dumps(offset_doc(3, NOW)))
    (reg / t.OFFSET_FALLBACK_FILE).write_text("{not json")
    assert t.read_offset_effective(reg)[0]["offset"] == 3


# --- PR #289 追加 2 (Codex P2): 大小を比べる前に識別子で候補を選ぶ ------------------------------------------------------

OLD_BOT = t.Credentials("999999:OLD-BOT-TOKEN-abcdefghij", CHAT)


def old_offset_doc(offset, last_poll_at):
    return {"offset": offset, "last_poll_at": last_poll_at, "bot_id": OLD_BOT.bot_id, "chat_hash": OLD_BOT.chat_hash}


def test_old_bot_large_primary_loses_to_current_bot_fallback(reg, api, primary_replace_fails):
    """(a) 本体 = 旧 bot の大きい offset (書けない)・退避先 = 新 bot の小さい offset → 退避先。2 回目で同じ update を取り直さない。"""
    (reg / t.OFFSET_FILE).write_text(json.dumps(old_offset_doc(5000, NOW - 100)))
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps(offset_doc(2, NOW - 50)))
    ident = t.identity_of(creds())
    state, _ = t.read_offset_effective(reg, identity=ident)
    assert state["offset"] == 2 and state["bot_id"] == creds().bot_id

    uid = api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1"))
    first = t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert first["answered"] == 1
    assert api.calls_of("getUpdates")[0]["offset"] == 2, "旧 bot の大きい offset (→ 0 へのリセット) に負けた"
    second = t.poll_once(reg, creds(), api_base=api.url, now=NOW + 60)
    assert api.calls_of("getUpdates")[1]["offset"] == uid + 1, "毎回最初の batch を取り直している"
    assert second["processed"] == 0 and second["rejected"] == 0
    cur, _ = t.read_offset_effective(reg, identity=ident)
    assert cur["last_poll_at"] == NOW + 60, "退避先の last_poll_at (間引き) が無視された"


def test_both_current_bot_still_takes_the_larger(reg):
    """(b) 両方とも今の bot → 大きい方。"""
    ident = t.identity_of(creds())
    (reg / t.OFFSET_FILE).write_text(json.dumps(offset_doc(5, NOW)))
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps(offset_doc(6, NOW + 1)))
    assert t.read_offset_effective(reg, identity=ident)[0]["offset"] == 6
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps(offset_doc(4, NOW + 1)))
    assert t.read_offset_effective(reg, identity=ident)[0]["offset"] == 5


def test_both_old_bot_falls_back_to_primary(reg):
    """(c) どちらも旧 bot → 従来どおり本体 (_receive_updates が 0 から始める)。"""
    ident = t.identity_of(creds())
    (reg / t.OFFSET_FILE).write_text(json.dumps(old_offset_doc(5, NOW)))
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps(old_offset_doc(9, NOW)))
    state, primary = t.read_offset_effective(reg, identity=ident)
    assert state["offset"] == 5 and primary["offset"] == 5


def test_current_bot_primary_beats_old_bot_larger_fallback(reg):
    """逆向き: 退避先の方が旧 bot で大きくても、今の bot の本体が勝つ。"""
    ident = t.identity_of(creds())
    (reg / t.OFFSET_FILE).write_text(json.dumps(offset_doc(3, NOW)))
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps(old_offset_doc(900, NOW + 5)))
    assert t.read_offset_effective(reg, identity=ident)[0]["offset"] == 3


def test_availability_ignores_state_of_another_bot(reg):
    """同じ型 (識別子を見ずに状態を使う) — `telegram_available` が旧 bot の受信状態・バックオフを今の bot のものとして読まない。"""
    ident = t.identity_of(creds())
    (reg / t.RECEIVER_FILE).write_text(json.dumps({"enabled": True, "checked_at": NOW, "reason": "ok",
                                                   "bot_id": creds().bot_id, "chat_hash": creds().chat_hash}))
    old = {"backoff_until": NOW + 900, "bot_id": OLD_BOT.bot_id, "chat_hash": OLD_BOT.chat_hash}
    (reg / t.SEND_FILE).write_text(json.dumps(old))
    assert t.telegram_available(reg, NOW) is False, "識別子を渡さない従来の呼び方は変えない"
    assert t.telegram_available(reg, NOW, identity=ident) is True
    (reg / t.RECEIVER_FILE).write_text(json.dumps({"enabled": True, "checked_at": NOW, "reason": "ok",
                                                   "bot_id": OLD_BOT.bot_id, "chat_hash": OLD_BOT.chat_hash}))
    assert t.telegram_available(reg, NOW, identity=ident) is False, "別の bot の受信状態"
