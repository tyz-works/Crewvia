"""E5 PR-1 (t005): 名乗りなしの報告に警告を出す。**plan.sh の判定 (通す / 拒否) は変えない**。

設計: `knowledge/execution.md` §20.2・§20.4 の 2 / 3 / 5・§20.5 の決定 2 (警告は stderr のみ)。受入条件:

1. 名乗りなし × active な試行 × 報告 (done / fail / needs-director / ready-for-verification / verify-result) → stderr に固定の 1 行。exit 0
2. 名乗れば出ない
3. **出ても書き込みは今と同じ**: card・record・監査の行・queue 全体が、警告の有無以外では変わらない (名乗りなしの結果 == E3 の結果)
4. 警告が出ない場面: 試行なしの card・DETACHED・`update --reset` / `retire` (対象外)
5. pull は stderr に「報告には --execution ex-… を付ける」を 1 行足す。stdout の JSON は変えない
6. dispatcher の割り当て文・start.sh の起動文が `--execution` に触れる
7. `kai-review.sh --skip-pull` は card の current_execution_id を読んで名乗る (`--execution` で上書きもできる。読めなければ警告)

**本番の queue / registry / mux には触れない** (`execution_e3_helpers` が `Box` で隔離する)。
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

import execution_e3_helpers as e3
from execution_e3_helpers import (MISSION, Box, audit_rows_for, legacy_in_progress, report_argv, run, set_card, take)

REPO_ROOT = Path(__file__).resolve().parent.parent
WARNING = ("[plan.sh] 名乗りなしで報告しました (execution id を --execution で渡してください)。"
           "将来は拒否されます")
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


def warnings_in(stderr):
    return [ln for ln in stderr.splitlines() if "名乗りなしで報告しました" in ln]


# ---------------------------------------------------------------------------
# 1〜3. 警告が出る / 名乗れば出ない / 出ても結果は同じ
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", REPORTS)
def test_unnamed_report_on_an_active_attempt_warns_once_and_still_passes(box, command):
    take(box)
    p = run(box, *report_argv(command))
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert warnings_in(p.stderr) == [WARNING], p.stderr
    assert card_outcome(box) == OUTCOME[command]
    assert [r["caller_check"] for r in audit_rows_for(box, command)] == ["unverified"]


@pytest.mark.parametrize("command", REPORTS)
def test_naming_the_attempt_does_not_warn(box, command):
    xid = take(box)
    p = run(box, *report_argv(command), "--execution", xid)
    assert p.returncode == 0, p.stderr
    assert warnings_in(p.stderr) == []
    assert [r["caller_check"] for r in audit_rows_for(box, command)] == ["verified"]


@pytest.mark.parametrize("command", REPORTS)
def test_the_environment_variable_counts_as_naming(box, command):
    xid = take(box)
    p = run(box, *report_argv(command), env={"CREWVIA_EXECUTION_ID": xid})
    assert p.returncode == 0, p.stderr
    assert warnings_in(p.stderr) == []


def _normalize(box, root):
    """queue のバイト列から、名乗りの有無で変わってよい物 (試行 ID・時刻) を落とす。"""
    out = {}
    for path, data in sorted(box.snapshot().items()):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        text = re.sub(r"ex-[0-9a-f]{32}", "ex-X", text).replace(str(root), "ROOT")
        text = re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:?\d\d)?", "T", text)
        out[re.sub(r"ex-[0-9a-f]{32}", "ex-X", str(path))] = text
    return out


@pytest.mark.parametrize("command", REPORTS)
def test_the_warning_changes_nothing_but_stderr(tmp_path, command):
    """同じ場面を 2 つ作り、片方は名乗り・片方は名乗りなしで報告する。queue は (ID・時刻を除いて) 同じ。
    差は監査の `caller_check` だけ (verified / unverified)。"""
    a = Box(tmp_path / "a", tasks=("t001",))
    b = Box(tmp_path / "b", tasks=("t001",))
    xa, xb = take(a), take(b)
    pa = run(a, *report_argv(command), "--execution", xa)
    pb = run(b, *report_argv(command))
    assert pa.returncode == pb.returncode == 0
    assert pa.stdout.replace(xa, "ex-X") == pb.stdout.replace(xb, "ex-X")
    assert _normalize(a, tmp_path / "a") == _normalize(b, tmp_path / "b")
    assert len(audit_rows_for(a)) == len(audit_rows_for(b))


def test_verify_result_unnamed_warns_and_named_does_not(box):
    xid = take(box)
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    assert run(box, *report_argv("verifying"), "--execution", xid, agent="verifier-dispatcher").returncode == 0
    p = run(box, *report_argv("verify-pass"), agent="V1")
    assert p.returncode == 0, p.stderr
    assert warnings_in(p.stderr) == [WARNING]
    assert box.card()["status"] == "verified"


def test_verifying_unnamed_warns(box):
    xid = take(box)
    assert run(box, *report_argv("ready-for-verification"), "--execution", xid).returncode == 0
    p = run(box, *report_argv("verifying"), agent="verifier-dispatcher")
    assert p.returncode == 0, p.stderr
    assert warnings_in(p.stderr) == [WARNING]


def test_a_refused_unnamed_report_does_not_warn(box):
    """名乗りなしでも拒否される報告 (遷移の狭め) では「通った」警告を出さない。"""
    take(box)
    set_card(box, status="pending")
    p = run(box, *report_argv("done"))
    assert p.returncode != 0
    assert warnings_in(p.stderr) == []


# ---------------------------------------------------------------------------
# 4. 警告が出ない場面 (対象外)
# ---------------------------------------------------------------------------

def test_a_card_without_an_attempt_does_not_warn(box):
    legacy_in_progress(box, "t002", "Ren")
    p = run(box, *report_argv("done", "t002"))
    assert p.returncode == 0, p.stderr
    assert warnings_in(p.stderr) == []


def test_reset_and_the_director_recovery_do_not_warn(box):
    take(box)
    p = run(box, "update", "t001", "--status", "pending", "--reset", "--mission", MISSION, agent="Sora")
    assert p.returncode == 0, p.stderr
    assert warnings_in(p.stderr) == []


def test_a_resend_naming_the_attempt_does_not_warn(box):
    xid = take(box)
    assert run(box, *report_argv("done"), "--execution", xid).returncode == 0
    again = run(box, *report_argv("done"), "--execution", xid)
    assert again.returncode == 0 and warnings_in(again.stderr) == []


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
