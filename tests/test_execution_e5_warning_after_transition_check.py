"""E5 PR-1 (t011 / PR #287 Codex 2 巡目): 名乗りなしの警告は**遷移の検査が通った後**にだけ出す (P2)。
kai-review.sh の空の `--execution` は「省略」ではなく拒否 (P1)。

P2: 以前は `_authorize(warn=True)` が `_check_task_status` より前で警告を出していたので、遷移が拒否される名乗りなしの報告
(例: in_progress でない task への done) でも「名乗りなしで報告しました」が出て、通ったように読めた。
P1: `--skip-pull --execution "$X"` の $X が空だと「フラグ省略」と区別がつかず、card の今の試行を採用して名乗ってしまった。

**本番の queue / registry / mux には触れない** (`execution_e3_helpers` の `Box`・偽の plan.sh / gh / codex)。
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from execution_e3_helpers import Box, last_error_code, report_argv, run, set_card, take

REPO_ROOT = Path(__file__).resolve().parent.parent
KAI_REVIEW = REPO_ROOT / "scripts" / "kai-review.sh"
MARK = "名乗りなしで報告しました"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def warnings_in(stderr):
    return [ln for ln in stderr.splitlines() if MARK in ln]


def _to_verifying(box, xid):
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0


# 6 コマンド (verify-result は 3 つの verdict) × 「遷移が拒否される」状態を作る関数 × 「通る」状態を作る関数
def _refused_in_progress_only(box, xid):          # done / fail / needs-director / ready-for-verification は in_progress だけ (fail は + needs_director)
    set_card(box, status="ready_for_verification")  # 試行は running のまま = ACTIVE (pending だと DETACHED で別の分岐)


def _refused_for_verify(box, xid):                # verifying / verify-result は検証待ちだけ
    pass                                           # in_progress のまま


def _ok_nothing(box, xid):
    pass


def _ok_ready(box, xid):
    _to_verifying(box, xid)


CASES = [
    ("done", _refused_in_progress_only, _ok_nothing),
    ("fail", _refused_in_progress_only, _ok_nothing),
    ("needs-director", _refused_in_progress_only, _ok_nothing),
    ("ready-for-verification", _refused_in_progress_only, _ok_nothing),
    ("verifying", _refused_for_verify, _ok_ready),
    ("verify-pass", _refused_for_verify, _ok_ready),
    ("verify-fail", _refused_for_verify, _ok_ready),
    ("verify-nhr", _refused_for_verify, _ok_ready),
]


@pytest.mark.parametrize("command,refuse,ok", CASES, ids=[c[0] for c in CASES])
def test_an_unnamed_report_refused_on_the_transition_does_not_warn(box, command, refuse, ok):
    xid = take(box)
    refuse(box, xid)
    before = box.snapshot()
    p = run(box, *report_argv(command), agent="V1" if command.startswith("verif") else "Ren")
    assert p.returncode == 2, (p.stdout, p.stderr)                          # 遷移の拒否 (前提の確認)
    assert last_error_code(p.stderr) == "INVALID_TRANSITION", p.stderr
    assert warnings_in(p.stderr) == [], p.stderr
    assert box.snapshot() == before                                         # 拒否は何も書かない


@pytest.mark.parametrize("command,refuse,ok", CASES, ids=[c[0] for c in CASES])
def test_an_unnamed_report_that_passes_the_transition_warns_once(box, command, refuse, ok):
    xid = take(box)
    ok(box, xid)
    p = run(box, *report_argv(command), agent="V1" if command.startswith("verif") else "Ren")
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert len(warnings_in(p.stderr)) == 1, p.stderr


def test_verify_result_needs_human_review_by_rework_cap_warns_after_the_check(box):
    """verify-result の mark_task 経路 (needs_human_review) も同じ: 検証待ちでない task では出さない。"""
    take(box)
    p = run(box, *report_argv("verify-nhr"), agent="V1")
    assert p.returncode != 0 and warnings_in(p.stderr) == [], p.stderr


# ---------------------------------------------------------------------------
# P1: kai-review.sh の空の --execution
# ---------------------------------------------------------------------------

XID = "ex-" + "b" * 32
PLAN_STUB = """#!/bin/bash
case "$1" in
  resolve-mission) echo m-test ;;
  *) echo "$@" >> "$CALLS_FILE" ;;
esac
exit 0
"""


def _exe(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_kai(tmp_path, extra, card_fields=f"current_execution_id: {XID}\nexecution_status: running\n"):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    _exe(root / "scripts" / "plan.sh", PLAN_STUB)
    tasks = tmp_path / "queue" / "missions" / "m-test" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "t001.md").write_text("---\nid: t001\ntitle: x\nstatus: in_progress\nworker: Kai-codex\n"
                                   + card_fields + "---\n\n## Result\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "gh", "#!/bin/bash\nexit 1\n")
    _exe(bindir / "codex", "#!/bin/bash\nexit 1\n")
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_NAME", "CREWVIA_EXECUTION_ID")}
    env.update({"CREWVIA_REPO_ROOT": str(root), "CREWVIA_QUEUE": str(tmp_path / "queue"),
                "CALLS_FILE": str(tmp_path / "calls.txt"), "PATH": f"{bindir}:{env['PATH']}"})
    p = subprocess.run(["bash", str(KAI_REVIEW), "--pr", "1", "--task", "t001", "--mission", "m-test", *extra],
                       env=env, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    calls = (tmp_path / "calls.txt").read_text() if (tmp_path / "calls.txt").exists() else ""
    return p, calls


@pytest.mark.parametrize("extra", [
    ["--skip-pull", "--execution", ""],
    ["--execution", "", "--skip-pull"],
    ["--skip-pull", "--execution"],                       # 値が無い (末尾)
    ["--execution", ""],                                  # --skip-pull なしでも同じ入口で拒否
], ids=["skip-then-empty", "empty-then-skip", "no-value", "empty-without-skip"])
def test_an_empty_execution_flag_is_refused_before_anything_is_read_or_reported(tmp_path, extra):
    p, calls = run_kai(tmp_path, extra)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert "--execution" in p.stderr
    assert calls == "", calls                              # done / needs-director / pull を打たない
    assert XID not in p.stdout + p.stderr                  # card の試行を採用していない (読んでいない)


def test_an_unset_variable_expands_to_the_same_refusal(tmp_path):
    p, calls = run_kai(tmp_path, ["--skip-pull", "--execution", os.environ.get("CREWVIA_T011_UNSET_VAR", "")])
    assert p.returncode == 1 and calls == "", (p.stderr, calls)


def test_omitting_the_flag_still_reads_the_card(tmp_path):
    p, calls = run_kai(tmp_path, ["--skip-pull"])
    assert f"--execution {XID}" in calls, (calls, p.stderr)
