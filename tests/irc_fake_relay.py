#!/usr/bin/env python3
"""偽の relay bot の HTTP サーバー (localhost)。t007 のテスト用。**本物の bot・本物の IRC は叩かない。**

`knowledge/irc-mention-relay.md` §2 の API を最小限に真似る (付録 A の応答の形)。`http.server` をテストの中で立て、
`irc.url` をそこへ向ける。

    with FakeRelay(token="…") as relay:
        relay.add(instance="wsl", name="sora", text="@Sora hi")      # 受信箱に積む (id は自動採番)
        relay.url                                                    # → "http://127.0.0.1:<port>"
        relay.requests                                               # → [{"path":…, "query":…, "auth":…}, …]
        relay.mode = "ok" | "http500" | "redirect" | "badjson" | "api2" | "404" | "409" | "hang" | "huge"

`auth` は受け取った Authorization ヘッダの値 (テストが token の取り違えを見るため)。違う token は 401 (A-5)。
"""

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EPOCH = "7f3a9c21"


class FakeRelay:
    def __init__(self, token="x" * 40, epoch=EPOCH, now=1_762_300_100, hang_seconds=3.0, redirect_to="http://127.0.0.1:9/"):
        self.token = token
        self.epoch = epoch
        self.now = now
        self.mode = "ok"
        self.hang_seconds = hang_seconds
        self.redirect_to = redirect_to
        self.gap = False
        self.delay = 0.0                 # 応答を遅らせる秒 (プロセス表の走査テスト用)
        self.raw_body = None             # mode=badjson のときに返すバイト列 (None なら壊れた JSON)
        self.mentions = []               # 積まれた発言 (instance つき)
        self.head_id = 0
        self.requests = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- 準備 ---------------------------------------------------------------
    def add(self, instance="wsl", name="sora", text="@Sora hi", kind="channel", channel="#crewvia", sender="tyz",
            account="tyz", ts=None, suppressed_before=0, truncated=False, id=None):
        with self._lock:
            self.head_id = id if id is not None else self.head_id + 1
            if kind == "dm":
                channel = None
            m = {"id": self.head_id, "ts": ts if ts is not None else self.now - 100, "kind": kind, "channel": channel,
                 "sender": sender, "account": account, "name": name, "text": text, "truncated": truncated,
                 "suppressed_before": suppressed_before, "_instance": instance}
            self.mentions.append(m)
            return self.head_id

    def skip_id(self):
        """他の instance 宛の id を 1 つ飛ばす (A-1 の id 42)。"""
        with self._lock:
            self.head_id += 1

    def query_values(self, key):
        return [r["query"].get(key, [None])[0] for r in self.requests if r["path"] == "/v1/mentions"]

    def mention_requests(self):
        return [r for r in self.requests if r["path"] == "/v1/mentions"]

    # -- サーバー -------------------------------------------------------------
    def __enter__(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                auth = self.headers.get("Authorization")
                with relay._lock:
                    relay.requests.append({"path": parts.path, "query": query, "auth": auth})
                if auth != "Bearer " + relay.token:
                    return self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
                if relay.delay:
                    time.sleep(relay.delay)
                mode = relay.mode
                if mode == "hang":
                    time.sleep(relay.hang_seconds)
                    return self._send(200, {})
                if mode == "http500":
                    return self._send(500, {"error": "internal"})
                if mode == "redirect":
                    return self._send(302, {}, {"Location": relay.redirect_to})
                if mode == "404" or parts.path != "/v1/mentions":
                    return self._send(404, {"error": "not_found"})
                if mode == "409":
                    return self._send(409, {"error": "after_ahead", "epoch": relay.epoch, "head_id": relay.head_id})
                if mode == "badjson":
                    return self._send(200, relay.raw_body if relay.raw_body is not None else b"{not json")
                if mode == "huge":
                    return self._send(200, b'{"x":"' + b"a" * (1024 * 1024 + 10) + b'"}')
                instance = query.get("instance", [None])[0]
                after = int(query.get("after", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                with relay._lock:
                    mine = [m for m in relay.mentions if m["_instance"] == instance and m["id"] > after]
                    page = mine[:limit]
                    has_more = len(mine) > limit
                    cursor = page[-1]["id"] if has_more else relay.head_id
                    body = {"api": 2 if mode == "api2" else 1, "epoch": relay.epoch, "instance": instance,
                            "now": relay.now, "head_id": relay.head_id, "cursor": max(cursor, after),
                            "has_more": has_more, "gap": relay.gap,
                            "mentions": [{k: v for k, v in m.items() if not k.startswith("_")} for m in page]}
                self._send(200, body)

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


class CountingListener:
    """リダイレクト先の「第 2 の宛先」。接続が 1 回でも来たら `connections` が増える (追っていないことの証明)。"""

    def __init__(self):
        import socket
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        self.connections = 0
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            self.connections += 1
            conn.close()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop = True
        self._thread.join(timeout=1)
        self._sock.close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._sock.getsockname()[1]}/"
