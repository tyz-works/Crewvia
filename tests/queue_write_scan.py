"""queue / registry への**書き込み**を静的に拾う検出器 (vNext 01a S5 / t020)。

`tests/test_queue_writes_go_through_the_store.py` が使う。読み取り側の先例
(`tests/test_queue_reads_go_through_the_guard.py`) と同じ「allowlist に無い書き込みが 1 つでもあれば赤」の
形だが、表は混ぜない (別ファイル)。設計: `knowledge/state-store.md` §6。

## 何を拾うか

* Python (`scripts/*.py` と、`scripts/*.sh` / `hooks/*.sh` に埋め込まれた**全ての** python ヒアドキュメント。
  先例は 1 ブロック目だけを見ていた) を AST で:
  `open(.., 'w'|'a'|'x'|'+')` (位置引数と `mode=`、`Path.open` の第 1 引数も)・`os.open` の書き込み系フラグ・
  `os.fdopen(fd, 'w')`・`.write_text` / `.write_bytes`・`os.replace/rename/remove/unlink/mkdir/makedirs/...`・
  `Path.unlink/rename/replace/mkdir/touch/...`・`shutil.move/copy*/rmtree`・`json.dump(obj, fp)`・
  `tempfile.mkstemp/mkdtemp/NamedTemporaryFile`。モードが定数でなければ**書き込みとみなす** (見逃しより誤検出)。
* bash (python ヒアドキュメントの外) を字句で: 引用符の外の `>` / `>>` (`/dev/null` と fd 複製 `2>&1` は除く)・
  `tee` / `touch` / `mv` / `cp` / `rm` / `mkdir` / `ln` / `install` / `truncate` / `sed -i` / `dd of=`。
  bash は変数を解決できないので、**書き先が何かは見ない** —— 全部拾い、誤検出は allowlist に理由付きで載せる。

## 拾えないもの (閉じないと明言する)

`exec` / `eval` / `ctypes` 経由・`getattr(os, name)(..)` の動的な名前・`subprocess` で起こした別プログラム
(`subprocess.run(['cp', ...])`) の書き込み・変数に取り置いた関数 (`w = os.replace; w(a, b)`)。
これは「うっかり書き込みを足す」ことを止める補助であって、敵対的な迂回を防ぐ境界ではない
(`tests/test_queue_reads_go_through_the_guard.py` の (A)(B)(C) と同じ割り切り)。
"""

from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass

#: 書き込み系の `os.*`
_OS_WRITE_FUNCS = frozenset({
    'replace', 'rename', 'renames', 'remove', 'unlink', 'rmdir', 'removedirs', 'mkdir', 'makedirs',
    'symlink', 'link', 'truncate', 'ftruncate', 'mkfifo', 'mknod', 'chmod', 'chown', 'utime',
})
#: 書き込み系の `shutil.*`
_SHUTIL_WRITE_FUNCS = frozenset({
    'move', 'copy', 'copy2', 'copyfile', 'copyfileobj', 'copytree', 'copymode', 'copystat', 'rmtree',
})
#: `pathlib.Path` の書き込み系メソッド (`str.replace` と区別するため replace は引数 1 個のときだけ)
_PATH_WRITE_METHODS = frozenset({
    'write_text', 'write_bytes', 'unlink', 'rename', 'replace', 'mkdir', 'touch', 'rmdir',
    'symlink_to', 'hardlink_to', 'chmod',
})
_TEMPFILE_FUNCS = frozenset({'mkstemp', 'mkdtemp', 'NamedTemporaryFile', 'TemporaryFile',
                             'SpooledTemporaryFile'})
_WRITE_MODE_CHARS = frozenset('wax+')
_OS_WRITE_FLAGS = frozenset({'O_WRONLY', 'O_RDWR', 'O_CREAT', 'O_APPEND', 'O_TRUNC', 'O_EXCL'})
_OS_READ_FLAGS = frozenset({'O_RDONLY', 'O_NONBLOCK', 'O_DIRECTORY', 'O_CLOEXEC', 'O_NOFOLLOW'})


@dataclass(frozen=True)
class Site:
    """検出した書き込み 1 件。`key` が allowlist の鍵 (ファイル名, 関数, 正規化したソース)。"""
    file: str
    function: str
    snippet: str
    kind: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.file, self.function, self.snippet)


# ---------------------------------------------------------------------------
# Python (AST)
# ---------------------------------------------------------------------------

def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _mode_is_write(node) -> bool:
    """open() のモード引数が書き込みか。定数でなければ書き込みとみなす。"""
    if node is None:
        return False
    text = _const_str(node)
    if text is None:
        return True
    return bool(_WRITE_MODE_CHARS & set(text))


def _flag_names(node) -> set[str] | None:
    """`os.O_A | os.O_B` の名前の集合。解析できない式なら None。"""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _flag_names(node.left), _flag_names(node.right)
        return None if left is None or right is None else left | right
    if isinstance(node, ast.Attribute) and node.attr.startswith('O_'):
        return {node.attr}
    if isinstance(node, ast.Name) and node.id.startswith('O_'):
        return {node.id}
    getattr_call = (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'getattr')
    if getattr_call:
        return set()          # getattr(os, 'O_DIRECTORY', 0) — 修飾フラグ
    if isinstance(node, ast.Constant) and node.value == 0:
        return set()
    return None


def _receiver_name(func: ast.Attribute):
    value = func.value
    return value.id if isinstance(value, ast.Name) else None


def classify_call(call: ast.Call) -> str | None:
    """この呼び出しが書き込みなら種類 (文字列)、そうでなければ None。"""
    func = call.func
    args, kwargs = call.args, {k.arg: k.value for k in call.keywords if k.arg}

    if isinstance(func, ast.Name):
        if func.id == 'open':
            mode = args[1] if len(args) > 1 else kwargs.get('mode')
            return 'open' if _mode_is_write(mode) else None
        if func.id in _TEMPFILE_FUNCS:
            return 'tempfile'
        return None

    if not isinstance(func, ast.Attribute):
        return None
    attr, recv = func.attr, _receiver_name(func)

    if attr == 'open':
        if recv == 'os':
            flags = _flag_names(args[1]) if len(args) > 1 else _flag_names(kwargs.get('flags'))
            if flags is None:
                return 'os.open(dynamic flags)'
            return 'os.open' if (flags & _OS_WRITE_FLAGS) else None
        if recv in ('io', 'codecs', 'builtins'):
            mode = args[1] if len(args) > 1 else kwargs.get('mode')
            return 'open' if _mode_is_write(mode) else None
        mode = args[0] if args else kwargs.get('mode')        # Path.open(mode)
        return 'Path.open' if _mode_is_write(mode) else None
    if attr == 'fdopen' and recv == 'os':
        mode = args[1] if len(args) > 1 else kwargs.get('mode')
        return 'os.fdopen' if _mode_is_write(mode) else None
    if recv == 'os' and attr in _OS_WRITE_FUNCS:
        return f'os.{attr}'
    if recv == 'shutil' and attr in _SHUTIL_WRITE_FUNCS:
        return f'shutil.{attr}'
    if recv == 'json' and attr == 'dump':
        return 'json.dump'
    if recv == 'tempfile' and attr in _TEMPFILE_FUNCS:
        return f'tempfile.{attr}'
    if attr in _PATH_WRITE_METHODS and recv not in ('os', 'shutil'):
        if attr == 'replace' and (len(args) != 1 or call.keywords):
            return None                       # str.replace(old, new) / datetime.replace(tzinfo=..)
        if isinstance(func.value, ast.Constant):
            return None                                       # 'text'.replace(..)
        return f'Path.{attr}'
    return None


def _enclosing_function_names(tree: ast.AST) -> dict[int, str]:
    """ノード id → 直近の関数名 (無ければ '<module>')。"""
    owner: dict[int, str] = {}

    def visit(node, current):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else current
            owner[id(child)] = name
            visit(child, name)

    owner[id(tree)] = '<module>'
    visit(tree, '<module>')
    return owner


def python_sites(source: str, file: str) -> list[Site]:
    tree = ast.parse(source, filename=file)
    owner = _enclosing_function_names(tree)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = classify_call(node)
        if kind is None:
            continue
        snippet = ast.unparse(node)
        if len(snippet) > 160:
            snippet = snippet[:157] + '...'
        sites.append(Site(file, owner.get(id(node), '<module>'), snippet, kind))
    return sites


# ---------------------------------------------------------------------------
# bash (字句)
# ---------------------------------------------------------------------------

_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_PY_CMD_RE = re.compile(r'\bpython3?\b')
_WRITE_CMDS = ('tee', 'touch', 'mv', 'cp', 'rm', 'mkdir', 'ln', 'install', 'truncate')
_CMD_RE = re.compile(
    r'(?:^|[;&|(`{]|\$\(|\b(?:then|do|else|elif)\b)\s*(?:sudo\s+|command\s+|exec\s+)?'
    r'(' + '|'.join(_WRITE_CMDS) + r')\b(?=\s|$)(.*)')


def split_heredocs(text: str):
    """(bash の行, [python ヒアドキュメントの本文]) に分ける。python 以外のヒアドキュメントの本文は捨てる
    (`cat > f <<EOF` の本文は書き込み先ではない)。"""
    bash_lines: list[str] = []
    python_blocks: list[str] = []
    pending: list[tuple[str, bool]] = []          # (終端語, python か)
    body: list[str] = []
    for line in text.splitlines():
        if pending:
            term, is_python = pending[0]
            if line.strip() == term:
                pending.pop(0)
                if is_python:
                    python_blocks.append('\n'.join(body))
                body = []
            elif is_python:
                body.append(line)
            continue
        bash_lines.append(line)
        # `<<<` (here-string) は除く
        stripped = re.sub(r'<<<', '   ', line)
        for m in _HEREDOC_RE.finditer(stripped):
            pending.append((m.group(2), bool(_PY_CMD_RE.search(line[:m.start()]))))
    return bash_lines, python_blocks


def _strip_comment_and_quotes(line: str, quote=None) -> tuple[str, str, str | None]:
    """(引用符の中身を潰した行, コメントを除いた元の行, 行末で開いたままの引用符)。
    引用符の外の `#` 以降は捨てる。引用符は行をまたぐ (複数行の文字列の中の `>` は書き込みではない)。"""
    out, raw = [], []
    prev = ' '
    for i, ch in enumerate(line):
        if quote:
            raw.append(ch)
            if ch == quote and prev != '\\':
                quote = None
                out.append(ch)
            else:
                out.append('_')
        else:
            if ch == '#' and (prev.isspace() or i == 0):
                break
            raw.append(ch)
            if ch in ('"', "'") and prev != '\\':
                quote = ch
            out.append(ch)
        prev = ch
    return ''.join(out), ''.join(raw).rstrip(), quote


def _redirect_targets(masked: str) -> list[str]:
    """引用符の外の `>` / `>>` の書き先 (`/dev/null` と fd 複製 `>&2` は除く)。"""
    targets = []
    i = 0
    while i < len(masked):
        if masked[i] == '>' and (i + 1 >= len(masked) or masked[i + 1] != '('):
            j = i + 1
            if j < len(masked) and masked[j] == '>':
                j += 1
            if j < len(masked) and masked[j] == '&':          # >&2 / 2>&1 (fd の複製)
                i = j + 1
                continue
            if i > 0 and masked[i - 1] in '=-<':               # `=>` / `->` / `<>`
                i = j
                continue
            if j < len(masked) and masked[j] == '|':
                j += 1
            m = re.match(r'\s*(\S+)', masked[j:])
            target = (m.group(1) if m else '').rstrip(';)&|')
            if target and target != '/dev/null':
                targets.append(target)
            i = j
            continue
        i += 1
    return targets


def bash_sites(bash_lines: list[str], file: str) -> list[Site]:
    sites = []
    quote = None
    for raw in bash_lines:
        masked, plain, quote = _strip_comment_and_quotes(raw, quote)
        if not plain.strip():
            continue
        # 算術・条件式の中の `>` (比較) は書き込みではない
        arith = re.sub(r'\$\(\([^)]*\)\)|\(\([^)]*\)\)|\[\[[^\]]*\]\]', ' ', masked)
        for target in _redirect_targets(arith):
            sites.append(Site(file, '<bash>', f'redirect > {_shorten(target)}', 'redirect'))
        for m in _CMD_RE.finditer(masked):
            sites.append(Site(file, '<bash>', f'{m.group(1)} {_shorten(m.group(2).strip())}', 'command'))
        if re.search(r'\bsed\s+(?:-[A-Za-z]*\s+)*-i', masked):
            sites.append(Site(file, '<bash>', _shorten(plain.strip()), 'sed -i'))
        if re.search(r'\bdd\b[^|;]*\bof=', masked):
            sites.append(Site(file, '<bash>', _shorten(plain.strip()), 'dd'))
    return sites


def _shorten(text: str, limit: int = 100) -> str:
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + '...'


def shell_sites(text: str, file: str) -> list[Site]:
    """`.sh` 1 本ぶん: bash の書き込み + 全 python ヒアドキュメントの書き込み。"""
    bash_lines, blocks = split_heredocs(text)
    sites = bash_sites(bash_lines, file)
    for block in blocks:
        sites.extend(python_sites(block, file))
    return sites


def scan_file(path: pathlib.Path, label: str | None = None) -> list[Site]:
    label = label or path.name
    text = path.read_text(encoding='utf-8')
    if path.suffix == '.py':
        return python_sites(text, label)
    return shell_sites(text, label)


def heredoc_block_count(text: str) -> int:
    return len(split_heredocs(text)[1])
