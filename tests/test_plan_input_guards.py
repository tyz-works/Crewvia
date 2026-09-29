#!/usr/bin/env python3
"""plan.sh の入力の守り 3 つ (t013 / C4)。

1. `plan.sh init --inactive` — 使い捨て mission を dispatcher に見せずに作る。
   `init` は `active_missions` に足して `default_mission` も書き換える。本番確認の QA が観察用に
   作った mission の task を、dispatcher が本物として配ろうとした (2026-09-28/29, t037 / t041)。
2. `add` / `update` は `blocked_by` の循環を exit 2 で拒否し、何も書かない。循環の定義は
   `lib_dep_rules.find_dependency_cycle()` の 1 か所 (lint_plan.py も同じ関数を呼ぶ)。
3. `deliverable: pr` の task の下流 (blocked_by を逆にたどった先) に skills が `review` の task が
   無ければ lint が WARN (FAIL にはしない)。

赤の実証は tests/red_proof_t013_input_guards.sh。
"""

from __future__ import annotations

import ast
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from fixture_tree import copy_plan_tree  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE = "m-live"
PROBE = "m-probe"


class Sandbox:
    """`<root>/queue` と `<root>/registry` を持つ使い捨ての crewvia (plan.sh は隔離コピー)。"""

    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / "registry" / "mux").mkdir(parents=True)
        copy_plan_tree(self.root)
        # lint が読む config (skill-permissions 等) と hooks/lib_skill_perms.py。実 config の写しで、本番には触れない
        shutil.copytree(REPO_ROOT / "config", self.root / "config")
        shutil.copytree(REPO_ROOT / "hooks", self.root / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.queue = self.root / "queue"
        self.state = self.queue / "state.yaml"

    def env(self):
        # ambient の AGENT_NAME / CREWVIA_* を引き継がない (env -i 相当)
        return {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE", str(pathlib.Path.home() / ".local")),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CREWVIA_REPO_ROOT": str(self.root),
            "CREWVIA_QUEUE": str(self.queue),
            "CREWVIA_TASKVIA": "disabled",
        }

    def plan(self, *args):
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "plan.sh"), *args],
            env=self.env(), cwd=str(self.root), capture_output=True, text=True, timeout=120)

    def ok(self, *args):
        r = self.plan(*args)
        assert r.returncode == 0, f"plan.sh {args}: rc={r.returncode}\n{r.stdout}\n{r.stderr}"
        return r

    def card_path(self, mission, task):
        return self.queue / "missions" / mission / "tasks" / f"{task}.md"

    def cards(self, mission):
        d = self.queue / "missions" / mission / "tasks"
        return {p.name: p.read_bytes() for p in sorted(d.glob("*.md"))}

    def add(self, mission, title, *extra):
        if "--deliverable" not in extra:
            extra = (*extra, "--deliverable", "none")
        return self.ok("add", title, "--mission", mission, "--skills", "code", *extra)


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


@pytest.fixture
def live(sb):
    """本物の作業 (m-live) が active・default の状態から始める。"""
    sb.ok("init", "live work", "--mission", LIVE)
    return sb


# ---------------------------------------------------------------------------
# (1) init --inactive
# ---------------------------------------------------------------------------

def test_init_inactive_does_not_touch_state(live):
    before = live.state.read_bytes()
    live.ok("init", "probe", "--mission", PROBE, "--inactive")
    assert live.state.read_bytes() == before, "state.yaml が変わった"
    assert (live.queue / "missions" / PROBE / "mission.yaml").exists()


def test_plain_init_still_activates_and_takes_default(live):
    """陽性対照: --inactive なしの init は active_missions に足し default_mission を奪う。"""
    before = live.state.read_bytes()
    live.ok("init", "probe", "--mission", PROBE)
    after = live.state.read_text()
    assert live.state.read_bytes() != before
    assert PROBE in after and f"default_mission: {PROBE}" in after


def test_inactive_mission_is_usable_with_explicit_mission(live):
    live.ok("init", "probe", "--mission", PROBE, "--inactive")
    live.add(PROBE, "first")
    live.add(PROBE, "second", "--blocked-by", "t001")
    live.ok("update", "t002", "--mission", PROBE, "--priority", "high")
    r = live.plan("lint", "--mission", PROBE)
    assert r.returncode == 0, r.stdout + r.stderr
    # 実行系も --mission 付きで動き、その間 state.yaml は一度も変わらない
    before = live.state.read_bytes()
    live.ok("pull", "--task", "t001", "--mission", PROBE, "--agent", "Probe", "--skills", "code")
    live.ok("done", "t001", "probe done", "--mission", PROBE, "--no-pr", "probe")
    assert live.state.read_bytes() == before


def test_dispatcher_does_not_hand_out_an_inactive_missions_task(tmp_path):
    """dispatcher 1 サイクルで、--inactive の mission の task に kickoff が飛ばない。

    陽性対照: 同じ手順で --inactive を外すと kickoff が飛ぶ (ハーネスが動いている証拠)。
    """
    from test_dispatcher_cycle_honours_hold import _kickoffs_for, run_dispatch_cycle
    from test_failed_dependency_hold import Sandbox as HoldSandbox

    def one(name, inactive):
        hs = HoldSandbox(tmp_path / name)
        # HoldSandbox は自前の mission を 1 つ持つ。本物の作業として active のまま残す
        s = Sandbox(tmp_path / f"{name}-plan")
        # dispatcher のハーネス (HoldSandbox) の root に plan.sh を向ける
        s.root, s.queue, s.state = hs.root, hs.queue, hs.queue / "state.yaml"
        args = ["init", "probe", "--mission", PROBE] + (["--inactive"] if inactive else [])
        s.ok(*args)
        s.add(PROBE, "probe task")
        mux, log = run_dispatch_cycle(hs)
        return _kickoffs_for(mux, "t001"), mux, log

    sent_inactive, mux, log = one("inactive", True)
    assert not sent_inactive, f"--inactive の mission の task が配られた: {mux.sent}\n{log}"
    sent_active, mux, log = one("active", False)
    assert sent_active, f"陽性対照: active な mission の task に kickoff が飛ばない: {mux.sent}\n{log}"


# ---------------------------------------------------------------------------
# (2) blocked_by の循環
# ---------------------------------------------------------------------------

@pytest.fixture
def chain(live):
    """t001 ← t002 ← t003 (t002 は t001 を、t003 は t002 を待つ)。"""
    live.add(LIVE, "one")
    live.add(LIVE, "two", "--blocked-by", "t001")
    live.add(LIVE, "three", "--blocked-by", "t002")
    return live


def test_update_that_closes_a_cycle_is_refused_and_writes_nothing(chain):
    before = chain.cards(LIVE)
    r = chain.plan("update", "t001", "--mission", LIVE, "--blocked-by", "t003")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "循環" in r.stderr
    assert chain.cards(LIVE) == before


def test_update_self_dependency_is_refused(chain):
    before = chain.cards(LIVE)
    r = chain.plan("update", "t002", "--mission", LIVE, "--blocked-by", "t002")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert chain.cards(LIVE) == before


def test_update_that_keeps_the_graph_acyclic_is_accepted(chain):
    """陽性対照: 循環にならない付け替えは通る (拒否が厳しすぎない)。"""
    chain.ok("update", "t003", "--mission", LIVE, "--blocked-by", "t001,t002")
    assert b"t001" in chain.cards(LIVE)["t003.md"]


def test_add_that_closes_a_cycle_is_refused_and_writes_nothing(live):
    """t001 が (まだ無い) t002 を待つ状態で、t002 が t001 を待つ task として add する。"""
    live.add(LIVE, "one")
    live.ok("update", "t001", "--mission", LIVE, "--blocked-by", "t002")
    before = live.cards(LIVE)
    r = live.plan("add", "two", "--mission", LIVE, "--skills", "code", "--deliverable", "none", "--blocked-by", "t001")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "循環" in r.stderr
    assert live.cards(LIVE) == before
    assert "t002.md" not in live.cards(LIVE)


def test_add_that_depends_on_its_own_future_id_is_refused(live):
    live.add(LIVE, "one")
    before = live.cards(LIVE)
    r = live.plan("add", "two", "--mission", LIVE, "--skills", "code", "--deliverable", "none", "--blocked-by", "t002")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert live.cards(LIVE) == before


def test_lint_still_reports_a_cycle_written_by_hand(chain):
    """既存 mission に手で書かれた循環を lint が FAIL にする (同じ関数を共有した後も)。"""
    p = chain.card_path(LIVE, "t001")
    p.write_text(p.read_text().replace("blocked_by: []", "blocked_by: [t003]"))
    r = chain.plan("lint", "--mission", LIVE)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "circular dependency detected" in r.stdout


def test_cycle_definition_is_only_in_lib_dep_rules():
    """循環の判定 (DFS の in_stack 走査) を lint_plan.py / plan.sh にコピーしない。"""
    import lib_dep_rules
    assert lib_dep_rules.find_dependency_cycle({"a": ["b"], "b": ["a"]}) == ["a", "b", "a"]
    assert lib_dep_rules.find_dependency_cycle({"a": ["b"], "b": []}) is None
    assert lib_dep_rules.find_dependency_cycle({"a": ["ghost"]}) is None   # 未定義の依存は辿らない
    for name in ("lint_plan.py", "plan.sh"):
        text = (REPO_ROOT / "scripts" / name).read_text()
        assert "in_stack" not in text, f"{name} に循環検出の写しがある"
        assert "find_dependency_cycle" in text, f"{name} が共有関数を呼んでいない"
    # 共有関数を呼ぶ側が、何かで包み直していないこと (lint_plan は lib_dep_rules を経由する)
    tree = ast.parse((REPO_ROOT / "scripts" / "lint_plan.py").read_text())
    assert any(isinstance(n, ast.Attribute) and n.attr == "find_dependency_cycle"
               for n in ast.walk(tree))


# ---------------------------------------------------------------------------
# (3) deliverable: pr に review の下流が無ければ WARN
# ---------------------------------------------------------------------------

def _pr_mission(sb, review_skills=None, chain_via=None):
    """t001 (deliverable: pr) と、任意で下流の review task を持つ mission を作る。"""
    sb.ok("init", "pr work", "--mission", LIVE)
    sb.ok("add", "impl", "--mission", LIVE, "--skills", "code", "--deliverable", "pr")
    if chain_via:
        sb.ok("add", "middle", "--mission", LIVE, "--skills", chain_via, "--deliverable", "none", "--blocked-by", "t001")
    if review_skills:
        dep = "t002" if chain_via else "t001"
        sb.ok("add", "review it", "--mission", LIVE, "--skills", review_skills, "--deliverable", "none", "--blocked-by", dep)


def _lint(sb):
    r = sb.plan("lint", "--mission", LIVE)
    return r, [ln for ln in r.stdout.splitlines() if "review" in ln and "deliverable" in ln and "WARN" in ln]


def test_pr_task_without_a_downstream_review_warns_but_does_not_fail(sb):
    _pr_mission(sb)
    r, warns = _lint(sb)
    assert warns and "task/t001" in warns[0], r.stdout
    assert "[FAIL]" not in r.stdout
    assert r.returncode == 0, r.stdout


def test_pr_task_with_a_downstream_review_does_not_warn(sb):
    _pr_mission(sb, review_skills="review")
    r, warns = _lint(sb)
    assert not warns, r.stdout


def test_pr_task_with_an_indirect_downstream_review_does_not_warn(sb):
    _pr_mission(sb, review_skills="review", chain_via="code")
    r, warns = _lint(sb)
    assert not warns, r.stdout


def test_a_downstream_task_that_is_not_review_still_warns(sb):
    """下流があっても skills に review が無ければ足りない (docs だけ積んだ場合)。"""
    _pr_mission(sb, review_skills="docs")
    r, warns = _lint(sb)
    assert warns, r.stdout


def test_a_review_task_that_is_not_downstream_does_not_count(sb):
    """同じ mission に review task があっても、t001 を待っていなければ数えない。"""
    _pr_mission(sb)
    sb.ok("add", "unrelated review", "--mission", LIVE, "--skills", "review", "--deliverable", "none")
    r, warns = _lint(sb)
    assert warns and "task/t001" in warns[0], r.stdout
