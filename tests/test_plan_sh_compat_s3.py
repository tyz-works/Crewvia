"""互換性テスト (S3 / 原案 §10.6 COMPAT-01): plan.sh の外から見える挙動は cutover の前後で変わらない。

固定 fixture のシナリオ (`tests/plan_sh_compat_scenario.py`) を今の plan.sh で走らせ、cutover 前 (a1f6957) の
plan.sh で走らせて作った golden (`tests/fixtures/plan_sh_compat_s3.golden.json`) と比べる。比べるもの:

- 各 subcommand の **exit code / stdout / stderr** (init / add / status / lint / pull の task 選択と busy の拒否 /
  needs-director / update --reset / done・fail の拒否と成功 / release-dep / ready-for-verification / verify-result /
  retire / reap-orphan-assignment / update / archive の全 39 段)
- 最後の queue の**全ファイルの中身** (card・mission.yaml・state.yaml・assignments。カードの round-trip で
  不要な差分が出ないこと)。archive の直前と直後の 2 時点

比べないもの: `queue/audit/` (S3 が足す唯一の新しい出力。別のテストが見る) と `.lock`。
実行ごとに変わる値 (時刻・今日の日付の slug・一時ディレクトリ) だけを正規化する。
"""

from __future__ import annotations

import json
import pathlib

import plan_sh_compat_scenario as scenario

HERE = pathlib.Path(__file__).resolve().parent
GOLDEN = json.loads((HERE / "fixtures" / "plan_sh_compat_s3.golden.json").read_text(encoding="utf-8"))
REPO = HERE.parent


def _run(tmp_path):
    return scenario.run_scenario(REPO, tmp_path)


def test_golden_is_not_vacuous():
    steps = GOLDEN["steps"]
    assert len(steps) >= 39
    # 成功・拒否 (2 / 3)・存在しない対象 (1) の終了コードがそれぞれ含まれる (全部 0 の golden は何も守らない)
    assert {s["rc"] for s in steps} >= {0, 1, 2, 3}
    assert len(GOLDEN["queue_final"]) >= 11 and len(GOLDEN["queue_before_archive"]) >= 9
    # archive の直前には card 6 枚・mission.yaml・state.yaml が揃っている
    assert sum(k.endswith(".md") for k in GOLDEN["queue_before_archive"]) == 6


def test_every_step_matches_the_pre_cutover_output(tmp_path):
    got = _run(tmp_path)
    assert len(got["steps"]) == len(GOLDEN["steps"])
    diffs = []
    for want, have in zip(GOLDEN["steps"], got["steps"]):
        if want != have:
            diffs.append({"step": want["cmd"], "want": want, "have": have})
    assert not diffs, "外から見える挙動が変わった:\n" + json.dumps(diffs, ensure_ascii=False, indent=1)


def test_every_queue_file_is_byte_identical_to_the_pre_cutover_run(tmp_path):
    got = _run(tmp_path)
    for key in ("queue_before_archive", "queue_final"):
        want, have = GOLDEN[key], got[key]
        assert sorted(have) == sorted(want), f"{key}: ファイルの集合が違う"
        changed = [name for name in want if want[name] != have[name]]
        assert not changed, f"{key}: 中身が変わったファイル: {changed}"
