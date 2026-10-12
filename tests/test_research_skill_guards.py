"""research スキルの構造的ガード (hooks/pre-tool-use.sh の "Research scope guard")。

本物の hook を subprocess で走らせる (判定を Python で再実装したテストは hook を直したことを
証明しない)。設計: knowledge/research-skill-guards.md
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "pre-tool-use.sh"
GUARD = REPO_ROOT / "hooks" / "lib_research_guard.py"
SLUG = "20261012-demo"
OTHER_SLUG = "20261012-other"


@pytest.fixture()
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    # hooks/ は丸ごと写す (名前で指して写すと lib を足すたびに CI でだけ赤くなる)
    shutil.copytree(REPO_ROOT / "hooks", repo / "hooks")
    (repo / "config").mkdir()
    shutil.copy(REPO_ROOT / "config" / "skill-permissions.yaml", repo / "config" / "skill-permissions.yaml")
    for slug in (SLUG, OTHER_SLUG):
        (repo / "queue" / "missions" / slug / "tasks").mkdir(parents=True)
        (repo / "queue" / "missions" / slug / "tasks" / "t001.md").write_text("---\ntitle: demo\nskills: [research]\n---\n")
    (repo / "queue" / "missions" / SLUG / "notes").mkdir()
    (repo / "queue" / "assignments").mkdir()
    (repo / "queue" / "assignments" / "Jiwon").write_text(f"{SLUG}:t001\n")
    return repo


def _run(repo, payload, skills="research", token="", extra=None):
    env = {
        "HOME": str(repo),
        "PATH": os.environ["PATH"],
        "CREWVIA_REPO": str(repo),
        "AGENT_NAME": "Jiwon",
        "SKILLS": skills,
        "TASKVIA_TOKEN": token,
        "TASKVIA_URL": "http://127.0.0.1:9",
    }
    env.update(extra or {})
    p = subprocess.run(
        ["bash", *(["-x"] if os.environ.get("RG_TRACE") else []), str(repo / "hooks" / "pre-tool-use.sh")],
        input=json.dumps(payload), capture_output=True, text=True, env=env, cwd=str(repo), timeout=60,
    )
    assert p.returncode == 0, p.stderr[-3000:]
    if not p.stdout.strip():
        return "native"  # hook が判定せず native に委ねた
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]


def _write(repo, path, tool="Write", **kw):
    return _run(repo, {"tool_name": tool, "tool_input": {"file_path": str(path)}}, **kw)


def _bash(repo, command, **kw):
    return _run(repo, {"tool_name": "Bash", "tool_input": {"command": command}}, **kw)


# ---------------------------------------------------------------- (1) write scope


def test_own_notes_are_writable_and_overwritable(fake_repo):
    note = fake_repo / "queue/missions" / SLUG / "notes" / "t001-findings.md"
    assert _write(fake_repo, note) == "allow"
    note.write_text("old")
    assert _write(fake_repo, note, tool="Edit") == "allow"
    assert _write(fake_repo, note, tool="MultiEdit") == "allow"
    assert _write(fake_repo, fake_repo / "queue/missions" / SLUG / "notes" / "t001.md") == "allow"


@pytest.mark.parametrize("rel", [
    f"queue/missions/{SLUG}/notes/t002-x.md",          # 他 task の notes
    f"queue/missions/{SLUG}/notes/t0010-x.md",         # tid の前方一致の偽装 (t001 + 0)
    f"queue/missions/{OTHER_SLUG}/notes/t001-x.md",    # 他 mission の notes
    f"queue/missions/{SLUG}/tasks/t001.md",            # card
    f"queue/missions/{SLUG}/mission.yaml",
    f"queue/missions/{SLUG}/notes/t001-x.txt",         # .md 以外
    f"queue/missions/{SLUG}/notes/sub/t001-x.md",      # 入れ子
    "scripts/plan.sh",                                  # リポジトリのコード
    "hooks/pre-tool-use.sh",
    f"queue/missions/{SLUG}/notes/../tasks/t001.md",   # .. で抜ける
    f"queue/missions/{SLUG}/notes/../../{OTHER_SLUG}/notes/t001-x.md",
])
def test_everything_else_is_denied(fake_repo, rel):
    (fake_repo / "queue/missions" / OTHER_SLUG / "notes").mkdir(exist_ok=True)
    assert _write(fake_repo, fake_repo / rel) == "deny"


def test_symlinks_cannot_escape(fake_repo, tmp_path):
    notes = fake_repo / "queue/missions" / SLUG / "notes"
    outside = tmp_path / "outside"
    outside.mkdir()
    (notes / "t001-link.md").symlink_to(outside / "victim.md")      # 既存の symlink への書き込み
    assert _write(fake_repo, notes / "t001-link.md") == "deny"
    shutil.rmtree(notes)
    notes.symlink_to(outside)                                        # notes/ 自体が symlink
    assert _write(fake_repo, notes / "t001-x.md") == "deny"


def test_no_assignment_means_no_notes(fake_repo):
    (fake_repo / "queue/assignments/Jiwon").unlink()
    assert _write(fake_repo, fake_repo / "queue/missions" / SLUG / "notes" / "t001-x.md") == "deny"


def test_obsidian_is_off_by_default_and_create_only_when_enabled(fake_repo, tmp_path):
    obs = tmp_path / "obsidian-research"
    obs.mkdir()
    target = obs / "20261012_topic.md"
    assert _write(fake_repo, target) == "deny"  # 既定は無効
    cfg = fake_repo / "config/skill-permissions.yaml"
    cfg.write_text(cfg.read_text().replace('obsidian_research_dir: ""', f'obsidian_research_dir: "{obs}"'))
    assert _write(fake_repo, target) == "allow"          # 新規作成
    assert _write(fake_repo, obs / "topic.md") == "deny"  # 日付つきでない名前
    assert _write(fake_repo, obs / "sub" / "20261012_x.md") == "deny"
    target.write_text("exists")
    assert _write(fake_repo, target) == "deny"            # 既存の上書きは不可
    assert _write(fake_repo, obs / ".." / "20261012_y.md") == "deny"


def _set_card_skills(repo, skills):
    card = repo / "queue/missions" / SLUG / "tasks" / "t001.md"
    card.write_text(f"---\ntitle: demo\nskills: [{skills}]\n---\n")


def test_other_skills_write_behaviour_is_unchanged(fake_repo):
    # research を含まない skill はこのガードの対象外 (card の skills も research でないとき)
    _set_card_skills(fake_repo, "code")
    target = fake_repo / "queue/missions" / SLUG / "mission.yaml"
    assert _write(fake_repo, target, skills="code") == "allow"  # code の yaml allow のまま
    assert _write(fake_repo, target, skills="bash") != "deny"


def test_card_skills_count_even_when_env_skills_lack_research(fake_repo):
    # env の SKILLS が空でも card の skills に research があれば対象 (既存の per-skill 判定と同じ読み方)
    assert _bash(fake_repo, "echo x > f", skills="") == "deny"


# ---------------------------------------------------------------- (2) bash allowlist

ALLOWED = [
    "ls -la",
    "sed -n '1,200p' scripts/watchdog.py",
    "grep -n 'foo' scripts/plan.sh | head -40",
    "cd /tmp && pwd && ls",
    "git log --oneline -3 && git diff origin/main --stat",
    "git fetch origin main -q",
    "git -C /tmp/x status -sb",
    "gh pr view 12 --json state",
    "gh run list --branch main --limit 3 2>&1 | head -5",
    "gh api repos/o/r/pulls/1/comments",
    "gh api -X GET repos/o/r",
    "wc -l a b; cat a 2>/dev/null",
    "find . -name '*.py' -not -path './node_modules/*'",
    "awk '{print $1}' file",
    "timeout 120 ugrep -G 'foo' knowledge/",
    "./scripts/plan.sh pull --agent X --skills research",
    "./scripts/plan.sh status",
    "source .crewvia-env && echo $CREWVIA_TASK_ID",
    "echo hi 2>&1 >/dev/null",
    "./scripts/plan.sh done t001 --mission m --result-file - <<'EOF'\nresult > text $(not run) `nor this`\nEOF",
    "python3 -m json.tool < x.json",
]
DENIED = [
    "echo x > f",
    "echo x >> f",
    "cat a > b",
    "cat > f <<'EOF'\nx\nEOF",
    "cat <<EOF\n$(rm -rf x)\nEOF",  # 引用符なし heredoc
    "tee f",
    "echo x | tee f",
    "sed -i 's/a/b/' f",
    "sed -n 'w out' f",
    "sed 's/a/b/w out' f",
    "cp a b",
    "mv a b",
    "rm a",
    "mkdir d",
    "touch f",
    "python3 -c \"open('f','w').write('x')\"",
    "python3 script.py",
    "python3 -c 'print(1)'",
    "bash -c 'echo x > f'",
    "sh script.sh",
    "node -e 1",
    "perl -e 1",
    "xargs rm",
    "find . -delete",
    "find . -exec rm {} \\;",
    "find . -fprint out",
    "awk 'BEGIN{print 1 > \"f\"}'",
    "awk '{system(\"rm x\")}' f",
    "echo $(rm x)",
    "echo `rm x`",
    "ls & rm x",
    "FOO=1 ls",
    "git add .",
    "git commit -m x",
    "git push origin HEAD",
    "git checkout -b x",
    "git -c core.pager=x log",
    "git log --output=out",
    "git diff --ext-diff",
    "gh pr merge 1",
    "gh pr create",
    "gh api -X POST repos/o/r/issues",
    "gh api repos/o/r/issues -f title=x",
    "gh api graphql -f query=x",
    "curl http://x",
    "./scripts/plan.sh add x",
    "./scripts/plan.sh update t001 --status done",
    "ls; echo x > f",
    "ls && cp a b",
    "ls | tee f",
    "cat a 2> err.txt",
    "cat a &> out",
    "cat a >out",
    "dd if=a of=b",
    "jq . a --rawfile x y",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowlist_passes_normal_investigation(fake_repo, cmd):
    out = subprocess.run(["python3", "-I", str(GUARD), "bash", cmd], capture_output=True, text=True)
    assert json.loads(out.stdout)["decision"] == "allow", (cmd, out.stdout)


@pytest.mark.parametrize("cmd", DENIED)
def test_writes_and_unknown_forms_are_denied_by_the_lib(cmd):
    out = subprocess.run(["python3", "-I", str(GUARD), "bash", cmd], capture_output=True, text=True)
    assert json.loads(out.stdout)["decision"] == "deny", (cmd, out.stdout)


@pytest.mark.parametrize("cmd", ["echo x > f", "tee f", "sed -i s/a/b/ f", "cp a b", "rm a", "python3 -c \"open('f','w')\""])
@pytest.mark.parametrize("token", ["", "tok"])
def test_hook_denies_research_bash_writes_with_or_without_taskvia_token(fake_repo, cmd, token):
    assert _bash(fake_repo, cmd, token=token) == "deny"


def test_hook_lets_allowed_bash_continue_to_the_normal_flow(fake_repo):
    # 通った形は deny されない (token 空 = native に委ねる)
    assert _bash(fake_repo, "ls -la") == "native"
    assert _bash(fake_repo, "git push origin HEAD") == "deny"  # 従来どおり (allowlist と yaml の両方)


def test_mixed_skill_set_is_still_guarded(fake_repo):
    assert _bash(fake_repo, "echo x > f", skills="research,docs") == "deny"
    assert _bash(fake_repo, "echo x > f", skills="docs,research") == "deny"


def test_other_skills_bash_is_unchanged(fake_repo):
    # research を含まない skill の Bash は今までどおり (リダイレクトも hook では止めない)
    _set_card_skills(fake_repo, "code")
    assert _bash(fake_repo, "echo x > f", skills="code") == "native"
    assert _bash(fake_repo, "cp a b", skills="bash") == "native"


def test_crashing_guard_fails_closed(fake_repo):
    (fake_repo / "hooks" / "lib_research_guard.py").write_text("raise SystemExit(7)\n")
    assert _bash(fake_repo, "ls") == "deny"
    assert _write(fake_repo, fake_repo / "queue/missions" / SLUG / "notes" / "t001-x.md") == "deny"
