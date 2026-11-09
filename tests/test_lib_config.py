#!/usr/bin/env python3
"""scripts/lib_config.py — 共通 + local の重ね (knowledge/machine-local-config.md §3, §4, §6)。

    python3 -m pytest tests/test_lib_config.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_config as lc  # noqa: E402
import lib_task_cards as cards  # noqa: E402

COMMON = """\
mode: tmux
wip_limit: 8
model_per_skill:
  docs: sonnet
  code: opus
telegram:
  enabled: false
  credentials:
    source: op
allowed: [a, b]
attempts:
  max_total: 6
"""

# ENV に印が無い状態で local_path を明示する (印の効きは別のテスト)
NOENV = {}


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture()
def tree(tmp_path):
    common = _write(tmp_path / "config" / "crewvia.yaml", COMMON)
    return tmp_path, common, tmp_path / "config" / "crewvia.local.yaml"


def load(tree, **kw):
    root, common, local = tree
    return lc.load_effective(root, common_path=common, local_path=local, environ=NOENV, **kw)


# --- なし -------------------------------------------------------------------

def test_no_local_is_common_only(tree):
    cfg = load(tree)
    assert cfg["mode"] == "tmux" and cfg["wip_limit"] == 8


def test_empty_and_comment_only_local_is_no_override(tree):
    _, _, local = tree
    for text in ("", "# just a comment\n", "\n\n"):
        _write(local, text)
        assert load(tree)["mode"] == "tmux"


def test_missing_common_is_empty(tmp_path):
    cfg = lc.load_effective(tmp_path, local_path=tmp_path / "none.yaml", environ=NOENV)
    assert cfg == {}


# --- あり -------------------------------------------------------------------

def test_scalar_override_and_deep_merge(tree):
    _, _, local = tree
    _write(local, "mode: rex\nmodel_per_skill:\n  docs: haiku\ntelegram:\n  enabled: true\n")
    cfg = load(tree)
    assert cfg["mode"] == "rex"
    assert cfg["model_per_skill"] == {"docs": "haiku", "code": "opus"}
    assert cfg["telegram"] == {"enabled": True, "credentials": {"source": "op"}}


def test_list_is_replaced_not_appended(tree):
    _, _, local = tree
    _write(local, "mode: rex\n")
    # allowed は共通にしかない。list 置換は ALLOWED_KEYS に載るキーで確かめる
    lc_keys = set(lc.ALLOWED_KEYS)
    assert "mode" in lc_keys
    assert lc.merge({"x": [1, 2]}, {"x": [3]}) == {"x": [3]}


def test_null_replaces_common_value(tree):
    _, _, local = tree
    _write(local, "wip_limit: null\ntelegram:\n  credentials: null\n")
    cfg = load(tree)
    assert cfg["wip_limit"] is None
    assert cfg["telegram"]["credentials"] is None
    assert lc.get_dotted(cfg, "wip_limit") == (False, None)


def test_local_dict_over_null_common_is_allowed():
    assert lc.merge({"a": None}, {"a": {"b": 1}}) == {"a": {"b": 1}}


# --- 壊れ -------------------------------------------------------------------

SECRET = "op://vault/SUPERSECRET-token-value"


@pytest.mark.parametrize("text,code", [
    ("mode: [unclosed\n  - x: : :\n", "local_malformed"),
    ("- a\n- b\n", "local_not_a_mapping"),
    ("just a string\n", "local_not_a_mapping"),
    ("attempts:\n  max_total: 9\n", "local_key_excluded"),
    ("timeout_max_per_skill:\n  code: 1\n", "local_key_excluded"),
    ("mdoe: rex\n", "local_key_not_allowed"),
    ("telegram: oops\n", "type_mismatch"),
    ("model_per_skill: [a]\n", "type_mismatch"),
    ("1: x\n", "local_bad_key"),
])
def test_broken_local_is_unreadable_with_fixed_code(tree, text, code):
    _, _, local = tree
    _write(local, text)
    r = load(tree)
    assert cards.is_unreadable(r)
    assert r.reason.startswith(code)


def test_fifo_local_is_unreadable(tree):
    _, _, local = tree
    local.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(local)
    r = load(tree)
    assert cards.is_unreadable(r) and r.reason.startswith("local_unreadable")


def test_non_utf8_local_is_unreadable(tree):
    _, _, local = tree
    local.write_bytes(b"mode: \xff\xfe\n")
    r = load(tree)
    assert cards.is_unreadable(r) and r.reason.startswith("local_unreadable")


def test_broken_common_is_unreadable(tree):
    _, common, _ = tree
    common.write_text("a: [unclosed\n", encoding="utf-8")
    r = load(tree)
    assert cards.is_unreadable(r) and r.reason.startswith("common_malformed")


def test_errors_never_echo_source_lines_or_values(tree):
    _, _, local = tree
    for text in (
        f"mode: [{SECRET}\n  : :\n",
        f"- {SECRET}\n",
        f"telegram: {SECRET}\n",
        f"bad key {SECRET}: 1\n",
        f"{SECRET}: 1\n",
    ):
        _write(local, text)
        r = load(tree)
        assert cards.is_unreadable(r)
        assert "SUPERSECRET" not in r.reason and "SUPERSECRET" not in repr(r)


def test_cli_error_never_echoes_secret(tmp_path):
    root = tmp_path
    _write(root / "config" / "crewvia.yaml", COMMON)
    _write(root / "config" / "crewvia.local.yaml", f"telegram: {SECRET}\n")
    # root は git 管理外 = スクリプトの位置 (root) だけに落ちる。隔離の印は外す
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWVIA_")}
    r = subprocess.run([sys.executable, str(SCRIPTS / "lib_config.py"), "--root", str(root), "dump", "--json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert "SUPERSECRET" not in r.stdout + r.stderr
    assert r.stderr.startswith("lib_config: type_mismatch")


# --- allowlist --------------------------------------------------------------

def test_allowlist_covers_every_common_top_level_key():
    """共通に新キーが増えたら、allowlist に入れるか除外するかの判断を強制する。"""
    import yaml
    common = yaml.safe_load((REPO_ROOT / "config" / "crewvia.yaml").read_text(encoding="utf-8"))
    unknown = set(common) - lc.ALLOWED_KEYS - lc.EXCLUDED_KEYS
    assert not unknown, f"allowlist に未分類のキー: {sorted(unknown)}"
    assert not (lc.ALLOWED_KEYS & lc.EXCLUDED_KEYS)


# --- テスト隔離の印 ----------------------------------------------------------

def test_isolation_mark_skips_real_local_but_explicit_path_reads(tmp_path):
    # 主 checkout 相当 (git repo) に local を置く
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path / "config" / "crewvia.yaml", COMMON)
    _write(tmp_path / "config" / "crewvia.local.yaml", "mode: rex\n")
    marked = {"CREWVIA_MUX_TEST_ISOLATION": "1"}
    assert lc.load_effective(tmp_path, environ=marked)["mode"] == "tmux"       # 印あり = 共通だけ
    assert lc.load_effective(tmp_path, environ={})["mode"] == "rex"            # 印なし = local が勝つ
    explicit = tmp_path / "config" / "crewvia.local.yaml"
    assert lc.load_effective(tmp_path, local_path=explicit, environ=marked)["mode"] == "rex"


def test_pytest_session_carries_the_mark():
    assert os.environ.get("CREWVIA_MUX_TEST_ISOLATION")


# --- 読み先 -----------------------------------------------------------------

def test_worktree_reads_local_from_main_checkout(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    g = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(g + ["init", "-q", str(main)], check=True)
    _write(main / "config" / "crewvia.yaml", COMMON)
    subprocess.run(g + ["-C", str(main), "add", "-A"], check=True)
    subprocess.run(g + ["-C", str(main), "commit", "-q", "-m", "x"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(g + ["-C", str(main), "worktree", "add", "-q", "-b", "b", str(wt)], check=True)
    _write(main / "config" / "crewvia.local.yaml", "mode: rex\n")
    assert lc.find_main_checkout(wt).resolve() == main.resolve()
    assert lc.find_main_checkout(main).resolve() == main.resolve()
    assert lc.load_effective(wt, environ={})["mode"] == "rex"


def test_non_git_dir_falls_back_to_script_location_not_env(tmp_path, monkeypatch):
    other = tmp_path / "other"
    _write(other / "config" / "crewvia.local.yaml", "mode: rex\n")
    monkeypatch.setenv("CREWVIA_REPO_ROOT", str(other))
    root = tmp_path / "plain"
    _write(root / "config" / "crewvia.yaml", COMMON)
    assert lc.find_main_checkout(root) == root or lc.find_main_checkout(root).resolve() != other.resolve()
    assert lc.load_effective(root, environ={})["mode"] == "tmux"


# --- CLI --------------------------------------------------------------------

def _cli(root, *args):
    env = {k: v for k, v in os.environ.items() if k != "CREWVIA_MUX_TEST_ISOLATION"}
    return subprocess.run([sys.executable, str(SCRIPTS / "lib_config.py"), "--root", str(root), *args],
                          capture_output=True, text=True, env=env)


def test_cli_get_and_dump(tmp_path):
    _write(tmp_path / "config" / "crewvia.yaml", COMMON)
    r = _cli(tmp_path, "get", "mode")
    assert (r.returncode, r.stdout.strip()) == (0, "tmux")
    assert _cli(tmp_path, "get", "model_per_skill.docs").stdout.strip() == "sonnet"
    assert _cli(tmp_path, "get", "telegram.enabled").stdout.strip() == "false"
    assert _cli(tmp_path, "get", "nope", "--default", "zz").stdout.strip() == "zz"
    r = _cli(tmp_path, "get", "nope")
    assert r.returncode == 2 and "key_not_found" in r.stderr
    d = _cli(tmp_path, "dump", "--json")
    assert json.loads(d.stdout)["wip_limit"] == 8
