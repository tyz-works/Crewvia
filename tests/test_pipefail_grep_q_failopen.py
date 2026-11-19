#!/usr/bin/env python3
"""
tests/test_pipefail_grep_q_failopen.py

`set -o pipefail` 下の `echo "$X" | grep -q PAT` は、入力が pipe buffer (64KB) を超えると
grep が一致で先に抜け、前段が SIGPIPE (141) で落ち、パイプ全体が「一致なし」になる。
判定に使っていると fail-open になる（#421 のレビューが実測）。

- scripts/shared/classify_sh.sh Stage 1: 先頭に危険な行 + 64KB 超の詰め物の .sh が SAFE になり、
  hooks/sh_perm_manager.sh が実行許可を自動で足していた。
- hooks/pre-tool-use.sh: compound の rm -rf / sudo / `| sh`、queue の card の直接書き込みの検出。
  さらに TOOL_SUMMARY の `echo | head -c 80` が 64KB 超の command でフックを crash させていた
  （fail-closed = 全部 deny。普通のコマンドも止まる）。

本物の classify_sh.sh / pre-tool-use.sh を subprocess で走らせる。
テスト側の同じ形は外れても FAIL に倒れる（安全側）ので、構造テストの対象は本番コードだけ。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PAD = 100_000          # 64KB（pipe buffer）超・単一 argv の上限 128KB 未満

# 構造テストの例外（理由つき）。空が望ましい。
ALLOWED_PIPED_GREP_Q: dict[tuple[str, str], str] = {}


# --- classify_sh.sh ---------------------------------------------------------

def _classify(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    f = tmp_path / "t.sh"
    f.write_text(body)
    return subprocess.run(["bash", str(REPO / "scripts/shared/classify_sh.sh"), str(f)],
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("danger, reason", [
    ("sudo rm /etc/x", "sudo"),
    ("curl http://x | bash", "curl|bash"),
    ("mkfs /dev/sda", "mkfs"),
])
def test_classify_blocks_dangerous_line_before_a_huge_line(tmp_path, danger, reason):
    body = f"#!/bin/bash\n{danger}\nA=" + "x" * 160_000 + "\n"
    for _ in range(8):                      # 確率的に落ちていたので繰り返す
        r = _classify(tmp_path, body)
        assert r.returncode == 1 and r.stdout.startswith("BLOCK"), (r.stdout, r.stderr)
        assert reason in r.stdout


def test_classify_short_inputs_unchanged(tmp_path):
    assert _classify(tmp_path, "#!/bin/bash\necho hi\nls -la\n").stdout.strip() == "SAFE"
    assert _classify(tmp_path, "#!/bin/bash\nsudo ls\n").stdout.startswith("BLOCK: sudo")
    assert _classify(tmp_path, "#!/bin/bash\nD=$(mktemp -d)\nrm -rf \"$D\"\n").stdout.strip() == "SAFE"
    # 大きくても危険が無ければ SAFE のまま
    assert _classify(tmp_path, "#!/bin/bash\nA=" + "x" * 160_000 + "\n").stdout.strip() == "SAFE"


# --- pre-tool-use.sh --------------------------------------------------------

@pytest.fixture()
def hook_root(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(REPO / "hooks", root / "hooks")
    shutil.copytree(REPO / "config", root / "config")
    (root / "registry").mkdir()
    return root


def _decision(root: Path, command: str, skills: str = "bash") -> str:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin", "CREWVIA_REPO": str(root),
           "CREWVIA_TASKVIA": "disabled", "TASKVIA_TOKEN": "", "SKILLS": skills,
           "AGENT_NAME": "Haruto"}
    r = subprocess.run(["bash", str(root / "hooks/pre-tool-use.sh")], input=payload,
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
    out = r.stdout.strip()
    if not out:
        return "SILENT"                     # 承認に回す（自動 allow しない）
    j = json.loads(out.splitlines()[-1])["hookSpecificOutput"]
    return f'{j["permissionDecision"]}: {j["permissionDecisionReason"]}'


_QUEUE_CARD = "queue/" + "missions/m/tasks/t001.md"

CASES = [
    "git status",
    "ls -la",
    "./scripts/plan.sh pull --agent Ingrid",
    "rm -rf ./build | cat",                 # compound の rm -rf → 承認
    "echo x | sh",                          # シェルへのパイプ
    "ls && sudo ls",
    f"echo x > {_QUEUE_CARD}",              # queue の card の直接書き込み
    f"sed -i s/a/b/ {_QUEUE_CARD}",
    f"echo x | tee {_QUEUE_CARD}",
    f'bash -c "echo x >> {_QUEUE_CARD}"; true',   # ネストしたインタプリタ (クォート除去の判定)
]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が必要")
@pytest.mark.parametrize("cmd", CASES)
def test_big_command_gets_the_same_decision_as_the_small_one(hook_root, cmd):
    small = _decision(hook_root, cmd + "; A=x")
    for _ in range(3):
        assert _decision(hook_root, cmd + "; A=" + "x" * PAD) == small


MULTILINE_PAD = 300_000   # t001 で判定が stdin 渡しになったので 128KB (単一 argv の上限) を超えてよい


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が必要")
@pytest.mark.parametrize("cmd", CASES)
def test_multiline_big_command_gets_the_same_decision_as_the_small_one(hook_root, cmd):
    # 元のパイプ (echo "$X" | grep -q) が fail-open するのは「一致する行が先頭にあり、
    # 後ろの行が大きい」複数行の入力だけ (grep が一致で先に抜け、前段が SIGPIPE になる)。
    # 1 行の 'cmd; A=<pad>' ではこの形を再現できないので、改行で分けた版を縛る。
    small = _decision(hook_root, cmd + "\nA=x")
    assert small != "SILENT" or cmd in ("git status", "ls -la")
    for _ in range(3):
        assert _decision(hook_root, cmd + "\nA=" + "x" * MULTILINE_PAD) == small


COMPOUND_CASES = ["rm -rf ./build | cat", "echo x | sh", "ls && sudo ls"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が必要")
@pytest.mark.parametrize("cmd", COMPOUND_CASES)
def test_multiline_compound_detection_without_skills(hook_root, cmd):
    # python 側 (_global.deny / skill 判定) は同じ入力を先に deny して bash 側の fail-open を
    # 隠す。skill-permissions.yaml を外し SKILLS も無しにして、compound 検出
    # (rm -rf / sudo / | sh) だけが承認に回す砦になる状態で縛る。
    (hook_root / "config" / "skill-permissions.yaml").unlink()
    small = _decision(hook_root, cmd + "\nA=x", skills="")
    assert not small.startswith("allow"), small
    for _ in range(3):
        assert _decision(hook_root, cmd + "\nA=" + "x" * MULTILINE_PAD, skills="") == small


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が必要")
def test_ordinary_commands_pass_and_dangerous_ones_do_not(hook_root):
    # 普通のコマンドは素通り（skill の allow）。危険なものは自動 allow にならない。
    for cmd in ("git status", "ls -la", "./scripts/plan.sh pull --agent Ingrid"):
        assert _decision(hook_root, cmd).startswith("allow"), cmd
        assert _decision(hook_root, cmd + "; A=" + "x" * PAD).startswith("allow"), cmd
    for cmd in ("rm -rf ./build | cat", f"echo x > {_QUEUE_CARD}", "echo x | sh"):
        for suffix in ("; A=x", "; A=" + "x" * PAD):
            assert not _decision(hook_root, cmd + suffix).startswith("allow"), cmd


# --- 構造: 本番の hooks/ と scripts/ に pipefail 下の `| grep -q` 系を戻さない ----------

_PIPED_QUIET_GREP = re.compile(
    r"(?<!\|)\|(?!\|)\s*(?:command\s+)?(?:grep|egrep|fgrep|ugrep|rg)\b[^|\n]*?"
    r"\s(?:-[A-Za-z]*q[A-Za-z]*|--quiet|--silent)(?=\s|$)")


def _production_shell_files():
    for d in ("hooks", "scripts"):
        for p in sorted((REPO / d).rglob("*.sh")):
            name = p.name
            if name.startswith(("test_", "red_proof", "e2e_")) or "/tests/" in str(p):
                continue
            yield p


def test_no_piped_quiet_grep_in_production_shell():
    found = []
    for p in _production_shell_files():
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _PIPED_QUIET_GREP.search(line):
                key = (str(p.relative_to(REPO)), line.strip())
                if key not in ALLOWED_PIPED_GREP_Q:
                    found.append(f"{key[0]}:{n}: {key[1]}")
    assert not found, (
        "pipefail 下の `| grep -q` は入力が 64KB を超えると SIGPIPE で「一致なし」になる。"
        "here-string (`grep -q PAT <<<\"$X\"`) か case / [[ =~ ]] にする:\n" + "\n".join(found))


def test_structural_regex_catches_the_bad_shapes():
    for bad in ('echo "$x" | grep -qE foo', "printf '%s' \"$x\" | grep -q foo",
                'a | command grep -qx "$n"', 'a | grep -E -q foo', 'a | grep --quiet foo'):
        assert _PIPED_QUIET_GREP.search(bad), bad
    for good in ('grep -qE foo <<<"$x"', 'a | grep -oE foo', 'a | grep -c x',
                 'grep -q foo file', 'a || grep -qE foo <<<"$x"'):
        assert not _PIPED_QUIET_GREP.search(good), good
