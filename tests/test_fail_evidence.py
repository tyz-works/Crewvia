"""`plan.sh fail` が証拠 (検証した head) を要求することを固定する (backlog #8 / t004)。

## 何が起きたか

`qa_checkpoints` / `required_evidence` が守っていたのは `cmd_done()` だけで、
FAIL の報告は検証ゲートの外にあった。QA Worker が **前回の handoff ファイル
(別の head 時点のもの) を 36 秒で再提出** して FAIL を報告できた。差し戻すと
古い handoff が `registry/handoffs/<agent>/<task>_HANDOFF.md` (agent 名 + task id
で決まる固定パス) に残るので、同名の Worker が同じ task をやり直すとそのまま
再提出できる。

## 設計判断 (コードの `_gate_terminal_report` のコメントと同じ内容)

FAIL は done と **別の規則** で検証する。done の検証 (QA Gate / required_evidence)
は「PASS の証拠が揃っていること」を要求する。FAIL の理由が「PASS の証拠が出せない」
であることは普通にあるので、同じ検証を流すと FAIL が報告できなくなる (= 別の outage)。
FAIL の証拠は「**何を検証した結果なのか**」— 検証対象の head (commit SHA) と、
その head に結び付いた handoff である。

このファイルが守るもの:

  1. 挙動: head なしの fail は拒否され、card は 1 バイトも変わらない
  2. 挙動: 古い handoff (別 head) の再提出は拒否される
  3. 挙動: 宣言済みゲート付き task でも、FAIL は PASS の証拠なしで報告できる
  4. 構造: done と fail が **同じ検証入口** を通る。「done は守るが fail は守らない」
     形が再発したら赤くなる
  5. 構造: `plan.sh fail` を呼ぶ自動経路 / 文書が head を渡していること
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import shutil
import subprocess

import pytest

from fixture_tree import copy_plan_tree

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLAN_SH = REPO_ROOT / "scripts" / "plan.sh"

MISSION = "m-fail"
GATE = "_gate_terminal_report"


# ---------------------------------------------------------------------------
# 構造テスト — plan.sh の python 本体を AST で調べる
# ---------------------------------------------------------------------------


def _plan_py_source() -> str:
    text = PLAN_SH.read_text()
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    assert m, "plan.sh の python ヒアドキュメント (PYEOF) が見つからない"
    return m.group(1)


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(_plan_py_source(), filename=str(PLAN_SH))


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _called_names(fn: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def _terminal_status_writers(tree: ast.Module) -> set[str]:
    """`meta['status'] = 'done' | 'failed'` を **定数で** 書く関数の名前。

    `verified` (Verifier の判定) / `skipped` は Worker の結末報告ではないので
    対象外。`update --status` は変数で書く (人間の手作業。cmd_update の docstring)。
    """
    writers: set[str] = set()
    for fn in _functions(tree).values():
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "meta"
                    and isinstance(tgt.slice, ast.Constant)
                    and tgt.slice.value == "status"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value in {"done", "failed"}
                ):
                    writers.add(fn.name)
    return writers


def test_done_and_fail_go_through_the_same_gate(tree):
    """done も fail も `_gate_terminal_report` を通る (直接 assert)。"""
    fns = _functions(tree)
    assert GATE in fns, f"{GATE}() が plan.sh に無い — 結末報告の検証入口が 1 つに無い"
    for cmd in ("cmd_done", "cmd_fail"):
        assert GATE in _called_names(fns[cmd]), (
            f"{cmd}() が {GATE}() を呼んでいない — done は守るが fail は守らない"
            "(またはその逆) という形の再発。検証は入口 1 つに集約すること"
        )


def test_every_terminal_report_writer_uses_the_gate(tree):
    """status を done / failed に定数で書く関数は、必ず入口を通ること。

    新しい「結末を書くコマンド」(例: skip / abort) を足したとき、入口を通さないと
    ここで赤くなる。通さない理由があるなら、この集合ではなく設計として説明すること。
    """
    fns = _functions(tree)
    writers = _terminal_status_writers(tree)
    assert {"cmd_done", "cmd_fail"} <= writers, f"検出が壊れている: {sorted(writers)}"
    leaks = sorted(w for w in writers if GATE not in _called_names(fns[w]))
    assert not leaks, f"検証入口を通らずに done/failed を書く関数: {leaks}"


def test_gate_treats_fail_with_its_own_rule_not_the_pass_rule(tree):
    """FAIL の入口は PASS の検証 (QA Gate / required_evidence) を流用しない。

    流用すると「required な checkpoint が failed / not_run」= FAIL の理由そのもの、
    が FAIL の報告を拒否する。done 側は従来どおり両方を通す。
    """
    fns = _functions(tree)
    gate = fns[GATE]
    kinds = {
        c.value
        for n in ast.walk(gate)
        if isinstance(n, ast.Compare)
        for c in n.comparators
        if isinstance(c, ast.Constant) and c.value in {"done", "fail"}
    }
    assert kinds == {"done", "fail"}, "入口が done / fail を区別していない"
    fail_rule = fns.get("_validate_fail_evidence")
    assert fail_rule is not None, "_validate_fail_evidence() が無い"
    assert not ({"_validate_qa_gate", "_validate_required_evidence"} & _called_names(fail_rule)), (
        "FAIL の検証が PASS の検証を呼んでいる — FAIL が報告できなくなる outage の形"
    )
    assert {"_validate_qa_gate", "_validate_required_evidence"} <= _called_names(gate), (
        "done 側の検証 (QA Gate / required_evidence) が入口から外れている"
    )


# ---------------------------------------------------------------------------
# 呼び出し元の洗い出し (受入条件 3)
# ---------------------------------------------------------------------------

#: `plan.sh fail` を呼んでよい自動経路。**現時点では空**。
#: 足すなら、その経路が head を渡す (または --no-head を外から見える形で使う)
#: ことを確かめてから。「渡せないから検証しない」に静かに倒さないこと。
AUTOMATED_FAIL_CALLERS: set[str] = set()

_FAIL_CALL = re.compile(
    r"""(?x)
    (?: plan(?:\.sh)?["']? \s+ fail\b            # plan.sh fail / plan fail
      | ["']plan(?:\.sh)?["'] \s*,\s* ["']fail["']   # ['plan.sh', 'fail']
      | \bcmd_fail\b
    )"""
)


def _automated_sources() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for base, patterns in (("scripts", ("*.sh", "*.py")), ("hooks", ("*.sh", "*.py"))):
        for pat in patterns:
            out.extend((REPO_ROOT / base).glob(pat))
    return sorted(
        p for p in out
        if p.name != "plan.sh" and not p.name.startswith("test_")
    )


#: plan.sh を参照しているファイル。呼び出しの **形** (変数経由か) には依存しない。
_MENTIONS_PLAN = re.compile(r"plan[._]sh|PLAN_SH|\bplan\b", re.IGNORECASE)
#: shell: コメントでない行に、単独の語 `fail` (`"$PLAN_SH" fail ...` / `"${PLAN[@]}" 'fail'`)
_BARE_FAIL_WORD = re.compile(r"""(?:^|[\s"'=(])fail(?=[\s"')]|$)""")


def _fail_literal_hits(src: pathlib.Path) -> list[int]:
    """plan.sh を参照するファイルの中の、サブコマンド位置にありうる `fail` の行番号。

    `_FAIL_CALL` (リテラル `plan.sh fail`) だけでは、デーモンの実際の呼び出し形
    (`["bash", str(self.plan_sh), "retire", ...]` — plan.sh は変数、サブコマンドは別要素)
    を検出できない (t005 QA の F1: 注入 M9a/M9b が緑のまま)。ここでは **変数名に依存せず**
    「plan.sh を参照するファイルに、リスト / タプルの要素・呼び出しの位置引数として単独の
    `"fail"` がある」(python) / 「コメントでない行に単独の語 `fail` がある」(shell) を拾う。

    検出できない形は knowledge/fail-evidence.md の「ガードが検出できない形」に明記してある
    (サブコマンド名を連結・変数・設定から組み立てる形。`"fa" + "il"` / `verb=fail; plan.sh $verb`)。
    """
    text = src.read_text(errors="replace")
    if not _MENTIONS_PLAN.search(text):
        return []
    hits: set[int] = set()
    if src.suffix == ".py":
        try:
            module = ast.parse(text)
        except SyntaxError:
            module = None
        if module is not None:
            for node in ast.walk(module):
                elts = (
                    node.elts if isinstance(node, (ast.List, ast.Tuple))
                    else node.args if isinstance(node, ast.Call)
                    else []
                )
                for e in elts:
                    if isinstance(e, ast.Constant) and e.value == "fail":
                        hits.add(e.lineno)
            return sorted(hits)
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if _BARE_FAIL_WORD.search(line):
            hits.add(i)
    return sorted(hits)


def _automated_fail_callers() -> set[str]:
    found = set()
    for src in _automated_sources():
        if _FAIL_CALL.search(src.read_text(errors="replace")) or _fail_literal_hits(src):
            found.add(str(src.relative_to(REPO_ROOT)))
    return found


def test_no_automated_caller_invokes_fail_without_a_decision():
    """daemon / hook / script から `plan.sh fail` を呼ぶ経路が増えたら赤くなる。

    2026-09-25 時点の洗い出し: dispatcher.sh / watchdog.py / lib_retirement.py は
    `plan.sh retire` を、kai-review.sh は `done` / `needs-director` を呼ぶ。
    `fail` を呼ぶのは **エージェント** (worker.md の graceful handoff) だけで、
    どれも head を渡せる。自動経路は 0 件。

    呼び出しの形は問わない: リテラル (`plan.sh fail`) も、変数経由
    (`["bash", str(self.plan_sh), "fail", ...]` / `"$PLAN_SH" fail ...`) も拾う。
    """
    found = _automated_fail_callers()
    assert found == AUTOMATED_FAIL_CALLERS, (
        f"`plan.sh fail` を呼ぶ自動経路が増えた/減った: {sorted(found)}。"
        " head を渡せるか、渡せないなら --no-head を外から見える形で使うかを"
        " 決めてから AUTOMATED_FAIL_CALLERS に足すこと"
    )


@pytest.mark.parametrize("name,src", [
    ("python: 変数経由の argv (lib_retirement.py の実際の形)",
     'plan_sh = self.plan_sh\nargv = ["bash", str(self.plan_sh), "fail", task_id]\n'),
    ("python: subprocess の位置引数",
     'import subprocess\nsubprocess.run(["bash", PLAN_SH, "fail", tid])\n'),
    ("shell: 変数経由", '"$PLAN_SH" fail "$TASK_ID" --head "$h"\n'),
    ("shell: 配列展開", '"${PLAN_CMD[@]}" fail "$TASK_ID"\n'),
])
def test_the_guard_detects_the_forms_the_daemons_actually_use(tmp_path, name, src):
    """ガード自身の検出力: 変数経由の呼び出し形を注入して拾えること (QA F1)。"""
    f = tmp_path / ("x.py" if name.startswith("python") else "x.sh")
    f.write_text(f"# plan.sh を呼ぶ\n{src}")
    assert _fail_literal_hits(f), f"検出できない形: {name}"


@pytest.mark.parametrize("src", [
    "# plan.sh fail は呼ばない\n\"$PLAN_SH\" retire \"$TASK\"\n",
    'x = {"status": "fail"}  # plan.sh とは無関係の verdict\n',
    "r = 'pass' if ok else 'fail'  # plan.sh\n",
])
def test_the_guard_does_not_flag_unrelated_fail_words(tmp_path, src):
    f = tmp_path / "x.py"
    f.write_text(src)
    assert not _fail_literal_hits(f)


def test_documented_fail_invocations_carry_the_head():
    """Worker が読む文書の `plan fail` 呼び出しがすべて head を渡している。"""
    text = (REPO_ROOT / "agents" / "worker.md").read_text()
    calls = [
        ln for ln in text.splitlines()
        if re.search(r"(^|[/\s])plan(\.sh)? fail [\"$]", ln)
    ]
    assert calls, "worker.md に `plan fail` の呼び出し例が無い (検出が壊れている)"
    bad = [ln for ln in calls if "--head" not in ln and "--no-head" not in ln]
    assert not bad, f"head を渡していない `plan fail` の例: {bad}"


def test_worker_docs_tell_the_worker_to_state_the_head():
    """Rule 4 (30 秒制約) の手順にも head が入っていること。"""
    text = (REPO_ROOT / "agents" / "worker.md").read_text()
    m = re.search(r"### Rule 4.*?(?=\n---\n)", text, re.DOTALL)
    assert m, "worker.md の Rule 4 が見つからない"
    assert "--head" in m.group(0), "Rule 4 の手順が head に触れていない"


# ---------------------------------------------------------------------------
# 挙動テスト — 隔離した queue / git repo で plan.sh を実走させる
# ---------------------------------------------------------------------------


def _git(cwd: pathlib.Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(
        ["git", "-C", str(cwd), *args], env=env, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _task_md(task_id: str, status: str, *, extra: str = "", worker: str = "Ren") -> str:
    return (
        "---\n"
        f"id: {task_id}\n"
        f"title: task {task_id}\n"
        "skills: [qa]\n"
        "priority: medium\n"
        f"status: {status}\n"
        "blocked_by: []\n"
        "target_dir: null\n"
        f"worker: {worker}\n"
        'started_at: "2026-01-01T00:00:00Z"\n'
        "completed_at: null\n"
        f"{extra}"
        "---\n\n"
        "## Description\n\nfixture\n\n"
        "## Result\n\n"
    )


@pytest.fixture
def sb(tmp_path):
    root = tmp_path / "repo"
    copy_plan_tree(root)

    queue = root / "queue"
    tasks = queue / "missions" / MISSION / "tasks"
    tasks.mkdir(parents=True)
    (queue / "archive").mkdir()
    (queue / "missions" / MISSION / "mission.yaml").write_text(
        f"title: fixture\nslug: {MISSION}\nstatus: in_progress\n"
        'created_at: "2026-01-01T00:00:00Z"\ncompleted_at: null\n'
        "next_task_id: 99\nmax_review_cycles: 3\n"
    )
    (queue / "state.yaml").write_text(
        f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n"
    )

    # 検証対象の repo (Worker の worktree に相当)。commit は 2 つ = head が 2 通り。
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    (work / "a.txt").write_text("1")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "one")
    old_head = _git(work, "rev-parse", "HEAD")
    (work / "a.txt").write_text("2")
    _git(work, "commit", "-q", "-am", "two")
    new_head = _git(work, "rev-parse", "HEAD")

    class SB:
        pass

    s = SB()
    s.root, s.queue, s.tasks, s.work = root, queue, tasks, work
    s.old_head, s.new_head = old_head, new_head
    s.handoffs = root / "registry" / "handoffs" / "Ren"

    def add(task_id="t001", status="in_progress", **kw):
        (tasks / f"{task_id}.md").write_text(_task_md(task_id, status, **kw))

    def card(task_id="t001") -> str:
        return (tasks / f"{task_id}.md").read_text()

    def run(*args, cwd=None):
        env = dict(os.environ)
        env.update(
            CREWVIA_REPO_ROOT=str(root), CREWVIA_QUEUE=str(queue),
            CREWVIA_TASKVIA="disabled", TASKVIA_URL="", TASKVIA_TOKEN="",
            CREWVIA_TASK_GRAPH="0",
        )
        env.pop("AGENT_NAME", None)
        env.pop("TARGET_DIR", None)
        return subprocess.run(
            ["bash", str(root / "scripts" / "plan.sh"), *args],
            env=env, cwd=str(cwd or work), capture_output=True, text=True,
        )

    def handoff(text: str) -> pathlib.Path:
        s.handoffs.mkdir(parents=True, exist_ok=True)
        p = s.handoffs / "t001_HANDOFF.md"
        p.write_text(text)
        return p

    s.add, s.card, s.run, s.handoff = add, card, run, handoff
    return s


def test_fail_without_head_is_rejected_and_leaves_the_card_untouched(sb):
    sb.add()
    before = sb.card()
    r = sb.run("fail", "t001", "--mission", MISSION)
    assert r.returncode != 0, r.stdout
    assert "--head" in r.stderr and "--no-head" in r.stderr, r.stderr
    assert sb.card() == before, "拒否したのに card が書き換わった"


def test_fail_with_a_malformed_head_is_rejected(sb):
    sb.add()
    before = sb.card()
    for bad in ("zzzzzzz", "abc12", "HEAD", ""):
        r = sb.run("fail", "t001", "--head", bad, "--mission", MISSION)
        assert r.returncode != 0, f"{bad!r} が通った: {r.stdout}"
    assert sb.card() == before


def test_fail_with_a_commit_that_does_not_exist_is_rejected(sb):
    sb.add()
    before = sb.card()
    r = sb.run("fail", "t001", "--head", "deadbeefdeadbeef", "--mission", MISSION)
    assert r.returncode != 0
    assert "deadbeefdeadbeef" in r.stderr
    assert sb.card() == before


def test_fail_with_the_head_records_the_full_sha(sb):
    sb.add()
    r = sb.run("fail", "t001", "--head", sb.new_head[:9], "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    card = sb.card()
    assert "status: failed" in card
    assert sb.new_head in card, "略称で渡しても card には完全な SHA を残す"
    assert f"head: {sb.new_head}" in card.split("## Result", 1)[1]


def test_no_head_is_an_explicit_recorded_waiver(sb):
    sb.add()
    r = sb.run("fail", "t001", "--no-head", "検証対象が git 管理外の docs", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert "検証対象が git 管理外の docs" in sb.card()
    assert "fail_head_waiver" in sb.card()
    assert "--no-head" in r.stderr, "waiver を使ったことが stderr に出ていない"


def test_no_head_needs_a_reason_and_cannot_be_combined_with_head(sb):
    sb.add()
    before = sb.card()
    assert sb.run("fail", "t001", "--no-head", "  ", "--mission", MISSION).returncode != 0
    assert sb.run(
        "fail", "t001", "--head", sb.new_head, "--no-head", "x", "--mission", MISSION
    ).returncode != 0
    assert sb.card() == before


def test_a_stale_handoff_from_another_head_is_rejected(sb):
    """事故そのもの: 前の head 時点の handoff を、今の head の FAIL として出す。"""
    sb.add()
    before = sb.card()
    stale = sb.handoff(f"# HANDOFF\n\n検証した head: {sb.old_head}\n進捗: ...\n")
    r = sb.run("fail", "t001", str(stale), "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode != 0, r.stdout
    assert "handoff" in r.stderr
    assert sb.card() == before


def test_a_handoff_that_names_the_head_is_accepted(sb):
    sb.add()
    fresh = sb.handoff(f"# HANDOFF\n\nhead: {sb.new_head[:12]}\n")
    r = sb.run("fail", "t001", str(fresh), "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert f"handoff_path: {fresh}" in sb.card()


def test_a_missing_handoff_file_is_reported_but_not_blocking(sb):
    """存在しない handoff は「古い再提出」にはなり得ない。警告して通す。"""
    sb.add()
    ghost = sb.handoffs / "t001_HANDOFF.md"
    r = sb.run("fail", "t001", str(ghost), "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert "handoff" in r.stderr


def test_declared_gates_do_not_turn_fail_into_an_outage(sb):
    """qa_checkpoints / required_evidence を宣言した task でも FAIL は報告できる。

    done と同じ検証を流用すると、QA Gate の無い (= PASS の証拠が出せない) FAIL が
    拒否される。宣言の有無にかかわらず、FAIL の証拠は head。
    """
    extra = "qa_checkpoints: [e2e]\nrequired_evidence: [pytest passed]\n"
    sb.add(extra=extra)
    # head が無ければ、宣言があっても拒否 (= 宣言済みだから緩む、ではない)
    assert sb.run("fail", "t001", "--mission", MISSION).returncode != 0
    r = sb.run("fail", "t001", "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert "status: failed" in sb.card()


def test_done_on_a_declared_task_is_still_gated(sb):
    """入口を共通にしても done の検証は弱まっていない。"""
    sb.add(extra="qa_checkpoints: [e2e]\n")
    r = sb.run("done", "t001", "all good", "--mission", MISSION)
    assert r.returncode != 0
    assert "QA Gate" in r.stderr
    assert "status: in_progress" in sb.card()


def test_reset_clears_the_previous_failure_evidence(sb):
    """誘因の側を潰す: reset したら、前回の handoff / head を持ち越さない。"""
    old = sb.handoff(f"# HANDOFF\n\nhead: {sb.old_head}\n")
    sb.add(status="failed", extra=f"handoff_path: {old}\nfail_head: {sb.old_head}\n")
    r = sb.run("update", "t001", "--reset", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    card = sb.card()
    assert "handoff_path" not in card
    assert "fail_head" not in card
    assert not old.exists(), "古い handoff が同じパスに残っている (再提出できてしまう)"
    moved = list(sb.handoffs.glob("t001_HANDOFF.md.stale-*"))
    assert len(moved) == 1, f"退避されていない: {list(sb.handoffs.iterdir())}"
    assert sb.old_head in moved[0].read_text(), "退避であって削除ではない"


def test_reset_never_moves_a_file_outside_the_handoff_directory(sb, tmp_path):
    """card の handoff_path は Worker が書く値。任意のパスを動かさない。"""
    outside = tmp_path / "precious.txt"
    outside.write_text("keep me")
    sb.add(status="failed", extra=f"handoff_path: {outside}\n")
    r = sb.run("update", "t001", "--reset", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert outside.read_text() == "keep me"
    assert "handoff_path" not in sb.card()


# ---------------------------------------------------------------------------
# Kai P2 (PR #216): handoff パスの基準 / 退避先の衝突
# ---------------------------------------------------------------------------


def test_a_relative_handoff_path_is_rejected(sb):
    """Worker の cwd (worktree) 基準の検証と dispatcher (main repo 基準) の読み取りが
    食い違う。stale-handoff の確認を迂回できてしまうので、相対パスは受け付けない。"""
    sb.add()
    before = sb.card()
    # cwd (work) に、head に触れている「別の」handoff を置く。cwd 基準で検証すると通ってしまう。
    (sb.work / "registry" / "handoffs" / "Ren").mkdir(parents=True)
    decoy = sb.work / "registry" / "handoffs" / "Ren" / "t001_HANDOFF.md"
    decoy.write_text(f"head: {sb.new_head}\n")
    rel = "registry/handoffs/Ren/t001_HANDOFF.md"
    for extra in (["--head", sb.new_head], ["--no-head", "git 管理外"]):
        r = sb.run("fail", "t001", rel, *extra, "--mission", MISSION)
        assert r.returncode != 0, r.stdout
        assert "絶対パス" in r.stderr
        assert sb.card() == before


def test_an_absolute_handoff_path_is_persisted_as_validated(sb):
    sb.add()
    fresh = sb.handoff(f"head: {sb.new_head}\n")
    dotted = str(fresh.parent / ".." / "Ren" / fresh.name)   # 正規化されるべき形
    r = sb.run("fail", "t001", dotted, "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert f"handoff_path: {fresh}\n" in sb.card()


def test_reset_resolves_a_relative_handoff_path_like_the_dispatcher(sb):
    """reset は card の値を動かす。相対値は dispatcher と同じ基準 (repo root) で解く。
    cwd 基準だと、cwd 側の無関係なファイルを退避し、本物の古い handoff が残る。"""
    real = sb.handoff(f"head: {sb.old_head}\n")
    decoy_dir = sb.work / "registry" / "handoffs" / "Ren"
    decoy_dir.mkdir(parents=True)
    decoy = decoy_dir / "t001_HANDOFF.md"
    decoy.write_text("decoy in cwd")
    sb.add(status="failed", extra="handoff_path: registry/handoffs/Ren/t001_HANDOFF.md\n")
    r = sb.run("update", "t001", "--reset", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert not real.exists(), "repo root 側の本物の古い handoff が残っている"
    assert decoy.read_text() == "decoy in cwd", "cwd 側の無関係なファイルを動かした"
    assert len(list(sb.handoffs.glob("t001_HANDOFF.md.stale-*"))) == 1


def test_set_aside_never_overwrites_an_earlier_report(sb):
    """同じ秒内の 2 回目の退避が 1 回目の証拠を消さない (os.rename は宛先を置換する)。

    時計を止められないので、いま/次の秒の退避先名を先に埋めておく: 実行の秒は必ずどちらか。
    """
    from datetime import datetime, timedelta, timezone
    sb.handoffs.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    earlier = {}
    for k in range(3):
        stamp = (now + timedelta(seconds=k)).strftime("%Y%m%dT%H%M%SZ")
        f = sb.handoffs / f"t001_HANDOFF.md.stale-{stamp}"
        f.write_text(f"earlier evidence {k}")
        earlier[f] = f"earlier evidence {k}"
    cur = sb.handoff(f"head: {sb.old_head}\n")
    sb.add(status="failed", extra=f"handoff_path: {cur}\n")
    r = sb.run("update", "t001", "--reset", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    for f, text in earlier.items():
        assert f.read_text() == text, f"先の退避が上書きされた: {f.name}"
    assert not cur.exists()
    stale = list(sb.handoffs.glob("t001_HANDOFF.md.stale-*"))
    assert len(stale) == 4, [p.name for p in stale]
    assert any(sb.old_head in p.read_text() for p in stale), "今回の handoff が退避されていない"


# ---------------------------------------------------------------------------
# Kai P2 2 巡目 (PR #216 / t028): 開き直しで引き継がれた handoff_path を断つ
#
# t004 は `update --reset` だけ handoff を掃除した。`update --status in_progress` で
# 開き直すと card に古い handoff_path が残り、次の `fail --head <新 SHA>` (handoff なし) が
# それに触れないまま failed にする → dispatcher が古いファイルを読んで Director に通知する。
# Result には `handoff: none` と書かれているのに。#8 の元の事故 (別 head 時点の handoff の
# 再提出) が別の入口から開いたままだった。
# ---------------------------------------------------------------------------

# `plan.sh update --status` が受け付ける failed 以外の status の全部 (cmd_update の
# valid_statuses から failed を除いたもの)。どれで開き直し / 動かしても証拠は残らない。
_NON_FAILED_STATUSES = sorted({
    "pending", "in_progress", "done", "blocked", "skipped", "verified",
    "ready_for_verification", "verifying", "verification_failed", "needs_human_review",
})

# 開き直す経路。(名前, update に渡す引数)。`--reset` だけを見て終わりにしない。
_REOPEN_ROUTES = [
    ("update --status in_progress", ("--status", "in_progress")),
    ("update --status pending", ("--status", "pending")),
    ("update --reset", ("--reset",)),
    ("update --reset --status in_progress", ("--reset", "--status", "in_progress")),
]


def _dispatcher_notifies_with(sb, task_id="t001"):
    """dispatcher.sh の「Handoff detection」が、この card について読むことになる handoff_path。

    dispatcher は `status == 'failed'` かつ `meta.get('handoff_path')` の card を、その
    ファイルを読んで Director に通知する。読み取りは dispatcher と同じ
    `lib_task_cards.list_task_cards` を通す (card を読む側の視点)。通知しないなら None。
    """
    import sys
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import lib_task_cards
    for meta, _body in lib_task_cards.list_task_cards(sb.tasks):
        if meta.get("id") == task_id and meta.get("status") == "failed":
            return meta.get("handoff_path") or None
    return None


def test_the_dispatcher_view_helper_mirrors_the_real_notify_condition():
    """`_dispatcher_notifies_with` は dispatcher.sh の条件の写し。写しがずれたら落とす。"""
    src = (REPO_ROOT / "scripts" / "dispatcher.sh").read_text()
    m = re.search(r"# Handoff detection:.*?handoff_path = meta\.get\('handoff_path'\)", src, re.DOTALL)
    assert m, "dispatcher.sh の handoff 通知の条件が変わった。_dispatcher_notifies_with を見直すこと"
    assert "meta.get('status') != 'failed'" in m.group(0)


def _result_section(card: str) -> str:
    return card.split("## Result", 1)[1]


def _assert_result_matches_card(card: str, expect_handoff):
    """Result の `handoff: <x>` と card の handoff_path が食い違わないこと (受入条件 2)。"""
    result = _result_section(card)
    m = re.search(r"handoff: (\S+)", result)
    assert m, f"Result に handoff の記述が無い: {result!r}"
    if expect_handoff is None:
        assert m.group(1) == "none", result
        assert "handoff_path" not in card, "Result は handoff: none なのに card に handoff_path が残っている"
    else:
        assert m.group(1) == str(expect_handoff), result
        assert f"handoff_path: {expect_handoff}\n" in card


@pytest.mark.parametrize("route", _REOPEN_ROUTES, ids=[r[0] for r in _REOPEN_ROUTES])
def test_a_reopened_card_cannot_carry_the_old_handoff_into_the_next_fail(sb, route):
    """再現手順そのもの: FAIL(古い head の handoff 付き) → 開き直す → handoff なしで fail。

    どの入口から開き直しても、dispatcher が読む handoff_path は空でなければならない。
    """
    _name, update_args = route
    old = sb.handoff(f"# HANDOFF\n\nhead: {sb.old_head}\n")
    sb.add(status="failed", extra=f"handoff_path: {old}\nfail_head: {sb.old_head}\n")
    assert _dispatcher_notifies_with(sb) == str(old), "前提: 開き直す前は古い handoff が見える"

    r = sb.run("update", "t001", *update_args, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    reopened = sb.card()
    assert "handoff_path" not in reopened, "開き直した card に古い handoff_path が残っている"
    assert "fail_head" not in reopened

    # 開き直した card が in_progress でなければ (reset → pending)、Worker が拾い直した状態にする
    (sb.tasks / "t001.md").write_text(reopened.replace("status: pending", "status: in_progress"))
    r = sb.run("fail", "t001", "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    card = sb.card()
    _assert_result_matches_card(card, None)
    assert _dispatcher_notifies_with(sb) is None, "dispatcher が古い handoff を読んで通知する"


def test_fail_without_a_handoff_clears_one_the_card_inherited(sb):
    """sink 側の単独確認: 開き直しの経路がどうであれ (旧版が書いた card / 手書き)、
    card に古い handoff_path が載ったまま in_progress になっていても、handoff なしの
    fail は failed の card にそれを載せない。update 側の掃除に頼らない。"""
    old = sb.handoff(f"head: {sb.old_head}\n")
    sb.add(status="in_progress", extra=f"handoff_path: {old}\nfail_head: {sb.old_head}\n")
    assert _dispatcher_notifies_with(sb) is None    # failed ではないので読まれない (前提)
    r = sb.run("fail", "t001", "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    card = sb.card()
    _assert_result_matches_card(card, None)
    assert f"fail_head: {sb.new_head}\n" in card
    assert _dispatcher_notifies_with(sb) is None


def test_no_head_fail_without_a_handoff_also_clears_the_inherited_one(sb):
    old = sb.handoff(f"head: {sb.old_head}\n")
    sb.add(status="in_progress", extra=f"handoff_path: {old}\n")
    r = sb.run("fail", "t001", "--no-head", "git 管理外", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    _assert_result_matches_card(sb.card(), None)
    assert _dispatcher_notifies_with(sb) is None


def test_a_handoff_supplied_to_fail_replaces_the_inherited_one_and_is_validated(sb):
    """渡された handoff は従来どおり検証され、card には渡された方だけが載る。"""
    old = sb.handoff(f"head: {sb.old_head}\n")
    sb.add(status="in_progress", extra=f"handoff_path: {old}\n")
    # 古い handoff を渡せば (別 head なので) 弾かれる — 継承を断っても検証は緩まない
    r = sb.run("fail", "t001", str(old), "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode != 0
    assert "触れていません" in r.stderr
    fresh = sb.handoff(f"head: {sb.new_head}\n")
    r = sb.run("fail", "t001", str(fresh), "--head", sb.new_head, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    card = sb.card()
    _assert_result_matches_card(card, fresh)
    assert _dispatcher_notifies_with(sb) == str(fresh)


def test_clearing_the_handoff_never_turns_a_legitimate_fail_into_an_outage(sb):
    """原則: FAIL が報告できなくなる別の outage を作らない (受入条件 9)。

    handoff を消す規則が、正当な FAIL を拒否する経路になっていないこと:
    (a) 古い handoff が card に残っていても、head だけの FAIL は通る
    (b) 開き直したあとの fresh な handoff 付き FAIL は通る
    (c) 宣言済みゲート付き task でも同じ
    (d) 継承された handoff のファイルが消えていても / 読めなくても、handoff を渡さない FAIL は通る
    """
    old = sb.handoff(f"head: {sb.old_head}\n")
    gated = ("qa_checkpoints:\n  - {id: c1, required: true}\n"
             "required_evidence: [\"screenshot\"]\n")
    sb.add("t001", status="in_progress", extra=f"handoff_path: {old}\n{gated}")
    old.unlink()                                                    # (d) ファイルが無い
    r = sb.run("fail", "t001", "--head", sb.new_head, "--mission", MISSION)   # (a)(c)(d)
    assert r.returncode == 0, r.stderr
    assert "handoff_path" not in sb.card()

    sb.add("t002", status="failed", extra=f"handoff_path: {sb.handoffs / 't002_HANDOFF.md'}\n")
    r = sb.run("update", "t002", "--status", "in_progress", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    sb.handoffs.mkdir(parents=True, exist_ok=True)
    fresh = sb.handoffs / "t002_HANDOFF.md"
    fresh.write_text(f"head: {sb.new_head}\n")
    r = sb.run("fail", "t002", str(fresh), "--head", sb.new_head, "--mission", MISSION)   # (b)
    assert r.returncode == 0, r.stderr
    assert f"handoff_path: {fresh}\n" in (sb.tasks / "t002.md").read_text()


@pytest.mark.parametrize("status", _NON_FAILED_STATUSES)
def test_every_status_change_away_from_failed_clears_the_evidence(sb, status):
    """`update --status <X>` は failed から動かせる全部の status で証拠を持ち越さない。"""
    old = sb.handoff(f"head: {sb.old_head}\n")
    sb.add(status="failed", extra=(f"handoff_path: {old}\nfail_head: {sb.old_head}\n"
                                   'fail_head_waiver: "x"\n'))
    r = sb.run("update", "t001", "--status", status, "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    card = sb.card()
    for key in ("handoff_path", "fail_head", "fail_head_waiver"):
        assert key not in card, f"{status}: {key} が残っている"
    assert not old.exists() and len(list(sb.handoffs.glob("t001_HANDOFF.md.stale-*"))) == 1, \
        "古い handoff が同じ固定パスに残っている (退避されていない)"


def test_marking_a_non_failed_card_failed_by_hand_does_not_adopt_stale_evidence(sb):
    """`update --status failed` は FAIL の報告 (gate) を経ない。in_progress の card に載っている
    handoff は今回の FAIL のものではないので、failed にした途端に dispatcher へ出てはいけない。"""
    old = sb.handoff(f"head: {sb.old_head}\n")
    sb.add(status="in_progress", extra=f"handoff_path: {old}\nfail_head: {sb.old_head}\n")
    r = sb.run("update", "t001", "--status", "failed", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert "handoff_path" not in sb.card()
    assert _dispatcher_notifies_with(sb) is None


def test_evidence_survives_updates_that_do_not_end_the_failure(sb):
    """掃除は「FAIL が終わる更新」だけ。同じ FAIL の再記録 / status に触れない更新では、
    検証済みの証拠を消さない (消すと Director が見る handoff が消える)。"""
    fresh = sb.handoff(f"head: {sb.new_head}\n")
    sb.add(status="in_progress")
    assert sb.run("fail", "t001", str(fresh), "--head", sb.new_head, "--mission", MISSION).returncode == 0
    for args in (("--priority", "high"), ("--status", "failed"), ("--pr-number", "216")):
        r = sb.run("update", "t001", *args, "--mission", MISSION)
        assert r.returncode == 0, r.stderr
        card = sb.card()
        assert f"handoff_path: {fresh}\n" in card, f"{args}: 検証済みの handoff が消えた"
        assert f"fail_head: {sb.new_head}\n" in card
        assert fresh.exists()
    assert _dispatcher_notifies_with(sb) == str(fresh)


def test_the_fail_gate_is_where_the_handoff_path_of_a_failed_card_is_decided(tree):
    """構造: `handoff_path` を card に書く欄は gate の戻り値 (fields) に 1 本化されている。
    cmd_fail が `meta['handoff_path'] = ...` を自前で足す形 (= 渡されないとき card に触れない形)
    に戻ったら赤くなる。"""
    fns = _functions(tree)
    for node in ast.walk(fns["cmd_fail"]):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "meta" and isinstance(node.ctx, ast.Store)
                and isinstance(node.slice, ast.Constant) and node.slice.value == "handoff_path"):
            pytest.fail("cmd_fail が meta['handoff_path'] を直接書いている — "
                        "渡されないときに古い値が残る形に戻っている")
