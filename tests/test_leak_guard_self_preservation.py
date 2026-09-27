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
import subprocess
import sys
import time

import pytest

import kill_budget
import leaked_descendants as guard

pytestmark = pytest.mark.skipif(
    not guard.available(), reason="/proc が無い環境では関門も走査も動かない")

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
    guard.kill_all(result.survivors, kill=lambda pid, sig: killed.append(pid))

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
    """
    monkeypatch.setattr(guard, "GRACE_SECONDS", 0.1)
    child = _sleeper()
    try:
        allowed, refused, fatal = kill_budget.partition([child.pid])
        assert fatal is None
        assert allowed == [child.pid], f"若い子孫が断られた: {[r.describe() for r in refused]}"

        killed: list[int] = []
        report = guard.kill_all(
            [guard.Survivor(child.pid, os.getpid(), "S", 0.0, "", "sleep 30", "test")],
            kill=lambda pid, sig: killed.append(pid))
        assert killed == [child.pid]
        assert report.fatal is None
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
