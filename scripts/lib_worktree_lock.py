#!/usr/bin/env python3
"""lib_worktree_lock.py — python から repo 単位の worktree ロックを通して git を実行する入口 (唯一の python 側の入口)。

ロックの**定義は scripts/lib_worktree_lock.sh の `crewvia_wt_git` 1 か所だけ** (ロックファイル名・旧名 → 新名の固定順・
flock の取り方)。ここは flock を自前で書かず、bash を起こして `crewvia_wt_git` を呼ぶだけにして、定義を 2 つにしない
(構造ガード test_flock_is_defined_only_in_the_shared_lib と、python 側に flock が出ないことを見る
test_worktree_lock_shared.py の対)。

    wt_git(repo, *args, timeout=..., lock_wait=...) -> (returncode, stdout_bytes, stderr_bytes)

- ロックが取れなかった (lock_wait 秒待っても) ときは git を実行せず returncode == LOCK_UNAVAILABLE (99)。
- 起動できない・lib が無い・時間切れは returncode = None (判定不能)。いずれも例外は投げない。
  呼び側 (デーモンの周期処理) は「失敗 = 次のサイクルに回す」で止めずに進む (fail-closed)。
- 時間切れのときは bash と配下の git をプロセスグループごと落とす (ロックを握ったまま残さない)。
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Optional, Tuple

LIB_SH = Path(__file__).resolve().parent / "lib_worktree_lock.sh"
LOCK_UNAVAILABLE = 99

_BASH = 'source "$1" || exit 98; shift; crewvia_wt_git "$@"'


def wt_git(repo, *args: str, timeout: float = 120.0, lock_wait: Optional[float] = None,
           env: Optional[dict] = None) -> Tuple[Optional[int], bytes, bytes]:
    """`git -C <repo> <args>` をロックの中で実行する。`(returncode, stdout, stderr)`。"""
    if not LIB_SH.is_file():
        return None, b"", b"lib_worktree_lock.sh not found"
    e = dict(os.environ if env is None else env)
    if lock_wait is not None:
        e["CREWVIA_WT_LOCK_WAIT_SECONDS"] = str(max(1, int(lock_wait)))
    try:
        proc = subprocess.Popen(["bash", "-c", _BASH, "_", str(LIB_SH), str(repo), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e,
                                start_new_session=True)
    except OSError as exc:
        return None, b"", f"{type(exc).__name__}: {exc}".encode()
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return None, b"", b"timeout"
    return proc.returncode, out, err
