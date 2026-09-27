#!/usr/bin/env python3
"""tests/test_ci_runs_every_script_test.py — scripts/test_*.sh は「CI で走る」か「理由付きで除外」のどちらか (t025 / backlog #30)。

## 何を止めるか

`scripts/test_*.sh` は 26 本あるのに、CI (`.github/workflows/ci.yml`) が名指しで走らせていたのは 4 本だけだった。
事故を防ぐテストほど CI の外にあり、新しく足したテストも誰も気づかないまま CI の外に置かれた。

CI は `scripts/ci-run-script-tests.sh` で glob して全部走らせる。走らせないものは
`scripts/ci-script-tests-excluded.txt` の `<path> | <理由>` だけ。このテストは次を機械で保証する。

1. 全 `scripts/test_*.sh` が、実行 (RUN) か除外 (SKIP) のどちらかに **ちょうど 1 回** 入る（漏れも二重も無い）
2. 除外ファイルの各行は、実在するファイルと **2 種類だけの理由** を持つ（理由の無い行・存在しない行・重複は赤）
3. CI の workflow が runner を呼び、`scripts/test_*.sh` を **名指しで** 走らせる step が無い（名指しの一覧が戻らない）
4. runner 自身の振る舞い: 1 本落ちても残りを止めない / 0 本走らせて PASS にしない / 壊れた除外ファイルで落ちる /
   新しく足したテストは何もしなくても走る

検査した件数は毎回出す（`-s` か失敗メッセージ）。0 件で PASS しない（memory:
registry-dir-single-definition-and-vacuous-static-guards）。
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
RUNNER = SCRIPTS / "ci-run-script-tests.sh"
ALLOWLIST = SCRIPTS / "ci-script-tests-excluded.txt"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: 除外の理由は 2 種類だけ（Director 決定）。理由を足したいときはこの表を直す＝レビューに載る。
REASON_LIVE_MUX = "live の herdr / tmux が要る（恒久）"
REASON_UNINVESTIGATED = "未調査 (t043)"
ALLOWED_REASONS = {REASON_LIVE_MUX, REASON_UNINVESTIGATED}


def _glob_tests() -> list[str]:
    return sorted(f"scripts/{p.name}" for p in SCRIPTS.glob("test_*.sh"))


def _parse_allowlist(text: str) -> tuple[list[tuple[int, str, str]], list[str]]:
    """(行番号, path, 理由) の一覧と、形の壊れた行の説明を返す。runner とは別実装（同じ見落としを共有しない）。"""
    entries: list[tuple[int, str, str]] = []
    problems: list[str] = []
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            problems.append(f"{no}: 理由が無い: {raw!r}")
            continue
        path, reason = (part.strip() for part in line.split("|", 1))
        entries.append((no, path, reason))
    return entries, problems


def _run_runner(runner: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(runner), *args], capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(runner.parent)},
    )


# --- 1 + 2: 実物の tree に対する構造検査 -----------------------------------------------------


def test_every_script_test_runs_or_is_allowlisted():
    """runner の実際の `--list` 出力（実際の呼び出し形）と、独立に glob した一覧を突き合わせる。"""
    all_tests = _glob_tests()
    proc = _run_runner(RUNNER, "--list")
    assert proc.returncode == 0, f"runner --list が落ちた: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    run = [ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if ln.startswith("RUN ")]
    skip = [ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if ln.startswith("SKIP ")]

    print(f"\n[ci-runs-every-script-test] 検査した scripts/test_*.sh: {len(all_tests)} 本"
          f"（CI で走る {len(run)} / 理由付き除外 {len(skip)}）")
    assert all_tests, "scripts/test_*.sh が 0 本。glob が空振りしている（0 件で PASS にしない）"
    assert run, "CI で走るテストが 0 本。全部除外されている（0 件で PASS にしない）"
    assert len(run) == len(set(run)) and len(skip) == len(set(skip)), "RUN / SKIP に重複がある"
    assert not (set(run) & set(skip)), f"RUN と SKIP の両方に載っている: {sorted(set(run) & set(skip))}"
    assert sorted(run + skip) == all_tests, (
        "runner の RUN+SKIP が glob と一致しない（黙って CI の外に残るテストがある）\n"
        f"  glob だけ: {sorted(set(all_tests) - set(run + skip))}\n"
        f"  runner だけ: {sorted(set(run + skip) - set(all_tests))}")
    assert f"検査した scripts/test_*.sh: {len(all_tests)} 本" in proc.stdout, "runner が検査件数を出していない"


def test_allowlist_lines_have_a_real_file_and_one_of_two_reasons():
    entries, problems = _parse_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
    print(f"\n[ci-runs-every-script-test] 検査した除外行: {len(entries)} 行")
    assert not problems, "除外ファイルの形が壊れている:\n  " + "\n  ".join(problems)

    bad: list[str] = []
    seen: set[str] = set()
    for no, path, reason in entries:
        if not re.fullmatch(r"scripts/test_[A-Za-z0-9_]+\.sh", path):
            bad.append(f"{no}: scripts/test_*.sh ではない: {path}")
        elif not (REPO_ROOT / path).is_file():
            bad.append(f"{no}: 存在しないファイル: {path}")
        if reason not in ALLOWED_REASONS:
            bad.append(f"{no}: 理由が {sorted(ALLOWED_REASONS)} のどれでもない: {reason!r} ({path})")
        if path in seen:
            bad.append(f"{no}: 重複: {path}")
        seen.add(path)
    assert not bad, "除外ファイルに不正な行がある:\n  " + "\n  ".join(bad)


def test_runner_skips_exactly_the_allowlisted_files():
    """runner の SKIP は除外ファイルの行と過不足なく一致する（除外ファイルを読み飛ばしていない）。"""
    entries, _ = _parse_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
    listed = sorted(path for _, path, _ in entries)
    proc = _run_runner(RUNNER, "--list")
    skipped = sorted(ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if ln.startswith("SKIP "))
    assert skipped == listed, f"除外ファイルの行と runner の SKIP が違う: {listed} vs {skipped}"


# --- 3: workflow が runner を呼び、名指しの一覧が戻っていない -------------------------------


def test_workflow_runs_the_runner_and_names_no_script_test():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    runs = [
        (job_name, step["run"])
        for job_name, job in doc["jobs"].items()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]
    print(f"\n[ci-runs-every-script-test] 検査した workflow の run step: {len(runs)} 件")
    assert runs, "workflow から run step が 1 件も読めない（0 件で PASS にしない）"

    calls = [job for job, cmd in runs if "scripts/ci-run-script-tests.sh" in cmd]
    assert calls, "workflow が scripts/ci-run-script-tests.sh を呼んでいない（glob の実行が CI に載っていない）"
    named = [(job, m.group(0)) for job, cmd in runs for m in re.finditer(r"scripts/test_\w+\.sh", cmd)]
    assert not named, (
        "workflow が scripts/test_*.sh を名指しで走らせている。名指しの一覧に戻ると、足したテストが"
        f"黙って CI の外に残る。runner (glob) に任せ、走らせたくないものは除外ファイルへ: {named}")


# --- 4: runner の振る舞い（一時 tree。本番の scripts/ には触れない） -----------------------------


@pytest.fixture
def tree(tmp_path):
    """scripts/ci-run-script-tests.sh だけを写した一時 tree。テストと除外ファイルは各テストが置く。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    dest = scripts / RUNNER.name
    dest.write_text(RUNNER.read_text(encoding="utf-8"), encoding="utf-8")
    dest.chmod(0o755)

    class Tree:
        root = tmp_path
        runner = dest

        @staticmethod
        def test(name: str, exit_code: int = 0) -> None:
            (scripts / name).write_text(f'#!/usr/bin/env bash\necho "ran {name}"\nexit {exit_code}\n', encoding="utf-8")

        @staticmethod
        def allowlist(*lines: str) -> None:
            (scripts / "ci-script-tests-excluded.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return Tree


def test_runner_keeps_going_after_a_failure_and_reports_each(tree):
    tree.test("test_a.sh", exit_code=1)
    tree.test("test_b.sh")
    tree.allowlist("# 空")
    proc = _run_runner(tree.runner)
    assert proc.returncode == 1
    assert "FAIL scripts/test_a.sh" in proc.stdout
    assert "PASS scripts/test_b.sh" in proc.stdout, "1 本の失敗で残りが走らなくなっている"
    assert "ran test_b.sh" in proc.stdout
    assert "検査した scripts/test_*.sh: 2 本（実行 2 / 除外 0 / 失敗 1）" in proc.stdout


def test_runner_picks_up_a_newly_added_test_without_any_registration(tree):
    tree.test("test_a.sh")
    tree.allowlist("# 空")
    before = _run_runner(tree.runner, "--list").stdout
    tree.test("test_brand_new.sh")
    after = _run_runner(tree.runner, "--list").stdout
    assert "RUN scripts/test_brand_new.sh" not in before
    assert "RUN scripts/test_brand_new.sh" in after, "足しただけのテストが CI で走らない（黙って CI の外になる）"


def test_runner_does_not_pass_when_nothing_ran(tree):
    tree.allowlist("# 空")  # テストが 1 本も無い
    empty = _run_runner(tree.runner)
    assert empty.returncode != 0, f"glob が空振りしたのに PASS した:\n{empty.stdout}"

    tree.test("test_only.sh")
    tree.allowlist("scripts/test_only.sh | 未調査 (t043)")  # 全部除外
    all_skipped = _run_runner(tree.runner)
    assert all_skipped.returncode != 0, f"全部除外なのに PASS した:\n{all_skipped.stdout}"


@pytest.mark.parametrize("lines, needle", [
    (["scripts/test_a.sh"], "理由が無い"),
    (["scripts/test_a.sh |   "], "理由が空"),
    (["scripts/test_gone.sh | 未調査 (t043)"], "存在しないファイル"),
    (["scripts/test_a.sh | 未調査 (t043)", "scripts/test_a.sh | 未調査 (t043)"], "重複"),
    (["scripts/other.sh | 未調査 (t043)"], "scripts/test_*.sh ではない"),
], ids=["no-reason", "empty-reason", "missing-file", "duplicate", "not-a-test"])
def test_runner_rejects_a_malformed_allowlist(tree, lines, needle):
    tree.test("test_a.sh")
    (tree.root / "scripts" / "other.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    tree.allowlist(*lines)
    proc = _run_runner(tree.runner)
    assert proc.returncode == 2, f"壊れた除外ファイルで落ちない: rc={proc.returncode}\n{proc.stdout}{proc.stderr}"
    assert needle in proc.stderr
    assert "ran test_a.sh" not in proc.stdout, "除外ファイルが壊れているのにテストを走らせた"


def test_runner_refuses_to_run_without_an_allowlist_file(tree):
    tree.test("test_a.sh")
    proc = _run_runner(tree.runner)
    assert proc.returncode == 2
    assert "除外ファイルが無い" in proc.stderr
