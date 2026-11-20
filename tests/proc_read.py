"""tests/proc_read.py — /proc/<pid>/{stat,comm} をバイト列で読む、テスト共通の補助。

`/proc/<pid>/stat` の 2 番目の欄 (comm) は UTF-8 として不正な任意のバイト列を取れる。
`read_text()` (str) は、並列 (xdist) で走る別のテストが作った無関係な 1 プロセスの名前だけで
`UnicodeDecodeError` (`OSError` のサブクラスではない) を投げ、`except OSError` をすり抜けて
テストを落とす (PR #430 の CI: test_kill_pane_tree_leaves_no_orphans_behind)。

ここでは必ずバイト列で読み、**最後の `)` より後ろ**だけを解釈する (comm 自体が `)` を含みうる)。
comm が要るときは `errors="replace"` で decode する。
"""
from __future__ import annotations

import pathlib
from typing import Optional


def stat_fields(pid: int) -> Optional[list[str]]:
    """`/proc/<pid>/stat` の field 3 以降 (state, ppid, pgrp, ...) を返す。

    読めない (既に居ない) ・形が壊れている場合は None。
    添字: [0]=state [1]=ppid [2]=pgrp [19]=starttime (field 22)。
    """
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    close = raw.rfind(b")")
    if close < 0:
        return None
    rest = raw[close + 1:].decode("ascii", errors="replace").split()
    return rest or None


def state_of(pid: int) -> Optional[str]:
    """state 文字 (R/S/Z/X ...)。読めなければ None。"""
    rest = stat_fields(pid)
    return rest[0] if rest else None


def comm_of(pid: int) -> Optional[str]:
    """`/proc/<pid>/stat` の comm (括弧の中) を errors=replace で decode したもの。"""
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    lp, rp = raw.find(b"("), raw.rfind(b")")
    if lp < 0 or rp < lp:
        return None
    return raw[lp + 1:rp].decode("utf-8", errors="replace")
