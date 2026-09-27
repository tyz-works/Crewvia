"""t017 (mission 20260926-mechanize-guards-a / PR5a, backlog #14):
起動まわりで Director が手で直していたものを機械にする。

1. `lib_registry.add_skills()` — start.sh が起動時に渡された skills を registry の
   当該 Worker に **和集合で** 足す。dispatcher は task の skills を registry と
   突き合わせるので、registry が古いと「起動要求 ⇄ 仕事なし退役」が互いを
   打ち消した (4 人で発生)。消すのではなく足す: registry には過去の担当で得た
   skill も入っている。
2. `lib_mux` の `spawn(env=)` の廃止 — 両 backend が黙って捨てていた引数。
   渡した呼び出し元は「env の付いた pane」を得たつもりで、実際には付いていなかった。
   引数ごと無くし、渡せば TypeError。呼び出し元の全数を AST で機械的に確かめる。

実行方法:
  python3 -m pytest tests/test_registry_skills_and_spawn_env.py -v
"""

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib_mux  # noqa: E402
import lib_registry  # noqa: E402

REGISTRY_TEXT = """\
# registry/workers.yaml (test fixture)
# header comment that must survive a rewrite

workers:
  - name: Ren
    skills: [code, python]
    task_count: 7
    last_active: 2026-09-01
  - name: Sora
    role: director
    skills: []
    task_count: 4
    last_active: 2026-09-01
  - name: Hana
    skills: [bash, ops]
    task_count: 5
    last_active: 2026-08-25
"""


@pytest.fixture
def registry(tmp_path):
    """An isolated registry — never the real one."""
    path = tmp_path / "registry" / "workers.yaml"
    path.parent.mkdir()
    path.write_text(REGISTRY_TEXT, encoding="utf-8")
    return path


def _skills(path, name):
    _, _, by_name = lib_registry.parse(str(path))
    return by_name[name]["skills"]


# ---------------------------------------------------------------------------
# add_skills — both sides of the union
# ---------------------------------------------------------------------------

def test_launch_skills_missing_from_the_registry_are_added(registry):
    """The bug: Ren `[code, python]` launched with `code python bash` — bash was
    never written, so a bash task was never assigned to Ren."""
    added = lib_registry.add_skills(str(registry), "Ren", ["code", "python", "bash"])

    assert added == ["bash"]
    assert _skills(registry, "Ren") == ["code", "python", "bash"]


def test_a_skill_only_the_registry_has_is_not_removed(registry):
    """The other side of the union: a launch with a SUBSET must not shrink the
    registry entry.  Hana earned `ops` on an earlier task."""
    added = lib_registry.add_skills(str(registry), "Hana", ["bash"])

    assert added == []
    assert _skills(registry, "Hana") == ["bash", "ops"]


def test_disjoint_launch_keeps_the_old_skills_and_appends_the_new_ones(registry):
    lib_registry.add_skills(str(registry), "Hana", ["qa", "python"])

    assert _skills(registry, "Hana") == ["bash", "ops", "qa", "python"]


def test_a_launch_that_adds_nothing_does_not_rewrite_the_file(registry):
    """Every Worker start goes through here; the common case (registry already
    covers the launch) must not touch the file or take a needless write."""
    before = registry.read_bytes()
    mtime = registry.stat().st_mtime_ns

    assert lib_registry.add_skills(str(registry), "Ren", ["python", "code"]) == []

    assert registry.read_bytes() == before
    assert registry.stat().st_mtime_ns == mtime


def test_duplicates_within_one_launch_are_added_once(registry):
    lib_registry.add_skills(str(registry), "Ren", ["bash", "bash", " bash "])

    assert _skills(registry, "Ren") == ["code", "python", "bash"]


def test_other_workers_and_the_other_fields_are_untouched(registry):
    lib_registry.add_skills(str(registry), "Ren", ["bash"])

    _, order, by_name = lib_registry.parse(str(registry))
    assert order == ["Ren", "Sora", "Hana"]
    assert by_name["Ren"]["task_count"] == 7
    assert by_name["Ren"]["last_active"] == "2026-09-01"
    assert by_name["Sora"] == {"name": "Sora", "role": "director", "skills": [],
                               "task_count": 4, "last_active": "2026-09-01"}
    assert by_name["Hana"]["skills"] == ["bash", "ops"]
    assert registry.read_text(encoding="utf-8").startswith(
        "# registry/workers.yaml (test fixture)\n# header comment that must survive")


def test_an_unregistered_name_is_a_noop_like_set_last_active(registry):
    before = registry.read_bytes()

    assert lib_registry.add_skills(str(registry), "Nobody", ["code"]) == []

    assert registry.read_bytes() == before


def test_a_missing_registry_is_a_noop_not_a_creation(tmp_path):
    path = tmp_path / "registry" / "workers.yaml"

    assert lib_registry.add_skills(str(path), "Ren", ["code"]) == []

    assert not path.exists()


@pytest.mark.parametrize("bad", ["a,b", "x]", "y #z", "has space", "-lead", "", "  "])
def test_a_tag_that_cannot_be_written_back_is_skipped_not_written(registry, bad, capsys):
    """A tag with `,` `]` `#` or whitespace would corrupt the flow list that
    `parse()` reads back with a regex.  Skipped, and said so — not silently."""
    lib_registry.add_skills(str(registry), "Ren", [bad, "bash"])

    assert _skills(registry, "Ren") == ["code", "python", "bash"]
    if bad.strip():
        assert "WARNING" in capsys.readouterr().err


def test_the_whole_cycle_runs_under_the_registry_lock(registry, monkeypatch):
    """parse → modify → write inside `with_lock`, like every other writer (a
    parse outside the lock is the lost-update race `with_lock` documents)."""
    seen = []
    real_parse = lib_registry.parse
    real_lock = lib_registry.with_lock
    state = {"locked": False}

    def spy_lock(path, callback):
        def wrapped():
            state["locked"] = True
            try:
                return callback()
            finally:
                state["locked"] = False
        seen.append("lock")
        return real_lock(path, wrapped)

    def spy_parse(path):
        seen.append(("parse", state["locked"]))
        return real_parse(path)

    monkeypatch.setattr(lib_registry, "with_lock", spy_lock)
    monkeypatch.setattr(lib_registry, "parse", spy_parse)

    lib_registry.add_skills(str(registry), "Ren", ["bash"])

    assert seen == ["lock", ("parse", True)]


# ---------------------------------------------------------------------------
# CLI — what start.sh actually calls
# ---------------------------------------------------------------------------

def _cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "lib_registry.py"), *args],
        capture_output=True, text=True, timeout=30)


def test_cli_add_skills_unions_and_reports(registry):
    r = _cli("add-skills", str(registry), "Ren", "code", "python", "bash")

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "added: bash"
    assert _skills(registry, "Ren") == ["code", "python", "bash"]

    again = _cli("add-skills", str(registry), "Ren", "code", "bash")
    assert again.returncode == 0
    assert again.stdout.strip() == "no-op"


def test_cli_add_skills_needs_at_least_one_skill(registry):
    r = _cli("add-skills", str(registry), "Ren")

    assert r.returncode == 2
    assert "usage: add-skills" in r.stderr


# ---------------------------------------------------------------------------
# spawn(env=) is gone
# ---------------------------------------------------------------------------

SPAWN_DEFINITIONS = [
    lib_mux._Backend,
    lib_mux.TmuxBackend,
    lib_mux.HerdrBackend,
    lib_mux.Mux,
]


@pytest.mark.parametrize("cls", SPAWN_DEFINITIONS, ids=lambda c: c.__name__)
def test_spawn_has_no_env_parameter(cls):
    assert list(inspect.signature(cls.spawn).parameters) == ["self", "name", "cmd", "cwd"]


@pytest.mark.parametrize("cls", SPAWN_DEFINITIONS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("value", [{"FOO": "1"}, {}, None])
def test_passing_env_to_spawn_raises_type_error(cls, value):
    """Binding fails before the body runs, so no mux is touched.  `None` counts:
    a caller that names the argument at all is the one to find."""
    with pytest.raises(TypeError, match="env"):
        cls.spawn(object(), "name", "cmd", cwd=None, env=value)


def test_a_fourth_positional_argument_is_refused_too():
    with pytest.raises(TypeError):
        lib_mux.Mux.spawn(object(), "name", "cmd", "/tmp", {"FOO": "1"})


def _spawn_env_uses(tree):
    """Every way of handing `env` to a `spawn` — or declaring that a `spawn`
    accepts one — found in a parsed module.

      - `x.spawn(..., env=...)` / `spawn(..., env=...)`
      - `x.spawn(a, b, c, d)`  (env by position: a 4th positional argument)
      - `def spawn(..., env=None)`  (a fake that mirrors the old signature: it
        would keep accepting what the real backends now refuse, so a regression
        would pass in the tests and fail only in production)
    """
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "spawn":
                if any(k.arg == "env" for k in node.keywords):
                    found.append((node.lineno, "spawn(env=...)"))
                if len(node.args) > 3:
                    found.append((node.lineno, "spawn(<4th positional>)"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "spawn":
            args = node.args
            if any(a.arg == "env" for a in args.args + args.kwonlyargs):
                found.append((node.lineno, "def spawn(..., env)"))
    return found


def test_the_scanner_finds_each_way_of_passing_env():
    """Positive control: a scan that finds nothing proves nothing until it is
    shown to find something."""
    bad = ast.parse(
        "m.spawn('n', 'c', env={})\n"
        "spawn('n', 'c', cwd=None, env=None)\n"
        "m.spawn('n', 'c', '/tmp', {})\n"
        "class F:\n"
        "    def spawn(self, name, cmd, cwd=None, env=None):\n"
        "        pass\n"
    )
    assert [what for _, what in _spawn_env_uses(bad)] == [
        "spawn(env=...)", "spawn(env=...)", "spawn(<4th positional>)", "def spawn(..., env)"]

    ok = ast.parse(
        "m.spawn('n', 'c', cwd='/x')\n"
        "dw.spawn_command('dispatcher', root, env={})\n"   # a different function
        "class F:\n"
        "    def spawn(self, name, cmd, cwd=None):\n"
        "        pass\n"
    )
    assert _spawn_env_uses(ok) == []


#: The one file allowed to pass `env` to `spawn`: the tests above do it on
#: purpose, to prove it raises.  Excluded by path, not by pattern.
_DELIBERATE = Path(__file__).resolve()


def _python_files():
    for base in ("scripts", "hooks", "tests", "agents", "config"):
        root = REPO_ROOT / base
        if root.is_dir():
            yield from (p for p in sorted(root.rglob("*.py")) if p.resolve() != _DELIBERATE)
    yield from sorted(REPO_ROOT.glob("*.py"))


def test_no_caller_passes_env_to_spawn():
    """The exhaustive grep the task asked for, kept as a test: every Python file
    in the repo parses, and none of them passes (or accepts) `env` on `spawn`.
    A new caller that does so fails here instead of getting a pane without it."""
    scanned = 0
    offenders = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:  # a file that cannot be read cannot be cleared
            offenders.append(f"{path.relative_to(REPO_ROOT)}: does not parse ({e})")
            continue
        scanned += 1
        for lineno, what in _spawn_env_uses(tree):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {what}")

    assert scanned > 50, f"only {scanned} files scanned — the scan is not looking where it should"
    assert offenders == []


def test_the_bash_entry_points_have_no_env_either():
    """`lib_mux.py spawn <name> <cmd> [<cwd>]` — the only door bash has — takes no
    env, and neither does the `mux_spawn` wrapper."""
    cli = subprocess.run(
        [sys.executable, str(SCRIPTS / "lib_mux.py"), "spawn", "only-one-arg"],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "CREWVIA_MUX_TEST_ISOLATION": "1"})
    assert cli.returncode == 2
    assert "Usage: lib_mux.py spawn <name> <cmd> [<cwd>]" in cli.stderr

    wrapper = (SCRIPTS / "lib_mux.sh").read_text(encoding="utf-8")
    assert "mux_spawn <name> <cmd> [<cwd>]" in wrapper
