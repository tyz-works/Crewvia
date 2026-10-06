#!/usr/bin/env python3
"""lib_escalation.py — Director の判断待ちの段階上げ (PR-B)。

設計: knowledge/director-escalation-telegram.md §5 / §6。

* `decide()` / `apply_failure()` は**純粋関数** (I/O なし・時計は引数)。規則表 R1〜R8 は設計 §5-4 のまま。
* 送信 (Director への mux send・Telegram) は呼び出し側が関数で渡す。このモジュールは通信しない。
* 台帳 `registry/daemons/escalation-state.json` の書き手は dispatcher だけ。消してよい
  (時計が最初からになる = 通知が**遅れる**側に倒れる)。
* 台帳が `Unreadable` のときは段階上げ全体を**見送る** (鳴らさない側。再送側に倒すと壊れている間
  毎サイクル Telegram に送り続ける)。
"""

import math
import os
import sys
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_daemon_state import (  # noqa: E402
    is_finite_number, load_json_store, told_lock, write_told_atomic,
)
from lib_dep_rules import card_dependencies  # noqa: E402
from lib_task_cards import is_missing, is_unreadable, read_regular_text_or_unreadable  # noqa: E402
from lib_task_status import AWAITING_DECISION_STATUSES  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

LEDGER_NAME = 'escalation-state.json'
DEFAULT_DIRECTOR_AFTER = 300.0
DEFAULT_TELEGRAM_AFTER = 600.0
ENV_DIRECTOR_AFTER = 'CREWVIA_ESCALATION_DIRECTOR_AFTER_SECONDS'
ENV_TELEGRAM_AFTER = 'CREWVIA_ESCALATION_TELEGRAM_AFTER_SECONDS'

#: 1 サイクルに送る Telegram の段階 2 の上限 (残りは次のサイクル。設計 §7)
MAX_TELEGRAM_PER_CYCLE = 3
#: 「送る」を台帳に書いた (sending) まま結果が書けなかったものを、失敗扱いにして 1 度だけ送り直せるようにするまでの秒数。
#: 送る前の書き込みが成功した後にしか再送しないので、保存が壊れている間に連投にはならない (設計 §11)。
SENDING_TIMEOUT_SECONDS = 600.0
REASON_MAX = 200
TELEGRAM_TEXT_MAX = 2000

ACTION_NONE = 'none'
ACTION_DIRECTOR = 'director_renotice'
ACTION_TELEGRAM = 'telegram_notice'

CardView = namedtuple('CardView', ['slug', 'tid', 'status', 'execution_id', 'observable'])
Decision = namedtuple('Decision', ['action', 'ledger_update'])

KEEP = ('keep',)
DELETE = ('delete',)


def _set(entry):
    return ('set', entry)


def new_entry(execution_id, now):
    return {'execution_id': execution_id, 'first_seen': now, 'stage_sent': 0,
            'stage_sent_at': None, 'stage1_failed_at': None, 'sending_stage': None, 'sending_at': None}


def clear_sending(entry):
    return dict(entry, sending_stage=None, sending_at=None)


def _expire_sending(entry, now):
    """期限切れの sending は「失敗」として読む (段階 1 なら stage1_failed_at を残す)。純粋。"""
    stage = entry.get('sending_stage')
    if stage is None:
        return entry
    at = entry.get('sending_at')
    if is_finite_number(at) and 0 <= now - at < SENDING_TIMEOUT_SECONDS:
        return entry
    out = clear_sending(entry)
    if stage == 1 and out.get('stage1_failed_at') is None:
        out['stage1_failed_at'] = now
    return out


# ---------------------------------------------------------------------------
# 純粋関数
# ---------------------------------------------------------------------------

def decide(card, entry, now, cfg, director_live, telegram_available):
    """規則表 (設計 §5-4)。上から最初に当たった 1 つ。1 回に高々 1 つの action。"""
    d, t = cfg
    if card.status not in AWAITING_DECISION_STATUSES:                       # R1
        return Decision(ACTION_NONE, DELETE if entry is not None else KEEP)
    if not card.observable:                                                 # R2
        return Decision(ACTION_NONE, KEEP)
    if entry is None:                                                       # R3
        return Decision(ACTION_NONE, _set(new_entry(card.execution_id, now)))
    if entry['execution_id'] != card.execution_id:                          # R4
        return Decision(ACTION_NONE, _set(new_entry(card.execution_id, now)))
    if entry['first_seen'] > now:                                           # R4b
        return Decision(ACTION_NONE, _set(dict(entry, first_seen=now)))
    if entry['stage_sent'] == 2:                                            # R5
        return Decision(ACTION_NONE, KEEP)
    entry = _expire_sending(entry, now)
    if entry.get('sending_stage') is not None:                              # R5b 送る途中 (結果が未記録)
        return Decision(ACTION_NONE, KEEP)
    elapsed = now - entry['first_seen']
    reachable = (entry['stage_sent'] >= 1 or d <= 0 or not director_live
                 or entry['stage1_failed_at'] is not None)
    if reachable and t > 0 and elapsed >= t and telegram_available:         # R6
        return Decision(ACTION_TELEGRAM, _set(clear_sending(dict(entry, stage_sent=2, stage_sent_at=now))))
    if d > 0 and entry['stage_sent'] == 0 and elapsed >= d and director_live:   # R7
        return Decision(ACTION_DIRECTOR, _set(clear_sending(dict(entry, stage_sent=1, stage_sent_at=now))))
    return Decision(ACTION_NONE, KEEP)                                      # R8


def apply_failure(entry, action, now):
    """送信が失敗したときの台帳の更新 (sending の印は外す)。段階 1 の失敗だけが stage1_failed_at を残す
    (段階 2 はバックオフが間隔を持つ)。"""
    if entry is None or action not in (ACTION_DIRECTOR, ACTION_TELEGRAM):
        return entry
    out = clear_sending(entry)
    if action == ACTION_DIRECTOR and out.get('stage1_failed_at') is None:
        out['stage1_failed_at'] = now
    return out


def blocked_dependents(slug, waiting_id, tasks_in_mission, done_ids, task_statuses):
    """文面用: この task を待っている pending の task id。数えられなければ None (行を省くだけ)。

    依存の定義は lib_dep_rules (不変条件 3) — ここでは `unmet` に含まれるかを見るだけ。
    """
    try:
        out = []
        for meta in tasks_in_mission:
            if meta.get('status') != 'pending':
                continue
            if waiting_id in card_dependencies(meta, done_ids, task_statuses).unmet:
                out.append(str(meta.get('id')))
        return sorted(out)
    except Exception:  # noqa: BLE001 — 数えられないだけ。段階上げは止めない
        return None


def _one_line(text, limit):
    first = (text or '').strip().splitlines()[0] if (text or '').strip() else ''
    return first if len(first) <= limit else first[:limit] + '…'


def notice_lines(head, slug, tid, status, elapsed_seconds, dependents, reason, session_link=''):
    """設計 §6 の文面。Director 宛と Telegram 宛で項目・順序は同じ (先頭のタグだけが違う)。"""
    lines = [head, f'mission: {slug}',
             f'task: {tid} ({status}) — 止まって {max(0, int(elapsed_seconds // 60))} 分']
    if dependents:
        lines.append(f'後続 {len(dependents)} 件が待機中: ' + ', '.join(dependents))
    lines.append('理由: ' + (_one_line(reason, REASON_MAX) or '(理由未記載)'))
    lines.append('👉 Director の画面で `plan.sh status` を見て対処してください')
    if session_link:
        lines.append(f'セッション: {session_link}')
    return lines


def telegram_text(lines):
    text = '\n'.join(lines)
    return text if len(text) <= TELEGRAM_TEXT_MAX else text[:TELEGRAM_TEXT_MAX - 1] + '…'


def director_text(lines):
    return ' / '.join(lines)


def _could_act(card, entry, now, cfg):
    """この card が段階 1 / 2 の送信に進みうるか (安い判定。外への問い合わせの前に置く)。"""
    if entry is None or not card.observable or entry['execution_id'] != card.execution_id:
        return False
    if entry['stage_sent'] == 2 or entry['first_seen'] > now:
        return False
    positive = [x for x in cfg if x > 0]
    return bool(positive) and now - entry['first_seen'] >= min(positive)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

def _parse_seconds(raw, default, name, warn):
    if raw is None:
        return default
    if isinstance(raw, bool):
        value = None
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
    if value is None or not math.isfinite(value):
        warn(f'WARNING: invalid escalation {name}={raw!r} (有限の秒数が必要) — 既定値 {default} を使います')
        return default
    return value


def parse_cfg(director_raw, telegram_raw, warn=lambda msg: None):
    """→ `(director_after, telegram_after)`。≤0 = その段階を使わない。使う段階どうしの順序だけ検証 (§5-3)。"""
    d = _parse_seconds(director_raw, DEFAULT_DIRECTOR_AFTER, 'director_after_seconds', warn)
    t = _parse_seconds(telegram_raw, DEFAULT_TELEGRAM_AFTER, 'telegram_after_seconds', warn)
    if d > 0 and t > 0 and t <= d:
        warn(f'WARNING: escalation telegram_after_seconds={t} は director_after_seconds={d} より大きくなければ'
             f'なりません — 既定値 {DEFAULT_DIRECTOR_AFTER:g} / {DEFAULT_TELEGRAM_AFTER:g} を使います')
        return DEFAULT_DIRECTOR_AFTER, DEFAULT_TELEGRAM_AFTER
    return d, t


def load_cfg(config_path, environ=None, warn=lambda msg: None):
    """`config/crewvia.yaml` の `escalation:` + env (env 優先)。読めない・欠けは既定値。"""
    environ = os.environ if environ is None else environ
    block = {}
    if yaml is not None:
        raw = read_regular_text_or_unreadable(config_path)
        if not is_unreadable(raw):
            try:
                loaded = yaml.safe_load(raw) or {}
                got = loaded.get('escalation') if isinstance(loaded, dict) else None
                block = got if isinstance(got, dict) else {}
            except Exception:  # noqa: BLE001 — config の壊れは既定値
                block = {}
    d_raw = environ.get(ENV_DIRECTOR_AFTER, block.get('director_after_seconds'))
    t_raw = environ.get(ENV_TELEGRAM_AFTER, block.get('telegram_after_seconds'))
    return parse_cfg(d_raw if d_raw != '' else None, t_raw if t_raw != '' else None, warn)


# ---------------------------------------------------------------------------
# 台帳
# ---------------------------------------------------------------------------

def _is_stamp(v):
    return v is None or is_finite_number(v)


def escalation_entry_problem(key, entry):
    if not isinstance(key, str) or key.count('/') != 1 or not all(key.split('/')):
        return f'key {key!r} is not <slug>/<tid>'
    if not isinstance(entry, dict):
        return f'{key}: entry is not an object'
    if not isinstance(entry.get('execution_id'), str) or not entry['execution_id']:
        return f"{key}: 'execution_id' is {entry.get('execution_id')!r}"
    if not is_finite_number(entry.get('first_seen')):
        return f"{key}: 'first_seen' is {entry.get('first_seen')!r}"
    if entry.get('stage_sent') not in (0, 1, 2) or isinstance(entry.get('stage_sent'), bool):
        return f"{key}: 'stage_sent' is {entry.get('stage_sent')!r}"
    for f in ('stage_sent_at', 'stage1_failed_at'):
        if f not in entry or not _is_stamp(entry[f]):
            return f"{key}: {f!r} is {entry.get(f)!r}"
    stage = entry.get('sending_stage')                  # 任意 (無ければ送る途中ではない)
    if stage not in (None, 1, 2) or isinstance(stage, bool):
        return f"{key}: 'sending_stage' is {stage!r}"
    if not _is_stamp(entry.get('sending_at')) or (stage is not None and entry.get('sending_at') is None):
        return f"{key}: 'sending_at' is {entry.get('sending_at')!r}"
    return None


def escalation_state_problem(data):
    for key, entry in data.items():
        problem = escalation_entry_problem(key, entry)
        if problem:
            return problem
    return None


def read_ledger(registry_dir, warn=None):
    return load_json_store(Path(registry_dir) / 'daemons' / LEDGER_NAME, check=escalation_state_problem, warn=warn)


def write_ledger(registry_dir, ledger):
    """ロックの下で原子的に書く。書けなければ False。"""
    path = Path(registry_dir) / 'daemons' / LEDGER_NAME
    with told_lock(str(path)) as held:
        if not held or escalation_state_problem(ledger):
            return False
        return write_told_atomic(path, ledger)


# ---------------------------------------------------------------------------
# 1 サイクル
# ---------------------------------------------------------------------------

def run_cycle(registry_dir, all_tasks, done_ids_by_mission, task_statuses_by_mission, active_missions,
              observed_slugs, now, cfg, *, director_live, telegram_available, send_director, send_telegram,
              execution_id_of, session_link='', log=lambda msg: None, trouble=None):
    """段階上げの 1 サイクル。**例外は呼び出し側が受ける**。→ 実行した action の `[(key, action, ok)]`。

    director_live / telegram_available は**呼べる値** (判断待ちの card があるときだけ・1 回だけ評価する)。
    send_director(text) / send_telegram(text) は成功なら True。
    """
    trouble = trouble or log
    awaiting = [(slug, meta) for slug, meta in all_tasks if meta.get('status') in AWAITING_DECISION_STATUSES]
    ledger = read_ledger(registry_dir)
    if is_missing(ledger):
        ledger = {}
    elif is_unreadable(ledger):
        log(f'WARNING: escalation-state: 台帳を使えない ({ledger.reason}) — 段階上げを見送ります '
            f'(registry/daemons/{LEDGER_NAME} を消すと復旧)')
        return []
    if not awaiting and not ledger:
        return []

    memo = {}

    def lazy(name, fn):
        if name not in memo:
            memo[name] = bool(fn())
        return memo[name]

    live_keys = set()
    new_ledger = dict(ledger)
    todo = []                       # (key, card, entry, decision)
    for slug, meta in awaiting:
        tid = str(meta.get('id', '?'))
        key = f'{slug}/{tid}'
        live_keys.add(key)
        card = CardView(slug, tid, meta.get('status'), execution_id_of(meta), slug in observed_slugs)
        entry = ledger.get(key)
        todo.append((key, meta, card, entry))
    # 判断待ちを離れた (または mission ごと無くなった) 台帳の行は R1 で消す。消すのは観測できた mission のものだけ
    # (active でない mission は観測できないので「遅れる側」= 消して時計をやり直す)
    for key, entry in ledger.items():
        if key in live_keys:
            continue
        slug, tid = key.split('/')
        if slug in observed_slugs or slug not in active_missions:
            new_ledger.pop(key, None)

    results = []
    telegram_sent = 0
    for key, meta, card, entry in todo:
        # mux / Telegram への問い合わせは、鳴らしうるときだけ (毎サイクル・待機中の task のたびに叩かない)。
        # 鳴らせないときの値は何でもよい — 経過が最小のしきい値に届かなければ R6・R7 のどちらにも当たらない。
        could_act = _could_act(card, entry, now, cfg)
        decision = decide(card, entry, now, cfg,
                          lazy('director', director_live) if could_act else True,
                          lazy('telegram', telegram_available) if could_act else False)
        action, update = decision
        if action == ACTION_TELEGRAM and telegram_sent >= MAX_TELEGRAM_PER_CYCLE:
            continue                                    # 残りは次のサイクル (記録しない)
        if action != ACTION_NONE:
            slug, tid = card.slug, card.tid
            tasks = [m for s, m in all_tasks if s == slug]
            dependents = blocked_dependents(
                slug, tid, tasks, done_ids_by_mission.get(slug, set()), task_statuses_by_mission.get(slug, {}))
            head = ('[escalation] Director の判断待ちが続いています' if action == ACTION_DIRECTOR
                    else '🛑 crewvia: Director の判断待ちが続いています')
            lines = notice_lines(head, slug, tid, card.status, now - entry['first_seen'], dependents,
                                 meta.get('needs_director_reason') or '', session_link)
            # 記録してから送る: 「段階 N を送る」を先に台帳へ書き、書けなければ送らない (設計 §11)。
            # 送った後の書き込みが失敗しても sending が残るので、次のサイクルは (期限まで) 同じ段階を送らない。
            # 倒れる向き = 保存が壊れている間は通知が欠ける側。連投にはならない。
            stage = 1 if action == ACTION_DIRECTOR else 2
            new_ledger[key] = dict(entry, sending_stage=stage, sending_at=now)
            if not write_ledger(registry_dir, new_ledger):
                new_ledger[key] = entry
                trouble(f'escalation-state に書けないので {key} の段階 {stage} の通知を見送ります '
                        f'(registry/daemons/{LEDGER_NAME} の権限・空き容量・ロックを確認)')
                return results
            ledger = dict(new_ledger)
            if action == ACTION_DIRECTOR:
                ok = bool(send_director(director_text(lines)))
            else:
                ok = bool(send_telegram(telegram_text(lines)))
                telegram_sent += 1
            results.append((key, action, ok))
            new_ledger[key] = update[1] if ok else apply_failure(_expire_sending(entry, now), action, now)
            if not write_ledger(registry_dir, new_ledger):
                log(f'WARNING: escalation-state に結果を書けない ({key}) — sending が残るので同じ段階は送り直しません')
                return results
            ledger = dict(new_ledger)
            continue
        if update[0] == 'set':
            new_ledger[key] = update[1]
        elif update[0] == 'delete':
            new_ledger.pop(key, None)
    if new_ledger != ledger and not write_ledger(registry_dir, new_ledger):
        trouble('escalation-state に書けない (時計・削除の更新) — 次のサイクルでやり直します')
    return results
