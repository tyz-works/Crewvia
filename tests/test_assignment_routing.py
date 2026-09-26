#!/usr/bin/env python3
"""割り当てを機械で照合する (t009 / backlog #21 #22 #24)。

2026-09-25 のミッション (memory `cross-repo-mission-worker-routing`) で、Director が手でやった作業と
Worker の自己確認に頼った箇所を、仕組みにした。ここは各項目の **拒否と許可の両側** を固定する:

1. `lib_worker_target` — 記録 (`registry/workers/<Name>/target_dir.json`) の書き・読み・判定表・掃除
2. `plan.sh pull --task` — target_dir の不一致 / 別の task を持つ Worker を exit 3 で拒否 (何も書かない)
3. `plan.sh done --pr <N>` — codex-review / review の task への PR 番号の伝播
4. lint — `blocked` (blocked_reason 付き) を drafting でも受理
5. 本物の `dispatch()` を 1 サイクル回して、TARGET_DIR 不一致・assignment 保持中・送信済み pull 待ちの
   Worker には回さない (実コード harness — ロジックを複製したテストは、dispatcher を直したことを証明しない)

    python3 -m pytest tests/test_assignment_routing.py -v
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
SCRIPTS_DIR = REPO / "scripts"
PLAN_SH = SCRIPTS_DIR / "plan.sh"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lib_worker_target as wt  # noqa: E402
from fixture_tree import copy_plan_tree  # noqa: E402
from lib_task_cards import Unreadable, is_missing, is_unreadable  # noqa: E402

MISSION = "m-route"
OTHER_REPO = "/srv/other-repo"


# ---------------------------------------------------------------------------
# 1. lib_worker_target
# ---------------------------------------------------------------------------

class TestRecord:
    def test_write_then_load_roundtrip(self, tmp_path):
        wt.write_record(tmp_path, "Ren", OTHER_REPO, now=1000.0)
        rec = wt.load_record(tmp_path, "Ren")
        assert rec == {"agent": "Ren", "target_dir": OTHER_REPO, "written_at": 1000.0}

    def test_null_target_is_a_fact_not_a_missing_record(self, tmp_path):
        wt.write_record(tmp_path, "Ren", None)
        rec = wt.load_record(tmp_path, "Ren")
        assert not is_unreadable(rec) and rec["target_dir"] is None
        # 空文字も「crewvia 本体で起動した」と同じ
        wt.write_record(tmp_path, "Ren", "")
        assert wt.load_record(tmp_path, "Ren")["target_dir"] is None

    def test_missing_is_distinguished_from_unreadable(self, tmp_path):
        assert is_missing(wt.load_record(tmp_path, "Nobody"))
        p = wt.record_path(tmp_path, "Ren")
        p.parent.mkdir(parents=True)
        p.write_text("{not json")
        rec = wt.load_record(tmp_path, "Ren")
        assert is_unreadable(rec) and not is_missing(rec)

    @pytest.mark.parametrize("body", [
        '[]', '"x"', '{"agent": "Ren", "target_dir": 5, "written_at": 1}',
        '{"agent": "Ren", "target_dir": "relative/path", "written_at": 1}',
        '{"agent": "Ren", "target_dir": null, "written_at": "now"}',
        '{"agent": "Ren", "target_dir": null, "written_at": NaN}',
        '{"agent": "", "target_dir": null, "written_at": 1}',
        '{"target_dir": null, "written_at": 1}',
    ])
    def test_wrongly_shaped_record_is_unreadable_not_an_exception(self, tmp_path, body):
        p = wt.record_path(tmp_path, "Ren")
        p.parent.mkdir(parents=True)
        p.write_text(body)
        assert is_unreadable(wt.load_record(tmp_path, "Ren"))

    @pytest.mark.parametrize("name", ["", "a/b", "../x", ".hidden", "..", "a\0b"])
    def test_agent_name_cannot_escape_the_directory(self, tmp_path, name):
        with pytest.raises(ValueError):
            wt.record_path(tmp_path, name)
        assert is_unreadable(wt.load_record(tmp_path, name))

    def test_record_lives_beside_not_inside_the_spawn_record(self, tmp_path):
        """kill の認可の証拠 (registry/mux/<name>.json) には相乗りしない。"""
        p = wt.record_path(tmp_path, "Ren")
        assert p == tmp_path / "workers" / "Ren" / "target_dir.json"
        assert "mux" not in p.parts

    def test_write_leaves_no_temp_file_and_replaces_atomically(self, tmp_path):
        wt.write_record(tmp_path, "Ren", OTHER_REPO)
        wt.write_record(tmp_path, "Ren", None)     # 同名の後任が別の TARGET_DIR で起動
        names = sorted(p.name for p in (tmp_path / "workers" / "Ren").iterdir())
        assert names == ["target_dir.json"]
        assert wt.load_record(tmp_path, "Ren")["target_dir"] is None

    def test_cli_record_and_show(self, tmp_path):
        r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "lib_worker_target.py"),
                            "record", str(tmp_path), "Ren", OTHER_REPO],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "lib_worker_target.py"),
                            "show", str(tmp_path), "Ren"], capture_output=True, text=True)
        assert r.returncode == 0 and json.loads(r.stdout)["target_dir"] == OTHER_REPO
        r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "lib_worker_target.py"),
                            "show", str(tmp_path), "Ghost"], capture_output=True, text=True)
        assert r.returncode == 2 and r.stdout.strip() == "missing"

    def test_cli_record_without_target_records_null(self, tmp_path):
        r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "lib_worker_target.py"),
                            "record", str(tmp_path), "Ren", ""], capture_output=True, text=True)
        assert r.returncode == 0 and wt.load_record(tmp_path, "Ren")["target_dir"] is None


def _rec(target):
    return {"agent": "Ren", "target_dir": target, "written_at": 1.0}


MISSING = Unreadable("x", "no such file", errno=2)
BROKEN = Unreadable("x", "malformed JSON")

# (名前, Worker の記録, task の target_dir, 期待)
DECISION_TABLE = [
    ("null task / no record",          MISSING,           None,        True),
    ("null task / unreadable record",  BROKEN,            None,        True),
    ("null task / null record",        _rec(None),        None,        True),
    ("null task / other-repo worker",  _rec(OTHER_REPO),  None,        False),
    ("target task / matching record",  _rec(OTHER_REPO),  OTHER_REPO,  True),
    ("target task / other target",     _rec("/srv/x"),    OTHER_REPO,  False),
    ("target task / null record",      _rec(None),        OTHER_REPO,  False),
    ("target task / no record",        MISSING,           OTHER_REPO,  False),
    ("target task / unreadable record", BROKEN,           OTHER_REPO,  False),
]


@pytest.mark.parametrize("name,record,task_target,expected", DECISION_TABLE,
                         ids=[r[0] for r in DECISION_TABLE])
def test_worker_may_take_table(name, record, task_target, expected):
    ok, why = wt.worker_may_take(record, task_target)
    assert ok is expected, (name, why)
    assert bool(why) == (not expected), "拒否には理由が付き、許可には付かない"


class TestSweep:
    def _age(self, tmp_path, agent, seconds):
        p = wt.record_path(tmp_path, agent)
        old = time.time() - seconds
        os.utime(p, (old, old))

    def test_stale_record_of_a_retired_worker_is_swept(self, tmp_path):
        wt.write_record(tmp_path, "Gone", OTHER_REPO)
        wt.write_record(tmp_path, "Alive", OTHER_REPO)
        self._age(tmp_path, "Gone", wt.SWEEP_MIN_AGE_SECONDS + 5)
        self._age(tmp_path, "Alive", wt.SWEEP_MIN_AGE_SECONDS + 5)
        assert wt.sweep_stale_records(tmp_path, {"Alive"}) == ["Gone"]
        assert is_missing(wt.load_record(tmp_path, "Gone"))
        assert not is_unreadable(wt.load_record(tmp_path, "Alive"))

    def test_a_successors_fresh_record_survives_a_window_that_is_not_visible_yet(self, tmp_path):
        wt.write_record(tmp_path, "Ren", OTHER_REPO)          # 起動したばかり
        assert wt.sweep_stale_records(tmp_path, {"Someone"}) == []
        assert not is_unreadable(wt.load_record(tmp_path, "Ren"))

    def test_an_empty_window_list_sweeps_nothing(self, tmp_path):
        """mux が一時的に空を返しただけで全記録を消さない (生きた Worker の記録まで消える)。"""
        wt.write_record(tmp_path, "Ren", OTHER_REPO)
        self._age(tmp_path, "Ren", 10 * wt.SWEEP_MIN_AGE_SECONDS)
        assert wt.sweep_stale_records(tmp_path, set()) == []
        assert not is_unreadable(wt.load_record(tmp_path, "Ren"))

    def test_a_broken_stale_record_is_swept_too(self, tmp_path):
        p = wt.record_path(tmp_path, "Gone")
        p.parent.mkdir(parents=True)
        p.write_text("{broken")
        self._age(tmp_path, "Gone", 10 * wt.SWEEP_MIN_AGE_SECONDS)
        assert wt.sweep_stale_records(tmp_path, {"Alive"}) == ["Gone"]

    def test_no_registry_dir_is_not_an_error(self, tmp_path):
        assert wt.sweep_stale_records(tmp_path / "nope", {"Alive"}) == []


# ---------------------------------------------------------------------------
# plan.sh の隔離実行環境
# ---------------------------------------------------------------------------

def _card(task_id, *, status="pending", skills="[code]", blocked_by="[]", target="null",
          worker="null", extra=()):
    lines = ["---", f"id: {task_id}", f"title: task {task_id}", f"skills: {skills}",
             "priority: medium", f"status: {status}", f"blocked_by: {blocked_by}",
             f"target_dir: {target}", f"worker: {worker}", "started_at: null",
             "completed_at: null", *extra, "---", "", "## Description", "", "fixture", "",
             "## Result", ""]
    return "\n".join(lines)


class Sandbox:
    """CREWVIA_REPO_ROOT / CREWVIA_QUEUE を隔離した plan.sh の実行環境 (本番の queue / registry に触れない)。"""

    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / "registry" / "mux").mkdir(parents=True)
        copy_plan_tree(self.root)
        self.queue = self.root / "queue"
        self.tasks = self.queue / "missions" / MISSION / "tasks"
        self.tasks.mkdir(parents=True)
        (self.queue / "archive").mkdir()
        (self.queue / "assignments").mkdir()
        (self.queue / "missions" / MISSION / "mission.yaml").write_text(
            f"title: fixture\nslug: {MISSION}\nstatus: in_progress\n"
            'created_at: "2026-01-01T00:00:00Z"\ncompleted_at: null\n'
            "next_task_id: 99\nmax_review_cycles: 3\n")
        (self.queue / "state.yaml").write_text(
            f"active_missions:\n  - {MISSION}\ndefault_mission: {MISSION}\n")

    def card(self, task_id, **kw):
        (self.tasks / f"{task_id}.md").write_text(_card(task_id, **kw))

    def text(self, task_id):
        return (self.tasks / f"{task_id}.md").read_text()

    def assignment(self, agent):
        p = self.queue / "assignments" / agent
        return p.read_text().strip() if p.exists() else None

    def snapshot(self):
        """queue の全ファイルの中身 (「何も書かなかった」の比較用)。`.lock` は排他ロックの入れ物で、
        中身は常に空 (pull は取ってから断るので、拒否でも作られる)。"""
        out = {}
        for p in sorted(self.queue.rglob("*")):
            if p.is_file() and p.name != ".lock":
                out[str(p.relative_to(self.queue))] = p.read_bytes()
        return out

    def run(self, *args, env_extra=None, drop=()):
        env = dict(os.environ)
        for k in ("TARGET_DIR", "AGENT_NAME", "CREWVIA_MISSION_SLUG", *drop):
            env.pop(k, None)
        env.update(CREWVIA_REPO_ROOT=str(self.root), CREWVIA_QUEUE=str(self.queue),
                   CREWVIA_TASKVIA="disabled", TASKVIA_URL="", TASKVIA_TOKEN="",
                   CREWVIA_TASK_GRAPH="0")
        env.update(env_extra or {})
        return subprocess.run(["bash", str(self.root / "scripts" / "plan.sh"), *args],
                              env=env, capture_output=True, text=True)


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


def _pull(sb, task, agent="Ren", target=None, env_target=None, extra=()):
    args = ["pull", "--task", task, "--mission", MISSION, "--agent", agent, "--skills", "code", *extra]
    if target:
        args += ["--target-dir", target]
    return sb.run(*args, env_extra={"TARGET_DIR": env_target} if env_target else None)


# ---------------------------------------------------------------------------
# 2. plan.sh pull --task — target_dir の照合
# ---------------------------------------------------------------------------

class TestPullTargetDir:
    def test_worker_without_target_dir_refuses_a_target_task(self, sb):
        sb.card("t001", target=OTHER_REPO)
        before = sb.snapshot()
        r = _pull(sb, "t001")
        assert r.returncode == 3, (r.stdout, r.stderr)
        assert "target_dir" in r.stderr and OTHER_REPO in r.stderr
        assert sb.snapshot() == before, "拒否は 1 バイトも書かない"

    def test_worker_with_target_dir_refuses_a_crewvia_local_task(self, sb, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        sb.card("t001", target="null")
        before = sb.snapshot()
        r = _pull(sb, "t001", env_target=str(other))
        assert r.returncode == 3, (r.stdout, r.stderr)
        assert sb.snapshot() == before

    def test_worker_with_a_different_target_dir_is_refused(self, sb, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        sb.card("t001", target=str(a))
        before = sb.snapshot()
        r = _pull(sb, "t001", env_target=str(b))
        assert r.returncode == 3
        assert sb.snapshot() == before

    def test_matching_target_dir_from_env_is_accepted(self, sb, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        sb.card("t001", target=str(a))
        r = _pull(sb, "t001", env_target=str(a))
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "status: in_progress" in sb.text("t001")
        assert sb.assignment("Ren") == f"{MISSION}:t001"

    def test_matching_target_dir_from_the_flag_is_accepted(self, sb, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        sb.card("t001", target=str(a))
        assert _pull(sb, "t001", target=str(a)).returncode == 0

    def test_crewvia_local_task_and_worker_still_work(self, sb):
        """前提の担保: 照合が全部を拒否しているわけではない。"""
        sb.card("t001")
        r = _pull(sb, "t001")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert sb.assignment("Ren") == f"{MISSION}:t001"

    def test_refusal_is_exit_3_not_the_no_task_exit_2(self, sb):
        """pull の exit 2 は「タスクなし」(Worker が待つ)。照合の拒否はそれと区別できること。"""
        sb.card("t001", target=OTHER_REPO)
        assert _pull(sb, "t001").returncode not in (0, 1, 2)

    def test_skill_check_is_still_bypassed_by_task(self, sb):
        """--task は skill の絞り込みを迂回する (dispatcher が済ませている) — 従来どおり (警告のみ)。"""
        sb.card("t001", skills="[bash]")
        r = _pull(sb, "t001")
        assert r.returncode == 0 and "WARNING" in r.stderr


# ---------------------------------------------------------------------------
# 2'. plan.sh pull --task — 二重割り当て
# ---------------------------------------------------------------------------

class TestPullDoubleAssignment:
    def test_worker_with_an_in_progress_card_refuses_another_task(self, sb):
        sb.card("t001", status="in_progress", worker="Ren")
        sb.card("t002")
        (sb.queue / "assignments" / "Ren").write_text(f"{MISSION}:t001\n")
        before = sb.snapshot()
        r = _pull(sb, "t002")
        assert r.returncode == 3, (r.stdout, r.stderr)
        assert "t001" in r.stderr
        assert sb.snapshot() == before
        assert sb.assignment("Ren") == f"{MISSION}:t001", "先の assignment は上書きされない"

    def test_in_progress_card_alone_is_enough_to_refuse(self, sb):
        """assignment が無くても (壊れた・消えた) card が持っている証拠になる。"""
        sb.card("t001", status="in_progress", worker="Ren")
        sb.card("t002")
        assert _pull(sb, "t002").returncode == 3

    def test_assignment_pointing_at_a_running_task_alone_is_enough_to_refuse(self, sb):
        sb.card("t001", status="in_progress", worker="Someone-else")
        sb.card("t002")
        (sb.queue / "assignments" / "Ren").write_text(f"{MISSION}:t001\n")
        assert _pull(sb, "t002").returncode == 3

    def test_another_workers_in_progress_card_does_not_block_me(self, sb):
        sb.card("t001", status="in_progress", worker="Erik")
        sb.card("t002")
        assert _pull(sb, "t002").returncode == 0

    @pytest.mark.parametrize("status", ["done", "failed", "cancelled", "skipped", "verified", "pending"])
    def test_orphan_assignment_pointing_at_a_released_task_is_overwritten(self, sb, status):
        """手放し済みの task を指す assignment は孤児 (#13 と同じ扱い)。塞ぐと codex-review が恒久に止まる。"""
        sb.card("t001", status=status, worker="Ren")
        sb.card("t002")
        (sb.queue / "assignments" / "Ren").write_text(f"{MISSION}:t001\n")
        r = _pull(sb, "t002")
        assert r.returncode == 0, (status, r.stdout, r.stderr)
        assert sb.assignment("Ren") == f"{MISSION}:t002"

    def test_assignment_pointing_at_a_missing_mission_is_an_orphan(self, sb):
        sb.card("t002")
        (sb.queue / "assignments" / "Ren").write_text("archived-mission:t009\n")
        assert _pull(sb, "t002").returncode == 0

    def test_needs_director_card_alone_does_not_block_a_reviewer(self, sb):
        """Kai-codex は needs-director の後も名前が card に残る。それで塞ぐと #13 が戻る。"""
        sb.card("t001", status="needs_director", worker="Ren")
        sb.card("t002")
        r = _pull(sb, "t002")
        assert r.returncode == 0, (r.stdout, r.stderr)

    def test_unreadable_assignment_is_refused_not_overwritten(self, sb):
        """別の task を指していないと証明できない — 上書きは防ぎたい事故そのもの。"""
        sb.card("t002")
        (sb.queue / "assignments" / "Ren").mkdir()          # 通常ファイルではない
        before = sb.snapshot()
        r = _pull(sb, "t002")
        assert r.returncode == 3, (r.stdout, r.stderr)
        assert sb.snapshot() == before

    def test_same_task_twice_is_not_this_refusal(self, sb):
        """割り当てメッセージの二重着弾: 同じ task は従来の判定 (already in_progress, exit 1) に委ねる。"""
        sb.card("t001")
        assert _pull(sb, "t001").returncode == 0
        again = _pull(sb, "t001")
        assert again.returncode == 1, (again.stdout, again.stderr)
        assert "already in_progress" in again.stderr

    def test_second_message_for_a_different_task_leaves_the_first_intact(self, sb):
        """#22 の再現: A を pull した後に届いた B の指示で、A の assignment が上書きされる。"""
        sb.card("t001")
        sb.card("t002")
        assert _pull(sb, "t001").returncode == 0
        assert _pull(sb, "t002").returncode == 3
        assert sb.assignment("Ren") == f"{MISSION}:t001"
        assert "status: pending" in sb.text("t002")

    def test_a_worker_without_a_name_is_not_subject_to_the_check(self, sb):
        """--agent も AGENT_NAME も無い pull (手動) は、持っている task を確かめる相手がいない。"""
        sb.card("t001")
        r = sb.run("pull", "--task", "t001", "--mission", MISSION, "--skills", "code")
        assert r.returncode == 0, (r.stdout, r.stderr)


# ---------------------------------------------------------------------------
# 3. plan.sh done --pr — PR 番号の自動伝播
# ---------------------------------------------------------------------------

def _done(sb, task, *extra):
    return sb.run("done", task, "finished", "--mission", MISSION, *extra)


def _pr_line(sb, task):
    for line in sb.text(task).splitlines():
        if line.startswith("pr_number:"):
            return line
    return None


def _status(sb, task):
    for line in sb.text(task).splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()


class TestDonePr:
    def _mission(self, sb):
        sb.card("t001", status="in_progress", worker="Ren")
        sb.card("t002", skills="[qa]", blocked_by="[t001]")
        sb.card("t003", skills="[codex-review]", blocked_by="[t001]", status="blocked",
                extra=['blocked_reason: "PR 番号待ち"'])
        sb.card("t004", skills="[review]", blocked_by="[t003]")
        sb.card("t005", skills="[review]", blocked_by="[t001]")
        sb.card("t006", skills="[codex-review]", blocked_by="[t002]", status="blocked",
                extra=['blocked_reason: "別件"'])

    def test_pr_number_reaches_codex_review_and_unblocks_it(self, sb):
        self._mission(sb)
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _pr_line(sb, "t003") == "pr_number: 231"
        assert _status(sb, "t003") == "pending"
        assert "blocked_reason" not in sb.text("t003"), "済んだ理由は消える"

    def test_review_task_gets_the_number_but_keeps_its_status(self, sb):
        self._mission(sb)
        sb.card("t005", skills="[review]", blocked_by="[t001]", status="blocked",
                extra=['blocked_reason: "別の理由で止めている"'])
        _done(sb, "t001", "--pr", "231")
        assert _pr_line(sb, "t005") == "pr_number: 231"
        assert _status(sb, "t005") == "blocked", "review の止まっている理由が PR 番号とは限らない"

    def test_only_direct_dependents_are_touched(self, sb):
        self._mission(sb)
        _done(sb, "t001", "--pr", "231")
        assert _pr_line(sb, "t002") is None, "qa task は対象の skill ではない"
        assert _pr_line(sb, "t004") is None, "t003 経由の間接的な依存には伝えない"
        assert _pr_line(sb, "t006") is None and _status(sb, "t006") == "blocked"

    def test_an_existing_pr_number_is_not_overwritten(self, sb):
        self._mission(sb)
        sb.card("t003", skills="[codex-review]", blocked_by="[t001]", status="blocked",
                extra=["pr_number: 100", 'blocked_reason: "手で入れた"'])
        _done(sb, "t001", "--pr", "231")
        assert _pr_line(sb, "t003") == "pr_number: 100"
        assert _status(sb, "t003") == "blocked", "番号を上書きしないなら status も動かさない"

    def test_without_the_flag_nothing_is_propagated_or_guessed_from_the_result(self, sb):
        self._mission(sb)
        r = sb.run("done", "t001", "PR #231 を作りました。head abc", "--mission", MISSION)
        assert r.returncode == 0
        assert _pr_line(sb, "t003") is None and _status(sb, "t003") == "blocked"

    def test_task_itself_is_done_and_the_result_is_kept(self, sb):
        self._mission(sb)
        _done(sb, "t001", "--pr", "231")
        assert _status(sb, "t001") == "done" and "finished" in sb.text("t001")

    @pytest.mark.parametrize("bad", ["0", "-3", "abc", "1.5", ""])
    def test_invalid_pr_is_rejected_before_anything_is_written(self, sb, bad):
        self._mission(sb)
        before = sb.snapshot()
        r = _done(sb, "t001", "--pr", bad)
        assert r.returncode != 0
        assert sb.snapshot() == before
        assert _status(sb, "t001") == "in_progress"

    def test_pr_with_no_dependents_says_so(self, sb):
        sb.card("t001", status="in_progress", worker="Ren")
        r = _done(sb, "t001", "--pr", "7")
        assert r.returncode == 0 and "何も伝えていません" in r.stdout

    def test_a_corrupt_card_is_left_alone(self, sb):
        self._mission(sb)
        (sb.tasks / "t007.md").write_text("not a card at all")
        r = _done(sb, "t001", "--pr", "231")
        assert r.returncode == 0
        assert (sb.tasks / "t007.md").read_text() == "not a card at all"

    def test_usage_documents_the_flag(self, sb):
        r = sb.run("done", "--help")
        assert r.returncode == 0 and "--pr <N>" in r.stdout


# ---------------------------------------------------------------------------
# 4. lint — drafting の blocked
# ---------------------------------------------------------------------------

def _lint(tasks):
    import lint_plan
    return lint_plan.check_frontmatter(tasks)


def _base(**kw):
    meta = {"id": "t001", "title": "x", "skills": ["code"], "status": "pending", "priority": "high"}
    meta.update(kw)
    return meta


class TestLintBlocked:
    def test_blocked_with_a_reason_is_accepted(self):
        assert _lint([_base(status="blocked", blocked_reason="PR 番号待ち")]) == []

    @pytest.mark.parametrize("reason", [None, "", "   ", 5, []])
    def test_blocked_without_a_usable_reason_is_rejected(self, reason):
        meta = _base(status="blocked")
        if reason is not None:
            meta["blocked_reason"] = reason
        results = _lint([meta])
        assert any(lvl == "FAIL" and "blocked_reason" in msg for lvl, _c, msg in results), results

    def test_unknown_status_is_still_rejected(self):
        assert any("unknown status" in msg for _l, _c, msg in _lint([_base(status="paused")]))

    def test_pending_needs_no_reason(self):
        assert _lint([_base()]) == []

    def test_end_to_end_through_the_cli(self, tmp_path):
        """lint の入口 (ファイルからの読み込み) を通しても受理される。"""
        mdir = tmp_path / "tasks"
        mdir.mkdir()
        (mdir / "t001.md").write_text(_card("t001", status="blocked",
                                            extra=['blocked_reason: "PR 番号待ち"']))
        import lint_plan
        text = (mdir / "t001.md").read_text()
        meta, _body = lint_plan._parse_frontmatter(text)
        assert lint_plan.check_frontmatter([meta]) == []


# ---------------------------------------------------------------------------
# 5. 本物の dispatch() を 1 サイクル回す
# ---------------------------------------------------------------------------

from test_dispatcher_retirement_exclusion import (  # noqa: E402
    AGENT, WINDOW, FakeMux, _build_repo, _load_dispatcher, SLUG,
)


def _set_card(root, task_id, *, target="null", status="pending", priority="high", skills="[code]"):
    (root / "queue" / "missions" / SLUG / "tasks" / f"{task_id}.md").write_text(
        f"---\nid: {task_id}\ntitle: work {task_id}\nskills: {skills}\npriority: {priority}\n"
        f"status: {status}\nblocked_by: []\ntarget_dir: {target}\nworker: null\n"
        "started_at: null\ncompleted_at: null\n---\n\n## Description\nwork\n\n## Result\n")


def _cycle(root, windows=(WINDOW,), director=True):
    names = list(windows) + (["Sora-director"] if director else [])
    mux = FakeMux(names)
    ns = _load_dispatcher(root, mux)
    ns["dispatch"]()
    return mux, ns


def _kickoffs(mux, task_id):
    return [m for t, m in mux.sent if t == WINDOW and f"タスク {task_id} " in m and "plan pull" in m]


def _director_msgs(mux):
    return [m for t, m in mux.sent if t == "Sora-director"]


@pytest.fixture
def repo(tmp_path):
    root = _build_repo(tmp_path)
    (root / "registry" / "workers.yaml").write_text(
        f"workers:\n  - name: {AGENT}\n    skills: [code]\n    experience: 0\n"
        "  - name: Sora\n    skills: [director]\n    role: director\n    experience: 0\n")
    return root


class TestDispatcherRouting:
    def test_matching_target_worker_gets_the_target_task(self, repo):
        _set_card(repo, "t001", target=OTHER_REPO)
        wt.write_record(repo / "registry", AGENT, OTHER_REPO)
        mux, _ = _cycle(repo)
        assert _kickoffs(mux, "t001"), mux.sent

    def test_crewvia_local_worker_is_not_given_a_target_task(self, repo):
        _set_card(repo, "t001", target=OTHER_REPO)
        wt.write_record(repo / "registry", AGENT, None)
        mux, _ = _cycle(repo)
        assert not _kickoffs(mux, "t001"), mux.sent

    def test_target_worker_is_not_given_a_crewvia_local_task(self, repo):
        """再現: TARGET_DIR 付きの Worker に crewvia 本体の task が回り、差し戻しても同じ Worker へ。"""
        _set_card(repo, "t001", target="null")
        wt.write_record(repo / "registry", AGENT, OTHER_REPO)
        mux, _ = _cycle(repo)
        assert not _kickoffs(mux, "t001"), mux.sent

    def test_worker_with_another_target_is_not_given_the_task(self, repo):
        _set_card(repo, "t001", target=OTHER_REPO)
        wt.write_record(repo / "registry", AGENT, "/srv/somewhere-else")
        mux, _ = _cycle(repo)
        assert not _kickoffs(mux, "t001")

    def test_worker_without_a_record_still_gets_crewvia_local_tasks(self, repo):
        """PR3 merge 時点で起動済みの Worker は記録を持たない。これを外すと restart の瞬間に全割り当てが止まる。"""
        _set_card(repo, "t001", target="null")
        mux, _ = _cycle(repo)
        assert _kickoffs(mux, "t001"), mux.sent

    def test_worker_without_a_record_is_held_for_a_target_task(self, repo):
        _set_card(repo, "t001", target=OTHER_REPO)
        mux, _ = _cycle(repo)
        assert not _kickoffs(mux, "t001")

    def test_worker_with_an_unreadable_record_is_held_for_a_target_task_only(self, repo):
        p = wt.record_path(repo / "registry", AGENT)
        p.parent.mkdir(parents=True)
        p.write_text("{broken")
        _set_card(repo, "t001", target=OTHER_REPO)
        _set_card(repo, "t002", target="null", priority="low")
        mux, _ = _cycle(repo)
        assert not _kickoffs(mux, "t001")
        assert _kickoffs(mux, "t002"), "壊れた記録で null の task まで止めない"

    def test_a_target_task_is_skipped_but_the_next_takeable_one_is_sent(self, repo):
        _set_card(repo, "t001", target=OTHER_REPO, priority="high")
        _set_card(repo, "t002", target="null", priority="low")
        wt.write_record(repo / "registry", AGENT, None)
        mux, _ = _cycle(repo)
        assert not _kickoffs(mux, "t001") and _kickoffs(mux, "t002")

    def test_worker_holding_an_assignment_is_not_given_another_task(self, repo):
        _set_card(repo, "t001", target="null")
        (repo / "queue" / "assignments" / AGENT).write_text(f"{SLUG}:t009\n")
        mux, _ = _cycle(repo)
        assert not _kickoffs(mux, "t001"), mux.sent

    def test_task_sent_but_not_yet_pulled_blocks_a_second_task(self, repo):
        """#22: A を送った 6 秒後に、優先度の高い B が現れて同じ Worker に送られた。"""
        _set_card(repo, "t001", priority="low")
        mux, _ = _cycle(repo)
        assert _kickoffs(mux, "t001"), "前提: A が送られている"
        _set_card(repo, "t002", priority="high")
        mux2 = FakeMux([WINDOW, "Sora-director"])
        ns = _load_dispatcher(repo, mux2)       # 同じ notify cache (repo/notify-cache.json)
        ns["dispatch"]()
        assert not _kickoffs(mux2, "t002"), mux2.sent
        assert not _kickoffs(mux2, "t001"), "A の再送もしない (TTL の内)"

    def test_the_guard_ends_when_the_first_task_is_pulled_and_finished(self, repo):
        _set_card(repo, "t001", priority="low")
        _cycle(repo)
        _set_card(repo, "t001", priority="low", status="done")
        _set_card(repo, "t002", priority="high")
        mux, _ = _cycle(repo)
        assert _kickoffs(mux, "t002"), mux.sent

    def test_the_guard_expires_with_the_notify_ttl(self, repo):
        _set_card(repo, "t001", priority="low")
        _cycle(repo)
        cache_path = repo / "notify-cache.json"
        cache = json.loads(cache_path.read_text())
        for k in list(cache):
            if k.startswith("assign_"):
                cache[k] = time.time() - 10_000
        cache_path.write_text(json.dumps(cache))
        _set_card(repo, "t002", priority="high")
        mux, _ = _cycle(repo)
        assert _kickoffs(mux, "t002"), "受け取られなかった送信は保護しない (従来どおり再び割り当てる)"

    def test_no_worker_request_carries_a_pasteable_start_command(self, repo):
        _set_card(repo, "t001", target=OTHER_REPO)
        wt.write_record(repo / "registry", AGENT, None)
        mux, _ = _cycle(repo)
        msgs = [m for m in _director_msgs(mux) if "起動" in m and "t001" in m]
        assert msgs, mux.sent
        msg = msgs[0]
        assert f"TARGET_DIR={OTHER_REPO}" in msg
        assert "CREWVIA_MUX_ENABLED=1" in msg and "CREWVIA_MUX=" in msg
        assert "AGENT_NAME=$(bash scripts/assign-name.sh code --fresh)" in msg, (
            "同じ skill の Worker が (別 TARGET_DIR で) 居るので新しい名前を取らせる")
        assert "bash scripts/start.sh worker code" in msg
        assert "担当できません" in msg

    def test_no_worker_request_for_a_local_task_has_no_target_dir(self, repo):
        _set_card(repo, "t001", target="null")
        wt.write_record(repo / "registry", AGENT, OTHER_REPO)
        mux, _ = _cycle(repo)
        msg = [m for m in _director_msgs(mux) if "t001" in m and "起動" in m][0]
        command = msg.split("起動コマンド: ", 1)[1]      # 理由の文言にも TARGET_DIR= は出る
        assert "TARGET_DIR=" not in command and "start.sh worker code" in command

    def test_no_request_when_a_matching_worker_exists(self, repo):
        _set_card(repo, "t001", target=OTHER_REPO)
        wt.write_record(repo / "registry", AGENT, OTHER_REPO)
        mux, _ = _cycle(repo)
        assert not [m for m in _director_msgs(mux) if "起動" in m], mux.sent

    def test_no_worker_at_all_gets_a_command_without_fresh(self, repo):
        _set_card(repo, "t001", target=OTHER_REPO, skills="[bash]")
        mux, _ = _cycle(repo)
        msgs = [m for m in _director_msgs(mux) if "t001" in m and "起動" in m]
        assert msgs and "--fresh" not in msgs[0] and f"TARGET_DIR={OTHER_REPO}" in msgs[0]

    def test_a_worker_left_with_only_mismatched_tasks_is_not_retired(self, repo):
        """Rule 2 (blocked-stuck) は「全部 blocked」の判定で、TARGET_DIR 不一致は当てはまらない。"""
        _set_card(repo, "t001", target=OTHER_REPO)
        p = repo / "queue" / "missions" / SLUG / "tasks" / "t001.md"
        old = time.time() - 10_000
        os.utime(p, (old, old))
        wt.write_record(repo / "registry", AGENT, None)
        mux, _ = _cycle(repo)
        assert not mux.killed
        assert not (repo / "registry" / "retirements" / f"{AGENT}.json").exists(), (
            "退役 marker が書かれた")

    def test_stale_record_of_a_retired_worker_is_swept_by_the_cycle(self, repo):
        _set_card(repo, "t001", target="null")
        wt.write_record(repo / "registry", "Gone", OTHER_REPO)
        rp = wt.record_path(repo / "registry", "Gone")
        old = time.time() - 10 * wt.SWEEP_MIN_AGE_SECONDS
        os.utime(rp, (old, old))
        wt.write_record(repo / "registry", AGENT, None)
        _cycle(repo)
        assert is_missing(wt.load_record(repo / "registry", "Gone"))
        assert not is_unreadable(wt.load_record(repo / "registry", AGENT)), "生きた Worker の記録は残る"

    def test_a_worker_with_a_fresh_heartbeat_but_no_window_keeps_its_record(self, repo):
        """mux が 1 人分だけ一時的に落とした回に、生きている Worker の記録を消さない。"""
        _set_card(repo, "t001", target="null")
        wt.write_record(repo / "registry", "Flicker", OTHER_REPO)
        rp = wt.record_path(repo / "registry", "Flicker")
        old = time.time() - 10 * wt.SWEEP_MIN_AGE_SECONDS
        os.utime(rp, (old, old))
        hb = repo / "registry" / "heartbeats"
        hb.mkdir(parents=True, exist_ok=True)
        (hb / "Flicker").write_text("alive")          # 新しい heartbeat (窓は無い)
        wt.write_record(repo / "registry", AGENT, None)
        _cycle(repo)
        assert not is_unreadable(wt.load_record(repo / "registry", "Flicker"))


# ---------------------------------------------------------------------------
# 一つの規則 — dispatcher と plan.sh が同じ答えを出す
# ---------------------------------------------------------------------------

def test_dispatcher_and_pull_agree_on_target_matching(sb, tmp_path):
    """判定表の行のうち、Worker の記録が読める行を plan.sh pull --task と突き合わせる。

    dispatcher が回さない組合せを pull が受理する (または逆) と、規則が二か所に分かれている。
    """
    for name, record, task_target, expected in DECISION_TABLE:
        if is_unreadable(record):
            continue
        worker_target = record["target_dir"]
        box = Sandbox(tmp_path / name.replace(" ", "_").replace("/", "_"))
        # 実在するディレクトリで置き換える (plan.sh add は存在確認をするが、pull は文字列比較)
        box.card("t001", target=task_target or "null")
        r = _pull(box, "t001", env_target=worker_target)
        assert (r.returncode == 0) is expected, (name, r.returncode, r.stderr)
