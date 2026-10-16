#!/usr/bin/env python3
"""lib_checkpoint.py — 退役の直前に worktree の状態を保存する (vNext 02 PR-G / P3)。

設計: `knowledge/vnext-02-attempt-reopen-checkpoint.md` §5。Worker が無言で止まって退役させられると、worktree の
未 commit の作業が失われかける (2026-10-04・10-05・10-11)。退役の実行者 (watchdog 側の `lib_retirement.py`) が
`plan.sh retire` の**前**にここを呼び、次の試行 (`previous_attempt.checkpoint_dir`)・Director が読めるように残す。

契約
----
* **worktree を読むだけ**。worktree・queue のどちらにも書かない (git は `diff` / `status` / `ls-files` / `hash-object` /
  `rev-parse` だけ。textconv・外部 diff・index の更新 (`--no-optional-locks`) を起動しない)。
* 置き場は `registry/checkpoints/<mission>/<tid>/<execution_id>/` (gitignored・dir 0700 / file 0600)。書き終えるまでは
  `<execution_id>.partial` に作り、**最後に rename** する (途中で落ちた dir が本物に見えない)。
* **秘密を生で残さない**: 秘密らしいファイル名は保存しない。保存する内容 (patch・untracked・pane・status) は全部 redaction
  を通し、**redaction が例外になったファイルは保存しない**。バイナリの内容は保存しない (圧縮・符号化された中の秘密を検出できない)。
* 失敗しても呼び出し側を止めない。`take()` は成功なら dir を、失敗なら `CheckpointError` (固定コード) を出し、途中の dir は消す。
  期限 (`deadline`) は cooperative: 各段の前に確かめ、git の subprocess の timeout は残り時間で切る。

CLI (観察用の手書き marker の手順・手動確認用):
    lib_checkpoint.py take   --registry-dir R --queue-dir Q --mission M --task tNNN --execution ex-… [--reason R] [--seconds 20]
    lib_checkpoint.py verify <dir> [--execution ex-…]
"""
import argparse
import fnmatch
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_daemon_state import load_json_store  # noqa: E402  (registry / queue の JSON の読み口。形の検証つき)
from lib_task_cards import Unreadable, is_missing  # noqa: E402

SCHEMA_VERSION = 1

#: dirty.patch の上限 (redaction の後で切る。秘密を途中で割って残さないため)
PATCH_LIMIT = 1024 * 1024
#: git の出力を読む上限 (redaction の対象の上限でもある。超えた分は捨てて truncated にする)
GIT_READ_LIMIT = 8 * 1024 * 1024
#: untracked の 1 ファイル / 合計の上限
UNTRACKED_FILE_LIMIT = 64 * 1024
UNTRACKED_TOTAL_LIMIT = 2 * 1024 * 1024
#: 列挙する untracked の件数の上限 (超えた分は件数だけ)
UNTRACKED_LISTING_LIMIT = 2000
#: バイナリ判定に使う先頭のバイト数
BINARY_SNIFF = 8 * 1024
PANE_LINES = 200
#: 1 回の git に許す時間の上限 (残り時間がこれより短ければ残り時間)
GIT_TIMEOUT = 10.0
DEFAULT_SECONDS = 20.0

FILE_MODE = 0o600
DIR_MODE = 0o700
PARTIAL_SUFFIX = '.partial'
META_NAME = 'meta.json'

_EXECUTION_ID_RE = re.compile(r'ex-[0-9a-f]{32}')

# 固定コード (メッセージ・ログにはこれと位置だけを出す。値・内容は出さない)
E_NO_WORKTREE = 'no_worktree'
E_RECORD = 'record_unreadable'
E_DEADLINE = 'deadline'
E_GIT = 'git_failed'
E_WRITE = 'write_failed'
E_EXISTS = 'exists_mismatch'
E_ARGS = 'invalid_argument'
E_VERIFY = 'verify_failed'
E_NOTHING = 'nothing_to_save'


class CheckpointError(Exception):
    """固定コード `code` が契約。メッセージに worktree の内容・秘密を入れない。"""

    def __init__(self, code, detail=''):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ---------------------------------------------------------------------------
# 秘密 (設計 §5.3)
# ---------------------------------------------------------------------------

#: ファイル名 (パスの各成分に当てる。大文字小文字は区別しない)。security.md の一覧と同じ
SECRET_NAME_GLOBS = ('.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', '*_rsa', '*_dsa', '*_ed25519',
                     '*credentials*', '*.secret', '*.secret.*', '*.secrets', '*.secrets.*')


def is_secret_name(path):
    """パスのどの成分 (dir 名を含む) が秘密らしいファイル名に当たるか。dir 名まで見るのは安全側 (取りすぎる側) に倒すため。"""
    for part in re.split(r'[\\/]+', str(path)):
        low = part.lower()
        if low and any(fnmatch.fnmatchcase(low, g) for g in SECRET_NAME_GLOBS):
            return True
    return False


# 量指定子を入れ子にしない・上限つき (1MB の 1 行でも線形。tests で通す)。値の側だけを置き換える形は group 2
_BEGIN_KEY = re.compile(r'-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----')
_END_KEY_RE = re.compile(r'-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----')
_TOKEN_PATTERNS = (
    re.compile(r'op://[^\s\'"<>)\]]{1,300}'),
    re.compile(r'(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,255}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{20,255}'),
    re.compile(r'sk-[A-Za-z0-9_-]{16,255}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'xox[abprs]-[A-Za-z0-9-]{10,255}'),
)
_BEARER = re.compile(r'(Bearer[ \t]{1,8})([A-Za-z0-9._~+/=-]{16,512})')
_ASSIGN = re.compile(
    r'((?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)[A-Za-z0-9_]{0,40}["\']?[ \t]{0,8}[=:][ \t]{0,8})'
    r'("(?:[^"\\\n]|\\.){0,1000}"|\'(?:[^\'\\\n]|\\.){0,1000}\'|["\'][^\n]{0,1000}|\S{1,1000})',
    re.IGNORECASE)


def redact(text):
    """`(redacted, count)`。置き換えは `[REDACTED:<n>]` (n は 1 から。このファイルの中の通し番号)。例外はそのまま出す
    (呼び出し側が「そのファイルを保存しない」に倒す)。"""
    n = 0
    out = []
    pos = 0
    # 1. 秘密鍵: BEGIN から END まで (END が無ければ末尾まで)。regex の `.*?` を使わず find で線形にする
    while True:
        m = _BEGIN_KEY.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        n += 1
        out.append(f"[REDACTED:{n}]")
        e = _END_KEY_RE.search(text, m.end())
        if not e:
            break
        pos = e.end()
    text = ''.join(out)

    def _whole(_m):
        nonlocal n
        n += 1
        return f"[REDACTED:{n}]"

    def _value(m):
        nonlocal n
        n += 1
        return f"{m.group(1)}[REDACTED:{n}]"

    for pat in _TOKEN_PATTERNS:
        text = pat.sub(_whole, text)
    text = _BEARER.sub(_value, text)
    text = _ASSIGN.sub(_value, text)
    return text, n


# ---------------------------------------------------------------------------
# git (読み取りだけ)
# ---------------------------------------------------------------------------

_GIT_ENV_DROP = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                 'GIT_COMMON_DIR', 'GIT_NAMESPACE', 'GIT_EXTERNAL_DIFF', 'GIT_PAGER', 'GIT_CONFIG', 'GIT_CONFIG_GLOBAL',
                 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_COUNT')


def _git_env():
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP and not k.startswith('GIT_CONFIG_')}
    # global / system の設定 (LFS 等の filter.*) と属性は読まない。repo ローカルの filter は `_refuse_local_filters` が止める
    env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_ATTR_NOSYSTEM='1')
    env['GIT_OPTIONAL_LOCKS'] = '0'
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['LC_ALL'] = 'C'
    return env


class _Clock:
    def __init__(self, seconds, now=None):
        self._now = now or time.monotonic
        self.deadline = self._now() + float(seconds)

    def remaining(self):
        return self.deadline - self._now()

    def check(self):
        if self.remaining() <= 0:
            raise CheckpointError(E_DEADLINE)


def _kill_group(proc):
    """git を別セッションで起動しているので、子 (filter・ssh 等) ごとプロセスグループで止める。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_git(clock, worktree, *args, limit=GIT_READ_LIMIT, check=True):
    """`(returncode, stdout_bytes, truncated)`。timeout は残り時間 (上限 GIT_TIMEOUT)。失敗は `E_GIT` (check=True のとき)。"""
    clock.check()
    timeout = max(0.1, min(GIT_TIMEOUT, clock.remaining()))
    argv = ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=/dev/null',
            '-C', worktree, *args]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                env=_git_env(), start_new_session=True)
    except OSError:
        raise CheckpointError(E_GIT, 'spawn')
    chunks, size, truncated = [], 0, False
    fd = proc.stdout.fileno()
    hard_stop = clock._now() + timeout
    try:
        while True:
            left = min(clock.remaining(), hard_stop - clock._now())
            if left <= 0:
                _kill_group(proc)
                proc.wait()
                raise CheckpointError(E_DEADLINE if clock.remaining() <= 0 else E_GIT, 'timeout')
            ready, _, _ = select.select([fd], [], [], min(0.2, left))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                truncated = True
                _kill_group(proc)
                break
        proc.wait()
        data = b''.join(chunks)[:limit]
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
    if check and not truncated and proc.returncode != 0:
        raise CheckpointError(E_GIT, f"exit {proc.returncode}")
    return proc.returncode, data, truncated


def _git_text(clock, worktree, *args, limit=GIT_READ_LIMIT):
    rc, data, truncated = _run_git(clock, worktree, *args, limit=limit)
    return data.decode('utf-8', errors='replace'), truncated


# ---------------------------------------------------------------------------
# 出力の書き込み (0700 / 0600・symlink を辿らない)
# ---------------------------------------------------------------------------

def _mkdir(path):
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    os.chmod(path, DIR_MODE)


def _write_file(path, data):
    """新規作成だけ (O_EXCL | O_NOFOLLOW)。str は UTF-8。"""
    if isinstance(data, str):
        data = data.encode('utf-8')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_tree(path):
    """自分が作った dir の掃除 (partial 専用)。`rm -rf` ではなく、symlink を辿らず 1 つずつ unlink / rmdir する。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return
    for name in os.listdir(path):
        child = os.path.join(path, name)
        cst = os.lstat(child)
        if stat.S_ISDIR(cst.st_mode):
            remove_tree(child)
        else:
            os.unlink(child)
    os.rmdir(path)


def _truncate_bytes(text, limit):
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text, len(raw), False
    return raw[:limit].decode('utf-8', errors='ignore'), len(raw), True


# ---------------------------------------------------------------------------
# patch の秘密ファイルの hunk を落とす
# ---------------------------------------------------------------------------

def _drop_secret_sections(patch):
    """`(patch, 落とした件数)`。`diff --git` ごとの節の見出し行 (と rename 行) のパスの成分が秘密らしければ節ごと落とす。"""
    if not patch:
        return patch, 0
    sections = re.split(r'(?m)^(?=diff --git )', patch)
    kept, dropped = [], 0
    for sec in sections:
        if not sec.startswith('diff --git '):
            kept.append(sec)
            continue
        head = sec.split('\n', 1)[0]
        names = [head]
        for line in sec.split('\n', 12)[1:12]:
            if line.startswith(('--- ', '+++ ', 'rename from ', 'rename to ', 'copy from ', 'copy to ')):
                names.append(line)
        if any(is_secret_name(s) for h in names for s in re.split(r'[ "]+', h)):
            dropped += 1
            continue
        kept.append(sec)
    return ''.join(kept), dropped


# ---------------------------------------------------------------------------
# take
# ---------------------------------------------------------------------------

class NothingToSave(CheckpointError):
    """保存するものが元から無い (試行の record が無い・まだ worktree を作っていない)。失敗ではない: 呼び出し側は通知しない。"""

    def __init__(self, detail=''):
        super().__init__(E_NOTHING, detail)


def checkpoint_dir_of(registry_dir, mission, tid, execution_id):
    if not _EXECUTION_ID_RE.fullmatch(execution_id or ''):
        raise CheckpointError(E_ARGS, 'execution_id')
    for label, value in (('mission', mission), ('task', tid)):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', value) or '..' in value:
            raise CheckpointError(E_ARGS, label)
    return os.path.join(os.fspath(registry_dir), 'checkpoints', mission, tid, execution_id)


def worktree_of_record(queue_dir, mission, execution_id):
    """record の `git.worktree` (ペインの有無に依存しない)。読めない / 無い / 実在しない dir は `CheckpointError`。"""
    checkpoint_dir_of('', mission, 't000', execution_id)        # 名前の検査だけ (パスの部品にしてよい形か)
    # mission / execution_id の形は `checkpoint_dir_of` が検査済み (パスの部品として安全)
    record_path = os.path.join(os.fspath(queue_dir), 'missions', mission, 'executions', f"{execution_id}.json")
    record = load_json_store(record_path)
    if is_missing(record):
        raise NothingToSave('record_absent')
    if isinstance(record, Unreadable):
        raise CheckpointError(E_RECORD, 'unreadable')
    git = record.get('git')
    path = git.get('worktree') if isinstance(git, dict) else None
    if path is None:
        raise NothingToSave('worktree_not_recorded')        # まだ worktree を作る前に終わった試行
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise CheckpointError(E_NO_WORKTREE, 'record')
    if not os.path.isdir(path):
        raise CheckpointError(E_NO_WORKTREE, 'missing')
    return path


def take(*, registry_dir, queue_dir, mission, task, execution_id, reason='unknown', pane_text=None,
         seconds=DEFAULT_SECONDS, now=None, worktree=None):
    """checkpoint を保存して dir を返す。失敗は `CheckpointError` (途中の dir は消してある)。

    同じ試行の dir が既にあり、`verify` を通れば**それを返す** (退役の再試行が毎サイクル撮り直さない)。
    `worktree` は試験用 (無ければ record から)。`pane_text` は呼び出し側がペインが生きているときだけ渡す。
    """
    clock = _Clock(seconds, now)
    final = checkpoint_dir_of(registry_dir, mission, task, execution_id)
    if os.path.lexists(final):
        try:
            verify(final, execution_id)
        except CheckpointError:
            raise CheckpointError(E_EXISTS)
        return final
    if worktree is None:
        worktree = worktree_of_record(queue_dir, mission, execution_id)
    worktree_real = os.path.realpath(worktree)
    partial = final + PARTIAL_SUFFIX
    try:
        _mkdir(os.path.dirname(final))
        if os.path.lexists(partial):
            remove_tree(partial)
        _mkdir(partial)
        meta = _build(clock, partial, worktree_real, mission, task, execution_id, reason, pane_text, now)
        _write_file(os.path.join(partial, META_NAME), json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
        clock.check()
        os.rename(partial, final)
    except CheckpointError:
        _discard(partial)
        raise
    except Exception as e:  # noqa: BLE001 — 退役を止めない側に倒す。型名だけ残す
        _discard(partial)
        raise CheckpointError(E_WRITE, type(e).__name__)
    return final


def _discard(path):
    try:
        remove_tree(path)
    except OSError:
        pass


def _refuse_local_filters(clock, worktree):
    """repo の有効な設定 (ローカル・include・config.worktree。global / system は読まない) に `filter.<name>.*` (clean / smudge / process) があれば保存しない。`git diff` / `status` は index と
    比べるとき clean filter を**起動する**ので、読み取りだけという契約を守れない (`--no-textconv` は filter を止めない)。
    global / system の設定は環境変数で読まない。設定が読めない・コマンドが失敗しても `check=False` で「無い」と読まず止める。"""
    rc, out, _ = _run_git(clock, worktree, 'config', '--includes', '--get-regexp', r'^filter\.', check=False)
    if rc == 0 and out.strip():
        raise CheckpointError(E_GIT, 'local_filters')
    if rc not in (0, 1):                         # 1 = 該当なし。それ以外は観測できなかった
        raise CheckpointError(E_GIT, 'config_unreadable')


def _build(clock, out, worktree, mission, task, execution_id, reason, pane_text, now):
    """保存物を `out` に書いて meta (dict) を返す。"""
    redactions = 0
    skipped_redaction = 0
    files = {}

    _refuse_local_filters(clock, worktree)
    branch = _git_text(clock, worktree, 'rev-parse', '--abbrev-ref', 'HEAD')[0].strip() or None
    head_rc, head_raw, _ = _run_git(clock, worktree, 'rev-parse', 'HEAD', check=False)
    head = head_raw.decode('ascii', errors='replace').strip() if head_rc == 0 else None

    # status.txt
    status_raw, status_trunc = _git_text(clock, worktree, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                                    '--ignore-submodules=all')
    dirty = bool(status_raw.strip('\x00'))
    saved, n = _save_text(out, 'status.txt', status_raw, limit=PATCH_LIMIT)
    redactions += n
    skipped_redaction += 0 if saved else 1
    files['status'] = _file_info(saved, status_trunc)

    # dirty.patch
    patch_raw, patch_trunc = _git_text(clock, worktree, 'diff', 'HEAD', '--no-ext-diff', '--no-textconv', '--no-color',
                                    '--ignore-submodules=all')
    patch_raw, secret_dropped = _drop_secret_sections(patch_raw)
    saved, n = _save_text(out, 'dirty.patch', patch_raw, limit=PATCH_LIMIT) if patch_raw else (False, 0)
    redactions += n
    info = _file_info(saved, patch_trunc)
    info.update(secret_files_dropped=secret_dropped)
    if patch_raw and not saved:
        skipped_redaction += 1
    files['dirty_patch'] = info

    # binary-files.txt (tracked の変更 + untracked)
    binary_lines = []
    clock.check()
    numstat, _ = _git_text(clock, worktree, 'diff', 'HEAD', '--numstat', '-z', '--no-ext-diff', '--no-textconv',
                              '--ignore-submodules=all')
    secret_excluded = secret_dropped
    for path in _numstat_binary_paths(numstat):
        if is_secret_name(path):
            secret_excluded += 1
            continue
        binary_lines.append(_binary_line(clock, worktree, path))

    # untracked
    untracked_info, ubinary, usecret, uredact, uskip = _save_untracked(clock, out, worktree)
    redactions += uredact
    skipped_redaction += uskip
    secret_excluded += usecret
    binary_lines.extend(ubinary)
    files['untracked'] = untracked_info

    if binary_lines:
        _write_file(os.path.join(out, 'binary-files.txt'), '\n'.join(binary_lines) + '\n')

    # pane.txt
    if pane_text:
        clock.check()
        tail = '\n'.join(pane_text.splitlines()[-PANE_LINES:])
        saved, n = _save_text(out, 'pane.txt', tail + '\n', limit=PATCH_LIMIT)
        redactions += n
        skipped_redaction += 0 if saved else 1
        files['pane'] = {'saved': bool(saved), 'lines': min(PANE_LINES, len(pane_text.splitlines()))}
    else:
        files['pane'] = {'saved': False, 'lines': 0}

    clock.check()
    return {
        'schema_version': SCHEMA_VERSION,
        'execution_id': execution_id,
        'mission': mission,
        'task': task,
        'taken_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now() if now else time.time())),
        'reason': reason,
        'worktree': worktree,
        'branch': branch,
        'head': head,
        'dirty': dirty,
        'files': files,
        'binary_files': len(binary_lines),
        'binary_restorable': False,
        'secret_files_excluded': secret_excluded,
        'redactions': redactions,
        'redaction_failed_files': skipped_redaction,
    }


def _file_info(saved, truncated_at_read):
    return {'saved': bool(saved), 'truncated': bool(saved and saved.get('truncated')) or bool(truncated_at_read),
            'original_size': saved.get('original_size') if saved else None,
            'saved_size': saved.get('saved_size') if saved else None}


def _save_text(out, name, text, *, limit):
    """redaction → 切り詰め → 書き込み。`(info | None, 置換の件数)`。redaction が例外なら保存しない (info=None)。"""
    try:
        red, count = redact(text)
    except Exception:  # noqa: BLE001 — 生のまま残さない側に倒す
        return None, 0
    body, original, truncated = _truncate_bytes(red, limit)
    _write_file(os.path.join(out, name), body)
    return {'truncated': truncated, 'original_size': original, 'saved_size': len(body.encode('utf-8'))}, count


def _numstat_binary_paths(numstat):
    """`git diff --numstat -z` の `-\t-\t<path>` (rename は `-\t-\t\0old\0new\0`)。"""
    parts = numstat.split('\x00')
    i = 0
    paths = []
    while i < len(parts):
        rec = parts[i]
        i += 1
        if not rec:
            continue
        fields = rec.split('\t', 2)
        if len(fields) < 3:
            continue
        added, deleted, path = fields
        if path == '' and i + 1 < len(parts):      # rename / copy: 続く 2 つが old / new
            path = parts[i + 1]
            i += 2
        if added == '-' and deleted == '-' and path:
            paths.append(path)
    return paths


def _blob_sha(clock, worktree, path):
    try:
        rc, data, _ = _run_git(clock, worktree, 'hash-object', '--no-filters', '--', path, check=False)
    except CheckpointError as e:
        if e.code == E_DEADLINE:
            raise
        return '?'
    return data.decode('ascii', errors='replace').strip() if rc == 0 and data else '?'


def _binary_line(clock, worktree, path):
    full = os.path.join(worktree, path)
    try:
        st = os.lstat(full)
        size = st.st_size if stat.S_ISREG(st.st_mode) else -1
        sha = _blob_sha(clock, worktree, path) if stat.S_ISREG(st.st_mode) else '?'
    except OSError:
        size, sha = -1, '?'
    return f"{path}\t{size}\t{sha}"


def _save_untracked(clock, out, worktree):
    """`(info, binary_lines, secret_excluded, redactions, redaction_failed)`。"""
    raw, _ = _git_text(clock, worktree, 'ls-files', '--others', '--exclude-standard', '-z')
    names = [p for p in raw.split('\x00') if p]
    saved_names, listed, binary_lines = [], [], []
    secret_excluded = redactions = redaction_failed = 0
    total = 0
    omitted = max(0, len(names) - UNTRACKED_LISTING_LIMIT)
    for rel in names[:UNTRACKED_LISTING_LIMIT]:
        clock.check()
        if rel.endswith('/') or os.path.isabs(rel) or '..' in rel.split('/'):
            listed.append({'path': rel, 'reason': 'not_a_file'})
            continue
        if is_secret_name(rel):
            secret_excluded += 1
            continue
        full = os.path.join(worktree, rel)
        try:
            if not os.path.realpath(os.path.dirname(full)).startswith(worktree.rstrip('/') + '/') \
                    and os.path.realpath(os.path.dirname(full)) != worktree:
                listed.append({'path': rel, 'reason': 'outside_worktree'})
                continue
            st = os.lstat(full)
        except OSError:
            listed.append({'path': rel, 'reason': 'unreadable'})
            continue
        if stat.S_ISLNK(st.st_mode):
            listed.append({'path': rel, 'reason': 'symlink'})
            continue
        if not stat.S_ISREG(st.st_mode):
            listed.append({'path': rel, 'reason': 'not_regular'})
            continue
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            listed.append({'path': rel, 'reason': 'unreadable'})
            continue
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode):
                listed.append({'path': rel, 'reason': 'not_regular'})
                continue
            head = os.read(fd, BINARY_SNIFF)
            if b'\x00' in head:
                binary_lines.append(f"{rel}\t{fst.st_size}\t{_blob_sha(clock, worktree, rel)}")
                continue
            if fst.st_size > UNTRACKED_FILE_LIMIT:
                listed.append({'path': rel, 'reason': 'too_large', 'size': fst.st_size})
                continue
            if total + fst.st_size > UNTRACKED_TOTAL_LIMIT:
                listed.append({'path': rel, 'reason': 'total_limit', 'size': fst.st_size})
                continue
            body = head + os.read(fd, UNTRACKED_FILE_LIMIT + 1)
        finally:
            os.close(fd)
        if len(body) > UNTRACKED_FILE_LIMIT:
            listed.append({'path': rel, 'reason': 'too_large', 'size': len(body)})
            continue
        try:
            red, count = redact(body.decode('utf-8', errors='replace'))
        except Exception:  # noqa: BLE001
            redaction_failed += 1
            listed.append({'path': rel, 'reason': 'redaction_failed'})
            continue
        dest = os.path.join(out, 'untracked', rel)
        _mkdir(os.path.dirname(dest))
        _write_file(dest, red)
        total += len(red.encode('utf-8'))
        redactions += count
        saved_names.append(rel)
    info = {'saved': len(saved_names), 'listed_only': listed, 'omitted_beyond_listing_limit': omitted,
            'total_bytes': total}
    return info, binary_lines, secret_excluded, redactions, redaction_failed


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def verify(path, execution_id=None):
    """dir が checkpoint として読めて、`meta.json` の `execution_id` が一致するか。通れば meta を返す。

    `execution_id` を渡せば、それと meta の ID・dir 名が全部一致しなければならない (別の試行の dir を指せない)。
    渡さなければ meta と dir 名の一致だけ。symlink の dir / meta は拒否。
    """
    path = os.fspath(path)
    if not os.path.isabs(path):
        raise CheckpointError(E_VERIFY, 'not_absolute')
    try:
        st = os.lstat(path)
    except OSError:
        raise CheckpointError(E_VERIFY, 'missing')
    if not stat.S_ISDIR(st.st_mode):
        raise CheckpointError(E_VERIFY, 'not_a_dir')
    meta_path = os.path.join(path, META_NAME)
    try:
        mst = os.lstat(meta_path)
    except OSError:
        raise CheckpointError(E_VERIFY, 'meta_unreadable')
    if not stat.S_ISREG(mst.st_mode):
        raise CheckpointError(E_VERIFY, 'meta_not_regular')
    meta = load_json_store(meta_path)
    if isinstance(meta, Unreadable):
        raise CheckpointError(E_VERIFY, 'meta_unreadable')
    if not isinstance(meta, dict) or not _EXECUTION_ID_RE.fullmatch(str(meta.get('execution_id') or '')):
        raise CheckpointError(E_VERIFY, 'meta_shape')
    if meta['execution_id'] != os.path.basename(path.rstrip('/')):
        raise CheckpointError(E_VERIFY, 'dir_name_mismatch')
    if execution_id is not None and meta['execution_id'] != execution_id:
        raise CheckpointError(E_VERIFY, 'execution_mismatch')
    return meta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='verb', required=True)
    t = sub.add_parser('take')
    t.add_argument('--registry-dir', required=True)
    t.add_argument('--queue-dir', required=True)
    t.add_argument('--mission', required=True)
    t.add_argument('--task', required=True)
    t.add_argument('--execution', required=True)
    t.add_argument('--reason', default='manual')
    t.add_argument('--seconds', type=float, default=DEFAULT_SECONDS)
    v = sub.add_parser('verify')
    v.add_argument('dir')
    v.add_argument('--execution', default=None)
    args = ap.parse_args(argv)
    try:
        if args.verb == 'take':
            d = take(registry_dir=args.registry_dir, queue_dir=args.queue_dir, mission=args.mission, task=args.task,
                     execution_id=args.execution, reason=args.reason, seconds=args.seconds)
            print(d)
        else:
            verify(args.dir, args.execution)
            print('ok')
    except CheckpointError as e:
        print(f"[lib_checkpoint] {e.code}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
