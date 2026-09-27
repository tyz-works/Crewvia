#!/usr/bin/env python3
"""task の成果物の宣言 `deliverable` (t013 / backlog #31)。

「PR を作る task に Write 禁止の skill (research / review / planning ...) を付けた」
「PR を作る task が --pr を付け忘れた」を、スキル名からの推測ではなく **宣言** で機械的に落とす:

1. `config/skill-permissions.yaml` の `can_produce_deliverable` — 判定の **唯一の情報源**
   (lint_plan.py にスキル名のリテラルは無い)
2. `plan.sh add / update --deliverable pr|file|none` — 不正値は exit 2 で何も書かない
3. `plan.sh init` — mission.yaml に `deliverable_required: true` の印を書く
4. lint — 印のある mission では全 task に宣言を求め、`pr|file` を宣言した task は
   skills の少なくとも 1 つが成果物を作れることを求める (印の有無にかかわらず)
5. `plan.sh done` — `deliverable: pr` の task は --pr か --no-pr が要る

    python3 -m pytest tests/test_task_deliverable.py -v
"""

from __future__ import annotations

import pathlib
import re
import shutil
import sys

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
SCRIPTS_DIR = REPO / "scripts"
CONFIG_DIR = REPO / "config"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import lint_plan  # noqa: E402
from test_assignment_routing import MISSION, Sandbox, _card  # noqa: E402

#: 成果物を作れない skill (config の `can_produce_deliverable: false`)。Director 決定の 6 つ。
NON_PRODUCERS = ("codex-review", "review", "research", "planning", "plan_review", "verify")


class DeliverableSandbox(Sandbox):
    """`Sandbox` + 実 config の隔離コピー (lint は `<repo_root>/config` を読む)。"""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        shutil.copytree(CONFIG_DIR, self.root / "config")

    def mark_required(self):
        path = self.queue / "missions" / MISSION / "mission.yaml"
        path.write_text(path.read_text() + "deliverable_required: true\n")

    def lint(self, *extra):
        return self.run("lint", "--mission", MISSION, *extra)


@pytest.fixture
def sb(tmp_path):
    return DeliverableSandbox(tmp_path)


def _field(sb, task, name):
    for line in sb.text(task).splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# 1. 情報源: config/skill-permissions.yaml
# ---------------------------------------------------------------------------

class TestConfigIsTheSingleSource:
    def _caps(self):
        caps, problem = lint_plan._load_deliverable_capabilities(str(CONFIG_DIR / "skill-permissions.yaml"))
        assert problem is None
        return caps

    def test_the_six_non_producers_declare_false(self):
        caps = self._caps()
        assert {s for s, v in caps.items() if v is False} == set(NON_PRODUCERS)

    def test_no_skill_carries_a_value_other_than_a_boolean(self):
        assert all(v in (True, False) for v in self._caps().values())

    def test_every_declared_skill_is_a_known_skill(self):
        known = lint_plan._load_known_skills(str(CONFIG_DIR / "skill-permissions.yaml"))
        assert set(self._caps()) <= known

    def test_lint_plan_carries_no_skill_name_literal(self):
        """判定は config の欄だけを読む。スキル名を書き足す抜け道を作らない。"""
        source = (SCRIPTS_DIR / "lint_plan.py").read_text()
        # コメント・docstring を除いたコード部分に、6 つのスキル名を文字列として書かない
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        for skill in NON_PRODUCERS:
            assert not re.search(rf"""['"]{re.escape(skill)}['"]""", code), skill

    def test_hook_loader_still_reads_the_file(self):
        """hooks/lib_skill_perms.py (yaml と fallback の両方) は新しい欄を許可規則に混ぜない。"""
        sys.path.insert(0, str(REPO / "hooks"))
        import lib_skill_perms
        path = str(CONFIG_DIR / "skill-permissions.yaml")
        fallback = lib_skill_perms._parse_yaml_fallback(path)
        loaded = lib_skill_perms.yaml.safe_load(open(path)) if lib_skill_perms.yaml else fallback
        for cfg in (fallback, loaded):
            assert cfg["skills"]["research"]["deny"], "research の deny が読めている"
            assert cfg["skills"]["code"]["allow"], "code の allow が読めている"
        # fallback は欄を許可規則として取り込まない
        assert all(isinstance(v, list) for v in fallback["skills"]["research"].values())


# ---------------------------------------------------------------------------
# 2. plan.sh add / update --deliverable
# ---------------------------------------------------------------------------

class TestAddAndUpdate:
    @pytest.mark.parametrize("value", ["pr", "file", "none"])
    def test_add_writes_the_declaration_to_the_card(self, sb, value):
        r = sb.run("add", "x", "--mission", MISSION, "--skills", "code", "--deliverable", value)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _field(sb, "t099", "deliverable") == value

    def test_add_without_the_flag_writes_no_field(self, sb):
        r = sb.run("add", "x", "--mission", MISSION, "--skills", "code")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _field(sb, "t099", "deliverable") is None

    @pytest.mark.parametrize("bad", ["PR", "pull-request", "", "null", "pr,file"])
    def test_add_rejects_an_invalid_value_with_exit_2_and_writes_nothing(self, sb, bad):
        before = sb.snapshot()
        r = sb.run("add", "x", "--mission", MISSION, "--skills", "code", "--deliverable", bad)
        assert r.returncode == 2, (r.stdout, r.stderr)
        assert "--deliverable" in r.stderr
        assert sb.snapshot() == before

    def test_update_sets_and_replaces_the_declaration(self, sb):
        sb.card("t001", status="pending")
        for value in ("pr", "none", "file"):
            r = sb.run("update", "t001", "--mission", MISSION, "--deliverable", value)
            assert r.returncode == 0, (r.stdout, r.stderr)
            assert f"deliverable={value}" in r.stdout
            assert _field(sb, "t001", "deliverable") == value

    @pytest.mark.parametrize("bad", ["PR", "yes", "", "pr,file"])
    def test_update_rejects_an_invalid_value_with_exit_2_and_writes_nothing(self, sb, bad):
        sb.card("t001", status="pending")
        before = sb.snapshot()
        r = sb.run("update", "t001", "--mission", MISSION, "--deliverable", bad)
        assert r.returncode == 2, (r.stdout, r.stderr)
        assert sb.snapshot() == before

    def test_update_alone_does_not_touch_other_fields(self, sb):
        sb.card("t001", status="in_progress", worker="Ren", extra=["pr_number: 7"])
        r = sb.run("update", "t001", "--mission", MISSION, "--deliverable", "pr")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _field(sb, "t001", "status") == "in_progress"
        assert _field(sb, "t001", "worker") == "Ren"
        assert _field(sb, "t001", "pr_number") == "7"

    @pytest.mark.parametrize("sub", ["add", "update", "done"])
    def test_usage_documents_the_flag(self, sb, sub):
        r = sb.run(sub, "--help")
        assert r.returncode == 0
        assert ("--deliverable" in r.stdout) == (sub != "done")
        if sub == "done":
            assert "deliverable: pr" in r.stdout


class TestInitMarksTheMission:
    def test_init_writes_deliverable_required_true(self, sb):
        r = sb.run("init", "fresh mission", "--mission", "m-fresh")
        assert r.returncode == 0, (r.stdout, r.stderr)
        text = (sb.queue / "missions" / "m-fresh" / "mission.yaml").read_text()
        assert re.search(r"^deliverable_required: true$", text, re.M), text

    def test_the_mark_survives_a_mission_rewrite(self, sb):
        """add (next_task_id の更新で mission.yaml を書き戻す) を経ても印は消えない。"""
        sb.run("init", "fresh mission", "--mission", "m-fresh")
        sb.run("add", "x", "--mission", "m-fresh", "--skills", "code", "--deliverable", "none")
        text = (sb.queue / "missions" / "m-fresh" / "mission.yaml").read_text()
        assert "deliverable_required: true" in text


# ---------------------------------------------------------------------------
# 3. lint (関数)
# ---------------------------------------------------------------------------

def _perms(tmp_path, body):
    path = tmp_path / "skill-permissions.yaml"
    path.write_text("skills:\n" + body)
    return str(path)


PERMS = (
    "  code:\n    allow: []\n    deny: []\n"
    "  docs:\n    can_produce_deliverable: true\n    allow: []\n    deny: []\n"
    "  research:\n    can_produce_deliverable: false\n    allow: []\n    deny: []\n"
    "  review:\n    can_produce_deliverable: false   # 読むだけ\n    allow: []\n    deny: []\n"
)


def _t(deliverable="__absent__", skills=("code",), tid="t001"):
    meta = {"id": tid, "title": "x", "skills": list(skills), "status": "pending", "priority": "high"}
    if deliverable != "__absent__":
        meta["deliverable"] = deliverable
    return meta


def _fails(results):
    return [msg for lvl, cat, msg in results if lvl == "FAIL" and cat == "deliverable"]


class TestCheckDeliverable:
    @pytest.fixture
    def perms(self, tmp_path):
        return _perms(tmp_path, PERMS)

    @pytest.mark.parametrize("declared", ["pr", "file"])
    @pytest.mark.parametrize("skills", [["research"], ["review"], ["research", "review"]])
    def test_a_deliverable_with_only_non_producing_skills_fails(self, perms, declared, skills):
        fails = _fails(lint_plan.check_deliverable([_t(declared, skills)], perms))
        assert len(fails) == 1 and "t001" in fails[0] and "can_produce_deliverable" in fails[0], fails

    @pytest.mark.parametrize("declared", ["pr", "file"])
    @pytest.mark.parametrize("skills", [["code"], ["docs"], ["research", "code"], ["review", "docs"]])
    def test_one_producing_skill_is_enough(self, perms, declared, skills):
        assert lint_plan.check_deliverable([_t(declared, skills)], perms) == []

    def test_none_needs_no_writer(self, perms):
        assert lint_plan.check_deliverable([_t("none", ["research"])], perms) == []

    def test_an_unknown_skill_or_a_skill_without_the_field_counts_as_able(self, perms):
        assert lint_plan.check_deliverable([_t("pr", ["no-such-skill"])], perms) == []
        assert lint_plan.check_deliverable([_t("pr", ["code"])], perms) == []

    @pytest.mark.parametrize("value", ["PR", "pull", 5, True, ["pr"]])
    def test_an_unknown_value_fails_even_without_the_mission_mark(self, perms, value):
        fails = _fails(lint_plan.check_deliverable([_t(value)], perms, required=False))
        assert len(fails) == 1 and "unknown deliverable" in fails[0], fails

    def test_missing_declaration_fails_only_when_the_mission_requires_it(self, perms):
        assert lint_plan.check_deliverable([_t()], perms, required=False) == []
        fails = _fails(lint_plan.check_deliverable([_t()], perms, required=True))
        assert len(fails) == 1 and "未宣言" in fails[0] and "update t001 --deliverable" in fails[0], fails

    def test_an_explicit_null_counts_as_undeclared(self, perms):
        assert _fails(lint_plan.check_deliverable([_t(None)], perms, required=True))

    def test_declared_tasks_pass_in_a_required_mission(self, perms):
        tasks = [_t("pr", ["code"], "t001"), _t("none", ["research"], "t002"), _t("file", ["docs"], "t003")]
        assert lint_plan.check_deliverable(tasks, perms, required=True) == []

    @pytest.mark.parametrize("declared", ["pr", "file"])
    def test_an_empty_skills_list_fails_because_no_skill_can_produce_it(self, perms, declared):
        """all([]) は True になり得るが、skills が空の task は producer を 1 つも持てない。

        `check_frontmatter` は `skills: []` を有効な frontmatter として通すため、
        `check_deliverable` 自身がこのケースを閉じる (前提を他の関数に預けない)。
        """
        fails = _fails(lint_plan.check_deliverable([_t(declared, [])], perms))
        assert len(fails) == 1 and "t001" in fails[0] and "skills" in fails[0], fails

    @pytest.mark.parametrize("declared", ["pr", "file"])
    def test_a_missing_skills_field_also_fails(self, perms, declared):
        """skills 欠落 (キー自体が無い) も、空リストと同じく producer が無いので FAIL。"""
        meta = {"id": "t001", "title": "x", "status": "pending", "priority": "high", "deliverable": declared}
        fails = _fails(lint_plan.check_deliverable([meta], perms))
        assert len(fails) == 1 and "t001" in fails[0] and "skills" in fails[0], fails

    def test_an_unreadable_config_is_a_fail_not_a_pass(self, tmp_path):
        missing = str(tmp_path / "nope.yaml")
        fails = _fails(lint_plan.check_deliverable([_t("pr", ["research"])], missing))
        assert len(fails) == 1 and "突き合わせられません" in fails[0], fails
        # 宣言の無い task・none の task は config を要らない
        assert lint_plan.check_deliverable([_t()], missing) == []
        assert lint_plan.check_deliverable([_t("none", ["research"])], missing) == []

    @pytest.mark.parametrize("raw", [
        "flase", "no", "0", "\"false\"", "maybe",           # 空白を含まない不正値 (従来から検出済み)
        "not a boolean",                                     # 空白入り文字列
        "[false, true]",                                     # リスト表記 (空白入り)
        "null",                                               # null
        "42",                                                 # 数値
    ])
    def test_a_malformed_capability_is_a_fail_not_silently_true_or_false(self, tmp_path, raw):
        """欠陥版 (`[^#\\s]*` で値を切る正規表現) に戻すと、空白を含む形 (`not a boolean` /
        `[false, true]`) は行全体がマッチせず欄が「無い」ものとして読み飛ばされ、
        `check_deliverable` は `pr` を通してしまう (このテストが赤になる)。
        """
        perms = _perms(tmp_path, f"  research:\n    can_produce_deliverable: {raw}\n    allow: []\n    deny: []\n")
        fails = _fails(lint_plan.check_deliverable([_t("pr", ["research"])], perms))
        assert len(fails) == 1 and "true / false ではありません" in fails[0], fails

    def test_a_mission_required_problem_fails_only_if_a_task_needs_the_answer(self, perms):
        assert lint_plan.check_deliverable([_t("pr")], perms, required_problem="boom") == []
        fails = _fails(lint_plan.check_deliverable([_t()], perms, required_problem="boom"))
        assert len(fails) == 1 and "boom" in fails[0], fails


# ---------------------------------------------------------------------------
# 3b. コメント付きヘッダが宣言を隠す/誤った skill に付く (2 巡目 t057→t059、PR#236 P2)
# ---------------------------------------------------------------------------

class TestCommentedHeadersDoNotHideOrMisassignCapabilities:
    """欠陥版 (手書きの行パーサ) に戻すと、これらは黙って `pr` を通してしまう
    (このクラスのテストが赤になる)。本物の YAML パーサに切り替えて構造ごと直した。
    """

    def _caps(self, tmp_path, content):
        path = tmp_path / "skill-permissions.yaml"
        path.write_text(content)
        caps, problem = lint_plan._load_deliverable_capabilities(str(path))
        assert problem is None, problem
        return caps

    def test_a_commented_skill_header_still_yields_its_own_declaration(self, tmp_path):
        """(1) `research: # read only` のようなコメント付き skill ヘッダ。"""
        caps = self._caps(tmp_path, (
            "skills:\n"
            "  code:\n    allow: []\n    deny: []\n"
            "  research: # read only\n"
            "    can_produce_deliverable: false\n    allow: []\n    deny: []\n"
        ))
        assert caps.get("research") is False
        assert "code" not in caps

    def test_a_commented_parent_skills_header_still_starts_the_skills_section(self, tmp_path):
        """(2) `skills: # permissions` のようなコメント付き親ヘッダ。"""
        caps = self._caps(tmp_path, (
            "skills: # permissions\n"
            "  research:\n    can_produce_deliverable: false\n    allow: []\n    deny: []\n"
        ))
        assert caps.get("research") is False

    def test_a_capability_after_a_commented_header_is_not_misassigned_to_the_previous_skill(self, tmp_path):
        """(3) コメント付きヘッダの後で capability が前の skill に付く誤り。

        欠陥版は `research: # read only` を「ヘッダでも他の何行でもない」として読み飛ばし、
        直後の `can_produce_deliverable: false` を直前の skill (`code`) の欄として拾ってしまう —
        `code` (実際は書いていない) が false になり、`research` 自身の false は失われる。
        """
        caps = self._caps(tmp_path, (
            "skills:\n"
            "  code:\n    allow: []\n    deny: []\n"
            "  research: # read only\n"
            "    can_produce_deliverable: false\n    allow: []\n    deny: []\n"
            "  review:\n    can_produce_deliverable: false\n    allow: []\n    deny: []\n"
        ))
        assert "code" not in caps, "code は宣言していないので辞書に載ってはいけない (誤って前の skill に付いた)"
        assert caps.get("research") is False
        assert caps.get("review") is False

    def test_a_quoted_string_value_is_rejected_not_coerced(self, tmp_path):
        """引用符付きの値 (`can_produce_deliverable: "false"`) — 拒否される (文字列のまま)。"""
        caps = self._caps(tmp_path, (
            'skills:\n  research:\n    can_produce_deliverable: "false"\n    allow: []\n    deny: []\n'
        ))
        assert caps.get("research") not in (True, False)

    def test_a_flow_style_value_is_read_as_a_real_boolean(self, tmp_path):
        """フロースタイル (`research: {can_produce_deliverable: false}`) — 真偽値として通る。"""
        caps = self._caps(tmp_path, "skills:\n  research: {can_produce_deliverable: false}\n")
        assert caps.get("research") is False

    def test_regression_t055_whitespace_and_list_values_still_fail(self, tmp_path):
        """(4) t055 が直した空白入り値・リスト値の回帰防止。"""
        for raw in ("not a boolean", "[false, true]"):
            path = tmp_path / f"p-{abs(hash(raw))}.yaml"
            path.write_text(f"skills:\n  research:\n    can_produce_deliverable: {raw}\n")
            caps, problem = lint_plan._load_deliverable_capabilities(str(path))
            assert problem is None
            assert caps.get("research") not in (True, False), raw

    @pytest.mark.parametrize("skills_value", [[], None])
    def test_regression_t055_empty_or_missing_skills_still_fail_the_lint(self, tmp_path, skills_value):
        """(5) t055 が直した `skills: []` と `skills` 欠落の回帰防止 (check_deliverable 経由)。"""
        meta = {"id": "t001", "title": "x", "status": "pending", "priority": "high", "deliverable": "pr"}
        if skills_value is not None:
            meta["skills"] = skills_value
        perms = _perms(tmp_path, PERMS)
        fails = _fails(lint_plan.check_deliverable([meta], perms))
        assert len(fails) == 1 and "skills" in fails[0], fails

    def test_a_skill_whose_value_is_not_a_mapping_makes_the_whole_load_unreadable(self, tmp_path):
        """1 skill の形が壊れているだけで、他の skill も含め config 全体を「読めない」にする
        (壊れた 1 件だけを黙って飛ばすと、その skill が絡む判定が気付かれずに緩む方向へ倒れる)。
        """
        path = tmp_path / "skill-permissions.yaml"
        path.write_text("skills:\n  research: not-a-mapping\n  code:\n    can_produce_deliverable: true\n")
        caps, problem = lint_plan._load_deliverable_capabilities(str(path))
        assert caps == {} and problem is not None

    def test_without_pyyaml_the_config_is_unreadable_not_permissively_empty(self, tmp_path, monkeypatch):
        """PyYAML が無い環境向けの簡易フォールバックは意図的に持たない — 「読めない」に倒す。"""
        monkeypatch.setattr(lint_plan, "yaml", None)
        path = tmp_path / "skill-permissions.yaml"
        path.write_text("skills:\n  research:\n    can_produce_deliverable: false\n")
        caps, problem = lint_plan._load_deliverable_capabilities(str(path))
        assert caps == {} and problem is not None and "PyYAML" in problem


class TestMissionMark:
    def _write(self, tmp_path, text):
        d = tmp_path / "missions" / "m"
        d.mkdir(parents=True)
        (d / "mission.yaml").write_text(text)
        return str(tmp_path)

    def test_true_is_required(self, tmp_path):
        q = self._write(tmp_path, "title: x\ndeliverable_required: true\nreview:\n  cycle_count: 0\n")
        assert lint_plan._mission_requires_deliverable("m", q) == (True, None)

    @pytest.mark.parametrize("text", ["title: x\n", "deliverable_required: false\n", "deliverable_required: null\n"])
    def test_no_mark_is_not_required(self, tmp_path, text):
        assert lint_plan._mission_requires_deliverable("m", self._write(tmp_path, text)) == (False, None)

    def test_a_missing_mission_yaml_is_the_only_absence_that_means_not_required(self, tmp_path):
        assert lint_plan._mission_requires_deliverable("m", str(tmp_path)) == (False, None)

    @pytest.mark.parametrize("value", ["maybe", "1", "\"true\""])
    def test_a_non_boolean_mark_is_a_problem_not_a_no(self, tmp_path, value):
        required, problem = lint_plan._mission_requires_deliverable(
            "m", self._write(tmp_path, f"deliverable_required: {value}\n"))
        assert required is False and problem and "true / false" in problem

    def test_an_unreadable_mission_yaml_is_a_problem_not_a_no(self, tmp_path):
        d = tmp_path / "missions" / "m"
        (d / "mission.yaml").mkdir(parents=True)          # 通常ファイルでない (IsADirectoryError)
        required, problem = lint_plan._mission_requires_deliverable("m", str(tmp_path))
        assert required is False and problem and "IsADirectoryError" in problem


# ---------------------------------------------------------------------------
# 4. lint (plan.sh lint の入口を通す)
# ---------------------------------------------------------------------------

class TestLintThroughThePlanShEntry:
    def test_an_old_mission_without_the_mark_is_not_stopped(self, sb):
        """印の無い mission は、宣言の無い card があっても deliverable 規則で FAIL しない。"""
        sb.card("t001", skills="[code]")
        sb.card("t002", skills="[research]")
        r = sb.lint()
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "deliverable" not in r.stdout

    def test_a_marked_mission_requires_a_declaration_on_every_card(self, sb):
        sb.mark_required()
        sb.card("t001", skills="[code]", extra=["deliverable: pr"])
        sb.card("t002", skills="[research]")
        r = sb.lint()
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert re.search(r"\[FAIL\] deliverable: task/t002: 'deliverable' が未宣言", r.stdout), r.stdout
        assert "task/t001: all checks passed" in r.stdout

    def test_a_marked_mission_with_every_declaration_passes(self, sb):
        sb.mark_required()
        sb.card("t001", skills="[code]", extra=["deliverable: pr"])
        sb.card("t002", skills="[research]", extra=["deliverable: none"])
        r = sb.lint()
        assert r.returncode == 0, (r.stdout, r.stderr)

    def test_pr_with_a_read_only_skill_fails_with_or_without_the_mark(self, sb):
        sb.card("t001", skills="[research]", extra=["deliverable: pr"])
        for marked in (False, True):
            if marked:
                sb.mark_required()
            r = sb.lint()
            assert r.returncode == 1, (marked, r.stdout, r.stderr)
            assert "can_produce_deliverable: false" in r.stdout

    def test_a_mixed_skill_list_passes(self, sb):
        sb.card("t001", skills="[research, code]", extra=["deliverable: pr"])
        assert sb.lint().returncode == 0

    def test_the_review_pre_step_uses_the_same_rule(self, sb):
        """`plan.sh review` は lint を前段に走らせる (FAIL は revise 扱い)。同じ関数を通ることを確かめる。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("lint_plan_sandbox", sb.root / "scripts" / "lint_plan.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sb.card("t001", skills="[review]", extra=["deliverable: file"])
        assert mod.lint_mission(MISSION, str(sb.queue), str(sb.root / "config")) == 1


# ---------------------------------------------------------------------------
# 5. plan.sh done
# ---------------------------------------------------------------------------

def _done(sb, task, *extra):
    return sb.run("done", task, "finished", "--mission", MISSION, *extra)


class TestDoneHonoursTheDeclaration:
    def _pr_task(self, sb, **kw):
        sb.card("t001", status="in_progress", worker="Ren", extra=["deliverable: pr"], **kw)

    def test_pr_without_a_flag_is_refused_with_exit_2_and_writes_nothing(self, sb):
        self._pr_task(sb)
        before = sb.snapshot()
        r = _done(sb, "t001")
        assert r.returncode == 2, (r.stdout, r.stderr)
        assert sb.snapshot() == before
        assert _field(sb, "t001", "status") == "in_progress"

    def test_the_refusal_names_the_way_out(self, sb):
        self._pr_task(sb)
        r = _done(sb, "t001")
        assert "deliverable: pr" in r.stderr
        assert "--pr <N>" in r.stderr and "--no-pr" in r.stderr and "update t001 --deliverable" in r.stderr

    def test_with_pr_it_passes_and_records_no_waiver(self, sb):
        self._pr_task(sb)
        r = _done(sb, "t001", "--pr", "12")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _field(sb, "t001", "status") == "done"
        assert _field(sb, "t001", "no_pr_waiver") is None

    def test_no_pr_waives_it_and_leaves_the_reason_on_the_card(self, sb):
        self._pr_task(sb)
        r = _done(sb, "t001", "--no-pr", "調べた結果、変更は不要だった")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _field(sb, "t001", "status") == "done"
        assert "no_pr_waiver: 調べた結果、変更は不要だった" in sb.text("t001")

    def test_pr_propagates_to_a_waiting_codex_review_as_before(self, sb):
        self._pr_task(sb)
        sb.card("t003", skills="[codex-review]", blocked_by="[t001]", status="blocked",
                extra=['blocked_reason: "PR 番号待ち"'])
        r = _done(sb, "t001", "--pr", "12")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _field(sb, "t003", "pr_number") == "12" and _field(sb, "t003", "status") == "pending"

    @pytest.mark.parametrize("declared", ["file", "none"])
    def test_file_and_none_need_neither_flag(self, sb, declared):
        sb.card("t001", status="in_progress", worker="Ren", extra=[f"deliverable: {declared}"])
        r = _done(sb, "t001")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _field(sb, "t001", "status") == "done"

    def test_a_card_without_the_field_behaves_as_before(self, sb):
        sb.card("t001", status="in_progress", worker="Ren")
        r = _done(sb, "t001")
        assert r.returncode == 0, (r.stdout, r.stderr)

    def test_a_card_without_the_field_is_still_refused_when_a_codex_review_waits(self, sb):
        sb.card("t001", status="in_progress", worker="Ren")
        sb.card("t003", skills="[codex-review]", blocked_by="[t001]", status="blocked",
                extra=['blocked_reason: "PR 番号待ち"'])
        before = sb.snapshot()
        r = _done(sb, "t001")
        assert r.returncode == 2 and sb.snapshot() == before

    @pytest.mark.parametrize("bad", ["PR", "pull", "maybe"])
    def test_an_unreadable_declaration_is_refused_not_read_as_undeclared(self, sb, bad):
        sb.card("t001", status="in_progress", worker="Ren", extra=[f"deliverable: {bad}"])
        before = sb.snapshot()
        r = _done(sb, "t001")
        assert r.returncode == 2, (r.stdout, r.stderr)
        assert "読めない値" in r.stderr and sb.snapshot() == before
        # 出口: 宣言を直す、または --no-pr
        assert sb.run("update", "t001", "--mission", MISSION, "--deliverable", "none").returncode == 0
        assert _done(sb, "t001").returncode == 0

    def test_pr_and_no_pr_together_are_still_refused(self, sb):
        self._pr_task(sb)
        before = sb.snapshot()
        r = _done(sb, "t001", "--pr", "1", "--no-pr", "x")
        assert r.returncode == 2 and sb.snapshot() == before
