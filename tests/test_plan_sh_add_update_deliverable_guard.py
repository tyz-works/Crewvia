#!/usr/bin/env python3
"""plan.sh add / update が、skills では作れない成果物の宣言を拒否する (t001)。

2026-10-11 の実例: skills=[research, docs]・deliverable=pr の task を足した。check_permission() は
全 skill の deny を allow より先に適用するので Write/Edit が拒否され、Worker は調査だけして
needs_director になった。`plan.sh lint` は検出できたが add は lint を走らせなかった。

判定の本体は lint_plan.deliverable_refusal() (= check_deliverable() への委譲)。ここで確かめるのは
(1) 拒否の形 (exit 2・何も書かない・理由と直し方が出る) と (2) lint と add の答えが
skill の組み合わせ全部で一致すること。

    python3 -m pytest tests/test_plan_sh_add_update_deliverable_guard.py -v
"""

from __future__ import annotations

import itertools
import pathlib
import re
import sys

import pytest
import yaml

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(TESTS_DIR))

import lint_plan  # noqa: E402
from test_assignment_routing import MISSION  # noqa: E402
from test_task_deliverable import DeliverableSandbox  # noqa: E402

PERMS = str(REPO / "config" / "skill-permissions.yaml")


@pytest.fixture
def sb(tmp_path):
    return DeliverableSandbox(tmp_path)


def _add(sb, skills, deliverable=None):
    args = ["add", "x", "--mission", MISSION, "--skills", ",".join(skills)]
    if deliverable is not None:
        args += ["--deliverable", deliverable]
    return sb.run(*args)


class TestAdd:
    def test_research_docs_pr_is_refused_and_nothing_is_written(self, sb):
        before = sb.snapshot()
        r = _add(sb, ["research", "docs"], "pr")
        assert r.returncode == 2, r.stderr
        assert sb.snapshot() == before
        assert "research" in r.stderr                      # 拒否した skill
        assert "deny" in r.stderr.lower() or "拒否" in r.stderr   # どの deny が効いたか
        assert "docs" in r.stderr                          # 直し方

    def test_docs_pr_passes(self, sb):
        r = _add(sb, ["docs"], "pr")
        assert r.returncode == 0, r.stderr

    def test_research_none_passes(self, sb):
        assert _add(sb, ["research"], "none").returncode == 0

    def test_undeclared_passes(self, sb):
        assert _add(sb, ["research"]).returncode == 0


class TestUpdate:
    def _seed(self, sb, skills, deliverable=None):
        r = _add(sb, skills, deliverable)
        assert r.returncode == 0, r.stderr
        return re.search(r"(t\d+) —", r.stdout).group(1)

    def test_adding_research_to_a_pr_task_is_refused(self, sb):
        tid = self._seed(sb, ["docs"], "pr")
        before = sb.snapshot()
        r = sb.run("update", tid, "--mission", MISSION, "--skills", "docs,research")
        assert r.returncode == 2, r.stderr
        assert sb.snapshot() == before

    def test_declaring_pr_on_a_research_task_is_refused(self, sb):
        tid = self._seed(sb, ["research"])
        before = sb.snapshot()
        r = sb.run("update", tid, "--mission", MISSION, "--deliverable", "pr")
        assert r.returncode == 2, r.stderr
        assert sb.snapshot() == before

    def test_fixing_the_skills_passes(self, sb):
        tid = self._seed(sb, ["code"], "pr")
        assert sb.run("update", tid, "--mission", MISSION, "--skills", "docs").returncode == 0

    def test_updates_that_touch_neither_field_are_not_checked(self, sb):
        # 既に作れない形で書かれている card でも、skills / deliverable を触らない更新は通す
        tid = self._seed(sb, ["research"])
        text = sb.text(tid).replace("status: pending", "status: pending\ndeliverable: pr", 1)
        (sb.tasks / f"{tid}.md").write_text(text)
        assert sb.run("update", tid, "--mission", MISSION, "--priority", "low").returncode == 0


def _all_skills():
    cfg = yaml.safe_load(open(PERMS, encoding="utf-8"))
    return sorted(cfg["skills"])


def _lint_says_ok(skills, declared):
    meta = {"id": "t001", "skills": list(skills), "deliverable": declared}
    return not [m for lvl, _c, m in lint_plan.check_deliverable([meta], PERMS) if lvl == "FAIL"]


class TestLintAndAddAgree:
    def test_every_pair_of_skills_in_process(self):
        skills = _all_skills() + ["no-such-skill"]
        combos = [(s,) for s in skills] + list(itertools.combinations(skills, 2))
        for declared in ("pr", "file", "none"):
            for combo in combos:
                refusal = lint_plan.deliverable_refusal(list(combo), declared, PERMS)
                assert (refusal is None) == _lint_says_ok(combo, declared), (combo, declared, refusal)

    def test_plan_sh_add_matches_lint_for_every_single_skill_and_research_pairs(self, sb):
        skills = _all_skills()
        combos = [(s,) for s in skills] + [("research", s) for s in skills if s != "research"]
        for declared in ("pr", "file"):
            for combo in combos:
                r = _add(sb, list(combo), declared)
                assert (r.returncode == 0) == _lint_says_ok(combo, declared), (combo, declared, r.stderr)
                assert r.returncode in (0, 2), (combo, declared, r.stderr)


def test_plan_sh_has_no_copy_of_the_permission_rule():
    text = (REPO / "scripts" / "plan.sh").read_text(encoding="utf-8")
    assert "check_permission(" not in text
    assert "deliverable_refusal" in text
