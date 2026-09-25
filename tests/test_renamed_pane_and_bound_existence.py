#!/usr/bin/env python3
"""PR #215 2 巡目 (t027): Kai-codex の P2 2 件。

## P2-1 — rename された pane に、記録が残っていても誰も届かない

t022 で `_pane_existence()` は「label 不一致」を `PANE_UNOBSERVED` にして記録を残した。
だが解決経路 (`_resolve_ids`) は UNOBSERVED を「記録を捨てて label で引き直す」と読み、
`herdr pane rename` された pane は label では二度と見つからない。**記録は残るのに
capture / send / pid / `kill --force` のどれも届かず、孤児化は解決していなかった。**

直し方: server の世代・pane id・**記録の tab id** が一致する pane は、label が変わって
いても同じ pane (`PANE_RENAMED`)。label で見つからないときに限り、記録の id で解決する。
label が別の pane を指しているなら従来どおりそちらが答えで、kill の認可
(`may_destroy_pane`) はその id を記録と突き合わせて食い違いを拒否する。

## P2-2 — 「無い」と答えた接続が、記録の server の同じ世代であることを確かめていない

sweep は `server_identity()` を 1 回取って接続を閉じ、`_pane_existence()` が別の CLI 接続を
張っていた。herdr が sweep の途中で再起動すると、後継 server は**旧 id すべてに**
`pane_not_found` を返し、「世代が違う記録は残す」という契約に反して記録が消えた。
識別子を全記録で使い回すキャッシュは窓を広げるだけだった。

直し方: `pane.get` を、SO_PEERCRED で世代を確かめた**その接続の上に**流す
(`_herdr_pane_get_bound`、`_herdr_close_tab_bound` と同じ形)。識別子は記録ごとに取り直す。

## ここで固定するもの

  A. 実プロセスの 2 世代 (実 unix socket + SO_PEERCRED) で、別世代の「無い」は記録を消さない
  B. sweep の途中で server が入れ替わっても記録が残る (窓を開けて固定する)
  C. rename された pane に capture / send / pid / kill (通常と --force) が**届く**
  D. それでも kill の認可は緩まない: 別 checkout の記録 / 別世代 / handle 食い違いは拒否のまま

  python3 -m pytest tests/test_renamed_pane_and_bound_existence.py -v
"""

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_mux  # noqa: E402
from lib_mux import HerdrBackend, PANE_GONE, PANE_RENAMED  # noqa: E402

FUTURE = time.time() + 3600
TAB = "wP:t7W"
PANE = "wP:p1"


# ---------------------------------------------------------------------------
# 実プロセスの偽 herdr server (世代 = プロセス)
# ---------------------------------------------------------------------------
#
# SO_PEERCRED はカーネルが答えるので、世代 (pid:starttime) の違いは作り物ではない。
# pane の一覧はファイルから毎回読むので、世代ごとに答えを変えられる。

_FAKE_HERDR = r'''
import json, os, socket, sys
sock_path, panes_path, log_path = sys.argv[1:4]
try:
    os.unlink(sock_path)
except FileNotFoundError:
    pass
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(8)
sys.stderr.write("ready\n")
sys.stderr.flush()
while True:
    conn, _ = srv.accept()
    with conn:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf.strip():
            continue                      # identity の確認だけ (要求なし)
        req = json.loads(buf.split(b"\n")[0])
        rid, method = req.get("id", ""), req.get("method")
        params = req.get("params", {})
        with open(log_path, "a") as fh:
            fh.write(json.dumps([os.getpid(), method, params]) + "\n")
        if method == "ping":
            body = {"id": rid, "result": {"type": "pong"}}
        elif method == "pane.get":
            with open(panes_path) as fh:
                panes = json.load(fh)
            pane = panes.get(params.get("pane_id"))
            if pane is None:
                body = {"id": rid, "error": {"code": "pane_not_found",
                                             "message": "pane not found"}}
            else:
                body = {"id": rid, "result": {"type": "pane_info", "pane": pane}}
        elif method == "tab.close":
            body = {"id": rid, "result": {"type": "ok"}}
        else:
            body = {"id": rid, "error": {"code": "unsupported_method"}}
        conn.sendall(json.dumps(body).encode() + b"\n")
'''


class FakeHerdrServers:
    """同じソケットの上に順番に立つ偽 herdr サーバー (世代)。"""

    def __init__(self, root: Path, monkeypatch):
        self.sock_path = root / "herdr.sock"
        self.panes_path = root / "panes.json"
        self.log_path = root / "requests.log"
        self.script = root / "fake_herdr.py"
        self.script.write_text(_FAKE_HERDR, encoding="utf-8")
        self.proc = None
        self.set_panes({})
        monkeypatch.setattr(lib_mux, "_HERDR_SOCK_PATH", self.sock_path)

    def set_panes(self, panes: dict):
        self.panes_path.write_text(json.dumps(panes), encoding="utf-8")

    def start(self, panes=None):
        if panes is not None:
            self.set_panes(panes)
        self.proc = subprocess.Popen(
            [sys.executable, str(self.script), str(self.sock_path),
             str(self.panes_path), str(self.log_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(str(self.sock_path))
                return
            except OSError:
                time.sleep(0.05)
        raise AssertionError("fake herdr server did not come up")

    def stop(self):
        if self.proc is None:
            return
        self.proc.kill()
        self.proc.wait(timeout=10)
        self.proc = None

    def requests(self, method=None):
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        rows = [json.loads(line) for line in lines]
        return [r for r in rows if method is None or r[1] == method]

    def closed_tabs(self):
        return [r[2]["tab_id"] for r in self.requests("tab.close")]


@pytest.fixture
def servers(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="t27-herdr-", dir="/tmp"))
    fake = FakeHerdrServers(root, monkeypatch)
    try:
        yield fake
    finally:
        fake.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "ours"
    (root / "registry" / "mux").mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: root)
    return root


def _label(name):
    return lib_mux._pane_name(name)


def _renamed(tab=TAB):
    """rename された pane: 同じ tab・同じ pane id・別の label。"""
    return {"pane_id": PANE, "tab_id": tab, "label": "renamed-by-someone"}


def _record(name="dispatcher", identity=None, pane_id=PANE, tab=TAB):
    assert lib_mux.write_pane_record(name, "herdr", tab, pane_id=pane_id,
                                     server=identity)


def _exists(name="dispatcher"):
    return lib_mux.pane_record_path(name).exists()


# ===========================================================================
# P2-2 — 存在の問い合わせを、記録の server の世代に束ねる
# ===========================================================================

def test_the_answer_is_asked_on_a_connection_of_the_recorded_generation(servers):
    servers.start({PANE: _renamed()})
    identity = lib_mux._herdr_server_identity()

    answer = lib_mux._herdr_pane_get_bound(PANE, identity)
    assert answer["result"]["pane"]["tab_id"] == TAB
    missing = lib_mux._herdr_pane_get_bound("wP:p99", identity)
    assert missing["error"]["code"] == "pane_not_found"


def test_a_replaced_server_is_not_asked_and_its_absence_is_not_evidence(servers):
    """A の世代で束縛した問い合わせは、B には届かない。B の「無い」は記録を消さない。

    後継 server は旧 id すべてに `pane_not_found` を返す。それを「消えた」と読むのが
    Kai 2巡目 P2-2 の欠陥だった。
    """
    servers.start({PANE: _renamed()})
    a_identity = lib_mux._herdr_server_identity()
    servers.stop()
    servers.start({})                       # B: 旧 id を何も知らない
    b_identity = lib_mux._herdr_server_identity()
    assert a_identity[1] != b_identity[1], "前提が崩れている: 2 世代の generation が同じ"

    assert lib_mux._herdr_pane_get_bound(PANE, a_identity) is None
    assert servers.requests("pane.get") == [], \
        "検証していない世代 (B) に pane.get が送られた"


def test_a_definite_absence_from_the_recorded_generation_still_ends_the_record(
        servers, checkout):
    """**可用性**: 束縛を足しても、記録の世代が「無い」と答えれば掃除は記録を消す。"""
    servers.start({})                       # A は pane を持っていない
    _record(identity=lib_mux._herdr_server_identity())

    dropped = lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                              now=FUTURE)
    assert dropped == ["dispatcher"] and not _exists()
    assert [r[1] for r in servers.requests()] == ["pane.get"]


def test_a_herdr_restart_during_the_sweep_keeps_the_record(servers, checkout,
                                                           monkeypatch):
    """**Kai 2巡目 P2-2 そのもの**: sweep の識別子確認のあと、問い合わせの前に入れ替わる。

    記録は A の世代で書かれている。sweep が A だと確かめた (識別子の前段) 直後に A が
    落ち、B が同じソケットで立つ。B は旧 id を知らないので `pane_not_found` を返す。
    問い合わせが別の接続を張るなら、それが「無い」と読まれて記録が消える。

    差し込むのは既にある継ぎ目 (`server_identity()`) をテスト側で包むだけで、本番コードに
    テスト用のフックは無い。
    """
    servers.start({PANE: _renamed()})
    a_identity = lib_mux._herdr_server_identity()
    _record(identity=a_identity)

    backend = HerdrBackend()
    real_identity = backend.server_identity

    def identify_then_replace_the_server():
        identity = real_identity()          # sweep は A を確かめた
        servers.stop()                      # その直後に A が落ち
        servers.start({})                   # B が立つ (旧 id は何も知らない)
        return identity
    monkeypatch.setattr(backend, "server_identity", identify_then_replace_the_server)

    dropped = lib_mux.reap_stale_pane_records(backend, repo_root=checkout, now=FUTURE)

    assert dropped == [], f"別世代の「無い」で記録が消えた: {dropped}"
    assert _exists()
    assert servers.requests("pane.get") == [], \
        "B (検証していない世代) が pane.get を受け取った"


def test_the_sweep_takes_the_identity_per_record_not_once(checkout, monkeypatch):
    """識別子を全記録で使い回さない (窓を広げるだけ)。記録ごとに取り直す。"""
    for name in ("Ann-worker", "Bea-worker", "Cyd-worker"):
        _record(name, identity=("/s", "g1"), pane_id="p-" + name, tab="t-" + name)
    taken = []

    class Counting(HerdrBackend):
        def server_identity(self):
            taken.append(1)
            return ("/s", "g1")
        def record_existence(self, name, record):
            return PANE_GONE

    dropped = lib_mux.reap_stale_pane_records(Counting(), repo_root=checkout,
                                              now=FUTURE)
    assert sorted(dropped) == ["Ann-worker", "Bea-worker", "Cyd-worker"]
    assert len(taken) == 3, \
        f"識別子を {len(taken)} 回しか取っていない (3 記録): 使い回している"


# ===========================================================================
# P2-1 — rename された pane に届く
# ===========================================================================

class _Cli:
    """`_herdr_run` / `_herdr_run_raw` の代わり。呼ばれた (verb, pane/tab) を記録する。"""

    def __init__(self, panes_in_list):
        self.calls = []
        self.panes_in_list = panes_in_list

    def run(self, cmd_key, extra_args, timeout=10):
        self.calls.append((cmd_key, list(extra_args)))
        if cmd_key == "pane_list":
            return {"result": {"panes": self.panes_in_list}}
        if cmd_key == "pane_process_info":
            return {"result": {"process_info": {"shell_pid": 4100,
                                                "foreground_processes": []}}}
        return {"result": {"type": "ok"}}

    def raw(self, cmd_key, extra_args, timeout=10):
        self.calls.append((cmd_key, list(extra_args)))
        return "❯ \n"

    def targets(self, cmd_key):
        return [a[0] for k, a in self.calls if k == cmd_key]


@pytest.fixture
def renamed_pane(servers, checkout, monkeypatch):
    """A の世代で pane を作り、記録を書き、その pane を rename した状態。

    `pane list` には rename 後の label で出るので、`name` の label では見つからない。
    """
    servers.start({PANE: _renamed()})
    identity = lib_mux._herdr_server_identity()
    _record("dispatcher", identity)
    _record("Ren-worker", identity)
    cli = _Cli([{"pane_id": PANE, "tab_id": TAB, "label": "renamed-by-someone"}])
    monkeypatch.setattr(lib_mux, "_herdr_run", cli.run)
    monkeypatch.setattr(lib_mux, "_herdr_run_raw", cli.raw)
    monkeypatch.setattr(lib_mux, "daemon_pane_owner",
                        lambda pane_pid, **kw: (lib_mux.OWNER_MINE, "stubbed"))
    backend = HerdrBackend()
    monkeypatch.setattr(backend, "_workspace_id", lambda: "w1")
    return backend, cli, servers


def test_a_renamed_pane_is_identified_by_where_it_is_not_by_name(renamed_pane):
    backend, cli, servers = renamed_pane
    record = lib_mux.read_pane_record("dispatcher")
    assert backend._pane_existence(record, _label("dispatcher")) == PANE_RENAMED


def test_capture_reaches_a_renamed_pane(renamed_pane):
    backend, cli, _ = renamed_pane
    assert backend.capture("Ren-worker") == "❯ \n"
    assert cli.targets("pane_read") == [PANE]


def test_pid_reaches_a_renamed_pane(renamed_pane):
    backend, cli, _ = renamed_pane
    assert backend.pid("Ren-worker") == 4100
    assert cli.targets("pane_process_info") == [PANE]


def test_send_reaches_a_renamed_pane(renamed_pane):
    backend, cli, _ = renamed_pane
    assert backend.send("Ren-worker", "hello") is True
    assert cli.targets("pane_run") == [PANE]


def test_kill_reaches_a_renamed_pane(renamed_pane):
    backend, cli, _ = renamed_pane
    assert backend.kill("Ren-worker") is True
    assert cli.targets("tab_close") == [TAB]
    assert not _exists("Ren-worker")


def test_kill_force_reaches_a_renamed_pane(renamed_pane):
    """Kai が mock で失敗を再現した形: `kill --force` (allow_foreign) が届く。"""
    backend, cli, _ = renamed_pane
    assert backend.kill("dispatcher", allow_foreign=True) is True
    assert cli.targets("tab_close") == [TAB]


def test_the_guarded_daemon_kill_reaches_a_renamed_pane(renamed_pane):
    """守られた経路 (--force なし) も届く。閉じるのは検査した世代の上で。"""
    backend, cli, servers = renamed_pane
    assert backend.kill("dispatcher") is True
    assert servers.closed_tabs() == [TAB]
    assert cli.targets("tab_close") == [], "束縛の無い CLI の tab close が使われた"


# ===========================================================================
# D. kill の認可は緩んでいない
# ===========================================================================

def test_another_checkouts_record_still_refuses_a_renamed_pane(renamed_pane):
    """記録経由で pane に届くようになっても、他人の記録は認可にならない。"""
    backend, cli, servers = renamed_pane
    path = lib_mux.pane_record_path("dispatcher")
    rec = json.loads(path.read_text())
    rec["checkout"] = "/somewhere/else"
    path.write_text(json.dumps(rec))
    assert backend.kill("dispatcher") is False
    assert servers.closed_tabs() == []


def test_a_record_of_another_generation_neither_resolves_nor_authorises(
        renamed_pane):
    """server が入れ替わった後は、記録の id では引かない (id は別の pane のもの)。"""
    backend, cli, servers = renamed_pane
    servers.stop()
    servers.start({})                       # B
    assert backend._resolve_ids("dispatcher") is None
    assert backend.kill("dispatcher") is False
    assert servers.closed_tabs() == []
    assert _exists("dispatcher"), "観測できなかっただけで記録が消えた"


def test_a_label_held_by_another_pane_is_still_refused_as_a_handle_mismatch(
        renamed_pane, monkeypatch):
    """label が別の pane (別の tab) を指すなら、その pane が答えで、記録と食い違って拒否。"""
    backend, cli, servers = renamed_pane
    cli.panes_in_list = [
        {"pane_id": "wP:p9", "tab_id": "wP:t9", "label": _label("dispatcher")}]
    ids = backend._resolve_ids("dispatcher")
    assert ids["tab_id"] == "wP:t9", "label が指す pane を記録で上書きした"
    assert backend.kill("dispatcher") is False
    assert servers.closed_tabs() == []
