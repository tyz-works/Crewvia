"""repo 単位の worktree ロック (scripts/lib_worktree_lock.sh の crewvia_wt_git) を kai-review.sh と git-helpers.sh が共有する (t001)。

- 構造: 両ファイルの worktree add / remove・fetch・update-ref -d・worktree list は crewvia_wt_git 経由 (ロックの外に残さない)。
  ロックの定義 (flock) は lib_worktree_lock.sh の 1 か所だけ。
- fail-closed: 別プロセスがロックを握っている間、crewvia_create_worktree は git を実行せず W5 で失敗する。
- 同時実行: 実 git の add / remove ループと fetch ループを並行に走らせ、ロックありなら落ちない。
"""
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from fixture_tree import copy_plan_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
LIB = SCRIPTS / "lib_worktree_lock.sh"

pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="flock が無い環境")

# ロックの外に残してはいけない git 呼び出し
BARE_GIT = re.compile(r"^[^#]*\bgit\s+(-C\s+\S+\s+)?(fetch|update-ref|worktree\s+(add|remove|list))\b")


def _code_lines(path: Path):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        # echo / 診断メッセージの文字列中の "git worktree add failed" は呼び出しではない
        if re.match(r"\s*echo\b", line):
            continue
        # 引用符の中 (診断メッセージの "git fetch ... failed" 等) は呼び出しではない。中身だけ Q に潰す
        yield n, re.sub(r'"[^"]*"', '"Q"', line)


@pytest.mark.structural
@pytest.mark.parametrize("name", ["kai-review.sh", "git-helpers.sh"])
def test_no_bare_worktree_git_outside_the_lock(name):
    bare = [(n, l.strip()) for n, l in _code_lines(SCRIPTS / name) if BARE_GIT.search(l)]
    assert bare == [], f"ロックの外の git 呼び出し: {bare}"


@pytest.mark.structural
def test_flock_is_defined_only_in_the_shared_lib():
    for name in ("kai-review.sh", "git-helpers.sh"):
        assert not [n for n, l in _code_lines(SCRIPTS / name) if re.search(r"\bflock\b", l)], name
    assert any("flock" in l for _, l in _code_lines(LIB))
    for name in ("kai-review.sh", "git-helpers.sh"):
        assert "lib_worktree_lock.sh" in (SCRIPTS / name).read_text()


@pytest.mark.structural
def test_lock_takes_legacy_then_new_name_in_fixed_order():
    src = LIB.read_text()
    assert src.index('LEGACY_LOCK_NAME}"') < src.index('LOCK_NAME}"', src.index('LEGACY_LOCK_NAME}"') + 1)
    assert 'CREWVIA_WT_LOCK_NAME="crewvia-worktree.lock"' in src
    assert 'CREWVIA_WT_LEGACY_LOCK_NAME="kai-review-worktree.lock"' in src


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout


def _clone(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    c = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(c)], check=True, capture_output=True)
    _git(c, "commit", "-q", "--allow-empty", "-m", "init")
    _git(c, "push", "-q", "origin", "HEAD:main")
    return c


def _bash(script: str, **kw):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=300, **kw)


def test_held_lock_makes_helper_fail_closed(tmp_path):
    """別プロセスがロックを握っている間は git を実行せず失敗する (終了コード 99)。"""
    c = _clone(tmp_path)
    gcd = c / ".git"
    holder = subprocess.Popen(
        ["bash", "-c", f'exec 9>>"{gcd}/crewvia-worktree.lock"; flock 9; echo held; sleep 30'],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        r = _bash(f'source "{LIB}"; CREWVIA_WT_LOCK_WAIT_SECONDS=1 crewvia_wt_git "{c}" worktree add --detach "{tmp_path}/w" HEAD')
        assert r.returncode == 99, r.stderr
        assert not (tmp_path / "w").exists()
    finally:
        os.killpg(holder.pid, 9)
        holder.wait()


def test_legacy_lock_held_by_old_code_also_blocks(tmp_path):
    """旧コード (kai-review-worktree.lock だけを取る) の review が走っている間も排他される。"""
    c = _clone(tmp_path)
    holder = subprocess.Popen(
        ["bash", "-c", f'exec 9>>"{c}/.git/kai-review-worktree.lock"; flock 9; echo held; sleep 30'],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        r = _bash(f'source "{LIB}"; CREWVIA_WT_LOCK_WAIT_SECONDS=1 crewvia_wt_git "{c}" worktree list')
        assert r.returncode == 99
    finally:
        os.killpg(holder.pid, 9)
        holder.wait()


def test_create_worktree_fails_w5_when_lock_is_unavailable(tmp_path):
    """git-helpers.sh の crewvia_create_worktree は、ロックが取れないとき W5 で失敗し worktree を作らない。"""
    c = _clone(tmp_path)
    copy_plan_tree(c)   # lib_* を glob で写す (lib_worktree_lock.sh も付いてくる)
    (c / "scripts" / "git-helpers.sh").write_text((SCRIPTS / "git-helpers.sh").read_text())   # stub を本物に
    q = tmp_path / "queue" / "missions" / "m-lock" / "tasks"
    q.mkdir(parents=True)
    (q.parent / "mission.yaml").write_text("slug: m-lock\n")
    _git(c, "update-ref", "refs/remotes/origin/main", "HEAD")
    holder = subprocess.Popen(
        ["bash", "-c", f'exec 9>>"{c}/.git/crewvia-worktree.lock"; flock 9; echo held; sleep 60'],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        env = {**os.environ, "CREWVIA_QUEUE": str(tmp_path / "queue"), "CREWVIA_WT_LOCK_WAIT_SECONDS": "1"}
        r = subprocess.run(
            ["bash", "-c", f'cd "{c}" && source scripts/git-helpers.sh && crewvia_create_worktree m-lock t001 slug'],
            capture_output=True, text=True, timeout=120, env=env)
        assert r.returncode != 0
        assert "W5" in r.stderr.strip().splitlines()[-1], r.stderr
        assert r.stdout.strip() == ""
        assert not list((c / ".claude").glob("**/t001-slug")) if (c / ".claude").exists() else True
    finally:
        os.killpg(holder.pid, 9)
        holder.wait()


RACE = textwrap.dedent('''
    source "$LIB"
    cd "$CLONE"
    g() { crewvia_wt_git "$CLONE" "$@"; }
    : > "$OUT/fails"
    ( for i in $(seq 1 40); do g worktree add -q --detach "$OUT/a_$i" HEAD 2>>"$OUT/err" || echo A >>"$OUT/fails"
                               g worktree remove --force "$OUT/a_$i" 2>>"$OUT/err" || echo R >>"$OUT/fails"; done ) &
    ( for i in $(seq 1 40); do g worktree add -q --detach "$OUT/b_$i" HEAD 2>>"$OUT/err" || echo A >>"$OUT/fails"
                               g worktree remove --force "$OUT/b_$i" 2>>"$OUT/err" || echo R >>"$OUT/fails"; done ) &
    ( for i in $(seq 1 40); do g fetch -q origin 2>>"$OUT/err" || echo F >>"$OUT/fails"
                               g worktree list >/dev/null 2>>"$OUT/err" || echo L >>"$OUT/fails"; done ) &
    wait
''')


def test_concurrent_add_remove_and_fetch_loops_do_not_fail_with_the_lock(tmp_path):
    """ロック付きの add / remove ループ 2 本と fetch / list ループが同時に走っても 1 回も落ちない
    (ロックなしだと同じ形が数回に 1 回の割合で commondir / bad object で落ちることを実測した。PR 参照)。"""
    for rnd in range(3):
        base = tmp_path / f"r{rnd}"
        base.mkdir()
        c = _clone(base)
        out = base / "out"
        out.mkdir()
        r = _bash(RACE, env={**os.environ, "LIB": str(LIB), "CLONE": str(c), "OUT": str(out)})
        assert r.returncode == 0, r.stderr
        fails = (out / "fails").read_text().split()
        assert fails == [], (fails, (out / "err").read_text()[:500])
