#!/usr/bin/env python3
"""`plan.sh reap-orphan-assignment` — Kai-codex の孤児 assignment を掃除する (t009 / backlog #34)。

## 背景

PR1 (#225 / t001) 以後、`codex_review_slot_busy()` は終了済み task / needs_director を
指す `queue/assignments/Kai-codex` を**読むだけ**で「塞がない」に倒す (孤児は次の
codex-review の spawn を止めない)。だがファイルそのものは誰も消さない —— 次の
`plan.sh pull` (= 次の codex-review) が同じ agent 名で assignment を上書きすれば
自然に消えるが、そのミッションにもう codex-review task が無ければ、孤児は
`queue/assignments/Kai-codex` に恒久的に残る。「消すのは plan.sh の役目」
(PR1 の設計、`agent_busy_elsewhere()` の docstring 参照)。

## 孤児ができる経路 (plan.sh Result 参照)

1. kai-review.sh が `plan.sh done` / `needs-director` のどちらも呼ばずに終わる
   (プロセスが外部から kill される・未捕捉の異常終了)。
2. Director が `plan.sh update <id> --status <終端 status>` のように **`--reset` を
   経ない**経路で card を終端 status に動かす (`--reset` だけが assignment を撤去する。
   `cmd_update` 参照)。

## このファイルが固定すること

(a) `plan.sh reap-orphan-assignment <agent>`: 読めて・「終了した」(TERMINAL_STATUSES ∪
    DEAD_DEP_STATUSES ∪ HELD_DEP_STATUSES) task を指す assignment だけを撤去する
    (identity サイドカーも一緒に)。
(b) 消さない (1 バイトも書かず exit 3 = PRECONDITION_UNMET): 読めない / 形が違う /
    task が見つからない / 進行中を指す / **needs_director を指す** (正常経路では
    needs_director への遷移そのものが assignment を撤去するので、それでも残っている
    のは証拠不足 — 破壊的な掃除の対象にしない。`codex_review_slot_busy()` の
    read-only な「塞がない」判定とは意図的に非対称)。
(c) 既に無い assignment は exit 0 の no-op。
(d) `--no-wait` はキューロックを待たずに諦め exit 4 (LOCK_BUSY)、何も書かない。
(e) dispatcher.sh: 孤児候補が無いサイクルでは `plan.sh` を 1 本も起動しない
    (`_kai_codex_orphan_candidate()`)。needs_director はここでも候補から除外する
    (plan.sh 側がどうせ拒否するので subprocess を無駄に起動しない)。
(f) dispatcher.sh → 実 plan.sh を通した end-to-end: 孤児を撤去した**同じサイクル**で
    次の codex-review が spawn される (`codex_review_slot_busy()` はもともと孤児を
    塞がないので、掃除がなくても spawn 自体はできていたが、掃除が spawn を
    邪魔しないことをここで確認する)。

テストは本物の plan.sh (`tests/fixture_tree.copy_plan_tree` で隔離コピー) と、本物の
dispatcher.sh (heredoc の python を exec()) を使う — ロジックを複製したテストは
「直したこと」を証明しない (tests/CLAUDE.md)。

## 赤の実証 (tests/CLAUDE.md の作法)

`cmd_reap_orphan_assignment` の削除条件チェック
(`status not in ORPHAN_ASSIGNMENT_FINISHED_STATUSES`) を一時的に外して (= 常に
「終了した」とみなして削除を許す欠陥注入)、(b) の各テストが赤くなることを確認した上で
元に戻した。同様に `_kai_codex_orphan_candidate()` の `status in RELEASED_WORK_STATUSES`
判定を `True` に固定する欠陥注入でも (e) が赤くなることを確認した (Result 参照。
`PYTHONDONTWRITEBYTECODE=1` + `__pycache__` 掃除つき)。
"""

from __future__ import annotations

import fcntl
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_tree import copy_plan_tree  # noqa: E402
from test_dispatcher_retirement_exclusion import (  # noqa: E402
    AGENT, SLUG, WINDOW, FakeMux, _load_dispatcher,
)
from test_failed_dependency_hold import MISSION, Sandbox  # noqa: E402
from test_needs_director_releases_assignment import (  # noqa: E402
    _assign, _assignments, _card_text, _put_card, _sb_card, _tasks_dir,
)

CODEX = "Kai-codex"

# ORPHAN_ASSIGNMENT_FINISHED_STATUSES と同じ値であるべき集合 (plan.sh の定義を
# コピーしてはいけないので、値そのものはテストが独自に持つ小さな真理値表として
# だけ使う。plan.sh 側の定義がこれと食い違えば下のパラメトライズが赤くなる)。
FINISHED_STATUSES = ["done", "verified", "skipped", "cancelled", "failed"]
UNFINISHED_STATUSES = ["in_progress", "pending", "ready_for_verification", "verifying"]


# ---------------------------------------------------------------------------
# (a)/(b)/(c)/(d): plan.sh reap-orphan-assignment 単体 (実 plan.sh, Sandbox)
# ---------------------------------------------------------------------------

@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


def _reap(sb, agent=CODEX, *extra):
    return sb.run("reap-orphan-assignment", agent, *extra)


@pytest.mark.parametrize("status", FINISHED_STATUSES)
def test_reaps_an_assignment_pointing_at_a_finished_task(sandbox, status):
    sb = sandbox
    _sb_card(sb, "t001", status, worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")
    (_assignments(sb) / f"{CODEX}.identity").write_text(
        '{"mission": "%s", "task": "t001", "worker": "%s", "started_at": "x"}\n'
        % (MISSION, CODEX))

    r = _reap(sb)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Reaped orphan assignment" in r.stdout, r.stdout
    assert not (_assignments(sb) / CODEX).exists(), "assignment 本体が残っている"
    assert not (_assignments(sb) / f"{CODEX}.identity").exists(), "identity サイドカーが残っている"


def test_control_the_reap_is_the_only_write_for_a_finished_task(sandbox):
    """対照: 撤去以外 (task card 自体) は書き換えないこと。"""
    sb = sandbox
    _sb_card(sb, "t001", "done", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")
    before = (sb.tasks / "t001.md").read_text()

    _reap(sb)

    assert (sb.tasks / "t001.md").read_text() == before


def test_keeps_an_assignment_pointing_at_a_needs_director_task(sandbox):
    """needs_director は「終了した」に含めない —— 正常経路では既に撤去されているはずで、
    それでも残っているのは証拠不足 (破壊的操作はより強い証拠を要求する)。"""
    sb = sandbox
    _sb_card(sb, "t001", "needs_director", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")

    r = _reap(sb)

    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert (_assignments(sb) / CODEX).read_text().strip() == f"{MISSION}:t001"


@pytest.mark.parametrize("status", UNFINISHED_STATUSES)
def test_keeps_an_assignment_pointing_at_a_task_that_has_not_finished(sandbox, status):
    sb = sandbox
    _sb_card(sb, "t001", status, worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")

    r = _reap(sb)

    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert (_assignments(sb) / CODEX).read_text().strip() == f"{MISSION}:t001"


def test_keeps_an_unreadable_assignment(sandbox):
    """通常ファイルでない (ここではディレクトリ) assignment は「観測できなかった」で保留。"""
    sb = sandbox
    _sb_card(sb, "t001", "done", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).mkdir()

    r = _reap(sb)

    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert (_assignments(sb) / CODEX).is_dir()


@pytest.mark.parametrize("value,why", [
    ("garbage\n", "コロンが無い"),
    (":t001\n", "mission が空"),
    (f"{MISSION}:\n", "task が空"),
], ids=["no-colon", "empty-mission", "empty-task"])
def test_keeps_an_assignment_with_the_wrong_shape(sandbox, value, why):
    sb = sandbox
    (_assignments(sb) / CODEX).write_text(value)

    r = _reap(sb)

    assert r.returncode == 3, f"{why}: {(r.returncode, r.stdout, r.stderr)}"
    assert (_assignments(sb) / CODEX).read_text() == value


def test_keeps_an_assignment_pointing_at_a_missing_task(sandbox):
    """mission が無い (archive 済みを含む) / task が無い — どちらも「見つからない」に落ちる。"""
    sb = sandbox
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t099\n")

    r = _reap(sb)

    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert (_assignments(sb) / CODEX).exists()


# ---------------------------------------------------------------------------
# t117: mission が archive 済みでも掃除できる (active dir だけを見て
# 「見つからない」に倒していたのが、B3 が本来消したかった孤児 —— 最後の
# ミッションが完了 → archive された直後 —— を永久に残す穴だった)。
# ---------------------------------------------------------------------------

def _archived_card(sb, task_id, status, **kw):
    d = sb.queue / "archive" / MISSION / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task_id}.md").write_text(_card_text(task_id, status, **kw))


def test_reaps_an_assignment_pointing_at_a_finished_task_in_an_archived_mission(sandbox):
    sb = sandbox
    _archived_card(sb, "t001", "done", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")

    r = _reap(sb)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not (_assignments(sb) / CODEX).exists()


def test_keeps_an_assignment_pointing_at_an_unfinished_task_in_an_archived_mission(sandbox):
    """archive 済みでも「終了した」以外は撤去しない — 判定基準は status であって
    archive の有無ではない。"""
    sb = sandbox
    _archived_card(sb, "t001", "in_progress", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")

    r = _reap(sb)

    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert (_assignments(sb) / CODEX).read_text().strip() == f"{MISSION}:t001"


def test_reaps_an_assignment_after_the_mission_was_archived_via_plan_sh(sandbox):
    """`plan.sh archive` で本当にミッション全体を archive/ へ動かした後でも掃除できる
    (red proof: 修正前は `task_path()` が active dir だけを見るので、ここで
    PRECONDITION_UNMET になり assignment が残り続けた)。"""
    sb = sandbox
    _sb_card(sb, "t001", "done", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")

    archived = sb.run("archive", MISSION)
    assert archived.returncode == 0, (archived.returncode, archived.stdout, archived.stderr)
    assert not (sb.queue / "missions" / MISSION).exists()
    assert (sb.queue / "archive" / MISSION / "tasks" / "t001.md").exists()

    r = _reap(sb)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not (_assignments(sb) / CODEX).exists()


def test_is_a_noop_when_the_assignment_is_already_absent(sandbox):
    sb = sandbox
    r = _reap(sb)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "既にありません" in r.stdout, r.stdout


def test_reaping_one_agent_does_not_touch_another_agents_assignment(sandbox):
    """assignment はファイル名 (= agent 名) で束縛される。別 agent には一切触れない。"""
    sb = sandbox
    _sb_card(sb, "t001", "done", worker=CODEX, skills="codex-review")
    _sb_card(sb, "t002", "in_progress", worker="Other-worker", skills="code")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")
    (_assignments(sb) / "Other-worker").write_text(f"{MISSION}:t002\n")

    r = _reap(sb, CODEX)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not (_assignments(sb) / CODEX).exists()
    assert (_assignments(sb) / "Other-worker").read_text().strip() == f"{MISSION}:t002", (
        "無関係な agent の assignment に触れた")


def test_no_wait_gives_up_on_a_busy_lock_without_writing(sandbox):
    sb = sandbox
    _sb_card(sb, "t001", "done", worker=CODEX, skills="codex-review")
    (_assignments(sb) / CODEX).write_text(f"{MISSION}:t001\n")

    lock_path = sb.queue / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        r = _reap(sb, CODEX, "--no-wait")

    assert r.returncode == 4, (r.returncode, r.stdout, r.stderr)
    assert (_assignments(sb) / CODEX).exists(), "ロックが取れなかったのに書き込んだ"


def test_requires_a_known_positional_agent(sandbox):
    sb = sandbox
    r = sb.run("reap-orphan-assignment")
    assert r.returncode != 0
    assert not (_assignments(sb) / CODEX).exists()


# ---------------------------------------------------------------------------
# (e): dispatcher.sh の安い判定 — 候補が無いサイクルは plan.sh を起動しない
# ---------------------------------------------------------------------------

def _dispatcher_root(tmp_path):
    """dispatcher.sh を exec() するための最小の root。実 plan.sh を copy_plan_tree で
    隔離コピーしてある (t009: reap_kai_codex_orphan_assignment が本当に plan.sh を
    起動する経路をテストするため)。"""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "registry" / "mux").mkdir(parents=True)
    (root / "registry" / "retirements").mkdir(parents=True)
    (root / "queue" / "missions" / SLUG / "tasks").mkdir(parents=True)
    (root / "queue" / "assignments").mkdir(parents=True)
    (root / "queue" / "archive").mkdir(parents=True)
    (root / "queue" / "state.yaml").write_text(
        f"active_missions:\n  - {SLUG}\ndefault_mission: {SLUG}\n")
    (root / "queue" / "missions" / SLUG / "mission.yaml").write_text(
        f'title: dispatch reap test\nslug: {SLUG}\nstatus: in_progress\n'
        f'created_at: "2026-01-01T00:00:00Z"\ncompleted_at: null\nnext_task_id: 99\n')
    (root / "registry" / "workers.yaml").write_text(
        f"workers:\n  - name: {AGENT}\n    skills: [code]\n    experience: 0\n")
    copy_plan_tree(root)
    return root


def _spy_run_namespace(spy_calls):
    return types.SimpleNamespace(**{**vars(subprocess), "run": lambda *a, **k: spy_calls.append((a, k))})


def test_dispatcher_skips_plan_sh_when_there_is_no_assignment(tmp_path):
    root = _dispatcher_root(tmp_path)
    mux = FakeMux([])
    ns = _load_dispatcher(root, mux)
    calls = []
    ns["subprocess"] = _spy_run_namespace(calls)

    ns["reap_kai_codex_orphan_assignment"]({})

    assert calls == [], f"孤児候補が無いのに plan.sh を起動した: {calls}"


def test_dispatcher_skips_plan_sh_for_a_needs_director_assignment(tmp_path):
    root = _dispatcher_root(tmp_path)
    _put_card(_tasks_dir(root), "t011", "needs_director", aged=True,
              worker=CODEX, skills="codex-review", pr=4)
    _assign(root, CODEX, f"{SLUG}:t011\n")
    mux = FakeMux([])
    ns = _load_dispatcher(root, mux)
    calls = []
    ns["subprocess"] = _spy_run_namespace(calls)

    ns["reap_kai_codex_orphan_assignment"]({SLUG: {"t011": "needs_director"}})

    assert calls == [], f"needs_director の assignment に plan.sh を起動した: {calls}"
    assert (root / "queue" / "assignments" / CODEX).exists()


@pytest.mark.parametrize("status", FINISHED_STATUSES)
def test_dispatcher_reaps_a_real_orphan_assignment_via_plan_sh(tmp_path, status):
    root = _dispatcher_root(tmp_path)
    _put_card(_tasks_dir(root), "t011", status, aged=True,
              worker=CODEX, skills="codex-review", pr=4)
    _assign(root, CODEX, f"{SLUG}:t011\n")
    mux = FakeMux([])
    ns = _load_dispatcher(root, mux)

    ns["reap_kai_codex_orphan_assignment"]({SLUG: {"t011": status}})

    assert not (root / "queue" / "assignments" / CODEX).exists(), (
        f"[{status}] 実 plan.sh を通した掃除が assignment を消さなかった")


# ---------------------------------------------------------------------------
# (f): end-to-end — 同じサイクルで孤児を掃除し、次の codex-review を spawn する
# ---------------------------------------------------------------------------

class _PopenSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *a, **kw):
        self.calls.append(list(cmd))
        return self


def _kai_spawns(spy):
    return [c for c in spy.calls if any(str(p).endswith("kai-review.sh") for p in c)]


def test_end_to_end_reaps_the_orphan_and_spawns_the_next_review_in_one_cycle(tmp_path):
    root = _dispatcher_root(tmp_path)
    tasks = _tasks_dir(root)
    _put_card(tasks, "t011", "done", aged=True, worker=CODEX, skills="codex-review", pr=4)
    _put_card(tasks, "t010", "pending", aged=True, skills="codex-review", pr=5)
    _assign(root, CODEX, f"{SLUG}:t011\n")
    (root / "scripts" / "kai-review.sh").write_text("#!/bin/bash\nexit 0\n")

    mux = FakeMux([])
    ns = _load_dispatcher(root, mux)
    spy = _PopenSpy()
    ns["subprocess"] = types.SimpleNamespace(**{**vars(subprocess), "Popen": spy})

    ns["dispatch"]()

    assert not (root / "queue" / "assignments" / CODEX).exists(), (
        "1 サイクルの中で孤児 assignment が掃除されなかった")
    spawned = _kai_spawns(spy)
    assert spawned and "t010" in spawned[0], f"次の codex-review が spawn されない: {spy.calls}"


# ---------------------------------------------------------------------------
# (g) t117: 掃除は dispatch() の早期 return (active なし / 全部 done) の
# 後ろではなく前で動く。red proof: 修正前はこの節の "reaps" 系がどちらも
# assignment を残したまま return していた。
# ---------------------------------------------------------------------------

def _set_no_active_missions(root):
    (root / "queue" / "state.yaml").write_text("active_missions: []\ndefault_mission: null\n")


def _mark_mission_done(root, slug):
    (root / "queue" / "missions" / slug / "mission.yaml").write_text(
        f'title: dispatch reap test\nslug: {slug}\nstatus: done\n'
        f'created_at: "2026-01-01T00:00:00Z"\ncompleted_at: "2026-01-02T00:00:00Z"\n'
        f'next_task_id: 99\n')


def _archived_dispatcher_card(root, slug, task_id, status, **kw):
    d = root / "queue" / "archive" / slug / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task_id}.md").write_text(_card_text(task_id, status, **kw))


def test_dispatch_reaps_an_orphan_in_an_archived_mission_when_there_are_no_active_missions(tmp_path):
    """B3 が本来直したかった形そのもの: 最後のミッションが完了 → archive され、
    active_missions が空になった直後でも、次のサイクルで孤児が掃除される。"""
    root = _dispatcher_root(tmp_path)
    _set_no_active_missions(root)
    _archived_dispatcher_card(root, SLUG, "t011", "done", worker=CODEX, skills="codex-review", pr=4)
    _assign(root, CODEX, f"{SLUG}:t011\n")
    mux = FakeMux([])
    ns = _load_dispatcher(root, mux)

    ns["dispatch"]()

    assert not (root / "queue" / "assignments" / CODEX).exists(), (
        "active なミッションが 0 件の早期 return の後ろで掃除が止まっている")


def test_dispatch_keeps_a_live_orphan_in_an_archived_mission_when_there_are_no_active_missions(tmp_path):
    """回帰 (t009): 参照先が実際に走っている (in_progress) なら、この早期 return
    経路でも消してはいけない。"""
    root = _dispatcher_root(tmp_path)
    _set_no_active_missions(root)
    _archived_dispatcher_card(root, SLUG, "t011", "in_progress", worker=CODEX, skills="codex-review", pr=4)
    _assign(root, CODEX, f"{SLUG}:t011\n")
    mux = FakeMux([])
    ns = _load_dispatcher(root, mux)

    ns["dispatch"]()

    assert (root / "queue" / "assignments" / CODEX).read_text().strip() == f"{SLUG}:t011", (
        "生きている codex-review の assignment を消してしまった")


def test_dispatch_reaps_an_orphan_when_all_active_missions_are_done(tmp_path):
    """`active_missions` に残っていても、mission.yaml が全部 done なら早期 return
    する —— それでも掃除は動くこと。"""
    root = _dispatcher_root(tmp_path)
    _mark_mission_done(root, SLUG)
    _put_card(_tasks_dir(root), "t011", "done", aged=True,
              worker=CODEX, skills="codex-review", pr=4)
    _assign(root, CODEX, f"{SLUG}:t011\n")
    mux = FakeMux([])
    ns = _load_dispatcher(root, mux)

    ns["dispatch"]()

    assert not (root / "queue" / "assignments" / CODEX).exists(), (
        "全 active ミッションが done の早期 return の後ろで掃除が止まっている")


def test_dispatch_keeps_a_live_orphan_when_all_active_missions_are_done(tmp_path):
    """回帰 (t009): 全 active ミッションが done の早期 return 経路でも、走っている
    codex-review の assignment は消さない。"""
    root = _dispatcher_root(tmp_path)
    _mark_mission_done(root, SLUG)
    _put_card(_tasks_dir(root), "t011", "in_progress", aged=True,
              worker=CODEX, skills="codex-review", pr=4)
    _assign(root, CODEX, f"{SLUG}:t011\n")
    mux = FakeMux([])
    ns = _load_dispatcher(root, mux)

    ns["dispatch"]()

    assert (root / "queue" / "assignments" / CODEX).read_text().strip() == f"{SLUG}:t011", (
        "生きている codex-review の assignment を消してしまった")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
