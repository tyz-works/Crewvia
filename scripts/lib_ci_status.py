#!/usr/bin/env python3
"""scripts/lib_ci_status.py — PR の CI の進み具合を取って記録し、1 行にする (見える化だけ)。

設計: `knowledge/ci-status.md`。

- **取得は dispatcher の 1 か所** (`poll_cycle`)。in_progress / ready_for_verification / verifying の task のうち
  PR があるもの (card の `pr_number`、無ければ Git Policy Resolver の branch から `gh pr list --head`) について
  `gh pr view <N> --json headRefOid,statusCheckRollup` を呼び、`registry/ci/<mission>__<task>.json` に書く。
- **失敗・timeout・形の違う JSON は `unknown`**。緑にも「CI なし」にも倒さない。
- 表示 (plan.sh status・tasks.json・watchdog / Rule 5 の通知) は**記録を読むだけ**で gh を呼ばない。
- 記録は消してよい (無ければ次の poll で取り直す)。書き手は dispatcher だけ。
- **判定 (黙る・殺す) には使わない**。

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
from lib_git_policy import GitPolicyError, check_mission_slug, check_task_id  # noqa: E402
from lib_task_cards import is_missing, is_unreadable  # noqa: E402
from lib_task_status import CI_WATCH_STATUSES  # noqa: E402

#: CI を見る task の status (Worker が PR の CI を待ちうる状態)。定義は lib_task_status の 1 か所。
POLL_STATUSES = CI_WATCH_STATUSES

#: 1 つの task (PR) を問い合わせる最短の間隔 (秒) の既定値。値を持つのは `lib_daemon_watch.WatchConfig.ci_poll_seconds`。
DEFAULT_POLL_SECONDS = 60
#: gh 1 回の上限 (秒)。
GH_TIMEOUT_SECONDS = 15
#: 1 サイクルで gh を呼んでよい時間の合計 (秒)。dispatcher の stale しきい値 (既定 60 秒) より十分小さく。
CYCLE_BUDGET_SECONDS = 20
#: 1 サイクルで問い合わせる task の上限。
MAX_TASKS_PER_CYCLE = 4
#: 記録がこの秒数より古ければ表示に「(n 分前)」を付ける。
STALE_AFTER_SECONDS = 300

#: 記録の state。`none` = gh は答えたが check が 1 つも無い (CI 未登録)。`no_pr` = PR が見つからない (表示しない)。
STATES = ('running', 'passed', 'failed', 'unknown', 'none', 'no_pr')

# 1 つの check の分類 (GitHub の CheckRun.status / conclusion と StatusContext.state)。
_RUN_PASSED = {'SUCCESS'}
_RUN_NEUTRAL = {'NEUTRAL', 'SKIPPED'}
_RUN_FAILED = {'FAILURE', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'STALE'}
_RUN_ACTIVE = {'QUEUED', 'IN_PROGRESS', 'WAITING', 'PENDING', 'REQUESTED'}
_CTX = {'SUCCESS': 'passed', 'FAILURE': 'failed', 'ERROR': 'failed', 'PENDING': 'running', 'EXPECTED': 'running'}

_SHA_RE = re.compile(r'[0-9a-f]{40}')
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
    """`load_json_store` の check。書き手も同じ形で書く。"""
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
        if (not isinstance(c, dict) or not isinstance(c.get('name'), str)
                or c.get('state') not in ('running', 'passed', 'neutral', 'failed', 'unknown')):
            return "'checks' has a malformed entry"
        for k in ('started_at', 'completed_at', 'conclusion'):
            if c.get(k) is not None and not isinstance(c.get(k), str):
                return f"'checks[].{k}' is not a string"
    if not isinstance(data.get('summary'), str):
        return "'summary' is not a string"
    return None


def format_line(record, now):
    """表示用の 1 行 (plan.sh status・通知)。`no_pr` は None (出さない)。古ければ「(n 分前)」。"""
    if record is None or record.get('state') == 'no_pr':
        return None
    age = now - record['observed_at']
    if record['state'] == 'unknown':
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
    if state == 'unknown':
        return {'state': 'unknown', 'summary': f"CI {('PR #%d ' % record['pr']) if record.get('pr') else ''}unknown"}
    return {'state': 'unknown' if state == 'none' else state,
            'summary': summarize(record['pr'], record['head'], record['checks'])}


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


def read_record(registry_dir, mission, task_id, warn=None):
    """記録 / None (無い・読めない・壊れている・名前が不正。表示は出さないだけ)。"""
    try:
        path = record_path(registry_dir, mission, task_id)
    except ValueError:
        return None
    data = load_json_store(path, check=record_problem, warn=warn)
    if is_missing(data) or is_unreadable(data):
        return None
    return data


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
        return None, f'gh_exit_{p.returncode}'
    return p.stdout, None


def _unknown(pr, reason, now):
    return {'pr': pr, 'head': None, 'state': 'unknown', 'checks': [], 'observed_at': now,
            'summary': f"CI {('PR #%d ' % pr) if pr else ''}unknown", 'reason': reason}


def observe_task(task, *, repo_root, branch_of, now, timeout, runner=subprocess.run, deadline=None,
                 clock=time.monotonic):
    """1 つの task を観測して記録 (dict) を返す。gh を最大 2 回呼ぶ。

    `task` は `{'mission','id','pr_number','target_dir'}`。`branch_of(task)` は branch 名 / None (決められない)。
    `deadline` (`clock()` の値) を渡すと、gh の**それぞれの**前に残りを計り直し、timeout をそれで切る
    (list と view に同じ timeout を渡すと 1 task で予算の 2 倍まで延びる)。残りが 1 秒未満なら呼ばず unknown。"""
    def gh(args):
        t = timeout
        if deadline is not None:
            t = min(t, deadline - clock())
            if t < 1:
                return None, 'cycle_budget'
        return run_gh(args, cwd=cwd, timeout=t, runner=runner)

    cwd = task.get('target_dir') or repo_root
    if task.get('target_dir') and not os.path.isdir(cwd):
        return _unknown(None, 'target_dir_missing', now)
    pr = task.get('pr_number')
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        pr = None
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
    out, err = gh(['pr', 'view', str(pr), '--json', 'headRefOid,statusCheckRollup'])
    if err:
        return _unknown(pr, err, now)
    try:
        head, checks = parse_view(out)
    except ValueError:
        return _unknown(pr, 'bad_json', now)
    return {'pr': pr, 'head': head, 'state': overall_state(checks), 'checks': checks, 'observed_at': now,
            'summary': summarize(pr, head, checks)}


def _observe_or_reuse(t, by_pr, *, repo_root, branch_of, now, runner, deadline, clock):
    """同じ PR を同じサイクルで 2 度引かない (実装 task と review task)。"""
    pr = t.get('pr_number')
    key = (t.get('target_dir') or '', pr)
    if isinstance(pr, int) and not isinstance(pr, bool) and key in by_pr:
        return dict(by_pr[key], observed_at=now)
    rec = observe_task(t, repo_root=repo_root, branch_of=branch_of, now=now, timeout=GH_TIMEOUT_SECONDS,
                       runner=runner, deadline=deadline, clock=clock)
    if rec.get('pr'):
        by_pr[(t.get('target_dir') or '', rec['pr'])] = rec
    return rec


def poll_cycle(tasks, *, registry_dir, repo_root, branch_of, poll_seconds=DEFAULT_POLL_SECONDS,
               now=None, clock=time.monotonic, runner=subprocess.run, warn=None,
               budget=CYCLE_BUDGET_SECONDS, max_tasks=MAX_TASKS_PER_CYCLE):
    """dispatcher の 1 サイクル分。間隔が来た task を古い順に、件数と時間の上限まで観測して書く。

    戻り値: tasks.json の `ci` 欄が変わった task の数 (呼び出し元が 0 でなければ task-graph を作り直す)。
    gh の失敗・timeout はその task を unknown にして次へ進む (サイクルを止めない)。想定外の例外も task ごとに
    閉じ込め (warn して次の task へ)、他の task の観測を止めない。"""
    now = time.time() if now is None else now
    due = []
    for t in tasks:
        if t.get('status') not in POLL_STATUSES:
            continue
        try:
            record_path(registry_dir, t.get('mission'), t.get('id'))
        except ValueError as e:
            if warn:
                warn(f'ci: skip task with a bad name ({e})')
            continue
        prev = read_record(registry_dir, t['mission'], t['id'], warn=warn)
        if prev is not None and now - prev['observed_at'] < poll_seconds:
            continue
        due.append((prev['observed_at'] if prev else -math.inf, t, prev))
    due.sort(key=lambda x: x[0])
    start = clock()
    changed = 0
    by_pr = {}
    deadline = start + budget
    for _age, t, prev in due[:max_tasks]:
        remaining = deadline - clock()
        if remaining < 1:
            break
        try:
            rec = _observe_or_reuse(t, by_pr, repo_root=repo_root, branch_of=branch_of, now=now, runner=runner,
                                    deadline=deadline, clock=clock)
        except Exception as e:  # noqa: BLE001 — 1 つの task の想定外で他の task を止めない
            if warn:
                warn(f"ci: observe {t['mission']}/{t['id']} failed: {type(e).__name__}")
            rec = _unknown(None, 'internal_error', now)
        if not write_record(registry_dir, t['mission'], t['id'], rec):
            if warn:
                warn(f"ci: cannot write {record_path(registry_dir, t['mission'], t['id'])}")
            continue
        if record_fingerprint(prev) != record_fingerprint(rec):
            changed += 1
    return changed
