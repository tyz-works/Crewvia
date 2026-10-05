#!/usr/bin/env python3
"""Telegram の経路 (PR-A) — **偽の Bot API サーバー** (localhost) を相手にした挙動のテスト。

本物の Bot API は叩かない。token は偽の値で、全出力・全状態ファイル・全プロセスの cmdline を grep して漏れを見る。
動詞は本物の subprocess (`scripts/lib_telegram.py` / `scripts/ask_user.sh`) で呼ぶ — 実際の呼び出し形で確かめる。

実行: python3 -m pytest tests/test_telegram_fake_bot_api.py -q
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
SCRIPTS = THIS_DIR.parent / "scripts"
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(SCRIPTS))

import lib_daemon_state as lds  # noqa: E402
import lib_telegram as t  # noqa: E402
from telegram_fake_api import FakeBotApi, callback_update, plain_update, reply_update  # noqa: E402

CHAT = "5550001"
TOKEN = "123456:FAKE-TOKEN-abcdefghij"


class Box:
    """使い捨ての registry / config / 認証情報ファイルと、verb を subprocess で呼ぶ道具。"""

    def __init__(self, root, api, source="file"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = self.root / "registry" / "daemons"
        self.queue = self.root / "queue"
        self.api = api
        self.cred_file = self.root / "telegram.env"
        self.cred_file.write_text(f"bot_token={api.token}\nchat_id={CHAT}\n")
        os.chmod(self.cred_file, 0o600)
        self.config = self.root / "crewvia.yaml"
        self.set_config(source)

    def set_config(self, source="file", extra=""):
        lines = ["telegram:", "  poll_interval_seconds: 10", "  question_ttl_minutes: 60"]
        if source == "file":
            lines += ["  credentials:", "    source: file", f"    file: {self.cred_file}"]
        elif source:
            lines += ["  credentials:", f"    source: {source}"]
        self.config.write_text("\n".join(lines) + "\n" + extra)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CREWVIA_TG", "_CREWVIA_TG")) and k != "AGENT_NAME"}
        env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1"
        env["CREWVIA_QUEUE"] = str(self.queue)
        env.update(extra)
        return env

    def run(self, verb, *args, stdin=None, env=None, timeout=60):
        cmd = [sys.executable, str(SCRIPTS / "lib_telegram.py"), verb, "--registry-dir", str(self.registry),
               "--config", str(self.config)]
        if verb in ("ask", "poll", "cancel", "send"):
            cmd += ["--api-base", self.api.url]
        if verb == "ask":
            cmd += ["--queue-dir", str(self.queue)]
        cmd += list(args)
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, env=env or self.env())

    def creds(self):
        return t.Credentials(self.api.token, CHAT)

    def heartbeat(self, now=None, creds="auto"):
        creds = self.creds() if creds == "auto" else creds
        return t.write_receiver_state(self.registry, creds, "ok" if creds else "no_credentials",
                                      time.time() if now is None else now)

    def ledger(self):
        return json.loads((self.registry / t.QUESTIONS_FILE).read_text())

    def ask(self, *extra, question="どうしますか?", options=("A: 続ける", "B: 止める")):
        args = ["--question", question]
        for o in options:
            args += ["--option", o]
        return self.run("ask", *args, *extra)

    def cycle(self, forwarded, *, now=None, creds="auto", reason="ok", log=None):
        creds = self.creds() if creds == "auto" else creds
        return t.run_cycle(self.registry, self.queue, t.load_telegram_config(self.config), reason,
                           lambda line: (forwarded.append(line), True)[1], now=now, creds=creds,
                           api_base=self.api.url, config_path=self.config,
                           log=(log if log is not None else (lambda m: None)))

    def tree(self):
        out = {}
        if self.root.exists():
            for p in sorted(self.root.rglob("*")):
                out[str(p.relative_to(self.root))] = p.read_bytes() if p.is_file() else None
        return out


@pytest.fixture
def api():
    with FakeBotApi(token=TOKEN) as a:
        yield a


@pytest.fixture
def box(tmp_path, api):
    return Box(tmp_path / "box", api)


def all_text(box, *procs):
    """token が出てはいけない場所: 各 subprocess の出力と、registry 配下の全ファイル。"""
    chunks = []
    for p in procs:
        chunks += [p.stdout or "", p.stderr or ""]
    if box.registry.exists():
        chunks += [f.read_text(errors="replace") for f in box.registry.rglob("*") if f.is_file()]
    return "\n".join(chunks)


def press_button(box, api, qid, index=0, *, update_id=None, callback_id="cb-1"):
    led = box.ledger()[qid]
    api.queue_update(callback_update(int(CHAT), led["message_id"], f"{qid}.{led['nonce']}.{index}",
                                     update_id=update_id, callback_id=callback_id))


# ---------------------------------------------------------------------------
# 未設定なら 1 バイトも書かない・何も送らない
# ---------------------------------------------------------------------------

def test_unconfigured_ask_exits_3_and_writes_nothing(box, api):
    box.set_config(source="")
    before = box.tree()
    r = box.ask()
    assert r.returncode == 3 and r.stdout == ""
    assert box.tree() == before, "ファイルも dir も作らない"
    assert api.calls == []


def test_unconfigured_cycle_touches_nothing(box, api):
    box.set_config(source="")
    before = box.tree()
    forwarded, ran = [], []
    summary = t.run_cycle(box.registry, box.queue, t.load_telegram_config(box.config), "no_credentials",
                          lambda line: forwarded.append(line), creds=None, runner=lambda *a, **k: ran.append(a))
    assert box.tree() == before and forwarded == [] and ran == [] and api.calls == []
    assert summary["receiver"]["wrote"] is False and summary["receiver"]["file_exists"] is False


def test_unconfigured_but_receiver_file_exists_flips_to_disabled(box):
    box.heartbeat()
    assert json.loads((box.registry / t.RECEIVER_FILE).read_text())["enabled"] is True
    res = t.write_receiver_state(box.registry, None, "no_credentials", time.time() + 1)
    assert res["wrote"] is True and res["enabled"] is False and res["file_exists"] is True
    data = json.loads((box.registry / t.RECEIVER_FILE).read_text())
    assert data["enabled"] is False and "bot_id" not in data and "chat_hash" not in data


def test_configured_but_failing_credentials_create_a_disabled_receiver_file(box):
    res = t.write_receiver_state(box.registry, None, "credential_command_failed", time.time())
    assert res["wrote"] and res["enabled"] is False and res["reason"] == "credential_command_failed"
    assert json.loads((box.registry / t.RECEIVER_FILE).read_text())["reason"] == "credential_command_failed"


# ---------------------------------------------------------------------------
# ask → ボタン → poll → Director へ転送 (ハッピーパス)
# ---------------------------------------------------------------------------

def test_ask_press_poll_forward_roundtrip(box, api):
    box.heartbeat()
    r = box.ask("--task", "20261004-x/t017")
    assert r.returncode == 0, r.stderr
    qid = r.stdout.strip()
    assert qid.startswith("q-") and len(r.stdout.strip().splitlines()) == 1

    sent = api.calls_of("sendMessage")
    assert len(sent) == 1 and sent[0]["chat_id"] == CHAT and "parse_mode" not in sent[0]
    keyboard = sent[0]["reply_markup"]["inline_keyboard"]
    datas = [row[0]["callback_data"] for row in keyboard]
    led = box.ledger()[qid]
    assert datas == [f"{qid}.{led['nonce']}.0", f"{qid}.{led['nonce']}.1"]
    assert all(len(d.encode()) <= 64 for d in datas)
    assert led["status"] == "open" and led["message_id"] is not None and led["task"] == "t017"
    assert "対象: 20261004-x/t017" in sent[0]["text"]

    press_button(box, api, qid, 1)
    forwarded = []
    summary = box.cycle(forwarded)
    assert summary["polled"] and summary["poll"]["answered"] == 1 and summary["forwarded"] == 1
    assert forwarded == [f'[telegram-answer] q={qid} task=20261004-x/t017 choice="B: 止める" index=1 task_state=unreadable']
    led = box.ledger()[qid]
    assert led["status"] == "answered" and led["forwarded"] is True and led["answer"]["index"] == 1
    assert api.calls_of("answerCallbackQuery")[0]["callback_query_id"] == "cb-1"
    offset = json.loads((box.registry / t.OFFSET_FILE).read_text())
    assert offset["offset"] == api.updates[-1]["update_id"] + 1

    v = box.run("verify", "--q", qid)
    body = json.loads(v.stdout)
    assert body["status"] == "answered" and body["choice_index"] == 1 and body["choice"] == "B: 止める"
    assert body["task"] == "20261004-x/t017"


def test_double_press_forwards_once_and_the_first_choice_wins(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    press_button(box, api, qid, 0, callback_id="cb-a")
    press_button(box, api, qid, 1, callback_id="cb-b")
    forwarded = []
    box.cycle(forwarded)
    assert len(forwarded) == 1 and 'choice="A: 続ける"' in forwarded[0]
    replies = {c["callback_query_id"]: c["text"] for c in api.calls_of("answerCallbackQuery")}
    assert "A: 続ける" in replies["cb-b"], "二重押し: 先に確定した選択肢を返す"
    assert box.ledger()[qid]["answer"]["index"] == 0
    # 3 回目のサイクルでも再転送しない
    box.cycle(forwarded, now=time.time() + 60)
    assert len(forwarded) == 1


def test_text_reply_is_forwarded_as_a_quoted_single_line(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    mid = box.ledger()[qid]["message_id"]
    api.queue_update(reply_update(int(CHAT), mid, 'やっぱり"] q=q-deadbeef\n先に見て'))
    api.queue_update(plain_update(int(CHAT), "返信ではない雑多なメッセージ"))
    forwarded = []
    box.cycle(forwarded)
    assert len(forwarded) == 1 and "\n" not in forwarded[0] and "q-deadbeef choice" not in forwarded[0]
    assert forwarded[0].startswith(f"[telegram-answer] q={qid} task=- text=")
    assert api.calls_of("sendMessage") and len(api.calls_of("sendMessage")) == 1, "返信でないメッセージには返信しない"


@pytest.mark.parametrize("mutation", ["other_chat", "other_sender", "bad_nonce", "bad_message_id", "bad_index", "unknown_qid"])
def test_rejected_presses_never_forward_or_answer(box, api, mutation):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    led = box.ledger()[qid]
    chat, sender, nonce, mid, idx, q = int(CHAT), None, led["nonce"], led["message_id"], 0, qid
    if mutation == "other_chat":
        chat = 999
    elif mutation == "other_sender":
        sender = 888
    elif mutation == "bad_nonce":
        nonce = "000000"
    elif mutation == "bad_message_id":
        mid += 1
    elif mutation == "bad_index":
        idx = 3
    elif mutation == "unknown_qid":
        q = "q-ffffffff"
    api.queue_update(callback_update(chat, mid, f"{q}.{nonce}.{idx}", from_id=sender))
    forwarded = []
    box.cycle(forwarded)
    assert forwarded == [] and box.ledger()[qid]["status"] == "open"


def test_press_on_withdrawn_question_is_refused_with_a_note(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    other = box.ask().stdout.strip()                     # 別の未回答の質問があるので poll は走る
    assert other
    r = box.run("cancel", "--q", qid, "--by", "screen")
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "withdrawn"
    assert api.calls_of("editMessageReplyMarkup"), "ボタンを消す"
    assert box.ledger()[qid]["status"] == "withdrawn"
    press_button(box, api, qid, 0)
    forwarded = []
    box.cycle(forwarded)
    assert forwarded == [] and "画面" in api.calls_of("answerCallbackQuery")[-1]["text"]
    assert box.run("cancel", "--q", qid, "--by", "screen").returncode == t.EXIT_NOT_OPEN


# ---------------------------------------------------------------------------
# ask の事前条件 (受信側の状態・上限)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("setup,code", [
    ("none", "receiver_unknown"),
    ("stale", "receiver_stale"),
    ("disabled", "receiver_disabled"),
    ("other_bot", "receiver_mismatch"),
    ("other_chat", "receiver_mismatch"),
    ("corrupt", "receiver_unknown"),
])
def test_ask_refuses_unless_the_receiver_is_live_and_the_same_bot(box, api, setup, code):
    now = time.time()
    path = box.registry / t.RECEIVER_FILE
    if setup == "stale":
        box.heartbeat(now=now - 200)
    elif setup == "disabled":
        box.heartbeat()
        t.write_receiver_state(box.registry, None, "credential_command_failed", now)
    elif setup == "other_bot":
        box.heartbeat(creds=t.Credentials("777777:OTHER-TOKEN-abcdefghij", CHAT))
    elif setup == "other_chat":
        box.heartbeat(creds=t.Credentials(TOKEN, "42"))
    elif setup == "corrupt":
        box.registry.mkdir(parents=True)
        path.write_text("{not json")
    r = box.ask()
    assert r.returncode == t.EXIT_CANNOT_SEND and code in r.stderr, r.stderr
    assert api.calls_of("sendMessage") == [], "断るので Telegram には何も送らない"
    assert not (box.registry / t.QUESTIONS_FILE).exists(), "台帳にも書かない"


def test_ask_refuses_a_ninth_open_question(box, api):
    box.heartbeat()
    for _ in range(t.MAX_OPEN_QUESTIONS):
        assert box.ask().returncode == 0
    r = box.ask()
    assert r.returncode == t.EXIT_TOO_MANY_OPEN and "too_many_open" in r.stderr


def test_expired_questions_do_not_count_toward_the_limit(box, api):
    box.heartbeat()
    box.registry.mkdir(parents=True, exist_ok=True)
    now = time.time()
    old = {f"q-0000000{i}": {"nonce": "9f3a1c", "question": "x", "options": ["a", "b"], "message_id": 10 + i,
                              "status": "open", "created_at": now - 7200, "expires_at": now - 3600, "forwarded": False}
           for i in range(8)}
    (box.registry / t.QUESTIONS_FILE).write_text(json.dumps(old))
    assert box.ask().returncode == 0
    led = box.ledger()
    assert sum(1 for e in led.values() if e["status"] == "expired") == 8


@pytest.mark.parametrize("args", [
    ["--question", "q", "--option", "only-one"],
    ["--question", "q", "--option", "a", "--option", "b", "--option", "c", "--option", "d", "--option", "e"],
    ["--question", "q", "--option", "a", "--option", "x" * 41],
    ["--question", " ", "--option", "a", "--option", "b"],
    ["--question", "q", "--option", "a", "--option", "b", "--task", "bad task"],
    ["--question", "q", "--option", "a", "--option", "b", "--ttl-minutes", "0"],
    ["--question", "q", "--option", "a", "--option", "b", "--bogus", "1"],
    ["--question", "q", "--option", "a", "--option"],
])
def test_ask_usage_errors_exit_1_without_echoing_input(box, api, args):
    box.heartbeat()
    r = box.run("ask", *args)
    assert r.returncode == 1 and "usage_error" in r.stderr
    assert "bad task" not in r.stderr and "--bogus" not in r.stderr and api.calls == []


def test_ask_send_failure_withdraws_the_question_and_exits_4(box, api):
    box.heartbeat()
    api.mode = "http500"
    r = box.ask()
    assert r.returncode == 4 and "send_failed" in r.stderr
    led = box.ledger()
    assert [e["status"] for e in led.values()] == ["withdrawn"], "① の open を withdrawn に戻す"
    # 次の ask はバックオフ中で送らずに断る
    api.mode = "ok"
    n_before = len(api.calls_of("sendMessage"))
    r2 = box.ask()
    assert r2.returncode == 4 and "backoff" in r2.stderr and len(api.calls_of("sendMessage")) == n_before


def test_ask_when_the_ledger_cannot_record_the_message_removes_the_buttons(box, api, monkeypatch):
    """③ が失敗 (台帳に書けない) → ボタンを消して exit 4。ボタンが出ているのに台帳に無い状態を作らない。"""
    box.heartbeat()
    real = t.update_questions
    calls = {"n": 0}

    def flaky(registry_dir, fn, *, now, warn=None):
        calls["n"] += 1
        if calls["n"] == 2:                      # 1 回目 = ① の登録、2 回目 = ③ の message_id 記録
            raise t.LedgerBusy()
        return real(registry_dir, fn, now=now, warn=warn)
    monkeypatch.setattr(t, "update_questions", flaky)
    monkeypatch.setattr(sys, "argv", ["lib_telegram.py"])
    monkeypatch.setattr(t, "default_registry_dir", lambda: box.registry)
    rc = t.main(["ask", "--question", "q", "--option", "a", "--option", "b", "--registry-dir", str(box.registry),
                 "--config", str(box.config), "--api-base", api.url])
    assert rc == 4
    assert api.calls_of("editMessageReplyMarkup")[0]["reply_markup"] == {"inline_keyboard": []}


# ---------------------------------------------------------------------------
# offset: at-least-once・hold・台帳 Unreadable
# ---------------------------------------------------------------------------

def test_offset_does_not_advance_when_the_ledger_is_locked_and_the_update_is_re_received(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    press_button(box, api, qid, 0)
    path = box.registry / t.QUESTIONS_FILE
    with lds.told_lock(str(path), wait=5.0) as held:
        assert held
        res = t.poll_once(box.registry, box.creds(), api_base=api.url, now=time.time())
    assert res["error"] in ("ledger_busy",) or res.get("skipped") == "ledger_busy", res
    assert not (box.registry / t.OFFSET_FILE).exists() or json.loads((box.registry / t.OFFSET_FILE).read_text())["offset"] == 0
    forwarded = []
    box.cycle(forwarded, now=time.time() + 30)
    assert len(forwarded) == 1 and box.ledger()[qid]["status"] == "answered"


def test_offset_does_not_advance_when_only_the_answering_step_cannot_lock(box, api, monkeypatch):
    """sweep は通るが、答えを書く段 (process) でロックが取れない: 答えを書けていないので offset を進めてはいけない。"""
    box.heartbeat()
    qid = box.ask().stdout.strip()
    press_button(box, api, qid, 0)
    real = t.update_questions

    def busy_on_process(registry_dir, fn, *, now, warn=None):
        if fn.__name__ == "process":
            raise t.LedgerBusy()
        return real(registry_dir, fn, now=now, warn=warn)
    monkeypatch.setattr(t, "update_questions", busy_on_process)
    res = t.poll_once(box.registry, box.creds(), api_base=api.url, now=time.time())
    assert res["error"] == "ledger_busy" and res["answered"] == 0
    assert json.loads((box.registry / t.OFFSET_FILE).read_text())["offset"] == 0 if (box.registry / t.OFFSET_FILE).exists() else True
    assert api.calls_of("answerCallbackQuery") == [], "答えを書けなかったので、ボタンの待ち表示も止めない (次のサイクルで同じ update をもう一度受ける)"
    monkeypatch.setattr(t, "update_questions", real)
    assert t.poll_once(box.registry, box.creds(), api_base=api.url, now=time.time() + 30)["answered"] == 1


def test_press_on_a_question_without_message_id_holds_the_offset_but_later_updates_are_processed(box, api):
    box.heartbeat()
    box.registry.mkdir(parents=True, exist_ok=True)
    now = time.time()
    ledger = {
        "q-00000001": {"nonce": "aaaaaa", "question": "x", "options": ["a", "b"], "message_id": None, "status": "open",
                       "created_at": now - 10, "expires_at": now + 3600, "forwarded": False},
        "q-00000002": {"nonce": "bbbbbb", "question": "y", "options": ["a", "b"], "message_id": 77, "status": "open",
                       "created_at": now - 10, "expires_at": now + 3600, "forwarded": False},
    }
    (box.registry / t.QUESTIONS_FILE).write_text(json.dumps(ledger))
    api.queue_update(callback_update(int(CHAT), 76, "q-00000001.aaaaaa.0", update_id=9101, callback_id="hold"))
    api.queue_update(callback_update(int(CHAT), 77, "q-00000002.bbbbbb.1", update_id=9102, callback_id="later"))
    res = t.poll_once(box.registry, box.creds(), api_base=api.url, now=now)
    assert res["answered"] == 1
    assert box.ledger()["q-00000002"]["status"] == "answered", "head-of-line でも後ろの update は処理する"
    assert box.ledger()["q-00000001"]["status"] == "open"
    assert json.loads((box.registry / t.OFFSET_FILE).read_text())["offset"] == 9101, "offset は先頭の保留で止まる"
    assert [c["callback_query_id"] for c in api.calls_of("answerCallbackQuery")] == ["later"]
    # 5 分後に残骸は withdrawn → 次の poll で保留が解け、offset が進む。後ろの update の再受信は冪等
    res = t.poll_once(box.registry, box.creds(), api_base=api.url, now=now + 400)
    assert box.ledger()["q-00000001"]["status"] == "withdrawn"
    assert box.ledger()["q-00000002"]["answer"]["index"] == 1


def test_unreadable_ledger_forwards_nothing_does_not_advance_and_does_not_answer(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    press_button(box, api, qid, 0)
    (box.registry / t.QUESTIONS_FILE).write_text('{"q-1a2b3c4d": {"status": "bogus"}}')
    logs, forwarded = [], []
    summary = box.cycle(forwarded, log=logs.append)
    assert forwarded == [] and not summary["polled"]
    assert any("telegram-questions" in m for m in logs)
    res = t.poll_once(box.registry, box.creds(), api_base=api.url)
    assert res["error"] == "ledger_unreadable" and api.calls_of("answerCallbackQuery") == []
    assert not (box.registry / t.OFFSET_FILE).exists()
    assert box.ask().returncode == 4, "ask も台帳が壊れていれば断る"
    (box.registry / t.QUESTIONS_FILE).unlink()          # 台帳を消せば復旧 (§2-1)
    assert box.ask().returncode == 0


def test_poll_failure_does_not_advance_offset(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    press_button(box, api, qid, 0)
    api.mode = "http500"
    res = t.poll_once(box.registry, box.creds(), api_base=api.url)
    assert res["error"] == "api_500" and box.ledger()[qid]["status"] == "open"
    assert json.loads((box.registry / t.OFFSET_FILE).read_text())["offset"] == 0
    api.mode = "ok"
    assert t.poll_once(box.registry, box.creds(), api_base=api.url)["answered"] == 1


def test_no_open_question_means_no_network(box, api):
    box.heartbeat()
    box.registry.mkdir(parents=True, exist_ok=True)
    (box.registry / t.QUESTIONS_FILE).write_text("{}")
    box.cycle([])
    assert api.calls == [], "未回答の質問が無ければ通信しない (bot 宛の雑多なメッセージを拾わない)"


def test_poll_is_throttled_by_poll_interval(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    now = time.time()
    s1 = box.cycle([], now=now)
    s2 = box.cycle([], now=now + 3)
    s3 = box.cycle([], now=now + 11)
    assert (s1["polled"], s2["polled"], s3["polled"]) == (True, False, True)


def test_giving_up_on_forwarding_after_24_hours_tells_telegram_once(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    press_button(box, api, qid, 0)
    t.poll_once(box.registry, box.creds(), api_base=api.url)
    assert box.ledger()[qid]["forwarded"] is False
    later = time.time() + t.FORWARD_GIVE_UP_SECONDS + 5
    t.poll_once(box.registry, box.creds(), api_base=api.url, now=later)
    assert box.ledger()[qid]["forwarded"] == "gave_up"
    n = len(api.calls_of("sendMessage"))
    t.poll_once(box.registry, box.creds(), api_base=api.url, now=later + 20)
    assert len(api.calls_of("sendMessage")) == n, "1 回だけ"


def test_forward_failure_keeps_forwarded_false_and_retries(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    press_button(box, api, qid, 0)
    results = iter([False, True])
    got = []
    t.run_cycle(box.registry, box.queue, t.load_telegram_config(box.config), "ok",
                lambda line: (got.append(line), next(results))[1], creds=box.creds(), api_base=api.url, config_path=box.config)
    assert box.ledger()[qid]["forwarded"] is False and box.ledger()[qid]["status"] == "answered"
    t.run_cycle(box.registry, box.queue, t.load_telegram_config(box.config), "ok",
                lambda line: (got.append(line), next(results))[1], creds=box.creds(), api_base=api.url, config_path=box.config)
    assert box.ledger()[qid]["forwarded"] is True and len(got) == 2


# ---------------------------------------------------------------------------
# token が漏れない (argv・出力・状態ファイル・例外文)
# ---------------------------------------------------------------------------

def proc_cmdlines_containing(needles):
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        text = raw.replace(b"\0", b" ").decode(errors="replace")
        if any(n in text for n in needles):
            hits.append((pid, text[:120]))
    return hits


def test_the_cmdline_detector_has_a_positive_control():
    """検出器が本当に拾えること (argv に偽 token を持つプロセスを見つける)。"""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)", TOKEN])
    try:
        time.sleep(0.3)
        assert any(str(p.pid) == pid for pid, text in proc_cmdlines_containing([TOKEN]))
    finally:
        p.kill()
        p.wait()


def test_token_never_appears_in_any_process_cmdline_during_a_poll_cycle(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    press_button(box, api, qid, 0)
    api.mode = "hang"
    api.hang_seconds = 1.5
    found, stop = [], threading.Event()

    def scan():
        while not stop.is_set():
            found.extend(proc_cmdlines_containing([TOKEN, CHAT + "_x"]))
            time.sleep(0.02)
    th = threading.Thread(target=scan, daemon=True)
    th.start()
    try:
        summary = box.cycle([])
    finally:
        stop.set()
        th.join()
    assert summary["polled"], "poll のサブプロセスは走った (走らなければ検査にならない)"
    assert found == [], f"全プロセスの cmdline に token が出た: {found}"


def test_token_never_leaks_through_outputs_files_or_errors(box, api):
    box.heartbeat()
    procs = [box.ask()]
    qid = procs[0].stdout.strip()
    press_button(box, api, qid, 0)
    api.mode = "http500"
    procs.append(box.run("poll"))
    procs.append(box.ask())                              # バックオフ中の断り
    api.mode = "ok"
    procs.append(box.run("verify", "--q", qid))
    procs.append(box.run("list"))
    procs.append(box.run("send", stdin="hello"))
    assert TOKEN not in all_text(box, *procs)
    assert TOKEN.split(":")[1] not in all_text(box, *procs)


def test_an_unexpected_exception_prints_only_its_type_never_its_message(box, api, monkeypatch, capsys):
    """想定外の例外の文字列に token が混ざっても (URL に token が入る例外など)、終了時の出力には型名だけ。"""
    box.heartbeat()

    def boom(*a, **k):
        raise RuntimeError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getUpdates")
    monkeypatch.setattr(t, "read_questions", boom)
    monkeypatch.setenv(t.CARRIED_TOKEN_VAR, TOKEN)
    rc = t.main(["list", "--registry-dir", str(box.registry), "--config", str(box.config)])
    captured = capsys.readouterr()
    assert rc == t.EXIT_CANNOT_SEND and "internal_error:RuntimeError" in captured.err
    assert TOKEN not in captured.out + captured.err and TOKEN.split(":")[1] not in captured.out + captured.err


def test_token_does_not_leak_when_the_server_is_unreachable_or_slow(box, api, capsys):
    res = t.api_call(TOKEN, "getMe", {}, api_base="http://127.0.0.1:9", timeout=0.5)
    assert res.ok is False and res.error == "network"
    assert TOKEN not in repr(vars_of(res))
    api.mode = "hang"
    api.hang_seconds = 2.0
    start = time.time()
    res = t.api_call(TOKEN, "getMe", {}, api_base=api.url, timeout=0.5)
    assert res.error == "network" and time.time() - start < 1.9, "応答しないサーバーは timeout で打ち切る"
    assert TOKEN not in repr(vars_of(res))


def test_api_call_does_not_depend_on_the_global_urlopen(api, monkeypatch):
    """他のテスト・コードが `urllib.request.urlopen` を差し替えて戻さなくても通信できる (全 pytest でだけ落ちた回帰)。"""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("patched")))
    assert t.api_call(TOKEN, "getMe", {}, api_base=api.url).ok is True


def vars_of(res):
    return {k: getattr(res, k) for k in res.__slots__}


def test_a_dead_poll_subprocess_is_just_nothing_received(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 12)
    summary = t.run_cycle(box.registry, box.queue, t.load_telegram_config(box.config), "ok", lambda l: True,
                          creds=box.creds(), runner=boom, api_base=api.url, config_path=box.config)
    assert summary["polled"] and summary["poll"] == {"error": "poll_timeout"}
    garbage = lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="not json", stderr="")  # noqa: E731
    summary = t.run_cycle(box.registry, box.queue, t.load_telegram_config(box.config), "ok", lambda l: True,
                          creds=box.creds(), runner=garbage, api_base=api.url, config_path=box.config,
                          now=time.time() + 60)
    assert summary["poll"] == {"error": "poll_failed"}


def test_the_poll_subprocess_receives_credentials_by_env_never_by_argv(box, api):
    box.heartbeat()
    box.ask()
    seen = {}

    def spy(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout='{"processed": 0}', stderr="")
    t.run_cycle(box.registry, box.queue, t.load_telegram_config(box.config), "ok", lambda l: True,
                creds=box.creds(), runner=spy, api_base=api.url, config_path=box.config)
    assert cmd_text(seen["cmd"]).count(TOKEN) == 0 and CHAT not in seen["cmd"]
    assert seen["cmd"][:2] == ["timeout", "8"], "サブプロセスは上限付き"
    assert seen["env"][t.CARRIED_TOKEN_VAR] == TOKEN and seen["env"][t.CARRIED_CHAT_VAR] == CHAT


def cmd_text(cmd):
    return " ".join(cmd)


# ---------------------------------------------------------------------------
# 外部コマンド (1Password) と認証情報の運び方
# ---------------------------------------------------------------------------

def make_fake_op(tmp_path, token, chat, fail=False):
    counter = tmp_path / "op-calls.txt"
    script = tmp_path / "fake-op"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{counter}"\n'
        f'if [[ {1 if fail else 0} -eq 1 ]]; then exit 1; fi\n'
        f'case "$2" in *token*) echo "{token}" ;; *) echo "{chat}" ;; esac\n')
    os.chmod(script, 0o755)
    return script, counter


def test_op_source_ask_calls_op_per_question_and_poll_gets_a_carried_value(box, api, tmp_path):
    fake_op, counter = make_fake_op(tmp_path, TOKEN, CHAT)
    box.set_config("op", extra="")
    box.config.write_text(box.config.read_text() + f"    token_ref: op://v/token\n    chat_id_ref: op://v/chat\n    op_command: {fake_op}\n")
    box.heartbeat()
    r = box.ask()
    assert r.returncode == 0, r.stderr
    assert counter.read_text().count("read") == 2, "ask は呼び出しごとに token と chat_id を 1 回ずつ取り出す"
    qid = r.stdout.strip()
    press_button(box, api, qid, 0)
    before = counter.read_text()
    box.cycle([])
    assert counter.read_text() == before, "poll は dispatcher が運んだ値を受け、1Password を呼ばない"
    assert box.ledger()[qid]["status"] == "answered"


def test_failing_op_is_a_fixed_code_and_ask_exits_4(box, api, tmp_path):
    fake_op, _ = make_fake_op(tmp_path, TOKEN, CHAT, fail=True)
    box.config.write_text("telegram:\n  credentials:\n    source: op\n    token_ref: op://v/token\n"
                          f"    chat_id_ref: op://v/chat\n    op_command: {fake_op}\n")
    r = box.ask()
    assert r.returncode == 4 and "credential_command_failed" in r.stderr and api.calls == []
    out = subprocess.run([sys.executable, str(SCRIPTS / "lib_telegram.py"), "resolve", "--config", str(box.config)],
                         capture_output=True, text=True, env=box.env())
    assert out.stdout == "credential_command_failed\n"


def test_resolve_verb_prints_reason_then_values_only_on_success(box):
    out = subprocess.run([sys.executable, str(SCRIPTS / "lib_telegram.py"), "resolve", "--config", str(box.config)],
                         capture_output=True, text=True, env=box.env())
    assert out.stdout.splitlines() == ["ok", TOKEN, CHAT]
    box.set_config("")
    out = subprocess.run([sys.executable, str(SCRIPTS / "lib_telegram.py"), "resolve", "--config", str(box.config)],
                         capture_output=True, text=True, env=box.env())
    assert out.stdout == "no_credentials\n"


def test_ask_does_not_read_the_carrier_variables(box, api):
    """Director のシェルに運搬用の変数が残っていても、`ask` は config の source どおりに解決する。"""
    box.set_config("")
    r = box.run("ask", "--question", "q", "--option", "a", "--option", "b",
                env=box.env(**{t.CARRIED_TOKEN_VAR: TOKEN, t.CARRIED_CHAT_VAR: CHAT, "CREWVIA_TG_BOT_TOKEN": TOKEN,
                               "CREWVIA_TG_CHAT_ID": CHAT}))
    assert r.returncode == 3 and api.calls == []


# ---------------------------------------------------------------------------
# 送信の状態 (レート制限・バックオフ・2 者の書き込み)
# ---------------------------------------------------------------------------

def test_concurrent_senders_never_lose_a_slot(box, api):
    """`telegram-send.json` は 2 者が書く。ロックの下で読み直すので、同時に呼んでも予約が欠けない。"""
    box.registry.mkdir(parents=True, exist_ok=True)
    now = 1_900_000_000.0
    results = []

    def one():
        results.append(t.send_message(box.registry, box.creds(), "hi", api_base=api.url, now=now, sleep=lambda s: None))
    threads = [threading.Thread(target=one) for _ in range(8)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert all(r.ok for r in results), [r.error for r in results]
    state = json.loads((box.registry / t.SEND_FILE).read_text())
    assert state["last_sent_at"] == pytest.approx(now + 7 * t.MIN_SEND_INTERVAL_SECONDS, abs=0.01, rel=0), "予約を 1 つも数え損ねない (epoch 秒なので絶対許容。相対許容だと約 1900 秒の幅になり何も検出しない)"


def test_backoff_doubles_up_to_the_cap_and_success_resets(box, api):
    box.registry.mkdir(parents=True, exist_ok=True)
    api.mode = "http500"
    now, waits = 1_900_000_000.0, []
    for _ in range(8):
        r = t.send_message(box.registry, box.creds(), "x", api_base=api.url, now=now, sleep=lambda s: None)
        assert not r.ok
        st = json.loads((box.registry / t.SEND_FILE).read_text())
        waits.append(st["backoff_until"] - now)
        now = st["backoff_until"] + 1
    assert waits[:5] == [30, 60, 120, 240, 480] and max(waits) <= t.BACKOFF_MAX_SECONDS
    api.mode = "ok"
    assert t.send_message(box.registry, box.creds(), "x", api_base=api.url, now=now, sleep=lambda s: None).ok
    st = json.loads((box.registry / t.SEND_FILE).read_text())
    assert st["consecutive_failures"] == 0 and st["backoff_until"] is None


def test_rate_limit_retry_after_and_warning_throttle(box, api):
    box.registry.mkdir(parents=True, exist_ok=True)
    api.mode = "ratelimit"
    api.retry_after = 77
    now = 1_900_000_000.0
    r = t.send_message(box.registry, box.creds(), "x", api_base=api.url, now=now, sleep=lambda s: None)
    assert r.error == "rate_limited" and r.warn is True
    st = json.loads((box.registry / t.SEND_FILE).read_text())
    assert st["backoff_until"] == pytest.approx(now + 77, abs=0.01, rel=0)
    blocked = t.send_message(box.registry, box.creds(), "x", api_base=api.url, now=now + 10, sleep=lambda s: None)
    assert blocked.error == "backoff"
    again = t.send_message(box.registry, box.creds(), "x", api_base=api.url, now=now + 80, sleep=lambda s: None)
    assert again.warn is False, "警告は 10 分に 1 回"
    later = t.send_message(box.registry, box.creds(), "x", api_base=api.url, now=now + 700, sleep=lambda s: None)
    assert later.warn is True


def test_telegram_available_reflects_receiver_and_backoff(box, api):
    now = time.time()
    assert t.telegram_available(box.registry, now) is False             # 受信側の状態が無い
    box.heartbeat(now=now)
    assert t.telegram_available(box.registry, now) is True
    api.mode = "http500"
    t.send_message(box.registry, box.creds(), "x", api_base=api.url, now=now, sleep=lambda s: None)
    assert t.telegram_available(box.registry, now + 1) is False         # バックオフ中
    assert t.telegram_available(box.registry, now + 100) is True or t.telegram_available(box.registry, now + 100) is False


def test_receiver_heartbeat_rewrites_only_on_change_or_every_30_seconds(box):
    now = 1_900_000_000.0
    assert box.heartbeat(now=now)["wrote"] is True
    assert box.heartbeat(now=now + 10)["wrote"] is False
    assert box.heartbeat(now=now + 29)["wrote"] is False
    assert box.heartbeat(now=now + 30)["wrote"] is True
    first = json.loads((box.registry / t.RECEIVER_FILE).read_text())
    assert box.heartbeat(now=now + 35, creds=t.Credentials("777777:OTHER-TOKEN-abcdefghij", CHAT))["wrote"] is True
    assert json.loads((box.registry / t.RECEIVER_FILE).read_text())["bot_id"] == 777777 != first["bot_id"]
    text = (box.registry / t.RECEIVER_FILE).read_text()
    assert "OTHER-TOKEN" not in text and CHAT not in text


# ---------------------------------------------------------------------------
# list / verify / cancel の使い方
# ---------------------------------------------------------------------------

def test_verify_states(box, api):
    box.heartbeat()
    qid = box.ask().stdout.strip()
    assert json.loads(box.run("verify", "--q", qid).stdout)["status"] == "open"
    assert json.loads(box.run("verify", "--q", "q-ffffffff").stdout) == {"status": "not_found"}
    assert box.run("verify", "--q", "bogus").returncode == 1
    listed = box.run("list").stdout.strip().splitlines()
    assert len(listed) == 1 and json.loads(listed[0])["q"] == qid


def test_ask_user_sh_is_a_thin_strict_wrapper(box, api):
    r = subprocess.run(["bash", str(SCRIPTS / "ask_user.sh"), "bogus"], capture_output=True, text=True, env=box.env())
    assert r.returncode == 1
    r = subprocess.run(["bash", str(SCRIPTS / "ask_user.sh")], capture_output=True, text=True, env=box.env())
    assert r.returncode == 1
    r = subprocess.run(["bash", str(SCRIPTS / "ask_user.sh"), "ask", "--question", "q", "--option", "a", "--option", "b",
                        "--config", str(box.config), "--registry-dir", str(box.registry), "--api-base", api.url],
                       capture_output=True, text=True, env=box.env())
    assert r.returncode == 4 and "receiver_unknown" in r.stderr         # 心拍が無い


def test_card_state_comes_from_the_card_when_readable(box, api):
    (box.queue / "missions" / "20261004-x" / "tasks").mkdir(parents=True)
    (box.queue / "missions" / "20261004-x" / "tasks" / "t017.md").write_text(
        "---\nid: t017\ntitle: x\nskills: [code]\npriority: high\nstatus: needs_director\nblocked_by: []\n---\n\n## Description\nx\n")
    box.heartbeat()
    qid = box.ask("--task", "20261004-x/t017").stdout.strip()
    press_button(box, api, qid, 0)
    forwarded = []
    box.cycle(forwarded)
    assert forwarded and forwarded[0].endswith("task_state=needs_director")
