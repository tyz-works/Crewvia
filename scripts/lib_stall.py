#!/usr/bin/env python3
"""lib_stall.py — mission の停滞 (進行中も割り当てられる task も 0 件) を Director に知らせる。

事例 (2026-11-13): review が要修正で done → 後続を Director が手で blocked → 実行中も割り当て可能な task も 0 件なのに
mission は done でない → Rule 5 も「全ミッション完了」も出ず 10h40m 無言で静止した。dispatcher は自分のログに `[blocked]` を
書くだけだった。

* `classify()` は**純粋関数** (I/O なし)。「割り当てられる」は `lib_dep_rules.card_dependencies` の定義を呼ぶだけ (不変条件 3)。
* 停滞が `threshold` 秒続いたら 1 回だけ知らせる。続いた時間の台帳は `registry/daemons/stall-state.json` (書き手は dispatcher だけ・
  消してよい = 時計が最初からになる = 通知が**遅れる**側)。重複防止は既存の通知台帳 `notify_state_once` (呼び出し側が渡す関数)。
* `needs_director` / `needs_human_review` だけが残っている mission は対象外 (段階上げ `lib_escalation` が受け持つ。二重に出さない)。
* 通知だけ。待ちを解く・done にする・割り当てることには使わない。env の停止スイッチは付けない
  (しきい値は `daemons.stall_notify_seconds`・0 以下で無効・dispatcher だけが読む)。
"""

import hashlib
import json
import re
import sys
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_daemon_state import is_finite_number, load_json_store, told_lock, write_told_atomic  # noqa: E402
from lib_dep_rules import card_dependencies  # noqa: E402
from lib_task_cards import is_missing, is_unreadable  # noqa: E402
from lib_task_status import (  # noqa: E402
    AWAITING_DECISION_STATUSES, EXECUTING_STATUSES, TASK_STATUSES, TERMINAL_STATUSES,
)

LEDGER_NAME = 'stall-state.json'
DEFAULT_THRESHOLD_SECONDS = 900

#: 「進行中」: Worker が実行している (検証中を含む) か、検証に出ていて verifier が動く status。
PROGRESSING_STATUSES = frozenset(EXECUTING_STATUSES | {'ready_for_verification'})
#: 文面に載せる止まっている task の最大件数 / 理由の最大文字数。
MAX_LISTED = 8
REASON_MAX = 80

Stall = namedtuple('Stall', ['stuck', 'last_done'])      # stuck: [(tid, status, 理由)]・last_done: task id / None

_CTRL_RE = re.compile(r'[\x00-\x1f\x7f\u0085  ]+')


def _one_line(value, limit=REASON_MAX):
    text = _CTRL_RE.sub(' ', str(value if value is not None else '')).strip()
    return text if len(text) <= limit else text[:limit] + '…'


def _why(meta, verdict):
    """止まっている理由 1 行 (表示用。判定には使わない)。"""
    status = meta.get('status')
    if status == 'pending':
        if verdict.held:
            return f"failed の依存 {list(verdict.held)} (release-dep 待ち)"
        if verdict.unmet:
            return f"未充足の依存 {list(verdict.unmet)}"
        return 'skills が無い'
    reason = meta.get('blocked_reason') or meta.get('needs_director_reason')
    return _one_line(reason) or '(理由未記載)'


def classify(tasks, done_ids, task_statuses, director_only_skills=frozenset({'director-only'})):
    """1 つの mission の task (card の dict の一覧) → `Stall` か None (止まっていない・判定できない)。純粋。

    止まっている = 完了していない task が 1 つ以上あり、①進行中 (`PROGRESSING_STATUSES`) の task が 0 件 ②割り当てられる pending
    (依存が満たされ・skills があり・Director 専用でない = 投げられる / Director が直接やる) が 0 件 ③残りが判断待ち
    (`AWAITING_DECISION_STATUSES`) だけではない (それは段階上げの対象)。語彙に無い status (破損等) があれば判定しない
    (観測できなかったことを「止まっている」に倒さない)。"""
    stuck = []
    last_done, last_stamp = None, ''
    for meta in tasks:
        status = meta.get('status')
        if status not in TASK_STATUSES:
            return None
        if status in TERMINAL_STATUSES:
            stamp = meta.get('completed_at')
            stamp = stamp if isinstance(stamp, str) else ''
            if last_done is None or stamp >= last_stamp:
                last_done, last_stamp = str(meta.get('id')), stamp
            continue
        if status in PROGRESSING_STATUSES:
            return None
        if status == 'pending':
            verdict = card_dependencies(meta, done_ids, task_statuses)
            skills = set(meta.get('skills') or [])
            if not verdict.unmet and skills:
                return None             # 割り当てられる (Director 専用も Director が直接やる)
            stuck.append((str(meta.get('id')), status, _why(meta, verdict)))
            continue
        stuck.append((str(meta.get('id')), status, _why(meta, None)))
    if not stuck:
        return None                     # 全部完了 (「全ミッション完了」の側の話)
    if all(status in AWAITING_DECISION_STATUSES for _tid, status, _w in stuck):
        return None                     # 段階上げの対象しか残っていない
    stuck.sort()
    return Stall(stuck, last_done)


def fingerprint(slug, stall):
    """通知内容を決める入力 = mission と止まっている task の集合と status (状態が変われば再通知)。"""
    blob = json.dumps([slug, [[tid, status] for tid, status, _w in stall.stuck]], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


def message(slug, stall, elapsed_seconds):
    """Director への 1 通 (1 行。card 由来の語は `_one_line` で無害化済み)。"""
    shown = stall.stuck[:MAX_LISTED]
    items = ', '.join(f'{tid} ({status}: {why})' for tid, status, why in shown)
    if len(stall.stuck) > len(shown):
        items += f' ほか {len(stall.stuck) - len(shown)} 件'
    last = f'直前に done になった task: {stall.last_done}。' if stall.last_done else ''
    minutes = max(0, int(elapsed_seconds // 60))
    return (f'[stall] mission {slug} が {minutes} 分止まっています (進行中の task も、割り当てられる pending も 0 件)。'
            f'止まっている task: {items}。{last}'
            f'出口: `plan.sh status --mission {slug}` で確認し、`plan.sh update <id> --status pending` で戻す / '
            f'`plan.sh reopen <id> --reason ... --branch reuse` で直す / 不要なら skipped にしてください。')


# ---------------------------------------------------------------------------
# 台帳 (停滞が続いている時間)。`{slug: {'fp': str, 'first_seen': epoch}}`
# ---------------------------------------------------------------------------

def stall_entry_problem(key, entry):
    if not isinstance(key, str) or not key or '/' in key:
        return f'key {key!r} is not a mission slug'
    if not isinstance(entry, dict):
        return f'{key}: entry is not an object'
    if not isinstance(entry.get('fp'), str) or not entry['fp']:
        return f"{key}: 'fp' is {entry.get('fp')!r}"
    if not is_finite_number(entry.get('first_seen')):
        return f"{key}: 'first_seen' is {entry.get('first_seen')!r}"
    return None


def stall_state_problem(data):
    for key, entry in data.items():
        problem = stall_entry_problem(key, entry)
        if problem:
            return problem
    return None


def read_ledger(registry_dir, warn=None):
    return load_json_store(Path(registry_dir) / 'daemons' / LEDGER_NAME, check=stall_state_problem, warn=warn)


def write_ledger(registry_dir, ledger):
    path = Path(registry_dir) / 'daemons' / LEDGER_NAME
    with told_lock(str(path)) as held:
        if not held or stall_state_problem(ledger):
            return False
        return write_told_atomic(path, ledger)


def told_key(slug):
    return f'stall-{slug}'


# ---------------------------------------------------------------------------
# 1 サイクル
# ---------------------------------------------------------------------------

def run_cycle(registry_dir, all_tasks, done_ids_by_mission, statuses_by_mission, active_missions, observed_slugs,
              now, threshold, *, notify, live_keys, log=lambda msg: None):
    """停滞検知の 1 サイクル。例外は呼び出し側が受ける。→ 通知した mission の slug の一覧。

    `notify(key, fp, slug, text) -> bool`: 既存の通知台帳 (`notify_state_once`) 越しの送信 (呼び出し側が渡す)。
    `live_keys`: 通知台帳の掃除 (`prune_told`) に渡す「今なお成り立っている key」の集合。停滞が続く間はここに足す。
    `threshold` ≤ 0 は無効 (何も読まず・書かず・送らない)。"""
    if not is_finite_number(threshold) or threshold <= 0:
        return []
    ledger = read_ledger(registry_dir)
    if is_unreadable(ledger):
        log(f'WARNING: stall-state: 台帳が壊れている ({ledger.reason}) — 作り直します '
            f'(停滞の時計が最初からになる = 通知が遅れる側。registry/daemons/{LEDGER_NAME})')
        ledger = {}
    elif is_missing(ledger):
        ledger = {}
    by_mission = {}
    for slug, meta in all_tasks:
        by_mission.setdefault(slug, []).append(meta)
    new_ledger = {}
    sent = []
    for slug in active_missions:
        if slug not in observed_slugs:
            if slug in ledger:
                new_ledger[slug] = ledger[slug]      # 観測できなかった = 「止まっていない」の証拠ではない。時計は触らない
            continue
        stall = classify(by_mission.get(slug, []), done_ids_by_mission.get(slug, set()), statuses_by_mission.get(slug, {}))
        if stall is None:
            continue                                  # 止まっていない → 時計を捨てる (通知台帳のキーは prune_told が畳む)
        fp = fingerprint(slug, stall)
        entry = ledger.get(slug)
        if entry is None or entry['fp'] != fp or entry['first_seen'] > now:
            entry = {'fp': fp, 'first_seen': now}
        new_ledger[slug] = entry
        live_keys.add(told_key(slug))
        elapsed = now - entry['first_seen']
        if elapsed >= threshold and notify(told_key(slug), fp, slug, message(slug, stall, elapsed)):
            sent.append(slug)
    if new_ledger != ledger and not write_ledger(registry_dir, new_ledger):
        log('WARNING: stall-state に書けない — 次のサイクルでやり直します')
    return sent
