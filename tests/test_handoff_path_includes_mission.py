"""handoff の path に mission を含める (vNext-02 PR-B)。

`crewvia_handoff_path <agent> <mission> <task_id>` は
`registry/handoffs/<agent>/<mission>/<task_id>_HANDOFF.md` を返す。別 mission の同じ tNNN が
同じファイルにならない。旧 2 引数形式は旧 path を返し (互換)、旧 path の handoff でも
`plan.sh fail` は通る (絶対パス + 存在を見るだけ)。
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from test_fail_evidence import MISSION, sb  # noqa: F401  (fixture)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPERS = REPO_ROOT / "scripts" / "git-helpers.sh"


def _path(*args, repo=REPO_ROOT):
    cmd = f'source "{HELPERS}"; crewvia_handoff_path "$@"'
    return subprocess.run(
        ["bash", "-c", cmd, "x", *args],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "CREWVIA_REPO_ROOT": str(repo)},
    )


def test_new_path_contains_mission():
    r = _path("Ren", "m-a", "t001")
    assert r.returncode == 0, r.stderr
    p = r.stdout.strip()
    assert p.endswith("/registry/handoffs/Ren/m-a/t001_HANDOFF.md")
    assert p.startswith("/")


def test_same_task_id_in_different_missions_gets_different_files():
    a = _path("Ren", "m-a", "t001").stdout.strip()
    b = _path("Ren", "m-b", "t001").stdout.strip()
    assert a != b


def test_two_arg_form_still_returns_the_old_path():
    r = _path("Ren", "t001")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("/registry/handoffs/Ren/t001_HANDOFF.md")


@pytest.mark.parametrize("bad", ["", ".", "..", "../x", "a/b", "a\\b", "a\nb", "a\tb"])
def test_bad_mission_slug_is_rejected(bad):
    r = _path("Ren", bad, "t001")
    assert r.returncode != 0
    assert r.stdout.strip() == ""


def test_wrong_argument_count_is_rejected():
    assert _path("Ren").returncode != 0
    assert _path("Ren", "m", "t001", "extra").returncode != 0


def test_fail_accepts_new_and_old_path_handoffs(sb):  # noqa: F811
    sb.add()
    text = f"# HANDOFF\n\nhead: {sb.new_head[:12]}\n"
    new = sb.handoffs / MISSION / "t001_HANDOFF.md"
    new.parent.mkdir(parents=True)
    new.write_text(text)
    r = sb.run("fail", "t001", str(new), "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert f"handoff_path: {new}" in sb.card()

    sb.add(status="in_progress")
    old = sb.handoffs / "t001_HANDOFF.md"
    old.write_text(text)
    r = sb.run("fail", "t001", str(old), "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert f"handoff_path: {old}" in sb.card()


def _worker_md_handoff_snippet():
    """agents/worker.md の Handoff 手順 Step 2 の bash ブロックをそのまま取り出す。"""
    text = (REPO_ROOT / "agents" / "worker.md").read_text()
    step = text.split("**Step 2**: HANDOFF.md を作成する", 1)[1]
    block = step.split("```bash\n", 1)[1].split("\n```", 1)[0]
    assert "crewvia_handoff_path" in block
    return block


def _run_snippet(env_extra):
    block = _worker_md_handoff_snippet()
    env = {
        "PATH": "/usr/bin:/bin",
        "CREWVIA_REPO": str(REPO_ROOT),
        "CREWVIA_REPO_ROOT": str(REPO_ROOT),
        "AGENT_NAME": "Ingrid",
        "TASK_ID": "t022",
        **env_extra,
    }
    # nounset: .crewvia-env を source しない target_dir の task と同じ条件
    return subprocess.run(
        ["bash", "-c", "set -u\n" + block + '\necho "$HANDOFF_PATH"'],
        capture_output=True, text=True, env=env,
    )


def test_worker_md_handoff_uses_pull_mission_not_crewvia_env_var():
    # target_dir の task: CREWVIA_MISSION_SLUG 未設定でも pull 由来の TASK_MISSION で動く
    r = _run_snippet({"TASK_MISSION": "m-now"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("/registry/handoffs/Ingrid/m-now/t022_HANDOFF.md")


def test_worker_md_handoff_ignores_stale_crewvia_mission_slug():
    # 前の task の値が残っていても、別 mission の下には書かない
    r = _run_snippet({"TASK_MISSION": "m-now", "CREWVIA_MISSION_SLUG": "m-stale"})
    assert r.returncode == 0, r.stderr
    assert "/m-now/" in r.stdout and "m-stale" not in r.stdout


def test_worker_md_handoff_stops_when_task_mission_unset():
    r = _run_snippet({"CREWVIA_MISSION_SLUG": "m-stale"})
    assert r.returncode != 0
    assert "m-stale" not in r.stdout


def _fail_relative_path_guidance(sb):  # noqa: F811
    sb.add()
    r = sb.run("fail", "t001", "rel/t001_HANDOFF.md", "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode != 0
    text = r.stdout + r.stderr
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("HANDOFF_PATH=")]
    assert len(lines) == 1, text
    return lines[0]


def test_fail_relative_path_guidance_uses_pull_mission(sb):  # noqa: F811
    cmd = _fail_relative_path_guidance(sb)
    assert "CREWVIA_MISSION_SLUG" not in cmd
    # 案内されたコマンドは、.crewvia-env を source しない target_dir の task (nounset) でも動く
    env = {"PATH": "/usr/bin:/bin", "CREWVIA_REPO": str(REPO_ROOT),
           "CREWVIA_REPO_ROOT": str(REPO_ROOT), "AGENT_NAME": "Ingrid",
           "TASK_ID": "t022", "TASK_MISSION": "m-now", "CREWVIA_MISSION_SLUG": "m-stale"}
    cmd_clean = cmd.split("  #", 1)[0]
    r = subprocess.run(["bash", "-c", f"set -u\nsource {REPO_ROOT}/scripts/git-helpers.sh\n" + cmd_clean + '\necho "$HANDOFF_PATH"'],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "/m-now/" in r.stdout and "m-stale" not in r.stdout
