"""モデルの段上げ PR-1: `registry/workers/<Name>/model.json` の記録 (`lib_worker_model.py`)。

設計: knowledge/model-escalation.md §4.2・§8 (PR-1)。記録の形の欠け・型違いを 1 つずつ、原子的な書き込み、
「無い」と「壊れている」の区別、通常ファイルでないもの (symlink / FIFO) を開かないこと、CLI。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lib_worker_model as wm  # noqa: E402
from lib_task_cards import is_missing, is_unreadable  # noqa: E402

GOOD = {"agent": "Ren", "model": "opus", "source": "env", "bound_task": None, "written_at": 1795000000}


def _write(reg, agent, data):
    p = reg / "workers" / agent / "model.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return p


def test_write_then_load_roundtrip(tmp_path):
    rec = wm.write_record(tmp_path, "Ren", "skill", "sonnet", None, now=1.5)
    assert rec == {"agent": "Ren", "model": "sonnet", "source": "skill", "bound_task": None, "written_at": 1.5}
    assert wm.load_record(tmp_path, "Ren") == rec
    assert (tmp_path / "workers" / "Ren" / "model.json").is_file()
    # 同じ dir の TARGET_DIR の記録とは別ファイル (mux の spawn 記録には書かない)
    assert not (tmp_path / "mux").exists()


def test_empty_model_is_null_not_a_made_up_value(tmp_path):
    rec = wm.write_record(tmp_path, "Ren", "none", "")
    assert rec["model"] is None and rec["source"] == "none"
    assert wm.load_record(tmp_path, "Ren")["model"] is None


def test_blank_model_is_null_when_written(tmp_path):
    rec = wm.write_record(tmp_path, "Ren", "env", "   ")
    assert rec["model"] is None
    assert wm.load_record(tmp_path, "Ren")["model"] is None


def test_missing_is_missing_not_unreadable_value(tmp_path):
    rec = wm.load_record(tmp_path, "Ren")
    assert is_missing(rec) and is_unreadable(rec)       # Unreadable の一種。is_missing を先に見る契約


@pytest.mark.parametrize("drop", sorted(GOOD))
def test_every_field_is_required(tmp_path, drop):
    data = {k: v for k, v in GOOD.items() if k != drop}
    _write(tmp_path, "Ren", data)
    rec = wm.load_record(tmp_path, "Ren")
    assert is_unreadable(rec) and not is_missing(rec)


@pytest.mark.parametrize("key,bad", [
    ("agent", ""), ("agent", 3), ("agent", None),
    ("model", ""), ("model", "  "), ("model", "\t\n"), ("model", 3), ("model", []), ("model", {}), ("model", True),
    ("source", "bogus"), ("source", []), ("source", None), ("source", 1), ("source", {}),
    ("bound_task", "no-slash"), ("bound_task", ""), ("bound_task", 5), ("bound_task", []),
    ("written_at", "x"), ("written_at", True), ("written_at", None), ("written_at", float("nan")),
])
def test_wrong_types_are_unreadable_and_do_not_raise(tmp_path, key, bad):
    data = dict(GOOD)
    data[key] = bad
    _write(tmp_path, "Ren", data)
    rec = wm.load_record(tmp_path, "Ren")
    assert is_unreadable(rec) and not is_missing(rec)


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '"s"', "{"])
def test_broken_json_is_unreadable_not_missing(tmp_path, text):
    _write(tmp_path, "Ren", text)
    rec = wm.load_record(tmp_path, "Ren")
    assert is_unreadable(rec) and not is_missing(rec)


def test_non_regular_files_are_not_opened(tmp_path):
    target = tmp_path / "elsewhere_dir"
    target.mkdir()                   # 通常ファイルでないものを指す symlink (read_regular_text が通常ファイルだけを読む)
    p = tmp_path / "workers" / "Ren" / "model.json"
    p.parent.mkdir(parents=True)
    p.symlink_to(target)
    assert is_unreadable(wm.load_record(tmp_path, "Ren")) and not is_missing(wm.load_record(tmp_path, "Ren"))
    p.unlink()
    os.mkfifo(p)                     # 開くと書き手が来るまで固まる。開かずに読めない扱いになること
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, %r); import lib_worker_model as w; "
                        "from lib_task_cards import is_unreadable; "
                        "print(is_unreadable(w.load_record(%r, 'Ren')))" % (str(REPO / "scripts"), str(tmp_path))],
                       capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == "True", (r.stdout, r.stderr)


@pytest.mark.parametrize("name", ["", "a/b", ".hidden", "..", "x\0y"])
def test_bad_agent_names_are_refused(tmp_path, name):
    with pytest.raises(ValueError):
        wm.write_record(tmp_path, name, "env", "opus")
    assert is_unreadable(wm.load_record(tmp_path, name))


def test_write_is_atomic_replace_and_leaves_no_tmp(tmp_path):
    wm.write_record(tmp_path, "Ren", "env", "opus")
    wm.write_record(tmp_path, "Ren", "skill", "sonnet")          # 同名の後任が丸ごと置き換える
    d = tmp_path / "workers" / "Ren"
    assert sorted(p.name for p in d.iterdir()) == ["model.json"]
    assert wm.load_record(tmp_path, "Ren")["model"] == "sonnet"


def test_refuses_to_write_an_unusable_record(tmp_path):
    with pytest.raises(ValueError):
        wm.write_record(tmp_path, "Ren", "bogus-source", "opus")
    assert not (tmp_path / "workers").exists()


def test_cli_record_and_show(tmp_path):
    lib = str(REPO / "scripts" / "lib_worker_model.py")
    r = subprocess.run([sys.executable, lib, "record", str(tmp_path), "Ren", "env", "opus"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["model"] == "opus"
    r = subprocess.run([sys.executable, lib, "show", str(tmp_path), "Ren"], capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["source"] == "env"
    r = subprocess.run([sys.executable, lib, "show", str(tmp_path), "Nobody"], capture_output=True, text=True)
    assert r.returncode == 2 and r.stdout.strip() == "missing"
    _write(tmp_path, "Bad", "{")
    r = subprocess.run([sys.executable, lib, "show", str(tmp_path), "Bad"], capture_output=True, text=True)
    assert r.returncode == 1 and r.stdout.startswith("unreadable")
    # モデルが空 (CLI の既定)
    r = subprocess.run([sys.executable, lib, "record", str(tmp_path), "Ren", "none", ""], capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["model"] is None
    r = subprocess.run([sys.executable, lib, "record", str(tmp_path), "Ren", "bogus", "opus"], capture_output=True, text=True)
    assert r.returncode == 1
