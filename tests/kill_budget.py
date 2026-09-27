#!/usr/bin/env python3
"""tests/kill_budget.py — 「殺してよい pid」の最後の関門 (2026-09-27 の自爆を二度とやらないため)。

## なぜ別ファイルなのか

`leaked_descendants.py` の判定 (`_belongs`) を **1 箇所** 壊すと、走査は同 uid の全プロセスを
「漏れた子孫」と答える。2026-09-27 14:04、ガードの変異テスト (`G1`: `_belongs` の頭に
`return "any", True` を注入) が実際にそれをやり、`systemd --user` / tmux / WSL の
キープアライブ / n8n コンテナ (uid 1000) を SIGKILL して WSL ごと落とした。

判定と同じファイルに安全弁を置くと、判定を壊す変異が安全弁も一緒に無効化しうる。ここは
**判定とは独立した第二の関門**であり、変異テストの対象にしない。判定が何と答えても、
次に当たる pid は殺さない:

- `pid <= 1`
- 自分 (`os.getpid()`) と **その祖先すべて**
- 自分のセッションリーダー / プロセスグループリーダー
- **自分より古いプロセス** — テストが起こした子孫が、テスト自身より先に生まれていることはない
- 許可が `MAX_KILLS` 件を超えたら **1 件も殺さない** — 「本当に N 個漏れた」ではなく
  判定の事故を疑う方に倒す

最後の 2 つは今回の事故を単独で止める: 巻き込まれた 4 つはどれも pytest より 11 日古く、
件数も数百件だった。上限は `CREWVIA_LEAK_KILL_BUDGET` で変えられる。

第三の関門は OS 側にある: 変異テストは PID 名前空間の中で走らせる
(`unshare -Urpf --mount-proc`)。名前空間の中からは外の pid が `/proc` に見えず、
`os.kill` も ESRCH になるので、判定がどう壊れても外には届かない。
"""

from __future__ import annotations

import os
import pathlib

_PROC = pathlib.Path("/proc")

#: 1 回の kill_all で殺してよい上限。これを超えたら判定の事故として 1 件も殺さない。
DEFAULT_MAX_KILLS = 16
BUDGET_VAR = "CREWVIA_LEAK_KILL_BUDGET"

#: 祖先を辿るときの打ち切り (ppid が輪になっている異常系でも返る)。
_MAX_WALK = 4096


class Refusal:
    """殺さなかった 1 件とその理由。"""

    def __init__(self, pid: int, reason: str):
        self.pid, self.reason = pid, reason

    def describe(self) -> str:
        return f"pid={self.pid}: {self.reason}"

    def __repr__(self) -> str:      # デバッグ用
        return f"Refusal({self.pid}, {self.reason!r})"


def _ppid_and_start(pid: int):
    """(ppid, starttime_ticks) — 読めなければ None。`comm` は括弧の中に空白を含みうる。"""
    try:
        raw = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    rp = raw.rfind(")")
    if rp < 0:
        return None
    rest = raw[rp + 2:].split()
    try:
        return int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None


def ancestors(pid: int) -> set[int]:
    """`pid` の祖先の pid 集合 (`pid` 自身は含まない)。辿れなくなったら打ち切る。"""
    out: set[int] = set()
    cur, steps = pid, 0
    while cur > 1 and steps < _MAX_WALK:
        got = _ppid_and_start(cur)
        if got is None:
            break
        cur = got[0]
        if cur in out or cur <= 0:
            break
        out.add(cur)
        steps += 1
    return out


def max_kills(environ=None) -> int:
    """上限。読めない値は既定に倒す (壊れた環境変数で上限が消えないように)。"""
    env = os.environ if environ is None else environ
    raw = env.get(BUDGET_VAR)
    if raw is None:
        return DEFAULT_MAX_KILLS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_KILLS
    return value if value >= 0 else DEFAULT_MAX_KILLS


def protected(me: int | None = None) -> set[int]:
    """絶対に殺さない pid。自分・祖先・セッション/グループリーダー・0・1。"""
    me = os.getpid() if me is None else me
    keep = {0, 1, me} | ancestors(me)
    for get in (lambda: os.getsid(0), os.getpgrp):
        try:
            keep.add(get())
        except OSError:
            pass
    return keep


def partition(pids, me: int | None = None, environ=None):
    """(殺してよい pid, 断った `Refusal`, 全部断る理由 | None)。

    `starttime` は boot からの tick なので単調で比較できる。**同じ tick は許す** —
    テストと同じ tick に生まれた子孫は本物なので、`<` (厳密に古い) だけを断る。
    """
    me = os.getpid() if me is None else me
    mine = _ppid_and_start(me)
    my_start = mine[1] if mine is not None else None
    keep = protected(me)

    allowed: list[int] = []
    refused: list[Refusal] = []
    for pid in pids:
        if pid <= 1 or pid in keep:
            refused.append(Refusal(pid, "自分・祖先・セッション/グループリーダー — 殺すと自分が死ぬ"))
            continue
        got = _ppid_and_start(pid)
        if got is not None and my_start is not None and got[1] < my_start:
            refused.append(Refusal(pid, "このテストセッションより古い — テストの子孫ではありえない"))
            continue
        allowed.append(pid)

    cap = max_kills(environ)
    if len(allowed) > cap:
        reason = (f"殺す対象が {len(allowed)} 件で上限 {cap} 件を超えた。"
                  f"「本当に {len(allowed)} 個漏れた」ではなく判定 (_belongs / scan) が"
                  f"壊れている疑いなので、1 件も殺さない。"
                  f"本当に必要なら {BUDGET_VAR} で上限を上げる。")
        refused += [Refusal(pid, "予算超過のため保留") for pid in allowed]
        return [], refused, reason
    return allowed, refused, None
