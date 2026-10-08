"""E5 PR-1 (t009): 報告用の Execution ID を `.crewvia-env` / `plan status` / card から**取り直す例**が agents/ skills/ に無い。

設計: `knowledge/execution.md` §20.2 / §20.4 (Codex P1)。Director の reset → 別 Worker の再 pull で `.crewvia-env` と card の今の試行は
置き換えの試行の ID になる。そこから ID を取り直して名乗ると、古い Worker が置き換えの試行を done / fail できる (§5.2 の捨てた案と同じ穴)。
報告の ID は**その Worker 自身の pull の JSON の `execution_id`** を `--execution ex-…` とリテラルで書く。

対象は `agents/*.md` と `skills/**/*.md` の fenced code block の論理行 (継続を結合・ヒアドキュメント本文とコメント行は見ない) と、
報告コマンドを含む地の文のインラインコード。
Director の代理報告 (`plan status` で見た試行を名指しする) は取り直しではないので、`plan status` は `--execution` と同じ文に無ければ対象外。
"""

from __future__ import annotations

import re
from pathlib import Path

import guard_report
from test_execution_e5_doc_examples_name_the_execution import INLINE_RE, classify, doc_files

ROOT = Path(__file__).resolve().parent.parent
REPORT = r"plan(?:\.sh)?\s+(?:done|fail|needs-director|ready-for-verification|verify-result)\b"

#: (名前, 正規表現)。どれかに当たる 1 文 = 取り直し
REFETCH = [
    ("--execution に変数/コマンド置換", re.compile(r"--execution[ =]+\"?\$")),
    ("EXECUTION_ID を env/コマンドから代入", re.compile(r"\bEXECUTION_ID=\"?\$")),
    ("source .crewvia-env と報告が同じ文", re.compile(r"source\s+\S*\.crewvia-env.*" + REPORT + r"|" + REPORT + r".*source\s+\S*\.crewvia-env")),
    ("current_execution_id を読む", re.compile(r"current_execution_id")),
    ("plan status の ID を --execution に", re.compile(r"plan(?:\.sh)?\s+status.*--execution|--execution.*plan(?:\.sh)?\s+status")),
]

#: {(相対パス, 行の literal): 理由}。取り直しではないと言える行 (今は無い。足すなら理由を書く)
ALLOWLIST: dict[tuple[str, str], str] = {}


def statements(text):
    """`[(行番号, 行の literal, 対象の文字列)]`。fenced は論理行・地の文はインラインコードの中身。字句の状態は共有の `classify`。"""
    out = []
    for item in classify(text)[0]:
        if item[0] == "prose":
            _k, n, raw = item
            for m in INLINE_RE.finditer(raw):
                if re.search(REPORT, m.group(1)):      # 禁止を説明する地の文 (`current_execution_id` の名指し等) は対象外
                    out.append((n, raw, m.group(1)))
        elif item[0] == "stmt":
            _k, n, raw, logical = item
            if not logical.lstrip().startswith("#"):   # コメント行 (Director の代理報告の説明) は実行されない
                out.append((n, raw, logical))
    return out


def refetches(text, rel):
    bad = []
    for lineno, raw, target in statements(text):
        for name, rx in REFETCH:
            if rx.search(target) and (rel, raw.strip()) not in ALLOWLIST:
                bad.append((rel, lineno, name, raw.strip()))
                break
    return bad


def test_no_doc_example_refetches_the_report_id():
    bad, files, total = [], doc_files(), 0
    for f in files:
        rel = str(f.relative_to(ROOT))
        text = f.read_text(encoding="utf-8")
        total += len(statements(text))
        bad += refetches(text, rel)
    guard_report.record("e5-doc-no-refetch", files=len(files), statements=total, violations=len(bad))
    assert len(files) >= 6 and total >= 200, (len(files), total)   # 検出器が壊れて 0 件で緑にならない
    assert not bad, "報告用の ID を取り直す例 (pull の JSON の execution_id を --execution ex-… とリテラルで書く):\n" + "\n".join(
        f"  {rel}:{n}: [{name}] {line}" for rel, n, name, line in bad)


def test_allowlist_has_no_dead_rows():
    seen = set()
    for f in doc_files():
        rel = str(f.relative_to(ROOT))
        for _n, raw, target in statements(f.read_text(encoding="utf-8")):
            if any(rx.search(target) for _name, rx in REFETCH):
                seen.add((rel, raw.strip()))
    assert not [k for k in ALLOWLIST if k not in seen]
    assert all(r.strip() for r in ALLOWLIST.values())


# --- 陽性 / 陰性対照 (検出器自身。直す前の実形) ---

def _hits(text):
    return [n for _rel, n, _name, _l in refetches(text, "x.md")]


def test_control_the_pre_fix_worker_md_form_is_flagged():
    bad = ("```bash\ncd \"$WORKTREE_PATH\" && source .crewvia-env && EXECUTION_ID=\"$CREWVIA_EXECUTION_ID\" && \\\n"
           "  plan done \"$TASK_ID\" --result-file r --mission m ${EXECUTION_ID:+--execution \"$EXECUTION_ID\"}\n```\n")
    assert _hits(bad) == [2]


def test_control_each_refetch_route_is_flagged():
    assert _hits("```bash\nplan done t1 --execution \"$(plan status | grep ex-)\"\n```\n") == [2]
    assert _hits("```bash\nsource .crewvia-env && plan fail t1 h\n```\n") == [2]
    assert _hits("```bash\nEXECUTION_ID=$(jq -r .execution_id pull.json)\n```\n") == [2]
    assert _hits("```bash\nxid=$(jq -r .current_execution_id card.json)\n```\n") == [2]
    assert _hits("card の `current_execution_id` を読んではならない\n") == []        # 禁止を述べる地の文
    assert _hits("```bash\n# plan status の値を --execution に渡す (説明)\n```\n") == []
    assert _hits("`plan.sh status` の値を `plan.sh done t1 --execution X` に\n") == []   # 別々のインラインコード
    assert _hits("`plan.sh done t1 --execution \"$(plan status)\"` で\n") == [1]
    assert _hits("```bash\nplan status --mission m && plan done t1 --execution ex-1\n```\n") == [2]


def test_control_literal_id_and_unrelated_source_pass():
    assert _hits("```bash\nplan done t1 --result-file r --mission m --execution ex-0123456789abcdef0123456789abcdef\n```\n") == []
    assert _hits("```bash\nsource .crewvia-env\necho ok\n```\n") == []
    assert _hits("```bash\nplan done t1 --execution ex-… <<'EOF'\nsource .crewvia-env plan done\nEOF\n```\n") == []
