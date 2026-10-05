"""kai-review.sh は Codex の出力の全文を `--result-file` で card に残す (mission 20261006-kai-review-full-findings t001)。

旧実装は `SUMMARY="${REVIEW_CONTENT:0:200}"` で先頭 200 文字だけを位置引数で渡し、一時ファイルも消えるため指摘の全文が失われた。
ここでは repo の外に置いた偽の plan.sh / gh / codex と、使い捨ての git repo (origin + refs/pull/1/head) で kai-review.sh を走らせ、
plan.sh が受け取った `--result-file` の中身 (= card に残る全文) を検査する。本物の plan.sh・queue・gh には触れない。
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KAI_REVIEW = REPO_ROOT / "scripts" / "kai-review.sh"

# 受け取った --result-file の中身をその場で複製する (kai-review.sh は呼び出しの後で消すため)。
PLAN_STUB = """#!/bin/bash
echo "$1 $*" >> "$CALLS_FILE"
case "$1" in
  resolve-mission) echo m-test ;;
  pull) printf '{"execution_id": "ex-%s"}' "$(printf 'a%.0s' $(seq 32))" ;;
  needs-director|done)
    prev=""
    for a in "$@"; do
      if [[ "$prev" == "--result-file" ]]; then cp "$a" "$CAPTURE_DIR/$1.txt"; fi
      prev="$a"
    done
    ;;
esac
exit 0
"""


def _exe(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@e"})


def run_kai_review(tmp_path, codex_output, extra_args=()):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "scripts").mkdir()
    (root / "config").mkdir()
    (root / "config" / "kai-review-findings.schema.json").write_text(
        (REPO_ROOT / "config" / "kai-review-findings.schema.json").read_text())
    _exe(root / "scripts" / "plan.sh", PLAN_STUB)
    (root / "README").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "main")
    _git(root, "checkout", "-q", "-b", "feat")
    (root / "README").write_text("base\nchange\n")
    _git(root, "commit", "-qam", "change")
    _git(root, "push", "-q", "origin", "feat:refs/pull/1/head")
    _git(root, "checkout", "-q", "main")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "gh", "#!/bin/bash\necho feat\n")
    (tmp_path / "codex_out.txt").write_text(codex_output)
    _exe(bindir / "codex", '#!/bin/bash\nwhile [[ $# -gt 0 ]]; do [[ "$1" == "-o" ]] && out="$2"; shift; done\n'
                           f'cp {tmp_path}/codex_out.txt "$out"\n')
    mdir = tmp_path / "queue" / "missions" / "m-test"
    mdir.mkdir(parents=True)
    (mdir / "mission.yaml").write_text("slug: m-test\n")           # git: 欄なし = 既定 (pr_base: main)
    capture = tmp_path / "capture"
    capture.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_NAME", "CREWVIA_EXECUTION_ID")}
    env.update({"CREWVIA_REPO_ROOT": str(root), "CREWVIA_QUEUE": str(tmp_path / "queue"),
                "CALLS_FILE": str(tmp_path / "calls.txt"), "CAPTURE_DIR": str(capture),
                "TMPDIR": str(tmp_path), "PATH": f"{bindir}:{env['PATH']}"})
    p = subprocess.run(["bash", str(KAI_REVIEW), "--pr", "1", "--task", "t001", "--mission", "m-test", *extra_args],
                       env=env, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    return p, capture


def _findings(n, body_len):
    return json.dumps({"findings": [
        {"priority": "P1" if i == 0 else "P2", "title": f"title-{i}", "body": f"BODY{i}-" + "x" * body_len + f"-END{i}",
         "file": f"scripts/f{i}.sh"} for i in range(n)]})


def _assert_base_resolved(p):
    assert "pr-base" not in p.stderr or "cannot resolve the PR base" not in p.stderr, (p.stdout, p.stderr)


def test_needs_director_keeps_every_finding_body_in_full(tmp_path):
    p, capture = run_kai_review(tmp_path, _findings(3, 2500))
    _assert_base_resolved(p)
    text = (capture / "needs-director.txt").read_text()
    for i in range(3):
        assert f"BODY{i}-" in text and f"-END{i}" in text and "x" * 2500 in text, (i, p.stdout, p.stderr)
    first = text.splitlines()[0]
    assert first.startswith("NEEDS FIX: PR#1 feat — ") and "P1×1, P2×2" in first and "title-0" in first
    assert len(text.splitlines()[0]) < 200                    # 先頭行は 1 行の要約 (本文はその後)
    calls = (tmp_path / "calls.txt").read_text()
    nd = [ln for ln in calls.splitlines() if ln.startswith("needs-director")][0]
    assert "--result-file" in nd and "NEEDS FIX" not in nd     # 理由は位置引数に渡さない (片方だけ)
    assert "--execution ex-" in nd


def test_done_on_a_clean_review_also_keeps_the_full_output(tmp_path):
    out = json.dumps({"findings": [{"priority": "P3", "title": "nit", "body": "N" * 2500 + "-TAIL", "file": None}]})
    p, capture = run_kai_review(tmp_path, out)
    _assert_base_resolved(p)
    text = (capture / "done.txt").read_text()
    assert text.splitlines()[0].startswith("LGTM: PR#1 feat") and "P3×1" in text.splitlines()[0]
    assert "N" * 2500 + "-TAIL" in text


def test_dry_run_prints_the_same_shape_and_writes_nothing(tmp_path):
    p, capture = run_kai_review(tmp_path, _findings(2, 2500), extra_args=("--dry-run",))
    assert "NEEDS-DIRECTOR相当" in p.stdout, (p.stdout, p.stderr)
    assert "-END0" in p.stdout and "-END1" in p.stdout        # dry-run も全文を出す
    assert not list(capture.iterdir())
    assert not any(ln.startswith(("needs-director", "done")) for ln in
                   ((tmp_path / "calls.txt").read_text().splitlines() if (tmp_path / "calls.txt").exists() else []))


def test_the_result_file_is_removed_after_the_call(tmp_path):
    run_kai_review(tmp_path, _findings(1, 50))
    assert not list(tmp_path.glob("kai-review-result.*"))
