#!/usr/bin/env python3
"""scripts/lib_state_store.py — queue への**書き込み**の唯一の入口 (vNext 01a / S2)。

読み取り専用の `lib_task_cards.py` と対になる。設計は `knowledge/state-store.md` の
§2 (crash モデル) / §3 (この API) / §4 (監査ログ)。

**呼び出し元は plan.sh だけ** (S3 / t012 で cutover。S2 では呼び出し元ゼロだった)。plan.sh の queue への
書き込み (card・mission・state・assignment の公開 / 撤去) と `queue/.lock` の取得は、すべてこの lib を通る。
dispatcher・hooks・verifier-dispatcher はまだ import しない (S5 で寄せる)。許可表は
`tests/test_state_store_callers.py`。plan.sh 側の使い方は `knowledge/state-store.md` §4.1。

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

import lib_execution as _ex  # noqa: E402
import lib_task_cards as _cards  # noqa: E402
import lib_task_status as _status  # noqa: E402

# ---------------------------------------------------------------------------
# 「内容を出さない」の門 (原案 §14-21 / STATE-06)
# ---------------------------------------------------------------------------
#
# stderr・監査ログ・例外メッセージへ出してよいのは、**固定のコード**と**安全なメタデータ**
# (ファイルパス・行番号・errno の名前・ファイル名由来の識別子・既知の status 語彙) だけ。
# カードの本文・frontmatter の値・Result・assignment の中身・パーサ例外の文字列は出さない。
# `lib_task_cards.parse_yaml()` の ValueError は**問題の行をそのまま含む** (`{line!r}`) ので、
# `str(e)` を経由すると壊れた行にあった reason / secret がそのまま残る。200 文字で切っても
# 約束は守れない — 切るのではなく、そもそも文字列を通さない。

_SAFE_TOKEN_RE = re.compile(r'[A-Za-z0-9_.-]{1,64}')
_SAFE_GENERATION_RE = re.compile(r'[A-Za-z0-9:_.+-]{1,64}')
_SAFE_EXECUTION_ID_RE = _ex.EXECUTION_ID_RE
_MALFORMED_LINE_RE = re.compile(r'malformed line (\d+)')

#: card の status として出してよい語彙 (これ以外の値は内容として扱い、出さない)。語彙は lib_task_status が持つ。
_KNOWN_STATUSES = frozenset(_status.TASK_STATUSES | {_cards.CORRUPT_TASK_STATUS})


def _safe_token(value):
    """ファイル名・Worker 名として**そのまま出してよい**なら文字列、そうでなければ None。"""
    return value if isinstance(value, str) and _SAFE_TOKEN_RE.fullmatch(value) else None


def _safe_generation(value):
    text = None if value is None else str(value)
    return text if text is not None and _SAFE_GENERATION_RE.fullmatch(text) else None


def _safe_status(value):
    return value if isinstance(value, str) and value in _KNOWN_STATUSES else None


def _safe_execution_id(value):
    """`ex-<32 hex>` の完全一致だけ。外れたら None (名乗られた値・壊れた値を監査ログに残さない。execution.md §1.5)。"""
    return value if isinstance(value, str) and _SAFE_EXECUTION_ID_RE.fullmatch(value) else None


def _row_actor(actor, hint):
    """監査行の `actor`。`actor` が使えなければ (無い・`unknown`・門を通らない) `hint` (操作の前の card の worker)、
    それも使えなければ `unknown`。**`actor` が使えるときは置き換えない**。"""
    safe = _safe_token(actor)
    if safe and safe != 'unknown':
        return safe
    return _safe_token(hint) or 'unknown'


def _safe_caller_check(value):
    return value if isinstance(value, str) and value in _ex.CALLER_CHECKS else None


#: 監査ログ・stderr の `detail` に出してよい**形**。自由文は通さない (パーサ例外・カードの値・枠の中身は
#: どの形にも合わない)。合わないものは 'redacted'。
_CODE = r'[a-z_]+(?::[A-Za-z0-9_=.-]+)*'                 # read_error:EACCES / parse_error:line=3
_IDENT = r'[A-Za-z0-9_.-]{1,64}'
_SAFE_DETAIL_RES = tuple(re.compile(p) for p in (
    _CODE,
    r'[A-Za-z0-9_./-]{1,300}: ' + _CODE,                  # missions/<slug>/tasks: list_error:EACCES
    r'leftover: t\d+(?:, t\d+)*',                         # R-3
    r'status=(?:' + '|'.join(sorted(_KNOWN_STATUSES)) + r'|<unknown>)',
    # duplicate_owner / owner_unprovable: `<slug>/<tid>` (退避先の card は `archive/<slug>/<tid>`)
    r'(?:archive/)?' + _IDENT + r'/' + _IDENT + r'(?:, (?:archive/)?' + _IDENT + r'/' + _IDENT + r')*',
    r'assignment body: lstat failed',
    r'presented=ex-[0-9a-f]{32}',                         # 照合に失敗した名乗り (形が正しいときだけ。execution.md §1.5)
))
_SAFE_RESULT_RE = re.compile(r'(ok|repaired:R-[1-5]|reported:[a-z_]+|refused:[A-Z_]+)')
_SAFE_RELPATH_RE = re.compile(r'[A-Za-z0-9_./-]{1,300}')


def _safe_detail(value):
    text = str(value)
    return text if len(text) <= 300 and any(r.fullmatch(text) for r in _SAFE_DETAIL_RES) else 'redacted'


def _safe_result(value):
    return value if isinstance(value, str) and _SAFE_RESULT_RE.fullmatch(value) else 'redacted'


def _safe_relpath(value):
    return value if isinstance(value, str) and _SAFE_RELPATH_RE.fullmatch(value) else None


def _errno_name(err_no):
    return _errno.errorcode.get(err_no, str(err_no)) if err_no else 'unknown'


def _parse_code(exc):
    """パーサ例外 → 固定コード (+ 行番号)。**例外の文字列は使わない** (問題の行を含むため)。"""
    m = _MALFORMED_LINE_RE.search(str(exc))
    return f"parse_error:line={m.group(1)}" if m else "parse_error"


def _unreadable_code(unreadable):
    """`Unreadable` → 固定コード。reason の文字列は使わない (種類の判定にだけ読む)。"""
    if unreadable.errno:
        return f"read_error:{_errno_name(unreadable.errno)}"
    reason = unreadable.reason
    if reason.startswith('not a regular file'):
        return 'not_regular_file'
    if reason.startswith('decode error'):
        return 'decode_error'
    return 'unreadable'


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class StoreError(Exception):
    """この lib が投げる例外の基底。plan.sh 側は die(msg, code) に写す
    (LockBusy → 4、前提外れ → 3、それ以外 → 1。設計 §3.2)。"""


class StoreWriteError(StoreError):
    """書けなかった。`op` は失敗した段 (serialize / stat / mkdir / mkstemp / write / chmod /
    fsync / replace / fsync_dir / unlink / append)、`errno` は OSError 由来のときだけ入る。

    `committed` が True のとき (`op == 'fsync_dir'` の**置換 / 削除の後**の失敗) は**新しい内容は既に置き換わっている**。
    耐久性だけが証明できない。それ以外の op では元のファイルはそのまま残っている
    (ディレクトリを作った直後の親の fsync も `fsync_dir` だが、まだ何も書いていないので `committed` は False)。"""

    def __init__(self, path, op, err_no=None, detail=''):
        self.path = str(path)
        self.op = op
        self.errno = err_no
        self.detail = str(detail)
        self.committed = False
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


class AlreadyExists(StoreError):
    """新規作成のつもりで書く先に、既に何かが在る (または在るか確かめられない)。

    回復 (R-3 等) が失敗して採番・前提が古いままでも、**既存の card / mission を上書きしない**
    ための排他作成の拒否 (t037)。`path` は queue からの相対 (中身は含めない)。
    """

    def __init__(self, path, state):
        self.path = path
        self.state = state                      # 'present' / 'unobservable'
        super().__init__(f"{path} は既に在ります ({state}) — 新規作成では上書きしません")


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


def _sys_rename(src, dst):
    os.rename(src, dst)


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


def ensure_dir(dirpath):
    """`dirpath` を (無ければ祖先ごと) **durable に**作る公開入口。作った各ディレクトリの**親**を fsync する
    (`os.makedirs` は fsync しないので、電源断で作ったディレクトリのエントリが消えうる)。
    既に在れば何もしない。失敗は `StoreWriteError` (`mkdir` / `fsync_dir`)。queue の下のディレクトリは
    `os.makedirs` ではなくこれで作る (`tests/test_plan_sh_s3_fix2.py` が plan.sh の呼び出しを固定)。"""
    dirpath = os.fspath(dirpath)
    _ensure_dir(dirpath, dirpath)


def _current_umask():
    """今の umask (読むには一度書き換えるしかない。単一スレッドの CLI / ロックの中で使う)。"""
    old = os.umask(0)
    os.umask(old)
    return old


def _new_file_mode(mode=None):
    """新規ファイルの mode。`open(path, 'w')` と同じく umask に従う (0666 & ~umask)。明示の `mode` があればそれ。"""
    return mode if mode is not None else 0o666 & ~_current_umask()


def _open_lock_file(lock_path):
    """ロックファイルを開く。通常ファイルでなければ (FIFO / device など) 閉じて OSError(EINVAL)。
    O_NONBLOCK は open だけのため (flock には効かない)。開いた後に外す。"""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o666)  # umask に従う (旧 open('a+') と同じ)
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(_errno.EINVAL, 'lock path is not a regular file')
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


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
    mode は既存ファイルのものを引き継ぐ (無ければ `mode`、それも無ければ umask に従う 0666 & ~umask)。mkstemp は
    0o600 で作るので、引き継がないと Worker / デーモン / hooks の間で読めなくなる。
    tmp の先頭が `.` なのは `tNNN.md` の列挙 (`TASK_FILENAME_RE`) に絶対に当たらないため。

    失敗は `StoreWriteError`。置換の前の失敗では tmp を消し、元のファイルは変わらない。
    """
    path = os.fspath(path)
    try:
        data = text.encode('utf-8')
    except (UnicodeError, AttributeError) as e:
        raise StoreWriteError(path, 'serialize', None, type(e).__name__) from e

    parent = os.path.dirname(path) or '.'
    name = os.path.basename(path)
    _fault('atomic:begin', path)
    _ensure_dir(parent, path)

    try:
        final_mode = _stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        final_mode = _new_file_mode(mode)       # 新規は umask に従う (既存は元の mode を保つ)
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
        try:
            _fsync_dir(parent, path)
        except StoreWriteError as e:
            e.committed = True                # 置換は済んでいる (耐久性だけが証明できない)
            raise
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
    try:
        _fsync_dir(os.path.dirname(path) or '.', path)
    except StoreWriteError as e:
        e.committed = True                    # 削除は済んでいる (耐久性だけが証明できない)
        raise
    _fault('remove:dir_synced', path)
    return True


def durable_rename(src, dst):
    """`src` を `dst` へ移し (同じファイルシステム上の rename)、**元と先の両方の親 dir** を fsync する。

    `shutil.move` は rename の後に親を fsync しないので、電源断で「元にも先にも無い」/「両方にある」
    が起きうる (archive / init --force の退避)。先の親が無ければ durable に作る。先が既に在れば
    `StoreWriteError(op='rename', EEXIST)` (上書きしない — `os.rename` がディレクトリ相手に黙って
    置き換える経路を作らない)。別のファイルシステムをまたぐ rename (EXDEV) は `StoreWriteError`
    (コピーして消す動きは途中で落ちると二重になる。queue の中は同じ FS)。
    """
    src, dst = os.fspath(src), os.fspath(dst)
    _fault('rename:begin', src)
    dst_parent = os.path.dirname(os.path.abspath(dst))
    src_parent = os.path.dirname(os.path.abspath(src))
    _ensure_dir(dst_parent, dst)
    if os.path.lexists(dst):
        raise StoreWriteError(dst, 'rename', _errno.EEXIST, 'destination exists')
    try:
        _sys_rename(src, dst)
    except OSError as e:
        raise StoreWriteError(src, 'rename', e.errno) from e
    _fault('rename:renamed', dst)
    try:
        _fsync_dir(dst_parent, dst)
        if src_parent != dst_parent:
            _fsync_dir(src_parent, src)
    except StoreWriteError as e:
        e.committed = True                    # rename は済んでいる (耐久性だけが証明できない)
        raise
    _fault('rename:dir_synced', dst)


# ---------------------------------------------------------------------------
# 直列化 (cutover 前の plan.sh の関数と**バイト単位で同じ**。S3 で plan.sh のコピーは消え、ここが唯一の定義)
# ---------------------------------------------------------------------------
#
# 出力は cutover 前 (a1f6957) の plan.sh (dump_yaml / serialize_frontmatter / save_state) の出力と
# 1 バイトも変えない。`tests/test_state_store_serialization_matches_plan_sh.py` が、その版の出力を写した
# golden (`tests/fixtures/state_store_serialization_golden.json`) との一致を固定する (ずれたら赤)。
# plan.sh にこの規則のコピーを戻さない (同じテストが plan.sh の AST で落とす)。

TASK_META_KEY_ORDER = [
    'id', 'title', 'skills', 'priority', 'status',
    'blocked_by', 'released_deps', 'timeout', 'target_dir', 'worker', 'started_at',
    *_ex.EXECUTION_FIELDS,                     # 01c: 試行の欄は `started_at` の直後 (execution.md §1.2)
    'completed_at',
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

#: 状態の語彙は lib_task_status が唯一の定義 (S1)。ここには status の集合を置かない。
_TERMINAL_STATUSES = _status.TERMINAL_STATUSES
_ASSIGNMENT_HOLDING_STATUSES = _status.ASSIGNMENT_HOLDING_STATUSES
_NEEDS_DIRECTOR = 'needs_director'
#: 今の reap-orphan-assignment の判定と同じ式 (終端 + failed)。
ORPHAN_ASSIGNMENT_FINISHED_STATUSES = _status.RELEASED_WORK_STATUSES


def is_orphan_target(status, worker):
    """assignment が指す card が、**assignment を撤去するコマンド自身の書く status** か (R-2 の集合。
    設計 §2.3)。手放し済み (done / verified / skipped / failed) ∪ needs_director (needs-director /
    retire) ∪ worker が空の pending (update --reset / retire reset)。R-2 と `plan.sh
    reap-orphan-assignment` が同じ定義を使う (1 か所)。blocked / verification_failed 等は含めない —
    Director が作業中の card に `update --status` で付けても Worker は動いており、assignment は残るのが
    正当 (「holding でない status すべて」にすると動いている Worker を殺す経路になる)。"""
    return (status in ORPHAN_ASSIGNMENT_FINISHED_STATUSES or status == _NEEDS_DIRECTOR
            or (status == 'pending' and not worker))


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


def _check_execution_id(value):
    if not _ex.is_execution_id(value):
        raise InvalidName("invalid execution id (ex-<32 hex> の形だけ。値は出さない)")
    return value


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
        fd = _open_lock_file(lock_path)
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
    add_missions: R-3 を見る mission。archive_slugs: R-4 を見る slug。
    unobservable: スコープを組む時点で**観測できなかった入力** `(queue からの相対パス, コード)`。
    `Scope.everything()` が列挙に失敗したディレクトリ・読めない state.yaml を、空に潰さずここに残し、
    `diagnose()` が finding として返す (P2-1: 観測できない store を健全と報告しない)。"""
    cards: tuple = ()
    agents: tuple = ()
    add_missions: tuple = ()
    archive_slugs: tuple = ()
    unobservable: tuple = ()

    @staticmethod
    def everything(queue_dir):
        """diagnose (store-check) 用: queue 全体。"""
        queue_dir = os.fspath(queue_dir)
        cards, missions, problems = [], [], []

        def ls(*parts):
            names, code = _list_dir(os.path.join(queue_dir, *parts))
            if code:
                problems.append((os.path.join(*parts), code))
            return names

        for slug in ls('missions'):
            missions.append(slug)
            for name in ls('missions', slug, 'tasks'):
                m = _cards.TASK_FILENAME_RE.fullmatch(name)
                if m:
                    cards.append((slug, f"t{m.group(1)}"))
        agents = [a for a in ls('assignments') if agent_name_problem(a) is None]
        active, code = _read_active_missions(queue_dir)
        if code:
            problems.append(('state.yaml', code))
        return Scope(cards=tuple(cards), agents=tuple(agents), add_missions=tuple(missions),
                     archive_slugs=tuple(active), unobservable=tuple(problems))


def _list_dir(path):
    """`(名前の一覧, コード)`。**ENOENT だけ**「無い」= `([], None)`。それ以外の失敗は
    `([], 'list_error:<ERRNO>')` — 空リストと同じ形で返さない (呼び出し側が必ずコードを見る)。"""
    try:
        return sorted(os.listdir(path)), None
    except FileNotFoundError:
        return [], None
    except OSError as e:
        return [], f"list_error:{_errno_name(e.errno)}"


_SLUG_VALUE_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')


def _state_problem(data):
    """parse 済みの state.yaml の**形**の検査。問題なら固定コード、健全なら None。値は出さない。

    - 空ファイル・`active_missions` 欄が無い → 'state_missing_key' (「active な mission が 0 件」と
      読むと archive 場面で生きている mission が消える。**ENOENT だけ**が「まだ無い」)
    - `active_missions` が list でない (true / 文字列 / 閉じていない inline list) → 'active_missions_not_list'
    - 要素が slug の形の文字列でない → 'active_missions_bad_entry'
    - `default_mission` が None か slug の形の文字列でない → 'default_mission_bad_type'
    `active_missions:` (値なし = null) は plan.sh と同じく空 list。
    """
    if not isinstance(data, dict) or 'active_missions' not in data:
        return 'state_missing_key'
    active = data['active_missions']
    if active is not None:
        if not isinstance(active, list):
            return 'active_missions_not_list'
        if not all(isinstance(s, str) and _SLUG_VALUE_RE.fullmatch(s) for s in active):
            return 'active_missions_bad_entry'
    default = data.get('default_mission')
    if default is not None and not (isinstance(default, str) and _SLUG_VALUE_RE.fullmatch(default)):
        return 'default_mission_bad_type'
    return None


def _read_active_missions(queue_dir):
    """`(active_missions, コード)`。state.yaml が無い (ENOENT) は `([], None)`、読めない・壊れている・
    形が違うは `([], コード)`。"""
    text = _cards.read_regular_text_or_unreadable(os.path.join(queue_dir, 'state.yaml'))
    if _cards.is_missing(text):
        return [], None
    if _cards.is_unreadable(text):
        return [], _unreadable_code(text)
    try:
        data = _cards.parse_yaml(text)
    except ValueError as e:
        return [], _parse_code(e)
    code = _state_problem(data)
    if code:
        return [], code
    return list(data['active_missions'] or []), None


def _path_state(path):
    """`'present'` / `'absent'` / `'unobservable'`。**ENOENT だけ** absent
    (`os.path.exists` / `lexists` は EACCES も False に潰す)。"""
    try:
        os.lstat(path)
        return 'present'
    except FileNotFoundError:
        return 'absent'
    except OSError:
        return 'unobservable'


def read_execution_record(queue_dir, slug, execution_id):
    """ロックなしで record を読む (`get_execution` と `Txn.read_execution_record` の唯一の読み口)。
    `('absent',)` / `('unreadable', 固定コード)` / `('ok', dict)`。"""
    path = os.path.join(os.fspath(queue_dir), 'missions', _check_slug(slug), 'executions',
                        f"{_check_execution_id(execution_id)}.json")
    text = _cards.read_regular_text_or_unreadable(path)
    if _cards.is_missing(text):
        return ('absent',)
    if _cards.is_unreadable(text):
        return ('unreadable', _unreadable_code(text))
    try:
        data = json.loads(text)
    except ValueError:
        return ('unreadable', 'json_parse_error')
    if not isinstance(data, dict):
        return ('unreadable', 'json_not_object')
    return ('ok', data)


class Txn:
    """`transaction()` が渡す。**書き込みはステージしない** — 呼び出し側が書いた順に、その場で
    atomic に書く (設計 §3.2。順序の規則そのものが crash モデルなので、書く順を保つ)。"""

    def __init__(self, queue_dir, *, op, actor):
        self.queue_dir = os.fspath(queue_dir)
        self.op = op
        self.actor = actor or 'unknown'
        self.txn_id = uuid.uuid4().hex
        self.audit_failures = 0
        self.durability_failures = []      # 親 dir の fsync だけが失敗した書き込みの errno 名 (§4.1)
        self._records = []
        self._files = []

    # ---- パス --------------------------------------------------------------
    def _p(self, *parts):
        return os.path.join(self.queue_dir, *parts)

    def card_path(self, slug, tid):
        return self._p('missions', _check_slug(slug), 'tasks', f"{_check_tid(tid)}.md")

    def mission_path(self, slug):
        return self._p('missions', _check_slug(slug), 'mission.yaml')

    def execution_record_path(self, slug, execution_id):
        """`queue/missions/<slug>/executions/<execution_id>.json` (識別子はファイル名。execution.md §1.2)。"""
        return self._p('missions', _check_slug(slug), 'executions', f"{_check_execution_id(execution_id)}.json")

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
            return 'unreadable', None, None, _unreadable_code(text), text.errno
        try:
            meta, body = _cards.parse_frontmatter(text, source=path)
        except ValueError as e:
            return 'unreadable', None, None, _parse_code(e), None
        if meta.get('id') not in (None, '', tid):
            return 'unreadable', None, None, 'id_mismatch', None
        # status は語彙の文字列だけ。list / mapping / 未知の値は frozenset の判定で TypeError になるか、
        # 黙って「別の状態」になる。値は出さない (固定コード)。
        status = meta.get('status')
        if not (isinstance(status, str) and status in _status.TASK_STATUSES):
            return 'unreadable', None, None, 'bad_status', None
        return 'ok', meta, body, None, None

    def load_mission(self, slug):
        path = self.mission_path(slug)
        text = _cards.read_regular_text_or_unreadable(path)
        if _cards.is_unreadable(text):
            raise StoreReadError(path, _unreadable_code(text), text.errno)
        try:
            return _cards.parse_yaml(text, source=path)
        except ValueError as e:
            code = _parse_code(e)
        # except の外で投げる: 例外の連鎖 (__context__) に問題の行を残さない
        raise StoreReadError(path, code)

    def load_state(self):
        """state.yaml。**ENOENT だけ**「まだ無い」= 空の既定値。それ以外の読めない・壊れているは例外。"""
        path = self.state_path
        text = _cards.read_regular_text_or_unreadable(path)
        if _cards.is_missing(text):
            return {'active_missions': [], 'default_mission': None}
        if _cards.is_unreadable(text):
            raise StoreReadError(path, _unreadable_code(text), text.errno)
        try:
            data = _cards.parse_yaml(text, source=path)
        except ValueError as e:
            code = _parse_code(e)
        else:
            code = None
        code = code or _state_problem(data)
        if code:
            raise StoreReadError(path, code)
        if data.get('active_missions') is None:
            data['active_missions'] = []
        data.setdefault('default_mission', None)
        return data

    # ---- 書き --------------------------------------------------------------
    def _durability_unproven(self, err):
        """置換 / 削除は済んだが、親 dir の fsync だけが失敗した (`StoreWriteError.committed`)。

        **トランザクションの途中では続行する** (警告 + 監査ログの `detail`)。ここで落とすと、card を書いた後・
        assignment を書く前で止まる = 「割れたトランザクション」を、耐久性を証明できなかったというだけで
        自分で作る。内容は既に置き換わっているので、続行しても状態は正しい (旧 plan.sh は親 dir の fsync を
        そもそもしていなかった = 悪化しない)。`atomic_write_text` を直接呼ぶ側は従来どおり例外を受ける。
        """
        self.durability_failures.append(_errno_name(err.errno))
        _warn(f"[state-store warn] 親ディレクトリの fsync に失敗しました "
              f"({_safe_relpath(self._rel(err.path)) or 'path'}: {_errno_name(err.errno)}) — "
              f"内容は書き込み済みですが、電源断に対する耐久性は確認できていません")

    def _write(self, path, text):
        try:
            atomic_write_text(path, text)
        except StoreWriteError as e:
            if not (e.committed and e.op == 'fsync_dir'):
                raise
            self._durability_unproven(e)
        self._files.append(self._rel(path))

    def _remove(self, path):
        try:
            removed = atomic_remove(path)
        except StoreWriteError as e:
            if not (e.committed and e.op == 'fsync_dir'):
                raise
            self._durability_unproven(e)
            removed = True
        if removed:
            self._files.append(self._rel(path))
        return removed

    def write_card(self, slug, tid, meta, body):
        if meta.get('id') not in (None, '', tid):
            raise InvalidName(f"meta['id'] が task id {tid} と食い違う (識別子はファイル名。値は出さない)")
        self._write(self.card_path(slug, tid), serialize_card(meta, body))

    def write_mission(self, slug, data):
        self._write(self.mission_path(slug), serialize_mission(data))

    # ---- 新規作成 (排他) ---------------------------------------------------
    # ロックの中で「無いことを確かめてから」書く。ENOENT 以外 (在る・EACCES 等で見えない) は
    # 書かずに `AlreadyExists` — 回復が失敗して採番が古いままでも、既存を黙って置き換えない。
    def path_state(self, *parts):
        """queue 相対の `parts` が `'present'` / `'absent'` / `'unobservable'`。"""
        return _path_state(self._p(*parts))

    def card_state(self, slug, tid):
        return _path_state(self.card_path(slug, tid))

    def create_card(self, slug, tid, meta, body):
        state = self.card_state(slug, tid)
        if state != 'absent':
            raise AlreadyExists(self._rel(self.card_path(slug, tid)), state)
        self.write_card(slug, tid, meta, body)

    def create_mission(self, slug, data):
        state = _path_state(self.mission_path(slug))
        if state != 'absent':
            raise AlreadyExists(self._rel(self.mission_path(slug)), state)
        self.write_mission(slug, data)

    def write_state(self, state):
        self._write(self.state_path, serialize_state(state))

    # ---- Execution record (projection。判断には使わない。execution.md §1.2) ------------
    def read_execution_record(self, slug, execution_id):
        """`('absent',)` (ENOENT だけ) / `('unreadable', 固定コード)` / `('ok', dict)`。
        読めない・JSON でない・オブジェクトでないは `unreadable` —— 「無い」に潰さない。"""
        return read_execution_record(self.queue_dir, slug, execution_id)

    def write_execution_record(self, slug, execution_id, record):
        """record を原子的に書く (card の後・assignment の前。呼び出し側が順序を守る)。"""
        self._write(self.execution_record_path(slug, execution_id),
                    json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + '\n')

    # ---- assignment (projection) ------------------------------------------
    def publish_assignment(self, agent, slug, tid, generation, execution_id=None):
        """identity → 本体の順 (本体 = 「存在 = busy」が世代不明で観測されないように)。

        `execution_id` (01c) は**指定されたときだけ** identity に欄を出す (None なら今とバイトが同じ。空文字は
        「指定されなかった」に倒さず `InvalidName`)。形は `ex-<32 hex>`。"""
        _check_agent(agent), _check_slug(slug), _check_tid(tid)
        identity = {'mission': slug, 'task': tid, 'worker': agent, 'started_at': generation}
        if execution_id is not None:
            identity['execution_id'] = _check_execution_id(execution_id)
        self._write(self.identity_path(agent), json.dumps(
            identity, ensure_ascii=False, sort_keys=True) + '\n')
        self._write(self.assignment_path(agent), f"{slug}:{tid}\n")

    def _read_slot(self, agent):
        """`('absent',)` / `('unverifiable', reason)` / `('ok', '<slug>:<tid>')`。
        分けるのは ENOENT の 1 点だけ (`Path.exists()` は EACCES も False に潰す)。"""
        text = _cards.read_regular_text_or_unreadable(self.assignment_path(agent))
        if _cards.is_missing(text):
            return ('absent',)
        if _cards.is_unreadable(text):
            return ('unverifiable', _unreadable_code(text))
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

    def classify_assignment(self, agent, slug, tid, execution_id=None):
        """公開中の assignment が「この実行のもの」かの判定 (plan.sh classify_assignment と同じ結論)。
        execution_id=None は「いま card が示している実行」(同じロックの中で card を読んだ直後。枠が task を指すだけで足りる)。

        `execution_id` を渡すと identity の `execution_id` と比べる (`lib_execution.identity_matches`)。E4b で世代
        (`started_at`) の比較は外した: identity に ID が無い (E2 より前・旧コードの publish) もこの試行のものとは
        言えないので SUCCESSOR (撤去しない・保留。resume は公開し直す)。identity が無い・読めないときだけ UNVERIFIABLE。"""
        if agent_name_problem(agent):
            return ASSIGN_UNVERIFIABLE
        slot = self._read_slot(agent)
        if slot[0] == 'absent':
            return ASSIGN_ABSENT
        if slot[0] == 'unverifiable':
            return ASSIGN_UNVERIFIABLE
        if slot[1] != f"{slug}:{tid}":
            return ASSIGN_OTHER_TASK
        if execution_id is None:
            return ASSIGN_MINE
        identity = self._read_identity(agent)
        if not identity:
            return ASSIGN_UNVERIFIABLE
        if identity.get('mission') != slug or identity.get('task') != tid:
            return ASSIGN_UNVERIFIABLE
        if not _ex.identity_matches(identity, execution_id=execution_id):
            return ASSIGN_SUCCESSOR
        return ASSIGN_MINE

    def retire_assignment(self, agent, slug, tid, execution_id=None):
        """撤去する唯一の入口。「この実行のもの」と確定したときだけ消し、判定を返す。
        消せなかったら `StoreWriteError` (plan.sh の warn で握り潰す型を持たない)。"""
        verdict = self.classify_assignment(agent, slug, tid, execution_id)
        if verdict != ASSIGN_MINE:
            return verdict
        self._remove(self.assignment_path(agent))
        self._remove(self.identity_path(agent))
        return ASSIGN_MINE

    # ---- 監査ログ -----------------------------------------------------------
    def record(self, mission, task, from_status, to_status, generation=None, detail=None,
               execution_id=None, caller_check=None, actor_hint=None):
        """コマンド本体の監査ログの 1 行を予約する (with が例外なしで抜けたときに書く)。

        `execution_id` は書いた後の card の `current_execution_id` (`generation` が書いた後の `started_at` なのと同じ。
        legacy の card は None)。`caller_check` は照合の結果 (`lib_execution.CALLER_CHECKS`)。None なら行に出さない。
        `actor_hint` は**操作の前の card の worker** (execution.md §8「`actor` が `unknown` の穴」): このトランザクションの
        `actor` が `unknown` (AGENT_NAME が無い呼び出し) のときだけ代わりに使う。AGENT_NAME がある呼び出しの `actor` は
        置き換えない (Director が Worker の代わりに done を打った行を Worker の行にしない)。"""
        self._records.append(dict(
            op=self.op, mission=mission, task=task, from_status=from_status,
            to_status=to_status, generation=generation, detail=detail, result='ok',
            execution_id=execution_id, caller_check=caller_check, actor_hint=actor_hint))

    @property
    def has_body_record(self):
        """コマンド本体の行が予約済みか (`record()` が 1 回以上呼ばれた)。plan.sh の `with_lock` が「card を書いたトランザクション」
        の判定に使う (Controller は `save_task` を通さず `record()` で行を予約するので、`_AUDIT_TASK_ROWS` だけでは見えない)。"""
        return bool(self._records)

    def report(self, result, mission, task, detail=None, execution_id=None):
        """`reported:<コード>` の行を、いま追記する (本体の書き込みとは別。execution.md §1.4 の手順 0 が使う)。"""
        if not (isinstance(result, str) and result.startswith('reported:')):
            raise ValueError("report() は 'reported:<コード>' だけ")
        self._append_audit(dict(op=self.op, mission=mission, task=task, result=result, detail=detail,
                                execution_id=execution_id), ())

    def refuse(self, code, mission, task, from_status=None, execution_id=None, presented=None):
        """照合・遷移の**拒否**の行を、いま (with の出口を待たず) 追記する (execution.md §8)。

        コマンド本体の `die` / 例外の後でも残る (回復の行と同じ経路)。`result=refused:<CODE>`。`execution_id` は
        **card の** ID で、呼び出し元が名乗った値ではない (照合に失敗した値を記録の正として残さない)。
        名乗られた ID は形が正しいときだけ `detail` に `presented=ex-…`。コードが `lib_execution.AUDITED_REFUSALS`
        に無ければ何も書かない (idle・使い方の誤り・ロックは行にしない)。拒否の行を足しても、コマンド本体は
        1 バイトも書かない (行は監査ログであって queue の正本ではない)。"""
        if code not in _ex.AUDITED_REFUSALS:
            return
        detail = f"presented={presented}" if _safe_execution_id(presented) else None
        self._append_audit(dict(
            op=self.op, mission=mission, task=task, from_status=from_status, to_status=None,
            result=f"refused:{code}", detail=detail, execution_id=execution_id), ())

    def _flush_body_records(self):
        files = tuple(self._files)
        for rec in self._records:
            self._append_audit(rec, files)
        self._records = []

    def _append_audit(self, rec, files):
        """1 行追記。失敗しても状態遷移は止めない (stderr に警告 + `audit_failures`)。"""
        # 監査ログの**唯一の出口**。カード由来の値 (status / generation / worker 名 / 例外文字列) は
        # ここで門を通す。通らないものは None / 'redacted' (内容を出さない。原案 §14-21)。
        row = {
            'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'txn_id': self.txn_id,
            'op': _safe_token(rec.get('op', self.op)) or 'unknown',
            'mission': _safe_token(rec.get('mission')),
            'task': _safe_token(rec.get('task')),
            'actor': _row_actor(self.actor, rec.get('actor_hint')),
            'pid': os.getpid(),
            'from_status': _safe_status(rec.get('from_status')),
            'to_status': _safe_status(rec.get('to_status')),
            'generation': _safe_generation(rec.get('generation')),
            'execution_id': _safe_execution_id(rec.get('execution_id')),   # 書き手が渡さなければ null (01a と同じ)
            'result': _safe_result(rec.get('result', 'ok')),
            'files': [f for f in map(_safe_relpath, files) if f is not None],
        }
        if _safe_caller_check(rec.get('caller_check')):
            row['caller_check'] = rec['caller_check']        # 照合した操作だけ。出さない行は今とバイトが同じ
        detail = rec.get('detail')
        if not detail and self.durability_failures:
            # 遷移は完了しているが親 dir の fsync を証明できなかった (`_durability_unproven`)。行に残す。
            detail = f"fsync_dir_failed:{self.durability_failures[0]}"
        if detail:
            row['detail'] = _safe_detail(detail)
        path = audit_path(self.queue_dir)
        try:
            _fault('audit:begin', path)
            _ensure_dir(os.path.dirname(path), path)
            line = (json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
            # O_NONBLOCK: 読み手のいない FIFO への open(O_WRONLY) は永久に待つ (queue/.lock を持ったまま)。
            # 開けても通常ファイルでなければ書かない (FIFO / socket / device)。
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o666)   # umask に従う
            try:
                if not _stat.S_ISREG(os.fstat(fd).st_mode):
                    raise StoreWriteError(path, 'audit_open', _errno.EINVAL, 'not a regular file')
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
              from_status=None, to_status=None, generation=None, execution_id=None):
        # カードの値・例外文字列が stderr / 監査ログ / 戻り値に出る**唯一の経路**。門を通す。
        mission, task, agent = _safe_token(mission), _safe_token(task), _safe_token(agent)
        detail = _safe_detail(detail) if detail else None
        from_status, to_status = _safe_status(from_status), _safe_status(to_status)
        generation = _safe_generation(generation)
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
            from_status=from_status, to_status=to_status, generation=generation,
            execution_id=execution_id), files)

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
            except InvalidName:
                self._cards[key] = ('unreadable', None, None, 'invalid_name', None)
            else:
                self._cards[key] = self.t._read_card(path, tid)
        return self._cards[key]

    def _archived_card(self, slug, tid):
        """`archive/<slug>/tasks/<tid>.md` の card (`_read_card` の結果)。R-2 が「枠が指す card」を
        missions/ に見つけられなかったときの 2 か所目。名前が不正なら無いものとして扱う。"""
        key = ('archive', slug, tid)
        if key not in self._cards:
            try:
                path = os.path.join(self.t._p('archive', _check_slug(slug), 'tasks'),
                                    f"{_check_tid(tid)}.md")
            except InvalidName:
                self._cards[key] = ('missing', None, None, None, None)
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
            if isinstance(w, str) and w and agent_name_problem(w) is None and _safe_token(w) is not None:
                agents.append(w)
        if named:
            wanted = {f"{slug}:{tid}" for slug, tid, _m in named}
            agents.extend(self._reverse_lookup(wanted))
        for agent in sorted(dict.fromkeys(agents)):
            self._r2(agent)

        for slug, tid, meta in named:
            self._r5(slug, tid, meta)
        for slug, tid, meta in named:
            self._r1(slug, tid, meta)
        for slug in dict.fromkeys(s.add_missions):
            self._r3(slug)
        if not self.apply:
            self._execution_findings(named)          # store-check だけ (書かない・回復は拒否も報告も足さない)
        for slug in dict.fromkeys(s.archive_slugs):
            self._r4(slug)
        return self.out

    def _reverse_lookup(self, wanted):
        """`assignments/` の本体ファイル (`.identity` / `.restarting` / `.tmp` を除く) のうち、
        本文が wanted のどれかに一致する枠の名前。読めない枠は「指していない」と証明できないが、
        消さない・止めない (store-check が別に出す)。"""
        found = []
        names, code = _list_dir(self.t._p('assignments'))
        if code:
            # 列挙できない = 「この card を指す枠は無い」と証明できない。空に潰さず報告する
            self._emit('reported:assignments_dir_unreadable', detail=code)
        for name in names:
            if agent_name_problem(name) is not None:
                continue
            slot = self.t._read_slot(name)
            if slot[0] == 'ok' and slot[1] in wanted:
                found.append(name)
        return found

    # -- R-2: 孤児 projection を消す ----------------------------------------------
    def _r2(self, agent):
        slot = self._slot(agent)
        if slot[0] == 'absent':
            return
        if slot[0] == 'unverifiable':
            # 読めない枠は消さない・上書きしない (証明できない) が、**黙って飛ばさない** (P2-1)。
            # 名指しの card が参照していなくても、scope の枠なら報告する。
            self._emit('reported:assignment_unverifiable', agent=agent, detail=slot[1])
            return
        pslug, sep, ptid = slot[1].rpartition(':')
        if not sep or not _cards.TASK_ID_RE.fullmatch(ptid):
            self._emit('reported:assignment_malformed', agent=agent)      # 中身は出さない
            return
        kind, meta, _b, reason, _e = self._card(pslug, ptid)
        if kind == 'missing':
            # mission が退避 (plan.sh の mission 退避) 済みなら card はそちらにある。決着した task を
            # 指したまま残った枠 (最後の mission が完了 → 退避された直後の孤児。reap-orphan-assignment の
            # t117 と同じ形) を、ここでも消せるようにする。枠が指す task の card を**見つけられなかった**
            # ときだけ報告に倒す (どちらにも無い = 証拠が無い)。
            kind, meta, _b, reason, _e = self._archived_card(pslug, ptid)
        if kind == 'missing':
            self._emit('reported:assignment_target_missing', pslug, ptid, agent)
            return
        if kind == 'unreadable':
            self._emit('reported:assignment_target_unreadable', pslug, ptid, agent, detail=reason)
            return
        status = meta.get('status')
        worker = meta.get('worker') or None
        if is_orphan_target(status, worker):
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
            self._emit('reported:assignment_owner_mismatch', pslug, ptid, agent)
            return
        # blocked / verification_failed 等: Worker は動いており assignment は残るのが正当
        self._emit('reported:assignment_on_non_orphan_status', pslug, ptid, agent,
                   detail=f"status={_safe_status(status) or '<unknown>'}")

    # -- 試行 (01c) ---------------------------------------------------------------
    @staticmethod
    def _execution_view(meta):
        """`(view, 固定コード)`。欄が壊れていれば `(None, コード)` —— 既定値に倒さない (呼び出し側が報告する)。"""
        try:
            return _ex.attempt_view(meta, _ASSIGNMENT_HOLDING_STATUSES), None
        except ValueError as e:
            return None, str(e)          # lib_execution.fields_problem の固定コードだけ (値を含まない)

    def _active_execution_id(self, meta):
        """identity に入れる ID。**`attempt_view` が ACTIVE のときだけ** (DETACHED (a) の card の枠は取り直した別の
        持ち主のもので、古い試行 X を名乗ると B の identity が古い試行を名乗る。execution.md §1.4 末尾)。"""
        view, problem = self._execution_view(meta)
        return meta['current_execution_id'] if problem is None and view == _ex.ACTIVE else None

    # -- R-5: record を card に合わせる ---------------------------------------------
    def _r5(self, slug, tid, meta):
        """名指しの card の**今の試行**の record を、追従する欄 (status / end_code) だけ card に合わせる。
        無ければ card から作る。不変の欄 (agent / reserved_at / git.* …) は上書きしない。過去の試行の record は
        対象外 (凍結。execution.md §1.4)。読めない・identity が食い違う record は消さない・上書きしない (報告だけ)。"""
        if not _ex.has_execution_fields(meta):
            return
        problem = _ex.fields_problem(meta)
        if problem:
            self._emit('reported:execution_fields_invalid', slug, tid, detail=problem)
            return
        xid = meta['current_execution_id']
        got = self.t.read_execution_record(slug, xid)
        if got[0] == 'unreadable':
            self._emit('reported:execution_record_unreadable', slug, tid, detail=got[1])
            return
        if got[0] == 'absent':
            record = _ex.record_from_card(meta, slug, tid)
            if record['agent'] is None:
                self._emit('reported:execution_record_owner_unknown', slug, tid)
            files = self._do_writes(lambda: self.t.write_execution_record(slug, xid, record))
            self._emit('repaired:R-5', slug, tid, files=files,
                       from_status=meta.get('status'), to_status=meta.get('status'), execution_id=xid)
            return
        record = got[1]
        if _ex.record_problem(record, meta, slug, tid):
            self._emit('reported:execution_record_identity_mismatch', slug, tid)
            return
        if _ex.record_follows_card(record, meta):
            return
        if not self.apply:
            # store-check: 名指しされない card の record が遅れたまま残る (execution.md §9.5 表外 3)。数えるだけ。
            self._emit('reported:execution_record_stale', slug, tid)
            return
        updated = _ex.follow_card(record, meta)
        files = self._do_writes(lambda: self.t.write_execution_record(slug, xid, updated))
        self._emit('repaired:R-5', slug, tid, files=files,
                   from_status=meta.get('status'), to_status=meta.get('status'), execution_id=xid)

    def _execution_findings(self, named):
        """diagnose 専用の報告 (execution.md §4.2 / §7 / §9.5)。**書かない・拒否を足さない**。"""
        status_of_card = {}
        for slug, tid, meta in named:
            if not _ex.has_execution_fields(meta) or _ex.fields_problem(meta):
                continue
            view, _problem = self._execution_view(meta)
            status_of_card[(slug, tid)] = meta['current_execution_id']
            if view != _ex.DETACHED:
                continue
            if meta.get('execution_status') not in _ex.ACTIVE_STATUSES:
                # 試行はもう閉じている (`abandon_detached_execution` 済み・正常に終わった後に旧コードが取り直した)。
                # 閉じた試行を active と報告し続けない (消す手段が無い報告になる)
                continue
            status = meta.get('status')
            if status in _ASSIGNMENT_HOLDING_STATUSES:
                # (a): 空でない started_at が予約の値と違う (旧コードの pull が取り直した持ち主)
                self._emit('reported:execution_detached', slug, tid, detail=f"status={_safe_status(status) or '<unknown>'}")
            elif status in _status.RELEASED_WORK_STATUSES:
                # (b) で task が終わっている: 履歴だけの欠け。E4b の gate に混ぜない別コード (Director は
                # `abandon_detached_execution` で閉じられる)
                self._emit('reported:execution_active_on_finished_task', slug, tid,
                           detail=f"status={_safe_status(status) or '<unknown>'}")
            else:
                self._emit('reported:execution_active_on_non_holding_status', slug, tid,
                           detail=f"status={_safe_status(status) or '<unknown>'}")
        # current でない record が active のまま (card を手で編集した・lib を通らない書き込みがあった)
        for slug in dict.fromkeys(self.scope.add_missions):
            names, code = _list_dir(os.path.join(self.t._p('missions'), slug, 'executions'))
            if code:
                self._emit('reported:execution_record_unreadable', slug, detail=code)
                continue
            for name in names:
                if not name.endswith('.json') or not _ex.is_execution_id(name[:-5]):
                    continue
                xid = name[:-5]
                got = self.t.read_execution_record(slug, xid)
                if got[0] == 'unreadable':
                    self._emit('reported:execution_record_unreadable', slug, detail=got[1])
                    continue
                if got[0] != 'ok':
                    continue
                if _ex.record_shape_problem(got[1]):
                    # 欠けた欄・型の違う欄 (`status: []` 等) は集合判定に使わない (unhashable で store-check ごと落ちる)。
                    # 値は出さず、record の位置 (mission) だけを報告して次の record へ進む
                    self._emit('reported:execution_record_malformed', slug, detail=_ex.record_shape_problem(got[1]))
                    continue
                rec_task = got[1].get('task')
                if (got[1].get('status') in _ex.ACTIVE_STATUSES and isinstance(rec_task, str)
                        and (slug, rec_task) in status_of_card
                        and status_of_card[(slug, rec_task)] != xid):
                    self._emit('reported:execution_record_superseded_active', slug, rec_task)

    # -- R-1: projection を作る ---------------------------------------------------
    def _r1(self, slug, tid, meta):
        status = meta.get('status')
        worker = meta.get('worker')
        gen = meta.get('started_at')
        holding = status in _ASSIGNMENT_HOLDING_STATUSES
        if not holding:
            return
        xid = self._active_execution_id(meta)
        if not (isinstance(worker, str) and worker):
            # worker が無いと枠の持ち主を決められない。R-1 は書かない (報告のみ) が、
            # assignment を持つはずの status (in_progress / ready_for_verification / verifying /
            # needs_human_review) は全部 finding にする (S4 持ち越し: 以前は in_progress だけだった)
            if status == 'in_progress':
                self._emit('reported:in_progress_without_worker', slug, tid)
            else:
                self._emit('reported:holding_without_worker', slug, tid,
                           detail=f"status={_safe_status(status) or '<unknown>'}")
            return
        if agent_name_problem(worker) or _safe_token(worker) is None:
            # ファイル名として通っても、識別子の形でない worker 名 (空白・= 等) は内容とみなす:
            # その名前で枠を作らない・値も出さない
            self._emit('reported:invalid_worker_name', slug, tid)
            return
        slot = self._slot(worker)
        if slot[0] == 'unverifiable':
            if ('reported:assignment_unverifiable', None, None, _safe_token(worker)) not in self._seen:
                self._emit('reported:assignment_unverifiable', slug, tid, worker, detail=slot[1])
            return
        if slot[0] == 'ok':
            if slot[1] != f"{slug}:{tid}":
                self._emit('reported:agent_slot_busy', slug, tid, worker)   # 枠の中身は出さない
                return
            # .identity の検査は assignment を持つ status 全部 (S4 持ち越し: 以前は in_progress だけ)。
            # 欠け・壊れ・読めない・別の task を指す、はどれも finding (修復はしない = report-only。
            # identity → 本体の書き順なので、本体があって identity が無いのは crash では作れない)。
            if xid:
                verdict = self.t.classify_assignment(worker, slug, tid, execution_id=xid)
                if verdict == ASSIGN_SUCCESSOR:
                    self._emit('reported:generation_mismatch', slug, tid, worker)
                elif verdict == ASSIGN_UNVERIFIABLE:
                    self._emit('reported:assignment_unverifiable', slug, tid, worker,
                               detail='identity unreadable or malformed')
            elif self.t._read_identity(worker) is None:
                self._emit('reported:assignment_unverifiable', slug, tid, worker,
                           detail='identity unreadable or malformed')
            return
        # 枠が ABSENT。R-1 が書くのは in_progress だけ (crash で欠けが生まれるのは pull の窓だけ)
        if status != 'in_progress':
            self._emit('reported:holding_without_assignment', slug, tid, worker,
                       detail=f"status={_safe_status(status) or '<unknown>'}")
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
            self.t.publish_assignment(worker, slug, tid, str(gen), execution_id=xid)
        files = self._do_writes(_publish)
        self._emit('repaired:R-1', slug, tid, worker, files=files,
                   from_status=status, to_status=status, generation=str(gen),
                   execution_id=meta.get('current_execution_id'))

    def _ownership_problem(self, agent, slug, tid):
        """所有の証拠の走査 (§2.5)。`worker = agent` の in_progress card が全 mission でこの 1 枚だけと
        **読んで確かめた**ときだけ None。2 枚以上 → duplicate_owner、読めない card が 1 枚でもあれば
        owner_unprovable。archive は読まない。読むのは status と worker だけ。"""
        owned, unprovable, list_errors = [], [], []
        # missions/ と archive/ の両方を読む。archive も所有の証拠: `plan.sh archive` は mission / task の
        # status を検査せず rename するので、in_progress のカードを持ったまま archive された mission に
        # A のカードが残りうる。missions/ だけを数えると、残る 1 枚だけを A の全てと読んで枠を書く
        # (S4 / t016。設計 §2.5)。archive の card は `archive/<slug>/tasks/tNNN.md`。
        for base, prefix in (('missions', ''), ('archive', 'archive/')):
            base_dir = self.t._p(base)
            try:
                slugs = sorted(os.listdir(base_dir))
            except FileNotFoundError:
                slugs = []
            except OSError as e:
                return 'owner_unprovable', f"{base}: list_error:{_errno_name(e.errno)}"
            for s in slugs:
                tdir = os.path.join(base_dir, s, 'tasks')
                try:
                    names = sorted(os.listdir(tdir))
                except FileNotFoundError:
                    continue
                except OSError as e:
                    list_errors.append(f"{prefix}{s}/tasks: list_error:{_errno_name(e.errno)}")
                    continue
                for name in names:
                    m = _cards.TASK_FILENAME_RE.fullmatch(name)
                    if not m:
                        continue
                    t = f"t{m.group(1)}"
                    if prefix:
                        kind, meta, _b, reason, _e = self._archived_card(s, t)
                    else:
                        kind, meta, _b, reason, _e = self._card(s, t)
                    if kind == 'unreadable':
                        unprovable.append(f"{prefix}{s}/{t}")
                    elif kind == 'ok' and meta.get('worker') == agent and meta.get('status') == 'in_progress':
                        owned.append(f"{prefix}{s}/{t}")
        if list_errors:
            return 'owner_unprovable', list_errors[0]        # 形は「パス: コード」1 件 (ids と混ぜない)
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
            self._emit('reported:invalid_scope_name')       # 名前は出さない (呼び出し側の値)
            return
        text = _cards.read_regular_text_or_unreadable(mpath)
        if _cards.is_missing(text):
            return
        if _cards.is_unreadable(text):
            self._emit('reported:mission_unreadable', slug, detail=_unreadable_code(text))
            return
        try:
            mission = _cards.parse_yaml(text, source=mpath)
        except ValueError as e:
            self._emit('reported:mission_unreadable', slug, detail=_parse_code(e))
            return
        try:
            nxt = int(mission.get('next_task_id') or 1)
        except (TypeError, ValueError):
            self._emit('reported:next_task_id_invalid', slug)
            return
        nums = []
        tdir = os.path.join(self.t._p('missions'), slug, 'tasks')
        names, code = _list_dir(tdir)
        if code:
            self._emit('reported:tasks_dir_unreadable', slug, detail=code)
            return
        for name in names:
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
            self._emit('reported:invalid_scope_name')
            return
        try:
            state = self.t.load_state()
        except StoreReadError as e:
            self._emit('reported:state_unreadable', detail=e.reason)      # e.reason は固定コード
            return
        active = list(state.get('active_missions') or [])
        if slug not in active:
            return
        # 「missions/<slug> が無い・archive/<slug> が在る」を**両方とも観測して**確かめる。
        # lexists / isdir は EACCES でも False に潰す — 観測できないのに「移動済み」と読んで
        # active_missions から外すと、生きている mission を dispatcher から隠す。
        src = _path_state(self.t._p('missions', slug))
        dst = _path_state(self.t._p('archive', slug))
        if src == 'present':
            return
        if src == 'unobservable' or dst == 'unobservable':
            self._emit('reported:archive_state_unobservable', slug)
            return
        if dst == 'absent' or not os.path.isdir(self.t._p('archive', slug)):
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
    # 観測できなかった入力は finding として残す (P2-1: 空リスト = 健全、にしない)
    for rel, code in scope.unobservable:
        findings.append(Finding('unobservable_input', detail=_safe_detail(f"{rel}: {code}")))
    adir = os.path.join(queue_dir, 'assignments')
    names, code = _list_dir(adir)
    if code and ('assignments', code) not in scope.unobservable:
        findings.append(Finding('unobservable_input', detail=_safe_detail(f"assignments: {code}")))
    for name in names:
        if name.endswith(IDENTITY_SUFFIX):
            body = name[:-len(IDENTITY_SUFFIX)]
            state = _path_state(os.path.join(adir, body))
            if state == 'absent':
                findings.append(Finding('orphan_identity', agent=_safe_token(body)))
            elif state == 'unobservable':
                findings.append(Finding('unobservable_input', agent=_safe_token(body),
                                        detail='assignment body: lstat failed'))
    walk_errors = []
    for root, _dirs, files in os.walk(queue_dir, onerror=walk_errors.append):
        for f in files:
            if re.match(r'^\..+\.tmp\..+', f):
                findings.append(Finding('stale_tmp', detail=_safe_detail(
                    os.path.relpath(os.path.join(root, f), queue_dir))))
    for err in walk_errors:
        # os.walk は既定で列挙の失敗を黙って飛ばす → 残骸の走査が「見えた範囲だけ」になる
        findings.append(Finding('unobservable_input', detail=_safe_detail(
            f"{os.path.relpath(err.filename or queue_dir, queue_dir)}: list_error:{_errno_name(err.errno)}")))
    return findings


# ---------------------------------------------------------------------------
# ロック外の小さな共有ファイル (queue/.lock を取らないもの。S5 が使う)
# ---------------------------------------------------------------------------

def locked_update_json(path, lock_path, fn, *, on_unreadable='raise'):
    """専用ロック + 読み直し + 原子的書き込み。`fn(dict) -> dict`。ファイルが無い (ENOENT) ときだけ
    `{}` から始める。読めない・JSON でないは `StoreReadError` (空に潰さない)。
    queue/.lock を握ったままこれを呼ばない (ロックの順序: 準備ロック (`acquire_prepare_lock`。pull の準備を task ごとに
    直列化する 1 段) → queue/.lock → この小さな共有ファイルのロック。queue/.lock は S5 の小さな共有ファイルより外側で、
    準備ロックはその更に外側)。

    `on_unreadable='reset'` は、再生成できる**キャッシュ**専用 (taskvia map: 次の同期が冪等に埋める)。
    読めない・壊れているときに空から作り直す —— 正本には使わない (既定の 'raise' が、空に潰さない側)。"""
    if on_unreadable not in ('raise', 'reset'):
        raise ValueError(f'on_unreadable must be raise|reset, got {on_unreadable!r}')
    path, lock_path = os.fspath(path), os.fspath(lock_path)
    _ensure_dir(os.path.dirname(lock_path) or '.', lock_path)
    try:
        fd = _open_lock_file(lock_path)
    except OSError as e:
        raise LockFailed(f"cannot open {lock_path}: {e}") from e
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            raise LockFailed(f"cannot lock {lock_path}: {e}") from e
        text = _cards.read_regular_text_or_unreadable(path)
        problem = None
        if _cards.is_missing(text):
            data = {}
        elif _cards.is_unreadable(text):
            data, problem = {}, StoreReadError(path, _unreadable_code(text), text.errno)
        else:
            try:
                data = json.loads(text)
                parse_failed = False
            except ValueError:
                data, parse_failed = None, True
            if parse_failed:           # except の外で投げる (例外の連鎖に本文を残さない)
                data, problem = {}, StoreReadError(path, 'json_parse_error')
            elif not isinstance(data, dict):
                data, problem = {}, StoreReadError(path, 'JSON top-level is not an object')
        if problem is not None and on_unreadable == 'raise':
            raise problem
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


# ---------------------------------------------------------------------------
# 準備ロック (task ごと。pull の「ロック外の準備」を直列化する。execution.md §6.1 / 01c E2)
# ---------------------------------------------------------------------------

class PrepareLock:
    """`queue/missions/<slug>/executions/<tid>.prepare.lock` の flock (LOCK_EX | LOCK_NB)。

    持ち主が死ねば kernel が外す — **ただし、同じ開いたファイル記述を持つ子孫が生きている間は外れない** (`fileno()`・
    pull が準備の subprocess に渡す。execution.md §6.1)。ファイルの有無は意味を持たない。消さない (mission dir ごと動かす)。
    `release()` は冪等。with でも使える。"""

    def __init__(self, fd, path):
        self._fd = fd
        self.path = path

    def fileno(self):
        """ロックを持つ**開いたファイル記述**の番号。準備の subprocess (worktree を作る helper・git) に
        `pass_fds=(lock.fileno(),)` で渡す: flock は開いたファイル記述に付き、子孫が記述子を持っている間は
        親が SIGKILL されても外れない (親だけが死んで子孫が生き残る形で、排他の外で準備が動き続けない)。"""
        if self._fd is None:
            raise ValueError("released prepare lock")
        return self._fd

    def release(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def acquire_prepare_lock(queue_dir, slug, tid):
    """同じ task の pull の準備 (Taskvia・worktree・`.crewvia-env`) を直列化する準備ロックを**待たずに**取る。

    取れなければ `LockBusy` (同じ task の pull が準備中)。何も書かない (ロックファイルを作るだけ)。
    **`queue/.lock` を握ったまま取らない** (ロックの順序: 準備ロックは `queue/.lock` の外側の 1 段。
    このスレッドが `queue/.lock` を握っていれば `NestedTransaction`)。LOCK_NB なので、仮に逆順で取るコードが
    入っても待ちの輪にはならず `LockBusy` になる。"""
    queue_dir = os.fspath(queue_dir)
    key = _lock_key(os.path.join(queue_dir, '.lock'))
    with _HELD_GUARD:
        if _HELD.get(key) == threading.get_ident():
            raise NestedTransaction("準備ロックは queue/.lock を握ったまま取りません (ロックの順序)")
    path = os.path.join(queue_dir, 'missions', _check_slug(slug), 'executions', f"{_check_tid(tid)}.prepare.lock")
    _ensure_dir(os.path.dirname(path), path)
    try:
        fd = _open_lock_file(path)
    except OSError as e:
        raise LockFailed(f"cannot open prepare lock {path}: {e}") from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (_errno.EWOULDBLOCK, _errno.EAGAIN):
            raise LockBusy(f"prepare lock {path} is held by another pull") from e
        raise LockFailed(f"cannot lock {path}: {e}") from e
    return PrepareLock(fd, path)
