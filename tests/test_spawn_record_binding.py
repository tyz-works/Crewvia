#!/usr/bin/env python3
"""tests/test_spawn_record_binding.py

**spawn 記録に出自と mux サーバー世代を持たせる** (t041 / PR #209, Codex 6 巡目)。

5 巡目 (t040) で破壊の根拠を「中の人の同定」から「自分が作った記録」に移した。
6 巡目の P1 4 件は、その**土台を否定していない** — 4 件とも
「**その記録が本当に自分のものだと証明できていない**」という詰めである。

ここで固定する契約:

  1. **空だと宣言する前に、root プロセスが idle なシェルであることを確認する**
     (`_pane_recognition` / `_settle_pane`)。
     `others` から pane_pid を無条件に除くと、root そのものが別チェックアウトの
     デーモンでも「ペインは空」になる。分類器がその argv を認識できなければ
     `NONE` — 記録が無くても破壊が認可される結論 — が出る。
     認識できない root プロセスは `UNKNOWN`。

  2. **記録を、書いたチェックアウトに束縛する** (`write_pane_record` /
     `pane_record_status`)。ファイル名を `_own_repo_root()` の下に置くだけでは、
     2 つのチェックアウトが `registry/mux` を symlink で共有していれば
     両方が同じ記録を自分のものとして受け入れる。

  3. **記録を mux サーバーのエンドポイントと世代にも束縛する**。
     tmux の `@window_id` は 1 つのサーバーの生存期間内でしか一意でない。
     別ソケット・別世代の同じ id は別の window である。

  4. **tmux の window id を作成コマンドの出力から取る** (`TmuxBackend.spawn`)。
     作成と `send-keys` のあとに名前で引き直すと、その間に別チェックアウトが
     対象を置き換え・rename していた場合、**他人の window を「自分が作った」と
     記録してしまう**。偽造された出自は後で破壊を認可する。

可用性 (これを壊すと本番が止まる):

  - t035 の husk 復活 — root が素の idle シェルで子が居ないペインは、
    これまで通り「確実に空」でなければならない。
  - 旧形式の記録 (出自を持たない) は、**共有されていない置き場所にある限り**
    受け入れる。共有ストレージでは拒否する。永久に kill できない状態は作らない。

    python3 -m pytest tests/test_spawn_record_binding.py -v
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_mux  # noqa: E402

from conftest import fake_tmux_if_shell  # noqa: E402


# ---------------------------------------------------------------------------
# 偽 /proc — root プロセスの姿まで指定できる版
# ---------------------------------------------------------------------------
#
# 既存の `_fake_proc` は stat を `(bash) S ... 0 0 0 ...` と決め打ちしていた。
# つまり**ペインの root がどんなプロセスかをモデル化していなかった** — P1-1 が
# 見えなかった理由そのもの。ここでは comm / tty / pgrp / tpgid / 子の有無まで
# 指定できるようにして、「root が別チェックアウトのデーモン」を書けるようにする。

def _fake_proc(tmp_path, entries, *, name="proc"):
    """`{pid: spec}` から偽 /proc を作る。

    spec は dict:
      ppid   親 pid
      argv   コマンドライン
      cwd    None にすると `/proc/<pid>/cwd` を作らない (= 読めない)
      comm   stat の括弧の中 (既定 "bash")
      state  R/S/D/T/Z (既定 "S")
      tty    tty_nr (既定 1234 = 端末がある)
      pgrp   プロセスグループ (既定 pid)
      tpgid  端末の前景グループ (既定 pgrp — すなわち「自分が前景」)
    """
    proc = tmp_path / name
    proc.mkdir(exist_ok=True)
    for pid, spec in entries.items():
        ppid = spec.get("ppid", 1)
        argv = spec.get("argv", ["bash"])
        cwd = spec.get("cwd")
        comm = spec.get("comm", "bash")
        state = spec.get("state", "S")
        tty = spec.get("tty", 1234)
        pgrp = spec.get("pgrp", pid)
        tpgid = spec.get("tpgid", pgrp)

        d = proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        # fields after the last ')': 0 state, 1 ppid, 2 pgrp, 3 session,
        # 4 tty_nr, 5 tpgid, ... 19 starttime
        tail = " ".join(["0"] * 14)
        (d / "stat").write_text(
            f"{pid} ({comm}) {state} {ppid} {pgrp} {pgrp} {tty} {tpgid} {tail}",
            encoding="utf-8")
        if cwd is not None:
            target = Path(cwd)
            target.mkdir(parents=True, exist_ok=True)
            link = d / "cwd"
            if not link.is_symlink():
                link.symlink_to(target)
    return str(proc)


def _checkout(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    (root / "scripts" / "dispatcher.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "scripts" / "watchdog.py").write_text("#\n", encoding="utf-8")
    return root


@pytest.fixture
def our_checkout(tmp_path):
    return _checkout(tmp_path / "ours")


@pytest.fixture
def their_checkout(tmp_path):
    return _checkout(tmp_path / "theirs")


@pytest.fixture
def as_our_checkout(monkeypatch, our_checkout):
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(our_checkout))
    return our_checkout


#: tmux サーバーの identity — (エンドポイント, 世代)。
SERVER = ("/tmp/tmux-1000/default", "900")
OTHER_GENERATION = ("/tmp/tmux-1000/default", "901")
OTHER_ENDPOINT = ("/tmp/tmux-1000/other", "900")


class _RecordingTmux:
    """tmux を実行せずに argv を記録する差し替え。

    答えるもの:
      - `display-message -p -t X '#{window_id} #{pane_pid} #{pid} #{socket_path}'`
      - `new-window -P -F '#{window_id}'` / `new-session -P -F '#{window_id}'`
        → **作成された window の id** (`created_id`)。名前で引き直したときの
        答え (`window_id`) とわざと変えられるようにしてある: P1-4 は
        「作成後に別チェックアウトが名前を奪った」状況そのものだから。
    """

    def __init__(self, pane_pid=None, window_id="@7", created_id=None,
                 server=SERVER, has_session=True, existing_windows=()):
        self.calls = []
        self.issued = []
        self.pane_pid = pane_pid
        self.window_id = window_id
        self.created_id = created_id
        self.server = server
        self.has_session = has_session
        self.existing_windows = list(existing_windows)

    def _expand_fmt(self, fmt):
        """tmux の書式文字列を、このサーバーの答えで置き換える。"""
        endpoint, generation = self.server if self.server else ("", "")
        for token, value in (
                ("#{window_id}", self.window_id),
                ("#{pane_pid}", self.pane_pid),
                ("#{pid}", generation),
                ("#{socket_path}", endpoint),
        ):
            fmt = fmt.replace(token, str(value))
        return fmt

    def _expand(self, argv):
        """要求された **書式そのもの** を展開して答える。

        固定の並びを返すと、書式が変わったときに古いコードでも新しいコードでも
        「たまたま」通ったり落ちたりする。ここが書式駆動でないと、赤の証明が
        本物かどうか分からない (実際、最初の版はこれで 1 件が偽の緑になった)。
        """
        return self._expand_fmt(argv[-1] if argv else "")

    def _answers(self):
        endpoint, generation = self.server if self.server else ("", "")
        return {"#{window_id}": self.window_id, "#{pane_pid}": self.pane_pid,
                "#{pid}": generation, "#{socket_path}": endpoint}

    def run(self, argv, _branch=False, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        # `issued` は **本番コードが自分で出した** 呼び出しだけ。if-shell の
        # then 側は tmux の中で走るので `calls` にしか入らない。この 2 つを
        # 混ぜると「素の kill-window を出していないか」を問えなくなる。
        if not _branch:
            self.issued.append(argv)
        text = kwargs.get("text", False)
        out = ""
        rc = 0
        if "if-shell" in argv:
            # 条件の解き方は conftest に 1 つだけ。then 側は同じ run() に流して
            # 記録させるので、kill_targets() はこの経路も数える。
            then_argv, out = fake_tmux_if_shell(argv, self._answers())
            if then_argv is not None:
                self.run(then_argv, _branch=True, text=text)
        elif "display-message" in argv:
            if self.pane_pid is None:
                rc = 1
            else:
                out = self._expand(argv) + "\n"
        elif "has-session" in argv:
            rc = 0 if self.has_session else 1
        elif "list-windows" in argv:
            out = "".join(f"{w}\n" for w in self.existing_windows)
        elif "new-window" in argv or "new-session" in argv:
            # `-P -F '#{window_id}'` を付けたときだけ答える。付けずに呼べば
            # tmux は何も出力しない — 欠陥版のコードが見ているのはその状態。
            if "-P" in argv and "-F" in argv and self.created_id:
                out = str(self.created_id) + "\n"
        return subprocess.CompletedProcess(
            argv, rc,
            stdout=out if text else out.encode(),
            stderr="" if text else b"")

    def __getattr__(self, item):
        return getattr(subprocess, item)

    def kill_targets(self):
        return [c[c.index("-t") + 1] for c in self.calls
                if "kill-window" in c and "-t" in c]

    def send_targets(self):
        return [c[c.index("-t") + 1] for c in self.calls
                if "send-keys" in c and "-t" in c]


# ===========================================================================
# P1-1  空だと宣言する前に root が idle なシェルであることを確認する
# ===========================================================================

def test_pane_rooted_at_an_unrecognised_daemon_is_not_empty(
        monkeypatch, tmp_path, as_our_checkout):
    """root プロセスが別チェックアウトのデーモンなら、ペインは空ではない。

    Codex 6 巡目 P1-1。`_pane_recognition` は `others` から `pane_pid` を
    **無条件に**除いていた。シェルが解決できない別名経由でデーモンを exec して
    いると `script_owner()` は何も認識できず (`NONE`)、子プロセスも無いので
    `others` は空 — 結果は「確実に空」。記録が 1 つも無くても破壊が認可される。

    ここでの root は、実在するがどのデーモンスクリプトでもないファイルを
    exec しており、comm はシェルではない。つまり **argv からは何も分からないが、
    シェルでないことは分かる**。それが `UNKNOWN` の根拠。
    """
    alien = tmp_path / "theirs" / "bin" / "mon"
    alien.parent.mkdir(parents=True, exist_ok=True)
    alien.write_text("#!/bin/sh\n", encoding="utf-8")

    proc_root = _fake_proc(tmp_path, {
        4100: dict(ppid=1, argv=[str(alien)], cwd=str(tmp_path), comm="mon"),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)

    owner, detail = lib_mux.daemon_pane_owner(4100, proc_root=proc_root)
    assert owner == lib_mux.OWNER_UNKNOWN, (
        f"root プロセスがシェルでないのにペインが空と判定された: {owner} / {detail}")


def test_pane_rooted_at_an_unrecognised_daemon_is_not_destroyed(
        monkeypatch, tmp_path, as_our_checkout):
    """同じ状況で、実際に kill が止まること (経路まで通す)。

    記録は無い。したがって破壊を認可しうる唯一の根拠は「確実に空」だけ。
    """
    alien = tmp_path / "theirs" / "bin" / "mon"
    alien.parent.mkdir(parents=True, exist_ok=True)
    alien.write_text("#!/bin/sh\n", encoding="utf-8")

    proc_root = _fake_proc(tmp_path, {
        4100: dict(ppid=1, argv=[str(alien)], cwd=str(tmp_path), comm="mon"),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_targets() == [], \
        f"root が別チェックアウトのデーモンのペインが壊された: {rec.kill_targets()}"


def test_pane_whose_root_cannot_be_read_is_unknown(
        monkeypatch, tmp_path, as_our_checkout):
    """root の状態が読めないペインも空ではない (端末を持たない = 我々の知る
    ペインシェルではない)。"""
    proc_root = _fake_proc(tmp_path, {
        4100: dict(ppid=1, argv=["bash"], cwd=str(tmp_path), tty=0),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)

    owner, detail = lib_mux.daemon_pane_owner(4100, proc_root=proc_root)
    assert owner == lib_mux.OWNER_UNKNOWN, (
        f"root の状態が読めないのにペインが空と判定された: {owner} / {detail}")


def test_husk_with_an_idle_shell_root_is_still_provably_empty(
        monkeypatch, tmp_path, as_our_checkout):
    """**可用性**: 素の idle シェルだけの husk はこれまで通り「確実に空」。

    t035 の復活経路がこれに乗っている。1 の修正でここが `UNKNOWN` に倒れると、
    herdr 再起動後の husk が二度と片付けられなくなる。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: dict(ppid=1, argv=["-bash"], cwd=str(tmp_path)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)

    owner, detail = lib_mux.daemon_pane_owner(4100, proc_root=proc_root)
    assert owner == lib_mux.OWNER_NONE, (
        f"素の idle シェルだけの husk が空と認められない: {owner} / {detail}")


# ===========================================================================
# P1-2  記録を、書いたチェックアウトに束縛する
# ===========================================================================

def _share_record_dir(shared: Path, *checkouts: Path) -> None:
    """`registry/mux` を symlink で共有させる (Codex P1-2 の前提そのもの)。"""
    shared.mkdir(parents=True, exist_ok=True)
    for root in checkouts:
        (root / "registry").mkdir(parents=True, exist_ok=True)
        link = root / "registry" / "mux"
        if not link.exists():
            link.symlink_to(shared)


def test_another_checkouts_record_is_not_ours(
        monkeypatch, tmp_path, our_checkout, their_checkout):
    """symlink で `registry/mux` を共有していても、他人の記録は他人のもの。

    Codex 6 巡目 P1-2。`pane_record_status` は backend と handle しか見て
    いなかった。ファイル名を `_own_repo_root()` の下に置くのは**置き場所**の
    隔離であって、**中身の出自**ではない。共有ストレージでは置き場所の隔離が
    そもそも成立しない。
    """
    _share_record_dir(tmp_path / "shared-mux", our_checkout, their_checkout)

    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(their_checkout))
    assert lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)

    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(our_checkout))
    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MISMATCH, (
        f"別チェックアウトが書いた記録を自分のものとして受け入れた: {detail}")


def test_our_own_record_still_matches_in_shared_storage(
        monkeypatch, tmp_path, our_checkout, their_checkout):
    """**可用性**: 共有していても、自分が書いた記録は自分のものとして通る。"""
    _share_record_dir(tmp_path / "shared-mux", our_checkout, their_checkout)

    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(our_checkout))
    assert lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MATCH, detail


def test_a_copied_record_does_not_become_ours(
        monkeypatch, tmp_path, our_checkout, their_checkout):
    """記録をそのままコピーしても、出自は書き換わらない。"""
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(their_checkout))
    lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)
    theirs = lib_mux.pane_record_path("dispatcher").read_text(encoding="utf-8")

    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(our_checkout))
    ours = lib_mux.pane_record_path("dispatcher")
    ours.parent.mkdir(parents=True, exist_ok=True)
    ours.write_text(theirs, encoding="utf-8")

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MISMATCH, (
        f"コピーされた記録を自分のものとして受け入れた: {detail}")


def test_legacy_record_in_shared_storage_is_refused(
        monkeypatch, tmp_path, our_checkout, their_checkout):
    """出自を持たない旧形式の記録は、共有ストレージでは拒否する。

    どちらのチェックアウトが書いたのか決めようがないため。
    """
    _share_record_dir(tmp_path / "shared-mux", our_checkout, their_checkout)
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(our_checkout))

    path = lib_mux.pane_record_path("dispatcher")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "handle": "@7", "tab_id": "@7", "pane_id": "", "backend": "tmux",
        "created_at": "2026-09-20T00:00:00Z",
    }), encoding="utf-8")

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MISMATCH, (
        f"共有ストレージの出自不明な記録が通った: {detail}")


def test_legacy_record_in_private_storage_still_works(
        monkeypatch, tmp_path, as_our_checkout, capsys):
    """**可用性 / 移行**: 共有されていない置き場所の旧形式の記録は通す。

    この変更より前に spawn された本番の dispatcher / watchdog の記録には
    出自もサーバー世代も入っていない。そこで一律に拒否すると、**稼働中の
    デーモンが二度と kill できなくなる** — 黙って作ってはいけない状態。
    置き場所が共有されていなければ、`_own_repo_root()` の下にあること自体が
    出自の (弱いが有効な) 証拠なので受け入れ、代わりに**必ず警告する**。
    """
    path = lib_mux.pane_record_path("dispatcher")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "handle": "@7", "tab_id": "@7", "pane_id": "", "backend": "tmux",
        "created_at": "2026-09-20T00:00:00Z",
    }), encoding="utf-8")

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MATCH, detail
    assert "legacy" in capsys.readouterr().err.lower(), \
        "旧形式の記録を黙って受け入れている"


# ===========================================================================
# P1-3  記録を mux サーバーのエンドポイントと世代に束縛する
# ===========================================================================

def test_record_from_a_previous_server_generation_does_not_match(
        monkeypatch, tmp_path, as_our_checkout):
    """tmux `@window_id` は 1 サーバーの生存期間内でしか一意でない。

    Codex 6 巡目 P1-3。サーバーが再起動すれば同じ `@7` が別の window に
    割り当てられる。世代が変わった記録は、その `@7` について何も言っていない。
    """
    lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=OTHER_GENERATION)
    assert status == lib_mux.PANE_RECORD_MISMATCH, (
        f"別世代のサーバーの記録が通った: {detail}")


def test_record_from_another_socket_does_not_match(
        monkeypatch, tmp_path, as_our_checkout):
    """別のソケットにも同じ `@7` は存在しうる。"""
    lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=OTHER_ENDPOINT)
    assert status == lib_mux.PANE_RECORD_MISMATCH, (
        f"別ソケットのサーバーの記録が通った: {detail}")


def test_same_server_still_matches(monkeypatch, tmp_path, as_our_checkout):
    """**可用性**: 同じサーバーの同じ世代なら、これまで通り通る。"""
    lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)
    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MATCH, detail


def test_record_with_server_is_refused_when_current_server_is_unknown(
        monkeypatch, tmp_path, as_our_checkout):
    """サーバーの identity が読めないときは、照合できない = 通さない。

    `--force` という出口があるので、ここは閉じる方に倒してよい。
    """
    lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)
    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=None)
    assert status == lib_mux.PANE_RECORD_MISMATCH, (
        f"現在のサーバーが不明なのに記録が通った: {detail}")


def test_stale_generation_does_not_authorise_a_kill(
        monkeypatch, tmp_path, as_our_checkout):
    """経路まで: 世代が違えば、分類器が UNKNOWN でも kill は止まる。"""
    proc_root = _fake_proc(tmp_path, {
        4100: dict(ppid=1, argv=["bash"], cwd=str(tmp_path)),
        4200: dict(ppid=4100, argv=["/opt/unknown/thing"], cwd=None, comm="thing"),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    # 記録は古い世代のサーバーのもの。今 tmux が名乗るのは新しい世代。
    lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7",
                         server=OTHER_GENERATION)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_targets() == [], \
        f"別世代のサーバーの記録で window が壊された: {rec.kill_targets()}"


def test_current_generation_authorises_a_kill(
        monkeypatch, tmp_path, as_our_checkout):
    """**可用性**: 同じ世代なら、これまで通り閉じられる。"""
    proc_root = _fake_proc(tmp_path, {
        4100: dict(ppid=1, argv=["bash"], cwd=str(tmp_path)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    lib_mux.write_pane_record("dispatcher", "tmux", "@7", server=SERVER)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7", server=SERVER)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher") is True
    assert rec.kill_targets() == ["@7"]


# ===========================================================================
# P1-4  window id は作成コマンドの出力から取る
# ===========================================================================

def test_spawn_records_the_window_it_created_not_the_name(
        monkeypatch, tmp_path, as_our_checkout):
    """作成した window の id を記録する — 名前で引き直さない。

    Codex 6 巡目 P1-4。`new-window` と `_record_spawn()` の間に別チェックアウトが
    同名の window を作れば、名前で引き直した `display-message` は**他人の
    window** を答える。それを「自分が作った」と記録すると、出自が偽造される。

    ここでは `new-window` が `@11` を返し、そのあと名前を引くと `@99` (他人の
    window) が返る状況を作る。記録されるべきは `@11`。
    """
    rec = _RecordingTmux(pane_pid=4100, window_id="@99", created_id="@11",
                         has_session=True, existing_windows=[])
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().spawn("dispatcher", "echo hi") is True

    record = lib_mux.read_pane_record("dispatcher")
    assert record is not None, "spawn 記録が書かれていない"
    assert record.get("handle") == "@11", (
        f"作成した window ではなく名前で引き直した結果が記録された: {record}")


def test_spawn_sends_to_the_window_it_created(
        monkeypatch, tmp_path, as_our_checkout):
    """起動コマンドも、作成した window の id に送る。

    記録だけ直して送信先が名前のままなら、**他人の window でコマンドが走る** —
    偽造された出自より直接的な被害。
    """
    rec = _RecordingTmux(pane_pid=4100, window_id="@99", created_id="@11",
                         has_session=True, existing_windows=[])
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().spawn("dispatcher", "echo hi") is True
    assert rec.send_targets(), "send-keys が呼ばれていない"
    assert set(rec.send_targets()) == {"@11"}, (
        f"作成した window 以外に送っている: {rec.send_targets()}")


def test_spawn_refuses_when_tmux_will_not_say_what_it_created(
        monkeypatch, tmp_path, as_our_checkout):
    """作成した id が取れなければ、その window は使わない。

    「名前で引き直す」に戻るのが一番危ない退避先なので、そこへは戻らない。
    """
    rec = _RecordingTmux(pane_pid=4100, window_id="@99", created_id=None,
                         has_session=True, existing_windows=[])
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().spawn("dispatcher", "echo hi") is False
    assert lib_mux.read_pane_record("dispatcher") is None, \
        "作成した id が取れないのに記録が書かれた"
    assert rec.send_targets() == [], \
        f"作成した id が取れないのにコマンドを送った: {rec.send_targets()}"


def test_husk_relaunch_keeps_the_inspected_window_id(
        monkeypatch, tmp_path, as_our_checkout):
    """husk への再投入も、**検査した** window の id を持ち回る。

    再利用経路では `new-window` を呼ばないので、id の出どころは
    `_inspect_pane()` の検査結果。そこから名前に戻ってはいけない。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: dict(ppid=1, argv=["-bash"], cwd=str(tmp_path)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    monkeypatch.setenv("CREWVIA_MUX_LAUNCH_VERIFY_SECONDS", "0")

    window = lib_mux._pane_name("dispatcher")
    rec = _RecordingTmux(pane_pid=4100, window_id="@5",
                         has_session=True, existing_windows=[window])
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    lib_mux.TmuxBackend().spawn("dispatcher", "echo hi")

    assert rec.send_targets(), "husk へ何も送っていない"
    assert set(rec.send_targets()) == {"@5"}, (
        f"検査した window 以外に送っている: {rec.send_targets()}")
    record = lib_mux.read_pane_record("dispatcher")
    assert record is not None and record.get("handle") == "@5", (
        f"検査した window の id が記録されていない: {record}")


def test_spawn_record_carries_checkout_and_server(
        monkeypatch, tmp_path, as_our_checkout):
    """spawn が書く記録には、出自とサーバー identity が両方入る。

    1 件でも欠けると、後の照合が旧形式扱いにフォールバックしてしまう。
    """
    rec = _RecordingTmux(pane_pid=4100, window_id="@11", created_id="@11",
                         server=SERVER, has_session=True, existing_windows=[])
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().spawn("dispatcher", "echo hi") is True
    record = lib_mux.read_pane_record("dispatcher")
    assert record.get("checkout"), f"出自が記録されていない: {record}"
    assert record.get("server", {}).get("endpoint") == SERVER[0], record
    assert record.get("server", {}).get("generation") == SERVER[1], record
