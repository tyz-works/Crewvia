"""git_decision_scan.py — 「Git の判断 (branch / base / worktree root) のリテラル」を拾う検出器 (vNext 01b G3 / t012)。

`tests/test_git_decisions_go_through_policy.py` が使う。設計は `knowledge/git-policy.md` §6。

**何を拾うか**: task の branch・base・PR base・worktree の置き場を**コードや文書が自分で決めている**形。判断は
`scripts/lib_git_policy.py` (Resolver) の 1 か所にあり、それ以外が `origin/main` / `--base main` /
`.claude/worktrees` のようなリテラルを書いていたら、Resolver を通さずに決めている (= policy を変えても効かない)。

* Python: AST の文字列定数 (`ast.Constant` と `JoinedStr` の各片)。関数の既定値 (`ref: str = "origin/main"`) と、
  `"main"` に**完全一致**する定数 (`branch="main"`) も拾う。docstring (関数・クラス・モジュールの先頭の式) は除く。
  `os.path.join(x, '.claude', 'worktrees')` のような**連続した 2 つの定数**も拾う。
* bash: `queue_write_scan._lex_line` (コメント・heredoc の本文を分ける字句分割器。t035 で、コメント中の `<<X` が
  残りを読み飛ばす盲点を直した版) で、コメントを除いたコードの行に正規表現を当てる。heredoc の本文が Python
  (`python3 - <<'PYEOF'`) なら、Python の規則で全ブロックを見る。
* 文書 (`agents/*.md`・`skills/*/SKILL.md`): **fenced code block の中だけ**。地の文は対象外 (ガードを説明する文が
  ガードに掛かる型。memory red-proof-for-a-text-pattern-guard-trips-itself)。

キーは `(ファイル, 関数)`。bash の関数は行頭の `name() {` から行頭の `}` まで。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import queue_write_scan as qws  # noqa: E402

#: bash の行 / 文書のコードブロックの行に当てる正規表現 (名前, パターン)。
#: `main` だけでなく**任意のリテラル**を拾う (G3 の QA t013: `--base develop`・`origin/develop` は赤にならなかった)。
#: 変数・コマンド置換・glob・プレースホルダ (`$PR_BASE`・`${DIFF_REF}`・`origin/*`・`origin/<pr_base>`) は通す:
#: リテラルは**英字で始まる語**だけ。`origin/` の直後・`--base` の値・`..HEAD` の直前がそれに当たるもの。
_LIT = r"[A-Za-z][\w.\-]*"
BASH_PATTERNS = (
    ("origin/<literal>", re.compile(rf"\borigin/{_LIT}")),
    ("--base <literal>", re.compile(rf"--base[ =]+['\"]?{_LIT}")),
    ("<literal>..HEAD", re.compile(rf"(?<![\w$}}\"'/.\-]){_LIT}(?:/{_LIT})*\.{{2,3}}HEAD\b")),
    ("main...", re.compile(r"\bmain\.\.\.?")),
    ("refs main", re.compile(r"refs/(?:heads|remotes/origin)/main\b")),
    ("main: refspec", re.compile(r"['\"]main:")),
    (".claude/worktrees", re.compile(r"\.claude/worktrees")),
    ("task/$ branch", re.compile(r"\btask/\$")),
)

#: Python の文字列定数に当てる (部分一致)。
PY_SUBSTRINGS = ("refs/remotes/origin/main", "refs/heads/main", ".claude/worktrees")
#: Python の定数中の `origin/<literal>` / `--base <literal>` (`origin/main` を含む。`origin/{x}` や `origin/*` は通す)。
_PY_LITERAL_RES = (
    ("origin/<literal>", re.compile(rf"\borigin/{_LIT}")),
    ("--base <literal>", re.compile(rf"--base[ =]+['\"]?{_LIT}")),
)
# branch の pattern の形: `task/<成分>/<成分>` で置換子 `{` を含む (`task/{tid}: msg` のような lint のメッセージは 1 つ目の / の後に
# 空白が来るので当たらない)。
_PY_TASK_BRANCH_RE = re.compile(r"^task/\S*\{\S*/\S|^task/\S+/\S*\{")


@dataclasses.dataclass(frozen=True)
class Hit:
    file: str
    function: str
    what: str      # 拾った形の名前
    snippet: str
    line: int


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _joined_pieces(node: ast.JoinedStr) -> str:
    return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}" for v in node.values)


def python_hits(source: str, file: str, line_offset: int = 0) -> list[Hit]:
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    funcs = qws._enclosing_function_names(tree)
    hits: list[Hit] = []

    def add(node, what, text):
        hits.append(Hit(file, funcs.get(id(node), "<module>"), what, _short(text), getattr(node, "lineno", 0) + line_offset))

    joined_children: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                joined_children.add(id(v))

    for node in ast.walk(tree):
        text = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip or id(node) in joined_children:
                continue
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = _joined_pieces(node)
        elif isinstance(node, ast.Call):
            # os.path.join(x, '.claude', 'worktrees') — 連続した 2 つの定数
            consts = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None for a in node.args]
            for a, b in zip(consts, consts[1:]):
                if a == ".claude" and b == "worktrees":
                    add(node, ".claude/worktrees (joined)", f"{a!r}, {b!r}")
            continue
        if text is None:
            continue
        if text == "main":
            add(node, '"main" (exact)', text)
            continue
        found = next((needle for needle in PY_SUBSTRINGS if needle in text), None) \
            or next((name for name, pat in _PY_LITERAL_RES if pat.search(text)), None) \
            or ("task/ pattern" if _PY_TASK_BRANCH_RE.match(text) else None)
        if found:
            add(node, found, text)
    return hits


# ---------------------------------------------------------------------------
# bash / 文書のコードブロック
# ---------------------------------------------------------------------------

_FUNC_OPEN_RE = re.compile(r"^(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")


def bash_line_hits(lines: list[str], file: str, line_offset: int = 0) -> list[Hit]:
    """bash の行 (heredoc の本文は含まない) からの検出。コメントは `_lex_line` が落とす。"""
    hits: list[Hit] = []
    stack: list = [["code", 0]]
    func = "<top>"
    for i, raw in enumerate(lines):
        m = _FUNC_OPEN_RE.match(raw)
        line_func = func
        if m:
            line_func = m.group(1)
            if not raw.rstrip().endswith("}"):      # `name() { ...; }` は 1 行で閉じる (次の行へ持ち越さない)
                func = line_func
        _masked, plain, _found = qws._lex_line(raw, stack)
        if plain.strip():
            for name, pat in BASH_PATTERNS:
                if pat.search(plain):
                    hits.append(Hit(file, line_func, name, _short(plain.strip()), i + 1 + line_offset))
        if raw.startswith("}"):
            func = "<top>"
    return hits


def shell_hits(text: str, file: str) -> list[Hit]:
    """`.sh` / 拡張子なしの bash スクリプト 1 本: bash の行 + 全 python heredoc の本文 (Python の規則)。"""
    bash_lines, blocks = qws.split_heredocs(text)
    hits = bash_line_hits(bash_lines, file)
    for block in blocks:
        hits.extend(python_hits(block, file))
    return hits


def file_hits(path: pathlib.Path, label: str) -> list[Hit]:
    text = path.read_text(encoding="utf-8")
    first = text.splitlines()[0] if text else ""
    if path.suffix == ".py" or (not path.suffix and "python" in first):
        return python_hits(text, label)
    return shell_hits(text, label)


_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")


def code_blocks(markdown: str) -> list[list[str]]:
    """fenced code block の中身 (行のリスト)。地の文は含まない。"""
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    fence = ""
    for line in markdown.splitlines():
        m = _FENCE_RE.match(line)
        if cur is None:
            if m:
                cur, fence = [], m.group(2)[0] * len(m.group(2))
        else:
            if m and m.group(2).startswith(fence) and not m.group(3).strip():
                blocks.append(cur)
                cur = None
            else:
                cur.append(line)
    if cur is not None:
        blocks.append(cur)   # 閉じないブロックも検査する (末尾まで code)
    return blocks


def doc_hits(markdown: str, file: str) -> tuple[list[Hit], int]:
    """(拾ったもの, code block の数)。キーの関数は `<code block>` に固定 (block の番号は文書の編集で動く)。"""
    hits: list[Hit] = []
    blocks = code_blocks(markdown)
    for block in blocks:
        for h in bash_line_hits(block, file):
            hits.append(dataclasses.replace(h, function="<code block>"))
    return hits, len(blocks)
