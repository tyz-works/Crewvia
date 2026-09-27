#!/usr/bin/env python3
"""PR #215 3 巡目 (t030, 最終): Kai-codex の P2 2 件。

## P2-1 — 解決経路が、判定した記録ではなく「いまの記録」を消していた

1 巡目の P1 (掃除が比較と削除を別操作で行い、その間に spawn が書いた記録を消す) と
**同じ構造がもう 1 箇所** 残っていた。`_resolve_ids()` は `PANE_GONE` の答えを受けると
素の `_delete_cache(name)` を呼んでいて、問い合わせの最中に spawn が書いた置き換えの
記録 (=生きた pane の kill の認可) を消せた。ロックは unlink を直列化するだけで、
**古い判断を新しくはできない**。

直し方: 記録を消す判断はすべて `_forget_record(name, judged)` を通り、
`drop_pane_record(name, expect=judged)` — 「ディスク上の記録が、判定したものと同じ
ときだけ」— になる。対象は解決経路と両 backend の `kill()`。`write_pane_record()` の
「サーバーが特定できなかった spawn の直前の記録を捨てる」だけは、判断ではなく spawn 自身が
名前の持ち主になった事実に基づくので無条件のまま (理由は下の表に書く)。

## P2-2 — 掃除が同期で全記録を順に問い合わせ、全体の上限が無い

応答しない herdr server (接続は受けるが返事をしない) だと、1 サイクルが
(問い合わせの timeout x 記録数) 止まる。記録は残るので毎サイクル繰り返し、13 件で
dispatcher の heartbeat しきい値 60 秒を越え、**相互監視が dispatcher を「死んだ」と判断して
respawn する**。`start.sh` も掃除の完了を待ってから spawn するので Worker の起動が遅れる。

直し方 (両方入れた。理由は §7-14):
  * sweep 全体に時間予算 (`STALE_SWEEP_BUDGET_SECONDS`)。予算を使い切ったら以降は何も
    問い合わせない。1 件ごとの問い合わせも「残り」に切り詰める。
  * 「問い合わせたが答えなかった」(`PANE_UNANSWERED`) が 1 件でも出たらその周は打ち切る。
    残りの記録も同じ理由で同じだけ待つことになるため。
記録はどちらでも残り、次の周が続きを拾う。倒す先は「失効記録が数秒長く残る」であって
「dispatch が待たされる」ではない。

## ここで固定するもの

  A. 判定した記録が置き換えられていたら、解決経路も kill も消さない (実バグの再現)
  B. 記録を消す全経路が `expect=` を通る (構造テスト。無条件に消せるのは表の 1 箇所だけ)
  C. 応答しない server がいても、sweep の 1 サイクルは予算を超えない (偽の clock。実時間に頼らない)
  D. dispatcher の 1 サイクルと `start.sh` (reap-records) が同じ上限に従う
  E. 本物の unix socket の「応答しない server」でも、`no_answer` で早く返る

  python3 -m pytest tests/test_sweep_budget_and_conditional_drop.py -v
"""

import ast
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import lib_mux  # noqa: E402
from lib_mux import (  # noqa: E402
    HerdrBackend, PANE_EXISTS, PANE_GONE, PANE_UNANSWERED, PANE_UNOBSERVED,
)
# fixtures + 実プロセスの偽 herdr (test_renamed_pane_and_bound_existence.py)
from test_renamed_pane_and_bound_existence import (  # noqa: E402,F401
    FakeHerdrServers, PANE, TAB, checkout, servers,
)
from test_stale_pane_record_sweep import _sweep_function  # noqa: E402

SERVER = ("/s", "g1")


def _record(name, *, pane_id=None, tab=None, server=SERVER):
    assert lib_mux.write_pane_record(
        name, "herdr", tab or "t-" + name, pane_id=pane_id or "p-" + name,
        server=server)


def _make_stale(name):
    """猶予 (120 秒) を過ぎた記録にする (`now` を渡せない経路 = Mux 経由のため)。"""
    path = lib_mux.pane_record_path(name)
    rec = json.loads(path.read_text())
    rec["created_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(rec))


def _exists(name):
    return lib_mux.pane_record_path(name).exists()


# ===========================================================================
# A. 判定した記録が置き換えられていたら、消さない
# ===========================================================================

def _bound_answers(monkeypatch, answer):
    monkeypatch.setattr(lib_mux, "_herdr_pane_get_bound",
                        lambda pane_id, server, timeout=None:
                        answer() if callable(answer) else answer)


NOT_FOUND = {"error": {"code": "pane_not_found", "message": "gone"}}


def test_resolution_does_not_drop_a_replacement_written_while_it_was_asking(
        checkout, monkeypatch):
    """**Kai 3巡目 P2-1 そのもの**。

    記録 (pA) について「無い」と答えが返る途中で、spawn が置き換えの記録 (pB) を書く。
    答えは pA についてのものなので、消してよいのは pA の記録だけ。pB は生きた pane の
    認可なので残らなければならない。
    """
    _record("Ren-worker", pane_id="pA", tab="tA")

    def spawn_replaces_the_record_meanwhile():
        _record("Ren-worker", pane_id="pB", tab="tB")
        return NOT_FOUND
    _bound_answers(monkeypatch, spawn_replaces_the_record_meanwhile)
    b = HerdrBackend()
    monkeypatch.setattr(b, "_label_lookup", lambda name: None)

    assert b._resolve_ids("Ren-worker") is None
    assert _exists("Ren-worker"), "解決経路が、置き換えられた新しい pane の記録を消した"
    assert lib_mux.read_pane_record("Ren-worker")["pane_id"] == "pB"


def test_resolution_still_drops_the_record_it_actually_judged(checkout, monkeypatch):
    """**可用性**: 置き換えが無ければ、これまで通り「消えた pane の記録」は消える。"""
    _record("Ren-worker", pane_id="pA", tab="tA")
    _bound_answers(monkeypatch, NOT_FOUND)
    b = HerdrBackend()
    monkeypatch.setattr(b, "_label_lookup", lambda name: None)
    assert b._resolve_ids("Ren-worker") is None
    assert not _exists("Ren-worker")


def test_herdr_kill_does_not_drop_a_replacement_written_after_the_close(
        checkout, monkeypatch):
    """kill も同じ: 閉じた pane の記録だけを消し、その間に書かれた新しい記録は消さない。"""
    _record("Ren-worker", pane_id="pA", tab="tA")
    _bound_answers(monkeypatch, {"result": {"pane": {"label": lib_mux._pane_name(
        "Ren-worker"), "pane_id": "pA", "tab_id": "tA"}}})

    def run(cmd_key, extra_args, timeout=10):
        if cmd_key == "tab_close":
            _record("Ren-worker", pane_id="pB", tab="tB")   # spawn が割り込む
        return {"result": {"type": "ok"}}
    monkeypatch.setattr(lib_mux, "_herdr_run", run)

    assert HerdrBackend().kill("Ren-worker") is True
    assert lib_mux.read_pane_record("Ren-worker")["pane_id"] == "pB", \
        "kill が、閉じた pane ではなく新しい pane の記録を消した"


def test_herdr_kill_still_drops_the_record_of_the_pane_it_closed(checkout,
                                                                 monkeypatch):
    _record("Ren-worker", pane_id="pA", tab="tA")
    _bound_answers(monkeypatch, {"result": {"pane": {"label": lib_mux._pane_name(
        "Ren-worker"), "pane_id": "pA", "tab_id": "tA"}}})
    monkeypatch.setattr(lib_mux, "_herdr_run",
                        lambda *a, **k: {"result": {"type": "ok"}})
    assert HerdrBackend().kill("Ren-worker") is True
    assert not _exists("Ren-worker")


def test_tmux_kill_does_not_drop_a_replacement_written_after_the_kill(
        checkout, monkeypatch):
    _record("Ren-worker", pane_id="", tab="@7")

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **kw):
        if "kill-window" in argv:
            _record("Ren-worker", pane_id="", tab="@9")     # spawn が割り込む
        return _Done()
    monkeypatch.setattr(lib_mux.subprocess, "run", run)

    assert lib_mux.TmuxBackend().kill("Ren-worker") is True
    assert lib_mux.read_pane_record("Ren-worker")["handle"] == "@9", \
        "tmux の kill が、新しい window の記録を消した"


# ===========================================================================
# B. 記録を消す全経路が expect= を通る (構造テスト)
# ===========================================================================

LIB_MUX_TREE = ast.parse((SCRIPTS / "lib_mux.py").read_text(encoding="utf-8"))

#: `drop_pane_record()` を呼んでよい場所と、`expect=` が要るかどうか。
#: 無条件 (expect なし) に消せるのは **1 か所だけ**で、理由を書く。
_DROP_CALLERS = {
    ("_forget_record", True):
        "判断に基づく削除 (kill / 解決経路) はすべてここを通り、判定した記録を渡す",
    ("reap_stale_pane_records", True):
        "掃除。判定した記録を渡す (1 巡目 P1)",
    ("write_pane_record", False):
        "**無条件のまま**: サーバーが特定できなかった spawn が、直前の記録を捨てる。"
        "古い観測に基づく判断ではなく、spawn 自身がこの名前の持ち主になった事実による",
}


def _calls_to(name):
    out = []
    for fn in ast.walk(LIB_MUX_TREE):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == name:
                out.append((fn.name, n))
    return out


def test_every_path_that_drops_a_record_goes_through_expect():
    seen = set()
    for fn, call in _calls_to("drop_pane_record"):
        has_expect = any(k.arg == "expect" for k in call.keywords)
        key = (fn, has_expect)
        assert key in _DROP_CALLERS, (
            f"`{fn}` が drop_pane_record を "
            f"{'expect= 付きで' if has_expect else '無条件に'} 呼んでいる。"
            f"記録を消す判断は `_forget_record(name, judged)` を通すこと "
            f"(無条件に消してよい場合は _DROP_CALLERS に理由を書く)")
        seen.add(key)
    assert seen == set(_DROP_CALLERS), \
        f"表にあるが存在しない項目: {sorted(set(_DROP_CALLERS) - seen)}"
    # 無条件は表の 1 か所だけ。
    assert [k for k in seen if not k[1]] == [("write_pane_record", False)]


def test_the_kills_and_the_resolution_path_use_forget_record_with_what_they_judged():
    """`_forget_record` を呼ぶ場所は 3 か所: 両 backend の kill と `_resolve_ids`。

    どれも第 2 引数 (判定した記録) を渡す。`_delete_cache(name)` のような
    「名前だけで消す」入口は残っていない。
    """
    callers = {}
    for fn, call in _calls_to("_forget_record") + [
            (fn.name, n) for fn in ast.walk(LIB_MUX_TREE)
            if isinstance(fn, ast.FunctionDef)
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_forget_record"]:
        assert len(call.args) + len(call.keywords) == 2, \
            f"{fn}: _forget_record は判定した記録を渡す形でしか呼べない"
        callers.setdefault(fn, 0)
        callers[fn] += 1
    assert set(callers) == {"kill", "_resolve_ids"}, sorted(callers)
    assert callers["kill"] == 4 and callers["_resolve_ids"] == 1
    source = (SCRIPTS / "lib_mux.py").read_text(encoding="utf-8")
    assert "_delete_cache" not in source, \
        "名前だけで記録を消す入口 (_delete_cache) が残っている"


# ===========================================================================
# C. 応答しない server がいても、sweep は予算を超えない
# ===========================================================================

class Clock:
    """偽の monotonic clock。スタブの「待ち」がここを進める (実時間は動かない)。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class StubMux(lib_mux._Backend):
    """問い合わせに `cost` 秒かかる mux。`hang=True` なら、渡された timeout を丸ごと
    使い切って何も答えない (= 接続は受けるが返事をしない server)。"""

    BACKEND_NAME = "herdr"

    def __init__(self, clock, *, hang=False, cost=0.0, answer=PANE_EXISTS,
                 identity_cost=0.0):
        self.clock, self.hang, self.cost = clock, hang, cost
        self.answer, self.identity_cost = answer, identity_cost
        self.queries = []           # 問い合わせに渡された timeout

    def server_identity(self):
        self.clock.advance(self.identity_cost)
        return SERVER

    def record_existence(self, name, record, timeout=None):
        self.queries.append((name, timeout))
        if self.hang:
            self.clock.advance(timeout)
            return PANE_UNANSWERED
        self.clock.advance(min(self.cost, timeout))
        return self.answer


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(lib_mux, "_sweep_clock", c)
    return c


def _thirteen_stale_records():
    names = [f"W{i:02d}-worker" for i in range(13)]     # 本番には 10 件以上ある
    for n in names:
        _record(n)
        _make_stale(n)
    return names


def test_a_server_that_never_answers_costs_one_query_not_thirteen(checkout, clock):
    """**Kai 3巡目 P2-2 の実数**: 13 記録 x 応答しない server。

    旧実装は (timeout x 13) を毎サイクル待った。最初の「答えなかった」で打ち切る。
    """
    names = _thirteen_stale_records()
    mux = StubMux(clock, hang=True)

    dropped = lib_mux.reap_stale_pane_records(mux)

    assert dropped == []
    assert len(mux.queries) == 1, \
        f"応答しない server に {len(mux.queries)} 回問い合わせた (1 回で打ち切るはず)"
    elapsed = clock.t - 1000.0
    assert elapsed <= lib_mux.STALE_SWEEP_QUERY_TIMEOUT_SECONDS
    assert elapsed <= lib_mux.STALE_SWEEP_BUDGET_SECONDS
    assert all(_exists(n) for n in names), "打ち切っても記録は 1 件も消えない"


def test_a_slow_server_is_cut_off_by_the_budget_even_when_it_does_answer(
        checkout, clock):
    """答えは返すが遅い server: 「答えなかった」では打ち切れないので、予算が止める。

    1 件 1.5 秒 x 13 = 19.5 秒かかるところを、予算 (5 秒) で止める。
    """
    names = _thirteen_stale_records()
    mux = StubMux(clock, cost=1.5)

    lib_mux.reap_stale_pane_records(mux)

    elapsed = clock.t - 1000.0
    assert elapsed <= lib_mux.STALE_SWEEP_BUDGET_SECONDS, \
        f"sweep が予算 ({lib_mux.STALE_SWEEP_BUDGET_SECONDS}s) を超えた: {elapsed}s"
    assert 0 < len(mux.queries) < len(names), \
        f"予算で止まっていない (問い合わせ {len(mux.queries)}/{len(names)})"
    # 各問い合わせは 1 件の上限を越えず、最後の 1 件は「残り」に切り詰められている
    # (1.5 秒 x 3 = 4.5 秒の後に残るのは 0.5 秒。4 件目はそれだけしか待たない)。
    assert all(t <= lib_mux.STALE_SWEEP_QUERY_TIMEOUT_SECONDS for _, t in mux.queries)
    assert mux.queries[-1][1] == pytest.approx(0.5)
    assert all(_exists(n) for n in names)


def test_the_budget_stops_the_sweep_before_the_next_query_even_if_the_filter_used_it_up(
        checkout, clock):
    """`server_identity()` (問い合わせの前の絞り込み) が予算を使い切っても、
    そのあと mux には何も尋ねない。"""
    _thirteen_stale_records()
    mux = StubMux(clock, identity_cost=lib_mux.STALE_SWEEP_BUDGET_SECONDS + 1)

    lib_mux.reap_stale_pane_records(mux)

    assert mux.queries == [], "予算が尽きたのに mux に問い合わせた"


def test_the_sweep_finishes_over_several_cycles_and_only_ends_gone_records(
        checkout, clock):
    """予算で止めても、次の周が続きを拾う (= 失効記録が数秒長く残るだけ)。"""
    names = _thirteen_stale_records()
    cycles, dropped_total = 0, []
    while cycles < 20:
        cycles += 1
        mux = StubMux(clock, cost=1.5, answer=PANE_GONE)
        before = clock.t
        dropped_total += lib_mux.reap_stale_pane_records(mux)
        assert clock.t - before <= lib_mux.STALE_SWEEP_BUDGET_SECONDS
        if not any(_exists(n) for n in names):
            break
    assert sorted(dropped_total) == sorted(names)
    assert 1 < cycles <= 20, "1 周で終わった (予算が効いていない) か、終わらなかった"


def test_only_the_answered_gone_records_are_dropped_when_the_server_stops_answering(
        checkout, clock):
    """途中から応答しなくなった server: それまでに「無い」と答えた分だけを消す。"""
    names = _thirteen_stale_records()

    class GoneThenSilent(StubMux):
        def record_existence(self, name, record, timeout=None):
            if len(self.queries) < 3:
                self.queries.append((name, timeout))
                return PANE_GONE
            return super().record_existence(name, record, timeout)
    mux = GoneThenSilent(clock, hang=True)

    dropped = lib_mux.reap_stale_pane_records(mux)

    assert len(dropped) == 3
    assert sum(_exists(n) for n in names) == 10


# ===========================================================================
# D. dispatcher の 1 サイクルと start.sh (reap-records) が同じ上限に従う
# ===========================================================================

#: 応答しない server (最初の「答えなかった」で止まる) と、答えは返すが遅い server
#: (予算が止める)。dispatcher と start.sh はどちらの経路でも同じ上限に従う。
_SILENT_OR_SLOW = [
    pytest.param(dict(hang=True), id="never-answers"),
    pytest.param(dict(cost=1.5), id="answers-slowly"),
]


@pytest.mark.parametrize("stub", _SILENT_OR_SLOW)
def test_a_dispatcher_cycle_stays_inside_the_budget_with_a_silent_server(
        checkout, clock, stub):
    """**受入条件 3**: 応答しない (遅い) server がいても、dispatcher の 1 サイクルの掃除は
    時間予算を超えない (heartbeat の 60 秒に対して)。

    dispatcher.sh の埋め込み関数 `sweep_stale_pane_records()` を、そのまま (実コード) 動かす。
    """
    _thirteen_stale_records()
    mux = lib_mux.Mux(backend=StubMux(clock, **stub))
    lines = []

    _sweep_function(mux, lines.append)()

    elapsed = clock.t - 1000.0
    assert elapsed <= lib_mux.STALE_SWEEP_BUDGET_SECONDS
    assert elapsed < 60, "dispatcher の heartbeat しきい値 (60 秒) に近づいた"
    assert lines == [], "打ち切っても、消していない記録を「消した」と記録してはいけない"


@pytest.mark.parametrize("stub", _SILENT_OR_SLOW)
def test_start_sh_waits_for_the_same_bounded_sweep(checkout, clock, stub):
    """`start.sh` の `mux_reap_records` は `lib_mux.py reap-records` を呼ぶ。同じ関数・同じ予算。"""
    _thirteen_stale_records()
    m = lib_mux.Mux(backend=StubMux(clock, **stub))

    assert m.reap_stale_records() == []

    assert clock.t - 1000.0 <= lib_mux.STALE_SWEEP_BUDGET_SECONDS
    # 配線: CLI の reap-records は Mux.reap_stale_records() を呼ぶだけで、独自の掃除を持たない。
    cli = next(n for n in ast.walk(LIB_MUX_TREE)
               if isinstance(n, ast.FunctionDef) and n.name == "_cli_main")
    verbs = [n for n in ast.walk(cli) if isinstance(n, ast.If)
             and "reap-records" in ast.unparse(n.test)]
    assert verbs and "reap_stale_records" in ast.unparse(verbs[0])
    assert "reap_stale_pane_records" not in ast.unparse(verbs[0])
    lib_sh = (SCRIPTS / "lib_mux.sh").read_text(encoding="utf-8")
    assert 'python3 "$_LIB_MUX_PY" reap-records' in lib_sh


# ===========================================================================
# E. 本物の unix socket の「応答しない server」
# ===========================================================================

_SILENT_HERDR = r'''
import os, socket, sys, time
sock_path = sys.argv[1]
try:
    os.unlink(sock_path)
except FileNotFoundError:
    pass
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(8)
sys.stderr.write("ready\n"); sys.stderr.flush()
held = []
while True:
    conn, _ = srv.accept()
    held.append(conn)       # 受けるが、何も返さない (閉じもしない)
'''


def test_a_real_silent_server_is_reported_as_unanswered_quickly(checkout,
                                                                monkeypatch,
                                                                tmp_path):
    """接続は受けるが返事をしない実 server。`no_answer` -> `PANE_UNANSWERED` で早く返る。"""
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="t30-silent-", dir="/tmp"))
    script = root / "silent.py"
    script.write_text(_SILENT_HERDR, encoding="utf-8")
    sock_path = root / "herdr.sock"
    proc = subprocess.Popen([sys.executable, str(script), str(sock_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(str(sock_path))
                break
            except OSError:
                time.sleep(0.05)
        monkeypatch.setattr(lib_mux, "_HERDR_SOCK_PATH", sock_path)
        identity = lib_mux._herdr_server_identity()
        assert identity is not None
        record = {"pane_id": "wP:p1", "tab_id": "wP:t1", "handle": "wP:t1",
                  "server": {"endpoint": identity[0], "generation": identity[1]}}

        started = time.monotonic()
        answer = lib_mux._herdr_pane_get_bound("wP:p1", identity, timeout=0.3)
        assert answer["error"]["code"] == "no_answer"
        assert HerdrBackend()._pane_existence(record, "L", timeout=0.3) \
            == PANE_UNANSWERED
        assert time.monotonic() - started < 3, "応答しない server を待ちすぎた"
    finally:
        proc.kill()
        proc.wait(timeout=10)
        for f in root.iterdir():
            f.unlink()
        root.rmdir()


def test_a_silent_answer_is_never_an_absence(checkout, monkeypatch):
    """`no_answer` は「無い」ではない: 記録は残る (解決経路でも)。"""
    _record("Ren-worker", pane_id="pA", tab="tA")
    _bound_answers(monkeypatch, {"error": {"code": "no_answer"}})
    b = HerdrBackend()
    monkeypatch.setattr(b, "_label_lookup", lambda name: None)
    assert b._pane_existence(lib_mux.read_pane_record("Ren-worker"), "L") \
        == PANE_UNANSWERED
    assert b._resolve_ids("Ren-worker") is None
    assert _exists("Ren-worker")
