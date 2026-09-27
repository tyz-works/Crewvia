#!/usr/bin/env python3
"""古い Worker worktree を安全に片付ける `scripts/worktree_gc.py` (t033 / backlog #33)。

一時ディレクトリの git repo (origin = bare、主 checkout = clone) と、隔離した queue / registry だけで動く。
**本番の主 checkout・queue・registry・mux には触れない** (`--repo` / `--queue` を必ず明示する)。

固定するもの:

1. remove にしてよいのは **全条件を満たすものだけ** — 条件ごとに、その 1 つだけを欠いた worktree が keep になる
2. **判定できない** (読めない・git が失敗・プロセス表を取れない) はすべて keep (保留)
3. dry-run は何も変えない・`--apply` しても keep のものは残る・隔離する直前に再判定する
4. `--force` / `-D` / `rm -rf` (`shutil.rmtree` 等) を使わない (AST で構造的に固定)
5. `--apply` は `git worktree remove` を **呼ばない** — `git worktree move` + `git worktree lock` で
   隔離するだけ (t071 / PR#239 3巡目)。`--restore` で完全に戻せる

    python3 -m pytest tests/test_worktree_gc.py -v
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
SCRIPTS_DIR = REPO / "scripts"
SCRIPT = SCRIPTS_DIR / "worktree_gc.py"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lib_worker_target  # noqa: E402
import worktree_gc as gc  # noqa: E402

SLUG = "20260101-old-mission"


# ---------------------------------------------------------------------------
# 隔離した git repo
# ---------------------------------------------------------------------------

def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "CREWVIA_"))}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return env


def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=_env())
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r


class Fixture:
    """origin (bare) ← 主 checkout (clone)。`queue` / `registry` は repo の外に置く (本番と混ざらない)。"""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], env=_env(), check=True)
        self.repo = tmp_path / "repo"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], env=_env(), check=True,
                       capture_output=True)
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "README").write_text("hello\n")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        self.queue = tmp_path / "q" / "queue"
        self.registry = tmp_path / "q" / "registry"
        (self.queue / "missions").mkdir(parents=True)
        self.registry.mkdir(parents=True)
        self.set_active([])

    # --- queue / registry ---
    def set_active(self, slugs):
        body = "active_missions:\n" + "".join(f"  - {s}\n" for s in slugs) if slugs else "active_missions: []\n"
        (self.queue / "state.yaml").write_text(body + "default_mission: null\n")

    def record_target(self, agent, target):
        lib_worker_target.write_record(self.registry, agent, target)

    # --- worktree ---
    def add(self, name, slug=SLUG, branch=True, base="origin/main"):
        path = self.repo / ".claude" / "worktrees" / slug / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if branch:
            git(self.repo, "worktree", "add", "-q", "-b", f"task/{slug}/{name}", str(path), base)
        else:
            git(self.repo, "worktree", "add", "-q", "--detach", str(path), base)
        return path

    def commit(self, wt, fname="work.txt"):
        (wt / fname).write_text(f"{time.time()}\n")
        git(wt, "add", fname)
        git(wt, "commit", "-q", "-m", f"work {fname}")

    def push(self, wt):
        branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        git(wt, "push", "-q", "origin", branch)

    def quarantine_entries(self):
        entries, problem = gc.find_quarantine_entries(str(self.repo))
        assert problem == "", problem
        return entries

    # --- 判定 ---
    def verdicts(self, monkeypatch=None):
        verdicts, problem = gc.classify_all(str(self.repo), str(self.queue))
        assert problem is None, problem
        return {v.path: v for v in verdicts}

    def verdict(self, wt):
        return self.verdicts()[str(wt)]

    def cli(self, *args, env=None):
        e = _env()
        e.update(env or {})
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo),
                               "--queue", str(self.queue), *args],
                              capture_output=True, text=True, env=e)


@pytest.fixture
def fx(tmp_path, monkeypatch):
    # 既定は「動いているプロセスの cwd は worktree の中に無い」。実プロセス表は専用のテストだけが使う。
    monkeypatch.setattr(gc, "scan_process_cwds", lambda: ([], ""))
    return Fixture(tmp_path)


def _pushed_worktree(fx, name="t001"):
    """全条件を満たす worktree: 管理下・mission は archive 済み・clean・コミットは origin にある。"""
    wt = fx.add(name)
    fx.commit(wt)
    fx.push(wt)
    return wt


# ---------------------------------------------------------------------------
# 1. remove は全条件を満たしたものだけ
# ---------------------------------------------------------------------------

class TestRemoveOnlyWhenEveryConditionHolds:
    def test_a_pushed_clean_worktree_of_an_archived_mission_is_removed(self, fx):
        wt = _pushed_worktree(fx)
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.REMOVE, gc.R_REMOVE), v

    def test_a_worktree_at_a_merged_commit_is_removed(self, fx):
        wt = fx.add("t001")                      # origin/main そのもの = merge 済み
        assert fx.verdict(wt).action == gc.REMOVE

    def test_a_detached_worktree_is_removed_and_has_no_branch(self, fx):
        wt = fx.add("t001", branch=False)
        v = fx.verdict(wt)
        assert v.action == gc.REMOVE and v.branch is None

    def test_a_worktree_pushed_but_not_merged_is_removed(self, fx):
        """「merge 済み、または remote branch に push 済み」の後者。"""
        wt = fx.add("t001")
        fx.commit(wt)
        fx.push(wt)
        assert fx.verdict(wt).action == gc.REMOVE

    def test_the_main_checkout_is_never_removed(self, fx):
        v = fx.verdict(fx.repo)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_MAIN)

    def test_a_worktree_outside_the_managed_dir_is_kept(self, fx, tmp_path):
        outside = tmp_path / "elsewhere" / "wt"
        git(fx.repo, "worktree", "add", "-q", "--detach", str(outside), "origin/main")
        v = fx.verdict(outside)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_OUTSIDE)

    def test_a_worktree_without_a_mission_directory_level_is_kept(self, fx):
        """`.claude/worktrees/<name>` (mission の階層が無い) は管理下の形ではない。"""
        path = fx.repo / ".claude" / "worktrees" / "loose"
        git(fx.repo, "worktree", "add", "-q", "--detach", str(path), "origin/main")
        assert fx.verdict(path).reason == gc.R_OUTSIDE

    def test_a_locked_worktree_is_kept(self, fx):
        wt = _pushed_worktree(fx)
        git(fx.repo, "worktree", "lock", str(wt))
        assert fx.verdict(wt).reason == gc.R_LOCKED

    def test_a_worktree_whose_directory_is_gone_is_kept_for_prune(self, fx):
        wt = _pushed_worktree(fx)
        subprocess.run(["mv", str(wt), str(wt) + ".moved"], check=True)
        assert fx.verdict(wt).reason == gc.R_PRUNABLE


class TestMissionCondition:
    def test_an_active_mission_keeps_its_worktrees(self, fx):
        wt = _pushed_worktree(fx)
        fx.set_active([SLUG])
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_MISSION_ACTIVE)

    def test_only_the_active_missions_worktrees_are_kept(self, fx):
        old, live = _pushed_worktree(fx, "t001"), fx.add("t001", slug="20260202-live")
        fx.set_active(["20260202-live"])
        got = fx.verdicts()
        assert got[str(old)].action == gc.REMOVE and got[str(live)].reason == gc.R_MISSION_ACTIVE

    def test_a_mission_still_in_queue_missions_is_kept_even_if_not_active(self, fx):
        wt = _pushed_worktree(fx)
        (fx.queue / "missions" / SLUG).mkdir()
        assert fx.verdict(wt).reason == gc.R_MISSION_NOT_ARCHIVED

    def test_an_unobservable_mission_dir_is_kept_not_read_as_absent(self, fx, monkeypatch):
        wt = _pushed_worktree(fx)
        real = os.lstat

        def flaky(path, *a, **kw):
            if str(path).endswith(f"missions/{SLUG}"):
                raise PermissionError(13, "denied", str(path))
            return real(path, *a, **kw)
        monkeypatch.setattr(gc.os, "lstat", flaky)
        assert fx.verdict(wt).reason == gc.R_MISSION_UNOBSERVABLE

    @pytest.mark.parametrize("how", ["missing", "unparsable", "wrong-shape", "not-a-list", "non-string-item", "directory"])
    def test_an_unreadable_state_keeps_every_worktree(self, fx, how):
        wt = _pushed_worktree(fx)
        state = fx.queue / "state.yaml"
        if how == "missing":
            state.unlink()
        elif how == "unparsable":
            state.write_text("active_missions: [\nthis is: not: valid\n???\n")
        elif how == "wrong-shape":
            state.write_text("default_mission: null\n")          # active_missions が無い
        elif how == "not-a-list":
            state.write_text("active_missions: nope\n")
        elif how == "non-string-item":
            state.write_text("active_missions: [1, 2]\n")
        elif how == "directory":
            state.unlink()
            state.mkdir()
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_STATE_UNREADABLE), (how, v)

    def test_an_empty_active_list_is_a_normal_state_not_a_failure(self, fx):
        wt = _pushed_worktree(fx)
        fx.set_active([])
        assert fx.verdict(wt).action == gc.REMOVE


# ---------------------------------------------------------------------------
# 2. Worker が今使っていない
# ---------------------------------------------------------------------------

class TestNotInUse:
    def test_a_target_dir_record_pointing_into_the_worktree_keeps_it(self, fx):
        wt = _pushed_worktree(fx)
        fx.record_target("Ren", str(wt))
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_IN_USE_TARGET) and "Ren" in v.detail

    def test_a_target_dir_record_inside_a_subdirectory_keeps_it(self, fx):
        wt = _pushed_worktree(fx)
        (wt / "sub").mkdir()
        fx.record_target("Ren", str(wt / "sub"))
        assert fx.verdict(wt).reason == gc.R_IN_USE_TARGET

    def test_a_sibling_directory_with_a_common_prefix_is_not_the_worktree(self, fx):
        wt = _pushed_worktree(fx)
        fx.record_target("Ren", str(wt) + "-other")
        assert fx.verdict(wt).action == gc.REMOVE

    def test_a_null_target_dir_record_is_a_fact_not_a_use(self, fx):
        wt = _pushed_worktree(fx)
        fx.record_target("Ren", None)
        assert fx.verdict(wt).action == gc.REMOVE

    def test_a_worker_directory_without_a_record_is_fine(self, fx):
        wt = _pushed_worktree(fx)
        (fx.registry / "workers" / "Ren").mkdir(parents=True)
        assert fx.verdict(wt).action == gc.REMOVE

    @pytest.mark.parametrize("body", ["{not json", "[]", '{"agent": "Ren", "target_dir": 5, "written_at": 1}'])
    def test_an_unreadable_record_keeps_every_worktree(self, fx, body):
        wt = _pushed_worktree(fx)
        p = lib_worker_target.record_path(fx.registry, "Ren")
        p.parent.mkdir(parents=True)
        p.write_text(body)
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_REGISTRY_UNREADABLE), v

    def test_an_unlistable_registry_keeps_every_worktree(self, fx, monkeypatch):
        wt = _pushed_worktree(fx)
        real = os.listdir

        def deny(path):
            if str(path).endswith("registry/workers"):
                raise PermissionError(13, "denied", str(path))
            return real(path)
        monkeypatch.setattr(gc.os, "listdir", deny)
        assert fx.verdict(wt).reason == gc.R_REGISTRY_UNREADABLE

    def test_a_process_whose_cwd_is_in_the_worktree_keeps_it(self, fx, monkeypatch):
        """**実プロセス表** を読む: worktree の中で `sleep` を動かす。"""
        monkeypatch.undo()                       # fixture の偽スキャナを外す
        monkeypatch.setattr(gc, "PROC_ROOT", pathlib.Path("/proc"))
        wt = _pushed_worktree(fx)
        (wt / "deep").mkdir()
        proc = subprocess.Popen(["sleep", "30"], cwd=wt / "deep")
        try:
            found, problem = gc.scan_process_cwds()
            if problem:
                pytest.skip(f"この環境ではプロセス表を取れない: {problem}")
            v = fx.verdict(wt)
            assert (v.action, v.reason) == (gc.KEEP, gc.R_IN_USE_PROCESS), v
            assert f"pid {proc.pid}" in v.detail
        finally:
            proc.kill()
            proc.wait()
        v = fx.verdict(wt)
        assert v.action == gc.REMOVE, v          # 止まったら手放している

    def test_a_scan_that_failed_keeps_every_worktree(self, fx, monkeypatch):
        wt = _pushed_worktree(fx)
        monkeypatch.setattr(gc, "scan_process_cwds", lambda: ([], "テスト: 取れなかった"))
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_PROCESS_SCAN_FAILED)


class TestProcessScan:
    """`/proc` の走査の規則 (偽の /proc を `PROC_ROOT` で差し込む)。"""

    def _proc(self, tmp_path, monkeypatch):
        root = tmp_path / "proc"
        root.mkdir()
        monkeypatch.setattr(gc, "PROC_ROOT", root)
        return root

    def _entry(self, root, pid, *, state="S", cmdline=b"x\0", comm="worker"):
        d = root / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm}) {state} 1 1 1 0\n")
        (d / "cmdline").write_bytes(cmdline)
        (d / "comm").write_text(comm + "\n")
        return d

    def test_readable_cwds_are_collected_as_realpaths(self, tmp_path, monkeypatch):
        root = self._proc(tmp_path, monkeypatch)
        target = tmp_path / "wt"
        target.mkdir()
        d = self._entry(root, 100)
        (d / "cwd").symlink_to(target)
        found, problem = gc.scan_process_cwds()
        assert problem == "" and found == [(100, str(target.resolve()))]

    def test_a_vanished_process_is_ignored(self, tmp_path, monkeypatch):
        root = self._proc(tmp_path, monkeypatch)
        (root / "200").mkdir()                    # cwd が無い = readlink が ENOENT
        assert gc.scan_process_cwds() == ([], "")

    @pytest.mark.parametrize("kwargs, ok", [
        ({"state": "Z"}, True), ({"state": "X"}, True),                  # zombie / 死亡
        ({"cmdline": b""}, True),                                        # mm が無い = 終了処理中
        ({"comm": "ssh-agent"}, True), ({"comm": "sshd"}, True), ({"comm": "systemd"}, True),
        ({"comm": "(sd-pam)"}, True),
        ({"comm": "claude"}, False), ({"comm": "bash"}, False), ({"comm": "node"}, False),
        ({"comm": "some-unknown-daemon"}, False),
    ])
    def test_an_unreadable_cwd_is_harmless_only_for_provable_cases(self, tmp_path, monkeypatch, kwargs, ok):
        root = self._proc(tmp_path, monkeypatch)
        self._entry(root, 300, **kwargs)
        assert (gc._cwd_unreadable_but_harmless("300") is not None) is ok

    def test_a_process_that_disappeared_while_checking_is_harmless(self, tmp_path, monkeypatch):
        self._proc(tmp_path, monkeypatch)
        assert gc._cwd_unreadable_but_harmless("999") == "消えた"

    def test_an_unreadable_unknown_process_of_ours_fails_the_scan(self, tmp_path, monkeypatch):
        """同じ uid で cwd を読めず、Worker でないと言えない → 取れなかった (= 全 keep の根拠)。"""
        root = self._proc(tmp_path, monkeypatch)
        self._entry(root, 400, comm="claude")
        real_readlink = os.readlink

        def deny(path, *a, **kw):
            if str(path).endswith("/400/cwd"):
                raise PermissionError(13, "denied", str(path))
            return real_readlink(path, *a, **kw)
        monkeypatch.setattr(gc.os, "readlink", deny)
        found, problem = gc.scan_process_cwds()
        assert found == [] and "/400/cwd (claude)" in problem

    def test_a_readonly_probe_where_stat_also_fails_with_eacces_fails_the_scan(self, tmp_path, monkeypatch):
        """readlink・stat の両方が EACCES を返す read-only probe (族A、PR#239 2巡目 P2)。
        `stat` の失敗を ENOENT/ESRCH 以外まで「消えた」に潰すと、この pid が実は worktree の中で
        作業中の Worker でも `([], '')` = スキャン成功として報告され、in-use 判定を作れなくなる。"""
        root = self._proc(tmp_path, monkeypatch)
        self._entry(root, 700, comm="claude")
        real_readlink, real_stat = os.readlink, os.stat

        def deny_readlink(path, *a, **kw):
            if str(path).endswith("/700/cwd"):
                raise PermissionError(13, "denied", str(path))
            return real_readlink(path, *a, **kw)

        def deny_stat(path, *a, **kw):
            if str(path).endswith("/700"):
                raise PermissionError(13, "denied", str(path))
            return real_stat(path, *a, **kw)
        monkeypatch.setattr(gc.os, "readlink", deny_readlink)
        monkeypatch.setattr(gc.os, "stat", deny_stat)
        found, problem = gc.scan_process_cwds()
        assert found == [] and problem, "EACCES で stat も読めないとき、スキャン成功 (問題なし) にしてはいけない"

    def test_a_stat_enoent_after_an_unreadable_cwd_is_still_harmless(self, tmp_path, monkeypatch):
        """stat が ENOENT/ESRCH のときは従来どおり「消えた」で continue してよい (回帰させない)。"""
        root = self._proc(tmp_path, monkeypatch)
        self._entry(root, 701, comm="claude")
        real_readlink, real_stat = os.readlink, os.stat

        def deny_readlink(path, *a, **kw):
            if str(path).endswith("/701/cwd"):
                raise PermissionError(13, "denied", str(path))
            return real_readlink(path, *a, **kw)

        def vanished_stat(path, *a, **kw):
            if str(path).endswith("/701"):
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real_stat(path, *a, **kw)
        monkeypatch.setattr(gc.os, "readlink", deny_readlink)
        monkeypatch.setattr(gc.os, "stat", vanished_stat)
        assert gc.scan_process_cwds() == ([], "")

    def test_an_unreadable_allowlisted_daemon_does_not_fail_the_scan(self, tmp_path, monkeypatch):
        root = self._proc(tmp_path, monkeypatch)
        self._entry(root, 500, comm="ssh-agent")
        real_readlink = os.readlink

        def deny(path, *a, **kw):
            if str(path).endswith("/500/cwd"):
                raise PermissionError(13, "denied", str(path))
            return real_readlink(path, *a, **kw)
        monkeypatch.setattr(gc.os, "readlink", deny)
        assert gc.scan_process_cwds() == ([], "")

    def test_without_proc_and_lsof_the_scan_fails_rather_than_finding_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "PROC_ROOT", tmp_path / "no-such-proc")

        def no_lsof(*a, **kw):
            raise FileNotFoundError("lsof")
        monkeypatch.setattr(gc.subprocess, "run", no_lsof)
        found, problem = gc.scan_process_cwds()
        assert found == [] and "lsof" in problem

    def test_an_empty_lsof_output_is_a_failure_not_an_empty_table(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gc, "PROC_ROOT", tmp_path / "no-such-proc")
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: subprocess.CompletedProcess(a, 1, stdout="", stderr=""))
        found, problem = gc.scan_process_cwds()
        assert found == [] and problem

    def test_a_nonzero_lsof_exit_with_partial_output_is_a_failure_not_a_partial_success(self, tmp_path, monkeypatch):
        """lsof は一部のプロセスの検査に失敗しても、集められた分の stdout を出しつつ非 0 を返しうる。
        その部分出力を「見つからなかった (= 使われていない)」の証拠にしてはいけない。"""
        monkeypatch.setattr(gc, "PROC_ROOT", tmp_path / "no-such-proc")
        target = tmp_path / "wt"
        target.mkdir()
        stdout = f"p123\nn{target}\n"
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: subprocess.CompletedProcess(
                                a, 1, stdout=stdout, stderr="lsof: WARNING: can't stat() fuse.gvfsd-fuse\n"))
        found, problem = gc.scan_process_cwds()
        assert found == [] and problem, "非 0 の lsof は、出力があっても不完全なスキャンとして拒否すること"


# ---------------------------------------------------------------------------
# 3. git の中身
# ---------------------------------------------------------------------------

class TestGitContent:
    def test_a_modified_tracked_file_keeps_it(self, fx):
        wt = _pushed_worktree(fx)
        (wt / "work.txt").write_text("edited\n")
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_DIRTY) and "work.txt" in v.detail

    def test_an_untracked_file_keeps_it(self, fx):
        wt = _pushed_worktree(fx)
        (wt / "new.txt").write_text("x\n")
        assert fx.verdict(wt).reason == gc.R_DIRTY

    def test_an_untracked_file_inside_an_untracked_directory_keeps_it(self, fx):
        """`--untracked-files=all`: 未追跡ディレクトリの中身も数える (ディレクトリ 1 行に畳まない)。"""
        wt = _pushed_worktree(fx)
        (wt / "d1" / "d2").mkdir(parents=True)
        (wt / "d1" / "d2" / "f").write_text("x\n")
        v = fx.verdict(wt)
        assert v.reason == gc.R_DIRTY and "d1/d2/f" in v.detail

    def test_a_staged_change_keeps_it(self, fx):
        wt = _pushed_worktree(fx)
        (wt / "staged.txt").write_text("x\n")
        git(wt, "add", "staged.txt")
        assert fx.verdict(wt).reason == gc.R_DIRTY

    def test_an_ignored_file_keeps_it_even_though_git_status_is_clean(self, fx):
        """`git status` (--ignored 無し) は ignored ファイルを数えないので、これが無いと remove になる。
        中身は読まず存在だけを扱う (実際の `.env` は作らない・読まない)。"""
        wt = _pushed_worktree(fx)
        (wt / ".gitignore").write_text("secret.local\n")
        git(wt, "add", ".gitignore")
        git(wt, "commit", "-q", "-m", "gitignore")
        fx.push(wt)
        (wt / "secret.local").write_text("x\n")
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_IGNORED) and "secret.local" in v.detail

    def test_an_entirely_ignored_directory_keeps_it(self, fx):
        wt = _pushed_worktree(fx)
        (wt / ".gitignore").write_text("secret/\n")
        git(wt, "add", ".gitignore")
        git(wt, "commit", "-q", "-m", "gitignore")
        fx.push(wt)
        (wt / "secret").mkdir()
        (wt / "secret" / "a").write_text("x\n")
        v = fx.verdict(wt)
        assert v.reason == gc.R_IGNORED and "secret" in v.detail

    def test_untracked_takes_priority_over_ignored_in_the_reported_reason(self, fx):
        wt = _pushed_worktree(fx)
        (wt / ".gitignore").write_text("secret.local\n")
        git(wt, "add", ".gitignore")
        git(wt, "commit", "-q", "-m", "gitignore")
        fx.push(wt)
        (wt / "secret.local").write_text("x\n")
        (wt / "new.txt").write_text("x\n")
        assert fx.verdict(wt).reason == gc.R_DIRTY

    def test_an_assume_unchanged_file_keeps_it_even_though_status_is_clean(self, fx):
        """`git update-index --assume-unchanged` を付けた tracked ファイルへの編集は `git status` に
        一切出ない (t071 / PR#239 3巡目、Codex の要求)。中身は読まず「フラグが付いている」ことだけを扱う。"""
        wt = _pushed_worktree(fx)
        git(wt, "update-index", "--assume-unchanged", "work.txt")
        (wt / "work.txt").write_text("edited but invisible to status\n")
        assert git(wt, "status", "--porcelain=v1").stdout == ""     # 前提: status は本当に空
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_INDEX_FLAGS) and "work.txt" in v.detail

    def test_a_skip_worktree_file_keeps_it_even_though_status_is_clean(self, fx):
        wt = _pushed_worktree(fx)
        git(wt, "update-index", "--skip-worktree", "work.txt")
        (wt / "work.txt").write_text("edited but invisible to status\n")
        assert git(wt, "status", "--porcelain=v1").stdout == ""
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_INDEX_FLAGS) and "work.txt" in v.detail

    def test_ls_files_failing_is_a_hold_not_a_pass(self, fx, monkeypatch):
        wt = _pushed_worktree(fx)
        real = gc.run_git

        def failing(cwd, *args):
            if args and args[0] == "ls-files":
                return None, "", "OSError: boom"
            return real(cwd, *args)
        monkeypatch.setattr(gc, "run_git", failing)
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_LS_FILES_FAILED)

    def test_core_ignore_stat_true_keeps_it_even_with_no_flags_yet(self, fx):
        """`core.ignoreStat=true` は以後の checkout 等が触れたファイルへ自動で assume-unchanged を
        付ける。今この瞬間に 1 件もフラグが無くても、今後の編集が見えなくなりうるので keep にする。"""
        wt = _pushed_worktree(fx)
        git(wt, "config", "core.ignoreStat", "true")
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_IGNORE_STAT)

    def test_core_ignore_stat_false_or_unset_does_not_keep_it(self, fx):
        wt = _pushed_worktree(fx)
        assert fx.verdict(wt).action == gc.REMOVE                 # 未設定
        git(wt, "config", "core.ignoreStat", "false")
        assert fx.verdict(wt).action == gc.REMOVE                 # 明示的に false

    def test_a_failing_ignore_stat_config_check_is_a_hold_not_a_pass(self, fx, monkeypatch):
        wt = _pushed_worktree(fx)
        real = gc.run_git

        def failing(cwd, *args):
            if args[:2] == ("config", "--bool"):
                return 128, "", "fatal: bad config"
            return real(cwd, *args)
        monkeypatch.setattr(gc, "run_git", failing)
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_CONFIG_CHECK_FAILED)

    def test_a_commit_that_is_not_on_origin_keeps_it(self, fx):
        wt = _pushed_worktree(fx)
        fx.commit(wt, "second.txt")              # push していない
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_UNPUSHED)

    def test_a_detached_commit_that_is_not_on_origin_keeps_it(self, fx):
        wt = fx.add("t001", branch=False)
        fx.commit(wt)
        assert fx.verdict(wt).reason == gc.R_UNPUSHED

    def test_a_repo_without_origin_keeps_everything_with_commits(self, fx):
        wt = fx.add("t001")
        fx.commit(wt)
        git(fx.repo, "remote", "remove", "origin")
        assert fx.verdict(wt).reason == gc.R_UNPUSHED

    def test_a_failing_git_status_is_a_hold_not_a_pass(self, fx, monkeypatch):
        wt = _pushed_worktree(fx)
        real = gc.run_git

        def failing(cwd, *args):
            if args and args[0] == "status":
                return None, "", "OSError: boom"
            return real(cwd, *args)
        monkeypatch.setattr(gc, "run_git", failing)
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_STATUS_FAILED)

    def test_a_failing_rev_list_is_a_hold_not_a_pass(self, fx, monkeypatch):
        wt = _pushed_worktree(fx)
        real = gc.run_git

        def failing(cwd, *args):
            if args and args[0] == "rev-list":
                return 128, "", "fatal: bad object"
            return real(cwd, *args)
        monkeypatch.setattr(gc, "run_git", failing)
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_HEAD_UNRESOLVED)

    def test_every_condition_removed_one_at_a_time_gives_exactly_that_reason(self, fx):
        """1 つだけ欠いた worktree は、それぞれ別の理由で keep になる (どの条件も他が肩代わりしない)。"""
        clean = _pushed_worktree(fx, "clean")
        dirty = _pushed_worktree(fx, "dirty")
        (dirty / "x").write_text("x\n")
        unpushed = _pushed_worktree(fx, "unpushed")
        fx.commit(unpushed, "y.txt")
        ignored = _pushed_worktree(fx, "ignored")
        (ignored / ".gitignore").write_text("secret.local\n")
        git(ignored, "add", ".gitignore")
        git(ignored, "commit", "-q", "-m", "gitignore")
        fx.push(ignored)
        (ignored / "secret.local").write_text("x\n")
        got = fx.verdicts()
        assert got[str(clean)].action == gc.REMOVE
        assert got[str(dirty)].reason == gc.R_DIRTY
        assert got[str(unpushed)].reason == gc.R_UNPUSHED
        assert got[str(ignored)].reason == gc.R_IGNORED


class TestGitEnvIsolation:
    """`_git_env()` は `-C <path>` を上書きしうる GIT_* 環境変数を継承しない
    (族B — 検査した対象と実際に作用する対象が違う。PR#239 2巡目 P1)。"""

    @pytest.mark.parametrize("key", list(gc._GIT_REPO_LOCATION_ENV_VARS))
    def test_git_env_drops_repo_location_vars(self, monkeypatch, key):
        monkeypatch.setenv(key, "/somewhere/else")
        assert key not in gc._git_env()

    def test_an_ambient_git_dir_pointing_elsewhere_does_not_hide_a_dirty_candidate(self, fx, monkeypatch, tmp_path):
        """t058 QA の実機再現: 呼び出し元シェルに GIT_DIR/GIT_WORK_TREE が残っていても、
        判定は `-C` で指定した candidate worktree を見る (decoy を指すと dirty が消えて見えていた)。"""
        wt = _pushed_worktree(fx)
        (wt / "work.txt").write_text("edited\n")
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        subprocess.run(["git", "init", "-q", str(decoy)], env=_env(), check=True)
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
        v = fx.verdict(wt)
        assert (v.action, v.reason) == (gc.KEEP, gc.R_DIRTY), v


class TestParseWorktreeList:
    def test_all_the_record_kinds_and_paths_with_spaces(self):
        raw = ("worktree /r/main\0HEAD aaa\0branch refs/heads/main\0\0"
               "worktree /r/with space/wt\0HEAD bbb\0detached\0locked because reasons\0\0"
               "worktree /r/gone\0HEAD ccc\0branch refs/heads/x\0prunable gitdir file points to non-existent location\0\0"
               "worktree /r/bare\0bare\0\0")
        got = gc.parse_worktree_list(raw)
        assert [w.path for w in got] == ["/r/main", "/r/with space/wt", "/r/gone", "/r/bare"]
        assert got[0].branch == "refs/heads/main" and not got[0].locked
        assert got[1].detached and got[1].locked and got[1].branch is None
        assert got[1].locked_reason == "because reasons"
        assert got[0].locked_reason is None
        assert got[2].prunable and got[3].bare

    def test_an_empty_list_is_empty(self):
        assert gc.parse_worktree_list("") == []


# ---------------------------------------------------------------------------
# 4. --apply
# ---------------------------------------------------------------------------

def _branches(fx):
    return set(git(fx.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split())


def _worktree_paths(porcelain_listing):
    """`git worktree list --porcelain` の `worktree <path>` 行だけを集める。

    `locked <reason>` 行には隔離前の元の絶対パスが `orig=...` として埋め込まれるので、listing 全体への
    単純な部分文字列検索では「登録されているか」を確かめられない (元のパスが quarantine エントリの
    reason 内に文字列として現れるだけで in を誤検出する)。"""
    return {line[len("worktree "):] for line in porcelain_listing.splitlines() if line.startswith("worktree ")}


class TestApply:
    def test_dry_run_changes_nothing(self, fx):
        _pushed_worktree(fx, "t001")
        before = (git(fx.repo, "worktree", "list", "--porcelain").stdout, _branches(fx))
        r = fx.cli()
        assert r.returncode == 0, r.stderr
        assert "dry-run" in r.stdout and "remove 1" in r.stdout
        assert (git(fx.repo, "worktree", "list", "--porcelain").stdout, _branches(fx)) == before
        assert (fx.repo / ".claude" / "worktrees" / SLUG / "t001").is_dir()

    def test_apply_quarantines_the_removable_and_leaves_every_keep_untouched(self, fx):
        """`--apply` は消さず `.quarantine/<timestamp>/<元の相対パス>` へ move + lock する。"""
        gone = _pushed_worktree(fx, "gone")
        dirty = _pushed_worktree(fx, "dirty")
        (dirty / "x").write_text("x\n")
        unpushed = _pushed_worktree(fx, "unpushed")
        fx.commit(unpushed, "y.txt")
        active = fx.add("live", slug="20260202-live")
        fx.set_active(["20260202-live"])
        locked = _pushed_worktree(fx, "locked")
        git(fx.repo, "worktree", "lock", str(locked))
        r = fx.cli("--apply")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert not gone.exists()                                   # 元の場所には何も残らない
        for keep in (dirty, unpushed, active, locked, fx.repo):
            assert keep.is_dir(), keep
        listed = git(fx.repo, "worktree", "list", "--porcelain").stdout
        paths = _worktree_paths(listed)
        assert str(gone) not in paths and all(str(k) in paths for k in (dirty, unpushed, active, locked))
        assert (dirty / "x").read_text() == "x\n"                 # 未コミットの中身も無事
        entries = fx.quarantine_entries()
        assert len(entries) == 1
        wt, _ts, orig = entries[0]
        assert orig == str(gone) and wt.locked
        assert (pathlib.Path(wt.path) / "work.txt").is_file(), "隔離後もファイルは無事"
        assert f"task/{SLUG}/gone" in _branches(fx), "隔離では branch に触れない (削除しない)"

    def test_quarantine_never_calls_worktree_remove_or_branch_delete(self, fx, monkeypatch):
        _pushed_worktree(fx, "t001")
        calls = []
        real = gc.run_git

        def spy(cwd, *args):
            calls.append(args)
            return real(cwd, *args)
        monkeypatch.setattr(gc, "run_git", spy)
        rc = gc.main(["--repo", str(fx.repo), "--queue", str(fx.queue), "--apply"])
        assert rc == 0
        assert ("worktree", "remove") not in calls
        assert ("branch", "-d") not in calls and ("branch", "-D") not in calls
        assert any(c[:2] == ("worktree", "move") for c in calls)
        assert any(c[:3] == ("worktree", "lock", "--reason") for c in calls)

    def test_a_merged_and_an_unmerged_branch_both_survive_quarantine(self, fx):
        """削除しないので、`-d` が通る/通らないの区別自体が無くなる — どちらも branch はそのまま残る。"""
        merged = fx.add("merged")                                  # origin/main と同じ = merge 済み
        pushed_only = _pushed_worktree(fx, "pushed-only")          # push 済みだが main に未 merge
        r = fx.cli("--apply")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert not merged.exists() and not pushed_only.exists()   # 元の場所からは消える (隔離済み)
        branches = _branches(fx)
        assert f"task/{SLUG}/merged" in branches and f"task/{SLUG}/pushed-only" in branches
        assert len(fx.quarantine_entries()) == 2

    def test_a_detached_worktree_is_quarantined_without_touching_any_branch(self, fx):
        wt = fx.add("t001", branch=False)
        before = _branches(fx)
        assert fx.cli("--apply").returncode == 0
        assert not wt.exists() and _branches(fx) == before
        assert len(fx.quarantine_entries()) == 1

    def test_remote_branches_are_never_touched(self, fx):
        wt = _pushed_worktree(fx)
        fx.cli("--apply")
        assert f"task/{SLUG}/t001" in git(fx.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout

    def test_state_that_changed_since_the_dry_run_is_rejudged_before_quarantining(self, fx):
        """dry-run と --apply の間に Worker が戻ってきた・変更が入った → 隔離しない。"""
        wt = _pushed_worktree(fx)
        verdicts, _ = gc.classify_all(str(fx.repo), str(fx.queue))
        assert next(v for v in verdicts if v.path == str(wt)).action == gc.REMOVE
        (wt / "late.txt").write_text("late work\n")
        results = gc.apply_quarantine(str(fx.repo), str(fx.queue), verdicts)
        assert [r["status"] for r in results] == ["skipped"] and "dirty" in results[0]["detail"]
        assert (wt / "late.txt").read_text() == "late work\n"

    def test_a_mission_that_became_active_since_the_dry_run_is_not_quarantined(self, fx):
        wt = _pushed_worktree(fx)
        verdicts, _ = gc.classify_all(str(fx.repo), str(fx.queue))
        fx.set_active([SLUG])
        results = gc.apply_quarantine(str(fx.repo), str(fx.queue), verdicts)
        assert results[0]["status"] == "skipped" and gc.R_MISSION_ACTIVE in results[0]["detail"]
        assert wt.is_dir()

    def test_one_failure_does_not_stop_the_rest_and_exits_1(self, fx, monkeypatch, capsys):
        first, second = _pushed_worktree(fx, "a"), _pushed_worktree(fx, "b")
        real = gc.run_git

        def failing(cwd, *args):
            if args[:2] == ("worktree", "move") and args[2].endswith("/a"):
                return 1, "", "fatal: refusing"
            return real(cwd, *args)
        monkeypatch.setattr(gc, "run_git", failing)
        rc = gc.main(["--repo", str(fx.repo), "--queue", str(fx.queue), "--apply"])
        out = capsys.readouterr().out
        assert rc == 1 and "failed 1" in out and "quarantined 1" in out
        assert first.is_dir() and not second.exists()
        assert f"task/{SLUG}/a" in _branches(fx), "隔離できなかった worktree の branch には触れない"

    def test_a_worktree_move_failure_is_kept_not_deleted(self, fx, monkeypatch):
        """`git worktree move` が失敗する (submodule 等) → keep 相当にする。削除にフォールバックしない (族A)。"""
        wt = _pushed_worktree(fx)
        real = gc.run_git

        def failing(cwd, *args):
            if args[:2] == ("worktree", "move"):
                return 1, "", "fatal: refusing to move (submodule?)"
            return real(cwd, *args)
        monkeypatch.setattr(gc, "run_git", failing)
        rc = gc.main(["--repo", str(fx.repo), "--queue", str(fx.queue), "--apply"])
        assert rc == 1
        assert wt.is_dir(), "move が失敗しても元の worktree はそのまま残る"
        assert fx.quarantine_entries() == []

    def test_an_existing_quarantine_destination_refuses_the_move_instead_of_calling_it(self, fx, monkeypatch):
        """隔離先が既に存在する場合、`git worktree move` を呼ばずに拒否する
        (`git worktree move` は既存ディレクトリの中へ移すだけで失敗しないため、見落とすと誤配置になる)。

        `os.path.exists` を広く (".quarantine" を含むパス全部) モックすると `os.makedirs(exist_ok=True)`
        の内部の exists チェックまで壊れ、別の失敗経路 (親ディレクトリを作れない) で偶然 assert が通ってしまう
        (red proof で確認済み — defect を戻しても赤にならなかった)。`_quarantine_destination` を固定パスに
        差し替え、そのパスを実際に (空ディレクトリとして) 作っておく方が、意図した分岐だけを通す。"""
        wt = _pushed_worktree(fx)
        fixed_dest = fx.repo / ".claude" / "worktrees" / ".quarantine" / "fixed" / SLUG / "t001"
        fixed_dest.mkdir(parents=True)                             # 隔離先を実際に埋めておく
        monkeypatch.setattr(gc, "_quarantine_destination", lambda *a, **k: str(fixed_dest))

        real_run_git = gc.run_git
        calls = []

        def spy(cwd, *args):
            calls.append(args)
            return real_run_git(cwd, *args)
        monkeypatch.setattr(gc, "run_git", spy)

        rc = gc.main(["--repo", str(fx.repo), "--queue", str(fx.queue), "--apply"])
        assert rc == 1
        assert wt.is_dir()
        assert ("worktree", "move") not in calls, "隔離先が存在するときは move 自体を呼ばない"

    def test_a_prunable_worktree_inside_the_managed_dir_is_left_registered_after_apply(self, fx):
        """`prunable` (ディレクトリが無い) は keep 止まり (R_PRUNABLE)。--apply は repository-wide の
        `git worktree prune` を呼ばないので、一度も verdict をやり直していない対象は登録されたまま残る
        (旧実装はここで無条件に prune していた — 全 verdict が keep でも state.yaml が読めなくても走る P1)。"""
        wt = _pushed_worktree(fx)
        stale = _pushed_worktree(fx, "stale")
        subprocess.run(["mv", str(stale), str(stale) + ".moved"], check=True)
        r = fx.cli("--apply")
        assert r.returncode == 0, r.stderr
        assert not wt.exists()                                     # 通常の remove 対象は消える
        assert str(stale) in git(fx.repo, "worktree", "list", "--porcelain").stdout, \
            "検証していない prunable entry を勝手に消してはいけない"

    def test_apply_never_touches_a_foreign_worktree_outside_the_managed_dir(self, fx, tmp_path):
        """管理対象ディレクトリの外の worktree はこのツールが一度も verdict を出していない対象。
        `prunable` (移動中・一時的に見えないだけかもしれない) であっても --apply の影響を受けてはいけない
        (旧実装の repository-wide `git worktree prune` は、ここで未 push のコミットを守る HEAD・reflog を
        消しうった)。"""
        outside = tmp_path / "elsewhere" / "wt"
        git(fx.repo, "worktree", "add", "-q", "--detach", str(outside), "origin/main")
        subprocess.run(["mv", str(outside), str(outside) + ".moved"], check=True)
        r = fx.cli("--apply")
        assert r.returncode == 0, r.stderr
        assert str(outside) in git(fx.repo, "worktree", "list", "--porcelain").stdout

    def test_apply_never_invokes_git_worktree_prune(self, fx, monkeypatch):
        _pushed_worktree(fx, "t001")
        calls = []
        real = gc.run_git

        def spy(cwd, *args):
            calls.append(args)
            return real(cwd, *args)
        monkeypatch.setattr(gc, "run_git", spy)
        rc = gc.main(["--repo", str(fx.repo), "--queue", str(fx.queue), "--apply"])
        assert rc == 0
        assert ("worktree", "prune") not in calls

    def test_nothing_is_removed_when_the_worktree_list_cannot_be_read(self, fx, tmp_path):
        wt = _pushed_worktree(fx)
        r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(fx.repo), "--queue", str(fx.queue),
                            "--apply"], capture_output=True, text=True, env={**_env(), "PATH": str(tmp_path)})
        assert r.returncode == 1 and "取れませんでした" in r.stderr
        assert wt.is_dir()

    def test_an_unreadable_state_removes_nothing_even_with_apply(self, fx):
        wt = _pushed_worktree(fx)
        (fx.queue / "state.yaml").unlink()
        r = fx.cli("--apply")
        assert r.returncode == 0 and wt.is_dir()
        assert "state-unreadable" in r.stdout


# ---------------------------------------------------------------------------
# 5. 隔離済みエントリの再分類 (族B: 対象の同定)
# ---------------------------------------------------------------------------

class TestQuarantinedEntriesAreNotReprocessed:
    def test_an_already_quarantined_entry_is_kept_with_its_own_reason(self, fx):
        wt = _pushed_worktree(fx, "gone")
        fx.cli("--apply")
        entries = fx.quarantine_entries()
        assert len(entries) == 1
        v = fx.verdict(pathlib.Path(entries[0][0].path))
        assert (v.action, v.reason) == (gc.KEEP, gc.R_QUARANTINED)

    def test_a_second_apply_does_not_move_an_already_quarantined_entry_again(self, fx):
        _pushed_worktree(fx, "gone")
        fx.cli("--apply")
        before = fx.quarantine_entries()
        r = fx.cli("--apply")
        assert r.returncode == 0, r.stderr
        assert "quarantined 0" in r.stdout
        assert fx.quarantine_entries() == before

    def test_a_path_under_quarantine_without_the_lock_reason_is_not_treated_as_ours(self, fx):
        """パスが `.quarantine/` 配下「だけ」では十分でない (族B — 対象の同定は 2 つの一致で確かめる)。
        reason 無しで手動 lock されただけの何かは、通常の R_LOCKED として扱われる (=quarantine
        扱いにして黙って見落とさない)。"""
        wt = fx.repo / ".claude" / "worktrees" / ".quarantine" / "20260101-000000" / SLUG / "manual"
        wt.parent.mkdir(parents=True)
        git(fx.repo, "worktree", "add", "-q", "-b", "manual-branch", str(wt), "origin/main")
        git(fx.repo, "worktree", "lock", str(wt))                  # reason 無し
        assert fx.verdict(wt).reason == gc.R_LOCKED, "族B: パスだけでは quarantine 扱いにしない"

    def test_our_lock_reason_outside_the_quarantine_dir_is_not_treated_as_ours(self, fx):
        """reason がこのツールの印と一致「だけ」でも、パスが `.quarantine/` の外なら quarantine
        扱いにしない (通常の判定に進ませる — ここでは push 済みなので REMOVE になる)。"""
        wt = _pushed_worktree(fx, "t001")
        reason = gc._quarantine_reason("20260101-000000", str(wt))
        git(fx.repo, "worktree", "lock", "--reason", reason, str(wt))
        assert fx.verdict(wt).reason == gc.R_LOCKED, "族B: reason だけでは quarantine 扱いにしない"


# ---------------------------------------------------------------------------
# 6. --restore / --list-quarantine
# ---------------------------------------------------------------------------

class TestRestoreAndListQuarantine:
    def test_restore_by_the_original_path_fully_reverses_the_quarantine(self, fx):
        wt = _pushed_worktree(fx, "gone")
        fx.commit(wt, "extra.txt")
        fx.push(wt)                                                # push しないと unpushed で keep になる
        fx.cli("--apply")
        assert not wt.exists()
        r = fx.cli("--restore", str(wt))
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert wt.is_dir()
        assert (wt / "extra.txt").is_file()
        assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == f"task/{SLUG}/gone"
        assert fx.quarantine_entries() == []
        listed = git(fx.repo, "worktree", "list", "--porcelain").stdout
        assert str(wt) in listed and ".quarantine" not in listed

    def test_restore_by_the_quarantine_path_also_works(self, fx):
        wt = _pushed_worktree(fx, "gone")
        fx.cli("--apply")
        quarantine_path = fx.quarantine_entries()[0][0].path
        r = fx.cli("--restore", quarantine_path)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert wt.is_dir()

    def test_restore_refuses_when_the_original_location_is_occupied(self, fx):
        """元の場所に既に何かある場合は上書きせず拒否する (`git worktree move` は既存ディレクトリの
        中へ移すだけで失敗しないため、素通しすると誤配置になる)。"""
        wt = _pushed_worktree(fx, "gone")
        fx.cli("--apply")
        assert not wt.exists()
        wt.mkdir(parents=True)
        (wt / "someone-elses-file").write_text("don't touch me\n")
        r = fx.cli("--restore", str(wt))
        assert r.returncode == 1
        assert "既に何かある" in r.stderr
        assert (wt / "someone-elses-file").read_text() == "don't touch me\n"
        assert len(fx.quarantine_entries()) == 1, "拒否したら隔離側も変更しない"

    def test_restore_of_an_unknown_path_fails_clearly(self, fx):
        r = fx.cli("--restore", str(fx.repo / "no-such-thing"))
        assert r.returncode == 1
        assert "見つかりません" in r.stderr

    def test_ambiguous_restore_by_original_path_asks_for_the_quarantine_path(self, fx):
        """同じ元パスが 2 回隔離された場合 (2 回に分けて --apply した場合等)、元のパスだけでは
        一意に決まらないので、隔離先を直接指定するよう求める。"""
        wt = _pushed_worktree(fx, "gone")
        fx.cli("--apply")
        # 2 回目の隔離: 同じ元パスにもう一度 worktree を作り、再度隔離する。
        # `task/{SLUG}/gone` は 1 回目の隔離先にまだ残っている (隔離は branch を削除しない) ので、
        # 同名では `git worktree add -b` が失敗する。別の branch 名で同じパスに作る。
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(fx.repo, "worktree", "add", "-q", "-b", f"task/{SLUG}/gone-2", str(wt), "origin/main")
        wt2 = wt
        fx.commit(wt2)
        fx.push(wt2)
        fx.cli("--apply")
        assert len(fx.quarantine_entries()) == 2
        r = fx.cli("--restore", str(wt))
        assert r.returncode == 1
        assert "複数の隔離エントリ" in r.stderr

    def test_list_quarantine_is_empty_before_any_apply(self, fx):
        _pushed_worktree(fx, "t001")
        r = fx.cli("--list-quarantine")
        assert r.returncode == 0
        assert "ありません" in r.stdout

    def test_list_quarantine_json_shape(self, fx):
        wt = _pushed_worktree(fx, "gone")
        fx.cli("--apply")
        data = json.loads(fx.cli("--list-quarantine", "--json").stdout)
        assert len(data) == 1
        assert data[0]["original_path"] == str(wt)
        assert data[0]["path"] == fx.quarantine_entries()[0][0].path

    def test_apply_and_restore_and_list_quarantine_are_mutually_exclusive(self, fx):
        r = fx.cli("--apply", "--list-quarantine")
        assert r.returncode == 2
        r = fx.cli("--restore", "x", "--apply")
        assert r.returncode == 2


class TestOutput:
    def test_json_has_the_summary_and_one_row_per_worktree(self, fx):
        _pushed_worktree(fx, "a")
        dirty = _pushed_worktree(fx, "b")
        (dirty / "x").write_text("x\n")
        data = json.loads(fx.cli("--json").stdout)
        assert data["applied"] is None
        assert data["summary"] == {"total": 3, "remove": 1, "keep": 2,
                                   "keep_by_reason": {"dirty": 1, "main-checkout": 1}}
        assert {w["reason"] for w in data["worktrees"]} == {gc.R_REMOVE, gc.R_DIRTY, gc.R_MAIN}

    def test_text_lists_every_worktree_with_its_reason_and_a_summary(self, fx):
        wt = _pushed_worktree(fx)
        out = fx.cli().stdout
        assert re.search(rf"^remove\s+{gc.R_REMOVE}\s+{re.escape(str(wt))}$", out, re.M), out
        assert "worktree 2 件: remove 1 / keep 1" in out and "keep main-checkout: 1" in out

    def test_quiet_prints_only_the_summary(self, fx):
        _pushed_worktree(fx)
        out = fx.cli("--quiet").stdout
        assert str(fx.repo / ".claude") not in out and "remove 1" in out

    def test_a_worktree_path_with_a_space_survives(self, fx):
        wt = fx.add("has space", branch=False)
        v = fx.verdict(wt)
        assert v.action == gc.REMOVE


# ---------------------------------------------------------------------------
# 5. 破壊的な操作を使わない (構造で固定)
# ---------------------------------------------------------------------------

class TestNoForcefulOperations:
    """`--force` / `-D` / `rm -rf` (rmtree・unlink・remove) を使わない。コメントと docstring は数えない。"""

    def _tree(self):
        return ast.parse(SCRIPT.read_text())

    def test_no_call_deletes_files_directly(self):
        banned = {"rmtree", "remove", "rmdir", "unlink", "removedirs", "rename", "replace", "system", "popen"}
        hits = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in banned:
                hits.append((node.lineno, node.func.attr))
        assert not hits, f"ファイルを直接消す・動かす呼び出しがある: {hits}"

    def test_git_is_only_called_with_the_allowed_verbs_and_flags(self):
        """`run_git(...)` の literal 引数から、使う verb / flag を洗い出す。想定外の 1 語で赤。"""
        verbs, flags = set(), set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_git":
                consts = [a.value for a in node.args[1:] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if consts:
                    verbs.add(consts[0])
                    flags.update(c for c in consts[1:] if c.startswith("-"))
        # t071 (PR#239 3巡目): --apply は削除ではなく隔離になったので `branch` / `worktree remove` は
        # 消え、代わりに `config` (core.ignoreStat 検査) / `ls-files` (index フラグ検査) が増えた。
        assert verbs == {"worktree", "status", "rev-list", "fetch", "config", "ls-files"}, verbs
        assert flags <= {"--porcelain", "-z", "--porcelain=v1", "--untracked-files=all", "--ignored=matching",
                         "--max-count=1", "--not", "--remotes=origin", "--prune", "--bool", "-v", "--reason"}, flags
        for banned in ("--force", "-f", "-D", "-d", "--delete", "--hard", "-B"):
            assert banned not in flags

    def test_no_deleting_git_verb_is_called_at_all(self):
        """隔離設計 (t071) では削除操作そのものが無い — `worktree remove` / `branch -d` / `-D` / `prune`。"""
        calls = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_git":
                consts = tuple(a.value for a in node.args[1:] if isinstance(a, ast.Constant))
                calls.append(consts)
        sub_verbs = {c[:2] for c in calls}
        assert ("worktree", "remove") not in sub_verbs
        assert ("worktree", "prune") not in sub_verbs, \
            "repository-wide の `git worktree prune` は呼ばない (検証していない他の worktree まで消しうる)"
        assert not any(c[:1] == ("branch",) for c in calls), "隔離では branch に触れない (削除しない)"
        for c in calls:
            if c[:1] == ("worktree",):
                assert c[1] in {"list", "move", "lock", "unlock"}, c
