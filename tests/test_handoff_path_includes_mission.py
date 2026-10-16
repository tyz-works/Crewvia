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
