"""t005: CI 取得の小物 (#377 P3-2/P3-3・#378 P3・#379 P3・Result の `## ` の扱い)。"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import lib_ci_status as ci  # noqa: E402
import lib_history  # noqa: E402
from task_graph_publisher_harness import load_plan_namespace  # noqa: E402

SHA = "a" * 40


def _runner(rc, stderr="", stdout=""):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)
    return run


def test_gh_pr_not_found_is_a_fixed_code_and_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("CREWVIA_CI_ALLOW_GH_IN_TESTS", "1")
    out, err = ci.run_gh(["pr", "view", "5"], cwd=str(tmp_path), timeout=5,
                         runner=_runner(1, "GraphQL: Could not resolve to a PullRequest with the number of 5."))
    assert (out, err) == (None, "gh_pr_not_found")
    _, err = ci.run_gh(["pr", "view", "5"], cwd=str(tmp_path), timeout=5, runner=_runner(1, "network down"))
    assert err == "gh_exit_1"           # 一過性の失敗は終端にしない
    rec = ci._view(5, lambda a: (None, "gh_pr_not_found"), 100.0)
    assert rec["pr_state"] == "NOT_FOUND" and rec["pr"] == 5 and rec["state"] == "no_pr"
    assert ci.record_problem(rec) is None
    assert ci.pr_closed(rec, 5)         # poll_cycle が取りに行かなくなる


def test_not_found_pr_is_never_notified():
    rec = ci._view(5, lambda a: (None, "gh_pr_not_found"), 100.0)
    assert rec["state"] != "failed" and rec["state"] != "unknown"


def test_dispatcher_main_poll_uses_mark_result_and_deadline_budget():
    src = (REPO / "scripts" / "dispatcher.sh").read_text(encoding="utf-8")
    assert "if not lib_ci_status.mark_main_polled(REGISTRY_DIR, now):" in src
    assert re.search(r"fetch_main_runs\(cwd=str\(REPO_ROOT\),\s*timeout=min\(lib_ci_status\.GH_TIMEOUT_SECONDS, left\)\)", src)


def test_worktree_gc_comment_points_at_a_real_heading():
    code = (REPO / "scripts" / "worktree_gc.py").read_text(encoding="utf-8")
    doc = (REPO / "knowledge" / "worktree-gc.md").read_text(encoding="utf-8")
    assert "「dbus-daemon と op daemon」" in code
    assert re.search(r"^#+ dbus-daemon と op daemon$", doc, re.MULTILINE)


def _ns(tmp_path):
    return load_plan_namespace(REPO / "scripts" / "plan.sh", str(tmp_path / "queue"), str(REPO))


def test_headings_in_a_result_are_demoted_so_approved_head_stays_inside(tmp_path):
    ns = _ns(tmp_path)
    result = f"## QA Gate\n- ok\n## 所見\nx\n\napproved_head: {SHA}"
    stored = ns["demote_section_headings"](result)
    assert "\n## " not in "\n" + stored.replace("### ", "")  # 行頭の ## は残らない
    body = ns["build_task_body"]("d", stored)
    assert ci.approved_head(lib_history.result_section(body)) == SHA
    # 対照: 下げないと approved_head は節の外に出る
    raw = ns["build_task_body"]("d", result)
    assert ci.approved_head(lib_history.result_section(raw)) is None
    # Description / Result の見出しは対象外・### 以下はそのまま
    assert ns["demote_section_headings"]("## Result\n### a\n##x") == "## Result\n### a\n##x"


def test_reopen_drops_leftover_sections_but_keeps_known_appendices(tmp_path):
    ns = _ns(tmp_path)
    old = ("## Description\nd\n\n## Result\nold\n## 前回の P2\n本文\n\n"
           "## Needs-Director 詳細\n詳細\n\n## Verification\nv\n")
    kept = ns["keep_known_appendices"](ns["extract_trailing_body_section"](old))
    assert "前回の P2" not in kept and "本文" not in kept
    assert "## Needs-Director 詳細\n詳細" in kept and "## Verification\nv" in kept
    assert ns["keep_known_appendices"]("") == ""


# -- コードフェンスの中の `## ` は触らない (t003 / #382 P3-1) ---------------------------------------------

FENCE = "`" * 3


def test_headings_inside_a_closed_fence_are_left_alone_and_approved_head_stays_inside(tmp_path):
    ns = _ns(tmp_path)
    result = f"## QA Gate\n{FENCE}md\n## 例\nx\n{FENCE}\n## 所見\ny\n\napproved_head: {SHA}"
    stored = ns["demote_section_headings"](result)
    assert f"{FENCE}md\n## 例\nx\n{FENCE}" in stored          # フェンスの中は元のまま
    assert "### QA Gate" in stored and "### 所見" in stored    # 外は下げる
    body = ns["build_task_body"]("d", stored)
    assert ci.approved_head(lib_history.result_section(body)) == SHA   # フェンスの中の ## で切れない
    # 対照: 読み手がフェンスを見ないと Result が切れて approved_head が外に出る (フェンスの中を下げない場合)
    lines = body.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln == "## Result") + 1
    naive_end = next(j for j in range(start, len(lines)) if lines[j].startswith("## "))
    assert "approved_head" not in "\n".join(lines[start:naive_end])


def test_unclosed_fence_is_not_a_fence_so_headings_after_it_are_still_demoted(tmp_path):
    ns = _ns(tmp_path)
    result = f"{FENCE}\n## 閉じ忘れの後ろ\nx\n\napproved_head: {SHA}"
    stored = ns["demote_section_headings"](result)
    assert "\n### 閉じ忘れの後ろ" in stored
    body = ns["build_task_body"]("d", stored)
    assert ci.approved_head(lib_history.result_section(body)) == SHA


def test_tilde_fence_and_longer_closer(tmp_path):
    ns = _ns(tmp_path)
    assert ns["demote_section_headings"]("~~~\n## a\n~~~\n## b") == "~~~\n## a\n~~~\n### b"
    four = "`" * 4
    text = f"{four}\n{FENCE}\n## a\n{four}\n## b"      # 4 本で開いたら 3 本では閉じない
    assert ns["demote_section_headings"](text) == f"{four}\n{FENCE}\n## a\n{four}\n### b"


def test_trailing_appendix_readers_skip_fenced_headings(tmp_path):
    ns = _ns(tmp_path)
    body = f"## Description\nd\n\n## Result\nr\n{FENCE}\n## 例\n{FENCE}\n\n## Needs-Director 詳細\n詳細\n"
    trailing = ns["extract_trailing_body_section"](body)
    assert trailing.startswith("## Needs-Director 詳細")
    kept = ns["keep_known_appendices"](f"## Verification\nv\n{FENCE}\n## 例\n{FENCE}\n")
    assert "## 例" in kept and kept.startswith("## Verification")
