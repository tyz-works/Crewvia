#!/usr/bin/env python3
"""tests/test_kill_bound_to_server.py

**破壊コマンドを、占有者を分類したサーバーに束縛する** (t043 / PR #209,
Codex 8 巡目の P1、この PR で最後に残った P1)。

7 巡目 (t042) で破壊の条件を「記録が一致する **AND** 占有者が MINE か確実に
空」にした。8 巡目の P1 は、その AND がサーバー再起動の競合を閉じていないこと
を指す。`TmuxBackend.kill()` は

  1. サーバー A に `display-message` を投げ、`@7` とその pane pid、
     A の世代を受け取る
  2. その pid を /proc で歩いて占有者を MINE と分類し、A の世代で書かれた
     spawn 記録と突き合わせる
  3. **別の tmux 呼び出し** で `kill-window -t @7` を実行する

3 が別の呼び出しである、すなわち **別のクライアント接続** である、すなわち
**別のサーバーでありうる**、というのが指摘の中身である。1 と 2 で積み上げた
2 つの積極的な証明 (記録・占有者) はどちらも **A についての** 事実であって、
A が終了し同じソケットで B が起動していれば、`@7` は B が `@0` から順に
振り直した id として解決される。B の占有者は一度も分類されていないのだから、
B の占有者が UNKNOWN になる必要すらない — AND 条件では防げない。

クライアント側にもう 1 回チェックを足しても窓が動くだけで閉じない。そこで
**検証と破壊を同じ 1 回の呼び出しに入れる**:

  - tmux — `tmux -S <socket> if-shell -F '#{==:#{pid},<世代>}' 'kill-window
    -t @7' '<不一致の印>'`。tmux は 1 つのクライアント接続のコマンド列を
    **1 つのサーバープロセス**が実行するので、`#{pid}` を評価したサーバーが
    そのまま `kill-window` を実行する。入れ替わっていれば比較が外れ、
    `kill-window` は実行されない。
  - herdr — CLI (`herdr tab close`) は自分で接続を張るので、検証とは別の接続に
    なる。そこで `tab.close` 要求を、**SO_PEERCRED でカーネルに名前を言わせた
    その接続の上に**直接流す。

Codex は「これに boot ID やプロセス開始時刻の追跡は不要」と明示している。
実際、必要なのは既に持っている世代を**破壊コマンドと同じ接続で**確かめること
だけである。

## 赤の作り方 (このファイルの要点)

サーバー入れ替えは実時間の競合なので、fake で「入れ替わったことにする」と
何を確かめているのか分からなくなる。ここでは **本物の tmux サーバーを落として
同じソケットで別のサーバーを起動する**。入れ替えの瞬間は、1 と 3 の間で実際に
時間を食っている層 — /proc を歩く占有者の分類 — に差し込む。分類器は本物の
時間を使う場所であり、かつ確かめたい層ではないので、ここが差し込み口として
正しい。

herdr 側も同じ考え方で、**本物の unix socket の上に本物の別プロセス**を
2 世代立てる。SO_PEERCRED はカーネルが答えるので、世代の違いは作り物ではない。

## 可用性 (これを壊すと本番が止まる)

  - 入れ替わっていない**ふつうの kill** は今まで通り通らなければならない。
    本番の dispatcher / watchdog のペインが閉じられなくなると、Worker の
    後始末ができなくなる。それを確かめるテストを両バックエンドに置く。
  - t006 QA で PASS した安全性と t035 の husk 復活を壊さない (それぞれ
    tests/test_destruction_and_condition.py, tests/test_daemon_husk_respawn.py)。

    python3 -m pytest tests/test_kill_bound_to_server.py -v
"""

import json
import os
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

from test_spawn_record_binding import _checkout  # noqa: E402


# ---------------------------------------------------------------------------
# 本物の tmux サーバーを 2 世代
# ---------------------------------------------------------------------------
#
# `TMUX_TMPDIR` で既定ソケットの置き場所ごと隔離する。lib_mux は `tmux` を
# 素で呼ぶので、`-S` を渡す口が無くてもこれで本番のサーバーには当たらない
# (宛先セッション名とペイン名接頭辞は conftest がさらに隔離している)。

class _TmuxServers:
    """同じソケットの上に順番に立つ tmux サーバー群。"""

    def __init__(self, root: Path):
        self.root = root

    # -- 素の tmux ----------------------------------------------------------
    def _tmux(self, *args, check=True):
        r = subprocess.run(["tmux", *args], capture_output=True, text=True,
                           timeout=10)
        if check and r.returncode != 0:
            raise AssertionError(f"tmux {args} failed: {r.stderr}")
        return r.stdout.strip()

    def _live_socket(self):
        """稼働中のサーバーのソケットパス。立っていなければ None。"""
        r = subprocess.run(
            ["tmux", "display-message", "-p", "#{socket_path}"],
            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None

    def _require_isolated(self):
        """触ろうとしている相手が自分の一時ディレクトリの上に居ること。

        `tmux kill-server` は宛先を取らないので、`TMUX_TMPDIR` が効いていなければ
        利用者の**本物の**サーバーを落とす。2026-09-23 の事故はこの形だった
        (`tests/conftest.py` 冒頭)。ここは `-S` を渡せない本番コードを相手に
        しているぶん、テスト側で毎回確かめるしかない。
        """
        socket_path = self._live_socket()
        if socket_path is None:
            return None
        if not socket_path.startswith(str(self.root)):
            raise AssertionError(
                f"tmux server is not isolated: socket {socket_path!r} is "
                f"outside {self.root} — refusing to touch it")
        return socket_path

    # -- 世代を立てる -------------------------------------------------------
    def start(self, first_window: str, *more_windows: str) -> dict:
        """新しいサーバーを起動し、`{window_name: window_id}` を返す。"""
        session = lib_mux._session()
        ids = {first_window: self._tmux(
            "new-session", "-d", "-s", session, "-n", first_window,
            "-P", "-F", "#{window_id}", "sleep 600")}
        self._require_isolated()
        for name in more_windows:
            ids[name] = self._tmux(
                "new-window", "-t", session, "-n", name,
                "-P", "-F", "#{window_id}", "sleep 600")
        return ids

    def stop(self):
        if self._require_isolated() is None:
            return                      # 立っていない
        subprocess.run(["tmux", "kill-server"], capture_output=True,
                       timeout=10)
        # サーバーが本当に降りるまで待つ。降りきる前に次を起動すると 2 世代目が
        # 1 世代目に接続してしまい、入れ替えたつもりで入れ替わっていない。
        deadline = time.time() + 10
        while time.time() < deadline:
            r = subprocess.run(["tmux", "list-sessions"],
                               capture_output=True, timeout=10)
            if r.returncode != 0:
                return
            time.sleep(0.05)
        raise AssertionError("tmux server did not go down")

    # -- 観測 ---------------------------------------------------------------
    def identity(self):
        out = self._tmux("display-message", "-p", "#{socket_path} #{pid}")
        endpoint, generation = out.split()
        return endpoint, generation

    def windows(self) -> dict:
        out = self._tmux("list-windows", "-a", "-F",
                         "#{window_id} #{window_name}", check=False)
        return dict(line.split(" ", 1) for line in out.splitlines() if line)


@pytest.fixture
def tmux_servers(monkeypatch):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    # sockaddr_un.sun_path は 108 バイトしかなく、pytest の tmp_path だけで
    # その大半を使ってしまう ("File name too long" で接続できない)。だから
    # サーバーの置き場所だけは短い mkdtemp にする。
    root = Path(tempfile.mkdtemp(prefix="t43-tmux-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(root))
    monkeypatch.delenv("TMUX", raising=False)
    servers = _TmuxServers(root)
    try:
        yield servers
    finally:
        servers.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def as_our_checkout(monkeypatch, tmp_path):
    root = _checkout(tmp_path / "ours")
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(root))
    return root


def _owner_is_ours(monkeypatch, *, meanwhile=None):
    """占有者の分類を差し替える。`meanwhile` を分類中の副作用として実行する。

    分類器は確かめたい層ではないが、**1 (検査) と 3 (破壊) の間で実際に時間を
    食っている層**である (guard 自身のコメントが「/proc の walk には本物の時間が
    かかる」と書いている)。だから入れ替えをここに差し込むのは作り物ではなく、
    指摘されている競合そのものの再現になる。
    """
    def owner(pane_pid, **kwargs):
        if meanwhile is not None:
            meanwhile()
        return lib_mux.OWNER_MINE, "stubbed: this checkout's own daemon"
    monkeypatch.setattr(lib_mux, "daemon_pane_owner", owner)


# ===========================================================================
# tmux — 破壊を、検査したサーバーに束縛する
# ===========================================================================

def test_tmux_kill_does_not_destroy_the_new_servers_window(
        monkeypatch, tmux_servers, as_our_checkout):
    """**Codex 8 巡目 P1 そのもの**。

    A の `@0` を検査して占有者を MINE と分類し、A の記録と一致させる。その
    分類の途中で A が落ち、同じソケットで B が起動する。B の `@0` は
    `bystander` — 一度も分類されていない、まったく別のペインである。

    束縛が無いと `kill-window -t @0` は B のそれを壊す。
    """
    pane = lib_mux._pane_name("dispatcher")
    a_ids = tmux_servers.start(pane, "keepalive")
    a_identity = tmux_servers.identity()
    assert a_ids[pane] == "@0", (
        f"前提が崩れている: A の dispatcher は @0 のはずが {a_ids[pane]}")

    # 記録は A の世代で書かれている (ふつうの spawn がそうする)。
    assert lib_mux.write_pane_record(
        "dispatcher", "tmux", a_ids[pane], server=a_identity)

    replaced = {}

    def replace_the_server():
        tmux_servers.stop()
        replaced.update(tmux_servers.start("bystander", "keepalive"))

    _owner_is_ours(monkeypatch, meanwhile=replace_the_server)

    killed = lib_mux.TmuxBackend().kill("dispatcher")

    assert replaced.get("bystander") == "@0", (
        f"前提が崩れている: B の bystander は @0 のはずが {replaced}")
    windows = tmux_servers.windows()
    assert windows.get("@0") == "bystander", (
        f"検査していないサーバーのペインが壊された: {windows}")
    assert killed is False, (
        "サーバーが入れ替わったのに kill が成功を報告した")


def test_tmux_kill_closes_the_pane_on_the_server_it_inspected(
        monkeypatch, tmux_servers, as_our_checkout):
    """**可用性**: 入れ替わっていなければ、これまで通り閉じる。

    本番の dispatcher / watchdog がここに乗っている。束縛を足したせいで
    ふつうの kill が落ちるようになったら、Worker の後始末が止まる。
    """
    pane = lib_mux._pane_name("dispatcher")
    ids = tmux_servers.start(pane, "keepalive")
    assert lib_mux.write_pane_record(
        "dispatcher", "tmux", ids[pane], server=tmux_servers.identity())

    _owner_is_ours(monkeypatch)

    assert lib_mux.TmuxBackend().kill("dispatcher") is True, \
        "入れ替わっていない自分のペインが閉じられなかった"
    windows = tmux_servers.windows()
    assert ids[pane] not in windows, f"ペインが残っている: {windows}"
    assert windows.get("@1") == "keepalive", \
        f"関係のないペインまで消えた: {windows}"
    assert not lib_mux.pane_record_path("dispatcher").exists(), \
        "閉じたペインの spawn 記録が残った"


def test_tmux_bound_destroy_refuses_a_replaced_server_on_its_own(
        tmux_servers):
    """束縛の実体を単体で固定する (guard を通さずに)。

    上の 2 本は経路を通すぶん、記録・分類・束縛のどこが効いたのかを 1 本では
    分けられない。ここは破壊の一手だけを、本物の入れ替えに当てる。
    """
    tmux_servers.start("bystander", "keepalive")
    a_identity = tmux_servers.identity()
    tmux_servers.stop()
    tmux_servers.start("bystander", "keepalive")

    backend = lib_mux.TmuxBackend()
    assert backend._destroy_window_on("dispatcher", "@0", a_identity) is False
    assert tmux_servers.windows().get("@0") == "bystander", \
        "別サーバーのペインが壊された"

    # 同じサーバーに束縛すれば通る (これが落ちると本番が止まる)。
    assert backend._destroy_window_on(
        "dispatcher", "@0", tmux_servers.identity()) is True
    assert "@0" not in tmux_servers.windows()


def test_tmux_bound_destroy_refuses_a_window_id_that_carries_a_command(
        tmux_servers):
    """id は tmux が**コマンドとして読み直す**文字列に入るので、形を要求する。

    束縛は `kill-window -t <id>` を 1 つの tmux コマンド文字列として渡す。
    `_inspect_pane_full()` が id に課しているのは「`@` で始まる空白なしの語」
    だけなので、`@0;kill-server` のような語はそこを素通りする。実機で確かめた
    ところ、それは **2 つ目のコマンドとして実行され、サーバーごと落ちた** —
    つまり束縛したサーバーの上で、検査していない破壊が走る。

    (世代のほうは同じ細工を試しても tmux 3.4 の書式パーサが弾き、比較は
    成立しなかった。数字だけを受けるのは実証された穴を塞ぐためではなく、
    tmux が出す形以外を splice せずに拒否するため。)
    """
    ids = tmux_servers.start("bystander", "keepalive")
    identity = tmux_servers.identity()

    assert lib_mux.TmuxBackend()._destroy_window_on(
        "dispatcher", f"{ids['bystander']};kill-server", identity) is False
    assert tmux_servers.windows() == {"@0": "bystander", "@1": "keepalive"}, \
        "id に紛れたコマンドが、束縛したサーバーの上で実行された"


def test_tmux_bound_destroy_refuses_a_generation_that_is_not_a_server_pid(
        tmux_servers, capsys):
    """世代が tmux の出す形でなければ、**こちら側で**止める。

    細工した世代は tmux 3.4 の書式パーサも弾くので、「破壊されなかったか」だけ
    を見ると、この検査を外しても緑のままになる (別の層が先に止めているため)。
    確かめたい層はこちらなので、拒否が**こちらから出た**ことまで見る。
    """
    tmux_servers.start("bystander", "keepalive")
    endpoint, _ = tmux_servers.identity()

    assert lib_mux.TmuxBackend()._destroy_window_on(
        "dispatcher", "@0", (endpoint, "900},#{==:1,1")) is False
    assert "is not an (endpoint, generation)" in capsys.readouterr().err, \
        "細工した世代が tmux まで渡された (止めたのはこちらではない)"
    assert tmux_servers.windows() == {"@0": "bystander", "@1": "keepalive"}


def test_tmux_kill_refuses_when_the_server_cannot_be_named(
        monkeypatch, tmp_path, as_our_checkout):
    """世代が分からなければ束縛できないので、破壊しない。

    `display-message` が `#{pid}` を展開しない古い tmux の場合。新形式の記録は
    このとき `_server_binding_problem()` が既に拒否するので、ここは**旧形式の
    記録**で来る。移行の緩和は server の照合を飛ばすので、「記録も占有者も
    通ったのに束縛する相手が居ない」という状態はこの経路にだけ残っていた。

    `--force` が出口として残っているので、永久に kill できない状態にはならない。
    """
    from test_spawn_record_binding import _RecordingTmux, _fake_proc

    _fake_proc(tmp_path, {4100: dict(ppid=1, argv=["bash"],
                                     cwd=str(tmp_path))})
    # 旧形式 = checkout も server も持たない記録。置き場所が共有されていない
    # かぎり出自は受け入れられ、handle は一致する。
    path = lib_mux.pane_record_path("dispatcher")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"handle": "@7", "tab_id": "@7",
                                "backend": "tmux"}), encoding="utf-8")
    rec = _RecordingTmux(pane_pid=4100, window_id="@7", server=None)
    monkeypatch.setattr(lib_mux, "subprocess", rec)
    _owner_is_ours(monkeypatch)

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=None)
    assert status == lib_mux.PANE_RECORD_MATCH, (
        f"前提が崩れている: 旧形式の記録が一致しない ({detail})")

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_targets() == [], \
        f"世代が分からないまま破壊された: {rec.kill_targets()}"


def test_tmux_kill_never_sends_an_unbound_kill_window(
        monkeypatch, tmp_path, as_our_checkout):
    """配線の固定: 保護された経路から素の `kill-window` が出ない。

    `_destroy_window_on()` を足しても `kill()` がそれを通らなければ意味がない。
    ここが緑のまま経路だけ戻される、という戻り方を塞ぐ。
    """
    from test_spawn_record_binding import _RecordingTmux, _fake_proc, SERVER

    _fake_proc(tmp_path, {4100: dict(ppid=1, argv=["bash"],
                                     cwd=str(tmp_path))})
    lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7", server=SERVER)
    monkeypatch.setattr(lib_mux, "subprocess", rec)
    _owner_is_ours(monkeypatch)

    assert lib_mux.TmuxBackend().kill("dispatcher") is True
    unbound = [c for c in rec.issued if "kill-window" in c]
    assert unbound == [], (
        f"検査したサーバーに束縛されていない kill-window が出た: {unbound}")
    assert rec.kill_targets() == ["@7"], (
        f"束縛された経路から破壊が実行されていない: {rec.calls}")
    bound = [c for c in rec.issued if "if-shell" in c]
    assert len(bound) == 1, f"束縛された破壊が 1 回ではない: {rec.issued}"
    assert SERVER[1] in " ".join(bound[0]), \
        f"検査した世代が破壊コマンドに入っていない: {bound[0]}"


# ===========================================================================
# herdr — 破壊要求を、名前を確かめた接続の上に流す
# ===========================================================================
#
# 本物の unix socket の上に本物の別プロセスを 2 世代立てる。SO_PEERCRED は
# カーネルが答えるので、世代 (pid:starttime) の違いは作り物ではない。

_FAKE_HERDR_SERVER = r'''
import json, os, socket, sys
sock_path, log_path = sys.argv[1], sys.argv[2]
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
        rid = req.get("id", "")
        if req.get("method") == "tab.close":
            with open(log_path, "a") as fh:
                fh.write(req.get("params", {}).get("tab_id", "") + "\n")
            body = {"id": rid, "result": {"type": "ok"}}
        elif req.get("method") == "ping":
            body = {"id": rid, "result": {"type": "pong"}}
        else:
            body = {"id": rid, "error": {"code": "unsupported_method"}}
        conn.sendall(json.dumps(body).encode() + b"\n")
'''


class _FakeHerdrServers:
    """同じソケットの上に順番に立つ偽 herdr サーバー群。"""

    def __init__(self, root: Path, monkeypatch):
        self.sock_path = root / "herdr.sock"
        self.log_path = root / "closed-tabs.log"
        self.script = root / "fake_herdr_server.py"
        self.script.write_text(_FAKE_HERDR_SERVER, encoding="utf-8")
        self.proc = None
        monkeypatch.setattr(lib_mux, "_HERDR_SOCK_PATH", self.sock_path)

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(self.script), str(self.sock_path),
             str(self.log_path)],
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

    def closed_tabs(self):
        try:
            return self.log_path.read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return []


@pytest.fixture
def herdr_servers(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="t43-herdr-", dir="/tmp"))
    servers = _FakeHerdrServers(root, monkeypatch)
    try:
        yield servers
    finally:
        servers.stop()
        shutil.rmtree(root, ignore_errors=True)


def test_herdr_bound_close_refuses_after_the_server_was_replaced(
        herdr_servers):
    """A の世代で束縛した `tab.close` が、B には届かない。

    CLI (`herdr tab close`) は自分で接続を張るので、検証とは別の接続になる。
    束縛された経路は、名前を確かめたその接続の上に要求を流す。
    """
    herdr_servers.start()
    a_identity = lib_mux._herdr_server_identity()
    assert a_identity is not None
    herdr_servers.stop()
    herdr_servers.start()
    b_identity = lib_mux._herdr_server_identity()
    assert b_identity is not None
    assert a_identity[1] != b_identity[1], \
        "前提が崩れている: 2 世代の generation が同じ"

    assert lib_mux._herdr_close_tab_bound("wP:t7W", a_identity) is False
    assert herdr_servers.closed_tabs() == [], \
        f"検査していないサーバーのタブが閉じられた: {herdr_servers.closed_tabs()}"


def test_herdr_bound_close_closes_on_the_inspected_server(herdr_servers):
    """**可用性**: 入れ替わっていなければ、これまで通り閉じる。"""
    herdr_servers.start()
    identity = lib_mux._herdr_server_identity()

    assert lib_mux._herdr_close_tab_bound("wP:t7W", identity) is True
    assert herdr_servers.closed_tabs() == ["wP:t7W"]


def test_herdr_close_rides_the_verified_connection(monkeypatch, herdr_servers):
    """**指摘の芯**: 送信の直前にもう 1 回確かめるだけでは閉じない。

    上の 2 本は「送る前に確かめている」ことしか示せない。Codex が閉じないと
    言っているのは **確かめてから送るまでの間** であり、そこは実時間では数
    マイクロ秒しかない。そこで、その窓をテスト側から開けて固定する:
    `_herdr_connect_identified()` が返ったあと、送信の前にサーバーを入れ替える。

    差し込むのは**既にある継ぎ目をテスト側で包むだけ**で、本番コードにテスト用の
    フックは足していない (memory: microsecond-race-fix-needs-structural-test)。

    要求が**検証した接続の上を流れる**なら、その接続は相手が死んで壊れている
    ので何も送れない。CLI のように張り直すなら、B がその要求を受け取る。
    """
    herdr_servers.start()
    a_identity = lib_mux._herdr_server_identity()
    real_connect = lib_mux._herdr_connect_identified

    def connect_then_replace_the_server():
        identified = real_connect()
        herdr_servers.stop()          # 検証は済んだ。ここで A が落ちる
        herdr_servers.start()         # 同じソケットに B が立つ
        return identified

    monkeypatch.setattr(lib_mux, "_herdr_connect_identified",
                        connect_then_replace_the_server)

    closed = lib_mux._herdr_close_tab_bound("wP:t7W", a_identity)

    assert herdr_servers.closed_tabs() == [], (
        f"検証した接続ではなく、張り直した接続に要求が流れた: "
        f"{herdr_servers.closed_tabs()}")
    assert closed is False, "何も閉じていないのに成功を報告した"


def test_herdr_kill_routes_through_the_bound_close(
        monkeypatch, herdr_servers, as_our_checkout):
    """配線の固定: `HerdrBackend.kill()` の保護された経路が束縛を通る。

    入れ替えは tmux 側と同じ差し込み口 — 占有者の分類の途中 — で起こす。
    """
    herdr_servers.start()
    a_identity = lib_mux._herdr_server_identity()
    assert lib_mux.write_pane_record(
        "dispatcher", "herdr", "wP:t7W", server=a_identity)

    backend = lib_mux.HerdrBackend()
    monkeypatch.setattr(
        backend, "_inspect_pane_full",
        lambda name: ("wP:t7W", 4100, a_identity))
    monkeypatch.setattr(backend, "_delete_cache", lambda name: None)

    def replace_the_server():
        herdr_servers.stop()
        herdr_servers.start()

    _owner_is_ours(monkeypatch, meanwhile=replace_the_server)

    assert backend.kill("dispatcher") is False
    assert herdr_servers.closed_tabs() == [], \
        f"検査していないサーバーのタブが閉じられた: {herdr_servers.closed_tabs()}"


def test_herdr_kill_closes_the_tab_on_the_inspected_server(
        monkeypatch, herdr_servers, as_our_checkout):
    """**可用性**: herdr でも、入れ替わっていなければ閉じる。"""
    herdr_servers.start()
    identity = lib_mux._herdr_server_identity()
    assert lib_mux.write_pane_record(
        "dispatcher", "herdr", "wP:t7W", server=identity)

    backend = lib_mux.HerdrBackend()
    monkeypatch.setattr(
        backend, "_inspect_pane_full",
        lambda name: ("wP:t7W", 4100, identity))
    deleted = []
    monkeypatch.setattr(backend, "_delete_cache", lambda name: deleted.append(name))

    _owner_is_ours(monkeypatch)

    assert backend.kill("dispatcher") is True
    assert herdr_servers.closed_tabs() == ["wP:t7W"]
    assert deleted == ["dispatcher"]
