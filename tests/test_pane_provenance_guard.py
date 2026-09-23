#!/usr/bin/env python3
"""tests/test_pane_provenance_guard.py

**破壊の根拠を「中の人の同定」から「自分が作った記録」に変える**
(t040 / PR #209, Codex 5 巡目 P1-1 / P1-2 / P1-3)。

4 巡目で判定表を allowlist に反転したが、5 巡目の P1 3 件は **分類器そのものが
失敗時に `NONE` (= 確実に空) を返す**ことに集中していた:

  1. 相対パスの別名で cwd が読めないと `NONE`
  2. `python3.12 /theirs/monitor` のような未知のインタプリタ + 別名で `NONE`
  3. `realpath()` の非 strict 解決で、失われた symlink 経由のパスが字面一致で `MINE`

`NONE` も `MINE` も破壊を許す結論である以上、**積極的な証明を要求しなければ
ならない**。「中の人を同定できなかった」は「誰も居ない」ではない。

ここで固定する契約:

  A. **破壊の土台は spawn 記録** — `registry/mux/<name>.json` は観測して推論した
     事実ではなく、spawn したときに**自分が書いた**事実。symlink も相対パスも
     未知のインタプリタも関係ない。デーモンペインを破壊してよいのは

       (A-1) その記録が「このバックエンドで、この不変 id のペインを作った」と
             言っているとき (中の人が *別チェックアウトのデーモンだと証明できた*
             ときを除く)、または
       (A-2) ペインに**生きたプロセスが 1 つも無いことを積極的に確認できた**とき

     のどちらか。`MINE` は単独ではもう根拠にならない — それが 3 の欠陥だった。

  B. **一致は不変の識別子で見る** — 名前ではなく tmux `@window_id` / herdr
     `tab_id`。覗いた対象と壊す対象の同一性 (4 巡目 P1-4) もこれで閉じる。

  C. **分類器は backstop として残り、失敗はすべて UNKNOWN に倒れる** —
     `NONE` を返してよいのは A-2 の条件のときだけ。

  D. **出口はある、が黙ってはいない** — 記録が失われても `--force` で通せる。
     通ったことは必ず stderr に出る。

  python3 -m pytest tests/test_pane_provenance_guard.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_daemon_watch as dw  # noqa: E402
import lib_mux  # noqa: E402


# ---------------------------------------------------------------------------
# 偽 /proc — cwd は本物のシンボリックリンクにする
# ---------------------------------------------------------------------------

def _fake_proc(tmp_path, entries, *, name="proc"):
    """`{pid: (ppid, [argv...], cwd_or_None)}` から偽 /proc を作る。

    `cwd` を None にすると `/proc/<pid>/cwd` を作らない = 読めない。
    """
    proc = tmp_path / name
    proc.mkdir(exist_ok=True)
    for pid, entry in entries.items():
        ppid, argv = entry[0], entry[1]
        cwd = entry[2] if len(entry) > 2 else None
        d = proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        # fields after the last ')': 0 state, 1 ppid, 2 pgrp, 3 session,
        # 4 tty_nr, 5 tpgid.  These used to be all-zero, which modelled a
        # process with **no controlling terminal** — i.e. not a pane shell at
        # all.  Nothing read them until t041 made "the pane is empty" require
        # the root to be a *positively identified* idle shell, and an
        # under-specified root is exactly the blind spot Codex 6巡目 P1-1 came
        # out of.  A pane shell owns its terminal's foreground group, so:
        # pgrp = session = tpgid = pid, on a tty.
        (d / "stat").write_text(
            f"{pid} (bash) S {ppid} {pid} {pid} 1234 {pid} "
            + " ".join(["0"] * 14),
            encoding="utf-8")
        if cwd is not None:
            target = Path(cwd)
            target.mkdir(parents=True, exist_ok=True)
            link = d / "cwd"
            if not link.is_symlink():
                link.symlink_to(target)
    return str(proc)


@pytest.fixture
def our_checkout(tmp_path):
    root = tmp_path / "ours"
    (root / "scripts").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "scripts" / "dispatcher.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "scripts" / "watchdog.py").write_text("#\n", encoding="utf-8")
    return root


@pytest.fixture
def their_checkout(tmp_path):
    root = tmp_path / "theirs"
    (root / "scripts").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "scripts" / "dispatcher.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "scripts" / "watchdog.py").write_text("#\n", encoding="utf-8")
    return root


@pytest.fixture
def as_our_checkout(monkeypatch, our_checkout):
    """mux 層の identity を `our_checkout` に差し替える。

    spawn 記録の置き場所もここから決まる — 「自分が作った記録」は、走っている
    チェックアウトのものでなければ意味がないので、分類器と同じ identity に
    束ねる。env ではなく自分のファイルの場所であるのは元のままｄ。
    """
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(our_checkout))
    return our_checkout


class _RecordingTmux:
    """tmux を実行せずに argv を記録する差し替え。

    `display-message` は `#{window_id} #{pane_pid}` を 1 回で答える。
    """

    def __init__(self, pane_pid=None, window_id="@7"):
        self.calls = []
        self.pane_pid = pane_pid
        self.window_id = window_id

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        text = kwargs.get("text", False)
        out = ""
        # Expand whatever format was asked for, rather than a fixed pair of
        # fields: a stub that answers only the format string it was written
        # against turns a change in the caller into a fake refusal
        # (memory: crewvia-fake-cli-and-qa-fail-gaps).  `new-window -P -F` is
        # answered too — since t041 spawn() takes the window id from the
        # command that created the window rather than looking it up by name.
        wants_format = "display-message" in argv or (
            ("new-window" in argv or "new-session" in argv)
            and "-P" in argv and "-F" in argv)
        if wants_format and self.pane_pid is not None:
            out = (argv[-1].replace("#{window_id}", str(self.window_id))
                           .replace("#{pane_pid}", str(self.pane_pid))
                           .replace("#{pid}", "900")
                           .replace("#{socket_path}", "/tmp/tmux-test/default")
                   + "\n")
        return subprocess.CompletedProcess(
            list(argv), 0,
            stdout=out if text else out.encode(),
            stderr="" if text else b"")

    def __getattr__(self, item):
        return getattr(subprocess, item)

    def kill_targets(self):
        return [c[c.index("-t") + 1] for c in self.calls
                if "kill-window" in c and "-t" in c]


# ===========================================================================
# A. 破壊の土台は spawn 記録
# ===========================================================================

def test_mine_alone_no_longer_authorises_destruction(
        monkeypatch, tmp_path, as_our_checkout, our_checkout):
    """自分のデーモンが走っていても、**記録が無ければ**壊さない。

    これが土台の反転そのもの。Codex 5 巡目 P1-3 は「`MINE` が字面一致で出る」
    経路だったので、`MINE` を根拠から外さないかぎり同じ穴がまた開く。
    ペインの中を読んだ結果ではなく、**自分が spawn したときに書いた記録**だけが
    破壊を許す (生きたプロセスが 1 つも無い場合を除く — 下のテスト)。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", str(our_checkout / "scripts" / "dispatcher.sh")],
               str(our_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    owner, detail = lib_mux.daemon_pane_owner(4100, proc_root=proc_root)
    assert owner == lib_mux.OWNER_MINE, detail      # 分類器は MINE と言う

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_targets() == [], \
        f"spawn 記録が無いデーモンペインが壊された: {rec.kill_targets()}"


def test_our_own_spawn_record_authorises_destruction(
        monkeypatch, tmp_path, as_our_checkout, our_checkout):
    """同じペインでも、spawn 記録が「この tab を作った」と言えば壊してよい。"""
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", str(our_checkout / "scripts" / "dispatcher.sh")],
               str(our_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    lib_mux.write_pane_record("dispatcher", "tmux", "@7")

    assert lib_mux.TmuxBackend().kill("dispatcher") is True
    assert rec.kill_targets() == ["@7"], \
        "覗いた window id そのものを閉じていない"


def test_a_record_for_another_tab_does_not_authorise_this_one(
        monkeypatch, tmp_path, as_our_checkout, our_checkout):
    """名前ではなく**不変の id** で一致を見る (B)。

    別チェックアウトが同じ `dispatcher` という名前でタブを作り直した形。
    名前で照合していると素通りする。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", str(our_checkout / "scripts" / "dispatcher.sh")],
               str(our_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@9")   # 今そこに在るのは @9
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    lib_mux.write_pane_record("dispatcher", "tmux", "@7")  # 記録は @7

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_targets() == [], \
        f"記録に無い window が壊された: {rec.kill_targets()}"


def test_a_record_written_by_the_other_backend_does_not_authorise(
        monkeypatch, tmp_path, as_our_checkout, our_checkout):
    """バックエンドまで一致して初めて「自分が作った」と言える。

    herdr の `tab_id` と tmux の `@window_id` は別の名前空間なので、
    たまたま文字列が一致しても同じものを指していない。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", str(our_checkout / "scripts" / "dispatcher.sh")],
               str(our_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    lib_mux.write_pane_record("dispatcher", "herdr", "@7")

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_targets() == []


def test_a_provably_empty_pane_is_still_destroyable_without_a_record(
        monkeypatch, tmp_path, as_our_checkout):
    """A-2 — 記録が失われても **確実に空**なら壊せる (可用性)。

    herdr サーバーを再起動するとタブは復元されるが中身は復元されず、
    registry/mux の id も古くなる。この出口が無いと husk の掃除 (t035) と
    crewvia の再起動が永久に詰まる。
    """
    proc_root = _fake_proc(tmp_path, {4100: (1, ["bash"], str(tmp_path))})
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    owner, detail = lib_mux.daemon_pane_owner(4100, proc_root=proc_root)
    assert owner == lib_mux.OWNER_NONE, detail

    assert lib_mux.TmuxBackend().kill("dispatcher") is True
    assert rec.kill_targets() == ["@7"]


def test_a_foreign_daemon_vetoes_even_our_own_record(
        monkeypatch, tmp_path, as_our_checkout, their_checkout):
    """記録が一致しても、中に *別チェックアウトの* デーモンが居るなら壊さない。

    記録は「このタブを作った」ことしか証明しない。そのタブの中で誰かが
    他所のデーモンを起動したなら、それは壊してよいものではない。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", str(their_checkout / "scripts" / "dispatcher.sh")],
               str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    lib_mux.write_pane_record("dispatcher", "tmux", "@7")

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_targets() == []


def test_worker_panes_are_out_of_scope(
        monkeypatch, tmp_path, as_our_checkout):
    """Worker のペインはこの判定を一切通らない。

    判定の unit を 1 つ (デーモンペイン) に絞るのは安全性そのもの。広げると
    「Worker を retire できない」になって、別の経路で必ず外される
    (memory: approve-judgment-needs-allowlist-and-scope)。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["claude", "--dangerously-skip-permissions"], str(tmp_path)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("Ren-code") is True
    assert rec.kill_targets(), "Worker ペインが記録のせいで殺せなくなっている"


# ---------------------------------------------------------------------------
# 記録を書くのは spawn、消すのは kill
# ---------------------------------------------------------------------------

def test_tmux_spawn_writes_the_record_it_will_later_be_judged_by(
        monkeypatch, tmp_path, as_our_checkout):
    """tmux も spawn 時に記録を残す。

    herdr だけが registry/mux を書いていたので、tmux モードでは「自分が作った
    記録」が常に存在せず、この新しい判定が常に拒否に倒れてしまう。
    """
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().spawn("dispatcher", "bash scripts/dispatcher.sh") is True

    record = lib_mux.read_pane_record("dispatcher")
    assert record is not None, "tmux spawn が記録を書いていない"
    assert record.get("handle") == "@7", record
    assert record.get("backend") == "tmux", record


def test_a_successful_kill_drops_the_record(
        monkeypatch, tmp_path, as_our_checkout):
    """壊したら記録も畳む。残すと、次に同じ名前で作られた他人のタブに効く。"""
    proc_root = _fake_proc(tmp_path, {4100: (1, ["bash"], str(tmp_path))})
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)
    lib_mux.write_pane_record("dispatcher", "tmux", "@7")

    assert lib_mux.TmuxBackend().kill("dispatcher") is True
    assert lib_mux.read_pane_record("dispatcher") is None


# ---------------------------------------------------------------------------
# D. 出口はある、が黙ってはいない
# ---------------------------------------------------------------------------

def test_force_lifts_the_refusal_and_says_so(
        monkeypatch, capsys, tmp_path, as_our_checkout, their_checkout):
    """`--force` は通る。ただし黙っては通らない。

    記録が失われたまま永久に kill できない状態を作らないための出口
    (memory: fail-closed-discard-vs-hold)。黙って使われると、この判定は
    「誰も見ていない儀式」になる。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", str(their_checkout / "scripts" / "dispatcher.sh")],
               str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingTmux(pane_pid=4100, window_id="@7")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher", allow_foreign=True) is True
    assert rec.kill_targets(), "--force が出口になっていない"

    err = capsys.readouterr().err
    assert "force" in err.lower(), f"バイパスが黙って通った: {err!r}"
    assert "dispatcher" in err, err


# ===========================================================================
# C. 分類器 backstop — Codex 5 巡目 P1 3 件
# ===========================================================================

def test_p1_1_an_unresolvable_relative_alias_is_undecided_not_empty(
        tmp_path, our_checkout):
    """P1-1 — cwd が読めない相対パスは `UNKNOWN`。`NONE` ではない。

    欠陥版は「`exec_arg` の basename が期待どおりのときだけ UNKNOWN」だった。
    `./monitor` のような**別名**は basename が一致しないので素通りし、
    ペイン全体が「空」= 壊してよい、と分類されていた。

    判定は **`script_owner()` に直接**訊く。ペイン単位で訊くと、同じ変更に
    含まれる別の修正 (「認識できない生きたプロセスが居るなら NONE ではない」)
    が先に UNKNOWN を返してしまい、この欠陥を戻しても緑のままだった — 別の層が
    先に止めていて偶然緑、という形
    (memory: red-proof-catches-tests-green-for-the-wrong-reason)。
    """
    argv = ["./monitor"]
    proc_root = _fake_proc(tmp_path, {4200: (4100, argv, None)})  # cwd が読めない
    mine = str(our_checkout / "scripts" / "watchdog.py")

    owner, detail = lib_mux.script_owner(
        4200, argv, "watchdog.py", mine, proc_root=proc_root)
    assert owner == lib_mux.OWNER_UNKNOWN, \
        f"解決できなかった相対パスが {owner} に倒れた: {detail}"

    # 呼び出し側の契約としても押さえておく。
    pane_proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, argv, None),
    }, name="proc-pane")
    owner, detail = lib_mux.pane_script_owner(
        4100, "watchdog.py", mine, proc_root=pane_proc_root)
    assert owner == lib_mux.OWNER_UNKNOWN, detail


def test_p1_2_a_versioned_interpreter_still_resolves_its_script(
        tmp_path, our_checkout, their_checkout):
    """P1-2 — `python3.12 /theirs/monitor` を見逃さない。

    `python3.12` は `_INTERPRETER_NAMES` に無いので argv[0] が「実行している
    プログラム」と見なされ、実際のスクリプト引数 `/theirs/monitor` が一度も
    解決されなかった。versioned interpreter と renamed symlink の**組み合わせ**が
    テストから漏れていた。
    """
    alias = their_checkout / "monitor"
    alias.symlink_to(their_checkout / "scripts" / "watchdog.py")

    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3.12", str(alias)], str(their_checkout)),
    })
    owner, detail = lib_mux.pane_script_owner(
        4100, "watchdog.py", str(our_checkout / "scripts" / "watchdog.py"),
        proc_root=proc_root)
    assert owner == lib_mux.OWNER_FOREIGN, \
        f"別チェックアウトの watchdog が {owner} に分類された: {detail}"


def test_p1_2_a_versioned_interpreter_also_finds_our_own_daemon(
        tmp_path, our_checkout):
    """同じ経路で、自分のデーモンも取り逃がさないこと (可用性側)。"""
    alias = our_checkout / "monitor"
    alias.symlink_to(our_checkout / "scripts" / "watchdog.py")

    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3.12", "-u", str(alias), "--repo-root", str(our_checkout)],
               str(our_checkout)),
    })
    owner, detail = lib_mux.pane_script_owner(
        4100, "watchdog.py", str(our_checkout / "scripts" / "watchdog.py"),
        proc_root=proc_root)
    assert owner == lib_mux.OWNER_MINE, detail


def test_p1_3_a_dotdot_folded_across_a_lost_symlink_is_not_ownership(
        tmp_path, our_checkout, their_checkout):
    """P1-3 — 失われた symlink を跨ぐ `..` の字面畳み込みを `MINE` にしない。

    起動時 `/ours/link` → `/theirs/subdir` だったので、プロセスが実際に走らせて
    いるのは `/theirs/scripts/watchdog.py`。あとで link が消えると、非 strict な
    `realpath()` は `..` を**字面で**畳んで `/ours/scripts/watchdog.py` を返し、
    `_same_script_file()` が文字列一致で True — 他所のデーモンが「自分のもの」に
    化ける。

    証明できないものは `MINE` ではない。
    """
    (their_checkout / "subdir").mkdir()
    link = our_checkout / "link"
    link.symlink_to(their_checkout / "subdir")
    argv_path = str(link / ".." / "scripts" / "watchdog.py")

    # link が在るうちは、走っているのが他所のものだと同定できる。
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3", argv_path], str(tmp_path)),
    })
    mine = str(our_checkout / "scripts" / "watchdog.py")
    owner, detail = lib_mux.pane_script_owner(
        4100, "watchdog.py", mine, proc_root=proc_root)
    assert owner == lib_mux.OWNER_FOREIGN, detail

    # link が消えると、そのパスが何を指していたかはもう分からない。
    link.unlink()
    owner, detail = lib_mux.pane_script_owner(
        4100, "watchdog.py", mine, proc_root=proc_root)
    assert owner != lib_mux.OWNER_MINE, \
        f"失われた symlink を跨いだ字面一致が所有の証明に使われた: {detail}"
    assert owner == lib_mux.OWNER_UNKNOWN, detail


def test_a_deleted_checkout_keeps_its_identity(tmp_path, our_checkout):
    """`..` を含まないパスは、チェックアウトが消えていても同定できること。

    P1-3 の修正が「存在しないパスは全部 UNKNOWN」に倒れると、worktree を消した
    あとの後始末ができなくなる。危ないのは **`..` を実在しないディレクトリ越しに
    畳むこと**だけなので、そこだけを曖昧扱いにする。
    """
    gone = tmp_path / "gone" / "scripts" / "watchdog.py"
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3", str(gone)], str(tmp_path)),
    })
    owner, detail = lib_mux.pane_script_owner(
        4100, "watchdog.py", str(our_checkout / "scripts" / "watchdog.py"),
        proc_root=proc_root)
    assert owner == lib_mux.OWNER_FOREIGN, detail


def test_none_requires_positively_confirming_the_pane_is_empty(
        tmp_path, our_checkout):
    """C — 読めても分からなかったものは `NONE` ではない。

    「デーモンだと認識できるものが無かった」と「ペインに何も居ない」は別の
    答え。前者を `NONE` にしていたから、分類器が取り逃がすたび (P1-1 / P1-2)
    ペインが「空」に化けた。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["some-unknown-supervisor", "--config", "x.yaml"], str(tmp_path)),
    })
    owner, detail = lib_mux.pane_script_owner(
        4100, "watchdog.py", str(our_checkout / "scripts" / "watchdog.py"),
        proc_root=proc_root)
    assert owner == lib_mux.OWNER_UNKNOWN, \
        f"認識できなかった生きたプロセスが「空のペイン」に化けた: {detail}"


def test_an_empty_pane_is_still_none(tmp_path, our_checkout):
    """逆側 — シェルだけのペイン (husk) は今まで通り `NONE` (t035)。"""
    proc_root = _fake_proc(tmp_path, {4100: (1, ["-bash"], str(tmp_path))})
    owner, detail = lib_mux.pane_script_owner(
        4100, "watchdog.py", str(our_checkout / "scripts" / "watchdog.py"),
        proc_root=proc_root)
    assert owner == lib_mux.OWNER_NONE, detail


# ===========================================================================
# 記録の読み書きそのもの
# ===========================================================================

def test_record_status_matrix(tmp_path, as_our_checkout):
    """記録の照合が返す 3 値。"""
    assert lib_mux.pane_record_status("dispatcher", "tmux", "@7")[0] \
        == lib_mux.PANE_RECORD_ABSENT

    lib_mux.write_pane_record("dispatcher", "tmux", "@7")
    assert lib_mux.pane_record_status("dispatcher", "tmux", "@7")[0] \
        == lib_mux.PANE_RECORD_MATCH
    assert lib_mux.pane_record_status("dispatcher", "tmux", "@9")[0] \
        == lib_mux.PANE_RECORD_MISMATCH
    assert lib_mux.pane_record_status("dispatcher", "herdr", "@7")[0] \
        == lib_mux.PANE_RECORD_MISMATCH
    # 覗いた側が id を答えられなかったのは、一致ではない。
    assert lib_mux.pane_record_status("dispatcher", "tmux", None)[0] \
        == lib_mux.PANE_RECORD_MISMATCH
    assert lib_mux.pane_record_status("dispatcher", "tmux", "")[0] \
        == lib_mux.PANE_RECORD_MISMATCH


def test_a_record_lands_under_the_running_checkout(tmp_path, as_our_checkout,
                                                   our_checkout):
    """記録は走っているチェックアウトの registry/mux/ に置かれる。

    別チェックアウトの記録を読んでしまうと、「自分が作った」証明にならない。
    """
    lib_mux.write_pane_record("dispatcher", "tmux", "@7")
    path = our_checkout / "registry" / "mux" / f"{lib_mux._pane_name('dispatcher')}.json"
    assert path.exists(), f"{path} が無い"
    assert json.loads(path.read_text(encoding="utf-8"))["handle"] == "@7"


def test_an_unparsable_record_is_not_a_match(tmp_path, as_our_checkout,
                                             our_checkout):
    """壊れた記録は「一致」ではない — 読めなかったことを証明にしない。"""
    d = our_checkout / "registry" / "mux"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{lib_mux._pane_name('dispatcher')}.json").write_text("{ not json",
                                                                encoding="utf-8")
    assert lib_mux.pane_record_status("dispatcher", "tmux", "@7")[0] \
        != lib_mux.PANE_RECORD_MATCH


# ===========================================================================
# herdr 側 — 同じ判定が同じように効くこと
# ===========================================================================

def test_herdr_kill_is_bound_to_the_recorded_tab_id(
        monkeypatch, tmp_path, as_our_checkout, our_checkout):
    """herdr でも、記録に無いタブは閉じない。"""
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", str(our_checkout / "scripts" / "dispatcher.sh")],
               str(our_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)

    closed = []

    def fake_run(cmd_key, extra_args, timeout=10):
        if cmd_key == "tab_close":
            closed.append(extra_args[0])
            return {"result": {}}
        return {"result": {}}

    monkeypatch.setattr(lib_mux, "_herdr_run", fake_run)
    backend = lib_mux.HerdrBackend()
    monkeypatch.setattr(backend, "_inspect_pane", lambda name: ("tab-9", 4100))

    lib_mux.write_pane_record("dispatcher", "herdr", "tab-7")
    assert backend.kill("dispatcher") is False
    assert closed == [], f"記録に無いタブが閉じられた: {closed}"

    lib_mux.write_pane_record("dispatcher", "herdr", "tab-9")
    assert backend.kill("dispatcher") is True
    assert closed == ["tab-9"]


def test_a_transient_herdr_failure_does_not_destroy_the_record(
        monkeypatch, tmp_path, as_our_checkout):
    """herdr に訊けなかっただけで記録を捨てない。

    捨てると、次の kill は「記録が無い」= 拒否に倒れ、一過性の失敗が
    永久の拒否に化ける。「消えたと答えられた」ときだけ畳む。
    """
    lib_mux.write_pane_record("dispatcher", "herdr", "tab-7", pane_id="pane-7")

    monkeypatch.setattr(lib_mux, "_herdr_run",
                        lambda cmd_key, extra, timeout=10: None)
    backend = lib_mux.HerdrBackend()
    backend._resolve_ids("dispatcher")

    assert lib_mux.read_pane_record("dispatcher") is not None, \
        "herdr に訊けなかっただけで spawn 記録が消えた"


# ===========================================================================
# 層をまたいだ束縛
# ===========================================================================

def test_restart_cannot_destroy_what_the_mux_layer_refuses(
        monkeypatch, tmp_path, our_checkout):
    """`restart()` は mux が断った kill を「済んだこと」にして進まない。

    判定 unit を 1 つに絞る、の実際の意味がこれ。`restart()` 側の事前チェックは
    あくまで早期脱出で、**壊すかどうかを決めるのは mux 層 1 箇所**。事前チェックが
    通ってしまっても、mux が断ったらそこで止まる。
    """
    class _RefusingMux:
        def __init__(self):
            self.spawned = []

        def pid(self, name):
            return 4100

        def kill(self, name, allow_foreign=False):
            return False                       # identity ガードが断った

        def spawn(self, name, cmd, cwd=None, env=None):
            self.spawned.append(name)
            return True

    proc_root = _fake_proc(tmp_path, {4100: (1, ["bash"], str(tmp_path))})
    mux = _RefusingMux()
    (our_checkout / "registry").mkdir(exist_ok=True)

    ok = dw.restart("dispatcher", repo_root=our_checkout, mux=mux,
                    log=lambda m: None, proc_root=proc_root)
    assert ok is False
    assert mux.spawned == [], \
        "mux が断ったのに restart が spawn まで進んだ"
