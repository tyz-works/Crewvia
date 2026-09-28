#!/usr/bin/env python3
"""tests/test_ci_runs_every_tests_sh.py — tests/*.sh は「CI で走る」か「理由付きで除外」のどちらか (t063 / t043 P3-3)。

## 何を止めるか

`tests/*.sh` は 27 本あるのに、どの CI job も走らせていなかった（bats は `tests/*.bats`
だけ拾い、pytest は python の glob なので `.sh` を拾わない）。理由付き allowlist にも
載っていないので「黙って CI の外」の型が `scripts/test_*.sh`（t025 / backlog #30）の
隣のディレクトリにそのまま残っていた（t028 レビュー P3-3、t043 で棚卸しのみ実施）。

`scripts/ci-run-script-tests.sh` / `scripts/ci-script-tests-excluded.txt` /
`tests/test_ci_runs_every_script_test.py` と同じ「形」（glob + 理由付き allowlist +
構造ガード）を `tests/*.sh` にも適用する。コードは共有しない（t063 Result / 各ファイル
冒頭コメント参照）が、検査する項目は同じにする。

1. 全 `tests/*.sh` が、実行 (RUN) か除外 (SKIP) のどちらかに **ちょうど 1 回** 入る（漏れも二重も無い）
2. 除外ファイルの各行は、実在するファイルと **決められた理由** を持つ（理由の無い行・存在しない行・重複は赤）
3. CI の workflow が runner を呼び、`tests/*.sh` を **名指しで** 走らせる step が無い（名指しの一覧が戻らない）
4. runner 自身の振る舞い: 1 本落ちても残りを止めない / 0 本走らせて PASS にしない / 壊れた除外ファイルで落ちる /
   新しく足したテストは何もしなくても走る / コメント判定は行を trim した後だけを見る / path の許容文字は
   ハイフンを含む（`watchdog-idle-e2e.sh` のような名前を扱うため `scripts/test_*.sh` 側の字クラスより広い）
5. 除外理由に「未調査」系の文字列が 1 つも無いこと（t063 の受入条件。t043 の allowlist を
   scripts/test_*.sh 側にだけ残し、ここへは新しい「未調査」を作らない）

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
TESTS_DIR = REPO_ROOT / "tests"
RUNNER = REPO_ROOT / "scripts" / "ci-run-tests-sh.sh"
ALLOWLIST = REPO_ROOT / "scripts" / "ci-tests-sh-excluded.txt"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: 除外の理由は Director 決定の固定表。理由を足したいときはこの表を直す＝レビューに載る。
#: t063 の受入条件: scripts/test_*.sh 側の「未調査 (t043)」をここへ持ち込まない
#: （新しい「未調査」も作らない）。除外は中身から導ける恒久の理由だけ。
REASON_SOURCED_LIB = "テストではない（source される lib。トップレベルに実行文が無い）"
REASON_HISTORICAL_RED_PROOF = "1 回限りの historical red proof（CI 化しない）"
ALLOWED_REASONS = {REASON_SOURCED_LIB, REASON_HISTORICAL_RED_PROOF}

#: allowlist の path とワークフロー内の「名指し」検出、両方が対象ファイル名として認める
#: 字クラス。scripts/ci-run-tests-sh.sh はこの文字列と同じ字クラスを直接埋め込んでおり
#: (bash から python の定数は import できないため)、変えるときは両方を直すこと。
#: scripts/test_*.sh 側 (`[A-Za-z0-9_]+`) と違い、`tests/*.sh` は
#: `watchdog-idle-e2e.sh` のようなハイフン入りの名前を実際に含むため `-` を許容する。
TEST_FILENAME_PATTERN = r"tests/[A-Za-z0-9_-]+\.sh"


def _glob_tests() -> list[str]:
    return sorted(f"tests/{p.name}" for p in TESTS_DIR.glob("*.sh"))


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


def test_every_tests_sh_runs_or_is_allowlisted():
    """runner の実際の `--list` 出力（実際の呼び出し形）と、独立に glob した一覧を突き合わせる。"""
    all_tests = _glob_tests()
    proc = _run_runner(RUNNER, "--list")
    assert proc.returncode == 0, f"runner --list が落ちた: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    run = [ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if ln.startswith("RUN ")]
    skip = [ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if ln.startswith("SKIP ")]

    print(f"\n[ci-runs-every-tests-sh] 検査した tests/*.sh: {len(all_tests)} 本"
          f"（CI で走る {len(run)} / 理由付き除外 {len(skip)}）")
    assert all_tests, "tests/*.sh が 0 本。glob が空振りしている（0 件で PASS にしない）"
    assert run, "CI で走るテストが 0 本。全部除外されている（0 件で PASS にしない）"
    assert len(run) == len(set(run)) and len(skip) == len(set(skip)), "RUN / SKIP に重複がある"
    assert not (set(run) & set(skip)), f"RUN と SKIP の両方に載っている: {sorted(set(run) & set(skip))}"
    assert sorted(run + skip) == all_tests, (
        "runner の RUN+SKIP が glob と一致しない（黙って CI の外に残るテストがある）\n"
        f"  glob だけ: {sorted(set(all_tests) - set(run + skip))}\n"
        f"  runner だけ: {sorted(set(run + skip) - set(all_tests))}")
    assert f"検査した tests/*.sh: {len(all_tests)} 本" in proc.stdout, "runner が検査件数を出していない"


def test_allowlist_lines_have_a_real_file_and_an_allowed_reason():
    entries, problems = _parse_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
    print(f"\n[ci-runs-every-tests-sh] 検査した除外行: {len(entries)} 行")
    assert not problems, "除外ファイルの形が壊れている:\n  " + "\n  ".join(problems)

    bad: list[str] = []
    seen: set[str] = set()
    for no, path, reason in entries:
        if not re.fullmatch(TEST_FILENAME_PATTERN, path):
            bad.append(f"{no}: tests/*.sh ではない: {path}")
        elif not (REPO_ROOT / path).is_file():
            bad.append(f"{no}: 存在しないファイル: {path}")
        if reason not in ALLOWED_REASONS:
            bad.append(f"{no}: 理由が {sorted(ALLOWED_REASONS)} のどれでもない: {reason!r} ({path})")
        if path in seen:
            bad.append(f"{no}: 重複: {path}")
        seen.add(path)
    assert not bad, "除外ファイルに不正な行がある:\n  " + "\n  ".join(bad)


def test_no_uninvestigated_reason_style_string_exists():
    """t063 の受入条件: 『未調査』を含む理由が 1 行も無いこと（scripts/test_*.sh 側の

    「未調査 (t043)」を再利用しない・新しい task id 付きの類似理由も作らない）。
    """
    entries, _ = _parse_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
    uninvestigated = [path for _, path, reason in entries if "未調査" in reason]
    print(f"\n[ci-runs-every-tests-sh] 検査した除外行: {len(entries)} 行中 『未調査』含む: {len(uninvestigated)} 行")
    assert not uninvestigated, f"『未調査』を含む理由が残っている: {uninvestigated}"


def test_runner_skips_exactly_the_allowlisted_files():
    """runner の SKIP は除外ファイルの行と過不足なく一致する（除外ファイルを読み飛ばしていない）。"""
    entries, _ = _parse_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
    listed = sorted(path for _, path, _ in entries)
    proc = _run_runner(RUNNER, "--list")
    skipped = sorted(ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if ln.startswith("SKIP "))
    assert skipped == listed, f"除外ファイルの行と runner の SKIP が違う: {listed} vs {skipped}"


# --- 3: workflow が runner を呼び、名指しの一覧が戻っていない -------------------------------


def test_workflow_runs_the_runner_and_names_no_tests_sh():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    runs = [
        (job_name, step["run"])
        for job_name, job in doc["jobs"].items()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]
    print(f"\n[ci-runs-every-tests-sh] 検査した workflow の run step: {len(runs)} 件")
    assert runs, "workflow から run step が 1 件も読めない（0 件で PASS にしない）"

    calls = [job for job, cmd in runs if "scripts/ci-run-tests-sh.sh" in cmd]
    assert calls, "workflow が scripts/ci-run-tests-sh.sh を呼んでいない（glob の実行が CI に載っていない）"
    named = [(job, m.group(0)) for job, cmd in runs for m in re.finditer(TEST_FILENAME_PATTERN, cmd)]
    assert not named, (
        "workflow が tests/*.sh を名指しで走らせている。名指しの一覧に戻ると、足したテストが"
        f"黙って CI の外に残る。runner (glob) に任せ、走らせたくないものは除外ファイルへ: {named}")


# --- 4: runner の振る舞い（一時 tree。本番の tests/ には触れない） -----------------------------


@pytest.fixture
def tree(tmp_path):
    """scripts/ci-run-tests-sh.sh だけを写した一時 tree。テストと除外ファイルは各テストが置く。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (tmp_path / "tests").mkdir()
    dest = scripts / RUNNER.name
    dest.write_text(RUNNER.read_text(encoding="utf-8"), encoding="utf-8")
    dest.chmod(0o755)

    class Tree:
        root = tmp_path
        runner = dest

        @staticmethod
        def test(name: str, exit_code: int = 0) -> None:
            (tmp_path / "tests" / name).write_text(
                f'#!/usr/bin/env bash\necho "ran {name}"\nexit {exit_code}\n', encoding="utf-8")

        @staticmethod
        def allowlist(*lines: str) -> None:
            (scripts / "ci-tests-sh-excluded.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return Tree


def test_runner_keeps_going_after_a_failure_and_reports_each(tree):
    tree.test("test_a.sh", exit_code=1)
    tree.test("test_b.sh")
    tree.allowlist("# 空")
    proc = _run_runner(tree.runner)
    assert proc.returncode == 1
    assert "FAIL tests/test_a.sh" in proc.stdout
    assert "PASS tests/test_b.sh" in proc.stdout, "1 本の失敗で残りが走らなくなっている"
    assert "ran test_b.sh" in proc.stdout
    assert "検査した tests/*.sh: 2 本（実行 2 / 除外 0 / 失敗 1）" in proc.stdout


def test_runner_picks_up_a_newly_added_test_without_any_registration(tree):
    tree.test("watchdog-e2e-example.sh")
    tree.allowlist("# 空")
    before = _run_runner(tree.runner, "--list").stdout
    tree.test("brand-new.sh")
    after = _run_runner(tree.runner, "--list").stdout
    assert "RUN tests/brand-new.sh" not in before
    assert "RUN tests/brand-new.sh" in after, "足しただけのテストが CI で走らない（黙って CI の外になる）"


def test_runner_does_not_pass_when_nothing_ran(tree):
    tree.allowlist("# 空")  # テストが 1 本も無い
    empty = _run_runner(tree.runner)
    assert empty.returncode != 0, f"glob が空振りしたのに PASS した:\n{empty.stdout}"

    tree.test("only.sh")
    tree.allowlist("tests/only.sh | テストではない（source される lib。トップレベルに実行文が無い）")  # 全部除外
    all_skipped = _run_runner(tree.runner)
    assert all_skipped.returncode != 0, f"全部除外なのに PASS した:\n{all_skipped.stdout}"


@pytest.mark.parametrize("lines, needle", [
    (["tests/a.sh"], "理由が無い"),
    (["tests/a.sh |   "], "理由が空"),
    (["tests/gone.sh | 1 回限りの historical red proof（CI 化しない）"], "存在しないファイル"),
    (["tests/a.sh | 1 回限りの historical red proof（CI 化しない）",
      "tests/a.sh | 1 回限りの historical red proof（CI 化しない）"], "重複"),
    (["scripts/other.sh | 1 回限りの historical red proof（CI 化しない）"], "tests/*.sh ではない"),
], ids=["no-reason", "empty-reason", "missing-file", "duplicate", "not-in-tests-dir"])
def test_runner_rejects_a_malformed_allowlist(tree, lines, needle):
    tree.test("a.sh")
    (tree.root / "scripts" / "other.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    tree.allowlist(*lines)
    proc = _run_runner(tree.runner)
    assert proc.returncode == 2, f"壊れた除外ファイルで落ちない: rc={proc.returncode}\n{proc.stdout}{proc.stderr}"
    assert needle in proc.stderr
    assert "ran a.sh" not in proc.stdout, "除外ファイルが壊れているのにテストを走らせた"


def test_runner_accepts_a_hyphenated_filename(tree):
    """`watchdog-idle-e2e.sh` のようなハイフン入りの名前が、scripts/test_*.sh 側と違い
    ここでは正しく受理されること（除外にも RUN にも使える）。"""
    tree.test("watchdog-idle-e2e.sh")
    tree.test("b.sh")  # --list は「走らせた 0 本」を空振りとして拒否するので、除外対象以外を 1 本置く
    tree.allowlist("tests/watchdog-idle-e2e.sh | 1 回限りの historical red proof（CI 化しない）")
    proc = _run_runner(tree.runner, "--list")
    assert proc.returncode == 0, f"ハイフン入りの正当な除外行で落ちた: {proc.stdout}{proc.stderr}"
    assert "SKIP tests/watchdog-idle-e2e.sh" in proc.stdout


def test_runner_comment_check_only_matches_a_truly_leading_hash(tree):
    """先頭が空白1個+行のどこかに # があるだけの正当な行を、runner がコメントとして
    黙って無視してはいけない（trim してから先頭 # だけを見る。pytest の parser と同じ規則。
    scripts/ci-run-script-tests.sh の t043 P3-1 と同じ規則をここでも踏襲する）。
    """
    tree.test("a.sh")
    tree.test("b.sh")  # --list は「走らせた 0 本」を空振りとして拒否するので、除外対象以外を 1 本置く
    tree.allowlist(" tests/a.sh | 1 回限りの historical red proof（CI 化しない）用の備考 #123")
    proc = _run_runner(tree.runner, "--list")
    assert proc.returncode == 0, f"--list が落ちた: {proc.stdout}{proc.stderr}"
    skipped = [ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if ln.startswith("SKIP ")]
    assert skipped == ["tests/a.sh"], (
        "先頭に空白 1 個 + 理由の中に # がある正当な行を、runner がコメットとして"
        f"読み飛ばした（除外として認識されない）: {proc.stdout}")


def test_runner_refuses_to_run_without_an_allowlist_file(tree):
    tree.test("a.sh")
    proc = _run_runner(tree.runner)
    assert proc.returncode == 2
    assert "除外ファイルが無い" in proc.stderr
