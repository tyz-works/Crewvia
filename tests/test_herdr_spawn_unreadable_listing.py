"""herdr の spawn: pane_list が読めない (None) ときは spawn しない (SPAWN_UNREADABLE)。

占有が観測できないのに tab create に進むと、pre-launch hook (model.json の書き込み) が
生きた Worker の記録を書き換えうる。他の backend (Rex / hc) と同じく fail-closed にする。
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lib_mux  # noqa: E402


def _backend(monkeypatch, pane_list_result):
    calls = []

    def fake_run(verb, args=None, timeout=None):
        calls.append(verb)
        if verb == "pane_list":
            return pane_list_result
        return {"result": {"tab": {"tab_id": "t1"}, "root_pane": {"pane_id": "p1"}}}

    monkeypatch.setattr(lib_mux, "_herdr_run", fake_run)
    b = lib_mux.HerdrBackend()
    monkeypatch.setattr(b, "_workspace_id", lambda: "ws1")
    monkeypatch.setattr(b, "_guard", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(b, "_write_cache", lambda *a, **k: None)
    ran = []
    b.pre_launch = lambda: ran.append(1) or True
    return b, calls, ran


def test_unreadable_pane_list_refuses_before_hook_and_tab_create(monkeypatch):
    b, calls, ran = _backend(monkeypatch, None)
    assert b.spawn("Omar-worker", "true") is False
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    assert calls == ["pane_list"]          # 呼ばれたのは一覧だけ (これが空振りでない証拠)
    assert ran == []


def test_readable_empty_pane_list_still_spawns(monkeypatch):
    b, calls, ran = _backend(monkeypatch, {"result": {"panes": []}})
    assert b.spawn("Omar-worker", "true") is True
    assert "tab_create" in calls and ran == [1]


import pytest  # noqa: E402


@pytest.mark.parametrize("resp", [
    {"error": {"code": "workspace_not_found", "message": "x"}},   # エラー応答 (非 0 でも JSON が返る)
    {},                                                            # result 無し
    {"result": {}},                                                # panes 無し
    {"result": {"panes": None}},                                   # list でない
    {"result": {"panes": {"a": 1}}},
])
def test_error_shaped_pane_list_refuses_like_unreadable(monkeypatch, resp):
    b, calls, ran = _backend(monkeypatch, resp)
    assert b.spawn("Omar-worker", "true") is False
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    assert calls == ["pane_list"]
    assert ran == []


@pytest.mark.parametrize("panes", [
    ["Omar-worker"],                                  # 文字列の要素
    [None],                                           # None の要素
    [["label", "Omar-worker"]],                       # list の要素
    [{"label": "other", "pane_id": "p9"}, "x"],       # 読める要素に壊れた要素が混ざる
])
def test_non_dict_pane_elements_refuse_like_unreadable(monkeypatch, panes):
    """要素が dict でない一覧は『占有が観測できない』: 例外で抜けず、hook も tab create も走らない。"""
    b, calls, ran = _backend(monkeypatch, {"result": {"panes": panes}})
    assert b.spawn("Omar-worker", "true") is False
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    assert calls == ["pane_list"]
    assert ran == []


@pytest.mark.parametrize("panes", [["x"], [None], [["a"]]])
def test_label_lookup_and_list_do_not_raise_on_non_dict_elements(monkeypatch, panes):
    b, calls, ran = _backend(monkeypatch, {"result": {"panes": panes}})
    assert b._label_lookup("Omar-worker") is None      # 見つからない = kill の根拠にならない側
    assert b.list() == []                              # list() に『読めない』の口は無い。spawn が止める
