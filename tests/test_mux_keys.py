#!/usr/bin/env python3
"""tests/test_mux_keys.py

`lib_mux.py keys <name> <key>...` — 選択ダイアログ用のキー送信 verb (t028 / backlog #16)。

## なぜ要るのか

選択ダイアログ (trust prompt / 承認メニュー) に `send` で数字を送ると、カーソルは動かず、
続く Enter が **先頭の項目**を選ぶ。Director が手でこれを踏んでいた。`keys` は
**名前付きキーだけ**を受ける (`Down Down Enter`)。テキストは受けない — 受けると
数字がまた「押したつもりのキー」になる。

## 固定する契約

  1. **語彙** — 未知のキー名が 1 つでもあれば**何も送らず**失敗する (tmux は未知の語を
     文字として打ってしまうので、通すと部分的に届く)。
  2. **`send` の付加動作を持ち込まない** — C-u で入力行を消さない・頼まれていない Enter を
     足さない。ダイアログの上で余計なキーを押さないのがこの verb の目的。
  3. **本番の宛先を名指しできない** — 他の verb と同じ `MuxTestIsolationError` に乗る
     (tests/test_mux_production_safety.py の `_MUTATIONS` にも入れてある)。
  4. **実物** — tmux は private socket の実 tmux で、herdr は PATH の偽 herdr で、
     argv が本当にその形で出ていくことを確かめる。

tmux / herdr を呼ぶ層はどのテストも記録用スタブか private socket に向けてあり、
本番の mux (workspace `crewvia`) には届かない。

  python3 -m pytest tests/test_mux_keys.py -v
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_mux  # noqa: E402

from conftest import PRODUCTION_DESTINATION  # noqa: E402

REAL_TMUX = shutil.which("tmux")


def _executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---------------------------------------------------------------------------
# 1. 語彙
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("backend,expected", [
    ("tmux",  ["Down", "Down", "Enter"]),
    ("herdr", ["down", "down", "enter"]),
])
def test_translate_keys_speaks_each_backends_spelling(backend, expected):
    assert lib_mux.translate_keys(["Down", "Down", "Enter"], backend) == (expected, [])


def test_translate_keys_is_case_insensitive_and_maps_escape():
    assert lib_mux.translate_keys(["dOwN", "ESC", "escape"], "tmux") == \
        (["Down", "Escape", "Escape"], [])
    # herdr's canonical Escape is `esc` (`herdr pane send-keys --help`).
    assert lib_mux.translate_keys(["Escape"], "herdr") == (["esc"], [])


@pytest.mark.parametrize("bad", ["2", "1", "y", "hello", "C-c", "Down Down", "", "Enter;"])
def test_translate_keys_refuses_anything_that_is_not_a_named_key(bad):
    """数字も文字も受けない — それが「数字を送ったのに先頭が選ばれた」の再発防止。"""
    translated, unknown = lib_mux.translate_keys(["Down", bad], "tmux")
    assert translated == [], "a bad key must not leave a usable partial result"
    assert unknown == [bad]


def test_translate_keys_refuses_an_empty_sequence():
    translated, unknown = lib_mux.translate_keys([], "tmux")
    assert translated == [] and unknown


# ---------------------------------------------------------------------------
# 2. tmux — 実行層を記録用に差し替えて argv を見る
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.returncode,
                                           stdout="", stderr="no such window")

    def __getattr__(self, item):
        return getattr(subprocess, item)


@pytest.fixture
def recording_tmux(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(lib_mux, "subprocess", rec)
    return rec


def test_tmux_keys_is_one_send_keys_call_with_only_the_named_keys(recording_tmux):
    assert lib_mux.TmuxBackend().keys("Ren-worker", ["Down", "Down", "Enter"]) is True

    assert len(recording_tmux.calls) == 1, recording_tmux.calls
    argv = recording_tmux.calls[0]
    assert argv[:2] == ["tmux", "send-keys"]
    assert argv[argv.index("-t") + 1].endswith(f":{os.environ['CREWVIA_MUX_PANE_PREFIX']}Ren-worker")
    assert argv[argv.index("-t") + 2:] == ["Down", "Down", "Enter"]
    # `send()` clears the input line first; here that would be a keystroke nobody asked for.
    assert "C-u" not in argv


def test_tmux_keys_does_not_add_an_enter(recording_tmux):
    lib_mux.TmuxBackend().keys("Ren-worker", ["Down"])
    assert recording_tmux.calls[0][-1] == "Down"
    assert "Enter" not in recording_tmux.calls[0]


def test_tmux_keys_refuses_an_unknown_key_before_touching_tmux(recording_tmux, capsys):
    assert lib_mux.TmuxBackend().keys("Ren-worker", ["Down", "2", "Enter"]) is False
    assert recording_tmux.calls == [], "nothing may be sent when any key is unknown"
    assert "'2'" in capsys.readouterr().err


def test_tmux_keys_reports_a_failed_send_keys(monkeypatch, capsys):
    monkeypatch.setattr(lib_mux, "subprocess", _Recorder(returncode=1))
    assert lib_mux.TmuxBackend().keys("Ren-worker", ["Enter"]) is False
    assert "no such window" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3. herdr — PATH の偽 herdr で argv を見る
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_herdr(tmp_path, monkeypatch):
    """PATH 先頭の偽 `herdr`。argv を 1 行 1 JSON で記録し、`FAKE_HERDR_REPLY` を返す。"""
    log = tmp_path / "herdr-argv.jsonl"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "herdr", f"""#!{sys.executable}
import json, os, sys
with open({str(log)!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.stdout.write(os.environ.get("FAKE_HERDR_REPLY", "{{}}"))
""")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def calls():
        return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return calls


@pytest.fixture
def herdr_backend(monkeypatch):
    backend = lib_mux.HerdrBackend()
    monkeypatch.setattr(backend, "_resolve_ids",
                        lambda name: {"tab_id": "t1", "pane_id": "p_42"})
    return backend


def test_herdr_keys_is_one_pane_send_keys_call(fake_herdr, herdr_backend):
    assert herdr_backend.keys("Ren-worker", ["Down", "Down", "Enter"]) is True
    assert fake_herdr() == [["pane", "send-keys", "p_42", "down", "down", "enter"]]


def test_herdr_keys_maps_escape_to_esc(fake_herdr, herdr_backend):
    assert herdr_backend.keys("Ren-worker", ["Escape"]) is True
    assert fake_herdr() == [["pane", "send-keys", "p_42", "esc"]]


def test_herdr_keys_refuses_an_unknown_key_before_touching_herdr(fake_herdr, herdr_backend):
    assert herdr_backend.keys("Ren-worker", ["2", "Enter"]) is False
    assert fake_herdr() == []


def test_herdr_keys_treats_an_error_reply_as_failure(fake_herdr, herdr_backend, monkeypatch):
    """herdr は拒否を `{"error": ...}` として (終了コード 0 で) 返す。届いたことにしない。"""
    monkeypatch.setenv("FAKE_HERDR_REPLY", json.dumps({"error": {"code": "pane_not_found"}}))
    assert herdr_backend.keys("Ren-worker", ["Enter"]) is False


def test_herdr_keys_fails_when_the_pane_cannot_be_resolved(fake_herdr, monkeypatch):
    backend = lib_mux.HerdrBackend()
    monkeypatch.setattr(backend, "_resolve_ids", lambda name: None)
    assert backend.keys("Ren-worker", ["Enter"]) is False
    assert fake_herdr() == []


# ---------------------------------------------------------------------------
# 4. 本番の宛先を名指しできない (MuxTestIsolationError のガード)
# ---------------------------------------------------------------------------

def test_keys_refuses_the_production_destination_on_tmux(recording_tmux, production_destination):
    with pytest.raises(lib_mux.MuxTestIsolationError) as excinfo:
        lib_mux.TmuxBackend().keys("dispatcher", ["Down", "Enter"])
    assert recording_tmux.calls == [], "keys reached tmux before the guard refused"
    assert PRODUCTION_DESTINATION in str(excinfo.value)


def test_keys_refuses_the_production_workspace_on_herdr(fake_herdr, production_destination):
    with pytest.raises(lib_mux.MuxTestIsolationError):
        lib_mux.HerdrBackend().keys("dispatcher", ["Down", "Enter"])
    assert fake_herdr() == [], "keys reached herdr before the guard refused"


def test_keys_refuses_a_bare_pane_name_even_on_an_isolated_destination(
        recording_tmux, monkeypatch):
    monkeypatch.setenv("CREWVIA_TMUX_SESSION", "crewvia-somewhere-else")
    monkeypatch.setenv("CREWVIA_MUX_PANE_PREFIX", "")
    with pytest.raises(lib_mux.MuxTestIsolationError):
        lib_mux.TmuxBackend().keys("dispatcher", ["Enter"])
    assert recording_tmux.calls == []


# ---------------------------------------------------------------------------
# 5. CLI (bash から使う形) — 終了コード
# ---------------------------------------------------------------------------

@pytest.fixture
def cli(tmp_path):
    """`lib_mux.py` を実 subprocess で走らせる。tmux / herdr は偽 (呼ばれた argv を記録)。"""
    log = tmp_path / "mux-argv.log"
    bindir = tmp_path / "clibin"
    bindir.mkdir()
    for tool in ("tmux", "herdr"):
        _executable(bindir / tool,
                    f"#!/usr/bin/env bash\necho \"{tool} $*\" >> {log}\nexit 0\n")

    def run(*args, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["CREWVIA_MUX"] = "tmux"
        env.update(env_extra or {})
        r = subprocess.run([sys.executable, str(SCRIPTS / "lib_mux.py"), *args],
                           capture_output=True, text=True, env=env, timeout=30)
        return r, (log.read_text() if log.exists() else "")
    return run


def test_cli_unknown_key_is_a_usage_error_and_sends_nothing(cli):
    r, sent = cli("keys", "Ren-worker", "Down", "2", "Enter")
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "'2'" in r.stderr and "send" in r.stderr
    assert sent == ""


def test_cli_without_keys_is_a_usage_error(cli):
    r, sent = cli("keys", "Ren-worker")
    assert r.returncode == 2
    assert "Usage" in r.stderr
    assert sent == ""


def test_cli_refuses_the_production_destination_with_the_isolation_exit_code(cli):
    r, sent = cli("keys", "dispatcher", "Down", "Enter",
                  env_extra={"CREWVIA_TMUX_SESSION": PRODUCTION_DESTINATION,
                             "CREWVIA_MUX_PANE_PREFIX": ""})
    assert r.returncode == lib_mux.MUX_TEST_ISOLATION_EXIT, (r.returncode, r.stderr)
    assert sent == ""


def test_the_bash_wrapper_exposes_mux_keys():
    text = (SCRIPTS / "lib_mux.sh").read_text(encoding="utf-8")
    assert "mux_keys()" in text
    assert '"$_LIB_MUX_PY" keys "$@"' in text


# ---------------------------------------------------------------------------
# 6. 実 tmux (private socket) — 押したキーが本当にペインへ届く
# ---------------------------------------------------------------------------

@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    """`tmux -L <private>` の実 tmux。`tmux` を PATH で包むので lib_mux は素の `tmux`
    を呼ぶだけでこの socket に着く — 本番の tmux server には届かない。"""
    if not REAL_TMUX:
        pytest.skip("tmux is not installed")
    sock = f"crewvia-keys-{os.getpid()}"
    bindir = tmp_path / "tmuxbin"
    bindir.mkdir()
    _executable(bindir / "tmux", f'#!/usr/bin/env bash\nexec {REAL_TMUX} -L {sock} "$@"\n')
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    session = os.environ["CREWVIA_TMUX_SESSION"]
    window = f"{os.environ['CREWVIA_MUX_PANE_PREFIX']}keys-probe"
    # `cat -v` shows a received Down as `^[[B`, so what arrived is visible on screen.
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", window,
                    "-x", "100", "-y", "20", "cat -v"], check=True, timeout=10)
    try:
        yield "keys-probe", f"{session}:{window}"
    finally:
        subprocess.run([REAL_TMUX, "-L", sock, "kill-server"],
                       capture_output=True, timeout=10)


def _screen(target: str, want: str, seconds: float = 5.0) -> str:
    deadline, out = time.time() + seconds, ""
    while time.time() < deadline:
        out = subprocess.run(["tmux", "capture-pane", "-t", target, "-p"],
                             capture_output=True, text=True, timeout=5).stdout
        if want in out:
            return out
        time.sleep(0.1)
    return out


def test_real_tmux_receives_the_named_keys(private_tmux):
    name, target = private_tmux
    assert lib_mux.TmuxBackend().keys(name, ["Down", "Down", "Enter"]) is True
    screen = _screen(target, "^[[B^[[B")
    assert "^[[B^[[B" in screen, screen


def test_real_tmux_receives_nothing_for_a_digit(private_tmux):
    """事故の再現の逆向き: 数字は `keys` を通らない。ペインには何も届かない。"""
    name, target = private_tmux
    assert lib_mux.TmuxBackend().keys(name, ["2"]) is False
    assert lib_mux.TmuxBackend().keys(name, ["Down"]) is True   # 届いたのはこれだけ
    screen = _screen(target, "^[[B")
    assert "^[[B" in screen, screen
    assert "2" not in screen.replace("^[[B", ""), screen
