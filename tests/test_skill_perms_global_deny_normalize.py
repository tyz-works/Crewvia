"""_global.deny を、複合コマンドの各文・git の大域オプション・前置きを外した形でも照合する (t001)。

権限昇格コマンドの名前は global deny に当たるので、ここでも伏せ字で書いて実行時に組み立てる。
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import lib_skill_perms
from lib_skill_perms import check_permission, load_config

YAML_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "skill-permissions.yaml")
ESC = "su" + "do"  # 伏せ字
RMRF = "rm " + "-" + "rf"  # 伏せ字


def _decision(cmd: str, skills: str = "code") -> str:
    return check_permission(load_config(YAML_PATH), skills, f"Bash({cmd})")["decision"]


DENIED = [
    # (a) git の大域オプション
    "git -cfoo=1 push --force origin main",
    "git -c foo=1 push origin main",
    "git -C /x push origin main",
    "git -C /x push origin master",
    "git --no-pager push -f origin x",
    "git -P push origin main",
    "git --git-dir=/x/.git push origin main",
    "git --git-dir /x/.git --work-tree /x push --force origin x",
    "git --namespace=n -p push origin main",
    "git --exec-path=/y --bare push origin main",
    "git --literal-pathspecs --no-replace-objects push origin main",
    "git --config-env=a=B push origin main",
    "git -ck=v push origin master",
    # (b) 複合コマンドの 2 文目以降
    "cd x && git push origin main",
    "true; git push --force",
    "false || git push -f origin x",
    "echo hi | git push origin main",
    "echo a\ngit push origin main",
    "(cd x && git push origin main)",
    "if true; then git push origin main; fi",
    'echo "$(git push origin main)"',
    "echo `git push origin main`",
    "cd x && git -C y push --force-with-lease origin x",
    # (c) 前置き
    "env A=1 git push origin main",
    "env -i A=1 git push origin main",
    "env -u X git push origin main",
    "A=1 git push origin main",
    "A=1 B=2 git -C x push origin main",
    "/usr/bin/git push origin main",
    "./git push origin main",
    "command git push origin main",
    "builtin exec git push origin main",
    "nohup git push origin main",
    "time git push origin main",
    "nice -n 5 git push origin main",
    "timeout 30 git push origin main",
    "exec -a x git push origin main",
    "A=1 env B=2 command /usr/bin/git -C x push origin main",
    # bash -c / sh -c / eval は 1 段だけ
    "bash -c 'git push origin main'",
    'sh -c "cd x && git -C y push origin main"',
    "bash -lc 'git push -f origin x'",
    "eval 'git push origin main'",
    # --- attempt 4: Codex の P1×4 + 自分で試した形 (設計の原則 A〜C) ---
    # (A) here-string・区切り語に自信が無いヒアドキュメント
    "cat <<< foo; git push origin main",
    "cat <<<x && git push origin main",
    "cat <<'A B'\nx\nA B\ngit push origin main",
    "cat <<'E'\"O\"F\ngit push origin main\nEOF",
    "cat <<'E O'\ngit push origin main\nE O",  # 区切りが空白入り = 本文を飛ばさない (誤検出側)
    # (B) リダイレクトが語に残る
    "git push origin main 2>/dev/null",
    "git push origin main>/dev/null",
    "git push origin main 2>&1",
    "git push origin main &>/dev/null",
    "git push origin main >>log 2>&1",
    ">out git push origin main",
    "2>/dev/null git push origin main",
    "git -C x push origin main </dev/null",
    "echo hi > $(git push origin main)",
    "echo hi >`git push origin main`",
    "diff <(git push origin main) x",
    # (C) env -S・殻の入れ子・置換の入れ子・stdin から読むコード
    "env -S 'git push origin main'",
    "env -S'git push origin main'",
    "env --split-string='git push origin main'",
    "env -iS 'git push origin main'",
    "env -S 'git -C x push' origin main",
    "bash -c 'echo \"$(git push origin main)\"'",
    "bash -c 'echo `git push origin main`'",
    'sh -c "echo \\$(git push origin main)"',
    "bash -c \"bash -c 'git push origin main'\"",
    "bash -c \"bash -c \\\"sh -c 'git push origin main'\\\"\"",
    "eval \"eval 'git push origin main'\"",
    "bash <<< 'git push origin main'",
    "bash <<EOF\ngit push origin main\nEOF",
    "bash <<'EOF'\ngit push origin main\nEOF",
    "cat <<EOF\n$(git push origin main)\nEOF",
    "cat <<EOF\n`git push origin main`\nEOF",
    "xargs git push origin main",
    "echo x | xargs -I{} git push origin main",
    "find . -exec git push origin main \\;",
    "trap 'git push origin main' EXIT",
    "watch -n 1 'git push origin main'",
    "stdbuf -oL git push origin main",
    "ionice -c 3 git push origin main",
    # 引用符・エスケープ・制御構文
    "\\git push origin main",
    "'git' push origin main",
    'g"it" push origin ma\'\'in',
    "git push origin main # c",
    "for i in 1; do git push origin main; done",
    "{ git push origin main; }",
    "f() { git push origin main; }; f",
    "$'git' push origin main",
    # 既存の網 (複合の再帰削除・昇格) も文単位で
    f"cd /tmp && env A=1 {RMRF} /",
    f"cd /tmp && {ESC} rm foo",
    f"env A=1 {ESC} ls",
]

ALLOWED = [
    "git status",
    "git -C /x push origin feature",
    "git -C /x status && git log --oneline",
    'git commit -m "fix: git push origin main の説明"',
    "git commit -m 'a; git push origin main'",
    'git commit -m "x && git push --force"',
    "echo git push origin main",
    'echo "git push origin main"',
    "echo 'cd x && git push origin main'",
    "cat <<'EOF'\ngit push origin main\nEOF",
    "cat <<EOF\ngit push --force\nEOF\necho done",
    "cat <<-EOF\n\tgit push origin main\n\tEOF\nls",
    'git commit -m "$(cat <<\'EOF\'\nmsg\ngit push origin main\nEOF\n)"',
    "# git push origin main",
    "grep -r 'git push --force' docs/",
    "git push origin feature/x",
    "git push --set-upstream origin task/x",
    "git push -u origin feat",
    "env A=1 git status",
    "bash -c 'git status'",
    "ls && pwd",
    "cat <<< 'git push origin main'",
    "echo hi 2>&1 | cat",
    "cat > f <<'EOF'\ngit push origin main\nEOF\nls",
    "git commit -F - <<'EOF'\nmsg git push origin main\nEOF",
    "git push origin feature 2>&1",
    "git push origin feature >/dev/null",
    "env -S 'git status'",
    "xargs git status",
    "find . -exec ls {} \\;",
    "trap 'echo x' EXIT",
    "echo hi > git_push_origin_main.txt",
    "cat <<A\nx\nA\ngit status",
]


@pytest.mark.parametrize("cmd", DENIED)
def test_denied(cmd):
    r = check_permission(load_config(YAML_PATH), "code", f"Bash({cmd})")
    assert r["decision"] == "deny", cmd
    assert r["source"].startswith("_global:deny"), r


@pytest.mark.parametrize("cmd", ALLOWED)
def test_not_global_denied(cmd):
    r = check_permission(load_config(YAML_PATH), "code", f"Bash({cmd})")
    assert not r["source"].startswith("_global:deny"), (cmd, r)


def test_meaning_of_existing_patterns_unchanged():
    # もともと deny のものは deny のまま / --force-with-lease が git push --force* に当たるのも今のまま
    assert _decision("git push origin main") == "deny"
    assert _decision("git push --force-with-lease origin x") == "deny"
    assert _decision("git push origin main-fix") == "deny"  # `git push origin main*` に今も当たる (変えない)
    assert _decision("git push origin feature") != "deny"


def test_no_skills_still_global_denied():
    assert check_permission(load_config(YAML_PATH), "", "Bash(cd x && git -C y push origin main)")["decision"] == "deny"


def test_non_bash_tool_untouched():
    assert check_permission(load_config(YAML_PATH), "code", "Edit")["decision"] != "deny"


def test_regression_candidates_removed_goes_red(monkeypatch):
    # 正規化の候補を空にする退行 → (a)(b)(c) が素通りする (= 上のテスト群が赤になる) ことの実証
    monkeypatch.setattr(lib_skill_perms, "_candidate_sigs", lambda cmd: [])
    assert _decision("git -C /x push origin main") != "deny"
    assert _decision("cd x && git push origin main") != "deny"
    assert _decision("env A=1 git push origin main") != "deny"


def test_regression_no_git_global_strip(monkeypatch):
    monkeypatch.setattr(lib_skill_perms, "_strip_git_globals", lambda w: w)
    assert _decision("git -C /x push origin main") != "deny"


@pytest.mark.parametrize("make", [
    lambda: "a && " * 200000 + "true",
    lambda: '"$(' * 250000,
    lambda: "git status; " * 90000,
    lambda: "(" * 500000,
    lambda: "$(" * 300000,
    lambda: "`" * 500000,
    lambda: "x=" + "'" * 400000,
    lambda: "bash -c 'bash -c " * 50000,
    lambda: "cat <<EOF\n" * 100000,
    lambda: "A=1 " * 250000 + "git push origin main",
])
def test_large_input_is_linear(make):
    cmd = make()
    assert len(cmd) >= 400000
    t = time.time()
    _decision(cmd)
    assert time.time() - t < 10
