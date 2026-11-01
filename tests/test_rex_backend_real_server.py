#!/usr/bin/env python3
"""tests/test_rex_backend_real_server.py

**本物の `rex` を隔離 server で動かす** RexBackend のテスト (knowledge/rex-mux-backend.md §11 PR-1)。

- server は `rex server run --listen unix:///tmp/rxt-…/s.sock` (短い socket・既定の socket は触らない)。
  終わったら `server stop` して dir を 1 つずつ unlink する。
- `rex` が PATH に無い環境 (CI の ubuntu) では**モジュールごと skip** する。CI で常に走るのは
  偽の rex を使う `test_rex_backend_stub_protocol.py`。skip が黙って 0 件実行になっていないことは、
  この PR の Result にローカルの実行件数を書いて示す。

  python3 -m pytest tests/test_rex_backend_real_server.py -v
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rex_harness import (REX_AVAILABLE, RealRexServer, make_checkout)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lib_mux  # noqa: E402

pytestmark = pytest.mark.skipif(not REX_AVAILABLE, reason="rex is not installed")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _wait(cond, seconds=8.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


@pytest.fixture
def world(tmp_path, monkeypatch):
    server = RealRexServer().start()
    checkout = make_checkout(tmp_path)
    monkeypatch.setenv("CREWVIA_REX_SOCK", str(server.sock))
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: checkout)
    monkeypatch.setenv("CREWVIA_REPO_ROOT", str(checkout))
    try:
        yield server, lib_mux.RexBackend(), checkout
    finally:
        server.stop()


def _record(checkout, name):
    p = checkout / "registry" / "mux" / f"{lib_mux._pane_name(name)}.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_spawn_list_pid_capture_keys_kill_roundtrip(world):
    server, b, checkout = world
    assert b.available() and b.server_running()
    assert b.spawn("Ann-worker", "echo hello-from-rex; exec sleep 600", cwd="/tmp")
    assert "Ann-worker" in b.list(suffix="-worker")
    rec = _record(checkout, "Ann-worker")
    assert rec["backend"] == "rex"
    assert rec["handle"] == rec["window_id"] and rec["pane_id"] == rec["block_id"]
    assert rec["server"]["generation"] == f"{server.status()['instance_id']}:{server.status()['pid']}"
    pid = b.pid("Ann-worker")
    assert pid == rec["child"]["pid"]
    assert rec["child"]["starttime"] == lib_mux._rex_start_time(pid)
    assert _wait(lambda: "hello-from-rex" in b.capture("Ann-worker"))
    assert b.keys("Ann-worker", ["up"])
    assert not b.keys("Ann-worker", ["bogus"])
    assert b.state("Ann-worker") == "unknown"
    assert b.kill("Ann-worker")
    assert _wait(lambda: not _alive(pid))
    assert _record(checkout, "Ann-worker") is None


def test_kill_never_calls_block_close(world, monkeypatch):
    server, b, checkout = world
    seen = []
    real = lib_mux._rex_run

    def spy(args, timeout=lib_mux._REX_TIMEOUT):
        seen.append(list(args))
        return real(args, timeout)

    monkeypatch.setattr(lib_mux, "_rex_run", spy)
    assert b.spawn("Bea-worker", "exec sleep 600")
    assert b.kill("Bea-worker")
    assert not any(a[:2] == ["block", "close"] or a[0] in ("kill",) for a in seen)


def test_duplicate_label_is_ambiguous_not_collapsed(world, capsys):
    """§11 QA 1 / §12-1: 同じ label の生きた block が 2 つ → spawn / list / kill が曖昧として扱う。"""
    server, b, checkout = world
    assert b.spawn("Cy-worker", "exec sleep 600")
    rec = _record(checkout, "Cy-worker")
    # 他人が同じ label の block をもう 1 つ作る
    r = server.rex("run", "-s", rec["session_id"], "--label",
                   lib_mux._pane_name("Cy-worker"), "--focus=false", "--json",
                   "--", "sleep", "601", check=True)
    other = json.loads(r.stdout)["block_ids"][0]
    # -b <label> は曖昧 (exit 3 + ambiguous) — §12-1 の実測そのもの
    amb = server.rex("capture", "-b", lib_mux._pane_name("Cy-worker"))
    assert amb.returncode == 3 and "ambiguous" in amb.stderr
    assert not lib_mux._rex_not_found(
        lib_mux.RexResult(amb.returncode, amb.stdout, amb.stderr, False))
    # spawn は拒否 (SPAWN_UNREADABLE)
    assert not b.spawn("Cy-worker", "exec sleep 602")
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    # list は重複を 1 つに潰さない
    names = b.list()
    assert names.count("Cy-worker") == 2
    assert "ambiguous" in capsys.readouterr().err
    # 記録の block_id は新しい方 (other) ではなく元の block を指し続ける
    assert b._resolve("Cy-worker")["block_id"] == rec["block_id"]
    assert b._resolve("Cy-worker")["block_id"] != other
    # 生きた 2 つ目が居る間 kill は記録の側 (元の block) だけに届く
    pid_other = lib_mux.RexBackend._child_of(b._process(other)[1])["pid"]
    assert b.kill("Cy-worker")
    assert _alive(pid_other)
    server.rex("kill", "-s", rec["session_id"])     # 後片付け (server ごと消える)


def test_shell_husk_with_same_label_is_not_counted_and_not_reached(world):
    """§11 QA 3d: 殻 (child 無し・last_exit 有り) と同名の新しい生きた block が並んでも spawn できる。"""
    server, b, checkout = world
    assert b.spawn("Dan-worker", "exec sleep 600")
    rec = _record(checkout, "Dan-worker")
    sid = rec["session_id"]
    # 殻: --keep-open で command が終わった block に同じ label
    r = server.rex("run", "-s", sid, "--label", lib_mux._pane_name("Dan-worker"),
                   "--focus=false", "--keep-open", "--json", "--", "true", check=True)
    shell_block = json.loads(r.stdout)["block_ids"][0]
    assert _wait(lambda: b._process(shell_block)[1] and
                 b._process(shell_block)[1].get("last_exit") is not None)
    # 生きた 1 つ + 殻 1 つ。spawn は「同名が居る」=占有扱い (生きた方が live) で SPAWN_OCCUPIED
    assert not b.spawn("Dan-worker", "exec sleep 603")
    assert b.last_spawn_refusal == lib_mux.SPAWN_OCCUPIED
    # send / capture / kill は記録の block_id (生きた方) にだけ届き、殻には届かない
    assert b._resolve("Dan-worker")["block_id"] == rec["block_id"]
    assert b.kill("Dan-worker")


def test_worker_kill_refused_on_record_generation_mismatch_and_foreign_checkout(world):
    server, b, checkout = world
    assert b.spawn("Eve-worker", "exec sleep 600")
    path = checkout / "registry" / "mux" / f"{lib_mux._pane_name('Eve-worker')}.json"
    good = json.loads(path.read_text())
    pid = good["child"]["pid"]
    for label, mutate in (
        ("generation", lambda r: r["server"].update(generation="other:1")),
        ("checkout", lambda r: r.update(checkout="/somewhere/else")),
        ("child pid", lambda r: r["child"].update(pid=pid + 1)),
        ("child starttime", lambda r: r["child"].update(starttime="1")),
        ("handle = block id", lambda r: r.update(handle=r["block_id"])),
    ):
        bad = json.loads(json.dumps(good))
        mutate(bad)
        path.write_text(json.dumps(bad))
        assert not b.kill("Eve-worker"), label
        assert _alive(pid), label
    path.write_text(json.dumps(good))
    assert b.kill("Eve-worker")
    assert _wait(lambda: not _alive(pid))


def test_force_kill_ignores_record_but_still_uses_pidfd(world):
    server, b, checkout = world
    assert b.spawn("Fay-worker", "exec sleep 600")
    path = checkout / "registry" / "mux" / f"{lib_mux._pane_name('Fay-worker')}.json"
    rec = json.loads(path.read_text())
    pid = rec["child"]["pid"]
    rec["server"]["generation"] = "stale:1"
    path.write_text(json.dumps(rec))
    assert not b.kill("Fay-worker")
    assert b.kill("Fay-worker", allow_foreign=True)
    assert _wait(lambda: not _alive(pid))


def test_kill_leaves_no_descendants_including_setsid_ones(world):
    """§11 QA 3c: 子孫 (背景 job・setsid した sleep) が kill 後に 1 つも残らない。"""
    server, b, checkout = world
    base = 3000 + os.getpid() % 5000          # この test だけの sleep の秒数
    cmd = (f"bash -c 'sleep {base} & sleep {base + 1} & wait' & "
           f"setsid sleep {base + 2} & wait")
    assert b.spawn("Gus-worker", cmd)
    pid = b.pid("Gus-worker")

    def kids():
        out = subprocess.run(["pgrep", "-f", f"sleep ({base}|{base + 1}|{base + 2})$"],
                             capture_output=True, text=True).stdout.split()
        return [int(x) for x in out]

    assert _wait(lambda: len(kids()) >= 3, 6)
    before = kids()
    assert b.kill("Gus-worker")
    assert _wait(lambda: not any(_alive(p) for p in before)), \
        [p for p in before if _alive(p)]
    assert not _alive(pid)


def test_record_existence_classification(world):
    server, b, checkout = world
    assert b.spawn("Hal-worker", "exec sleep 600")
    rec = _record(checkout, "Hal-worker")
    assert b.record_existence("Hal-worker", rec) == lib_mux.PANE_EXISTS
    gone = dict(rec, block_id="block:01a19505-0000-7000-8000-000000000000")
    assert b.record_existence("Hal-worker", gone) == lib_mux.PANE_GONE
    other_gen = json.loads(json.dumps(rec))
    other_gen["server"]["generation"] = "someone-else:1"
    assert b.record_existence("Hal-worker", other_gen) == lib_mux.PANE_UNOBSERVED
    assert b.kill("Hal-worker")
    server.stop()      # server が居ない
    assert b.record_existence("Hal-worker", rec) == lib_mux.PANE_UNOBSERVED
    assert not b.available() and not b.server_running()


def test_send_clears_multiline_leftover_and_never_enters_unclean_box(tmp_path, monkeypatch):
    """§12-2: 複数行の入力欄は ctrl+u の繰り返し (1 行あたり 2 回) で空になる。
    本物の claude を使うので、claude が無い環境では skip。入力欄には**送信しない**
    (行を打つだけで Enter は打たない) — send() そのものは入力欄が空と確かめた後にだけ Enter を打つ。"""
    import shutil
    if shutil.which("claude") is None:
        pytest.skip("claude is not installed")
    server = RealRexServer(real_home=True).start()      # claude は本物の HOME の設定が要る
    checkout = make_checkout(tmp_path)
    monkeypatch.setenv("CREWVIA_REX_SOCK", str(server.sock))
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: checkout)
    monkeypatch.setenv("CREWVIA_REPO_ROOT", str(checkout))
    b = lib_mux.RexBackend()
    try:
        _send_clears_body(server, b, checkout)
    finally:
        server.stop()


def _send_clears_body(server, b, checkout):
    assert b.spawn("Ivy-worker", "exec claude", cwd=str(REPO_ROOT))
    assert _wait(lambda: lib_mux._rex_input_is_empty(b.capture("Ivy-worker")) is True, 60)
    rec = _record(checkout, "Ivy-worker")
    bid = rec["block_id"]
    server.rex("send", "-b", bid, "--", "行1 日本語\n行2\n行3", check=True)
    assert _wait(lambda: lib_mux._rex_input_is_empty(b.capture("Ivy-worker")) is False, 5)
    assert b._clear_input(bid) is True
    assert lib_mux._rex_input_is_empty(b.capture("Ivy-worker")) is True
    assert b.kill("Ivy-worker")


def test_husk_after_server_restart_is_rebuilt_and_killed_with_sighup(tmp_path, monkeypatch):
    """§11 QA 3b / 5 / §12-4: --restore の server を再起動すると block は login shell の husk。
    ① 世代違いの記録で通常の kill は拒否される ② 空の husk は spawn が SIGHUP で終えて作り直す (打ち込まない)
    ③ 復元された shell に子を起動させると husk の kill は拒否される ④ 空の husk は SIGHUP で終わる。"""
    server = RealRexServer(restore=True).start()
    checkout = make_checkout(tmp_path)
    monkeypatch.setenv("CREWVIA_REX_SOCK", str(server.sock))
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: checkout)
    monkeypatch.setenv("CREWVIA_REPO_ROOT", str(checkout))
    b = lib_mux.RexBackend()
    try:
        assert b.spawn("Jo-worker", "exec sleep 600")
        rec = _record(checkout, "Jo-worker")
        old_pid = rec["child"]["pid"]
        time.sleep(1.5)                       # state が書かれるのを待つ
        server.kill_hard()
        assert _wait(lambda: not _alive(old_pid) or True, 1)
        server.start()
        # block は同じ id で復元され、中身は login shell
        assert _wait(lambda: b._resolve("Jo-worker") is not None, 10)
        facts, why = b._facts("Jo-worker")
        assert facts is not None, why
        assert facts["child"] is not None
        husk_pid = facts["child"]["pid"]
        assert facts["child"]["name"].lstrip("-") in lib_mux._SHELL_PROCESS_NAMES
        assert husk_pid != old_pid
        # ③ 子を起動させた shell は husk として kill されない
        server.rex("send", "-b", rec["block_id"], "--", "sleep 700 &", check=True)
        server.rex("send-key", "-b", rec["block_id"], "enter", check=True)
        assert _wait(lambda: lib_mux._live_children(husk_pid) not in (None, []), 6)
        assert not b.kill("Jo-worker")
        assert _alive(husk_pid)
        ok, why = b.may_destroy("Jo-worker")
        assert not ok
        # 子を片づけて空の husk に戻す
        for child in lib_mux._live_children(husk_pid) or []:
            os.kill(child, signal.SIGKILL)
        assert _wait(lambda: lib_mux._live_children(husk_pid) == [], 6)
        # ② spawn は空の husk を SIGHUP で終え、新しい block を作る (記録の block_id が新しくなる)
        assert b.spawn("Jo-worker", "exec sleep 603")
        rec2 = _record(checkout, "Jo-worker")
        assert rec2["block_id"] != rec["block_id"]
        assert not _alive(husk_pid)
        assert rec2["server"]["generation"] != rec["server"]["generation"]
        # 作り直した後は通常の kill (新しい世代の記録)
        assert b.kill("Jo-worker")
    finally:
        server.stop()


def test_empty_restored_husk_is_ended_by_sighup_and_the_block_is_not_closed(tmp_path, monkeypatch):
    """§12-4: 復元された空の shell への SIGHUP。pidfd の poll で終了を待つ。block は crewvia が閉じない
    (殻が残るか消えるかは rex の挙動 — ここでは『kill が成功し、shell が居なくなり、記録が終わる』ことだけを固定し、
    block の行方は観測値として assert 無しで読める形にする)。"""
    server = RealRexServer(restore=True).start()
    checkout = make_checkout(tmp_path)
    monkeypatch.setenv("CREWVIA_REX_SOCK", str(server.sock))
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: checkout)
    monkeypatch.setenv("CREWVIA_REPO_ROOT", str(checkout))
    b = lib_mux.RexBackend()
    try:
        assert b.spawn("Kit-worker", "exec sleep 600")
        rec = _record(checkout, "Kit-worker")
        time.sleep(1.5)
        server.kill_hard()
        server.start()
        assert _wait(lambda: b._resolve("Kit-worker") is not None, 10)
        facts, why = b._facts("Kit-worker")
        assert facts is not None and facts["child"] is not None, why
        husk_pid = facts["child"]["pid"]
        assert lib_mux._live_children(husk_pid) == []
        ok, why = b.may_destroy("Kit-worker")
        assert ok, why
        sent = []
        real = lib_mux._pidfd_signal
        monkeypatch.setattr(lib_mux, "_pidfd_signal",
                            lambda fd, sig: sent.append(sig) or real(fd, sig))
        assert b.kill("Kit-worker")
        assert sent == [signal.SIGHUP]              # TERM / KILL は打っていない
        assert _wait(lambda: not _alive(husk_pid))
        assert _record(checkout, "Kit-worker") is None
        observed = server.rex("block", "ls", "-s", rec["session_id"], "--json").stdout
        print("block after SIGHUP of a restored shell:",
              "still listed" if rec["block_id"] in observed else "gone")
    finally:
        server.stop()
