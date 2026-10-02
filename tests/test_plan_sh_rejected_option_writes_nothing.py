"""plan.sh の queue を書くサブコマンド × 全 option: 不正な値は「何も書かずに」拒否される (01c t035 / PR #274 Codex P1)。

「書いてから検証して失敗する」型は 01c で 4 回続いた (t029 reserve の ID・t030 時刻引数・t034 同じ ID の違う中身・
t035 `update --reset --pr-number invalid`)。Controller は即座に書く (巻き戻さない) ので、拒否されたコマンドが、
生きている Worker の枠を奪い試行を閉じてしまう。箇所ごとの手当てをやめ、構造で閉じる。

表の作り:

- 対象のサブコマンドは plan.sh の `QUEUE_MUTATING_SUBCOMMANDS`、option は各 `cmd_*` の `parse_opts({...})` の
  dict リテラルから**実装から導く** (表から導かない)。実装に option が増えて、下の `CASES` にも `EXEMPT` にも
  載っていなければ赤 (`test_every_option_of_every_mutating_subcommand_is_in_the_table`)
- `CASES`: (サブコマンド, option) → 書き込みが成功するはずの引数 + 不正な値を差した argv。実走して、
  **exit が非 0・queue の全バイトが不変** (`.lock` を除く)。基準の状態は「t001 が Ren で running・t002 / t003 は pending」
- `EXEMPT`: 値を取らない / どんな文字列も正しい option は、理由つきで除く (理由が空なら赤)
- 検査件数は `-s` / CI ログに `[rejected-option-table] ...` として出す

限界: 不正な値の拒否が「想定した検証」で起きたかは exit と無変化でしか見ていない (usage エラーでも通る)。
検証が書き込みより後ろにある欠陥 (今回の P1) は、書き込む相手が実在する状態 (running の試行・枠) で打つことで見える。
その状態の無いサブコマンド (verifying など) は、状態の違いによる拒否と区別できない (別の task で追加の表を足す余地)。
"""

from __future__ import annotations

import ast
import re

import pytest

import pull_execution_helpers as h
from pull_execution_helpers import Box, MISSION
from test_task_graph import _plan_py_source

M = MISSION
NOPE = "no-such-mission"


def _mutating_options() -> dict[str, set[str]]:
    src = _plan_py_source()
    m = re.search(r"^QUEUE_MUTATING_SUBCOMMANDS = (\{.*?\})$", src, re.DOTALL | re.MULTILINE)
    assert m, "QUEUE_MUTATING_SUBCOMMANDS が plan.sh に無い"
    mutating = ast.literal_eval(m.group(1))
    dispatch = dict(re.findall(r"'([a-z-]+)': (\w+)",
                               re.search(r"^dispatch = \{(.*?)^\}$", src, re.DOTALL | re.MULTILINE).group(1)))
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    found: dict[str, set[str]] = {}
    for sub in sorted(mutating):
        opts: set[str] = set()
        for node in ast.walk(funcs[dispatch[sub]]):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "parse_opts":
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        opts |= {k.value for k in arg.keys if isinstance(k, ast.Constant)}
        found[sub] = opts
    return found


# (サブコマンド, option) -> (agent, argv)。{X} は t001 の今の試行の ID。argv は不正な値を差した形
CASES = {
    ("init", "--mission"): (None, ["init", "title", "--mission", "bad/slug"]),
    ("add", "--mission"): (None, ["add", "new", "--skills", "code", "--mission", NOPE]),
    ("add", "--skills"): (None, ["add", "new", "--mission", M, "--skills", ""]),
    ("add", "--priority"): (None, ["add", "new", "--skills", "code", "--mission", M, "--priority", "urgent"]),
    ("add", "--target-dir"): (None, ["add", "new", "--skills", "code", "--mission", M, "--target-dir", "/no/such/dir"]),
    ("add", "--idle-timeout"): (None, ["add", "new", "--skills", "code", "--mission", M, "--idle-timeout", "abc"]),
    ("add", "--max-timeout"): (None, ["add", "new", "--skills", "code", "--mission", M, "--max-timeout", "abc"]),
    ("add", "--pr-number"): (None, ["add", "new", "--skills", "code", "--mission", M, "--pr-number", "abc"]),
    ("add", "--deliverable"): (None, ["add", "new", "--skills", "code", "--mission", M, "--deliverable", "bogus"]),
    ("pull", "--mission"): ("Sora", ["pull", "--agent", "Sora", "--skills", "code", "--task", "t002", "--mission", NOPE]),
    ("pull", "--skills"): ("Sora", ["pull", "--agent", "Sora", "--task", "t002", "--mission", M, "--skills", ""]),
    ("pull", "--agent"): (None, ["pull", "--skills", "code", "--task", "t002", "--mission", M, "--agent", "bad name!"]),
    ("pull", "--target-dir"): ("Sora", ["pull", "--agent", "Sora", "--skills", "code", "--task", "t002", "--mission", M,
                                        "--target-dir", "/no/such/dir"]),
    ("pull", "--task"): ("Sora", ["pull", "--agent", "Sora", "--skills", "code", "--mission", M, "--task", "bogus"]),
    ("done", "--mission"): ("Ren", ["done", "t001", "r", "--no-pr", "x", "--mission", NOPE]),
    ("done", "--pr"): ("Ren", ["done", "t001", "r", "--mission", M, "--pr", "invalid"]),
    ("done", "--no-pr"): ("Ren", ["done", "t001", "r", "--mission", M, "--no-pr", ""]),
    ("done", "--result-file"): ("Ren", ["done", "t001", "--no-pr", "x", "--mission", M, "--result-file", "/no/such/file"]),
    ("done", "--execution"): ("Ren", ["done", "t001", "r", "--no-pr", "x", "--mission", M, "--execution", "not-an-id"]),
    ("fail", "--mission"): ("Ren", ["fail", "t001", "--no-head", "x", "--mission", NOPE]),
    ("fail", "--head"): ("Ren", ["fail", "t001", "--mission", M, "--head", ""]),
    ("fail", "--no-head"): ("Ren", ["fail", "t001", "--mission", M, "--no-head", ""]),
    ("fail", "--execution"): ("Ren", ["fail", "t001", "--no-head", "x", "--mission", M, "--execution", "not-an-id"]),
    ("needs-director", "--mission"): ("Ren", ["needs-director", "t001", "r", "--mission", NOPE]),
    ("needs-director", "--result-file"): ("Ren", ["needs-director", "t001", "--mission", M, "--result-file", "/no/such/file"]),
    ("needs-director", "--execution"): ("Ren", ["needs-director", "t001", "r", "--mission", M, "--execution", "not-an-id"]),
    ("ready-for-verification", "--mission"): ("Ren", ["ready-for-verification", "t001", "--mission", NOPE]),
    ("ready-for-verification", "--execution"): ("Ren", ["ready-for-verification", "t001", "--mission", M,
                                                        "--execution", "not-an-id"]),
    ("verifying", "--verifier"): ("Ren", ["verifying", "t001", "--mission", M, "--verifier", ""]),
    ("verifying", "--mission"): ("Ren", ["verifying", "t001", "--verifier", "V", "--mission", NOPE]),
    ("verifying", "--execution"): ("Ren", ["verifying", "t001", "--verifier", "V", "--mission", M,
                                           "--execution", "not-an-id"]),
    ("verify-result", "--mission"): ("Ren", ["verify-result", "t001", "pass", "--mission", NOPE]),
    ("verify-result", "--notes"): ("Ren", ["verify-result", "t001", "bogus-verdict", "--mission", M, "--notes", "n"]),
    ("verify-result", "--notes-file"): ("Ren", ["verify-result", "t001", "pass", "--mission", M,
                                                "--notes-file", "/no/such/file"]),
    ("verify-result", "--execution"): ("Ren", ["verify-result", "t001", "pass", "--mission", M,
                                               "--execution", "not-an-id"]),
    ("snapshot", "--section-file"): ("Ren", ["snapshot", "t001", "--mission", M, "--section-file", "/no/such/file"]),
    ("snapshot", "--mission"): ("Ren", ["snapshot", "t001", "--section-file", "/dev/null", "--mission", NOPE]),
    ("release-dep", "--mission"): (None, ["release-dep", "t002", "--mission", NOPE]),
    ("release-dep", "--dep"): (None, ["release-dep", "t002", "--mission", M, "--dep", "t999"]),
    ("retire", "--mission"): (None, ["retire", "t001", "--agent", "Ren", "--execution", "{X}", "--mission", NOPE]),
    ("retire", "--agent"): (None, ["retire", "t001", "--execution", "{X}", "--mission", M, "--agent", "bad name!"]),
    ("retire", "--started-at"): (None, ["retire", "t001", "--agent", "Ren", "--mission", M, "--started-at", ""]),
    ("retire", "--execution"): (None, ["retire", "t001", "--agent", "Ren", "--mission", M, "--execution", ""]),
    ("retire", "--outcome"): (None, ["retire", "t001", "--agent", "Ren", "--execution", "{X}", "--mission", M,
                                     "--outcome", "bogus"]),
    ("retire", "--reason"): (None, ["retire", "t001", "--agent", "Ren", "--execution", "{X}", "--mission", M,
                                    "--outcome", "needs-director", "--reason", ""]),
    ("update", "--mission"): ("Director", ["update", "t001", "--reset", "--mission", NOPE]),
    ("update", "--blocked-by"): ("Director", ["update", "t001", "--reset", "--mission", M, "--blocked-by", "t001"]),
    ("update", "--priority"): ("Director", ["update", "t001", "--reset", "--mission", M, "--priority", "urgent"]),
    ("update", "--status"): ("Director", ["update", "t001", "--reset", "--mission", M, "--status", "bogus"]),
    ("update", "--pr-number"): ("Director", ["update", "t001", "--reset", "--mission", M, "--pr-number", "invalid"]),
    ("update", "--deliverable"): ("Director", ["update", "t001", "--reset", "--mission", M, "--deliverable", "bogus"]),
}

#: 不正な値が無い option (理由つき)。理由を空にすると赤
EXEMPT = {
    ("init", "--force"): "値を取らない flag",
    ("init", "--inactive"): "値を取らない flag",
    ("update", "--skills"): "任意の csv。不正な値が無い",
    ("update", "--worker"): "任意の名前 (null / none / 空 は worker を外す正規の指定)",
    ("update", "--description"): "任意の文章。不正な値が無い",
    ("update", "--reset"): "値を取らない flag",
    ("update", "--close-execution"): "値を取らない flag",
    ("retire", "--no-wait"): "値を取らない flag",
    ("reap-orphan-assignment", "--no-wait"): "値を取らない flag",
    ("add", "--description"): "任意の文章。不正な値が無い",
    ("add", "--blocked-by"): "存在しない task id も受理する (依存の検査は lint / pull の保留。書いてから失敗する経路ではない)",
}

#: 値を取らない option が無く、parse_opts を使わないサブコマンド (引数は位置引数だけ)
NO_OPTION_SUBCOMMANDS = {"archive", "review", "launch"}


def test_every_option_of_every_mutating_subcommand_is_in_the_table():
    found = _mutating_options()
    assert found, "queue を書くサブコマンドを 1 つも導けなかった"
    derived = {(sub, opt) for sub, opts in found.items() for opt in opts}
    tabled = set(CASES) | set(EXEMPT)
    assert derived - tabled == set(), (
        f"表に無い option: {sorted(derived - tabled)} — CASES に不正な値の argv を足すか、EXEMPT に理由を書く")
    assert tabled - derived == set(), f"実装に無い option が表にある: {sorted(tabled - derived)}"
    assert not (set(CASES) & set(EXEMPT)), "CASES と EXEMPT の両方にある"
    assert all(reason.strip() for reason in EXEMPT.values()), "EXEMPT の理由が空"
    assert {s for s, o in found.items() if not o} <= NO_OPTION_SUBCOMMANDS | {"reap-orphan-assignment"}, (
        f"option を 1 つも導けないサブコマンド: {sorted(s for s, o in found.items() if not o)}")


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path / "root", tasks=("t001", "t002", "t003"))
    p = b.pull("Ren")
    assert p.returncode == 0, p.stderr
    # 回復 (R-1〜R-4) は最初の書き込み系コマンドが行う。表の実走の前に済ませる (拒否と無関係な修復を「変化」と読まない)
    p = b.plan("add", "warm-up", "--skills", "code", "--mission", M, agent="Director")
    assert p.returncode == 0, p.stderr
    return b


def _argv(box, argv):
    x = box.card()["current_execution_id"]
    return [a.replace("{X}", x) for a in argv]


def test_the_rejected_option_leaves_the_queue_untouched(box, capsys):
    ran, bad = 0, []
    for (sub, opt), (agent, argv) in sorted(CASES.items()):
        before = box.snapshot()
        p = box.plan(*_argv(box, argv), agent=agent)
        after = box.snapshot()
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        if p.returncode == 0:
            bad.append((sub, opt, "不正な値が受理された"))
        elif changed:
            bad.append((sub, opt, "拒否されたのに queue が変わった", changed[:4]))
        ran += 1
    with capsys.disabled():
        print(f"\n[rejected-option-table] サブコマンド {len({s for s, _ in CASES})} 個 × option {ran} 件を実走"
              f" (EXEMPT {len(EXEMPT)} 件。全て exit 非 0・queue 不変)")
    assert bad == [], bad
    assert ran == len(CASES)


def test_the_p1_reproduction_update_reset_with_a_bad_pr_number_keeps_the_running_attempt(box):
    """PR #274 Codex P1 そのもの: 拒否された `update --reset` が、生きている Worker の試行を閉じ枠を外してはいけない。"""
    x = box.card()["current_execution_id"]
    slot = box.slot("Ren")
    p = box.plan("update", "t001", "--reset", "--mission", M, "--pr-number", "invalid", agent="Director")
    assert p.returncode != 0
    meta = box.card()
    assert (meta["status"], meta["worker"], meta["execution_status"], meta["current_execution_id"]) == (
        "in_progress", "Ren", "running", x)
    assert box.slot("Ren") == slot and box.record(x)["status"] == "running"
    p = box.plan("update", "t001", "--reset", "--mission", M, "--blocked-by", "t001", agent="Director")
    assert p.returncode != 0 and box.card()["execution_status"] == "running" and box.slot("Ren") == slot


def test_a_valid_combination_with_reset_still_works_and_applies_every_option(box):
    p = box.plan("update", "t001", "--reset", "--mission", M, "--pr-number", "7", "--priority", "low",
                 "--blocked-by", "t002", agent="Director")
    assert p.returncode == 0, p.stderr
    meta = box.card()
    assert (meta["status"], meta["pr_number"], meta["priority"], meta["blocked_by"]) == ("pending", 7, "low", ["t002"])
    assert meta["execution_status"] == "failed" and box.slot("Ren") is None
    p = box.plan("update", "t001", "--mission", M, "--pr-number", "null", agent="Director")
    assert p.returncode == 0 and "pr_number" not in box.card()
