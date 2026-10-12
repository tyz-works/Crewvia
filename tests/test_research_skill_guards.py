"""research スキルの構造的ガード (hooks/pre-tool-use.sh の "Research scope guard")。

本物の hook を subprocess で走らせる (判定を Python で再実装したテストは hook を直したことを
証明しない)。設計: knowledge/research-skill-guards.md
"""

import importlib.util
import json
import os
import shlex
import shutil
import string
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


def _run(repo, payload, skills="research", token="", extra=None, cwd=None):
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
        input=json.dumps(payload), capture_output=True, text=True, env=env, cwd=str(cwd or repo), timeout=60,
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
#
# t005: 「解釈が要らない形だけ通す」allowlist (knowledge/research-skill-guards.md §2)。


def _load_guard():
    spec = importlib.util.spec_from_file_location("lib_research_guard_under_test", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LIB = _load_guard()


@pytest.fixture(autouse=True)
def _pin_trusted_root(monkeypatch):
    # plan.sh の信頼先 (CREWVIA_REPO_ROOT) と cwd を固定する (t007)。開発機のセッション env の
    # CREWVIA_REPO_ROOT が主 checkout を指していても、lib を直に呼ぶテストの答えが変わらないように
    monkeypatch.setenv("CREWVIA_REPO_ROOT", str(REPO_ROOT))
    monkeypatch.chdir(REPO_ROOT)


def _lib(cmd, **ctx):
    out = subprocess.run(["python3", "-I", str(GUARD), "bash", cmd], capture_output=True, text=True,
                         cwd=str(REPO_ROOT), env=dict(os.environ))
    return json.loads(out.stdout)["decision"]


ALLOWED = [
    "ls -la",
    "sed -n '1,200p' scripts/watchdog.py",
    "sed -n '688,700p;735,745p' scripts/watchdog.py",
    "grep -n 'foo' scripts/plan.sh | head -40",
    "grep -rn 'x\\|y' --include=*.md . | grep -v '^./queue'",
    "cd /tmp && pwd && ls",
    "git log --oneline -3 && git diff --no-ext-diff --no-textconv origin/main --stat",
    "git fetch --no-prune --no-prune-tags origin main",
    "git fetch --no-prune --no-prune-tags origin",
    "git fetch --no-prune --no-prune-tags",
    "git status -s",
    "git log --max-count=5 --format=%H -p --no-ext-diff --no-textconv -- hooks/x.py",
    "git log -10 --since=2.weeks",
    "git rev-list --left-right --count origin/main...HEAD",
    "git branch -a",
    "git branch --list 'task/*'",
    "git tag -l 'v1*'",
    "git config --get remote.origin.url",
    "git remote -v",
    "git worktree list",
    "gh pr view 12 --json state",
    "gh pr view 12 --json state --jq .state",
    "gh run list --branch main --limit 3 2>&1 | head -5",
    "gh api repos/o/r/pulls/1/comments",
    "gh api repos/o/r/pulls --paginate --jq '.[].number'",
    "gh api -q .name repos/o/r",
    "wc -l a b; cat a 2>/dev/null",
    "find . -name '*.py' -not -path './node_modules/*'",
    "timeout 120 ugrep -G 'foo' knowledge/",
    "./scripts/plan.sh pull --agent X --skills research",
    "./scripts/plan.sh status",
    "source ./.crewvia-env && ls",
    ". ./.crewvia-env",
    "echo hi 2>&1 | head -1",
    "cat a 2>/dev/null; ls x 2>/dev/null && ls",
    "echo \"--- merge-base vs origin/main ---\" && git log --oneline origin/main -1",
    "cat -n f | sed 's/^/ /'",
    "ls queue/missions/*/notes",
    "jq -r .name x.json",
    "head -150 f && tail -n 5 f",
    "sort -u f | uniq -c",
    "printenv AGENT_NAME",
    "./scripts/plan.sh needs-director --help 2>&1 | head -20",
]
DENIED = [
    # 書き込み
    "echo x > f",
    "echo x >> f",
    "cat a > b",
    "cat a 2> err.txt",
    "cat a &> out",
    "cat a >out",
    "cat a >| out",
    "cat a <> f",
    "tee f",
    "echo x | tee f",
    "sed -i 's/a/b/' f",
    "sed -ni 's/a/b/' f",
    "sed -n 'w out' f",
    "sed 's/a/b/w out' f",
    "sed 's/a/b/e' f",
    "cp a b",
    "mv a b",
    "rm a",
    "mkdir d",
    "touch f",
    "dd if=a of=b",
    "uniq in out",
    "uniq in o*",
    "sort -o out in",
    "sort --output=out in",
    "sort --compress-program=sh in",
    "tree -o out",
    # heredoc は全面拒否 (t005 項目 1)
    "cat > f <<'EOF'\nx\nEOF",
    "cat <<'EOF'\nx\nEOF",
    "cat <<EOF\n$(rm -rf x)\nEOF",
    "cat <<< 'x'",
    "./scripts/plan.sh done t001 --mission m --result-file - <<'EOF'\nresult\nEOF",
    # 展開・状態変更 (t005 項目 2)
    "echo $(rm x)",
    "echo `rm x`",
    "echo $HOME",
    "echo ${HOME}",
    "ls # comment",
    "grep -n '#' f",
    "ls & rm x",
    "FOO=1 ls",
    "X=1",
    "export X=1",
    "unset PATH",
    "alias ls=rm",
    "declare -x X=1",
    "set -e",
    "source evil.sh",
    ". evil.sh",
    "source ./x/.crewvia-env",
    "ls && source .crewvia-env",
    "ls && source ./.crewvia-env",
    "source .crewvia-env",
    "printf '%s\\n' a b",
    "printf x",
    "git diff",
    "git show HEAD",
    "git log -p",
    "git blame f",
    "env ls",
    "command ls",
    "builtin cd",
    "exec ls",
    "eval ls",
    "xargs rm",
    "nohup ls",
    "time ls",
    "timeout 5 rm x",
    "timeout 5 timeout 5 ls",
    "timeout -s KILL 5 ls",
    "(rm x)",
    "{ rm x; }",
    "! ls",
    "ls {a,-o}",
    "ls <(rm x)",
    "/tmp/ls",
    "bin/cat f",
    "sort * ",
    "grep -e * f",
    "cat -n*",
    # 言語を持つツール (t005 項目 3)
    "awk '{print $1}' file",
    "awk '{print 1}' file",
    "python3 -m json.tool < x.json",
    "python3 -c 'print(1)'",
    "python3 script.py",
    "node -e 1",
    "perl -e 1",
    "xxd f",
    "bash -c 'ls'",
    "sh script.sh",
    "yq . f",
    # オプションの allowlist (t005 項目 4)
    "find . -delete",
    "find . -exec rm {} \\;",
    "find . -fprint out",
    "find . -newer x -okdir rm",
    # find の述語の値・始点の引用符の無い glob (t015。展開で -delete 等の述語になりうる)
    "find . -name -*",
    "find . -name *.md",
    "find . -path ./a*",
    "find -* .",
    "find * -name x",
    "jq . a --rawfile x y",
    "jq -f prog.jq a",
    "grep --pre=sh x f",
    "rg --pre sh x",
    "rg --hostname-bin=sh x",
    "ugrep --save-config x",
    "ugrep --filter=x f",
    "less f",
    "git add .",
    "git commit -m x",
    "git push origin HEAD",
    "git checkout -b x",
    "git -c core.pager=x log",
    "git --exec-path=/tmp log",
    "git log --output=out",
    "git diff --ext-diff",
    "git diff --textconv",
    "git grep -O x",
    "git grep -L -O vim x",
    "git grep -n x f",         # git grep は外した (t007。grep -r / rg で代わりになる)
    # 値を次の語から取るオプションの「値」に別のオプションを隠す形。- で始まる値を拒否する規則だけが止める
    "sort -k -o x",
    "git cat-file --filters HEAD:x",
    "git fetch --upload-pack=sh origin",
    "git fetch origin main:main",
    "git fetch origin +x:y",
    "git ls-remote 'ext::sh -c x'",
    "git branch newbranch",
    "git tag v9",
    "git config user.name x",
    "git remote add x y",
    "git stash",
    "git help log",
    # gh (t005 項目 4)
    "gh pr merge 1",
    "gh pr create",
    "gh pr view 1 --web",
    "gh api -X GET repos/o/r",
    "gh api -X POST repos/o/r/issues",
    "gh api --method=DELETE repos/o/r",
    "gh api repos/o/r/issues -f title=x",
    "gh api repos/o/r -Ffoo=bar",
    "gh api repos/o/r --raw-field=a=b",
    "gh api repos/o/r --input f",
    "gh api repos/o/r -H 'X: y'",
    "gh api graphql",
    "gh api a b",
    "curl http://x",
    # plan.sh
    "./scripts/plan.sh add x",
    "./scripts/plan.sh update t001 --status done",
    "./scripts/plan.sh done t001 'result text'",
    "./scripts/plan.sh done t001 --result-file -",
    "./scripts/plan.sh done t001 --result-file /tmp/r.md",
    "./scripts/plan.sh needs-director t001 'reason'",
    "../scripts/plan.sh status",
    "plan status --mission m",          # 素の plan (alias / PATH で解決される。t007)
    "plan.sh status",
    "/tmp/plan.sh status",
    "./scripts/../scripts/plan.sh status",
    # 区切りの組み合わせ
    "ls; echo x > f",
    "ls && cp a b",
    "ls | tee f",
    "ls || rm x",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowlist_passes_normal_investigation(cmd):
    assert _lib(cmd) == "allow"


@pytest.mark.parametrize("cmd", DENIED)
def test_writes_and_unknown_forms_are_denied_by_the_lib(cmd):
    assert _lib(cmd) == "deny"


# Codex (t003) の指摘の再現。どれも PR #299 の初版 (b5d1b81) では allow だった
CODEX_REPRODUCTIONS = [
    "command rm victim",                                   # 指摘 1
    "cat /dev/null # <<'EOF'\ntouch victim\nEOF",          # 指摘 2
    "cat /dev/null <<'EOF'\nEOF\ntouch victim",            # 指摘 2 (heredoc そのもの)
    "awk -v x=1 'BEGIN {system(\"touch victim\")}'",       # 指摘 3
    "python3 -m json.tool input.json victim",              # 指摘 4
    "xxd -r input victim",                                 # 指摘 4
    "sort -ovictim input",                                 # 指摘 4
    "gh api -XDELETE repos/o/r",                           # 指摘 5
    "gh api -fa=b repos/o/r",                              # 指摘 5
    "gh api -Fa=b repos/o/r",                              # 指摘 5
    "export PATH=/tmp; ls",                                # 指摘 6
    "export LD_PRELOAD=x; ls",                             # 指摘 6
    # Codex 2 巡目 (t003 → t007)。どれも c35cb83 では allow だった
    "git grep -LOecho zzz_unmatched -- hooks/lib_research_guard.py",  # 2-1 git のオプション表
    "uniq /dev/null '2'>/dev/null",                        # 2-2 引用符つきの fd 番号
    "/tmp/plan.sh status",                                 # 2-3 plan.sh の実体
    "git fetch --prune --prune-tags origin",               # 2-4 fetch の刈り込み
    # Codex 3 巡目 (t003 → t009)。どれも 4e8a822 では allow だった
    "source .crewvia-env",                                 # 3-1 PATH を探す (bash の sourcepath)
    "printf 'abc%n' PATH",                                 # 3-2 %n が変数 (PATH) を書く
    "git diff --no-index /dev/null /etc/hostname",         # 3-3 GIT_EXTERNAL_DIFF を起動する
    "git diff",                                            # 3-3 diff.<driver>.command / textconv
    "git show HEAD",                                       # 3-3
    "git log -p",                                          # 3-3
]


@pytest.mark.parametrize("cmd", CODEX_REPRODUCTIONS)
def test_codex_reproductions_are_denied_by_the_lib(cmd):
    assert _lib(cmd) == "deny"


@pytest.mark.parametrize("cmd", CODEX_REPRODUCTIONS)
@pytest.mark.parametrize("token", ["", "tok"])
def test_codex_reproductions_are_denied_by_the_hook(fake_repo, cmd, token):
    assert _bash(fake_repo, cmd, token=token) == "deny"


# ---- 列挙: 表 (lib の all_specs) から全組み合わせを作る。表に行を足すとテストも増える

_CANDIDATES = string.ascii_letters + string.digits + "%+.@_=,:"


def _spec_ids():
    return [" ".join(prefix) for prefix, _ in LIB.all_specs()]


@pytest.mark.parametrize("prefix_s", _spec_ids())
def test_every_unlisted_option_is_denied_in_split_and_attached_form(prefix_s):
    spec = dict((" ".join(p), s) for p, s in LIB.all_specs())[prefix_s]
    prefix = prefix_s.split()
    if spec.any_args:
        pytest.skip("any_args (echo): 引数を検査しない行。読み書きも実行もしない builtin だけに使う")
    listed = set(spec.short) | set(spec.short_arg)
    checked = 0
    for c in _CANDIDATES:
        if c in listed or (spec.digits and c.isdigit()):
            continue
        forms = [[f"-{c}victim"]]
        if not (spec.numeric and c.isdigit()):
            forms.append([f"-{c}", "victim"])
        if spec.short:
            forms.append([f"-{spec.short[0]}{c}victim"])   # 許可された文字の後ろに結合
        for extra in forms:
            words = [*prefix, *extra]
            cmd = " ".join(shlex.quote(w) for w in words)
            decision, reason = LIB.check_bash(cmd)
            assert decision == "deny", (cmd, reason)
            checked += 1
    for long in ("zz-not-listed", "output", "exec"):
        if long in spec.all_long():
            continue
        for extra in ([f"--{long}"], [f"--{long}=victim"], [f"--{long}", "victim"]):
            cmd = " ".join(shlex.quote(w) for w in [*prefix, *extra])
            assert LIB.check_bash(cmd)[0] == "deny", cmd
            checked += 1
    for name in spec.long:   # 値を取らない長いオプションに = で値を付けた形
        cmd = " ".join(shlex.quote(w) for w in [*prefix, f"--{name}=victim"])
        assert LIB.check_bash(cmd)[0] == "deny", cmd
        checked += 1
    if spec.strict:          # git (t007): 列挙済みの字でも束ね・結合値は拒否、値は --name=<値> だけ
        for c in spec.short:
            for extra in ([f"-{c}{c}"], [f"-{c}victim"], [f"-{c}Oecho"], [f"-{c}{spec.short[0]}"]):
                cmd = " ".join(shlex.quote(w) for w in [*prefix, *extra])
                assert LIB.check_bash(cmd)[0] == "deny", cmd
                checked += 1
        for name in spec.long_arg:
            cmd = " ".join(shlex.quote(w) for w in [*prefix, f"--{name}", "victim"])
            assert LIB.check_bash(cmd)[0] == "deny", cmd
            checked += 1
    assert checked >= 20


def test_enumeration_covers_the_whole_table():
    ids = _spec_ids()
    assert len(ids) >= 80
    for must in ("sort", "grep", "rg", "ugrep", "jq", "sed", "git log", "git fetch", "gh api", "gh pr view",
                 "./scripts/plan.sh done"):
        assert must in ids


# GNU getopt_long と git の parse-options は長いオプションの「一意な前方一致」を受け付ける。
# allowlist の名前が危ない名前の前方一致になっていると、その名前で危ないオプションが動く。
DANGEROUS_LONG = {
    # git の大域オプション (--exec-path / --git-dir / --work-tree / --config-env / -c) は _check_git が
    # サブコマンドの前で別に拒否する (DENIED)。ここはサブコマンドのオプション
    "git": ["output", "ext-diff", "textconv", "open-files-in-pager", "upload-pack", "exec", "filters",
            "set-upstream", "contents", "exec-path", "receive-pack", "prune", "prune-tags", "force",
            "work-tree", "config-env", "paginate"],
    "sort": ["output", "compress-program", "temporary-directory"],
    "sed": ["in-place", "file", "follow-symlinks"],
    "grep": ["pre"], "egrep": ["pre"], "fgrep": ["pre"],
    "rg": ["pre", "pre-glob", "hostname-bin", "search-zip"],
    "ugrep": ["filter", "pager", "save-config", "config", "view", "query", "decompress"],
    "jq": ["from-file", "rawfile", "slurpfile", "library-path"],
    "tree": ["output", "fromfile"],
    # gh (cobra / pflag) は前方一致を受け付けないので載せない (完全一致の allowlist で足りる)
}


@pytest.mark.parametrize("prefix_s", _spec_ids())
def test_no_listed_long_option_abbreviates_a_dangerous_one(prefix_s):
    spec = dict((" ".join(p), s) for p, s in LIB.all_specs())[prefix_s]
    for name in spec.all_long():
        for bad in DANGEROUS_LONG.get(prefix_s.split()[0], []):
            assert not bad.startswith(name), (prefix_s, name, bad)


def test_glob_words_cannot_become_options(tmp_path):
    # 展開すると -o… になりうる語は通さない。先頭が字そのものなら展開しても先頭は変わらない
    assert LIB.check_bash("ls *.md")[0] == "deny"
    assert LIB.check_bash("ls ?x")[0] == "deny"
    assert LIB.check_bash("ls [-]o")[0] == "deny"
    assert LIB.check_bash("ls a*.md")[0] == "allow"
    assert LIB.check_bash("ls '-'*")[0] == "deny"
    assert LIB.check_bash("grep -r x --include=*.md .")[0] == "allow"
    assert LIB.check_bash("grep -r x -e*")[0] == "deny"


def test_source_crewvia_env_is_the_one_fixed_form():
    # t009: スラッシュ無し (PATH を探す) は不可。先頭の区間で、信頼する場所の .crewvia-env をパスで指す形だけ
    assert LIB.check_bash("source ./.crewvia-env")[0] == "allow"
    assert LIB.check_bash(f"source {REPO_ROOT}/.crewvia-env")[0] == "allow"
    assert LIB.check_bash("source ./.crewvia-env && ./scripts/plan.sh status")[0] == "allow"
    for bad in ("source .crewvia-env", ". .crewvia-env", "source ./.crewvia-env x", "source ../.crewvia-env",
                "cd /x && source ./.crewvia-env", "cd /x && source .crewvia-env", "timeout 5 source ./.crewvia-env",
                "source './.crewvia-env'x", "source ./scripts/../.crewvia-env", "source ./.crewvia-en?",
                "source /tmp/.crewvia-env", "source", "source ./hooks/pre-tool-use.sh"):
        assert LIB.check_bash(bad)[0] == "deny", bad


def test_source_is_bound_to_the_own_worktree_env_file(fake_repo, tmp_path):
    # Codex 3 巡目 指摘 1: どの cwd の .crewvia-env でも source できた。cd を 1 回目の呼び出し、source を
    # 2 回目の呼び出しに分けると、2 回目の hook の cwd は cd 先 (= 別の checkout) になる
    wt = fake_repo / ".claude/worktrees" / SLUG / "t001-demo"
    other_wt = fake_repo / ".claude/worktrees" / SLUG / "t002-demo"
    other_slug_wt = fake_repo / ".claude/worktrees" / OTHER_SLUG / "t001-demo"
    evil = tmp_path / "evil"
    for d in (wt, other_wt, other_slug_wt, evil):
        d.mkdir(parents=True)
        (d / ".crewvia-env").write_text("touch victim\n")
    (fake_repo / ".crewvia-env").write_text("\n")
    linked = fake_repo / ".claude/worktrees" / SLUG / "t001-linked"
    linked.mkdir()
    (linked / ".crewvia-env").symlink_to(evil / ".crewvia-env")          # 自分の worktree の名前の symlink
    (fake_repo / ".claude/worktrees" / SLUG / "t001-dirlink").symlink_to(evil)   # worktree dir ごと symlink
    ok = [
        ("source ./.crewvia-env", wt),
        (". ./.crewvia-env", wt),
        (f"source {wt}/.crewvia-env", evil),                   # 絶対パスなら cwd に依らない
        ("source ./.crewvia-env && ls", wt),
        ("source ./.crewvia-env", fake_repo),                   # repo root の .crewvia-env
    ]
    for cmd, cwd in ok:
        assert _bash(fake_repo, cmd, cwd=cwd) == "native", (cmd, cwd)
    bad = [
        ("cd " + str(evil), fake_repo, "native"),               # 1 回目: cd だけなら通る (読み取り)
        ("source ./.crewvia-env", evil, "deny"),                # 2 回目: cd 先の .crewvia-env は不可
        ("source .crewvia-env", wt, "deny"),                    # PATH を探す形は自分の worktree でも不可
        ("source ./.crewvia-env", other_wt, "deny"),            # 他の task の worktree
        ("source ./.crewvia-env", other_slug_wt, "deny"),       # 他の mission の worktree
        (f"source {evil}/.crewvia-env", wt, "deny"),
        ("source ./.crewvia-env", linked, "deny"),              # symlink の .crewvia-env
        (f"source {linked}/.crewvia-env", wt, "deny"),
        (f"source {fake_repo}/.claude/worktrees/{SLUG}/t001-dirlink/.crewvia-env", wt, "deny"),
        (f"source {wt}/../t002-demo/.crewvia-env", wt, "deny"),
        (f"cd {wt} && source ./.crewvia-env", evil, "deny"),    # 同じコマンドの cd の後ろ (先頭の区間でない)
        (f"cd {wt} && source {wt}/.crewvia-env", evil, "deny"),
    ]
    for cmd, cwd, want in bad:
        assert _bash(fake_repo, cmd, cwd=cwd) == want, (cmd, cwd)
    (fake_repo / "queue/assignments/Jiwon").unlink()            # assignment が無ければ worktree は信頼しない
    assert _bash(fake_repo, "source ./.crewvia-env", cwd=wt) == "deny"


def test_bare_source_searches_path_before_cwd(tmp_path):
    # 前提の確認: bash の source はスラッシュ無しの名前を PATH から先に探す (= cwd の検査では足りない)
    on_path = tmp_path / "bin"
    on_path.mkdir()
    (on_path / ".crewvia-env").write_text("echo FROM_PATH\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".crewvia-env").write_text("echo FROM_CWD\n")
    p = subprocess.run(["bash", "-c", "source .crewvia-env"], cwd=str(cwd), capture_output=True, text=True,
                       env={"PATH": f"{on_path}:/usr/bin:/bin"})
    assert p.stdout.strip() == "FROM_PATH"


def test_printf_is_not_allowed_because_percent_n_writes_a_variable():
    # Codex 3 巡目 指摘 2。前提: bash の printf の %n は -v 無しでも変数を書く
    p = subprocess.run(["bash", "-c", "printf 'abc%n' X; echo; echo \"[$X]\""], capture_output=True, text=True)
    assert p.stdout.splitlines()[-1] == "[3]"
    for cmd in ("printf 'abc%n' PATH", "printf x", "printf '%s\\n' a", "timeout 5 printf x"):
        assert LIB.check_bash(cmd)[0] == "deny", cmd
    assert "printf" not in LIB.SPECS
    assert LIB.check_bash("echo -e 'a\\tb'")[0] == "allow"      # 代わりは echo


@pytest.mark.parametrize("cmd", [
    "cat <<'EOF'\nx\nEOF",
    "cat <<EOF\nx\nEOF",
    "cat <<-'EOF'\n\tx\n\tEOF",
    "cat <<< x",
    "cat /dev/null # <<'EOF'\ntouch victim\nEOF",
    "./scripts/plan.sh done t001 --result-file - <<'EOF'\nx\nEOF",
    "grep -n '<<' f",
])
def test_heredoc_is_rejected_up_front_with_guidance(cmd):
    # heredoc は文字の段階で拒否し、代わりの手順 (Write でメモ → --result-file) を返す。
    # 字句解析も本文の行をコマンドとして検査するので二重に止まるが、案内はこの段でしか出ない
    decision, reason = LIB.check_bash(cmd)
    assert decision == "deny"
    assert "heredoc" in reason and "--result-file" in reason, reason


# ---- plan.sh done / needs-director の結果は --result-file <自分の notes> だけ (t005 項目 1)


@pytest.mark.parametrize("sub", ["done", "needs-director"])
def test_plan_result_file_must_be_own_notes(fake_repo, sub):
    notes = fake_repo / "queue/missions" / SLUG / "notes"
    (notes / "t001-result.md").write_text("result\n")
    rel = f"queue/missions/{SLUG}/notes/t001-result.md"
    ok = [
        f"./scripts/plan.sh {sub} t001 --mission {SLUG} --execution ex-1 --result-file {rel}",
        f"./scripts/plan.sh {sub} t001 --result-file {notes / 't001-result.md'}",
        f"cd {fake_repo} && {fake_repo}/scripts/plan.sh {sub} t001 --result-file {notes / 't001-result.md'}",
    ]
    for cmd in ok:
        assert _bash(fake_repo, cmd) == "native", cmd   # 通る (TASKVIA_TOKEN 空 = native に委ねる)
    other = fake_repo / "queue/missions" / OTHER_SLUG / "notes"
    other.mkdir()
    (other / "t001-x.md").write_text("x")
    (notes / "t002-x.md").write_text("x")
    bad = [
        f"./scripts/plan.sh {sub} t001 --result-file {other / 't001-x.md'}",       # 他 mission
        f"./scripts/plan.sh {sub} t001 --result-file {notes / 't002-x.md'}",       # 他 task
        f"./scripts/plan.sh {sub} t001 --result-file queue/missions/{SLUG}/tasks/t001.md",
        f"./scripts/plan.sh {sub} t001 --result-file -",
        f"./scripts/plan.sh {sub} t001 'inline text'",
        f"./scripts/plan.sh {sub} t001",
        f"cd {fake_repo} && {fake_repo}/scripts/plan.sh {sub} t001 --result-file {rel}",  # cd の後ろの相対パス
        f"./scripts/plan.sh {sub} t001 --result-file {rel} --result-file {rel}",
        f"./scripts/plan.sh {sub} t001 --result-file - <<'EOF'\nx\nEOF",
    ]
    for cmd in bad:
        assert _bash(fake_repo, cmd) == "deny", cmd


def test_plan_result_file_needs_an_assignment(fake_repo):
    (fake_repo / "queue/missions" / SLUG / "notes" / "t001-r.md").write_text("r")
    (fake_repo / "queue/assignments/Jiwon").unlink()
    cmd = f"./scripts/plan.sh done t001 --result-file queue/missions/{SLUG}/notes/t001-r.md"
    assert _bash(fake_repo, cmd, skills="research") == "deny"


# ---- hook 側


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


def test_card_skills_count_even_when_task_id_is_already_set(fake_repo):
    # Codex 指摘 7 (P2): env の TASK_ID が設定済みで SKILLS が空 / research を含まなくても、
    # assignment の card の skills に research があれば対象
    for skills in ("", "docs"):
        for cmd in ("echo x > f", "command rm victim"):
            assert _bash(fake_repo, cmd, skills=skills, extra={"TASK_ID": "t001"}) == "deny", (skills, cmd)
        note = fake_repo / "queue/missions" / SLUG / "notes" / "t001-x.md"
        assert _write(fake_repo, note, skills=skills, extra={"TASK_ID": "t001"}) == "allow"
        assert _write(fake_repo, fake_repo / "hooks/x.sh", skills=skills, extra={"TASK_ID": "t001"}) == "deny"


def test_task_id_set_but_card_is_not_research_is_unchanged(fake_repo):
    _set_card_skills(fake_repo, "code")
    assert _bash(fake_repo, "echo x > f", skills="code", extra={"TASK_ID": "t001"}) == "native"


def test_other_skills_bash_is_unchanged(fake_repo):
    # research を含まない skill の Bash は今までどおり (リダイレクトも hook では止めない)
    _set_card_skills(fake_repo, "code")
    assert _bash(fake_repo, "echo x > f", skills="code") == "native"
    assert _bash(fake_repo, "cp a b", skills="bash") == "native"
    assert _bash(fake_repo, "cat <<'EOF'\nx\nEOF", skills="code") == "native"


def test_crashing_guard_fails_closed(fake_repo):
    (fake_repo / "hooks" / "lib_research_guard.py").write_text("raise SystemExit(7)\n")
    assert _bash(fake_repo, "ls") == "deny"
    assert _write(fake_repo, fake_repo / "queue/missions" / SLUG / "notes" / "t001-x.md") == "deny"


# ---- t007 (Codex 2 巡目 P1×4): 解釈して判定する部分を、解釈不要の形へさらに狭める


def test_git_specs_are_per_subcommand_and_strict():
    # 指摘 1: 共有の表をやめる。git grep は外す。どの表も strict (束ね不可・値は --name=<値>)
    assert ("grep",) not in LIB.GIT_SPECS
    specs = list(LIB.GIT_SPECS.values())
    assert len({id(s) for s in specs}) == len(specs), "git のサブコマンドが表を共有している"
    for key, spec in LIB.GIT_SPECS.items():
        assert spec.strict and not spec.short_arg and not spec.digits, key


@pytest.mark.parametrize("key", sorted(LIB.GIT_SPECS), ids=lambda k: " ".join(k))
def test_each_listed_git_short_option_alone_still_passes(key):
    # 束ねを拒否しても 1 字ずつなら通る (表が死んでいないことの確認)
    spec = LIB.GIT_SPECS[key]
    for c in spec.short:
        need = ["--list"] if key == ("config",) else []   # config は読み取りの形 (--get / --list) が要る
        if key in (("diff",), ("show",), ("log",)):
            need = ["--no-ext-diff", "--no-textconv"]         # 差分を出す git は両方必須 (t009)
        if key == ("blame",):
            need = ["--no-textconv", "f"]
        cmd = " ".join(["git", *key, f"-{c}", *need])
        assert LIB.check_bash(cmd)[0] == "allow", (cmd, LIB.check_bash(cmd)[1])


@pytest.mark.parametrize("cmd", [
    "git grep -LOecho zzz_unmatched -- hooks/lib_research_guard.py",
    "git grep -L -Oecho x",
    "git log -LOecho x",
    "git log -pOecho",
    "git show -sOecho",
    "git diff -wO echo",
    "git log -pL1,2:f",
    "git log -n5",
    "git blame -L1,5 f",
    "git log --max-count 5",
    "git log --open-files-in-pager=echo",
    "git log -c",
    "git diff --ext-diff",
    "git log --textconv -p",
    "git cat-file --textconv HEAD:x",
    "git cat-file --filters HEAD:x",
    "git ls-remote --upload-pack=echo origin",
    "git ls-remote --exec=echo origin",
    "git status -sb",
    "git branch -vv",
])
def test_git_bundles_and_pager_options_are_denied(cmd):
    assert LIB.check_bash(cmd)[0] == "deny", cmd


@pytest.mark.parametrize("glob", ["-c core.pager=echo", "-ccore.pager=echo", "-C .", "-C.", "--git-dir=.git",
                                  "--work-tree=.", "--exec-path=/tmp", "--no-pager", "-P", "-p",
                                  "--config-env=core.pager=X", "--namespace=x", "--bare"])
def test_git_global_options_are_denied(glob):
    for sub in ("log", "status", "fetch"):
        cmd = f"git {glob} {sub}"
        assert LIB.check_bash(cmd)[0] == "deny", cmd


@pytest.mark.parametrize("cmd,decision", [
    # 指摘 2: 許すのは引用符の外の完全一致のトークン 2>/dev/null と 2>&1 (直前が空白) だけ
    ("cat a 2>/dev/null", "allow"),
    ("cat a 2>&1 | head", "allow"),
    ("cat a 2>/dev/null&& ls", "allow"),
    ("cat a 2>/dev/null;ls", "allow"),
    ("uniq /dev/null '2'>/dev/null", "deny"),
    ('uniq /dev/null "2">/dev/null', "deny"),
    ("uniq /dev/null \\2>/dev/null", "deny"),
    ("uniq /dev/null x'2'>/dev/null", "deny"),
    # 引用符の中の > は文字: 2'>'/dev/null は「2>/dev/null という名前」の 1 語。uniq の位置引数が 2 つになり拒否
    ("uniq /dev/null 2'>'/dev/null", "deny"),
    ("cat a 1>/dev/null", "deny"),
    ("cat a >/dev/null", "deny"),
    ("cat a >>/dev/null", "deny"),
    ("cat a &>/dev/null", "deny"),
    ("cat a 2>>/dev/null", "deny"),
    ("cat a 2> /dev/null", "deny"),
    ("cat a 2>/dev/nullx", "deny"),
    ("cat a 2>/dev/null/x", "deny"),
    ("cat a 2>&2", "deny"),
    ("cat a 2>&1x", "deny"),
    ("cat a x2>/dev/null", "deny"),
    ("cat a 22>/dev/null", "deny"),
    ("cat a 2>/dev/stdout", "deny"),
    ("cat < a", "deny"),
    ("cat 0<a", "deny"),
    ("2>/dev/null cat a", "deny"),
])
def test_only_two_exact_redirect_tokens(cmd, decision):
    assert LIB.check_bash(cmd)[0] == decision, (cmd, LIB.check_bash(cmd)[1])


def test_quoted_fd_number_reproduction_does_not_write(tmp_path):
    # 指摘 2 の実害 (uniq が cwd に 2 という名前のファイルを作る) を本物の bash で確かめ、ガードが止める形であること
    (tmp_path / "in").write_text("a\n")
    cmd = "uniq in '2'>/dev/null"
    subprocess.run(["bash", "-c", cmd], cwd=str(tmp_path), check=False)
    assert (tmp_path / "2").exists(), "前提: bash では '2' が uniq の出力先になる"
    assert LIB.check_bash(cmd)[0] == "deny"


@pytest.mark.parametrize("cmd,decision", [
    # 指摘 4 / t016: git fetch --no-prune --no-prune-tags [origin [<ref>]] だけ。ほかのオプションは不可
    ("git fetch --no-prune --no-prune-tags", "allow"),
    ("git fetch --no-prune --no-prune-tags origin", "allow"),
    ("git fetch --no-prune --no-prune-tags origin main", "allow"),
    ("git fetch --no-prune --no-prune-tags origin task/x-y_z.1", "allow"),
    # 旧 3 形 (prune 設定が true だと ref・tag を消す) と、片方だけの形は拒否
    ("git fetch", "deny"),
    ("git fetch origin", "deny"),
    ("git fetch origin main", "deny"),
    ("git fetch --no-prune origin", "deny"),
    ("git fetch --no-prune-tags origin main", "deny"),
    # --no-prune 2 つが揃っていても、ほかのオプション・形は引き続き拒否
    ("git fetch --no-prune --no-prune-tags --prune origin", "deny"),
    ("git fetch --no-prune --no-prune-tags --tags origin", "deny"),
    ("git fetch --no-prune --no-prune-tags -q origin", "deny"),
    ("git fetch --no-prune --no-prune-tags --force origin main", "deny"),
    ("git fetch --no-prune --no-prune-tags origin main:main", "deny"),
    ("git fetch --no-prune --no-prune-tags upstream", "deny"),
    ("git fetch --prune --prune-tags origin", "deny"),
    ("git fetch --prune origin", "deny"),
    ("git fetch --prune-tags origin", "deny"),
    ("git fetch -p origin", "deny"),
    ("git fetch -P origin", "deny"),
    ("git fetch --tags origin", "deny"),
    ("git fetch --force origin main", "deny"),
    ("git fetch -q origin", "deny"),
    ("git fetch origin main -q", "deny"),
    ("git fetch --all", "deny"),
    ("git fetch --depth=1 origin", "deny"),
    ("git fetch origin main:main", "deny"),
    ("git fetch origin +main", "deny"),
    ("git fetch origin 'refs/heads/*'", "deny"),
    ("git fetch origin 'main^'", "deny"),
    ("git fetch origin a b", "deny"),
    ("git fetch upstream", "deny"),
    ("git fetch /tmp/repo", "deny"),
    ("git fetch 'ext::sh -c x'", "deny"),
    ("git fetch origin 'ext::x'", "deny"),
])
def test_git_fetch_only_three_forms(cmd, decision):
    assert LIB.check_bash(cmd)[0] == decision, (cmd, LIB.check_bash(cmd)[1])


def test_git_fetch_deny_reason_names_the_required_options():
    d, reason = LIB.check_bash("git fetch origin main")
    assert d == "deny" and "--no-prune --no-prune-tags" in reason, reason


def test_plan_sh_must_resolve_to_the_trusted_script(fake_repo, tmp_path):
    # 指摘 3: コマンド語を cwd に対して解決し、字面と realpath の両方が信頼する plan.sh であること
    (fake_repo / "scripts").mkdir()
    (fake_repo / "scripts" / "plan.sh").write_text("#!/bin/bash\n")
    evil = tmp_path / "evil"
    (evil / "scripts").mkdir(parents=True)
    (evil / "scripts" / "plan.sh").write_text("#!/bin/bash\ntouch victim\n")
    link_file = tmp_path / "lf" / "scripts"
    link_file.mkdir(parents=True)
    (link_file / "plan.sh").symlink_to(fake_repo / "scripts" / "plan.sh")   # 信頼する plan.sh への symlink
    (tmp_path / "ld").symlink_to(fake_repo / "scripts")                       # 信頼する scripts/ への symlink
    wt = fake_repo / ".claude/worktrees" / SLUG / "t001-demo"
    other_wt = fake_repo / ".claude/worktrees" / SLUG / "t002-demo"
    other_slug_wt = fake_repo / ".claude/worktrees" / OTHER_SLUG / "t001-demo"
    for w in (wt, other_wt, other_slug_wt):
        (w / "scripts").mkdir(parents=True)
        (w / "scripts" / "plan.sh").write_text("#!/bin/bash\n")
    ok = [
        "./scripts/plan.sh status",
        "scripts/plan.sh status",
        f"{fake_repo}/scripts/plan.sh status",
        f"timeout 60 {fake_repo}/scripts/plan.sh status",
        f"{wt}/scripts/plan.sh status",                       # 自分の worktree
        f"cd {wt} && {wt}/scripts/plan.sh status",            # cd の後ろでも絶対パスなら可
    ]
    for cmd in ok:
        assert _bash(fake_repo, cmd) == "native", cmd
    bad = [
        f"{evil}/scripts/plan.sh status",
        "/tmp/plan.sh status",
        "plan status",
        "plan.sh status",
        f"{link_file}/plan.sh status",                       # symlink 経由 (plan.sh は自分の位置から lib を読む)
        f"{tmp_path}/ld/plan.sh status",
        "./scripts/../scripts/plan.sh status",
        f"cd {evil} && ./scripts/plan.sh status",
        f"cd {tmp_path}/lf && ./scripts/plan.sh status",
        "cd scripts && ./plan.sh status",                    # 相対の cd は CDPATH を引きうる = 確定できない
        f"cd {wt} && ./scripts/plan.sh status",              # 同じコマンドの cd の後ろの相対パスは不可 (下の test)
        f"{other_wt}/scripts/plan.sh status",                # 他の task の worktree
        f"{other_slug_wt}/scripts/plan.sh status",           # 他の mission の worktree
        f"timeout 60 {evil}/scripts/plan.sh status",
    ]
    for cmd in bad:
        assert _bash(fake_repo, cmd) == "deny", cmd
    (fake_repo / "queue/assignments/Jiwon").unlink()        # assignment が無ければ worktree は信頼しない
    assert _bash(fake_repo, f"{wt}/scripts/plan.sh status") == "deny"


def test_plan_sh_trusted_root_comes_from_crewvia_repo_root(fake_repo, tmp_path):
    # CREWVIA_REPO_ROOT があればそれ、無ければ hook の位置 (fake_repo) を信頼する
    other = tmp_path / "other-repo"
    (other / "scripts").mkdir(parents=True)
    (other / "scripts" / "plan.sh").write_text("#!/bin/bash\n")
    assert _bash(fake_repo, f"{other}/scripts/plan.sh status") == "deny"
    assert _bash(fake_repo, f"{other}/scripts/plan.sh status", extra={"CREWVIA_REPO_ROOT": str(other)}) == "native"
    assert _bash(fake_repo, "./scripts/plan.sh status", extra={"CREWVIA_REPO_ROOT": str(other)}) == "deny"
    link = tmp_path / "repo-link"                              # repo root 自体が symlink の場所
    link.symlink_to(fake_repo)
    for cmd in (f"{link}/scripts/plan.sh status", f"{fake_repo}/scripts/plan.sh status"):
        assert _bash(fake_repo, cmd, extra={"CREWVIA_REPO_ROOT": str(link)}) == "native", cmd


@pytest.mark.parametrize("tmpl", [
    # 同じコマンドの中の cd は効かないことがある (パイプ = サブシェル・条件で飛ばされる・外部コマンド扱い)。
    # cd を追跡すると「repo にいる」と判定したまま、元の cwd (evil) の scripts/plan.sh が走る
    "cd {repo} | ./scripts/plan.sh status",
    "cd {repo} |& ./scripts/plan.sh status",
    "false && cd {repo}; ./scripts/plan.sh status",
    "true || cd {repo}; ./scripts/plan.sh status",
    "timeout 5 cd {repo}; ./scripts/plan.sh status",
    "cd {repo}/nonexistent || ./scripts/plan.sh status",
    "cd {repo} && ls; ./scripts/plan.sh status",
    "cd {repo} && ./scripts/plan.sh status",
])
def test_cd_in_the_same_command_does_not_vouch_for_a_relative_plan_sh(fake_repo, tmp_path, tmpl):
    evil = tmp_path / "evil"
    (evil / "scripts").mkdir(parents=True)
    (evil / "scripts" / "plan.sh").write_text("#!/bin/bash\ntouch victim\n")
    (fake_repo / "scripts").mkdir()
    (fake_repo / "scripts" / "plan.sh").write_text("#!/bin/bash\n")
    cmd = tmpl.format(repo=fake_repo)
    # 前提: パイプの中の cd は効かず、元の cwd の plan.sh が走る (本物の bash で確かめる)
    if tmpl.startswith("cd {repo} | "):
        p = subprocess.run(["bash", "-c", "cd {} | pwd".format(shlex.quote(str(fake_repo)))], cwd=str(evil),
                           capture_output=True, text=True)
        assert p.stdout.strip() == str(evil)
    assert _bash(fake_repo, cmd, cwd=evil) == "deny", cmd
    assert _bash(fake_repo, "./scripts/plan.sh status", cwd=evil) == "deny"     # cwd の plan.sh は信頼しない
    assert _bash(fake_repo, f"{fake_repo}/scripts/plan.sh status", cwd=evil) == "native"


# ---- t009 (Codex 3 巡目 指摘 3): 差分を出す git は外部 diff・textconv を起動しない形だけ

@pytest.mark.parametrize("cmd, decision", [
    ("git diff --no-ext-diff --no-textconv HEAD~1", "allow"),
    ("git diff --no-textconv --no-ext-diff --stat", "allow"),
    ("git show --no-ext-diff --no-textconv HEAD", "allow"),
    ("git show --no-ext-diff --no-textconv --stat HEAD", "allow"),
    ("git log -p --no-ext-diff --no-textconv -3", "allow"),
    ("git log --stat --no-ext-diff --no-textconv", "allow"),
    ("git blame --no-textconv f", "allow"),
    ("git log --oneline -3", "allow"),                      # 差分を出さない log はフラグ不要
    ("git log --format=%H --max-count=5 --since=2.weeks", "allow"),
    ("git log --graph --decorate --all", "allow"),
    ("git diff", "deny"),
    ("git diff HEAD~1", "deny"),
    ("git diff --stat", "deny"),
    ("git diff --no-ext-diff HEAD~1", "deny"),
    ("git diff --no-textconv HEAD~1", "deny"),
    ("git diff --no-index /dev/null /etc/hostname", "deny"),
    ("git diff --no-ext-diff --no-textconv --no-index /dev/null /etc/hostname", "deny"),
    ("git show HEAD", "deny"),
    ("git show --stat HEAD", "deny"),
    ("git show --no-patch HEAD", "deny"),                   # show は一律
    ("git show --no-ext-diff HEAD", "deny"),
    ("git log -p", "deny"),
    ("git log --patch", "deny"),
    ("git log -p --no-ext-diff", "deny"),
    ("git log --stat", "deny"),
    ("git log --follow -- f", "deny"),
    ("git log --cherry-pick HEAD...x", "deny"),
    ("git log --word-diff", "deny"),
    ("git log --name-only", "deny"),
    ("git blame f", "deny"),
    ("git blame -w f", "deny"),
])
def test_diffing_git_needs_no_ext_diff_and_no_textconv(cmd, decision):
    assert LIB.check_bash(cmd)[0] == decision, (cmd, LIB.check_bash(cmd)[1])


def _textconv_repo(tmp_path):
    # textconv を設定した使い捨ての repo (本物の git で「起動する / しない」を確かめる)
    r = tmp_path / "tc"
    r.mkdir()
    marker = tmp_path / "ran"
    g = ["git", "-C", str(r)]
    subprocess.run([*g, "init", "-q"], check=True)
    for k, v in (("user.email", "a@b"), ("user.name", "a"),
                 ("diff.tc.textconv", f"sh -c 'touch {marker}; cat \"$0\"'")):
        subprocess.run([*g, "config", k, v], check=True)
    (r / ".gitattributes").write_text("*.txt diff=tc\n")
    (r / "f.txt").write_text("a\n")
    subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-qm", "1"], check=True)
    (r / "f.txt").write_text("a\nb\n")
    subprocess.run([*g, "commit", "-qam", "2"], check=True)
    return r, marker


@pytest.mark.parametrize("cmd", [
    "git diff HEAD~1", "git show HEAD", "git log -p -1", "git blame f.txt",
])
def test_default_git_runs_textconv_and_the_required_flags_stop_it(tmp_path, cmd):
    # 前提: フラグ無しの形は本物の git で textconv を起動する (= 列挙から --textconv を外すだけでは止まらない)。
    # git blame も既定で起動する (Codex の指摘外。t009 の実測で見つけた)。フラグを足した形は起動しない
    r, marker = _textconv_repo(tmp_path)
    subprocess.run(cmd.split(), cwd=str(r), capture_output=True, check=True)
    assert marker.exists(), cmd
    assert LIB.check_bash(cmd)[0] == "deny", cmd
    marker.unlink()
    words = cmd.split()
    fixed = [*words[:2], *(["--no-textconv"] if words[1] == "blame" else ["--no-ext-diff", "--no-textconv"]), *words[2:]]
    subprocess.run(fixed, cwd=str(r), capture_output=True, check=True)
    assert not marker.exists(), fixed
    assert LIB.check_bash(" ".join(fixed))[0] == "allow", fixed


def test_log_without_diff_options_does_not_run_textconv(tmp_path):
    # フラグ不要にしている log の形が本当に textconv を起動しないこと (_LOG_NO_DIFF の根拠)
    r, marker = _textconv_repo(tmp_path)
    for cmd in ("git log --oneline -3", "git log --graph --decorate --all", "git log --format=%H --max-count=5"):
        assert LIB.check_bash(cmd)[0] == "allow", cmd
        subprocess.run(cmd.split(), cwd=str(r), capture_output=True, check=True)
        assert not marker.exists(), cmd


# ---- t011 (Codex 4 巡目 P2): research の範囲例外は _global.deny を上書きしない ----


def _add_global_deny(repo, *patterns):
    cfg = repo / "config" / "skill-permissions.yaml"
    text = cfg.read_text()
    assert "_global:\n  deny:\n" in text
    block = "".join(f'    - "{p}"\n' for p in patterns)
    cfg.write_text(text.replace("_global:\n  deny:\n", "_global:\n  deny:\n" + block, 1))


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
@pytest.mark.parametrize("token", ["", "tok"])
def test_global_deny_beats_the_research_write_scope(fake_repo, tool, token):
    note = fake_repo / "queue/missions" / SLUG / "notes" / "t001-x.md"
    assert _write(fake_repo, note, tool=tool, token=token) == "allow"  # 対照: 足す前は通る
    _add_global_deny(fake_repo, tool)
    assert _write(fake_repo, note, tool=tool, token=token) == "deny"


def test_global_deny_on_one_tool_leaves_the_other_tools_scope_intact(fake_repo):
    note = fake_repo / "queue/missions" / SLUG / "notes" / "t001-x.md"
    _add_global_deny(fake_repo, "Write")
    assert _write(fake_repo, note, tool="Write") == "deny"
    assert _write(fake_repo, note, tool="Edit") == "allow"
    assert _write(fake_repo, fake_repo / "hooks/x.sh", tool="Edit") == "deny"  # 範囲外は従来どおり


def test_global_deny_beats_the_research_bash_allowlist(fake_repo):
    assert _bash(fake_repo, "ls -la") == "native"  # 対照: allowlist を通る
    _add_global_deny(fake_repo, "Bash(ls*)")
    assert _bash(fake_repo, "ls -la") == "deny"
    assert _bash(fake_repo, "ls -la", token="tok") == "deny"


def test_other_skills_deny_beats_the_research_write_scope(fake_repo):
    # research と同時に付いた skill の deny も範囲例外より先。deny を持たない skill (docs) は対照
    cfg = fake_repo / "config" / "skill-permissions.yaml"
    text = cfg.read_text()
    assert "\nskills:\n" in text
    cfg.write_text(text.replace("\nskills:\n", "\nskills:\n  zz_nowrite:\n    deny:\n      - \"Write\"\n", 1))
    note = fake_repo / "queue/missions" / SLUG / "notes" / "t001-x.md"
    assert _write(fake_repo, note, skills="research,docs") == "allow"
    assert _write(fake_repo, note, skills="research,zz_nowrite") == "deny"


def test_unreadable_skill_perms_denies_the_research_write(fake_repo):
    (fake_repo / "hooks" / "lib_skill_perms.py").write_text("raise SystemExit(7)\n")
    note = fake_repo / "queue/missions" / SLUG / "notes" / "t001-x.md"
    assert _write(fake_repo, note) == "deny"


# ---- t013 (Codex 5 巡目 P2): yaml が読めないとき research の範囲例外は allow しない / date は +FORMAT だけ ----


def test_missing_yaml_denies_the_research_write_scope(fake_repo):
    note = fake_repo / "queue/missions" / SLUG / "notes" / "t001-x.md"
    assert _write(fake_repo, note) == "allow"  # 対照: yaml がある間は通る
    (fake_repo / "config" / "skill-permissions.yaml").unlink()
    assert _write(fake_repo, note) == "deny"
    assert _write(fake_repo, note, tool="Edit") == "deny"


@pytest.mark.parametrize("cmd", ["date 101212002026", "date -s 2026-01-01", "date --set=2026-01-01", "date +%F 101212002026"])
def test_date_rejects_clock_setting_forms(cmd):
    assert LIB.check_bash(cmd)[0] != "allow", cmd


@pytest.mark.parametrize("cmd", ["date", "date +%F", "date -d yesterday +%s", "date -u +%Y"])
def test_date_format_forms_still_pass(cmd):
    assert LIB.check_bash(cmd)[0] == "allow", cmd
