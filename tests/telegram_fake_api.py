#!/usr/bin/env python3
"""偽の Telegram Bot API サーバー (localhost)。PR-A のテスト用。**本物の Bot API は叩かない。**

`http.server` をテストの中で立て、`lib_telegram` の `api_base` 引数 (`--api-base`) でそこへ向ける。
env の TEST 専用スイッチを本番コードに足さない (設計 §9)。

使い方:
    with FakeBotApi(token="123456:FAKE-TOKEN-abcdefghij") as api:
        api.queue_update({...})           # getUpdates が返す update を積む (update_id は自動採番も可)
        api.url                           # → "http://127.0.0.1:<port>"
        api.calls                         # → [(method, payload), ...] (受けた呼び出しの全記録)
        api.mode = "ok" | "http500" | "http400" | "hang" | "ratelimit"
        api.method_modes["editMessageReplyMarkup"] = "hang"   # メソッドごとに上書き (他は api.mode)

トークンはパスの一部 (`/bot<token>/<method>`)。違う token は 401 で拒否する (token を取り違えたテストが緑にならない)。
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeBotApi:
    def __init__(self, token="123456:FAKE-TOKEN-abcdefghij", hang_seconds=3.0):
        self.token = token
        self.mode = "ok"
        self.method_modes = {}          # {メソッド名: モード}。無いメソッドは self.mode
        self.hang_seconds = hang_seconds
        self.retry_after = 7
        self.calls = []                 # [(method, payload)]
        self.updates = []               # 積まれた update (dict)
        self._next_update_id = 9000
        self._next_message_id = 4700
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- 準備 ---------------------------------------------------------------
    def queue_update(self, update):
        with self._lock:
            if "update_id" not in update:
                self._next_update_id += 1
                update = dict(update, update_id=self._next_update_id)
            self.updates.append(update)
            return update["update_id"]

    def calls_of(self, method):
        with self._lock:
            return [p for m, p in self.calls if m == method]

    # -- サーバー -------------------------------------------------------------
    def __enter__(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):    # 標準エラーを汚さない
                pass

            def do_POST(self):               # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {}
                parts = self.path.strip("/").split("/")
                if len(parts) != 2 or not parts[0].startswith("bot") or parts[0][3:] != api.token:
                    return self._send(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                method = parts[1]
                with api._lock:
                    api.calls.append((method, payload))
                mode = api.method_modes.get(method, api.mode)
                if mode == "hang":
                    time.sleep(api.hang_seconds)
                if mode == "http500":
                    return self._send(500, {"ok": False, "error_code": 500, "description": "boom"})
                if mode == "http400":
                    return self._send(400, {"ok": False, "error_code": 400, "description": "Bad Request: message can't be edited"})
                if mode == "ratelimit":
                    return self._send(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                            "parameters": {"retry_after": api.retry_after}})
                return self._send(200, {"ok": True, "result": api._result(method, payload)})

            def _send(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def _result(self, method, payload):
        with self._lock:
            if method == "sendMessage":
                self._next_message_id += 1
                return {"message_id": self._next_message_id, "chat": {"id": int(payload.get("chat_id", 0))},
                        "text": payload.get("text", "")}
            if method == "getUpdates":
                offset = int(payload.get("offset") or 0)
                return [u for u in self.updates if u["update_id"] >= offset]
            return True


# ---------------------------------------------------------------------------
# update の作り方 (本物の Bot API の形)
# ---------------------------------------------------------------------------

def callback_update(chat_id, message_id, data, *, from_id=None, callback_id="cb-1", update_id=None):
    u = {"callback_query": {"id": callback_id, "from": {"id": chat_id if from_id is None else from_id},
                            "message": {"message_id": message_id, "chat": {"id": chat_id}},
                            "data": data}}
    if update_id is not None:
        u["update_id"] = update_id
    return u


def reply_update(chat_id, reply_to_message_id, text, *, from_id=None, update_id=None):
    u = {"message": {"message_id": 1, "chat": {"id": chat_id}, "from": {"id": chat_id if from_id is None else from_id},
                     "text": text, "reply_to_message": {"message_id": reply_to_message_id}}}
    if update_id is not None:
        u["update_id"] = update_id
    return u


def plain_update(chat_id, text, *, update_id=None):
    u = {"message": {"message_id": 2, "chat": {"id": chat_id}, "from": {"id": chat_id}, "text": text}}
    if update_id is not None:
        u["update_id"] = update_id
    return u
