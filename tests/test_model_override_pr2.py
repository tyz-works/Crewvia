"""モデルの段上げ PR-2 (`model_override` の欄と Worker の束ね)。設計: knowledge/model-escalation.md §4.3・§4.4・§4.5・§4.8・§5・§8 (PR-2)。

- lib_model: `ESCALATION_LADDER` / `escalate` / `effective_model`
- lib_worker_model.worker_may_take: §4.3 の表を総当たり + dispatcher と plan.sh pull が**同じ関数**を呼ぶ (AST)
- dispatcher: 割り当て・退役 (§4.3 の 5) の総当たり・no_worker の文面・壊れた override
- plan.sh: update --model-override / model-for / pull の拒否 / lint / store-check / status
- start.sh: CREWVIA_BIND_TASK (bats は tests/start-sh-model-bind.bats)
"""

from __future__ import annotations

import ast
import itertools
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "tests"))

import lib_model as lm  # noqa: E402
import lib_retirement  # noqa: E402
import lib_worker_model as wm  # noqa: E402
import lib_worker_target as wt  # noqa: E402
import test_dispatcher_retirement_exclusion as base  # noqa: E402
from lib_task_cards import Unreadable  # noqa: E402

AGENT = base.AGENT
SLUG = base.SLUG


# ---------------------------------------------------------------------------
# lib_model
# ---------------------------------------------------------------------------

def test_ladder_is_the_single_definition():
    assert lm.ESCALATION_LADDER == ("haiku", "sonnet", "opus")
    for ok in lm.ESCALATION_LADDER:
        assert lm.is_alias(ok)
    for bad in ("Opus", "opus ", "claude-opus-4-1", "", None, True, 3, ["opus"]):
        assert not lm.is_alias(bad)


@pytest.mark.parametrize("model,alias", [
    ("opus", "opus"), ("claude-sonnet-4-5", "sonnet"), ("HAIKU", "haiku"),
    ("fable", None), ("", None), (None, None), (5, None),
])
def test_alias_of(model, alias):
    assert lm.alias_of(model) == alias


@pytest.mark.parametrize("base_model,override,expected", [
    ("haiku", None, "sonnet"), ("sonnet", None, "opus"), ("opus", None, "opus"),
    ("claude-sonnet-4-5", None, "opus"),
    ("haiku", "opus", "opus"),            # 既にある override より下げない
    ("haiku", "sonnet", "sonnet"),
    ("sonnet", "haiku", "opus"),
    ("fable", None, None),                # 起点が分からず override も無い = 段上げしない
    ("fable", "sonnet", "sonnet"),
    (None, "haiku", "haiku"),
])
def test_escalate(base_model, override, expected):
    assert lm.escalate(base_model, override) == expected


def test_escalation_base_skips_unknown_names():
    assert lm.escalation_base("fable", None, "sonnet") == "sonnet"
    assert lm.escalation_base("claude-opus-4-1", "haiku", "sonnet") == "claude-opus-4-1"
    assert lm.escalation_base("fable", None, "") is None


def test_effective_model_prefers_override_and_refuses_a_broken_one(tmp_path):
    cfg = tmp_path / "crewvia.yaml"
    cfg.write_text("worker_model: sonnet\nmodel_per_skill:\n  docs: haiku\n")
    assert lm.effective_model(str(cfg), ["docs"]) == "haiku"
    assert lm.effective_model(str(cfg), "docs", "opus") == "opus"
    with pytest.raises(ValueError):
        lm.effective_model(str(cfg), ["docs"], "claude-opus-4-1")


# ---------------------------------------------------------------------------
# worker_may_take の総当たり (§4.3 の表)
# ---------------------------------------------------------------------------

ME = f"{SLUG}/t001"


def _rec(state, *, bound=None, source="skill", model="sonnet"):
    if state == "missing":
        return Unreadable("x", "no such file", errno=2)
    if state == "unreadable":
        return Unreadable("x", "broken", errno=None)
    return {"agent": "A", "model": model, "source": source, "bound_task": bound, "written_at": 1}


def _expected(task_override, rec_state, bound, source, model):
    """表を別の書き方 (全行) で書き下す。実装の分岐を写さず、1 行ずつ。"""
    if task_override == "broken":
        return False
    if task_override is None:
        if rec_state == "missing":
            return True
        if rec_state == "unreadable":
            return False
        return bound in (None, ME)
    if rec_state in ("missing", "unreadable"):
        return False
    if bound == ME:
        return True
    if bound is not None:
        return False
    rank = {"haiku": 1, "sonnet": 2, "opus": 3}
    have = next((a for a in rank if model and a in model), None)
    return source == "env" and have is not None and rank[have] >= rank[task_override]


@pytest.mark.parametrize("task_override,rec_state,bound,source,model", list(itertools.product(
    [None, "haiku", "sonnet", "opus", "broken"],
    ["missing", "unreadable", "known"],
    [None, ME, f"{SLUG}/t002", "other-mission/t001"],
    ["env", "skill", "override", "none"],
    ["haiku", "sonnet", "opus", "fable", None],
)))
def test_worker_may_take_matches_the_table(task_override, rec_state, bound, source, model):
    meta = {"id": "t001"}
    if task_override == "broken":
        meta["model_override"] = "opus-4"
    elif task_override:
        meta["model_override"] = task_override
    rec = _rec(rec_state, bound=bound, source=source, model=model)
    ok, why = wm.worker_may_take(rec, meta, SLUG)
    assert ok == _expected(task_override, rec_state, bound, source, model)
    assert (why == "") == ok          # 可のときだけ理由が空


@pytest.mark.parametrize("bad", [None, "", "Opus", "claude-opus-4-1", 3, True, ["opus"], {"a": 1}])
def test_a_present_but_unusable_override_is_never_taken(bad):
    rec = _rec("known", bound=ME, source="env", model="opus")
    assert wm.worker_may_take(rec, {"id": "t001", "model_override": bad}, SLUG)[0] is False
    assert wm.override_of({"model_override": bad})[1]


def test_worker_may_take_for_reads_the_record_inside_the_lib(tmp_path):
    reg = tmp_path / "registry"
    meta = {"id": "t001", "model_override": "opus"}
    assert wm.worker_may_take_for(reg, "Ren", meta, SLUG)[0] is False            # 記録が無い
    assert wm.worker_may_take_for(reg, "", meta, SLUG)[0] is False                # --agent なし
    assert wm.worker_may_take_for(reg, "", {"id": "t001"}, SLUG)[0] is True
    wm.write_record(reg, "Ren", "override", "opus", ME)
    assert wm.worker_may_take_for(reg, "Ren", meta, SLUG)[0] is True
    assert wm.worker_may_take_for(reg, "Ren", {"id": "t002"}, SLUG)[0] is False    # 束ねた Worker は他の task を取らない
    p = wm.record_path(reg, "Ren")
    p.write_text("{ nope")
    assert wm.worker_may_take_for(reg, "Ren", {"id": "t002"}, SLUG)[0] is False    # 読めない = 保留


# ---------------------------------------------------------------------------
# dispatcher と plan.sh が同じ判定を呼ぶ (コピーさせない)
# ---------------------------------------------------------------------------

def _calls(src, attr):
    tree = ast.parse(src)
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr]


def test_dispatcher_and_pull_call_the_one_shared_function():
    dsrc = re.search(r"<<'PYEOF'\n(.*)\nPYEOF", (SCRIPTS / "dispatcher.sh").read_text(), re.S).group(1)
    assert len(_calls(dsrc, "worker_may_take")) == 2          # target + model の AND (1 関数 worker_take_verdict の中)
    assert any(c.func.value.id == "_worker_model" for c in _calls(dsrc, "worker_may_take"))
    psrc = (SCRIPTS / "plan.sh").read_text()
    assert psrc.count("_WORKER_MODEL.worker_may_take_for(") == 3     # --task の照合 + 自動の絞り込み + 予約の再開
    # 判定の表をコピーしない: plan.sh / dispatcher.sh に bound_task の比較が無い
    assert "bound_task" not in psrc
    assert "'bound_task'" not in dsrc


def test_dispatcher_takes_target_before_model():
    dsrc = re.search(r"<<'PYEOF'\n(.*)\nPYEOF", (SCRIPTS / "dispatcher.sh").read_text(), re.S).group(1)
    fn = next(n for n in ast.walk(ast.parse(dsrc)) if isinstance(n, ast.FunctionDef) and n.name == "worker_take_verdict")
    order = [c.func.value.id for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == "worker_may_take"]
    assert order == ["_worker_target", "_worker_model"]


# ---------------------------------------------------------------------------
# dispatcher の 1 サイクル (割り当て・退役)
# ---------------------------------------------------------------------------

def _set_override(root, value):
    p = root / "queue" / "missions" / SLUG / "tasks" / "t001.md"
    t = p.read_text()
    p.write_text(t.replace("target_dir: null\n", f"target_dir: null\nmodel_override: {value}\n", 1))


def _cycle(root, extra_windows=()):
    mux = base.FakeMux([base.WINDOW, *extra_windows])
    base._load_dispatcher(root, mux)["dispatch"]()
    return mux


def _assigned(mux):
    return any("plan pull --task t001" in m for _t, m in mux.sent)


def _retired(root):
    return lib_retirement.request_path(root / "registry", AGENT).exists()


def _log(root):
    p = root / "dispatcher.log"
    return p.read_text() if p.exists() else ""


def _put_model_record(root, *, bound=None, source="skill", model="sonnet"):
    wm.write_record(root / "registry", AGENT, source, model, bound)


def _put_raw(root, text):
    p = wm.record_path(root / "registry", AGENT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_plain_task_is_still_assigned_to_a_worker_with_no_model_record(tmp_path):
    root = base._build_repo(tmp_path)
    mux = _cycle(root)
    assert _assigned(mux) and not _retired(root)


def test_default_worker_does_not_take_an_escalated_task(tmp_path):
    root = base._build_repo(tmp_path)
    _set_override(root, "opus")
    _put_model_record(root)            # skill の既定 sonnet・束ねなし
    mux = _cycle(root)
    assert not _assigned(mux)


def test_bound_worker_takes_its_task_and_nothing_else(tmp_path):
    root = base._build_repo(tmp_path)
    _set_override(root, "opus")
    _put_model_record(root, bound=f"{SLUG}/t001", source="override", model="opus")
    assert _assigned(_cycle(root))
    # 束ねた task が override でも別の task に束ねられていれば取らない
    root2 = base._build_repo(tmp_path / "b")
    _put_model_record(root2, bound=f"{SLUG}/t009", source="override", model="opus")
    assert not _assigned(_cycle(root2))


def test_escalated_task_is_taken_by_a_human_started_high_model_worker(tmp_path):
    root = base._build_repo(tmp_path)
    _set_override(root, "opus")
    _put_model_record(root, source="env", model="opus")
    assert _assigned(_cycle(root))
    root2 = base._build_repo(tmp_path / "b")
    _set_override(root2, "opus")
    _put_model_record(root2, source="env", model="sonnet")
    assert not _assigned(_cycle(root2))


def test_no_worker_message_carries_bind_command_model_and_exclude(tmp_path):
    root = base._build_repo(tmp_path)
    _set_override(root, "opus")
    _put_model_record(root)
    mux = _cycle(root)
    msgs = [m for _t, m in mux.sent if "起動コマンド" in m]
    assert msgs, mux.sent
    m = msgs[0]
    assert f"CREWVIA_BIND_TASK={SLUG}/t001" in m
    assert f"--fresh --exclude {AGENT}" in m
    assert "opus（段上げ）" in m
    assert "CREWVIA_WORKER_MODEL" not in m
    assert "モデル" in m            # 担当できない理由 (model) も出る


def _put_roster(root, entries):
    """registry の名簿を lib_registry の書き込み口で作る (生の write_text は構造ガードが検出する)。"""
    import lib_registry
    by_name = {n: {"name": n, "skills": sk, "task_count": 0, "last_active": "", **({"role": r} if r else {})}
               for n, sk, r in entries}
    lib_registry.write(str(root / "registry" / "workers.yaml"), "", [n for n, _s, _r in entries], by_name)


def test_no_worker_message_for_a_plain_task_has_no_bind(tmp_path):
    root = base._build_repo(tmp_path)
    # 担当できる Worker がいない (skill 違い) 平文の task
    _put_roster(root, [("Other", ["docs"], None)])
    mux = base.FakeMux([])
    base._load_dispatcher(root, mux)["dispatch"]()
    msgs = [m for _t, m in mux.sent if "起動コマンド" in m]
    for m in msgs:
        assert "CREWVIA_BIND_TASK" not in m


# --- 退役の総当たり (§4.3 の 5) -----------------------------------------------------

def _repo(tmp_path, *, tasks, target_rec, model_rec):
    """tasks: [(id, override|None, bound_to_other?)]。t001 を上書きし、残りを足す。"""
    root = base._build_repo(tmp_path)
    tdir = root / "queue" / "missions" / SLUG / "tasks"
    # 取れない task だけにするため、既定の t001 は消して指定のものを置く
    (tdir / "t001.md").unlink()
    for tid, ov in tasks:
        extra = f"model_override: {ov}\n" if ov else ""
        (tdir / f"{tid}.md").write_text(
            f"---\nid: {tid}\ntitle: {tid}\nskills: [code]\npriority: high\nstatus: pending\n"
            f"blocked_by: [t999]\ntarget_dir: null\n{extra}worker: null\nstarted_at: null\ncompleted_at: null\n---\n\n"
            "## Description\nw\n\n## Result\n")
    (tdir / "t999.md").write_text(
        "---\nid: t999\ntitle: blocker\nskills: [docs]\npriority: low\nstatus: in_progress\nblocked_by: []\n"
        "target_dir: null\nworker: SomeoneElse\nstarted_at: null\ncompleted_at: null\n---\n\n## Description\nw\n\n## Result\n")
    if target_rec == "ok":
        wt.write_record(root / "registry", AGENT, None)
    elif target_rec == "broken":
        p = wt.record_path(root / "registry", AGENT)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ bad")
    if model_rec == "ok_plain":
        _put_model_record(root)
    elif model_rec == "ok_bound_other":
        _put_model_record(root, bound=f"{SLUG}/t008", source="override", model="opus")
    elif model_rec == "ok_bound_self":
        _put_model_record(root, bound=f"{SLUG}/t001", source="override", model="opus")
    elif model_rec == "broken_json":
        _put_raw(root, "{ nope")
    elif model_rec == "bad_shape":
        _put_raw(root, json.dumps({"agent": AGENT, "model": 5, "source": "env", "bound_task": None, "written_at": 1}))
    elif model_rec == "dir":
        wm.record_path(root / "registry", AGENT).mkdir(parents=True)
    elif model_rec == "symlink":
        p = wm.record_path(root / "registry", AGENT)
        p.parent.mkdir(parents=True, exist_ok=True)
        (root / "elsewhere.json").write_text("{}")      # 実在するファイルへの symlink (形が違う = 読めない)。宙に浮いた symlink は ENOENT = 「無い」
        p.symlink_to(root / "elsewhere.json")
    return root


TASK_SETS = {
    "none": [],
    "plain": [("t001", None)],
    "override_only": [("t001", "opus")],
    "bound_self": [("t001", "opus")],
    "bound_other": [("t002", None)],
}


@pytest.mark.parametrize("target_rec", ["ok", "missing", "broken"])
@pytest.mark.parametrize("model_rec", ["missing", "ok_plain", "ok_bound_self", "ok_bound_other",
                                       "broken_json", "bad_shape", "dir", "symlink"])
@pytest.mark.parametrize("tasks", ["none", "plain", "override_only", "bound_other"])
def test_idle_worker_retirement_matrix(tmp_path, tasks, model_rec, target_rec):
    """読めない記録が 1 つでもあって task が残っていれば退役しない。無い記録 × override の task だけ → 退役。"""
    root = _repo(tmp_path, tasks=TASK_SETS[tasks], target_rec=target_rec, model_rec=model_rec)
    mux = _cycle(root)
    rec = {"missing": _rec("missing"),
           "ok_plain": _rec("known"),
           "ok_bound_self": _rec("known", bound=f"{SLUG}/t001", source="override", model="opus"),
           "ok_bound_other": _rec("known", bound=f"{SLUG}/t008", source="override", model="opus")}.get(
               model_rec, _rec("unreadable"))
    model_known = model_rec in ("missing", "ok_plain", "ok_bound_self", "ok_bound_other")
    target_known = target_rec == "ok"      # 従来: TARGET_DIR の記録は「無い」も待機側 (_rec_known)
    # target_dir なしの task は、target の記録が何でも lib_worker_target では取れる (従来の表)。モデルの表だけが効く
    any_takeable = any(wm.worker_may_take(rec, {"id": t, **({"model_override": o} if o else {})}, SLUG)[0]
                       for t, o in TASK_SETS[tasks])
    if not TASK_SETS[tasks]:
        expect_retired = True                       # task が無ければ今どおり退役
    elif any_takeable:
        expect_retired = False
    else:
        expect_retired = target_known and model_known   # 観測できなかったものを根拠に破壊しない
    assert _retired(root) == expect_retired, (tasks, model_rec, target_rec, mux.sent, _log(root))


def test_unreadable_model_record_waits_with_its_own_log_line(tmp_path):
    root = _repo(tmp_path, tasks=TASK_SETS["plain"], target_rec="ok", model_rec="broken_json")
    _cycle(root)
    log = _log(root)
    assert not _retired(root)
    assert "[model]" in log and "model.json" in log
    assert "TARGET_DIR の記録が無い/読めない" not in log      # 原因で文面を分ける


def test_unreadable_target_record_keeps_its_own_log_line(tmp_path):
    root = _repo(tmp_path, tasks=[("t001", None)], target_rec="broken", model_rec="ok_bound_other")
    _cycle(root)
    log = _log(root)
    assert not _retired(root)
    assert "TARGET_DIR の記録が無い/読めない" in log


def test_missing_record_with_only_an_escalated_task_is_retired(tmp_path):
    root = _repo(tmp_path, tasks=[("t001", "opus")], target_rec="ok", model_rec="missing")
    _cycle(root)
    assert _retired(root)


def test_bound_worker_that_let_go_of_its_task_is_retired(tmp_path):
    root = _repo(tmp_path, tasks=[("t002", None)], target_rec="ok", model_rec="ok_bound_other")
    _cycle(root)
    assert _retired(root)


def test_second_bound_worker_with_the_task_already_taken_is_retired(tmp_path):
    root = _repo(tmp_path, tasks=[], target_rec="ok", model_rec="ok_bound_self")
    _cycle(root)
    assert _retired(root)


# --- 壊れた override ---------------------------------------------------------------

def test_broken_override_is_not_assigned_and_the_director_is_told_once(tmp_path):
    root = base._build_repo(tmp_path)
    _set_override(root, "opus-4")
    _put_model_record(root, source="env", model="opus")
    # Director が生きている構成
    _put_roster(root, [(AGENT, ["code"], None), ("Dirk", [], "director")])
    mux = base.FakeMux([base.WINDOW, "Dirk-director"])
    ns = base._load_dispatcher(root, mux)
    ns["dispatch"]()
    assert not _assigned(mux)
    told = [m for _t, m in mux.sent if "[model-override-invalid]" in m]
    if told:          # Director 名の解決は環境依存。届いたなら 1 回だけ + 直し方が出る
        assert len(told) == 1 and "--model-override" in told[0]
        ns["dispatch"]()
        assert len([m for _t, m in mux.sent if "[model-override-invalid]" in m]) == 1
    assert not any("起動コマンド" in m and "要求スキル" in m for _t, m in mux.sent)   # no_worker を重ねない


# ---------------------------------------------------------------------------
# plan.sh (update / model-for / pull / lint / store-check / status)
# ---------------------------------------------------------------------------

from execution_e3_helpers import MISSION, Box  # noqa: E402


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path / "root", tasks=("t001",))
    (b.root / "config").mkdir(exist_ok=True)
    (b.root / "config" / "crewvia.yaml").write_text(
        "worker_model: sonnet\nmodel_per_skill:\n  code: haiku\n  review: opus\n")
    return b


def _update(box, *args):
    return box.plan("update", "t001", "--mission", MISSION, *args, agent="Director")


def test_update_model_override_sets_and_clears(box):
    r = _update(box, "--model-override", "opus")
    assert r.returncode == 0, r.stderr
    assert box.card()["model_override"] == "opus"
    assert "model_override=opus" in r.stdout
    r = _update(box, "--model-override", "none")
    assert r.returncode == 0, r.stderr
    assert "model_override" not in box.card()


@pytest.mark.parametrize("bad", ["Opus", "claude-opus-4-1", "", "null", "fable", "opus,sonnet"])
def test_update_model_override_refuses_a_non_alias_and_writes_nothing(box, bad):
    before = box.snapshot()
    r = _update(box, "--model-override", bad)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert box.snapshot() == before


def test_update_model_override_refuses_a_codex_review_card_and_writes_nothing(box):
    assert _update(box, "--skills", "codex-review").returncode == 0
    before = box.snapshot()
    r = _update(box, "--model-override", "opus")
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "codex-review" in r.stderr
    assert box.snapshot() == before
    assert _update(box, "--model-override", "none").returncode == 0     # 消す口は残す


def test_dispatcher_holds_a_codex_review_card_that_has_an_override(tmp_path):
    root = base._build_repo(tmp_path)
    p = root / "queue" / "missions" / SLUG / "tasks" / "t001.md"
    t = p.read_text()
    assert "- code" in t or "skills" in t
    t = re.sub(r"(skills:\s*\[)[^\]]*(\])", r"\1codex-review\2", t, count=1) if "skills: [" in t \
        else re.sub(r"(skills:\n)(\s*- .*\n)+", r"\1  - codex-review\n", t, count=1)
    p.write_text(t)
    _set_override(root, "opus")
    spawned = []
    mux = base.FakeMux([base.WINDOW])
    ns = base._load_dispatcher(root, mux)
    ns["handle_codex_review"] = lambda *a, **k: spawned.append(a)
    ns["dispatch"]()
    assert not spawned
    assert not _assigned(mux)


def test_update_model_override_also_clears_the_at_cap_fields(box):
    assert _update(box, "--model-override", "opus").returncode == 0
    # PR-3 が書く欄 (書き手は無い) を手で置き、none で一緒に消えること
    p = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    t = p.read_text().replace("model_override: opus\n", "model_override: opus\nmodel_at_cap_count: 2\n")
    p.write_text(t)
    assert _update(box, "--model-override", "none").returncode == 0
    assert "model_at_cap_count" not in box.card()


def test_model_for(box):
    r = box.plan("model-for", "t001", "--mission", MISSION)
    assert (r.returncode, r.stdout.strip()) == (0, "haiku"), r.stderr         # skills code → config の haiku
    assert _update(box, "--model-override", "opus").returncode == 0
    r = box.plan("model-for", "t001", "--mission", MISSION)
    assert (r.returncode, r.stdout.strip()) == (0, "opus")


@pytest.mark.parametrize("args", [
    ("t999", "--mission", MISSION),               # card が無い
    ("t001", "--mission", "../etc"),              # 識別子が使えない
    ("t001", "--mission", "no-such-mission"),
    ("../t001", "--mission", MISSION),
    ("t001",),                                    # --mission なし
])
def test_model_for_cannot_decide_exit_1_and_stdout_empty(box, args):
    r = box.plan("model-for", *args)
    assert r.returncode == 1 and r.stdout == "", (r.returncode, r.stdout, r.stderr)


def test_model_for_with_a_broken_override_does_not_fall_back_to_the_default(box):
    p = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    p.write_text(p.read_text().replace("target_dir: null\n", "target_dir: null\nmodel_override: opus-4\n", 1))
    r = box.plan("model-for", "t001", "--mission", MISSION)
    assert r.returncode == 1 and r.stdout == ""


def test_model_for_source_says_override_only_when_the_card_has_one(box):
    r = box.plan("model-for", "t001", "--mission", MISSION, "--source")
    assert (r.returncode, r.stdout.split()) == (0, ["haiku", "skill"]), r.stderr
    assert _update(box, "--model-override", "opus").returncode == 0
    r = box.plan("model-for", "t001", "--mission", MISSION, "--source")
    assert (r.returncode, r.stdout.split()) == (0, ["opus", "override"])


def test_dispatcher_logs_a_model_refusal_under_its_own_key():
    src = (SCRIPTS / "dispatcher.sh").read_text()
    assert "_skip_{agent_name}_{slug}" in src and "worker_take_verdict" in src


def test_model_for_writes_nothing(box):
    before = box.snapshot()
    box.plan("model-for", "t001", "--mission", MISSION)
    assert box.snapshot() == before


def _bind(box, agent, bound, model="opus", source="override"):
    wm.write_record(box.root / "registry", agent, source, model, bound)


def test_pull_task_refused_for_a_default_worker_and_nothing_is_written(box):
    assert _update(box, "--model-override", "opus").returncode == 0
    _bind(box, "Ren", None, "haiku", "skill")
    before = box.snapshot()
    p = box.pull("Ren")
    assert p.returncode == 3, (p.returncode, p.stderr)
    assert "CREWVIA_BIND_TASK" in p.stderr and "opus" in p.stderr
    assert box.snapshot() == before


def test_pull_task_refused_when_the_workers_record_is_missing_for_an_escalated_task(box):
    assert _update(box, "--model-override", "sonnet").returncode == 0
    assert box.pull("Ren").returncode == 3


def test_pull_task_allowed_for_the_bound_worker_and_json_carries_the_override(box):
    assert _update(box, "--model-override", "opus").returncode == 0
    _bind(box, "Ren", f"{MISSION}/t001")
    p = box.pull("Ren")
    assert p.returncode == 0, p.stderr
    res = json.loads(p.stdout)
    assert res["model_override"] == "opus" and res["execution_model"] == "opus"


def test_pull_json_has_no_override_field_for_a_plain_task(box):
    res = json.loads(box.pull("Ren").stdout)
    assert "model_override" not in res


def test_bound_worker_refuses_a_different_task_and_an_unreadable_record_holds(box):
    _bind(box, "Ren", f"{MISSION}/t009")
    assert box.pull("Ren").returncode == 3                       # --task で別の task
    r = box.pull("Ren", auto=True)
    assert r.returncode == 2, (r.returncode, r.stderr)           # 自動選択でも取れる task が無い (idle)
    wm.record_path(box.root / "registry", "Ren").write_text("{ no")
    assert box.pull("Ren").returncode == 3                       # 読めない = 保留


def test_auto_pull_skips_an_escalated_task_for_a_default_worker(box):
    assert _update(box, "--model-override", "opus").returncode == 0
    _bind(box, "Ren", None, "sonnet", "skill")
    r = box.pull("Ren", auto=True)
    assert r.returncode == 2 and "t001" not in r.stdout
    _bind(box, "Seo", f"{MISSION}/t001")
    r = box.pull("Seo", auto=True)
    assert r.returncode == 0, r.stderr


def test_pull_refuses_a_broken_override(box):
    p = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    p.write_text(p.read_text().replace("target_dir: null\n", "target_dir: null\nmodel_override: opus-4\n", 1))
    _bind(box, "Ren", f"{MISSION}/t001")
    r = box.pull("Ren")
    assert r.returncode == 3 and "model_override" in r.stderr


def test_store_check_reports_a_broken_override_without_the_value(box):
    p = box.queue / "missions" / MISSION / "tasks" / "t001.md"
    p.write_text(p.read_text().replace("target_dir: null\n", "target_dir: null\nmodel_override: SECRET-xyz\n", 1))
    r = box.plan("store-check")
    assert "model-override-invalid" in r.stdout and "SECRET-xyz" not in r.stdout + r.stderr


def test_status_marks_an_escalated_task(box):
    assert _update(box, "--model-override", "opus").returncode == 0
    r = box.plan("status", "--mission", MISSION)
    assert "model=opus↑" in r.stdout


def test_lint_flags_a_broken_override_and_at_cap_count():
    import lint_plan
    ok = {"id": "t001", "title": "t", "skills": ["code"], "status": "pending"}
    def fails(meta):
        res = lint_plan.check_frontmatter([dict(ok, **meta)]) if hasattr(lint_plan, "check_frontmatter") else []
        return [r for r in res if r[0] == "FAIL" and ("model_override" in r[2] or "model_at_cap_count" in r[2])]
    assert not fails({"model_override": "opus"})
    assert fails({"model_override": "opus-4"})
    assert fails({"model_at_cap_count": -1})
    assert fails({"model_at_cap_count": True})
    assert not fails({"model_at_cap_count": 2})


# --- 予約の再開も同じモデルの判定を通る (Codex P2) ----------------------------------

def _seed_reserved(box):
    """Ren が t001 を予約しただけ (start 前) の形にする。"""
    import lib_state_store as store
    assert box.pull("Ren").returncode == 0
    with store.transaction(box.queue, op="seed", actor="test") as txn:
        meta, body = txn.load_card(MISSION, "t001")
        meta.update(execution_status="reserved")
        txn.write_card(MISSION, "t001", meta, body)


@pytest.mark.parametrize("auto", [True, False], ids=["auto-resumable", "explicit-in_progress"])
def test_resume_of_a_reservation_is_refused_when_the_model_rule_no_longer_allows_it(box, auto):
    _bind(box, "Ren", None, "sonnet", "skill")
    _seed_reserved(box)
    assert _update(box, "--model-override", "opus").returncode == 0     # 予約の後に段上げされた
    before = box.snapshot()
    p = box.pull("Ren", auto=True) if auto else box.pull("Ren")
    assert p.returncode == 3 and p.stdout.strip() == "", (p.returncode, p.stderr)
    assert "再開" in p.stderr
    assert box.snapshot() == before


@pytest.mark.parametrize("auto", [True, False], ids=["auto-resumable", "explicit-in_progress"])
def test_resume_of_a_reservation_still_works_for_a_plain_task(box, auto):
    _bind(box, "Ren", None, "sonnet", "skill")
    _seed_reserved(box)
    p = box.pull("Ren", auto=True) if auto else box.pull("Ren")
    assert p.returncode == 0, p.stderr
