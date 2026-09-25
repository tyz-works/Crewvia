#!/usr/bin/env python3
"""本物の `dispatch()` を 1 サイクル回して、保留の task に kickoff が飛ばないことを固定する。

QA t008 (Erik) の FAIL 観点 5 への対応 (t025)。

## 穴

`tests/test_failed_dependency_hold.py::test_dispatcher_agrees` は `dependency_gate()` —— 規則を
呼ぶだけの薄い関数 —— を直接問う。`dispatch()` がその関数を通ることは、
`test_dispatcher_uses_the_shared_rule` が `return card_dependencies(` という**文字列の存在**として
しか見ていない。だから `dispatch()` の呼び出し箇所で `verdict = dependency_gate(...)` を旧規則の
直書きに置き換える (QA の注入 D1) と、pytest 1003 passed / bats 248 ok / shell 全 rc=0 で
**全部緑のまま**、それでいて failed の依存を持つ task に kickoff が飛ぶ (元の事故そのもの)。
plan.sh 側 (task-graph / status / pull --task) に同じ種類の注入をすると赤くなるので、
穴は `dispatch()` の呼び出し箇所 1 点だった。

## 直し方

(a) 呼び出し側の**実挙動**を問う: 本物の `dispatch()` を、mux だけフェイクにして 1 サイクル回し、
    idle Worker が 1 人いる状態で「kickoff が飛んだか」を、5 者の突き合わせ表と同じ依存パターンで
    assert する。結果の上書き・迂回は、どこで起きても kickoff の有無に出る。
(b) 併せて構造を固定する: `dispatch()` が `dependency_gate` を呼び、判定の規則を直接触らず、
    `verdict` を上書きしないこと。(a) が挙動、(b) が形。(b) だけでは結果の上書きや
    迂回を検出できないので、(a) が本筋。

`tests/test_dispatcher_retirement_exclusion.py` と同じ方式 (dispatcher.sh の heredoc の python を
exec し、`lib_mux` だけフェイクに差し替える)。queue / registry は使い捨て。
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from test_dispatcher_retirement_exclusion import (  # noqa: E402
    AGENT, WINDOW, FakeMux, _load_dispatcher,
)
from test_failed_dependency_hold import (  # noqa: E402
    DOWNSTREAM, IDS, PATTERNS, Sandbox, _build,
)
from test_task_card_identity import _dispatcher_source  # noqa: E402

MISSION = "m-hold"


def _run_one_cycle(tmp_path, deps, blocked_by, released):
    """`Sandbox` に card を置き、idle Worker 1 人 + フェイク mux で本物の dispatch() を 1 回回す。"""
    sb = Sandbox(tmp_path)
    _build(sb, deps, blocked_by, released)
    return run_dispatch_cycle(sb)


def run_dispatch_cycle(sb):
    """card を置き終えた `Sandbox` で、本物の dispatch() を 1 回回す (`(mux, log)` を返す)。

    card の置き方が `_build()` に収まらないテスト (`blocked_by` の生の YAML を渡すなど、
    `tests/test_malformed_blocked_by.py`) が、同じハーネスを共有するために切り出してある。
    """
    root = sb.root
    (root / "queue" / "assignments").mkdir(exist_ok=True)
    (root / "registry" / "retirements").mkdir(parents=True, exist_ok=True)
    (root / "registry" / "workers.yaml").write_text(
        f"workers:\n  - name: {AGENT}\n    skills: [code]\n    experience: 0\n")
    # spawn 猶予を抜けさせる (猶予中は Worker が「まだ起動中」扱いになる)
    (root / "registry" / "mux" / f"{WINDOW}.firstseen").write_text("1.0")

    mux = FakeMux([WINDOW])
    ns = _load_dispatcher(root, mux)
    ns["dispatch"]()
    log_file = root / "dispatcher.log"
    return mux, (log_file.read_text() if log_file.exists() else "")


def _kickoffs_for(mux, task_id):
    return [msg for target, msg in mux.sent
            if target == WINDOW and f"タスク {task_id} " in msg and "plan pull" in msg]


# ---------------------------------------------------------------------------
# (a) 本物の dispatch() が、pull と同じ答えを出す
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,deps,blocked_by,released,expected", PATTERNS, ids=IDS)
def test_real_dispatch_cycle_sends_a_kickoff_only_for_a_ready_task(
        tmp_path, name, deps, blocked_by, released, expected):
    """expected = ready → kickoff が飛ぶ。waiting / held → 飛ばない (5 者の表と同じパターン)。

    飛んでしまうと Worker は起動し、`pull --task` の防御が拒否するまでの間 (あるいは
    拒否できない経路では) 元の事故 —— QA が FAIL した直後に review / merge が進む ——
    が起きる。
    """
    mux, log = _run_one_cycle(tmp_path, deps, blocked_by, released)
    sent = _kickoffs_for(mux, DOWNSTREAM)
    if expected == "ready":
        assert sent, (
            f"[{name}] 進めてよい task に kickoff が飛んでいない (規則が厳しすぎる / "
            f"前提のハーネスが動いていない): sent={mux.sent}\nlog:\n{log}")
    else:
        assert not sent, (
            f"[{name}] pull が拒否する task に dispatcher が kickoff を送った "
            f"({expected}): {sent}\nlog:\n{log}")


@pytest.mark.parametrize("name,deps,blocked_by,released,expected", PATTERNS, ids=IDS)
def test_real_dispatch_cycle_says_held_only_for_a_held_task(
        tmp_path, name, deps, blocked_by, released, expected):
    """保留の task だけが `[held]` (解除の案内付き) としてログに出る。"""
    _mux, log = _run_one_cycle(tmp_path, deps, blocked_by, released)
    held_lines = [ln for ln in log.splitlines()
                  if "[held]" in ln and f"task {DOWNSTREAM} " in ln]
    if expected == "held":
        assert held_lines, f"[{name}] 保留がログに出ていない:\n{log}"
        assert "release-dep" in held_lines[0] and f"--mission {MISSION}" in held_lines[0], held_lines
    else:
        assert not held_lines, f"[{name}] 保留でない task が [held] と出た: {held_lines}"


# ---------------------------------------------------------------------------
# (b) 形: dispatch() は dependency_gate を通り、規則を直接触らず、結果を上書きしない
# ---------------------------------------------------------------------------

#: 「依存が満たされた」の規則を構成する名前。dispatch() が直接参照してはならない
#: (規則は lib_dep_rules に 1 つだけ。dependency_gate() が呼ぶ)。
#: `blocked_by` は含めない: dispatch() には Rule 2 (stuck 判定の mtime と in_progress の blocker 検出)
#: が `blocked_by` を読む正当な箇所があり、それは依存の判定ではない。
RULE_NAMES = {
    "unmet_dependencies", "held_dependencies", "card_dependencies",
    "DEAD_DEP_STATUSES", "HELD_DEP_STATUSES", "released_deps",
}


def _dispatch_function():
    tree = ast.parse(_dispatcher_source())
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "dispatch"]
    assert len(funcs) == 1, "dispatch() が 1 つに定まらない"
    return funcs[0]


def _names_in(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
    return out


def test_dispatch_calls_the_gate():
    calls = [n for n in ast.walk(_dispatch_function())
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "dependency_gate"]
    assert calls, "dispatch() が dependency_gate() を呼んでいない (規則を迂回している)"


def test_dispatch_does_not_touch_the_rule_directly():
    touched = _names_in(_dispatch_function()) & RULE_NAMES
    assert not touched, (
        f"dispatch() が規則を直接触っている: {sorted(touched)} — dependency_gate() を通すこと")


def test_dispatch_does_not_overwrite_the_verdict():
    """`verdict` への代入は、`dependency_gate()` の結果を受ける 1 回だけ。
    後から `_replace()` / 再代入で書き換えると、held が消える。"""
    fn = _dispatch_function()
    assigns = [n for n in ast.walk(fn)
               if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign))
               and any(isinstance(t, ast.Name) and t.id == "verdict"
                       for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    assert len(assigns) == 1, f"verdict への代入が {len(assigns)} 回ある (1 回だけのはず)"
    value = assigns[0].value
    assert (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "dependency_gate"), (
        "verdict が dependency_gate() の結果ではない: " + ast.dump(value)[:200])
