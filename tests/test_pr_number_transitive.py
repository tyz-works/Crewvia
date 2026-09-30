#!/usr/bin/env python3
"""PR 番号の推移的な伝播 (t017 / backlog #29)。

`plan.sh done --pr` は、直接の依存先だけでなく、`deliverable` が `pr` でないと明示
宣言された task (`file` / `none`) を通過した先の codex-review / review にも
`pr_number` を届ける。通過するのは宣言が `file` / `none` の task だけで、
`deliverable: pr` の task (別の PR) と、宣言の無い task ("従来どおり直接依存だけ" の
後方互換) で止まる。

上流に `deliverable: pr` の task が 2 つ以上ある合流点 (例: 複数 PR の merge 後に
走る本番確認 task) には書かない —— どの PR の番号か一意に決まらないため。

付け忘れの拒否 (`--pr` も `--no-pr` も無い done を断る) も、実際に番号が届く対象と
同じ規則で推移的に数える (PR7 との整合)。

    python3 -m pytest tests/test_pr_number_transitive.py -v
"""

from __future__ import annotations

import pathlib
import sys

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from test_assignment_routing import MISSION, Sandbox, _done, _pr_line, _status  # noqa: E402


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


# ---------------------------------------------------------------------------
# 推移的な伝播
# ---------------------------------------------------------------------------

class TestTransitivePropagation:
    def test_reaches_a_review_task_past_a_non_pr_qa_and_codex_review(self, sb):
        """実装 (pr) → QA (none) + codex-review (none) → merge (review, none) の形で、
        merge task は t001 の直接の依存先ではないが番号が届く (backlog #29 の本題)。"""
        sb.card("t001", status="in_progress", worker="Ren", extra=["deliverable: pr"])
        sb.card("t002", skills="[qa]", blocked_by="[t001]", extra=["deliverable: none"])
        sb.card("t003", skills="[codex-review]", blocked_by="[t001]", status="blocked",
                extra=["deliverable: none", 'blocked_reason: "PR 番号待ち"'])
        sb.card("t004", skills="[review]", blocked_by="[t002, t003]", extra=["deliverable: none"])
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t003") == "pr_number: 231" and _status(sb, "t003") == "pending"
        assert _pr_line(sb, "t004") == "pr_number: 231", "推移的な review task にも届く"

    def test_stops_at_another_pr_producing_task(self, sb):
        """次の PR の実装 task (deliverable: pr) で通過が止まり、その先の codex-review
        には書かれない (別の PR を作る task で止まる)。"""
        sb.card("t001", status="in_progress", worker="Ren", extra=["deliverable: pr"])
        sb.card("t002", skills="[review]", blocked_by="[t001]", extra=["deliverable: none"])
        sb.card("t003", skills="[code]", blocked_by="[t002]", extra=["deliverable: pr"])
        sb.card("t004", skills="[codex-review]", blocked_by="[t003]", status="blocked",
                extra=["deliverable: none"])
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t002") == "pr_number: 231"
        assert _pr_line(sb, "t004") is None, "別の PR を作る t003 で止まる"
        assert _status(sb, "t004") == "blocked"

    def test_does_not_pass_through_an_undeclared_task(self, sb):
        """`deliverable` を宣言していない task はそこで止まる (従来どおり直接依存だけ)。"""
        sb.card("t001", status="in_progress", worker="Ren", extra=["deliverable: pr"])
        sb.card("t002", skills="[review]", blocked_by="[t001]")  # 宣言なし
        sb.card("t003", skills="[codex-review]", blocked_by="[t002]", status="blocked")
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t002") == "pr_number: 231", "直接の依存先は宣言がなくても届く"
        assert _pr_line(sb, "t003") is None, "宣言の無い t002 は通過しない"

    def test_a_deeper_chain_of_non_pr_tasks_still_propagates(self, sb):
        """複数段の file / none を通過して、さらに先まで届く。"""
        sb.card("t001", status="in_progress", worker="Ren", extra=["deliverable: pr"])
        sb.card("t002", skills="[qa]", blocked_by="[t001]", extra=["deliverable: none"])
        sb.card("t003", skills="[qa]", blocked_by="[t002]", extra=["deliverable: file"])
        sb.card("t004", skills="[review]", blocked_by="[t003]", extra=["deliverable: none"])
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t004") == "pr_number: 231"

    def test_an_existing_pr_number_on_a_transitive_target_is_not_overwritten(self, sb):
        """S4 (t016) で変わった: 伝播先に**違う**番号が既にあるとき、以前は黙って飛ばして done を通した
        (正本は 231・レビュー対象は 100 で確定)。今は done を拒否する (exit 3・何も書かない。設計 §2.2 D0 (a))。
        どちらの番号が正しいかは自動では決めず、Director が伝播先を直してから再度 done する。
        「上書きしない」は変わらない (拒否するので 100 は 100 のまま)。"""
        sb.card("t001", status="in_progress", worker="Ren", extra=["deliverable: pr"])
        sb.card("t002", skills="[qa]", blocked_by="[t001]", extra=["deliverable: none"])
        sb.card("t003", skills="[review]", blocked_by="[t002]",
                extra=["deliverable: none", "pr_number: 100"])
        before = sb.snapshot()
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 3, (r.stdout, r.stderr)
        assert "t003" in r.stderr and "pr_number=100" in r.stderr
        assert "update t003 --pr-number 231" in r.stderr, "出口をメッセージに出す"
        assert sb.snapshot() == before and _pr_line(sb, "t003") == "pr_number: 100"
        # 出口: 依存先を正しい番号に直せば通る
        assert sb.run("update", "t003", "--pr-number", "231", "--mission", MISSION).returncode == 0
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t003") == "pr_number: 231"

    def test_the_same_number_on_a_transitive_target_is_not_a_conflict(self, sb):
        """再実行 (前の done が伝播の途中で落ちた) で、依存先が既に**同じ**番号を持つのは食い違いではない。"""
        sb.card("t001", status="in_progress", worker="Ren", extra=["deliverable: pr"])
        sb.card("t002", skills="[qa]", blocked_by="[t001]", extra=["deliverable: none"])
        sb.card("t003", skills="[review]", blocked_by="[t002]",
                extra=["deliverable: none", "pr_number: 231"])
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t003") == "pr_number: 231"


class TestCorruptCardBreaksTheChain:
    def test_a_corrupt_card_is_left_alone_and_does_not_propagate_through(self, sb):
        sb.card("t001", status="in_progress", worker="Ren", extra=["deliverable: pr"])
        sb.card("t002", skills="[review]", blocked_by="[t001]", extra=["deliverable: none"])
        (sb.tasks / "t002.md").write_text("not a card at all")
        sb.card("t003", skills="[review]", blocked_by="[t002]", extra=["deliverable: none"])
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert (sb.tasks / "t002.md").read_text() == "not a card at all"
        assert _pr_line(sb, "t003") is None, "壊れた t002 の先には伝わらない"


# ---------------------------------------------------------------------------
# 合流点 (上流に deliverable: pr の task が 2 つ以上)
# ---------------------------------------------------------------------------

class TestConfluencePoint:
    def _two_prs_converging(self, sb, target_skill="review", target_status="pending"):
        sb.card("t001", status="in_progress", worker="Ren", extra=["deliverable: pr"])
        sb.card("t002", skills="[review]", blocked_by="[t001]", extra=["deliverable: none"])
        sb.card("t010", status="pending", extra=["deliverable: pr"])
        sb.card("t011", skills="[review]", blocked_by="[t010]", extra=["deliverable: none"])
        sb.card("t020", skills=f"[{target_skill}]", blocked_by="[t002, t011]",
                status=target_status, extra=["deliverable: none"])

    def test_a_confluence_of_two_pr_ancestors_is_not_written(self, sb):
        self._two_prs_converging(sb)
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t020") is None
        assert "合流点" in r.stderr and "t010" in r.stderr

    def test_confluence_does_not_force_pr_on_the_upstream_done(self, sb):
        """t020 は 2 つの別 PR (t090 / t010) 由来の合流点。無関係な t001 の done は
        `--pr` を付けなくても t020 待ちを理由に拒否されない。

        t001 自身は `deliverable` を宣言しない (宣言すると別の「自分自身の宣言」チェックが
        --pr を要求してしまい、ここで確かめたい「合流点は待っているうちに数えない」を
        切り分けられない)。合流の 2 本は t090 (既に done 済みの別 PR) と t010。
        """
        sb.card("t090", status="done", extra=["deliverable: pr", "pr_number: 90"])
        sb.card("t001", status="in_progress", worker="Ren")
        sb.card("t002", skills="[review]", blocked_by="[t001, t090]", extra=["deliverable: none"])
        sb.card("t010", status="pending", extra=["deliverable: pr"])
        sb.card("t011", skills="[review]", blocked_by="[t010]", extra=["deliverable: none"])
        sb.card("t020", skills="[codex-review]", blocked_by="[t002, t011]", status="blocked",
                extra=["deliverable: none"])
        r = sb.run("done", "t001", "finished", "--mission", MISSION)
        assert r.returncode == 0, (r.stdout, r.stderr)

    def test_direct_dependent_still_wins_over_a_further_confluence(self, sb):
        """t020 が合流点でも、直接依存・非合流の t002 には従来どおり書かれる。"""
        self._two_prs_converging(sb, target_skill="codex-review", target_status="blocked")
        sb.card("t002", skills="[codex-review]", blocked_by="[t001]", status="blocked",
                extra=["deliverable: none"])
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t002") == "pr_number: 231" and _status(sb, "t002") == "pending"
        assert _pr_line(sb, "t020") is None


# ---------------------------------------------------------------------------
# 付け忘れの拒否 (PR7) の推移的な整合
# ---------------------------------------------------------------------------

class TestTransitiveAwaitingRefusal:
    def _waiting_transitively(self, sb, *, status="blocked"):
        sb.card("t001", status="in_progress", worker="Ren")
        sb.card("t002", skills="[qa]", blocked_by="[t001]", extra=["deliverable: none"])
        sb.card("t003", skills="[codex-review]", blocked_by="[t002]", status=status,
                extra=["deliverable: none"])

    def test_refuses_when_a_transitive_codex_review_still_waits(self, sb):
        self._waiting_transitively(sb)
        before = sb.snapshot()
        r = sb.run("done", "t001", "finished", "--mission", MISSION)
        assert r.returncode == 2, (r.stdout, r.stderr)
        assert sb.snapshot() == before, "拒否は何も書かない"
        assert "t003" in r.stderr

    def test_pr_resolves_the_transitive_refusal(self, sb):
        self._waiting_transitively(sb)
        r = sb.run("done", "t001", "finished", "--pr", "231", "--mission", MISSION)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t003") == "pr_number: 231" and _status(sb, "t003") == "pending"

    def test_no_pr_still_waives_the_transitive_wait(self, sb):
        self._waiting_transitively(sb)
        r = sb.run("done", "t001", "finished", "--no-pr", "調査 task", "--mission", MISSION)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t003") is None and _status(sb, "t003") == "blocked"

    def test_an_indirect_wait_through_an_undeclared_task_does_not_count(self, sb):
        """宣言の無い中継点は通過しないので、その先の codex-review 待ちは数えない
        (既存の直接依存版の後方互換と同じ判断)。"""
        sb.card("t001", status="in_progress", worker="Ren")
        sb.card("t002", skills="[qa]", blocked_by="[t001]")  # 宣言なし
        sb.card("t003", skills="[codex-review]", blocked_by="[t002]", status="blocked")
        r = sb.run("done", "t001", "finished", "--mission", MISSION)
        assert r.returncode == 0, (r.stdout, r.stderr)
