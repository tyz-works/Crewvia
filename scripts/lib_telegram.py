#!/usr/bin/env python3
"""lib_telegram.py — Telegram の経路 (Director の質問・ボタンの答え・受信側の状態)。PR-A。

設計: `knowledge/director-escalation-telegram.md` (§1 受信・§1-1 認証情報・§2 質問台帳・§3 転送・§4 ask)。

## 守ること (設計から)

* **token はプロセスの argv にも、ログにも、例外文にも、状態ファイルにも出さない。** Bot API は
  Python の `urllib` をプロセス内で呼ぶ (curl の引数に token が出ない)。失敗は例外ではなく
  **固定コードの値**で返す (`ApiResult.error`)。例外の文字列を出さない (URL に token が入る)。
* **認証情報の解決は `resolve_credentials()` の 1 か所** (§1-1)。config の `telegram.credentials.source`
  (`op` | `file`) が指す 1 つの取り出し方からだけ。`CREWVIA_TG_*` の env は**読まない**。
  dispatcher が起動時に取り出した値は bash の前置代入で `_CREWVIA_TG_RESOLVED_*` に載って届く
  (`carried=True` の動詞だけが読む。`ask` は読まない)。
* **未設定なら 1 バイトも書かない** (受信側の状態ファイルが既にあるときの `enabled: false` への書き換えだけが例外)。
* 状態ファイルの読みは `lib_daemon_state.load_json_store` の入口、書きは `told_lock` + `write_told_atomic`。
  壊れていたら (`Unreadable`) 転送しない・offset を進めない・`ask` は断る (観測できなかったことを
  「答え無し」や「有効」に倒さない)。
* 判定は**純粋関数** (`classify_update` / `sweep_questions` / `receiver_verdict` / `parse_callback_data` …) にして
  I/O から切り離す。テストは偽の Bot API サーバー (`api_base` 引数) と純粋関数の網羅で。
"""

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_task_cards import (  # noqa: E402
    CORRUPT_TASK_STATUS, is_missing, is_unreadable, read_regular_text_or_unreadable, read_task_card,
)
from lib_daemon_state import (  # noqa: E402
    is_finite_number, load_json_store, told_lock, write_told_atomic,
    telegram_offset_problem, telegram_question_entry_problem, telegram_questions_problem,
    telegram_receiver_problem, telegram_send_problem,
)

try:
    import yaml
except ImportError:  # pragma: no cover — PyYAML が無い環境では config は既定値
    yaml = None

API_BASE_DEFAULT = 'https://api.telegram.org'

#: dispatcher が受信側の状態を書き直す間隔 (変化が無くても)。`poll_interval` と独立の定数 (§1-1 / t014 P2-1)。
RECEIVER_HEARTBEAT_SECONDS = 30
#: `ask` が「古い」と見るしきい値 = 心拍の 3 倍 (心拍から導く)。
RECEIVER_STALE_SECONDS = 3 * RECEIVER_HEARTBEAT_SECONDS

DEFAULT_POLL_INTERVAL_SECONDS = 10
DEFAULT_QUESTION_TTL_MINUTES = 60
MAX_OPEN_QUESTIONS = 8
NULL_MESSAGE_GRACE_SECONDS = 300           # message_id が null のまま残った open の猶予 (§2-3b)
CLOSED_RETENTION_SECONDS = 7 * 24 * 3600   # answered / withdrawn / expired を残す期間
FORWARD_GIVE_UP_SECONDS = 24 * 3600        # 答えを Director に転送できなかった場合に諦める (§2-3)
LEDGER_LOCK_WAIT_SECONDS = 2.0
API_TIMEOUT_SECONDS = 5
#: poll の 1 回の通信の持ち時間 (dispatcher のサブプロセスの上限 `timeout 8` より短く)。受信 (getUpdates) は最初に走り、
#: 後始末 (answerCallbackQuery・ボタンを消す・諦めの通知) は**残りだけ**を使う (§2-3d)。
POLL_BUDGET_SECONDS = 6.5
BUDGET_MIN_REMAINING_SECONDS = 1.0          # 残りがこれ未満なら後始末の通信は次のサイクルに回す
CLEANUP_MAX_PER_CYCLE = 3                   # 1 サイクルで後始末に使う通信の件数 (種類ごと)
UNBUTTON_MAX_TRIES = 5                      # 一時的な失敗でボタンを消す試みを諦める回数
UNBUTTON_GIVE_UP_SECONDS = 3600             # 閉じてからこれを過ぎたら諦める
#: ボタンを消せないことが確定する Bot API の応答 (編集できない・消えたメッセージ・bot が外された)。待っても直らない。
UNBUTTON_PERMANENT_ERRORS = frozenset({'api_400', 'api_403', 'api_404'})
OP_TIMEOUT_SECONDS = 15
MIN_SEND_INTERVAL_SECONDS = 1.0
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 600
WARN_INTERVAL_SECONDS = 600

LABEL_MAX = 40
QUESTION_MAX = 1200
REASON_MAX = 200
MESSAGE_MAX = 2000
ANSWER_TEXT_MAX = 500

CARRIED_TOKEN_VAR = '_CREWVIA_TG_RESOLVED_TOKEN'
CARRIED_CHAT_VAR = '_CREWVIA_TG_RESOLVED_CHAT_ID'

QUESTIONS_FILE = 'telegram-questions.json'
OFFSET_FILE = 'telegram-offset.json'
SEND_FILE = 'telegram-send.json'
RECEIVER_FILE = 'telegram-receiver.json'
POLL_LOCK_FILE = 'telegram-poll.lock'

_TOKEN_RE = re.compile(r'^[0-9]{1,15}:[A-Za-z0-9_-]{10,200}$')
_CHAT_RE = re.compile(r'^-?[0-9]{1,20}$')
_CALLBACK_RE = re.compile(r'^(q-[0-9a-f]{8})\.([0-9a-f]{6})\.([0-9]{1,2})$')
_SLUG_RE = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._-]{0,100}$')
_TASK_RE = re.compile(r'^t[0-9]{3,6}$')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

def repo_root():
    env = os.environ.get('CREWVIA_REPO_ROOT')
    return Path(env) if env else Path(__file__).resolve().parent.parent


def default_config_path():
    return repo_root() / 'config' / 'crewvia.yaml'


def default_registry_dir():
    return repo_root() / 'registry' / 'daemons'


def default_queue_dir():
    env = os.environ.get('CREWVIA_QUEUE')
    return Path(env) if env else repo_root() / 'queue'


def _positive_number(value, default):
    if isinstance(value, bool) or not is_finite_number(value) or value <= 0:
        return default
    return value


def load_telegram_config(config_path=None):
    """`config/crewvia.yaml` の `telegram:` ブロック。読めない・欠けは既定値 (= 未設定)。

    秘密は入れない: `credentials.token_ref` / `chat_id_ref` は `op://…` の**参照**、`credentials.file` はパス。
    """
    cfg = {
        'poll_interval_seconds': DEFAULT_POLL_INTERVAL_SECONDS,
        'question_ttl_minutes': DEFAULT_QUESTION_TTL_MINUTES,
        'session_link': '',
        'credentials': {'source': '', 'token_ref': '', 'chat_id_ref': '', 'file': '', 'op_command': 'op'},
    }
    if yaml is None:
        return cfg
    path = default_config_path() if config_path is None else config_path
    raw = read_regular_text_or_unreadable(path)
    if is_unreadable(raw):
        return cfg
    try:
        loaded = yaml.safe_load(raw) or {}
    except Exception:  # noqa: BLE001 — config の壊れは「未設定」に倒す (何も送らない側)
        return cfg
    block = loaded.get('telegram') if isinstance(loaded, dict) else None
    if not isinstance(block, dict):
        return cfg
    cfg['poll_interval_seconds'] = _positive_number(block.get('poll_interval_seconds'), cfg['poll_interval_seconds'])
    cfg['question_ttl_minutes'] = _positive_number(block.get('question_ttl_minutes'), cfg['question_ttl_minutes'])
    link = block.get('session_link')
    if isinstance(link, str):
        cfg['session_link'] = link.strip()
    creds = block.get('credentials')
    if isinstance(creds, dict):
        for key in ('source', 'token_ref', 'chat_id_ref', 'file', 'op_command'):
            value = creds.get(key)
            if isinstance(value, str) and value.strip():
                cfg['credentials'][key] = value.strip()
    return cfg


# ---------------------------------------------------------------------------
# 認証情報 — 解決は resolve_credentials() の 1 か所 (§1-1)
# ---------------------------------------------------------------------------

class Credentials:
    """bot token と chat_id。`repr` / `str` に値を出さない (ログ・例外文に混ざっても漏れない)。"""

    __slots__ = ('token', 'chat_id')

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def __repr__(self):
        return '<Credentials redacted>'

    __str__ = __repr__

    @property
    def bot_id(self):
        return int(self.token.split(':', 1)[0])

    @property
    def chat_hash(self):
        return hashlib.sha256(self.chat_id.encode('utf-8')).hexdigest()[:12]


def _validated(token, chat_id):
    if not (isinstance(token, str) and _TOKEN_RE.fullmatch(token) and isinstance(chat_id, str) and _CHAT_RE.fullmatch(chat_id)):
        return None, 'credential_invalid'
    return Credentials(token, chat_id), 'ok'


def _read_file_credentials(path, euid):
    """0600 のファイル (`bot_token=…` / `chat_id=…`)。権限が合わなければ**中身を開かない**。"""
    try:
        st = os.lstat(path)
    except OSError:
        return None, 'credential_file_missing'
    # シンボリックリンクは辿らない・通常ファイルだけ・所有者は自分・group / other のビットが 0 (0400 も通す。t016 P3-2)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != euid or (st.st_mode & 0o077):
        return None, 'credential_file_permissions'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, 'credential_file_permissions'
    try:
        fst = os.fstat(fd)
        if (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
            return None, 'credential_file_permissions'
        with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as fh:
            fd = None
            text = fh.read(65536)
    except OSError:
        return None, 'credential_file_missing'
    finally:
        if fd is not None:
            os.close(fd)
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        values[key.strip()] = value.strip().strip('"\'')
    return _validated(values.get('bot_token', ''), values.get('chat_id', ''))


def _read_op(ref, op_command, runner):
    if not ref.startswith('op://'):
        return None
    try:
        proc = runner([op_command, 'read', ref], capture_output=True, text=True,
                      timeout=OP_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 — 見つからない・timeout・何でも固定コードへ (stderr / 例外文は出さない)
        return None
    if proc.returncode != 0:
        return None
    value = (proc.stdout or '').strip()
    return value or None


def resolve_credentials(config, environ=None, *, carried=False, runner=subprocess.run, euid=None):
    """→ `(Credentials | None, reason)`。reason は固定コード。**唯一の解決の入口** (§1-1)。

    * `source` が未設定・不正 → `(None, 'no_credentials')` (何もしない側)。
    * `source=op`: `op read <ref>` を呼ぶ (`telegram.credentials.op_command`、既定 `op`)。`file` は読まない。
    * `source=file`: リポジトリ外の 0600 ファイル。`op` は呼ばない。
    * `carried=True` (dispatcher が起動する動詞だけ): `source` が有効なとき、dispatcher が起動時に取り出して
      前置代入で運んだ `_CREWVIA_TG_RESOLVED_*` を受ける。`ask` は `carried=False` で、運搬用の変数を読まない。
    * `CREWVIA_TG_*` の env は**どの経路でも読まない**。
    """
    creds_cfg = config.get('credentials') or {}
    source = creds_cfg.get('source', '')
    if source not in ('op', 'file'):
        return None, 'no_credentials'
    if carried:
        environ = os.environ if environ is None else environ
        token = environ.get(CARRIED_TOKEN_VAR, '')
        chat_id = environ.get(CARRIED_CHAT_VAR, '')
        if not token and not chat_id:
            return None, 'no_credentials'
        return _validated(token, chat_id)
    if source == 'op':
        token_ref = creds_cfg.get('token_ref', '')
        chat_ref = creds_cfg.get('chat_id_ref', '')
        if not token_ref or not chat_ref:
            return None, 'no_credentials'
        op_command = creds_cfg.get('op_command') or 'op'
        token = _read_op(token_ref, op_command, runner)
        chat_id = _read_op(chat_ref, op_command, runner)
        if token is None or chat_id is None:
            return None, 'credential_command_failed'
        return _validated(token, chat_id)
    path = creds_cfg.get('file', '')
    if not path:
        return None, 'no_credentials'
    return _read_file_credentials(os.path.expanduser(path), os.geteuid() if euid is None else euid)


# ---------------------------------------------------------------------------
# Bot API クライアント — 失敗は値で返す。例外の文字列・URL・token を出さない
# ---------------------------------------------------------------------------

class ApiResult:
    __slots__ = ('ok', 'result', 'error', 'retry_after')

    def __init__(self, ok, result=None, error=None, retry_after=None):
        self.ok = ok
        self.result = result
        self.error = error            # 固定コード: network / http_<n> / bad_json / api_<code> / rate_limited
        self.retry_after = retry_after


def api_call(token, method, payload, *, api_base=API_BASE_DEFAULT, timeout=API_TIMEOUT_SECONDS):
    """Bot API を 1 回呼ぶ。**例外を出さない**。token は URL にだけ載り、戻り値にも例外文にも出ない。"""
    url = f'{api_base.rstrip("/")}/bot{token}/{method}'
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
    status = 200
    try:
        # `urllib.request.urlopen` (モジュールの大域) ではなく自前の opener を使う: 同じ既定の handler (環境のプロキシ設定を含む) で、
        # 他のコードが大域の `urlopen` を差し替えていても (テストが戻し忘れる等) 影響されない。
        with urllib.request.build_opener().open(req, timeout=timeout) as resp:  # noqa: S310 — api_base は呼び出し側が固定
            raw = resp.read(1 << 20)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read(1 << 20)
        except Exception:  # noqa: BLE001
            raw = b''
    except Exception:  # noqa: BLE001 — URLError・timeout・SSL・何でも。文字列化しない (URL に token)
        return ApiResult(False, error='network')
    try:
        data = json.loads(raw.decode('utf-8', errors='replace'))
    except Exception:  # noqa: BLE001
        return ApiResult(False, error=f'http_{status}' if status != 200 else 'bad_json')
    if not isinstance(data, dict):
        return ApiResult(False, error='bad_json')
    if data.get('ok') is True:
        return ApiResult(True, result=data.get('result'))
    params = data.get('parameters') if isinstance(data.get('parameters'), dict) else {}
    retry_after = params.get('retry_after')
    if status == 429 or isinstance(retry_after, int):
        return ApiResult(False, error='rate_limited',
                         retry_after=retry_after if isinstance(retry_after, int) and not isinstance(retry_after, bool) else None)
    code = data.get('error_code')
    return ApiResult(False, error=f'api_{code}' if isinstance(code, int) else f'http_{status}')


# ---------------------------------------------------------------------------
# 純粋関数: callback_data・掃除・受け付けの判定・文面
# ---------------------------------------------------------------------------

def make_callback_data(qid, nonce, index):
    return f'{qid}.{nonce}.{index}'


def parse_callback_data(data):
    """`<qid>.<nonce>.<index>` に**全体一致**したときだけ `(qid, nonce, index)`。余りは拒否。"""
    if not isinstance(data, str):
        return None
    m = _CALLBACK_RE.fullmatch(data)
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def is_unanswered(entry, now):
    """「未回答の質問がある」の定義 (§2-3b。受信を起動する条件と `ask` の上限 8 件で**同じ関数**)。"""
    if entry.get('status') != 'open' or not entry['expires_at'] > now:
        return False
    return entry.get('message_id') is not None or now - entry['created_at'] < NULL_MESSAGE_GRACE_SECONDS


def open_questions(ledger, now):
    return {qid: e for qid, e in ledger.items() if is_unanswered(e, now)}


def sweep_questions(ledger, now):
    """期限・残骸・保管期間の掃除 (§2-3b)。**純粋関数**: 新しい台帳を返す (入力は書き換えない)。

    1. open かつ `now ≥ expires_at` → expired
    2. open かつ message_id が null のまま 300 秒 → withdrawn (ask が途中で落ちた残骸)
    3. 閉じて 7 日を過ぎたもの (転送待ちの answered を除く) → 削除
    ボタンを消す必要がある (message_id を持つ) ものには `unbutton: True` を付ける。
    """
    out = {}
    for qid, entry in ledger.items():
        e = dict(entry)
        if e['status'] == 'open':
            if now >= e['expires_at']:
                e['status'], e['closed_at'], e['unbutton'] = 'expired', now, e.get('message_id') is not None
            elif e.get('message_id') is None and now - e['created_at'] >= NULL_MESSAGE_GRACE_SECONDS:
                e['status'], e['closed_at'], e['unbutton'] = 'withdrawn', now, False
        elif e['status'] in ('answered', 'withdrawn', 'expired'):
            closed = e.get('closed_at') if e.get('closed_at') is not None else e['created_at']
            waiting = e['status'] == 'answered' and e.get('forwarded') is False
            if not waiting and now - closed >= CLOSED_RETENTION_SECONDS and not e.get('unbutton'):
                continue
        out[qid] = e
    return out


class Decision:
    """`classify_update` の戻り値。kind: answer / reject / ignore / hold。"""

    __slots__ = ('kind', 'qid', 'answer', 'callback_id', 'reply')

    def __init__(self, kind, qid=None, answer=None, callback_id=None, reply=None):
        self.kind, self.qid, self.answer, self.callback_id, self.reply = kind, qid, answer, callback_id, reply


def sanitize_answer_text(text):
    """制御文字を除き、改行を空白にし、500 文字で切る (§2-2)。"""
    text = str(text).replace('\r', ' ').replace('\n', ' ')
    text = _CONTROL_RE.sub('', text).strip()
    if len(text) > ANSWER_TEXT_MAX:
        text = text[:ANSWER_TEXT_MAX] + '…(truncated)'
    return text


def identity_of(creds):
    """永続する状態に結び付ける、秘密でない識別子 (`bot_id` と `chat_hash`。receiver.json と同じもの)。"""
    return {'bot_id': creds.bot_id, 'chat_hash': creds.chat_hash}


def is_bound(record, identity):
    """状態 (質問・offset・送信状態) が今の認証情報のものか。**識別子の無い (旧形式の) 状態は「束縛の証拠が無い」= 別物**。
    `identity is None` は束縛を見ない呼び出し (純粋関数の単体テスト用) で、本番の呼び出しは必ず渡す。"""
    if identity is None:
        return True
    return record.get('bot_id') == identity['bot_id'] and record.get('chat_hash') == identity['chat_hash']


#: 質問のメッセージを**対象にする** Bot API のメソッド (message_id が要る)。chat ごとの連番なので、別の chat の質問には使えない。
QUESTION_TARGET_METHODS = frozenset({'editMessageReplyMarkup', 'editMessageText', 'deleteMessage'})


def question_api_call(creds, entry, method, payload, *, api_base=API_BASE_DEFAULT, timeout=API_TIMEOUT_SECONDS):
    """**質問に結び付く Bot API 呼び出しの唯一の関所** (PR #281 の Codex P1・4 巡目。設計 §2-5c)。

    質問 (台帳の `entry`) の `bot_id` + `chat_hash` が今の認証情報と一致しなければ、**通信せずに** `identity_mismatch` を返す
    (別の chat の `message_id` は無関係なメッセージを指しうる)。`chat_id` は `creds` から、`message_id` は `entry` から**ここで埋める**
    (呼び出し側が渡した値は上書きする — 質問と別の宛先を指せない)。呼び出し側は `identity_mismatch` をその質問の後始末の対象から外す。
    lib_telegram.py の中で `api_call` を直接呼べるのは `tests/test_telegram_question_gate.py` の許可表にある 3 箇所だけ (構造テスト)。
    """
    if not is_bound(entry, identity_of(creds)):
        return ApiResult(False, error='identity_mismatch')
    body = dict(payload, chat_id=creds.chat_id)
    if method in QUESTION_TARGET_METHODS:
        if entry.get('message_id') is None:
            return ApiResult(False, error='no_message_id')
        body['message_id'] = entry['message_id']
    return api_call(creds.token, method, body, api_base=api_base, timeout=timeout)


def rebind_ledger(ledger, identity, now):
    """今の認証情報のものでない `open` の質問を `withdrawn` にする (**純粋関数**。→ `(新しい台帳, 取り下げた qid の列)`)。

    別の bot / chat の message_id・callback は今の bot で照合できず、message_id は chat ごとの連番で衝突しうる。
    ボタンを消す試みはしない (`unbutton: False` — 別の bot では消せない)。閉じた質問 (answered 等) は触らない
    (answered の転送は台帳の中身だけで済む)。
    """
    out, withdrawn = {}, []
    for qid, entry in ledger.items():
        if entry['status'] == 'open' and not is_bound(entry, identity):
            e = dict(entry)
            e['status'], e['closed_at'], e['unbutton'], e['closed_reason'] = 'withdrawn', now, False, 'identity_changed'
            out[qid] = e
            withdrawn.append(qid)
        else:
            out[qid] = entry
    return out, sorted(withdrawn)


def _same_id(a, b):
    return isinstance(a, int) and not isinstance(a, bool) and str(a) == str(b)


def classify_update(update, ledger, chat_id, now, identity=None):
    """update 1 件をどう扱うか (**純粋関数**。§2-2 の全条件 AND・§2-3 の各ケース)。

    * 他 chat・形の合わない callback_data・無関係なメッセージ → `ignore` (転送も返信もしない)
    * 台帳に無い / nonce 違い / message_id 違い / index 範囲外 / 期限切れ / 回答済み → `reject` (+ answerCallbackQuery の文)
    * `message_id == null` の open を指す押下 → `hold` (offset を進めない)
    * 全部通れば `answer`
    """
    chat = str(chat_id)
    if identity is not None:      # 今の bot / chat のものでない質問は、照合の相手にも保留の理由にもしない
        ledger = {q: e for q, e in ledger.items() if is_bound(e, identity)}
    cq = update.get('callback_query')
    if isinstance(cq, dict):
        sender = (cq.get('from') or {}).get('id')
        message = cq.get('message') if isinstance(cq.get('message'), dict) else {}
        if not _same_id(sender, chat) and str(sender) != chat:
            return Decision('ignore')
        if str((message.get('chat') or {}).get('id')) != chat:
            return Decision('ignore')
        cb_id = cq.get('id') if isinstance(cq.get('id'), str) else None
        parsed = parse_callback_data(cq.get('data'))
        if parsed is None:
            return Decision('ignore')
        qid, nonce, index = parsed
        entry = ledger.get(qid)
        if entry is None or entry['nonce'] != nonce:
            return Decision('reject', qid, callback_id=cb_id, reply='不明な質問です')
        if entry['status'] == 'open' and entry.get('message_id') is None:
            return Decision('hold', qid)
        if message.get('message_id') != entry.get('message_id'):
            return Decision('reject', qid, callback_id=cb_id, reply='不明な質問です')
        if index >= len(entry['options']):
            return Decision('reject', qid, callback_id=cb_id, reply='不明な質問です')
        if entry['status'] == 'answered':
            ans = entry.get('answer') or {}
            label = entry['options'][ans['index']] if ans.get('kind') == 'choice' else '返信'
            return Decision('reject', qid, callback_id=cb_id, reply=f'回答済み: {label}')
        if entry['status'] == 'withdrawn':
            return Decision('reject', qid, callback_id=cb_id, reply='画面で回答済みです')
        if entry['status'] == 'expired' or now >= entry['expires_at']:
            return Decision('reject', qid, callback_id=cb_id, reply='期限切れです')
        answer = {'kind': 'choice', 'index': index, 'at': now, 'update_id': update.get('update_id')}
        return Decision('answer', qid, answer=answer, callback_id=cb_id, reply=f'受け付けました: {entry["options"][index]}')
    msg = update.get('message')
    if isinstance(msg, dict):
        if str((msg.get('chat') or {}).get('id')) != chat or str((msg.get('from') or {}).get('id')) != chat:
            return Decision('ignore')
        reply_to = (msg.get('reply_to_message') or {}).get('message_id')
        if reply_to is None or not isinstance(msg.get('text'), str):
            return Decision('ignore')
        for qid, entry in ledger.items():
            if entry['status'] == 'open' and entry.get('message_id') == reply_to and now < entry['expires_at']:
                text = sanitize_answer_text(msg['text'])
                if not text:
                    return Decision('ignore')
                answer = {'kind': 'text', 'text': text, 'at': now, 'update_id': update.get('update_id')}
                return Decision('answer', qid, answer=answer)
        # 照合できる質問が無い。ただし「まだ message_id が記録されていない open がある」(ask の送信 ② と記録 ③ の間) なら、
        # この返信はその質問への答えかもしれない — 捨てて offset を進めると、ユーザーの答えが黙って失われる (Codex P1)。
        # ボタンの押下 (message_id が null の open を指す) と同じく**保留**する。窓は猶予 (5 分) で閉じる (sweep が withdrawn にする)。
        # (返信先がすでに記録済みの別の質問のメッセージなら、それは「閉じた質問への返信」で保留の理由にならない。)
        if sanitize_answer_text(msg['text']) and all(e.get('message_id') != reply_to for e in ledger.values()) and any(
                e['status'] == 'open' and e.get('message_id') is None and now < e['expires_at']
                and now - e['created_at'] < NULL_MESSAGE_GRACE_SECONDS for e in ledger.values()):
            return Decision('hold')
        return Decision('ignore')
    return Decision('ignore')


def apply_answer(ledger, qid, answer, now):
    """`open` → `answered` を 1 回だけ (CAS)。先に確定した 1 つだけが有効。新しい台帳を返す。"""
    entry = ledger.get(qid)
    if entry is None or entry['status'] != 'open':
        return ledger
    new = dict(ledger)
    e = dict(entry)
    e['status'], e['answer'], e['forwarded'], e['closed_at'] = 'answered', answer, False, now
    new[qid] = e
    return new


def receiver_verdict(receiver, now, mine=None):
    """`ask` が受信側の状態から出す答え → `(ok, code)` (§1-1)。**純粋関数**。

    `receiver` は `load_json_store` の結果。壊れている / 古い / 無い / 無効 / 別の bot は**断る**
    (観測できなかったことを「有効」に倒さない)。`mine` = 自分の解決結果 `(bot_id, chat_hash)`。
    """
    if is_missing(receiver):
        return False, 'receiver_unknown'
    if is_unreadable(receiver):
        return False, 'receiver_unknown'
    if not receiver.get('enabled'):
        return False, 'receiver_disabled'
    if not now - receiver['checked_at'] <= RECEIVER_STALE_SECONDS:
        return False, 'receiver_stale'
    if mine is not None and (receiver.get('bot_id'), receiver.get('chat_hash')) != tuple(mine):
        return False, 'receiver_mismatch'
    return True, 'ok'


def _shorten(text, limit):
    text = _CONTROL_RE.sub(' ', str(text))
    return text if len(text) <= limit else text[:limit - 1] + '…'


def build_question_text(question, options_note, *, target=None, session_link='', expires_at=None):
    """質問の文面 (§6)。`parse_mode` は使わない (プレーンテキスト)。2000 文字で切る。"""
    lines = ['❓ crewvia Director からの質問', _shorten(question, QUESTION_MAX)]
    if target:
        lines.append(f'対象: {target}')
    lines.append('💬 込み入った指示はこのメッセージに返信してください')
    if session_link:
        lines.append(f'セッション: {session_link}')
    if expires_at is not None:
        lines.append('期限: ' + time.strftime('%H:%M', time.localtime(expires_at)) + ' まで有効')
    return _shorten('\n'.join(lines), MESSAGE_MAX)


def format_forward_line(qid, entry, task_state=None):
    """Director の画面に入れる 1 行 (§3-1)。値は JSON 文字列で引用符エスケープ (1 行・1 欄にしか見えない)。"""
    answer = entry['answer']
    task = f'{entry["slug"]}/{entry["task"]}' if entry.get('slug') and entry.get('task') else '-'
    parts = [f'[telegram-answer] q={qid}', f'task={task}']
    if answer['kind'] == 'choice':
        parts.append('choice=' + json.dumps(entry['options'][answer['index']], ensure_ascii=False))
        parts.append(f'index={answer["index"]}')
    else:
        parts.append('text=' + json.dumps(answer['text'], ensure_ascii=False))
    if task_state is not None:
        parts.append(f'task_state={task_state}')
    return ' '.join(parts)


# ---------------------------------------------------------------------------
# ファイルの入出力 (置き場: registry/daemons/)
# ---------------------------------------------------------------------------

def _path(registry_dir, name):
    return Path(registry_dir) / name


def read_questions(registry_dir, warn=None):
    return load_json_store(_path(registry_dir, QUESTIONS_FILE), check=telegram_questions_problem, warn=warn)


def read_receiver(registry_dir, warn=None):
    return load_json_store(_path(registry_dir, RECEIVER_FILE), check=telegram_receiver_problem, warn=warn)


def read_offset(registry_dir, warn=None):
    return load_json_store(_path(registry_dir, OFFSET_FILE), check=telegram_offset_problem, warn=warn)


class LedgerBusy(Exception):
    """質問台帳のロックを取れなかった (次のサイクルで)。"""


class LedgerUnreadable(Exception):
    """質問台帳が使えない (壊れている)。"""


def update_questions(registry_dir, fn, *, now, warn=None):
    """質問台帳の read-modify-write (`told_lock` の下)。`fn(ledger) -> (new_ledger, extra)`。

    台帳が壊れていれば `LedgerUnreadable` (書かない)・ロックが取れなければ `LedgerBusy`。
    書くものは `telegram_questions_problem` を通してから (読み手と同じ形)。
    """
    path = _path(registry_dir, QUESTIONS_FILE)
    with told_lock(str(path), wait=LEDGER_LOCK_WAIT_SECONDS) as held:
        if not held:
            raise LedgerBusy()
        ledger = load_json_store(path, check=telegram_questions_problem, warn=warn)
        if is_missing(ledger):
            ledger = {}
        elif is_unreadable(ledger):
            raise LedgerUnreadable()
        new_ledger, extra = fn(ledger)
        if new_ledger != ledger:
            if telegram_questions_problem(new_ledger):
                raise LedgerUnreadable()
            if not write_told_atomic(path, new_ledger):
                raise LedgerBusy()
        return extra


def _write_json_locked(path, data):
    return write_told_atomic(path, data)


def write_receiver_state(registry_dir, creds, reason, now, *, heartbeat=RECEIVER_HEARTBEAT_SECONDS):
    """受信側の状態を書く (**書き手は dispatcher だけ**。§1-1)。

    → `{'enabled', 'reason', 'wrote', 'file_exists'}`。
    * 認証情報が解決できていない (`creds is None`) かつ `reason == 'no_credentials'` (= 初めから未設定) かつ
      ファイルが無い → **何も作らない**。
    * 設定されているのに失敗している (`credential_command_failed` 等) → ファイルを作って `enabled: false`。
    * 変化が無く、心拍の間隔 (30 秒) に届いていなければ書き直さない。
    """
    path = _path(registry_dir, RECEIVER_FILE)
    current = load_json_store(path, check=telegram_receiver_problem)
    exists = not is_missing(current)
    if creds is None:
        want = {'enabled': False, 'checked_at': now, 'reason': reason}
        if not exists and reason == 'no_credentials':
            return {'enabled': False, 'reason': reason, 'wrote': False, 'file_exists': False}
    else:
        want = {'enabled': True, 'checked_at': now, 'reason': 'ok',
                'bot_id': creds.bot_id, 'chat_hash': creds.chat_hash}
    unchanged = (not is_unreadable(current) and exists
                 and {k: v for k, v in current.items() if k != 'checked_at'}
                 == {k: v for k, v in want.items() if k != 'checked_at'}
                 and 0 <= now - current['checked_at'] < heartbeat)
    wrote = False
    if not unchanged:
        wrote = _write_json_locked(path, want)
    return {'enabled': want['enabled'], 'reason': want['reason'], 'wrote': wrote, 'file_exists': True}


def _update_send_state(registry_dir, fn, identity=None):
    path = _path(registry_dir, SEND_FILE)
    with told_lock(str(path), wait=LEDGER_LOCK_WAIT_SECONDS) as held:
        if not held:
            return None
        state = load_json_store(path, check=telegram_send_problem)
        if is_missing(state) or is_unreadable(state):
            state = {}
        if identity is not None and state and not is_bound(state, identity):
            state = {}      # 別の bot / chat のレート・バックオフの記憶は引き継がない (識別子の無い旧形式も同じ)
        new_state, extra = fn(dict(state))
        if identity is not None:
            new_state['bot_id'], new_state['chat_hash'] = identity['bot_id'], identity['chat_hash']
        if telegram_send_problem(new_state) is None:
            _write_json_locked(path, new_state)
        return extra


class SendResult:
    __slots__ = ('ok', 'error', 'message_id', 'warn')

    def __init__(self, ok, error=None, message_id=None, warn=False):
        self.ok, self.error, self.message_id, self.warn = ok, error, message_id, warn


def send_message(registry_dir, creds, text, *, api_base=API_BASE_DEFAULT, reply_markup=None,
                 now=None, sleep=time.sleep, clock=time.time):
    """sendMessage (全体で 1 秒に 1 通・失敗はバックオフ・429 は `retry_after`)。**例外を出さない**。

    状態は `telegram-send.json` (書き手は `ask_user.sh` と dispatcher の 2 者 — どちらもロックの下で読み直す。§2-5)。
    """
    start = clock() if now is None else now
    plan = {}

    def reserve(state):
        until = state.get('backoff_until')
        if is_finite_number(until) and start < until:
            return state, {'blocked': True}
        wait = 0.0
        last = state.get('last_sent_at')
        if is_finite_number(last):
            wait = max(0.0, last + MIN_SEND_INTERVAL_SECONDS - start)
        state['last_sent_at'] = start + wait
        return state, {'blocked': False, 'wait': wait}

    identity = identity_of(creds)
    got = _update_send_state(registry_dir, reserve, identity)
    if got is None:
        return SendResult(False, 'send_state_busy')
    if got['blocked']:
        return SendResult(False, 'backoff')
    if got['wait'] > 0:
        sleep(got['wait'])
    payload = {'chat_id': creds.chat_id, 'text': text}
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    res = api_call(creds.token, 'sendMessage', payload, api_base=api_base)
    finished = clock() if now is None else now + got['wait']
    warn_flag = {'v': False}

    def record(state):
        if res.ok:
            state['consecutive_failures'], state['backoff_until'] = 0, None
            return state, None
        failures = int(state.get('consecutive_failures') or 0) + 1
        state['consecutive_failures'] = failures
        if res.error == 'rate_limited' and res.retry_after:
            delay = res.retry_after
        else:
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (failures - 1)))
        state['backoff_until'] = finished + delay
        last_warn = state.get('last_warn_at')
        if not is_finite_number(last_warn) or finished - last_warn >= WARN_INTERVAL_SECONDS:
            state['last_warn_at'] = finished
            warn_flag['v'] = True
        return state, None

    _update_send_state(registry_dir, record, identity)
    if res.ok:
        message_id = res.result.get('message_id') if isinstance(res.result, dict) else None
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return SendResult(False, 'bad_response')
        return SendResult(True, message_id=message_id)
    return SendResult(False, res.error, warn=warn_flag['v'])


def telegram_available(registry_dir, now, creds_ok=True):
    """段階 2 (PR-B) の入力: 受信側が有効で、送信がバックオフ中でないか。"""
    receiver = read_receiver(registry_dir)
    ok, _ = receiver_verdict(receiver, now)
    if not (creds_ok and ok):
        return False
    state = load_json_store(_path(registry_dir, SEND_FILE), check=telegram_send_problem)
    if is_unreadable(state) and not is_missing(state):
        return False
    if is_missing(state):
        return True
    until = state.get('backoff_until')
    return not (is_finite_number(until) and now < until)


# ---------------------------------------------------------------------------
# poll — getUpdates(timeout=0) を 1 回。dispatcher がサブプロセス (timeout 付き) で呼ぶ
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _poll_lock(registry_dir):
    path = _path(registry_dir, POLL_LOCK_FILE)
    fd = None
    held = False
    try:
        try:
            Path(registry_dir).mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NONBLOCK, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except OSError:
            held = False
        yield held
    finally:
        if fd is not None:
            if held:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class _Budget:
    """poll 1 回の通信の持ち時間。後始末はここから**残りだけ**を取る (受信を締め出さない)。"""

    def __init__(self, seconds=POLL_BUDGET_SECONDS, clock=time.monotonic):
        self._clock = clock
        self._deadline = clock() + seconds

    def timeout(self):
        """次の 1 回の通信に使える秒数。残りが足りなければ `None` (その通信は次のサイクルに回す)。"""
        remaining = self._deadline - self._clock()
        if remaining < BUDGET_MIN_REMAINING_SECONDS:
            return None
        return min(float(API_TIMEOUT_SECONDS), remaining)


def _unbutton_give_up(entry, now, identity=None):
    """ボタンを消す試みを諦める条件 (通信しない・純粋)。「消えないボタン」は押されても台帳の照合で断られる (§2-3d)。
    `identity` を渡すと、今の認証情報の質問でないものも諦める (別の bot / chat では消せない。§2-5c)。"""
    if entry.get('message_id') is None:
        return True
    if not is_bound(entry, identity):
        return True
    tries = entry.get('unbutton_tries') or 0
    closed = entry.get('closed_at')
    aged = is_finite_number(closed) and now - closed >= UNBUTTON_GIVE_UP_SECONDS
    return tries >= UNBUTTON_MAX_TRIES or bool(aged)


def _settle_unbutton(registry_dir, qid, outcome, now):
    """`outcome`: `done` (消せた・消せないと確定) / `giveup` / `retry` (一時的な失敗 — 回数を 1 足す)。"""
    def apply(ledger_now):
        e = ledger_now.get(qid)
        if e is None or not e.get('unbutton'):
            return ledger_now, None
        out = dict(ledger_now)
        ne = dict(e)
        if outcome == 'retry':
            ne['unbutton_tries'] = (ne.get('unbutton_tries') or 0) + 1
        else:
            ne['unbutton'] = False
            ne.pop('unbutton_tries', None)
        out[qid] = ne
        return out, None
    with contextlib.suppress(LedgerBusy, LedgerUnreadable):
        update_questions(registry_dir, apply, now=now)


def _unbutton_pending(registry_dir, creds, api_base, now, *, budget=None, limit=CLEANUP_MAX_PER_CYCLE):
    """掃除で閉じた質問のボタンを消す (best effort)。失敗しても台帳は進める。

    **持ち時間と件数に上限がある** (`budget` の残り・`limit` 件)。残りが足りなければ次のサイクルに回す。
    確定した失敗 (Bot API の 400 / 403 / 404) は諦めて `unbutton` を外す。一時的な失敗は回数 (`UNBUTTON_MAX_TRIES`) か
    閉じてからの時間 (`UNBUTTON_GIVE_UP_SECONDS`) で諦める。試行の少ない順に回し、失敗し続ける 1 件が他を止めない。
    """
    ledger = read_questions(registry_dir)
    if is_unreadable(ledger) or is_missing(ledger):
        return 0
    budget = budget or _Budget()
    identity = identity_of(creds)
    pending = [(qid, e) for qid, e in ledger.items() if e.get('unbutton')]
    for qid, e in pending:
        if _unbutton_give_up(e, now, identity):
            _settle_unbutton(registry_dir, qid, 'giveup', now)
    todo = sorted(((e.get('unbutton_tries') or 0, e.get('closed_at') or 0, qid, e['message_id'])
                   for qid, e in pending if not _unbutton_give_up(e, now, identity)))[:limit]
    attempted = 0
    for _tries, _closed, qid, message_id in todo:
        timeout = budget.timeout()
        if timeout is None:
            break
        res = question_api_call(creds, ledger[qid], 'editMessageReplyMarkup', {'reply_markup': {'inline_keyboard': []}},
                                api_base=api_base, timeout=timeout)
        if res.error == 'identity_mismatch':
            _settle_unbutton(registry_dir, qid, 'giveup', now)   # 通信していない (予算にも数えない)。即「諦め」(§2-5c)
            continue
        attempted += 1
        if res.ok:
            _settle_unbutton(registry_dir, qid, 'done', now)
        elif res.error in UNBUTTON_PERMANENT_ERRORS:
            _settle_unbutton(registry_dir, qid, 'giveup', now)
        elif res.error == 'rate_limited':
            break                                   # 次のサイクルまで待つ (回数には数えない。時間の上限が諦めを担う)
        else:
            _settle_unbutton(registry_dir, qid, 'retry', now)
    return attempted


def sweep_file(registry_dir, now):
    """通信を伴わない掃除 (ファイルがあるときだけ)。"""
    if is_missing(read_questions(registry_dir)):
        return False
    with contextlib.suppress(LedgerBusy, LedgerUnreadable):
        update_questions(registry_dir, lambda ledger: (sweep_questions(ledger, now), None), now=now)
    return True


def poll_once(registry_dir, creds, *, api_base=API_BASE_DEFAULT, now=None, budget=None):
    """→ `{'skipped'?, 'error'?, 'processed', 'answered', 'rejected'}`。**例外を出さない**。

    順序は **受信 → 台帳と offset への記録 → 後始末** (§2-3d)。受信より前にネットワークを呼ばない。
    後始末 (answerCallbackQuery・ボタンを消す・諦めの通知) は `budget` の残りだけを使い、足りなければ次のサイクルに回す。
    """
    now = time.time() if now is None else now
    budget = budget or _Budget()
    out = {'processed': 0, 'answered': 0, 'rejected': 0, 'error': None}
    with _poll_lock(registry_dir) as held:
        if not held:
            out['skipped'] = 'busy'
            return out
        identity = identity_of(creds)
        try:
            out['identity_changed'] = update_questions(
                registry_dir, lambda ledger: rebind_ledger(sweep_questions(ledger, now), identity, now), now=now)
        except LedgerBusy:
            out['skipped'] = 'ledger_busy'
            return out
        except LedgerUnreadable:
            out['error'] = 'ledger_unreadable'      # offset を進めず、answerCallbackQuery も呼ばない (§2-3)
            return out
        replies = _receive_updates(registry_dir, creds, api_base, now, out, identity)
        for cb_id, text in replies[:CLEANUP_MAX_PER_CYCLE * 2]:   # 台帳に書いた後に返す (待ち表示を止めるだけ。失敗・時間切れは無視)
            timeout = budget.timeout()
            if timeout is None:
                break
            api_call(creds.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': text},
                     api_base=api_base, timeout=timeout)
        _unbutton_pending(registry_dir, creds, api_base, now, budget=budget)
        _give_up_forwarding(registry_dir, creds, api_base, now, budget=budget)
    return out


def _receive_updates(registry_dir, creds, api_base, now, out, identity):
    """getUpdates → 台帳に記録 → offset。→ 後で返す `[(callback_id, text)]` (通信はしない)。"""
    ledger = read_questions(registry_dir)
    if is_unreadable(ledger):
        out['error'] = 'ledger_unreadable'
        return []
    if not open_questions(ledger, now):
        return []                                   # 未回答の質問が無ければ通信しない
    offset_state = read_offset(registry_dir)
    if is_unreadable(offset_state):
        # 読めない offset は「0 から読み直す」に倒す (at-least-once。重複は台帳の CAS と forwarded が止める)。
        # 無い (ENOENT) は通常運用。**それ以外**は壊れ / 型違い / 権限なしなので、1 度だけ見える形で残す (黙って直し続けない)。
        # この後の書き込み (どの経路でも) が正しい形で上書きするので、壊れたままにはならない。
        if not is_missing(offset_state):
            out['offset_unreadable'] = (errno.errorcode.get(offset_state.errno, 'EIO') if offset_state.errno else 'invalid')   # 固定の語だけ (解析エラーの本文は出さない)
        offset_state = {}
    offset = offset_state['offset'] if offset_state else 0
    if offset and not is_bound(offset_state, identity):
        offset = 0      # 他の bot の (または識別子の無い) offset は捨てて最初から。update_id は bot ごとの列で、使うと新しい bot の update を飛ばす
    res = api_call(creds.token, 'getUpdates',
                   {'offset': offset, 'timeout': 0, 'allowed_updates': ['callback_query', 'message']},
                   api_base=api_base)
    if not res.ok or not isinstance(res.result, list):
        out['error'] = res.error or 'bad_response'
        _write_json_locked(_path(registry_dir, OFFSET_FILE), dict(identity, offset=offset, last_poll_at=now))
        return []
    updates = [u for u in res.result if isinstance(u, dict)
               and isinstance(u.get('update_id'), int) and not isinstance(u.get('update_id'), bool)]
    updates.sort(key=lambda u: u['update_id'])
    replies = []
    hold_ids = []

    def process(ledger_now):
        cur = ledger_now
        for update in updates:
            d = classify_update(update, cur, creds.chat_id, now, identity)
            if d.kind == 'hold':
                hold_ids.append(update['update_id'])
            elif d.kind == 'answer':
                cur = apply_answer(cur, d.qid, d.answer, now)
                out['answered'] += 1
            if d.kind in ('answer', 'reject') and d.callback_id:
                replies.append((d.callback_id, d.reply))
                if d.kind == 'reject':
                    out['rejected'] += 1
            out['processed'] += 1
        return cur, None

    try:
        update_questions(registry_dir, process, now=now)
    except (LedgerBusy, LedgerUnreadable) as e:
        out['error'] = 'ledger_busy' if isinstance(e, LedgerBusy) else 'ledger_unreadable'
        out['answered'] = out['rejected'] = out['processed'] = 0
        return []                                   # offset を進めない → 次のサイクルで同じ update を受ける
    if updates:
        new_offset = min(hold_ids) if hold_ids else updates[-1]['update_id'] + 1
        new_offset = max(new_offset, offset)
    else:
        new_offset = offset
    _write_json_locked(_path(registry_dir, OFFSET_FILE), dict(identity, offset=new_offset, last_poll_at=now))
    return replies


def _give_up_forwarding(registry_dir, creds, api_base, now, *, budget=None, limit=CLEANUP_MAX_PER_CYCLE):
    """答えが入ってから 24 時間転送できなかったものを諦め、Telegram に 1 回だけ返す (§2-3)。

    持ち時間と件数に上限がある。**残りが足りなければ印を付けない** (印 → 通知の順なので、印だけ付いて通知が出ない穴を作らない)。
    """
    ledger = read_questions(registry_dir)
    if is_unreadable(ledger) or is_missing(ledger):
        return
    budget = budget or _Budget()
    stale = sorted(((e['answer']['at'], qid) for qid, e in ledger.items()
                    if e['status'] == 'answered' and e.get('forwarded') is False
                    and now - e['answer']['at'] >= FORWARD_GIVE_UP_SECONDS))
    for _at, qid in stale[:limit]:
        timeout = budget.timeout()
        if timeout is None:
            return

        def mark(ledger_now, qid=qid):
            e = ledger_now.get(qid)
            if e is None or e['status'] != 'answered' or e.get('forwarded') is not False:
                return ledger_now, False
            out = dict(ledger_now)
            ne = dict(e)
            ne['forwarded'] = 'gave_up'
            out[qid] = ne
            return out, True
        try:
            marked = update_questions(registry_dir, mark, now=now)
        except (LedgerBusy, LedgerUnreadable):
            return
        if marked:
            question_api_call(creds, ledger[qid], 'sendMessage',
                              {'text': f'Director に届きませんでした (質問 {qid})。画面で確認してください。'},
                              api_base=api_base, timeout=timeout)     # 別の chat の質問の通知を今の chat に出さない (§2-5c)


# ---------------------------------------------------------------------------
# dispatcher のサイクルへの差し込み口 (run_cycle)
# ---------------------------------------------------------------------------

def pending_forwards(ledger):
    return [(qid, e) for qid, e in sorted(ledger.items(), key=lambda kv: kv[1]['answer']['at'] if kv[1].get('answer') else 0)
            if e['status'] == 'answered' and e.get('forwarded') is False]


def card_status(queue_dir, slug, task):
    """転送の時点の card の status (読めなければ `unreadable`)。**判断の根拠ではない** (§3-1)。"""
    if not slug or not task or not _SLUG_RE.fullmatch(slug) or not _TASK_RE.fullmatch(task):
        return None
    path = Path(queue_dir) / 'missions' / slug / 'tasks' / f'{task}.md'
    try:
        meta, _body = read_task_card(path, task)
    except Exception:  # noqa: BLE001
        return 'unreadable'
    status = meta.get('status') if isinstance(meta, dict) else None
    if not isinstance(status, str) or not status or status == CORRUPT_TASK_STATUS:
        return 'unreadable'                 # 隔離された (破損) card は status を信用しない
    return status


def mark_forwarded(registry_dir, qid, now):
    def mark(ledger):
        e = ledger.get(qid)
        if e is None or e['status'] != 'answered' or e.get('forwarded') is not False:
            return ledger, False
        out = dict(ledger)
        ne = dict(e)
        ne['forwarded'] = True
        out[qid] = ne
        return out, True
    return update_questions(registry_dir, mark, now=now)


def run_cycle(registry_dir, queue_dir, config, carried_reason, forward, *, now=None, creds=None,
              api_base=None, config_path=None, log=lambda msg: None, runner=subprocess.run, python=sys.executable):
    """dispatcher の 1 サイクルが呼ぶ入口。**例外を出さない**。→ 要約 dict。

    1. 受信側の心拍 (`telegram-receiver.json`)。結果の `enabled` / `reason` を呼び出し側が Director への通知に使う。
    2. `forwarded=false` の answered を `forward(line) -> bool` で Director に送り、送れたものだけ記録する。
    3. 認証情報があり、未回答の質問 / ボタンを消す対象があり、間引き (poll_interval) を過ぎていれば
       `poll` を **サブプロセスで** (`timeout 8`) 呼ぶ。認証情報は env で渡す (argv に出さない)。
    未設定 (`creds is None`) で台帳が無ければ、何も読まず何も書かない。
    """
    now = time.time() if now is None else now
    summary = {'receiver': None, 'forwarded': 0, 'polled': False, 'poll': None, 'identity_changed': []}
    try:
        summary['receiver'] = write_receiver_state(registry_dir, creds, carried_reason, now)
        ledger = read_questions(registry_dir)
        if is_missing(ledger):
            return summary
        if is_unreadable(ledger):
            log('WARNING: telegram-questions: 台帳を使えない — 転送せず offset も進めません (台帳を消せば復旧)')
            return summary
        def forward_pending():
            current = read_questions(registry_dir)
            if is_unreadable(current) or is_missing(current):
                return
            for qid, entry in pending_forwards(current):
                line = format_forward_line(qid, entry, card_status(queue_dir, entry.get('slug'), entry.get('task')))
                if forward(line):
                    with contextlib.suppress(LedgerBusy, LedgerUnreadable):
                        if mark_forwarded(registry_dir, qid, now):
                            summary['forwarded'] += 1
        forward_pending()
        if creds is None:
            sweep_file(registry_dir, now)
            return summary
        # 認証情報 (bot / chat) が変わっていたら、前の bot の open な質問を取り下げる (poll の前・サイクルごとに安く)。
        # 取り下げた qid は呼び出し側 (dispatcher) が Director に 1 回知らせる。
        identity = identity_of(creds)
        try:
            summary['identity_changed'] = update_questions(
                registry_dir, lambda l: rebind_ledger(l, identity, now), now=now)
        except (LedgerBusy, LedgerUnreadable):
            pass
        ledger = read_questions(registry_dir)
        if is_unreadable(ledger) or is_missing(ledger):
            return summary
        needs_net = bool(open_questions(ledger, now)) or any(
            e.get('unbutton') for e in ledger.values()) or any(
            e['status'] == 'answered' and e.get('forwarded') is False
            and now - e['answer']['at'] >= FORWARD_GIVE_UP_SECONDS for e in ledger.values())
        if not needs_net:
            sweep_file(registry_dir, now)
            return summary
        offset = read_offset(registry_dir)
        last = None if is_missing(offset) or is_unreadable(offset) else offset.get('last_poll_at')
        interval = config.get('poll_interval_seconds', DEFAULT_POLL_INTERVAL_SECONDS)
        if is_finite_number(last) and 0 <= now - last < interval:
            return summary
        cmd = ['timeout', '8', python, str(Path(__file__).resolve()), 'poll', '--registry-dir', str(registry_dir)]
        if api_base:
            cmd += ['--api-base', api_base]
        if config_path:
            cmd += ['--config', str(config_path)]
        env = dict(os.environ)
        env[CARRIED_TOKEN_VAR], env[CARRIED_CHAT_VAR] = creds.token, creds.chat_id
        summary['polled'] = True
        try:
            proc = runner(cmd, capture_output=True, text=True, timeout=12, env=env, stdin=subprocess.DEVNULL)
            try:
                summary['poll'] = json.loads(proc.stdout)
            except Exception:  # noqa: BLE001 — サブプロセスが死んでも「今回は何も受けなかった」で進む
                summary['poll'] = {'error': 'poll_failed'}
        except Exception:  # noqa: BLE001
            summary['poll'] = {'error': 'poll_timeout'}
        forward_pending()                          # 今受けた答えを次のサイクルまで待たせない
    except Exception as e:  # noqa: BLE001 — dispatcher のサイクルを落とさない。型名だけ (値・token は出さない)
        log(f'WARNING: telegram cycle failed ({type(e).__name__})')
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NOT_CONFIGURED = 3
EXIT_CANNOT_SEND = 4
EXIT_TOO_MANY_OPEN = 5
EXIT_NOT_OPEN = 6


class _UsageError(Exception):
    pass


def _parse(argv, spec):
    """厳格引数 (未知のオプション・欠けた値・位置引数は使い方の誤り)。**値をエラーに出さない**。

    spec = {'--name': 'one' | 'many' | 'flag'}。
    """
    out = {k.lstrip('-').replace('-', '_'): ([] if v == 'many' else (False if v == 'flag' else None)) for k, v in spec.items()}
    i = 0
    while i < len(argv):
        name = argv[i]
        kind = spec.get(name)
        if kind is None:
            raise _UsageError()
        key = name.lstrip('-').replace('-', '_')
        if kind == 'flag':
            out[key] = True
            i += 1
            continue
        if i + 1 >= len(argv):
            raise _UsageError()
        value = argv[i + 1]
        if kind == 'many':
            out[key].append(value)
        else:
            if out[key] is not None:
                raise _UsageError()
            out[key] = value
        i += 2
    return out


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')


def _common(opts):
    registry = Path(opts['registry_dir']) if opts.get('registry_dir') else default_registry_dir()
    config = load_telegram_config(opts.get('config') or None)
    return registry, config


def _usage_exit(verb):
    sys.stderr.write(f'usage_error: {verb}\n')
    return EXIT_USAGE


def cmd_resolve(argv):
    """dispatcher.sh が起動時に 1 回呼ぶ。標準出力: 1 行目 = reason、2 行目 = token、3 行目 = chat_id (失敗時は reason だけ)。"""
    opts = _parse(argv, {'--config': 'one'})
    config = load_telegram_config(opts['config'])
    creds, reason = resolve_credentials(config)
    sys.stdout.write(reason + '\n')
    if creds is not None:
        sys.stdout.write(creds.token + '\n' + creds.chat_id + '\n')
    return EXIT_OK


def cmd_poll(argv):
    opts = _parse(argv, {'--registry-dir': 'one', '--config': 'one', '--api-base': 'one'})
    registry, config = _common(opts)
    creds, reason = resolve_credentials(config, carried=True)
    if creds is None:
        _emit({'error': reason, 'processed': 0})
        return EXIT_OK
    _emit(poll_once(registry, creds, api_base=opts['api_base'] or API_BASE_DEFAULT))
    return EXIT_OK


def _cmd_with_creds(opts):
    registry, config = _common(opts)
    creds, reason = resolve_credentials(config)
    return registry, config, creds, reason


def cmd_ask(argv):
    opts = _parse(argv, {'--question': 'one', '--option': 'many', '--task': 'one', '--ttl-minutes': 'one',
                         '--session-link': 'one', '--registry-dir': 'one', '--queue-dir': 'one',
                         '--config': 'one', '--api-base': 'one'})
    question = opts['question']
    options = opts['option']
    if not question or not question.strip() or not 2 <= len(options) <= 4 \
            or any(not o.strip() or len(o) > LABEL_MAX or _CONTROL_RE.search(o) for o in options):
        return _usage_exit('ask')
    slug = tid = None
    if opts['task']:
        slug, sep, tid = opts['task'].partition('/')
        if not sep or not _SLUG_RE.fullmatch(slug) or not _TASK_RE.fullmatch(tid):
            return _usage_exit('ask')
    ttl_minutes = None
    if opts['ttl_minutes'] is not None:
        try:
            ttl_minutes = float(opts['ttl_minutes'])
        except ValueError:
            return _usage_exit('ask')
        if not is_finite_number(ttl_minutes) or ttl_minutes <= 0:
            return _usage_exit('ask')
    registry, config, creds, reason = _cmd_with_creds(opts)
    if creds is None:
        if reason == 'no_credentials':
            sys.stderr.write('not_configured\n')
            return EXIT_NOT_CONFIGURED
        sys.stderr.write(f'{reason}\n')
        return EXIT_CANNOT_SEND
    api_base = opts['api_base'] or API_BASE_DEFAULT
    now = time.time()
    ok, code = receiver_verdict(read_receiver(registry), now, (creds.bot_id, creds.chat_hash))
    if not ok:
        sys.stderr.write(code + '\n')
        return EXIT_CANNOT_SEND
    ttl = (ttl_minutes if ttl_minutes is not None else config['question_ttl_minutes']) * 60
    qid = 'q-' + secrets.token_hex(4)
    identity = identity_of(creds)
    entry = {'nonce': secrets.token_hex(3), 'question': _shorten(question, QUESTION_MAX), 'options': list(options),
             'message_id': None, 'status': 'open', 'created_at': now, 'expires_at': now + ttl, 'forwarded': False,
             'bot_id': identity['bot_id'], 'chat_hash': identity['chat_hash']}
    if slug:
        entry['slug'], entry['task'] = slug, tid
        meta = None
        qdir = Path(opts['queue_dir']) if opts['queue_dir'] else default_queue_dir()
        with contextlib.suppress(Exception):
            meta, _body = read_task_card(qdir / 'missions' / slug / 'tasks' / f'{tid}.md', tid)
        if isinstance(meta, dict) and isinstance(meta.get('current_execution_id'), str) and meta['current_execution_id']:
            entry['execution_id'] = meta['current_execution_id']

    def register(ledger):
        ledger, _ = rebind_ledger(sweep_questions(ledger, now), identity, now)   # ⓪ 掃除 (poll と同じ関数)。別の bot の open は上限に数えない
        if len(open_questions(ledger, now)) >= MAX_OPEN_QUESTIONS:
            return ledger, False
        new = dict(ledger)
        new[qid] = entry                                           # ① message_id=null で先に書く
        return new, True
    try:
        accepted = update_questions(registry, register, now=now)
    except (LedgerBusy, LedgerUnreadable):
        sys.stderr.write('ledger_unavailable\n')
        return EXIT_CANNOT_SEND
    if not accepted:
        sys.stderr.write('too_many_open\n')
        return EXIT_TOO_MANY_OPEN
    target = f'{slug}/{tid}' if slug else None
    text = build_question_text(question, None, target=target,
                               session_link=(opts['session_link'] or os.environ.get('CREWVIA_DIRECTOR_SESSION_URL', '').strip()
                                             or config['session_link']), expires_at=now + ttl)
    keyboard = {'inline_keyboard': [[{'text': label, 'callback_data': make_callback_data(qid, entry['nonce'], i)}]
                                    for i, label in enumerate(options)]}
    sent = send_message(registry, creds, text, api_base=api_base, reply_markup=keyboard)
    if not sent.ok:                                                # ② 失敗 → ① を withdrawn に
        with contextlib.suppress(LedgerBusy, LedgerUnreadable):
            update_questions(registry, lambda l: (_close(l, qid, 'withdrawn', now, unbutton=False), None), now=now)
        sys.stderr.write(f'send_failed:{sent.error}\n')
        return EXIT_CANNOT_SEND

    def attach(ledger):                                            # ③ message_id を記録
        if qid not in ledger:
            raise LedgerUnreadable()
        new = dict(ledger)
        e = dict(new[qid])
        e['message_id'] = sent.message_id
        new[qid] = e
        return new, None
    try:
        update_questions(registry, attach, now=now)
    except (LedgerBusy, LedgerUnreadable):
        # ボタンは出ているが台帳が答えを受けられない → ボタンを消して断る (ボタンが出ているのに台帳に無い状態を作らない)
        question_api_call(creds, dict(entry, message_id=sent.message_id), 'editMessageReplyMarkup',
                          {'reply_markup': {'inline_keyboard': []}}, api_base=api_base)
        with contextlib.suppress(LedgerBusy, LedgerUnreadable):
            update_questions(registry, lambda l: (_close(l, qid, 'withdrawn', now, unbutton=False), None), now=now)
        sys.stderr.write('ledger_unavailable\n')
        return EXIT_CANNOT_SEND
    sys.stdout.write(qid + '\n')
    return EXIT_OK


def _close(ledger, qid, status, now, *, unbutton):
    e = ledger.get(qid)
    if e is None or e['status'] != 'open':
        return ledger
    new = dict(ledger)
    ne = dict(e)
    ne['status'], ne['closed_at'], ne['unbutton'] = status, now, bool(unbutton and e.get('message_id') is not None)
    new[qid] = ne
    return new


def cmd_verify(argv):
    opts = _parse(argv, {'--q': 'one', '--registry-dir': 'one', '--config': 'one'})
    if not opts['q'] or not re.fullmatch(r'q-[0-9a-f]{8}', opts['q']):
        return _usage_exit('verify')
    registry = Path(opts['registry_dir']) if opts['registry_dir'] else default_registry_dir()
    ledger = read_questions(registry)
    if is_unreadable(ledger) and not is_missing(ledger):
        _emit({'status': 'unreadable'})
        return EXIT_OK
    entry = None if is_missing(ledger) else ledger.get(opts['q'])
    if entry is None:
        _emit({'status': 'not_found'})
        return EXIT_OK
    out = {'status': entry['status'], 'q': opts['q'], 'task': (f'{entry["slug"]}/{entry["task"]}' if entry.get('slug') and entry.get('task') else None),
           'execution_id': entry.get('execution_id'), 'forwarded': entry.get('forwarded')}
    answer = entry.get('answer')
    if entry['status'] == 'answered' and answer:
        if answer['kind'] == 'choice':
            out.update({'choice_index': answer['index'], 'choice': entry['options'][answer['index']]})
        else:
            out.update({'text': answer['text']})
    _emit(out)
    return EXIT_OK


def cmd_cancel(argv):
    opts = _parse(argv, {'--q': 'one', '--by': 'one', '--note': 'one', '--registry-dir': 'one',
                         '--config': 'one', '--api-base': 'one'})
    if not opts['q'] or not re.fullmatch(r'q-[0-9a-f]{8}', opts['q']) or opts['by'] not in ('screen', 'other'):
        return _usage_exit('cancel')
    registry, config = _common(opts)
    now = time.time()
    try:
        entry = update_questions(registry, lambda l: (_close(l, opts['q'], 'withdrawn', now, unbutton=True), l.get(opts['q'])), now=now)
        after = read_questions(registry)
    except (LedgerBusy, LedgerUnreadable):
        sys.stderr.write('ledger_unavailable\n')
        return EXIT_CANNOT_SEND
    if entry is None or entry['status'] != 'open':
        _emit({'status': entry['status'] if entry else 'not_found'})
        return EXIT_NOT_OPEN
    creds, _ = resolve_credentials(config)                          # ボタンを消す (best effort。失敗しても withdrawn は有効)
    if creds is not None:
        _unbutton_pending(registry, creds, opts['api_base'] or API_BASE_DEFAULT, now, budget=_Budget())
    _emit({'status': 'withdrawn'})
    return EXIT_OK


def cmd_list(argv):
    opts = _parse(argv, {'--registry-dir': 'one', '--config': 'one'})
    registry = Path(opts['registry_dir']) if opts['registry_dir'] else default_registry_dir()
    ledger = read_questions(registry)
    if is_missing(ledger):
        return EXIT_OK
    if is_unreadable(ledger):
        sys.stderr.write('ledger_unreadable\n')
        return EXIT_CANNOT_SEND
    now = time.time()
    for qid, e in sorted(open_questions(ledger, now).items()):
        _emit({'q': qid, 'task': (f'{e["slug"]}/{e["task"]}' if e.get('slug') and e.get('task') else None),
               'expires_in_seconds': int(e['expires_at'] - now), 'question': _shorten(e['question'], 80)})
    return EXIT_OK


def cmd_send(argv):
    """プレーンテキストを 1 通送る (段階 2 = PR-B の送信と同じ入口。本文は標準入力)。"""
    opts = _parse(argv, {'--registry-dir': 'one', '--config': 'one', '--api-base': 'one'})
    registry, config, creds, reason = _cmd_with_creds(opts)
    if creds is None:
        sys.stderr.write(('not_configured' if reason == 'no_credentials' else reason) + '\n')
        return EXIT_NOT_CONFIGURED if reason == 'no_credentials' else EXIT_CANNOT_SEND
    text = _shorten(sys.stdin.read(), MESSAGE_MAX)
    if not text.strip():
        return _usage_exit('send')
    sent = send_message(registry, creds, text, api_base=opts['api_base'] or API_BASE_DEFAULT)
    if not sent.ok:
        sys.stderr.write(f'send_failed:{sent.error}\n')
        return EXIT_CANNOT_SEND
    return EXIT_OK


VERBS = {'resolve': cmd_resolve, 'poll': cmd_poll, 'ask': cmd_ask, 'verify': cmd_verify,
         'cancel': cmd_cancel, 'list': cmd_list, 'send': cmd_send}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in VERBS:
        sys.stderr.write('usage_error: unknown verb\n')
        return EXIT_USAGE
    try:
        return VERBS[argv[0]](argv[1:])
    except _UsageError:
        return _usage_exit(argv[0])
    except Exception as e:  # noqa: BLE001 — 型名だけ。メッセージ・値・token は出さない
        sys.stderr.write(f'internal_error:{type(e).__name__}\n')
        return EXIT_CANNOT_SEND


if __name__ == '__main__':
    sys.exit(main())
