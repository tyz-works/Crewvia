#!/usr/bin/env python3
"""壊れたデーモン側 JSON 状態ストアで、**倒れる向き** が決めたとおりであること (t026)。

`tests/test_daemon_state_reads_go_through_the_entry.py` は「読み取りが入口を通っている」
ことを構造で見る。こちらは **本物の dispatcher.sh の 1 サイクル** を回し、ストアの中身が
壊れているとき:

  1. サイクルが落ちない (`TypeError` / `AttributeError` を出さない)
  2. WARNING が出る (台帳: `notified-state`)
  3. **再送側** に倒れる — 通知が届く。永久沈黙は作らない (t027 の 10 時間全停止が根拠)
  4. 次に正しく書けたとき、ストアは自己修復している

を確かめる。PR #214 の Kai 3 巡目 P2 は、台帳の **内側のエントリ** が壊れていると
`prune_told()` が毎サイクル `TypeError` になる、というもの。ここでは同じ型を **台帳・通知
スロットル・Rule 5 の状態・相互監視の状態** で確かめる (同じ根)。

実行: python3 -m pytest tests/test_daemon_state_fail_direction.py -v
"""

import json
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent / "scripts"))

# 本物の dispatcher.sh を 1 サイクル回す道具は、姉妹テストのものをそのまま使う。
from test_dispatcher_notify_once import Harness, SLUG, about  # noqa: E402
from test_daemon_state_reads_go_through_the_entry import BAD_LEDGERS  # noqa: E402

import lib_daemon_state  # noqa: E402
import lib_daemon_watch as dw  # noqa: E402


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path / "repo", monkeypatch)


def put_ledger(h, body):
    h.told_file.parent.mkdir(parents=True, exist_ok=True)
    h.told_file.write_text(body if isinstance(body, str) else json.dumps(body))


def ledger_is_healthy(h):
    got = lib_daemon_state.load_json_store(
        h.told_file, check=lib_daemon_state.told_ledger_problem)
    return isinstance(got, dict)


# ---------------------------------------------------------------------------
# 「伝えた」台帳
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(BAD_LEDGERS))
def test_a_malformed_ledger_never_crashes_the_cycle_and_resends(h, name):
    """壊れた台帳 (外側でも内側でも): サイクルは落ちず、WARNING が出て、再送側に倒れる。

    台帳が使えないとき「伝えた」を信じると、通知が欠ける。`prune_told()` が内側で
    `TypeError` になると、サイクルの残りが飛び、状態を離れて戻った task が沈黙する。
    """
    put_ledger(h, BAD_LEDGERS[name])
    h.card("t001", "needs_director", needs_director_reason="x")
    assert len(about(h.cycle(), "t001")) == 1, name           # 落ちず、届く
    assert "WARNING: notified-state" in h.log_text(), name    # 起動失敗として声を出す
    assert ledger_is_healthy(h), name                         # 自己修復した
    assert about(h.cycle(ttl_expired=True), "t001") == []     # 直った後は「1 回だけ」に戻る


def test_a_bad_entry_beside_a_told_one_does_not_silence_the_told_state(h):
    """Kai 3 巡目 P2 の実例。台帳に `{"bad": {"slug": []}}` が混ざる。

    旧実装: 外側だけ検証 → `prune_told()` が毎サイクル TypeError → prune は永久に走らず、
    サイクルの残りも飛ぶ。台帳が「伝えた」を返し続ける限り、この task は **黙る**。
    今: 台帳全体が unreadable → 再送側。
    """
    h.card("t001", "needs_director", needs_director_reason="same reason")
    assert len(about(h.cycle(), "t001")) == 1                # 記録される
    ledger = json.loads(h.told_file.read_text())
    ledger["bad"] = {"slug": []}                             # 内側のエントリを壊す
    put_ledger(h, ledger)
    # 状態を離れて、同じ理由で戻る = 新しい事象。サイクルが落ちてはいけない。
    h.card("t001", "pending")
    h.cycle()
    h.card("t001", "needs_director", needs_director_reason="same reason")
    assert len(about(h.cycle(ttl_expired=True), "t001")) == 1
    assert ledger_is_healthy(h)


def test_a_bad_ledger_that_cannot_be_repaired_still_resends_at_the_throttle_rate(h):
    """台帳が壊れていて、しかも書き換えられない: 連射せず、TTL ごとに再送 (スロットルだけの旧挙動)。"""
    put_ledger(h, {"k": []})
    h.told_file.chmod(0o444)
    h.told_file.parent.chmod(0o555)
    try:
        h.card("t001", "needs_director", needs_director_reason="x")
        total = sum(len(about(h.cycle(), "t001")) for _ in range(4))
        assert total == 1                                    # スロットルは効く: 連射しない
        assert len(about(h.cycle(ttl_expired=True), "t001")) == 1   # TTL 後は再送側
        assert "WARNING: notified-state" in h.log_text()
    finally:
        h.told_file.parent.chmod(0o755)
        h.told_file.chmod(0o644)


def test_what_the_dispatcher_writes_it_can_read_back(h):
    """書き手が書いた台帳を、読み手が必ず受け付ける (自分で書いた台帳を「壊れている」と読まない)。"""
    h.card("t001", "needs_director", needs_director_reason="x")
    h.card("t002", "failed", handoff_path="/tmp/handoff.md")
    h.cycle()
    got = lib_daemon_state.load_json_store(
        h.told_file, check=lib_daemon_state.told_ledger_problem)
    assert isinstance(got, dict) and len(got) == 2, got
    assert "WARNING: notified-state" not in h.log_text()


# ---------------------------------------------------------------------------
# /tmp の通知スロットル
# ---------------------------------------------------------------------------

#: スロットルの壊れ方。値 (JSON リテラル) を全 key に入れる。`None` は外側そのものを壊す。
BAD_THROTTLE_VALUES = {
    "value-string": '"x"',
    "value-list": "[]",
    "value-bool": "true",
    "value-nan": "NaN",             # now - nan > TTL は常に偽 = その key の通知を永久に遮る
    "value-infinity": "Infinity",
    "value-far-future": "1e300",    # 同上 (遠い未来まで遮る)
    "value-negative": "-5",
}
BAD_THROTTLE_OUTER = {"outer-list": "[]", "outer-string": '"x"', "truncated": "{ not json"}


def _break_the_throttle(h, name):
    cache = json.loads(h.notify_cache.read_text())
    assert cache, "スロットルが書かれていない (このテストの前提が崩れている)"
    if name in BAD_THROTTLE_OUTER:
        h.notify_cache.write_text(BAD_THROTTLE_OUTER[name])
        return
    literal = BAD_THROTTLE_VALUES[name]
    h.notify_cache.write_text(
        "{" + ", ".join(f"{json.dumps(k)}: {literal}" for k in cache) + "}")


@pytest.mark.parametrize("name", sorted(BAD_THROTTLE_VALUES) + sorted(BAD_THROTTLE_OUTER))
def test_a_malformed_throttle_cache_does_not_crash_or_silence(h, name):
    """スロットルの値が数でない / NaN / 遠い未来: サイクルは落ちず、通知は届く。

    旧実装: `[]` だと `cache[key] = ...` が TypeError、文字列だと `time.time() - "x"` が
    TypeError でサイクルが落ちる。NaN / 遠い未来は **その key の通知を永久に遮る**。
    """
    h.card("t001", "needs_director", needs_director_reason="x")
    assert len(about(h.cycle(), "t001")) == 1                # 記録 + スロットルが書かれる
    _break_the_throttle(h, name)                              # 実在するスロットル key の値を壊す
    h.told_file.unlink()                                      # 「伝えた」台帳は無い = スロットルだけが効く状況
    assert len(about(h.cycle(), "t001")) == 1, name           # 落ちず、遮られず、届く
    good = lib_daemon_state.load_json_store(
        h.notify_cache, check=lib_daemon_state.notify_cache_problem)
    assert isinstance(good, dict), name                       # 正しい形で書き直された


# ---------------------------------------------------------------------------
# Rule 5 の grace 追跡 (registry/mux/<name>.state.json)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "[]", '"x"', "{}", '{"state": "blocked"}', '{"state": "blocked", "since": "x"}',
    '{"state": 5, "since": 1.0}', '{"state": "blocked", "since": NaN}',
    '{"state": "blocked", "since": true}', "{ not json",
], ids=["list", "string", "empty", "no-since", "since-string", "state-int",
        "since-nan", "since-bool", "truncated"])
def test_a_malformed_rule5_state_entry_means_the_grace_restarts(h, body):
    """Rule 5 の状態が壊れている: `{}` (= grace が最初からやり直し = 通知が遅れる側)。落ちない。

    旧実装は `json.loads(raw)` をそのまま返した: list なら `.get` が AttributeError、
    `since` が文字列なら `now - since` が TypeError で、Rule 5 のサイクルが落ちる。"""
    h.cycle()                                                 # 名前空間を用意する
    path = h.registry / "mux" / "sofia.state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    assert h.ns["_load_state_entry"]("sofia") == {}


def test_a_valid_rule5_state_entry_is_kept(h):
    h.cycle()
    path = h.registry / "mux" / "sofia.state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "blocked", "since": 12.5}))
    assert h.ns["_load_state_entry"]("sofia") == {"state": "blocked", "since": 12.5}


# ---------------------------------------------------------------------------
# 相互監視の状態 (registry/daemons/<peer>.watch.json / .respawns.json)
# ---------------------------------------------------------------------------

def make_watch(tmp_path):
    registry = tmp_path / "registry"
    (registry / "daemons").mkdir(parents=True)
    return dw.DaemonWatch(
        registry_dir=registry, repo_root=tmp_path, self_name="dispatcher", mux=None,
        config=dw.WatchConfig(), log=lambda msg: None, notify=None,
        now=lambda: 10_000.0, scan=lambda *a: [], identity_ok=None)


BAD_WATCH_STATES = {
    "grace-string": {"grace_until": "soon"},
    "grace-dict": {"grace_until": {"a": 1}},
    "grace-list": {"grace_until": [1]},
    "grace-nan": {"grace_until": float("nan")},
    "last-respawn-string": {"last_respawn_at": "yesterday"},
    "hold-bool": {"hold_since": True},
}


@pytest.mark.parametrize("name", sorted(BAD_WATCH_STATES))
def test_a_malformed_watch_state_reads_as_the_defaults_not_a_crash(tmp_path, name):
    """`float("x")` / `float({})` が watch のサイクルを落とさない。既定値 = まだ何も覚えていない。"""
    watch = make_watch(tmp_path)
    dw.write_json_atomic(dw.watch_state_path(watch.registry_dir, watch.peer_name),
                         BAD_WATCH_STATES[name])
    state = watch._read_state()
    assert state == {"grace_until": 0.0, "last_respawn_at": None, "hold_since": None}, name


def test_a_valid_watch_state_is_read_as_written(tmp_path):
    watch = make_watch(tmp_path)
    dw.write_json_atomic(dw.watch_state_path(watch.registry_dir, watch.peer_name),
                         {"grace_until": 5.5, "last_respawn_at": 3.0, "hold_since": 4})
    assert watch._read_state() == {"grace_until": 5.5, "last_respawn_at": 3.0,
                                   "hold_since": 4}


@pytest.mark.parametrize("entries", [
    [1], ["x"], [None], [[]], [{"at": "x"}], [{"at": None}], [{"at": float("nan")}],
    [{"at": float("inf")}], [{"at": True}], [{}],
], ids=["int", "string", "null", "list", "at-string", "at-null", "at-nan", "at-inf",
        "at-bool", "no-at"])
def test_a_malformed_respawn_entry_is_dropped_not_a_crash(tmp_path, entries):
    """この記録は **エントリごとに** 使えないものを捨てる設計。object でないエントリで
    `entry.get` が AttributeError になり、flap ガードのサイクルが落ちていた。"""
    watch = make_watch(tmp_path)
    good = {"at": 9_990.0, "by": "dispatcher", "daemon": "watchdog"}
    dw.write_json_atomic(dw.respawn_log_path(watch.registry_dir, watch.peer_name),
                         {"daemon": "watchdog", "entries": entries + [good]})
    assert watch._flap_entries() == [good]                    # 壊れた 1 件だけ捨て、残りは数える


@pytest.mark.parametrize("body", ["[]", "{ not json", '{"entries": "x"}', '{"entries": 5}'],
                         ids=["list", "truncated", "entries-string", "entries-int"])
def test_an_unusable_respawn_log_reads_as_no_history(tmp_path, body):
    watch = make_watch(tmp_path)
    dw.respawn_log_path(watch.registry_dir, watch.peer_name).write_text(body)
    assert watch._flap_entries() == []
