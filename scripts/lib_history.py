#!/usr/bin/env python3
"""scripts/lib_history.py — 試行の台帳 `queue/missions/<m>/history/tNNN/` の形と照合 (vNext 02 / PR-D)。

設計: `knowledge/vnext-02-attempt-reopen-checkpoint.md` §3.2。**1 遷移 = 1 ファイル** (`<seq4>-<kind>-<execution_id>.md`)。
既存のファイルを上書きも追記もしない。ここは**形と比べ方だけ**の純粋な module (I/O を持たない)。書く入口は
`lib_state_store.Txn.write_history`、読む入口は `lib_state_store.list_history` / `read_history`、呼ぶのは
Controller の `_finish` の 1 か所 (書く側) と `plan.sh history` / `pull` (読む側)。

## 同定キーと再送の冪等 (§3.2 の 1〜5)

イベントの同定キーは (`execution_id`, `kind`, `reopen_count`)。`find_events()` が名前 (`*-<kind>-<execution_id>.md`) で候補を
絞り、frontmatter の `reopen_count` で確定する。見つかれば `seq4` と `ended_at` をそのファイルから再利用し、比べる欄が全部
同じなら何も書かずに進む。1 つでも違えば `differing_fields()` が**欄の名前だけ**を返す (値は返さない)。除くのは `seq4` (名前) と
`ended_at` だけ。

## 秘密

例外・戻り値の欄名は固定の語彙。台帳の値 (Result・理由) を例外文・メッセージに出さない。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import lib_execution as ex  # noqa: E402
import lib_state_store as store  # noqa: E402
import lib_task_cards as _cards  # noqa: E402

#: kind の語彙。この PR で書くのは end / reset / retire。retry / reopen は PR-E・F が書く (名前の形は今から受け付ける)。
KINDS = ('end', 'reset', 'retire', 'retry', 'reopen')

NAME_RE = store.HISTORY_NAME_RE

#: 再送で比べる欄 (`ended_at` は除く。本文は `body` として別に比べる)。
COMPARED_FIELDS = ('execution_id', 'attempt', 'kind', 'end_code', 'reopen_count', 'head', 'pr_number', 'worker',
                   'actor', 'reason', 'checkpoint_dir', 'result')

#: `result` 欄の語彙 (§3.2)。recorded = 本文に Result 全文を入れた / none = その試行は Result を書かずに終わった /
#: unattributed = 帰属の欄 (`result_execution_id`) が無い card (この設計より前のもの) で、旧本文の Result は入れない。
RESULT_RECORDED = 'recorded'
RESULT_NONE = 'none'
RESULT_UNATTRIBUTED = 'unattributed'
RESULT_STATES = frozenset({RESULT_RECORDED, RESULT_NONE, RESULT_UNATTRIBUTED})


class HistoryError(Exception):
    """台帳の形の誤り。`code` は固定語彙 (値・本文は持たない)。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def filename(seq, kind, execution_id):
    if kind not in KINDS:
        raise HistoryError('bad_kind')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise HistoryError('bad_seq')
    if not ex.is_execution_id(execution_id):
        raise HistoryError('bad_execution_id')
    return f"{seq:04d}-{kind}-{execution_id}.md"


def parse_filename(name):
    """`(seq, kind, execution_id)` か None (台帳の名前の形でない)。"""
    m = NAME_RE.fullmatch(name) if isinstance(name, str) else None
    return (int(m.group(1)), m.group(2), m.group(3)) if m else None


def next_seq(names):
    """今ある台帳の名前の最大の `seq4` + 1 (形が合わない名前は数えない。無ければ 1)。"""
    best = 0
    for name in names:
        parsed = parse_filename(name)
        if parsed is not None:
            best = max(best, parsed[0])
    return best + 1


def candidates(names, kind, execution_id):
    """`*-<kind>-<execution_id>.md` の名前 (昇順)。同定キーの `reopen_count` で確定する前の候補。"""
    out = []
    for name in sorted(names):
        parsed = parse_filename(name)
        if parsed is not None and parsed[1] == kind and parsed[2] == execution_id:
            out.append(name)
    return out


def _norm(value):
    """比べるための正規形: None と欠落は同じ・数と数字の文字列は同じ。"""
    if value is None:
        return None
    return str(value)


def render(fields, body):
    """台帳 1 件の本文 (frontmatter + 本文)。None の欄は書かない。"""
    meta = {k: v for k, v in fields.items() if v is not None}
    problem = fields_problem(meta)
    if problem:
        raise HistoryError(problem)
    return store.serialize_history(meta, body)


def parse(text, source='<history>'):
    """`(fields, body)`。読めなければ `HistoryError('parse_error')` (原因の文字列は持たない)。"""
    try:
        meta, body = _cards.parse_frontmatter(text, source=source)
    except ValueError:
        raise HistoryError('parse_error') from None
    problem = fields_problem(meta)
    if problem:
        raise HistoryError(problem)
    return meta, body


def _is_text(value):
    return isinstance(value, str)


def _is_pr_number(value):
    return (isinstance(value, int) and not isinstance(value, bool) and value >= 0) or isinstance(value, str)


#: 省略できる (None か str) 欄。型の違う値 (list / dict / bool / int) は `bad_field_type`。
_OPTIONAL_TEXT_FIELDS = ('end_code', 'head', 'worker', 'actor', 'reason', 'checkpoint_dir')


def fields_problem(meta):
    """frontmatter の形の検査。問題なら固定コード、健全なら None (値は出さない)。

    **型を先に確かめる**: 正しい YAML でも `result: []` / `kind: {}` のように型の違う値は集合・タプルとの照合で TypeError に
    なり、`HistoryUnreadable` の扱いを素通りする。どの欄も、中身を照合する前に型を見る。"""
    if not isinstance(meta, dict):
        return 'not_a_mapping'
    if not ex.is_execution_id(meta.get('execution_id')):
        return 'bad_execution_id'
    kind = meta.get('kind')
    if not _is_text(kind) or kind not in KINDS:
        return 'bad_kind'
    attempt = meta.get('attempt')
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        return 'bad_attempt'
    reopen = meta.get('reopen_count', 0)
    if reopen is not None and (isinstance(reopen, bool) or not isinstance(reopen, int) or reopen < 0):
        return 'bad_reopen_count'
    if not isinstance(meta.get('ended_at'), str) or not meta.get('ended_at'):
        return 'bad_ended_at'
    result = meta.get('result')
    if result is not None and (not _is_text(result) or result not in RESULT_STATES):
        return 'bad_result'
    for key in _OPTIONAL_TEXT_FIELDS:
        value = meta.get(key)
        if value is not None and not _is_text(value):
            return 'bad_field_type'
    pr = meta.get('pr_number')
    if pr is not None and not _is_pr_number(pr):
        return 'bad_field_type'
    return None


def same_name_and_fields(name, fields):
    """名前 (`<seq4>-<kind>-<execution_id>`) と frontmatter の kind / execution_id が一致するか。"""
    parsed = parse_filename(name)
    return parsed is not None and parsed[1] == fields.get('kind') and parsed[2] == fields.get('execution_id')


#: end_code → 台帳の kind (この PR では end / reset / retire。retry / reopen は PR-E・F)。無ければ `end`。
KIND_BY_END_CODE = {ex.RESET_BY_DIRECTOR: 'reset', ex.RETIRED: 'retire'}


def kind_for_end_code(end_code):
    return KIND_BY_END_CODE.get(end_code, 'end')


def reopen_count_of(fields):
    return fields.get('reopen_count') or 0


def differing_fields(old_fields, old_body, new_fields, new_body):
    """再送で食い違う欄の**名前**。除くのは `ended_at` (と名前の `seq4`)。本文は `body`。値は返さない。"""
    diffs = []
    for k in COMPARED_FIELDS:
        if k == 'reopen_count':
            differs = reopen_count_of(old_fields) != reopen_count_of(new_fields)     # 欠落は 0
        else:
            differs = _norm(old_fields.get(k)) != _norm(new_fields.get(k))
        if differs:
            diffs.append(k)
    if (old_body or '').strip('\n') != (new_body or '').strip('\n'):
        diffs.append('body')
    return diffs


# ---------------------------------------------------------------------------
# Result 節の取り出し (card の本文から)
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r'^##\s+(Description|Result)\s*$', re.IGNORECASE)
_RESULT_RE = re.compile(r'^##\s+Result\s*$', re.IGNORECASE)
_SECTION_RE = re.compile(r'^##\s+\S')


def result_section(body):
    """card の本文の `## Result` 節 (最初の `## Result` から、次の別名の `## ` 節の手前まで)。無ければ ''。

    `plan.sh` の `parse_task_body` / `extract_trailing_body_section` と同じ読み方: 最初の `## Result` だけが境界で、その後に
    もう一度現れる `## Description` / `## Result` は Result の一部。末尾の別の節 (`## Needs-Director 詳細`・
    `## Verification` 等) は Result に入れない。`tests/test_history_result_section_matches_plan_sh.py` が両者の一致を固定する。
    """
    lines = (body or '').splitlines()
    start = None
    for i, line in enumerate(lines):
        if _RESULT_RE.match(line):
            start = i + 1
            break
    if start is None:
        return ''
    end = len(lines)
    for j in range(start, len(lines)):
        line = lines[j]
        if _HEADER_RE.match(line):
            continue
        if _SECTION_RE.match(line):
            end = j
            break
    return '\n'.join(lines[start:end]).strip()


def build_event(*, kind, execution_id, attempt, ended_at, end_code=None, reopen_count=0, head=None,
                pr_number=None, worker=None, actor=None, reason=None, checkpoint_dir=None,
                result_state=RESULT_NONE, result_text=''):
    """`(fields, body)`。`result_state` が recorded のときだけ本文に Result 全文を入れる。"""
    if result_state not in RESULT_STATES:
        raise HistoryError('bad_result')
    fields = dict(execution_id=execution_id, attempt=attempt, kind=kind, end_code=end_code, ended_at=ended_at,
                  reopen_count=reopen_count, head=head, pr_number=pr_number, worker=worker, actor=actor,
                  reason=reason, checkpoint_dir=checkpoint_dir, result=result_state)
    body = (result_text or '').rstrip() if result_state == RESULT_RECORDED else ''
    return fields, body


def ledger_ended_at(queue_dir, slug, tid, execution_id, end_code, reopen_count):
    """その遷移の台帳 (同定キー execution_id・kind・reopen_count) の `ended_at`。台帳が無い・読めない・複数あるときは None
    (回復は推測で時刻を作らない。台帳が無い旧来の card は record の ended_at を null のまま残す)。"""
    kind = kind_for_end_code(end_code)
    listed = store.list_history(queue_dir, slug, tid)
    if listed[0] != 'ok':
        return None
    found = []
    for name in candidates(listed[1], kind, execution_id):
        got = read_event(queue_dir, slug, tid, name)
        if got[0] == 'ok' and reopen_count_of(got[1]) == reopen_count:
            found.append(got[1]['ended_at'])
    return found[0] if len(found) == 1 else None


def missing_record_ended_at(queue_dir, slug, tid, record, meta):
    """終わった試行の execution record で `ended_at` が欠けているとき、台帳の `ended_at` (無ければ None)。

    card の書き込みの後・record の書き込みの前で落ちた試行の回復 (R-5) と、同じ報告の冪等な再送が使う。台帳が無い
    (この設計より前の card) ときは None = 時刻を作らず null のまま残す (record の `ended_at` は表示用で、判断に使わない)。"""
    if record.get('ended_at') is not None:
        return None
    if meta.get('execution_status') not in ex.TERMINAL_EXECUTION_STATUSES:
        return None
    reopen = meta.get('reopen_count')
    if isinstance(reopen, bool) or not isinstance(reopen, int) or reopen < 0:
        reopen = 0 if reopen is None else None
    if reopen is None:
        return None
    return ledger_ended_at(queue_dir, slug, tid, meta.get('current_execution_id'),
                           meta.get('execution_end_code'), reopen)


# ---------------------------------------------------------------------------
# 読み口 (ロックなし。読めないファイルは飛ばさず止める — 空に潰さない)
# ---------------------------------------------------------------------------

class HistoryUnreadable(Exception):
    """台帳の列挙・読み・解釈の失敗。`code` は固定語彙、`name` は台帳の名前の形のファイル名 (無ければ None)。
    原因の文字列・ファイルの中身は持たない。"""

    def __init__(self, code, name=None):
        super().__init__(code)
        self.code = code
        self.name = name


def read_event(queue_dir, slug, tid, name):
    """台帳 1 件。`('ok', fields, body)` / `('unreadable', 固定コード)`。名前と frontmatter の kind / execution_id が
    食い違うものは `name_mismatch` (読めたが、名前が嘘をついている)。"""
    got = store.read_history(queue_dir, slug, tid, name)
    if got[0] == 'absent':
        return ('unreadable', 'vanished')
    if got[0] == 'unreadable':
        return ('unreadable', got[1])
    try:
        fields, body = parse(got[1], source=name)
    except HistoryError as e:
        return ('unreadable', e.code)
    if not same_name_and_fields(name, fields):
        return ('unreadable', 'name_mismatch')
    return ('ok', fields, body)


def load_events(queue_dir, slug, tid):
    """task の台帳を seq 昇順で読む: `[{'name', 'seq', 'path', 'fields', 'body'}]`。台帳が無ければ []。
    列挙・読み・解釈のどれかが失敗したら `HistoryUnreadable` (そのファイルを飛ばして残りを返さない)。
    名前の形でないもの (tmp の残骸等) は数えない (store-check が報告する)。"""
    listed = store.list_history(queue_dir, slug, tid)
    if listed[0] != 'ok':
        raise HistoryUnreadable(listed[1])
    base = store.history_dir(queue_dir, slug, tid)
    events = []
    for name in listed[1]:
        parsed = parse_filename(name)
        if parsed is None:
            continue
        got = read_event(queue_dir, slug, tid, name)
        if got[0] != 'ok':
            raise HistoryUnreadable(got[1], name)
        events.append({'name': name, 'seq': parsed[0], 'path': str(Path(base) / name),
                       'fields': got[1], 'body': got[2]})
    events.sort(key=lambda e: (e['seq'], e['name']))
    return events


def previous_attempt(events, current_execution_id):
    """`pull` の JSON に足す `previous_attempt`: 今の試行**以外**の最後の台帳の要約 (無ければ None)。
    欄は `{execution_id, attempt, end_code, ended_at, head, history_path, checkpoint_dir}`。値の無い欄は省く。"""
    prior = [e for e in events if e['fields'].get('execution_id') != current_execution_id]
    if not prior:
        return None
    last = prior[-1]
    f = last['fields']
    out = {'execution_id': f.get('execution_id'), 'attempt': f.get('attempt'), 'end_code': f.get('end_code'),
           'ended_at': f.get('ended_at'), 'head': f.get('head'), 'history_path': last['path'],
           'checkpoint_dir': f.get('checkpoint_dir')}
    return {k: v for k, v in out.items() if v is not None}


def format_event(event):
    """`plan.sh history` の 1 件分の表示 (台帳のファイルそのもの: frontmatter + 本文)。"""
    return store.serialize_history(
        {k: v for k, v in event['fields'].items() if v is not None}, event['body'])


# ---------------------------------------------------------------------------
# store-check (報告だけ。直さない)
# ---------------------------------------------------------------------------

def diagnose(queue_dir, mission_slugs):
    """台帳の食い違いを**書かずに**列挙する: `[(kind, mission, task, detail)]`。

    * `history_unobservable` — 列挙・読めない・解釈できない・名前と中身の食い違い (detail は固定コード + 名前)
    * `history_without_card` — 台帳の dir があるのに card が無い
    * `history_duplicate_key` — 同じ同定キー (execution_id, kind, reopen_count) が 2 つ以上
    * `history_orphan` — card がまだ閉じていない試行の台帳 (card がコミット点なので、card の書き込みに失敗した孤児。
      害は無く、同じ報告の再送が再利用して先へ進む)
    """
    out = []
    for slug in mission_slugs:
        if not isinstance(slug, str) or store._safe_token(slug) is None:
            continue
        root = Path(queue_dir) / 'missions' / slug
        names, code = store._list_dir(str(root / store.HISTORY_DIRNAME))
        if code:
            out.append(('history_unobservable', slug, None, store._safe_detail(f"history: {code}")))
            continue
        txn = store.Txn(queue_dir, op='diagnose', actor='diagnose')
        for tid in names:
            if not re.fullmatch(r't\d+', tid):
                out.append(('history_unobservable', slug, None, 'redacted'))
                continue
            listed = store.list_history(queue_dir, slug, tid)
            if listed[0] != 'ok':
                out.append(('history_unobservable', slug, tid, store._safe_detail(listed[1])))
                continue
            events = []
            for name in listed[1]:
                if parse_filename(name) is None:
                    continue
                got = read_event(queue_dir, slug, tid, name)
                if got[0] != 'ok':          # 読めないファイルがあっても、読めるファイルの検査は続ける
                    out.append(('history_unobservable', slug, tid, store._safe_detail(f"{got[1]}:{name}")))
                    continue
                events.append({'name': name, 'fields': got[1]})
            try:
                meta, _body = txn.load_card(slug, tid)
            except store.CardNotFound:
                out.append(('history_without_card', slug, tid, None))
                continue
            except store.StoreError:
                meta = None                 # card が読めない: orphan の判定はしない (回復の側が報告する)
            seen = {}
            for e in events:
                f = e['fields']
                key = (f.get('execution_id'), f.get('kind'), reopen_count_of(f))
                seen.setdefault(key, []).append(e['name'])
            for key, ev_names in sorted(seen.items(), key=lambda kv: kv[1]):
                if len(ev_names) > 1:
                    out.append(('history_duplicate_key', slug, tid, f"{len(ev_names)} files"))
            if meta is not None and meta.get('execution_status') in ex.ACTIVE_STATUSES:
                current = meta.get('current_execution_id')
                reopen = meta.get('reopen_count') or 0
                for e in events:
                    f = e['fields']
                    if f.get('execution_id') == current and reopen_count_of(f) == reopen:
                        out.append(('history_orphan', slug, tid, f.get('kind')))
    return out
