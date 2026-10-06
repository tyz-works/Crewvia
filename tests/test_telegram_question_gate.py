#!/usr/bin/env python3
"""質問に結び付く Bot API 呼び出しは 1 つの関所 (`question_api_call`) を通る (PR #281 の Codex P1・4 巡目。設計 §2-5c)。

欠陥: 後始末 (editMessageReplyMarkup) が**今の認証情報の chat_id** で、台帳に残った前の bot / chat の質問の message_id を編集しにいく。
message_id は chat ごとの連番なので、新しい chat の同じ番号の無関係なメッセージを編集しうる。
ここでは (1) 偽 Bot API 2 つで再現し、(2) 関所の外から質問系の呼び出しをすると落ちる構造テストを置く。

実行: python3 -m pytest tests/test_telegram_question_gate.py -q
"""

import ast
import json
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent / "scripts"))

import lib_telegram as t  # noqa: E402
from telegram_fake_api import FakeBotApi  # noqa: E402

SOURCE = THIS_DIR.parent / "scripts" / "lib_telegram.py"
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


def closed_question(identity, qid="q-0000000a", message_id=4711, **over):
    e = {"nonce": "9f3a1c", "question": "どうする?", "options": ["A", "B"], "message_id": message_id, "status": "expired",
         "created_at": NOW - 7200, "expires_at": NOW - 3600, "closed_at": NOW - 10, "unbutton": True, "forwarded": False}
    if identity is not None:
        e.update({"bot_id": identity.bot_id, "chat_hash": identity.chat_hash})
    e.update(over)
    return {qid: e}


def write_ledger(reg, ledger):
    (reg / t.QUESTIONS_FILE).write_text(json.dumps(ledger))


def read_ledger(reg):
    return json.loads((reg / t.QUESTIONS_FILE).read_text())


# ---------------------------------------------------------------------------
# 再現: bot A / chat A の質問を残したまま bot B / chat B に切り替える
# ---------------------------------------------------------------------------

def test_cleanup_never_edits_a_message_in_the_new_chat_for_the_old_bots_question(reg, apis):
    api_a, api_b = apis
    write_ledger(reg, closed_question(creds_a()))          # 期限切れで後始末待ちの A の質問 (message_id 4711)
    t.poll_once(reg, creds_b(), api_base=api_b.url, now=NOW)
    assert api_b.calls_of("editMessageReplyMarkup") == []  # B の chat の 4711 番を触らない
    assert api_a.calls == []                               # A にも送らない (今の認証情報は B)
    assert read_ledger(reg)["q-0000000a"]["unbutton"] is False   # 即「諦め」(後始末の対象から外れる)


def test_a_question_without_an_identity_is_not_cleaned_up_either(reg, apis):
    _, api_b = apis
    write_ledger(reg, closed_question(None))               # 旧形式 = 束縛の証拠が無い = 別物
    t.poll_once(reg, creds_b(), api_base=api_b.url, now=NOW)
    assert api_b.calls_of("editMessageReplyMarkup") == []
    assert read_ledger(reg)["q-0000000a"]["unbutton"] is False


def test_cleanup_of_the_current_identity_still_works(reg, apis):
    api_a, _ = apis
    write_ledger(reg, closed_question(creds_a()))
    t.poll_once(reg, creds_a(), api_base=api_a.url, now=NOW)
    edits = api_a.calls_of("editMessageReplyMarkup")
    assert len(edits) == 1 and edits[0]["chat_id"] == CHAT_A and edits[0]["message_id"] == 4711
    assert read_ledger(reg)["q-0000000a"]["unbutton"] is False


def test_a_mismatch_does_not_use_the_cleanup_budget_and_does_not_block_a_bound_question(reg, apis):
    """別の bot の質問が先頭にいても、今の identity の質問の後始末は同じサイクルで済む (予算・件数を食わない)。"""
    api_a, api_b = apis
    led = closed_question(creds_a(), "q-00000001", closed_at=NOW - 20)
    led.update(closed_question(creds_b(), "q-00000002", message_id=4800))
    write_ledger(reg, led)
    t.poll_once(reg, creds_b(), api_base=api_b.url, now=NOW)
    edits = api_b.calls_of("editMessageReplyMarkup")
    assert [(e["chat_id"], e["message_id"]) for e in edits] == [(CHAT_B, 4800)]


def test_the_giveup_notice_for_the_old_chats_question_is_not_sent_to_the_new_chat(reg, apis):
    _, api_b = apis
    led = closed_question(creds_a(), status="answered", unbutton=False,
                          answer={"kind": "choice", "index": 0, "label": "A", "update_id": 5, "at": NOW - t.FORWARD_GIVE_UP_SECONDS - 5})
    write_ledger(reg, led)
    t._give_up_forwarding(reg, creds_b(), api_b.url, NOW)
    assert api_b.calls_of("sendMessage") == []
    assert read_ledger(reg)["q-0000000a"]["forwarded"] == "gave_up"    # 印は付く (毎サイクル再判定しない)


# ---------------------------------------------------------------------------
# 関所の単体
# ---------------------------------------------------------------------------

def test_the_gate_fills_chat_and_message_from_the_question_and_credentials(apis):
    api_a, _ = apis
    entry = closed_question(creds_a(), message_id=4711)["q-0000000a"]
    res = t.question_api_call(creds_a(), entry, "editMessageReplyMarkup",
                              {"reply_markup": {"inline_keyboard": []}, "chat_id": "999", "message_id": 1}, api_base=api_a.url)
    assert res.ok
    (sent,) = api_a.calls_of("editMessageReplyMarkup")
    assert sent["chat_id"] == CHAT_A and sent["message_id"] == 4711     # 呼び出し側の値は上書きされる


def test_the_gate_refuses_a_foreign_or_unbound_question_without_calling(apis):
    api_a, _ = apis
    for entry in (closed_question(creds_b())["q-0000000a"], closed_question(None)["q-0000000a"],
                  closed_question(t.Credentials(TOKEN_A, "5550099"))["q-0000000a"]):    # 同じ bot・別の chat も別物
        res = t.question_api_call(creds_a(), entry, "editMessageReplyMarkup", {}, api_base=api_a.url)
        assert not res.ok and res.error == "identity_mismatch"
    assert api_a.calls == []


def test_the_gate_refuses_a_message_method_without_a_message_id(apis):
    api_a, _ = apis
    entry = closed_question(creds_a(), message_id=None)["q-0000000a"]
    assert t.question_api_call(creds_a(), entry, "editMessageReplyMarkup", {}, api_base=api_a.url).error == "no_message_id"
    assert api_a.calls == []


# ---------------------------------------------------------------------------
# 構造: 関所を通らない api_call は落ちる (実際の呼び出し形 `api_call(creds.token, 'method', …)` を走査する)
# ---------------------------------------------------------------------------

#: 関所の外で `api_call` を直接呼んでよい (関数, メソッド)。**質問の message_id を参照しない**ものだけ。
#: * send_message / sendMessage: 質問を**送る**呼び出し (今の認証情報で新規に出す。台帳の message_id は使わない)
#: * _receive_updates / getUpdates: 今の bot の受信
#: * poll_once / answerCallbackQuery: 今の bot の getUpdates が返した callback_query_id への応答 (台帳の質問は参照しない)
GATE = "question_api_call"
ALLOWED_DIRECT = {("send_message", "sendMessage"), ("_receive_updates", "getUpdates"), ("poll_once", "answerCallbackQuery")}


def direct_api_calls(source):
    """→ `[(外側の関数名, メソッド名 or None (文字列定数でない), 行)]`。`api_call(...)` / `x.api_call(...)` の両方を拾う。"""
    tree = ast.parse(source)
    found = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name == "api_call":
                method = None
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    method = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "method" and isinstance(kw.value, ast.Constant):
                        method = kw.value.value
                found.append((self.stack[-1] if self.stack else "<module>", method, node.lineno))
            self.generic_visit(node)

    V().visit(tree)
    return found


def violations(source):
    return [(fn, m, ln) for fn, m, ln in direct_api_calls(source) if fn != GATE and (fn, m) not in ALLOWED_DIRECT]


def test_no_api_call_outside_the_gate_except_the_allowlist():
    assert violations(SOURCE.read_text()) == []


def test_every_allowlisted_call_site_still_exists_so_the_table_cannot_rot():
    seen = {(fn, m) for fn, m, _ in direct_api_calls(SOURCE.read_text())}
    assert ALLOWED_DIRECT <= seen, ALLOWED_DIRECT - seen


def test_the_scanner_has_positive_controls():
    """走査が空振りしていないこと: 関所の外から質問系を呼ぶ実際の形 4 つを検出する。"""
    bad = [
        "def f(creds, e):\n    api_call(creds.token, 'editMessageReplyMarkup', {'chat_id': creds.chat_id})\n",
        "def f(creds, e):\n    t.api_call(creds.token, 'deleteMessage', {})\n",
        "def f(creds, e, m):\n    api_call(creds.token, m, {})\n",                               # メソッド名が定数でない
        "def poll_once(creds, e):\n    api_call(creds.token, 'editMessageReplyMarkup', {})\n",    # 許可表の関数でもメソッドが違う
    ]
    for src in bad:
        assert violations(src), src
    ok = "def question_api_call(creds, e):\n    return api_call(creds.token, 'x', {})\n"
    assert violations(ok) == []


def test_gate_is_the_only_place_that_names_the_question_target_methods():
    """editMessageReplyMarkup 等の文字列は、関所の呼び出し (第 3 引数) 以外で api_call に渡されない。"""
    src = SOURCE.read_text()
    for method in t.QUESTION_TARGET_METHODS:
        assert not [v for v in violations(src) if v[1] == method]
