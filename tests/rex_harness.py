#!/usr/bin/env python3
"""tests/rex_harness.py — Rex backend のテスト用の道具 (テストではない)。

2 種類の rex を用意する。どちらも**既定の socket (= 本番の server) には触れない**:

  * `FakeRex`   — PATH の先頭に置く偽の `rex`。引数を記録し、台本どおりの出力を返す。
                  **CI で常に走る** (CI の ubuntu に本物の rex は入らない)。
  * `real_rex_server()` — 本物の `rex server run` を短い socket で起こす。rex が無ければ
                  呼び出し側が skip する (`REX_AVAILABLE`)。終わったら止めて片付ける
                  (`rm -rf` は使わない — 1 つずつ unlink / rmdir)。

lib_mux 側の約束 (`CREWVIA_REX_SOCK` はテスト中にだけ読まれる) をここで満たす。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

REX_AVAILABLE = shutil.which("rex") is not None

_SERVER_ENV_DENY = ("CLAUDE", "CODEX_", "CREWVIA_", "TASK_", "TASKVIA", "NTFY_",
                    "AGENT_NAME", "AI_AGENT", "TMUX", "HERDR")


def clean_env():
    """server に渡す env (session 変数を落とす。pane に流れ込むので)。"""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(_SERVER_ENV_DENY)}


def short_dir(prefix="rxt-"):
    """sun_path (108 byte) に収まる使い捨て dir。"""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def remove_tree(path: Path) -> None:
    """`rm -rf` を使わず、1 つずつ消す。"""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            try:
                os.unlink(os.path.join(root, f))
            except OSError:
                pass
        for d in dirs:
            full = os.path.join(root, d)
            try:
                os.unlink(full) if os.path.islink(full) else os.rmdir(full)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def make_checkout(tmp_path: Path) -> Path:
    """`registry/mux` を置ける使い捨ての checkout (`.git` つき)。"""
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "registry" / "mux").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# 偽の rex
# ---------------------------------------------------------------------------

_FAKE_SCRIPT = r'''#!__PYTHON__
import json, os, sys
d = os.environ["FAKE_REX_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as fh:
    fh.write(json.dumps(argv) + "\n")
with open(os.path.join(d, "scenario.json")) as fh:
    rules = json.load(fh)
counters_path = os.path.join(d, "counters.json")
try:
    counters = json.load(open(counters_path))
except Exception:
    counters = {}
for i, rule in enumerate(rules):
    if "head" in rule and argv[3:4] != [rule["head"]]:
        continue
    if all(tok in argv for tok in rule["match"]):
        if "seq" in rule:
            n = counters.get(str(i), 0)
            resp = rule["seq"][min(n, len(rule["seq"]) - 1)]
            counters[str(i)] = n + 1
            json.dump(counters, open(counters_path, "w"))
        else:
            resp = rule
        sys.stdout.write(resp.get("out", ""))
        sys.stderr.write(resp.get("err", ""))
        sys.exit(resp.get("rc", 0))
sys.stderr.write("fake rex: no rule for %r\n" % (argv,))
sys.exit(99)
'''


class FakeRex:
    """PATH 上の偽 `rex`。`calls()` で渡された引数列を全部見られる。"""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = Path(tempfile.mkdtemp(prefix="fkr-", dir=str(tmp_path)))
        bindir = self.dir / "bin"
        bindir.mkdir()
        script = bindir / "rex"
        script.write_text(_FAKE_SCRIPT.replace("__PYTHON__", sys.executable))
        script.chmod(0o755)
        self.sock = str(self.dir / "s.sock")      # 偽なので bind はしない
        monkeypatch.setenv("FAKE_REX_DIR", str(self.dir))
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("CREWVIA_REX_SOCK", self.sock)
        self.set([])

    def set(self, rules):
        (self.dir / "scenario.json").write_text(json.dumps(rules))
        for f in ("counters.json",):
            try:
                os.unlink(self.dir / f)
            except OSError:
                pass

    def raw_calls(self):
        """渡された引数列そのまま (`-S <endpoint> --autostart=false` を含む)。"""
        path = self.dir / "calls.log"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l]

    def calls(self):
        """全呼び出しの共通前置き (`-S <endpoint> --autostart=false`) を除いた引数列。"""
        out = []
        for argv in self.raw_calls():
            assert argv[:1] == ["-S"] and "--autostart=false" in argv[:3], argv
            out.append(argv[3:])
        return out

    def clear_calls(self):
        try:
            os.unlink(self.dir / "calls.log")
        except OSError:
            pass


def status_json(*, pid=4242, instance_id="inst-A", revision=9,
                endpoint=None) -> str:
    return json.dumps({
        "endpoint": endpoint or "unix:///fake/s.sock", "pid": pid,
        "instance_id": instance_id, "api_revision": revision,
        "session_count": 1})


def ok(out="", rc=0, err=""):
    return {"out": out, "rc": rc, "err": err}


# ---------------------------------------------------------------------------
# 本物の rex (隔離 server)
# ---------------------------------------------------------------------------

class RealRexServer:
    def __init__(self, restore=False, real_home=False):
        self.real_home = real_home
        self.dir = short_dir()
        self.sock = self.dir / "s.sock"
        self.state = self.dir / "state"
        self.restore = restore
        self.proc = None

    def base(self):
        return ["rex", "-S", f"unix://{self.sock}", "--autostart=false"]

    def rex(self, *args, timeout=15, check=False):
        r = subprocess.run(self.base() + list(args), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise AssertionError(f"rex {args} -> {r.returncode}: {r.stderr}")
        return r

    def start(self):
        # 空の HOME: 復元された pane の login shell が利用者の profile を読むと、
        # (この機械では) `tmux attach` が子として立ち、husk が「空の shell」にならない。
        home = self.dir / "home"
        home.mkdir(exist_ok=True)
        env = clean_env()
        if not self.real_home:
            env["HOME"] = str(home)
        log = open(self.dir / "server.log", "ab")
        self.proc = subprocess.Popen(
            ["rex", "server", "run", "--listen", f"unix://{self.sock}",
             "--state-dir", str(self.state),
             f"--restore={'true' if self.restore else 'false'}"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, env=env)
        deadline = time.time() + 15
        while time.time() < deadline:
            r = self.rex("server", "status", "--json", timeout=5)
            if r.returncode == 0:
                return self
            time.sleep(0.2)
        raise AssertionError("isolated rex server did not come up")

    def status(self):
        return json.loads(self.rex("server", "status", "--json", check=True).stdout)

    def stop(self):
        try:
            self.rex("server", "stop", timeout=10)
        except Exception:
            pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
                self.proc.wait(timeout=5)
        remove_tree(self.dir)

    def kill_hard(self):
        """restart の実験用: server を SIGKILL する (state-dir は残る)。"""
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        try:
            os.unlink(self.sock)
        except OSError:
            pass
        self.proc = None
