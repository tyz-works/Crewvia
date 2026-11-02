"""PR task-line guard (hooks/pre-tool-use.sh の "PR task-line guard")。

t001 (mission 20261102-pr-task-line) の調査: agents/worker.md の gh pr create テンプレートは
`task: <mission>/<task>` 行を含むが散文でしかなく強制力が無い。実測で #327〜#336 の 10 本全てに
この行が無かった。本テストは「本物の hook を subprocess で走らせる」(research skill guard と
同じ作法。knowledge/research-skill-guards.md 参照) ことで、判定を Python 側で再実装せずに
hook そのものを直したことを確かめる。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "pre-tool-use.sh"
MISSION = "20261102-demo"
TASK = "t001"


@pytest.fixture()
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    # hooks/ は丸ごと写す (名前で指して写すと lib を足すたびに CI でだけ赤くなる)
    shutil.copytree(REPO_ROOT / "hooks", repo / "hooks")
    (repo / "queue" / "assignments").mkdir(parents=True)
    (repo / "queue" / "assignments" / "Ingrid").write_text(f"{MISSION}:{TASK}\n")
    return repo


def _bash(repo, command, agent="Ingrid", extra=None):
    env = {
        "HOME": str(repo),
        "PATH": os.environ["PATH"],
        "CREWVIA_REPO": str(repo),
        "AGENT_NAME": agent,
        "TASKVIA_TOKEN": "",
        "TASKVIA_URL": "http://127.0.0.1:9",
    }
    env.update(extra or {})
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    p = subprocess.run(
        ["bash", str(repo / "hooks" / "pre-tool-use.sh")],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        cwd=str(repo), timeout=60,
    )
    assert p.returncode == 0, p.stderr[-3000:]
    if not p.stdout.strip():
        return "native"  # hook が判定せず native に委ねた (今回の対象外コマンド等)
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]


GH_PR_CREATE_WITH_LINE = (
    f'gh pr create --title "x" --body "## 概要\n\ntask: {MISSION}/{TASK}\nbranch: foo"'
)
GH_PR_CREATE_WITHOUT_LINE = 'gh pr create --title "x" --body "## 概要\n\nno task line here"'


def test_body_with_task_line_passes(fake_repo):
    assert _bash(fake_repo, GH_PR_CREATE_WITH_LINE) != "deny"


def test_body_missing_task_line_is_denied(fake_repo):
    assert _bash(fake_repo, GH_PR_CREATE_WITHOUT_LINE) == "deny"


def test_denial_reason_names_the_expected_line(fake_repo):
    # Worker が再実行時に何を足せばいいか分かること (黙って deny しない)
    payload = {"tool_name": "Bash", "tool_input": {"command": GH_PR_CREATE_WITHOUT_LINE}}
    env = {
        "HOME": str(fake_repo), "PATH": os.environ["PATH"], "CREWVIA_REPO": str(fake_repo),
        "AGENT_NAME": "Ingrid", "TASKVIA_TOKEN": "", "TASKVIA_URL": "http://127.0.0.1:9",
    }
    p = subprocess.run(["bash", str(fake_repo / "hooks" / "pre-tool-use.sh")],
                        input=json.dumps(payload), capture_output=True, text=True, env=env,
                        cwd=str(fake_repo), timeout=60)
    reason = json.loads(p.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert f"task: {MISSION}/{TASK}" in reason


def test_unrelated_commands_are_not_denied(fake_repo):
    assert _bash(fake_repo, "ls -la") != "deny"
    assert _bash(fake_repo, "git status") != "deny"


def test_quoted_mention_of_gh_pr_create_is_not_a_false_positive(fake_repo):
    # t019 と同型の誤検知: 報告コマンドが「gh pr create」という文字列を引用しているだけで、
    # 実際に gh pr create を呼び出しているわけではない。
    cmd = './scripts/plan.sh done t001 "report: gh pr create was run earlier" --execution ex-1'
    assert _bash(fake_repo, cmd) != "deny"


def test_gh_pr_create_after_unrelated_command_is_still_checked(fake_repo):
    # 制御演算子の後ろに本物の gh pr create が続く compound command も検出できること。
    cmd = f'echo start; {GH_PR_CREATE_WITHOUT_LINE}'
    assert _bash(fake_repo, cmd) == "deny"
    cmd_ok = f'echo start; {GH_PR_CREATE_WITH_LINE}'
    assert _bash(fake_repo, cmd_ok) != "deny"


def test_fail_open_when_no_assignment_file(fake_repo):
    # Director の設計制約 1: mission/task を解決できない呼び出し元 (Director 自身の PR・
    # 手動 PR・assignment を持たないセッション) まで deny すると誰も PR を出せなくなる。
    assert _bash(fake_repo, GH_PR_CREATE_WITHOUT_LINE, agent="NoSuchAgent") != "deny"


def test_fail_open_when_assignment_file_is_malformed(fake_repo):
    # ':' を含まない壊れた assignment ファイルは「解決できなかった」扱いにする
    # (mission/task をゴミの値のまま誤って強制しない)。
    (fake_repo / "queue" / "assignments" / "Ingrid").write_text("garbage-no-colon")
    assert _bash(fake_repo, GH_PR_CREATE_WITHOUT_LINE) != "deny"


def test_gh_pr_edit_is_not_in_scope(fake_repo):
    # Director の設計制約 4 への回答: gh pr edit は対象にしない (Result に理由を明記)。
    cmd = "gh pr edit 123 --body 'no task line'"
    assert _bash(fake_repo, cmd) != "deny"


# --- claude review (t005) P2-1/P2-2/P3 の回帰テスト ---


def test_apostrophe_in_double_quotes_does_not_hide_the_real_call(fake_repo):
    # P2-1: クォート除去をシングル/ダブルの 2 パスに分けると、二重引用符内の
    # アポストロフィ (don't) が後方の無関係な「'」(echo 'done') と対になり、
    # 間にある実物の gh pr create ごと消えてしまう (ガードが黙って発火しなくなる
    # = 検出漏れが最悪の形で起きる)。task: 行が無いのでここは deny されるべき。
    # (sed は行単位で処理するため、埋め込みの改行があると同一行内でしかペアに
    # ならない。この欠陥を同一行で再現するため、わざと 1 行のコマンドにする。)
    cmd = (
        'echo "don\'t forget" && gh pr create --title "x" '
        '--body "no task line here" && echo \'done\''
    )
    assert _bash(fake_repo, cmd) == "deny"


def test_body_file_is_not_denied(fake_repo):
    # P2-2: --body-file は knowledge の「既知の穴」どおり検出できない対象外の形。
    # 以前は task: 行が無いとして誤って deny していた。
    assert _bash(fake_repo, "gh pr create --title x --body-file notes.txt") != "deny"


def test_fill_is_not_denied(fake_repo):
    assert _bash(fake_repo, "gh pr create --fill") != "deny"


def test_web_is_not_denied(fake_repo):
    assert _bash(fake_repo, "gh pr create --web") != "deny"


def test_help_is_not_denied(fake_repo):
    assert _bash(fake_repo, "gh pr create --help") != "deny"


def test_task_id_prefix_collision_is_not_treated_as_present(fake_repo):
    # P3: grep -qF の無アンカー一致だと、割り当てられた task が t1 のとき、
    # 本文にある別 task の行 "task: <mission>/t10" を部分一致で「ある」と
    # 誤判定してしまう。アンカーで防ぎ、t1 自身の行が無ければ deny すること。
    (fake_repo / "queue" / "assignments" / "Ingrid").write_text(f"{MISSION}:t1\n")
    cmd = f'gh pr create --title "x" --body "task: {MISSION}/t10\nbranch: foo"'
    assert _bash(fake_repo, cmd) == "deny"
