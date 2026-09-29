#!/usr/bin/env python3
"""lib_usage_limit.py — Worker の画面から「利用枠切れ」を同定する唯一の定義 (C2 / t005)

Claude の利用枠 (5 時間枠・週の枠) が切れると、Worker の画面に

    ⚠ Usage limit reached · continuing automatically at 6pm

のような通知行が出て止まる。これは「普通の idle」ではない。2026-09-27 夜には区別しなかった
ために、Rule 5 が 5 分おきに 5 人分発火して 2 時間で約 80 通が Director に届き、watchdog は
idle×2 で Worker を終了させ、止まっていた時間も max に数えて Haruto を kill した。

dispatcher (Rule 5) と watchdog (idle / max) の**両方がこのファイルの `detect()` を使う**。
別の判定を片方に置かない (答えが割れる)。

## 同定は「位置と構造」に束縛する (文言の部分一致にしない)

mission B の B6 で「画面のどこかに文言があれば trust ダイアログ」とした結果、パスや出力に同じ
文言があるだけで正常な Worker を kill しかけた (`lib_trust.py` の族 B)。ここでも同じ轍を踏まない。
次の**全部**が揃ったときだけ利用枠切れとする:

  1. 通知行が**行頭 (0 桁目) の `⚠`** で始まる。ツール出力は `⎿` の下にインデントされて出る。
     ツール呼び出しは `⏺`、入力の写しは `❯` で始まる。パス (`Working directory: /x/⚠ ...`)・
     引用・ファイルの中身 (`cat` の出力) は、どれも行頭が `⚠` にならない
  2. 通知行が画面の**末尾側** (空行を除いた最後の `TAIL_LINES` 行) にある
  3. 通知行の**直下 (空行を除いて `FRAME_WITHIN` 行以内) に入力欄の枠**
     (`╭──…` / `╰──…` / `────…` の罫線) がある。スクロールして上に流れた古い通知は、下に枠が
     続かないので除外される。「入力欄の上の通知行」という位置そのものを条件にしている

**判定の向き (fail-direction)**: 画面が読めない・空・構造が合わない → 常に `None` (= 利用枠切れ
ではない = 今までと同じ挙動)。誤検知が起きても、利用枠切れとして扱う効果 (通知の抑制・kill の
見送り) には**必ず上限**がある (`excuse_deadline()`)。永久に黙る・永久に殺さない経路を作らない。

## リセット時刻

通知行から読む。読めなければ `reset_at=None` (= 時刻不明)。

  * `continuing automatically at 6pm` / `at 11:16 PM` / `at 18:00` → 次に来るその時刻 (ローカル時刻)
  * `+1h57m` / `+45m` / `+2h` (残り時間の形) → now + その長さ

「次に来る時刻」は**観測した瞬間の now から**計算する。リセット時刻を過ぎても画面の
`at 6pm` は残るので、毎回計算し直すと 18:05 に「明日の 18:00」に化ける。だから最初に見た
時点の値を `observe()` が記録として持ち、**同じ通知行の間は保つ**。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

#: 通知行を探す範囲: 空行を除いた画面の最後の N 行。
TAIL_LINES = 14

#: 通知行から入力欄の枠までの最大距離 (空行を除いた行数)。
FRAME_WITHIN = 3

#: リセット予定を過ぎてもなお利用枠切れのままの Worker に、通常の判定へ戻すまで与える猶予。
GRACE_AFTER_RESET_SECONDS = 3600

#: リセット時刻が読めないときに、見え始めから数える上限 (5 時間枠 + 猶予 1 時間)。
UNKNOWN_RESET_MAX_SECONDS = 6 * 3600

# 行頭の ⚠ (先頭に空白も付けさせない — ツール出力は必ずインデントされる)
_NOTICE_RE = re.compile(r'^⚠️?\s*usage limit reached\b(?P<rest>.*)$', re.IGNORECASE)

# 入力欄の枠: 罫線 8 文字以上 (角付きも可)
_FRAME_RE = re.compile(r'^\s*[╭╰┌└├]?[─━═]{8,}')

_AT_RE = re.compile(
    r'\bat\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?\b', re.IGNORECASE)
_REL_RE = re.compile(r'\+\s*(?:(?P<h>\d+)\s*h)?\s*(?:(?P<m>\d+)\s*m)?', re.IGNORECASE)
# 「残り時間」の部分 (`+1h57m`) は毎分変わる。同じ利用枠切れかどうかの照合からは外す。
_REL_STRIP_RE = re.compile(r'\+\s*\d+\s*[hm](?:\s*\d+\s*m)?', re.IGNORECASE)


@dataclass(frozen=True)
class UsageLimit:
    #: 通知行そのもの (前後の空白を除いた文字列)。同じ通知かどうかの照合に使う。
    notice: str
    #: リセット予定 (epoch 秒)。読めなければ None (時刻不明)。
    reset_at: Optional[float]


def _parse_reset(rest: str, now: float) -> Optional[float]:
    m = _REL_RE.search(rest)
    if m and (m.group('h') or m.group('m')):
        secs = int(m.group('h') or 0) * 3600 + int(m.group('m') or 0) * 60
        if secs > 0:
            return now + secs
    m = _AT_RE.search(rest)
    if not m:
        return None
    hour = int(m.group('h'))
    minute = int(m.group('m') or 0)
    ap = (m.group('ap') or '').lower()
    if ap:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ap == 'pm' else 0)
    elif not 0 <= hour <= 23:
        return None
    if not 0 <= minute <= 59:
        return None
    lt = time.localtime(now)
    try:
        cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
    except (OverflowError, ValueError):
        return None
    if cand <= now:
        cand += 24 * 3600
    return cand


def identity(notice: str) -> str:
    """同じ利用枠切れの通知かどうかを決める文字列 (残り時間の表示は含めない)。
    ledger の fingerprint にも使う — 記録ファイルを失っても、同じ通知は同じ fp に落ちる。"""
    return ' '.join(_REL_STRIP_RE.sub('', str(notice)).lower().split())


def detect(screen, now: Optional[float] = None) -> Optional[UsageLimit]:
    """画面 (`capture()` の文字列) が利用枠切れの表示なら `UsageLimit`、そうでなければ None。

    読めない (`None` / 文字列でない / 空) は None。例外は投げない。
    """
    if not isinstance(screen, str) or not screen.strip():
        return None
    if now is None:
        now = time.time()
    lines = [ln.rstrip() for ln in screen.splitlines() if ln.strip()]
    tail = lines[-TAIL_LINES:]
    found: Optional[UsageLimit] = None
    for i, raw in enumerate(tail):
        m = _NOTICE_RE.match(raw)
        if not m:
            continue
        below = tail[i + 1:i + 1 + FRAME_WITHIN]
        if not any(_FRAME_RE.match(ln) for ln in below):
            continue
        found = UsageLimit(notice=raw.strip(), reset_at=_parse_reset(m.group('rest'), now))
    return found


def observe(previous: Optional[dict], screen, now: Optional[float] = None) -> Optional[dict]:
    """1 回の観測を、Worker ごとの記録 (`entry`) に畳む。利用枠切れでなければ None。

    entry = {'first_seen': epoch, 'notice': str, 'reset_at': epoch|None}

    `previous` と同じ通知行が続いている間は `first_seen` / `reset_at` を保つ (モジュール
    docstring 参照: 過ぎたリセット時刻が翌日に化けない)。通知行が変わったら新しい利用枠切れ
    として作り直す。
    """
    if now is None:
        now = time.time()
    found = detect(screen, now)
    if found is None:
        return None
    if (isinstance(previous, dict) and identity(previous.get('notice')) == identity(found.notice)
            and isinstance(previous.get('first_seen'), (int, float))):
        reset_at = previous.get('reset_at')
        return {
            'first_seen': float(previous['first_seen']),
            'notice': found.notice,
            'reset_at': float(reset_at) if isinstance(reset_at, (int, float)) else None,
        }
    return {'first_seen': float(now), 'notice': found.notice, 'reset_at': found.reset_at}


def excuse_deadline(entry: dict) -> float:
    """この利用枠切れを「普通の idle / max ではない」として扱ってよい最終時刻。

    リセット予定 + 猶予。時刻不明なら見え始め + `UNKNOWN_RESET_MAX_SECONDS`。これを過ぎても
    まだ利用枠切れの表示が続くなら、通常の判定に戻す (誤検知・リセット後も動かない Worker が
    永久に黙る/殺されない経路を作らない)。
    """
    reset_at = entry.get('reset_at')
    if isinstance(reset_at, (int, float)):
        return float(reset_at) + GRACE_AFTER_RESET_SECONDS
    return float(entry['first_seen']) + UNKNOWN_RESET_MAX_SECONDS


def format_reset(entry: dict) -> str:
    reset_at = entry.get('reset_at')
    if not isinstance(reset_at, (int, float)):
        return '時刻不明'
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(reset_at))
