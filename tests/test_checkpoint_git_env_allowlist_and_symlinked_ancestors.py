"""PR #307 Codex 4 巡目 (t056): git の env を allowlist に・除外を Python 側でも・保存先の symlink を辿らない。

1. `GIT_LITERAL_PATHSPECS=1` だと `:(exclude,literal)<path>` が普通のファイル名になり、鍵を含むファイルの除外が効かず、鍵の途中の変更が
   BEGIN / END なしで保存された。`_git_env()` は `PATH` / `HOME` / `TMPDIR` / `TZ` だけ引き継ぎ、`GIT_*` は 1 つも引き継がない。
   族 (pathspec・repo の差し替え・config の追加・外部プログラム・trace) を 1 つずつ設定して take し、除外・redaction・「外部を起動しない」を確かめる。
2. 除外は git の pathspec だけに頼らず、git が返した patch を Python 側でも同じ規則で落とす (どちらか一方が壊れても漏れない)。
   「pathspec 側だけ壊す」「Python 側だけ壊す」の両方で鍵の行が残らない。
3. `checkpoints/<mission>/<task>/<execution_id>(.partial)` の各段を symlink にしても、take は外へ 1 バイトも書かず・消さず・chmod せず、
   `unsafe_path` で保存しない (退役は止めない・Director へ 1 回)。`checkpoint_gc` の隔離・復元も同じ。
テストは pytest の中で tmp_path に偽の鍵を作る (Bash では作れない)。
"""

from __future__ import annotations

import ast
import os
import pathlib
import stat
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import checkpoint_gc as gc  # noqa: E402
import lib_checkpoint as ck  # noqa: E402
from test_checkpoint_lib import MISSION, TASK, XID, git, take, wt  # noqa: E402,F401  (使い捨て repo の道具)
from test_checkpoint_multiline_secret_abs_path_and_gc_utc import (  # noqa: E402
    FRAGMENTS, assert_no_secret, commit_config, edit_line, key_block, meta_of,
)

# ---------------------------------------------------------------------------
# 1. env の族
# ---------------------------------------------------------------------------


def _script(path, marker):
    """実行されたら `marker` を作る外部プログラム (偽)。"""
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def _other_repo(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "other-branch")
    (other / "other.txt").write_text("other\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "other")
    return other


def poison_cases(tmp_path):
    """名前 -> {env 名: 値} (poison を env に置く)。`{MARK}` はテストが実行の印として監視するファイル。"""
    marker = tmp_path / "marker-ran"
    script = _script(tmp_path / "evil.sh", marker)
    other = _other_repo(tmp_path)
    return marker, {
        "literal_pathspecs": {"GIT_LITERAL_PATHSPECS": "1"},                   # Codex が実証した本体
        "glob_pathspecs": {"GIT_GLOB_PATHSPECS": "1"},
        "noglob_pathspecs": {"GIT_NOGLOB_PATHSPECS": "1"},
        "icase_pathspecs": {"GIT_ICASE_PATHSPECS": "1"},
        "git_dir": {"GIT_DIR": str(other / ".git")},
        "work_tree": {"GIT_WORK_TREE": str(other)},
        "index_file": {"GIT_INDEX_FILE": str(tmp_path / "bogus-index")},
        "object_directory": {"GIT_OBJECT_DIRECTORY": str(tmp_path / "empty-objects")},
        "alternate_objects": {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git" / "objects")},
        "common_dir": {"GIT_COMMON_DIR": str(other / ".git")},
        "namespace": {"GIT_NAMESPACE": "evil"},
        "config_count": {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "diff.external", "GIT_CONFIG_VALUE_0": script,
                         "GIT_CONFIG_KEY_1": "core.fsmonitor", "GIT_CONFIG_VALUE_1": script},
        "config_parameters": {"GIT_CONFIG_PARAMETERS": f"'diff.external={script}' 'core.fsmonitor={script}'"},
        "config_filter": {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "filter.x.clean", "GIT_CONFIG_VALUE_0": script,
                          "GIT_CONFIG_KEY_1": "core.attributesFile", "GIT_CONFIG_VALUE_1": str(tmp_path / "attrs")},
        "config_global": {"GIT_CONFIG_GLOBAL": str(tmp_path / "global-config")},
        "external_diff": {"GIT_EXTERNAL_DIFF": script},
        "pager": {"GIT_PAGER": script, "PAGER": script},
        "askpass": {"GIT_ASKPASS": script, "SSH_ASKPASS": script},
        "ssh_command": {"GIT_SSH_COMMAND": script, "GIT_SSH": script},
        "exec_path": {"GIT_EXEC_PATH": str(tmp_path / "no-such-exec-path")},
        "trace": {"GIT_TRACE": str(tmp_path / "trace.log"), "GIT_TRACE_PERFORMANCE": str(tmp_path / "trace-perf.log"),
                  "GIT_TRACE2": str(tmp_path / "trace2.log")},
        "optional_locks": {"GIT_OPTIONAL_LOCKS": "1"},
        "ceiling": {"GIT_CEILING_DIRECTORIES": str(tmp_path)},
    }


def poison_cases_names(base):
    # 名前の一覧 (parametrize に副作用なしで渡す。`test_the_case_list_matches_the_poison_table` が poison_cases と一致を見る)
    return [
        "literal_pathspecs", "glob_pathspecs", "noglob_pathspecs", "icase_pathspecs", "git_dir", "work_tree", "index_file",
        "object_directory", "alternate_objects", "common_dir", "namespace", "config_count", "config_parameters", "config_filter",
        "config_global", "external_diff", "pager", "askpass", "ssh_command", "exec_path", "trace", "optional_locks", "ceiling"]


def test_the_case_list_matches_the_poison_table(tmp_path):
    assert sorted(poison_cases_names(tmp_path)) == sorted(poison_cases(tmp_path)[1])


def _scene(wt):
    """鍵のブロックを持つ tracked のファイルの**中ほど**だけが変わった場面 + 鍵の無いファイルの変更 + 秘密の名前の untracked。"""
    commit_config(wt)
    edit_line(wt, 10, "CHANGED-middle-body-line")
    (wt / "a.txt").write_text("one\ntwo\nplain change\n")
    (wt / ".env.local").write_text("API_KEY=" + "k" * 30 + "\n")


def _assert_saved_correctly(d, wt):
    assert_no_secret(d)
    patch = (d / "dirty.patch").read_text()
    assert "plain change" in patch and "config.txt" not in patch and "CHANGED-middle" not in patch
    meta = meta_of(d)
    assert meta["branch"] == "task-branch" and meta["head"] == git(wt, "rev-parse", "HEAD").stdout.strip()
    assert meta["secret_block_files_dropped"] == 1
    assert not (d / "untracked" / ".env.local").exists() and meta["secret_files_excluded"] >= 1


@pytest.mark.parametrize("name", poison_cases_names(pathlib.Path(".")))
def test_an_inherited_git_variable_changes_nothing_that_is_saved(wt, tmp_path, monkeypatch, name):
    marker, cases = poison_cases(tmp_path)
    _scene(wt)
    for key, value in cases[name].items():
        monkeypatch.setenv(key, value)
    d = pathlib.Path(take(wt, tmp_path))
    _assert_saved_correctly(d, wt)
    assert not marker.exists(), "継いだ env の外部プログラム (diff.external / fsmonitor / filter / pager …) が起動した"
    for stray in ("trace.log", "trace-perf.log", "trace2.log"):
        assert not (tmp_path / stray).exists(), f"{stray}: GIT_TRACE* が効いた (git の子に GIT_* が渡った)"


def test_all_the_poison_at_once(wt, tmp_path, monkeypatch):
    marker, cases = poison_cases(tmp_path)
    _scene(wt)
    for name, env in cases.items():
        if name in ("exec_path", "git_dir", "work_tree", "index_file", "object_directory", "alternate_objects", "common_dir"):
            pass
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    d = pathlib.Path(take(wt, tmp_path))
    _assert_saved_correctly(d, wt)
    assert not marker.exists()


def test_git_env_has_only_the_allowlist_and_the_pinned_settings(monkeypatch, tmp_path):
    clean = ck._git_env()
    for key in ("GIT_LITERAL_PATHSPECS", "GIT_DIR", "GIT_CONFIG_COUNT", "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_TRACE",
                "GIT_SOMETHING_NEW_IN_A_FUTURE_GIT"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("SOME_SECRET_TOKEN", "x")
    poisoned = ck._git_env()
    assert poisoned == clean, "継いだ GIT_* / 無関係な env が git の環境に入った"
    assert set(clean) <= set(ck._GIT_ENV_KEEP) | {k for k in clean if k.startswith("GIT_")} | {"LC_ALL"}
    assert clean["GIT_CONFIG_GLOBAL"] == "/dev/null" and clean["GIT_CONFIG_NOSYSTEM"] == "1"
    assert clean["GIT_LITERAL_PATHSPECS"] == "0" and clean["GIT_TERMINAL_PROMPT"] == "0" and clean["LC_ALL"] == "C"
    assert "SOME_SECRET_TOKEN" not in clean


# ---------------------------------------------------------------------------
# 2. 二重の網 (pathspec / Python)
# ---------------------------------------------------------------------------

BROKEN_PATHSPEC_ENV = {"GIT_LITERAL_PATHSPECS": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}


def _break_pathspec_side(monkeypatch):
    """git の pathspec による除外だけを無効にする (旧 `_git_env` が継いでいた状態を再現)。"""
    monkeypatch.setattr(ck, "_git_env", lambda: {**os.environ, **BROKEN_PATHSPEC_ENV})


def test_control_the_broken_pathspec_really_lets_the_key_hunk_through_git(wt):
    """対照: この env では git の除外は効かない (このテスト群が空振りしていないこと)。"""
    _scene(wt)
    env = {**os.environ, **BROKEN_PATHSPEC_ENV}
    out = subprocess.run(["git", "-C", str(wt), "diff", "HEAD", "--", ".", ":(exclude,literal)config.txt"],
                         capture_output=True, text=True, env=env).stdout
    assert "CHANGED-middle-body-line" in out


def test_only_the_pathspec_side_broken_the_python_side_still_drops_the_key_file(wt, tmp_path, monkeypatch):
    _scene(wt)
    _break_pathspec_side(monkeypatch)
    d = pathlib.Path(take(wt, tmp_path))
    _assert_saved_correctly(d, wt)


def test_only_the_python_side_broken_the_pathspec_still_excludes_the_key_file(wt, tmp_path, monkeypatch):
    _scene(wt)
    monkeypatch.setattr(ck, "_drop_block_sections", lambda patch, blocked: (patch, 0))
    monkeypatch.setattr(ck, "_drop_secret_sections", lambda patch: (patch, 0))
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    patch = (d / "dirty.patch").read_text()
    assert "plain change" in patch and "config.txt" not in patch


@pytest.mark.parametrize("scene", ["middle_edit", "block_added", "block_removed", "deleted", "renamed"])
def test_both_nets_each_hold_alone_for_every_way_a_key_block_changes(wt, tmp_path, monkeypatch, scene):
    commit_config(wt)
    if scene == "middle_edit":
        edit_line(wt, 10, "CHANGED-middle")
    elif scene == "block_added":
        (wt / "a.txt").write_text("one\ntwo\n" + key_block())
    elif scene == "block_removed":
        (wt / "config.txt").write_text("header 1\nfooter 1\n")
    elif scene == "deleted":
        (wt / "config.txt").unlink()
    else:
        git(wt, "mv", "config.txt", "renamed-config.txt")
        edit_line_in = (wt / "renamed-config.txt")
        lines = edit_line_in.read_text().split("\n")
        lines[10] = "CHANGED-after-rename"
        edit_line_in.write_text("\n".join(lines))
    # (a) pathspec 側だけ壊す
    with monkeypatch.context() as m:
        _break_pathspec_side(m)
        d = pathlib.Path(take(wt, tmp_path / "a"))
        assert_no_secret(d)
    # (b) Python 側だけ壊す
    with monkeypatch.context() as m:
        m.setattr(ck, "_drop_block_sections", lambda patch, blocked: (patch, 0))
        m.setattr(ck, "_drop_secret_sections", lambda patch: (patch, 0))
        d = pathlib.Path(take(wt, tmp_path / "b"))
        assert_no_secret(d)


def test_the_head_side_check_does_not_depend_on_pathspec_semantics(wt, tmp_path, monkeypatch):
    """HEAD 側の鍵の印の検出 (`git grep`) は pathspec を使わない: pathspec の解釈が変わっても『印なし』に取りこぼさない。"""
    commit_config(wt)
    (wt / "config.txt").write_text("header 1\nfooter 1\n")                  # 鍵は HEAD 側にだけある
    _break_pathspec_side(monkeypatch)
    d = pathlib.Path(take(wt, tmp_path))
    assert_no_secret(d)
    assert meta_of(d)["secret_block_files_dropped"] == 1


def test_block_section_dropper_handles_quoted_and_spaced_names_and_does_not_overreach():
    names = ["dir/my config.txt", "é.txt", 'q"uote.txt']
    patch = ""
    for n in names + ["dir/my config.txt.bak", "other.txt"]:
        q_a, q_b = ck._git_quote("a/" + n), ck._git_quote("b/" + n)
        patch += f"diff --git {q_a} {q_b}\nindex 111..222 100644\n--- {q_a}\n+++ {q_b}\n@@ -1 +1 @@\n-x\n+y\n"
    patch += "diff --git a/old.txt b/new.txt\nsimilarity index 90%\nrename from old.txt\nrename to new.txt\n"
    kept, dropped = ck._drop_block_sections(patch, names + ["old.txt"])
    assert dropped == len(names) + 1
    assert "dir/my config.txt.bak" in kept and "other.txt" in kept            # 名前が前方一致するだけの別ファイルは落とさない
    for n in names:
        assert ck._git_quote("a/" + n) + " " not in kept.replace("config.txt.bak", "")


# ---------------------------------------------------------------------------
# 3. symlink の祖先
# ---------------------------------------------------------------------------

def tree_state(root):
    """root 以下の全部 (lstat。symlink は辿らない) の {相対パス: (種別, mode, 中身)}。"""
    out = {}
    root = pathlib.Path(root)
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        for n in dirs + files:
            p = pathlib.Path(dirpath, n)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[str(p.relative_to(root))] = ("link", os.readlink(p), None)
            elif stat.S_ISDIR(st.st_mode):
                out[str(p.relative_to(root))] = ("dir", stat.S_IMODE(st.st_mode), None)
            else:
                out[str(p.relative_to(root))] = ("file", stat.S_IMODE(st.st_mode), p.read_bytes())
    out["."] = ("dir", stat.S_IMODE(os.lstat(root).st_mode), None)
    return out


LEVELS = ["checkpoints", "mission", "task", "partial", "final"]


def build_outside(outside, level):
    """symlink の行き先 (外の dir)。ライブラリが辿ってしまえば消える・書かれる・chmod される物を置く。"""
    tail = {"checkpoints": [MISSION, TASK, XID + ".partial"], "mission": [TASK, XID + ".partial"], "task": [XID + ".partial"],
            "partial": [], "final": []}[level]
    cur = outside
    cur.mkdir(parents=True)
    cur.chmod(0o755)
    for part in tail:
        cur = cur / part
        cur.mkdir()
        cur.chmod(0o755)
    (cur / "precious.txt").write_text("do not touch\n")
    if level == "final":                                                    # 本物そっくりの checkpoint (meta が合う)
        (cur / "meta.json").write_text('{"execution_id": "%s"}\n' % XID)
    (outside / "sibling.txt").write_text("sibling\n")


def make_registry_with_symlink(tmp_path, level):
    registry = tmp_path / "registry"
    outside = tmp_path / "outside"
    build_outside(outside, level)
    parts = {"checkpoints": [], "mission": ["checkpoints"], "task": ["checkpoints", MISSION],
             "partial": ["checkpoints", MISSION, TASK], "final": ["checkpoints", MISSION, TASK]}[level]
    base = registry
    for p in parts:
        base = base / p
    base.mkdir(parents=True)
    name = {"checkpoints": "checkpoints", "mission": MISSION, "task": TASK, "partial": XID + ".partial", "final": XID}[level]
    (base / name).symlink_to(outside)
    return registry, outside


@pytest.mark.parametrize("level", LEVELS)
def test_take_never_writes_removes_or_chmods_through_a_symlinked_level(wt, tmp_path, level):
    registry, outside = make_registry_with_symlink(tmp_path, level)
    (wt / "a.txt").write_text("one\ntwo\nchanged\n")
    before = tree_state(outside)
    with pytest.raises(ck.CheckpointError) as e:
        ck.take(registry_dir=registry, queue_dir=tmp_path / "queue", mission=MISSION, task=TASK, execution_id=XID,
                worktree=str(wt), seconds=30)
    assert e.value.code in (ck.E_PATH, ck.E_EXISTS), e.value.code
    assert tree_state(outside) == before, "symlink の先 (外) が変わった (書き込み・削除・chmod)"


@pytest.mark.parametrize("level", ["checkpoints", "mission", "task", "partial"])
def test_the_error_is_the_fixed_code_and_names_nothing(wt, tmp_path, level):
    registry, outside = make_registry_with_symlink(tmp_path, level)
    with pytest.raises(ck.CheckpointError) as e:
        ck.take(registry_dir=registry, queue_dir=tmp_path / "queue", mission=MISSION, task=TASK, execution_id=XID,
                worktree=str(wt), seconds=30)
    assert e.value.code == ck.E_PATH and str(outside) not in str(e.value)


def test_a_real_tree_still_works_and_modes_stay_0700(wt, tmp_path):
    (wt / "a.txt").write_text("one\ntwo\nchanged\n")
    d = pathlib.Path(take(wt, tmp_path))
    for part in (d, d.parent, d.parent.parent, d.parent.parent.parent):
        assert stat.S_IMODE(os.lstat(part).st_mode) == 0o700, part
    again = pathlib.Path(take(wt, tmp_path))                                   # 再実行は再利用
    assert again == d


def test_a_symlinked_registry_root_itself_is_allowed(wt, tmp_path):
    """基点 (設定で決めた registry の場所) が別のディスクへの symlink でもよい。辿らないのは checkpoints/ 以下。"""
    real = tmp_path / "real-registry"
    real.mkdir()
    link = tmp_path / "registry"
    link.symlink_to(real)
    d = ck.take(registry_dir=link, queue_dir=tmp_path / "queue", mission=MISSION, task=TASK, execution_id=XID,
                worktree=str(wt), seconds=30)
    assert (real / "checkpoints" / MISSION / TASK / XID / "meta.json").exists() and d.startswith(str(link))


def test_a_leftover_real_partial_is_still_cleaned_and_replaced(wt, tmp_path):
    partial = tmp_path / "registry" / "checkpoints" / MISSION / TASK / (XID + ".partial")
    (partial / "sub").mkdir(parents=True)
    (partial / "sub" / "old.txt").write_text("old\n")
    (partial / "dangling").symlink_to(tmp_path / "does-not-exist")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    (partial / "points-out").symlink_to(victim)                                # partial の中の symlink は辿らず、リンクだけ消す
    d = pathlib.Path(take(wt, tmp_path))
    assert (d / "meta.json").exists() and not partial.exists() and victim.read_text() == "keep\n"


def test_untracked_names_cannot_make_a_write_escape_the_partial_dir(wt, tmp_path):
    (wt / "sub").mkdir()
    (wt / "sub" / "ok.txt").write_text("fine\n")
    d = pathlib.Path(take(wt, tmp_path))
    assert (d / "untracked" / "sub" / "ok.txt").read_text() == "fine\n"


def test_the_failed_take_leaves_no_partial_behind_in_a_real_tree(wt, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ck, "_save_untracked", boom)
    (wt / "new.txt").write_text("x\n")
    with pytest.raises(ck.CheckpointError):
        take(wt, tmp_path)
    parent = tmp_path / "registry" / "checkpoints" / MISSION / TASK
    assert list(parent.iterdir()) == []


# ---- retirement: a refused save does not stop the retirement; the Director is told once ----

from test_retirement import AGENT, SLUG, TASK_ID, WINDOW, FakeMux, make_executor, sandbox  # noqa: E402,F401
from test_checkpoint_retirement_stage import checkpoint_arg, drive_to_cleanup  # noqa: E402,F401
from test_checkpoint_retirement_stage import worker  # noqa: E402,F401


def test_a_symlinked_checkpoint_dir_does_not_stop_the_retirement_and_the_director_hears_once(sandbox, worker, wt, tmp_path):
    pane_pid, mux = worker
    outside = tmp_path / "outside-of-registry"
    (outside / TASK_ID).mkdir(parents=True)
    (outside / TASK_ID / "precious.txt").write_text("keep\n")
    ckdir = sandbox.registry / "checkpoints"
    ckdir.mkdir(parents=True, exist_ok=True)
    (ckdir / SLUG).symlink_to(outside)
    before = tree_state(outside)
    calls = []

    def take_it(**kw):
        return ck.take(worktree=str(wt), **kw)

    ex = make_executor(sandbox, mux, run_command=lambda argv, env: (calls.append(list(argv)), (0, ""))[1],
                       take_checkpoint=take_it)
    drive_to_cleanup(sandbox, ex, mux, pane_pid)
    assert len(calls) == 1 and checkpoint_arg(calls[0]) == "none"
    assert not ex.has_marker(AGENT), "symlink で保存を断ったことが退役を止めた"
    notices = [n for n in sandbox.notes if "[checkpoint]" in n]
    assert len(notices) == 1 and ck.E_PATH in notices[0]
    assert tree_state(outside) == before


# ---- checkpoint_gc ----

def _old_checkpoint(tmp_path, wt, registry=None):
    """30 日以上前の終端 task の checkpoint を 1 つ作り、(registry, queue, dir) を返す。"""
    registry = registry or tmp_path / "registry"
    queue = tmp_path / "queue"
    tasks = queue / "missions" / MISSION / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{TASK}.md").write_text(f"---\nid: {TASK}\ntitle: t\nstatus: done\nskills: [code]\nblocked_by: []\n---\n\n## Result\n")
    d = ck.take(registry_dir=registry, queue_dir=queue, mission=MISSION, task=TASK, execution_id=XID, worktree=str(wt),
                seconds=30, now=lambda: 1_000_000.0)
    return registry, queue, d


def test_gc_apply_never_follows_a_symlinked_ancestor_or_destination(wt, tmp_path):
    registry, queue, d = _old_checkpoint(tmp_path, wt)
    outside = tmp_path / "outside-quarantine"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("s\n")
    (registry / "checkpoints" / gc.QUARANTINE_DIRNAME).symlink_to(outside)         # 隔離先の祖先が symlink
    before = tree_state(outside)
    verdicts, err = gc.scan(registry, queue, now=10_000_000_000.0)
    assert err is None and [v.action for v in verdicts] == [gc.REMOVE]
    (res,) = gc.apply_quarantine(registry, queue, verdicts, now=10_000_000_000.0)
    assert res["quarantined"] is False and res["reason"].startswith(gc.R_UNSAFE_PATH)
    assert tree_state(outside) == before and os.path.isdir(d)


def test_gc_apply_refuses_when_the_source_ancestor_became_a_symlink_after_the_scan(wt, tmp_path):
    registry, queue, d = _old_checkpoint(tmp_path, wt)
    verdicts, _ = gc.scan(registry, queue, now=10_000_000_000.0)
    moved = tmp_path / "moved-away"
    os.rename(registry / "checkpoints" / MISSION / TASK, moved)
    (registry / "checkpoints" / MISSION / TASK).symlink_to(moved)               # 判定の後で task dir が symlink に化けた
    before = tree_state(moved)
    (res,) = gc.apply_quarantine(registry, queue, verdicts, now=10_000_000_000.0)
    assert res["quarantined"] is False
    assert tree_state(moved) == before and not (registry / "checkpoints" / gc.QUARANTINE_DIRNAME).exists()


def test_gc_apply_and_restore_work_on_a_real_tree(wt, tmp_path):
    registry, queue, d = _old_checkpoint(tmp_path, wt)
    verdicts, _ = gc.scan(registry, queue, now=10_000_000_000.0)
    (res,) = gc.apply_quarantine(registry, queue, verdicts, now=10_000_000_000.0)
    assert res["quarantined"] is True and not os.path.exists(d) and os.path.isdir(res["quarantine"])
    ok, msg = gc.restore(registry, res["quarantine"])
    assert ok and os.path.isdir(d), msg
    assert (pathlib.Path(d) / "meta.json").exists()


def test_gc_restore_never_follows_a_symlinked_destination(wt, tmp_path):
    registry, queue, d = _old_checkpoint(tmp_path, wt)
    verdicts, _ = gc.scan(registry, queue, now=10_000_000_000.0)
    (res,) = gc.apply_quarantine(registry, queue, verdicts, now=10_000_000_000.0)
    outside = tmp_path / "outside-restore"
    (outside / TASK).mkdir(parents=True)
    (outside / TASK / "sentinel.txt").write_text("s\n")
    mission_dir = registry / "checkpoints" / MISSION
    for child in list(mission_dir.iterdir()):
        os.rmdir(child)                                                         # 元の task dir (空) を片付けて mission 自体を symlink にする
    os.rmdir(mission_dir)
    mission_dir.symlink_to(outside)
    before = tree_state(outside)
    ok, msg = gc.restore(registry, res["quarantine"])
    assert not ok and gc.R_UNSAFE_PATH in msg
    assert tree_state(outside) == before and os.path.isdir(res["quarantine"])


def test_gc_moves_only_through_move_between_which_only_renames():
    tree = ast.parse((REPO / "scripts" / "lib_checkpoint.py").read_text())
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "move_between"]
    called = {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "")
              for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "rename" in called and not (called & {"rmtree", "remove", "unlink", "rmdir", "removedirs", "system", "run", "Popen"})
