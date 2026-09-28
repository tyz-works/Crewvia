#!/usr/bin/env python3
"""tests/proc_group.py — タイムアウトで **プロセスグループごと** 殺す subprocess ヘルパー (t029 / backlog #32)。

## なぜ

`subprocess.run(["bash", "plan.sh", ...], timeout=N)` はタイムアウトすると **直接の子 (bash) だけ**
を kill する。plan.sh は bash の下で `python3 - <queue> <cmd> ...` を起こすので、python は
孤児になって生き残る。FIFO のテストでは、その python が「書き手のいない FIFO の open()」で
永久に待つ (`wchan = wait_for_partner`)。init に付け替わって誰も回収せず、全 pytest を
回すたびに数個ずつ溜まった (2026-09-26 に 22 個・287MB を手で kill した)。

    bash plan.sh done ...            ← subprocess.run が kill するのはここだけ
      └ python3 - <queue> done ...   ← 残る。FIFO を open して待ち続ける

直し方は、子を **新しいセッション (= 新しいプロセスグループ) の頭** にして、タイムアウトや後始末で
`os.killpg` を使うこと。グループの中身は bash と、その子孫の python / sleep / …。
本番の plan.sh は別のセッションなので巻き込まない (グループは `start_new_session=True` で
この呼び出しが作ったものに限る)。

`tests/leaked_descendants.py` の構造ガードが「それでも残った子孫」を検出して落とす。
このヘルパーはガードの代わりではなく、ガードが赤くならないための正しい書き方である。
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess


def kill_group(proc: subprocess.Popen) -> None:
    """`start_new_session=True` で起こした `proc` のグループ全員を SIGKILL する。

    グループが既に無ければ何もしない。`proc` 自身が先に終わっていても、孤児になった子孫は
    同じ pgid のまま残るので `os.killpg(proc.pid)` で届く。`ProcessLookupError` (相手が既に
    居ない) 以外の `Exception` (`PermissionError` 等) も握り潰す —— これは `run_in_own_group`
    の `finally` 相当の掃除経路から呼ばれるので、ここで例外を漏らすと呼び出し元の後始末
    そのものを止めてしまう (leaked_descendants.kill_all の `_signal_one` と同じ境界、
    5巡目の主眼)。`KeyboardInterrupt` / `SystemExit` は `Exception` を継承しないので素通りする。
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass


def descendants(pid: int) -> list[int]:
    """`pid` の子孫の pid (`/proc` の ppid を辿る)。`pid` 自身は含まない。

    stat の読み取りはバイト列のまま解析する。`comm` は UTF-8 として不正な任意バイト列を
    取れるので、`read_text()` (str) は無関係な 1 プロセスの名前だけで `UnicodeDecodeError`
    (`OSError` のサブクラスではない) を投げ、走査全体 (と、それを使う `kill_tree` の後片付け)
    を落とす (`leaked_descendants._read_stat` / `kill_budget._ppid_and_start` と同じ族、
    4巡目 codex review P2-1)。`rfind` が見つからない・`ppid` が読めない行は無視する
    (旧実装は例外なしに `int(rest[1])` を呼んでおり、不正な行があれば `IndexError` /
    `ValueError` を無条件に漏らしていた)。
    """
    children: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = pathlib.Path(f"/proc/{name}/stat").read_bytes()
        except OSError:
            continue                 # 走査中に死んだ
        rp = raw.rfind(b")")
        if rp < 0:
            continue
        rest = raw[rp + 2:].split()
        try:
            ppid = int(rest[1])
        except (IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(name))
    out, stack = [], list(children.get(pid, []))
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(children.get(p, []))
    return out


def kill_tree(pid: int) -> None:
    """`pid` とその子孫を SIGKILL する。**親を殺す前に** 子孫を数える (殺すと子は init に付け替わって辿れない)。

    対話シェル (pty) は job control で裏の仕事を **別の** プロセスグループに置くので、`kill_group` では
    届かない。そういう相手の後片付けはこちらを使う。

    1 件ごとに境界を置く —— `os.kill` が `ProcessLookupError` 以外の `Exception`
    (`PermissionError` 等) を出しても、その 1 件で `victims` の残りへの kill を諦めない
    (`leaked_descendants.kill_all` の `_signal_one` / `kill_group` と同じ族、5巡目の主眼)。
    """
    victims = descendants(pid) + [pid]
    for v in victims:
        try:
            os.kill(v, signal.SIGKILL)
        except Exception:
            pass


#: `kill_group` の後の後始末 (communicate/wait) に許す上限秒数 (6巡目 P2-2)。
CLEANUP_GRACE_SECONDS = 5.0


class ProcGroupCleanupError(RuntimeError):
    """`kill_group` の後の後始末 (`communicate()` / `wait()`) が上限時間内に終わらなかった。

    `kill_group` は終了エラー (`PermissionError` 等) を握り潰すので、シグナルが届かなかった
    ことは戻り値からは分からない。届かなかった場合、止まった子孫が pipe (stdout/stderr) を
    開いたまま残るか、`os.killpg` の対象グループを抜けた子孫がまだ生きているかのどちらかで、
    上限なしの `communicate()` / `wait()` は無期限に待つ (6巡目 P2-2)。ここで上限を切って
    例外にすることで、「後始末に失敗した」という事実そのものを呼び出し元 (延いては leak guard)
    に伝播する —— 黙って進まない。
    """


def run_in_own_group(cmd, *, timeout, env=None, cwd=None, text=True, capture_output=True):
    """`subprocess.run(..., timeout=)` と同じ形で、タイムアウト時にグループごと kill する。

    タイムアウトしたら `subprocess.TimeoutExpired` を **そのまま** 上げる (呼び出し側の
    「返らなかったら赤」の判定を変えない)。終わったあとも、正常終了・異常終了を問わずグループを
    掃除する (bash が先に終わって python だけ残る形も止める)。

    `kill_group` の後の後始末 (パイプが閉じるのを待つ `communicate()` / 子の終了を待つ `wait()`)
    には `CLEANUP_GRACE_SECONDS` の上限を置く (6巡目 P2-2)。`kill_group` は
    `PermissionError` を含む終了エラーを黙って握り潰すので、シグナルが実際に届いたかは
    ここでは分からない —— 届かなかった、または process group を抜けた子孫が pipe を保持したまま
    残っていると、上限なしの待ちは無期限に止まり、呼び出し元の `TimeoutExpired` すら上がらない
    (leak guard まで制御が返らない)。上限に達したら `ProcGroupCleanupError` を送出して報告する。
    """
    kwargs = {"env": env, "cwd": cwd, "text": text, "start_new_session": True}
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_group(proc)
        # 読み手がいなくなったパイプは通常ここで返るが、kill_group が終了に失敗した相手や
        # group を抜けた子孫が pipe を握ったままだと返らない —— 上限を切って報告する。
        try:
            exc.stdout, exc.stderr = proc.communicate(timeout=CLEANUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired as cleanup_exc:
            raise ProcGroupCleanupError(
                f"run_in_own_group: {cmd!r} がタイムアウトし kill_group を送ったが、"
                f"{CLEANUP_GRACE_SECONDS}s 待っても後始末 (pipe を握った子孫の可能性) が"
                f"終わらなかった") from cleanup_exc
        raise
    except BaseException:
        kill_group(proc)
        try:
            proc.wait(timeout=CLEANUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired as cleanup_exc:
            raise ProcGroupCleanupError(
                f"run_in_own_group: {cmd!r} の後始末中に例外が起き kill_group を送ったが、"
                f"{CLEANUP_GRACE_SECONDS}s 待っても子の終了を確認できなかった") from cleanup_exc
        raise
    kill_group(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
