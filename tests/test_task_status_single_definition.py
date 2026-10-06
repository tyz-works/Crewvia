#!/usr/bin/env python3
"""task の status の語彙と許可遷移が、`scripts/lib_task_status.py` の 1 か所にあること (vNext 01a S1)。

## なぜ構造で見るのか

status の集合は以前 lint_plan.py / plan.sh / dispatcher.sh / taskvia-sync.sh / lib_dep_rules.py に
コピーがあり、揃え漏れが実害になっていた:

* `needs_director` を plan.sh 自身が書くのに lint が FAIL (lint と update の集合に無かった)
* `cancelled` は書き手が 0 なのに、依存判定・task-graph・PR 待ちの 3 か所に現れた
* `TERMINAL_STATUSES` が plan.sh と dispatcher.sh に二重定義
* 「もう終わっている」の判定が command ごとに違った

1 件ずつ直しても 4 回目は別の場所に出るので、「status の文字列を 2 つ以上並べたリテラルが
定義モジュールの外にあるか」そのものを AST で見る。**検査したリテラルの件数を出し、0 件では
通さない** (memory: registry-dir-single-definition-and-vacuous-static-guards)。

## 見えるもの / 見えないもの

見える: `.py` 全部と `.sh` の heredoc の中の python の `{...}` / `(...)` / `[...]` / `frozenset({...})` /
`x in (...)` で、status の名前が 2 つ以上並んだもの。
見えない (**実態より狭く書かない**): 1 つだけの比較 (`== 'done'`。これは集合ではない)・
plan.sh 内 jq の `.status == "done"` (mission の表示)・動的に組み立てた集合・`tests/` の中。
`tests/` を対象外にしているのは、テストが期待値を**設計の写し**として持つため
(本物を直したとき、テストの写しが赤くなるのは意図した動作)。

    python3 -m pytest tests/test_task_status_single_definition.py -v
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lib_task_status  # noqa: E402
from fixture_tree import copy_plan_tree  # noqa: E402

DEFINITION = SCRIPTS_DIR / "lib_task_status.py"

#: task の status ではないが、名前が同じ文字列。読み取りの擬似 status・mission の status も含む。
#: これらが 2 つ以上並んだリテラルは (task の) 語彙のコピーの疑いがあるので、下の allowlist に
#: 理由を書いたものだけ通す。
_EXTRA_NAMES = frozenset({"cancelled", "corrupted", "drafting", "reviewing", "ready"})


def _status_names() -> frozenset[str]:
    return frozenset(lib_task_status.TASK_STATUSES) | _EXTRA_NAMES


#: (ファイル名, 行の前後 1 行を含む断片に含まれる印) → 理由。**該当が無くなったら赤**
#: (直したのに allowlist が残っていると、次の同じ形が黙って通る)。
ALLOWLIST = {
    ("dispatcher.sh", "('idle', 'done', 'blocked')"):
        "herdr の**ペイン**の agent status (idle / done / blocked)。task の status ではない",
    ("lib_mux.py", '("blocked", "working", "idle", "done", "unknown")'):
        "herdr の**ペイン**の agent status。task の status ではない",
    ("plan.sh", "('drafting', 'ready')"):
        "**mission** の status (drafting / ready)。mission の語彙は S1 の範囲外 (state-store.md §0)",
    ("plan.sh", "('done', 'verified')"):
        "`plan.sh status` の表示の分岐 (done と verified は同じ「完了」表示。skipped は別の分岐)。"
        "集合として何かを判定していない",
    ("plan.sh", "'ready-for-verification', 'verifying', 'snapshot'"):
        "`QUEUE_MUTATING_SUBCOMMANDS` —— queue を書き換える**サブコマンド名**の集合。`done` / `verifying` は"
        "status の名前でもあるが、ここは subcommand (S5 / t020 が `verifying` を足した。"
        "`lib_task_status.ACCEPTS_FROM` の command キーと同じ語)。task の status を判定していない",
}


def _python_blocks(path: pathlib.Path) -> list[str]:
    text = path.read_text(errors="replace")
    if path.suffix == ".py":
        return [text]
    blocks = []
    for m in re.finditer(r"<<-?['\"]?(\w+)['\"]?\n(.*?)\n[ \t]*\1\n", text, re.DOTALL):
        try:
            ast.parse(m.group(2))
        except SyntaxError:
            continue          # bash の heredoc (python ではない)
        blocks.append(m.group(2))
    return blocks


def scan_status_sets(source: str, names: frozenset[str], min_values: int = 2):
    """`(行, 断片, 値)` を、status の名前が `min_values` 個以上並んだリテラルの全部について返す。

    合成ソースでも呼べる形にしてある (検出器そのものの陽性対照のため)。
    """
    tree = ast.parse(source)
    found = []
    inspected = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            continue
        inspected += 1
        values = [e.value for e in node.elts
                  if isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value in names]
        if len(values) >= min_values:
            found.append((node.lineno, " ".join((ast.get_source_segment(source, node) or "").split()), values))
    return found, inspected


def _targets():
    files = sorted(SCRIPTS_DIR.glob("*.py")) + sorted(SCRIPTS_DIR.glob("*.sh"))
    files += sorted((REPO_ROOT / "hooks").glob("*.py")) + sorted((REPO_ROOT / "hooks").glob("*.sh"))
    files.append(REPO_ROOT / "crewvia")
    return [f for f in files if f.exists() and f.resolve() != DEFINITION.resolve()]


# ---------------------------------------------------------------------------
# 検出器の陽性対照 (実際の書き方で)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", [
    "TERMINAL = {'done', 'verified', 'skipped'}",                       # dispatcher.sh / plan.sh の旧形
    "if st in ('done', 'verified', 'skipped'):\n    pass",              # taskvia-sync.sh の旧形
    "S = frozenset({'in_progress', 'verifying'})",                       # frozenset の中
    "X = ['pending', 'blocked']",                                        # list
    "if cur not in ('in_progress', 'verifying', 'x'):\n    pass",         # 3 つ目が別物でも 2 つで検出
    "R = TERMINAL | {'done', 'failed'} | {'pending'}",                   # 合成の中に並べ直し
])
def test_the_detector_sees_real_shapes(source):
    found, _ = scan_status_sets(source, _status_names())
    assert found, f"検出できない形: {source!r}"


@pytest.mark.parametrize("source", [
    "if st == 'done':\n    pass",                       # 1 つだけの比較は集合ではない
    "X = {'pending'}",
    "X = ('idle', 'done')",                              # status の名前が 1 つ
    "D = {'done': 1, 'failed': 2}",                      # dict のキーは (別の検査で) 見る
])
def test_the_detector_ignores_non_sets(source):
    found, _ = scan_status_sets(source, _status_names())
    assert not found, f"誤検出: {source!r} → {found}"


# ---------------------------------------------------------------------------
# 本体: 定義モジュールの外に status の集合が無い
# ---------------------------------------------------------------------------

def test_no_status_set_is_defined_outside_lib_task_status():
    names = _status_names()
    hits = []
    inspected_literals = 0
    parsed_blocks = 0
    for path in _targets():
        for block in _python_blocks(path):
            parsed_blocks += 1
            found, inspected = scan_status_sets(block, names)
            inspected_literals += inspected
            for lineno, segment, values in found:
                hits.append((path.name, lineno, segment, values))

    # 0 件で通さない: glob / heredoc の抽出が壊れたら、ここで赤くなる。
    assert parsed_blocks >= 40, f"python として読めたブロックが {parsed_blocks} 件しかない (抽出が壊れている)"
    assert inspected_literals >= 500, f"検査したリテラルが {inspected_literals} 件しかない (走査が壊れている)"
    print(f"[status-set guard] ブロック {parsed_blocks} / リテラル {inspected_literals} / 検出 {len(hits)}")

    used = set()
    offenders = []
    for name, lineno, segment, values in hits:
        key = next((k for k in ALLOWLIST if k[0] == name and k[1] in segment), None)
        if key:
            used.add(key)
        else:
            offenders.append(f"{name}:{lineno} {segment} {values}")

    assert not offenders, (
        "status の集合が lib_task_status.py の外にある (import して使うこと。足りない集合は "
        "lib_task_status に足す):\n  " + "\n  ".join(offenders))
    stale = set(ALLOWLIST) - used
    assert not stale, f"allowlist に載っているのに該当が無い (直したなら消す): {sorted(stale)}"


# ---------------------------------------------------------------------------
# 表・辞書の語彙が定義と一致する / 定義モジュール自身の整合
# ---------------------------------------------------------------------------

def _dict_keys_assigned_to(name: str) -> set[str]:
    src = (SCRIPTS_DIR / "plan.sh").read_text()
    for block in _python_blocks(SCRIPTS_DIR / "plan.sh"):
        for node in ast.walk(ast.parse(block)):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                    and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)):
                return {k.value for k in node.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError(f"{name} が plan.sh に無い ({len(src)} bytes を走査)")


@pytest.mark.parametrize("name", ["STATUS_ICON", "TASK_GRAPH_STATUS_MAP"])
def test_presentation_maps_only_use_the_vocabulary(name):
    """表示の対応表 (キーが status の dict) は、語彙に無い status を持たない。

    `cancelled` が TASK_GRAPH_STATUS_MAP に残っていたのが実例 (書き手が無いのに表だけ持っていた)。
    """
    keys = _dict_keys_assigned_to(name)
    assert keys, f"{name} のキーを読めない"
    unknown = keys - set(lib_task_status.TASK_STATUSES)
    assert not unknown, f"{name} に語彙に無い status がある: {sorted(unknown)}"


def test_the_vocabulary_is_self_consistent():
    S = lib_task_status
    assert "cancelled" not in S.TASK_STATUSES
    assert len(S.TASK_STATUSES) == 12
    for group in (S.TERMINAL_STATUSES, S.RELEASED_WORK_STATUSES, S.ASSIGNMENT_HOLDING_STATUSES,
                  S.WAITS_ON_DIRECTOR_STATUSES, S.AWAITING_DECISION_STATUSES, S.PR_NOT_AWAITED_STATUSES, S.EXECUTING_STATUSES,
                  set(S.HELD_DEP_STATUSES)):
        assert group <= S.TASK_STATUSES, group - S.TASK_STATUSES
    for command, sources in S.ACCEPTS_FROM.items():
        assert sources and sources <= S.TASK_STATUSES, command
    assert S.RELEASED_WORK_STATUSES == S.TERMINAL_STATUSES | set(S.HELD_DEP_STATUSES)
    # 完了した status から新しい実行を始められるコマンドは無い
    for command in ("pull", "needs-director", "ready-for-verification", "retire", "verifying"):
        assert not (S.ACCEPTS_FROM[command] & S.TERMINAL_STATUSES), command


def test_consumers_read_the_shared_sets():
    """消費者が同じ集合オブジェクトを読んでいる (値が同じ別物ではない)。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lib_dep_rules", SCRIPTS_DIR / "lib_dep_rules.py")
    dep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dep)
    assert tuple(dep.HELD_DEP_STATUSES) == tuple(lib_task_status.HELD_DEP_STATUSES)
    for path, needle in (
        (SCRIPTS_DIR / "plan.sh", "_load_scripts_module('lib_task_status')"),
        (SCRIPTS_DIR / "dispatcher.sh", "from lib_task_status import"),
        (SCRIPTS_DIR / "taskvia-sync.sh", "from lib_task_status import"),
        (SCRIPTS_DIR / "verifier-dispatcher.sh", "from lib_task_status import"),
        (SCRIPTS_DIR / "lint_plan.py", "_load_scripts_module('lib_task_status')"),
    ):
        assert needle in path.read_text(), f"{path.name} が lib_task_status を読んでいない"


# ---------------------------------------------------------------------------
# 挙動: 隔離した queue / repo root で plan.sh を実走させる
# ---------------------------------------------------------------------------

MISSION = "m-status"


def _card(task_id, status, extra=()):
    lines = ["---", f"id: {task_id}", f"title: task {task_id}", "skills: [code]",
             "priority: medium", f"status: {status}", "blocked_by: []", "target_dir: null",
             "worker: Ren", "started_at: null", "completed_at: null", *extra, "---", "",
             "## Description", "", "fixture", "", "## Result", ""]
    return "\n".join(lines) + "\n"


class Sandbox:
    def __init__(self, tmp_path):
        import os
        self.os = os
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / "registry" / "mux").mkdir(parents=True)
        copy_plan_tree(self.root)
        self.queue = self.root / "queue"
        self.tasks = self.queue / "missions" / MISSION / "tasks"
        self.tasks.mkdir(parents=True)
        (self.queue / "archive").mkdir()
        (self.queue / "missions" / MISSION / "mission.yaml").write_text(
            f"title: fixture\nslug: {MISSION}\nstatus: in_progress\n"
            'created_at: "2026-01-01T00:00:00Z"\ncompleted_at: null\n'
            "next_task_id: 99\nmax_review_cycles: 3\n")
        (self.queue / "state.yaml").write_text(
            f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n")

    def card(self, task_id, status, extra=()):
        (self.tasks / f"{task_id}.md").write_text(_card(task_id, status, extra))

    def read(self, task_id):
        return (self.tasks / f"{task_id}.md").read_bytes()

    def run(self, *args):
        env = {k: v for k, v in self.os.environ.items()
               if not k.startswith(("CREWVIA_", "AGENT_NAME", "TASK_"))}
        env.update(CREWVIA_REPO_ROOT=str(self.root), CREWVIA_QUEUE=str(self.queue),
                   CREWVIA_TASKVIA="disabled", TASKVIA_URL="", TASKVIA_TOKEN="",
                   CREWVIA_TASK_GRAPH="0", AGENT_NAME="Ren")
        return subprocess.run(["bash", str(self.root / "scripts" / "plan.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=120)


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


ALL = sorted(lib_task_status.TASK_STATUSES) + ["cancelled"]

#: **設計の写し** (knowledge/execution.md §4.3 の表。01c E3 で狭めた後。S1 までは state-store.md §1.4 の「現状の写し」だった)。
#: 本物の ACCEPTS_FROM を読み返さず、表の言葉をそのまま書く (本物を直すと、ここが赤くなって設計との食い違いを知らせる)。
#: 値 = そのコマンドが**拒否する**元の status (`cancelled` = 語彙に無い status の代表)。
#: done は in_progress だけ・fail は in_progress と needs_director (Director が判断待ちの task を諦める出口)・
#: verify-result は検証に出ている task (ready_for_verification / verifying / needs_human_review) だけ。
REFUSED = {
    "done": set(ALL) - {"in_progress"},
    "fail": set(ALL) - {"in_progress", "needs_director"},
    "verify-result": set(ALL) - {"ready_for_verification", "verifying", "needs_human_review"},
    "needs-director": set(ALL) - {"in_progress"},
    "ready-for-verification": set(ALL) - {"in_progress"},
}

#: 各コマンドの最小の呼び出し (拒否されたら何も書かないことを見るので、受理側の副作用は問わない)。
CALL = {
    "done": ["done", "t001", "fixture", "--no-pr", "fixture", "--mission", MISSION],
    "fail": ["fail", "t001", "--no-head", "fixture", "--mission", MISSION],
    "verify-result": ["verify-result", "t001", "pass", "--mission", MISSION],
    "needs-director": ["needs-director", "t001", "fixture", "--mission", MISSION],
    "ready-for-verification": ["ready-for-verification", "t001", "--mission", MISSION],
}


@pytest.mark.parametrize("command", sorted(CALL))
@pytest.mark.parametrize("status", ALL)
def test_transition_table_is_enforced(sandbox, command, status):
    """許可遷移表のとおりに、受理 (exit 0) か拒否 (exit 2・何も書かない) になる。"""
    sandbox.card("t001", status,
                 extra=["needs_director_reason: fixture"] if status == "needs_director" else [])
    before = sandbox.read("t001")
    r = sandbox.run(*CALL[command])
    if status in REFUSED[command]:
        assert r.returncode == 2, f"{command} from {status}: rc={r.returncode} {r.stderr}"
        # 使い方の誤りも exit 2 なので、**遷移表の拒否であること**を文面で確かめる (空振りで緑にしない)
        assert "使えません" in r.stderr or "needs_director state" in r.stderr, r.stderr
        assert sandbox.read("t001") == before, "拒否したのに card が書き換わった"
    else:
        assert r.returncode == 0, f"{command} from {status}: rc={r.returncode} {r.stderr}"
        assert sandbox.read("t001") != before, "受理したのに card が変わっていない"


def test_update_status_accepts_needs_director(sandbox):
    """2026-09-28 に踏んだバグ: plan.sh 自身が書く status を update が受け付けなかった。"""
    sandbox.card("t001", "in_progress")
    r = sandbox.run("update", "t001", "--status", "needs_director", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert b"status: needs_director" in sandbox.read("t001")


def test_update_status_rejects_cancelled(sandbox):
    sandbox.card("t001", "pending")
    before = sandbox.read("t001")
    r = sandbox.run("update", "t001", "--status", "cancelled", "--mission", MISSION)
    assert r.returncode != 0
    assert sandbox.read("t001") == before


def _lint(sandbox):
    r = sandbox.run("lint", "--mission", MISSION)
    return r.returncode, r.stdout + r.stderr


def test_lint_accepts_a_needs_director_card_that_needs_director_wrote(sandbox):
    """赤の実証の 1 つ目: needs_director のカードが lint を通る (修正前は unknown status で FAIL)。"""
    sandbox.card("t001", "in_progress")
    assert sandbox.run("needs-director", "t001", "判断してください", "--mission", MISSION).returncode == 0
    _rc, out = _lint(sandbox)
    assert "unknown status" not in out, out
    assert "needs_director_reason" not in out, out


def test_lint_rejects_needs_director_without_a_reason(sandbox):
    sandbox.card("t001", "needs_director")
    _rc, out = _lint(sandbox)
    assert "requires a non-empty 'needs_director_reason'" in out, out


def test_lint_rejects_cancelled(sandbox):
    sandbox.card("t001", "cancelled")
    _rc, out = _lint(sandbox)
    assert "unknown status 'cancelled'" in out, out
