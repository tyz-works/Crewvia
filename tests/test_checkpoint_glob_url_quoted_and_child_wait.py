"""checkpoint fix 2 (PR #307 Codex 2 巡目 P1/P2 + QA t045):

1. 秘密ファイル名の glob は設計 §5.3 と 1 対 1 (`.env*` / `*.secret*`)。tracked の変更・untracked・バイナリのすべてで保存されない。
2. URL に埋め込んだ認証 (`<scheme>://<user>:<pass>@`) の `<pass>` を伏せる。
3. 全パターン × 値の囲み (なし / " / ') で、引用符つきの値も丸ごと伏せる (QA t045 の `Bearer "<token>"`)。
4. 子プロセスの待ちに期限: stdout を閉じてから固まる git でも期限内に返り、プロセスが残らない。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import lib_checkpoint as ck  # noqa: E402
from test_checkpoint_lib import git, take, wt  # noqa: E402,F401

# ---------------------------------------------------------------- 1. glob

SECRET_NAMES = [".envrc", ".env", ".env.local", ".env.production", ".environment", "sub/.envrc", "dir/.ENV.Prod",
                "x.secret", "x.secrets", "prod.secrets.yaml", "a.secret.json", "a.SECRET", "k.pem", "k.key", "k.p12",
                "k.pfx", "id_rsa", "id_dsa", "id_ed25519", "aws-credentials", "credentials.json"]
ORDINARY = ["src/main.py", "README.md", "scripts/task-graph-notes.md", "secretary.txt", "env.md", "my.environment.txt"]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_every_design_glob_matches(name):
    assert ck.is_secret_name(name) is True


def test_a_directory_component_that_matches_hides_everything_under_it():
    assert ck.is_secret_name("config/.env.d/app.conf") is True
    assert ck.is_secret_name("Keys.pem/readme.txt") is True


@pytest.mark.parametrize("name", [n for n in ORDINARY if n != "my.environment.txt"])
def test_ordinary_names_still_pass(name):
    assert ck.is_secret_name(name) is False


def _everything(d):
    out = b""
    for root, dirs, files in os.walk(d):
        out += os.path.relpath(root, d).encode() + b"\n"
        for n in dirs + files:
            out += n.encode() + b"\n"
        for f in files:
            out += pathlib.Path(root, f).read_bytes() + b"\n"
    return out


BODY = "export DATABASE_URL=" + "postgres://user:" + "plainpass1234" + "@db/app\n"


@pytest.mark.parametrize("name", [".envrc", ".env.local", "prod.secrets.yaml", "x.secret", "a.secret.json"])
def test_secret_named_files_are_never_saved_tracked_untracked_or_binary(wt, tmp_path, name):
    p = wt / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(BODY)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "add")
    p.write_text(BODY + "# changed\n")                                        # tracked の変更
    (wt / f"u-{name}").write_text(BODY)                                       # untracked
    (wt / f"bin-{name}").write_bytes(b"\x00" + BODY.encode() + b"\x00")       # untracked のバイナリ
    d = pathlib.Path(take(wt, tmp_path, reason="timeout"))
    assert b"plainpass1234" not in _everything(d)
    meta = json.loads((d / "meta.json").read_text())
    assert meta["secret_files_excluded"] >= 1


# ---------------------------------------------------------------- 2. URL 認証

PASS = "Zp9" + "xQ2mVk7Lw4Rt"


@pytest.mark.parametrize("fmt", [
    "postgres://user:{p}@db/app",
    "postgres://user:{p}@db:5432/app?sslmode=require",
    "https://user:{p}@host.example.com/path/a:b@c",
    "redis://:{p}@cache:6379/0",
    '"mongodb+srv://u:{p}@cluster0.example.net/db"',
    "'amqp://guest:{p}@mq:5672//'",
    "export URL=postgres://user:{p}@db/app",
    "git clone https://oauth2:{p}@gitlab.example.com/g/r.git",
    "postgres://user:p@ss{p}@db/app",                                         # pass に生の @
])
def test_url_credentials_are_replaced(fmt):
    secret_text = fmt.format(p=PASS)
    red, n = ck.redact(secret_text)
    assert n >= 1 and PASS not in red
    assert red.count("://") == secret_text.count("://")                      # 構造 (scheme・host) は残る


def test_url_without_credentials_is_left_alone():
    for t in ["https://example.com/a/b?c=d", "see https://host/a:b@c for details", "ssh://git@github.com/x/y.git"]:
        assert ck.redact(t) == (t, 0)


@pytest.mark.parametrize("n", [100, 5000, 1_000_000])
def test_long_url_passwords_are_replaced_whole_and_linearly(n):
    long_pass = (PASS * (n // len(PASS) + 1))[:n]
    t0 = time.monotonic()
    red, count = ck.redact(f"postgres://user:{long_pass}@db/app\n")
    assert count == 1 and long_pass[:16] not in red and long_pass[-16:] not in red
    assert time.monotonic() - t0 < 5


def test_adversarial_url_lines_are_linear():
    for t in ["://" * 200_000, "a://" + ":" * 1_000_000, "a://" + "@" * 1_000_000, "a://x:" + "@:" * 500_000]:
        t0 = time.monotonic()
        ck.redact(t)
        assert time.monotonic() - t0 < 5


# ---------------------------------------------------------------- 3. パターン × 囲み

V = "Qw7" + "Er5TyUi9OpAs2Df"          # 15 文字: 全パターンの最小長を超える
GH = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
SK = "sk" + "-" + "zYxWvUtSrQpOnMlKjIhGfEdCbA987654"
AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
SLACK = "xox" + "b-" + "1234567890-abcdef"

# (名前, 値の本体, 入力の組み立て。{q} が囲み、{v} が値)
FAMILY = [
    ("PASSWORD=", V, "PASSWORD={q}{v}{q}"),
    ("password:", V, "password: {q}{v}{q}"),
    ("json password", V, '{{"password": {q}{v}{q}}}'),
    ("TOKEN=", V, "TOKEN={q}{v}{q}"),
    ("TOKEN:", V, "TOKEN: {q}{v}{q}"),
    ("API_KEY=", V, "API_KEY={q}{v}{q}"),
    ("Bearer", V, "Authorization: Bearer {q}{v}{q}"),
    ("Bearer 空白入り", V + " " + V, "Authorization: Bearer {q}{v}{q}"),
    ("ghp_", GH, "x={q}{v}{q}"),
    ("sk-", SK, "x={q}{v}{q}"),
    ("AKIA", AWS, "x={q}{v}{q}"),
    ("xox", SLACK, "x={q}{v}{q}"),
    ("op://", "Vault/item/field", "x={q}op://{v}{q}"),
    ("URL 認証", V, "x={q}postgres://u:{v}@h/d{q}"),
]


@pytest.mark.parametrize("q", ["", '"', "'"], ids=["bare", "dq", "sq"])
@pytest.mark.parametrize("name,value,shape", FAMILY, ids=[f[0] for f in FAMILY])
def test_pattern_x_quote_matrix_replaces_the_whole_value(name, value, shape, q):
    if q == "" and " " in value:
        pytest.skip("空白入りの値は囲みがあるときだけ 1 つの値")
    text = shape.format(q=q, v=value)
    red, n = ck.redact(text)
    assert n >= 1
    for w in (value[:8], value[-8:], value.split(" ")[-1][:8]):
        assert w not in red, f"{name} / {q!r}: 値が残った: {red!r}"


# ---------------------------------------------------------------- 4. 子の待ちに期限

def _fake_git(tmp_path, monkeypatch, script):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "git").write_text(script)
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")


def _alive(pattern):
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()


def test_a_git_that_closes_stdout_then_hangs_is_cut_at_the_deadline(wt, tmp_path, monkeypatch):
    marker = f"closeout-{os.getpid()}-{time.monotonic_ns()}"
    _fake_git(tmp_path, monkeypatch, f"#!/bin/sh\n# {marker}\nexec 1>&-\nsleep 60 # {marker}\n")
    t0 = time.monotonic()
    with pytest.raises(ck.CheckpointError):
        take(wt, tmp_path, seconds=2)
    assert time.monotonic() - t0 < 8
    time.sleep(0.3)
    assert _alive(marker) == [], "子プロセスが残った"
    assert not list((tmp_path / "registry").rglob("*.partial"))


def test_a_child_that_ignores_nothing_but_outlives_stdout_does_not_outlast_git_timeout(wt, tmp_path, monkeypatch):
    """残り時間が長くても GIT_TIMEOUT (1 回の git の上限) で切る。"""
    marker = f"gittimeout-{os.getpid()}-{time.monotonic_ns()}"
    _fake_git(tmp_path, monkeypatch, f"#!/bin/sh\n# {marker}\nexec 1>&-\nsleep 60 # {marker}\n")
    monkeypatch.setattr(ck, "GIT_TIMEOUT", 1.0)
    t0 = time.monotonic()
    with pytest.raises(ck.CheckpointError):
        take(wt, tmp_path, seconds=30)
    assert time.monotonic() - t0 < 10
    time.sleep(0.3)
    assert _alive(marker) == []


def test_reap_is_bounded_even_when_the_child_survives_sigkill(monkeypatch):
    class Stubborn:
        pid = 2 ** 22 + 1                                                    # 存在しない pid

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            raise subprocess.TimeoutExpired("x", timeout)

        def kill(self):
            pass

    monkeypatch.setattr(ck, "REAP_SECONDS", 0.05)
    p = Stubborn()
    t0 = time.monotonic()
    ck._reap(p)
    assert p.waits == 2 and time.monotonic() - t0 < 2


# ---------------------------------------------------------------- 4b. 退役の後始末コマンド

def test_retirement_cleanup_command_is_cut_even_if_a_grandchild_holds_the_pipes(tmp_path, monkeypatch):
    import lib_retirement as lr
    marker = f"grandchild-{os.getpid()}-{time.monotonic_ns()}"
    script = tmp_path / "plan.sh"
    script.write_text(f"#!/bin/bash\n# {marker}\n(sleep 60 # {marker}\n) &\nsleep 60\n")
    script.chmod(0o755)
    monkeypatch.setattr(lr, "CLEANUP_COMMAND_TIMEOUT", 1)
    t0 = time.monotonic()
    rc, out = lr._default_run_command(["bash", str(script)], dict(os.environ))
    assert rc == 1 and "Timeout" in out and time.monotonic() - t0 < 8
    time.sleep(0.3)
    assert _alive(marker) == [], "孫プロセスが残った"


# ---------------------------------------------------------------- 5. 長い値の所要時間 (線形であること)

_TIMING_SHAPES = ["PASSWORD={v}", 'PASSWORD="{v}"', "Authorization: Bearer {v}", 'Authorization: Bearer "{v}"',
                  "ghp" + "_{v}", "sk" + "-{v}", "op://{v}", "postgres://u:{v}@h/d", '"postgres://u:{v}@h/d"', "{v}"]


def _alnum(n):
    return ("Zq7" + "Ab3Cd9Ef1Gh5Jk")[:0] + ("Ab3Cd9Ef1Gh5Jk7Lm2Np4Qr6St8Uv" * (n // 28 + 1))[:n]


@pytest.mark.parametrize("shape", _TIMING_SHAPES)
def test_redact_time_is_linear_in_value_length_and_one_megabyte_is_fast(shape):
    def run(n):
        text = shape.format(v=_alnum(n))
        t0 = time.perf_counter()
        ck.redact(text)
        return time.perf_counter() - t0

    small, big = run(64 * 1024), run(1024 * 1024)
    assert big < 3.0, f"1MB の値で {big:.2f}s (退役の期限 20 秒の中で動く)"
    # 16 倍の長さで O(n^2) なら 256 倍。小さい側が計測の誤差に埋もれないよう下駄を履かせて 64 倍を上限にする
    assert big < max(small, 0.005) * 64
