#!/usr/bin/env python3
"""lib_irc_relay の取得 (poll_once) と配達 (deliver_pending) と 1 サイクル (run_cycle) — 偽の relay HTTP サーバーと偽の mux で確かめる。

設計 knowledge/irc-mention-relay.md §4 (状態・配達)・§5 (壊れた入力)・§6 (画面)。本物の bot・本物の mux は使わない。
t003 の Codex が残した P2 2 件 (配達の確認 = verify_sent / 取得件数を空きに合わせる) もここで固定する。

実行: python3 -m pytest tests/test_irc_relay_poll_deliver.py -q
"""

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
REPO = THIS_DIR.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(THIS_DIR))

import lib_irc_relay as L  # noqa: E402
from lib_task_cards import is_missing, is_unreadable  # noqa: E402
from irc_fake_relay import CountingListener, FakeRelay  # noqa: E402

TOKEN = "T" + uuid.uuid4().hex + uuid.uuid4().hex[:8]       # プロセスごとに一意 (/proc の走査が他の pytest を拾わない)
NOW = 1_762_300_200.0


def make_cfg(url, **over):
    cfg = L.default_config()
    cfg.update(enabled=True, url=url, instance="wsl")
    cfg["credentials"] = dict(cfg["credentials"], source="file", file="/unused-in-tests")   # carried の経路は source が有効なときだけ使う
    cfg.update(over)
    return cfg


def tok():
    return L.Token(TOKEN)


def poll(reg, relay, **over):
    return L.poll_once(reg, make_cfg(relay.url, **over), tok(), now=NOW)


def state_of(reg):
    s = L.read_state(reg)
    assert not is_unreadable(s) and not is_missing(s)
    return s


@pytest.fixture
def reg(tmp_path):
    d = tmp_path / "daemons"
    d.mkdir()
    return d


@pytest.fixture
def relay():
    with FakeRelay(token=TOKEN) as r:
        yield r


# ---------------------------------------------------------------------------
# 取得 — 正常系
# ---------------------------------------------------------------------------

def test_first_poll_records_pending_and_cursor_in_one_state(reg, relay):
    relay.add(text="@Sora PR 340 を見てくれる?")
    relay.skip_id()
    relay.add(name="minjun", kind="dm", text="Minjun: t002 は?", account=None)
    out = poll(reg, relay)
    assert out["code"] == "ok" and out["received"] == 2 and out["gap"] is False
    s = state_of(reg)
    assert s["cursor"] == 3 and s["epoch"] == "7f3a9c21"
    assert [(e["epoch"], e["id"], e["name"]) for e in s["pending"]] == [("7f3a9c21", 1, "sora"), ("7f3a9c21", 3, "minjun")]
    assert s["skew"] == relay.now - NOW and s["last_poll_at"] == NOW
    assert s["identity"] == L.make_identity(make_cfg(relay.url))
    assert relay.requests[0]["auth"] == "Bearer " + TOKEN
    assert relay.query_values("instance") == ["wsl"] and relay.query_values("after") == ["0"]


def test_second_poll_asks_after_cursor_and_does_not_duplicate(reg, relay):
    relay.add()
    poll(reg, relay)
    relay.add(text="@Sora again")
    out = poll(reg, relay)
    assert out["received"] == 1
    assert relay.query_values("after") == ["0", "1"]
    assert [e["id"] for e in state_of(reg)["pending"]] == [1, 2]


def test_poll_dedupes_same_epoch_id_already_pending(reg, relay):
    relay.add()
    poll(reg, relay)
    s = state_of(reg)
    s["cursor"] = 0                                   # cursor だけ戻っても二重に積まない
    assert L.write_state(reg, s)
    assert poll(reg, relay)["received"] == 0
    assert len(state_of(reg)["pending"]) == 1


def test_empty_response_advances_cursor_to_head(reg, relay):
    relay.add(instance="other")                       # 他の instance 宛 (id 1)
    out = poll(reg, relay)
    assert out["code"] == "ok" and out["received"] == 0
    assert state_of(reg)["cursor"] == 1


def test_stored_text_is_sanitized(reg, relay):
    relay.add(text="hi\x1b[31m `rm` $HOME !!\nsecond", sender="n\x1bick")
    poll(reg, relay)
    e = state_of(reg)["pending"][0]
    blob = json.dumps(e, ensure_ascii=False)
    assert "\x1b" not in blob and "`" not in blob and "$" not in blob and "\\n" not in blob
    assert e["text"] == "hi[31m ｀rm｀ ＄HOME ！！ second"


def test_gap_is_reported_and_remaining_mentions_are_kept(reg, relay):
    relay.add()
    relay.gap = True
    out = poll(reg, relay)
    assert out["gap"] is True and out["received"] == 1


def test_expired_by_bot_clock_is_not_taken(reg, relay):
    relay.add(ts=relay.now - L.EXPIRY_SECONDS - 1)
    relay.add(ts=relay.now - 10)
    out = poll(reg, relay)
    assert out["received"] == 1 and state_of(reg)["cursor"] == 2


# ---------------------------------------------------------------------------
# t003 P2: 取得件数を空きに合わせる
# ---------------------------------------------------------------------------

def fill_pending(reg, relay, n):
    relay.add()
    poll(reg, relay)
    s = state_of(reg)
    base = s["pending"][0]
    s["pending"] = [dict(base, id=1000 + i) for i in range(n)]
    assert L.write_state(reg, s)


def test_limit_is_min_batch_and_free_space(reg, relay):
    fill_pending(reg, relay, 190)
    poll(reg, relay)
    assert relay.query_values("limit")[-1] == "10"
    fill_pending(reg, relay, 20)
    poll(reg, relay)
    assert relay.query_values("limit")[-1] == "50"


def test_full_pending_does_not_poll_and_does_not_move_cursor(reg, relay):
    fill_pending(reg, relay, L.PENDING_MAX)
    before = state_of(reg)
    n = len(relay.requests)
    relay.add(text="@Sora more")
    out = poll(reg, relay)
    assert out["code"] == "pending_full" and len(relay.requests) == n
    assert state_of(reg) == before


def test_never_advances_past_entries_that_were_not_kept(reg, relay):
    fill_pending(reg, relay, 197)                       # 空き 3
    for i in range(10):
        relay.add(text=f"@Sora {i}")
    out = poll(reg, relay)
    s = state_of(reg)
    assert out["received"] == 3 and len(s["pending"]) == L.PENDING_MAX
    assert s["cursor"] == relay.mentions[3]["id"] or s["cursor"] <= relay.mentions[3]["id"]
    # 空いたら残りが取れる (飛ばされていない)
    s["pending"] = s["pending"][-5:]
    assert L.write_state(reg, s)
    poll(reg, relay)
    ids = {e["id"] for e in state_of(reg)["pending"]}
    assert {m["id"] for m in relay.mentions} <= ids | {1}


def test_response_larger_than_limit_is_rejected_whole(reg, relay):
    fill_pending(reg, relay, 197)
    cursor = state_of(reg)["cursor"]
    for i in range(5):
        relay.add(text=f"@Sora {i}")
    # 偽サーバーが limit を無視した応答を返す
    relay.mode = "badjson"
    relay.raw_body = json.dumps({
        "api": 1, "epoch": relay.epoch, "instance": "wsl", "now": relay.now, "head_id": 10, "cursor": 10,
        "has_more": False, "gap": False,
        "mentions": [{k: v for k, v in m.items() if not k.startswith("_")} for m in relay.mentions]}).encode()
    out = poll(reg, relay)
    assert out["code"] == "bad_shape"
    assert state_of(reg)["cursor"] == cursor and len(state_of(reg)["pending"]) == 197


# ---------------------------------------------------------------------------
# 壊れた入力 (§5-1)
# ---------------------------------------------------------------------------

def test_unauthorized_keeps_state_and_slows_down(reg, relay):
    relay.add()
    poll(reg, relay)
    before = state_of(reg)
    relay.token = "other-" + TOKEN
    out = poll(reg, relay)
    assert out["code"] == "unauthorized"
    s = state_of(reg)
    assert (s["cursor"], s["pending"], s["epoch"]) == (before["cursor"], before["pending"], before["epoch"])
    assert L.effective_interval(s, make_cfg(relay.url)) == L.INTERVAL_MAX_SECONDS


@pytest.mark.parametrize("mode,code", [("404", "api_unsupported"), ("api2", "api_unsupported"), ("http500", "http_5xx"),
                                       ("redirect", "http_redirect")])
def test_error_modes_keep_cursor_and_pending(reg, relay, mode, code):
    relay.add()
    poll(reg, relay)
    before = state_of(reg)
    relay.mode = mode
    out = poll(reg, relay)
    assert out["code"] == code
    s = state_of(reg)
    assert (s["cursor"], s["pending"]) == (before["cursor"], before["pending"])
    if mode in ("http500", "redirect"):
        assert s["fail_streak"] == 1 and out["fail_streak"] == 1


def test_unreachable_counts_streak_and_doubles_interval(reg):
    cfg = make_cfg("http://127.0.0.1:9")                # 閉じたポート
    for i in range(1, 4):
        out = L.poll_once(reg, cfg, tok(), now=NOW + i)
        assert out["code"] == "http_unreachable" and out["fail_streak"] == i
    s = state_of(reg)
    assert L.effective_interval(s, cfg) == 10 * 8
    assert not L.is_due(s, cfg, s["last_poll_at"] + 79) and L.is_due(s, cfg, s["last_poll_at"] + 80)


def test_success_resets_failure_counters(reg, relay):
    relay.mode = "http500"
    poll(reg, relay)
    poll(reg, relay)
    relay.mode = "ok"
    out = poll(reg, relay)
    assert out["code"] == "ok" and state_of(reg)["fail_streak"] == 0 and out["fail_streak"] == 0


def test_redirect_is_never_followed(reg, relay):
    with CountingListener() as second:
        relay.mode = "redirect"
        relay.redirect_to = second.url
        out = poll(reg, relay)
        time.sleep(0.4)
        assert out["code"] == "http_redirect" and second.connections == 0
    assert len(relay.mention_requests()) == 1


def test_proxy_environment_is_ignored(reg, relay, monkeypatch):
    with CountingListener() as proxy:
        hostport = proxy.url.rstrip("/").replace("http://", "")
        for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.setenv(var, "http://" + hostport)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        relay.add()
        out = poll(reg, relay)
        time.sleep(0.3)
        assert out["code"] == "ok" and out["received"] == 1 and proxy.connections == 0


def test_bad_json_is_counted_and_nothing_is_adopted(reg, relay):
    relay.add()
    poll(reg, relay)
    before = state_of(reg)
    relay.add(text="@Sora second")
    relay.mode = "badjson"
    for i in (1, 2, 3):
        out = poll(reg, relay)
        assert out["code"] == "bad_json" and out["bad_streak"] == i
    s = state_of(reg)
    assert (s["cursor"], s["pending"]) == (before["cursor"], before["pending"])
    relay.mode = "ok"
    out = poll(reg, relay)
    assert out["received"] == 1 and out["bad_streak"] == 0


def test_partially_invalid_response_is_dropped_whole(reg, relay):
    relay.add()
    relay.add(text="@Sora two")
    relay.mode = "badjson"
    good = [{k: v for k, v in m.items() if not k.startswith("_")} for m in relay.mentions]
    good[1]["kind"] = "bogus"
    relay.raw_body = json.dumps({"api": 1, "epoch": relay.epoch, "instance": "wsl", "now": relay.now, "head_id": 2,
                                 "cursor": 2, "has_more": False, "gap": False, "mentions": good}).encode()
    out = poll(reg, relay)
    assert out["code"] == "bad_shape"
    assert state_of(reg)["pending"] == [] and state_of(reg)["cursor"] == 0


def test_oversize_response_is_bad_json(reg, relay):
    relay.mode = "huge"
    assert poll(reg, relay)["code"] == "bad_json"


def test_hang_is_a_timeout_not_a_stall(reg, relay):
    relay.mode = "hang"
    relay.hang_seconds = 3
    t0 = time.monotonic()
    out = poll(reg, relay, request_timeout_seconds=1)
    assert out["code"] == "http_unreachable" and time.monotonic() - t0 < 2.5


def test_conflict_after_ahead_resets_cursor_and_records_epoch(reg, relay):
    relay.add()
    poll(reg, relay)
    s = state_of(reg)
    s["cursor"] = 999
    assert L.write_state(reg, s)
    relay.mode = "409"
    out = poll(reg, relay)
    assert out["code"] == "cursor_reset" and out["cursor_reset"] is True
    s = state_of(reg)
    assert s["cursor"] == 0 and s["epoch"] == relay.epoch and len(s["pending"]) == 1
    relay.mode = "ok"
    assert poll(reg, relay)["code"] == "ok"             # 次のサイクルで取り直せる (dedupe で二重にならない)
    assert len(state_of(reg)["pending"]) == 1


def test_epoch_change_resets_cursor_but_keeps_pending_with_old_epoch(reg, relay):
    relay.add()
    poll(reg, relay)
    relay.epoch = "aaaa1111"
    relay.add(text="@Sora new world")
    out = poll(reg, relay)
    assert out["epoch_changed"] is True and out["received"] == 0
    s = state_of(reg)
    assert s["cursor"] == 0 and s["epoch"] == "aaaa1111"
    assert [(e["epoch"], e["id"]) for e in s["pending"]] == [("7f3a9c21", 1)]      # 要素の epoch は書き換えない
    out = poll(reg, relay)
    assert out["received"] == 2
    assert sorted((e["epoch"], e["id"]) for e in state_of(reg)["pending"]) == [
        ("7f3a9c21", 1), ("aaaa1111", 1), ("aaaa1111", 2)]


def test_unreadable_state_blocks_everything(reg, relay):
    relay.add()
    (reg / L.STATE_FILE).write_text("{broken", encoding="utf-8")
    out = poll(reg, relay)
    assert out["code"] == "state_unreadable" and relay.requests == []
    (reg / L.STATE_FILE).write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert poll(reg, relay)["code"] == "state_unreadable" and relay.requests == []


@pytest.mark.parametrize("breaker", [
    lambda s: s["pending"][0].pop("epoch"),                       # epoch 無し
    lambda s: s["pending"][0].update(epoch="XYZ"),
    lambda s: s["pending"][0].update(attempts="1"),
    lambda s: s.update(cursor=-1),
    lambda s: s.update(identity={"instance": "wsl"}),
    lambda s: s.update(pending="x"),
    lambda s: s.pop("fail_streak"),
    lambda s: s.update(extra=1),
])
def test_every_required_field_is_enforced_on_read(reg, relay, breaker):
    relay.add()
    poll(reg, relay)
    s = state_of(reg)
    breaker(s)
    (reg / L.STATE_FILE).write_text(json.dumps(s), encoding="utf-8")
    assert is_unreadable(L.read_state(reg))
    n = len(relay.requests)
    assert poll(reg, relay)["code"] == "state_unreadable" and len(relay.requests) == n


def test_state_write_failure_leaves_cursor_unmoved(reg, relay, monkeypatch):
    relay.add()
    poll(reg, relay)
    before = state_of(reg)
    relay.add(text="@Sora two")
    monkeypatch.setattr(L, "write_state", lambda *a, **k: False)
    assert poll(reg, relay)["code"] == "state_unwritable"
    monkeypatch.undo()
    assert state_of(reg) == before


def test_invalid_config_does_not_touch_anything(reg):
    for cfg in (make_cfg("http://8.8.8.8"), make_cfg("http://127.0.0.1:1", instance="bad name"), make_cfg("")):
        out = L.poll_once(reg, cfg, tok(), now=NOW)
        assert out["code"] in ("url_not_allowed", "config_invalid")
    assert list(reg.iterdir()) == []


# ---------------------------------------------------------------------------
# identity (§4-3)
# ---------------------------------------------------------------------------

def test_identity_change_quarantines_old_pending_and_starts_fresh(reg, relay):
    relay.add()
    relay.add(text="@Sora two")
    poll(reg, relay)
    with FakeRelay(token=TOKEN, epoch="bbbb2222") as other:
        other.add(text="@Sora from other bot")
        out = L.poll_once(reg, make_cfg(other.url), tok(), now=NOW + 1)
        assert out["code"] == "ok" and out["quarantined"] == 2 and "quarantined_from" in out
        s = state_of(reg)
        assert [e["text"] for e in s["pending"]] == ["@Sora from other bot"]
        assert other.query_values("after") == ["0"]                      # 旧 cursor を新しい bot に当てない
    q = L.read_quarantine(reg)
    assert len(q["batches"]) == 1 and len(q["batches"][0]["pending"]) == 2
    assert q["batches"][0]["identity"] == L.make_identity(make_cfg(relay.url))


def test_instance_change_is_an_identity_change(reg, relay):
    relay.add()
    poll(reg, relay)
    out = L.poll_once(reg, make_cfg(relay.url, instance="other"), tok(), now=NOW + 1)
    assert out["quarantined"] == 1 and state_of(reg)["identity"]["instance"] == "other"


def test_identity_change_with_unwritable_quarantine_does_not_poll(reg, relay, monkeypatch):
    relay.add()
    poll(reg, relay)
    before = state_of(reg)
    n = len(relay.requests)
    monkeypatch.setattr(L, "_quarantine_pending", lambda *a, **k: False)
    out = L.poll_once(reg, make_cfg(relay.url, instance="other"), tok(), now=NOW + 1)
    assert out["code"] == "identity_changed_failed" and len(relay.requests) == n
    assert state_of(reg) == before


def test_token_rotation_keeps_cursor(reg, relay):
    relay.add()
    poll(reg, relay)
    relay.token = "rotated-" + TOKEN
    out = L.poll_once(reg, make_cfg(relay.url), L.Token("rotated-" + TOKEN), now=NOW + 1)
    assert out["code"] == "ok" and out["quarantined"] == 0 and state_of(reg)["cursor"] == 1


# ---------------------------------------------------------------------------
# 配達 (§4-5・§6-1)。偽の mux
# ---------------------------------------------------------------------------

RULE = "─" * 40
READY = f"output\n{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode\n"


class FakeMux:
    def __init__(self, windows=("Sora-director",), screens=None, verify=True, send_ok=True):
        self.windows = list(windows)
        self.screens = screens or {}
        self.verify = verify
        self.send_ok = send_ok
        self.sent = []
        self.list_calls = 0
        self.captures = []
        self.after_send = None

    def list(self, suffix=None):
        self.list_calls += 1
        return [w for w in self.windows if suffix is None or w.endswith(suffix)]

    def capture(self, name):
        self.captures.append(name)
        if self.after_send is not None:
            # 送信の直後の画面: verify が真なら入力欄は空 (次の capture から通常の画面)、偽なら送った文が欄に残る
            sent_name, text = self.after_send
            try:
                ok = self.verify(sent_name, text) if callable(self.verify) else self.verify
            except Exception:  # noqa: BLE001 — 読めない画面 = 確認できない
                return ""
            if not ok:
                return READY.replace("❯ \n", "❯ " + text[:20] + "\n")
            self.after_send = None
        return self.screens.get(name, READY)

    def send(self, name, text):
        self.sent.append((name, text))
        self.after_send = (name, text)
        return self.send_ok


def ent(i, name="sora", **over):
    e = {"epoch": "7f3a9c21", "id": i, "ts": int(NOW) - 100, "name": name, "kind": "channel", "channel": "#crewvia",
         "sender": "tyz", "account": "tyz", "text": f"@{name} msg {i}", "truncated": False, "suppressed_before": 0,
         "attempts": 0, "last_attempt_at": None}
    e.update(over)
    return e


def st(*entries, skew=0):
    s = L.fresh_state({"instance": "wsl", "endpoint": "0" * 16})
    s["pending"] = list(entries)
    s["skew"] = skew
    return s


CFG = make_cfg("http://127.0.0.1:1")


def deliver(state, mux, cfg=CFG, now=NOW):
    return L.deliver_pending(state, mux, cfg, now, sleep=lambda s: None)


def test_deliver_to_ready_pane_and_remove_after_verify():
    mux = FakeMux()
    new, stats = deliver(st(ent(41)), mux)
    assert new["pending"] == [] and stats["delivered"] == 1
    assert mux.sent == [("Sora-director", L.format_line(ent(41), "Sora", 300))]
    assert mux.captures == ["Sora-director"] * 2 and mux.list_calls == 1


def test_t003_p2_unverified_send_stays_pending():
    mux = FakeMux(verify=False)
    new, stats = deliver(st(ent(41), ent(42)), mux)
    assert [e["id"] for e in new["pending"]] == [41, 42] and stats["delivered"] == 0 and stats["failed"] == 1
    assert new["pending"][0]["attempts"] == 1 and new["pending"][0]["last_attempt_at"] == NOW
    assert new["pending"][1]["attempts"] == 0
    assert len(mux.sent) == 1                                  # 失敗した窓にはそれ以上送らない


def test_verify_is_retried_once_before_giving_up():
    seen = []

    def verify(name, text):
        seen.append(1)
        return len(seen) >= 2

    new, _ = deliver(st(ent(41)), FakeMux(verify=verify))
    assert new["pending"] == [] and len(seen) == 2


def test_verify_exception_and_send_exception_keep_pending():
    def boom(*a):
        raise RuntimeError("x")

    new, stats = deliver(st(ent(41)), FakeMux(verify=boom))
    assert len(new["pending"]) == 1 and stats["failed"] == 1
    mux = FakeMux()
    mux.send = boom
    new, stats = deliver(st(ent(41)), mux)
    assert len(new["pending"]) == 1 and new["pending"][0]["attempts"] == 1


def test_send_failure_keeps_pending_and_counts_attempt():
    new, stats = deliver(st(ent(41)), FakeMux(send_ok=False))
    assert len(new["pending"]) == 1 and new["pending"][0]["attempts"] == 1 and stats["failed"] == 1


@pytest.mark.parametrize("screen,kind", [
    (f"out\n{RULE}\n❯ typing…\n{RULE}\n", "draft"),
    ("Do you want to proceed?\n❯ 1. Yes\n  2. No\n", "dialog"),
    ("", "unknown"),
    ("$ \n", "unknown"),
])
def test_unready_pane_defers_without_sending_or_counting_attempts(screen, kind):
    mux = FakeMux(screens={"Sora-director": screen})
    new, stats = deliver(st(ent(41)), mux)
    assert mux.sent == [] and new["pending"][0]["attempts"] == 0 and stats["deferred"] == {kind: 1}


def test_no_window_keeps_pending_and_is_not_an_error():
    new, stats = deliver(st(ent(41)), FakeMux(windows=["Minjun-worker"]))
    assert len(new["pending"]) == 1 and stats["delivered"] == 0 and stats["failed"] == 0


def test_ambiguous_target_is_not_delivered():
    mux = FakeMux(windows=["Sora-director", "SORA-director"])
    new, stats = deliver(st(ent(41)), mux)
    assert mux.sent == [] and stats["ambiguous"] == 1 and len(new["pending"]) == 1


def test_allowed_targets_filter():
    mux = FakeMux(windows=["sora-worker"])
    new, _ = deliver(st(ent(41)), mux, make_cfg("http://127.0.0.1:1", allowed_targets=["director"]))
    assert mux.sent == [] and len(new["pending"]) == 1
    new, _ = deliver(st(ent(41)), mux, make_cfg("http://127.0.0.1:1", allowed_targets=["worker"]))
    assert new["pending"] == [] and mux.sent[0][0] == "sora-worker"


def test_case_insensitive_window_names_use_the_window_spelling_in_the_frame():
    mux = FakeMux(windows=["SORA-director"])
    deliver(st(ent(41)), mux)
    assert "宛先=SORA " in mux.sent[0][1]


def test_per_name_cap_one_normal_line_plus_one_omit_line():
    mux = FakeMux()
    new, stats = deliver(st(*[ent(i) for i in range(41, 46)]), mux)
    assert new["pending"] == [] and len(mux.sent) == 2
    assert "本文=" in mux.sent[0][1] and "id=7f3a9c21:41 " in mux.sent[0][1]
    assert "省略=4" in mux.sent[1][1] and "id=7f3a9c21:42-45" in mux.sent[1][1] and '本文="' not in mux.sent[1][1]
    assert stats["delivered"] == 1 and stats["omitted"] == 4


def test_two_entries_are_two_plain_lines_and_over_two_are_summarized():
    mux = FakeMux()
    deliver(st(ent(41), ent(42)), mux)
    assert len(mux.sent) == 2 and all("省略=" not in t for _, t in mux.sent)
    mux = FakeMux()
    deliver(st(ent(41), ent(42), ent(43)), mux)
    assert len(mux.sent) == 2 and "省略=2" in mux.sent[1][1]


def test_cycle_cap_spreads_names_over_cycles_without_omitting():
    mux = FakeMux(windows=["Sora-director", "Minjun-worker"])
    state = st(ent(1, "sora"), ent(2, "sora"), ent(3, "minjun"), ent(4, "minjun"))
    new, _ = deliver(state, mux)                               # 合計 3 行
    assert len(mux.sent) == 3
    assert [(e["name"], e["id"]) for e in new["pending"]] == [("minjun", 4)]
    mux2 = FakeMux(windows=["Sora-director", "Minjun-worker"])
    new, _ = deliver(new, mux2)
    assert new["pending"] == [] and len(mux2.sent) == 1


def test_omission_needs_two_lines_of_budget_otherwise_sends_only_the_oldest():
    mux = FakeMux(windows=["Sora-director", "Minjun-worker"])
    state = st(ent(1, "sora"), ent(2, "sora"), ent(3, "minjun"), ent(4, "minjun"), ent(5, "minjun"), ent(6, "minjun"))
    new, _ = deliver(state, mux)                               # sora 2 行 + minjun は残り 1 行だけ
    assert [t for _, t in mux.sent].count(next(t for _, t in mux.sent if "id=7f3a9c21:3 " in t)) == 1
    assert not any("省略=" in t for _, t in mux.sent)
    assert [e["id"] for e in new["pending"]] == [4, 5, 6]


def test_failed_omit_line_keeps_all_omitted_entries():
    calls = []

    def verify(name, text):
        calls.append(text)
        return "省略=" not in text

    new, stats = deliver(st(*[ent(i) for i in range(1, 6)]), FakeMux(verify=verify))
    assert [e["id"] for e in new["pending"]] == [2, 3, 4, 5] and stats["omitted"] == 0


def test_expired_entries_are_dropped_using_bot_clock_skew():
    old = ent(1, ts=int(NOW) - L.EXPIRY_SECONDS - 10)
    fresh = ent(2)
    new, stats = deliver(st(old, fresh), FakeMux(windows=[]))
    assert [e["id"] for e in new["pending"]] == [2] and stats["expired"] == 1
    # 期限の境界は bot の時計: crewvia の時計が 1 時間進んでいても skew で補正される
    edge = ent(3, ts=int(NOW) - L.EXPIRY_SECONDS + 600)
    new, _ = deliver(st(edge, skew=0), FakeMux(windows=[]))
    assert len(new["pending"]) == 1
    new, _ = deliver(st(edge, skew=700), FakeMux(windows=[]))
    assert new["pending"] == []
    new, _ = deliver(st(edge, skew=-3600), FakeMux(windows=[]))
    assert len(new["pending"]) == 1


def test_mux_is_not_asked_when_nothing_is_pending_and_list_at_most_once():
    mux = FakeMux()
    deliver(st(), mux)
    assert mux.list_calls == 0
    mux = FakeMux(windows=["Sora-director", "Minjun-worker"])
    deliver(st(ent(1, "sora"), ent(2, "minjun"), ent(3, "nobody")), mux)
    assert mux.list_calls == 1


def test_deliver_does_not_mutate_the_input_state():
    state = st(ent(41))
    snapshot = json.loads(json.dumps(state))
    deliver(state, FakeMux())
    deliver(state, FakeMux(verify=False))
    assert state == snapshot


def test_delivered_line_has_no_control_characters_even_for_hostile_stored_text():
    hostile = L.sanitize_for_pane("a\x1b[2J\x03\n`x`‮\u0085 end")
    mux = FakeMux()
    deliver(st(ent(41, text=hostile)), mux)
    line = mux.sent[0][1]
    assert all(ord(c) >= 0x20 and not (0x7F <= ord(c) <= 0x9F) for c in line) and line.startswith(L.FRAME_PREFIX)


# ---------------------------------------------------------------------------
# run_cycle
# ---------------------------------------------------------------------------

def run_cycle(reg, cfg, token, mux, runner=None, reason="", now=NOW):
    """poll のサブプロセスは config を自分で読む (本番と同じ)。cfg を YAML にして `--config` で渡す。"""
    import yaml
    cfg_path = reg.parent / "crewvia.yaml"
    cfg_path.write_text(yaml.safe_dump({"irc": cfg}), encoding="utf-8")
    kwargs = {"runner": runner} if runner else {}
    return L.run_cycle(reg, cfg, token, reason, mux, now=now, config_path=cfg_path, sleep=lambda s: None, **kwargs)


def no_runner(*a, **k):
    raise AssertionError("poll subprocess was started")


def test_disabled_writes_nothing_reads_nothing_sends_nothing(reg):
    mux = FakeMux()
    cfg = L.default_config()
    out = run_cycle(reg, cfg, None, mux, no_runner)
    assert out["reason"] == "disabled" and list(reg.iterdir()) == [] and mux.sent == [] and mux.list_calls == 0


def test_disabled_rewrites_an_existing_receiver_file_only(reg):
    receiver = {"enabled": True, "checked_at": NOW - 5, "reason": "ok", "api": 1, "last_ok_at": NOW - 5, "pending_count": 0}
    (reg / L.RECEIVER_FILE).write_text(json.dumps(receiver), encoding="utf-8")
    run_cycle(reg, L.default_config(), None, FakeMux(), no_runner)
    r = L.read_receiver(reg)
    assert r["enabled"] is False and r["reason"] == "disabled"
    assert sorted(p.name for p in reg.iterdir() if not p.name.endswith(".lock")) == [L.RECEIVER_FILE]


def test_bad_url_disables_without_polling(reg):
    out = run_cycle(reg, make_cfg("http://8.8.8.8"), tok(), FakeMux(), no_runner)
    assert out["reason"] == "url_not_allowed" and out["enabled"] is False
    r = L.read_receiver(reg)
    assert r["enabled"] is False and r["reason"] == "url_not_allowed"
    assert not (reg / L.STATE_FILE).exists()


def test_missing_credentials_disable_with_the_fixed_code(reg):
    out = run_cycle(reg, make_cfg("http://127.0.0.1:1"), None, FakeMux(), no_runner, reason="credential_command_failed")
    assert out["reason"] == "credential_command_failed"
    assert L.read_receiver(reg)["reason"] == "credential_command_failed" and not (reg / L.STATE_FILE).exists()


def test_unreadable_state_means_no_poll_no_send(reg):
    (reg / L.STATE_FILE).write_text("nope", encoding="utf-8")
    mux = FakeMux()
    out = run_cycle(reg, make_cfg("http://127.0.0.1:1"), tok(), mux, no_runner)
    assert out["state_unreadable"] is True and out["reason"] == "state_unreadable" and mux.sent == []
    assert L.read_receiver(reg)["enabled"] is False


def test_end_to_end_with_real_subprocess_poll_and_delivery(reg, relay):
    relay.add(text="@Sora PR 340 を見てくれる?")
    mux = FakeMux()
    out = run_cycle(reg, make_cfg(relay.url), tok(), mux)
    assert out["polled"] is True and out["poll"]["code"] == "ok" and out["delivered"] == 1
    assert mux.sent[0][1].startswith(L.FRAME_PREFIX) and "PR 340" in mux.sent[0][1]
    assert state_of(reg)["pending"] == [] and state_of(reg)["cursor"] == 1
    r = L.read_receiver(reg)
    assert r["enabled"] is True and r["reason"] == "ok" and r["api"] == 1 and r["pending_count"] == 0


def test_not_due_does_not_poll_but_still_delivers_pending(reg, relay):
    relay.add()
    now = time.time()                                  # poll のサブプロセスは実時計で last_poll_at を書く
    run_cycle(reg, make_cfg(relay.url), tok(), FakeMux(windows=[]), now=now)        # 窓が無い: pending に残る
    assert len(state_of(reg)["pending"]) == 1
    mux = FakeMux()
    out = run_cycle(reg, make_cfg(relay.url), tok(), mux, no_runner, now=now + 1)
    assert out["polled"] is False and out["delivered"] == 1 and state_of(reg)["pending"] == []


def test_full_pending_does_not_start_a_poll(reg, relay):
    fill_pending(reg, relay, L.PENDING_MAX)
    n = len(relay.requests)
    out = run_cycle(reg, make_cfg(relay.url), tok(), FakeMux(windows=[]), no_runner, now=NOW + 1000)
    assert out["polled"] is False and len(relay.requests) == n


def test_identity_mismatch_never_delivers_old_pending(reg, relay):
    relay.add()
    run_cycle(reg, make_cfg(relay.url), tok(), FakeMux(windows=[]))
    mux = FakeMux()
    with FakeRelay(token=TOKEN, epoch="cccc3333") as other:
        out = run_cycle(reg, make_cfg(other.url), tok(), mux, now=NOW + 1)
    assert mux.sent == [] and out["poll"]["quarantined"] == 1 and out["pending_count"] == 0
    assert L.read_receiver(reg)["quarantine"]["count"] == 1


def test_poll_subprocess_failure_is_not_fatal(reg, relay):
    class Dead:
        stdout = "not json"
        returncode = 1

    out = run_cycle(reg, make_cfg(relay.url), tok(), FakeMux(), lambda *a, **k: Dead())
    assert out["poll"] == {"code": "poll_failed"} and out["reason"] == "poll_failed"

    def timeout(*a, **k):
        import subprocess
        raise subprocess.TimeoutExpired("x", 1)

    assert run_cycle(reg, make_cfg(relay.url), tok(), FakeMux(), timeout, now=NOW + 1)["poll"] == {"code": "poll_timeout"}


def test_cycle_never_raises(reg, relay):
    class Exploding:
        def list(self, suffix=None):
            raise RuntimeError("boom")

    relay.add()
    out = run_cycle(reg, make_cfg(relay.url), tok(), Exploding())
    assert isinstance(out, dict)
    out = L.run_cycle(reg, make_cfg(relay.url), tok(), "", object(), now=NOW + 5)      # mux が壊れた object
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# token の漏れ (§9)
# ---------------------------------------------------------------------------

def test_token_never_in_argv_state_receiver_or_summary(reg, relay):
    captured = {}

    def spy(cmd, **kw):
        captured["cmd"] = cmd
        captured["env_keys"] = sorted(k for k, v in kw["env"].items() if TOKEN in v)
        import subprocess
        return subprocess.run(cmd, **kw)

    relay.add()
    out = run_cycle(reg, make_cfg(relay.url), tok(), FakeMux(), spy)
    assert TOKEN not in " ".join(captured["cmd"]) and captured["env_keys"] == [L.CARRIED_TOKEN_VAR]
    for p in reg.iterdir():
        assert TOKEN not in p.read_text(encoding="utf-8"), p.name
    assert TOKEN not in json.dumps(out)


def test_token_not_in_any_process_cmdline_while_polling(reg, relay):
    relay.delay = 0.8
    relay.add()
    stop = threading.Event()
    hits = []

    def scan():
        while not stop.is_set():
            for pid in os.listdir("/proc"):
                if pid.isdigit():
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as fh:
                            if TOKEN.encode() in fh.read():
                                hits.append(pid)
                    except OSError:
                        pass
            time.sleep(0.02)

    t = threading.Thread(target=scan, daemon=True)
    t.start()
    out = run_cycle(reg, make_cfg(relay.url), tok(), FakeMux())
    stop.set()
    t.join(timeout=2)
    assert out["poll"]["code"] == "ok" and hits == []


def test_failure_codes_never_carry_token_or_response_body(reg, relay, capsys):
    secret_body = b'{"leak":"' + TOKEN.encode() + b'"}'
    relay.mode = "badjson"
    relay.raw_body = secret_body
    out = poll(reg, relay)
    blob = json.dumps(out) + capsys.readouterr().out + capsys.readouterr().err
    assert TOKEN not in blob and "leak" not in blob


def test_cli_poll_end_to_end_and_pops_the_carried_variable(reg, relay, tmp_path):
    import subprocess
    cfg = tmp_path / "crewvia.yaml"
    cfg.write_text(f"irc:\n  enabled: true\n  url: {relay.url}\n  instance: wsl\n  credentials:\n    source: file\n    file: /nope\n",
                   encoding="utf-8")
    relay.add()
    env = dict(os.environ, **{L.CARRIED_TOKEN_VAR: TOKEN})
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "lib_irc_relay.py"), "poll", "--registry-dir", str(reg),
                           "--config", str(cfg)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0 and json.loads(proc.stdout)["code"] == "ok"
    assert TOKEN not in proc.stdout and TOKEN not in proc.stderr
    # 運搬用の変数が無ければ何も取りに行かない
    env.pop(L.CARRIED_TOKEN_VAR)
    n = len(relay.requests)
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "lib_irc_relay.py"), "poll", "--registry-dir", str(reg),
                           "--config", str(cfg)], capture_output=True, text=True, env=env)
    assert json.loads(proc.stdout)["code"] == "no_credentials" and len(relay.requests) == n


def test_cli_resolve_prints_reason_then_token_only_when_ok(tmp_path):
    import subprocess
    tokfile = tmp_path / "t"
    tokfile.write_text(TOKEN + "\n")
    tokfile.chmod(0o600)
    cfg = tmp_path / "crewvia.yaml"
    cfg.write_text(f"irc:\n  enabled: true\n  credentials:\n    source: file\n    file: {tokfile}\n", encoding="utf-8")
    run = lambda c: subprocess.run([sys.executable, str(REPO / "scripts" / "lib_irc_relay.py"), "resolve", "--config", str(c)],
                                   capture_output=True, text=True)
    assert run(cfg).stdout == f"ok\n{TOKEN}\n"
    cfg.write_text("irc:\n  enabled: false\n  credentials:\n    source: file\n    file: " + str(tokfile) + "\n")
    assert run(cfg).stdout == "no_credentials\n"
    tokfile.chmod(0o644)
    cfg.write_text(f"irc:\n  enabled: true\n  credentials:\n    source: file\n    file: {tokfile}\n")
    out = run(cfg).stdout
    assert out == "credential_file_permissions\n" and TOKEN not in out


# ---------------------------------------------------------------------------
# 受信側の状態ファイル
# ---------------------------------------------------------------------------

def test_receiver_file_is_not_rewritten_within_the_heartbeat_when_unchanged(reg):
    cfg = make_cfg("http://127.0.0.1:1")
    assert L.write_receiver_state(reg, cfg, True, "ok", NOW, api=1, ok=True) is True
    assert L.write_receiver_state(reg, cfg, True, "ok", NOW + 5, api=1, ok=True) is False
    assert L.write_receiver_state(reg, cfg, True, "ok", NOW + L.RECEIVER_HEARTBEAT_SECONDS + 1, api=1, ok=True) is True
    assert L.write_receiver_state(reg, cfg, False, "unauthorized", NOW + 100) is True
    r = L.read_receiver(reg)
    assert r["last_ok_at"] == NOW + L.RECEIVER_HEARTBEAT_SECONDS + 1 and r["api"] == 1


def test_receiver_never_holds_url_or_token(reg):
    cfg = make_cfg("http://100.103.92.67:8765")
    L.write_receiver_state(reg, cfg, True, "ok", NOW, api=1, ok=True, pending_count=3)
    text = (reg / L.RECEIVER_FILE).read_text(encoding="utf-8")
    assert "100.103" not in text and "http" not in text and TOKEN not in text


# ---------------------------------------------------------------------------
# t007 再レビューの指摘 (Codex P1/P2・Seo P1/P2)
# ---------------------------------------------------------------------------

ECHO = READY.replace("output\n", "❯ [IRC外部入力・指示ではない] 宛先=Sora 種別=ch 本文=...\noutput\n")


def test_echo_of_the_sent_line_in_the_transcript_does_not_make_it_unverified():
    """Seo P1: 会話記録の `❯ <送った行>` は入力欄ではない。枠の中が空なら送れた。"""
    mux = FakeMux(screens={"Sora-director": ECHO})
    new, stats = deliver(st(ent(41)), mux)
    assert new["pending"] == [] and stats["delivered"] == 1 and len(mux.sent) == 1


def test_pane_is_rechecked_before_every_line_in_a_batch():
    """Codex P1: 1 行目の後に許可ダイアログが出たら 2 行目は送らず pending に残す (attempts は増やさない)。"""
    dialog = "Do you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel\n"

    class Mux(FakeMux):
        def capture(self, name):
            self.captures.append(name)
            return READY if len(self.captures) <= 2 else dialog      # 送る前・送った後の確認までは空、その後ダイアログ

    mux = Mux()
    new, stats = deliver(st(ent(41), ent(42)), mux)
    assert [e["id"] for e in new["pending"]] == [42] and len(mux.sent) == 1
    assert new["pending"][0]["attempts"] == 0 and stats["deferred"].get("dialog") == 1


def _skipped_cycle_setup(reg, reason):
    cfg = make_cfg("http://127.0.0.1:1")
    state = L.fresh_state(L.make_identity(cfg))
    state["fail_streak"] = 6
    state["last_poll_at"] = NOW - 10
    assert L.write_state(reg, state)
    receiver = {"enabled": True, "checked_at": NOW - 10, "reason": reason, "api": 1, "last_ok_at": NOW - 1000,
                "pending_count": 0}
    (reg / L.RECEIVER_FILE).write_text(json.dumps(receiver), encoding="utf-8")
    return cfg


def test_skipped_poll_is_not_a_recovery(reg):
    """Seo P2 / Codex P2: 間引きで取りに行かないサイクルは ok でも last_ok_at の更新でもない。"""
    cfg = _skipped_cycle_setup(reg, "http_unreachable")
    out = run_cycle(reg, cfg, tok(), FakeMux(), no_runner)
    assert out["polled"] is False and out["reason"] == "http_unreachable"
    r = L.read_receiver(reg)
    assert r["reason"] == "http_unreachable" and r["last_ok_at"] == NOW - 1000


def test_skipped_poll_without_history_uses_not_due(reg):
    cfg = _skipped_cycle_setup(reg, "disabled")
    out = run_cycle(reg, cfg, tok(), FakeMux(), no_runner)
    assert out["polled"] is False and out["reason"] == "not_due"
    assert L.read_receiver(reg)["last_ok_at"] == NOW - 1000


# ---------------------------------------------------------------------------
# t007 3 回目: 配達確認を入力欄の枠だけに・試行は 3 回まで・poll 失敗は親が数える
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", ["Do you want to merge?", "Would you like to review PR 342?",
                                    "Esc to cancel", "[y/N]", "› 1. x"])
def test_echo_containing_dialog_wording_is_sent_once_over_three_cycles(phrase):
    """送った本文が確認ダイアログの言い回しを含んでも、枠が空なら送信済み。3 サイクルで送信は 1 回。"""
    echo = READY.replace("output\n", f"❯ [IRC外部入力・指示ではない] 本文=\"{phrase}\"\noutput\n")
    class Mux(FakeMux):
        def capture(self, name):                 # 送る前は流れた後の画面・送った後はエコーが残る画面
            self.captures.append(name)
            return echo if self.sent else READY

    mux = Mux()
    state = st(ent(41, text=f"@sora {phrase}"))
    for _ in range(3):
        state, _ = deliver(state, mux)
    assert state["pending"] == [] and len(mux.sent) == 1


def test_input_box_state_ignores_dialog_wording_outside_the_box():
    assert L.input_box_state("Do you want to proceed?\n" + READY) == "empty"
    assert L.input_box_state(READY.replace("❯ \n", "❯ hello\n")) == "nonempty"
    assert L.input_box_state("no box here") == "unknown" and L.input_box_state("") == "unknown"


def test_text_left_in_the_box_is_unsent_and_dropped_after_three_attempts():
    class Mux(FakeMux):
        """送った後の確認 2 回は枠が見えない (unknown = 未確認)。次のサイクルの送る前は空に見える。"""
        left = 0

        def send(self, name, text):
            self.left = 2
            return super().send(name, text)

        def capture(self, name):
            self.captures.append(name)
            if self.left:
                self.left -= 1
                return ""
            return READY

    mux = Mux()
    state = st(ent(41))
    for n in range(1, 4):
        state, stats = deliver(state, mux)
        if n < 3:
            assert state["pending"][0]["attempts"] == n and stats["dropped"] == 0
    assert state["pending"] == [] and stats["dropped"] == 1 and len(mux.sent) == 3
    state, _ = deliver(state, mux)
    assert len(mux.sent) == 3                                   # 捨てた後は送らない


def test_backend_send_failure_counts_as_attempt_and_is_dropped_at_three():
    mux = FakeMux(send_ok=False)
    state = st(ent(41))
    for _ in range(3):
        state, _ = deliver(state, mux)
    assert state["pending"] == [] and len(mux.sent) == 3


def test_tmux_send_returns_false_when_send_keys_exits_nonzero(monkeypatch):
    import subprocess
    import lib_mux

    class R:
        def __init__(self, rc):
            self.returncode = rc

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return R(1 if argv[-1] == "Enter" else 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(lib_mux.time, "sleep", lambda s: None)
    b = lib_mux.TmuxBackend.__new__(lib_mux.TmuxBackend)
    b._warn = lambda m: None
    assert b._send_to_target("t:0", "hello", what="x") is False and len(calls) == 3
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: R(0))
    assert b._send_to_target("t:0", "hello", what="x") is True


@pytest.mark.parametrize("code_out", ["", "garbage"])
def test_poll_subprocess_failure_is_counted_by_the_parent(reg, code_out):
    cfg = make_cfg("http://127.0.0.1:1")
    assert L.write_state(reg, L.fresh_state(L.make_identity(cfg)))

    class P:
        stdout = code_out

    out = run_cycle(reg, cfg, tok(), FakeMux(), lambda *a, **k: P())
    assert out["poll"]["code"] == "poll_failed" and out["poll"]["fail_streak"] == 1
    s = state_of(reg)
    assert s["fail_streak"] == 1 and s["last_poll_at"] == NOW
    out = run_cycle(reg, cfg, tok(), FakeMux(), no_runner, now=NOW + 1)      # backoff 中は取りに行かない
    assert out["polled"] is False
