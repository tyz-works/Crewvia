"""E5: 報告の関門の順序 (PR-2) と、kai-review.sh の空の `--execution` の拒否 (PR-1 / PR #287 Codex 2 巡目 P1)。

PR-2: 名乗りなし × active な試行の拒否 (`EXECUTION_REQUIRED`) は**遷移の検査より前**。遷移が不正な状態でも、名乗らない報告は
先に `EXECUTION_REQUIRED` で止まり (exit 3)、名乗れば遷移の拒否 (exit 2) が出る。どちらも何も書かない。
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


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001",))


def _to_verifying(box, xid):
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0


# 8 通りの報告 × 「遷移が拒否される」状態を作る関数
def _refused_in_progress_only(box, xid):          # done / fail / needs-director / ready-for-verification は in_progress だけ (fail は + needs_director)
    set_card(box, status="ready_for_verification")  # 試行は running のまま = ACTIVE (pending だと DETACHED で別の分岐)


def _refused_for_verify(box, xid):                # verifying / verify-result は検証待ちだけ
    pass                                           # in_progress のまま


CASES = [
    ("done", _refused_in_progress_only),
    ("fail", _refused_in_progress_only),
    ("needs-director", _refused_in_progress_only),
    ("ready-for-verification", _refused_in_progress_only),
    ("verifying", _refused_for_verify),
    ("verify-pass", _refused_for_verify),
    ("verify-fail", _refused_for_verify),
    ("verify-nhr", _refused_for_verify),
]


@pytest.mark.parametrize("command,refuse", CASES, ids=[c[0] for c in CASES])
def test_the_claim_gate_comes_before_the_transition_check(box, command, refuse):
    xid = take(box)
    refuse(box, xid)
    before = box.snapshot()
    agent = "V1" if command.startswith("verif") else "Ren"
    unnamed = run(box, *report_argv(command), agent=agent)
    assert unnamed.returncode == 3 and last_error_code(unnamed.stderr) == "EXECUTION_REQUIRED", (unnamed.stdout, unnamed.stderr)
    assert box.snapshot() == before
    named = run(box, *report_argv(command), "--execution", xid, agent=agent)
    assert named.returncode == 2 and last_error_code(named.stderr) == "INVALID_TRANSITION", (named.stdout, named.stderr)
    assert box.snapshot() == before                                         # どちらの拒否も何も書かない


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


# ---------------------------------------------------------------------------
# t012 (PR #287 Codex 3 巡目 P1): 値の欠落が次のオプションを食わない・ID の形でない値は副作用の前に拒否
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    ["--execution", "--dry-run"],                         # 値が無く後続オプション: --dry-run を失って本物の pull をしていた
    ["--execution", "--skip-pull"],
    ["--execution", "-x"],
    ["--skip-pull", "--execution", "-x"],
    ["--execution", "foo"],                               # 形が違う
    ["--execution", "ex-" + "b" * 31],                    # 桁が足りない
    ["--execution", "ex-" + "B" * 32],                    # 大文字
    ["--execution", XID + "0"],                           # 余分
    ["--execution", f" {XID}"],
], ids=["then-dry-run", "then-skip-pull", "then-dash-x", "skip-then-dash-x", "foo", "short", "upper", "long", "leading-space"])
def test_a_missing_or_malformed_execution_value_is_refused_before_any_side_effect(tmp_path, extra):
    p, calls = run_kai(tmp_path, extra)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert "--execution" in p.stderr or "Unknown option" in p.stderr   # -x は値として食わず、未知のオプションとして拒否される
    assert calls == "", calls                              # pull / done / needs-director の plan.sh 呼び出しが 0 回
    assert XID not in p.stdout + p.stderr                  # card を読んで採用していない


def test_a_well_formed_execution_value_is_still_accepted(tmp_path):
    other = "ex-" + "c" * 32
    p, calls = run_kai(tmp_path, ["--skip-pull", "--execution", other])
    assert f"--execution {other}" in calls, (calls, p.stderr)
