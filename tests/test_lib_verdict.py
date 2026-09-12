#!/usr/bin/env python3
"""
tests/test_lib_verdict.py

scripts/lib_verdict.py の pytest 回帰テスト (t002, mission
20260912-verdict-ci-launcher, PR #199 QA Finn 実測 F1/F3 の恒久化)。

scripts/test_lib_verdict.sh (bash, CLI 経由) は既存の不変条件 (完全一致
allowlist・最初の非空行のみ・自己矛盾検出) を広くカバーしているが、CI では
実行されない (scripts/test_*.sh は手動実行のみ)。本ファイルは
`python3 -m pytest tests/` で CI から拾われる場所に、F1/F3 で見つかった
具体的な危険入力を恒久回帰テストとして置く。

F3 (str.splitlines() → str.split("\n")): splitlines() は `\n` 以外にも
`\r` / `\x0b` / `\x0c` / `\x1c` / `\x1d` / `\x1e` / U+0085 / U+2028 /
U+2029 の 9 種で分割する。lib_verdict.py の呼び出し元 (grep / bash の
文字列比較) はいずれも `\n` だけを行区切りとみなすため、旧実装 (splitlines)
はこれらの文字で「1行目の判定語」と「その後に続く否定文」を誤って2行に
分割し、`**Verdict:** approve<SEP>ではない。修正が必要です` を approve と
誤判定していた。

注意 (`\r` について): lib_verdict.py の実際の呼び出し元 (CLI の
`open(path, encoding="utf-8")`) は Python の universal newlines により
ファイル読み込み時点で `\r` を `\n` に変換してしまうため、ファイル経由の
呼び出しでは `\r` 単体の危険入力は (splitlines/split のどちらを使っても
結果的に) 顕在化しない。本テストは `extract_canonical_verdict()` を
関数として直接検証するため、その変換を経由しない生の `\r` も含めて全9種を
検証する — 将来 `newline=""` で開く呼び出し元や、ファイルを経由しない
呼び出し (今回のような直接呼び出しや、structured output 経由の文字列) が
増えても安全側であることを保証するため。

F1 (normalize_plan_review_verdict.py の find_alt_verdict 削除): 削除に
伴い、別表記の救済 (ファイル全体走査) 自体が無くなった。extract_canonical_verdict()
は元々「最初の非空行のみ」を見る設計 (t012) のため、blockquote で引用された
別表記も含め、1行目でない判定語はそもそも読まない — この不変条件が
維持されていることをここでも確認する (review-plan.sh レベルの e2e 相当は
scripts/test_plan_review_verdict_e2e_variants.sh 側)。

実行方法:
  python3 -m pytest tests/test_lib_verdict.py -v
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_verdict  # noqa: E402


# ---------------------------------------------------------------------------
# 正常系 (regression なし)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "verdict",
    ["approve", "revise", "reject"],
)
def test_canonical_verdict_line(verdict):
    text = f"**Verdict:** {verdict}\n"
    assert lib_verdict.extract_canonical_verdict(text) == verdict


def test_no_verdict_language():
    assert lib_verdict.extract_canonical_verdict("# body only, no verdict language") is None


def test_empty_file():
    assert lib_verdict.extract_canonical_verdict("") is None


def test_upper_case_not_accepted():
    # QA t011 NEW-3: 危険側への緩和 (大文字小文字を無視する) を戻した。
    assert lib_verdict.extract_canonical_verdict("**Verdict:** APPROVE") is None


def test_value_with_annotation_not_accepted():
    assert lib_verdict.extract_canonical_verdict("**Verdict:** approve (軽微な指摘あり)") is None


def test_verdict_not_on_first_line_is_unreadable():
    text = "# Plan Review: test\n\n**Verdict:** approve"
    assert lib_verdict.extract_canonical_verdict(text) is None


def test_self_contradiction_is_unreadable():
    text = "**Verdict:** approve\n\nnote\n\n**Verdict:** revise"
    assert lib_verdict.extract_canonical_verdict(text) is None


def test_duplicate_same_value_is_not_a_contradiction():
    text = "**Verdict:** approve\n\nnote\n\n**Verdict:** approve"
    assert lib_verdict.extract_canonical_verdict(text) == "approve"


# ---------------------------------------------------------------------------
# F3: str.splitlines() が扱う 9 種の行区切り文字
# ---------------------------------------------------------------------------

# splitlines() が \n 以外に行区切りとみなす文字 (Python 3 の str.splitlines
# ドキュメント記載の全集合から \n 自身と \r\n を除いたもの)。
_SPLITLINES_ONLY_SEPARATORS = {
    "CR": "\r",
    "VT": "\x0b",
    "FF": "\x0c",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "NEL": "",
    "LS": " ",
    "PS": " ",
}


@pytest.mark.parametrize(
    "name,sep", list(_SPLITLINES_ONLY_SEPARATORS.items()), ids=list(_SPLITLINES_ONLY_SEPARATORS.keys())
)
def test_splitlines_only_separator_does_not_split_first_line(name, sep):
    """F3 (QA Finn 実測): `approve<SEP>否定文` が9種すべてで approve にならないこと。

    修正前 (7642f9b, str.splitlines() 使用) はこれらの区切り文字で
    「**Verdict:** approve」と「ではない。修正が必要です」を2行に分割して
    しまい、1行目だけを見ても approve と誤判定していた。修正後
    (str.split("\\n")) はこれらの文字を行区切りとみなさないため、1行目全体
    (`**Verdict:** approve<SEP>ではない。修正が必要です`) が完全一致の
    3値のどれとも一致せず、判定不能になる。
    """
    text = f"**Verdict:** approve{sep}ではない。修正が必要です"
    assert lib_verdict.extract_canonical_verdict(text) is None, (
        f"{name} ({sep!r}) が行区切りとして扱われ、否定文を無視して approve と誤判定した (F3 regression)"
    )


def test_splitlines_only_separator_regression_would_fail_on_old_splitlines():
    """上のテストが実際に F3 を検出できることの自己確認 (メタテスト)。

    str.splitlines() を使う「修正前」相当のロジックを直接ここで再現し、
    それが approve を返してしまうこと (=このテストが red だったこと) を
    示す。lib_verdict.py 本体を書き換えずに検証するための対照実験。
    """
    text = "**Verdict:** approve\x0bではない。修正が必要です"

    def _old_first_content_line(t):
        for line in t.splitlines():
            if line.strip():
                return line
        return None

    old_first_line = _old_first_content_line(text)
    assert old_first_line == "**Verdict:** approve", (
        "対照実験の前提が崩れている: splitlines() が \\x0b で分割しなくなった"
    )


# ---------------------------------------------------------------------------
# F1 (normalize_plan_review_verdict.py の find_alt_verdict 削除):
# blockquote で引用された別表記が誤って approve にならないこと。
# ---------------------------------------------------------------------------

def test_blockquoted_alt_wording_is_unreadable():
    """F1 (QA Finn 実測): 前 cycle の判定を引用した blockquote は approve にならない。

    旧 normalize_plan_review_verdict.py の find_alt_verdict() はファイル
    全体を走査し、blockquote (`>`) を認識しないヒューリスティックの
    stripper しか持たなかったため、以下の入力を誤って approve と判定して
    いた (今回のミッション以前は「reviewer が明示的に保留と書いている」に
    もかかわらず ready/approve になっていた)。find_alt_verdict 削除後、
    別表記の救済は scripts/review-plan.sh の構造化出力経路に一本化されて
    おり、extract_canonical_verdict() 自身は元々「最初の非空行のみ」しか
    見ないため、この入力は最初から判定不能になる。
    """
    text = (
        "# Plan Review (cycle 2)\n"
        "\n"
        "前回のレビュー:\n"
        "\n"
        "> ## 総合判定: **GO**\n"
        "\n"
        "今回は判断を保留します。追加情報待ち。"
    )
    assert lib_verdict.extract_canonical_verdict(text) is None


def test_first_content_line_skips_leading_blank_lines_only():
    text = "\n\n  \n**Verdict:** approve\nsecond line"
    assert lib_verdict.first_content_line(text) == "**Verdict:** approve"


def test_first_content_line_none_for_blank_text():
    assert lib_verdict.first_content_line("\n\n   \n\t\n") is None
