#!/usr/bin/env python3
"""tests/test_registry_isolation.py — テストが本番の registry を書かないことを構造で固定する (t013 / PR4)。

## 何が起きたか (2026-09-26)

`plan.sh done` の task_count 加算は `CREWVIA_REPO_ROOT` (無ければ plan.sh の位置) の
`registry/workers.yaml` を書いた。一方 registry の**読み取り**は `dirname(QUEUE_DIR)/registry`。
読み書きの registry が食い違っていたので、`CREWVIA_QUEUE` だけを一時ディレクトリに向けて
`plan.sh done` を走らせるテスト (bats の plan-assignment-identity ほか) は、queue だけ隔離した
つもりで **本番の task_count を加算し続けた** (Ren が 7 → 402)。

修正は `registry_dir()` の 1 関数 (queue の隣)。このファイルは、修正を戻すと赤くなる
テストと、「新しい subcommand を足したら、ここに隔離実行の呼び出しも足す」を強制するテスト。

## 何を確かめるか

`plan.sh` の**位置の** `registry/` (= 本番の代役。`checkout/registry/`) と、queue を置く
**別の**一時ディレクトリを用意し、`CREWVIA_QUEUE` だけをそちらに向けて全 subcommand を走らせる。
そのあと checkout 側の `registry/` は **1 バイトも変わっていてはならない** (中身・ファイルの
有無・mtime を含む)。`CREWVIA_REPO_ROOT` を checkout に向けた変種も同じ (start.sh が
export する本番の env の形。queue だけが付け替わった実行)。

陽性対照: `done` が queue の隣の registry を実際に加算することを確かめる (何も走っていない
のに「変わらなかった」で緑になるのを防ぐ)。
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
import sys

import pytest

from fixture_tree import copy_plan_tree

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLAN_SH = REPO_ROOT / "scripts" / "plan.sh"

MISSION = "iso-m"

_WORKERS_YAML = (
    "workers:\n"
    "  - name: Ren\n"
    "    skills: [code, bash]\n"
    "    task_count: 7\n"
    "    last_active: 2026-01-01\n"
    "  - name: Riker\n"
    "    role: director\n"
    "    skills: [planning]\n"
    "    task_count: 0\n"
)


def _card(task_id: str, status: str, worker: str = "null", extra: str = "") -> str:
    return (
        "---\n"
        f"id: {task_id}\n"
        f"title: task {task_id}\n"
        "skills: [code]\n"
        "priority: medium\n"
        f"status: {status}\n"
        "blocked_by: []\n"
        "target_dir: null\n"
        f"worker: {worker}\n"
        'started_at: "2026-01-01T00:00:00Z"\n'
        "completed_at: null\n"
        f"{extra}"
        "---\n\n## Description\n\nfixture\n\n## Result\n\n"
    )


class World:
    """checkout (= plan.sh の位置。本番の代役) と、別の場所の queue。"""

    def __init__(self, tmp_path: pathlib.Path):
        self.checkout = tmp_path / "checkout"          # plan.sh の位置 = 「本番」
        self.other = tmp_path / "isolated"             # テストが queue を置く場所
        copy_plan_tree(self.checkout)
        for sub in ("retirements", "mux", "handoffs/Ren", "task-graph", "daemons",
                    "heartbeats", "workers"):
            (self.checkout / "registry" / sub).mkdir(parents=True)
        (self.checkout / "registry" / "workers.yaml").write_text(_WORKERS_YAML)
        (self.checkout / "registry" / "task-graph" / "tasks.json").write_text('{"tasks": []}\n')
        (self.checkout / "registry" / "retirements" / "Ren.json").write_text("{}\n")
        (self.checkout / "registry" / "handoffs" / "Ren" / "t003_HANDOFF.md").write_text("h\n")
        (self.checkout / "queue").mkdir()               # 本番 queue の代役 (触れてはいけない)
        (self.checkout / "queue" / "state.yaml").write_text("active_missions: []\n")

        self.queue = self.other / "queue"
        self.registry = self.other / "registry"        # queue の隣 = 使われるべき registry
        (self.queue / "archive").mkdir(parents=True)
        self.registry.mkdir()
        (self.registry / "workers.yaml").write_text(_WORKERS_YAML)
        mdir = self.queue / "missions" / MISSION
        (mdir / "tasks").mkdir(parents=True)
        (mdir / "mission.yaml").write_text(
            f"title: iso\nslug: {MISSION}\nstatus: in_progress\n"
            'created_at: "2026-01-01T00:00:00Z"\ncompleted_at: null\n'
            "next_task_id: 20\nmax_review_cycles: 3\n")
        (self.queue / "state.yaml").write_text(
            f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n")
        for tid, status, worker in (
            ("t001", "pending", "null"),
            ("t002", "in_progress", "Ren"),      # done
            ("t003", "in_progress", "Ren"),      # fail
            ("t004", "in_progress", "Ren"),      # needs-director
            ("t005", "in_progress", "Ren"),      # retire
            ("t006", "in_progress", "Ren"),      # ready-for-verification
            ("t007", "verifying", "Ren"),        # verify-result
            ("t008", "failed", "Ren"),           # release-dep / update --reset
        ):
            extra = ""
            if tid == "t008":
                # 開き直す (`update --reset`) と、card の handoff_path が指す古い handoff を改名する。
                # 指す先は checkout (本番の代役) の registry/handoffs — 隔離実行はそれを動かさない
                extra = f"handoff_path: {self.checkout}/registry/handoffs/Ren/t003_HANDOFF.md\n"
            (mdir / "tasks" / f"{tid}.md").write_text(_card(tid, status, worker, extra))

    def run(self, *args: str, with_repo_root: bool = False,
            repo_root: pathlib.Path | None = None) -> subprocess.CompletedProcess:
        env = {
            # claude / tmux / herdr が PATH に居ても、ここでは起動されないようにしておく
            "PATH": f"{pathlib.Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(self.other),
            "CREWVIA_QUEUE": str(self.queue),
            "AGENT_NAME": "Ren",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if with_repo_root:
            env["CREWVIA_REPO_ROOT"] = str(self.checkout)
        if repo_root is not None:
            env["CREWVIA_REPO_ROOT"] = str(repo_root)
        return subprocess.run(
            ["bash", str(self.checkout / "scripts" / "plan.sh"), *args],
            cwd=self.other, env=env, capture_output=True, text=True, timeout=120,
        )


def _snapshot(root: pathlib.Path) -> dict[str, tuple]:
    """`root` 以下の全エントリの (種別, 中身の hash, mtime_ns)。ファイルの有無も含む。"""
    snap: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            snap[rel] = ("link", os.readlink(path), 0)
        elif path.is_dir():
            snap[rel] = ("dir", "", 0)
        else:
            snap[rel] = ("file", hashlib.sha256(path.read_bytes()).hexdigest(),
                         path.stat().st_mtime_ns)
    return snap


#: subcommand → その subcommand を走らせる引数列 (複数可)。exit code は問わない —
#: **失敗した実行も registry を書いてはいけない**。queue を書き換える run が先に済んでいても
#: 後始末で書く経路 (done の task_count・retire の marker・handoff の退避) に届くよう、
#: 意味のある task を指す。`review` は mission が in_progress なので review-plan.sh
#: (= claude を起動する) の手前で止まる。
ISOLATED_INVOCATIONS: dict[str, list[list[str]]] = {
    "init": [["init", "iso two", "--mission", "iso-two"]],
    "add": [["add", "new task", "--skills", "code", "--mission", MISSION]],
    "pull": [["pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION]],
    "done": [["done", "t002", "finished", "--mission", MISSION]],
    "needs-director": [["needs-director", "t004", "need a decision", "--mission", MISSION]],
    "fail": [["fail", "t003", "--no-head", "iso", "--mission", MISSION]],
    "update": [
        ["update", "t001", "--priority", "high", "--mission", MISSION],
        ["update", "t008", "--reset", "--mission", MISSION],
    ],
    "release-dep": [["release-dep", "t008", "--mission", MISSION]],
    "retire": [["retire", "t005", "--agent", "Ren", "--started-at", "2026-01-01T00:00:00Z",
                "--mission", MISSION, "--outcome", "reset", "--no-wait"]],
    "ready-for-verification": [["ready-for-verification", "t006", "--mission", MISSION]],
    "verify-result": [["verify-result", "t007", "pass", "--mission", MISSION]],
    "review": [["review", MISSION]],
    "launch": [["launch", MISSION]],
    "task-graph": [["task-graph"]],
    "lint": [["lint", MISSION]],
    "status": [["status", "--mission", MISSION]],
    "archive": [["archive", "iso-two"]],
    "resync": [["resync"]],
    "dashboard-data": [["dashboard-data"]],
    "resolve-mission": [["resolve-mission", "t001", "--mission", MISSION]],
}


def _dispatch_names() -> set[str]:
    """plan.sh 末尾の dispatch テーブルから subcommand を**実装から**拾う。"""
    text = PLAN_SH.read_text()
    table = text[text.index("\ndispatch = {"):]
    table = table[:table.index("\n}\n")]
    return set(re.findall(r"^\s+'([a-z-]+)':\s*cmd_", table, re.M))


def test_every_subcommand_has_an_isolated_invocation():
    """新しい subcommand を足したら、隔離実行の呼び出しもここに足す (足し忘れは赤)。"""
    assert _dispatch_names() == set(ISOLATED_INVOCATIONS), (
        "plan.sh の dispatch と ISOLATED_INVOCATIONS がずれている。新しい subcommand は "
        "この表に呼び出しを足し、CREWVIA_QUEUE だけを付け替えた実行が checkout の registry を"
        "書かないことを確かめること")


@pytest.mark.parametrize("with_repo_root", [False, True], ids=["queue-only", "queue+repo-root"])
def test_isolated_queue_never_writes_the_checkout_registry(tmp_path, with_repo_root):
    """CREWVIA_QUEUE を付け替えた全 subcommand の実行は、plan.sh の位置の registry/ を変えない。"""
    world = World(tmp_path)
    before = _snapshot(world.checkout / "registry")
    ran = []
    for name in sorted(ISOLATED_INVOCATIONS):
        for args in ISOLATED_INVOCATIONS[name]:
            proc = world.run(*args, with_repo_root=with_repo_root)
            ran.append((args, proc.returncode))
    after = _snapshot(world.checkout / "registry")
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert not changed, (
        f"CREWVIA_QUEUE だけを付け替えた plan.sh が、位置の registry を書き換えた: {changed}\n"
        f"実行: {ran}")


def test_done_bumps_the_registry_next_to_the_queue(tmp_path):
    """陽性対照: done の task_count 加算は queue の隣の registry に着く (読みと同じ場所)。"""
    world = World(tmp_path)
    proc = world.run("done", "t002", "finished", "--mission", MISSION)
    assert proc.returncode == 0, proc.stderr
    bumped = (world.registry / "workers.yaml").read_text()
    assert re.search(r"name: Ren\n(?:.*\n)*?\s+task_count: 8\b", bumped), bumped
    assert (world.checkout / "registry" / "workers.yaml").read_text() == _WORKERS_YAML


def test_pull_is_not_blocked_by_a_retirement_marker_in_the_checkout_registry(tmp_path):
    """読み取りも queue の隣の registry: 位置の registry の marker (本番の代役) は見ない。

    checkout の registry/retirements/Ren.json は Ren の退役予約。queue を付け替えた実行がそれを
    読むと pull が拒否される (本番の状態がテストの結果を左右する)。
    """
    world = World(tmp_path)
    assert (world.checkout / "registry" / "retirements" / "Ren.json").exists()
    proc = world.run("pull", "--agent", "Ren", "--skills", "code", "--mission", MISSION)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_worker_in_a_worktree_still_bumps_the_production_registry(tmp_path):
    """Worker が worktree の plan.sh から done しても、本番の registry に着く。

    `world.checkout` が worktree (plan.sh の位置)、`world.other` が本番 (queue と registry が
    並ぶ。CREWVIA_REPO_ROOT / CREWVIA_QUEUE は本番を指す — start.sh が export する形)。
    書き先を CREWVIA_REPO_ROOT から queue の隣に替えても、これは変わらない (正しい挙動)。
    """
    world = World(tmp_path)
    proc = world.run("done", "t002", "finished", "--mission", MISSION,
                     repo_root=world.other)
    assert proc.returncode == 0, proc.stderr
    assert "task_count: 8" in (world.registry / "workers.yaml").read_text()
    assert (world.checkout / "registry" / "workers.yaml").read_text() == _WORKERS_YAML
