"""PR #307 Codex 5 巡目 (t058) P2-2: `SafeDir.chain()` が途中の成分で例外になっても、それまでに開いた中間の記述子を閉じる。

拒否 1 回につき fd が 1 つ漏れると、長く動く watchdog が記述子を使い果たす。symlink・権限なし・dir でない成分で 1000 回失敗させ、
`/proc/self/fd` の数が増えないことを確かめる。`chain` の例外経路を戻すと赤になる。
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lib_checkpoint as ck  # noqa: E402

ROUNDS = 1000


def fd_count():
    return len(os.listdir("/proc/self/fd"))


def build(tmp_path, kind):
    """`a/b/<bad>/d` の形。`bad` が `kind` の理由で開けない。"""
    root = tmp_path / "reg"
    (root / "a" / "b").mkdir(parents=True)
    bad = root / "a" / "b" / "bad"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        bad.symlink_to(outside)
    elif kind == "not_dir":
        bad.write_text("x")
    elif kind == "no_perm":
        bad.mkdir()
        (bad / "d").mkdir()
        bad.chmod(0)
    return root


@pytest.mark.parametrize("kind", ["symlink", "not_dir", "no_perm"])
def test_chain_failure_does_not_leak_fds(tmp_path, kind):
    if kind == "no_perm" and os.geteuid() == 0:
        pytest.skip("root は権限なしの dir も開ける")
    root = build(tmp_path, kind)
    sd = ck.SafeDir.open_root(root)
    try:
        # 通る成分 (a, b) を開いてから bad で失敗する
        names = ("a", "b", "bad", "d")
        before = fd_count()
        for _ in range(ROUNDS):
            with pytest.raises(ck.CheckpointError):
                sd.chain(names)
        assert fd_count() <= before
    finally:
        sd.close()
        if kind == "no_perm":
            (root / "a" / "b" / "bad").chmod(0o700)


def test_chain_failure_on_create_path_does_not_leak_fds(tmp_path):
    root = build(tmp_path, "symlink")
    sd = ck.SafeDir.open_root(root)
    try:
        before = fd_count()
        for _ in range(ROUNDS):
            with pytest.raises(ck.CheckpointError):
                sd.chain(("a", "b", "bad", "d"), create=True, chmod_last=True)
        assert fd_count() <= before
    finally:
        sd.close()


def test_chain_success_and_missing_still_close_intermediates(tmp_path):
    root = build(tmp_path, "not_dir")
    (root / "a" / "b" / "ok").mkdir()
    sd = ck.SafeDir.open_root(root)
    try:
        before = fd_count()
        for _ in range(200):
            got = sd.chain(("a", "b", "ok"))
            assert got is not None
            got.close()
            assert sd.chain(("a", "b", "nope", "x")) is None
        assert fd_count() <= before
    finally:
        sd.close()
