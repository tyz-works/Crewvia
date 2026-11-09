#!/usr/bin/env python3
"""読み口ごとの「local なし / あり / 壊れ」(knowledge/machine-local-config.md §3, §6, §10)。

    python3 -m pytest tests/test_config_readers_local.py -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_config  # noqa: E402
import lib_escalation  # noqa: E402
import lib_history  # noqa: E402
import lib_irc_relay  # noqa: E402
import lib_model  # noqa: E402
import lib_mux  # noqa: E402
import lib_review_engine  # noqa: E402
import lib_telegram  # noqa: E402

COMMON = """\
mode: tmux  # インラインコメントは値に混ざらない
model_per_skill:
  docs: sonnet
  code: opus
telegram:
  poll_interval_seconds: 20
irc:
  enabled: false
escalation:
  director_after_seconds: 300
  telegram_after_seconds: 600
review:
  engine: codex
attempts:
  max_total: 6
"""

BROKEN = "mode: [unclosed\n"


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    # conftest が立てるテスト隔離の印があると local は読まれない。ここは local を読む場面を試す
    monkeypatch.delenv("CREWVIA_MUX_TEST_ISOLATION", raising=False)
    for k in ("CREWVIA_REVIEW_ENGINE", "CREWVIA_ESCALATION_DIRECTOR_AFTER_SECONDS",
              "CREWVIA_ESCALATION_TELEGRAM_AFTER_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "crewvia.yaml").write_text(COMMON, encoding="utf-8")
    return tmp_path


def common(tree) -> Path:
    return tree / "config" / "crewvia.yaml"


def set_local(tree, text: str) -> None:
    (tree / "config" / "crewvia.local.yaml").write_text(text, encoding="utf-8")


# --- telegram ---------------------------------------------------------------

def test_telegram(tree):
    assert lib_telegram.load_telegram_config(common(tree))["poll_interval_seconds"] == 20
    set_local(tree, "telegram:\n  poll_interval_seconds: 7\n")
    assert lib_telegram.load_telegram_config(common(tree))["poll_interval_seconds"] == 7
    set_local(tree, BROKEN)
    # 壊れ → 未設定 (何も送らない側)。共通の 20 には戻らない
    assert lib_telegram.load_telegram_config(common(tree))["credentials"]["source"] == ""
    assert lib_config.config_problem(common(tree)) is not None


# --- irc --------------------------------------------------------------------

def test_irc(tree):
    assert lib_irc_relay.load_irc_config(common(tree))["enabled"] is False
    set_local(tree, "irc:\n  enabled: true\n")
    assert lib_irc_relay.load_irc_config(common(tree))["enabled"] is True
    set_local(tree, BROKEN)
    assert lib_irc_relay.load_irc_config(common(tree))["enabled"] is False


# --- escalation -------------------------------------------------------------

def _esc(tree):
    cfg = lib_escalation.load_cfg(common(tree), environ={})
    return cfg


def test_escalation(tree):
    base = _esc(tree)
    set_local(tree, "escalation:\n  director_after_seconds: 111\n")
    over = _esc(tree)
    assert over != base
    assert 111 in tuple(over) or 111 in vars(over).values() or "111" in repr(over)
    set_local(tree, BROKEN)
    _esc(tree)  # 例外にならず既定値


# --- review engine ----------------------------------------------------------

def test_review_engine(tree):
    assert lib_review_engine.load_full(str(common(tree)), environ={})["engine"] == "codex"
    set_local(tree, "review:\n  engine: agy\n")
    assert lib_review_engine.load_full(str(common(tree)), environ={})["engine"] == "agy"
    set_local(tree, BROKEN)
    with pytest.raises(lib_review_engine.ReviewConfigError):
        lib_review_engine.load_full(str(common(tree)), environ={})  # codex に倒さない


# --- history (attempts は共有規則 = local で上書き不可) -------------------------

def test_history_attempts(tree):
    assert lib_history.load_default_max_attempts(common(tree)) == 6
    set_local(tree, "attempts:\n  max_total: 2\n")
    with pytest.raises(lib_history.ConfigUnreadable):  # 許可外キー = 壊れ扱い
        lib_history.load_default_max_attempts(common(tree))
    set_local(tree, BROKEN)
    with pytest.raises(lib_history.ConfigUnreadable):
        lib_history.load_default_max_attempts(common(tree))


# --- model ------------------------------------------------------------------

def test_model_resolve(tree):
    assert lib_model.resolve(str(common(tree)), "docs") == "sonnet"
    set_local(tree, "model_per_skill:\n  docs: haiku\n")
    assert lib_model.resolve(str(common(tree)), "docs") == "haiku"
    set_local(tree, BROKEN)
    assert lib_model.resolve(str(common(tree)), "docs") == ""  # --model を付けない


# --- mux mode (D3: 壊れた local で止める / インラインコメント回帰) -----------------

def test_mux_mode(tree):
    assert lib_mux._config_mode(common(tree)) == "tmux"  # `# …` が値に混ざらない
    set_local(tree, "mode: hc\n")
    assert lib_mux._config_mode(common(tree)) == "hc"
    set_local(tree, BROKEN)
    with pytest.raises(lib_mux.MuxConfigUnreadable):
        lib_mux._config_mode(common(tree))


# --- shell の入口 (python3 lib_config.py get) ---------------------------------

def _get(tree, key, default=None):
    cmd = [sys.executable, str(SCRIPTS / "lib_config.py"), "--root", str(tree), "get", key]
    if default is not None:
        cmd += ["--default", default]
    env = {"PATH": "/usr/bin:/bin"}  # 隔離の印なし
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_cli_get(tree):
    r = _get(tree, "mode")
    assert (r.returncode, r.stdout.strip()) == (0, "tmux")
    set_local(tree, "mode: rex\n")
    assert _get(tree, "mode").stdout.strip() == "rex"
    assert _get(tree, "wip_limit", "5").stdout.strip() == "5"
    set_local(tree, BROKEN)
    r = _get(tree, "mode", "tmux")
    assert r.returncode == 1 and "lib_config:" in r.stderr
    assert "unclosed" not in r.stderr  # 元の行を出さない
