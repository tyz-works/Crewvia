"""E5 PR-1 (t005): agents/ と skills/ の「報告の 6 コマンドの例」はすべて Execution ID を名乗る。

設計: `knowledge/execution.md` §20.2 (列挙を機械に任せる) / §20.4 の 4。ID なしの例が 1 つ残ると、そのとおりに動いた Worker は
名乗りなしで報告する (10-05 Seo・10-08 Ren の実例)。表を直すだけで終えず、**漏れたら赤**にする。

対象: `agents/*.md` と `skills/**/*.md` の
  (a) fenced code block の行 (継続行は 1 行にまとめる。ヒアドキュメントの本文は見ない = 開始行までが 1 文)
  (b) 地の文のインラインコード (`...`) のうち、コマンドに引数が続く形 (`plan done <id> ...`)
で、`plan` / `plan.sh` の後ろに 6 コマンド (done / fail / needs-director / ready-for-verification / verifying / verify-result) と
引数が続くもの。**同じ行に** `--execution` か `${EXECUTION_ID:+` が無ければ赤。コマンド名だけの言及 (`plan.sh done が…`) は対象外。
例外は理由つきの allowlist (キーは行の literal。該当が無くなったら赤 = 死んだ行を残さない)。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import guard_report

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = r"(?:done|fail|needs-director|ready-for-verification|verifying|verify-result)"
# `plan` / `plan.sh` (前に英数字・`.`・`-` が付かない) + 6 コマンド + 空白 + ASCII の引数の先頭
CALL_RE = re.compile(r"(?<![\w.\-])plan(?:\.sh)?\s+" + COMMANDS + r"\s+(?=[A-Za-z0-9<$\"'\-])")
NAMED_RE = re.compile(r"--execution|\$\{EXECUTION_ID:\+")
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
INLINE_RE = re.compile(r"`([^`\n]+)`")

#: {(相対パス, 行の literal): 理由}。名乗らなくてよい行 (今は無い。足すなら理由を書く)
ALLOWLIST: dict[tuple[str, str], str] = {}


def doc_files():
    files = sorted((ROOT / "agents").glob("*.md")) + sorted((ROOT / "skills").rglob("*.md"))
    return [f for f in files if f.is_file()]


def example_lines(text):
    """`[(行番号, 行の literal, 対象の文字列)]`。対象 = fenced なら論理行 (継続を結合)・地の文ならインラインコードの中身。"""
    out = []
    lines = text.split("\n")
    fenced = False
    heredoc_end = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if heredoc_end is not None:
            if stripped == heredoc_end:
                heredoc_end = None
            i += 1
            continue
        if stripped.startswith("```"):
            fenced = not fenced
            i += 1
            continue
        if not fenced:
            for m in INLINE_RE.finditer(raw):
                if CALL_RE.search(m.group(1) + " "):
                    out.append((i + 1, raw, raw))      # 地の文は**行**で見る (表のセル・散文の 1 行)
                    break
            i += 1
            continue
        start = i
        logical = raw
        while logical.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            logical = logical.rstrip()[:-1] + " " + lines[i].strip()
        h = HEREDOC_RE.search(logical)
        if h:
            heredoc_end = h.group(2)
        if CALL_RE.search(logical + " "):
            out.append((start + 1, raw, logical))
        i += 1
    return out


def violations(text, rel):
    bad = []
    for lineno, raw, target in example_lines(text):
        if NAMED_RE.search(target):
            continue
        if (rel, raw.strip()) in ALLOWLIST:
            continue
        bad.append((rel, lineno, raw.strip()))
    return bad


def test_every_report_example_names_the_execution():
    bad, total, files = [], 0, doc_files()
    for f in files:
        rel = str(f.relative_to(ROOT))
        text = f.read_text(encoding="utf-8")
        total += len(example_lines(text))
        bad += violations(text, rel)
    guard_report.record("e5-doc-examples", files=len(files), examples=total, violations=len(bad))
    assert len(files) >= 6, files                      # 対象を取り違えて 0 件で緑にならない
    assert total >= 30, total                          # 検出器が壊れて 0 件で緑にならない (本物は 39 件。直す前の実測)
    assert not bad, "ID を名乗らない報告の例 (--execution か ${EXECUTION_ID:+…} を足す):\n" + "\n".join(
        f"  {rel}:{n}: {line}" for rel, n, line in bad)


def test_allowlist_has_no_dead_rows():
    seen = set()
    for f in doc_files():
        rel = str(f.relative_to(ROOT))
        for _n, raw, target in example_lines(f.read_text(encoding="utf-8")):
            if not NAMED_RE.search(target):
                seen.add((rel, raw.strip()))
    dead = [k for k in ALLOWLIST if k not in seen]
    assert not dead, dead
    assert all(reason.strip() for reason in ALLOWLIST.values())


# ---------------------------------------------------------------------------
# 陽性 / 陰性対照 (検出器自身。本物の文書から切り出した実形)
# ---------------------------------------------------------------------------

def _found(text):
    return [n for n, _raw, t in example_lines(text) if not NAMED_RE.search(t)]


def test_control_fenced_example_without_the_id_is_flagged():
    assert _found("```bash\nplan done \"$TASK_ID\" --result-file - --mission \"$TASK_MISSION\"\n```\n") == [2]


def test_control_fenced_example_with_flag_or_expansion_passes():
    text = ("```bash\n"
            "plan done t001 --execution \"$EXECUTION_ID\" --mission m\n"
            "plan done t001 ${EXECUTION_ID:+--execution \"$EXECUTION_ID\"} --mission m\n"
            "```\n")
    assert _found(text) == []


def test_control_continuation_line_counts_as_one_statement():
    text = "```bash\nplan.sh fail t001 \\\n  --head abc \\\n  --execution \"$EXECUTION_ID\"\n```\n"
    assert _found(text) == []
    assert _found("```bash\nplan.sh fail t001 \\\n  --head abc\n```\n") == [2]


def test_control_heredoc_opener_is_the_statement_and_its_body_is_not_scanned():
    ok = ("```bash\nplan done t001 --result-file - ${EXECUTION_ID:+--execution \"$EXECUTION_ID\"} <<'EOF'\n"
          "plan done は最後に呼ぶ plan.sh done t002 のこと\nEOF\n```\n")
    assert _found(ok) == []
    bad = "```bash\nplan done t001 --result-file - <<'EOF'\nbody\nEOF\n```\n"
    assert _found(bad) == [2]


def test_control_inline_code_with_arguments_is_checked_but_a_bare_mention_is_not():
    assert _found("実装 task の `plan.sh done <id> --pr <N>` が書く\n") == [1]
    assert _found("実装 task の `plan.sh done <id> --pr <N> --execution <ex-…>` が書く\n") == []
    assert _found("`plan.sh done` が自動で bump する。plan.sh done が FAIL を返す\n") == []
    assert _found("verdict LGTM → plan.sh done / 要修正 → plan.sh needs-director\n") == []


def test_control_the_allowlist_key_is_the_literal_line():
    text = "```bash\nplan done t001 --mission m\n```\n"
    assert violations(text, "x.md") == [("x.md", 2, "plan done t001 --mission m")]
    ALLOWLIST[("x.md", "plan done t001 --mission m")] = "対照"
    try:
        assert violations(text, "x.md") == []
        assert violations(text, "y.md") != []
    finally:
        del ALLOWLIST[("x.md", "plan done t001 --mission m")]


@pytest.mark.parametrize("name", ["agents/worker.md", "agents/director.md", "agents/verifier.md", "agents/worker-codex.md",
                                  "skills/crewvia-qa/SKILL.md", "skills/crewvia-plan-review/SKILL.md"])
def test_every_known_caller_document_is_scanned_and_has_examples(name):
    assert (ROOT / name) in doc_files()
    assert example_lines((ROOT / name).read_text(encoding="utf-8")), name
