#!/usr/bin/env python3
"""tests/test_workers_yaml_untracked.py — registry/workers.yaml を追跡しない運用 (machine-local-config §7 / PR-3a t019)。

名簿は実行時の台帳で、機ごとの稼働で必ずずれる。追跡を外した後は「ファイルが無い」が通常の初回状態になるので、
読み書きの入口 (lib_registry / assign-name.sh) が無いファイルから空の名簿を作って動くことを固定する。
本番の registry には触れない (すべて tmp_path)。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_registry  # noqa: E402
from fixture_tree import copy_plan_tree  # noqa: E402


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, **kw)


def test_workers_yaml_is_not_tracked_and_is_ignored():
    tracked = _run(["git", "ls-files", "--", "registry/workers.yaml"], cwd=REPO_ROOT)
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == "", "registry/workers.yaml は追跡しない (§7 (a))"
    ignored = _run(["git", "check-ignore", "-q", "registry/workers.yaml"], cwd=REPO_ROOT)
    assert ignored.returncode == 0, ".gitignore に registry/workers.yaml の行が要る"


def test_parse_missing_file_is_an_empty_roster(tmp_path):
    assert lib_registry.parse(str(tmp_path / "registry" / "workers.yaml")) == ("", [], {})


def test_cli_writers_on_a_missing_file_do_not_crash_or_invent_workers(tmp_path):
    path = tmp_path / "registry" / "workers.yaml"
    for cmd in (["get-director", str(path)],
                ["add-skills", str(path), "Nobody", "bash"],
                ["set-last-active", str(path), "Nobody"],
                ["bump-task-count", str(path), "Nobody"]):
        r = _run([sys.executable, str(SCRIPTS / "lib_registry.py"), *cmd])
        assert r.returncode == 0, (cmd, r.stderr)
    # 名前の無い書き手は名簿を作り替えない (Worker を勝手に増やさない)
    _, order, _ = lib_registry.parse(str(path))
    assert order == []


def test_register_director_creates_the_roster_from_nothing(tmp_path):
    path = tmp_path / "registry" / "workers.yaml"
    lib_registry.register_director(str(path), "Sora")
    assert lib_registry.get_director(str(path)) == "Sora"


def test_assign_name_creates_the_roster_when_the_file_is_absent(tmp_path):
    copy_plan_tree(tmp_path)   # lib_* はこの helper だけが写す (test_fixture_tree_is_the_only_copier)
    (tmp_path / "config").mkdir()
    (tmp_path / "scripts" / "assign-name.sh").write_bytes((SCRIPTS / "assign-name.sh").read_bytes())
    shutil.copy2(REPO_ROOT / "config" / "worker-names.yaml", tmp_path / "config" / "worker-names.yaml")
    assert not (tmp_path / "registry" / "workers.yaml").exists()
    r = _run(["bash", str(tmp_path / "scripts" / "assign-name.sh"), "code"], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    name = r.stdout.strip().splitlines()[-1]
    assert name
    _, order, by_name = lib_registry.parse(str(tmp_path / "registry" / "workers.yaml"))
    assert name in order
    assert "code" in by_name[name]["skills"]
