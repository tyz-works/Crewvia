#!/usr/bin/env python3
"""tests/hc_harness.py — HcBackend のテスト用の道具 (テストではない)。

`FakeHc` — PATH の先頭に置く偽の `hc`。台本どおりの出力を返す。CLI 呼び出し
(`hc get ... --json` 等) と `hc connect`（stdin から 1 行の JSON-RPC リクエスト
を読んで 1 行の応答を書く）の両方を同じ台本の形で扱う。本物の hc server には
一切触れない（`tests/rex_harness.py` と同じ立て付け）。
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

HC_AVAILABLE = shutil.which("hc") is not None

_FAKE_SCRIPT = r'''#!__PYTHON__
import json, os, sys

d = os.environ["FAKE_HC_DIR"]
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


def respond(resp):
    sys.stdout.write(resp.get("out", ""))
    sys.stderr.write(resp.get("err", ""))
    sys.exit(resp.get("rc", 0))


if argv[:1] == ["connect"]:
    line = sys.stdin.readline()
    try:
        req = json.loads(line)
    except Exception:
        sys.exit(99)
    method = req.get("method")
    for i, rule in enumerate(rules):
        if rule.get("connect") and rule.get("method") == method:
            if "seq" in rule:
                n = counters.get(str(i), 0)
                resp = rule["seq"][min(n, len(rule["seq"]) - 1)]
                counters[str(i)] = n + 1
                json.dump(counters, open(counters_path, "w"))
            else:
                resp = rule
            body = {"jsonrpc": "2.0", "id": req.get("id", 1)}
            if "result" in resp:
                body["result"] = resp["result"]
            else:
                body["error"] = resp.get("error", {"message": "fake: no rule"})
            sys.stdout.write(json.dumps(body) + "\n")
            sys.exit(0)
    sys.stderr.write("fake hc connect: no rule for method %r\n" % (method,))
    sys.exit(1)

for i, rule in enumerate(rules):
    if rule.get("connect"):
        continue
    if "head" in rule and argv[:1] != [rule["head"]]:
        continue
    if all(tok in argv for tok in rule["match"]):
        if "seq" in rule:
            n = counters.get(str(i), 0)
            resp = rule["seq"][min(n, len(rule["seq"]) - 1)]
            counters[str(i)] = n + 1
            json.dump(counters, open(counters_path, "w"))
        else:
            resp = rule
        respond(resp)
sys.stderr.write("fake hc: no rule for %r\n" % (argv,))
sys.exit(99)
'''


class FakeHc:
    """PATH 上の偽 `hc`。`calls()` で渡された引数列を全部見られる。"""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = Path(tempfile.mkdtemp(prefix="fkh-", dir=str(tmp_path)))
        bindir = self.dir / "bin"
        bindir.mkdir()
        script = bindir / "hc"
        script.write_text(_FAKE_SCRIPT.replace("__PYTHON__", sys.executable))
        script.chmod(0o755)
        monkeypatch.setenv("FAKE_HC_DIR", str(self.dir))
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        self.set([])

    def set(self, rules):
        (self.dir / "scenario.json").write_text(json.dumps(rules))
        for f in ("counters.json",):
            try:
                os.unlink(self.dir / f)
            except OSError:
                pass

    def raw_calls(self):
        path = self.dir / "calls.log"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l]

    def clear_calls(self):
        try:
            os.unlink(self.dir / "calls.log")
        except OSError:
            pass


def ok(out="", rc=0, err=""):
    return {"out": out, "rc": rc, "err": err}


def err_json(kind: str, message: str = "fake error", hint: str = "", rc: int = 1):
    body = {"error": {"kind": kind, "message": message, "hint": hint}}
    return {"out": "", "rc": rc, "err": json.dumps(body)}


def connect_result(method: str, result: dict):
    return {"connect": True, "method": method, "result": result}


def connect_error(method: str, kind: str, message: str = "fake error"):
    return {"connect": True, "method": method,
            "error": {"code": -32000, "message": message,
                      "data": {"kind": kind}}}


def status_json(*, core_id="core-A", instance="inst-A", generation=1) -> str:
    return json.dumps({
        "core": {"id": core_id}, "holder": {"instance": instance},
        "generation": generation, "version": "0.1.18",
    })


def terminal_json(*, address="t1", name="worker", state="live", pid=4242,
                  reported=False, status=None, labels=None) -> str:
    return json.dumps({
        "address": address, "name": name, "state": state, "pid": pid,
        "reported": reported, "status": status,
        "labels": labels or {}, "turn": 0,
    })
