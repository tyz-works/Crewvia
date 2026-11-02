#!/usr/bin/env python3
"""review skill の card が CI 完走待ちで watchdog の max に殺される事故の恒久対応 (t002 / 20261102-review-ci-timeout)。

## 背景

review Worker が PR の CI 完走を待っている間に watchdog の絶対上限 (feature_impl 既定 3600s)
で terminate される事故が 2 回起きた (2026-09-13 / 2026-11-02。pytest の実測所要は 3975s)。
`scripts/watchdog.py:850-857` の絶対上限は idle/executing 判定から独立した「最後の安全弁」で
あり続ける設計 (PR #204 / t016) — グローバル既定を上げる案はこの安全弁を review 以外の全 task
についても弱めるため却下した (t001 の Result / knowledge/review-ci-wait-timeout.md)。

## 選んだ対応 (案B)

`config/crewvia.yaml` の `model_per_skill:` と同じパターンで、`plan.sh add` が
`--max-timeout` を明示しない review skill の card に `timeout_max_per_skill:` の既定値
(7200s) を書き込む。明示値・review 以外の skill には一切触れない。
`plan.sh update` にも `--idle-timeout` / `--max-timeout` を新設し、frontmatter 直編集
(flow 形式で watchdog をクラッシュさせる罠がある) に依らず既存 card の上限を直せるようにした。

## このファイルが固定すること

1. review skill の card は既定で timeout.max=7200 を持つ (add)
2. 明示した --max-timeout は既定で上書きされない (add)
3. review を含まない card は何も変わらない — 回帰が無い (add)
4. 複数 skill が異なる既定値にマッチしたら最も緩い (大きい) 方を採る (add, カスタム config)
5. config が壊れている / 読めないときは既定を適用せず、Worker 起動を止めない (add)
6. plan.sh update は既存の timeout dict とマージする。片方だけ渡せばもう片方は保つ
7. plan.sh update は壊れた timeout (flow 形式が str として読まれた状態) を正しい block 形式で直す
8. 境界: 解決された timeout.max の手前と先で WorkerMonitor.check() の判定が変わる
   (review 既定 7200s と、既定が適用されない場合の feature_impl 既定 3600s の両方で)

赤の実証は tests/red_proof_t002_review_ci_timeout.sh (add の既定付与を取り除くと 1, 4 が落ちる)。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from fixture_tree import copy_plan_tree  # noqa: E402
from e5_autoname import AutoName  # noqa: E402
import lib_task_cards as task_cards  # noqa: E402
import watchdog  # noqa: E402

from test_watchdog_idle import _stub, _write_activity, AGENT, TASK_ID  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MISSION = "m-timeout"

#: Hugo の実際の事故: pytest 実測所要 (CLAUDE.md / t001 の Result と同じ値)
HUGO_PYTEST_SECONDS = 3975


class Sandbox:
    """`<root>/queue` と `<root>/registry` を持つ使い捨ての crewvia (plan.sh は隔離コピー)。

    `tests/test_plan_input_guards.py::Sandbox` と同じ型だが、`add()` が `--skills` を
    固定しない (このファイルは skills の組み合わせそのものを主題にするため)。
    """

    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        self.names = AutoName()
        (self.root / ".git").mkdir(parents=True)
        (self.root / "registry" / "mux").mkdir(parents=True)
        copy_plan_tree(self.root)
        shutil.copytree(REPO_ROOT / "config", self.root / "config")
        shutil.copytree(REPO_ROOT / "hooks", self.root / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.queue = self.root / "queue"
        self.state = self.queue / "state.yaml"

    def env(self):
        return {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE", str(pathlib.Path.home() / ".local")),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CREWVIA_REPO_ROOT": str(self.root),
            "CREWVIA_QUEUE": str(self.queue),
            "CREWVIA_TASKVIA": "disabled",
        }

    def plan(self, *args):
        args = self.names.before(args)
        r = subprocess.run(
            ["bash", str(self.root / "scripts" / "plan.sh"), *args],
            env=self.env(), cwd=str(self.root), capture_output=True, text=True, timeout=120)
        self.names.after(args, r.stdout, r.returncode)
        return r

    def ok(self, *args):
        r = self.plan(*args)
        assert r.returncode == 0, f"plan.sh {args}: rc={r.returncode}\n{r.stdout}\n{r.stderr}"
        return r

    def card_path(self, task):
        return self.queue / "missions" / MISSION / "tasks" / f"{task}.md"

    def meta(self, task):
        text = self.card_path(task).read_text()
        m, _body = task_cards.parse_frontmatter(text)
        return m

    def add(self, title, skills, *extra):
        extra = (*extra, "--deliverable", "none") if "--deliverable" not in extra else extra
        return self.ok("add", title, "--mission", MISSION, "--skills", skills, *extra)

    def set_config_timeout_map(self, mapping_lines):
        """`config/crewvia.yaml` の `timeout_max_per_skill:` を丸ごと差し替える
        (テストの既定値を実配置の値に依存させないための隔離。`model_per_skill` の
        差し替えテスト (test_review_plan_model.sh Test 3) と同じ考え方)。"""
        path = self.root / "config" / "crewvia.yaml"
        text = path.read_text()
        start = text.index("timeout_max_per_skill:")
        # 次の空行まで (このブロックの終わり) を切り出して置換する
        end = text.index("\n\n", start)
        block = "timeout_max_per_skill:\n" + "".join(f"  {line}\n" for line in mapping_lines)
        path.write_text(text[:start] + block + text[end + 1:])


@pytest.fixture
def sb(tmp_path):
    s = Sandbox(tmp_path)
    s.ok("init", "timeout work", "--mission", MISSION)
    return s


# ---------------------------------------------------------------------------
# 1〜3: plan.sh add の既定付与
# ---------------------------------------------------------------------------

def test_review_skill_gets_default_timeout_max(sb):
    """review skill の card は既定で timeout.max=7200 を持つ。"""
    sb.add("review this", "review")
    meta = sb.meta("t001")
    assert meta.get("timeout") == {"max": 7200}, meta


def test_explicit_max_timeout_is_not_overridden(sb):
    """--max-timeout を明示したら既定値は適用されない (陽性対照: 既定の値と違うことを確かめる)。"""
    sb.add("review this", "review", "--max-timeout", "1800")
    meta = sb.meta("t001")
    assert meta.get("timeout") == {"max": 1800}, meta


def test_non_review_skills_are_unaffected(sb):
    """review を含まない card には何も書かれない (回帰が無いことの陽性対照)。"""
    sb.add("implement this", "bash,code")
    meta = sb.meta("t001")
    assert meta.get("timeout") is None, meta


def test_review_mixed_with_other_skills_still_gets_default(sb):
    """review が他の skill と混在していても既定が付く (部分一致でよい)。"""
    sb.add("review and fix", "review,code")
    meta = sb.meta("t001")
    assert meta.get("timeout") == {"max": 7200}, meta


def test_idle_timeout_flag_is_independent_of_the_default(sb):
    """--idle-timeout だけを明示しても review の既定 max は普通に乗る (マージ)。"""
    sb.add("review this", "review", "--idle-timeout", "600")
    meta = sb.meta("t001")
    assert meta.get("timeout") == {"idle": 600, "max": 7200}, meta


# ---------------------------------------------------------------------------
# 4: 複数 skill の解決 (最も緩い方)
# ---------------------------------------------------------------------------

def test_multiple_matching_skills_pick_the_more_lenient_value(sb):
    """複数 skill が異なる既定値にマッチしたら、max が大きい方を採る。"""
    sb.set_config_timeout_map(["review: 7200", "qa: 9000"])
    sb.add("review and verify", "review,qa")
    meta = sb.meta("t001")
    assert meta.get("timeout") == {"max": 9000}, meta

    # 逆順でも同じ (どちらが先でも最大を採る)
    sb.add("verify and review", "qa,review")
    meta2 = sb.meta("t002")
    assert meta2.get("timeout") == {"max": 9000}, meta2


# ---------------------------------------------------------------------------
# 5: config が壊れている / 読めない
# ---------------------------------------------------------------------------

def test_broken_config_does_not_block_add(sb):
    """`timeout_max_per_skill` の型が壊れていても add は失敗せず、既定も適用しない。"""
    path = sb.root / "config" / "crewvia.yaml"
    text = path.read_text()
    start = text.index("timeout_max_per_skill:")
    end = text.index("\n\n", start)
    # review: の値を非数値にする (壊れた型)
    path.write_text(text[:start] + "timeout_max_per_skill:\n  review: not-a-number\n" + text[end + 1:])
    sb.add("review this", "review")
    meta = sb.meta("t001")
    assert meta.get("timeout") is None, meta


def test_missing_config_file_does_not_block_add(sb):
    """config/crewvia.yaml が無くても add は動き、既定は適用されない (Unreadable を既定値に倒さず、
    かつ Worker 起動を止めない — 両方の fail-open/fail-closed を両立する設計の確認)。"""
    (sb.root / "config" / "crewvia.yaml").unlink()
    sb.add("review this", "review")
    meta = sb.meta("t001")
    assert meta.get("timeout") is None, meta


# ---------------------------------------------------------------------------
# 6〜7: plan.sh update --idle-timeout / --max-timeout
# ---------------------------------------------------------------------------

def test_update_max_timeout_merges_with_existing_idle(sb):
    sb.add("review this", "review", "--idle-timeout", "600", "--max-timeout", "1800")
    sb.ok("update", "t001", "--mission", MISSION, "--max-timeout", "9999")
    meta = sb.meta("t001")
    assert meta.get("timeout") == {"idle": 600, "max": 9999}, meta


def test_update_idle_timeout_merges_with_existing_max(sb):
    sb.add("review this", "review")  # timeout: {max: 7200} (既定)
    sb.ok("update", "t001", "--mission", MISSION, "--idle-timeout", "300")
    meta = sb.meta("t001")
    assert meta.get("timeout") == {"idle": 300, "max": 7200}, meta


def test_update_fixes_a_broken_flow_style_timeout(sb):
    """flow 形式 `timeout: {max: 7200}` は parse_yaml に文字列として読まれ、watchdog の
    `timeout.get("max")` が AttributeError になる罠がある (t001 の確認済みの事実)。
    `plan.sh update --max-timeout` はこの壊れた card を正しい block 形式で書き直す。"""
    sb.add("review this", "review", "--deliverable", "none")
    path = sb.card_path("t001")
    text = path.read_text()
    broken = text.replace("timeout:\n  max: 7200", "timeout: {max: 7200}")
    assert broken != text, "fixture の前提 (既定の block 形式) が崩れている"
    path.write_text(broken)

    # 壊れた状態を確認: timeout は dict ではなく文字列として読まれる
    broken_meta = sb.meta("t001")
    assert isinstance(broken_meta.get("timeout"), str), broken_meta

    sb.ok("update", "t001", "--mission", MISSION, "--max-timeout", "7200")
    fixed_meta = sb.meta("t001")
    assert fixed_meta.get("timeout") == {"max": 7200}, fixed_meta

    # watchdog が実際にこの card を解決できることまで確かめる (壊れていれば timeout.get で例外)
    task_card = {"worker": AGENT, "timeout": fixed_meta["timeout"]}
    monitor = watchdog.WorkerMonitor(task_id=TASK_ID, task_card=task_card,
                                     profiles=watchdog.PROFILES, repo_root=path.parent)
    assert monitor.max_threshold == 7200


# ---------------------------------------------------------------------------
# 8: 境界 — WorkerMonitor.check() が実際に変わる (両側)
# ---------------------------------------------------------------------------

def _monitor_from_card_timeout(tmp_path, timeout_meta):
    """card の `timeout:` (dict または None) から、実運用と同じ経路 (WorkerMonitor の
    コンストラクタ) で max_threshold を解決した monitor を組み立てる。"""
    task_card = {"worker": AGENT, "timeout": timeout_meta} if timeout_meta else {"worker": AGENT}
    monitor = watchdog.WorkerMonitor(task_id=TASK_ID, task_card=task_card,
                                     profiles=watchdog.PROFILES, repo_root=tmp_path)
    return _stub(monitor)


def test_review_default_keeps_a_1h6m_ci_wait_alive(tmp_path):
    """Hugo の事故の再現と恒久対応の確認: review 既定 (max=7200) なら、実測の CI 所要
    (3975s) はまだ terminate されない (境界の手前)。"""
    monitor = _monitor_from_card_timeout(tmp_path, {"max": 7200})
    _write_activity(tmp_path, age_seconds=5)
    monitor.started_at = time.time() - HUGO_PYTEST_SECONDS
    assert monitor.check() == "alive"


def test_review_default_boundary_still_terminates_past_it(tmp_path):
    """境界の先: 7200s を超えたら review 既定でも terminate する (安全弁は壊していない)。"""
    monitor = _monitor_from_card_timeout(tmp_path, {"max": 7200})
    monitor.started_at = time.time() - 7201
    assert monitor.check() == "terminate"


def test_without_the_default_the_same_ci_wait_would_be_killed(tmp_path):
    """陽性対照 (regression guard): timeout.max が無い card (= 既定付与が外れた状態と同じ)
    は、同じ CI 待ち (3975s) で feature_impl 既定 (3600s) に terminate される。
    この挙動自体は変えていない — review skill の card にだけ既定を足したことの対照。"""
    monitor = _monitor_from_card_timeout(tmp_path, None)
    assert monitor.max_threshold == watchdog.PROFILES["feature_impl"]["max"] == 3600
    monitor.started_at = time.time() - HUGO_PYTEST_SECONDS
    assert monitor.check() == "terminate"
