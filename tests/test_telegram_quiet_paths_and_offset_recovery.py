#!/usr/bin/env python3
"""Telegram 経路の P3 4 件 (PR #281 の merge 時に残したもの)。

1. 未設定 (台帳なし) の `cancel` / `list` / `verify` は 1 バイトも書かない (0 byte の lock を作らない)。
2. 閉じた質問のボタンが Telegram に残っている間は、他に未回答が無くても受信して「回答済み」を返す。
   質問 0 件・古い閉じた質問だけなら getUpdates を叩かない (節約は保つ)。
3. offset が読めた / 書けた回復で、一度だけ通知の台帳を畳む (dispatcher 側。glue と同じ Harness)。
4. offset の本体が書けないとき: 二重処理しない・間引きが効く・知らせる (印を返す)・回復で退避先を消す。

実行: python3 -m pytest tests/test_telegram_quiet_paths_and_offset_recovery.py -q
"""

import json
import os
import subprocess
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
QID, QID2 = "q-000000aa", "q-000000bb"


def creds():
    return t.Credentials(TOKEN, CHAT)


def entry(qid, status="open", message_id=77, **kw):
    ident = creds()
    e = {"nonce": "9f3a1c", "question": "どうする?", "options": ["A", "B"], "message_id": message_id, "status": status,
         "created_at": NOW - 600, "expires_at": NOW + 3600, "forwarded": True,
         "bot_id": ident.bot_id, "chat_hash": ident.chat_hash}
    if status == "answered":
        e["answer"] = {"kind": "choice", "index": 0, "at": NOW - 100, "update_id": 1}
    if status != "open":
        e["closed_at"] = NOW - 100
    e.update(kw)
    return e


@pytest.fixture
def api():
    with FakeBotApi(token=TOKEN) as a:
        yield a


@pytest.fixture
def reg(tmp_path):
    d = tmp_path / "registry" / "daemons"
    d.mkdir(parents=True)
    return d


def write_ledger(reg, **entries):
    (reg / t.QUESTIONS_FILE).write_text(json.dumps(entries))


def tree(root):
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


# ---------------------------------------------------------------------------
# F1: 未設定で lock を作らない
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verb,args", [
    ("cancel", ["--q", QID, "--by", "screen"]),
    ("list", []),
    ("verify", ["--q", QID]),
])
def test_unconfigured_verbs_write_nothing(tmp_path, verb, args):
    reg = tmp_path / "registry" / "daemons"
    reg.mkdir(parents=True)
    cfg = tmp_path / "crewvia.yaml"
    cfg.write_text("telegram:\n  poll_interval_seconds: 10\n")        # credentials 無し = 未設定
    before = tree(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CREWVIA_TG", "_CREWVIA_TG")) and k != "AGENT_NAME"}
    r = subprocess.run([sys.executable, str(SCRIPTS / "lib_telegram.py"), verb, "--registry-dir", str(reg),
                        "--config", str(cfg), *args], capture_output=True, text=True, timeout=60, env=env)
    assert tree(tmp_path) == before, f"{verb} が未設定で何かを作った: {sorted(set(tree(tmp_path)) - set(before))}"
    assert not list(reg.glob("*.lock")), "0 byte の lock が残った"
    assert TOKEN not in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# F2: 閉じた質問のボタンの再押下に応答する
# ---------------------------------------------------------------------------

def test_pressing_an_answered_button_gets_a_reply_even_with_no_open_question(reg, api):
    write_ledger(reg, **{QID: entry(QID, "answered")})
    api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1", callback_id="cb-9"))
    res = t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert res["rejected"] == 1, res
    answers = api.calls_of("answerCallbackQuery")
    assert len(answers) == 1 and answers[0]["callback_query_id"] == "cb-9"
    assert "回答済み" in answers[0]["text"]


def test_pressing_a_withdrawn_button_gets_a_reply(reg, api):
    write_ledger(reg, **{QID: entry(QID, "withdrawn", unbutton=True)})
    api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.0", callback_id="cb-8"))
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert "画面で回答済み" in api.calls_of("answerCallbackQuery")[0]["text"]


def test_no_questions_or_only_old_closed_ones_never_call_get_updates(reg, api):
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)                       # 台帳なし
    old = entry(QID, "answered", closed_at=NOW - t.CLOSED_WATCH_SECONDS - 1)
    write_ledger(reg, **{QID: old})
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)                       # 閉じて久しい
    assert api.calls_of("getUpdates") == []


def _cycle_cmds(reg, ledger_entries, now=NOW):
    write_ledger(reg, **ledger_entries)
    cmds = []

    def runner(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
    cfg = {"poll_interval_seconds": 10}
    t.run_cycle(reg, reg.parent / "queue", cfg, "ok", lambda line: True, now=now, creds=creds(), runner=runner)
    return cmds


def test_run_cycle_polls_for_a_recently_closed_question_and_not_for_an_old_one(reg):
    assert _cycle_cmds(reg, {QID: entry(QID, "answered")}), "押されうるボタンが残っているのに poll が走らない"
    assert _cycle_cmds(reg, {QID: entry(QID, "answered", closed_at=NOW - t.CLOSED_WATCH_SECONDS - 1)}) == []


# ---------------------------------------------------------------------------
# F3 の lib 側の印 / F4: offset の本体が書けない
# ---------------------------------------------------------------------------

def test_poll_reports_offset_ok_only_when_it_actually_read_the_offset(reg, api):
    write_ledger(reg, **{QID: entry(QID)})
    assert t.poll_once(reg, creds(), api_base=api.url, now=NOW).get("offset_ok") is True       # 無い = 通常運用
    (reg / t.OFFSET_FILE).write_text("{broken")
    res = t.poll_once(reg, creds(), api_base=api.url, now=NOW + 60)
    assert res.get("offset_unreadable") and not res.get("offset_ok")
    write_ledger(reg)                                                                           # 質問なし = offset を読まない
    assert "offset_ok" not in t.poll_once(reg, creds(), api_base=api.url, now=NOW + 120)


def _block_offset(reg):
    (reg / t.OFFSET_FILE).mkdir()


def test_unwritable_offset_does_not_reprocess_the_same_update(reg, api):
    write_ledger(reg, **{QID: entry(QID), QID2: entry(QID2, message_id=78)})
    uid = api.queue_update(callback_update(int(CHAT), 77, f"{QID}.9f3a1c.1"))
    _block_offset(reg)
    first = t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert first["answered"] == 1 and first.get("offset_unwritable") is True
    second = t.poll_once(reg, creds(), api_base=api.url, now=NOW + 60)
    assert api.calls_of("getUpdates")[1]["offset"] == uid + 1, "書けない offset が 0 のまま同じ update を読み直した"
    assert second["processed"] == 0 and second["rejected"] == 0


def test_unwritable_offset_is_still_throttled(reg):
    write_ledger(reg, **{QID: entry(QID)})
    _block_offset(reg)
    t.store_offset(reg, {"offset": 5, "last_poll_at": NOW, "bot_id": creds().bot_id, "chat_hash": creds().chat_hash}, {})
    assert _cycle_cmds(reg, {QID: entry(QID)}, now=NOW + 3) == [], "間引き (poll_interval) が効かない"
    assert _cycle_cmds(reg, {QID: entry(QID)}, now=NOW + 30), "間隔を過ぎたら走る"


def test_offset_recovery_removes_the_fallback_and_reports_write_ok(reg, api):
    write_ledger(reg, **{QID: entry(QID)})
    _block_offset(reg)
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert (reg / t.OFFSET_FALLBACK_FILE).exists()
    (reg / t.OFFSET_FILE).rmdir()
    res = t.poll_once(reg, creds(), api_base=api.url, now=NOW + 60)
    assert res.get("offset_write_ok") is True and not res.get("offset_unwritable")
    assert not (reg / t.OFFSET_FALLBACK_FILE).exists() and (reg / t.OFFSET_FILE).is_file()


def test_fallback_is_ignored_while_the_main_offset_is_merely_missing(reg):
    (reg / t.OFFSET_FALLBACK_FILE).write_text(json.dumps({"offset": 9, "last_poll_at": NOW}))
    state, _ = t.read_offset_effective(reg)
    assert t.is_missing(state)
