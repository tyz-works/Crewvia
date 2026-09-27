#!/usr/bin/env python3
"""PR #217 (t007 / backlog #9) への Kai-codex の指摘 P1 / P2 の回帰 (t025)。

## P1 — 解除の案内が別 mission の task を解除する

`held_dependency_hint()` が出す `plan.sh release-dep tNNN` / `plan.sh update tNNN ...`
には `--mission` が無かった。task ID は mission ごとの自動採番なので、別 mission に
同じ `tNNN` が普通にある。`--mission` の無いコマンドは default_mission の task を
解除する / skip する —— **出した文面をそのまま打つと、別の task が解除され、意図した
task は保留のまま**になる。案内そのものが罠だった。

ここで固定するのは「文面に `--mission` が入っている」ではなく、**その文面をそのまま
実行すると、意図した mission の意図した task が解除される** こと。同じ ID を持つ
2 つの mission を用意し、両方を保留にしておく (`--mission` が落ちると default の
task が動く = 検出できる形)。

## P2 — `released_deps` を検証せずに解除として解釈する

共有の card parser は `released_deps` を検証せず、`unmet_dependencies()` がそのまま
`set()` にしていた。`true` / `123` は `TypeError` (1 枚の card で dispatch と status が
落ちる)、mapping (`t001: false`) はキーだけが集合になり **failed の依存を誤って
解除する**。card の正規化で「task ID の list だけ」を受理し、それ以外は `[破損]` に
隔離する。隔離は保留の出口を塞がない (release-dep が不正な値を捨てて書き直す)。
"""

from __future__ import annotations

import json
import pathlib
import re
import shlex
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import lib_dep_rules  # noqa: E402
import lib_task_cards  # noqa: E402
from test_failed_dependency_hold import (  # noqa: E402
    DISPATCHER_SH, MISSION, PLAN_SH, Sandbox, _card, _dispatcher_namespace,
    _mission_yaml,
)

OTHER = "m-other"
CORRUPT_MARK = "💥"


# ---------------------------------------------------------------------------
# フィクスチャ — 同じ tNNN を持つ 2 つの mission
# ---------------------------------------------------------------------------

def _two_missions(tmp_path):
    """`m-hold` (default) と `m-other`。**両方**に failed の t001 とそれに保留される
    t002 がある。`--mission` を落としたコマンドは default の t002 に当たる —— 当たった
    ことが見えるように、default 側も保留にしてある。
    """
    sb = Sandbox(tmp_path)
    other_tasks = sb.queue / "missions" / OTHER / "tasks"
    other_tasks.mkdir(parents=True)
    (sb.queue / "missions" / OTHER / "mission.yaml").write_text(_mission_yaml(OTHER))
    (sb.queue / "state.yaml").write_text(
        f"active_missions:\n  - {MISSION}\n  - {OTHER}\ndefault_mission: {MISSION}\n")
    for d in (sb.tasks, other_tasks):
        (d / "t001.md").write_text(_card("t001", "failed"))
        (d / "t002.md").write_text(_card("t002", "pending", ["t001"]))
    sb.other_tasks = other_tasks
    return sb


def _card_text(sb, mission, task_id):
    base = sb.tasks if mission == MISSION else sb.other_tasks
    return (base / f"{task_id}.md").read_text()


def _status_of(sb, mission, task_id):
    m = re.search(r"^status: (\S+)$", _card_text(sb, mission, task_id), re.M)
    return m.group(1)


def _released_of(sb, mission, task_id):
    return re.search(r"^released_deps:.*$", _card_text(sb, mission, task_id), re.M)


def _commands_after(text, marker):
    """`marker` より後ろの `plan.sh ...` (バッククォートで囲まれたもの) を出現順に返す。"""
    assert marker in text, f"{marker!r} が出力に無い:\n{text}"
    tail = text[text.index(marker):]
    return [shlex.split(c) for c in re.findall(r"`plan\.sh ([^`]+)`", tail)]


def _pick(cmds, verb):
    hit = [c for c in cmds if c[0] == verb]
    assert hit, f"{verb} の案内が出力に無い: {cmds}"
    return hit[0]


# 保留を見せる 4 つの場所。(名前, 実行する plan.sh の引数, 案内が始まる印)
#   status 要約 / status 詳細 / pull の診断 (自動選択) / pull --task の拒否
SITES = [
    ("status-summary", ("status",), f"  {OTHER} —"),
    ("status-detail", ("status", "--mission", OTHER), "Held (failed"),
    ("pull-diagnostic", ("pull", "--skills", "code", "--agent", "Ren"), f"[{OTHER}/t002]"),
    ("pull-task-refusal",
     ("pull", "--task", "t002", "--mission", OTHER, "--skills", "code", "--agent", "Ren"),
     "is held:"),
]
SITE_IDS = [s[0] for s in SITES]


def _guidance(sb, args, marker):
    r = sb.run(*args)
    return _commands_after(r.stdout + r.stderr, marker)


# ---------------------------------------------------------------------------
# P1 — 出した文面をそのまま打つと、意図した task が解除される
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site,args,marker", SITES, ids=SITE_IDS)
def test_pasted_release_dep_releases_the_intended_task(tmp_path, site, args, marker):
    sb = _two_missions(tmp_path)
    cmd = _pick(_guidance(sb, args, marker), "release-dep")

    r = sb.run(*cmd)
    assert r.returncode == 0, f"[{site}] 案内のコマンドが実行できない: {cmd}\n{r.stderr}"

    assert _released_of(sb, OTHER, "t002"), (
        f"[{site}] 案内のとおり打ったのに、意図した {OTHER}/t002 が解除されていない")
    assert not _released_of(sb, MISSION, "t002"), (
        f"[{site}] 案内のコマンドが別 mission の t002 を解除した ({MISSION}/t002): {cmd}")


@pytest.mark.parametrize("site,args,marker", SITES, ids=SITE_IDS)
def test_pasted_skip_command_skips_the_intended_task(tmp_path, site, args, marker):
    sb = _two_missions(tmp_path)
    cmd = _pick(_guidance(sb, args, marker), "update")

    r = sb.run(*cmd)
    assert r.returncode == 0, f"[{site}] 案内のコマンドが実行できない: {cmd}\n{r.stderr}"

    assert _status_of(sb, OTHER, "t002") == "skipped", (
        f"[{site}] 案内のとおり打ったのに、意図した {OTHER}/t002 が skip されていない")
    assert _status_of(sb, MISSION, "t002") == "pending", (
        f"[{site}] 案内のコマンドが別 mission の t002 を skip した ({MISSION}/t002): {cmd}")


@pytest.mark.parametrize("site,args,marker", SITES, ids=SITE_IDS)
def test_every_command_in_the_guidance_names_its_mission(tmp_path, site, args, marker):
    """コマンドを 1 本ずつ実行しなくても、案内に出る `plan.sh` は全部 `--mission` を持つ。"""
    sb = _two_missions(tmp_path)
    r = sb.run(*args)
    cmds = [shlex.split(c) for c in re.findall(r"`plan\.sh ([^`]+)`", r.stdout + r.stderr)]
    assert cmds, f"[{site}] 案内が出ていない:\n{r.stdout}{r.stderr}"
    for c in cmds:
        assert "--mission" in c, f"[{site}] --mission の無い案内: plan.sh {' '.join(c)}"


def test_task_graph_marker_carries_no_bare_command(tmp_path):
    """task-graph の印 (`[保留: ...]`) は理由だけを出し、コマンドは持たない。
    もし将来コマンドを足すなら、`--mission` の無い形は許さない。"""
    sb = _two_missions(tmp_path)
    assert sb.run("task-graph").returncode == 0
    nodes = {t["id"]: t for t in json.loads(sb.graph.read_text())["tasks"]}
    node = nodes[f"{OTHER}:t002"]
    assert "保留" in node["title"] and "t001" in node["title"], node["title"]
    for c in re.findall(r"plan\.sh ([^`\n]+)", node["title"]):
        assert "--mission" in c, f"task-graph の印に --mission の無いコマンド: {c}"


def test_dispatcher_log_command_names_its_mission():
    """dispatcher のログ (`[held]`) も同じ罠を持ちうる。ソースに出る `plan.sh release-dep` は
    すべて `--mission` を伴う (実行はデーモンのサイクルが要るので、形で固定する)。"""
    hits = [ln for ln in DISPATCHER_SH.read_text().splitlines()
            if "plan.sh release-dep" in ln]
    assert hits, "dispatcher の [held] ログの案内が消えている"
    for ln in hits:
        assert "--mission" in ln, f"dispatcher の案内に --mission が無い: {ln.strip()}"


def _calls(source, name):
    """`name(` の呼び出し (def を除く) の実引数リストを返す。括弧の対応を数える。"""
    out = []
    for m in re.finditer(rf"(?<!def ){name}\(", source):
        depth, i, args, cur = 1, m.end(), [], []
        while depth:
            ch = source[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    break
            if ch == "," and depth == 1:
                args.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
            i += 1
        if "".join(cur).strip():
            args.append("".join(cur).strip())
        out.append(args)
    return out


def test_no_call_of_the_hint_can_omit_the_mission():
    """構造: `slug` は必須引数 (既定値なし) で、呼び出しは全部 3 引数。
    表で「4 箇所」と数えるのではなく、mission を渡し忘れた呼び出しが 1 つでも
    増えたら落ちるようにする。"""
    src = PLAN_SH.read_text()
    sig = re.search(r"def held_dependency_hint\(([^)]*)\)", src).group(1)
    assert [a.strip() for a in sig.split(",")] == ["task_id", "held", "slug"], (
        f"引数の形が変わった (slug は既定値なしの必須): {sig!r}")
    calls = _calls(src, "held_dependency_hint")
    assert len(calls) >= 5, f"呼び出しを拾い損ねている: {calls}"
    for args in calls:
        assert len(args) == 3, f"mission を渡していない呼び出し: {args}"


# ---------------------------------------------------------------------------
# P2 — released_deps の検証
# ---------------------------------------------------------------------------

# (名前, frontmatter に書く生の行)。受理してはいけない形。
BAD_RELEASED = [
    ("bool",             "released_deps: true"),
    ("int",              "released_deps: 123"),
    ("bare-string",      "released_deps: t001"),
    ("quoted-string",    'released_deps: "t001"'),
    ("mapping-false",    "released_deps:\n  t001: false"),   # 旧: {'t001'} → 誤解除
    ("mapping-true",     "released_deps:\n  t001: true"),
    ("list-non-id",      "released_deps: [foo]"),
    ("list-mixed",       "released_deps: [t001, bar]"),
    ("list-int",         "released_deps: [123]"),
    ("list-bool",        "released_deps: [true]"),
    ("list-almost-id",   "released_deps: [t1x]"),
    ("block-list-non-id", "released_deps:\n  - t001\n  - nope"),
]
BAD_IDS = [b[0] for b in BAD_RELEASED]

# 受理する形。
GOOD_RELEASED = [
    ("absent",      None),
    ("null",        "released_deps: null"),
    ("empty-list",  "released_deps: []"),
    ("inline-list", "released_deps: [t001]"),
    ("block-list",  "released_deps:\n  - t001"),
]


def _write_with_raw_released(sb, raw, task_id="t002", blocked_by=("t001",)):
    """`_card()` は list しか書けないので、生の行を差し込む。"""
    text = _card(task_id, "pending", blocked_by)
    if raw is not None:
        text = text.replace("target_dir: null", raw + "\ntarget_dir: null", 1)
    (sb.tasks / f"{task_id}.md").write_text(text)


def _failed_dep_sandbox(tmp_path, raw):
    sb = Sandbox(tmp_path)
    sb.card("t001", "failed")
    _write_with_raw_released(sb, raw)
    return sb


def _no_crash(r, what):
    assert "Traceback" not in r.stderr, f"[{what}] 例外で落ちた:\n{r.stderr}"


@pytest.mark.parametrize("name,raw", BAD_RELEASED, ids=BAD_IDS)
def test_bad_released_deps_isolates_the_card(tmp_path, name, raw):
    sb = _failed_dep_sandbox(tmp_path, raw)
    r = sb.run("status", "--mission", MISSION)
    _no_crash(r, f"status/{name}")
    assert r.returncode == 0, r.stderr
    lines = [l for l in r.stdout.splitlines() if "t002" in l and CORRUPT_MARK in l]
    assert lines, f"[{name}] t002 が [破損] として出ていない:\n{r.stdout}"
    line = lines[0]
    assert "released_deps" in line, f"[{name}] 何が悪いのかが出ていない: {line}"
    assert "released_deps" in r.stderr, f"[{name}] 警告に理由が無い: {r.stderr}"


@pytest.mark.parametrize("name,raw", BAD_RELEASED, ids=BAD_IDS)
def test_bad_released_deps_never_releases_and_never_crashes(tmp_path, name, raw):
    """この card だけが隔離され、status (要約) / pull / task-graph / dispatch は落ちず、
    failed の依存は誤って解除されない (mapping の `t001: false` を含む)。"""
    sb = _failed_dep_sandbox(tmp_path, raw)

    r = sb.run("status")
    _no_crash(r, f"status-summary/{name}")
    assert r.returncode == 0, r.stderr

    r = sb.run("pull", "--skills", "code", "--agent", "Ren")
    _no_crash(r, f"pull/{name}")
    assert '"id": "t002"' not in r.stdout, f"[{name}] 隔離した card を pull した: {r.stdout}"

    r = sb.run("pull", "--task", "t002", "--mission", MISSION,
               "--skills", "code", "--agent", "Ren")
    _no_crash(r, f"pull --task/{name}")
    assert r.returncode != 0, f"[{name}] 不正な released_deps の card を pull できた"

    r = sb.run("task-graph")
    _no_crash(r, f"task-graph/{name}")
    assert r.returncode == 0, r.stderr
    node = sb.graph_node("t002")
    assert node["status"] != "ready", f"[{name}] DAG が READY と描いた: {node}"

    ns = _dispatcher_namespace(sb.root)
    all_tasks, done_ids, statuses = ns["load_all_tasks"]([MISSION])   # 落ちないこと
    meta = next(m for _s, m in all_tasks if m["id"] == "t002")
    assert meta["status"] == lib_task_cards.CORRUPT_TASK_STATUS, (
        f"[{name}] dispatcher が t002 を隔離していない: {meta}")
    ns["dependency_gate"](MISSION, meta, done_ids, statuses)          # 落ちないこと


@pytest.mark.parametrize("name,raw", BAD_RELEASED, ids=BAD_IDS)
def test_release_dep_is_still_an_exit_from_a_quarantined_card(tmp_path, name, raw):
    """隔離が「保留の出口」を奪わない: Director は release-dep 1 発で、不正な値を
    捨てて書き直せる。書き直した後は健全な card として進む。"""
    sb = _failed_dep_sandbox(tmp_path, raw)
    r = sb.run("release-dep", "t002", "--mission", MISSION)
    _no_crash(r, f"release-dep/{name}")
    assert r.returncode == 0, f"[{name}] 隔離された card を release-dep で救えない:\n{r.stderr}"
    assert re.search(r"^released_deps: \[t001\]$", _card_text(sb, MISSION, "t002"), re.M), (
        _card_text(sb, MISSION, "t002"))

    r = sb.run("pull", "--task", "t002", "--mission", MISSION,
               "--skills", "code", "--agent", "Ren")
    assert r.returncode == 0, f"[{name}] 解除したのに pull できない:\n{r.stderr}"


@pytest.mark.parametrize("name,raw", BAD_RELEASED[:4] + BAD_RELEASED[6:8],
                         ids=BAD_IDS[:4] + BAD_IDS[6:8])
def test_update_blocked_by_is_not_crashed_by_a_bad_released_deps(tmp_path, name, raw):
    """もう 1 つの出口 (`update --blocked-by`) も、不正な値で落ちない。
    解除は増えず、依存は保留のまま。"""
    sb = _failed_dep_sandbox(tmp_path, raw)
    r = sb.run("update", "t002", "--mission", MISSION, "--blocked-by", "t001")
    _no_crash(r, f"update/{name}")
    assert r.returncode == 0, r.stderr
    assert not _released_of(sb, MISSION, "t002"), _card_text(sb, MISSION, "t002")
    r = sb.run("pull", "--task", "t002", "--mission", MISSION,
               "--skills", "code", "--agent", "Ren")
    assert r.returncode != 0 and "held" in r.stderr, "保留のまま (勝手に解除されない) のはず"


@pytest.mark.parametrize("name,raw", GOOD_RELEASED, ids=[g[0] for g in GOOD_RELEASED])
def test_well_formed_released_deps_is_not_quarantined(tmp_path, name, raw):
    """受理する形が隔離されない (隔離が広すぎて正しい解除まで落とさない)。"""
    sb = _failed_dep_sandbox(tmp_path, raw)
    r = sb.run("status", "--mission", MISSION)
    assert r.returncode == 0, r.stderr
    assert CORRUPT_MARK not in r.stdout, f"[{name}] 正しい形が隔離された:\n{r.stdout}"
    released = raw is not None and "t001" in raw
    pulled = sb.run("pull", "--task", "t002", "--mission", MISSION,
                    "--skills", "code", "--agent", "Ren")
    assert (pulled.returncode == 0) == released, (
        f"[{name}] 解除の有無と pull の可否が食い違う: {pulled.stderr}")


# --- 規則そのもの (関数の単位) -------------------------------------------

@pytest.mark.parametrize("value", [True, False, 0, 123, "t001", "",
                                   {"t001": False}, {"t001": True}, {},
                                   [123], [True], [None], ["foo"], ["t001", "x"],
                                   ["T001"], ["t001 "], ["t"], [["t001"]]])
def test_validator_rejects(value):
    assert lib_task_cards.released_deps_problem(value), f"{value!r} を受理した"


@pytest.mark.parametrize("value", [None, [], ["t001"], ["t001", "t012"]])
def test_validator_accepts(value):
    assert lib_task_cards.released_deps_problem(value) is None


@pytest.mark.parametrize("value", [True, 123, "t001", {"t001": False}, {"t001": True},
                                   [123], [True], [None]])
def test_second_layer_never_releases_on_a_bad_shape(value):
    """読み取りの隔離を通らずに形の違う値が来ても (release-dep の生 card など)、
    `card_dependencies()` は落ちず、failed の依存を解除しない。"""
    meta = {"blocked_by": ["t001"], "released_deps": value}
    verdict = lib_dep_rules.card_dependencies(meta, set(), {"t001": "failed"})
    assert verdict.unmet == ["t001"] and verdict.held == ["t001"], (
        f"{value!r} で判定が変わった: {verdict}")


def test_second_layer_still_honours_a_well_formed_release():
    meta = {"blocked_by": ["t001"], "released_deps": ["t001"]}
    verdict = lib_dep_rules.card_dependencies(meta, set(), {"t001": "failed"})
    assert verdict.unmet == [] and verdict.held == []
