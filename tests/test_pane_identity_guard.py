#!/usr/bin/env python3
"""tests/test_pane_identity_guard.py

**隔離の境界を「呼ぶ側の env」から「対象の identity」へ移す** (t038 / PR #209,
Codex 3 巡目 P1-1 / P1-2 / P1-3 / P2-4 / P2-5)。

t037 で入れた隔離は env に依存している。Codex の実証はこうだった:

    env を消して `TmuxBackend().kill("dispatcher")` を呼ぶと
    `tmux kill-window -t crewvia:dispatcher` が発行された

`CREWVIA_MUX_TEST_ISOLATION` は **呼ぶ側の性質**なので、きれいな env で
サブプロセスを起動すれば消える。本当の防壁は **対象が誰のものか** — ペインの
中で走っているのがどのチェックアウトのデーモンか — でなければならない。
2026-09-23 の事故も、identity ガードが効いていれば env に関係なく防げた。

ここで固定する契約:

  1. **mux 層の backstop** (P1-1) — `kill()` は、ペインの中で *別チェックアウトの*
     crewvia デーモンが走っていることを **積極的に証明できたとき** に限って断る。
     テスト中かどうかを一切見ない。判断できないとき (UNKNOWN) は断らない —
     ここは Worker のペインも通る道なので、allowlist にすると Worker が
     kill できなくなる。allowlist はデーモン層 (`pane_daemon_owner`) の役目で、
     この 2 層は **別々の証拠**に立っている。
  2. **起動形態の同定** (P1-3) — `bash scripts/dispatcher.sh` のような相対パス
     起動を「空のペイン (husk)」と分類しない。プロセスの cwd に対して解決し、
     曖昧なものは UNKNOWN に倒す。
  3. **respawn に隔離を引き継ぐ** (P1-2) — mux 経由で起動されるプロセスは
     呼び出し側ではなく mux サーバーの env を継承するので、隔離マーカーと
     名前空間はコマンド文字列で渡すしかない。
  4. **起動確認の三値** (P2-4) — 不正なプロセスエントリは `live` ではなく
     `unknown`。
  5. **保護を消せなかったら成功と言わない** (P2-5) — `resume()` が marker を
     消せなかったのに true を返すと、相互監視が永久に hold する。

  python3 -m pytest tests/test_pane_identity_guard.py -v
"""

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

from conftest import PRODUCTION_DESTINATION, fake_tmux_if_shell  # noqa: E402


# ---------------------------------------------------------------------------
# 偽 /proc — cwd は本物のシンボリックリンクにする
# ---------------------------------------------------------------------------

def _fake_proc(tmp_path, entries, *, name="proc"):
    """`{pid: (ppid, [argv...], cwd_or_None)}` から偽 /proc を作る。

    `cwd` を None にすると `/proc/<pid>/cwd` を作らない = 読めない。「相対パスを
    解決できなかった」経路を通すためのもので、これを NONE に倒すのが P1-3 の
    欠陥そのものなので、テストからは必ず届かせる。
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
    """`lib_mux` 自身がそこから走っていることにするチェックアウト。"""
    root = tmp_path / "ours"
    (root / "scripts").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "scripts" / "dispatcher.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "scripts" / "watchdog.py").write_text("#\n", encoding="utf-8")
    return root


@pytest.fixture
def their_checkout(tmp_path):
    """別チェックアウト。2026-09-23 に本番だった側。"""
    root = tmp_path / "theirs"
    (root / "scripts").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "scripts" / "dispatcher.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    return root


@pytest.fixture
def as_our_checkout(monkeypatch, our_checkout):
    """mux 層の identity を `our_checkout` に差し替える。

    本番では `Path(lib_mux.__file__).parent.parent` — env ではなく **自分の
    ファイルの場所**。env が嘘をついても動かせないのがこのガードの要点なので、
    テストからも env ではなくここを差し替える。
    """
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: Path(our_checkout))
    return our_checkout


# ---------------------------------------------------------------------------
# 1. mux 層の backstop — env を一切見ない (P1-1)
# ---------------------------------------------------------------------------

#: この偽 tmux が名乗るサーバー — (エンドポイント, 世代)。spawn 記録は
#: これに束縛されるので、記録を書くテストは同じ値を渡す。
SERVER = ("/tmp/tmux-test/default", "900")


class _RecordingSubprocess:
    """tmux を **実行せずに** argv を記録する差し替え。

    `display-message` はペインの window / pid と、それが載っている **mux
    サーバー** を 1 回で答える。ペインを覗いた時点の window と、あとで閉じる
    window が同じものである保証は「同じ 1 回の問い合わせで得た id」以外に
    無いので、テスト側の偽物もその形にしておく (P1-4)。

    答えるのは **訊かれた書式そのもの**を展開したもので、固定の並びではない。
    決め打ちで返す偽物は、呼び手が書式を変えた瞬間に「サーバーが分からない」
    = 本番には存在しない拒否を作り出す
    (memory: crewvia-fake-cli-and-qa-fail-gaps)。t041 で `#{pid}` と
    `#{socket_path}` が加わり、t042 でその欠落が拒否になったので、ここが
    書式駆動でないと全部が偽の赤になる。

    `window_id_after` を渡すと、最初の `display-message` のあとに window_id が
    すり替わる — 覗いたタブが消えて別チェックアウトが同じ名前で作り直した形。
    """

    def __init__(self, pane_pid=None, window_id="@1", window_id_after=None,
                 server=SERVER):
        self.calls = []
        self.issued = []
        self.pane_pid = pane_pid
        self.window_id = window_id
        self.window_id_after = window_id_after
        self.server = server

    def _expand(self, fmt):
        endpoint, generation = self.server if self.server else ("", "")
        for token, value in (
                ("#{window_id}", self.window_id),
                ("#{pane_pid}", self.pane_pid),
                ("#{pid}", generation),
                ("#{socket_path}", endpoint),
        ):
            fmt = fmt.replace(token, str(value))
        return fmt

    def _answers(self):
        endpoint, generation = self.server if self.server else ("", "")
        return {"#{window_id}": self.window_id, "#{pane_pid}": self.pane_pid,
                "#{pid}": generation, "#{socket_path}": endpoint}

    def run(self, argv, _branch=False, **kwargs):
        self.calls.append(list(argv))
        # `issued` は本番コードが自分で出した呼び出しだけ。`if-shell` の then 側は
        # tmux の中で走るので `calls` にしか入らない。
        if not _branch:
            self.issued.append(list(argv))
        text = kwargs.get("text", False)
        out = ""
        if "if-shell" in argv:
            # 束縛された破壊 (t043)。条件が外れたら then 側は実行されない。
            then_argv, out = fake_tmux_if_shell(argv, self._answers())
            if then_argv is not None:
                self.run(then_argv, _branch=True, text=text)
        elif "display-message" in argv and self.pane_pid is not None:
            out = self._expand(argv[-1]) + "\n"
            if self.window_id_after is not None:
                self.window_id = self.window_id_after
        return subprocess.CompletedProcess(
            list(argv), 0,
            stdout=out if text else out.encode(),
            stderr="" if text else b"")

    def __getattr__(self, item):
        return getattr(subprocess, item)

    def kill_calls(self):
        return [c for c in self.calls if "kill-window" in c]

    def kill_targets(self):
        return [c[c.index("-t") + 1] for c in self.kill_calls() if "-t" in c]


@pytest.fixture
def production_env(monkeypatch):
    """2026-09-23 の env — 隔離マーカーが無く、宛先は本番。

    このガードは「テスト中か」を見ないので、**本番の env でこそ**効かなければ
    意味がない。env ベースの層はここでは全部外してある。

    `PYTEST_CURRENT_TEST` は pytest が **フェーズごとに置き直す**ので、fixture で
    消しても call フェーズには戻ってきてしまう。Codex が実証した「きれいな env の
    サブプロセス」= `_test_isolation_active()` が False を返す状態そのものなので、
    その 1 点を直接落とす。ここで落ちるのが env ベースの層の全部で、残って
    ほしいのは identity ガードだけ。
    """
    for var in ("CREWVIA_MUX_TEST_ISOLATION", "PYTEST_CURRENT_TEST",
                "CREWVIA_MUX_PANE_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(lib_mux, "_test_isolation_active", lambda: False)
    monkeypatch.setenv("CREWVIA_TMUX_SESSION", PRODUCTION_DESTINATION)
    monkeypatch.setenv("CREWVIA_HERDR_WORKSPACE", PRODUCTION_DESTINATION)


def test_kill_refuses_a_pane_running_another_checkouts_daemon(
        monkeypatch, tmp_path, as_our_checkout, their_checkout, production_env):
    """Codex の実証そのもの: きれいな env でも、他人のデーモンは kill できない。

    env マーカーが両方とも無い状態で `TmuxBackend().kill("dispatcher")` を呼ぶ。
    t037 の隔離はここで完全に無効なので、止まるとすればそれは **ペインの中身**
    を見たからでしかない。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", str(their_checkout / "scripts" / "dispatcher.sh")],
               str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_calls() == [], \
        f"another checkout's dispatcher was killed: {rec.kill_calls()}"


def test_kill_refuses_a_relatively_launched_foreign_daemon(
        monkeypatch, tmp_path, as_our_checkout, their_checkout, production_env):
    """`bash scripts/dispatcher.sh` — 普通の起動の仕方も同じように守られる。

    絶対パスの形だけ塞いでも、**普通に打つ形**が抜けていれば守っていない。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", "scripts/dispatcher.sh"], str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_calls() == []


def test_kill_still_ends_our_own_daemon(
        monkeypatch, tmp_path, as_our_checkout, production_env):
    """自分のチェックアウトのデーモンなら、今まで通り kill できる。

    ガードが「常に断る」になっていないことの確認。これが無いと、安全側に
    倒したつもりで restart の道を塞いだことに気付けない。

    t040 以降、破壊を許すのは **spawn 記録** であって「中の人が自分のものだと
    分類できたこと」ではない (Codex 5 巡目 P1-3: その分類自体が字面一致で
    出せてしまう)。なのでここでも、本番と同じように spawn 時の記録を置く。
    記録が無い側が断られることは
    `test_pane_provenance_guard.py::test_mine_alone_no_longer_authorises_destruction`
    が押さえている。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", "scripts/dispatcher.sh"], str(as_our_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)
    lib_mux.write_pane_record("dispatcher", "tmux", rec.window_id,
                              server=rec.server)

    assert lib_mux.TmuxBackend().kill("dispatcher") is True
    assert rec.kill_calls(), "our own daemon's pane was not killed"


def test_kill_still_closes_a_husk_pane(
        monkeypatch, tmp_path, as_our_checkout, production_env):
    """プロセスが死んでペインだけ残った形 (husk) は今まで通り閉じられる。

    t035 が足した生存性。ここを塞ぐと普通のクラッシュから復旧できなくなる。
    """
    proc_root = _fake_proc(tmp_path, {4100: (1, ["bash"], str(tmp_path))})
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher") is True
    assert rec.kill_calls()


def test_a_worker_pane_stays_killable_even_with_a_foreign_daemon_inside(
        monkeypatch, tmp_path, as_our_checkout, their_checkout, production_env):
    """Worker のペインは、中に別チェックアウトのデーモンが居ても素通り。

    これは妥協ではなく **スコープの決定**。crewvia の QA Worker は自分のペインで
    `bash scripts/dispatcher.sh` を走らせることが実際にある (与えられた worktree
    のもの = watchdog から見れば foreign)。ここまで守りを広げると、その Worker を
    watchdog が二度と retire できなくなる — 「永久に kill できない」を作る側に
    回ってしまう。守るのはデーモンのペインだけ。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", "scripts/dispatcher.sh"], str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("Ren-worker") is True
    assert rec.kill_calls()


def test_a_worker_kill_does_not_pay_for_the_guard(
        monkeypatch, tmp_path, as_our_checkout, production_env):
    """スコープ外の名前では、そもそもペインを覗きに行かない。

    覗きに行くなら /proc の全走査が Worker の kill ごとに乗る。スコープを
    「デーモンのペインだけ」に絞ったことが、実際に効いていることの確認。
    """
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", str(tmp_path / "nonexistent-proc"))
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("Ren-worker") is True
    assert not [c for c in rec.calls if "display-message" in c], \
        f"the pane was inspected for a name the guard does not cover: {rec.calls}"


def test_kill_refuses_a_daemon_pane_that_cannot_be_read(
        monkeypatch, tmp_path, as_our_checkout, production_env):
    """デーモンのペインが読めないなら断る (t039 P1-1)。

    これは **以前と逆の答え**。前は「読めない = 断る理由が無い」で kill を
    通していたが、それは `UNKNOWN` を破壊の許可に変換していたということで、
    Codex が 4 巡目に挙げた 4 件のうち 3 件がその形だった。

    可用性の言い訳もここでは立たない: スコープは既に `DAEMON_PANE_NAMES` に
    絞ってあるので、読めない Worker のペインは**この分岐に来ない**
    (次のテストがそれを押さえる)。デーモンのペインを本当に取り返したいときの
    出口は `--force` の方。
    """
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", str(tmp_path / "nonexistent-proc"))
    rec = _RecordingSubprocess(pane_pid=None)   # display-message が答えない
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    # 判定そのものを名指しで押さえる。`kill()` の結果だけを見ていると、
    # P1-4 の「覗いた window に束縛する」方が先に断るので、**判定を元に戻しても
    # 緑のまま**だった — 別の層が先に止めていて偶然緑、という形
    # (memory: red-proof-catches-tests-green-for-the-wrong-reason)。
    #
    # t040 以降、読めなかったペインは「破壊を許す 2 つの積極的な証明」の
    # どちらも出せない: spawn 記録は無く、「空である」ことも確認できていない。
    assert lib_mux.OWNER_UNKNOWN not in lib_mux._OWNERS_THAT_PROVE_EMPTY
    allowed, why = lib_mux.may_destroy_pane("dispatcher", "tmux", None, None)
    assert allowed is False, why
    assert lib_mux.TmuxBackend()._refuses_foreign_daemon(
        "dispatcher", None, None) is True

    assert lib_mux.TmuxBackend().kill("dispatcher") is False
    assert rec.kill_calls() == [], \
        f"a daemon pane nobody could read was killed anyway: {rec.kill_calls()}"


def test_a_worker_pane_that_cannot_be_read_is_still_killable(
        monkeypatch, tmp_path, as_our_checkout, production_env):
    """読めない Worker のペインは今まで通り閉じられる。

    上のテストで足した拒否が、Worker の retire を道連れにしていないことの確認。
    「判断できなければ断る」を全ペインに広げると、pid が取れないペインが永久に
    残る — 安全側に倒したつもりで別の壊れ方を作る形
    (memory: fail-closed-discard-vs-hold)。
    """
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", str(tmp_path / "nonexistent-proc"))
    rec = _RecordingSubprocess(pane_pid=None)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("Ren-worker") is True
    assert rec.kill_calls()


def test_allow_foreign_is_the_documented_way_past_the_backstop(
        monkeypatch, tmp_path, as_our_checkout, their_checkout, production_env):
    """`--force` の出口は残す。

    出口の無い fail closed は「判断できない」が「何も二度と動かない」に化ける
    (memory: fail-closed-discard-vs-hold)。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", "scripts/dispatcher.sh"], str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher", allow_foreign=True) is True
    assert rec.kill_calls()


def test_herdr_kill_refuses_a_foreign_daemon_too(
        monkeypatch, tmp_path, as_our_checkout, their_checkout, production_env):
    """本番は herdr で動いているので、こちらが実害の経路。"""
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", "scripts/dispatcher.sh"], str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)

    calls = []

    def fake_run(verb, args, timeout=10):
        calls.append((verb, list(args)))
        if verb == "pane_process_info":
            return {"result": {"process_info": {"shell_pid": 4100,
                                                "foreground_processes": []}}}
        if verb == "pane_get":
            return {"result": {"pane": {"pane_id": "w1:p1"}}}
        return None

    monkeypatch.setattr(lib_mux, "_herdr_run", fake_run)
    backend = lib_mux.HerdrBackend()
    monkeypatch.setattr(backend, "_resolve_ids",
                        lambda name: {"tab_id": "w1:t1", "pane_id": "w1:p1"})

    assert backend.kill("dispatcher") is False
    assert [v for v, _ in calls if v == "tab_close"] == [], \
        f"another checkout's dispatcher tab was closed: {calls}"


def test_the_backstop_covers_every_daemon_script():
    """スクリプト名の表が `lib_daemon_watch.SCRIPT_OF` と揃っていること。

    片方にデーモンが増えてもう片方に増えないと、その新しいデーモンだけ
    ガードの外に出る — 沈黙して。
    """
    assert set(lib_mux.DAEMON_SCRIPTS) == set(dw.SCRIPT_OF.values())
    assert set(lib_mux.DAEMON_PANE_NAMES) == set(dw.DAEMONS)


# ---------------------------------------------------------------------------
# 2. 起動形態の同定 (P1-3)
# ---------------------------------------------------------------------------

def test_a_relative_launch_from_another_checkout_is_foreign_not_a_husk(
        tmp_path, our_checkout, their_checkout):
    """P1-3 の本体。

    `bash scripts/dispatcher.sh` は `/scripts/dispatcher.sh` で終わらないので、
    修正前は **husk (= 破壊を認可)** に分類されていた。これが「普通に起動した
    別チェックアウトのデーモン」を殺した形。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", "scripts/dispatcher.sh"], str(their_checkout)),
    })
    owner, detail = dw.pane_daemon_owner(
        _PaneMux(4100), dw.DAEMON_DISPATCHER, our_checkout, proc_root=proc_root)
    assert owner == dw.PANE_OWNER_FOREIGN, detail


def test_a_relative_launch_from_our_own_checkout_is_mine(
        tmp_path, our_checkout):
    """自分のチェックアウトを相対パスで起動した場合は `mine`。

    `foreign` に倒しきってしまうと restart が使えなくなるだけで安全にはならない。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", "scripts/dispatcher.sh"], str(our_checkout)),
    })
    owner, detail = dw.pane_daemon_owner(
        _PaneMux(4100), dw.DAEMON_DISPATCHER, our_checkout, proc_root=proc_root)
    assert owner == dw.PANE_OWNER_MINE, detail


def test_an_unresolvable_relative_launch_is_unknown_not_a_husk(
        tmp_path, our_checkout):
    """cwd が読めなければ「どのチェックアウトか」は決まらない。

    決まらないものを NONE (= 破壊を認可) に倒すのが、この一連の欠陥の型。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["bash", "scripts/dispatcher.sh"], None),   # cwd が無い
    })
    owner, detail = dw.pane_daemon_owner(
        _PaneMux(4100), dw.DAEMON_DISPATCHER, our_checkout, proc_root=proc_root)
    assert owner == dw.PANE_OWNER_UNKNOWN, detail


def test_an_unrecognised_launch_shape_is_unknown_not_a_husk(
        tmp_path, our_checkout, their_checkout):
    """`env FOO=1 bash scripts/dispatcher.sh` — 知らない起動の仕方。

    「認識できなかった」を「空のペイン」と書くと、知らない形が全部 **破壊側**
    に落ちる。知らないなら知らないと言うこと。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["env", "FOO=1", "bash", "scripts/dispatcher.sh"],
               str(their_checkout)),
    })
    owner, detail = dw.pane_daemon_owner(
        _PaneMux(4100), dw.DAEMON_DISPATCHER, our_checkout, proc_root=proc_root)
    assert owner == dw.PANE_OWNER_UNKNOWN, detail


def test_merely_mentioning_our_script_path_is_not_ownership(
        tmp_path, our_checkout):
    """`tail -f <our>/scripts/dispatcher.sh` は「自分のデーモン」ではない。

    修正前は **引数のどこかに** 自分のパスがあれば即 `mine` (= kill 可) だった。
    実行しているスクリプトと、ただ言及されただけのパスは別のもの。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["tail", "-f", str(our_checkout / "scripts" / "dispatcher.sh")],
               str(our_checkout)),
    })
    owner, detail = dw.pane_daemon_owner(
        _PaneMux(4100), dw.DAEMON_DISPATCHER, our_checkout, proc_root=proc_root)
    assert owner != dw.PANE_OWNER_MINE, detail
    assert owner != dw.PANE_OWNER_NONE, detail


def test_an_empty_pane_is_still_a_husk(tmp_path, our_checkout):
    """何も入っていないペインは今まで通り `none` (= restart してよい)。"""
    proc_root = _fake_proc(tmp_path, {4100: (1, ["bash"], str(tmp_path))})
    owner, detail = dw.pane_daemon_owner(
        _PaneMux(4100), dw.DAEMON_DISPATCHER, our_checkout, proc_root=proc_root)
    assert owner == dw.PANE_OWNER_NONE, detail


def test_a_python_daemon_launched_with_flags_is_still_identified(
        tmp_path, our_checkout, their_checkout):
    """`python3 -u scripts/watchdog.py --repo-root …` も同定できること。

    watchdog は python なので、bash だけ直しても片側しか守らない。
    """
    (their_checkout / "scripts" / "watchdog.py").write_text("#\n", encoding="utf-8")
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3", "-u", "scripts/watchdog.py",
                      "--repo-root", str(our_checkout)], str(their_checkout)),
    })
    owner, detail = dw.pane_daemon_owner(
        _PaneMux(4100), dw.DAEMON_WATCHDOG, our_checkout, proc_root=proc_root)
    assert owner == dw.PANE_OWNER_FOREIGN, detail


class _PaneMux:
    """pid() だけ答える mux。kill / spawn は記録するだけ。"""

    def __init__(self, pane_pid=None):
        self.pane_pid = pane_pid
        self.calls = []

    def pid(self, name):
        return self.pane_pid

    def kill(self, name, **kwargs):
        self.calls.append(("kill", name, kwargs))
        return True

    def spawn(self, name, cmd, cwd=None):
        self.calls.append(("spawn", name, {}))
        return True


def test_restart_force_reaches_the_kill_past_the_mux_backstop(
        tmp_path, their_checkout):
    """`--force` は mux 層の backstop も通り抜けられなければ意味がない。

    デーモン層だけ force を効かせて mux 層が断ると、出口があるように見えて
    実際には無い — 一番たちの悪い形。
    """
    root = tmp_path / "crewvia"
    (root / "registry" / "daemons").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / ".git").mkdir()

    mux = _PaneMux(pane_pid=4100)
    ok = dw.restart(dw.DAEMON_DISPATCHER, repo_root=root, mux=mux,
                    log=lambda m: None, force=True,
                    proc_root=_fake_proc(tmp_path, {4100: (1, ["bash"], str(tmp_path))}))
    assert ok is True
    kills = [c for c in mux.calls if c[0] == "kill"]
    assert kills, mux.calls
    assert kills[0][2].get("allow_foreign") is True, kills


# ---------------------------------------------------------------------------
# 3. respawn に隔離と名前空間を引き継ぐ (P1-2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("var", ["CREWVIA_MUX_TEST_ISOLATION",
                                 "CREWVIA_MUX_PANE_PREFIX"])
def test_spawn_command_carries_the_isolation_variables(tmp_path, var):
    """mux 経由で起動されるプロセスは **mux サーバーの env** を継承する。

    呼び出し側の env は渡らないので、隔離マーカーと名前空間はコマンド文字列に
    載せるしかない。載っていないと、名前空間付きのテストペインに起動された
    デーモンが素のペイン名 (`dispatcher`) を名指しし、保護を両方とも失う。
    """
    env = {"CREWVIA_MUX": "herdr", var: "marker-value"}
    cmd = dw.spawn_command(dw.DAEMON_DISPATCHER, tmp_path / "crewvia", env=env)
    assert f"export {var}='marker-value'" in cmd, cmd


def test_a_daemon_respawned_from_a_namespaced_pane_keeps_its_namespace(
        monkeypatch, tmp_path):
    """server の env に何も無くても、起動されたデーモンは名前空間を保つ。

    「server の env にそれらが無い状態でテストすること」(Codex P1-2) が要点なので、
    継承ではなくコマンド文字列だけで渡っていることを主張する。
    """
    root = tmp_path / "crewvia"
    (root / "scripts").mkdir(parents=True)
    env = {
        "CREWVIA_MUX": "herdr",
        "CREWVIA_HERDR_WORKSPACE": "crewvia-pytest-xyz",
        "CREWVIA_MUX_PANE_PREFIX": "pytest-xyz-",
        "CREWVIA_MUX_TEST_ISOLATION": "1",
    }
    cmd = dw.spawn_command(dw.DAEMON_WATCHDOG, root, env=env)

    # 起動本体だけ落として、その手前 (cd + export 群) をそのまま走らせる。
    prefix = cmd.rsplit("python3", 1)[0]
    out = subprocess.run(
        ["bash", "-c",
         prefix + "printenv CREWVIA_MUX_PANE_PREFIX CREWVIA_MUX_TEST_ISOLATION"],
        capture_output=True, text=True, timeout=30,
        # herdr server の env は空 — 継承では何も渡らない。
        env={"PATH": os.environ["PATH"]})
    assert out.stdout.split() == ["pytest-xyz-", "1"], (out.stdout, out.stderr, cmd)


# ---------------------------------------------------------------------------
# 4. 起動確認の三値 (P2-4)
# ---------------------------------------------------------------------------

def test_a_malformed_process_entry_is_unknown_not_live(monkeypatch):
    """`foreground_processes: [{}]` は「走っている」の証拠ではない。

    修正前は `_is_idle_shell_process({})` が False を返し、それが
    `all(...) == False` 経由で **PANE_LIVE** に写っていた。`_wait_until_launched()`
    はそれを「起動した」と読むので、何も走っていないのに respawn が成功したと
    報告される — 相手は grace と flap 枠を使い、同じ死体を見続ける。
    """
    monkeypatch.setattr(
        lib_mux, "_herdr_run",
        lambda verb, args, timeout=10: {
            "result": {"process_info": {"shell_pid": 1,
                                        "foreground_processes": [{}]}}})
    assert lib_mux.HerdrBackend()._pane_process_state("w1:p1") == lib_mux.PANE_UNKNOWN


def test_a_process_entry_without_argv_is_unknown_not_live(monkeypatch):
    """argv が無いエントリも同じ。名前だけでは何も証明できない。"""
    monkeypatch.setattr(
        lib_mux, "_herdr_run",
        lambda verb, args, timeout=10: {
            "result": {"process_info": {
                "shell_pid": 1,
                "foreground_processes": [{"name": "bash"}]}}})
    assert lib_mux.HerdrBackend()._pane_process_state("w1:p1") == lib_mux.PANE_UNKNOWN


def test_a_demonstrably_live_process_beside_an_unreadable_one_is_live(monkeypatch):
    """1 つでも確実に走っていれば、ペインは live。

    UNKNOWN に倒しきると「占有されているのに launch してよい」と読む側が
    出てくる。live の証拠がある限り live。
    """
    monkeypatch.setattr(
        lib_mux, "_herdr_run",
        lambda verb, args, timeout=10: {
            "result": {"process_info": {"shell_pid": 1, "foreground_processes": [
                {}, {"name": "claude", "argv": ["claude"]}]}}})
    assert lib_mux.HerdrBackend()._pane_process_state("w1:p1") == lib_mux.PANE_LIVE


def test_an_unknown_pane_state_fails_the_launch_verification(monkeypatch):
    """`_wait_until_launched()` は UNKNOWN を「起動した」と読まない。"""
    monkeypatch.setenv("CREWVIA_MUX_LAUNCH_VERIFY_SECONDS", "1")
    warnings = []
    assert lib_mux._wait_until_launched(
        lambda: lib_mux.PANE_UNKNOWN, warn=warnings.append, name="dispatcher") is False
    assert warnings


def test_an_idle_shell_entry_is_still_idle(monkeypatch):
    """herdr 0.9.0 が実際に返す idle の形は今まで通り idle (t035 の生存性)。"""
    monkeypatch.setattr(
        lib_mux, "_herdr_run",
        lambda verb, args, timeout=10: {
            "result": {"process_info": {"shell_pid": 1, "foreground_processes": [
                {"name": "bash", "argv": ["/bin/bash"]}]}}})
    assert lib_mux.HerdrBackend()._pane_process_state("w1:p1") == lib_mux.PANE_IDLE


# ---------------------------------------------------------------------------
# 5. 保護を消せなかったら成功と言わない (P2-5)
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path):
    d = tmp_path / "registry"
    (d / "daemons").mkdir(parents=True)
    return d


def test_forced_resume_reports_failure_when_the_marker_survives(registry):
    """読めない marker を `--force` で消しに行って、消せなかった場合。

    修正前は `unlink_quiet()` を呼んで **無条件に true**。marker がディレクトリ
    だったり権限で失敗すると、保護が残ったまま「解除しました」と答え、相互監視は
    永久に hold する。消せたかどうかを確かめること。
    """
    path = dw.pause_path(registry, dw.DAEMON_DISPATCHER)
    path.mkdir(parents=True)                  # 中身が読めず、unlink もできない
    (path / "keeps-it-undeletable").write_text("x", encoding="utf-8")

    assert dw.read_pause_state(registry, dw.DAEMON_DISPATCHER)[0] == dw.PAUSE_UNREADABLE
    assert dw.resume(registry, dw.DAEMON_DISPATCHER, force=True) is False
    assert path.exists(), "the marker is gone — then the refusal was wrong"


def test_resume_reports_failure_when_a_valid_marker_cannot_be_removed(registry):
    """token が合っていても、消せなかったなら成功ではない。

    `--force` の枝だけ直すと、同じ欠陥が普通の枝に残る。unlink の成否を見る
    という一点は両方に要る。
    """
    token = dw.pause(registry, dw.DAEMON_DISPATCHER, reason="maintenance")
    assert token
    path = dw.pause_path(registry, dw.DAEMON_DISPATCHER)
    parent = path.parent
    mode = parent.stat().st_mode
    parent.chmod(0o555)                        # 書き込み不可 = unlink できない
    try:
        assert dw.resume(registry, dw.DAEMON_DISPATCHER, token=token) is False
        assert path.exists()
    finally:
        parent.chmod(mode)


def test_resume_still_lifts_a_marker_it_can_remove(registry):
    """普通に消せる場合は今まで通り true。

    「消せなかったら false」を入れたせいで maintenance が終わらなくなる、
    という逆の壊し方をしていないことの確認。
    """
    token = dw.pause(registry, dw.DAEMON_DISPATCHER, reason="maintenance")
    assert token
    assert dw.resume(registry, dw.DAEMON_DISPATCHER, token=token) is True
    assert not dw.pause_path(registry, dw.DAEMON_DISPATCHER).exists()
    assert dw.read_pause_state(registry, dw.DAEMON_DISPATCHER)[0] == dw.PAUSE_ABSENT


# ---------------------------------------------------------------------------
# 6. 既定を「拒否」に反転する — mux 層の allowlist (t039 / Codex 4 巡目)
#
# 4 件の P1 は別々の穴に見えて 1 つの形をしている: **識別できなかったケースが
# 破壊の許可に落ちる**。個別に塞ぐと次の「思いつかなかった起動形態」でまた開く
# ので、関数の契約の方を反転させる —
#
#   破壊してよいのは MINE と、確実に空の NONE だけ。それ以外はすべて拒否。
#
# デーモン層の `_MAY_KILL_OWNERS` が既にその形なので、mux 層をそれに揃える。
# ---------------------------------------------------------------------------

def _foreign_watchdog(their_checkout):
    """別チェックアウト側に watchdog.py を実在させる。"""
    script = their_checkout / "scripts" / "watchdog.py"
    script.write_text("#\n", encoding="utf-8")
    return script


def test_both_backends_route_destruction_through_the_one_decision(monkeypatch):
    """壊してよいかを決めるのは `may_destroy_pane()` 1 箇所だけであること。

    t040 で判定の土台が「中の人の同定」から「自分が作った記録」に変わり、
    2 つあった表 (mux 層の allowlist とデーモン層の allowlist) は 1 つの関数に
    畳まれた。畳んだつもりで片方のバックエンドに独自の判断が残っていると、
    そのバックエンドだけ素通りする — `t006 QA FAIL-1` はまさにその形だった。

    なので「表が一致していること」ではなく「**そこしか見ていないこと**」を
    縛る: 判定関数を差し替えたら、両バックエンドの答えがそれに従う。
    """
    seen = []

    def _refuse_everything(name, backend, handle, pane_pid, **kwargs):
        seen.append(backend)
        return False, "stubbed refusal"

    monkeypatch.setattr(lib_mux, "may_destroy_pane", _refuse_everything)
    for backend in (lib_mux.TmuxBackend(), lib_mux.HerdrBackend()):
        assert backend._refuses_foreign_daemon("dispatcher", "@1", 1) is True
    assert seen == ["tmux", "herdr"], seen

    def _allow_everything(name, backend, handle, pane_pid, **kwargs):
        return True, "stubbed permission"

    monkeypatch.setattr(lib_mux, "may_destroy_pane", _allow_everything)
    for backend in (lib_mux.TmuxBackend(), lib_mux.HerdrBackend()):
        assert backend._refuses_foreign_daemon("dispatcher", "@1", 1) is False


def test_the_daemon_layers_early_exit_never_outranks_the_mux_layer():
    """デーモン層の事前チェックは、mux 層が拒否するものを許可しない。

    `restart()` の事前チェックは pause marker を書く前に抜けるための早期脱出で、
    壊すかどうかを決めるのは mux 層。早期脱出の方が広いと、marker を書いてから
    mux に断られる — 落ちた先に marker が残り、相互監視が黙って止まる。
    """
    assert dw._MAY_KILL_OWNERS & lib_mux._OWNERS_THAT_VETO == frozenset()


def test_kill_refuses_when_proc_cannot_be_walked_completely(
        monkeypatch, tmp_path, as_our_checkout, production_env):
    """/proc に 1 つでも読めない cmdline があれば断る (P1-1)。

    `proc_table()` は元から「1 つでも読めなければ None」と正しく答えていた。
    落ちていたのはその先 — backstop がその None を「断る理由が無い」に変換して
    いた。ペインの中身を **一度も見られていない** のだから、そこから言える
    ことは何も無い。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3", "scripts/watchdog.py"], str(tmp_path)),
    })
    unreadable = Path(proc_root) / "4200" / "cmdline"
    unreadable.chmod(0o000)
    if os.access(unreadable, os.R_OK):
        pytest.skip("running as root — an unreadable cmdline cannot be staged")
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    try:
        assert lib_mux.TmuxBackend().kill("watchdog") is False
        assert rec.kill_calls() == [], \
            f"a pane nobody could read through was killed: {rec.kill_calls()}"
    finally:
        unreadable.chmod(0o644)


def test_kill_refuses_a_daemon_under_an_unrecognised_interpreter(
        monkeypatch, tmp_path, as_our_checkout, their_checkout, production_env):
    """`python3.12 …/watchdog.py` — 表に無いインタプリタでも断る (P1-1)。

    `_INTERPRETER_NAMES` は allowlist なので `python3.12` はそこに無く、
    `script_owner()` は正しく `UNKNOWN` を返していた。にもかかわらず backstop が
    それを許可に変換していたので、**別チェックアウトの watchdog** が入った
    ペインを、きれいな env からの `mux.kill("watchdog")` が閉じられた。
    """
    _foreign_watchdog(their_checkout)
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3.12", str(their_checkout / "scripts" / "watchdog.py")],
               str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("watchdog") is False
    assert rec.kill_calls() == [], \
        f"another checkout's watchdog was killed: {rec.kill_calls()}"


def test_a_daemon_reached_through_a_renamed_symlink_is_not_a_husk(
        monkeypatch, tmp_path, as_our_checkout, their_checkout, production_env):
    """別名の symlink 越しに起動されたデーモンを「空のペイン」にしない (P1-2)。

    `python3 /theirs/monitor` — `monitor` は `/theirs/scripts/watchdog.py` を
    指す実在の symlink。argv の basename を**文字列として**先に見ていたため、
    FOREIGN でも UNKNOWN でもなく **NONE**、つまり「本当に空のペイン」に
    分類されていた。NONE は restart の allowlist が唯一通す答えなので、生きた
    他人のデーモンが husk として潰せた。

    実ファイル + 実 symlink で再現する。字面の一致ではなく、**実行しているファイル**
    が何かを見ているかどうかが問われているので。
    """
    script = _foreign_watchdog(their_checkout)
    alias = their_checkout / "monitor"
    alias.symlink_to(script)
    assert alias.resolve() == script.resolve()

    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3", str(alias)], str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    owner, detail = lib_mux.daemon_pane_owner(4100, proc_root=proc_root)
    assert owner != lib_mux.OWNER_NONE, \
        f"a live foreign daemon was classified as an empty pane: {detail}"
    assert lib_mux.TmuxBackend().kill("watchdog") is False
    assert rec.kill_calls() == [], \
        f"a daemon behind an alias was killed: {rec.kill_calls()}"


def test_a_parent_traversal_through_a_symlink_is_not_our_script(
        monkeypatch, tmp_path, as_our_checkout, their_checkout, production_env):
    """`..` を字面で畳むと、指すファイルが変わる (P1-3)。

    `/ours/link` が `/theirs/subdir` を指すとき、
    `/ours/link/../scripts/watchdog.py` が実際に動かすのは
    `/theirs/scripts/watchdog.py`。ところが `normpath()` を先に呼ぶと
    `/ours/scripts/watchdog.py` に畳まれて **MINE** — 自分のデーモンだから
    kill してよい、と両方の identity ガードが答えていた。

    これも実ファイル + 実 symlink で再現する。`normpath` と `realpath` の差は
    ファイルシステムが実在しないと出ない。
    """
    script = _foreign_watchdog(their_checkout)
    (their_checkout / "subdir").mkdir()
    link = as_our_checkout / "link"
    link.symlink_to(their_checkout / "subdir")

    traversed = f"{link}/../scripts/watchdog.py"
    assert os.path.realpath(traversed) == os.path.realpath(script), \
        "the fixture does not actually traverse the symlink"
    assert os.path.normpath(traversed) == str(as_our_checkout / "scripts" / "watchdog.py"), \
        "the fixture does not exercise the lexical collapse this is about"

    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3", traversed], str(their_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    owner, detail = lib_mux.daemon_pane_owner(4100, proc_root=proc_root)
    assert owner == lib_mux.OWNER_FOREIGN, \
        f"a path that runs another checkout's script was read as ours: {owner} {detail}"
    assert lib_mux.TmuxBackend().kill("watchdog") is False
    assert rec.kill_calls() == []


def test_an_alias_to_our_own_script_is_still_ours(
        monkeypatch, tmp_path, as_our_checkout, production_env):
    """別名越しでも、指す先が自分のスクリプトなら今まで通り kill できる。

    P1-2 / P1-3 の修正を「symlink が絡んだら全部断る」で済ませていないことの
    確認。実行しているファイルを解決した結果が自分のものなら、それは自分のもの。

    spawn 記録を置くのは t040 以降の本番と同じ形にするため — ここで確かめたい
    のは「別名が拒否の理由にならない」ことなので、記録の有無で落ちてしまうと
    確かめたい層に届かない。
    """
    alias = as_our_checkout / "monitor"
    alias.symlink_to(as_our_checkout / "scripts" / "watchdog.py")
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"], str(tmp_path)),
        4200: (4100, ["python3", str(alias)], str(as_our_checkout)),
    })
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100)
    monkeypatch.setattr(lib_mux, "subprocess", rec)
    lib_mux.write_pane_record("watchdog", "tmux", rec.window_id,
                              server=rec.server)

    assert lib_mux.TmuxBackend().kill("watchdog") is True
    assert rec.kill_calls(), "our own daemon behind an alias became un-killable"


def test_tmux_kill_targets_the_window_it_inspected_not_the_name(
        monkeypatch, tmp_path, as_our_checkout, production_env):
    """覗いた window そのものを閉じる — 名前で引き直さない (P1-4)。

    所有権の判定は真偽値しか持ち帰らず、kill は `<session>:<window 名>` を
    **もう一度**解決していた。tmux の window 名は可変で、/proc の全走査には
    実時間がかかる。その間に覗いたタブが消え、別チェックアウトが同じ名前で
    作り直すと、**一度も覗いていない後継**を閉じることになる。チェックアウト
    ローカルのデーモンロックは別チェックアウトを直列化しない。

    ここでは display-message のあとに window_id をすり替えて、その入れ替わりを
    作る。kill が不変の id (`@5`) を持ち込んでいれば、後継の `@9` には届かない。
    """
    proc_root = _fake_proc(tmp_path, {4100: (1, ["bash"], str(tmp_path))})
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)
    rec = _RecordingSubprocess(pane_pid=4100, window_id="@5", window_id_after="@9")
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().kill("dispatcher") is True
    assert rec.kill_targets() == ["@5"], (
        "the kill was not bound to the window that was inspected: "
        f"{rec.kill_targets()}")


def test_herdr_kill_closes_the_tab_it_inspected(
        monkeypatch, tmp_path, as_our_checkout, production_env):
    """herdr 側も同じ — `pid()` が解決済みなのに `_resolve_ids()` を呼び直さない。

    本番は herdr なので、実害の経路はこちら。ラベルで引き直すと、覗いたタブが
    消えたあとに同じラベルで作られた別のタブを閉じる。
    """
    proc_root = _fake_proc(tmp_path, {4100: (1, ["bash"], str(tmp_path))})
    monkeypatch.setattr(lib_mux, "_PROC_ROOT", proc_root)

    calls = []
    resolved = []
    closed = []

    def fake_run(verb, args, timeout=10):
        calls.append((verb, list(args)))
        if verb == "pane_process_info":
            return {"result": {"process_info": {"shell_pid": 4100,
                                                "foreground_processes": []}}}
        if verb == "tab_close":
            return {"result": {"ok": True}}
        return None

    def fake_close_bound(tab_id, server):
        # Since t043 the guarded path does not go through the CLI at all: the
        # request rides the connection whose peer was verified.  Stubbed here
        # so this test cannot reach a real herdr socket while asking a question
        # about *which* tab is addressed.
        closed.append(tab_id)
        return True

    monkeypatch.setattr(lib_mux, "_herdr_run", fake_run)
    monkeypatch.setattr(lib_mux, "_herdr_close_tab_bound", fake_close_bound)
    monkeypatch.setattr(lib_mux, "_herdr_server_identity",
                        lambda: ("/tmp/herdr-test.sock", "900:1"))
    backend = lib_mux.HerdrBackend()

    def shifting_ids(name):
        """2 回目からは別のタブが同じラベルを名乗っている。"""
        resolved.append(name)
        n = len(resolved)
        return {"tab_id": f"w1:t{n}", "pane_id": f"w1:p{n}"}

    monkeypatch.setattr(backend, "_resolve_ids", shifting_ids)

    assert backend.kill("dispatcher") is True
    assert closed == ["w1:t1"], \
        f"the kill closed a tab it never inspected: {closed} (resolves: {resolved})"
    assert [args for verb, args in calls if verb == "tab_close"] == [], \
        "the guarded path still went out through the herdr CLI"
