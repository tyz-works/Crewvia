"""01c E3 fix 4 巡目 (t034 / PR #273 P3): kai-review.sh が pull の JSON から execution_id を読めず名乗りなしへ切り替わるとき、stderr に 1 行残す。

E3 は名乗りなしを拒否しないので動作は変わらない。ただ E5 の観察で「名乗れなかった実行」を数えるには、切り替えが見える必要がある
(黙って名乗りなしに落ちると、名乗りが効いていない Worker / reviewer が見えないまま cutover に進む)。
kai-review.sh は repo の外に置いた偽の plan.sh と偽の gh / codex で走らせる (本物の plan.sh・queue・gh には触れない)。
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KAI_REVIEW = REPO_ROOT / "scripts" / "kai-review.sh"
WARN = "execution_id を読めませんでした"

PLAN_STUB = """#!/bin/bash
case "$1" in
  resolve-mission) echo m-test ;;
  pull) printf '%s' "$PULL_STDOUT" ;;
  *) echo "$@" >> "$CALLS_FILE" ;;
esac
exit 0
"""


def _exe(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_kai_review(tmp_path, pull_stdout):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    _exe(root / "scripts" / "plan.sh", PLAN_STUB)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "gh", "#!/bin/bash\nexit 1\n")          # pull の後の gh は失敗して needs-director へ倒れる (早く終わる)
    _exe(bindir / "codex", "#!/bin/bash\nexit 1\n")
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_NAME", "CREWVIA_EXECUTION_ID")}
    env.update({"CREWVIA_REPO_ROOT": str(root), "PULL_STDOUT": pull_stdout,
                "CALLS_FILE": str(tmp_path / "calls.txt"), "PATH": f"{bindir}:{env['PATH']}"})
    return subprocess.run(["bash", str(KAI_REVIEW), "--pr", "1", "--task", "t001", "--mission", "m-test"],
                          env=env, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))


@pytest.mark.parametrize("pull_stdout", ["this is not json", '{"id": "t001"}', '{"execution_id": 7}', ""])
def test_a_pull_without_a_readable_execution_id_leaves_one_warning_line(tmp_path, pull_stdout):
    p = run_kai_review(tmp_path, pull_stdout)
    assert p.stderr.count(WARN) == 1, (p.stdout, p.stderr)
    assert "t001" in [ln for ln in p.stderr.splitlines() if WARN in ln][0]
    assert pull_stdout.strip() == "" or pull_stdout not in p.stderr      # JSON の中身は出さない


def test_a_pull_with_an_execution_id_does_not_warn_and_names_the_attempt(tmp_path):
    xid = "ex-" + "a" * 32
    p = run_kai_review(tmp_path, '{"execution_id": "%s"}' % xid)
    assert WARN not in p.stderr, p.stderr
    calls = (tmp_path / "calls.txt").read_text() if (tmp_path / "calls.txt").exists() else ""
    assert f"--execution {xid}" in calls, (calls, p.stderr)               # 名乗りは needs-director に渡る
