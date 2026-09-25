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
    PANE_EXISTS, PANE_GONE, PANE_RENAMED, PANE_UNOBSERVED, HerdrBackend,
    TmuxBackend,
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
    """`_herdr_pane_get_bound` の代わり。pane_id → 応答 で差し替える。

    本物と同じく **束縛を守る**: 問い合わせに渡された server が、いま答えている
    server (`live`) と違うなら、何も尋ねずに None を返す。
    """

    def __init__(self, answers, live=SERVER):
        self.answers = answers     # pane_id -> dict | None | callable
        self.asked = []
        self.live = live

    def __call__(self, pane_id, server, timeout=None):
        if tuple(server) != tuple(self.live):
            return None
        self.asked.append(pane_id)
        ans = self.answers[pane_id]
        return ans() if callable(ans) else ans


def _pane(label, tab="t1"):
    """`_write()` の記録 (tab_id = "t1") と同じ tab にいる pane の答え。"""
    return {"result": {"pane": {"label": label, "pane_id": "x", "tab_id": tab}}}


def _rec(pane_id="p1", tab="t1", server=SERVER):
    """`_pane_existence()` に渡す記録 (ディスクの記録と同じ形)。"""
    return {"pane_id": pane_id, "tab_id": tab, "handle": tab,
            "server": {"endpoint": server[0], "generation": server[1]}}


NOT_FOUND = {"error": {"code": "pane_not_found", "message": "pane p1 not found"}}
SERVER_DOWN = {"error": {"code": "server_not_running", "message": "no herdr server"}}


@pytest.fixture
def herdr(monkeypatch):
    def install(answers, live=SERVER):
        fake = FakeHerdr(answers, live)
        monkeypatch.setattr(lib_mux, "_herdr_pane_get_bound", fake)
        monkeypatch.setattr(lib_mux, "_herdr_server_identity", lambda: live)
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


def test_a_live_pane_under_another_label_keeps_its_record(checkout, herdr):
    """label が違っても、id が引けるなら pane は「無い」ではない (QA F1, PR #215)。

    `herdr pane rename` された生存 pane の記録を GONE と読むと、記録が消えて kill が
    `pane not found` で永久に届かず、pane が孤児として残る。
    同じ tab にいる (server の世代・pane id・tab id が記録と一致) なら RENAMED、
    tab が違う / 記録に tab が無いなら同定できないので UNOBSERVED。どちらも残す。
    """
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": _pane(_label("Someone-else"))})
    b = HerdrBackend()
    assert b._pane_existence(_rec("pA"), _label("Ren-worker")) == PANE_RENAMED
    other_tab = _pane(_label("Someone-else"), tab="t99")
    herdr({"pA": other_tab})
    assert b._pane_existence(_rec("pA"), _label("Ren-worker")) == PANE_UNOBSERVED
    assert b._pane_existence(_rec("pA", tab=""), _label("Ren-worker")) \
        == PANE_UNOBSERVED
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []
    assert _exists(checkout, "Ren-worker")


def test_a_renamed_live_pane_is_not_orphaned_by_resolution(checkout, herdr):
    """解決経路も、label 不一致で記録を消さず、記録の id で pane に届く (Kai 2巡目 P2-1)。"""
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": _pane(_label("Someone-else"))})
    b = HerdrBackend()
    b._label_lookup = lambda name: None      # rename 後は label では見つからない
    assert b._resolve_pane_id("Ren-worker") == "pA"
    ids = b._resolve_ids("Ren-worker")
    assert ids["pane_id"] == "pA" and ids["tab_id"] == "t1"
    assert _exists(checkout, "Ren-worker")


def test_a_label_that_finds_a_pane_still_wins_over_a_renamed_record(checkout,
                                                                    herdr):
    """名前 → pane の意味は変えない: label が別の pane を指すなら、その pane を返す。

    記録経由は「label で見つからないとき」だけ。kill の認可 (`may_destroy_pane`) は
    その pane の id を記録と突き合わせて食い違いを拒否する。
    """
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": _pane(_label("Someone-else"))})
    b = HerdrBackend()
    holder = {"tab_id": "t2", "pane_id": "pB", "backend": "herdr"}
    b._label_lookup = lambda name: holder
    assert b._resolve_ids("Ren-worker") == holder


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


def test_existence_is_read_in_exactly_one_place(monkeypatch):
    """判定の本体は `_pane_existence` だけ。各ケースを直に突く。"""
    b = HerdrBackend()

    def ask(answer, record=None):
        monkeypatch.setattr(lib_mux, "_herdr_pane_get_bound",
                            lambda pane_id, server, timeout=None: answer)
        return b._pane_existence(record if record is not None else _rec("p1"), "L")
    assert ask(NOT_FOUND) == PANE_GONE
    assert ask(SERVER_DOWN) == PANE_UNOBSERVED
    assert ask(None) == PANE_UNOBSERVED
    assert ask(_pane("L")) == PANE_EXISTS
    assert ask(_pane("")) == PANE_EXISTS                 # 空 label は不一致ではない
    assert ask(_pane("other")) == PANE_RENAMED           # 同じ tab で label だけ違う (F1)
    assert ask(_pane("other", tab="t9")) == PANE_UNOBSERVED   # 別の tab は同定できない
    assert ask({"result": {}}) == PANE_UNOBSERVED
    assert b._pane_existence(_rec(""), "L") == PANE_UNOBSERVED   # 空 id は「無い」の証拠ではない
    # 束縛の無い記録は、問い合わせる前に UNOBSERVED (差し替えた問い合わせは NOT_FOUND を
    # 返すので、尋ねていれば GONE になる — UNOBSERVED は「尋ねなかった」ことの証拠)
    no_server = dict(_rec("p1")); no_server.pop("server")
    assert ask(NOT_FOUND, no_server) == PANE_UNOBSERVED


# ---------------------------------------------------------------------------
# 解決経路も同じ判定を通る (受入条件 3 の実バグ)
# ---------------------------------------------------------------------------

def test_resolution_keeps_the_record_while_the_server_is_down(checkout, herdr,
                                                              monkeypatch):
    """herdr 停止中の send/capture/pid が、記録を「pane 消失」と誤読して消していた。"""
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": SERVER_DOWN})
    b = HerdrBackend()
    monkeypatch.setattr(b, "_label_lookup", lambda name: None)   # live lookup も届かない
    assert b._resolve_pane_id("Ren-worker") is None
    assert b._resolve_ids("Ren-worker") is None
    assert _exists(checkout, "Ren-worker"), \
        "server が答えなかっただけで、この checkout が pane を作った証拠が消えた"


def test_resolution_still_self_heals_on_a_definite_absence(checkout, herdr,
                                                           monkeypatch):
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": NOT_FOUND})
    b = HerdrBackend()
    monkeypatch.setattr(b, "_label_lookup", lambda name: None)
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


def test_a_record_rewritten_after_the_final_comparison_survives(checkout, herdr,
                                                                monkeypatch):
    """最終比較と unlink の**あいだ**に spawn が書き直しても、その記録は消えない。

    Kai P1 (PR #215): 比較 (`read_pane_record`) と削除 (`unlink`) が書き手とロックを
    共有していないと、比較の後で書かれた新しい記録を掃除が消す。記録は kill の唯一の
    認可なので、生きた pane の認可を掃除自身が壊すことになる。

    数マイクロ秒の窓なので実時間では再現しない。**比較が終わった直後**に、別スレッドで
    spawn (`write_pane_record`) を走らせて窓を作る。ロックがあれば書き手は掃除が終わる
    まで待たされ、あとで書く (記録が残る)。無ければ書き手はすぐ書き、掃除がそれを消す。
    """
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": NOT_FOUND})
    real_read = lib_mux.read_pane_record
    reads = []
    threads = []
    results = []

    def read_then_let_spawn_in(name, **kw):
        result = real_read(name, **kw)
        reads.append(name)
        if len(reads) == 2:     # 1 回目 = 掃除の判定用の読み / 2 回目 = 最終比較
            import threading
            t = threading.Thread(target=lambda: results.append(
                lib_mux.write_pane_record("Ren-worker", "herdr", "t2",
                                          pane_id="pB", server=SERVER,
                                          repo_root=checkout)))
            threads.append(t)
            t.start()
            t.join(timeout=0.3)   # 書き手にすぐ書けるだけの時間を与える
        return result
    monkeypatch.setattr(lib_mux, "read_pane_record", read_then_let_spawn_in)

    lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout, now=FUTURE)
    threads[0].join(timeout=10)
    assert results == [True], "書き手が記録を書けなかった"
    assert _exists(checkout, "Ren-worker"), \
        "掃除が、比較の後に書かれた spawn の記録を消した (記録は kill の唯一の認可)"
    assert real_read("Ren-worker", repo_root=checkout)["pane_id"] == "pB"


def _hold_record_lock(root):
    import fcntl
    fh = open(lib_mux.pane_record_dir(root) / lib_mux._PANE_RECORD_LOCK_NAME, "a+")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def test_the_sweep_keeps_the_record_when_a_writer_holds_the_lock(checkout, herdr,
                                                                 monkeypatch):
    """ロックを取れない掃除は消さない (取れない = 書き手を排除できない)。"""
    monkeypatch.setattr(lib_mux, "PANE_RECORD_LOCK_TIMEOUT_SECONDS", 0.1)
    _write(checkout, "Ren-worker", pane_id="pA")
    herdr({"pA": NOT_FOUND})
    fh = _hold_record_lock(checkout)
    try:
        assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                               now=FUTURE) == []
        assert _exists(checkout, "Ren-worker")
    finally:
        fh.close()
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == ["Ren-worker"]


def test_a_writer_waits_for_the_lock_and_reports_when_it_cannot_get_it(
        checkout, monkeypatch):
    """spawn の書き込みも同じロックに並ぶ。取れなければ「記録できなかった」と返す。"""
    monkeypatch.setattr(lib_mux, "PANE_RECORD_LOCK_TIMEOUT_SECONDS", 0.1)
    _write(checkout, "Ren-worker", pane_id="pA")
    fh = _hold_record_lock(checkout)
    try:
        assert lib_mux.write_pane_record("Ren-worker", "herdr", "t2", pane_id="pB",
                                         server=SERVER, repo_root=checkout) is False
    finally:
        fh.close()
    assert lib_mux.read_pane_record("Ren-worker", repo_root=checkout)["pane_id"] == "pA"


def test_the_record_lock_is_not_a_record(checkout, herdr):
    """`.records.lock` は `<name>.json` の走査に入らない。"""
    _write(checkout, "Ren-worker", pane_id="pA")
    assert (lib_mux.pane_record_dir(checkout) / lib_mux._PANE_RECORD_LOCK_NAME).exists()
    herdr({"pA": _pane(_label("Ren-worker"))})
    assert lib_mux.reap_stale_pane_records(HerdrBackend(), repo_root=checkout,
                                           now=FUTURE) == []


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


# ---------------------------------------------------------------------------
# 「記録を消せる者」は 1 人だけ (構造テスト)
#
# QA (t002) が欠陥を注入して測ったところ、旧版のこのテストは lib_mux.py 内の
# リテラル `"pane_get"` と unlink/remove/rmdir にしか効かず、`dispatcher.sh` の
# 埋め込み python に足した `os.unlink(registry/mux/*-worker.json)` (I5) や
# `shutil.move` での削除 (I4) は緑のままだった。ここでは対象を「記録に触れうる
# 全ファイル (埋め込み python を含む)」に広げ、削除系の呼び出しを allowlist 方式で
# 全部拾う: **表に無い削除が 1 つでも増えたら落ちる。**
#
# 塞げないもの (AST の限界。allowlist の理由欄にも残す):
#   * 変数・連結を経由した名前 — `op = "pane" + "_get"`、`getattr(os, "unl" + "ink")`
#   * `subprocess.run(["rm", ...])` / `os.system(...)` のような外部コマンド
#   * `open(path, "w")` での上書き (削除ではなく置換)
# これらは静的には見つけられない。レビューで見るか、red proof で実害を測る。
# ---------------------------------------------------------------------------

#: どのファイルの、どの関数の、どの呼び出しなら記録の隣で削除してよいか。
#: キーは (ファイル, 関数, 呼び出しのソース)。理由が書けない項目は足さないこと。
ALLOWED_DELETIONS = {
    ("lib_mux.py", "drop_pane_record", "path.unlink()"):
        "**記録 (registry/mux/<name>.json) を消す唯一の場所**。kill・解決経路・掃除が"
        "ここを通り、書き手と同じロックの内側で消す",
    ("dispatcher.sh", "tmux_kill_window", "firstseen.unlink(missing_ok=True)"):
        "`<name>.firstseen` (dispatcher 自身の spawn 猶予マーカー)。`<name>.json` ではない",
    ("dispatcher.sh", "sweep_spawn_grace_markers", "marker.unlink(missing_ok=True)"):
        "同上の `.firstseen` マーカーの掃除。glob は `*.firstseen` に限られる",
    ("dispatcher.sh", "set_all_done_state", "ALL_DONE_STATE_FILE.unlink()"):
        "all-done 通知の状態ファイル。registry/mux の外",
    ("lib_retirement.py", "write_json_atomic", "tmp.unlink(missing_ok=True)"):
        "temp + os.replace の失敗時に自分の temp を片付ける",
    ("lib_retirement.py", "write_json_atomic", "os.replace(tmp, path)"):
        "retirement の request/progress を書くときの atomic 置換 (registry/retirements/)",
    ("lib_retirement.py", "unlink_quiet", "Path(path).unlink(missing_ok=True)"):
        "retirement marker を消す helper。呼び出し側は下の表で個別に見る",
    ("lib_daemon_watch.py", "_remove_marker", "path.unlink()"):
        "daemons/ の pause・maintenance マーカー。registry/mux の外",
    ("lib_daemon_watch.py", "resume", "_remove_marker(path)"):
        "pause マーカー (daemons/) を解く。registry/mux の外",
    ("lib_daemon_watch.py", "_write_reports", "unlink_quiet(path)"):
        "相互監視の自己申告 ledger (`reports_path`, daemons/)。registry/mux の外",
    ("lib_retirement.py", "_check_stall", "unlink_quiet(stall_path(self.registry_dir, agent))"):
        "retirement の stall マーカー (registry/retirements/)",
    ("lib_retirement.py", "_settle_discarded", "unlink_quiet(request_path(self.registry_dir, agent))"):
        "retirement の request (registry/retirements/)",
    ("lib_retirement.py", "_settle_discarded", "unlink_quiet(progress_path(self.registry_dir, agent))"):
        "retirement の progress (registry/retirements/)",
    ("lib_retirement.py", "_settle_discarded", "unlink_quiet(stall_path(self.registry_dir, agent))"):
        "retirement の stall マーカー (registry/retirements/)",
    ("lib_retirement.py", "_settle_terminated", "unlink_quiet(request_path(self.registry_dir, agent))"):
        "retirement の request (registry/retirements/)",
    ("lib_retirement.py", "_settle_terminated", "unlink_quiet(progress_path(self.registry_dir, agent))"):
        "retirement の progress (registry/retirements/)",
    ("lib_retirement.py", "_settle_terminated", "unlink_quiet(stall_path(self.registry_dir, agent))"):
        "retirement の stall マーカー (registry/retirements/)",
    ("lib_retirement.py", "write_json_exclusive", "unlink_quiet(tmp)"):
        "排他書き込みの失敗時に自分の temp を片付ける",
}

#: 「ファイルを消す」意味の helper。中身は上の表で見るが、呼び出し側も数える。
_DELETE_HELPERS = {"unlink_quiet", "_remove_marker"}

DELETE_SCAN_FILES = ["lib_mux.py", "dispatcher.sh", "watchdog.py",
                     "lib_retirement.py", "lib_daemon_watch.py"]

_STRICT_DELETE_ATTRS = {"unlink", "rmdir", "rmtree", "removedirs"}
_MODULE_ONLY_DELETE_ATTRS = {"remove", "rename", "replace", "move"}


def _python_blocks(name):
    """`.py` はそのまま、`.sh` は埋め込まれた python (`<<'PYEOF'`) を全部返す。"""
    import re
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    if name.endswith(".py"):
        return [text]
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    assert blocks, f"{name}: 埋め込み python が見つからない (抽出の前提が変わった)"
    return blocks


def _is_deletion(call):
    """ファイルを消す / 動かして「そこから無くする」呼び出しか。

    `str.replace(a, b)` と `list.remove(x)` は名前が同じなので、モジュール
    (`os` / `shutil`) 経由か、`Path.replace(target)` のように引数が 1 つのものだけを
    削除と見なす (`str.replace` は必ず 2 引数以上)。
    """
    if isinstance(call.func, ast.Name):
        return call.func.id in _DELETE_HELPERS
    if not isinstance(call.func, ast.Attribute):
        return False
    attr, recv = call.func.attr, call.func.value
    if attr in _STRICT_DELETE_ATTRS:
        return True
    if attr in _MODULE_ONLY_DELETE_ATTRS and isinstance(recv, ast.Name) \
            and recv.id in ("os", "shutil"):
        return True
    return attr in ("replace", "rename") and len(call.args) == 1 and not call.keywords


def _deletions_in(name):
    """`[(関数, 呼び出しのソース)]` — 関数の外 (module 直下) も `<module>` として拾う。"""
    found = []
    for code in _python_blocks(name):
        tree = ast.parse(code)
        owner = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    owner[id(n)] = fn.name     # 内側の関数が後に上書きする
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and _is_deletion(n):
                found.append((owner.get(id(n), "<module>"), ast.unparse(n)))
    return found


def test_deletions_next_to_the_records_are_all_accounted_for():
    """ファイルを消す呼び出しは、理由付きの表に載っているものだけ。

    載っていない削除が 1 つでも現れたら落ちる — lib_mux.py に限らず、
    dispatcher.sh / watchdog.py の埋め込み python にも
    `os.unlink(registry/mux/*-worker.json)` を足せない (QA F2 の I5)。
    """
    seen = {}
    for name in DELETE_SCAN_FILES:
        for fn, call in _deletions_in(name):
            seen.setdefault((name, fn, call), 0)
            seen[(name, fn, call)] += 1
    unexplained = sorted(set(seen) - set(ALLOWED_DELETIONS))
    assert not unexplained, (
        "理由の書かれていない削除呼び出しがある。registry/mux/<name>.json は "
        "`drop_pane_record` (書き手と同じロック) 以外で消してはいけない。"
        f"消す対象が記録でないなら ALLOWED_DELETIONS に理由を書いて足すこと: {unexplained}")
    stale = sorted(set(ALLOWED_DELETIONS) - set(seen))
    assert not stale, f"表にあるが、もう存在しない項目 (表を直すこと): {stale}"


def test_the_record_is_unlinked_by_exactly_one_call():
    """lib_mux.py の削除は `drop_pane_record` の 1 か所だけ (`shutil.move` 等も含めて)。"""
    in_lib_mux = [(fn, c) for fn, c in _deletions_in("lib_mux.py")]
    assert in_lib_mux == [("drop_pane_record", "path.unlink()")], in_lib_mux


def test_the_deletion_detector_sees_what_it_claims_to(tmp_path, monkeypatch):
    """検出器自身の自己診断: I3 / I4 / I5 型の削除を実際に拾えること。"""
    samples = {
        "os.unlink(rec)": True,                     # I5
        "os.remove(rec)": True,
        "os.rename(rec, other)": True,
        "os.replace(rec, other)": True,
        "shutil.move(rec, other)": True,            # I4
        "shutil.rmtree(d)": True,
        "rec.unlink(missing_ok=True)": True,        # I3
        "rec.rename(other)": True,
        "rec.replace(other)": True,                 # Path.replace は 1 引数
        "unlink_quiet(rec)": True,
        "text.replace('a', 'b')": False,            # str.replace は 2 引数
        "items.remove(x)": False,                   # list.remove
        "os.path.join(a, b)": False,
    }
    for src, expected in samples.items():
        call = ast.parse(src).body[0].value
        assert _is_deletion(call) is expected, src


def test_pane_get_answers_are_read_in_one_place():
    """`pane get` の答えを「消えた」と読んでよいのは `_pane_existence` だけ。

    `state()` は agent_status を読むだけで、消えたかどうかは判定しない。

    見るのは呼び出しの形ではなく **文字列定数 `"pane_get"` の出現そのもの** (代入・
    引数・辞書のどこでも)。ただし、`"pane" + "_get"` のような連結や外部から渡された
    名前は静的に見えない — そこは塞げない (上のコメント参照)。
    """
    # 「存在」の問い合わせは 2 つの綴りで書ける: CLI 表のキー `"pane_get"` と、束縛された
    # 接続に流すソケット API の method `"pane.get"`。どちらの定数も数える。
    spellings = ("pane_get", "pane.get")
    readers = {s: set() for s in spellings}
    # `FUNCS` は関数名をキーにした dict で、同名のメソッド (各 backend の `state` など)
    # が上書きし合う。ここでは全部の関数を見る。
    every_function = [n for n in ast.walk(LIB_MUX_TREE)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in every_function:
        for n in ast.walk(fn):
            if isinstance(n, ast.Constant) and n.value in readers:
                readers[n.value].add(fn.name)
    # CLI 経由 (束縛なし) の `pane get` は `state()` の agent_status 読みだけ。
    # 「消えた」の判定に使う `_pane_existence` はもう CLI を通らない (Kai 2巡目 P2-2)。
    assert readers["pane_get"] == {"state"}, \
        f"束縛なしの pane get を読む関数が変わった: {sorted(readers['pane_get'])}"
    # 束縛された `pane.get` を送るのは 1 関数だけ。
    assert readers["pane.get"] == {"_herdr_pane_get_bound"}, \
        f"pane.get を独自に送る関数が増えた: {sorted(readers['pane.get'])}"
    # その関数を呼んでよいのは `_pane_existence` だけ (答えの読み方が 1 か所になる)。
    callers = {fn.name for fn in every_function
               for n in ast.walk(fn)
               if isinstance(n, ast.Name) and n.id == "_herdr_pane_get_bound"
               and fn.name != "_herdr_pane_get_bound"}
    assert callers == {"_pane_existence"}, \
        f"束縛付きの存在問い合わせを、別の関数が読んでいる: {sorted(callers)}"
    for name in DELETE_SCAN_FILES[1:]:      # lib_mux 以外の記録に触れうるコード
        for code in _python_blocks(name):
            assert "pane_get" not in code and "pane.get" not in code, \
                f"{name} が `pane get` を直に読んでいる — 「消えた」の判定は 1 か所に"


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
