"""bats は、実行した checkout の registry/ を書かない (t006)。

11-24 に主 checkout の registry/mux/Test-worker.json (fake tmux の endpoint を指す古い記録) が見つかった。
原因: tests/lib-mux.bats の spawn テストなどが、`lib_mux.py` / `start.sh` を実行した checkout の
registry/ (spawn 記録・Worker の記録) にそのまま書いていた (書き先は `__file__` 基準で変えられない)。
bats を主 checkout で回すと本番の registry/mux が汚れる。

直し方は tests/bats_sandbox.sh (複製の中で走らせる + 実 checkout の前後比較)。このテストは
checkout の**複製**を作って、その中で bats を回し、複製の registry/ が増えないことを見る
(本物の checkout は触らない = 欠陥が戻っても本番を汚さずに赤くなる)。
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# (bats ファイル, spawn 記録を書く 1 件の絞り込み)。ファイルごとに「書く」テストを 1 つずつ。
LEAKY_FILES = [
    ("tests/lib-mux.bats", "spawn: CREWVIA_TMUX_SESSION overrides session name"),
    ("tests/permission-mode.bats", "worker \\(mux mode\\): 起動コマンド"),
    ("tests/start-sh-kickoff.bats", "kickoff lands on the first attempt"),
    ("tests/start-sh-mux-gate.bats", "uses parallel mode, not inline"),
]

pytestmark = pytest.mark.skipif(shutil.which("bats") is None, reason="bats が無い")


def _copy_checkout(dest: Path) -> None:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True, capture_output=True).stdout
    for rel in filter(None, out.decode().split("\0")):
        src = REPO_ROOT / rel
        if not src.is_file() and not src.is_symlink():
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)


def _registry_files(root: Path):
    reg = root / "registry"
    return sorted(str(p.relative_to(root)) for p in reg.rglob("*") if p.is_file()) if reg.exists() else []


@pytest.fixture(scope="module")
def checkout_copy(tmp_path_factory):
    dest = tmp_path_factory.mktemp("checkout")
    _copy_checkout(dest)
    return dest


@pytest.mark.parametrize("bats_file,only", LEAKY_FILES, ids=[f for f, _ in LEAKY_FILES])
def test_bats_leaves_the_checkouts_registry_untouched(checkout_copy, bats_file, only):
    assert _registry_files(checkout_copy) == [], "複製の時点で registry/ が空であること"
    # 開発者のシェルに近い最小の env (CREWVIA_* のテスト印・pytest の PYTHONPATH 等を持ち込まない)
    env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]}
    r = subprocess.run(["bats", "--filter", only, bats_file], cwd=checkout_copy, env=env,
                       capture_output=True, text=True, timeout=240)
    # 絞り込みが 1 件以上に当たって走ったこと (空振りで緑にならない)
    assert "\nok " in "\n" + r.stdout or r.stdout.startswith("ok "), (r.stdout, r.stderr)
    assert _registry_files(checkout_copy) == [], f"{bats_file} が複製の registry/ を書いた"


def test_every_leaky_bats_file_uses_the_sandbox_helper():
    """構造: 4 ファイルとも helper を source し、setup で複製を作り、teardown の最後で壊す。"""
    for bats_file, _ in LEAKY_FILES:
        text = (REPO_ROOT / bats_file).read_text()
        assert "bats_sandbox.sh" in text, bats_file
        assert "bats_sandbox_create" in text, bats_file
        td = text[text.index("\nteardown() {"):]
        td = td[:td.index("\n}\n")]
        assert td.rstrip().endswith("bats_sandbox_destroy"), bats_file
