#!/usr/bin/env python3
"""scripts/lib_state_store.py — queue への**書き込み**の唯一の入口 (vNext 01a / S2)。

読み取り専用の `lib_task_cards.py` と対になる。設計は `knowledge/state-store.md` の
§2 (crash モデル) / §3 (この API) / §4 (監査ログ)。

**S2 の時点では呼び出し元ゼロ** (R2)。plan.sh・dispatcher・hooks はまだ import しない
(`tests/test_state_store_has_no_callers_yet.py` が固定。S3 で plan.sh が移るときに外す)。
本番の挙動はこの PR では 1 バイトも変わらない。

## この lib が保証すること

1. **原子的な書き込み** (`atomic_write_text` / `atomic_remove`)
   同じディレクトリの一意な tmp → 全バイト書く → fsync(file) → 既存の mode を引き継ぐ →
   `os.replace` → **親ディレクトリの fsync**。途中で落ちても元のファイルか完全な新ファイルの
   どちらかが読める。書けない = 例外 (`StoreWriteError`)。`None` / `False` / 成功に潰さない。
2. **トランザクション** (`transaction`): `queue/.lock` を取り、取得**後**に必ず読み直す。
   取得できなければ明示的な例外 (`LockBusy` / `LockFailed`) で、1 バイトも書かない。
   **再入しない**: 同じスレッドの入れ子は待たずに `NestedTransaction` (別 fd の 2 回目の
   flock は自分自身を待って止まるので、再入を許す設計は deadlock か「内側の書き込みが
   外側にコミットされたように見える」のどちらかになる)。
3. **projection の再生成と食い違いの検出** (`Txn.recover` / `diagnose`): カード (正本) が
   言っていることに assignment / `.identity` (projection) を合わせる。修復は R-1〜R-4 の
   4 つだけで、**正本 (status / worker / started_at) は書かない**。
4. **監査ログ** (`queue/audit/transitions-YYYYMMDD.jsonl`): Result 本文・理由・description・
   env・token は**書かない**。書けなくても状態遷移は止めない (stderr に警告)。

## 「読めない」を潰さない

読みは `lib_task_cards.read_regular_text_or_unreadable()` (`Unreadable`) だけを通す。
ここでは `Unreadable` を例外 (`StoreReadError`) に写す —— 戻り値の形で「読めない」を空に
潰す経路をこの lib は持たない。

## ロックの中でしないこと

subprocess・ネットワーク・LLM・全 mission の走査 (原案 §14-15)。例外は R-1 の書く**直前**の
「所有の証拠の走査」だけ (`knowledge/state-store.md` §2.5。平常時は 1 回も走らない)。

## 障害注入の口

`FAULT_HOOK` (モジュール変数。env ではない — 不変条件 5) は書き込みの各段で `hook(point, path)` を
呼ぶ。既定は None (何もしない)。テストが子プロセスの中でだけ差し込み、`SIGKILL` を自分に送って
「その点で落ちた」状態を作る (`tests/test_state_store_crash_injection.py`)。
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno as _errno
import fcntl
import json
import os
import re
import stat as _stat
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import lib_dep_rules as _dep_rules  # noqa: E402
import lib_task_cards as _cards  # noqa: E402

# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class StoreError(Exception):
    """この lib が投げる例外の基底。plan.sh 側は die(msg, code) に写す
    (LockBusy → 4、前提外れ → 3、それ以外 → 1。設計 §3.2)。"""


class StoreWriteError(StoreError):
    """書けなかった。`op` は失敗した段 (serialize / stat / mkdir / mkstemp / write / chmod /
    fsync / replace / fsync_dir / unlink / append)、`errno` は OSError 由来のときだけ入る。

    `op == 'fsync_dir'` のときは**新しい内容は既に置き換わっている** (置換の後の失敗)。
    耐久性だけが証明できない。それ以外の op では元のファイルはそのまま残っている。"""

    def __init__(self, path, op, err_no=None, detail=''):
        self.path = str(path)
        self.op = op
        self.errno = err_no
        self.detail = str(detail)
        super().__init__(
            f"{op} failed for {self.path}"
            + (f" (errno {err_no}: {os.strerror(err_no)})" if err_no else "")
            + (f": {self.detail}" if self.detail else ""))


class LockFailed(StoreError):
    """ロックを取れなかった (開けない・flock が想定外の理由で失敗)。何も書いていない。"""


class LockBusy(LockFailed):
    """`nonblocking=True` で他が握っていた。何も書いていない。呼び出し側は次のサイクルで再試行できる。"""


class NestedTransaction(StoreError):
    """同じスレッドで transaction を入れ子にした。待たずに即エラー (設計 §3.2)。"""


class StoreReadError(StoreError):
    """ロックの中で読み直したものが読めない / 形が違う / 名前が不正。`Unreadable` の写し。"""

    def __init__(self, path, reason, err_no=None):
        self.path = str(path)
        self.reason = str(reason)
        self.errno = err_no
        super().__init__(f"cannot read {self.path}: {self.reason}")


class CardUnreadable(StoreReadError):
    """task card が読めない (破損・通常ファイルでない・id がファイル名と食い違う)。"""


class CardNotFound(StoreError):
    """task card が本当に無い (ENOENT)。"""


class InvalidName(StoreError):
    """slug / task id / agent 名がパスとして使えない。書き込みを始める前に投げる。"""


# ---------------------------------------------------------------------------
# 障害注入の口
# ---------------------------------------------------------------------------

#: `hook(point, path)`。既定は None。テストだけが差し込む (env ではない)。
FAULT_HOOK = None


def _fault(point, path=''):
    hook = FAULT_HOOK
    if hook is not None:
        hook(point, str(path))


# システムコールの薄い包み。テストは lib の属性を差し替えて各段の失敗を作る。
def _sys_write(fd, data):
    return os.write(fd, data)


def _sys_fsync(fd):
    os.fsync(fd)


def _sys_replace(src, dst):
    os.replace(src, dst)


def _sys_unlink(path):
    os.unlink(path)


# ---------------------------------------------------------------------------
# 原子的な書き込み
# ---------------------------------------------------------------------------

#: 親ディレクトリの fsync がこの errno を返すファイルシステムだけ黙って続行する。
#: **それ以外の OSError は raise** (「できなかった」を「した」にしない)。
_DIR_FSYNC_UNSUPPORTED = frozenset({_errno.EINVAL, _errno.ENOTSUP, _errno.EOPNOTSUPP})


def _fsync_dir(dirpath, error_path):
    """`dirpath` の fsync。失敗は `StoreWriteError(op='fsync_dir')`。"""
    try:
        fd = os.open(dirpath, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError as e:
        raise StoreWriteError(error_path, 'fsync_dir', e.errno, f'open {dirpath}: {e.strerror}') from e
    try:
        try:
            _sys_fsync(fd)
        except OSError as e:
            if e.errno in _DIR_FSYNC_UNSUPPORTED:
                return
            raise StoreWriteError(error_path, 'fsync_dir', e.errno, dirpath) from e
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _ensure_dir(dirpath, error_path):
    """`dirpath` を (無ければ祖先ごと) 作る。作った各ディレクトリの**親**を fsync する。"""
    dirpath = os.fspath(dirpath) or '.'
    if os.path.isdir(dirpath):
        return
    parent = os.path.dirname(os.path.abspath(dirpath))
    if parent and parent != dirpath:
        _ensure_dir(parent, error_path)
    try:
        os.mkdir(dirpath)
    except FileExistsError:
        if not os.path.isdir(dirpath):
            raise StoreWriteError(error_path, 'mkdir', _errno.EEXIST, f'{dirpath} is not a directory')
        return
    except OSError as e:
        raise StoreWriteError(error_path, 'mkdir', e.errno, dirpath) from e
    _fsync_dir(parent, error_path)


def _write_all(fd, data, path):
    view = memoryview(data)
    while view:
        try:
            n = _sys_write(fd, view)
        except OSError as e:
            raise StoreWriteError(path, 'write', e.errno) from e
        if n <= 0:
            raise StoreWriteError(path, 'write', _errno.EIO, 'short write returned 0 bytes')
        view = view[n:]


def atomic_write_text(path, text, *, mode=None):
    """`path` に `text` を原子的に書く。ロックとは独立 (ロック外の書き手もこれを使う)。

    tmp (`.<name>.tmp.<random>`) → 全バイト → fsync(file) → mode → `os.replace` → fsync(親 dir)。
    mode は既存ファイルのものを引き継ぐ (無ければ `mode`、それも無ければ 0o644)。mkstemp は
    0o600 で作るので、引き継がないと Worker / デーモン / hooks の間で読めなくなる。
    tmp の先頭が `.` なのは `tNNN.md` の列挙 (`TASK_FILENAME_RE`) に絶対に当たらないため。

    失敗は `StoreWriteError`。置換の前の失敗では tmp を消し、元のファイルは変わらない。
    """
    path = os.fspath(path)
    try:
        data = text.encode('utf-8')
    except (UnicodeError, AttributeError) as e:
        raise StoreWriteError(path, 'serialize', None, f'{type(e).__name__}: {e}') from e

    parent = os.path.dirname(path) or '.'
    name = os.path.basename(path)
    _fault('atomic:begin', path)
    _ensure_dir(parent, path)

    try:
        final_mode = _stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        final_mode = 0o644 if mode is None else mode
    except OSError as e:
        raise StoreWriteError(path, 'stat', e.errno) from e

    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=f'.{name}.tmp.')
    except OSError as e:
        raise StoreWriteError(path, 'mkstemp', e.errno, parent) from e

    replaced = False
    try:
        try:
            _fault('atomic:tmp_created', tmp)
            _write_all(fd, data, path)
            _fault('atomic:written', tmp)
            try:
                os.fchmod(fd, final_mode)
            except OSError as e:
                raise StoreWriteError(path, 'chmod', e.errno) from e
            try:
                _sys_fsync(fd)
            except OSError as e:
                raise StoreWriteError(path, 'fsync', e.errno) from e
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        _fault('atomic:synced', tmp)
        try:
            _sys_replace(tmp, path)
        except OSError as e:
            raise StoreWriteError(path, 'replace', e.errno) from e
        replaced = True
        _fault('atomic:replaced', path)
        _fsync_dir(parent, path)
        _fault('atomic:dir_synced', path)
    except BaseException:
        if not replaced:
            try:
                _sys_unlink(tmp)
            except OSError:
                pass
        raise


def atomic_remove(path):
    """`path` を消し、親ディレクトリを fsync する。無かった (ENOENT) なら False。
    それ以外の失敗は `StoreWriteError`。"""
    path = os.fspath(path)
    _fault('remove:begin', path)
    try:
        _sys_unlink(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        raise StoreWriteError(path, 'unlink', e.errno) from e
    _fault('remove:unlinked', path)
    _fsync_dir(os.path.dirname(path) or '.', path)
    _fault('remove:dir_synced', path)
    return True


# ---------------------------------------------------------------------------
# 直列化 (今の plan.sh の関数と**バイト単位で同じ**。S3 で plan.sh がこちらを使う)
# ---------------------------------------------------------------------------
#
# この節は plan.sh:456-565 (dump_yaml / serialize_frontmatter) と同じ規則の写しで、
# S3 までの間だけ二重になる。`tests/test_state_store_serialization_matches_plan_sh.py` が
# plan.sh の関数を AST で取り出して出力の一致を固定する (ずれたら赤)。

TASK_META_KEY_ORDER = [
    'id', 'title', 'skills', 'priority', 'status',
    'blocked_by', 'released_deps', 'timeout', 'target_dir', 'worker', 'started_at', 'completed_at',
    'handoff_path', 'fail_head', 'fail_head_waiver', 'pr_number', 'no_pr_waiver', 'deliverable',
    'acceptance_criteria', 'verification', 'rework_count', 'max_rework',
    'qa_checkpoints', 'required_evidence', 'needs_director_reason',
]

MISSION_KEY_ORDER = ['title', 'slug', 'status', 'created_at', 'completed_at', 'next_task_id',
                     'max_review_cycles', 'deliverable_required', 'review']


def dump_yaml(data, key_order=None):
    lines = []
    # plan.sh の同名関数は渡された key_order を書き換える (未知のキーを追記する)。1 回で終わる CLI では
    # 無害だが、この lib は長く生きるプロセスからも呼ばれるので、コピーして表を汚さない。
    # 出力は同じバイト (1 回目の呼び出しの結果は変わらない)。
    keys = list(key_order) if key_order else list(data.keys())
    if key_order:
        for k in data.keys():
            if k not in keys:
                keys.append(k)
    for k in keys:
        if k not in data:
            continue
        lines.append(_dump_kv(k, data[k]))
    return '\n'.join(lines) + '\n'


def _dump_kv(key, val):
    if val is None:
        return f"{key}: null"
    if isinstance(val, bool):
        return f"{key}: {'true' if val else 'false'}"
    if isinstance(val, int):
        return f"{key}: {val}"
    if isinstance(val, dict):
        lines = [f"{key}:"]
        for k, v in val.items():
            lines.append(f"  {k}: {_dump_inline(v)}")
        return '\n'.join(lines)
    if isinstance(val, list):
        if not val:
            return f"{key}: []"
        items = ', '.join(_dump_inline(x) for x in val)
        return f"{key}: [{items}]"
    return f"{key}: {_dump_scalar(str(val))}"


_NEEDS_QUOTE = set(':#[]{},\'"\n&*!|>%@`')


def _dump_scalar(s):
    if s == '':
        return '""'
    if '\n' in s or '\r' in s:
        # frontmatter の値は 1 行でなければ読み戻せない (plan.sh の同名関数と同じ理由)。
        s = re.sub(r'(?:\r\n|\r|\n)+$', '', s)
        s = re.sub(r'\r\n|\r|\n', ' / ', s)
    if any(ch in _NEEDS_QUOTE for ch in s):
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if s.lower() in ('true', 'false', 'null', 'yes', 'no', '~'):
        return f'"{s}"'
    if re.fullmatch(r'-?\d+', s):
        return f'"{s}"'
    return s


def _dump_inline(val):
    if val is None:
        return 'null'
    if isinstance(val, bool):
        return 'true' if val else 'false'
    if isinstance(val, int):
        return str(val)
    return _dump_scalar(str(val))


def serialize_card(meta, body):
    yaml_text = dump_yaml(meta, key_order=TASK_META_KEY_ORDER)
    if not body.endswith('\n'):
        body = body + '\n'
    return f"---\n{yaml_text}---\n\n{body}"


def serialize_mission(data):
    return dump_yaml(data, key_order=MISSION_KEY_ORDER)


def serialize_state(state):
    active = state.get('active_missions', []) or []
    lines = []
    if active:
        lines.append('active_missions:')
        for slug in active:
            lines.append(f"  - {_dump_inline(slug)}")
    else:
        lines.append('active_missions: []')
    lines.append(_dump_kv('default_mission', state.get('default_mission')))
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# 名前・パス
# ---------------------------------------------------------------------------

IDENTITY_SUFFIX = '.identity'
#: assignments ディレクトリで別の意味を持つ suffix。Worker 名として使わせない。
RESERVED_AGENT_SUFFIXES = (IDENTITY_SUFFIX, '.restarting', '.tmp')

# classify_assignment() の判定 (plan.sh の ASSIGN_* と同じ語彙)。撤去してよいのは MINE だけ。
ASSIGN_MINE = 'mine'
ASSIGN_ABSENT = 'absent'
ASSIGN_OTHER_TASK = 'other_task'
ASSIGN_SUCCESSOR = 'successor'
ASSIGN_UNVERIFIABLE = 'unverifiable'

#: 状態の語彙。S1 (t004) が `lib_task_status.py` に 1 か所へ寄せる — そのとき
#: この 3 つをそちらの import に置き換える (backlog: 語彙のコピー)。
#: HELD / DEAD は依存の意味なので lib_dep_rules から取る (コピーしない。不変条件 3)。
_TERMINAL_STATUSES = frozenset({'done', 'verified', 'skipped'})
_ASSIGNMENT_HOLDING_STATUSES = frozenset(
    {'in_progress', 'ready_for_verification', 'verifying', 'needs_human_review'})
_NEEDS_DIRECTOR = 'needs_director'
#: 今の reap-orphan-assignment (plan.sh:908) の判定と同じ式。
ORPHAN_ASSIGNMENT_FINISHED_STATUSES = (
    _TERMINAL_STATUSES
    | frozenset(_dep_rules.DEAD_DEP_STATUSES)
    | frozenset(_dep_rules.HELD_DEP_STATUSES))


def agent_name_problem(agent):
    """Worker 名が assignment ファイル名として使えない理由。使えるなら None。"""
    if not isinstance(agent, str) or not agent or '/' in agent or '\0' in agent \
            or agent in ('.', '..') or agent.startswith('.'):
        return "'/' や先頭の '.' を含まない名前にしてください"
    for suffix in RESERVED_AGENT_SUFFIXES:
        if agent.endswith(suffix):
            return f"'{suffix}' で終わる名前は queue/assignments/ で予約済みです"
    return None


def _check_slug(slug):
    if not isinstance(slug, str) or not slug or '/' in slug or '\0' in slug \
            or slug in ('.', '..') or slug.startswith('.'):
        raise InvalidName(f"invalid mission slug {slug!r}")
    return slug


def _check_tid(tid):
    if not isinstance(tid, str) or not _cards.TASK_ID_RE.fullmatch(tid):
        raise InvalidName(f"invalid task id {tid!r} (tNNN の形だけ)")
    return tid


def _check_agent(agent):
    problem = agent_name_problem(agent)
    if problem:
        raise InvalidName(f"invalid agent name {agent!r}: {problem}")
    return agent


# ---------------------------------------------------------------------------
# 監査ログ
# ---------------------------------------------------------------------------

AUDIT_DIRNAME = 'audit'


def audit_path(queue_dir, now=None):
    now = now or datetime.now(timezone.utc)
    return os.path.join(queue_dir, AUDIT_DIRNAME, f"transitions-{now.strftime('%Y%m%d')}.jsonl")


def _warn(msg):
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# トランザクション
# ---------------------------------------------------------------------------

#: 「このスレッドが今どのロックを握っているか」。入れ替え防止の入れ子検出用。
_HELD = {}
_HELD_GUARD = threading.Lock()


def _lock_key(lock_path):
    try:
        st = os.stat(lock_path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return ('path', os.path.realpath(lock_path))


@contextlib.contextmanager
def transaction(queue_dir, *, op, actor, nonblocking=False):
    """`queue/.lock` を取って `Txn` を渡す。with を抜けたら (例外なしのとき) コマンド本体の
    監査ログを追記してから unlock する。

    - 取得できなければ `LockBusy` (nonblocking) / `LockFailed`。**何も書かない**。
    - 同じスレッドの入れ子は `NestedTransaction` (待たない)。
    - 例外・`SystemExit` でトランザクションを抜けたときは本体の行を書かない
      (回復の行は `Txn.recover()` の中で即時に書き終えている。設計 §2.3 末尾 / §4)。
    """
    queue_dir = os.fspath(queue_dir)
    lock_path = os.path.join(queue_dir, '.lock')
    _ensure_dir(queue_dir, lock_path)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        raise LockFailed(f"cannot open queue lock {lock_path}: {e}") from e
    key = _lock_key(lock_path)
    me = threading.get_ident()
    try:
        with _HELD_GUARD:
            if _HELD.get(key) == me:
                raise NestedTransaction(
                    f"transaction is already open in this thread ({lock_path}); "
                    f"入れ子は待たずにエラーにします (再入しない)")
        try:
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            fcntl.flock(fd, flags)
        except OSError as e:
            if nonblocking and e.errno in (_errno.EWOULDBLOCK, _errno.EAGAIN):
                raise LockBusy(f"queue lock {lock_path} is held by another process") from e
            raise LockFailed(f"cannot lock {lock_path}: {e}") from e
        with _HELD_GUARD:
            _HELD[key] = me
        try:
            txn = Txn(queue_dir, op=op, actor=actor)
            yield txn
            txn._flush_body_records()
        finally:
            with _HELD_GUARD:
                if _HELD.get(key) == me:
                    del _HELD[key]
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@dataclasses.dataclass
class Repair:
    """回復が 1 件したこと。`result` は `repaired:R-n` か `reported:<理由コード>`。"""
    result: str
    mission: str | None = None
    task: str | None = None
    agent: str | None = None
    detail: str | None = None
    files: tuple = ()

    @property
    def repaired(self):
        return self.result.startswith('repaired:')

    @property
    def rule(self):
        return self.result.split(':', 1)[1] if self.repaired else None


@dataclasses.dataclass(frozen=True)
class Scope:
    """回復が見る範囲 (設計 §2.5)。範囲外は読まない。

    cards: 名指しの (slug, tid)。agents: 呼び出し元の Worker 名。
    add_missions: R-3 を見る mission。archive_slugs: R-4 を見る slug。"""
    cards: tuple = ()
    agents: tuple = ()
    add_missions: tuple = ()
    archive_slugs: tuple = ()

    @staticmethod
    def everything(queue_dir):
        """diagnose (store-check) 用: queue 全体。"""
        queue_dir = os.fspath(queue_dir)
        cards, missions = [], []
        for slug in _list_dir(os.path.join(queue_dir, 'missions')):
            missions.append(slug)
            for name in _list_dir(os.path.join(queue_dir, 'missions', slug, 'tasks')):
                m = _cards.TASK_FILENAME_RE.fullmatch(name)
                if m:
                    cards.append((slug, f"t{m.group(1)}"))
        agents = [a for a in _list_dir(os.path.join(queue_dir, 'assignments'))
                  if agent_name_problem(a) is None]
        state = _read_state_lenient(queue_dir)
        return Scope(cards=tuple(cards), agents=tuple(agents), add_missions=tuple(missions),
                     archive_slugs=tuple(state))


def _list_dir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _read_state_lenient(queue_dir):
    text = _cards.read_regular_text_or_unreadable(os.path.join(queue_dir, 'state.yaml'))
    if _cards.is_unreadable(text):
        return []
    try:
        return list(_cards.parse_yaml(text).get('active_missions') or [])
    except ValueError:
        return []


class Txn:
    """`transaction()` が渡す。**書き込みはステージしない** — 呼び出し側が書いた順に、その場で
    atomic に書く (設計 §3.2。順序の規則そのものが crash モデルなので、書く順を保つ)。"""

    def __init__(self, queue_dir, *, op, actor):
        self.queue_dir = os.fspath(queue_dir)
        self.op = op
        self.actor = actor or 'unknown'
        self.txn_id = uuid.uuid4().hex
        self.audit_failures = 0
        self._records = []
        self._files = []

    # ---- パス --------------------------------------------------------------
    def _p(self, *parts):
        return os.path.join(self.queue_dir, *parts)

    def card_path(self, slug, tid):
        return self._p('missions', _check_slug(slug), 'tasks', f"{_check_tid(tid)}.md")

    def mission_path(self, slug):
        return self._p('missions', _check_slug(slug), 'mission.yaml')

    @property
    def state_path(self):
        return self._p('state.yaml')

    def assignment_path(self, agent):
        return self._p('assignments', _check_agent(agent))

    def identity_path(self, agent):
        return self.assignment_path(agent) + IDENTITY_SUFFIX

    def _rel(self, path):
        return os.path.relpath(path, self.queue_dir)

    # ---- 読み (ロックの中で読み直す。取得前に読んだ内容を根拠にしない) --------
    def load_card(self, slug, tid):
        """`(meta, body)`。無ければ `CardNotFound`、読めなければ `CardUnreadable`。"""
        path = self.card_path(slug, tid)
        kind, meta, body, reason, err_no = self._read_card(path, tid)
        if kind == 'missing':
            raise CardNotFound(f"task '{tid}' not found in mission '{slug}' ({path})")
        if kind == 'unreadable':
            raise CardUnreadable(path, reason, err_no)
        return meta, body

    def _read_card(self, path, tid):
        """`(kind, meta, body, reason, errno)`。kind = ok / missing / unreadable。"""
        text = _cards.read_regular_text_or_unreadable(path)
        if _cards.is_missing(text):
            return 'missing', None, None, None, text.errno
        if _cards.is_unreadable(text):
            return 'unreadable', None, None, text.reason, text.errno
        try:
            meta, body = _cards.parse_frontmatter(text, source=path)
        except ValueError as e:
            return 'unreadable', None, None, f'parse error: {e}', None
        if meta.get('id') not in (None, '', tid):
            return ('unreadable', None, None,
                    f"id 欄 {meta.get('id')!r} がファイル名 {tid} と食い違う (識別子はファイル名)", None)
        return 'ok', meta, body, None, None

    def load_mission(self, slug):
        path = self.mission_path(slug)
        text = _cards.read_regular_text_or_unreadable(path)
        if _cards.is_unreadable(text):
            raise StoreReadError(path, text.reason, text.errno)
        try:
            return _cards.parse_yaml(text, source=path)
        except ValueError as e:
            raise StoreReadError(path, f'parse error: {e}') from e

    def load_state(self):
        """state.yaml。**ENOENT だけ**「まだ無い」= 空の既定値。それ以外の読めない・壊れているは例外。"""
        path = self.state_path
        text = _cards.read_regular_text_or_unreadable(path)
        if _cards.is_missing(text):
            return {'active_missions': [], 'default_mission': None}
        if _cards.is_unreadable(text):
            raise StoreReadError(path, text.reason, text.errno)
        try:
            data = _cards.parse_yaml(text, source=path)
        except ValueError as e:
            raise StoreReadError(path, f'parse error: {e}') from e
        if data.get('active_missions') is None:
            data['active_missions'] = []
        data.setdefault('default_mission', None)
        return data

    # ---- 書き --------------------------------------------------------------
    def _write(self, path, text):
        atomic_write_text(path, text)
        self._files.append(self._rel(path))

    def _remove(self, path):
        removed = atomic_remove(path)
        if removed:
            self._files.append(self._rel(path))
        return removed

    def write_card(self, slug, tid, meta, body):
        if meta.get('id') not in (None, '', tid):
            raise InvalidName(f"meta['id']={meta.get('id')!r} が task id {tid} と食い違う (識別子はファイル名)")
        self._write(self.card_path(slug, tid), serialize_card(meta, body))

    def write_mission(self, slug, data):
        self._write(self.mission_path(slug), serialize_mission(data))

    def write_state(self, state):
        self._write(self.state_path, serialize_state(state))

    # ---- assignment (projection) ------------------------------------------
    def publish_assignment(self, agent, slug, tid, generation):
        """identity → 本体の順 (本体 = 「存在 = busy」が世代不明で観測されないように)。"""
        _check_agent(agent), _check_slug(slug), _check_tid(tid)
        self._write(self.identity_path(agent), json.dumps({
            'mission': slug, 'task': tid, 'worker': agent, 'started_at': generation,
        }, ensure_ascii=False, sort_keys=True) + '\n')
        self._write(self.assignment_path(agent), f"{slug}:{tid}\n")

    def _read_slot(self, agent):
        """`('absent',)` / `('unverifiable', reason)` / `('ok', '<slug>:<tid>')`。
        分けるのは ENOENT の 1 点だけ (`Path.exists()` は EACCES も False に潰す)。"""
        text = _cards.read_regular_text_or_unreadable(self.assignment_path(agent))
        if _cards.is_missing(text):
            return ('absent',)
        if _cards.is_unreadable(text):
            return ('unverifiable', text.reason)
        return ('ok', text.strip())

    def _read_identity(self, agent):
        text = _cards.read_regular_text_or_unreadable(self.identity_path(agent))
        if _cards.is_unreadable(text):
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def classify_assignment(self, agent, slug, tid, generation):
        """公開中の assignment が「この実行のもの」かの判定 (plan.sh classify_assignment と同じ結論)。
        generation=None は「いま card が示している実行」(同じロックの中で card を読んだ直後)。"""
        if agent_name_problem(agent):
            return ASSIGN_UNVERIFIABLE
        slot = self._read_slot(agent)
        if slot[0] == 'absent':
            return ASSIGN_ABSENT
        if slot[0] == 'unverifiable':
            return ASSIGN_UNVERIFIABLE
        if slot[1] != f"{slug}:{tid}":
            return ASSIGN_OTHER_TASK
        if generation is None:
            return ASSIGN_MINE
        identity = self._read_identity(agent)
        if not identity:
            return ASSIGN_UNVERIFIABLE
        if identity.get('mission') != slug or identity.get('task') != tid:
            return ASSIGN_UNVERIFIABLE
        recorded = identity.get('started_at')
        if recorded is None or str(recorded) != str(generation):
            return ASSIGN_SUCCESSOR
        return ASSIGN_MINE

    def retire_assignment(self, agent, slug, tid, generation):
        """撤去する唯一の入口。「この実行のもの」と確定したときだけ消し、判定を返す。
        消せなかったら `StoreWriteError` (plan.sh の warn で握り潰す型を持たない)。"""
        verdict = self.classify_assignment(agent, slug, tid, generation)
        if verdict != ASSIGN_MINE:
            return verdict
        self._remove(self.assignment_path(agent))
        self._remove(self.identity_path(agent))
        return ASSIGN_MINE

    # ---- 監査ログ -----------------------------------------------------------
    def record(self, mission, task, from_status, to_status, generation=None, detail=None):
        """コマンド本体の監査ログの 1 行を予約する (with が例外なしで抜けたときに書く)。"""
        self._records.append(dict(
            op=self.op, mission=mission, task=task, from_status=from_status,
            to_status=to_status, generation=generation, detail=detail, result='ok'))

    def _flush_body_records(self):
        files = tuple(self._files)
        for rec in self._records:
            self._append_audit(rec, files)
        self._records = []

    def _append_audit(self, rec, files):
        """1 行追記。失敗しても状態遷移は止めない (stderr に警告 + `audit_failures`)。"""
        row = {
            'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'txn_id': self.txn_id,
            'op': rec.get('op', self.op),
            'mission': rec.get('mission'),
            'task': rec.get('task'),
            'actor': self.actor,
            'pid': os.getpid(),
            'from_status': rec.get('from_status'),
            'to_status': rec.get('to_status'),
            'generation': rec.get('generation'),
            'execution_id': None,        # 01c が埋める。01a では null 固定
            'result': rec.get('result', 'ok'),
            'files': list(files),
        }
        if rec.get('detail'):
            row['detail'] = str(rec['detail'])[:200]
        path = audit_path(self.queue_dir)
        try:
            _fault('audit:begin', path)
            _ensure_dir(os.path.dirname(path), path)
            line = (json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                _write_all(fd, line, path)
                _sys_fsync(fd)
            finally:
                os.close(fd)
            _fault('audit:appended', path)
        except (OSError, StoreWriteError) as e:
            self.audit_failures += 1
            err_no = getattr(e, 'errno', None)
            _warn(f"[state-store warn] audit log を書けませんでした ({path}: {err_no})")

    # ---- 回復 ---------------------------------------------------------------
    def recover(self, scope):
        """§2.3 の R-1〜R-4 を `scope` の中だけで行う。**コマンドの前提検査より前**に呼ぶ。

        - 正本 (status / worker / started_at) は書かない。
        - 修復・報告 1 件ごとに `op=recover` の行を**その場で**追記する (with の出口を待たない)。
        - 「表に無い食い違い」は書かずに報告する (`reported:<コード>`)。raise しない・拒否を足さない。
        - 冪等: 2 回目は修復が空になる (報告は状態が続く限り毎回出る)。
        """
        return _Recovery(self, scope, apply=True).run()


# ---------------------------------------------------------------------------
# 回復 (R-1〜R-4) — apply=False のときは何も書かず、書くはずだったものを返す (diagnose)
# ---------------------------------------------------------------------------

class _Recovery:
    def __init__(self, txn, scope, apply):
        self.t = txn
        self.scope = scope
        self.apply = apply
        self.out = []
        self._seen = set()
        self._cleared = set()      # dry-run: R-2 で消す予定の枠は、その後 ABSENT として扱う
        self._cards = {}           # (slug, tid) -> _read_card の結果 (この 1 回の回復の中だけ)

    # -- 出力 -----------------------------------------------------------------
    def _emit(self, result, mission=None, task=None, agent=None, detail=None, files=(),
              from_status=None, to_status=None, generation=None):
        key = (result, mission, task, agent)
        if key in self._seen:
            return
        self._seen.add(key)
        rep = Repair(result=result, mission=mission, task=task, agent=agent,
                     detail=detail, files=tuple(files))
        self.out.append(rep)
        if not self.apply:
            return
        if not rep.repaired:
            _warn(f"[state-store] reported: {result} mission={mission} task={task} "
                  f"agent={agent}" + (f" ({detail})" if detail else ""))
        self.t._append_audit(dict(
            op='recover', mission=mission, task=task, result=result, detail=detail,
            from_status=from_status, to_status=to_status, generation=generation), files)

    def _do_writes(self, fn):
        """書き込みを行い、そこで書いたファイルの相対パスを返す。dry-run では何もしない。"""
        if not self.apply:
            return ()
        start = len(self.t._files)
        fn()
        return tuple(self.t._files[start:])

    def _card(self, slug, tid):
        key = (slug, tid)
        if key not in self._cards:
            try:
                path = self.t.card_path(slug, tid)
            except InvalidName as e:
                self._cards[key] = ('unreadable', None, None, str(e), None)
            else:
                self._cards[key] = self.t._read_card(path, tid)
        return self._cards[key]

    def _slot(self, agent):
        if agent in self._cleared:
            return ('absent',)
        return self.t._read_slot(agent)

    # -- 本体 -----------------------------------------------------------------
    def run(self):
        s = self.scope
        named = []
        for slug, tid in s.cards:
            kind, meta, _b, reason, _e = self._card(slug, tid)
            if kind == 'unreadable':
                self._emit('reported:card_unreadable', slug, tid, detail=reason)
            elif kind == 'ok':
                named.append((slug, tid, meta))

        # R-2 の対象の枠: 呼び出し元 + 名指しの card の worker + 逆引き (card を指す枠)
        agents = [a for a in s.agents if agent_name_problem(a) is None]
        for slug, tid, meta in named:
            w = meta.get('worker')
            if isinstance(w, str) and w and agent_name_problem(w) is None:
                agents.append(w)
        if named:
            wanted = {f"{slug}:{tid}" for slug, tid, _m in named}
            agents.extend(self._reverse_lookup(wanted))
        for agent in sorted(dict.fromkeys(agents)):
            self._r2(agent)

        for slug, tid, meta in named:
            self._r1(slug, tid, meta)
        for slug in dict.fromkeys(s.add_missions):
            self._r3(slug)
        for slug in dict.fromkeys(s.archive_slugs):
            self._r4(slug)
        return self.out

    def _reverse_lookup(self, wanted):
        """`assignments/` の本体ファイル (`.identity` / `.restarting` / `.tmp` を除く) のうち、
        本文が wanted のどれかに一致する枠の名前。読めない枠は「指していない」と証明できないが、
        消さない・止めない (store-check が別に出す)。"""
        found = []
        for name in _list_dir(self.t._p('assignments')):
            if agent_name_problem(name) is not None:
                continue
            slot = self.t._read_slot(name)
            if slot[0] == 'ok' and slot[1] in wanted:
                found.append(name)
        return found

    # -- R-2: 孤児 projection を消す ----------------------------------------------
    def _r2(self, agent):
        slot = self._slot(agent)
        if slot[0] != 'ok':
            return
        pslug, sep, ptid = slot[1].rpartition(':')
        if not sep or not _cards.TASK_ID_RE.fullmatch(ptid):
            self._emit('reported:assignment_malformed', agent=agent, detail=slot[1][:80])
            return
        kind, meta, _b, reason, _e = self._card(pslug, ptid)
        if kind == 'missing':
            self._emit('reported:assignment_target_missing', pslug, ptid, agent)
            return
        if kind == 'unreadable':
            self._emit('reported:assignment_target_unreadable', pslug, ptid, agent, detail=reason)
            return
        status = meta.get('status')
        worker = meta.get('worker') or None
        if status in ORPHAN_ASSIGNMENT_FINISHED_STATUSES or status == _NEEDS_DIRECTOR \
                or (status == 'pending' and worker is None):
            # 撤去するコマンド自身が書く status だけ (「途中で落ちた」以外に説明が無い組)。
            def _retire():
                self.t.retire_assignment(agent, pslug, ptid, None)
            files = self._do_writes(_retire)
            self._cleared.add(agent)
            self._emit('repaired:R-2', pslug, ptid, agent, files=files,
                       from_status=status, to_status=status)
            return
        if worker == agent and status in _ASSIGNMENT_HOLDING_STATUSES:
            return                                  # 正常 (この Worker が持っている)
        if status == 'in_progress':
            self._emit('reported:assignment_owner_mismatch', pslug, ptid, agent,
                       detail=f"card worker={worker}")
            return
        # blocked / verification_failed 等: Worker は動いており assignment は残るのが正当
        self._emit('reported:assignment_on_non_orphan_status', pslug, ptid, agent,
                   detail=f"status={status}")

    # -- R-1: projection を作る ---------------------------------------------------
    def _r1(self, slug, tid, meta):
        status = meta.get('status')
        worker = meta.get('worker')
        gen = meta.get('started_at')
        holding = status in _ASSIGNMENT_HOLDING_STATUSES
        if not holding:
            return
        if not (isinstance(worker, str) and worker):
            if status == 'in_progress':
                self._emit('reported:in_progress_without_worker', slug, tid)
            return
        if agent_name_problem(worker):
            self._emit('reported:invalid_worker_name', slug, tid, worker)
            return
        slot = self._slot(worker)
        if slot[0] == 'unverifiable':
            self._emit('reported:assignment_unverifiable', slug, tid, worker, detail=slot[1])
            return
        if slot[0] == 'ok':
            if slot[1] != f"{slug}:{tid}":
                self._emit('reported:agent_slot_busy', slug, tid, worker,
                           detail=f"assignment points to {slot[1][:80]}")
                return
            if status == 'in_progress':
                verdict = self.t.classify_assignment(worker, slug, tid, str(gen) if gen else None)
                if gen and verdict == ASSIGN_SUCCESSOR:
                    self._emit('reported:generation_mismatch', slug, tid, worker)
                elif gen and verdict == ASSIGN_UNVERIFIABLE:
                    self._emit('reported:assignment_unverifiable', slug, tid, worker,
                               detail='identity unreadable or malformed')
            return
        # 枠が ABSENT。R-1 が書くのは in_progress だけ (crash で欠けが生まれるのは pull の窓だけ)
        if status != 'in_progress':
            self._emit('reported:holding_without_assignment', slug, tid, worker,
                       detail=f"status={status}")
            return
        if gen is None or gen == '':
            self._emit('reported:in_progress_without_generation', slug, tid, worker)
            return
        problem = self._ownership_problem(worker, slug, tid)
        if problem is not None:
            code, detail = problem
            self._emit(f'reported:{code}', slug, tid, worker, detail=detail)
            return

        def _publish():
            self.t.publish_assignment(worker, slug, tid, str(gen))
        files = self._do_writes(_publish)
        self._emit('repaired:R-1', slug, tid, worker, files=files,
                   from_status=status, to_status=status, generation=str(gen))

    def _ownership_problem(self, agent, slug, tid):
        """所有の証拠の走査 (§2.5)。`worker = agent` の in_progress card が全 mission でこの 1 枚だけと
        **読んで確かめた**ときだけ None。2 枚以上 → duplicate_owner、読めない card が 1 枚でもあれば
        owner_unprovable。archive は読まない。読むのは status と worker だけ。"""
        owned, unprovable = [], []
        missions_dir = self.t._p('missions')
        try:
            slugs = sorted(os.listdir(missions_dir))
        except FileNotFoundError:
            slugs = []
        except OSError as e:
            return 'owner_unprovable', f"cannot list missions: {e.strerror}"
        for s in slugs:
            tdir = os.path.join(missions_dir, s, 'tasks')
            try:
                names = sorted(os.listdir(tdir))
            except FileNotFoundError:
                continue
            except OSError as e:
                unprovable.append(f"{s}/tasks: {e.strerror}")
                continue
            for name in names:
                m = _cards.TASK_FILENAME_RE.fullmatch(name)
                if not m:
                    continue
                t = f"t{m.group(1)}"
                kind, meta, _b, reason, _e = self._card(s, t)
                if kind == 'unreadable':
                    unprovable.append(f"{s}/{t}")
                elif kind == 'ok' and meta.get('worker') == agent and meta.get('status') == 'in_progress':
                    owned.append(f"{s}/{t}")
        if unprovable:
            return 'owner_unprovable', ', '.join(unprovable[:10])
        if owned != [f"{slug}/{tid}"]:
            return 'duplicate_owner', ', '.join(owned[:10])
        return None

    # -- R-3: 採番を進める --------------------------------------------------------
    def _r3(self, slug):
        try:
            mpath = self.t.mission_path(slug)
        except InvalidName:
            return
        text = _cards.read_regular_text_or_unreadable(mpath)
        if _cards.is_missing(text):
            return
        if _cards.is_unreadable(text):
            self._emit('reported:mission_unreadable', slug, detail=text.reason)
            return
        try:
            mission = _cards.parse_yaml(text, source=mpath)
        except ValueError as e:
            self._emit('reported:mission_unreadable', slug, detail=str(e))
            return
        try:
            nxt = int(mission.get('next_task_id') or 1)
        except (TypeError, ValueError):
            self._emit('reported:next_task_id_invalid', slug)
            return
        nums = []
        tdir = os.path.join(self.t._p('missions'), slug, 'tasks')
        for name in _list_dir(tdir):
            m = _cards.TASK_FILENAME_RE.fullmatch(name)
            if m:
                nums.append(int(m.group(1)))
        if not nums or max(nums) < nxt:
            return
        leftover = ', '.join(f"t{n:03d}" for n in sorted(nums) if n >= nxt)

        def _advance():
            mission['next_task_id'] = max(nums) + 1
            self.t.write_mission(slug, mission)
        files = self._do_writes(_advance)
        if self.apply:
            _warn(f"[state-store] R-3: {slug} の next_task_id を {max(nums) + 1} に進めた "
                  f"(前回の残りの card: {leftover})")
        self._emit('repaired:R-3', slug, detail=f"leftover: {leftover}", files=files)

    # -- R-4: archive 済みを外す --------------------------------------------------
    def _r4(self, slug):
        try:
            _check_slug(slug)
        except InvalidName:
            return
        try:
            state = self.t.load_state()
        except StoreReadError as e:
            self._emit('reported:state_unreadable', detail=e.reason)
            return
        active = list(state.get('active_missions') or [])
        if slug not in active:
            return
        if os.path.lexists(self.t._p('missions', slug)) or not os.path.isdir(self.t._p('archive', slug)):
            return

        def _drop():
            active.remove(slug)
            state['active_missions'] = active
            if state.get('default_mission') == slug:
                state['default_mission'] = active[0] if active else None
            self.t.write_state(state)
        files = self._do_writes(_drop)
        self._emit('repaired:R-4', slug, files=files)


# ---------------------------------------------------------------------------
# 診断 (ロックを取らない・書かない)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Finding:
    kind: str          # would-repair:R-n / reported:<code> / stale_tmp / orphan_identity
    mission: str | None = None
    task: str | None = None
    agent: str | None = None
    detail: str | None = None


def diagnose(queue_dir, scope=None):
    """store-check の本体。R-1〜R-4 と「表に無い食い違い」を**書かずに**列挙し、残骸 (`.*.tmp.*`)・
    本体の無い `.identity` も出す。ロックを取らないので、途中のトランザクションを 1 回見うる
    (2 回連続で出たものだけが本物)。"""
    queue_dir = os.fspath(queue_dir)
    scope = scope or Scope.everything(queue_dir)
    txn = Txn(queue_dir, op='diagnose', actor='diagnose')
    findings = []
    for rep in _Recovery(txn, scope, apply=False).run():
        kind = f"would-repair:{rep.rule}" if rep.repaired else rep.result
        findings.append(Finding(kind, rep.mission, rep.task, rep.agent, rep.detail))
    adir = os.path.join(queue_dir, 'assignments')
    for name in _list_dir(adir):
        if name.endswith(IDENTITY_SUFFIX) and not os.path.lexists(os.path.join(adir, name[:-len(IDENTITY_SUFFIX)])):
            findings.append(Finding('orphan_identity', agent=name[:-len(IDENTITY_SUFFIX)]))
    for root, _dirs, files in os.walk(queue_dir):
        for f in files:
            if re.match(r'^\..+\.tmp\..+', f):
                findings.append(Finding('stale_tmp', detail=os.path.relpath(os.path.join(root, f), queue_dir)))
    return findings


# ---------------------------------------------------------------------------
# ロック外の小さな共有ファイル (queue/.lock を取らないもの。S5 が使う)
# ---------------------------------------------------------------------------

def locked_update_json(path, lock_path, fn):
    """専用ロック + 読み直し + 原子的書き込み。`fn(dict) -> dict`。ファイルが無い (ENOENT) ときだけ
    `{}` から始める。読めない・JSON でないは `StoreReadError` (空に潰さない)。
    queue/.lock を握ったままこれを呼ばない (ロックの順序: queue/.lock が最も外側)。"""
    path, lock_path = os.fspath(path), os.fspath(lock_path)
    _ensure_dir(os.path.dirname(lock_path) or '.', lock_path)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        raise LockFailed(f"cannot open {lock_path}: {e}") from e
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            raise LockFailed(f"cannot lock {lock_path}: {e}") from e
        text = _cards.read_regular_text_or_unreadable(path)
        if _cards.is_missing(text):
            data = {}
        elif _cards.is_unreadable(text):
            raise StoreReadError(path, text.reason, text.errno)
        else:
            try:
                data = json.loads(text)
            except ValueError as e:
                raise StoreReadError(path, f'not valid JSON: {e}') from e
            if not isinstance(data, dict):
                raise StoreReadError(path, 'JSON top-level is not an object')
        new = fn(data)
        if not isinstance(new, dict):
            raise StoreError('locked_update_json: fn must return a dict')
        atomic_write_text(path, json.dumps(new, ensure_ascii=False, indent=2) + '\n')
        return new
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
