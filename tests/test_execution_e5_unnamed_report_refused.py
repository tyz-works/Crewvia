"""E5 の呼び出し側の支え: 名乗りなしの報告の拒否 (PR-2) と、名乗りを促す指示文 (PR-1)。

設計: `knowledge/execution.md` §20.2・§20.3・§20.4・§20.10。受入条件:

1. 名乗りなし × active な試行 × 報告 (done / fail / needs-director / ready-for-verification / verifying / verify-result)
   → exit 3・`error_code=EXECUTION_REQUIRED`。**何も書かない** (queue 全体のバイト列が不変・監査は `refused:EXECUTION_REQUIRED` の 1 行)
2. 名乗れば今までどおり通る (flag でも env でも)。警告は無い (PR-1 の警告は PR-2 で拒否に置き換わった)
3. 対象外は通る: 試行なしの card・`update --reset` / Director の回復
4. pull は stderr に「報告には --execution ex-… を付ける」を 1 行足す。stdout の JSON は変えない
5. dispatcher の割り当て文・start.sh の起動文が `--execution` に触れる
6. `kai-review.sh --skip-pull` は card の current_execution_id を読んで名乗る (`--execution` で上書きもできる。読めなければ警告)

**本番の queue / registry / mux には触れない** (`execution_e3_helpers` が `Box` で隔離する)。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import (MISSION, Box, audit_rows_for, last_error_code, legacy_in_progress, refusals,
                                  report_argv, run, set_card, take)

REPO_ROOT = Path(__file__).resolve().parent.parent
OLD_WARNING = "名乗りなしで報告しました"                                  # PR-1 の警告 (PR-2 で消えた)
OUTCOME = {
    "done": ("completed", "DONE", "done"),
    "fail": ("failed", "WORKER_FAILED", "failed"),
    "needs-director": ("failed", "NEEDS_DIRECTOR", "needs_director"),
    "ready-for-verification": ("running", None, "ready_for_verification"),
}
REPORTS = list(OUTCOME)


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path / "root", tasks=("t001", "t002", "t003"))


def card_outcome(box, tid="t001"):
    m = box.card(tid)
    return m["execution_status"], m.get("execution_end_code"), m["status"]


# ---------------------------------------------------------------------------
# 1〜2. 名乗りなしは拒否 (何も書かない) / 名乗れば通る
# ---------------------------------------------------------------------------

def assert_required_refusal(box, p, command, before):
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert last_error_code(p.stderr) == "EXECUTION_REQUIRED", p.stderr
    assert "--execution" in p.stderr and OLD_WARNING not in p.stderr
    assert box.snapshot() == before                                       # card・record・枠・identity は 1 バイトも変わらない
    rows = refusals(box, command)
    assert len(rows) == 1 and rows[0]["result"] == "refused:EXECUTION_REQUIRED", rows


@pytest.mark.parametrize("command", REPORTS)
def test_unnamed_report_on_an_active_attempt_is_refused_and_writes_nothing(box, command):
    take(box)
    before = box.snapshot()
    p = run(box, *report_argv(command))
    assert_required_refusal(box, p, command, before)
    assert card_outcome(box) == ("running", None, "in_progress")


@pytest.mark.parametrize("command", REPORTS)
def test_naming_the_attempt_passes_without_a_warning(box, command):
    xid = take(box)
    p = run(box, *report_argv(command), "--execution", xid)
    assert p.returncode == 0, p.stderr
    assert OLD_WARNING not in p.stderr and "EXECUTION_REQUIRED" not in p.stderr
    assert card_outcome(box) == OUTCOME[command]
    assert [r["caller_check"] for r in audit_rows_for(box, command)] == ["verified"]


@pytest.mark.parametrize("command", REPORTS)
def test_the_environment_variable_counts_as_naming(box, command):
    xid = take(box)
    p = run(box, *report_argv(command), env={"CREWVIA_EXECUTION_ID": xid})
    assert p.returncode == 0, p.stderr
    assert OLD_WARNING not in p.stderr


def test_the_refusal_names_identifiers_only(box):
    """拒否文は固定文言 + slug/task id だけ。card の本文・他の欄を出さない (secret を仕込んで確かめる)。"""
    take(box)
    set_card(box, title=e3.SECRET)
    p = run(box, *report_argv("done"))
    assert p.returncode == 3 and e3.SECRET not in p.stdout + p.stderr
    assert f"{MISSION}/t001" in p.stderr
    assert all(e3.SECRET not in json.dumps(r) for r in box.audit_rows())


def test_verify_result_unnamed_is_refused_and_named_passes(box):
    xid = take(box)
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    assert run(box, *report_argv("verifying"), "--execution", xid, agent="verifier-dispatcher").returncode == 0
    before = box.snapshot()
    for verdict in ("verify-pass", "verify-fail", "verify-nhr"):
        assert_required_refusal_for(box, run(box, *report_argv(verdict), agent="V1"), before)
    p = run(box, *report_argv("verify-pass"), "--execution", xid, agent="V1")
    assert p.returncode == 0, p.stderr
    assert box.card()["status"] == "verified"


def assert_required_refusal_for(box, p, before):
    assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_REQUIRED", (p.stdout, p.stderr)
    assert box.snapshot() == before


def test_verifying_unnamed_is_refused(box):
    xid = take(box)
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    before = box.snapshot()
    p = run(box, *report_argv("verifying"), agent="verifier-dispatcher")
    assert_required_refusal(box, p, "verifying", before)
    assert box.card()["status"] == "ready_for_verification"


def test_the_director_is_not_exempt(box):
    """§20.5 の決定 3: Director の done / fail / needs-director も名乗りが要る (例外経路を作らない)。"""
    take(box)
    before = box.snapshot()
    for command in ("done", "fail", "needs-director"):
        p = run(box, *report_argv(command), agent="Sora")
        assert p.returncode == 3 and last_error_code(p.stderr) == "EXECUTION_REQUIRED", (command, p.stderr)
    assert box.snapshot() == before


# ---------------------------------------------------------------------------
# 3. 対象外 (通る)
# ---------------------------------------------------------------------------

def test_a_card_without_an_attempt_still_passes_unnamed(box):
    legacy_in_progress(box, "t002", "Ren")
    p = run(box, *report_argv("done", "t002"))
    assert p.returncode == 0, p.stderr
    assert box.card("t002")["status"] == "done"


def test_reset_and_the_director_recovery_still_pass_unnamed(box):
    take(box)
    p = run(box, "update", "t001", "--status", "pending", "--reset", "--mission", MISSION, agent="Sora")
    assert p.returncode == 0, p.stderr
    assert "EXECUTION_REQUIRED" not in p.stderr
    # 開き直した card への Director の done は名乗りなしで通る (試行は閉じている)
    assert run(box, "update", "t001", "--status", "in_progress", "--reset", "--mission", MISSION, agent="Sora").returncode == 0
    done = run(box, *report_argv("done"), agent="Sora")
    assert done.returncode == 0, done.stderr


def test_a_transition_refusal_after_the_claim_check_keeps_its_own_code(box):
    """名乗りなし + active の拒否は遷移の検査より前。名乗れば遷移の拒否 (exit 2) が出る。"""
    xid = take(box)
    set_card(box, status="pending")
    p = run(box, *report_argv("done"), "--execution", xid)
    assert p.returncode in (2, 3), p.stderr
    assert "EXECUTION_REQUIRED" not in p.stderr


# ---------------------------------------------------------------------------
# 5. pull の stderr
# ---------------------------------------------------------------------------

def test_pull_prints_a_pasteable_hint_on_stderr_and_keeps_the_json_clean(box):
    p = box.pull("Ren", "t001")
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)                                           # stdout は JSON だけ
    xid = data["execution_id"]
    hints = [ln for ln in p.stderr.splitlines() if "報告には --execution" in ln]
    assert hints == [f"[plan.sh] 報告には --execution {xid} を付ける (done / fail / needs-director / ready-for-verification)"]
    assert "報告には" not in p.stdout


# ---------------------------------------------------------------------------
# 6. 指示文 (dispatcher の割り当て文・start.sh の起動文)
# ---------------------------------------------------------------------------

def _dispatcher_assignment_text():
    src = (REPO_ROOT / "scripts" / "dispatcher.sh").read_text(encoding="utf-8")
    i = src.index('f"タスク {task_id} (mission={slug}) を実行して。"')
    j = src.index("if tmux_send(target, msg):", i)
    return src[i:j]


def test_dispatcher_assignment_message_tells_the_worker_to_name_the_attempt():
    text = _dispatcher_assignment_text()
    assert "--execution" in text and "execution_id" in text
    assert "plan pull --task {task_id} --mission {slug}" in text          # 元の指示は残す


def test_start_sh_kickoff_messages_tell_the_worker_to_name_the_attempt():
    lines = [ln for ln in (REPO_ROOT / "scripts" / "start.sh").read_text(encoding="utf-8").splitlines()
             if ln.lstrip().startswith("KICKOFF_MSG=") and "plan pull --agent" in ln]
    assert len(lines) == 2, lines                                         # target_dir 版と通常版
    for ln in lines:
        assert "--execution" in ln and "execution_id" in ln, ln


# ---------------------------------------------------------------------------
# 7. kai-review.sh --skip-pull
# ---------------------------------------------------------------------------

KAI_REVIEW = REPO_ROOT / "scripts" / "kai-review.sh"
PLAN_STUB = """#!/bin/bash
case "$1" in
  resolve-mission) echo m-test ;;
  pull) echo "pull must not be called" >> "$CALLS_FILE" ;;
  *) echo "$@" >> "$CALLS_FILE" ;;
esac
exit 0
"""
SKIP_WARN = "--skip-pull: 今の試行の execution_id を読めませんでした"


def _exe(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_skip_pull(tmp_path, card_fields, extra=()):
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
    p = subprocess.run(["bash", str(KAI_REVIEW), "--pr", "1", "--task", "t001", "--mission", "m-test", "--skip-pull", *extra],
                       env=env, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    calls = (tmp_path / "calls.txt").read_text() if (tmp_path / "calls.txt").exists() else ""
    return p, calls


XID = "ex-" + "b" * 32


def test_skip_pull_names_the_attempt_the_card_holds(tmp_path):
    p, calls = run_skip_pull(tmp_path, f"current_execution_id: {XID}\nexecution_status: running\n")
    assert SKIP_WARN not in p.stderr, p.stderr
    assert f"--execution {XID}" in calls and "pull must not be called" not in calls, (calls, p.stderr)


def test_skip_pull_with_a_reserved_attempt_names_it_too(tmp_path):
    p, calls = run_skip_pull(tmp_path, f"current_execution_id: {XID}\nexecution_status: reserved\n")
    assert f"--execution {XID}" in calls, (calls, p.stderr)


@pytest.mark.parametrize("fields", [
    "",                                                                                     # 試行の欄なし (旧形式)
    f"current_execution_id: {XID}\nexecution_status: completed\n",                          # 終わった試行は名乗らない
    "current_execution_id: not-an-id\nexecution_status: running\n",                         # 形が違う
])
def test_skip_pull_without_an_active_attempt_warns_and_reports_unnamed(tmp_path, fields):
    p, calls = run_skip_pull(tmp_path, fields)
    assert p.stderr.count(SKIP_WARN) == 1, p.stderr
    assert "--execution" not in calls and calls, (calls, p.stderr)         # 報告はする (名乗りなしで)


def test_skip_pull_accepts_an_explicit_execution_id(tmp_path):
    other = "ex-" + "c" * 32
    p, calls = run_skip_pull(tmp_path, "", extra=("--execution", other))
    assert SKIP_WARN not in p.stderr
    assert f"--execution {other}" in calls, (calls, p.stderr)


def test_skip_pull_does_not_adopt_an_attempt_held_by_another_worker(tmp_path):
    """t009 (P1): card の worker が自分でなければ、その試行 (置き換えの試行) を名乗らない。"""
    p, calls = run_skip_pull(tmp_path, f"current_execution_id: {XID}\nexecution_status: running\n", extra=("--agent", "Someone-else"))
    assert p.stderr.count(SKIP_WARN) == 1, p.stderr
    assert XID not in calls and calls, (calls, p.stderr)
