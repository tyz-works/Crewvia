#!/usr/bin/env python3
"""tests/test_mux_production_safety.py

**テストが本番の mux を壊さない**ことの回帰テスト (t037 / PR #209)。

2026-09-23、t036 の赤いテストが本番の dispatcher ペインを乗っ取り、約 4 時間半
Worker の割り当てが止まった。経緯と仕組みは `tests/conftest.py` の冒頭に書いた。

ここで固定する契約は 2 つで、**別々の証拠**に立っている (片方が破れても
もう片方が残る形にしてある。memory: fail-closed-guard-can-recreate-the-defect):

  1. **宛先と名前空間** — テスト中は、既定のセッション / ワークスペース `crewvia`
     と、既定のペイン名 `dispatcher` / `watchdog` に到達できない。
     env が無ければ `MuxTestIsolationError` で落ちる。
  2. **repo identity** — 破壊的操作 (kill) は、そのペインで走っているのが
     **自分の repo_root のデーモン**であることを確かめてから行う。別チェックアウト
     のコードから本番のデーモンを kill / 置換できない。

1 は「テスト中かどうか」に依存する。2 は依存しない — 本番同士でも効く。
どちらか片方だけでは、今回の事故はまた別の経路で起きる。

## 実行層は必ず差し替える

このファイルのテストは *本番を狙ったときに止まるか* を確かめる。止まらなければ
本当に本番へ行く。だから tmux / herdr を呼ぶ層は必ず記録用スタブに差し替え、
「何も実行されなかった」ことをテストの主張そのものにしてある。

  python3 -m pytest tests/test_mux_production_safety.py -v
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

from conftest import PRODUCTION_DESTINATION  # noqa: E402


# ---------------------------------------------------------------------------
# 実行層のスタブ
# ---------------------------------------------------------------------------

class _RecordingSubprocess:
    """`lib_mux.subprocess` の差し替え。tmux を **実行せずに** argv を記録する。

    未知の属性は本物へ委譲するので、`subprocess.CompletedProcess` などはそのまま
    使える。
    """

    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        text = kwargs.get("text", False)
        empty = "" if text else b""
        return subprocess.CompletedProcess(list(argv), 0, stdout=empty, stderr=empty)

    def __getattr__(self, item):
        return getattr(subprocess, item)

    def tmux_calls(self):
        return [c for c in self.calls if c and c[0] == "tmux"]


@pytest.fixture
def recording_tmux(monkeypatch):
    rec = _RecordingSubprocess()
    monkeypatch.setattr(lib_mux, "subprocess", rec)
    return rec


@pytest.fixture
def recording_herdr(monkeypatch):
    calls = []

    def fake_run(verb, args, timeout=10):
        calls.append((verb, list(args)))
        return None       # 「herdr が答えなかった」= どの verb も副作用なしで失敗

    monkeypatch.setattr(lib_mux, "_herdr_run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# 1. 宛先と名前空間 — 既定値に到達できない
# ---------------------------------------------------------------------------

_MUTATIONS = [
    ("kill", lambda b, name: b.kill(name)),
    ("spawn", lambda b, name: b.spawn(name, "echo hi", cwd="/tmp")),
    ("send", lambda b, name: b.send(name, "hello")),
]


@pytest.mark.parametrize("verb,call", _MUTATIONS, ids=[v for v, _ in _MUTATIONS])
@pytest.mark.parametrize("name", [dw.DAEMON_DISPATCHER, dw.DAEMON_WATCHDOG])
def test_tmux_mutations_refuse_the_production_destination(
        recording_tmux, production_destination, verb, call, name):
    """本番セッション `crewvia` の `dispatcher` / `watchdog` は、テスト中は触れない。

    これが 2026-09-23 に起きたことそのもの: `--repo-root` は隔離されていたのに、
    宛先は周囲の env から来ていて本番だった。

    「何も実行されなかった」まで主張するのが肝心で、例外だけを見ていると
    kill が通ったあとに投げても緑になる。
    """
    backend = lib_mux.TmuxBackend()
    with pytest.raises(lib_mux.MuxTestIsolationError) as excinfo:
        call(backend, name)
    assert recording_tmux.tmux_calls() == [], \
        f"{verb} reached tmux before refusing: {recording_tmux.tmux_calls()}"
    assert PRODUCTION_DESTINATION in str(excinfo.value)


@pytest.mark.parametrize("verb,call", _MUTATIONS, ids=[v for v, _ in _MUTATIONS])
def test_herdr_mutations_refuse_the_production_workspace(
        recording_herdr, production_destination, verb, call):
    """herdr 側も同じ契約。本番は herdr で動いているので、こちらが実害の経路。"""
    backend = lib_mux.HerdrBackend()
    with pytest.raises(lib_mux.MuxTestIsolationError):
        call(backend, dw.DAEMON_DISPATCHER)
    assert recording_herdr == [], \
        f"{verb} reached herdr before refusing: {recording_herdr}"


def test_a_missing_pane_prefix_is_refused_even_on_an_isolated_destination(
        recording_tmux, monkeypatch):
    """宛先だけ隔離しても、接頭辞が無ければ既定のペイン名を名乗ってしまう。

    「env が無ければ落ちる」の env は 2 つあり、片方だけでは通さない。
    """
    monkeypatch.setenv("CREWVIA_TMUX_SESSION", "crewvia-somewhere-else")
    monkeypatch.setenv("CREWVIA_MUX_PANE_PREFIX", "")
    with pytest.raises(lib_mux.MuxTestIsolationError):
        lib_mux.TmuxBackend().kill(dw.DAEMON_DISPATCHER)
    assert recording_tmux.tmux_calls() == []


def test_under_isolation_the_pane_name_is_namespaced(recording_tmux):
    """conftest が効いている通常のテストでは、`dispatcher` は本番の `dispatcher`
    ではない別のペインに解決される。

    テスト作者が何も意識しなくても既定名に届かない、というのがこの層の役目。
    """
    prefix = os.environ["CREWVIA_MUX_PANE_PREFIX"]
    assert prefix, "conftest did not install a pane prefix"

    lib_mux.TmuxBackend().kill(dw.DAEMON_DISPATCHER)
    targets = [c[-1] for c in recording_tmux.tmux_calls() if "kill-window" in c]
    assert targets, recording_tmux.tmux_calls()
    for target in targets:
        session, _, pane = target.partition(":")
        assert session != PRODUCTION_DESTINATION, target
        assert pane == f"{prefix}{dw.DAEMON_DISPATCHER}", target


def test_list_hides_the_namespace_from_callers(monkeypatch):
    """接頭辞は mux 層の内側だけの話。呼び手が見る名前は素のままでなければ、
    dispatcher の Worker 照合 (`-worker` 接尾辞) が静かに壊れる。"""
    prefix = os.environ["CREWVIA_MUX_PANE_PREFIX"]
    rec = _RecordingSubprocess()

    def fake_run(argv, **kwargs):
        rec.calls.append(list(argv))
        if "list-windows" in argv:
            out = f"{prefix}Ren-worker\n{prefix}dispatcher\nsomeone-elses-window\n"
            return subprocess.CompletedProcess(list(argv), 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

    rec.run = fake_run
    monkeypatch.setattr(lib_mux, "subprocess", rec)

    assert lib_mux.TmuxBackend().list() == ["Ren-worker", "dispatcher"]
    assert lib_mux.TmuxBackend().list(suffix="-worker") == ["Ren-worker"]


def test_production_keeps_its_bare_names_and_no_guard(monkeypatch, recording_tmux):
    """本番 (= 隔離マーカーが無い) では、この層は完全な no-op でなければならない。

    接頭辞が既定で空であること、ガードが本番を止めないこと。ここが崩れると
    「テストを守るための機構が本番を壊す」という一番まずい壊れ方をする。
    """
    for var in ("CREWVIA_MUX_TEST_ISOLATION", "PYTEST_CURRENT_TEST",
                "CREWVIA_MUX_PANE_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CREWVIA_TMUX_SESSION", PRODUCTION_DESTINATION)

    assert lib_mux.TmuxBackend().kill(dw.DAEMON_DISPATCHER) is True
    targets = [c[-1] for c in recording_tmux.tmux_calls() if "kill-window" in c]
    assert targets == [f"{PRODUCTION_DESTINATION}:{dw.DAEMON_DISPATCHER}"], targets


# ---------------------------------------------------------------------------
# 2. 事故そのものの形 — subprocess で走る CLI
# ---------------------------------------------------------------------------

_TMUX_STUB = """#!/usr/bin/env bash
# 記録するだけの tmux。実行はしない。
printf '%s\\n' "$*" >> "$TMUX_STUB_LOG"
# pane の shell pid を聞かれたら、呼び出し元の python 自身を返す。実在する pid
# なので repo identity ガードが「読めない」に落ちず、その子孫に dispatcher.sh は
# 居ないので「誰も居ない (= husk)」と正しく判定される。init (pid 1) を返すと
# **本番のデーモンが子孫に入ってしまい**、本番の稼働状況でテストが揺れる。
if [ "$1" = "display-message" ]; then printf '%s\\n' "$PPID"; fi
exit 0
"""


@pytest.fixture
def stub_tmux(tmp_path):
    """PATH に置く記録専用の `tmux`。本物は一切呼ばれない。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "tmux"
    stub.write_text(_TMUX_STUB, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "tmux-calls.log"
    log.write_text("", encoding="utf-8")
    return {"bindir": bindir, "log": log}


@pytest.fixture
def healthy_repo(tmp_path):
    """restart() が **kill まで到達できる** checkout。

    `blocked_marker_repo` (マーカーが書けない) ではここに来る前に断られてしまい、
    確かめたい「本番へ向かう kill」を通らない。
    """
    root = tmp_path / "crewvia"
    (root / "registry" / "daemons").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / ".git").mkdir()
    return root


def test_the_incident_shape_is_refused_end_to_end(stub_tmux, healthy_repo):
    """2026-09-23 の再現: pytest から subprocess で CLI を起こし、env は本番を指す。

    隔離されているのは `--repo-root` だけ。宛先は周囲の env から来るので、
    ここで止まらなければ本番の dispatcher ペインが死ぬ。

    非ゼロで終わることと、**tmux が一度も呼ばれていない**ことの両方を見る。
    """
    env = dict(os.environ)
    env["PATH"] = f"{stub_tmux['bindir']}:{env['PATH']}"
    env["TMUX_STUB_LOG"] = str(stub_tmux["log"])
    env["CREWVIA_MUX"] = "tmux"
    env["CREWVIA_TMUX_SESSION"] = PRODUCTION_DESTINATION
    env["CREWVIA_MUX_PANE_PREFIX"] = ""
    env["CREWVIA_MUX_TEST_ISOLATION"] = "1"

    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "lib_daemon_watch.py"), "restart",
         dw.DAEMON_DISPATCHER, "--repo-root", str(healthy_repo)],
        capture_output=True, text=True, timeout=60, env=env)

    assert stub_tmux["log"].read_text(encoding="utf-8") == "", (
        "the CLI drove tmux against the production session:\n"
        + stub_tmux["log"].read_text(encoding="utf-8"))
    assert out.returncode != 0, out
    assert "Traceback" not in out.stderr, out.stderr
    assert PRODUCTION_DESTINATION in out.stderr, out.stderr
    # マーカーを残したまま帰ると相互監視が止まる (stale-pause の 30 分コース)。
    assert not dw.pause_path(healthy_repo / "registry", dw.DAEMON_DISPATCHER).exists()


def test_the_isolated_shape_still_works_end_to_end(stub_tmux, healthy_repo):
    """隔離された宛先なら、同じ CLI は今まで通り動く。

    ガードが「テストでは何もできない」になっていないことの確認。これが無いと、
    安全側に倒したつもりで実 mux の回帰テストを全部殺していたのに気付けない。
    """
    env = dict(os.environ)
    env["PATH"] = f"{stub_tmux['bindir']}:{env['PATH']}"
    env["TMUX_STUB_LOG"] = str(stub_tmux["log"])
    env["CREWVIA_MUX"] = "tmux"
    env["CREWVIA_TMUX_SESSION"] = "crewvia-pytest-isolated"
    env["CREWVIA_MUX_PANE_PREFIX"] = "pytest-isolated-"
    env["CREWVIA_MUX_TEST_ISOLATION"] = "1"

    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "lib_daemon_watch.py"), "restart",
         dw.DAEMON_DISPATCHER, "--repo-root", str(healthy_repo)],
        capture_output=True, text=True, timeout=60, env=env)

    log = stub_tmux["log"].read_text(encoding="utf-8")
    assert "kill-window" in log, (out.stdout, out.stderr, log)
    assert "pytest-isolated-dispatcher" in log, log
    assert f"{PRODUCTION_DESTINATION}:{dw.DAEMON_DISPATCHER}" not in log, log


# ---------------------------------------------------------------------------
# 3. repo identity — 隔離マーカーに依存しない 2 本目の証拠
# ---------------------------------------------------------------------------

class _PaneMux:
    """pid() だけ答える mux。kill / spawn は記録するだけ。"""

    def __init__(self, pane_pid=None):
        self.pane_pid = pane_pid
        self.calls = []

    def pid(self, name):
        return self.pane_pid

    def kill(self, name):
        self.calls.append(("kill", name))
        return True

    def spawn(self, name, cmd, cwd=None, env=None):
        self.calls.append(("spawn", name))
        return True


def _fake_proc(tmp_path, entries):
    """`{pid: (ppid, [argv...])}` から偽 /proc を作る。"""
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    for pid, (ppid, argv) in entries.items():
        d = proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        # /proc/<pid>/stat: pid (comm) state ppid ...
        (d / "stat").write_text(f"{pid} (bash) S {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1234",
                                encoding="utf-8")
    return str(proc)


def test_restart_refuses_to_kill_a_pane_owned_by_another_checkout(
        healthy_repo, tmp_path):
    """別チェックアウトのデーモンが入っているペインは kill しない。

    これが 2026-09-23 に**本当に起きた**関係: ペインの中で走っていたのは
    `/home/tkadmin/workspace/crewvia/scripts/dispatcher.sh` で、殺しに来たのは
    `/tmp/pytest-.../crewvia` を repo_root と思っているプロセスだった。

    この判定は「テスト中か」を一切見ない。本番同士の誤射にも同じように効く。
    """
    other = "/home/tkadmin/workspace/crewvia"
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"]),                                     # ペインのシェル
        4200: (4100, ["bash", f"{other}/scripts/dispatcher.sh"]),  # 他人のデーモン
    })

    mux = _PaneMux(pane_pid=4100)
    ok = dw.restart(dw.DAEMON_DISPATCHER, repo_root=healthy_repo, mux=mux,
                    log=lambda m: None, proc_root=proc_root)

    assert ok is False
    assert mux.calls == [], \
        f"a daemon from {other} was killed by a run rooted at {healthy_repo}"
    assert not dw.pause_path(healthy_repo / "registry", dw.DAEMON_DISPATCHER).exists()


def test_restart_kills_a_pane_that_holds_our_own_daemon(healthy_repo, tmp_path):
    """自分の repo_root のデーモンなら、今まで通り restart できる。

    上のガードが「常に断る」になっていないことの確認 (それでは restart が
    使えなくなるだけで、安全にはならない)。
    """
    proc_root = _fake_proc(tmp_path, {
        4100: (1, ["bash"]),
        4200: (4100, ["bash", str(Path(healthy_repo) / "scripts" / "dispatcher.sh")]),
    })
    mux = _PaneMux(pane_pid=4100)
    ok = dw.restart(dw.DAEMON_DISPATCHER, repo_root=healthy_repo, mux=mux,
                    log=lambda m: None, proc_root=proc_root)

    assert ok is True
    assert [c[0] for c in mux.calls] == ["kill", "spawn"], mux.calls


def test_restart_still_works_on_a_husk_pane(healthy_repo, tmp_path):
    """プロセスが死んで pane だけ残った形 (husk) は restart できなければならない。

    t035 が足した生存性。repo identity ガードでこれを塞ぐと、普通のクラッシュから
    手で復旧する道が消える。
    """
    proc_root = _fake_proc(tmp_path, {4100: (1, ["bash"])})
    mux = _PaneMux(pane_pid=4100)
    ok = dw.restart(dw.DAEMON_DISPATCHER, repo_root=healthy_repo, mux=mux,
                    log=lambda m: None, proc_root=proc_root)

    assert ok is True
    assert [c[0] for c in mux.calls] == ["kill", "spawn"], mux.calls


def test_restart_refuses_when_the_pane_owner_cannot_be_determined(
        healthy_repo, tmp_path):
    """読めなかったものを「誰も居ない」と読み替えない。

    /proc が歩けないときに kill を通すと、ガードは「読めるときだけ効く」ものに
    なる — 本番が壊れるのはたいてい読めないときなので、それでは意味が無い。
    逃げ道は --force で、黙って倒れない。
    """
    mux = _PaneMux(pane_pid=4100)
    ok = dw.restart(dw.DAEMON_DISPATCHER, repo_root=healthy_repo, mux=mux,
                    log=lambda m: None, proc_root=str(tmp_path / "no-such-proc"))
    assert ok is False
    assert mux.calls == []

    mux2 = _PaneMux(pane_pid=4100)
    assert dw.restart(dw.DAEMON_DISPATCHER, repo_root=healthy_repo, mux=mux2,
                      log=lambda m: None, proc_root=str(tmp_path / "no-such-proc"),
                      force=True) is True
    assert [c[0] for c in mux2.calls] == ["kill", "spawn"], mux2.calls
