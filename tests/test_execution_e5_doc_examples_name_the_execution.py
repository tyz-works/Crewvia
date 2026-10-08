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


def heredoc_opener(logical):
    """論理行がヒアドキュメントを開くなら終端語、そうでなければ None。**行頭 `#` の行 (コメント) は開始ではない**
    (`# … <<'RESULT_EOF' … RESULT_EOF` を開始と読むと、終端語が現れず以降が未走査になった。t012)。"""
    if logical.lstrip().startswith("#"):
        return None
    h = HEREDOC_RE.search(logical)
    return h.group(2) if h else None


def classify(text):
    """文書を 1 回走査して `(items, unclosed)` を返す。字句の状態 (fence・ヒアドキュメント) の持ち方はここ 1 か所。

    items の要素:
      ("prose", 行番号, 行)                地の文 (fence の外)
      ("stmt", 行番号, 行, 論理行)          fenced の文 (継続行を結合。ヒアドキュメントは開始行までが 1 文)
      ("body", 行番号, 行)                 ヒアドキュメントの本文として**見なかった**行 (走査漏れの監査に使う)
    unclosed: 終端語が現れないまま fence の終わり / 文書の末尾に達したヒアドキュメントの開始行番号の list。
    fence (```) が閉じたらヒアドキュメントの状態は解除する (閉じない開始があっても以降の文書を飲み込まない)。
    """
    items, unclosed = [], []
    lines = text.split("\n")
    fenced = False
    heredoc_end = None
    opened_at = 0
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if heredoc_end is not None:
            if stripped.startswith("```"):
                unclosed.append(opened_at)
                heredoc_end = None                      # fence の終わりで解除して、下で fence として扱う
            else:
                items.append(("body", i + 1, raw))
                if stripped == heredoc_end:
                    heredoc_end = None
                i += 1
                continue
        if stripped.startswith("```"):
            fenced = not fenced
            i += 1
            continue
        if not fenced:
            items.append(("prose", i + 1, raw))
            i += 1
            continue
        start = i
        logical = raw
        while logical.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            logical = logical.rstrip()[:-1] + " " + lines[i].strip()
        end = heredoc_opener(logical)
        if end is not None:
            heredoc_end, opened_at = end, start + 1
        items.append(("stmt", start + 1, raw, logical))
        i += 1
    if heredoc_end is not None:
        unclosed.append(opened_at)
    return items, unclosed


def example_lines(text):
    """`[(行番号, 行の literal, 対象の文字列)]`。対象 = fenced なら論理行 (継続を結合)・地の文ならインラインコードの中身。"""
    out = []
    for item in classify(text)[0]:
        if item[0] == "prose":
            _k, n, raw = item
            for m in INLINE_RE.finditer(raw):
                if CALL_RE.search(m.group(1) + " "):
                    out.append((n, raw, raw))          # 地の文は**行**で見る (表のセル・散文の 1 行)
                    break
        elif item[0] == "stmt":
            _k, n, raw, logical = item
            if CALL_RE.search(logical + " "):
                out.append((n, raw, logical))
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


# ---------------------------------------------------------------------------
# t012: 走査漏れそのものを赤にする (QA FAIL: コメント行の `<<'RESULT_EOF'` を開始と読み worker.md 677 行以降が未走査だった)
# ---------------------------------------------------------------------------

#: 独立の素朴検出: 6 コマンドの名前を含む行 (字句の状態を持たない。走査器の理解に依らない)
NAIVE_RE = re.compile(r"(?<![\w.\-])plan(?:\.sh)?\s+" + COMMANDS + r"\b")

#: {(相対パス, 行の literal): 理由}。ヒアドキュメントの本文として見なくてよい、コマンド名を含む行 (今は無い)
BODY_ALLOWLIST: dict[tuple[str, str], str] = {}


def unscanned_naive_lines(text, rel):
    """素朴検出に当たるのに、走査器が**見なかった**行 (ヒアドキュメントの本文として飛ばした行)。"""
    return [(rel, n, raw.strip()) for k, n, raw, *_ in classify(text)[0]
            if k == "body" and NAIVE_RE.search(raw) and (rel, raw.strip()) not in BODY_ALLOWLIST]


def test_every_line_naming_a_report_command_is_scanned():
    """素朴検出の集合 ⊆ 走査した行の集合。閉じないヒアドキュメント (= 以降の未走査) も 0 件。"""
    missed, unclosed, bodies = [], [], 0
    for f in doc_files():
        rel = str(f.relative_to(ROOT))
        text = f.read_text(encoding="utf-8")
        items, unc = classify(text)
        missed += unscanned_naive_lines(text, rel)
        unclosed += [(rel, n) for n in unc]
        bodies += sum(1 for it in items if it[0] == "body")
        scanned = {it[1] for it in items if it[0] in ("prose", "stmt")}
        naive = {i + 1 for i, ln in enumerate(text.split("\n")) if NAIVE_RE.search(ln)}
        assert naive - scanned <= {n for _r, n, _l in missed}, (rel, sorted(naive - scanned))
    guard_report.record("e5-doc-scan-coverage", bodies=bodies, missed=len(missed), unclosed=len(unclosed))
    assert not unclosed, f"終端語が現れないヒアドキュメント (以降が未走査): {unclosed}"
    assert not missed, "報告コマンドを含むのに走査していない行:\n" + "\n".join(f"  {r}:{n}: {l}" for r, n, l in missed)


def test_control_a_heredoc_opener_inside_a_comment_does_not_blind_the_rest():
    """worker.md:677-679 の実形。コメント行の `<<'RESULT_EOF' … RESULT_EOF` は開始ではない。"""
    text = ("```bash\n"
            "plan done \"$TASK_ID\" --result-file \"$F\" --execution ex-…\n"
            "# Write が使えない skill は\n"
            "#   plan done \"$TASK_ID\" --result-file - --execution ex-… <<'RESULT_EOF' … RESULT_EOF\n"
            "```\n\n"
            "```bash\nplan done \"$TASK_ID\" --pr 123\n```\n")
    assert _found(text) == [8]                                       # 後ろの ID なしの例を見落とさない
    assert classify(text)[1] == [] and not [i for i in classify(text)[0] if i[0] == "body"]


def test_control_dropping_the_id_from_the_line_after_a_comment_opener_is_red():
    """worker.md:698 相当: コメントの開始の後ろの行の ID を外す / 取り直し形にすると赤になる。"""
    head = "```bash\n# 説明 <<'RESULT_EOF' … RESULT_EOF\n"
    assert _found(head + "plan done \"$TASK_ID\" --result-file r --mission m --pr 123\n```\n") == [3]
    assert _found(head + "plan done \"$TASK_ID\" --result-file r --mission m --pr 123 --execution ex-…\n```\n") == []


def test_control_a_fence_close_ends_an_unclosed_heredoc():
    text = ("```bash\nplan done t1 --result-file - --execution ex-… <<'EOF'\nbody without terminator\n```\n\n"
            "```bash\nplan fail t2 --head abc\n```\n")
    items, unclosed = classify(text)
    assert unclosed == [2]                                           # 閉じない開始は数える
    assert _found(text) == [7]                                       # fence を越えて以降を飲み込まない


def test_control_the_audit_flags_a_command_line_skipped_as_heredoc_body():
    text = "```bash\ncat <<'EOF'\nplan done t1 --pr 1\nEOF\n```\n"
    assert unscanned_naive_lines(text, "x.md") == [("x.md", 3, "plan done t1 --pr 1")]
    assert _found(text) == []                                        # 走査器は見ない = 監査が拾わなければ盲点になる


def test_control_a_heredoc_opener_in_code_is_still_an_opener():
    ok = "```bash\ncat <<'EOF'\nbody\nEOF\nplan done t1 --pr 1\n```\n"
    assert classify(ok)[1] == [] and _found(ok) == [5]
