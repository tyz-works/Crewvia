#!/usr/bin/env python3
"""lib_daemon_state.py — デーモン側 JSON 状態ストアを読むことの唯一の入口 (t026)。

## なぜこれが要るのか

PR #214 で、**同じ根の欠陥が 3 回** 出た:

1. t019 1 巡目: `lib_review_refusal.load()` が欄の「存在」しか見ず、`diff_bytes: null` で
   `describe()` が `TypeError` → dispatch サイクル全体が落ちる
2. t019 3 巡目: `load_told()` が台帳の **外側** (JSON object か) しか見ず、
   `{"bad": {"slug": []}}` のような内側のエントリで `prune_told()` が `TypeError`
   → 毎サイクル繰り返し、prune が一切行われず、サイクルの残りも飛ぶ。
   **状態を離れて戻った task が永久に沈黙する** (`already_told` が「伝えた」を返し続ける)
3. t015 (PR #217): `released_deps` が未検証で `true` / `123` なら `TypeError`

どれも「読めた JSON を、**中身の形を確かめずに** 使う」型である。1 件ずつ site patch を
当てても 4 回目は「まだ書かれていないストア」に出る。そこで、読み取りの **入口を 1 つに**
する (`lib_task_cards` が queue のカードについて既にそうしているのと同じ作法):

    load_json_store(path, check=<形の検証>, warn=<警告>)
        → 検証済みの値、または `Unreadable`

* 読み取りの失敗も、JSON として壊れていることも、**形が使えないことも**、`None` / `{}` /
  `[]` で返さない。`Unreadable` を返す (`bool()` / `len()` / `in` / `[]` / 反復 / `.get()` が
  すべて `TypeError`。空の入れ物として振る舞わない)。
* **ENOENT だけが「まだ無い」** (`is_missing()`)。呼び出し側はそれを「空」として扱ってよい。
* 検証は **エントリごと**。壊れたエントリが 1 つでもあれば、ストア全体を `Unreadable` として
  返す (一部だけ生かすと、どのエントリが壊れているかを呼び出し側が知っていることになる)。
* `check` が例外を出しても `Unreadable` に倒す。検証器のバグでサイクルを落とさない。

## 倒す向きはここでは決めない

`Unreadable` をどう読むかは判定ごとに違う (memory: fail-direction-is-per-judgment)。

* notified-state 台帳 (`dispatcher.load_told`): **再送側**。台帳が使えないときは「伝えていない」
  として扱い、WARNING を 1 行出す。通知が欠ける方が高くつく (t027 の 10 時間全停止)。
  **永久沈黙は作らない**。
* review-refusals (`lib_review_refusal.load`): **spawn を保留** (拒否されていないと証明できない)。

## 構造テスト

`tests/test_daemon_state_reads_go_through_the_entry.py` が、対象モジュールの
`json.load` / `json.loads` を AST で全部拾い、この入口の中にあるもの以外は理由付き allowlist に
無ければ落とす。`dispatcher.sh` の埋め込み python も対象。表で示すだけにしない。
"""

import contextlib
import fcntl
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_task_cards import (  # noqa: E402
    Unreadable, is_missing, is_unreadable, read_regular_text_or_unreadable,
)


def _safe_warn(warn, msg):
    """警告の出力先が壊れていても、読み取りの結果を変えない。"""
    if warn is None:
        return
    try:
        warn(msg)
    except Exception:  # noqa: BLE001 — 警告は補助。落としてよいのは警告だけ。
        pass


def load_json_store(path, check=None, warn=None, expect=dict):
    """JSON ファイルを読み、**形まで検証して** 返す。

    戻り値:
      * 検証を通った値 (`expect` の型。既定は `dict`)
      * `Unreadable` かつ `is_missing()`: ファイルが無い (= まだ書かれていない)
      * それ以外の `Unreadable`: 読めない / JSON として壊れている / 形が使えない

    `check(data)` は使えない理由の文字列を返す (使えれば `None`)。例外を出しても
    `Unreadable` に倒す。**この関数は例外を出さない** (`BaseException` を除く)。
    """
    text = read_regular_text_or_unreadable(path, warn=warn)
    if is_unreadable(text):
        return text
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001 — ValueError 以外 (RecursionError 等) も同じ扱い
        return _rejected(path, f'malformed JSON ({type(e).__name__}: {e})', warn)
    if not isinstance(data, expect):
        kind = 'a JSON object' if expect is dict else expect.__name__
        return _rejected(path, f'expected {kind}, got {type(data).__name__}', warn)
    if check is not None:
        try:
            problem = check(data)
        except Exception as e:  # noqa: BLE001 — 検証器のバグでサイクルを落とさない
            problem = f'validator failed: {type(e).__name__}: {e}'
        if problem:
            return _rejected(path, problem, warn)
    return data


def _rejected(path, reason, warn):
    _safe_warn(warn, f'unusable JSON store {path}: {reason}')
    return Unreadable(path, reason)


# ---------------------------------------------------------------------------
# 形の検証 (ストアごと)
# ---------------------------------------------------------------------------

#: 「伝えた」台帳のエントリが持つ欄。全部、空でない文字列。
TOLD_ENTRY_FIELDS = ('fp', 'kind', 'slug', 'task')


def told_entry_problem(key, entry):
    """台帳の 1 エントリが使えない理由 (使えれば `None`)。

    書き手 (`dispatcher.record_told`) が書くものと **同じ形** を要求する。読み手だけが
    厳しいと、書いたばかりの台帳を自分が読めなくなる (= 永久に再送側へ倒れる)。
    書き手もこの関数を通してから書くこと。
    """
    if not isinstance(key, str) or not key:
        return f'ledger key {key!r} is not a non-empty string'
    if not isinstance(entry, dict):
        return f'entry {key!r} is {type(entry).__name__}, expected an object'
    for field in TOLD_ENTRY_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value:
            return f'entry {key!r}: field {field!r} is {value!r}, expected a non-empty string'
    return None


def told_ledger_problem(data):
    """台帳全体が使えない理由。1 エントリでも壊れていれば、台帳全体が使えない。"""
    for key, entry in data.items():
        problem = told_entry_problem(key, entry)
        if problem:
            return problem
    return None


# ---------------------------------------------------------------------------
# 「伝えた」台帳の書き込み (t021)
# ---------------------------------------------------------------------------
#
# 台帳 (`registry/daemons/notified-state.json`) の書き手は **2 人** になった:
# dispatcher (needs_director / handoff / review 拒否) と watchdog (timeout 終了の
# 通知)。read-modify-write は全体で 1 つのクリティカルセクションでなければならず、
# 原子的な置換 (temp + os.replace) だけでは「A が読む → B が書く → A が書く」で B の
# エントリが消える。消えたのが dispatcher のエントリなら、対処済みの状態の通知が
# 再送される (2026-09-25 に数十通届いた洪水の型)。
#
# そこで台帳の書き換えは **すべて `told_lock()` の中** で行う。読むだけの側は
# ロックを取らない (置換は原子的なので、途中の姿は見えない)。

#: ロックを待つ上限。dispatcher は 5 秒周期、watchdog は 30 秒周期で、どちらも
#: 「取れなければ次のサイクルで」に倒せるので、長く待つ理由が無い。
TOLD_LOCK_WAIT_SECONDS = 2.0

#: watchdog が書く timeout 通知のエントリの kind。**この kind のエントリは、書かれて
#: から `TOLD_TIMEOUT_TTL_SECONDS` のあいだは dispatcher の prune の対象外**である
#: (状態ベースの通知と違い、「状態を離れた」ことを dispatcher は観測できない — task は
#: 後始末で pending に戻っているので live key に現れない)。TTL を過ぎたものは
#: dispatcher の prune が掃除する。
TOLD_TIMEOUT_KIND = 'timeout'
TOLD_TIMEOUT_TTL_SECONDS = 24 * 3600


@contextlib.contextmanager
def told_lock(path, wait=TOLD_LOCK_WAIT_SECONDS):
    """台帳の read-modify-write を直列化する排他ロック。取れたかどうかを yield する。

    `with told_lock(TOLD_FILE) as held:` — **`held` が False のとき本体は走るが、台帳に
    書いてはいけない** (書けなかった扱いにして呼び出し側の「取れなければ次のサイクル」
    に倒す)。ロックのファイルは `<path>.lock`。中身は読まない (fd は `flock` にしか
    渡さない) ので書き込み専用で開く。`O_NONBLOCK` は、そこに FIFO を置かれても
    open が返るようにするため。開けなければ (置き場が使えない) 取れなかった扱い。
    """
    lock_path = f'{path}.lock'
    fd = None
    held = False
    try:
        try:
            os.makedirs(os.path.dirname(lock_path) or '.', exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_NONBLOCK, 0o644)
        except OSError:
            fd = None
        if fd is not None:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
        yield held
    finally:
        if fd is not None:
            if held:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)


def told_matches(told, key, fp):
    """`told` (= `load_json_store(..., check=told_ledger_problem)` の結果) が、この key を
    この fingerprint で「伝えた」としているか。使えない台帳は「伝えていない」(再送側)。"""
    if is_unreadable(told):
        return False
    entry = told.get(key)
    return isinstance(entry, dict) and entry.get('fp') == fp


def told_is_fresh_timeout(entry, now=None):
    """dispatcher の prune が触ってはいけない timeout 通知のエントリか (TTL 内)。"""
    if not isinstance(entry, dict) or entry.get('kind') != TOLD_TIMEOUT_KIND:
        return False
    at = entry.get('at')
    if not is_finite_number(at):
        return False
    now = time.time() if now is None else now
    return 0 <= now - at < TOLD_TIMEOUT_TTL_SECONDS


def write_told_atomic(path, told):
    """台帳を temp + os.replace で書く。書けなければ False (例外は出さない)。

    **`told_lock()` の中で呼ぶこと。**
    """
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(told, ensure_ascii=False, sort_keys=True))
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def told_record(path, key, entry, warn=None):
    """台帳に 1 エントリ足す (ロックの下で read-modify-write)。書けたら True。

    エントリは `told_entry_problem()` を通してから書く (読み手と同じ形)。台帳が壊れて
    いれば作り直す (自己修復 — dispatcher の `record_told()` と同じ向き)。ロックが
    取れなければ False。
    """
    problem = told_entry_problem(key, entry)
    if problem:
        _safe_warn(warn, f'notified-state: 台帳に書けない形のエントリ ({problem})')
        return False
    with told_lock(path) as held:
        if not held:
            _safe_warn(warn, f'notified-state: {path} のロックを取れなかった')
            return False
        told = load_json_store(path, check=told_ledger_problem, warn=warn)
        if is_missing(told) or is_unreadable(told):
            told = {}
        told[key] = entry
        return write_told_atomic(path, told)


# ---------------------------------------------------------------------------
# 数値の欄
# ---------------------------------------------------------------------------

def is_finite_number(v):
    """時刻・秒数として使える値か: 有限の数 (bool・NaN・inf は数ではない)。

    NaN は `now - nan > TTL` が常に偽になり、スロットルが **永久に** 通知を遮る。
    JSON は `NaN` / `Infinity` を (Python の `json` では) 受理するので、ここで落とす。
    """
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


#: 「未来の時刻」をどこまで許すか。これより先は時計の巻き戻りでは説明できず、
#: スロットルとして読むと **その時刻まで通知を遮る** (= 沈黙) ので、使えない値として扱う。
FUTURE_SLACK_SECONDS = 24 * 3600


def notify_cache_problem(data, now=None):
    """`/tmp` の通知スロットル `{key: 最後に送った epoch 秒}` が使えない理由。

    `should_notify()` は `time.time() - cache[key] > TTL` を計算し、`record_notify()` は
    `cache[key] = ...` を書く。値が数でなければ TypeError でサイクルが落ち、NaN / 遠い未来
    なら通知が永久に遮られる。
    """
    now = time.time() if now is None else now
    for key, value in data.items():
        if not is_finite_number(value):
            return f'throttle {key!r} is {value!r}, expected a finite timestamp'
        if value < 0 or value > now + FUTURE_SLACK_SECONDS:
            return f'throttle {key!r} is {value!r}, not a plausible timestamp'
    return None


def rule5_state_problem(data):
    """Rule 5 の grace 追跡 (`registry/mux/<name>.state.json`) が使えない理由。

    書き手 (`_save_state_entry`) は `{'state': str, 'since': epoch}` を書く。
    読み手は `entry.get('state')` と `now - entry.get('since')` を使う。
    """
    if not isinstance(data.get('state'), str):
        return f"'state' is {data.get('state')!r}, expected a string"
    if not is_finite_number(data.get('since')):
        return f"'since' is {data.get('since')!r}, expected a finite timestamp"
    return None


def watch_state_problem(data):
    """相互監視の `<peer>.watch.json` が使えない理由。

    `grace_until` / `last_respawn_at` / `hold_since` は、あれば有限の数か null。
    (`float("x")` や `float({})` で watch のサイクルが落ちるのを、ここで止める。)
    """
    for field in ('grace_until', 'last_respawn_at', 'hold_since'):
        value = data.get(field)
        if value is not None and not is_finite_number(value):
            return f'{field!r} is {value!r}, expected a finite number or null'
    return None
