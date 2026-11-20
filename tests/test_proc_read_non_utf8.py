"""/proc/<pid>/{stat,comm,cmdline,status} をテキストで読む書き方が tests/ scripts/ に戻らないことの保証 (t003)。

背景: PR #430 の CI で `_pgid_of` が `Path(f"/proc/{pid}/stat").read_text()` を使っており、並列に走る別テストの
UTF-8 でない comm (例: `b"bad-\\xff"`) のプロセスに当たって `UnicodeDecodeError` (OSError ではない) で落ちた。
引き金になるのは `tests/test_leaked_descendants_guard.py` の `_spawn_with_raw_comm` (prctl(PR_SET_NAME) で comm に
0xff を入れた子を立てる 5 件のテスト)。xdist で同じ worker・別 worker のどちらでも、同じ /proc に居る間は
無関係なテストの `/proc` 走査・単発読みに当たりうる。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import proc_read

ROOT = Path(__file__).resolve().parent.parent
THIS = Path(__file__).name

_BAD_COMM = b"bad-\xff"

_CHILD = (
    "import ctypes, time, sys\n"
    "ctypes.CDLL(None).prctl(15, bytes.fromhex(sys.argv[1]), 0, 0, 0)\n"
    "time.sleep(30)\n"
)


@pytest.fixture
def bad_comm_child():
    if not Path("/proc/self/stat").exists():
        pytest.skip("/proc が無い")
    child = subprocess.Popen([sys.executable, "-c", _CHILD, _BAD_COMM.hex()])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if _BAD_COMM[:15] in Path(f"/proc/{child.pid}/stat").read_bytes():
                    break
            except OSError:
                pass
            time.sleep(0.02)
        else:
            pytest.fail("comm が書き換わらない (prctl 失敗)")
        yield child
    finally:
        child.kill()
        child.wait()


def test_text_mode_read_really_fails_on_non_utf8_comm(bad_comm_child):
    """赤の実証: 直す前の書き方 (read_text) は、comm が UTF-8 でないだけで UnicodeDecodeError になる。"""
    with pytest.raises(UnicodeDecodeError):
        Path(f"/proc/{bad_comm_child.pid}/stat").read_text()


def test_proc_read_helpers_survive_non_utf8_comm(bad_comm_child):
    pid = bad_comm_child.pid
    rest = proc_read.stat_fields(pid)
    assert rest is not None
    assert rest[0] in ("R", "S")
    assert int(rest[1]) == os.getpid()           # ppid
    assert int(rest[2]) == os.getpgid(pid)       # pgrp
    assert proc_read.state_of(pid) == rest[0]
    comm = proc_read.comm_of(pid)
    assert comm is not None and comm.startswith("bad-")


def test_proc_read_helpers_handle_gone_pid_and_paren_in_comm():
    assert proc_read.stat_fields(2 ** 22 + 12345) is None
    assert proc_read.state_of(2 ** 22 + 12345) is None
    assert proc_read.comm_of(2 ** 22 + 12345) is None
    # comm が ')' を含んでも最後の ')' で切る
    p = subprocess.Popen([sys.executable, "-c", _CHILD, b"a) S 1 2".hex()])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and proc_read.comm_of(p.pid) != "a) S 1 2":
            time.sleep(0.02)
        assert proc_read.comm_of(p.pid) == "a) S 1 2"
        assert int(proc_read.stat_fields(p.pid)[1]) == os.getpid()
    finally:
        p.kill()
        p.wait()


# --- 構造テスト --------------------------------------------------------------

_PROC_TEXT = re.compile(r"/proc/[^\"'\n]*?/(stat|comm|cmdline|status)\b")
_SAFE = re.compile(r"read_bytes\(|errors\s*=|[\"']rb[\"']|\\\"rb\\\"")
_TEXT_READ = re.compile(r"read_text\(|\bopen\(")

#: 理由付き allowlist: (ファイル名, 行に含まれる断片) -> 理由
ALLOWLIST = {
    ("red_proof_t047.sh", 'read_text()'): "欠陥注入用の文字列 (わざと旧 read_text 形に書き換える)。実行されるのは複製のみ",
}


def _scan():
    hits, scanned = [], 0
    files = [p for d in ("tests", "scripts") for p in (ROOT / d).rglob("*")
             if p.suffix in (".py", ".sh") and p.name != THIS and "__pycache__" not in p.parts]
    for path in files:
        scanned += 1
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#") or not _PROC_TEXT.search(line) or not _TEXT_READ.search(line) or _SAFE.search(line):
                continue
            if any(path.name == f and frag in line for (f, frag) in ALLOWLIST):
                continue
            hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    return scanned, hits


def test_no_text_mode_proc_reads():
    scanned, hits = _scan()
    assert scanned > 100, f"検査対象が少なすぎる ({scanned})"
    assert not hits, ("/proc/<pid>/stat 等をテキストで読んでいる (comm は任意のバイト列)。"
                      "tests/proc_read.py か read_bytes() を使うこと:\n" + "\n".join(hits))


def test_allowlist_has_no_dead_entries():
    for (name, frag) in ALLOWLIST:
        found = False
        for p in ROOT.rglob(name):
            if any(frag in l and _PROC_TEXT.search(l) and _TEXT_READ.search(l)
                   for l in p.read_text(errors="replace").splitlines()):
                found = True
        assert found, f"allowlist の行が実在しない: {name} {frag}"


def test_structural_scan_detects_the_old_shapes():
    for bad in ('x = Path(f"/proc/{pid}/stat").read_text()',
                'with open(f"/proc/{pid}/stat") as f:',
                'Path(f"/proc/{pid}/comm").read_text().strip()'):
        assert _PROC_TEXT.search(bad) and _TEXT_READ.search(bad) and not _SAFE.search(bad), bad
    for ok in ('Path(f"/proc/{pid}/stat").read_bytes()',
               'Path(f"/proc/{pid}/stat").read_text(errors="replace")',
               'open(f"/proc/{pid}/cmdline", "rb")'):
        assert _SAFE.search(ok), ok


# --- 構造テスト 2: Path の組み立て形 (entry / "stat"・Path("/proc") / pid / "stat") と別の行の read_text -------
# 上の行単位の検査は `/proc/<pid>/stat` が 1 行に書かれた形しか見ない。scan_daemon_pids は
# `(entry / "stat").read_text(encoding="utf-8")` (entry は Path(proc_root).iterdir() の要素) で、
# 行単位の検査をすり抜けていた。ここでは .py の関数ごとに「/proc を指す語 (文字列 "/proc"・名前に proc を含む
# 識別子) と stat/comm/cmdline/status の文字列定数が同居し、かつ同じ関数にバイト列でないテキスト読みがある」を拾う。

import ast

_PROC_FILES = ("stat", "comm", "cmdline", "status")


_PROC_NAMES = frozenset(["proc_root", "procfs", "proc_dir", "proc_path"])


def _is_text_read(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    kw = {k.arg for k in node.keywords}
    if isinstance(f, ast.Attribute) and f.attr == "read_text":
        return "errors" not in kw
    if (isinstance(f, ast.Name) and f.id == "open") or (isinstance(f, ast.Attribute) and f.attr == "open"):
        modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
        modes += [k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
        if any(isinstance(m, str) and "b" in m for m in modes):
            return False
        return "errors" not in kw
    return False


def _mentions_proc(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("/proc"):
            return True
        if isinstance(n, ast.Name) and n.id.lower().lstrip("_") in _PROC_NAMES:
            return True
        if isinstance(n, ast.arg) and n.arg.lower().lstrip("_") in _PROC_NAMES:
            return True
    return False


def _names_proc_file(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str)
               and (n.value in _PROC_FILES or re.search(r"^/proc/.*/(stat|comm|cmdline|status)\b", n.value))
               for n in ast.walk(node))


def _function_hits(source: str, rel: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (_mentions_proc(fn) and _names_proc_file(fn)):
            continue
        for n in ast.walk(fn):
            if _is_text_read(n):
                hits.append(f"{rel}:{n.lineno}: {fn.name}() が /proc の stat 等を組み立てた上でテキスト読みをしている")
    return hits


_FN_ALLOWLIST: dict = {}   # (相対パス, 関数名) -> 理由


def _scan_functions():
    hits, scanned = [], 0
    for d in ("tests", "scripts"):
        for path in (ROOT / d).rglob("*.py"):
            if path.name == THIS or "__pycache__" in path.parts:
                continue
            scanned += 1
            rel = str(path.relative_to(ROOT))
            for h in _function_hits(path.read_text(errors="replace"), rel):
                fn = h.split(": ", 1)[1].split("()", 1)[0]
                if (rel, fn) not in _FN_ALLOWLIST:
                    hits.append(h)
    return scanned, hits


def test_no_text_mode_proc_reads_via_built_paths():
    scanned, hits = _scan_functions()
    assert scanned > 100, f"検査対象が少なすぎる ({scanned})"
    assert not hits, ("Path の組み立て形で /proc/<pid>/stat 等をテキストで読んでいる。read_bytes() を使い、"
                      "最後の ')' より後ろを解釈すること:\n" + "\n".join(hits))


def test_function_scan_detects_the_old_shapes():
    old = (
        "from pathlib import Path\n"
        "def scan(proc_root='/proc'):\n"
        "    for entry in Path(proc_root).iterdir():\n"
        "        stat = (entry / 'stat').read_text(encoding='utf-8')\n"
    )
    assert _function_hits(old, "x.py")
    split = (
        "from pathlib import Path\n"
        "def f(pid):\n"
        "    p = Path('/proc') / str(pid) / 'comm'\n"
        "    return p.read_text()\n"
    )
    assert _function_hits(split, "x.py")
    assert _function_hits("def f(pid):\n    return open('/proc/%d/status' % pid).read()\n", "x.py")
    ok = (
        "from pathlib import Path\n"
        "def scan(proc_root='/proc'):\n"
        "    for entry in Path(proc_root).iterdir():\n"
        "        stat = (entry / 'stat').read_bytes()\n"
    )
    assert not _function_hits(ok, "x.py")
    assert not _function_hits("def f(proc_root):\n    return Path('/etc/x').read_text()\n", "x.py")


def test_function_scan_is_red_on_the_original_lib_daemon_watch():
    """lib_daemon_watch.py を元の形 (read_text) に戻すと赤になる。"""
    src = (ROOT / "scripts" / "lib_daemon_watch.py").read_text()
    good = '(entry / "stat").read_bytes()'
    assert good in src, "scan_daemon_pids の bytes 読みが無い"
    assert not _function_hits(src, "scripts/lib_daemon_watch.py")
    bad = src.replace(good, '(entry / "stat").read_text(encoding="utf-8")')
    assert _function_hits(bad, "scripts/lib_daemon_watch.py")
