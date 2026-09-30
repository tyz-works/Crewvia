"""PR #258 (S3 cutover) の Codex 1 巡目 P2 ×2 の回帰テスト (t032)。

P2-1: `cmd_init` が `missions/<slug>/tasks` を `os.makedirs` で作っていた。lib の `_ensure_dir` は既存 dir なら
      即 return するので `missions/` の fsync が走らず、`<slug>` のエントリが永続化されない
      (電源断で init 済み mission が消え、state.yaml だけが参照する)。→ queue の下の dir 作成は lib の
      durable な `ensure_dir` (作成 + 親 dir の fsync) を通す。電源断は再現しないので、`os.mkdir` / `os.fsync` の
      呼び出しを記録するスタブで「作った dir の親が fsync される」ことを検出する。
P2-2: 新しい書き込み経路が新規ファイルを無条件に 0644 にしていた。旧 `open(.., 'w')` は umask に従う
      (umask 077 なら 0600)。→ 新規は `0666 & ~umask`、既存ファイルの置き換えは元の mode を保つ。
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import stat
import subprocess

import pytest

import state_store_scenarios  # noqa: F401  (scripts/ を sys.path に足す)
import lib_state_store as store
import task_graph_publisher_harness as harness
from fixture_tree import copy_plan_tree

PLAN_SH_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "plan.sh"


# ---------------------------------------------------------------------------
# P2-1
# ---------------------------------------------------------------------------

def _namespace(root):
    plan = copy_plan_tree(root)
    (root / "queue").mkdir(exist_ok=True)
    (root / "registry").mkdir(exist_ok=True)
    return harness.load_plan_namespace(plan, str(root / "queue"), str(root))


def _record(monkeypatch):
    events = []
    real_mkdir, real_fsync = os.mkdir, os.fsync

    def mkdir(path, *a, **k):
        events.append(("mkdir", os.path.realpath(os.fspath(path))))
        return real_mkdir(path, *a, **k)

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append(("fsync_dir", os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))))
        return real_fsync(fd)

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "fsync", fsync)
    return events


def _fsynced_after_mkdir(events, created):
    """`created` を作った後に、その**親**が fsync されたか。"""
    parent = os.path.dirname(created)
    idx = [i for i, e in enumerate(events) if e == ("mkdir", created)]
    assert idx, f"{created} が作られていない: {events}"
    return any(e == ("fsync_dir", parent) for e in events[idx[0] + 1:])


def test_init_fsyncs_the_parent_of_every_directory_it_creates(tmp_path, monkeypatch):
    ns = _namespace(tmp_path)
    events = _record(monkeypatch)
    ns["cmd_init"](["Durable mission"])
    queue = os.path.realpath(tmp_path / "queue")
    slug = sorted(p.name for p in (pathlib.Path(queue) / "missions").iterdir())[0]
    mission = f"{queue}/missions/{slug}"
    assert _fsynced_after_mkdir(events, mission), f"missions/ の fsync が無い (<slug> のエントリが永続化されない)\n{events}"
    assert _fsynced_after_mkdir(events, f"{mission}/tasks"), events
    assert _fsynced_after_mkdir(events, f"{queue}/missions"), events
    assert _fsynced_after_mkdir(events, f"{queue}/archive"), events


def test_recorder_detects_a_makedirs_that_skips_the_fsync(tmp_path, monkeypatch):
    """陽性対照: `os.makedirs` (旧 init の作り方) を同じスタブに通すと述語が満たされない。"""
    events = _record(monkeypatch)
    target = os.path.realpath(tmp_path) + "/m/tasks"
    os.makedirs(target)
    assert not _fsynced_after_mkdir(events, os.path.dirname(target))
    store.ensure_dir(tmp_path / "n" / "tasks")
    assert _fsynced_after_mkdir(events, os.path.realpath(tmp_path) + "/n/tasks")


def test_lib_ensure_dir_is_idempotent_and_reports_failures(tmp_path):
    store.ensure_dir(tmp_path / "a" / "b")
    store.ensure_dir(tmp_path / "a" / "b")
    (tmp_path / "f").write_text("x")
    with pytest.raises(store.StoreWriteError):
        store.ensure_dir(tmp_path / "f" / "sub")


def test_plan_sh_creates_no_queue_directory_with_os_makedirs():
    """構造: plan.sh の `os.makedirs` は queue の外 / 再生成物の関数だけ (理由付き allowlist)。
    ここに queue の下の作成が増えたら赤。検査した呼び出しの件数を出す (0 件で PASS しない)。"""
    src = harness.plan_python_source(PLAN_SH_SRC)
    tree = ast.parse(src)
    #: 関数名 → 残す理由
    allowed = {
        "task_graph_pending_lock": "registry/task-graph の lock 用 dir (再生成物。queue の正本ではない)",
        "acquire_task_graph_lock": "同上",
        "_mark_task_graph_pending": "registry/task-graph の pending 印 (再生成物)",
        "_append_knowledge_director": "knowledge/ への追記 (queue の外)",
    }
    found, offenders = [], []

    def walk(node, owner):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                    and child.func.attr in ("makedirs", "mkdir") \
                    and isinstance(child.func.value, ast.Name) and child.func.value.id == "os":
                found.append(owner)
                if owner not in allowed:
                    offenders.append(f"{owner} (line {child.lineno})")
            walk(child, child.name if isinstance(child, ast.FunctionDef) else owner)

    walk(tree, None)
    assert len(found) == 4, found                       # 検査した呼び出しの件数 (0 件で PASS しない)
    assert not offenders, "queue の下の dir を os.makedirs / os.mkdir で作っている (lib の ensure_dir を使う): " + ", ".join(offenders)
    assert set(found) == set(allowed), f"allowlist に載っているのに無い / 載っていないのに在る: {set(found) ^ set(allowed)}"


# ---------------------------------------------------------------------------
# P2-2
# ---------------------------------------------------------------------------

def _run(plan, env, umask, *args):
    p = subprocess.run([str(plan), *args], env=env, capture_output=True, text=True, timeout=120,
                       preexec_fn=lambda: os.umask(umask))
    assert p.returncode == 0, f"{args}: {p.stderr}"
    return p


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize("umask", [0o022, 0o077])
def test_new_files_follow_the_umask_and_existing_files_keep_their_mode(tmp_path, umask):
    plan = copy_plan_tree(tmp_path)
    queue = tmp_path / "queue"
    queue.mkdir()
    (tmp_path / "registry").mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LANG": "C.UTF-8", "CREWVIA_QUEUE": str(queue),
           "CREWVIA_REPO_ROOT": str(tmp_path), "CREWVIA_TASKVIA": "disabled", "CREWVIA_TASK_GRAPH": "0"}
    _run(plan, env, umask, "init", "Umask mission")
    _run(plan, env, umask, "add", "T", "--skills", "bash")
    _run(plan, env, umask, "pull", "--agent", "Ren", "--skills", "bash", "--task", "t001")
    want = 0o666 & ~umask                                   # 旧 open(.., 'w') と同じ
    slug = sorted(p.name for p in (queue / "missions").iterdir())[0]
    files = {
        "state.yaml": queue / "state.yaml",
        "mission.yaml": queue / "missions" / slug / "mission.yaml",
        "card": queue / "missions" / slug / "tasks" / "t001.md",
        "assignment": queue / "assignments" / "Ren",
        "identity": queue / "assignments" / "Ren.identity",
        ".lock": queue / ".lock",
    }
    files["audit"] = next((queue / "audit").glob("transitions-*.jsonl"))
    got = {k: _mode(v) for k, v in files.items()}
    assert got == {k: want for k in files}, got
    # 既存カードの置き換えは元の mode を保つ (umask を変えても、chmod 済みの mode のまま)
    os.chmod(files["card"], 0o640)
    _run(plan, env, 0o002, "update", "t001", "--priority", "high")
    assert _mode(files["card"]) == 0o640
    assert "priority: high" in files["card"].read_text()


def test_atomic_write_new_file_umask_and_explicit_mode(tmp_path):
    old = os.umask(0o077)
    try:
        store.atomic_write_text(tmp_path / "a", "x")
        store.atomic_write_text(tmp_path / "b", "x", mode=0o600)
        os.umask(0o022)
        store.atomic_write_text(tmp_path / "c", "x")
        store.atomic_write_text(tmp_path / "a", "y")               # 既存: umask が変わっても元の mode
    finally:
        os.umask(old)
    assert (_mode(tmp_path / "a"), _mode(tmp_path / "b"), _mode(tmp_path / "c")) == (0o600, 0o600, 0o644)
