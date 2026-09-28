"""2026-09-27 の自爆の回帰テスト (t029 / backlog #32)。

あの日、ガードの変異テスト G1 (`_belongs` の頭に `return "any", True` を注入) が同 uid の
全プロセス — `systemd --user` / tmux / WSL キープアライブ / n8n (uid 1000) — を SIGKILL し、
WSL ごと落ちた。判定を壊す変異は今後も書かれる。この 1 本は「判定が壊れても自分の首は
絞めない」を仕組みとして固定する。

**本物のシグナルは 1 発も送らない**: `kill_all` の `kill=` 差し替え口に記録用の関数を渡す。
"""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

import kill_budget
import leaked_descendants as guard

pytestmark = [
    pytest.mark.skipif(not guard.available(), reason="/proc が無い環境では関門も走査も動かない"),
    pytest.mark.skipif(not guard.pidfd_supported(), reason="このカーネルは pidfd_open が使えない"),
]

#: 上限を確実に超えるための合成 pid の個数。上限ぴったり + 1 だと、たまたま実在して年齢で
#: 却下された 1 個のせいで「上限内」に落ちてしまう。余裕を持たせて環境に依存させない。
_OVER_CAP = kill_budget.DEFAULT_MAX_KILLS * 2 + 1

#: 実在しにくい高い pid 帯 (stat が読めない候補を作るのに使う)。
_SYNTHETIC_BASE = 4_000_001


def _broken_belongs(pid, marker, basetemp):
    """G1-count-everything-new 相当。判定を潰して全部「このセッションの子孫」と答える。"""
    return "any", True


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _sleepers(count):
    return [_sleeper() for _ in range(count)]


def _reap(children):
    for c in children:
        c.kill()
    for c in children:
        c.wait()


def test_broken_predicate_refuses_the_dangerous_targets(monkeypatch):
    """判定を壊すと走査は同 uid を全部拾う。それでも関門が断った pid には kill が飛ばない。

    候補に何が入るかは環境で変わる (WSL のホストでは祖先が root 所有なので候補に入らない)。
    だから前提は「断るべき対象が 1 件以上あること」に置く — それが無い環境では、守れたことの
    証明にならないので落とす。
    """
    monkeypatch.setattr(guard, "_belongs", _broken_belongs)
    result = guard.scan(set(), "marker-that-matches-nothing", "/nonexistent-basetemp")
    assert result.survivors, "壊れた判定が 1 件も拾わないなら、この検証は成立していない"

    _, refused, _ = kill_budget.partition([s.pid for s in result.survivors])
    assert refused, "断るべき対象が候補に無いと、守れたことの証明にならない"

    killed: list[int] = []
    guard.kill_all(result.survivors, kill=lambda survivor, sig: killed.append(survivor.pid))

    dangerous = {r.pid for r in refused}
    assert not (set(killed) & dangerous), f"断ったはずの pid を殺しに行った: {sorted(set(killed) & dangerous)}"
    assert not (set(killed) & kill_budget.protected()), "自分/祖先を殺しに行った"


def test_ancestors_and_self_are_refused():
    me = os.getpid()
    anc = sorted(kill_budget.ancestors(me))
    assert anc, "祖先が 1 つも見えない環境ではこのテストは無意味"
    allowed, refused, fatal = kill_budget.partition(anc + [me])
    assert allowed == [], f"祖先/自分が kill 対象に残った: {allowed}"
    assert {r.pid for r in refused} == set(anc) | {me}


def test_older_than_the_session_is_refused():
    """祖先でなくても、テストセッションより古いプロセスは子孫ではありえない。"""
    old = _sleeper()
    time.sleep(0.25)            # starttime の tick を確実にずらす (SC_CLK_TCK=100)
    young = _sleeper()
    try:
        allowed, refused, fatal = kill_budget.partition([old.pid], me=young.pid)
        assert allowed == [], "自分より古いプロセスが kill 対象に残った"
        assert refused and "古い" in refused[0].reason
    finally:
        for p in (old, young):
            p.kill()
            p.wait()


def test_a_real_young_descendant_is_still_killed(monkeypatch):
    """関門を足してもガード本来の仕事は死んでいない (守りすぎて用をなさないの防止)。

    本物のシグナルを送らないので子は死なず、`kill_all` の待ちループが `GRACE_SECONDS` を
    空回りする。その間この子を生かしたままにすると、後続のテストに無駄なプロセスの揺らぎを
    持ち込むので、待ちを短くする。

    finding 3 以降、`kill_all` は survivor に束縛済みの pidfd 経由でしか殺さないので、
    ここで手組みする `Survivor` にも本物の pidfd (`os.pidfd_open`) を持たせる —— 無いと
    「pidfd を束縛できなかった」扱いで kill 経路にすら乗らず、このテストの前提が崩れる。
    """
    monkeypatch.setattr(guard, "GRACE_SECONDS", 0.1)
    child = _sleeper()
    pidfd = os.pidfd_open(child.pid)
    try:
        allowed, refused, fatal = kill_budget.partition([child.pid])
        assert fatal is None
        assert allowed == [child.pid], f"若い子孫が断られた: {[r.describe() for r in refused]}"

        killed: list[int] = []
        report = guard.kill_all(
            [guard.Survivor(child.pid, os.getpid(), "S", 0.0, "", "sleep 30", "test", pidfd=pidfd)],
            kill=lambda survivor, sig: killed.append(survivor.pid))
        assert killed == [child.pid]
        assert report.fatal is None
        assert report.refused == [], f"pidfd を持つ若い子孫が断られた: {[r.describe() for r in report.refused]}"
    finally:
        child.kill()
        child.wait()


def test_budget_cap_refuses_everything():
    """上限を超えたら「本当に漏れた」ではなく判定の事故を疑い、1 件も殺さない。

    候補は実在して stat が読める (= 素通りすれば allowed に入りうる) 若いプロセスでなければ
    ならない。stat が読めない候補は fail-closed の対象になり、上限判定に届く前に個別の理由で
    refused になってしまうため (fail-open fix: `partition` は読めない候補を allowed にしない)、
    合成 (実在しない) pid では上限判定そのものを検証できない。
    """
    children = _sleepers(_OVER_CAP)
    try:
        allowed, refused, fatal = kill_budget.partition([c.pid for c in children])
        assert fatal is not None and "上限" in fatal, "上限を超えたのに見送っていない"
        assert allowed == [], "上限超過でも kill 対象が残った"
        assert len(refused) == len(children), "見送った件は全部 refused に出す"
    finally:
        _reap(children)


def test_budget_var_can_raise_the_cap_but_garbage_falls_back():
    """上限は環境変数で上げられる。読めない値で上限が消えてはいけない。

    (上と同じ理由で、上限判定を検証する候補は実在する若いプロセスでなければならない。)
    """
    children = _sleepers(_OVER_CAP)
    try:
        pids = [c.pid for c in children]
        _, _, fatal_raised = kill_budget.partition(pids, environ={kill_budget.BUDGET_VAR: "1000"})
        assert fatal_raised is None, "上限を上げたのに見送られた"
        _, _, fatal_garbage = kill_budget.partition(pids, environ={kill_budget.BUDGET_VAR: "yes-please"})
        assert fatal_garbage is not None, "壊れた環境変数で上限が消えてはいけない"
    finally:
        _reap(children)


def test_a_candidate_whose_stat_cannot_be_read_is_refused_not_allowed():
    """kill_budget fail-open fix: 対象の stat が読めない (= 存在しない / 消えた) candidate を allowed に入れない。

    旧実装は `got is not None and ...` の条件が False になるだけで候補が `allowed` に落ち、
    上限判定でしか止まらなかった (合成 pid が上限を超えたときだけ偶然守られていた)。
    """
    bogus = _SYNTHETIC_BASE - 1
    assert not pathlib.Path(f"/proc/{bogus}").exists(), "前提: この pid は実在しない"
    allowed, refused, fatal = kill_budget.partition([bogus])
    assert allowed == [], "stat が読めない候補が allowed に入った"
    assert fatal is None
    assert len(refused) == 1 and "読めない" in refused[0].reason, refused


def test_all_candidates_are_refused_when_the_sessions_own_start_time_is_unreadable():
    """kill_budget fail-open fix: 自分の開始時刻が読めなければ、候補がどれだけ若くて実在しても年齢を検証できない。

    検証できないことを「制限なし」に倒さず、全候補を拒否する。
    """
    child = _sleeper()
    try:
        bogus_me = _SYNTHETIC_BASE - 2
        assert not pathlib.Path(f"/proc/{bogus_me}").exists(), "前提: この pid は実在しない"
        allowed, refused, fatal = kill_budget.partition([child.pid], me=bogus_me)
        assert allowed == [], "自分の開始時刻が読めないのに候補が allowed に入った"
        assert refused and all("開始時刻" in r.reason for r in refused), refused
    finally:
        child.kill()
        child.wait()


# --- finding 3: pidfd による同一性束縛 (2 巡目 codex review) ---------------------------------


def test_open_pidfd_verified_returns_a_working_fd_when_starttime_matches():
    """本物の starttime を渡せば束縛できる (前提が壊れていないことの確認)。"""
    stat = guard._read_stat(os.getpid())
    assert stat is not None, "前提: 自分の stat が読めない"
    fd = guard._open_pidfd_verified(os.getpid(), expected_start=stat[2])
    assert fd is not None
    os.close(fd)


def test_open_pidfd_verified_rejects_a_recycled_pid(monkeypatch):
    """`pidfd_open` の直後に starttime がずれていたら (pid 再利用) 束縛を拒否する。

    実時間で本物のレース (pidfd_open してから再読みするまでの数マイクロ秒に本当に pid が
    再利用される) を再現するのは非決定的 (memory: microsecond-race-fix-needs-structural-test)。
    ここでは `_read_stat` の返り値を差し替え、「pidfd_open は成功したが直後の再読みでは
    別プロセスの starttime が見える」を構造的に模す。
    """
    real_pid = os.getpid()

    def fake_stat(pid):
        assert pid == real_pid
        return ("S", 1, 999999)   # 呼び出し元が期待する starttime とは別の値

    monkeypatch.setattr(guard, "_read_stat", fake_stat)
    fd = guard._open_pidfd_verified(real_pid, expected_start=1)
    assert fd is None, "starttime が食い違った (再利用された) のに pidfd を束縛した"


def test_open_pidfd_verified_refuses_when_pidfd_open_itself_fails(monkeypatch):
    """`pidfd_open` が失敗する (プロセス消滅等) ケースは None —— 例外を外に漏らさない。"""
    def fake_pidfd_open(pid):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(os, "pidfd_open", fake_pidfd_open)
    assert guard._open_pidfd_verified(4_100_003, expected_start=0) is None


def test_default_kill_sends_only_through_the_pidfd_never_by_bare_pid(monkeypatch):
    """本物の破壊経路 (`_default_kill`) は `signal.pidfd_send_signal` だけを使う。

    `os.kill` (pid 番号での送信) を呼んだらこのテストが失敗するようにして、フォールバック
    経路が復活していないことを構造的に固定する (finding 3)。
    """
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda fd, sig, *a, **k: calls.append((fd, sig)))

    def _forbidden(*a, **k):
        raise AssertionError("os.kill を pid 番号で直接呼んではいけない (finding 3 のフォールバック復活)")

    monkeypatch.setattr(os, "kill", _forbidden)
    survivor = guard.Survivor(4_100_004, os.getpid(), "S", 0.0, "", "x", "test", pidfd=77)
    guard._default_kill(survivor, signal.SIGKILL)
    assert calls == [(77, signal.SIGKILL)]


def test_default_kill_refuses_a_survivor_without_a_bound_pidfd():
    """防御的な最終防波堤: `pidfd is None` の survivor が `_default_kill` に来ても送らない。"""
    survivor = guard.Survivor(4_100_005, os.getpid(), "S", 0.0, "", "x", "test", pidfd=None)
    with pytest.raises(ProcessLookupError):
        guard._default_kill(survivor, signal.SIGKILL)


def test_kill_all_refuses_a_survivor_without_a_verified_pidfd(monkeypatch):
    """`kill_all` は pidfd を束縛できなかった survivor に、pid 番号でのフォールバックをしない。

    kill_budget の年齢/予算判定は通っても (`allowed` に入っても)、pidfd が無ければ kill 経路
    に乗せない —— 観測時に同一性を束縛できなかった対象は「殺せる」に倒さない (finding 3)。
    """
    monkeypatch.setattr(guard, "GRACE_SECONDS", 0.1)
    child = _sleeper()
    try:
        killed: list[int] = []
        survivor = guard.Survivor(child.pid, os.getpid(), "S", 0.0, "", "sleep 30", "test", pidfd=None)
        report = guard.kill_all([survivor], kill=lambda s, sig: killed.append(s.pid))
        assert killed == [], "pidfd が無いのに kill 経路を通した"
        assert report.killed == [], "pidfd が無い survivor が killed に入った"
        assert any("pidfd" in r.reason for r in report.refused), report.refused
    finally:
        child.kill()
        child.wait()


def test_kill_all_closes_the_pidfds_it_was_given():
    """成功・拒否のどちらでも、渡された pidfd は `kill_all` の後に必ず閉じられる (fd リーク防止)。"""
    fd = os.pidfd_open(os.getpid())   # 束縛はするが kill_budget が自分自身を必ず断る (protected)
    survivor = guard.Survivor(os.getpid(), os.getppid(), "S", 0.0, "", "self", "test", pidfd=fd)
    guard.kill_all([survivor], kill=lambda s, sig: None)
    assert survivor.pidfd is None, "kill_all の後も pidfd が開いたままになっている"
    with pytest.raises(OSError):
        os.close(fd)   # 既に閉じられているはず (二重 close は EBADF)


# --- finding 1 (3巡目 codex review): pidfd API が属性として無い環境 -----------------------
#
# `os.pidfd_open` / `signal.pidfd_send_signal` は Linux 5.3+ / Python 3.9+ でしか属性として
# 存在しない。以下は monkeypatch で「属性そのものが無い」を直接模し、**本物のシグナルは
# 送らない**まま fail closed (None / False を返すだけで例外を漏らさない) を固定する。
# 「収集の段階で pytest.mark.skipif が評価されて落ちる」方の回帰は
# `tests/red_proof_t047.sh` の case G (PID 名前空間の中で `os.pidfd_open` を実際に
# 属性として消してから pytest を起動する) が担う —— この 2 本はその補完で、通常の
# pytest 実行に混ざって毎回走る安い回帰。


def test_pidfd_supported_returns_false_without_raising_when_os_pidfd_open_is_absent(monkeypatch):
    """`os.pidfd_open` が属性として無くても `pidfd_supported()` は `False` を返すだけで済む。"""
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    assert guard.pidfd_supported() is False


def test_pidfd_supported_returns_false_without_raising_when_signal_pidfd_send_signal_is_absent(monkeypatch):
    """`signal.pidfd_send_signal` が属性として無くても `pidfd_supported()` は `False` を返すだけで済む。

    `os.pidfd_open` だけあっても、送る手段 (`signal.pidfd_send_signal`) が無ければ実際には
    殺せない —— 「使えない」の判定は両方の属性を要求する。
    """
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    assert guard.pidfd_supported() is False


def test_open_pidfd_verified_returns_none_without_raising_when_os_pidfd_open_is_absent(monkeypatch):
    """`_open_pidfd_verified` は `pidfd_supported()` を経由せず `scan()` から無条件に呼ばれる。

    `os.pidfd_open` が属性として無い環境でここが `AttributeError` を漏らすと `scan()` ごと
    落ちる (`except OSError` は `AttributeError` を捕まえない)。属性が無ければ「束縛できな
    かった」と同じ `None` を返し、fail closed のまま抜けることを固定する (族A)。
    """
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    assert guard._open_pidfd_verified(os.getpid(), expected_start=0) is None


def test_open_pidfd_verified_returns_none_without_raising_when_signal_pidfd_send_signal_is_absent(monkeypatch):
    """`os.pidfd_open` はあっても `signal.pidfd_send_signal` が属性として無い構成でも、
    `_open_pidfd_verified` は束縛せず `None` を返す (4巡目 codex review P2-2)。

    3巡目の fix は `pidfd_supported()` を両属性ゲートにしたが (finding 1)、`scan()` が実際に
    呼ぶのは `pidfd_supported()` を経由しない `_open_pidfd_verified` という**別の呼び出し
    経路**であり、そこには届いていなかった。ここでガードしないと `os.pidfd_open` だけで
    束縛に成功して `pidfd` が非 None になり、後段の `_default_kill` が
    `signal.pidfd_send_signal` を素で呼んで `AttributeError` を漏らす —— 「検出のみ」に
    倒すはずが、その例外で `kill_all` ごと止まり、残りの survivors の後片付けと pidfd の
    クローズが飛ばされる。

    `expected_start` には**本物の** starttime を渡す (`os.pidfd_open` 不在ケースの姉妹テストの
    ように `0` にしない) —— `0` だと starttime 不一致の再確認 (§ `_open_pidfd_verified` の
    pid 再利用チェック) が先に `None` を返してしまい、確かめたい signal 側のゲートを経由せず
    に「たまたま」緑になる (red proof `case J` の 1 回目の実装がまさにこれで欠陥を見逃した)。
    """
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    stat = guard._read_stat(os.getpid())
    assert stat is not None, "前提: 自分の stat が読めない"
    assert guard._open_pidfd_verified(os.getpid(), expected_start=stat[2]) is None


def test_kill_all_does_not_crash_and_refuses_when_signal_pidfd_send_signal_is_absent(monkeypatch):
    """P2-2 の結合確認: `signal.pidfd_send_signal` が無い環境でも `scan()` → `kill_all()` が
    例外を漏らさず最後まで走り、「検出のみ」(refused、pidfd 理由) になる。"""
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(guard, "GRACE_SECONDS", 0.1)
    child = _sleeper()
    try:
        stat = guard._read_stat(child.pid)
        assert stat is not None, "前提: 子の stat が読めない"
        pidfd = guard._open_pidfd_verified(child.pid, expected_start=stat[2])
        assert pidfd is None, "signal.pidfd_send_signal が無いのに束縛してしまった"
        survivor = guard.Survivor(child.pid, os.getpid(), "S", 0.0, "", "sleep 30", "test", pidfd=pidfd)
        report = guard.kill_all([survivor])   # kill= 省略 = 本物の _default_kill 経路
        assert report.fatal is None
        assert report.killed == []
        assert any("pidfd" in r.reason for r in report.refused), report.refused
    finally:
        child.kill()
        child.wait()


# --- 5巡目 codex review P2-2: シグナル送信の境界 (`_signal_one`) ---------------------------
#
# `pidfd_send_signal` は `ProcessLookupError` の他に `PermissionError` (資格情報の変化・
# セキュリティ制約) も出しうる。旧実装は `except ProcessLookupError` しか無く、それ以外の
# 例外は `kill_all` を丸ごと抜けて呼び出し元 (pytest hook) まで伝播し、残りの survivor の
# kill 試行も pidfd のクローズも飛んでいた。


def test_signal_one_returns_false_and_the_exception_for_a_non_lookup_failure():
    """`_signal_one` は `ProcessLookupError` 以外の `Exception` を握り潰さず `(False, 例外)` で返す。"""
    def _raises_permission(survivor, sig):
        raise PermissionError("simulated: signal blocked by security policy")

    survivor = guard.Survivor(4_100_010, os.getpid(), "S", 0.0, "", "x", "test", pidfd=99)
    ok, exc = guard._signal_one(_raises_permission, survivor, signal.SIGKILL)
    assert ok is False
    assert isinstance(exc, PermissionError)


def test_signal_one_treats_process_lookup_error_as_success():
    """相手が既に死んでいる (`ProcessLookupError`) のはシグナルの失敗ではない —— 成功扱い。"""
    def _raises_lookup(survivor, sig):
        raise ProcessLookupError("gone")

    survivor = guard.Survivor(4_100_011, os.getpid(), "S", 0.0, "", "x", "test", pidfd=99)
    ok, exc = guard._signal_one(_raises_lookup, survivor, signal.SIGKILL)
    assert ok is True
    assert exc is None


def test_signal_one_does_not_swallow_keyboard_interrupt():
    """境界は `Exception` だけを閉じ込める。`KeyboardInterrupt` はここで捕まらず素通りする。"""
    def _raises_kbd(survivor, sig):
        raise KeyboardInterrupt()

    survivor = guard.Survivor(4_100_012, os.getpid(), "S", 0.0, "", "x", "test", pidfd=99)
    with pytest.raises(KeyboardInterrupt):
        guard._signal_one(_raises_kbd, survivor, signal.SIGKILL)


def test_kill_all_continues_past_a_permission_error_and_closes_all_pidfds(monkeypatch):
    """P2-2 の結合確認: 1 件が `PermissionError` を出しても、残りの survivor は kill され、
    すべての pidfd が閉じられ、`kill_all` 自身は例外を漏らさず最後まで走る。"""
    monkeypatch.setattr(guard, "GRACE_SECONDS", 0.1)
    good_child = _sleeper()
    bad_child = _sleeper()
    try:
        killed: list[int] = []

        def _kill(survivor, sig):
            if survivor.pid == bad_child.pid:
                raise PermissionError("simulated")
            killed.append(survivor.pid)

        good_pidfd = os.pidfd_open(good_child.pid)
        bad_pidfd = os.pidfd_open(bad_child.pid)
        survivors = [
            guard.Survivor(good_child.pid, os.getpid(), "S", 0.0, "", "sleep 30", "test", pidfd=good_pidfd),
            guard.Survivor(bad_child.pid, os.getpid(), "S", 0.0, "", "sleep 30", "test", pidfd=bad_pidfd),
        ]
        report = guard.kill_all(survivors, kill=_kill)
        assert killed == [good_child.pid], "PermissionError の隣の survivor が処理されなかった"
        assert report.fatal is None
        assert any("送れなかった" in r.reason for r in report.refused), report.refused
        assert survivors[0].pidfd is None and survivors[1].pidfd is None, "pidfd が閉じられていない"
    finally:
        good_child.kill()
        good_child.wait()
        bad_child.kill()
        bad_child.wait()
