#!/usr/bin/env python3
"""
tests/test_rule5_mux_state_error.py — dispatcher Rule 5 が mux state 'error' を黙殺する (t001)

## 背景

`HcBackend.state()` (scripts/lib_mux.py) だけが 'idle' / 'working' / 'done' / 'blocked' に
加えて 'error' を返しうる。dispatcher.sh の Rule 5 (`check_rule5()`) はこれまで「通知する」
条件を `st == 'blocked'` (A) と `st in ('idle', 'done') かつ assignment あり` (B) の 2 本の
列挙でしか判定しておらず、'error' はどちらにも当たらないため
`if not is_A and not is_B: _save_state_entry(...); return` という素通り分岐に落ち、
**Director に一切伝わらなかった** (mission 20261109-hc-production-switch t001)。

## 修正の方向

Rule 5 に `is_E`（「既知の静かな値ではない」）を足し、'error' だけでなく、この関数がまだ
知らない将来の値も黙って捨てず報告側に倒した（詳細: `check_rule5()` のドキュストリング）。
これにより:

  * 'error' は 'blocked' / 'idle-with-task' と同じ作法（grace・NOTIFY_TTL dedup・
    needs_director 除外・working 復帰時の掃除）に乗る。
  * 通知文・dedup キーは固定の 'error' ラベルではなく **実際の state 文字列** を使う
    （`condition = st`, `notify_key = f'{st}_{name}'`）ので、'error' 以外の将来値も
    同じコードパスでそのまま報告される。

## 回帰の確認

tmux / herdr / rex の `state()` は今日の実装では構造的に 'error' を返せない。これを
「読んだ限り」ではなく実際に実行して固定する:

  * TmuxBackend.state() は引数に関わらず常に "unknown" を返す (純粋)。
  * HerdrBackend.state() は `agent_status` が既知の 5 値以外なら無条件に "unknown" へ
    正規化する — 本物の herdr が将来 'error' 相当を返し始めても、ここで吸収される。
  * RexBackend.state() は純粋関数 `rex_screen_state()` の戻り値そのもので、
    その関数は "blocked" / "working" / "idle" / "unknown" の 4 つしか return しない。

本物の dispatcher.sh の埋め込み python を `exec()` して `check_rule5()` を呼ぶ
(`tests/test_background_work_is_not_idle.py` の `Rule5` / `PaneMux` harness を再利用)。
'error' は `st in ('idle', 'done')` に入らないため B1 の裏 job 判定 (`has_job` /
`worker_has_background_work`) を経由しない — 重い `panes` プロセス木 fixture は不要。

実行: python3 -m pytest tests/test_rule5_mux_state_error.py -v
赤の実証: python3 tests/red_proof_rule5_mux_state_error.py
"""

import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lib_mux  # noqa: E402
from test_background_work_is_not_idle import AGENT, TARGET, PaneMux, Rule5  # noqa: E402
from test_dispatcher_notify_once import FakeMux, Harness  # noqa: E402
from test_rex_pr2_wiring import (  # noqa: E402
    IDLE_SCREEN, PERMISSION_SCREEN, TRUST_SCREEN, WORKING_SCREEN,
)


@pytest.fixture
def r5(tmp_path, monkeypatch):
    return Rule5(Harness(tmp_path / "repo", monkeypatch), monkeypatch)


def _run(r5_, waits_on_director=False):
    """`Rule5.run()` と同じだが `waits_on_director` も渡せる (needs_director 除外のテスト用)。"""
    FakeMux.sent = []
    r5_.ns["check_rule5"](AGENT, TARGET, r5_.assignment, {}, waits_on_director)
    return [m["message"] for m in FakeMux.sent]


# ---------------------------------------------------------------------------
# grace / NOTIFY_TTL dedup — 'blocked' / 'idle-with-task' と同じ作法
# ---------------------------------------------------------------------------

def test_error_state_is_silent_before_grace(r5):
    PaneMux.pane_state = "error"
    r5.age_state("error", seconds=10)
    assert r5.run() == []


def test_error_state_notifies_director_after_grace(r5):
    PaneMux.pane_state = "error"
    r5.age_state("error", seconds=600)
    msgs = r5.run()
    assert len(msgs) == 1
    assert "error" in msgs[0]
    assert AGENT in msgs[0]


def test_error_state_dedups_within_notify_ttl(r5):
    PaneMux.pane_state = "error"
    r5.age_state("error", seconds=600)
    assert len(r5.run()) == 1
    assert r5.run() == []  # NOTIFY_TTL 内の再送は抑止


def test_error_state_renotifies_after_ttl_expiry(r5):
    PaneMux.pane_state = "error"
    r5.age_state("error", seconds=600)
    assert len(r5.run()) == 1
    r5.ns["NOTIFY_CACHE"].unlink()  # TTL が過ぎた状況を模す (スロットルだけを消す)
    assert len(r5.run()) == 1


def test_error_state_notifies_even_without_an_assignment(r5):
    """'blocked' と同じ扱い: error は assignment の有無を問わず通知する。"""
    r5.assignment.unlink()
    PaneMux.pane_state = "error"
    r5.age_state("error", seconds=600)
    assert len(r5.run()) == 1


# ---------------------------------------------------------------------------
# needs_director が握っている Worker/task の組は黙る (t032 F4 と同じ除外)
# ---------------------------------------------------------------------------

def test_error_state_is_suppressed_when_the_card_waits_on_director(r5):
    PaneMux.pane_state = "error"
    r5.age_state("error", seconds=600)
    assert _run(r5, waits_on_director=True) == []


# ---------------------------------------------------------------------------
# working に戻ったときの後始末 (dedup キーの掃除 + grace タイマーの再起動)
# ---------------------------------------------------------------------------

def test_recovery_to_working_clears_the_error_dedup_key_and_resets_grace(r5):
    PaneMux.pane_state = "error"
    r5.age_state("error", seconds=600)
    assert len(r5.run()) == 1
    assert f"error_{AGENT}" in r5.ns["load_notify_cache"]()

    PaneMux.pane_state = "working"
    assert r5.run() == []
    assert f"error_{AGENT}" not in r5.ns["load_notify_cache"]()
    assert r5.entry()["state"] == "working"
    assert time.time() - r5.entry()["since"] < 30


def test_a_fresh_error_after_recovery_starts_a_new_grace_period(r5):
    """working 復帰直後に 'error' が再発しても、grace を測り直してから通知する
    (ゼロ秒で即再通知しない — 'blocked'/'idle-with-task' と同じ作法)。"""
    PaneMux.pane_state = "error"
    r5.age_state("error", seconds=600)
    assert len(r5.run()) == 1

    PaneMux.pane_state = "working"
    assert r5.run() == []

    PaneMux.pane_state = "error"
    assert r5.run() == []  # 条件が変わった最初の cycle は grace の起点
    assert r5.entry()["state"] == "error"


# ---------------------------------------------------------------------------
# 根本原因への対応: 既知の集合に無い値は「黙る」ではなく「報告する」側に倒す
# (is_E は 'error' 専用の分岐ではない — その他の未知の値も同じ経路で報告する)
# ---------------------------------------------------------------------------

def test_an_unrecognised_future_state_value_also_notifies_not_silently_drops(r5):
    """今日どの backend も返さない架空の値でも、Rule 5 は黙って捨てない。
    'error' のためだけの特別扱いではなく、列挙から外れた既定を安全側に
    変えたこと自体を固定する。"""
    PaneMux.pane_state = "crashed"
    r5.age_state("crashed", seconds=600)
    msgs = r5.run()
    assert len(msgs) == 1
    assert "crashed" in msgs[0]


def test_idle_without_assignment_is_still_the_one_quiet_case(r5):
    """対照: is_E を足しても、assignment の無い idle/done は従来どおり黙る
    (「既知の集合に無い値は報告」が idle/done にまで暴走していないこと)。"""
    r5.assignment.unlink()
    PaneMux.pane_state = "idle"
    r5.age_state("idle", seconds=600)
    assert r5.run() == []


# ---------------------------------------------------------------------------
# 回帰の確認: tmux / herdr / rex の state() は今日 'error' を返せない (実測)
# ---------------------------------------------------------------------------

def test_tmux_backend_state_is_always_unknown():
    backend = lib_mux.TmuxBackend()
    assert backend.state("anything") == "unknown"
    assert backend.state("") == "unknown"


def test_herdr_backend_normalises_an_unexpected_agent_status_to_unknown(monkeypatch):
    """herdr が (将来) 'error' 相当の agent_status を返し始めても、既知の 5 値の
    allowlist に無ければ 'unknown' に正規化される — Rule 5 まで 'error' は届かない。"""
    backend = lib_mux.HerdrBackend()
    monkeypatch.setattr(lib_mux.HerdrBackend, "_resolve_pane_id",
                         lambda self, name: "pane-1")
    monkeypatch.setattr(
        lib_mux, "_herdr_run",
        lambda cmd_key, extra_args, timeout=10: {
            "result": {"pane": {"agent_status": "error"}}})
    assert backend.state("whatever") == "unknown"


def test_herdr_backend_known_vocabulary_round_trips_unchanged(monkeypatch):
    """対照: 既知の値はそのまま通る (今回の変更が herdr 側の正常系を壊していない)。"""
    backend = lib_mux.HerdrBackend()
    monkeypatch.setattr(lib_mux.HerdrBackend, "_resolve_pane_id",
                         lambda self, name: "pane-1")
    for known in ("blocked", "working", "idle", "done", "unknown"):
        monkeypatch.setattr(
            lib_mux, "_herdr_run",
            lambda cmd_key, extra_args, timeout=10, _s=known: {
                "result": {"pane": {"agent_status": _s}}})
        assert backend.state("whatever") == known


def test_rex_backend_state_vocabulary_never_includes_error():
    """`RexBackend.state()` は `rex_screen_state()` の戻り値そのもの。その関数が
    実際に返しうる値を、承認ダイアログ・スピナー・空欄・ゴミ入力の実物相当で確かめる。"""
    screens = (
        IDLE_SCREEN, WORKING_SCREEN, PERMISSION_SCREEN, TRUST_SCREEN,
        WORKING_SCREEN + PERMISSION_SCREEN,
        "", "   \n\n", "just a shell prompt\n$ ", "complete garbage \x00\x01",
    )
    seen = set()
    for screen in screens:
        result = lib_mux.rex_screen_state(screen)
        assert result != "error"
        seen.add(result)
    assert seen <= {"blocked", "working", "idle", "unknown"}
    assert seen == {"blocked", "working", "idle", "unknown"}  # 前提: 4 値とも実際に出た


def test_hc_backend_is_the_one_backend_whose_state_passes_error_through(monkeypatch):
    """バグの根本そのものを固定する対照: 今日 'error' を実際に返すのは hc だけ。"""
    backend = lib_mux.HcBackend()
    monkeypatch.setattr(
        lib_mux.HcBackend, "_get",
        lambda self, target, timeout=10.0: (
            {"reported": True, "status": {"state": "error"}}, None))
    assert backend.state("whatever") == "error"
