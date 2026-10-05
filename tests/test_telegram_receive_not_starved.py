#!/usr/bin/env python3
"""受信は後始末に締め出されない (PR #281 の Codex P1・3 巡目)。

poll の 1 サイクルは `timeout 8` のサブプロセス。修正前は `getUpdates` の**前**にボタンを消す `editMessageReplyMarkup` を
質問ごとに最大 5 秒まで待っていたので、Telegram が遅い / 編集が失敗し続ける (消えたメッセージ等) と、後始末だけで 8 秒を使い切り、
`getUpdates` に届かないサイクルが永久に続いた (= open な質問への押下が読まれない)。

不変条件: **どんな後始末の失敗が続いても、open な質問への押下は 1 サイクルで受信され、台帳に記録される。**
設計 §2-3d の表の各セルを押さえる。

実行: python3 -m pytest tests/test_telegram_receive_not_starved.py -q
"""

import ast
import json
import os
import subprocess
import sys
import time
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
OPEN_QID = "q-000000aa"


def creds():
    return t.Credentials(TOKEN, CHAT)


@pytest.fixture
def api():
    with FakeBotApi(token=TOKEN, hang_seconds=5.6) as a:
        yield a


@pytest.fixture
def reg(tmp_path):
    d = tmp_path / "registry" / "daemons"
    d.mkdir(parents=True)
    return d


def entry(status="open", message_id=100, **over):
    ident = creds()
    e = {"nonce": "9f3a1c", "question": "どうする?", "options": ["A", "B"], "message_id": message_id, "status": status,
         "created_at": NOW - 600, "expires_at": NOW + 3600, "forwarded": False,
         "bot_id": ident.bot_id, "chat_hash": ident.chat_hash}
    if status != "open":
        e.update({"closed_at": NOW - 10, "unbutton": True})
    e.update(over)
    return e


def seed(reg, n_unbutton, extra=None):
    ledger = {OPEN_QID: entry("open", message_id=77)}
    for i in range(n_unbutton):
        ledger[f"q-{i + 1:08x}"] = entry("expired", message_id=200 + i)
    ledger.update(extra or {})
    (reg / t.QUESTIONS_FILE).write_text(json.dumps(ledger))
    return ledger


def ledger_of(reg):
    return json.loads((reg / t.QUESTIONS_FILE).read_text())


def press(api, update_id=None):
    return api.queue_update(callback_update(int(CHAT), 77, f"{OPEN_QID}.9f3a1c.1", update_id=update_id))


def order(api):
    return [m for m, _ in api.calls]


# ---------------------------------------------------------------------------
# 再現: dispatcher と同じ `timeout 8` のサブプロセスで、編集が遅い
# ---------------------------------------------------------------------------

def test_slow_button_cleanup_does_not_keep_the_press_from_being_received(reg, api, tmp_path):
    """3 件の「消えないボタン」(編集が 5 秒の timeout まで返らない) があっても、`timeout 8` の poll で押下が記録される。
    修正前: 3 × 5 秒 = 15 秒を getUpdates の前に使い、8 秒で殺され、答えは永久に読まれない。"""
    seed(reg, 3)
    press(api)
    api.method_modes["editMessageReplyMarkup"] = "hang"
    config = tmp_path / "crewvia.yaml"
    config.write_text("telegram:\n  credentials:\n    source: file\n    file: /nonexistent\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CREWVIA_TG", "_CREWVIA_TG"))}
    env.update({"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1", t.CARRIED_TOKEN_VAR: TOKEN, t.CARRIED_CHAT_VAR: CHAT})
    started = time.monotonic()
    proc = subprocess.run(["timeout", "8", sys.executable, str(SCRIPTS / "lib_telegram.py"), "poll",
                           "--registry-dir", str(reg), "--config", str(config), "--api-base", api.url],
                          capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, f"timeout で殺された (elapsed={elapsed:.1f}s)"
    assert json.loads(proc.stdout)["answered"] == 1
    assert ledger_of(reg)[OPEN_QID]["status"] == "answered"
    assert order(api)[0] == "getUpdates", "受信が後始末より先"
    assert elapsed < 8


# ---------------------------------------------------------------------------
# 不変条件: どんな後始末の失敗でも 1 サイクルで受信される
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["editMessageReplyMarkup", "sendMessage", "answerCallbackQuery"])
@pytest.mark.parametrize("mode", ["http500", "http400", "ratelimit"])
def test_no_cleanup_failure_keeps_a_press_from_being_received_within_one_cycle(reg, api, method, mode):
    """後始末に使う全メソッド × 失敗の種類。1 サイクルで押下が `answered`、offset が進む。"""
    stale = {"q-000000bb": entry("answered", message_id=300, forwarded=False,
                                 answer={"kind": "choice", "index": 0, "at": NOW - t.FORWARD_GIVE_UP_SECONDS - 10, "update_id": 1},
                                 closed_at=NOW - t.FORWARD_GIVE_UP_SECONDS)}
    seed(reg, 3, stale)
    uid = press(api)
    api.method_modes[method] = mode
    res = t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert res["answered"] == 1 and ledger_of(reg)[OPEN_QID]["status"] == "answered"
    assert json.loads((reg / t.OFFSET_FILE).read_text())["offset"] == uid + 1
    assert order(api).index("getUpdates") == 0


def test_receive_comes_before_every_other_network_call(reg, api):
    """順序の表: getUpdates → (記録) → answerCallbackQuery → editMessageReplyMarkup → sendMessage。"""
    stale = {"q-000000bb": entry("answered", message_id=300, forwarded=False,
                                 answer={"kind": "choice", "index": 0, "at": NOW - t.FORWARD_GIVE_UP_SECONDS - 10, "update_id": 1},
                                 closed_at=NOW - t.FORWARD_GIVE_UP_SECONDS)}
    seed(reg, 1, stale)
    press(api)
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    methods = order(api)
    assert methods[0] == "getUpdates"
    assert methods.index("answerCallbackQuery") < methods.index("editMessageReplyMarkup") < methods.index("sendMessage")


def test_cleanup_runs_even_when_no_question_is_open(reg, api):
    """open が 0 件でも (受信は通信しない) ボタンを消す後始末は走る — 順序を入れ替えて後始末を落としていないこと。"""
    (reg / t.QUESTIONS_FILE).write_text(json.dumps({"q-00000001": entry("expired", message_id=200)}))
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert order(api) == ["editMessageReplyMarkup"]
    assert ledger_of(reg)["q-00000001"]["unbutton"] is False


# ---------------------------------------------------------------------------
# 後始末の予算: 件数・時間・諦め
# ---------------------------------------------------------------------------

def test_cleanup_per_cycle_is_capped_by_count(reg, api):
    seed(reg, 7)
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)
    assert len(api.calls_of("editMessageReplyMarkup")) == t.CLEANUP_MAX_PER_CYCLE
    left = [q for q, e in ledger_of(reg).items() if e.get("unbutton")]
    assert len(left) == 7 - t.CLEANUP_MAX_PER_CYCLE, "残りは次のサイクルへ (消えない・失われない)"


def test_cleanup_stops_when_the_time_budget_is_spent(reg, api):
    """持ち時間が残っていなければ後始末の通信を 1 回もしない。受信は予算に関係なく先に済む。"""
    seed(reg, 3)
    press(api)
    spent = t._Budget(seconds=0)
    res = t.poll_once(reg, creds(), api_base=api.url, now=NOW, budget=spent)
    assert res["answered"] == 1
    assert order(api) == ["getUpdates"]
    assert sum(1 for e in ledger_of(reg).values() if e.get("unbutton")) == 3


def test_budget_timeout_is_capped_by_what_remains():
    clock = {"v": 100.0}
    b = t._Budget(seconds=6.0, clock=lambda: clock["v"])
    assert b.timeout() == t.API_TIMEOUT_SECONDS
    clock["v"] += 4.5
    assert b.timeout() == pytest.approx(1.5)
    clock["v"] += 1.0
    assert b.timeout() is None, "残りが BUDGET_MIN_REMAINING_SECONDS 未満なら次のサイクルへ"


@pytest.mark.parametrize("mode", ["http400", "http403", "http404"])
def test_permanent_edit_failures_are_dropped_at_once(reg, api, mode):
    """Bot API の 400 / 403 / 404 (編集できない・消えたメッセージ・bot が外された) は待っても直らない → pending から外す。"""
    (reg / t.QUESTIONS_FILE).write_text(json.dumps({"q-00000001": entry("expired", message_id=200)}))
    api.method_modes["editMessageReplyMarkup"] = "http400"
    if mode != "http400":                                   # fake は 400 しか返さないので、他のコードは api_call の戻りを差し替えて確かめる
        orig = t.api_call
        code = int(mode[4:])
        t.api_call = lambda *a, **k: t.ApiResult(False, error=f"api_{code}")
        try:
            t._unbutton_pending(reg, creds(), api.url, NOW)
        finally:
            t.api_call = orig
    else:
        t._unbutton_pending(reg, creds(), api.url, NOW)
    assert ledger_of(reg)["q-00000001"]["unbutton"] is False


@pytest.mark.parametrize("mode", ["http500", "ratelimit"])
def test_transient_edit_failures_give_up_after_a_bounded_number_of_cycles(reg, api, mode):
    """一時的な失敗は回数で諦める (ratelimit は回数に数えず、閉じてからの時間で諦める)。無限に pending に残らない。"""
    (reg / t.QUESTIONS_FILE).write_text(json.dumps({"q-00000001": entry("expired", message_id=200)}))
    api.method_modes["editMessageReplyMarkup"] = mode
    cycles = 0
    while ledger_of(reg)["q-00000001"].get("unbutton") and cycles < t.UNBUTTON_MAX_TRIES + 2:
        t._unbutton_pending(reg, creds(), api.url, NOW + cycles)
        cycles += 1
        if mode == "ratelimit":
            break
    if mode == "http500":
        assert ledger_of(reg)["q-00000001"]["unbutton"] is False
        assert cycles == t.UNBUTTON_MAX_TRIES + 1, "5 回試して 6 回目の入口で諦める"
        assert len(api.calls_of("editMessageReplyMarkup")) == t.UNBUTTON_MAX_TRIES
    else:
        assert ledger_of(reg)["q-00000001"]["unbutton"] is True and "unbutton_tries" not in ledger_of(reg)["q-00000001"]
        t._unbutton_pending(reg, creds(), api.url, NOW + t.UNBUTTON_GIVE_UP_SECONDS + 1)
        assert ledger_of(reg)["q-00000001"]["unbutton"] is False, "閉じてから 1 時間で諦める"


def test_least_tried_entries_go_first_so_one_stuck_entry_cannot_starve_the_rest(reg, api):
    stuck = entry("expired", message_id=200, unbutton_tries=4)
    fresh = entry("expired", message_id=201)
    (reg / t.QUESTIONS_FILE).write_text(json.dumps({"q-00000001": stuck, "q-00000002": fresh}))
    t._unbutton_pending(reg, creds(), api.url, NOW, limit=1)
    assert [p["message_id"] for p in api.calls_of("editMessageReplyMarkup")] == [201]


def test_unbutton_without_a_message_id_is_dropped_without_a_call(reg, api):
    (reg / t.QUESTIONS_FILE).write_text(json.dumps({"q-00000001": entry("expired", message_id=None)}))
    t._unbutton_pending(reg, creds(), api.url, NOW)
    assert api.calls == [] and ledger_of(reg)["q-00000001"]["unbutton"] is False


def test_giving_up_a_forward_is_not_marked_when_there_is_no_time_to_tell_the_user(reg, api):
    """印 → 通知の順なので、時間が足りないときは印を付けない (印だけ付いて「届かなかった」の通知が出ない穴を作らない)。"""
    old = entry("answered", message_id=300, forwarded=False, closed_at=NOW - t.FORWARD_GIVE_UP_SECONDS,
                answer={"kind": "choice", "index": 0, "at": NOW - t.FORWARD_GIVE_UP_SECONDS - 10, "update_id": 1})
    old.pop("unbutton", None)
    (reg / t.QUESTIONS_FILE).write_text(json.dumps({"q-000000bb": old}))
    t._give_up_forwarding(reg, creds(), api.url, NOW, budget=t._Budget(seconds=0))
    assert ledger_of(reg)["q-000000bb"]["forwarded"] is False and api.calls == []
    t._give_up_forwarding(reg, creds(), api.url, NOW, budget=t._Budget())
    assert ledger_of(reg)["q-000000bb"]["forwarded"] == "gave_up" and len(api.calls_of("sendMessage")) == 1


def test_a_button_that_could_not_be_removed_is_still_refused_by_the_ledger(reg, api):
    """諦めても安全な理由: 「消えないボタン」を押しても、閉じた質問は台帳の照合で拒否される (転送されない)。"""
    (reg / t.QUESTIONS_FILE).write_text(json.dumps({OPEN_QID: entry("expired", message_id=77)}))
    api.method_modes["editMessageReplyMarkup"] = "http400"
    t.poll_once(reg, creds(), api_base=api.url, now=NOW)       # 諦める
    assert ledger_of(reg)[OPEN_QID]["unbutton"] is False
    (reg / t.QUESTIONS_FILE).write_text(json.dumps({**ledger_of(reg), "q-000000cc": entry("open", message_id=78)}))
    api.queue_update(callback_update(int(CHAT), 77, f"{OPEN_QID}.9f3a1c.0", callback_id="cb-late"))
    res = t.poll_once(reg, creds(), api_base=api.url, now=NOW + 1)
    assert res["answered"] == 0 and res["rejected"] == 1
    assert ledger_of(reg)[OPEN_QID]["status"] == "expired" and "answer" not in ledger_of(reg)[OPEN_QID]


# ---------------------------------------------------------------------------
# 構造: 受信の前にネットワークを呼ばない・poll の経路の通信はすべて持ち時間を取る
# ---------------------------------------------------------------------------

def _functions():
    tree = ast.parse((SCRIPTS / "lib_telegram.py").read_text())
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _api_calls(fn):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "api_call"]


@pytest.mark.parametrize("name", ["poll_once", "_unbutton_pending", "_give_up_forwarding"])
def test_every_network_call_on_the_poll_path_takes_a_time_budget(name):
    calls = _api_calls(_functions()[name])
    assert calls, name
    for c in calls:
        assert any(k.arg == "timeout" for k in c.keywords), f"{name}:{c.lineno} の api_call に timeout (持ち時間) が無い"


def test_poll_once_receives_before_any_cleanup_call():
    fn = _functions()["poll_once"]
    receive = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_receive_updates"]
    later = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") in ("api_call", "_unbutton_pending", "_give_up_forwarding")]
    assert receive and later and min(receive) < min(later)
    receive_fn = _functions()["_receive_updates"]
    assert [c.args[1].value for c in _api_calls(receive_fn)][:1] == ["getUpdates"]
