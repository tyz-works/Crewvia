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
BARE_GIT = re.compile(r"^[^#]*\bgit\s+(-C\s+\S+\s+)?(fetch|update-ref|merge --ff-only|worktree\s+(add|remove|list))\b")


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


# ---- t001 (mission 20261113-wt-lock-rest): python 側 (デーモン・worktree_gc) と sync-main-checkout.sh ----
import sys  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
import lib_worktree_lock as pylock  # noqa: E402


def _hold(path, secs=30):
    h = subprocess.Popen(["bash", "-c", f'exec 9>>"{path}"; flock 9; echo held; sleep {secs}'],
                         stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert h.stdout.readline().strip() == "held"
    return h


@pytest.mark.structural
def test_python_side_has_no_flock_of_its_own():
    """python の入口はロックを自前で定義しない (bash の crewvia_wt_git を呼ぶだけ)。"""
    for name in ("lib_worktree_lock.py", "lib_daemon_watch.py", "worktree_gc.py"):
        code = [l for l in (SCRIPTS / name).read_text().splitlines() if not l.lstrip().startswith("#")]
        assert not [l for l in code if re.search(r"fcntl\.flock|flock\s+-", l)] or name == "lib_daemon_watch.py", name
    assert "crewvia_wt_git" in (SCRIPTS / "lib_worktree_lock.py").read_text()


@pytest.mark.structural
def test_bare_git_fetch_and_worktree_ops_are_gone_from_daemons_and_sync():
    w = (SCRIPTS / "lib_daemon_watch.py").read_text()
    assert '"fetch", remote, branch],\n            capture_output' not in w
    assert "wt_git(" in w
    sync = [(n, l.strip()) for n, l in _code_lines(SCRIPTS / "sync-main-checkout.sh") if BARE_GIT.search(l)]
    assert sync == [], sync
    assert "merge --ff-only origin/main" in (SCRIPTS / "sync-main-checkout.sh").read_text()
    assert "crewvia_wt_git . merge --ff-only origin/main" in (SCRIPTS / "sync-main-checkout.sh").read_text()


def test_wt_git_fail_closed_when_lock_held(tmp_path):
    c = _clone(tmp_path)
    h = _hold(f"{c}/.git/crewvia-worktree.lock")
    try:
        rc, out, err = pylock.wt_git(c, "worktree", "list", lock_wait=1, timeout=30)
        assert rc == pylock.LOCK_UNAVAILABLE
    finally:
        os.killpg(h.pid, 9)
        h.wait()
    rc, out, err = pylock.wt_git(c, "worktree", "list", lock_wait=1, timeout=30)
    assert rc == 0 and str(c).encode() in out


def test_fetch_origin_returns_false_without_running_git_when_lock_held(tmp_path):
    import lib_daemon_watch as w
    c = _clone(tmp_path)
    assert w.fetch_origin(c) is True
    h = _hold(f"{c}/.git/kai-review-worktree.lock")
    try:
        assert w.fetch_origin(c) is False
    finally:
        os.killpg(h.pid, 9)
        h.wait()


def test_worktree_gc_run_git_fails_closed_when_lock_held(tmp_path):
    import worktree_gc as gc
    c = _clone(tmp_path)
    h = _hold(f"{c}/.git/crewvia-worktree.lock")
    old = os.environ.get("CREWVIA_WT_LOCK_WAIT_SECONDS")
    os.environ["CREWVIA_WT_LOCK_WAIT_SECONDS"] = "1"
    try:
        rc, _o, _e = gc.run_git(str(c), "worktree", "list", "--porcelain", "-z")
        assert rc == 99
        rc2, _o, _e = gc.run_git(str(c), "rev-parse", "HEAD")   # repo を列挙しない読み取りはロック不要
        assert rc2 == 0
    finally:
        os.killpg(h.pid, 9)
        h.wait()
        if old is None:
            os.environ.pop("CREWVIA_WT_LOCK_WAIT_SECONDS")
        else:
            os.environ["CREWVIA_WT_LOCK_WAIT_SECONDS"] = old


def test_remove_worktree_fails_when_list_cannot_be_taken(tmp_path):
    """ロックが取れないとき crewvia_remove_worktree は「無い」と読んで rc=0 で skip せず、失敗を返す。"""
    c = _clone(tmp_path)
    copy_plan_tree(c)
    (c / "scripts" / "git-helpers.sh").write_text((SCRIPTS / "git-helpers.sh").read_text())
    q = tmp_path / "queue" / "missions" / "m-lock" / "tasks"
    q.mkdir(parents=True)
    (q.parent / "mission.yaml").write_text("slug: m-lock\n")
    h = _hold(f"{c}/.git/crewvia-worktree.lock", 60)
    try:
        env = {**os.environ, "CREWVIA_QUEUE": str(tmp_path / "queue"), "CREWVIA_WT_LOCK_WAIT_SECONDS": "1"}
        r = subprocess.run(["bash", "-c", f'cd "{c}" && source scripts/git-helpers.sh && crewvia_remove_worktree m-lock t001 slug'],
                           capture_output=True, text=True, timeout=120, env=env)
        assert r.returncode != 0, r.stderr
        assert "skipping" not in r.stderr
    finally:
        os.killpg(h.pid, 9)
        h.wait()


def test_git_child_does_not_inherit_the_lock_fds(tmp_path):
    """git が裏に残す子 (maintenance --auto 等) はロックの fd を継がない: crewvia_wt_git が戻ったらロックは取れる。"""
    c = _clone(tmp_path)
    real = shutil.which("git")
    stub = tmp_path / "stubbin"
    stub.mkdir()
    pidfile = tmp_path / "child.pid"
    (stub / "git").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        for a in "$@"; do
          if [[ "$a" == rev-parse ]]; then exec "{real}" "$@"; fi
        done
        # detach した子 (fd は継ぐ) を残して親は終わる
        sleep 30 </dev/null >/dev/null 2>&1 &
        echo $! > "{pidfile}"
        exit 0
        """))
    (stub / "git").chmod(0o755)
    try:
        r = _bash(f'source "{LIB}"; PATH="{stub}:$PATH" crewvia_wt_git "{c}" fetch origin')
        assert r.returncode == 0, r.stderr
        for name in ("crewvia-worktree.lock", "kai-review-worktree.lock"):
            lock = c / ".git" / name
            got = _bash(f'flock -n -w 0 "{lock}" true')
            assert got.returncode == 0, f"{name} が git の子に握られたまま"
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text().strip()), 9)
            except (OSError, ValueError):
                pass


def test_remove_worktree_does_not_match_a_longer_sibling_path(tmp_path):
    """…/task-foo を消すとき …/task-foo-bar の行を「在る」と読まない (部分一致): 無ければ skip して rc=0、sibling は残る。"""
    c = _clone(tmp_path)
    copy_plan_tree(c)
    (c / "scripts" / "git-helpers.sh").write_text((SCRIPTS / "git-helpers.sh").read_text())
    q = tmp_path / "queue" / "missions" / "m-sub" / "tasks"
    q.mkdir(parents=True)
    (q.parent / "mission.yaml").write_text("slug: m-sub\n")
    env = {**os.environ, "CREWVIA_QUEUE": str(tmp_path / "queue")}
    probe = subprocess.run(["bash", "-c", f'cd "{c}" && source scripts/git-helpers.sh && '
                            '_crewvia_resolve_task m-sub t001 foo "$(_crewvia_repo_root)"'],
                           capture_output=True, text=True, timeout=120, env=env)
    assert probe.returncode == 0, probe.stderr
    wt = probe.stdout.splitlines()[1]
    sibling = wt + "-bar"
    _git(c, "worktree", "add", "-q", "--detach", sibling, "HEAD")
    r = subprocess.run(["bash", "-c", f'cd "{c}" && source scripts/git-helpers.sh && crewvia_remove_worktree m-sub t001 foo'],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stderr
    assert "skipping" in r.stderr, r.stderr
    assert Path(sibling).is_dir()


# ---- t001 (mission 20261114-wt-lock-held-test): git の実行「中」もロックが握られている ----
def _probing_git_stub(tmp_path):
    """rev-parse は本物へ。それ以外は、旧名・新名の両方に flock -n を試して結果を書く stub の git。

    stub は fd 8/9 を継いでいない (crewvia_wt_git が 8>&- 9>&- で閉じる) ので、ここでの flock -n は
    別の open file description からの取得になる。握っているのは subshell だけ。
    subshell の最後のコマンドが exec で git に置き換わると、その fd が閉じてここで取れてしまう。
    """
    real = shutil.which("git")
    stub = tmp_path / "stubbin"
    stub.mkdir()
    out = tmp_path / "probe.txt"
    (stub / "git").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        for a in "$@"; do
          if [[ "$a" == rev-parse ]]; then exec "{real}" "$@"; fi
        done
        gcd="$("{real}" -C "${{@:2:1}}" rev-parse --path-format=absolute --git-common-dir)"
        : > "{out}"
        for name in kai-review-worktree.lock crewvia-worktree.lock; do
          flock -n "$gcd/$name" true
          echo "$name $?" >> "{out}"
        done
        exit 0
        """))
    (stub / "git").chmod(0o755)
    return stub, out


def _assert_both_locks_were_held(out: Path):
    got = dict(l.split() for l in out.read_text().splitlines())
    assert set(got) == {"kai-review-worktree.lock", "crewvia-worktree.lock"}, got
    # flock -n が取れなかった (rc=1) = git の実行中もロックが握られている。0 なら外れている。
    assert got == {"kai-review-worktree.lock": "1", "crewvia-worktree.lock": "1"}, got


def test_both_locks_are_held_while_git_runs(tmp_path):
    c = _clone(tmp_path)
    stub, out = _probing_git_stub(tmp_path)
    r = _bash(f'source "{LIB}"; PATH="{stub}:$PATH" crewvia_wt_git "{c}" fetch origin')
    assert r.returncode == 0, r.stderr
    _assert_both_locks_were_held(out)


def test_both_locks_are_held_while_git_runs_via_python_entry(tmp_path):
    c = _clone(tmp_path)
    stub, out = _probing_git_stub(tmp_path)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    rc, _o, err = pylock.wt_git(c, "fetch", "origin", timeout=60, env=env)
    assert rc == 0, err
    _assert_both_locks_were_held(out)
