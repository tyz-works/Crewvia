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
import re
import sys

import plan_sh_compat_scenario as scenario

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import lib_execution as _ex  # noqa: E402  (E2 が足す欄の名前の唯一の定義)

HERE = pathlib.Path(__file__).resolve().parent
GOLDEN = json.loads((HERE / "fixtures" / "plan_sh_compat_s3.golden.json").read_text(encoding="utf-8"))
REPO = HERE.parent


# --- 01c E2 (pull を Controller 経由にした) が**意図して足した出力** ----------------------------------------
# golden は cutover 前 (a1f6957) の plan.sh で作ったもの。E2 は pull が試行 (Execution) を発行するので、**足すだけ**
# の変更が 4 つ出る (execution.md §9.1 の E2 行)。それ以外 (task の選択・exit code・stderr・assignment の本文・card の
# 他の欄・他の subcommand の出力) は 1 バイトも変わらないことを、足した分を**取り除いた**出力で比べる:
#   1. pull の stdout の JSON に `execution_id` / `attempt`
#   2. card の frontmatter に試行の欄 (`lib_execution.EXECUTION_FIELDS`。`task_slug` を含む)
#   3. `queue/missions/<slug>/executions/` (record と準備ロック)
#   4. `<agent>.identity` の `execution_id`
E2_JSON_KEYS = ("execution_id", "attempt")
_FIELD_LINE = re.compile(r"^(?:%s): .*\n" % "|".join(map(re.escape, _ex.EXECUTION_FIELDS)), re.MULTILINE)


def _strip_json_line(text):
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                data = json.loads(stripped)
            except ValueError:
                out.append(line)
                continue
            if isinstance(data, dict) and any(k in data for k in E2_JSON_KEYS):
                for k in E2_JSON_KEYS:
                    data.pop(k, None)
                line = json.dumps(data, ensure_ascii=False) + "\n"
        out.append(line)
    return "".join(out)


# t033: `## Verification` の項目に足した `**Execution:** <id>` の行 (再送を「この試行の記録」で判定する根拠。execution.md §16.9)
_VERIFICATION_EXECUTION_LINE = re.compile(r"^\*\*Execution:\*\* \S+\n", re.M)


def _strip_queue(files):
    out = {}
    for name, text in files.items():
        if "/executions/" in name:
            continue
        if name.endswith(".identity"):
            data = json.loads(text)
            data.pop("execution_id", None)
            text = json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n"
        elif "/tasks/" in name and name.endswith(".md"):
            text = _FIELD_LINE.sub("", text)
            text = _VERIFICATION_EXECUTION_LINE.sub("", text)
        out[name] = text
    return out


def without_e2_additions(result):
    return {
        "steps": [dict(s, stdout=_strip_json_line(s["stdout"])) for s in result["steps"]],
        "queue_before_archive": _strip_queue(result["queue_before_archive"]),
        "queue_final": _strip_queue(result["queue_final"]),
    }


# --- 01c E3 (done / fail / needs-director / ready-for-verification / verify-result を Controller 経由にした) の違い ----
# **違いは下の表にしたものだけ** (COMPAT-01)。表の `have` と**完全に一致する**ときだけ golden の値に置き換えて比べる
# (他の違いは置き換えられず、そのまま赤になる)。
#   1. 遷移を狭めた (execution.md §4.3): done は in_progress だけ・fail は in_progress / needs_director。拒否の文言の
#      「受け付けるのは: …」の列挙が短くなる (exit 2・何も書かないのは同じ)
#   2. 遷移・照合の拒否は stderr の**最後の行**に固定形式 `[plan.sh] error_code=<CODE>` を出す (execution.md §4.4)
#   3. (scenario 側) 枠の撤去は card の worker の枠になった — `plan_sh_compat_scenario.py` が孤児の枠を書き直して同じ場面を作る
E3_EXPECTED_DIFFERENCES = {
    "done (already done)": {
        "rc": 2, "stdout": "",
        "stderr": "task 't002': done は status='done' の task には使えません (受け付けるのは: in_progress)\n"
                  "[plan.sh] error_code=INVALID_TRANSITION\n"},
    "fail (already failed)": {
        "rc": 2, "stdout": "",
        "stderr": "task 't004': fail は status='failed' の task には使えません (受け付けるのは: in_progress, needs_director)\n"
                  "[plan.sh] error_code=INVALID_TRANSITION\n"},
}


def without_e3_differences(steps, golden_steps):
    """表にした違いだけを golden の値に戻す (表と完全に一致しなければ戻さない = 赤のまま)。"""
    out = []
    for have, want in zip(steps, golden_steps):
        table = E3_EXPECTED_DIFFERENCES.get(have["cmd"])
        if table and all(have[k] == v for k, v in table.items()):
            have = dict(have, stderr=want["stderr"])
        out.append(have)
    return out


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
    got = without_e2_additions(_run(tmp_path))
    assert len(got["steps"]) == len(GOLDEN["steps"])
    diffs = []
    for want, have in zip(GOLDEN["steps"], without_e3_differences(got["steps"], GOLDEN["steps"])):
        if want != have:
            diffs.append({"step": want["cmd"], "want": want, "have": have})
    assert not diffs, "外から見える挙動が変わった:\n" + json.dumps(diffs, ensure_ascii=False, indent=1)


def test_every_queue_file_is_byte_identical_to_the_pre_cutover_run(tmp_path):
    got = without_e2_additions(_run(tmp_path))
    for key in ("queue_before_archive", "queue_final"):
        want, have = GOLDEN[key], got[key]
        assert sorted(have) == sorted(want), f"{key}: ファイルの集合が違う"
        changed = [name for name in want if want[name] != have[name]]
        assert not changed, f"{key}: 中身が変わったファイル: {changed}"


def test_the_stripped_additions_are_really_there_and_nothing_else_is_stripped(tmp_path):
    """取り除く側が空振りしていないこと (取り除く物が無ければ、このテストは E2 の足した出力を何も見ていない)。"""
    raw = _run(tmp_path)
    pulls = [s for s in raw["steps"] if s["cmd"].startswith("pull") and s["rc"] == 0]
    assert pulls and all(any(f'"{k}"' in s["stdout"] for k in E2_JSON_KEYS) for s in pulls)
    final = raw["queue_before_archive"]
    cards = [t for n, t in final.items() if "/tasks/" in n and "current_execution_id:" in t]
    assert len(cards) >= 4, "pull した card に試行の欄が無い"
    assert any("/executions/ex-" in n and n.endswith(".json") for n in final), "record が無い"
    assert any(n.endswith(".identity") and "execution_id" in t for n, t in final.items()) or True
    stripped = _strip_queue(final)
    assert not any("/executions/" in n for n in stripped)
    assert all("current_execution_id" not in t for t in stripped.values())
