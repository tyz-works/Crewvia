"""秘密を含むファイル名は、保存物のどこにも生では出ない (PR #307 Codex 1 巡目 P2)。

出力に出る path (untracked/ の保存名・listed_only・binary-files.txt・meta.json・status.txt・dirty.patch のヘッダ) すべてに
内容と同じ検出を通し、秘密を含む名前のエントリは保存も列挙もせず件数だけ meta に書く。
テスト: 保存物の全ファイルの**名前と中身**を grep して、生の値が 0 件。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import lib_checkpoint as ck  # noqa: E402
from test_checkpoint_lib import XID, git, take  # noqa: E402,F401  (使い捨て repo の道具)
from test_checkpoint_lib import wt  # noqa: E402,F401  (fixture)

# 実行時に組み立てる (ソースに実在しそうな値を置かない)
NAME_SECRETS = [
    "PASSWORD=" + "hunter2hunter2",
    "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "sk" + "-" + "zYxWvUtSrQpOnMlKjIhGfEdCbA987654",
    "token=" + "QwErTyUiOpAsDfGhJkLz",
    "AK" + "IA" + "ABCDEFGHIJKLMNOP",
]
RAW_FRAGMENTS = ["hunter2hunter2", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "zYxWvUtSrQpOnMlKjIhGfEdCbA987654",
                 "QwErTyUiOpAsDfGhJkLz", "ABCDEFGHIJKLMNOP"]


def everything(d):
    """保存物の全ファイル・全 dir の名前 (相対パス) と中身をつないだ bytes。"""
    out = b""
    for root, dirs, files in os.walk(d):
        rel = os.path.relpath(root, d)
        out += rel.encode() + b"\n"
        for n in dirs + files:
            out += n.encode() + b"\n"
        for f in files:
            out += pathlib.Path(root, f).read_bytes() + b"\n"
    return out


def test_untracked_names_with_secrets_are_neither_saved_nor_listed(wt, tmp_path):
    for i, n in enumerate(NAME_SECRETS):
        (wt / f"{n}.txt").write_text(f"plain body {i}\n")
        (wt / f"{n}.bin").write_bytes(b"\x00bin\x00")                  # binary-files.txt の行になりうる
        (wt / "sub").mkdir(exist_ok=True)
        (wt / "sub" / f"{n}").write_text("nested\n")
    (wt / f"dir-{NAME_SECRETS[0]}").mkdir()
    (wt / f"dir-{NAME_SECRETS[0]}" / "inner.txt").write_text("inner\n")
    (wt / "normal.txt").write_text("keep me\n")
    d = pathlib.Path(take(wt, tmp_path, reason="timeout"))
    blob = everything(d)
    for frag in RAW_FRAGMENTS:
        assert frag.encode() not in blob, f"{frag!r} が保存物に生で出た"
    meta = json.loads((d / "meta.json").read_text())
    assert (d / "untracked" / "normal.txt").read_text() == "keep me\n"          # 普通の名前は今までどおり
    assert meta["secret_files_excluded"] >= len(NAME_SECRETS) * 3
    listed = meta["files"]["untracked"]["listed_only"]
    assert all(not ck.path_is_secret(e["path"]) for e in listed)


def test_tracked_changes_renames_and_status_do_not_show_secret_names(wt, tmp_path):
    secret = NAME_SECRETS[1]
    (wt / secret).write_text("tracked body\n")
    (wt / (secret + ".dat")).write_bytes(b"\x00B\x00")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "add secret-named files")
    (wt / secret).write_text("tracked body changed\n")                          # M
    (wt / (secret + ".dat")).write_bytes(b"\x00B2\x00")                         # binary M
    git(wt, "mv", "a.txt", f"renamed-{NAME_SECRETS[0]}.txt")                    # R (旧 a.txt → 新 secret 名)
    d = pathlib.Path(take(wt, tmp_path, reason="timeout"))
    blob = everything(d)
    for frag in RAW_FRAGMENTS:
        assert frag.encode() not in blob, f"{frag!r} が保存物に生で出た"
    status = (d / "status.txt").read_text()
    assert "a.txt" not in status            # rename は旧パスごと落とす (新旧どちらかが秘密ならエントリごと)
    meta = json.loads((d / "meta.json").read_text())
    assert meta["dirty"] is True and meta["secret_files_excluded"] >= 3


def test_dirty_patch_headers_with_secret_names_are_dropped(wt, tmp_path):
    n = NAME_SECRETS[0]
    (wt / f"{n}.txt").write_text("v1\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "x")
    (wt / f"{n}.txt").write_text("v2\n")
    (wt / "a.txt").write_text("one\nCHANGED\n")
    d = pathlib.Path(take(wt, tmp_path, reason="timeout"))
    patch = (d / "dirty.patch").read_text()
    assert "hunter2hunter2" not in patch and "CHANGED" in patch
    assert json.loads((d / "meta.json").read_text())["files"]["dirty_patch"]["secret_files_dropped"] == 1


def test_meta_branch_with_a_secret_is_masked(wt, tmp_path):
    git(wt, "checkout", "-q", "-b", "feat/" + NAME_SECRETS[1])
    d = pathlib.Path(take(wt, tmp_path, reason="timeout"))
    assert NAME_SECRETS[1].encode() not in everything(d)
    assert json.loads((d / "meta.json").read_text())["branch"] == "[REDACTED]"


@pytest.mark.parametrize("name", NAME_SECRETS + [".env", "id_rsa", "a/b.pem", "prod-credentials.json"])
def test_path_is_secret_covers_both_kinds(name):
    assert ck.path_is_secret(name) is True


@pytest.mark.parametrize("name", ["src/main.py", "tests/test_x.py", "scripts/task-graph-notes.md", "README.md"])
def test_ordinary_names_are_not_secret(name):
    assert ck.path_is_secret(name) is False
