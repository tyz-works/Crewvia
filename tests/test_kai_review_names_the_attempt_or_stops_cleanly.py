"""E5 PR-2 (t001 項目 4): 名乗りなしの done / needs-director が `EXECUTION_REQUIRED` (exit 3) で拒否されても、kai-review.sh は
黙って固まらない。

- pull の JSON が読めなくても、card の今の試行 (active・worker が自分) を**起動時に 1 回だけ**読んで名乗る
- それも読めずに報告が拒否されたら、exit 3 のまま終わり、card が in_progress に残ること・Director の回復手順 (`update --reset`)
  を stderr に出す。Result の全文は消さない
- 名乗れている実行は今までどおり通る (拒否に見える変更は無い)

kai-review.sh は repo の外に置いた偽の plan.sh (名乗りなしの報告を exit 3 で拒否する) と偽の gh / codex で走らせる。
本物の plan.sh・queue・gh には触れない。
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KAI_REVIEW = REPO_ROOT / "scripts" / "kai-review.sh"
XID = "ex-" + "d" * 32

PLAN_STUB = """#!/bin/bash
case "$1" in
  resolve-mission) echo m-test ;;
  pull) printf '%s' "$PULL_STDOUT" ;;
  done|needs-director)
    case " $* " in
      *" --execution "*) echo "$@" >> "$CALLS_FILE"; exit 0 ;;
      *) echo "$1 refused" >> "$CALLS_FILE"
         echo "[plan.sh $1] m-test/t001: この task は実行中の試行があります。" >&2
         echo "[plan.sh] error_code=EXECUTION_REQUIRED" >&2
         exit 3 ;;
    esac ;;
  *) echo "$@" >> "$CALLS_FILE" ;;
esac
exit 0
"""


def _exe(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_kai(tmp_path, pull_stdout, card_fields=None):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    _exe(root / "scripts" / "plan.sh", PLAN_STUB)
    queue = tmp_path / "queue"
    if card_fields is not None:
        tasks = queue / "missions" / "m-test" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "t001.md").write_text("---\nid: t001\ntitle: x\nstatus: in_progress\nworker: Kai-codex\n"
                                       + card_fields + "---\n\n## Result\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "gh", "#!/bin/bash\nexit 1\n")          # pull の後の gh は失敗して fail_needs_director に倒れる (早く終わる)
    _exe(bindir / "codex", "#!/bin/bash\nexit 1\n")
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_NAME", "CREWVIA_EXECUTION_ID")}
    env.update({"CREWVIA_REPO_ROOT": str(root), "CREWVIA_QUEUE": str(queue), "PULL_STDOUT": pull_stdout,
                "CALLS_FILE": str(tmp_path / "calls.txt"), "PATH": f"{bindir}:{env['PATH']}"})
    p = subprocess.run(["bash", str(KAI_REVIEW), "--pr", "1", "--task", "t001", "--mission", "m-test"],
                       env=env, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    calls = (tmp_path / "calls.txt").read_text() if (tmp_path / "calls.txt").exists() else ""
    return p, calls


ACTIVE_CARD = f"current_execution_id: {XID}\nexecution_status: running\n"


@pytest.mark.parametrize("pull_stdout", ["this is not json", '{"id": "t001"}', ""])
def test_an_unreadable_pull_json_falls_back_to_the_card_and_still_reports(tmp_path, pull_stdout):
    p, calls = run_kai(tmp_path, pull_stdout, ACTIVE_CARD)
    assert f"--execution {XID}" in calls and "refused" not in calls, (calls, p.stderr)
    assert "EXECUTION_REQUIRED" not in p.stderr


@pytest.mark.parametrize("card", [None, "", "current_execution_id: not-an-id\nexecution_status: running\n",
                                  f"current_execution_id: {XID}\nexecution_status: completed\n"])
def test_when_the_attempt_cannot_be_named_the_refusal_ends_the_script_with_a_recovery_hint(tmp_path, card):
    p, calls = run_kai(tmp_path, "not json", card)
    assert p.returncode == 3, (p.returncode, p.stdout, p.stderr)         # 拒否の exit をそのまま返す (成功にも 1 にも潰さない)
    assert "needs-director refused" in calls
    assert "update t001 --status pending --reset --mission m-test" in p.stderr, p.stderr
    assert "execution_id を読めませんでした" in p.stderr                    # 名乗れなかったことは 1 行残る


def test_a_named_run_is_unchanged(tmp_path):
    p, calls = run_kai(tmp_path, '{"execution_id": "%s"}' % XID)
    assert f"--execution {XID}" in calls and "refused" not in calls
    assert "update t001 --status pending --reset" not in p.stderr
