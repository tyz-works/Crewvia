#!/usr/bin/env python3
"""`plan.sh needs-director` は assignment を外し、判断待ちの Worker は殺されない (t001 / backlog #13)。

## 背景

1 ミッションで Director が `queue/assignments/Kai-codex` を 15 回手で消した。

- `cmd_needs_director` は `retire_assignment()` を呼ばなかった (`cmd_done` / `cmd_fail` は呼ぶ)。
- dispatcher の codex-review spawn は `queue/assignments/Kai-codex` の **有無だけ** を見る。
  needs_director で止まった run の assignment が残ると、以後の codex-review は恒久的に spawn
  されない (task は pending のまま `plan.sh status` は ready と出す)。

単純に assignment を外すと別の事故になる (memory: assignment-removal-triggers-rule2-kill)。
dispatcher の Rule 2 は `status == 'in_progress'` の card しか「仕事」と数えないので、assignment を
失った Worker は idle でタスク無し = `no-task` / `blocked-stuck` の退役対象になり、Director の
判断を待っているだけの Worker が殺される。

## 固定すること

(a) plan.sh: needs-director が (この task を指す) assignment を外す。別 task を指す assignment は残す。
(b) dispatcher: needs_director の card を持つ Worker は退役の対象にならず、新しい task も割り当てない。
    needs_director 以外の「手放していない」status (needs_human_review / blocked / verifying) も同じ。
    card を手放した status (done / failed / cancelled) の Worker は従来どおり退役する (対照)。
(c) dispatcher: Kai-codex の assignment が終わった task / needs_director を指す孤児なら、次の
    codex-review は spawn される。走っているかもしれない run・読めない・形が違う・指す先が無い
    assignment は従来どおり塞ぐ。
(d) plan.sh → dispatcher をつないだ end-to-end: 実 plan.sh の needs-director の後で、実 dispatcher
    が次の codex-review を spawn する。

テストは本物の plan.sh と dispatcher.sh の python を使う (tests/test_dispatcher_retirement_exclusion.py と
同じ方式。ロジックの複製は本物を直しても緑のままになる)。queue / registry は使い捨て、mux はフェイク。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import lib_retirement  # noqa: E402
from test_dispatcher_retirement_exclusion import (  # noqa: E402
    AGENT, SLUG, WINDOW, FakeMux, _build_repo, _load_dispatcher,
)
from test_failed_dependency_hold import MISSION, Sandbox  # noqa: E402

CODEX = "Kai-codex"
OLD = time.time() - 10_000       # BLOCKED_STUCK_THRESHOLD (600 秒) を十分に超える


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------

def _card_text(task_id, status, worker=None, skills="code", blocked_by=(), pr=None):
    lines = [
        "---",
        f"id: {task_id}",
        f"title: task {task_id}",
        f"skills: [{skills}]",
        "priority: high",
        f"status: {status}",
        "blocked_by: [" + ", ".join(blocked_by) + "]",
        "target_dir: null",
        f"worker: {worker or 'null'}",
        "started_at: null",
        "completed_at: null",
    ]
    if pr is not None:
        lines.append(f"pr_number: {pr}")
    lines += ["---", "", "## Description", "", "fixture", "", "## Result", ""]
    return "\n".join(lines) + "\n"


def _put_card(tasks_dir, task_id, status, aged=False, **kw):
    path = tasks_dir / f"{task_id}.md"
    path.write_text(_card_text(task_id, status, **kw))
    if aged:
        os.utime(path, (OLD, OLD))
    return path


def _tasks_dir(root):
    return root / "queue" / "missions" / SLUG / "tasks"


def _assign(root, agent, value):
    (root / "queue" / "assignments" / agent).write_text(value)


def _run(root, extra_cards=(), assignments=None, *, base_status="done"):
    """`_build_repo` の pending な t001 を `base_status` に置き換え、cards / assignments を置く。"""
    tasks = _tasks_dir(root)
    _put_card(tasks, "t001", base_status, aged=True)
    for tid, status, kw in extra_cards:
        _put_card(tasks, tid, status, aged=True, **kw)
    for agent, value in (assignments or {}).items():
        _assign(root, agent, value)


def _retire_marker(root, agent=AGENT):
    return (lib_retirement.request_path(root / "registry", agent).exists()
            or lib_retirement.progress_path(root / "registry", agent).exists())


def _one_cycle(root, mux=None, popen=None):
    """本物の dispatch() を 1 回。`popen` を渡すと、dispatcher の名前空間の `subprocess.Popen`
    だけをそれに差し替える (kai-review.sh を本当に起動しない。テスト側の subprocess には触れない)。"""
    mux = mux or FakeMux([WINDOW])
    ns = _load_dispatcher(root, mux)
    if popen is not None:
        ns["subprocess"] = types.SimpleNamespace(**{**vars(subprocess), "Popen": popen})
    ns["dispatch"]()
    return mux, ns


def _assign_messages(mux):
    return [msg for target, msg in mux.sent if target == WINDOW and "plan pull" in msg]


# ---------------------------------------------------------------------------
# (a) plan.sh needs-director が assignment を外す
# ---------------------------------------------------------------------------

def _needs_director(sb, task_id, agent="Kai-codex", **env_extra):
    env = sb.env()
    env.pop("AGENT_NAME", None)
    if agent is not None:
        env["AGENT_NAME"] = agent
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(sb.root / "scripts" / "plan.sh"), "needs-director", task_id,
         "NEEDS FIX: fixture", "--mission", MISSION],
        env=env, capture_output=True, text=True)



def _assignments(sb):
    d = sb.queue / "assignments"
    d.mkdir(exist_ok=True)
    return d


def _sb_card(sb, task_id, status, **kw):
    (sb.tasks / f"{task_id}.md").write_text(_card_text(task_id, status, **kw))


def test_needs_director_removes_the_assignment_that_points_at_the_task(tmp_path):
    sb = Sandbox(tmp_path)
    _sb_card(sb, "t001", "in_progress", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")
    (_assignments(sb) / f"{CODEX}.identity").write_text(
        '{"mission": "%s", "task": "t001", "worker": "%s", "started_at": "x"}\n' % (MISSION, CODEX))

    r = _needs_director(sb, "t001")

    assert r.returncode == 0, r.stderr
    assert "status: needs_director" in (sb.tasks / "t001.md").read_text()
    assert not (_assignments(sb) / CODEX).exists(), "assignment が残っている (backlog #13 の本体)"
    assert not (_assignments(sb) / f"{CODEX}.identity").exists(), "世代のサイドカーも一緒に外れること"


def test_needs_director_keeps_an_assignment_that_points_at_another_task(tmp_path):
    """同名 Worker が別の task に就いているなら、その assignment を消してはならない。"""
    sb = Sandbox(tmp_path)
    _sb_card(sb, "t001", "in_progress", worker=CODEX, skills="codex-review")
    _sb_card(sb, "t002", "in_progress", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t002\n")

    r = _needs_director(sb, "t001")

    assert r.returncode == 0, r.stderr
    assert (_assignments(sb) / CODEX).read_text().strip() == f"{MISSION}:t002"
    assert "削除しませんでした" in r.stderr, r.stderr


def test_needs_director_without_an_agent_name_touches_no_assignment(tmp_path):
    """AGENT_NAME が無い呼び出し (Director の手動実行など) は、どの assignment も外さない。"""
    sb = Sandbox(tmp_path)
    _sb_card(sb, "t001", "in_progress", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")

    r = _needs_director(sb, "t001", agent=None)

    assert r.returncode == 0, r.stderr
    assert (_assignments(sb) / CODEX).exists()


def test_needs_director_is_fine_when_the_assignment_is_already_gone(tmp_path):
    sb = Sandbox(tmp_path)
    _sb_card(sb, "t001", "in_progress", worker=CODEX, skills="codex-review")
    r = _needs_director(sb, "t001")
    assert r.returncode == 0, r.stderr
    assert "警告" not in r.stderr and "削除しませんでした" not in r.stderr


# ---------------------------------------------------------------------------
# (b) 判断待ちの Worker は殺されず、新しい task も割り当てられない
# ---------------------------------------------------------------------------

#: card を手放していない status。assignment が無くても Worker は「仕事あり」のまま。
HELD_STATUSES = ["needs_director", "needs_human_review", "blocked", "verifying", "in_progress"]
#: card を手放した status。assignment が無く他に仕事が無ければ従来どおり退役する。
RELEASED_STATUSES = ["done", "verified", "skipped", "failed", "cancelled"]


@pytest.mark.parametrize("status", RELEASED_STATUSES)
def test_control_a_worker_that_released_its_card_is_still_retired(tmp_path, status):
    """対照: これが緑でなければ、下の「殺されない」は harness が退役を起こせないだけで緑になる。"""
    root = _build_repo(tmp_path)
    _run(root, [("t002", status, {"worker": AGENT})])

    _one_cycle(root)

    assert _retire_marker(root), (
        f"[{status}] card を手放した Worker が退役の対象にならない — harness が Rule 2 を駆動できていない")


@pytest.mark.parametrize("status", HELD_STATUSES)
def test_a_worker_holding_a_card_is_not_retired_as_no_task(tmp_path, status):
    """assignment が無くても、card の `worker` が自分で手放されていなければ退役しない。

    needs_director は assignment を外す (a) ので、この判定を card に置かないと判断待ちの
    Worker が `no-task` で殺される。
    """
    root = _build_repo(tmp_path)
    _run(root, [("t002", status, {"worker": AGENT})])

    mux, _ = _one_cycle(root)

    assert not _retire_marker(root), f"[{status}] 仕事を持つ Worker に退役が要求された"
    assert not mux.killed


@pytest.mark.parametrize("status", ["needs_human_review", "blocked", "verifying"])
def test_a_worker_holding_a_card_is_not_retired_as_blocked_stuck(tmp_path, status):
    """Rule 2 (blocked-stuck) 側: 一致する task が全部 blocked で、chain が古い。

    needs_director は is_idle の段階で止まる (下) ので、ここは Rule 2 の `has_in_progress` を
    広げた部分 (needs_director 以外の status) を直接問う。
    """
    root = _build_repo(tmp_path)
    _run(root, [
        ("t001", "pending", {"blocked_by": ["t009"]}),
        ("t009", "blocked", {"skills": "docs"}),
        ("t002", status, {"worker": AGENT}),
    ], base_status="pending")

    _one_cycle(root)

    assert not _retire_marker(root), f"[{status}] blocked-stuck で仕事を持つ Worker が退役に回された"


def test_control_blocked_stuck_retires_a_worker_without_a_card(tmp_path):
    """対照: 同じ構図で card を持たない Worker は blocked-stuck で退役する。"""
    root = _build_repo(tmp_path)
    _run(root, [
        ("t001", "pending", {"blocked_by": ["t009"]}),
        ("t009", "blocked", {"skills": "docs"}),
    ], base_status="pending")

    _one_cycle(root)

    assert _retire_marker(root), "harness が Rule 2 blocked-stuck を駆動できていない"


def test_a_worker_parked_on_needs_director_is_not_handed_a_new_task(tmp_path):
    """判断待ちの Worker は、assignment を外したあとも busy (以前は assignment が示していた)。"""
    root = _build_repo(tmp_path)
    _run(root, [
        ("t001", "pending", {}),
        ("t002", "needs_director", {"worker": AGENT}),
    ], base_status="pending")

    mux, _ = _one_cycle(root)

    assert not _assign_messages(mux), f"判断待ちの Worker に新しい task を送った: {mux.sent}"
    assert not _retire_marker(root)


def test_control_the_same_worker_without_a_held_card_is_handed_the_task(tmp_path):
    root = _build_repo(tmp_path)
    _run(root, [
        ("t001", "pending", {}),
        ("t002", "done", {"worker": AGENT}),
    ], base_status="pending")

    mux, _ = _one_cycle(root)

    assert _assign_messages(mux), f"前提: 手放した Worker には割り当てる: {mux.sent}"


# --- Rule 5: assignment が無くなっても、判断待ちの Worker に二重通知しない -----------------

class _BlockedPaneMux(FakeMux):
    def state(self, target):
        return "blocked"


def _rule5_messages(mux):
    return [msg for _t, msg in mux.sent if "[Rule 5]" in msg]


def _rule5_two_cycles(root):
    mux = _BlockedPaneMux([WINDOW, "Sora-director"])
    for _ in range(2):          # 1 回目は状態の記録だけ、2 回目に grace(0) が満ちて通知する
        ns = _load_dispatcher(root, mux)
        ns["STATE_GRACE"] = 0
        ns["dispatch"]()
    return mux


def test_rule5_does_not_double_notify_a_worker_parked_on_needs_director(tmp_path):
    """t032 F4 の維持。assignment が無い分、根拠を card に移した。"""
    root = _build_repo(tmp_path)
    _run(root, [("t002", "needs_director", {"worker": AGENT})])

    mux = _rule5_two_cycles(root)

    assert not _rule5_messages(mux), f"needs_director の Worker に Rule 5 が重ねて鳴った: {mux.sent}"


def test_control_rule5_fires_for_a_blocked_worker_on_an_in_progress_card(tmp_path):
    root = _build_repo(tmp_path)
    _run(root, [("t002", "in_progress", {"worker": AGENT})],
         assignments={AGENT: f"{SLUG}:t002\n"})

    mux = _rule5_two_cycles(root)

    assert _rule5_messages(mux), (
        f"前提: blocked な pane の Worker には Rule 5 が鳴る (harness が駆動できていない): {mux.sent}")


# ---------------------------------------------------------------------------
# (c) 孤児の Kai-codex assignment は、次の codex-review を塞がない
# ---------------------------------------------------------------------------

class _PopenSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *a, **kw):
        self.calls.append(list(cmd))
        return self


def _codex_setup(root, assignment_task_status=None, assignment_value=None):
    """pending で unblocked な codex-review task (t010) と、Kai-codex の assignment を置く。"""
    tasks = _tasks_dir(root)
    _put_card(tasks, "t001", "done")
    _put_card(tasks, "t010", "pending", skills="codex-review", pr=5)
    if assignment_task_status is not None:
        _put_card(tasks, "t011", assignment_task_status, worker=CODEX, skills="codex-review", pr=4)
    if assignment_value is not None:
        _assign(root, CODEX, assignment_value)
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "kai-review.sh").write_text("#!/bin/bash\nexit 0\n")


def _kai_spawns(spy):
    return [c for c in spy.calls if any(str(p).endswith("kai-review.sh") for p in c)]


def _spawns(root):
    spy = _PopenSpy()
    _one_cycle(root, popen=spy)
    return _kai_spawns(spy)


@pytest.mark.parametrize(
    "status", ["done", "verified", "skipped", "cancelled", "failed", "needs_director"])
def test_an_orphan_kai_codex_assignment_does_not_block_the_next_review(tmp_path, status):
    root = _build_repo(tmp_path)
    _codex_setup(root, assignment_task_status=status, assignment_value=f"{SLUG}:t011\n")

    spawned = _spawns(root)

    assert spawned, f"[{status}] 孤児の assignment が codex-review の spawn を塞いでいる"
    assert "t010" in spawned[0]
    assert (root / "queue" / "assignments" / CODEX).exists(), (
        "dispatcher は読むだけ — assignment を消すのは plan.sh の役目 (次の pull が上書きする)")


def test_control_no_assignment_spawns(tmp_path):
    root = _build_repo(tmp_path)
    _codex_setup(root)
    assert _spawns(root), "前提: assignment が無ければ spawn する"


@pytest.mark.parametrize("status", ["in_progress", "pending", "ready_for_verification", "verifying"])
def test_a_kai_codex_assignment_on_a_live_run_still_blocks(tmp_path, status):
    """走っているかもしれない run は塞ぐ (同時 1 実行)。"""
    root = _build_repo(tmp_path)
    _codex_setup(root, assignment_task_status=status, assignment_value=f"{SLUG}:t011\n")
    assert not _spawns(root), f"[{status}] 進行中の run があるのに 2 つ目を spawn した"


@pytest.mark.parametrize("value,why", [
    (f"{SLUG}:t099\n", "指す task が見つからない"),
    ("garbage\n", "<mission>:<task> の形でない"),
    (":t011\n", "mission が空"),
    (f"{SLUG}:\n", "task が空"),
], ids=["dangling", "no-colon", "empty-mission", "empty-task"])
def test_an_assignment_that_cannot_be_proven_an_orphan_still_blocks(tmp_path, value, why):
    """孤児と証明できないものは塞ぐ側に倒す (誤って 2 つ目の run を走らせるより安い)。"""
    root = _build_repo(tmp_path)
    _codex_setup(root, assignment_task_status="done", assignment_value=value)
    assert not _spawns(root), f"{why} assignment を孤児と扱って spawn した"


def test_an_unreadable_kai_codex_assignment_still_blocks(tmp_path):
    """読めない (ここでは通常ファイルでない) assignment は「観測できなかった」で、塞ぐ側に倒す。"""
    root = _build_repo(tmp_path)
    _codex_setup(root, assignment_task_status="done")
    (root / "queue" / "assignments" / CODEX).mkdir()
    assert not _spawns(root), "読めない assignment を孤児と扱って spawn した"


# ---------------------------------------------------------------------------
# (d) 実 plan.sh の needs-director → 実 dispatcher が次の codex-review を spawn する
# ---------------------------------------------------------------------------

def test_end_to_end_needs_director_then_the_next_review_spawns(tmp_path):
    """修正前は plan.sh が assignment を残し、この spawn は起きなかった (backlog #13 の再現)。"""
    sb = Sandbox(tmp_path)
    root = sb.root
    (root / "registry" / "retirements").mkdir(parents=True, exist_ok=True)
    (root / "registry" / "workers.yaml").write_text(
        f"workers:\n  - name: {AGENT}\n    skills: [code]\n    experience: 0\n")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "kai-review.sh").write_text("#!/bin/bash\nexit 0\n")
    _assignments(sb)

    _sb_card(sb, "t001", "in_progress", worker=CODEX, skills="codex-review", pr=4)
    (sb.tasks / "t002.md").write_text(_card_text("t002", "pending", skills="codex-review", pr=5))
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")

    spy = _PopenSpy()

    # 1 回目: run 中 (t001 in_progress) は塞がる
    _one_cycle(root, FakeMux([]), popen=spy)
    assert not _kai_spawns(spy)

    # kai-review.sh が findings を出して needs-director で止まる
    r = _needs_director(sb, "t001")
    assert r.returncode == 0, r.stderr

    # 2 回目: assignment は plan.sh が外している → 次の review が spawn される
    _one_cycle(root, FakeMux([]), popen=spy)
    spawned = _kai_spawns(spy)
    assert spawned and "t002" in spawned[0], f"次の codex-review が spawn されない: {spy.calls}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
