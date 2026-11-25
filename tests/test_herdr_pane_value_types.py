"""herdr の pane_list の要素の値 (label / pane_id / tab_id) の型検査。

不変条件: 在るなら str (None / 空は「無い」として今までどおり許す)。型の違う値が 1 つでもある一覧は
「形が読めない一覧」と同じ扱い: spawn は SPAWN_UNREADABLE で hook も tab create も走らない・
_label_lookup は None・list() は [] で例外を出さない。判定は _herdr_panes() の 1 か所。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lib_mux  # noqa: E402


def _backend(monkeypatch, pane_list_result, tab_create_result=None):
    calls = []
    if tab_create_result is None:
        tab_create_result = {"result": {"tab": {"tab_id": "t1"},
                                        "root_pane": {"pane_id": "p1"}}}

    def fake_run(verb, args=None, timeout=None):
        calls.append(verb)
        if verb == "pane_list":
            return pane_list_result
        if verb == "tab_create":
            return tab_create_result
        return {"result": {}}

    monkeypatch.setattr(lib_mux, "_herdr_run", fake_run)
    b = lib_mux.HerdrBackend()
    monkeypatch.setattr(b, "_workspace_id", lambda: "ws1")
    monkeypatch.setattr(b, "_guard", lambda *a, **k: None, raising=False)
    written = []
    monkeypatch.setattr(b, "_write_cache", lambda *a, **k: written.append(a))
    ran = []
    monkeypatch.setattr(b, "_pre_launch_ok", lambda name: ran.append(1) or True)
    return b, calls, ran, written


BAD_PANES = [
    [{"label": 5, "pane_id": "p9", "tab_id": "t9"}],
    [{"label": "x", "pane_id": 7, "tab_id": "t9"}],
    [{"label": "x", "pane_id": "p9", "tab_id": ["t9"]}],
    [{"label": ["Omar-worker"], "pane_id": "p9"}],
    [{"label": {"a": 1}}],
    [{"label": "ok", "pane_id": "p1"}, {"label": 1.5, "pane_id": "p2"}],
    [{"label": True}],
]


@pytest.mark.parametrize("panes", BAD_PANES)
def test_wrong_typed_values_refuse_spawn(monkeypatch, panes):
    b, calls, ran, written = _backend(monkeypatch, {"result": {"panes": panes}})
    assert b.spawn("Omar-worker", "true") is False
    assert b.last_spawn_refusal == lib_mux.SPAWN_UNREADABLE
    assert calls == ["pane_list"]
    assert ran == [] and written == []


@pytest.mark.parametrize("panes", BAD_PANES)
def test_wrong_typed_values_do_not_break_list_or_lookup(monkeypatch, panes):
    b, *_ = _backend(monkeypatch, {"result": {"panes": panes}})
    assert b.list() == []
    assert b._label_lookup("Omar-worker") is None


@pytest.mark.parametrize("panes", [
    [{"pane_id": "p1"}],                          # label 欄なし
    [{"label": None, "pane_id": "p1"}],
    [{"label": "other", "pane_id": "p1", "tab_id": "t1"}],
])
def test_absent_or_str_values_are_still_readable(monkeypatch, panes):
    assert lib_mux._herdr_panes({"result": {"panes": panes}}) == panes


@pytest.mark.parametrize("tab_create", [
    {"result": {"tab": "x", "root_pane": {"pane_id": "p1"}}},
    {"result": {"tab": {"tab_id": "t1"}, "root_pane": ["p1"]}},
    {"result": {"tab": {"tab_id": 3}, "root_pane": {"pane_id": "p1"}}},
    {"result": {"tab": {"tab_id": "t1"}, "root_pane": {"pane_id": 5}}},
    {"result": None},
    {"result": "x"},
])
def test_tab_create_wrong_shapes_fail_without_exception(monkeypatch, tab_create):
    b, calls, ran, written = _backend(
        monkeypatch, {"result": {"panes": []}}, tab_create)
    assert b.spawn("Omar-worker", "true") is False
    assert "pane_run" not in calls and written == []
