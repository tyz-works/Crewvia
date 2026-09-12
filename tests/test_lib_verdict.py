#!/usr/bin/env python3
"""
tests/test_lib_verdict.py

scripts/lib_verdict.py の pytest 回帰テスト (t002, mission
20260912-verdict-ci-launcher, PR #199 QA Finn 実測 F1/F3 の恒久化 /
t015 FAIL-2 / t018 FAIL-A)。

scripts/test_lib_verdict.sh (bash, CLI 経由) と同じ不変条件を、関数呼び出しと
CLI の両方から確認する。PR の CI では "Python Unit Tests (pytest)" job が
`tests/` を実行する (PR #199 の run 34691557924 で確認)。scripts/test_*.sh の
大半は CI で実行されないため、CI で守りたい回帰はこちらに置く。

t018 (QA t016 Finn FAIL-A / Kai-codex t004 2 回目 P1): lib_verdict は
VALID / NO_SIGN / VIOLATION の 3 状態を返す。救済 (構造化出力で plan_review.md の
verdict を補う) が許されるのは NO_SIGN だけで、verdict 行の兆候が 1 つでも
ある VALID 以外のファイルは VIOLATION になる。

red の証拠について (QA t016 FAIL-B): 以前ここにあった
test_splitlines_only_separator_regression_would_fail_on_old_splitlines は、
旧ロジック (splitlines) をテスト内に書き写して実行していただけで、旧コードを
一度も動かしていなかった。red の証拠にならないため削除した。脆弱版を実際に
動かす red 確認は scripts/test_review_plan_verdict_binding_e2e.sh --verify-red
(脆弱版の commit から git で取り出したファイルを、blob id を表示して実行する)
に置いている。

実行方法:
  python3 -m pytest tests/test_lib_verdict.py -v
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_VERDICT_PY = SCRIPTS_DIR / "lib_verdict.py"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_verdict  # noqa: E402
from lib_verdict import (  # noqa: E402
    EXIT_NO_SIGN,
    EXIT_USAGE,
    EXIT_VALID,
    EXIT_VIOLATION,
    STATE_NO_SIGN,
    STATE_VALID,
    STATE_VIOLATION,
)


def _state(text):
    state, verdict, _reason = lib_verdict.classify_verdict(text)
    return state, verdict


# ---------------------------------------------------------------------------
# VALID (regression なし)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict", ["approve", "revise", "reject"])
def test_canonical_verdict_line(verdict):
    text = f"**Verdict:** {verdict}\n"
    assert _state(text) == (STATE_VALID, verdict)
    assert lib_verdict.extract_canonical_verdict(text) == verdict


def test_leading_blank_lines_and_body_without_sign():
    text = "\n\n**Verdict:** revise\n\n# Plan Review: m\n\n## Summary\n修正が必要。\n"
    assert _state(text) == (STATE_VALID, "revise")


def test_template_note_without_colon_is_not_a_sign():
    # agents/plan_reviewer.md の雛形の注記。verdict の直後がコロンでないので
    # 兆候にしない (正常系の plan_review.md を VIOLATION にしないため)。
    text = "**Verdict:** approve\n\n## Issues\n(verdict が revise/reject の場合のみ記載)\n"
    assert _state(text) == (STATE_VALID, "approve")


# ---------------------------------------------------------------------------
# NO_SIGN (構造化出力による救済が許される唯一の状態)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n   \n",
        "# body only, no verdict language",
        "## 総合判定: GO",
        "# Plan Review (cycle 2)\n\n前回のレビュー:\n\n> ## 総合判定: **GO**\n\n今回は判断を保留します。",
        # 意図的な残余: コロンの無いラベルは兆候にしない (モジュール docstring 参照)
        "**Verdict** revise",
    ],
    ids=["empty", "blank", "no_verdict_language", "alt_wording_go", "blockquoted_alt_wording", "bold_no_colon"],
)
def test_no_sign(text):
    assert _state(text) == (STATE_NO_SIGN, None)
    assert lib_verdict.extract_canonical_verdict(text) is None


# ---------------------------------------------------------------------------
# VIOLATION (兆候があるのに VALID ではない → 構造化出力の値に関係なく fail-closed)
# ---------------------------------------------------------------------------

_VIOLATIONS = {
    # QA t016 (Finn) FAIL-A の実測入力
    "k1_revise_then_quoted_approve": "**Verdict:** revise\n\n書式例:\n\n**Verdict:** approve\n",
    "k2_approve_then_revise": "**Verdict:** approve\n\nnote\n\n**Verdict:** revise",
    "k3_revise_annotated": "**Verdict:** revise (重大な指摘あり)",
    "k4_revise_upper": "**Verdict:** REVISE",
    "k5_reject_then_approve": "**Verdict:** reject\n\n**Verdict:** approve",
    # Director 設計判断2: 複数あって値が同じ も VIOLATION
    "duplicate_same_value": "**Verdict:** approve\n\nnote\n\n**Verdict:** approve",
    # 記法 (どこに書いても同じ)
    "blockquote_quote": "**Verdict:** revise\n\n> **Verdict:** approve",
    "indented_quote": "**Verdict:** revise\n\n    **Verdict:** approve",
    "list_quote": "**Verdict:** revise\n\n- **Verdict:** approve",
    "fenced_quote": "**Verdict:** revise\n\n```\n**Verdict:** approve\n```",
    "html_comment_quote": "**Verdict:** revise\n\n<!-- **Verdict:** approve -->",
    "inline_code_quote": "**Verdict:** revise\n\n本文中の `**Verdict:** approve` という書式",
    "not_first_line": "# Plan Review: test\n\n**Verdict:** approve",
    # 1 行目の書式
    "annotation": "**Verdict:** approve (軽微な指摘あり)",
    "upper_value": "**Verdict:** APPROVE",
    "unknown_word": "**Verdict:** STOP",
    "empty_value": "**Verdict:**",
    "template_three_words": "**Verdict:** approve | revise | reject",
    # 兆候の正規化 (Director 定義の上位集合)
    "lowercase_label": "**verdict:** revise",
    "upper_label": "**VERDICT:** revise",
    "no_bold": "Verdict: revise",
    "colon_outside_bold": "**Verdict**: revise",
    "underscore_bold": "__Verdict:__ revise",
    "fullwidth_label": "＊＊Ｖｅｒｄｉｃｔ：＊＊ revise",
    "zero_width_in_label": "**Ver​dict:** revise",
    "bom_before_label": "﻿**Verdict:** approve",
}


@pytest.mark.parametrize("text", list(_VIOLATIONS.values()), ids=list(_VIOLATIONS.keys()))
def test_violation(text):
    assert _state(text) == (STATE_VIOLATION, None)
    assert lib_verdict.extract_canonical_verdict(text) is None


@pytest.mark.parametrize(
    "line,expected",
    [
        ("**Verdict:** approve", True),
        ("> - `**verdict:**` x", True),
        ("Verdict : revise", True),
        ("## 総合判定: GO", False),
        ("(verdict が revise/reject の場合のみ記載)", False),
        ("<!-- authoritative verdict written by scripts/review-plan.sh from the claude --json-schema structured output -->", False),
    ],
)
def test_is_verdict_sign(line, expected):
    assert lib_verdict.is_verdict_sign(line) is expected


# ---------------------------------------------------------------------------
# F3: str.splitlines() が扱う 9 種の行区切り文字
# ---------------------------------------------------------------------------

_SPLITLINES_ONLY_SEPARATORS = {
    "CR": "\r",
    "VT": "\x0b",
    "FF": "\x0c",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "NEL": "\x85",
    "LS": " ",
    "PS": " ",
}


@pytest.mark.parametrize(
    "name,sep", list(_SPLITLINES_ONLY_SEPARATORS.items()), ids=list(_SPLITLINES_ONLY_SEPARATORS.keys())
)
def test_splitlines_only_separator_does_not_split_first_line(name, sep):
    """F3 (QA Finn 実測): `approve<SEP>否定文` が9種すべてで approve にならないこと。

    1 行目全体 (`**Verdict:** approve<SEP>ではない。修正が必要です`) が
    規定形式と完全一致しないため、t018 以降は VIOLATION。
    """
    text = f"**Verdict:** approve{sep}ではない。修正が必要です"
    assert _state(text) == (STATE_VIOLATION, None), (
        f"{name} ({sep!r}) が行区切りとして扱われた (F3 regression)"
    )


def test_first_content_line_skips_leading_blank_lines_only():
    text = "\n\n  \n**Verdict:** approve\nsecond line"
    assert lib_verdict.first_content_line(text) == "**Verdict:** approve"


def test_first_content_line_none_for_blank_text():
    assert lib_verdict.first_content_line("\n\n   \n\t\n") is None


# ---------------------------------------------------------------------------
# CLI (ファイル経由)。scripts/review-plan.sh / wait_for_plan_review.sh は
# すべてこの CLI を呼ぶ。
# ---------------------------------------------------------------------------

def _run_cli(*args):
    proc = subprocess.run(
        [sys.executable, str(LIB_VERDICT_PY), *map(str, args)],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip(), proc.returncode


@pytest.mark.parametrize(
    "name,sep", list(_SPLITLINES_ONLY_SEPARATORS.items()), ids=list(_SPLITLINES_ONLY_SEPARATORS.keys())
)
def test_file_based_cli_separator_does_not_approve(tmp_path, name, sep):
    """FAIL-2 (t015): 9 種の区切り文字すべてで、ファイル経由でも approve にならないこと。"""
    p = tmp_path / f"case_{name}.md"
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(f"**Verdict:** approve{sep}ではない。修正が必要です")
    out, rc = _run_cli(p)
    assert (out, rc) == ("", EXIT_VIOLATION), f"{name} ({sep!r}): stdout={out!r} rc={rc}"


def test_file_based_cli_bare_cr_is_the_documented_fail2_repro(tmp_path):
    p = tmp_path / "bare_cr.md"
    p.write_bytes(b"**Verdict:** approve\rnot approved; revisions required")
    assert _run_cli(p) == ("", EXIT_VIOLATION)


def test_file_based_cli_crlf_still_reads_as_approve(tmp_path):
    p = tmp_path / "crlf.md"
    p.write_bytes(b"**Verdict:** approve\r\n\r\n# Plan Review\r\nWindows-style CRLF file.\r\n")
    assert _run_cli(p) == ("approve", EXIT_VALID)


def test_cli_self_contradiction_is_violation_not_no_sign(tmp_path):
    """QA t016 B_k1 そのもの。t015 までは NO_SIGN と同じ終了コード 1 だった。"""
    p = tmp_path / "k1.md"
    p.write_text("**Verdict:** revise\n\n書式例:\n\n**Verdict:** approve\n", encoding="utf-8")
    assert _run_cli(p) == ("", EXIT_VIOLATION)


def test_cli_alt_wording_is_no_sign(tmp_path):
    p = tmp_path / "go.md"
    p.write_text("# Plan Review\n\n## 総合判定: GO\n", encoding="utf-8")
    assert _run_cli(p) == ("", EXIT_NO_SIGN)


def test_cli_missing_file_is_no_sign(tmp_path):
    assert _run_cli(tmp_path / "missing.md") == ("", EXIT_NO_SIGN)


def test_cli_directory_is_violation(tmp_path):
    d = tmp_path / "dir.md"
    d.mkdir()
    assert _run_cli(d) == ("", EXIT_VIOLATION)


def test_cli_invalid_utf8_is_violation(tmp_path):
    """t015 までは UnicodeDecodeError が未捕捉で rc=1 (= 救済経路) になっていた。"""
    p = tmp_path / "bad.md"
    p.write_bytes("## 総合判定: GO\n".encode("utf-8") + b"\xff\xfe")
    assert _run_cli(p) == ("", EXIT_VIOLATION)


def test_cli_usage():
    assert _run_cli()[1] == EXIT_USAGE
