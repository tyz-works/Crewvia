#!/usr/bin/env python3
"""tests/test_destruction_and_condition.py

**破壊の条件を「記録 AND 占有者」の両方成立に変える** (t042 / PR #209,
Codex 7 巡目)。

7 巡目の P1 5 件は、6 巡目までに積んだ「自分が書いた記録を破壊の根拠にする」
という土台そのものを否定していない。5 件とも **「その記録が偽造・再利用され
うる」** という一点を別々の角度から突いている:

  3. 作成と `server_identity()` の間にサーバーが入れ替わると、記録は A の
     window id と B の世代を組み合わせて書かれる
  4. 検証と `kill-window` の間にサーバーが再起動すると、同じ id が別の
     window を指す
  5. tmux の世代は素の pid なので、pid が回ってくれば古い記録と一致する

これらを「記録を偽造不可能にする」方向で潰すと boot id やプロセス開始時刻を
持ち込む話になり、複雑さに対して得られる安全が見合わない。指摘されている
攻撃はいずれも

    「記録が (古い/偽造で) 一致する」 **かつ** 「占有者が UNKNOWN」

の組み合わせで初めて成立する。そこで条件そのものを変える:

    破壊は「記録が一致する **AND** 占有者が MINE か確実に空」でのみ通る。

これまでは記録が一致すれば占有者が UNKNOWN でも通っていた (実質 OR)。
AND にすれば、古い記録・共有された記録・世代が再利用された記録があっても、
**占有者を確認できない限り破壊しない**。

AND 条件と独立に必要な 2 件も、ここで固定する:

  1. **旧形式の受け入れが handle の比較を飛ばしていた** (Codex 7 巡目 P1-1)。
     移行の緩和は「出自の証拠が弱い」ことへの緩和であって、
     **「どの pane でもよい」ことへの緩和ではない**。`@7` の旧形式の記録が
     `@99` を認可してはならない。

  2. **server の束縛が欠けていると検証ごと飛んでいた** (P1-2)。
     `server_identity()` が失敗したときに `_record_spawn()` は
     「以後の kill は拒否される」と警告しつつ server 無しの記録を書いていた。
     その記録は別エンドポイント・別世代でも一致する。新形式の記録には
     完全な server 束縛を要求し、同定に失敗したときは**記録を書かない**。

可用性 (これを壊すと本番が止まる):

  - **本番の dispatcher / watchdog は占有者が MINE** と判定される。AND に
    しても、動いているデーモンのペインはこれまで通り閉じられなければならない。
  - **t035 の husk 復活** — 記録が無くても「確実に空」なペインは片付く。
  - **旧形式の記録** は、共有されていない置き場所にあり、かつ **handle が
    一致する限り**、これまで通り通る。本番の記録は旧形式である。
  - 永久に kill できない状態を黙って作らない。`--force` の出口は維持する。

    python3 -m pytest tests/test_destruction_and_condition.py -v
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

# 6 巡目の偽 /proc とチェックアウトの組み立ては同じものを使う。片方だけ直すと
# 「同じ状況」のつもりで違う状況を試すことになる。
from test_spawn_record_binding import (  # noqa: E402
    SERVER,
    OTHER_GENERATION,
    _RecordingTmux,
    _checkout,
    _fake_proc,
)


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


# ---------------------------------------------------------------------------
# ペインの中身を 4 通り作り分ける
# ---------------------------------------------------------------------------
#
# `may_destroy_pane()` の入力は「記録」と「占有者」の 2 つ。AND にするという
# のは後者が効くようにするということなので、後者を 4 通りとも書けないと
# 何も確かめられない。

PANE_PID = 4100


def _pane_with_our_daemon(tmp_path, checkout, monkeypatch):
    """占有者 = MINE。自分のチェックアウトの dispatcher が動いている。"""
    proc_root = _fake_proc(tmp_path, {
        PANE_PID: dict(ppid=1, argv=["bash"], cwd=str(tmp_path)),
        PANE_PID + 1: dict(
            ppid=PANE_PID,
            argv=["bash", str(Path(checkout) / "scripts" / "dispatcher.sh")],
            cwd=str(checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    return proc_root


def _pane_with_a_foreign_daemon(tmp_path, other, monkeypatch):
    """占有者 = FOREIGN。別チェックアウトの dispatcher が動いている。"""
    proc_root = _fake_proc(tmp_path, {
        PANE_PID: dict(ppid=1, argv=["bash"], cwd=str(tmp_path)),
        PANE_PID + 1: dict(
            ppid=PANE_PID,
            argv=["bash", str(Path(other) / "scripts" / "dispatcher.sh")],
            cwd=str(other)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    return proc_root


def _pane_with_something_unreadable(tmp_path, monkeypatch):
    """占有者 = UNKNOWN。中で何かが動いているが、それが何かは分からない。

    これが 7 巡目の 5 件すべてに共通する前提。`cwd` が読めず argv も解決
    できないので、分類器は「自分のではない」とも「空だ」とも言えない。
    """
    proc_root = _fake_proc(tmp_path, {
        PANE_PID: dict(ppid=1, argv=["bash"], cwd=str(tmp_path)),
        PANE_PID + 1: dict(ppid=PANE_PID, argv=["/opt/unknown/thing"],
                           cwd=None, comm="thing"),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    return proc_root


def _pane_that_is_empty(tmp_path, monkeypatch):
    """占有者 = NONE。素の idle シェルだけ — t035 の husk。"""
    proc_root = _fake_proc(tmp_path, {
        PANE_PID: dict(ppid=1, argv=["bash"], cwd=str(tmp_path)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    return proc_root


def _write_matching_record(name="dispatcher", handle="@7", server=SERVER):
    assert lib_mux.write_pane_record(name, "tmux", handle, server=server)


def _may_destroy(name="dispatcher", handle="@7", server=SERVER,
                 pane_pid=PANE_PID):
    return lib_mux.may_destroy_pane(name, "tmux", handle, pane_pid,
                                    server=server)


# ===========================================================================
# AND 条件 — 記録が一致しても、占有者が確認できなければ破壊しない
# ===========================================================================

def test_matching_record_does_not_authorise_an_unidentifiable_occupant(
        monkeypatch, tmp_path, as_our_checkout):
    """7 巡目の核。**記録が完全に一致していても** 中身が読めなければ止まる。

    ここが赤いままだと、指摘 3・4・5 が描く「古い/偽造された記録が一致する」
    経路はすべてそのまま破壊に届く。逆にここが緑なら、記録が一致するという
    ことだけでは誰も殺せない。
    """
    _pane_with_something_unreadable(tmp_path, monkeypatch)
    _write_matching_record()

    allowed, reason = _may_destroy()
    assert allowed is False, (
        f"記録が一致しただけで、中身が読めないペインの破壊が認可された: {reason}")
    assert "unknown" in reason.lower(), reason


def test_matching_record_still_authorises_our_own_running_daemon(
        monkeypatch, tmp_path, as_our_checkout):
    """**可用性の本丸**: 本番の dispatcher / watchdog はこの形をしている。

    記録が一致し、中で動いているのが自分のチェックアウトのデーモン (MINE)。
    AND にしてここが赤くなるなら、稼働中のデーモンが二度と閉じられない —
    それは別の障害であって安全ではない。
    """
    _pane_with_our_daemon(tmp_path, as_our_checkout, monkeypatch)
    _write_matching_record()

    allowed, reason = _may_destroy()
    assert allowed is True, (
        f"自分のデーモンが動いている自分のペインが閉じられない: {reason}")


def test_matching_record_still_authorises_an_empty_pane(
        monkeypatch, tmp_path, as_our_checkout):
    """記録が一致し、かつ確実に空。AND の「空」側。"""
    _pane_that_is_empty(tmp_path, monkeypatch)
    _write_matching_record()

    allowed, reason = _may_destroy()
    assert allowed is True, reason


def test_an_empty_pane_is_still_cleared_without_any_record(
        monkeypatch, tmp_path, as_our_checkout):
    """**t035 の生存性**: herdr 再起動後の husk は記録が無くても片付く。

    AND を「記録 AND 占有者」と読んで記録を必須にすると、再起動で全記録が
    陳腐化したあと husk が永久に残る。空であることは、それ自体が
    「壊しても何も失われない」という独立した証明。
    """
    _pane_that_is_empty(tmp_path, monkeypatch)
    # 記録は一切書かない。
    allowed, reason = _may_destroy()
    assert allowed is True, (
        f"確実に空のペインが片付けられない (t035 の生存性): {reason}")


def test_a_foreign_daemon_still_vetoes_a_matching_record(
        monkeypatch, tmp_path, as_our_checkout, their_checkout):
    """veto は AND の前段として残る。記録があっても他人のデーモンは殺さない。"""
    _pane_with_a_foreign_daemon(tmp_path, their_checkout, monkeypatch)
    _write_matching_record()

    allowed, reason = _may_destroy()
    assert allowed is False, reason


def test_kill_refuses_when_the_record_matches_but_the_occupant_is_unknown(
        monkeypatch, tmp_path, as_our_checkout):
    """経路まで: `TmuxBackend.kill()` が実際に `kill-window` を出さない。

    `may_destroy_pane()` が False を返すだけでは足りない。破壊の実行者が
    それを見ているかどうかが実害の分かれ目。
    """
    _pane_with_something_unreadable(tmp_path, monkeypatch)
    _write_matching_record()
    rec = _RecordingTmux(pane_pid=PANE_PID, window_id="@7", server=SERVER)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_targets() == [], (
        f"中身が読めないのに window が壊された: {rec.kill_targets()}")


def test_force_still_gets_past_an_unidentifiable_occupant(
        monkeypatch, tmp_path, as_our_checkout):
    """**出口**: AND で拒否が増えるぶん、`--force` は効き続けなければならない。

    「判断できない」を永久の拒否にしないための唯一の抜け道
    (memory: fail-closed-discard-vs-hold)。
    """
    _pane_with_something_unreadable(tmp_path, monkeypatch)
    _write_matching_record()
    rec = _RecordingTmux(pane_pid=PANE_PID, window_id="@7", server=SERVER)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher", allow_foreign=True) is True
    assert rec.kill_targets(), "--force でも閉じられない"


# ===========================================================================
# P1-1  旧形式の受け入れは handle の比較を飛ばしてはならない
# ===========================================================================

def _write_legacy_record(name="dispatcher", handle="@7"):
    """出自も server も持たない、この変更より前の形式の記録。"""
    path = lib_mux.pane_record_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "handle": handle, "tab_id": handle, "pane_id": "", "backend": "tmux",
        "created_at": "2026-09-20T00:00:00Z",
    }), encoding="utf-8")
    return path


def test_a_legacy_record_does_not_authorise_a_different_pane(
        monkeypatch, tmp_path, as_our_checkout):
    """Codex 7 巡目 P1-1。`@7` の旧形式の記録が `@99` を認可してはならない。

    移行の緩和が緩めてよいのは「この記録を書いたのが自分だという証拠の強さ」
    だけ。**どの pane についての記録なのか** は緩めるところではない。
    緩めた瞬間、記録は「無制限の認可」になる。
    """
    _write_legacy_record(handle="@7")

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@99", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MISMATCH, (
        f"@7 の旧形式の記録が @99 を認可した: {detail}")


def test_a_legacy_record_still_matches_its_own_pane(
        monkeypatch, tmp_path, as_our_checkout, capsys):
    """**可用性 / 移行**: 本番の記録は旧形式。同じ pane なら通り続ける。

    `registry/mux/dispatcher.json` は `tab_id` だけを持つ旧形式で、稼働中の
    デーモンがそれに依存している。ここが赤くなると本番が閉じられなくなる。
    """
    _write_legacy_record(handle="@7")

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MATCH, (
        f"旧形式の記録が自分の pane を認可しない: {detail}")
    assert "legacy" in capsys.readouterr().err.lower(), \
        "旧形式の記録を黙って受け入れている"


def test_a_legacy_record_is_accepted_without_a_server_identity(
        monkeypatch, tmp_path, as_our_checkout):
    """**可用性**: 旧形式は server を持たないので、server の要求は課さない。

    P1-2 の「完全な server 束縛を要求する」を旧形式にも適用すると、稼働中の
    デーモンが全部 kill 不能になる。要求するのは新形式の記録だけ。
    """
    _write_legacy_record(handle="@7")

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=None)
    assert status == lib_mux.PANE_RECORD_MATCH, detail


def test_kill_refuses_when_a_legacy_record_names_another_window(
        monkeypatch, tmp_path, as_our_checkout):
    """経路まで: 旧形式の記録で別の window が壊されない。

    Codex はこれを「同じサーバーの生存期間内でも、置き換わった pane を
    古い記録が認可する」と実証している。

    中身は **MINE** にしてある。空にすると「確実に空」の方で独立に認可されて
    しまい、handle を比較しているかどうかが見えない (最初の版はこれで偽の赤に
    なった)。MINE なら通り道は記録しか残らないので、止まったとすれば理由は
    handle の比較だけになる。
    """
    _pane_with_our_daemon(tmp_path, as_our_checkout, monkeypatch)
    _write_legacy_record(handle="@7")
    # 名前の下にあるのは、記録とは別の window。
    rec = _RecordingTmux(pane_pid=PANE_PID, window_id="@99", server=SERVER)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    lib_mux.TmuxBackend().kill("dispatcher")
    assert rec.kill_targets() != ["@99"], (
        "@7 の旧形式の記録で @99 が壊された")


# ===========================================================================
# P1-2  server の束縛が欠けている新形式の記録は fail open してはならない
# ===========================================================================

def _write_record_with_raw_server(server_field, name="dispatcher",
                                  handle="@7"):
    """出自は持つが `server` が壊れている/欠けている新形式の記録。

    `server_identity()` が失敗したときに `_record_spawn()` が実際に書いて
    いたのがこの形 (`server` キーごと無い)。通常の spawn で到達する。
    """
    path = lib_mux.pane_record_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "handle": handle, "tab_id": handle, "pane_id": "", "backend": "tmux",
        "checkout": lib_mux._record_checkout_identity(),
        "created_at": "2026-09-22T00:00:00Z",
    }
    if server_field is not _ABSENT:
        record["server"] = server_field
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


_ABSENT = object()


@pytest.mark.parametrize("server_field, label", [
    (_ABSENT, "server キーが無い"),
    (None, "server が null"),
    ("/tmp/tmux-1000/default", "server が辞書でない"),
    ({"endpoint": "/tmp/tmux-1000/default"}, "generation が無い"),
    ({"generation": "900"}, "endpoint が無い"),
    ({"endpoint": "", "generation": ""}, "endpoint も generation も空"),
])
def test_a_new_format_record_without_a_complete_server_binding_is_refused(
        monkeypatch, tmp_path, as_our_checkout, server_field, label):
    """Codex 7 巡目 P1-2。server が無ければ検証を飛ばす、ではなく、拒否する。

    束縛が欠けた記録は「どのサーバーの `@7` か」を何も言っていない。
    それを「照合する対象が無いので通す」と読むのが fail open であり、
    別エンドポイント・別世代の `@7` にそのまま一致してしまう。
    """
    _write_record_with_raw_server(server_field)

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MISMATCH, (
        f"{label} 新形式の記録が通った: {detail}")


def test_a_server_less_record_does_not_match_another_generation(
        monkeypatch, tmp_path, as_our_checkout):
    """Codex が実証した形そのもの: 束縛の無い記録は別世代でも一致していた。"""
    _write_record_with_raw_server(_ABSENT)

    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=OTHER_GENERATION)
    assert status == lib_mux.PANE_RECORD_MISMATCH, detail


def test_no_record_is_written_when_the_server_cannot_be_identified(
        monkeypatch, tmp_path, as_our_checkout, capsys):
    """同定に失敗したら**書かない**。後で拒否される記録を残す意味が無い。

    これまでは「以後の kill は拒否される」と警告しつつ書いていた。実際には
    拒否されず、束縛の無い記録として通っていた (上のテスト)。拒否するように
    直すなら、そもそも書かないのが筋が通る。
    """
    assert lib_mux.write_pane_record(
        "dispatcher", "tmux", "@7", server=None) is False
    assert not lib_mux.pane_record_path("dispatcher").exists(), \
        "サーバーが同定できないのに記録が書かれた"
    assert "server" in capsys.readouterr().err.lower(), \
        "記録を書かなかったことが黙って起きている"


def test_a_failed_server_identity_drops_the_previous_record(
        monkeypatch, tmp_path, as_our_checkout):
    """書かないだけでは足りない — 前の記録が残れば新しい pane を認可しうる。

    同じ名前で spawn し直したのに記録が更新されないと、古い記録が
    「この名前の pane は自分のものだ」と言い続ける。
    """
    _write_matching_record(handle="@7")
    assert lib_mux.pane_record_path("dispatcher").exists()

    lib_mux.write_pane_record("dispatcher", "tmux", "@8", server=None)
    assert not lib_mux.pane_record_path("dispatcher").exists(), \
        "サーバーが同定できないまま、古い記録が残った"


def test_a_complete_record_is_still_written_and_still_matches(
        monkeypatch, tmp_path, as_our_checkout):
    """**可用性**: サーバーが分かる通常の spawn はこれまで通り記録される。"""
    assert lib_mux.write_pane_record(
        "dispatcher", "tmux", "@7", server=SERVER) is True
    status, detail = lib_mux.pane_record_status(
        "dispatcher", "tmux", "@7", server=SERVER)
    assert status == lib_mux.PANE_RECORD_MATCH, detail


def test_spawn_does_not_record_when_tmux_will_not_name_its_server(
        monkeypatch, tmp_path, as_our_checkout):
    """経路まで: `server_identity()` が答えない tmux で spawn しても書かない。"""
    _fake_proc(tmp_path, {PANE_PID: dict(ppid=1, argv=["bash"],
                                         cwd=str(tmp_path))})
    rec = _RecordingTmux(pane_pid=None, window_id="@7", created_id="@7",
                         server=None, has_session=True)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    lib_mux.TmuxBackend().spawn("dispatcher", "true")
    assert not lib_mux.pane_record_path("dispatcher").exists(), \
        "サーバーを名乗らない tmux で書かれた記録が残った"
