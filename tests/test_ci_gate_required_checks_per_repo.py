"""repo ごとの必須 check (`config` の `ci.required_checks`) と `no_required_check_config`。

固定すること: crewvia 本体 (scope `''`) は config が無くても・壊れていても `ci-ok` のまま / 他の repo は config に載せた
check が**全部**記録にあり passed のときだけ通る / 未設定は `no_required_check_config` / 壊れた config・解けない `dir:` は通さない /
`plan.sh ci-gate` と `merge-ready` 通知の両方の経路。gh は stub (本物に飛ばさない)。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from fixture_tree import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import lib_ci_status as ci  # noqa: E402
import lib_config  # noqa: E402
from lib_task_cards import Unreadable  # noqa: E402
from test_ci_gate import HEAD, NOW, chk, rec  # noqa: E402

MINERVA = "repo:tyz-works/minerva"
CFG = {"ci": {"required_checks": {"tyz-works/Minerva": ["test-ubuntu", "test-windows"]}}}


def verdict(record, scope, config, **kw):
    return ci.gate_verdict(record, pr_head=HEAD, now=NOW, scope=scope, config=config, **kw)


def mrec(checks, **kw):
    return rec(checks, scope=MINERVA, **kw)


# --- crewvia 本体 (旧仕様のまま) ---

@pytest.mark.parametrize("name", ["none", "empty", "cfg", "unreadable", "garbage"])
def test_crewvia_scope_is_ci_ok_whatever_the_config_says(name, monkeypatch):
    config = {"none": None, "empty": {}, "cfg": CFG, "unreadable": Unreadable("x", "broken"),
              "garbage": {"ci": "garbage"}}[name]

    def boom(*a, **k):
        raise AssertionError("crewvia 本体の scope は config を読まない")
    monkeypatch.setattr(lib_config, "load_effective", boom)
    assert verdict(rec([chk("pytest"), chk("ci-ok")]), "", config) == (True, "ok")
    assert verdict(rec([chk("pytest")]), "", config) == (False, "no_required_check")


def test_crewvia_ci_ok_is_not_configurable_through_required_checks():
    cfg = {"ci": {"required_checks": {"tyz-works/crewvia": ["pytest"]}}}
    assert verdict(rec([chk("pytest")]), "", cfg) == (False, "no_required_check")


def test_scope_is_required():
    with pytest.raises(TypeError):
        ci.gate_verdict(rec(), pr_head=HEAD, now=NOW)       # 省略で crewvia 本体に倒さない


# --- 他の repo ---

def test_configured_repo_passes_when_every_required_check_passed():
    assert verdict(mrec([chk("test-ubuntu"), chk("test-windows")]), MINERVA, CFG) == (True, "ok")
    # 大文字小文字は畳んで照合 (config 側が Minerva・scope 側が小文字)
    assert verdict(mrec([chk("test-ubuntu"), chk("test-windows"), chk("lint")]), "repo:TYZ-works/MINERVA", CFG) \
        == (True, "ok")


@pytest.mark.parametrize("checks", [
    [chk("test-ubuntu")],                                                        # 1 つ欠ける
    [chk("test-windows")],
    [chk("test-ubuntu"), chk("test-windows", "neutral")],                        # skipped は緑と数えない
    [chk("test-ubuntu"), chk("test-windows"), chk("test-windows", "failed")],    # 同名が 1 つでも緑でない
    [chk("ci-ok")],                                                              # ci-ok は他 repo の根拠にならない
    [chk("Test-Ubuntu"), chk("test-windows")],                                   # 名前は大文字小文字を区別
])
def test_configured_repo_without_every_required_check_is_no_required_check(checks):
    state = "passed"
    assert verdict(mrec(checks, state=state), MINERVA, CFG) == (False, "no_required_check")


def test_failed_check_is_failed_before_the_required_set_is_looked_at():
    assert verdict(mrec([chk("test-ubuntu"), chk("test-windows", "failed")]), MINERVA, CFG) == (False, "failed")


@pytest.mark.parametrize("config", [
    {}, {"ci": {}}, {"ci": {"required_checks": None}}, {"ci": {"required_checks": {}}},
    {"ci": {"required_checks": {"someone/else": ["x"]}}},
])
def test_unconfigured_repo_is_no_required_check_config(config):
    assert verdict(mrec([chk("test-ubuntu"), chk("test-windows")]), MINERVA, config) == (False, "no_required_check_config")


@pytest.mark.parametrize("bad", [
    "text", ["a/b"], 3,                                                             # mapping でない
    {"nope": ["x"]}, {"a/b/c": ["x"]}, {"": ["x"]}, {3: ["x"]},                     # repo 名の形
    {"tyz-works/minerva": ["x"], "Tyz-Works/Minerva": ["y"]},                        # 畳むと重複
    {"tyz-works/minerva": []}, {"tyz-works/minerva": "x"}, {"tyz-works/minerva": None},
    {"tyz-works/minerva": [""]}, {"tyz-works/minerva": [" x"]}, {"tyz-works/minerva": [3]},
    {"tyz-works/minerva": ["x\n"]},
    {"tyz-works/minerva": ["test-ubuntu"], "broken": ["x"]},                          # 別の repo の壊れた値でも通さない
])
def test_broken_required_checks_never_pass(bad):
    cfg = {"ci": {"required_checks": bad}}
    ok, why = verdict(mrec([chk("test-ubuntu"), chk("test-windows"), chk("x")]), MINERVA, cfg)
    assert (ok, why) == (False, "unknown:config_invalid")


def test_unreadable_config_never_passes():
    assert verdict(mrec([chk("test-ubuntu"), chk("test-windows")]), MINERVA, Unreadable("p", "local_malformed")) \
        == (False, "unknown:config_unreadable")


def test_bad_scope_never_passes():
    for scope in (None, "nope", "repo:nope", "repo:a/b/c", 3):
        assert verdict(mrec([chk("test-ubuntu")]), scope, CFG)[0] is False


# --- dir: scope は owner/name に解く。解けなければ通さない ---

def _git_repo(path, origin):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)


@pytest.mark.parametrize("url", [
    "https://github.com/tyz-works/Minerva.git", "https://github.com/tyz-works/Minerva",
    "git@github.com:tyz-works/Minerva.git", "ssh://git@github.com/tyz-works/Minerva.git",
])
def test_dir_scope_resolves_through_origin(tmp_path, url):
    _git_repo(tmp_path / "w", url)
    assert ci.resolve_dir_repo(str(tmp_path / "w")) == "tyz-works/minerva"
    assert verdict(rec([chk("test-ubuntu"), chk("test-windows")], scope=f"dir:{tmp_path / 'w'}"),
                   f"dir:{tmp_path / 'w'}", CFG) == (True, "ok")


@pytest.mark.parametrize("origin", [None, "https://example.com/tyz-works/Minerva.git", "/local/path/repo", "garbage"])
def test_dir_scope_that_cannot_be_resolved_is_denied(tmp_path, origin):
    _git_repo(tmp_path / "w", origin)
    assert ci.resolve_dir_repo(str(tmp_path / "w")) is None
    assert verdict(rec([chk("test-ubuntu"), chk("test-windows")]), f"dir:{tmp_path / 'w'}", CFG) \
        == (False, "unknown:repo_unresolved")


def test_dir_scope_missing_or_relative_dir_is_denied(tmp_path):
    for d in (str(tmp_path / "gone"), "relative/dir", ""):
        assert ci.resolve_dir_repo(d) is None
        assert verdict(rec([chk("test-ubuntu")]), f"dir:{d}", CFG) == (False, "unknown:repo_unresolved")


def test_dir_scope_resolving_to_an_unconfigured_repo_is_no_required_check_config(tmp_path):
    _git_repo(tmp_path / "w", "https://github.com/someone/else.git")
    assert verdict(rec([chk("test-ubuntu")]), f"dir:{tmp_path / 'w'}", CFG) == (False, "no_required_check_config")


# --- config の入口 (lib_config 経由・local で書ける) ---

def test_ci_key_is_allowed_in_local_and_the_common_default_is_empty():
    assert "ci" in lib_config.ALLOWED_KEYS
    common = lib_config.load_effective(use_local=False)
    assert common["ci"] == {"required_checks": {}}


def test_local_required_checks_are_merged_over_the_common_default(tmp_path):
    local = tmp_path / "crewvia.local.yaml"
    local.write_text('ci:\n  required_checks:\n    "tyz-works/Minerva": [test-ubuntu, test-windows]\n')
    cfg = lib_config.load_effective(local_path=local)
    assert verdict(mrec([chk("test-ubuntu"), chk("test-windows")]), MINERVA, cfg) == (True, "ok")
    local.write_text("ci: [broken\n")
    cfg = lib_config.load_effective(local_path=local)
    assert verdict(mrec([chk("test-ubuntu"), chk("test-windows")]), MINERVA, cfg) == (False, "unknown:config_unreadable")


def test_required_checks_example_is_documented():
    assert "required_checks" in (REPO_ROOT / "config" / "crewvia.local.yaml.example").read_text()
    assert "no_required_check_config" in (REPO_ROOT / "knowledge" / "ci-status.md").read_text()


# --- 通知 (merge-ready / ci-green) の経路 ---

def _task(**kw):
    t = {"mission": "m", "id": "t001", "status": "done", "pr_number": 5, "pr_repo": "tyz-works/Minerva", "skills": ["code"]}
    t.update(kw)
    return t


def _write(tmp_path, checks, **kw):
    r = {"pr": 5, "head": HEAD, "state": ci.overall_state(checks), "checks": checks, "observed_at": NOW,
         "summary": "s", "pr_state": "OPEN", "scope": MINERVA}
    r.update(kw)
    assert ci.write_record(tmp_path, "m", "t001", r)


def _events(tmp_path, monkeypatch, config):
    monkeypatch.setattr(lib_config, "load_effective", lambda *a, **k: config)
    return ci.pr_events([_task()], registry_dir=tmp_path, now=NOW + 5)[0]


def test_notifications_follow_the_per_repo_required_checks(tmp_path, monkeypatch):
    _write(tmp_path, [chk("test-ubuntu"), chk("test-windows")])
    assert [e["kind"] for e in _events(tmp_path, monkeypatch, CFG)] == ["ci-green"]
    assert _events(tmp_path, monkeypatch, {}) == []                                   # 未設定: 緑でも知らせない
    assert _events(tmp_path, monkeypatch, {"ci": {"required_checks": {"tyz-works/minerva": []}}}) == []
    assert _events(tmp_path, monkeypatch, Unreadable("p", "local_malformed")) == []
    _write(tmp_path, [chk("test-ubuntu")], state="passed")                             # 1 つ欠ける
    assert _events(tmp_path, monkeypatch, CFG) == []


# --- plan.sh ci-gate の経路 (gh は PATH の stub) ---

def test_cli_gate_uses_the_configured_checks(tmp_path, monkeypatch):
    from test_ci_gate import MISSION, Repo, done_with_pr, gate, put_gh, shutil, write_rec
    r = Repo(tmp_path)
    shutil.copytree(REPO_ROOT / "config", r.root / "config")
    shutil.copytree(REPO_ROOT / "hooks", r.root / "hooks")
    done_with_pr(r)
    assert r.plan("update", "t001", "--pr-repo", "tyz-works/Minerva", "--mission", MISSION).returncode == 0
    put_gh(r)
    write_rec(r, rec([chk("test-ubuntu"), chk("test-windows")], observed=time.time() - 5, scope=MINERVA))
    # config に無い repo は通さない (理由は no_required_check_config)
    p = gate(r, "313", "--repo", "tyz-works/minerva")
    assert (p.returncode, p.stdout) == (1, "ci-gate: DENY PR #313 no_required_check_config\n"), p.stderr
    # 主 checkout の local に足せば通る (fixture は git 管理外なので common に直接書いて local の代わりにする)
    cfg = r.root / "config" / "crewvia.yaml"
    cfg.write_text(cfg.read_text().replace(
        "required_checks: {}", 'required_checks: {"tyz-works/Minerva": [test-ubuntu, test-windows]}'))
    p = gate(r, "313", "--repo", "tyz-works/minerva")
    assert p.returncode == 0 and f"PASS PR #313 head={HEAD}" in p.stdout, (p.stdout, p.stderr)
    write_rec(r, rec([chk("test-ubuntu")], state="passed", observed=time.time() - 5, scope=MINERVA))
    p = gate(r, "313", "--repo", "tyz-works/minerva")
    assert (p.returncode, p.stdout) == (1, "ci-gate: DENY PR #313 no_required_check\n")
