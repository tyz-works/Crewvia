#!/usr/bin/env python3
"""tests/test_stale_pane_record_sweep.py

**pane が消えても `registry/mux/<name>.json` が残る** (t001, backlog #7)。

## 実測した事実 (隔離 herdr / 本番ログ)

- Worker の retirement は pane の shell pid に SIGTERM → SIGKILL を送るだけで、
  `mux.kill()` を通らない。mux は 0.2 秒以内に pane を自分で閉じるが、
  **記録を消す者がいない**。本番の失効記録 5 件 (Ren / Haruto / Wei / Seo / Arjun) は
  いずれも retire 済みの Worker だった。
- `spawn()` / `list()` は記録を見ない。だから失効記録は「起動拒否」の原因
  **ではなかった** (症状 (a) の真因は本番ログからも特定できていない)。
  記録が実害になるのは、kill の認可 (`may_destroy_pane`) と、dispatcher が読む
  `created_at` の側。

## ここで固定する契約

  A. **記録を終わらせるのは、mux が「その id は無い」と明確に答えたときだけ。**
     server 不達・timeout・読めない応答・未知のエラーは、すべて記録を残す
     (記録は `may_destroy_pane()` の唯一の証拠で、消すと次の kill が恒久拒否になる。
     knowledge/empty-vs-unobservable.md)。
     **herdr は失敗を全部 `{"error": ...}` で返す** — `server_not_running` もその形。
     「error がある = pane が無い」と読むと、herdr の停止中に呼ばれただけで記録が消える。
  B. **判定は 1 箇所** (`HerdrBackend._pane_existence` / `TmuxBackend.record_existence`)。
     解決経路 (`_resolve_*`) と掃除が同じ答えを使う。
  C. **kill の認可とは別経路**。掃除は `may_destroy_pane` を呼ばず、`may_destroy_pane` も
     掃除を呼ばない。掃除が消せるのは「消えた pane の記録」だけで、他人の pane を
     殺す認可に化けない。
  D. **`list()` は掃除しない。** `list()` は watchdog も呼ぶ。`registry/mux/` の書き手を
     増やさない (knowledge/daemon-authority.md §3 / F1)。
  E. **`spawn` の終了コードは「なぜ起動しなかったか」を運ぶ**: 10 = live なプロセスが居る、
     11 = 読めなかった (busy 扱い)、1 = それ以外 (起動が定着しなかった等)。
     「already running」と言ってよいのは 10 / 11 だけ。

  python3 -m pytest tests/test_stale_pane_record_sweep.py -v
"""

import ast
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_mux  # noqa: E402
from lib_mux import (  # noqa: E402
    PANE_EXISTS, PANE_GONE, PANE_UNOBSERVED, HerdrBackend, TmuxBackend,
)

SERVER = ("/tmp/fake-herdr.sock", "gen-1")
FUTURE = time.time() + 3600   # 記録の猶予 (120 秒) を十分に越えた「いま」


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "ours"
    (root / "registry" / "mux").mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.setattr(lib_mux, "_own_repo_root", lambda: root)
    return root


def _write(root, name, *, pane_id="p1", server=SERVER, backend="herdr"):
    assert lib_mux.write_pane_record(name, backend, "t1", pane_id=pane_id,
                                     server=server, repo_root=root)


def _exists(root, name):
    return lib_mux.pane_record_path(name, repo_root=root).exists()


class FakeHerdr:
    """`_herdr_run` の代わり。`pane_get` の答えを name → 応答 で差し替える。"""

    def __init__(self, answers):
        self.answers = answers     # pane_id -> dict | None | callable
        self.asked = []

    def __call__(self, cmd_key, extra_args, timeout=10):
        assert cmd_key == "pane_get", f"unexpected herdr call {cmd_key}"
        pane_id = extra_args[0]
        self.asked.append(pane_id)
        ans = self.answers[pane_id]
        return ans() if callable(ans) else ans


def _pane(label):
    return {"result": {"pane": {"label": label, "pane_id": "x"}}}


NOT_FOUND = {"error": {"code": "pane_not_found", "message": "pane p1 not found"}}
SERVER_DOWN = {"error": {"code": "server_not_running", "message": "no herdr server"}}


@pytest.fixture
def herdr(monkeypatch):
    def install(answers):
        fake = FakeHerdr(answers)
        monkeypatch.setattr(lib_mux, "_herdr_run", fake)
        monkeypatch.setattr(lib_mux, "_herdr_server_identity", lambda: SERVER)
        return fake
    return install


def _label(name):
    return lib_mux._pane_name(name)


# ---------------------------------------------------------------------------
# A. 消してよいのは「明確に無い」ときだけ
# ---------------------------------------------------------------------------

def test_record_of_a_vanished_pane_is_dropped(checkout, herdr):
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": NOT_FOUND})
    dropped = lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                              now=FUTURE)
    assert dropped == ["Ren-worker"]
    assert not _exists(checkout, "Ren-worker")


def test_record_of_a_live_pane_is_kept(checkout, herdr):
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": _pane(_label("Ren-worker"))})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []
    assert _exists(checkout, "Ren-worker")


def test_id_now_held_by_another_label_counts_as_gone(checkout, herdr):
    """server 再起動で id が別のタブに振り直されると、記録が指す pane は「無い」。"""
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": _pane(_label("Someone-else"))})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == ["Ren-worker"]


@pytest.mark.parametrize("answer", [
    pytest.param(SERVER_DOWN, id="server_not_running"),
    pytest.param(None, id="timeout/unparseable"),
    pytest.param({}, id="empty-body"),
    pytest.param({"error": {"code": "internal", "message": "?"}}, id="other-error"),
    pytest.param({"error": "pane_not_found"}, id="error-not-a-dict"),
    pytest.param({"unexpected": True}, id="no-result-no-error"),
    pytest.param(_pane(""), id="empty-label-is-not-a-mismatch"),
])
def test_an_answer_that_is_not_a_definite_absence_keeps_the_record(
        checkout, herdr, answer):
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": answer})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []
    assert _exists(checkout, "Ren-worker")


def test_existence_is_read_in_exactly_one_place():
    """判定の本体は `_pane_existence` だけ。各ケースを直に突く。"""
    b = HerdrBackend()
    def ask(answer, monkeypatch_target=lib_mux):
        orig = lib_mux._herdr_run
        lib_mux._herdr_run = lambda *a, **k: answer
        try:
            return b._pane_existence("p1", "L")
        finally:
            lib_mux._herdr_run = orig
    assert ask(NOT_FOUND) == PANE_GONE
    assert ask(SERVER_DOWN) == PANE_UNOBSERVED
    assert ask(None) == PANE_UNOBSERVED
    assert ask(_pane("L")) == PANE_EXISTS
    assert ask(_pane("other")) == PANE_GONE
    assert b._pane_existence("", "L") == PANE_UNOBSERVED   # 空 id は「無い」の証拠ではない


# ---------------------------------------------------------------------------
# 解決経路も同じ判定を通る (受入条件 3 の実バグ)
# ---------------------------------------------------------------------------

def test_resolution_keeps_the_record_while_the_server_is_down(checkout, herdr,
                                                              monkeypatch):
    """herdr 停止中の send/capture/pid が、記録を「pane 消失」と誤読して消していた。"""
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": SERVER_DOWN})
    b = HerdrBackend()
    monkeypatch.setattr(b, "_workspace_id", lambda: None)   # live lookup も届かない
    assert b._resolve_pane_id("Ren-worker") is None
    assert b._resolve_ids("Ren-worker") is None
    assert _exists(checkout, "Ren-worker"), \
        "server が答えなかっただけで、この checkout が pane を作った証拠が消えた"


def test_resolution_still_self_heals_on_a_definite_absence(checkout, herdr,
                                                           monkeypatch):
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": NOT_FOUND})
    b = HerdrBackend()
    monkeypatch.setattr(b, "_workspace_id", lambda: None)
    assert b._resolve_pane_id("Ren-worker") is None
    assert not _exists(checkout, "Ren-worker")


# ---------------------------------------------------------------------------
# 迷ったら残す: 自分の記録でない / 束縛が無い / 若い
# ---------------------------------------------------------------------------

def test_a_young_record_is_never_swept(checkout, herdr):
    """spawn は pane を先に作り、記録を後で書く。その隙間の pane を「無い」と見ない。"""
    _write(checkout, "Ren-worker", pane_id="pA")
    fake = herdr({"pA": NOT_FOUND})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=time.time()) == []
    assert _exists(checkout, "Ren-worker")
    assert fake.asked == [], "猶予内の記録について mux に問い合わせるべきではない"


def test_a_record_whose_age_cannot_be_read_is_kept(checkout, herdr):
    _write(checkout, "Ren-worker", pane_id="pA")
    path = lib_mux.pane_record_path("Ren-worker", repo_root=checkout)
    rec = json.loads(path.read_text())
    rec["created_at"] = "yesterday-ish"
    path.write_text(json.dumps(rec))
    herdr({"pA": NOT_FOUND})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []


@pytest.mark.parametrize("mutate,why", [
    (lambda r: r.update(checkout="/somewhere/else"), "別 checkout が書いた記録"),
    (lambda r: r.update(backend="tmux"), "別 backend の記録"),
    (lambda r: r.pop("server"), "server 束縛の無い記録"),
    (lambda r: r.update(server={"endpoint": SERVER[0], "generation": "gen-0"}),
     "別世代の server に対する記録"),
    (lambda r: r.update(server={"endpoint": "/other.sock", "generation": "gen-1"}),
     "別 endpoint の server に対する記録"),
])
def test_records_that_are_not_provably_about_this_server_are_kept(
        checkout, herdr, mutate, why):
    _write(checkout, "Ren-worker", pane_id="pA")
    path = lib_mux.pane_record_path("Ren-worker", repo_root=checkout)
    rec = json.loads(path.read_text())
    mutate(rec)
    path.write_text(json.dumps(rec))
    fake = herdr({"pA": NOT_FOUND})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == [], why
    assert _exists(checkout, "Ren-worker"), why
    assert fake.asked == [], f"{why}: 別のものについての id を mux に尋ねてはいけない"


def test_a_server_that_cannot_be_identified_keeps_everything(checkout, herdr,
                                                            monkeypatch):
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": NOT_FOUND})
    monkeypatch.setattr(lib_mux, "_herdr_server_identity", lambda: None)
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []
    assert _exists(checkout, "Ren-worker")


def test_state_and_firstseen_markers_are_not_pane_records(checkout, herdr):
    """`<name>.state.json` / `.firstseen` は dispatcher 自身の別の印。触らない。"""
    d = lib_mux.pane_record_dir(checkout)
    (d / (lib_mux._pane_name("Ren-worker") + ".state.json")).write_text("{}")
    (d / (lib_mux._pane_name("Ren-worker") + ".firstseen")).write_text("1")
    herdr({})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []
    assert len(list(d.iterdir())) == 2


def test_other_namespaces_records_are_left_alone(checkout, herdr, monkeypatch):
    """接頭辞が違う (= 別の名前空間の) 記録は、名前が似ていても対象外。"""
    _write(checkout, "Ren-worker", pane_id="pA")
    monkeypatch.setenv("CREWVIA_MUX_PANE_PREFIX", "someone-else-")
    herdr({"pA": NOT_FOUND})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []


def test_a_record_rewritten_while_the_mux_was_asked_is_not_dropped(checkout, herdr):
    """判定した記録と消す記録は同じでなければならない (spawn の書き直しを消さない)。"""
    _write(checkout, "Ren-worker", pane_id="pA")

    def respawn_meanwhile():
        _write(checkout, "Ren-worker", pane_id="pB")
        return NOT_FOUND
    herdr({"pA": respawn_meanwhile})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []
    assert lib_mux.read_pane_record("Ren-worker", repo_root=checkout)["pane_id"] == "pB"


def test_the_stop_switch_touches_nothing(checkout, herdr, monkeypatch):
    _write(checkout, "Ren-worker", pane_id="pA")
    fake = herdr({"pA": NOT_FOUND})
    monkeypatch.setenv("CREWVIA_MUX_RECORD_SWEEP", "0")
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []
    assert _exists(checkout, "Ren-worker") and fake.asked == []


def test_a_missing_or_unreadable_directory_is_not_an_error(tmp_path):
    root = tmp_path / "nowhere"
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=root,
                                           now=FUTURE) == []


# ---------------------------------------------------------------------------
# tmux
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _tmux_record(generation="900", handle="@7"):
    return {"handle": handle, "tab_id": handle, "backend": "tmux",
            "server": {"endpoint": "/tmp/tmux-x/default", "generation": generation}}


@pytest.mark.parametrize("listing,expected", [
    (_Proc(0, "900 @7\n900 @8\n"), PANE_EXISTS),
    (_Proc(0, "900 @8\n900 @9\n"), PANE_GONE),
    (_Proc(0, "901 @8\n"), PANE_UNOBSERVED),          # 別世代の server の一覧
    (_Proc(0, "900 @8\n901 @9\n"), PANE_UNOBSERVED),  # 世代が混ざる一覧は信用しない
    (_Proc(1, ""), PANE_UNOBSERVED),                  # server が居ない / 繋がらない
    (_Proc(0, ""), PANE_UNOBSERVED),                  # 空の一覧は「無い」の証拠ではない
    (_Proc(0, "watchdog\n"), PANE_UNOBSERVED),        # 形式が違う
    (_Proc(0, "#{pid} #{window_id}\n"), PANE_UNOBSERVED),   # 古い tmux は展開しない
])
def test_tmux_existence_needs_a_listing_from_the_generation_that_issued_the_id(
        monkeypatch, listing, expected):
    monkeypatch.setattr(lib_mux.subprocess, "run", lambda *a, **k: listing)
    assert TmuxBackend().record_existence("Ren-worker", _tmux_record()) == expected


def test_tmux_existence_refuses_ids_that_are_not_window_ids(monkeypatch):
    monkeypatch.setattr(lib_mux.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not ask tmux"))
    assert TmuxBackend().record_existence(
        "Ren-worker", _tmux_record(handle="not-an-id")) == PANE_UNOBSERVED
    rec = _tmux_record()
    rec.pop("server")
    assert TmuxBackend().record_existence("Ren-worker", rec) == PANE_UNOBSERVED


# ---------------------------------------------------------------------------
# C. kill の認可とは別経路 / D. list() は掃除しない
# ---------------------------------------------------------------------------

def _functions(tree):
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _names_used(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


LIB_MUX_TREE = ast.parse((SCRIPTS / "lib_mux.py").read_text(encoding="utf-8"))
FUNCS = _functions(LIB_MUX_TREE)


def test_the_sweep_and_the_kill_authorisation_do_not_call_each_other():
    sweep = _names_used(FUNCS["reap_stale_pane_records"])
    assert not sweep & {"may_destroy_pane", "pane_record_status",
                        "_refuses_foreign_daemon", "kill"}, \
        "掃除が kill の認可を借りている: 失効記録の掃除が他人の pane を殺す認可に化ける"
    for gate in ("may_destroy_pane", "pane_record_status", "_refuses_foreign_daemon"):
        assert not _names_used(FUNCS[gate]) & {"reap_stale_pane_records",
                                               "reap_stale_records"}, gate


def test_nothing_that_lists_also_sweeps():
    """`list()` は watchdog も呼ぶ。掃除を混ぜると registry/mux/ の書き手が増える。"""
    lists = [n for n in ast.walk(LIB_MUX_TREE)
             if isinstance(n, ast.FunctionDef) and n.name == "list"]
    assert len(lists) >= 3   # _Backend / TmuxBackend / HerdrBackend / Mux
    for fn in lists:
        assert not _names_used(fn) & {"reap_stale_pane_records",
                                      "reap_stale_records", "drop_pane_record"}, fn.lineno


@pytest.mark.parametrize("daemon", ["watchdog.py", "lib_retirement.py",
                                    "lib_daemon_watch.py", "verifier-dispatcher.sh"])
def test_watchdog_side_never_sweeps(daemon):
    """registry/mux/ の所有者は dispatcher (daemon-authority §3, F1)。"""
    text = (SCRIPTS / daemon).read_text(encoding="utf-8")
    assert "reap_stale" not in text and "reap-records" not in text


def test_a_record_file_is_deleted_by_one_function_only():
    """記録を unlink できるのは `drop_pane_record` だけ (掃除も kill もそこを通る)。"""
    offenders = []
    for fn_name, fn in FUNCS.items():
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("unlink", "remove", "rmdir")):
                if fn_name != "drop_pane_record":
                    offenders.append(f"{fn_name}:{n.lineno}")
    assert not offenders, f"記録の削除が drop_pane_record 以外にある: {offenders}"


def test_pane_get_answers_are_read_in_one_place():
    """`pane get` の答えを「消えた」と読んでよいのは `_pane_existence` だけ。

    `state()` は agent_status を読むだけで、消えたかどうかは判定しない。
    """
    readers = set()
    for fn_name, fn in FUNCS.items():
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_herdr_run"
                    and n.args and isinstance(n.args[0], ast.Constant)
                    and n.args[0].value == "pane_get"):
                readers.add(fn_name)
    assert "_pane_existence" in readers
    assert readers <= {"_pane_existence", "state"}, \
        f"pane get を独自に読む関数が増えた: {sorted(readers)}"


# ---------------------------------------------------------------------------
# E. spawn の拒否理由が終了コードに乗る
# ---------------------------------------------------------------------------

class FakeHerdrSpawn:
    """spawn() が使う herdr 呼び出しの最小の偽物。"""

    def __init__(self, panes, process_info):
        self.panes = panes                # list of {label, pane_id, tab_id}
        self.process_info = process_info  # dict | None
        self.calls = []

    def __call__(self, cmd_key, extra_args, timeout=10):
        self.calls.append(cmd_key)
        if cmd_key == "workspace_list":
            return {"result": {"workspaces": [
                {"label": HerdrBackend()._workspace_label(), "workspace_id": "w1"}]}}
        if cmd_key == "pane_list":
            return {"result": {"panes": self.panes}}
        if cmd_key == "pane_process_info":
            return self.process_info
        if cmd_key == "pane_run":
            return {"result": {}}
        if cmd_key == "tab_create":
            return {"result": {"tab": {"tab_id": "t9"}, "root_pane": {"pane_id": "p9"}}}
        if cmd_key == "pane_rename":
            return {"result": {}}
        raise AssertionError(cmd_key)


def _procs(*entries):
    return {"result": {"process_info": {"foreground_processes": list(entries)}}}


LIVE = _procs({"name": "claude", "argv": ["claude"]})
IDLE = _procs({"name": "bash", "argv": ["/bin/bash"]})


def _spawn_exit(monkeypatch, fake, *, wait_ok=True):
    """`lib_mux.py spawn` の CLI をそのまま通して終了コードを返す。"""
    monkeypatch.setattr(lib_mux, "_herdr_run", fake)
    monkeypatch.setattr(lib_mux, "_herdr_server_identity", lambda: SERVER)
    monkeypatch.setattr(lib_mux, "_wait_until_launched", lambda *a, **k: wait_ok)
    mux = lib_mux.Mux(backend=HerdrBackend())
    monkeypatch.setattr(lib_mux, "Mux", lambda: mux)
    return lib_mux._cli_main(["spawn", "Ren-worker", "claude"])


def _existing(label_name="Ren-worker"):
    return [{"label": lib_mux._pane_name(label_name), "pane_id": "p1", "tab_id": "t1"}]


def test_a_live_pane_is_exit_10(monkeypatch, checkout):
    assert _spawn_exit(monkeypatch, FakeHerdrSpawn(_existing(), LIVE)) == 10


def test_a_pane_that_cannot_be_read_is_exit_11(monkeypatch, checkout):
    assert _spawn_exit(monkeypatch, FakeHerdrSpawn(_existing(), None)) == 11


def test_a_relaunch_that_did_not_take_is_plain_1_not_already_running(monkeypatch,
                                                                     checkout):
    """husk への再起動が定着しなかった。名前は `list` に出続けるが、居ない。"""
    code = _spawn_exit(monkeypatch, FakeHerdrSpawn(_existing(), IDLE),
                       wait_ok=False)
    assert code == 1


def test_an_unresolvable_workspace_is_plain_1(monkeypatch, checkout):
    class NoWorkspace(FakeHerdrSpawn):
        def __call__(self, cmd_key, extra_args, timeout=10):
            if cmd_key == "workspace_list":
                return None
            return super().__call__(cmd_key, extra_args, timeout)
    assert _spawn_exit(monkeypatch, NoWorkspace(_existing(), LIVE)) == 1


def test_a_successful_spawn_is_exit_0(monkeypatch, checkout):
    assert _spawn_exit(monkeypatch, FakeHerdrSpawn([], LIVE)) == 0


def test_a_refusal_does_not_leak_into_the_next_spawn(monkeypatch, checkout):
    """同じ backend で 2 回目が別の理由で失敗したら、前回の拒否理由を引きずらない。"""
    monkeypatch.setattr(lib_mux, "_herdr_server_identity", lambda: SERVER)
    b = HerdrBackend()
    monkeypatch.setattr(lib_mux, "_herdr_run", FakeHerdrSpawn(_existing(), LIVE))
    assert b.spawn("Ren-worker", "claude") is False
    assert b.last_spawn_refusal == lib_mux.SPAWN_OCCUPIED

    class TabCreateFails(FakeHerdrSpawn):
        def __call__(self, cmd_key, extra_args, timeout=10):
            return None if cmd_key == "tab_create" else super().__call__(
                cmd_key, extra_args, timeout)
    monkeypatch.setattr(lib_mux, "_herdr_run", TabCreateFails([], LIVE))
    assert b.spawn("Ren-worker", "claude") is False
    assert b.last_spawn_refusal is None


def test_tmux_classifies_the_refusal_too(monkeypatch):
    b = TmuxBackend()
    calls = {"n": 0}

    def fake_run(argv, **kw):
        if argv[1] == "has-session":
            return _Proc(0)
        if argv[1] == "list-windows":
            return _Proc(0, lib_mux._pane_name("Ren-worker") + "\n")
        raise AssertionError(argv)
    monkeypatch.setattr(lib_mux.subprocess, "run", fake_run)
    monkeypatch.setattr(b, "_inspect_pane_full", lambda name: ("@1", 4242, SERVER))
    for state, want in ((lib_mux.PANE_LIVE, lib_mux.SPAWN_OCCUPIED),
                        (lib_mux.PANE_UNKNOWN, lib_mux.SPAWN_UNREADABLE)):
        monkeypatch.setattr(b, "_pane_process_state", lambda name, pid=None, s=state: s)
        assert b.spawn("Ren-worker", "claude") is False
        assert b.last_spawn_refusal == want


# ---------------------------------------------------------------------------
# dispatcher が毎 cycle 呼ぶ (掃除の所有者は dispatcher)
# ---------------------------------------------------------------------------

def _dispatcher_embedded():
    import re
    text = (SCRIPTS / "dispatcher.sh").read_text(encoding="utf-8")
    m = re.search(r"<<'PYEOF'\n(.*)\nPYEOF", text, re.S)
    assert m, "dispatcher.sh の埋め込み python が見つからない"
    return m.group(1), ast.parse(m.group(1))


def test_the_dispatcher_cycle_calls_the_sweep():
    src, tree = _dispatcher_embedded()
    top_calls = [n.value.func.id for n in tree.body
                 if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                 and isinstance(n.value.func, ast.Name)]
    assert "sweep_stale_pane_records" in top_calls, \
        "dispatcher の cycle が失効記録の掃除を呼んでいない"
    # 早期 return する cycle でも走るよう、dispatch() の後ろ・sweep_spawn_grace_markers の隣
    assert top_calls.index("sweep_stale_pane_records") > top_calls.index("dispatch")


def _sweep_function(mux, log):
    src, tree = _dispatcher_embedded()
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "sweep_stale_pane_records")
    ns = {"_mux": mux, "log": log}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "dispatcher-sweep", "exec"), ns)
    return ns["sweep_stale_pane_records"]


def test_the_dispatcher_sweep_logs_what_it_dropped():
    lines = []

    class M:
        def reap_stale_records(self):
            return ["Ren-worker", "Wei-worker"]
    _sweep_function(M(), lines.append)()
    assert len(lines) == 2 and "Ren-worker" in lines[0]


def test_the_dispatcher_sweep_can_never_fail_a_cycle():
    lines = []

    class Broken:
        def reap_stale_records(self):
            raise RuntimeError("mux exploded")
    _sweep_function(Broken(), lines.append)()   # 例外を出さない
    assert any("sweep failed" in l for l in lines)
