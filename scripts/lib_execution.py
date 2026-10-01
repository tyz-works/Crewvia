#!/usr/bin/env python3
"""scripts/lib_execution.py — Execution (試行) の語彙・判定・record の形。**データと純粋関数だけ** (vNext 01c / E1)。

設計: `knowledge/execution.md` §1 (置き場と正本)・§2.2 (世代の置き換え)・§4 (遷移表・冪等・domain error)。

`lib_task_status.py` と同じ作法 —— **I/O なし・他の crewvia モジュールを import しない**。書き込みの lib
(回復 R-1 / R-5・`classify_assignment`・監査の門) も `lib_task_controller` (遷移) も、ここの関数を呼ぶ。
ここに置くのは「card 1 枚から決まること」だけで、書き込みの lib が `lib_task_controller` を import すると
循環するため、両者が共有する判定を 1 か所に分けた (設計文書は `attempt_view` / `execution_matches` を
`lib_task_controller` に置くと書いているが、置き場だけ変えた。名前と契約は同じ)。

## 正本は card

試行の status・終了コード・attempt・予約時の世代・予約した agent・task_slug は card の frontmatter に置く
(`EXECUTION_FIELDS`)。record (`queue/missions/<slug>/executions/<id>.json`) は projection で、判断には
使わない。`attempt_view()` が card 1 枚から 4 つ (NONE / DETACHED / ACTIVE / TERMINAL) のどれかを返し、
照合・冪等・R-1・reserve・reset・store-check はこれだけを読む。

## 出さないもの

`ControllerError` のメッセージ・`detail` に、card の本文・frontmatter の他の欄・Result・名乗られた値の中身を
入れない (固定コード + 識別子だけ。01a / 01b で 3 回指摘された「解析エラーに元の行が漏れる」族)。
"""

from __future__ import annotations

import re
import uuid

# ---------------------------------------------------------------------------
# 語彙
# ---------------------------------------------------------------------------

EXECUTION_ID_RE = re.compile(r'ex-[0-9a-f]{32}')

#: Execution status (原案 EXEC-04)。`stale` / `abandoned` / `recovered` は後続 (01c の範囲外)。
RESERVED, RUNNING, COMPLETED, FAILED, RELEASED = 'reserved', 'running', 'completed', 'failed', 'released'
ACTIVE_STATUSES = frozenset({RESERVED, RUNNING})
TERMINAL_EXECUTION_STATUSES = frozenset({COMPLETED, FAILED, RELEASED})
EXECUTION_STATUSES = ACTIVE_STATUSES | TERMINAL_EXECUTION_STATUSES

#: 許可遷移 (EXEC-04)。running → released は無い (結果なしで手放すのは failed + 固定コード)。
TRANSITIONS = {
    RESERVED: frozenset({RUNNING, FAILED, RELEASED}),
    RUNNING: frozenset({COMPLETED, FAILED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    RELEASED: frozenset(),
}

# 終了コード (card の `execution_end_code`。execution.md §4.4)
DONE = 'DONE'
VERIFIED = 'VERIFIED'
WORKER_FAILED = 'WORKER_FAILED'
NEEDS_DIRECTOR = 'NEEDS_DIRECTOR'
VERIFICATION_REJECTED = 'VERIFICATION_REJECTED'
RESET_BY_DIRECTOR = 'RESET_BY_DIRECTOR'
RETIRED = 'RETIRED'
WORKSPACE_CREATE_FAILED = 'WORKSPACE_CREATE_FAILED'
ABANDONED_OUTSIDE_CONTROLLER = 'ABANDONED_OUTSIDE_CONTROLLER'

#: execution_status → その status にしてよい終了コード (欄の組の整合。execution.md §1.2)
END_CODES_BY_STATUS = {
    COMPLETED: frozenset({DONE, VERIFIED}),
    FAILED: frozenset({WORKER_FAILED, NEEDS_DIRECTOR, VERIFICATION_REJECTED, RESET_BY_DIRECTOR, RETIRED,
                       WORKSPACE_CREATE_FAILED, ABANDONED_OUTSIDE_CONTROLLER}),
    RELEASED: frozenset({RESET_BY_DIRECTOR, RETIRED}),
}
END_CODES = frozenset().union(*END_CODES_BY_STATUS.values())

#: 同じ ID の再送を**成功にする**組 (execution.md §4.4 の表)。キーは終了コード、値は来た操作。
#: RESET_BY_DIRECTOR / WORKSPACE_CREATE_FAILED / ABANDONED_OUTSIDE_CONTROLLER は誰の再送も成功にしない
#: (その試行は持ち主以外の操作で終わり、もう誰の作業でもない)。
IDEMPOTENT_OPERATION = {
    DONE: 'done',
    VERIFIED: 'verify-result:pass',
    WORKER_FAILED: 'fail',
    NEEDS_DIRECTOR: 'needs-director',
    VERIFICATION_REJECTED: 'verify-result:fail',
    RETIRED: 'retire',
}

#: card の frontmatter に足す欄。書き込みの lib の `TASK_META_KEY_ORDER` は `started_at` の直後にこの順で並べる。
#: `execution_agent` は 7 つ目 (execution.md §1.2 の 6 欄 + E1 で足した「予約した agent の写し」)。
EXECUTION_FIELDS = (
    'current_execution_id', 'execution_status', 'execution_end_code', 'execution_count',
    'execution_reserved_at', 'execution_agent', 'task_slug',
)
#: 試行があるなら**必ずある**欄 (execution_end_code は terminal のときだけ、execution_agent は agent を
#: 名指しして予約したときだけ)。
REQUIRED_WHEN_PRESENT = ('current_execution_id', 'execution_status', 'execution_count',
                         'execution_reserved_at', 'task_slug')

# ---------------------------------------------------------------------------
# domain error (CTRL-05)
# ---------------------------------------------------------------------------

STATE_INVALID = 'STATE_INVALID'
LOCK_FAILED = 'LOCK_FAILED'
TASK_NOT_FOUND = 'TASK_NOT_FOUND'
TASK_NOT_ELIGIBLE = 'TASK_NOT_ELIGIBLE'
TASK_ALREADY_RESERVED = 'TASK_ALREADY_RESERVED'
EXECUTION_NOT_FOUND = 'EXECUTION_NOT_FOUND'
EXECUTION_NOT_CURRENT = 'EXECUTION_NOT_CURRENT'
EXECUTION_ALREADY_TERMINAL = 'EXECUTION_ALREADY_TERMINAL'
INVALID_TRANSITION = 'INVALID_TRANSITION'
GIT_POLICY_INVALID = 'GIT_POLICY_INVALID'
INVALID_ARGUMENT = 'INVALID_ARGUMENT'       # 呼び出し側の誤り (CTRL-05 の「最低限」に足した。exit 1)

ERROR_CODES = frozenset({
    STATE_INVALID, LOCK_FAILED, TASK_NOT_FOUND, TASK_NOT_ELIGIBLE, TASK_ALREADY_RESERVED,
    EXECUTION_NOT_FOUND, EXECUTION_NOT_CURRENT, EXECUTION_ALREADY_TERMINAL, INVALID_TRANSITION,
    GIT_POLICY_INVALID, INVALID_ARGUMENT,
})

#: 他のコマンドの exit code (execution.md §4.4 の表。plan.sh が使う写し。pull の idle の 2 は別)。
EXIT_CODES = {
    STATE_INVALID: 1, LOCK_FAILED: 1, TASK_NOT_FOUND: 1, TASK_NOT_ELIGIBLE: 1, TASK_ALREADY_RESERVED: 1,
    GIT_POLICY_INVALID: 1, INVALID_ARGUMENT: 1,
    INVALID_TRANSITION: 2,                                   # 今の REFUSED_TRANSITION と同じ
    EXECUTION_NOT_FOUND: 3, EXECUTION_NOT_CURRENT: 3, EXECUTION_ALREADY_TERMINAL: 3,
}

#: 拒否の行 (監査ログの `result=refused:<CODE>`) にするもの (execution.md §8)。
AUDITED_REFUSALS = frozenset({
    EXECUTION_NOT_CURRENT, EXECUTION_NOT_FOUND, EXECUTION_ALREADY_TERMINAL, INVALID_TRANSITION,
    TASK_ALREADY_RESERVED,
})


class ControllerError(Exception):
    """Controller が返す domain error。**機械が読むのは `code`** (文字列 message の解析を契約にしない)。

    `message` は人が読む 1 行で、固定の文言 + 識別子 (mission / task / execution id) だけ。card の中身・
    名乗られた値は入れない。`exit_code` は plan.sh が使う写し。
    """

    def __init__(self, code, message, *, mission=None, task=None, execution_id=None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.message = message
        self.mission = mission
        self.task = task
        self.execution_id = execution_id
        super().__init__(f"[{code}] {message}")

    @property
    def exit_code(self):
        return EXIT_CODES[self.code]


# ---------------------------------------------------------------------------
# ID
# ---------------------------------------------------------------------------

def uuid4_execution_id():
    """既定の ID 生成器: `ex-` + UUID v4 の 32 桁小文字 16 進。agent 名・task ID・時刻から作らない (EXEC-01)。"""
    return 'ex-' + uuid.uuid4().hex


def is_execution_id(value):
    return isinstance(value, str) and EXECUTION_ID_RE.fullmatch(value) is not None


# ---------------------------------------------------------------------------
# 欄の組の整合 (lint と STATE_INVALID が同じ関数を呼ぶ)
# ---------------------------------------------------------------------------

def has_execution_fields(meta):
    """試行に関する欄が 1 つでも**書かれているか** (キーの有無。値の真偽では見ない)。"""
    return any(k in meta for k in EXECUTION_FIELDS)


def fields_problem(meta):
    """card の試行の欄が壊れていれば**固定コード**、健全 (または試行なし) なら None。値は返さない。

    - 片方だけある (`current_execution_id` だけ・`execution_status` だけ …) = 手編集か壊れた書き込み
    - `current_execution_id` が `ex-<32hex>` でない・`execution_status` が語彙外・`execution_count` が正の整数でない
    - `execution_status` が active なら `execution_end_code` は空、terminal なら §4.4 の組にあるもの
    - `execution_reserved_at` / `task_slug` / `execution_agent` が文字列でない
    """
    if not has_execution_fields(meta):
        return None
    for key in REQUIRED_WHEN_PRESENT:
        if meta.get(key) in (None, ''):
            return 'execution_fields_incomplete'
    if not is_execution_id(meta['current_execution_id']):
        return 'execution_id_malformed'
    status = meta['execution_status']
    if not isinstance(status, str) or status not in EXECUTION_STATUSES:
        return 'execution_status_unknown'
    count = meta['execution_count']
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return 'execution_count_invalid'
    for key in ('execution_reserved_at', 'task_slug'):
        if not isinstance(meta[key], str):
            return 'execution_field_type'
    agent = meta.get('execution_agent')
    if agent is not None and (not isinstance(agent, str) or agent == ''):
        return 'execution_field_type'
    end = meta.get('execution_end_code')
    if status in ACTIVE_STATUSES:
        if end not in (None, ''):
            return 'execution_end_code_on_active'
    else:
        if not isinstance(end, str) or end not in END_CODES_BY_STATUS[status]:
            return 'execution_end_code_mismatch'
    return None


# ---------------------------------------------------------------------------
# attempt_view (execution.md §1.2)
# ---------------------------------------------------------------------------

NONE = 'NONE'
DETACHED = 'DETACHED'
ACTIVE = 'ACTIVE'
TERMINAL = 'TERMINAL'


def attempt_view(meta, holding_statuses):
    """card の欄**だけ**から 4 つのどれかを返す。照合・冪等・R-1・reserve・reset・store-check が呼ぶ唯一の読み方。

    上から順に判定する:

    1. `NONE`     — `current_execution_id` が無い (legacy の card)
    2. `DETACHED` — (a) `started_at` が空でなく `execution_reserved_at` と違う (Controller を通らない pull が取り直した)、
                    または (b) `execution_status` が reserved / running で task の status が `holding_statuses` に無い
                    (Controller の外で手放した)。**card の今の持ち主は X の持ち主ではない**
    3. `ACTIVE`   — DETACHED でなく status が reserved / running
    4. `TERMINAL` — DETACHED でなく status が terminal

    `holding_statuses` は引数で受ける (`lib_task_status.ASSIGNMENT_HOLDING_STATUSES`。この module は何も import しない)。
    **欄が壊れている card は呼び出し側が先に `fields_problem()` で弾く**: ここでは壊れた値を既定値に倒さず
    `ValueError` (固定文言) にする。DETACHED は保存しない (毎回 card から計算する)。
    """
    problem = fields_problem(meta)
    if problem:
        raise ValueError(problem)
    if not has_execution_fields(meta):
        return NONE
    started = meta.get('started_at')
    if started not in (None, '') and str(started) != meta['execution_reserved_at']:
        return DETACHED
    if meta['execution_status'] in ACTIVE_STATUSES:
        if meta.get('status') not in holding_statuses:
            return DETACHED
        return ACTIVE
    return TERMINAL


def is_detached_a(meta):
    """DETACHED のうち (a): 空でない `started_at` が予約の値と違う。store-check が (a) / (b) を別の報告にするのに使う。"""
    started = meta.get('started_at')
    return started not in (None, '') and str(started) != meta.get('execution_reserved_at')


def is_legacy_execution(meta, holding_statuses):
    """E4b の merge 条件 (1) の数え方: holding の card で `current_execution_id` が無い。
    **E1 では store-check に配線しない** (今の本番データでは進行中の全 card が当たり、E1 の「本番の挙動は変わらない」を
    破る。E2 が pull に ID を発行させる段で 1 行足す)。"""
    return meta.get('status') in holding_statuses and meta.get('current_execution_id') in (None, '')


# ---------------------------------------------------------------------------
# 世代の置き換え (execution.md §2.2) — 照合の規則 1 か所
# ---------------------------------------------------------------------------

MATCH = 'match'
MISMATCH = 'mismatch'

# caller_check (監査行の値)
CHECK_VERIFIED = 'verified'
CHECK_UNVERIFIED = 'unverified'
CHECK_LEGACY_GENERATION = 'legacy_generation'
CHECK_NO_EXECUTION = 'no_execution'
CHECK_DETACHED = 'detached_execution'
CALLER_CHECKS = frozenset({CHECK_VERIFIED, CHECK_UNVERIFIED, CHECK_LEGACY_GENERATION, CHECK_NO_EXECUTION,
                           CHECK_DETACHED})


def execution_matches(view, meta, *, execution_id=None, started_at=None):
    """名乗られた証拠が card の今の試行と一致するか。移行期の規則の**唯一の定義** (execution.md §2.2)。

    `view` は `attempt_view(meta, ...)` の答え。証拠は `execution_id` と `started_at` (旧 marker・旧引数) の 2 種類で、
    **「指定されたか」は None かどうかで見る** (空文字を「無い」に倒さない —— 空の明示指定は `MISMATCH`)。

    戻り値 `(MATCH|MISMATCH, caller_check)`:

    | view | 証拠 | 判定 | caller_check |
    |---|---|---|---|
    | ACTIVE / TERMINAL | execution_id = X | match | verified |
    | 同上 | execution_id ≠ X | mismatch (`started_at` が一致していても。ID が優先) | — |
    | 同上 | execution_id なし・started_at のみ | `started_at` が予約の値なら match | legacy_generation |
    | NONE | started_at のみ | 今の世代の照合: card の `started_at` と一致で match | legacy_generation |
    | NONE | execution_id | mismatch (この card はその ID を発行していない) | — |
    | DETACHED | execution_id | mismatch | — |
    | DETACHED | started_at のみ | card の `started_at` と一致で match (旧コードの持ち主の世代) | detached_execution |
    | どれでも | 証拠なし | mismatch (照合できない。呼び出し側が「名乗りなし」の経路を決める) | — |
    """
    if execution_id is not None:
        if view in (ACTIVE, TERMINAL):
            return ((MATCH, CHECK_VERIFIED) if execution_id == meta.get('current_execution_id')
                    else (MISMATCH, None))
        return (MISMATCH, None)              # NONE / DETACHED
    if started_at is None:
        return (MISMATCH, None)
    recorded = meta.get('started_at')
    same = recorded not in (None, '') and str(recorded) == str(started_at)
    if view in (ACTIVE, TERMINAL):
        # 予約時の世代 (card の `started_at` は reset 等で null になりうるので `execution_reserved_at` と比べる)
        return ((MATCH, CHECK_LEGACY_GENERATION) if str(started_at) == meta.get('execution_reserved_at')
                else (MISMATCH, None))
    if view == NONE:
        return ((MATCH, CHECK_LEGACY_GENERATION) if same else (MISMATCH, None))
    return ((MATCH, CHECK_DETACHED) if same else (MISMATCH, None))     # DETACHED


def identity_matches(identity, *, execution_id=None, generation=None):
    """projection (`.identity`) が「この試行のもの」か。execution.md §2.2 の末尾の規則。

    identity と呼び出し側の**両方**に `execution_id` があれば ID で、そうでなければ `started_at` で比べる。
    「片方にだけ ID がある」組 (card に X・identity に ID なし) は、identity が E2 前に公開されたものか旧コードの
    publish なので、`started_at` が一致すれば一致 (読めないに倒すと cutover の瞬間に全 Worker の枠が
    UNVERIFIABLE になる)。ID が両方にあって違えば、`started_at` が一致していても不一致 (ID が優先)。
    """
    recorded_id = identity.get('execution_id')
    if recorded_id is not None and execution_id is not None:
        return recorded_id == execution_id
    recorded = identity.get('started_at')
    if recorded is None or generation is None:
        return False
    return str(recorded) == str(generation)


# ---------------------------------------------------------------------------
# record (projection)
# ---------------------------------------------------------------------------

RECORD_SCHEMA_VERSION = 1

#: R-5 が card に合わせる欄 (追従する欄)。それ以外 (不変の欄) は作るとき 1 回だけ書き、以後どの回復も上書きしない。
FOLLOWING_FIELDS = ('status', 'end_code')
IMMUTABLE_FIELDS = ('schema_version', 'execution_id', 'mission', 'task', 'attempt', 'agent', 'reserved_at', 'git')


def empty_git():
    return {'branch': None, 'base': None, 'pr_base': None, 'worktree': None, 'head_at_start': None}


def record_from_card(meta, mission, task, *, git=None):
    """card から record を作る (R-5 の「無いとき」と reserve が使う)。

    `agent` は card の `execution_agent` (reserve が予約した agent の写し。次の reserve まで変わらない)。無ければ
    null (呼び出し側が `reported:execution_record_owner_unknown` を出す)。`worker` は読まない —— reset・retire・
    `verify-result fail` は worker を null にし、`update --worker B` は書き換えるのが正常で、予約した人とは別。
    `running_at` / `ended_at` / `git.*` は card に無い (判断に使わない詳細) ので、渡されなければ null。
    """
    g = empty_git()
    if git:
        g.update({k: git.get(k) for k in g})
    return {
        'schema_version': RECORD_SCHEMA_VERSION,
        'execution_id': meta['current_execution_id'],
        'mission': mission,
        'task': task,
        'attempt': meta['execution_count'],
        'agent': meta.get('execution_agent'),
        'status': meta['execution_status'],
        'reserved_at': meta['execution_reserved_at'],
        'running_at': None,
        'ended_at': None,
        'end_code': meta.get('execution_end_code') or None,
        'git': g,
    }


def record_problem(record, meta, mission, task):
    """読めた record の形の検査。`None` (健全) / `'identity_mismatch'` (手編集か壊れた書き込み。上書きしない) 。

    不変の欄 `execution_id` / `attempt` / `mission` / `task` が card と食い違う、または型が違う record は
    R-5 が触らず報告する。`agent` / `reserved_at` は比べない (card の worker / started_at は null や別の名前になるのが
    正常で、record と食い違って当然。execution.md §1.4)。
    """
    if record_shape_problem(record):
        return 'identity_mismatch'
    if (record.get('execution_id') != meta['current_execution_id']
            or record.get('attempt') != meta['execution_count']
            or record.get('mission') != mission or record.get('task') != task):
        return 'identity_mismatch'
    return None


def record_shape_problem(record):
    """record の**形**の検査 (欄の有無と型。値は card と比べない)。`None` (健全) か固定コード `'record_not_object'` /
    `'record_schema'` / `'record_field_type'`。値は返さない。

    判断・回復・診断のどの経路も、record を読む前にこれを通す: 欠けた欄で KeyError、`status: []` で
    unhashable の TypeError、を起こさない (崩れた projection は card から導くか finding にする。execution.md §14)。
    `Execution.from_record` が読む欄 (必須: execution_id / mission / task / attempt / status / reserved_at / git、
    任意: agent / end_code / running_at / ended_at) がすべて対象。"""
    if not isinstance(record, dict):
        return 'record_not_object'
    if record.get('schema_version') != RECORD_SCHEMA_VERSION:
        return 'record_schema'
    for key in ('execution_id', 'mission', 'task', 'reserved_at'):
        if not isinstance(record.get(key), str) or record[key] == '':       # 空は「欄が無い」と同じ (card から導く)
            return 'record_field_type'
    attempt = record.get('attempt')
    if not isinstance(attempt, int) or isinstance(attempt, bool):
        return 'record_field_type'
    status = record.get('status')
    if not isinstance(status, str) or status not in EXECUTION_STATUSES:
        return 'record_field_type'
    for key in ('agent', 'end_code', 'running_at', 'ended_at'):
        if record.get(key) is not None and not isinstance(record[key], str):
            return 'record_field_type'
    if not isinstance(record.get('git'), dict):
        return 'record_field_type'
    return None


def record_follows_card(record, meta):
    """追従する欄 (`status` / `end_code`) が card と一致しているか。"""
    return (record.get('status') == meta['execution_status']
            and (record.get('end_code') or None) == (meta.get('execution_end_code') or None))


def follow_card(record, meta, *, ended_at=None, running_at=None, git=None):
    """追従する欄だけ card に合わせた record の**コピー**を返す。不変の欄と card に無い欄は既存の値を残す。
    `ended_at` / `running_at` / `git` は書いた操作 (start 等) が渡したときだけ入れる (R-5 は渡さず、既存の値を残す)。"""
    out = dict(record)
    out['git'] = dict(record['git'])
    out['status'] = meta['execution_status']
    out['end_code'] = meta.get('execution_end_code') or None
    if ended_at is not None:
        out['ended_at'] = ended_at
    if running_at is not None:
        out['running_at'] = running_at
    if git:
        out['git'].update({k: v for k, v in git.items() if k in out['git']})
    return out


# ---------------------------------------------------------------------------
# task_slug (title から)
# ---------------------------------------------------------------------------

def slugify_title(title, fallback):
    """title → URL-safe な task_slug。`plan.sh cmd_pull` の `_slugify` と**同じ式** (E2 で plan.sh のコピーを消す)。
    ASCII 以外を捨て、英数字の連なりを `-` でつなぎ、40 文字で切る。空なら `fallback` (task id)。"""
    ascii_only = re.sub(r'[^\x00-\x7F]+', ' ', str(title))
    normalized = re.sub(r'[^a-zA-Z0-9]+', ' ', ascii_only)
    parts = [p.lower() for p in normalized.split() if p]
    slug = '-'.join(parts)[:40].rstrip('-')
    return slug or fallback
