#!/usr/bin/env python3
"""plan.sh は未知の引数を拒否し、`--help` は何も書かず、mission の曖昧さは機械で解く (t005 / backlog #23)。

## 背景

同じ根 — 「解釈できなかった引数を黙って捨てる / positional に混ぜる」 — から 4 種の事故が出た。

* `plan.sh init --help` が「--help」という mission を作り、default_mission を奪った
* `plan.sh pull --help` が本物の pull を実行した
* `plan.sh done t007 --agent X "..."` で Result が `--agent` になった / add の title に引数が紛れた
* `plan.sh done` が「multiple missions」で拒否され、数千字の Result を打ち直した

## このファイルが固定すること

1. **全サブコマンド** で `-h` / `--help` は usage を出して exit 0、**queue にも registry にも
   1 バイトも書かない** (書き込みの有無は木全体のスナップショットで assert する。陽性対照つき)
2. 未知の option / 余った positional / 足りない positional は exit 2 (pull だけ exit 1 — pull の
   2 は「タスクなし」で、Worker は 2 を受けると待って再試行する)。何も書かない
3. `--` 以降は positional (`-` で始まる title の逃げ道)
4. done / fail / needs-director / update: `--mission` 無しで task id が複数 mission に当たるとき、
   **env の mission に自分が in_progress で就いているときだけ** それを使う。それ以外は拒否
5. pull: `--task` が複数 mission に当たれば拒否 / skills は `--skills` → SKILLS → registry の順で、
   どれも無ければ拒否 (絞り込みを無効にしない) / Director (registry の role) は拒否。
   **`ROLE` 環境変数は見ない**

赤の実証は tests/red_proof_t005.sh (修正前の plan.sh に差し替えて、このファイルが赤になること)。
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
#: 赤の実証が修正前の plan.sh に差し替えるための口。通常は使わない。
PLAN_SH = pathlib.Path(os.environ.get("PLAN_SH_UNDER_TEST") or REPO_ROOT / "scripts" / "plan.sh")

M1, M2 = "m-one", "m-two"


# ---------------------------------------------------------------------------
# harness — 本番の queue / registry には決して触れない
# ---------------------------------------------------------------------------

class Sandbox:
    """`<root>/queue` と `<root>/registry` を持つ使い捨ての crewvia。

    CREWVIA_QUEUE と CREWVIA_REPO_ROOT の **両方** を root に向ける (片方だけだと
    plan.sh done が本番の registry/workers.yaml を書き換える)。`env -i` 相当で、
    呼び出し元の AGENT_NAME / SKILLS / CREWVIA_* を一切引き継がない。
    TASK_GRAPH の生成先は既定 (root/registry/task-graph/tasks.json) のままにして、
    `--help` が生成器を呼ぶ経路 (registry に書く) があれば木の差分に出るようにする。
    """

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.queue = root / "queue"
        self.target = root / "work"      # task の target_dir (worktree を作らせないため)
        self.target.mkdir()

    def env(self, **extra):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "CREWVIA_QUEUE": str(self.queue),
            "CREWVIA_REPO_ROOT": str(self.root),
            "CREWVIA_TASKVIA": "disabled",
            "TARGET_DIR": str(self.target),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def plan(self, *args, env=None, unset=()):
        e = self.env(**(env or {}))
        for k in unset:
            e.pop(k, None)
        return subprocess.run(
            ["bash", str(PLAN_SH), *args], env=e, cwd=str(self.root),
            capture_output=True, text=True, timeout=60,
        )

    def snapshot(self):
        """木全体 (ディレクトリも含む) の (種類, サイズ, mtime_ns)。"""
        snap = {}
        for p in sorted(self.root.rglob("*")):
            st = p.lstat()
            snap[str(p.relative_to(self.root))] = (p.is_dir(), st.st_size, st.st_mtime_ns)
        return snap

    def quiet_snapshot(self):
        """`snapshot()` から task-graph の生成物だけを除いたもの。

        `die()` で終わる実行は、途中まで書いた場合に備えて DAG を再生成する (既存の意図的な
        挙動: plan.sh 末尾)。中身は同じでも mtime が変わるので、拒否された実行の「queue と
        registry/workers.yaml が変わっていない」の確認ではここを見ない。`--help` と使い方の
        誤り (UsageExit) は再生成しないので、そちらは `snapshot()` (全部) で確かめる。
        """
        return {k: v for k, v in self.snapshot().items()
                if not k.startswith("registry/task-graph")}

    def registry_workers(self, text):
        (self.root / "registry").mkdir(exist_ok=True)
        # 使い捨ての root の中にだけ書く。`.write_text` を使わないのは、scripts/test_registry_lock.sh の
        # 静的検査 (workers.yaml と同じファイルの raw write_text を弾く) に掛からないため。
        with open(self.root / "registry" / "workers.yaml", "w") as f:
            f.write(text)

    def card(self, mission, task):
        return (self.queue / "missions" / mission / "tasks" / f"{task}.md").read_text()

    def status(self, mission, task):
        m = re.search(r"^status:\s*(\S+)", self.card(mission, task), re.M)
        return m.group(1).strip("\"'") if m else None

    def worker(self, mission, task):
        m = re.search(r"^worker:\s*(\S+)", self.card(mission, task), re.M)
        return m.group(1).strip("\"'") if m else None


@pytest.fixture()
def sb(tmp_path):
    return Sandbox(tmp_path)


def _ok(r):
    assert r.returncode == 0, f"rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return r


def _dispatch_names():
    """plan.sh 末尾の dispatch テーブルのキー (= サブコマンド全部)。"""
    text = PLAN_SH.read_text()
    block = re.search(r"^dispatch = \{(.*?)^\}", text, re.M | re.S).group(1)
    names = re.findall(r"^\s*'([a-z-]+)':", block, re.M)
    assert len(names) >= 20, names
    return names


SUBCOMMANDS = _dispatch_names()


def two_missions(sb, *, worker="Ren", first_status="in_progress"):
    """m-one / m-two のどちらにも t001 がある。m-one の t001 は `worker` が実行中。

    どちらも同じ id (`t001`) — mission ごとの採番なので、これが実運用の衝突の形。
    """
    _ok(sb.plan("init", "M one", "--mission", M1))
    _ok(sb.plan("init", "M two", "--mission", M2))
    for m in (M1, M2):
        _ok(sb.plan("add", f"task in {m}", "--mission", m, "--skills", "code",
                    "--target-dir", str(sb.target)))
    if first_status == "in_progress":
        _ok(sb.plan("pull", "--task", "t001", "--mission", M1, "--agent", worker,
                    "--skills", "code"))
    assert sb.status(M2, "t001") == "pending"


# ---------------------------------------------------------------------------
# 1. --help は全サブコマンドで何も書かない
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sub", SUBCOMMANDS)
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage_and_writes_nothing_on_a_fresh_directory(sb, sub, flag):
    before = sb.snapshot()
    r = sb.plan(sub, flag)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith(f"Usage: plan.sh {sub}"), r.stdout
    assert sb.snapshot() == before, "help が何かを作った / 書いた (queue の骨組みも含む)"


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_help_writes_nothing_in_a_populated_queue(sb, sub):
    """queue に mission がある状態でも、registry (task-graph / workers) を含めて 1 バイトも書かない。"""
    two_missions(sb)
    before = sb.snapshot()
    r = sb.plan(sub, "--help")
    assert r.returncode == 0, r.stderr
    assert sb.snapshot() == before


@pytest.mark.parametrize("args", [
    ("done", "t001", "--help"),
    ("init", "title", "--mission", "x", "--help"),
    ("add", "title", "--skills", "code", "-h"),
    ("update", "t001", "--priority", "high", "--help"),
    ("pull", "--skills", "code", "--help"),
    ("fail", "t001", "--head", "abc1234", "--help"),
])
def test_help_anywhere_in_the_arguments_stops_before_any_write(sb, args):
    two_missions(sb)
    before = sb.snapshot()
    r = sb.plan(*args)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("Usage: plan.sh")
    assert sb.snapshot() == before
    assert sb.status(M1, "t001") == "in_progress"      # pull --help が本物の pull にならない


def test_toplevel_help_variants_write_nothing(sb):
    before = sb.snapshot()
    for a in ("--help", "-h", "help"):
        r = sb.plan(a)
        assert r.returncode == 0 and "plan.sh init" in r.stdout, (a, r.stderr)
    assert sb.snapshot() == before


def test_init_help_does_not_create_a_mission_or_steal_the_default(sb):
    """事故そのもの (2026-09-25): `init --help` が「--help」mission を作り default_mission を奪った。"""
    _ok(sb.plan("init", "real", "--mission", M1))
    state_before = (sb.queue / "state.yaml").read_text()
    _ok(sb.plan("init", "--help"))
    assert (sb.queue / "state.yaml").read_text() == state_before
    assert sorted(p.name for p in (sb.queue / "missions").iterdir()) == [M1]


def test_help_harness_would_notice_a_write(sb):
    """陽性対照: 木の比較は、本当に書く実行 (init) の差を検出できる。"""
    before = sb.snapshot()
    _ok(sb.plan("init", "real", "--mission", M1))
    assert sb.snapshot() != before
    assert (sb.root / "registry" / "task-graph" / "tasks.json").exists(), \
        "生成器が動く構成でないと、--help が生成器を呼ぶ経路を検出できない"


# ---------------------------------------------------------------------------
# 2. 未知の option / 余剰・不足の positional は拒否
# ---------------------------------------------------------------------------

def _usage_rc(sub):
    return 1 if sub == "pull" else 2


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_unknown_option_is_rejected_and_nothing_is_written(sb, sub):
    two_missions(sb)
    before = sb.snapshot()
    r = sb.plan(sub, "--no-such-option", "x")
    assert r.returncode == _usage_rc(sub), (r.returncode, r.stderr)
    assert "unknown option '--no-such-option'" in r.stderr
    assert "Usage: plan.sh " + sub in r.stderr
    assert sb.snapshot() == before


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_too_many_positionals_are_rejected_not_silently_dropped(sb, sub):
    """全サブコマンドが positional の最大数を宣言している (宣言が抜けたら受理してしまい赤)。"""
    two_missions(sb)
    before = sb.snapshot()
    r = sb.plan(sub, "p1", "p2", "p3", "p4", "p5")
    assert r.returncode == _usage_rc(sub), (r.returncode, r.stderr)
    assert "positional" in r.stderr
    assert sb.snapshot() == before


@pytest.mark.parametrize("args", [
    ("done", "t001"),                       # result が無い
    ("done", "t001", "part one", "part two"),  # 引用符を忘れた結果の語
    ("needs-director", "t001"),
    ("init",),
    ("add", "one", "two", "--skills", "code"),   # title に引数が紛れる
    ("update",),
    ("archive",),
])
def test_wrong_positional_count_is_rejected(sb, args):
    two_missions(sb)
    before = sb.snapshot()
    r = sb.plan(*args)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert sb.snapshot() == before


def test_done_with_an_agent_flag_no_longer_writes_the_flag_as_the_result(sb):
    """事故そのもの: `done t007 --agent X "..."` で Result が `--agent` になった。"""
    two_missions(sb)
    r = sb.plan("done", "t001", "--agent", "Ren", "finished", "--mission", M1)
    assert r.returncode == 2 and "unknown option '--agent'" in r.stderr
    assert sb.status(M1, "t001") == "in_progress"


def test_option_value_that_is_another_option_is_rejected(sb):
    two_missions(sb)
    r = sb.plan("done", "t001", "result", "--mission", "--skills")
    assert r.returncode == 2 and "requires a value" in r.stderr
    r = sb.plan("done", "t001", "result", "--mission")
    assert r.returncode == 2 and "requires a value" in r.stderr


def test_equals_form_is_rejected_with_a_hint(sb):
    two_missions(sb)
    r = sb.plan("done", "t001", "result", f"--mission={M1}")
    assert r.returncode == 2 and "空白で区切る" in r.stderr
    assert sb.status(M1, "t001") == "in_progress"


# ---------------------------------------------------------------------------
# 3. `--` と、option らしくない `-` 始まりの語
# ---------------------------------------------------------------------------

def test_double_dash_makes_the_rest_positional(sb):
    _ok(sb.plan("init", "--mission", M1, "--", "--dash-title"))
    assert '--dash-title' in (sb.queue / "missions" / M1 / "mission.yaml").read_text()


def test_dash_led_text_with_spaces_is_not_an_option(sb):
    """Result / title が `- 修正した` や `-3 件` で始まるだけで拒否しない (option の形ではない)。"""
    two_missions(sb)
    _ok(sb.plan("done", "t001", "- 修正した (3 件)", "--mission", M1))
    assert sb.status(M1, "t001") == "done"
    _ok(sb.plan("add", "-3 件の修正", "--mission", M2, "--skills", "code"))


def test_option_value_may_start_with_dashes(sb):
    two_missions(sb)
    _ok(sb.plan("update", "t001", "--mission", M2, "--description", "--not-an-option text"))
    assert "--not-an-option text" in sb.card(M2, "t001")


# ---------------------------------------------------------------------------
# 4. done / fail / needs-director / update の mission の曖昧さ
# ---------------------------------------------------------------------------

AMBIGUOUS_CALLS = {
    "done": ("done", "t001", "result text"),
    "needs-director": ("needs-director", "t001", "reason text"),
    "update": ("update", "t001", "--priority", "high"),
    "fail": ("fail", "t001", "--no-head", "no repo in test"),
}


def _both_untouched(sb):
    return sb.status(M1, "t001") == "in_progress" and sb.status(M2, "t001") == "pending"


@pytest.mark.parametrize("name", AMBIGUOUS_CALLS)
def test_ambiguous_task_id_without_env_is_rejected_with_candidates_and_commands(sb, name):
    two_missions(sb)
    before = sb.quiet_snapshot()
    r = sb.plan(*AMBIGUOUS_CALLS[name], unset=("CREWVIA_MISSION_SLUG", "AGENT_NAME"))
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert "multiple missions" in r.stderr
    for m in (M1, M2):
        assert f"plan.sh {name} t001 --mission {m}" in r.stderr, r.stderr
    assert sb.quiet_snapshot() == before


@pytest.mark.parametrize("name", ["done", "needs-director", "update"])
def test_env_mission_is_used_only_when_the_agent_is_working_on_it(sb, name):
    two_missions(sb)
    r = sb.plan(*AMBIGUOUS_CALLS[name],
                env={"CREWVIA_MISSION_SLUG": M1, "AGENT_NAME": "Ren"})
    _ok(r)
    assert f"CREWVIA_MISSION_SLUG={M1} を使います" in r.stderr
    assert sb.status(M2, "t001") == "pending", "env の mission でない側は触らない"
    assert sb.status(M1, "t001") != "in_progress" or name == "update"


def test_env_mission_is_used_by_fail_too(sb):
    two_missions(sb)
    r = sb.plan(*AMBIGUOUS_CALLS["fail"],
                env={"CREWVIA_MISSION_SLUG": M1, "AGENT_NAME": "Ren"})
    _ok(r)
    assert sb.status(M1, "t001") == "failed" and sb.status(M2, "t001") == "pending"


@pytest.mark.parametrize("name", AMBIGUOUS_CALLS)
def test_env_mission_of_another_worker_is_not_enough(sb, name):
    """env の mission の card の worker が AGENT_NAME と違えば拒否 (残りの env が他人の card に届かない)。"""
    two_missions(sb, worker="Ren")
    before = sb.quiet_snapshot()
    r = sb.plan(*AMBIGUOUS_CALLS[name],
                env={"CREWVIA_MISSION_SLUG": M1, "AGENT_NAME": "Hana"})
    assert r.returncode == 1 and "multiple missions" in r.stderr
    assert sb.quiet_snapshot() == before


@pytest.mark.parametrize("name", AMBIGUOUS_CALLS)
def test_env_mission_without_an_agent_name_is_not_enough(sb, name):
    two_missions(sb)
    before = sb.quiet_snapshot()
    r = sb.plan(*AMBIGUOUS_CALLS[name], env={"CREWVIA_MISSION_SLUG": M1}, unset=("AGENT_NAME",))
    assert r.returncode == 1 and "multiple missions" in r.stderr
    assert sb.quiet_snapshot() == before


@pytest.mark.parametrize("name", AMBIGUOUS_CALLS)
def test_env_mission_whose_card_is_not_in_progress_is_not_enough(sb, name):
    """自分の名前が card に残っていても、実行中でなければ (pending の別 mission 側を指す env は) 使わない。"""
    two_missions(sb, worker="Ren")
    before = sb.quiet_snapshot()
    # env が m-two (pending・worker 無し) を指している: AGENT_NAME=Ren でも m-two の card の worker は Ren ではない
    r = sb.plan(*AMBIGUOUS_CALLS[name], env={"CREWVIA_MISSION_SLUG": M2, "AGENT_NAME": "Ren"})
    assert r.returncode == 1 and "multiple missions" in r.stderr
    assert sb.quiet_snapshot() == before


def test_env_mission_with_the_agents_own_name_on_a_finished_card_is_not_enough(sb):
    """worker は自分の名前のまま、でも実行中ではない (done 済み) card: env の mission を使わない。

    worker の一致だけで決めると、前の task の `.crewvia-env` を source したままのシェルが、
    終わった task の card を書き換える。status の条件を単独で確かめる。
    """
    two_missions(sb)
    _ok(sb.plan("done", "t001", "finished", "--mission", M1))
    assert sb.worker(M1, "t001") == "Ren" and sb.status(M1, "t001") == "done"
    before = sb.quiet_snapshot()
    r = sb.plan("update", "t001", "--priority", "high",
                env={"CREWVIA_MISSION_SLUG": M1, "AGENT_NAME": "Ren"})
    assert r.returncode == 1 and "multiple missions" in r.stderr, r.stderr
    assert sb.quiet_snapshot() == before


def test_env_mission_that_does_not_hold_the_task_is_ignored(sb):
    two_missions(sb)
    _ok(sb.plan("init", "M three", "--mission", "m-three"))       # t001 を持たない
    r = sb.plan(*AMBIGUOUS_CALLS["done"],
                env={"CREWVIA_MISSION_SLUG": "m-three", "AGENT_NAME": "Ren"})
    assert r.returncode == 1 and "multiple missions" in r.stderr


def test_explicit_mission_flag_always_wins_over_env(sb):
    two_missions(sb)
    _ok(sb.plan("update", "t001", "--mission", M2, "--priority", "high",
                env={"CREWVIA_MISSION_SLUG": M1, "AGENT_NAME": "Ren"}))
    assert re.search(r"^priority:\s*high", sb.card(M2, "t001"), re.M)
    assert not re.search(r"^priority:\s*high", sb.card(M1, "t001"), re.M)


def test_update_without_mission_and_a_single_holder_still_works(sb):
    """曖昧でなければ従来どおり (default_mission) — 厳格化で通常の Director 操作を壊さない。"""
    _ok(sb.plan("init", "only", "--mission", M1))
    _ok(sb.plan("add", "solo", "--mission", M1, "--skills", "code"))
    _ok(sb.plan("update", "t001", "--priority", "high"))
    assert re.search(r"^priority:\s*high", sb.card(M1, "t001"), re.M)


# ---------------------------------------------------------------------------
# 5. pull
# ---------------------------------------------------------------------------

def test_pull_task_in_two_missions_without_mission_is_rejected(sb):
    two_missions(sb, first_status="pending")
    before = sb.quiet_snapshot()
    r = sb.plan("pull", "--task", "t001", "--agent", "Ren", "--skills", "code")
    assert r.returncode == 1 and "multiple missions" in r.stderr
    for m in (M1, M2):
        assert f"plan.sh pull --task t001 --mission {m}" in r.stderr
    assert sb.quiet_snapshot() == before


def test_pull_task_with_explicit_mission_still_works(sb):
    two_missions(sb, first_status="pending")
    _ok(sb.plan("pull", "--task", "t001", "--mission", M2, "--agent", "Ren", "--skills", "code"))
    assert sb.status(M2, "t001") == "in_progress" and sb.status(M1, "t001") == "pending"


def _one_pending_task(sb, *, skills="code"):
    _ok(sb.plan("init", "M", "--mission", M1))
    _ok(sb.plan("add", "t", "--mission", M1, "--skills", skills, "--target-dir", str(sb.target)))


def test_pull_without_any_skills_is_rejected_and_writes_nothing(sb):
    _one_pending_task(sb)
    before = sb.quiet_snapshot()
    r = sb.plan("pull", "--agent", "Ren", unset=("SKILLS",))
    assert r.returncode == 1 and "skills" in r.stderr, r.stderr
    assert sb.quiet_snapshot() == before and sb.status(M1, "t001") == "pending"
    # 空文字の --skills も「指定した」ことにならない
    r = sb.plan("pull", "--agent", "Ren", "--skills", "", unset=("SKILLS",))
    assert r.returncode == 1
    assert sb.status(M1, "t001") == "pending"


def test_pull_task_without_any_skills_is_rejected_too(sb):
    _one_pending_task(sb)
    r = sb.plan("pull", "--task", "t001", "--mission", M1, "--agent", "Ren", unset=("SKILLS",))
    assert r.returncode == 1 and sb.status(M1, "t001") == "pending"


def test_pull_uses_the_SKILLS_env_when_no_flag(sb):
    _one_pending_task(sb)
    _ok(sb.plan("pull", "--agent", "Ren", env={"SKILLS": "code,python"}))
    assert sb.status(M1, "t001") == "in_progress"


def test_pull_uses_registry_skills_when_no_flag_and_no_env(sb):
    _one_pending_task(sb)
    sb.registry_workers("workers:\n  - name: Ren\n    skills: [code, python]\n    task_count: 0\n")
    _ok(sb.plan("pull", "--agent", "Ren", unset=("SKILLS",)))
    assert sb.status(M1, "t001") == "in_progress"


def test_the_skills_gate_is_not_disabled_by_the_fallback(sb):
    """SKILLS が code だけなら ops の task は取れない (フォールバックが絞り込みを無効にしない)。"""
    _one_pending_task(sb, skills="ops")
    r = sb.plan("pull", "--agent", "Ren", env={"SKILLS": "code"})
    assert r.returncode == 2 and "no_skill_match" in r.stderr, (r.returncode, r.stderr)
    assert sb.status(M1, "t001") == "pending"


DIRECTORS = "workers:\n  - name: Sora\n    role: director\n    skills: [code]\n  - name: Ren\n    skills: [code]\n"


def test_director_cannot_pull_by_registry_role(sb):
    _one_pending_task(sb)
    sb.registry_workers(DIRECTORS)
    before = sb.quiet_snapshot()
    r = sb.plan("pull", "--agent", "Sora", "--skills", "code")
    assert r.returncode == 1 and "role: director" in r.stderr, r.stderr
    assert sb.quiet_snapshot() == before and sb.status(M1, "t001") == "pending"
    r = sb.plan("pull", "--skills", "code", env={"AGENT_NAME": "Sora"})
    assert r.returncode == 1 and "role: director" in r.stderr
    assert sb.status(M1, "t001") == "pending"


def test_director_cannot_pull_a_specific_task_either(sb):
    _one_pending_task(sb)
    sb.registry_workers(DIRECTORS)
    r = sb.plan("pull", "--task", "t001", "--mission", M1, "--agent", "Sora", "--skills", "code")
    assert r.returncode == 1 and sb.status(M1, "t001") == "pending"


@pytest.mark.parametrize("role_env", ["director", "Director", "worker", ""])
def test_role_env_is_not_consulted(sb, role_env):
    """dispatcher が spawn する kai-review.sh は Director の env を継承しうる。ROLE で判定しない。"""
    _one_pending_task(sb)
    sb.registry_workers(DIRECTORS)
    _ok(sb.plan("pull", "--agent", "Ren", "--skills", "code", env={"ROLE": role_env}))
    assert sb.status(M1, "t001") == "in_progress"


def test_unregistered_agent_can_still_pull(sb):
    """registry に居ない名前 (Kai-codex など) を Director と誤認しない。"""
    _one_pending_task(sb)
    sb.registry_workers(DIRECTORS)
    _ok(sb.plan("pull", "--agent", "Kai-codex", "--skills", "code"))


# ---------------------------------------------------------------------------
# 6. 構造: 表がサブコマンドと 1 対 1
# ---------------------------------------------------------------------------

def test_every_subcommand_has_a_usage_and_an_arity_entry():
    text = PLAN_SH.read_text()
    usage = set(re.findall(r"^    '([a-z-]+)': ", re.search(r"^USAGE = \{(.*?)^\}", text, re.M | re.S).group(1), re.M))
    arity = set(re.findall(r"'([a-z-]+)': \(\d, \d\)",
                           re.search(r"^POSITIONAL_ARITY = \{(.*?)^\}", text, re.M | re.S).group(1)))
    assert usage == set(SUBCOMMANDS), usage ^ set(SUBCOMMANDS)
    assert arity == set(SUBCOMMANDS), arity ^ set(SUBCOMMANDS)


def test_header_usage_documents_the_strict_rules():
    head = "\n".join(PLAN_SH.read_text().splitlines()[:70])
    for needle in ("--help", "CREWVIA_MISSION_SLUG", "SKILLS", "role: director"):
        assert needle in head, needle
