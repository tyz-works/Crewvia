#!/usr/bin/env python3
"""scripts/lib_ci_status.py — PR の CI の進み具合を取って記録し、1 行にする (見える化 + Director への知らせ)。

設計: `knowledge/ci-status.md`。

- **取得は dispatcher の 1 か所** (`poll_cycle`)。`card_is_watched` の task (card に `pr_number` があれば status 不問・無ければ
  in_progress / ready_for_verification / verifying で、Git Policy Resolver の branch から `gh pr list --head`) について
  `gh pr view <N> --json headRefOid,statusCheckRollup,state` を呼び、`registry/ci/<mission>__<task>.json` に書く。
- **失敗・timeout・形の違う JSON は `unknown`**。緑にも「CI なし」にも倒さない。
- 表示 (plan.sh status・tasks.json・watchdog / Rule 5 の通知) は**記録を読むだけ**で gh を呼ばない。
- 記録は消してよい (無ければ次の poll で取り直す)。書き手は dispatcher だけ。
- **判定 (黙る・殺す) には使わない**。例外は門 (`gate_verdict`) 1 つだけ: 「通さない」「知らせる」にだけ使い、
  待ちを解く・done にする・Rule 5 / watchdog を黙らせることには使わない (設計 `knowledge/ci-wait-operations.md` §2.2・§2.3)。

純粋関数 (`classify_check` / `summarize` / `format_line` / `record_problem` / `parse_view` / `parse_pr_list`) と
I/O (`run_gh` / `read_record` / `write_record` / `poll_cycle`) を分けてある。
"""

from __future__ import annotations

import calendar
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib_daemon_state import load_json_store, write_told_atomic  # noqa: E402
from lib_git_policy import DEFAULT_BASE_BRANCH, GitPolicyError, check_mission_slug, check_task_id  # noqa: E402
from lib_task_cards import is_missing, is_unreadable  # noqa: E402
from lib_task_status import CI_FOLLOW_PR_STATUSES, CI_WATCH_STATUSES, TERMINAL_STATUSES  # noqa: E402

#: CI を見る task の status (Worker が PR の CI を待ちうる状態)。定義は lib_task_status の 1 か所。
POLL_STATUSES = CI_WATCH_STATUSES
#: card に `pr_number` がある task は、status を問わず PR が open な間は見続ける (done を含む。定義は lib_task_status の 1 か所)。
FOLLOW_STATUSES = CI_FOLLOW_PR_STATUSES

#: 1 つの task (PR) を問い合わせる最短の間隔 (秒) の既定値。値を持つのは `lib_daemon_watch.WatchConfig.ci_poll_seconds`。
DEFAULT_POLL_SECONDS = 60
#: gh 1 回の上限 (秒)。
GH_TIMEOUT_SECONDS = 15
#: 1 サイクルで gh を呼んでよい時間の合計 (秒)。dispatcher の stale しきい値 (既定 60 秒) より十分小さく。
CYCLE_BUDGET_SECONDS = 20
#: 1 サイクルで問い合わせる task の上限。
MAX_TASKS_PER_CYCLE = 4
#: 結論が出た PR (state が passed / failed。head が変われば次の取得で running に戻り DEFAULT_POLL_SECONDS に戻る) の間隔 (秒)。
CONCLUDED_POLL_SECONDS = 300
#: `ci-unknown` を出す、unknown が続いた取得の回数 (設計 §2.4。既定の間隔 60 秒で約 180 秒)。
UNKNOWN_NOTIFY_STREAK = 3
#: main の最新 run を問い合わせる最短の間隔 (秒)。
MAIN_POLL_SECONDS = 300
#: 記録がこの秒数より古ければ表示に「(n 分前)」を付ける。
STALE_AFTER_SECONDS = 300
#: 門 (`gate_verdict`) が記録を「新しい」と認める最大の経過秒 (設計 §2.2 条件 4。10 分)。表示用の STALE とは別の定義。
GATE_MAX_AGE_SECONDS = 600
#: 門が緑の根拠にする必須 check の名前 (設計 §2.2 条件 3・§5。ci.yml の集約 job)。
REQUIRED_CHECK = 'ci-ok'

#: 記録の state。`none` = gh は答えたが check が 1 つも無い (CI 未登録)。`no_pr` = PR が見つからない (表示しない)。
STATES = ('running', 'passed', 'failed', 'unknown', 'none', 'no_pr')
#: 記録の任意の欄 `pr_state` (gh の PR の state)。MERGED / CLOSED は終端 = 取得の対象から外れる。
#: 欄が無い記録 (古い版が書いた) は OPEN 扱いで、次の取得で書き直される。
#: `NOT_FOUND` は gh が「その番号の PR は無い」と答えた印 (gh の state ではない。#377 P3-2: 別 repo の PR 番号を持つ card が
#: crewvia の repo に `gh pr view N` を打ち続けない)。終端として扱う (card の `pr_number` が変われば取り直す)。
PR_STATES = ('OPEN', 'MERGED', 'CLOSED', 'NOT_FOUND')
PR_TERMINAL_STATES = ('MERGED', 'CLOSED', 'NOT_FOUND')

#: 記録と check の欄。**検査 (`record_problem`) が必須にする欄 = 読み手 (`summarize` / `format_line` / `task_graph_ci` /
#: `poll_cycle`) が `[]` で読む欄**の 1 定義 (#314 r4: 検査が `name` と `state` だけの check を通し、summarize が
#: `c['started_at']` で KeyError)。書き手 (`classify_check` / `_view` / `_unknown` / `observe_task`) は全部の欄を書く。
#: 欄を読み手に足したらここに足す (`reason` は get で読む任意の欄)。
RECORD_FIELDS = ('pr', 'head', 'state', 'checks', 'observed_at', 'summary')
CHECK_FIELDS = ('name', 'state', 'conclusion', 'started_at', 'completed_at')

# 1 つの check の分類 (GitHub の CheckRun.status / conclusion と StatusContext.state)。
_RUN_PASSED = {'SUCCESS'}
_RUN_NEUTRAL = {'NEUTRAL', 'SKIPPED'}
_RUN_FAILED = {'FAILURE', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'STALE'}
_RUN_ACTIVE = {'QUEUED', 'IN_PROGRESS', 'WAITING', 'PENDING', 'REQUESTED'}
_CTX = {'SUCCESS': 'passed', 'FAILURE': 'failed', 'ERROR': 'failed', 'PENDING': 'running', 'EXPECTED': 'running'}

_SHA_RE = re.compile(r'[0-9a-f]{40}')
_PR_NOT_FOUND_RE = re.compile(r'Could not resolve to a PullRequest|no pull requests? found', re.IGNORECASE)
_ISO_RE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')
_NAME_MAX = 80


def _env_test_isolation_blocks_gh(environ=None):
    """テスト中 (`CREWVIA_MUX_TEST_ISOLATION`) は、テストが stub を置いた印
    (`CREWVIA_CI_ALLOW_GH_IN_TESTS=1`) が無ければ gh を呼ばない (本物の GitHub に飛ばさない)。"""
    environ = os.environ if environ is None else environ
    return bool(environ.get('CREWVIA_MUX_TEST_ISOLATION')) and environ.get('CREWVIA_CI_ALLOW_GH_IN_TESTS') != '1'


# ---------------------------------------------------------------------------
# 純粋関数
# ---------------------------------------------------------------------------

def _iso(value):
    return value if isinstance(value, str) and _ISO_RE.fullmatch(value) else None


def _epoch(iso):
    try:
        return calendar.timegm(time.strptime(iso, '%Y-%m-%dT%H:%M:%SZ')) if iso else None
    except (TypeError, ValueError):
        return None


def _clean_name(name):
    """check 名は外 (PR の workflow) から来て、Director の画面への送信文に入る。制御文字・改行・空白の連続を
    1 つの空白にし、長さを切る。空になれば '?'。"""
    if not isinstance(name, str):
        return '?'
    name = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in name).split())
    return name[:_NAME_MAX] or '?'


def classify_check(item):
    """statusCheckRollup の 1 要素 → `{name, state, conclusion, started_at, completed_at}`。
    state は running / passed / neutral / failed / unknown (知らない値・形は unknown)。"""
    if not isinstance(item, dict):
        return {'name': '?', 'state': 'unknown', 'conclusion': None, 'started_at': None, 'completed_at': None}
    kind = item.get('__typename')
    if kind == 'StatusContext':
        name, raw = item.get('context'), item.get('state')
        state = _CTX.get(raw, 'unknown') if isinstance(raw, str) else 'unknown'
        conclusion = raw if isinstance(raw, str) else None
    elif kind == 'CheckRun':
        name, status, conclusion = item.get('name'), item.get('status'), item.get('conclusion')
        # 外から来る値。文字列でなければ (配列・dict 等) 集合と照合しない = unknown (照合すると TypeError)
        if not isinstance(status, str) or (conclusion is not None and not isinstance(conclusion, str)):
            status, conclusion = None, None
        if status == 'COMPLETED':
            state = ('passed' if conclusion in _RUN_PASSED else 'neutral' if conclusion in _RUN_NEUTRAL
                     else 'failed' if conclusion in _RUN_FAILED else 'unknown')
        elif status in _RUN_ACTIVE:
            state = 'running'
        else:
            state = 'unknown'
        conclusion = conclusion if isinstance(conclusion, str) and conclusion else None
    else:
        name, state, conclusion = item.get('name') or item.get('context'), 'unknown', None
    name = _clean_name(name)
    return {'name': name, 'state': state, 'conclusion': conclusion,
            'started_at': _iso(item.get('startedAt')), 'completed_at': _iso(item.get('completedAt'))}


def overall_state(checks):
    """check の一覧 → 記録の state。failed > unknown > running > passed。空は `none`。"""
    if not checks:
        return 'none'
    states = {c['state'] for c in checks}
    for s in ('failed', 'unknown', 'running'):
        if s in states:
            return s
    return 'passed'


def _minutes(seconds):
    return max(0, int(seconds // 60))


def summarize(pr, head, checks, now=None):
    """1 行の要約。`now` を渡すと実行中の check の経過分を付ける (tasks.json 用は付けない = 安定した文字列)。

    例: `CI PR #313 @086c9f5: ✓5 ⏳1 (pytest 12m) ✗0`"""
    state = overall_state(checks)
    head7 = (head or '?')[:7]
    if state == 'none':
        return f'CI PR #{pr} @{head7}: CI 未登録 (check 0)'
    count = {s: sum(1 for c in checks if c['state'] == s) for s in ('passed', 'running', 'failed', 'neutral', 'unknown')}
    parts = [f"✓{count['passed']}", f"⏳{count['running']}"]
    running = [c for c in checks if c['state'] == 'running']
    if running:
        def started(c):
            e = _epoch(c['started_at'])
            return e if e is not None else math.inf
        longest = min(running, key=started)
        e = _epoch(longest['started_at'])
        if now is not None and e is not None:
            parts.append(f"({longest['name']} {_minutes(now - e)}m)")
        else:
            parts.append(f"({longest['name']})")
    parts.append(f"✗{count['failed']}")
    failed = [c['name'] for c in checks if c['state'] == 'failed']
    if failed:
        parts.append('(' + ', '.join(failed[:3]) + (' …' if len(failed) > 3 else '') + ')')
    if count['neutral']:
        parts.append(f"–{count['neutral']}")
    if count['unknown']:
        parts.append(f"?{count['unknown']}")
    return f'CI PR #{pr} @{head7}: ' + ' '.join(parts)


def parse_view(text):
    """`gh pr view --json headRefOid,statusCheckRollup` の stdout → `(head, checks)`。形が違えば ValueError。"""
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError('not_object')
    head = doc.get('headRefOid')
    if 'statusCheckRollup' not in doc:       # 頼んだ欄が無い = 形が違う (「CI なし」に倒さない)
        raise ValueError('no_rollup')
    rollup = doc['statusCheckRollup']
    if not isinstance(head, str) or not _SHA_RE.fullmatch(head):
        raise ValueError('bad_head')
    if rollup is None:
        rollup = []
    if not isinstance(rollup, list):
        raise ValueError('bad_rollup')
    return head, [classify_check(i) for i in rollup]


def parse_pr_state(text):
    """`gh pr view --json ...,state` の stdout → `OPEN` / `MERGED` / `CLOSED` / None (`state` の欄が無い = 古い stub・版)。
    欄があって知らない値・文字列でない値は ValueError (終端と取り違えない = unknown 側)。`parse_view` と同じ stdout を読む。"""
    doc = json.loads(text)
    if not isinstance(doc, dict) or 'state' not in doc:
        return None
    state = doc['state']
    if not isinstance(state, str) or state not in PR_STATES:
        raise ValueError('bad_pr_state')
    return state


def parse_pr_list(text):
    """`gh pr list --head <b> --state open --json number,isCrossRepository` の stdout → PR 番号 / None (無い)。
    fork から来た同名 branch の PR (`isCrossRepository: true`) は数えない。2 つ以上・形の違いは ValueError
    (どれか決められない = unknown)。"""
    doc = json.loads(text)
    if not isinstance(doc, list):
        raise ValueError('not_list')
    if any(not isinstance(d, dict) or not isinstance(d.get('isCrossRepository'), bool) for d in doc):
        raise ValueError('bad_entry')
    nums = [d.get('number') for d in doc if d['isCrossRepository'] is False]
    if any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in nums):
        raise ValueError('bad_entry')
    if not nums:
        return None
    if len(nums) > 1:
        raise ValueError('ambiguous')
    return nums[0]


def record_problem(data):
    """`load_json_store` の check。書き手も同じ形で書く。欄の有無は `RECORD_FIELDS` / `CHECK_FIELDS` (値が None の欄も
    「有る」こと。無い欄を None とみなして通すと、読み手の `[]` が KeyError になる)。"""
    if not isinstance(data, dict):
        return 'not an object'
    missing = [k for k in RECORD_FIELDS if k not in data]
    if missing:
        return f"missing {missing[0]!r}"
    state = data.get('state')
    if state not in STATES:
        return f"'state' is {state!r}"
    obs = data.get('observed_at')
    if isinstance(obs, bool) or not isinstance(obs, (int, float)) or not math.isfinite(obs):
        return "'observed_at' is not a finite timestamp"
    pr = data.get('pr')
    if pr is not None and (isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0):
        return "'pr' is not a positive integer"
    head = data.get('head')
    if head is not None and (not isinstance(head, str) or not _SHA_RE.fullmatch(head)):
        return "'head' is not a sha"
    checks = data.get('checks')
    if not isinstance(checks, list):
        return "'checks' is not a list"
    for c in checks:
        if (not isinstance(c, dict) or any(k not in c for k in CHECK_FIELDS) or not isinstance(c.get('name'), str)
                or c.get('state') not in ('running', 'passed', 'neutral', 'failed', 'unknown')):
            return "'checks' has a malformed entry"
        for k in ('started_at', 'completed_at', 'conclusion'):
            if c.get(k) is not None and not isinstance(c.get(k), str):
                return f"'checks[].{k}' is not a string"
    if not isinstance(data.get('summary'), str):
        return "'summary' is not a string"
    if 'pr_state' in data and data['pr_state'] not in PR_STATES:
        return f"'pr_state' is {data['pr_state']!r}"
    if 'unknown_streak' in data:
        streak = data['unknown_streak']
        if isinstance(streak, bool) or not isinstance(streak, int) or streak < 0:
            return "'unknown_streak' is not a non-negative integer"
    return None


def _unknown_without_breakdown(record):
    """内訳を出せない unknown (gh の失敗・timeout・形の違い = `reason` つき、または PR / head が無い)。
    gh が成功して check の一部が未知の状態だっただけの unknown は内訳を出す (#314 r3: 「(unknown)」だけにすると
    ✓5 ⏳1 のような分かっている内訳まで捨てる)。"""
    if record.get('state') != 'unknown':
        return False
    return bool(record.get('reason')) or not record.get('pr') or not record.get('head')


def format_line(record, now):
    """表示用の 1 行 (plan.sh status・通知)。`no_pr` は None (出さない)。古ければ「(n 分前)」。"""
    if record is None or record.get('state') == 'no_pr':
        return None
    age = now - record['observed_at']
    if _unknown_without_breakdown(record):
        pr = f"PR #{record['pr']} " if record.get('pr') else ''
        reason = record.get('reason') or 'unknown'
        line = f"CI {pr}unknown ({reason})"
    else:
        line = summarize(record['pr'], record['head'], record['checks'], now=now)
    if age > STALE_AFTER_SECONDS:
        line += f' ({_minutes(age)} 分前)'
    return line


def task_graph_ci(record):
    """tasks.json の `ci` 欄 `{state, summary}`。state は running / passed / failed / unknown (CI 未登録の `none` も
    unknown に畳む = plugin に新しい値を見せない)。経過分を含まない (変わるたびに tasks.json を書き直さない)。`no_pr` は None。"""
    if record is None or record.get('state') == 'no_pr':
        return None
    state = record['state']
    if _unknown_without_breakdown(record):
        return {'state': 'unknown', 'summary': f"CI {('PR #%d ' % record['pr']) if record.get('pr') else ''}unknown"}
    return {'state': 'unknown' if state == 'none' else state,
            'summary': summarize(record['pr'], record['head'], record['checks'])}


def gate_verdict(record, *, pr_head, now, max_age=GATE_MAX_AGE_SECONDS):
    """門 (設計 §2.2)。`(True, 'ok')` か `(False, 理由の固定語)`。**疑わしきは通さない** (fail-closed)。

    通す条件は全部 AND: ①記録が形として読める ②記録の `head` == PR の現 head (`pr_head`。呼び出し元が門を引く瞬間に
    取り直した値) ③`observed_at` が新しい ④全体の state が passed ⑤必須 check (`ci-ok`) が 1 件以上あり全部 passed。
    承認 head との比較 (§2.5) はここに入れない (CI の緑だけを見る関数のまま)。

    理由の固定語: `unknown:<reason>` / `head_mismatch` / `stale` / `running` / `failed` / `none` / `no_required_check`。
    使ってよい場所は「通さない」と「知らせる」だけ。待ちを解く・done にする・Rule 5 / watchdog を黙らせることには使わない。
    共有規則なので env の停止スイッチは付けない (不変条件 5)。"""
    if record is None or record_problem(record) is not None:
        return False, 'unknown:unreadable'
    if not isinstance(pr_head, str) or not _SHA_RE.fullmatch(pr_head):
        return False, 'unknown:no_pr_head'
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        return False, 'unknown:bad_clock'
    state = record['state']
    if state == 'no_pr':
        return False, 'unknown:no_pr'
    if state == 'unknown':
        reason = record.get('reason')
        return False, 'unknown:' + (_clean_name(reason) if isinstance(reason, str) and reason else 'check')
    if record['head'] != pr_head:
        return False, 'head_mismatch'
    age = now - record['observed_at']
    if age < 0 or age > max_age:        # 未来の時刻 (時計のずれ・壊れた記録) も新しいとは言えない
        return False, 'stale'
    if state != 'passed':               # running / failed / none
        return False, state
    required = [c for c in record['checks'] if c['name'] == REQUIRED_CHECK]
    if not required or any(c['state'] != 'passed' for c in required):
        return False, 'no_required_check'
    return True, 'ok'


#: 承認 head を残す review 系 task の skill (設計 §2.5)。PR に紐づく (card の `pr_number` が同じ) これらが比較の対象。
APPROVAL_SKILLS = ('review', 'codex-review', 'qa')
#: 承認 head の理由の固定語 (`gate_verdict` の理由とは別の欄。`plan.sh ci-gate` の 2 行目と `merge-ready` の条件に使う)。
APPROVAL_REASONS = ('ok', 'no_approval', 'approved_head_mismatch')
_APPROVED_HEAD_RE = re.compile(r'approved_head: ([0-9a-f]{40})')


def approved_head(result):
    """review の Result から承認 head を読む**唯一の関数**。末尾の非空行が `approved_head: <40 桁の小文字 sha>` に
    fullmatch するときだけその sha、それ以外は None (本文のどこかにある sha を推測しない・大文字や短縮形は読まない)。"""
    if not isinstance(result, str):
        return None
    lines = [ln.rstrip() for ln in result.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return None
    m = _APPROVED_HEAD_RE.fullmatch(lines[-1])
    return m.group(1) if m else None


def approval_verdict(reviews, *, pr_head):
    """承認 head の比較 (設計 §2.5。`gate_verdict` とは別の層)。`(True, 'ok')` か `(False, 理由の固定語)`。

    `reviews`: PR に紐づく review 系 task (`APPROVAL_SKILLS`) の `{'status','completed_at','result'}` の一覧
    (呼び出し元が集める)。①1 本も無い・1 本でも終端 (`TERMINAL_STATUSES`) でない → `no_approval`。②`completed_at` が
    最大のもの (同時刻が複数なら全部) の Result の末尾行が承認 head でない → `no_approval`。③現 head と違う →
    `approved_head_mismatch`。疑わしきは通さない。使ってよい場所は「知らせる」と「ci-gate の出力」だけ。"""
    if not isinstance(pr_head, str) or not _SHA_RE.fullmatch(pr_head):
        return False, 'unknown:no_pr_head'
    reviews = list(reviews or [])
    if not reviews:
        return False, 'no_approval'
    if any(r.get('status') not in TERMINAL_STATUSES for r in reviews):
        return False, 'no_approval'

    def stamp(r):
        v = r.get('completed_at')
        return v if isinstance(v, str) else ''
    newest = max(stamp(r) for r in reviews)
    heads = {approved_head(r.get('result')) for r in reviews if stamp(r) == newest}
    if None in heads:
        return False, 'no_approval'
    if heads != {pr_head}:
        return False, 'approved_head_mismatch'
    return True, 'ok'


def record_fingerprint(record):
    """tasks.json の再生成が要るかの比較に使う (経過分は含まない)。"""
    return json.dumps(task_graph_ci(record), ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def ci_dir(registry_dir):
    return Path(registry_dir) / 'ci'


def record_path(registry_dir, mission, task_id):
    """記録のパス。mission / task id は card 由来 (外から来る) なので、パスにする前に Git Policy と同じ検査を通す
    (`../x` で registry/ci の外を指させない)。通らなければ ValueError。"""
    try:
        check_mission_slug(mission)
        check_task_id(task_id)
    except GitPolicyError as e:
        raise ValueError(f'bad_record_name:{e.code}') from None
    return ci_dir(registry_dir) / f'{mission}__{task_id}.json'


def card_pr(value):
    """card の `pr_number` を PR 番号 (正の int) / None に。bool・0 以下・int 以外は None (= card は PR を名指ししていない)。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def record_matches_card(record, pr_number):
    """card が PR を名指ししていて、記録がそれと違う PR (PR 無し・不明を含む) を指していれば False。
    #314 r2: 別の PR の CI を、今の PR の状況として出さない (PR を作り直した・card の pr_number が後から付いた)。"""
    want = card_pr(pr_number)
    return want is None or record.get('pr') == want


def read_record(registry_dir, mission, task_id, warn=None, pr_number=None):
    """記録 / None (無い・読めない・壊れている・名前が不正・card の `pr_number` と違う PR。表示は出さないだけ)。
    `pr_number` は読み手が持っている card の値 (無ければ照合しない)。"""
    try:
        path = record_path(registry_dir, mission, task_id)
    except ValueError:
        return None
    data = load_json_store(path, check=record_problem, warn=warn)
    if is_missing(data) or is_unreadable(data):
        return None
    if not record_matches_card(data, pr_number):
        return None
    return data


#: tasks.json にまだ載せていない記録の変化がある印 (#314 r2)。task-graph の再生成に失敗しても、成功するまで毎サイクル
#: 再生成を試みる (記録は先に書かれるので、次のサイクルの `changed` は 0 になり、印が無いと tasks.json が古いまま残る)。
#: 消してよい (消すと、次に記録が変わるまで再生成しないだけ)。名前は `record_path` の検査を通らない形なので記録と衝突しない。
PENDING_NAME = '.task-graph-pending.json'


def _pending_path(registry_dir):
    return ci_dir(registry_dir) / PENDING_NAME


def mark_task_graph_pending(registry_dir, now):
    """書けなければ False (呼び出し元はそのサイクルの再生成をそのまま試みる)。"""
    return write_told_atomic(_pending_path(registry_dir), {'since': now})


def task_graph_pending(registry_dir):
    """印があれば True。読めない・壊れている印も True (再生成は何度走っても害が無い側)。"""
    data = load_json_store(_pending_path(registry_dir))
    return not is_missing(data)


def clear_task_graph_pending(registry_dir):
    """再生成に成功した後だけ呼ぶ。消せなければ False (次のサイクルでもう一度再生成するだけ)。"""
    try:
        _pending_path(registry_dir).unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def write_record(registry_dir, mission, task_id, record):
    """書き手は dispatcher だけ (同時に書く者が居ないので told_lock は要らない)。書けない・名前が不正なら False。"""
    assert record_problem(record) is None, record_problem(record)
    try:
        path = record_path(registry_dir, mission, task_id)
    except ValueError:
        return False
    return write_told_atomic(path, record)


def run_gh(args, *, cwd, timeout, runner=subprocess.run, gh='gh'):
    """gh を呼ぶ。`(stdout, None)` か `(None, 理由の固定語)`。例外は出さない。"""
    if _env_test_isolation_blocks_gh():
        return None, 'gh_blocked_in_tests'
    try:
        p = runner([gh, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                   stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, 'gh_timeout'
    except (OSError, ValueError):
        return None, 'gh_unavailable'
    if p.returncode != 0:
        # gh の stderr は出さない。「その番号の PR が無い」だけを固定語にする (一過性の失敗と区別して終端にするため)
        if _PR_NOT_FOUND_RE.search(getattr(p, 'stderr', None) or ''):
            return None, 'gh_pr_not_found'
        return None, f'gh_exit_{p.returncode}'
    return p.stdout, None


def fetch_pr_head(pr, *, cwd, timeout=GH_TIMEOUT_SECONDS, runner=subprocess.run):
    """門を引く瞬間の PR の現 head。`(sha, None)` か `(None, 理由の固定語)` (取り直せなければ門は通さない)。"""
    out, err = run_gh(['pr', 'view', str(pr), '--json', 'headRefOid'], cwd=cwd, timeout=timeout, runner=runner)
    if err:
        return None, err
    try:
        doc = json.loads(out)
    except ValueError:
        return None, 'bad_json'
    head = doc.get('headRefOid') if isinstance(doc, dict) else None
    if not isinstance(head, str) or not _SHA_RE.fullmatch(head):
        return None, 'bad_head'
    return head, None


def _unknown(pr, reason, now):
    return {'pr': pr, 'head': None, 'state': 'unknown', 'checks': [], 'observed_at': now,
            'summary': f"CI {('PR #%d ' % pr) if pr else ''}unknown", 'reason': reason}


def observe_task(task, *, repo_root, branch_of, now, timeout, runner=subprocess.run, deadline=None,
                 clock=time.monotonic, by_pr=None):
    """1 つの task を観測して記録 (dict) を返す。gh を最大 2 回呼ぶ。

    `task` は `{'mission','id','pr_number','target_dir'}`。`branch_of(task)` は branch 名 / None (決められない)。
    `deadline` (`clock()` の値) を渡すと、gh の**それぞれの**前に残りを計り直し、timeout をそれで切る
    (list と view に同じ timeout を渡すと 1 task で予算の 2 倍まで延びる)。残りが 1 秒未満なら呼ばず unknown。

    `by_pr` (`{(target_dir, pr): 記録}`) を渡すと、PR が決まった時点 (card の `pr_number`、または branch から解決した後)
    でこのサイクルに観測済みの PR なら view を呼ばずにそれを使い、新しく観測した記録はそこに足す
    (実装 task と review task が同じ PR を指す。#314 r2: branch から解決した PR も引く)。"""
    def gh(args):
        t = timeout
        if deadline is not None:
            t = min(t, deadline - clock())
            if t < 1:
                return None, 'cycle_budget'
        return run_gh(args, cwd=cwd, timeout=t, runner=runner)

    cwd = task.get('target_dir') or repo_root
    if task.get('target_dir') and not os.path.isdir(cwd):
        # card の PR を入れる (#314 r3: None で書くと読み手の照合で隠れ、poll_cycle も毎サイクル取り直す)
        return _unknown(card_pr(task.get('pr_number')), 'target_dir_missing', now)
    pr = card_pr(task.get('pr_number'))
    if pr is None:
        branch = branch_of(task)
        if not branch:
            return _unknown(None, 'no_branch', now)
        out, err = gh(['pr', 'list', '--head', branch, '--state', 'open', '--json', 'number,isCrossRepository'])
        if err:
            return _unknown(None, err, now)
        try:
            pr = parse_pr_list(out)
        except ValueError:
            return _unknown(None, 'bad_pr_list', now)
        if pr is None:
            return {'pr': None, 'head': None, 'state': 'no_pr', 'checks': [], 'observed_at': now,
                    'summary': 'no PR'}
    key = (task.get('target_dir') or '', pr)
    if by_pr is not None and key in by_pr:
        return dict(by_pr[key], observed_at=now)
    rec = _view(pr, gh, now)
    if by_pr is not None:
        by_pr[key] = rec
    return rec


def _view(pr, gh, now):
    out, err = gh(['pr', 'view', str(pr), '--json', 'headRefOid,statusCheckRollup,state'])
    if err == 'gh_pr_not_found':
        # card の持ち主の repo を判別できない (別 repo の PR 番号) か、本当に無い PR。誤った PR の CI を通知しない側に倒し、
        # 終端として記録する (unknown にすると毎サイクル取り直し、3 回で ci-unknown を出す)
        return {'pr': pr, 'head': None, 'state': 'no_pr', 'checks': [], 'observed_at': now,
                'summary': 'no PR', 'reason': err, 'pr_state': 'NOT_FOUND'}
    if err:
        return _unknown(pr, err, now)
    try:
        head, checks = parse_view(out)
        pr_state = parse_pr_state(out)
    except ValueError:
        return _unknown(pr, 'bad_json', now)
    rec = {'pr': pr, 'head': head, 'state': overall_state(checks), 'checks': checks, 'observed_at': now,
           'summary': summarize(pr, head, checks)}
    if pr_state is not None:        # 同じ呼び出しで取る (MERGED / CLOSED は poll_cycle が終端として扱う)
        rec['pr_state'] = pr_state
    return rec


def card_is_watched(task):
    """この task の PR を取得の対象にするか。①`CI_WATCH_STATUSES` (PR が card に無くても branch から解決する) か、
    ②`pr_number` を持つ task (status 不問 = done を含む。`CI_FOLLOW_PR_STATUSES`)。PR が MERGED / CLOSED になれば
    記録の `pr_state` で `poll_cycle` が外す (設計 §2.0)。"""
    status = task.get('status')
    if status in POLL_STATUSES:
        return True
    return status in FOLLOW_STATUSES and card_pr(task.get('pr_number')) is not None


def pr_closed(record, pr_number):
    """card が PR を名指ししていて、その PR の記録が MERGED / CLOSED を言っている (= 取得の対象から外す)。"""
    return (record is not None and card_pr(pr_number) is not None and record_matches_card(record, pr_number)
            and record.get('pr_state') in PR_TERMINAL_STATES)


def poll_interval(prev, poll_seconds, concluded_seconds):
    """前の記録から次の取得までの最短の間隔。結論が出た (passed / failed) 記録は長く、それ以外 (running / unknown /
    none / 記録なし) は `poll_seconds`。head が変われば取得の結果が running に戻るので、そこから `poll_seconds`。"""
    if prev is not None and prev.get('state') in ('passed', 'failed'):
        return max(poll_seconds, concluded_seconds)
    return poll_seconds


def poll_cycle(tasks, *, registry_dir, repo_root, branch_of, poll_seconds=DEFAULT_POLL_SECONDS,
               now=None, clock=time.monotonic, runner=subprocess.run, warn=None,
               budget=CYCLE_BUDGET_SECONDS, max_tasks=MAX_TASKS_PER_CYCLE,
               concluded_seconds=CONCLUDED_POLL_SECONDS):
    """dispatcher の 1 サイクル分。間隔が来た task を古い順に、件数と時間の上限まで観測して書く。

    対象は `card_is_watched`。PR が MERGED / CLOSED と記録された task は外す。上限 `max_tasks` は **PR 単位**で数える
    (このサイクルで観測済みの PR を指す task は gh を呼ばないので数えない)。

    戻り値: tasks.json の `ci` 欄が変わった task の数 (呼び出し元が 0 でなければ task-graph を作り直す)。
    gh の失敗・timeout はその task を unknown にして次へ進む (サイクルを止めない)。想定外の例外も task ごとに
    閉じ込め (warn して次の task へ)、他の task の観測を止めない。"""
    now = time.time() if now is None else now
    due = []
    for t in tasks:
        if not card_is_watched(t):
            continue
        try:
            record_path(registry_dir, t.get('mission'), t.get('id'))
        except ValueError as e:
            if warn:
                warn(f'ci: skip task with a bad name ({e})')
            continue
        prev = read_record(registry_dir, t['mission'], t['id'], warn=warn)
        if pr_closed(prev, t.get('pr_number')):
            continue        # 終端 (MERGED / CLOSED): もう取りに行かない。card の pr_number が変われば pr_closed が False になる
        # card の PR と違う PR の記録は間隔を待たずに取り直す (#314 r2。読み手もその記録は出さない)
        if (prev is not None and record_matches_card(prev, t.get('pr_number'))
                and now - prev['observed_at'] < poll_interval(prev, poll_seconds, concluded_seconds)):
            continue
        due.append((prev['observed_at'] if prev else -math.inf, t, prev))
    due.sort(key=lambda x: x[0])
    start = clock()
    changed = 0
    by_pr = {}
    deadline = start + budget
    spent = 0
    for _age, t, prev in due:
        remaining = deadline - clock()
        if remaining < 1:
            break
        pr_key = card_pr(t.get('pr_number'))
        free = pr_key is not None and (t.get('target_dir') or '', pr_key) in by_pr
        if not free:
            if spent >= max_tasks:
                continue        # 件数は PR 単位。この task は次のサイクルへ (同じ PR を観測済みの task はまだ入る)
            spent += 1
        try:
            rec = observe_task(t, repo_root=repo_root, branch_of=branch_of, now=now, timeout=GH_TIMEOUT_SECONDS,
                               runner=runner, deadline=deadline, clock=clock, by_pr=by_pr)
        except Exception as e:  # noqa: BLE001 — 1 つの task の想定外で他の task を止めない
            if warn:
                warn(f"ci: observe {t['mission']}/{t['id']} failed: {type(e).__name__}")
            rec = _unknown(card_pr(t.get('pr_number')), 'internal_error', now)
        rec = dict(rec)
        rec.pop('unknown_streak', None)
        if rec['state'] == 'unknown':       # 連続した回数 (ci-unknown の条件。一過性の失敗で騒がない)
            was = prev is not None and prev.get('state') == 'unknown' and record_matches_card(prev, t.get('pr_number'))
            rec['unknown_streak'] = (prev.get('unknown_streak', 1) if was else 0) + 1
        if not write_record(registry_dir, t['mission'], t['id'], rec):
            if warn:
                warn(f"ci: cannot write {record_path(registry_dir, t['mission'], t['id'])}")
            continue
        if record_fingerprint(prev) != record_fingerprint(rec):
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# 通知の元になる事実 (dispatcher が Director に 1 回だけ送る。設計 §2.1・§2.4・§2.5 の移行中の名前)
# ---------------------------------------------------------------------------

#: 通知の種類 (固定語)。`ci-green` は CI が緑になった事実だけで承認を含意しない。`merge-ready` は緑 + 承認 head == 現 head (§2.5)。
EVENT_KINDS = ('ci-failed', 'ci-green', 'ci-unknown', 'merge-ready')
#: 通知に入れる check 名の最大件数。
EVENT_MAX_CHECKS = 5


def pr_events(tasks, *, registry_dir, now, warn=None, head_of=None, is_sent=None, rotate=0):
    """PR ごとの通知の候補 `(events, closed_prs)`。**gh を呼ばない** (記録を読むだけ)。

    `events`: `{'kind','pr','head','checks','reason'}` の一覧。1 つの PR を指す task が何本あっても 1 つの記録
    (`observed_at` が最も新しいもの) だけを見る。`closed_prs`: MERGED / CLOSED と記録された PR 番号 (台帳の掃除用)。
    `ci-green` の条件は門 (`gate_verdict`) が通ること。ここは**知らせる**ためだけに使い、待ちを解く・done にする・
    Rule 5 / watchdog を黙らせる・割り当てることには使わない (設計 §2.2)。

    `head_of(pr) -> (sha, None) | (None, 理由)`: 緑を知らせる直前に PR の現 head を取り直す関数 (呼び出し元が gh の
    予算の中で用意する。#377 レビュー P3-1)。取り直せなければその PR は今回は何も出さない (次のサイクルで再試行 =
    fail-closed)。省略したときだけ記録自身の head を現 head とみなす (gh を呼ばない単体テスト用)。
    緑のとき、PR に紐づく review 系 task (task の `skills` / `completed_at` / `result`) の承認 head が現 head と
    一致すれば `merge-ready`、しなければ理由 (`no_approval` / `approved_head_mismatch`) つきの `ci-green`。

    `is_sent(key) -> bool` (台帳に既にあるか): 取り直し (gh) は**まだ送っていない候補だけ**にする。記録の head で出すはずの
    通知が既に台帳にあれば head_of を呼ばない (通知済みの PR で毎サイクル gh を呼ばない。#379 レビュー P2-2)。
    `rotate`: 緑の候補を回す開始位置 (呼び出し元が時刻から決める)。head_of の予算が切れる PR がいつも同じにならないように
    する (飢餓を避ける)。review 系 task は `target_dir` が同じものだけを同じ PR の review に数える (ci-gate と同じ束ね方)。"""
    newest = {}
    rec_td = {}
    reviews = {}
    closed = set()
    for t in tasks:
        pr_number = card_pr(t.get('pr_number'))
        if pr_number is None or t.get('status') not in FOLLOW_STATUSES:
            continue
        if set(t.get('skills') or ()) & set(APPROVAL_SKILLS):
            # 収集は read_record より前 (記録の読み取りが落ちた review を数えないと、古い承認で ok に倒れうる。fail-open)
            reviews.setdefault((t.get('target_dir') or None, pr_number), []).append(
                {'status': t.get('status'), 'completed_at': t.get('completed_at'), 'result': t.get('result')})
        try:
            rec = read_record(registry_dir, t['mission'], t['id'], warn=warn, pr_number=pr_number)
        except Exception:  # noqa: BLE001 — 通知の元の読み取りが想定外でも他の PR を止めない
            continue
        if rec is None or rec.get('pr') != pr_number:
            continue
        if rec.get('pr_state') in PR_TERMINAL_STATES:
            closed.add(pr_number)
            continue
        if rec['state'] == 'no_pr':
            continue
        cur = newest.get(pr_number)
        if cur is None or rec['observed_at'] > cur['observed_at']:
            newest[pr_number] = rec
            rec_td[pr_number] = t.get('target_dir') or None
    # 終端 (MERGED / CLOSED / NOT_FOUND) は PR 単位 (t011)。同じ PR を指す別 task の記録が OPEN・passed のまま古く残っていても、
    # どれか 1 つが終端を言ったら通知しない。畳んだ台帳のキーを古い記録で復活させない (merge 直後の ci-green / merge-ready の再送)。
    for pr_number in closed:
        newest.pop(pr_number, None)
    events = []
    keys = sorted(newest)
    if keys and rotate:
        k = rotate % len(keys)
        keys = keys[k:] + keys[:k]      # 緑の候補の取り直しの順を回す (予算切れがいつも同じ PR にならない)
    for pr_number in keys:
        rec = newest[pr_number]
        head, state = rec['head'], rec['state']
        if state == 'failed':
            names = [c['name'] for c in rec['checks'] if c['state'] == 'failed'][:EVENT_MAX_CHECKS]
            events.append({'kind': 'ci-failed', 'pr': pr_number, 'head': head, 'checks': names, 'reason': None})
        elif state == 'unknown':
            if rec.get('unknown_streak', 0) >= UNKNOWN_NOTIFY_STREAK:
                reason = _clean_name(rec['reason']) if isinstance(rec.get('reason'), str) and rec['reason'] else 'check'
                events.append({'kind': 'ci-unknown', 'pr': pr_number, 'head': head, 'checks': [], 'reason': reason})
        elif state == 'passed' and head is not None:
            pr_reviews = reviews.get((rec_td.get(pr_number), pr_number))
            current = head
            if head_of is not None:
                if is_sent is not None:
                    # 記録の head で出すはずの通知が台帳にあれば gh を呼ばない (通知済みの PR で毎サイクル取り直さない)
                    pre_ok, _ = approval_verdict(pr_reviews, pr_head=head)
                    pre = {'kind': 'merge-ready' if pre_ok else 'ci-green', 'pr': pr_number, 'head': head}
                    if is_sent(event_key(pre)):
                        continue
                current, err = head_of(pr_number)
                if err is not None:
                    continue
            ok, _why = gate_verdict(rec, pr_head=current, now=now)
            if not ok:
                continue
            approved, why = approval_verdict(pr_reviews, pr_head=current)
            if approved:
                events.append({'kind': 'merge-ready', 'pr': pr_number, 'head': current, 'checks': [], 'reason': None})
            else:
                events.append({'kind': 'ci-green', 'pr': pr_number, 'head': current, 'checks': [], 'reason': why})
    return events, sorted(closed)


def event_key(event):
    """通知台帳のキー `(pr, head, 種類)`。head が変われば (修正 push) 別の通知。unknown は head が無いことがある。"""
    head = (event.get('head') or 'nohead')[:12]
    return f"ci-{event['kind']}-pr{event['pr']}-{head}"


def event_message(event):
    """Director への 1 通。PR・head・落ちた check 名 (80 字に切って無害化済み) だけを入れる。"""
    pr, head = event['pr'], (event.get('head') or '?')[:12]
    if event['kind'] == 'ci-failed':
        return (f"[ci-failed] PR #{pr} @{head}: CI が失敗しました。落ちた check: " + (', '.join(event['checks']) or '?')
                + "。(通知だけです。修正 task の起票は Director の判断)")
    if event['kind'] == 'ci-green':
        return (f"[ci-green] PR #{pr} @{head}: CI が全部緑です (承認ではありません。理由: {event.get('reason') or 'no_approval'})。"
                "merge は review の所見を読み、plan.sh ci-gate で確かめてから Director が判断してください。")
    if event['kind'] == 'merge-ready':
        return (f"[merge-ready] PR #{pr} @{head}: CI が全部緑で、review の承認 head がこの head と一致しています。"
                "merge は plan.sh ci-gate で確かめてから --match-head-commit で Director が行ってください。")
    return (f"[ci-unknown] PR #{pr} @{head}: CI の状態を {UNKNOWN_NOTIFY_STREAK} 回続けて確かめられません"
            f" (理由: {event.get('reason') or 'check'})。gh の認証・ネットワークを確認してください。")


# --- main の最新 run (nightly / push) ---

MAIN_RUNS_NAME = '.main-runs.json'
#: 結論が「落ちた」と言える run の conclusion。
_RUN_BAD = {'failure', 'timed_out', 'startup_failure', 'cancelled', 'action_required', 'stale'}
#: 見る event (schedule = nightly の全件・push = main への merge)。
MAIN_EVENTS = ('schedule', 'push')
MAIN_RUN_LIMIT = 20


def main_runs_path(registry_dir):
    return ci_dir(registry_dir) / MAIN_RUNS_NAME


def parse_main_runs(text):
    """`gh run list --branch main --json databaseId,headSha,status,conclusion,event,workflowName,createdAt` →
    `{event: {'id','head','conclusion','workflow'}}` (event ごとに**完了済みの最新**1 件)。形が違えば ValueError
    (「落ちていない」に倒さない = 呼び出し元は何も言わない)。進行中 (completed でない) の run は飛ばす。"""
    doc = json.loads(text)
    if not isinstance(doc, list):
        raise ValueError('not_list')
    best = {}
    for d in doc:
        if not isinstance(d, dict):
            raise ValueError('bad_entry')
        event, status, conclusion = d.get('event'), d.get('status'), d.get('conclusion')
        head, rid, created = d.get('headSha'), d.get('databaseId'), d.get('createdAt')
        if not isinstance(event, str) or not isinstance(status, str):
            raise ValueError('bad_entry')
        if event not in MAIN_EVENTS or status != 'completed':
            continue
        if (not isinstance(head, str) or not _SHA_RE.fullmatch(head) or isinstance(rid, bool)
                or not isinstance(rid, int) or not isinstance(conclusion, str) or not _iso(created)):
            raise ValueError('bad_entry')
        cur = best.get(event)
        if cur is None or created > cur['created']:
            best[event] = {'id': rid, 'head': head, 'conclusion': conclusion.lower(), 'created': created,
                           'workflow': _clean_name(d.get('workflowName'))}
    return best


def main_events(runs):
    """`parse_main_runs` の結果 → 通知の候補。落ちた run だけ (`kind='ci-failed'`、PR は無い)。"""
    events = []
    for event in MAIN_EVENTS:
        r = runs.get(event)
        if r is not None and r['conclusion'] in _RUN_BAD:
            events.append({'kind': 'ci-failed', 'pr': None, 'head': r['head'], 'checks': [r['workflow']],
                           'reason': event, 'run': r['id']})
    return events


def main_event_key(event):
    return f"ci-main-{event['reason']}-{event['head'][:12]}"


def main_event_message(event):
    return (f"[ci-failed] main の {event['reason']} run が失敗しました @{event['head'][:12]}"
            f" (workflow: {event['checks'][0]}, run {event.get('run')})。"
            "(通知だけです。原因の調査・修正 task の起票は Director の判断)")


def fetch_main_runs(*, cwd, timeout=GH_TIMEOUT_SECONDS, runner=subprocess.run):
    """main の最新の run。`(runs, None)` か `(None, 理由の固定語)`。"""
    out, err = run_gh(['run', 'list', '--branch', DEFAULT_BASE_BRANCH, '--workflow', 'ci.yml', '--limit', str(MAIN_RUN_LIMIT),
                       '--json', 'databaseId,headSha,status,conclusion,event,workflowName,createdAt'],
                      cwd=cwd, timeout=timeout, runner=runner)
    if err:
        return None, err
    try:
        return parse_main_runs(out), None
    except ValueError:
        return None, 'bad_json'


def main_poll_due(registry_dir, now, interval=MAIN_POLL_SECONDS):
    """前回の問い合わせから `interval` 経っていれば True。印が無い・読めないは True (取りに行く側)。"""
    data = load_json_store(main_runs_path(registry_dir))
    if is_missing(data) or is_unreadable(data) or not isinstance(data, dict):
        return True
    at = data.get('at')
    return isinstance(at, bool) or not isinstance(at, (int, float)) or not (0 <= now - at < interval)


def mark_main_polled(registry_dir, now):
    return write_told_atomic(main_runs_path(registry_dir), {'at': now})
