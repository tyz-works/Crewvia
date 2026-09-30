"""crash 注入 (原案 §10.3 / 設計 §2.2): 各書き込みの点でプロセスを強制終了し、次のロック取得で収束する。

場面 (`state_store_scenarios.SCENARIOS`) は設計の表の複数ファイル更新 (pull / done / fail /
update --reset / add / archive) を lib の API だけで再現したもの。lib は書き込みの各段
(tmp 作成・書き込み・fsync・replace・親 dir fsync・unlink・監査ログ) で `FAULT_HOOK` を呼ぶので、
その k 番目で子プロセスが自分に SIGKILL を送る。そのうえで**別プロセス (この pytest)** が
`transaction()` を取り直して `recover()` し、次を確かめる:

  1. 壊れたファイルが無い (card / mission / state / identity がすべて読める)
  2. 正本と projection の食い違いが無い (`diagnose` が残骸 `stale_tmp` 以外を出さない)
  3. 回復は冪等 (2 回目の修復が空)
  4. 場面ごとの意味 (done は派生値が正本より前・reset は旧所有者の枠が消える・add は採番が進む …)

全点 × 20 回反復する (flaky 無しの確認。原案 §10.3)。点の数は dry-run で数える — 0 件や
急減は注入口の故障なので、下限を assert する (空虚な PASS を防ぐ)。
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

import state_store_scenarios as sc
import lib_state_store as store

REPS = 20

#: 場面ごとの点の数の下限 (lib の現在値。書き込みの段が減ったら注入口が壊れている)
MIN_POINTS = {"pull": 18, "done": 24, "fail": 12, "reset": 12, "add": 12, "archive": 8}


@pytest.fixture(scope="module")
def seeds(tmp_path_factory):
    """場面ごとの開始状態 (1 回作って、反復のたびに複製する)。点の一覧もここで取る。"""
    out = {}
    for name in sc.SCENARIOS:
        root = tmp_path_factory.mktemp(f"seed-{name}")
        sc.seed(root / "q", name)
        probe = tmp_path_factory.mktemp(f"probe-{name}")
        shutil.copytree(root / "q", probe / "q")
        out[name] = (root / "q", sc.points_of(probe / "q", name))
    return out


def test_every_scenario_has_enough_points(seeds):
    for name, (_q, points) in seeds.items():
        assert len(points) >= MIN_POINTS[name], (name, len(points), points)
    # 注入口が本当に各段を網羅している (1 つの書き込みで 6 点: begin/tmp/written/synced/replaced/dir)
    assert {"atomic:begin", "atomic:tmp_created", "atomic:written", "atomic:synced",
            "atomic:replaced", "atomic:dir_synced"} <= set(seeds["pull"][1])
    assert {"remove:begin", "remove:unlinked", "remove:dir_synced"} <= set(seeds["done"][1])
    assert {"audit:begin", "audit:appended"} <= set(seeds["pull"][1])
    assert {"scenario:before_move", "scenario:after_move"} <= set(seeds["archive"][1])


@pytest.mark.parametrize("scenario", sorted(sc.SCENARIOS))
def test_crash_at_every_point_converges_after_next_lock(scenario, seeds, tmp_path):
    seed_q, points = seeds[scenario]
    checked = 0
    for k, point in enumerate(points):
        for rep in range(REPS):
            q = tmp_path / f"{k}-{rep}" / "q"
            shutil.copytree(seed_q, q)
            died = sc.fork_and_crash(q, scenario, k)
            assert died, f"{scenario}[{k}={point}] 子が SIGKILL で落ちていない (注入口が効いていない)"

            # 次のロック取得 (別プロセス): 回復して収束する
            with store.transaction(q, op="next", actor="test") as t:
                first = t.recover(sc.SCOPES[scenario])
            sc.assert_converged(q, scenario)
            # 冪等: 収束後の 2 回目は何も修復しない
            with store.transaction(q, op="next2", actor="test") as t:
                second = t.recover(sc.SCOPES[scenario])
            assert [r for r in second if r.repaired] == [], (scenario, k, point, second)
            # 修復は必ず監査ログに残る (recover() の中で即時に追記される)
            audit = list((q / "audit").glob("transitions-*.jsonl"))
            lines = "".join(p.read_text() for p in audit)
            for r in first:
                if r.repaired:
                    assert f'"result": "{r.result}"' in lines, (scenario, k, point, r)
            checked += 1
    assert checked == len(points) * REPS


def test_pull_crash_between_card_and_assignment_is_repaired_by_r1(seeds, tmp_path):
    """設計 §2.2 pull の行: card の後・identity の前 → R-1 が identity(G) → assignment を書く。"""
    seed_q, points = seeds["pull"]
    # card の replace が済んだ直後 (dir_synced) で落とす = 最初の dir_synced
    k = points.index("atomic:dir_synced")
    q = tmp_path / "q"
    shutil.copytree(seed_q, q)
    assert sc.fork_and_crash(q, "pull", k)
    assert sc.read_meta(q, sc.MISSION, "t001")["status"] == "in_progress"
    assert sc.slot_text(q, sc.AGENT) is None
    with store.transaction(q, op="next", actor="t") as t:
        repairs = t.recover(sc.SCOPES["pull"])
    assert [r.result for r in repairs] == ["repaired:R-1"]
    assert sc.slot_text(q, sc.AGENT) == f"{sc.MISSION}:t001"
    assert sc.identity(q, sc.AGENT)["started_at"] == sc.NEW_GEN


def test_reset_crash_between_card_and_retire_is_repaired_by_reverse_lookup(seeds, tmp_path):
    """update --reset は card の worker を null にしてから枠を消す。その間で落ちても、
    card からは旧所有者が分からないので**逆引き**で枠を見つけて R-2 (設計 §2.5)。"""
    seed_q, points = seeds["reset"]
    k = points.index("atomic:dir_synced")
    q = tmp_path / "q"
    shutil.copytree(seed_q, q)
    assert sc.fork_and_crash(q, "reset", k)
    assert sc.read_meta(q, sc.MISSION, "t001")["worker"] is None
    assert sc.slot_text(q, sc.AGENT) == f"{sc.MISSION}:t001"          # 孤児
    with store.transaction(q, op="next", actor="t") as t:
        repairs = t.recover(store.Scope(cards=((sc.MISSION, "t001"),)))   # 呼び出し元の枠も渡さない
    assert [r.result for r in repairs] == ["repaired:R-2"]
    assert sc.slot_text(q, sc.AGENT) is None


def test_add_crash_between_card_and_next_task_id_is_repaired_by_r3(seeds, tmp_path):
    seed_q, points = seeds["add"]
    k = points.index("atomic:dir_synced")           # 新しい card の後・mission の前
    q = tmp_path / "q"
    shutil.copytree(seed_q, q)
    assert sc.fork_and_crash(q, "add", k)
    assert (q / "missions" / sc.MISSION / "tasks" / "t003.md").exists()
    with store.transaction(q, op="next", actor="t") as t:
        repairs = t.recover(sc.SCOPES["add"])
    assert [r.result for r in repairs] == ["repaired:R-3"]
    assert "t003" in repairs[0].detail          # 前回の残りが見える (黙って上書きしない)
