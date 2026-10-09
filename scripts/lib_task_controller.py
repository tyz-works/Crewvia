#!/usr/bin/env python3
"""scripts/lib_task_controller.py — Task Controller: 試行 (Execution) の予約・開始・終了 (vNext 01c / E1)。

設計: `knowledge/execution.md` §1 (置き場と正本)・§3 (attempt と branch)・§4 (API・遷移表・冪等・domain error)・
§5 (呼び出し元の照合)。語彙と card 1 枚から決まる判定 (`attempt_view` / `execution_matches` / 欄の整合 / record の形) は
`lib_execution.py` (純粋)。この module は**書く側**で、`lib_state_store` の `Txn` (ロック保持中) を受け取る。

**呼び出し元は plan.sh だけ**: E2 (t008) で pull (reserve / start / G1 の fail)、E3 (t012) で done / fail / needs-director /
ready-for-verification / verifying / verify-result と `update --close-execution`。E4 で reset / retire が足される。どれも
cutover (ユーザー承認の PR)。呼び出し元が増えたら `tests/test_task_controller_has_no_callers_yet.py` が赤になる。

## 書く順序 (正本が先・projection が後。state-store.md §2.1)

どの操作も **card (コミット点) → record → assignment / identity** の順。record と assignment は card から再生成できる
(回復 R-1 / R-5)。reserve の手順 0 (前の試行 X が card 上で active のまま pending に戻っていた) は、Y を発行する
**前に** X の終端を card に書く (current を進めるのは reserve だけ・進める前に前の current を card 上で terminal にする)。

## しないこと

- **ロックを取らない** (取るのは plan.sh の `with_lock` 1 か所。`NestedTransaction` に合わせる)。回復 (`recover_before`)
  も呼び出し側が先に走らせる。
- 依存の判定 (`lib_dep_rules`)・pull の候補選び・done の D0〜D5 (pr_number の伝播)・fail の証拠・QA gate・Taskvia・
  worktree は持たない (task の内容の検査で、試行の遷移ではない)。呼び出し側が Controller の**前に**検査する。
- 名乗られた値・card の中身を、例外メッセージ・監査ログに出さない (固定コード + 識別子だけ)。
- Director 専用の照合バイパス (`--as-director` 等) は作らない (確かめられない役割で照合を外す経路が agent 名だけの
  照合になる。execution.md §5.3)。Director は `--execution` で ID を名指しするだけ。

## 検査と書き込みを分ける (`dry_run`)

`complete_execution` / `fail_execution` の `dry_run=True` は**照合と遷移の検査だけ**を同じ規則 (`_finish`) で行い、判定
(`PROCEED` / `IDEMPOTENT` / `TASK_ONLY`) を返す (何も書かない。拒否は同じ拒否の行と domain error)。コミット点より前に派生値を
書く呼び出し側 (done の pr_number の伝播) が、書く**前**に「この操作は通る」を確かめるのに使う (検査が 2 か所に分かれない)。

## 戻り値

`reserve_task` → `ExecutionContext`。試行を動かす操作 → `Execution`。**試行の無い card (legacy・Director が開いた card・
DETACHED の持ち主でない呼び出し) への操作は task の遷移だけを行い `None`** を返す (execution.md §5.2 の「試行なし」の経路)。
同じ terminal の再送は `Execution(idempotent=True)` で何も書かない。
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import lib_execution as ex  # noqa: E402
import lib_state_store as store  # noqa: E402
import lib_task_status as _status  # noqa: E402

_HOLDING = _status.ASSIGNMENT_HOLDING_STATUSES
_ACCEPTS = _status.ACCEPTS_FROM

# task の status とコマンドの名前は**単独のリテラル**で持つ (集合・組に並べない。status の集合の唯一の定義は
# lib_task_status。`tests/test_task_status_single_definition.py` が AST で落とす)。
_S_PENDING = 'pending'
_S_NEEDS_DIRECTOR = 'needs_director'
_S_FAILED = 'failed'
_S_DONE = 'done'
_S_VERIFIED = 'verified'
_C_DONE = 'done'
_C_FAIL = 'fail'
_C_NEEDS_DIRECTOR = 'needs-director'
_C_VERIFY_RESULT = 'verify-result'
_C_RETIRE = 'retire'
_OP_VERIFY_PASS = 'verify-result:pass'
_OP_VERIFY_FAIL = 'verify-result:fail'

#: 名乗りなしの報告を拒否する操作 (6 コマンドの再送の名前。retire / reset / G1 の fail は含めない。execution.md §20.3)。
#: 実行中 (active) の試行がある card への名乗りなしは `EXECUTION_REQUIRED` (exit 3)。試行なし・DETACHED・TERMINAL は通す
_REPORT_OPERATIONS = frozenset({_C_DONE, _C_FAIL, _C_NEEDS_DIRECTOR, _OP_VERIFY_PASS, _OP_VERIFY_FAIL})

#: `Caller.source` の語彙 (どこから ID を得たか。拒否の文言で env 由来の直し方を言うのに使う)
SOURCES = ('flag', 'env', 'none')

GIT_KEYS = ('branch', 'base', 'pr_base', 'worktree', 'head_at_start')


# ---------------------------------------------------------------------------
# 値
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Caller:
    """操作を打った者が**名乗った**試行 (execution.md §5.2)。agent 名は照合の根拠にしない (AC-04)。

    `execution_id` が None = 名乗りなし。**空文字は名乗りなしに倒さない** (指定された値が不正 = 照合で不一致)。
    `source` は `flag` (`--execution`) / `env` (`CREWVIA_EXECUTION_ID`) / `none`。名乗りがあるのに `none`、
    名乗りがないのに `flag` / `env` は矛盾 (`ValueError`)。`agent` は監査の `actor` を決める側が使う (照合には使わない)。
    """
    execution_id: str | None = None
    source: str = 'none'
    agent: str | None = None

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f"unknown caller source {self.source!r}")
        if (self.execution_id is None) != (self.source == 'none'):
            raise ValueError("Caller.execution_id と source が矛盾しています")

    @property
    def presented(self):
        return self.execution_id is not None


NO_CALLER = Caller()


@dataclasses.dataclass(frozen=True)
class ExecutionContext:
    """`reserve_task` の結果 (pull が JSON と `.crewvia-env` に出す値の元)。"""
    execution_id: str
    attempt: int
    mission: str
    task: str
    agent: str | None
    started_at: str                       # card の `started_at` (= `execution_reserved_at`)
    task_slug: str
    abandoned_execution_id: str | None = None      # 手順 0 で閉じた前の試行 (無ければ None)


@dataclasses.dataclass(frozen=True)
class Execution:
    """試行 1 件の見え方。`idempotent` は「同じ terminal の再送 / 既に running への start で、何も書かなかった」。"""
    execution_id: str
    mission: str
    task: str
    attempt: int
    agent: str | None
    status: str
    end_code: str | None
    reserved_at: str
    running_at: str | None = None
    ended_at: str | None = None
    git: dict = dataclasses.field(default_factory=ex.empty_git)
    idempotent: bool = False

    @staticmethod
    def from_record(record, *, idempotent=False):
        g = ex.empty_git()
        g.update({k: v for k, v in (record.get('git') or {}).items() if k in g})
        return Execution(
            execution_id=record['execution_id'], mission=record['mission'], task=record['task'],
            attempt=record['attempt'], agent=record.get('agent'), status=record['status'],
            end_code=record.get('end_code') or None, reserved_at=record['reserved_at'],
            running_at=record.get('running_at'), ended_at=record.get('ended_at'), git=g,
            idempotent=idempotent)


# ---------------------------------------------------------------------------
# 内部の道具
# ---------------------------------------------------------------------------

def _now_text(now=None):
    return now if now is not None else datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _err(code, message, slug=None, tid=None, execution_id=None):
    return ex.ControllerError(code, message, mission=slug, task=tid,
                              execution_id=execution_id if ex.is_execution_id(execution_id) else None)


def _require_txn(txn):
    if not isinstance(txn, store.Txn):
        raise TypeError("Controller は lib_state_store の Txn (ロック保持中) を受け取ります")


def _load(txn, slug, tid):
    """`(meta, body)`。名前・存在・読めるか・試行の欄の整合を**ここで**確かめる (どれも固定コードの domain error)。"""
    _require_txn(txn)
    try:
        meta, body = txn.load_card(slug, tid)
    except store.InvalidName:
        raise _err(ex.INVALID_ARGUMENT, "mission slug / task id の形が不正です") from None
    except store.CardNotFound:
        raise _err(ex.TASK_NOT_FOUND, f"task {tid!r} が mission {slug!r} に無い", slug, tid) from None
    except store.CardUnreadable as e:
        raise _err(ex.STATE_INVALID, f"{slug}/{tid}: card が読めません ({e.reason})", slug, tid) from None
    problem = ex.fields_problem(meta)
    if problem:
        raise _err(ex.STATE_INVALID, f"{slug}/{tid}: 試行の欄が壊れています ({problem})", slug, tid)
    return meta, body


def _view(meta):
    return ex.attempt_view(meta, _HOLDING)


def _refuse(txn, code, message, slug, tid, meta, caller=None):
    """拒否の行 (監査ログ。ロックの中で即時) を残して domain error を投げる。**card・record・枠は 1 バイトも書かない**。"""
    presented = caller.execution_id if caller is not None else None
    txn.refuse(code, slug, tid, meta.get('status'),
               execution_id=meta.get('current_execution_id'), presented=presented)
    raise _err(code, message, slug, tid, meta.get('current_execution_id'))


def _check_agent_arg(agent):
    if agent is None:
        return None
    # 枠のファイル名として使えるか (`agent_name_problem`) と、空白・制御文字を含まないこと。名前は worker 名 (ポジション) で
    # 英数字に限らない: 今の pull は非 ASCII の名前 (例: 日本語の名前) も受け付けるので、`_safe_token` の英数字だけに
    # 絞らない (COMPAT-01。E2 の cutover で全 pull を止めない)。空白・制御文字 (改行・タブ) は card の frontmatter・
    # identity・record を壊しうるので拒否する。監査ログの `actor` は門 (`_safe_token`) が `unknown` にするので、名前の中身は
    # 監査ログに出ない (record の `agent` と card の `worker` は名前そのもの)。
    if store.agent_name_problem(agent) or re.search(r'[\s\x00-\x1f\x7f]', agent):
        raise _err(ex.INVALID_ARGUMENT, "agent 名が枠のファイル名として使えません")
    return agent


def _is_iso_instant(text):
    """watchdog の `parse_iso_epoch` と同じ読み方 (`Z` → `+00:00`・`datetime.fromisoformat`・タイムゾーン付き) で
    時刻として読めるか。`started_at` は watchdog の idle 時計の起点でもあるので、時刻でない文字列は通さない (E1 O2)。"""
    try:
        parsed = datetime.fromisoformat(text[:-1] + '+00:00' if text.endswith('Z') else text)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _check_generation(now):
    """`now` (= card の `started_at`・`execution_reserved_at`) は世代の形 **かつ時刻として読める** 文字列だけ。
    `'yesterday'` は世代の形 (`[A-Za-z0-9:_.+-]{1,64}`) には合うが時刻ではなく、watchdog が idle の起点として
    読めない (idle 判定が黙って壊れる)。"""
    if not isinstance(now, str) or store._safe_generation(now) is None or not _is_iso_instant(now):
        raise _err(ex.INVALID_ARGUMENT, "now は時刻 (ISO 8601。例 2026-10-01T10:00:00.123456Z) の文字列で指定してください")
    return now


def _check_now(now):
    """`now` を取る全操作 (start / complete / fail / release / reset / abandon) の最初の書き込みの前の検証。
    None (= 今の時刻) か世代の形の文字列だけ。それ以外は `INVALID_ARGUMENT` (record の直列化で落ちて card だけ
    確定した状態を作らない)。reserve は必須の `_check_generation` を使う。"""
    if now is None:
        return None
    return _check_generation(now)


def _check_caller(caller):
    if not isinstance(caller, Caller):
        raise _err(ex.INVALID_ARGUMENT, "caller は Caller で指定してください")
    return caller


def _check_body(body):
    if body is not None and not isinstance(body, str):
        raise _err(ex.INVALID_ARGUMENT, "body は文字列か None")
    return body


def _check_git_context(git_context):
    if git_context is None:
        return None
    if not isinstance(git_context, dict) or not set(git_context) <= set(GIT_KEYS):
        raise _err(ex.INVALID_ARGUMENT, "git_context は branch / base / pr_base / worktree / head_at_start だけ")
    for k, v in git_context.items():
        if v is not None and not isinstance(v, str):
            raise _err(ex.INVALID_ARGUMENT, "git_context の値は文字列か None")
    wt = git_context.get('worktree')
    if wt is not None and not os.path.isabs(wt):
        raise _err(ex.INVALID_ARGUMENT, "git_context.worktree は絶対パスだけ")
    return dict(git_context)


_FORBIDDEN_UPDATE_KEYS = frozenset({'id', 'status', 'worker', 'started_at'} | set(ex.EXECUTION_FIELDS))


def _check_updates(meta_updates, meta_remove=()):
    """card に同時に書く他の欄 (completed_at・pr_number・needs_director_reason …。plan.sh が決める) と、同時に**消す**欄
    (`meta_remove`。fail が前回の証拠を持ち越さない: `fail_head` 等)。status / worker / started_at / 試行の欄は
    Controller だけが書く (消すのも不可)。戻り値: `(updates, remove)`。"""
    if meta_updates is None:
        meta_updates = {}
    if not isinstance(meta_updates, dict) or _FORBIDDEN_UPDATE_KEYS & set(meta_updates):
        raise _err(ex.INVALID_ARGUMENT, "meta_updates は status / worker / started_at / 試行の欄を含められません")
    if (not isinstance(meta_remove, (tuple, list, frozenset, set)) or not all(isinstance(k, str) for k in meta_remove)
            or _FORBIDDEN_UPDATE_KEYS & set(meta_remove) or set(meta_remove) & set(meta_updates)):
        raise _err(ex.INVALID_ARGUMENT, "meta_remove は status / worker / started_at / 試行の欄・meta_updates の欄を含められません")
    try:
        json.dumps(meta_updates, allow_nan=False)           # 直列化できない値は card の書き込みで落ちる (書く前に弾く)
    except (TypeError, ValueError):
        raise _err(ex.INVALID_ARGUMENT, "meta_updates の値は JSON にできる値だけ") from None
    return dict(meta_updates), tuple(meta_remove)


def _owner_slot(meta):
    """終端の後に撤去する枠の持ち主 (操作の**前**の card の worker。名前が使えなければ None)。"""
    w = meta.get('worker')
    return w if isinstance(w, str) and w and store.agent_name_problem(w) is None else None


def _write_record(txn, slug, tid, meta, *, ended_at=None, running_at=None, git=None):
    """record を card に合わせて書く (card の**後**)。無ければ card から作る。読めない・identity が食い違う record は
    消さない・上書きしない (回復 R-5 が報告する)。戻り値: 書いた record か None。"""
    xid = meta['current_execution_id']
    got = txn.read_execution_record(slug, xid)
    if got[0] == 'absent':
        record = ex.record_from_card(meta, slug, tid, git=git)
        if running_at is not None:
            record['running_at'] = running_at
        if ended_at is not None:
            record['ended_at'] = ended_at
    elif got[0] == 'ok' and ex.record_problem(got[1], meta, slug, tid) is None:
        record = ex.follow_card(got[1], meta, ended_at=ended_at, running_at=running_at, git=git)
    else:
        return None
    txn.write_execution_record(slug, xid, record)
    return record


def _execution_of(txn, slug, tid, meta, *, idempotent=False):
    """card (と、あれば record の表示用の欄) から `Execution` を作る。判断には record を使わない。"""
    got = txn.read_execution_record(slug, meta['current_execution_id'])
    if got[0] == 'ok' and ex.record_problem(got[1], meta, slug, tid) is None:
        record = ex.follow_card(got[1], meta)
    else:
        record = ex.record_from_card(meta, slug, tid)
    return Execution.from_record(record, idempotent=idempotent)


# ---------------------------------------------------------------------------
# 照合 (execution.md §5.2 の表。ここが唯一の実装)
# ---------------------------------------------------------------------------

PROCEED = 'proceed'            # 試行を動かす
IDEMPOTENT = 'idempotent'      # 同じ terminal の再送 (何も書かない)
TASK_ONLY = 'task_only'        # 試行の無い card (task の遷移だけ。試行の欄は触らない)

_NOT_CURRENT_END_CODES = frozenset({ex.RESET_BY_DIRECTOR, ex.RETIRED, ex.WORKSPACE_CREATE_FAILED,
                                    ex.ABANDONED_OUTSIDE_CONTROLLER})


def _authorize(txn, slug, tid, meta, caller, *, operation, require_name=False):
    """`(PROCEED|IDEMPOTENT|TASK_ONLY, caller_check)`。拒否は拒否の行を残して domain error。

    `operation` は §4.4 の再送の表で「同じ操作」かを言うための名前 (`done` / `verify-result:pass` / `fail` /
    `needs-director` / `verify-result:fail` / `retire`)。None なら再送を成功にしない操作 (reset 等)。
    """
    view = _view(meta)
    if caller.presented and not ex.is_execution_id(caller.execution_id):
        # 名乗った ID の形が違う (空文字を含む)。ID を詮索しない。
        _refuse(txn, ex.EXECUTION_NOT_FOUND, "名乗った execution id の形が不正です", slug, tid, meta, caller)
    verdict, check = ex.execution_matches(view, meta, execution_id=caller.execution_id)
    if caller.presented:
        if verdict == ex.MATCH:
            if view == ex.TERMINAL:
                return _idempotent_or_conflict(txn, slug, tid, meta, caller, operation)
            return PROCEED, check
        if view == ex.NONE:
            _refuse(txn, ex.EXECUTION_NOT_FOUND,
                    f"{slug}/{tid}: この card はその execution id を発行していません", slug, tid, meta, caller)
        hint = ("" if caller.source != 'env' else
                " (名乗りは環境変数 CREWVIA_EXECUTION_ID 由来です。`unset CREWVIA_EXECUTION_ID` するか --execution で渡してください)")
        _refuse(txn, ex.EXECUTION_NOT_CURRENT,
                f"{slug}/{tid}: 名乗った execution は、この task の今の試行ではありません{hint}", slug, tid, meta, caller)
    # 名乗りなし
    if view == ex.ACTIVE:
        if require_name:
            # E5 PR-2 (execution.md §20.3): 6 つの報告コマンドは、実行中の試行があるなら名乗る。固定文言 + 識別子だけ
            _refuse(txn, ex.EXECUTION_REQUIRED,
                    f"{slug}/{tid}: この task は実行中の試行があります。報告には今の試行の execution id を名乗ってください"
                    f" (--execution <ex-…>。ID は pull の JSON の execution_id か `plan.sh status --mission {slug}` の進行中の行"
                    f" [ex-… attempt N])。何も書いていません。", slug, tid, meta, caller)
        return PROCEED, ex.CHECK_UNVERIFIED
    if view == ex.DETACHED:
        return TASK_ONLY, ex.CHECK_DETACHED
    if view == ex.NONE and meta.get('status') in _HOLDING:
        return TASK_ONLY, ex.CHECK_LEGACY_GENERATION
    return TASK_ONLY, ex.CHECK_NO_EXECUTION


def _idempotent_or_conflict(txn, slug, tid, meta, caller, operation):
    end = meta.get('execution_end_code')
    if operation is not None and ex.IDEMPOTENT_OPERATION.get(end) == operation:
        return IDEMPOTENT, ex.CHECK_VERIFIED
    if end in _NOT_CURRENT_END_CODES:
        _refuse(txn, ex.EXECUTION_NOT_CURRENT,
                f"{slug}/{tid}: その試行は持ち主以外の操作で終わっています", slug, tid, meta, caller)
    _refuse(txn, ex.EXECUTION_ALREADY_TERMINAL,
            f"{slug}/{tid}: その試行は別の結果で終了済みです", slug, tid, meta, caller)


def _check_task_status(txn, command, slug, tid, meta, caller=None):
    """§4.3 の狭めた表。`command` が None なら検査しない (Director の `update --reset` は any → pending)。"""
    if command is not None and meta.get('status') not in _ACCEPTS[command]:
        # 文言は今までの `refuse_transition` (lib_task_status.refusal_reason) と同じ形。status は門を通した値だけ
        allowed = ', '.join(sorted(_ACCEPTS[command]))
        shown = store._safe_status(meta.get('status')) or '<unknown>'
        _refuse(txn, ex.INVALID_TRANSITION,
                f"task '{tid}': {command} は status={shown!r} の task には使えません (受け付けるのは: {allowed})",
                slug, tid, meta, caller)


# ---------------------------------------------------------------------------
# reserve (pull の 1 つ目のロック)
# ---------------------------------------------------------------------------

def reserve_task(txn, slug, tid, agent, *, now, id_factory=None, foreign_slot_checked=False):
    """pending の task を予約する。pending → in_progress / (なし or terminal) → reserved。

    1 トランザクションで: (手順 0: 前の試行 X が card 上で active のまま pending に戻っていたら、Y を発行する**前に** X を
    `failed` / `ABANDONED_OUTSIDE_CONTROLLER` で閉じる) → card (Y・attempt・`execution_reserved_at`・`task_slug`・
    `started_at` = `now`) → record Y → identity → 枠。**card がコミット点**。

    - `agent` は None (枠を作らない = 今の worker 無しの pull) か、枠のファイル名として使える名前。空文字は拒否
      (`INVALID_ARGUMENT`。None に倒さない)。
    - `now` は世代の形の文字列 (plan.sh の `now_generation()`)。`started_at` と `execution_reserved_at` に同じ値を書く。
    - `id_factory` は ID 生成器の注入 (テスト用。既定は UUID v4)。返した値が `ex-<32hex>` でない・既存の試行と
      衝突する (record が既にある・現在の ID と同じ) 場合は `INVALID_ARGUMENT` で何も書かない。
    - attempt は card の `execution_count` + 1 (無ければ 1)。同じロックの中で決めるので並行 reserve で重複しない
      (2 本目は status が pending でないので `TASK_ALREADY_RESERVED`)。
    - 依存・skill・target_dir・busy の判定はしない (pull の候補選びの仕事。呼び出し側が先に済ませる)。
    - `foreign_slot_checked` (既定 False): 呼び出し側が「`agent` の枠が別の task を指していても、それは**孤児** (手放し済みの
      task・無い mission を指す) で上書きしてよい」と確かめ済みのときだけ True。False なら、別の task を指す枠・読めない枠を
      `TASK_NOT_ELIGIBLE` で拒否する (孤児かどうかは他の card の status で決まり、Controller は他の card を読まない)。
      plan.sh の pull は `agent_busy_elsewhere` (card の in_progress と枠の両方・孤児は上書き・読めない枠は拒否) を先に通すので True
      (今の pull が孤児の枠を上書きする挙動を保つ。COMPAT-02)。
    """
    agent = _check_agent_arg(agent)
    _check_generation(now)
    meta, body = _load(txn, slug, tid)
    status = meta.get('status')
    if status not in _ACCEPTS['pull']:
        if status in _HOLDING:
            _refuse(txn, ex.TASK_ALREADY_RESERVED, f"{slug}/{tid}: 既に予約・実行中です", slug, tid, meta)
        raise _err(ex.TASK_NOT_ELIGIBLE,
                   f"{slug}/{tid}: status={store._safe_status(status) or '<unknown>'} の task は予約できません", slug, tid)
    if agent is not None and not foreign_slot_checked and txn.classify_assignment(agent, slug, tid, None) in (
            store.ASSIGN_OTHER_TASK, store.ASSIGN_UNVERIFIABLE):
        raise _err(ex.TASK_NOT_ELIGIBLE, f"{slug}/{tid}: {agent} の枠が別の task を指している (または読めない)", slug, tid)

    # 候補の ID の検証は**最初の書き込み (手順 0 の `_abandon`) より前**に終える (拒否は何も書かない)
    xid = (id_factory or ex.uuid4_execution_id)()
    if not ex.is_execution_id(xid):
        raise _err(ex.INVALID_ARGUMENT, "id_factory が execution id の形でない値を返しました", slug, tid)
    if xid == meta.get('current_execution_id') or txn.read_execution_record(slug, xid)[0] != 'absent':
        raise _err(ex.INVALID_ARGUMENT, "id_factory が既存の試行と同じ execution id を返しました", slug, tid)

    abandoned = None
    if _view(meta) == ex.DETACHED and meta['execution_status'] in ex.ACTIVE_STATUSES:
        abandoned = meta['current_execution_id']
        meta = _abandon(txn, slug, tid, meta, body, now=None)

    previous_status = status
    meta = dict(meta)
    meta.update(status='in_progress', worker=agent, started_at=now, current_execution_id=xid,
                execution_status=ex.RESERVED, execution_count=int(meta.get('execution_count') or 0) + 1,
                execution_reserved_at=now,
                task_slug=meta.get('task_slug') or ex.slugify_title(meta.get('title') or '', tid))
    meta.pop('execution_end_code', None)
    if agent is None:
        meta.pop('execution_agent', None)
    else:
        meta['execution_agent'] = agent
    txn.write_card(slug, tid, meta, body)                       # コミット点
    _write_record(txn, slug, tid, meta)
    if agent is not None:
        txn.publish_assignment(agent, slug, tid, now, execution_id=xid)
    txn.record(slug, tid, previous_status, 'in_progress', now, execution_id=xid)
    return ExecutionContext(execution_id=xid, attempt=meta['execution_count'], mission=slug, task=tid,
                            agent=agent, started_at=now, task_slug=meta['task_slug'],
                            abandoned_execution_id=abandoned)


def _abandon(txn, slug, tid, meta, body, *, now):
    """DETACHED で active な試行 X を `failed` / `ABANDONED_OUTSIDE_CONTROLLER` で閉じる (card → record X)。
    task の欄 (status / worker / started_at) は触らない。書いた後の meta を返す。reserve の手順 0・`reset_task`・
    `abandon_detached_execution` の 3 か所が同じ書き込みを使う (execution.md §1.4)。"""
    xid = meta['current_execution_id']
    meta = dict(meta)
    meta.update(execution_status=ex.FAILED, execution_end_code=ex.ABANDONED_OUTSIDE_CONTROLLER)
    txn.write_card(slug, tid, meta, body)                       # 終端が正本に先に入る
    _write_record(txn, slug, tid, meta, ended_at=_now_text(now))
    txn.report('reported:stale_execution_status', slug, tid, execution_id=xid)
    return meta


def abandon_detached_execution(txn, slug, tid, *, now=None):
    """Director が使う「閉じる手段」: DETACHED で active なまま残った試行 (旧コードが card を手放した後の X) を、task に
    触れずに `failed` / `ABANDONED_OUTSIDE_CONTROLLER` にする。終わった task (done / failed 等) に残る欄と record を
    履歴として閉じるのが主な用途 (store-check の `execution_active_on_finished_task`)。
    DETACHED でない・active でない試行は `INVALID_TRANSITION` (何も書かない・拒否の行は残す)。呼び出し元は E4 が足す。"""
    _check_now(now)
    meta, body = _load(txn, slug, tid)
    if _view(meta) != ex.DETACHED or meta.get('execution_status') not in ex.ACTIVE_STATUSES:
        _refuse(txn, ex.INVALID_TRANSITION, f"{slug}/{tid}: 閉じるべき DETACHED の試行がありません", slug, tid, meta)
    status = meta.get('status')
    meta = _abandon(txn, slug, tid, meta, body, now=now)
    txn.record(slug, tid, status, status, meta.get('started_at'), execution_id=meta['current_execution_id'])
    return _execution_of(txn, slug, tid, meta)


# ---------------------------------------------------------------------------
# start (pull の 2 つ目のロック)
# ---------------------------------------------------------------------------

def start_execution(txn, slug, tid, execution_id, git_context=None, *, now=None):
    """reserved → running。名指しの card の `current_execution_id` と `execution_id` が一致し、`ACTIVE` のときだけ。

    - 既に running なら**冪等** (何も書かず `Execution(idempotent=True)`)。「reserved だったか」を CAS として使う
      呼び出し側 (E2 の pull) は `.idempotent` / `.status` で見る。
    - DETACHED・別の ID は `EXECUTION_NOT_CURRENT`、試行なしは `EXECUTION_NOT_FOUND`、terminal は `INVALID_TRANSITION`。
    - `git_context` (branch / base / pr_base / worktree (絶対パス) / head_at_start) は record の `git` に入れる
      (判断には使わない。W2 で前の試行の commit が残った worktree を再利用したとき、どの commit から始めた試行かを追う)。
    """
    git = _check_git_context(git_context)
    _check_now(now)
    meta, body = _load(txn, slug, tid)
    caller = Caller(execution_id, 'flag') if execution_id is not None else NO_CALLER
    view = _view(meta)
    if execution_id is None or not ex.is_execution_id(execution_id):
        _refuse(txn, ex.EXECUTION_NOT_FOUND, "start には execution id の形が正しい値が要ります", slug, tid, meta, caller)
    if view == ex.NONE:
        _refuse(txn, ex.EXECUTION_NOT_FOUND, f"{slug}/{tid}: この card は試行を持っていません", slug, tid, meta, caller)
    if view == ex.DETACHED or execution_id != meta['current_execution_id']:
        _refuse(txn, ex.EXECUTION_NOT_CURRENT,
                f"{slug}/{tid}: 名乗った execution は、この task の今の試行ではありません", slug, tid, meta, caller)
    if view == ex.TERMINAL:
        _refuse(txn, ex.INVALID_TRANSITION, f"{slug}/{tid}: 終了済みの試行は開始できません", slug, tid, meta, caller)
    if meta['execution_status'] == ex.RUNNING:
        return _execution_of(txn, slug, tid, meta, idempotent=True)
    meta = dict(meta)
    meta['execution_status'] = ex.RUNNING
    txn.write_card(slug, tid, meta, body)                       # コミット点
    _write_record(txn, slug, tid, meta, running_at=_now_text(now), git=git)
    txn.record(slug, tid, meta.get('status'), meta.get('status'), meta.get('started_at'),
               execution_id=execution_id, caller_check=ex.CHECK_VERIFIED)
    return _execution_of(txn, slug, tid, meta)


# ---------------------------------------------------------------------------
# 終わらせる操作 (complete / fail / release / reset)
# ---------------------------------------------------------------------------

#: fail_execution の failure_code → (task の to_status の許可, 受け付ける元の command, 再送の操作名, owner を外すか, 枠を撤去するか)
_FAIL_RULES = {
    ex.WORKER_FAILED: (frozenset({_S_FAILED}), _C_FAIL, _C_FAIL, False, True),
    ex.NEEDS_DIRECTOR: (frozenset({_S_NEEDS_DIRECTOR}), _C_NEEDS_DIRECTOR, _C_NEEDS_DIRECTOR, False, True),
    ex.VERIFICATION_REJECTED: (frozenset({_S_PENDING}), _C_VERIFY_RESULT, _OP_VERIFY_FAIL, True, True),
    ex.RESET_BY_DIRECTOR: (frozenset({_S_PENDING}), None, None, True, True),
    ex.RETIRED: (frozenset({_S_PENDING, _S_NEEDS_DIRECTOR}), _C_RETIRE, _C_RETIRE, None, True),   # owner: pending のときだけ外す
    ex.WORKSPACE_CREATE_FAILED: (frozenset({_S_NEEDS_DIRECTOR}), _C_NEEDS_DIRECTOR, None, False, True),
}
_RELEASE_RULES = {ex.RESET_BY_DIRECTOR: None, ex.RETIRED: _C_RETIRE}


def _apply_updates(out, updates, remove):
    out.update(updates)
    for key in remove:
        out.pop(key, None)


def _finish(txn, slug, tid, meta, body, caller, *, new_status, end_code, to_status, command, operation,
            clear_owner, retire_slot, meta_updates, new_body, now, required_exec_status=None,
            abandon_detached=False, meta_remove=(), dry_run=False):
    """終わらせる操作の共通の骨 (照合 → 遷移の検査 → card → record → 枠 → 監査)。

    `dry_run=True` は**照合と遷移の検査だけ**を同じ規則で行い、何も書かずに判定 (`PROCEED` / `IDEMPOTENT` / `TASK_ONLY`)
    を返す (拒否は同じ拒否の行と domain error)。呼び出し側が、コミット点より前に派生値を書く操作 (done の pr_number の
    伝播) の**前**に「この操作は通る」を確かめるのに使う — 検査が 2 か所に分かれず、書いた後で拒否になる窓が無い。
    """
    _check_caller(caller)
    _check_now(now)
    _check_body(new_body)
    decision, check = _authorize(txn, slug, tid, meta, caller, operation=operation,
                                 require_name=operation in _REPORT_OPERATIONS)
    updates, remove = _check_updates(meta_updates, meta_remove)
    body_out = body if new_body is None else new_body
    previous = meta.get('status')
    owner = _owner_slot(meta)
    hint = owner if check == ex.CHECK_VERIFIED else None        # 監査の actor が unknown のときの補い (§8)

    if decision == IDEMPOTENT:
        return decision if dry_run else _execution_of(txn, slug, tid, meta, idempotent=True)

    if decision == TASK_ONLY:
        _check_task_status(txn, command, slug, tid, meta, caller)               # 書く前に遷移を検査する
        if dry_run:
            return decision
        if abandon_detached and _view(meta) == ex.DETACHED and meta.get('execution_status') in ex.ACTIVE_STATUSES:
            # 試行は reset の前に Controller の外で手放されていた: RESET ではなく ABANDONED で閉じる (§4.2)
            meta = _abandon(txn, slug, tid, meta, body, now=now)
        out = dict(meta)
        _apply_updates(out, updates, remove)
        out['status'] = to_status
        if clear_owner:
            out.update(worker=None, started_at=None)
        txn.write_card(slug, tid, out, body_out)
        if retire_slot and owner is not None:
            txn.retire_assignment(owner, slug, tid, None)
        txn.record(slug, tid, previous, to_status, out.get('started_at'),
                   execution_id=out.get('current_execution_id'), caller_check=check, actor_hint=hint)
        return None

    # PROCEED: 試行を動かす
    current = meta['execution_status']
    if required_exec_status is not None and current not in required_exec_status:
        _refuse(txn, ex.INVALID_TRANSITION,
                f"{slug}/{tid}: 試行の status={current} からはこの操作に進めません", slug, tid, meta, caller)
    if new_status not in ex.TRANSITIONS[current]:
        _refuse(txn, ex.INVALID_TRANSITION,
                f"{slug}/{tid}: 試行を {current} から {new_status} にはできません", slug, tid, meta, caller)
    _check_task_status(txn, command, slug, tid, meta, caller)
    if dry_run:
        return decision
    out = dict(meta)
    _apply_updates(out, updates, remove)
    out.update(status=to_status, execution_status=new_status, execution_end_code=end_code)
    if clear_owner:
        out.update(worker=None, started_at=None)
    txn.write_card(slug, tid, out, body_out)                    # 終了コードも status と同じ書き込みに入る (コミット点)
    _write_record(txn, slug, tid, out, ended_at=_now_text(now))
    if retire_slot and owner is not None:
        txn.retire_assignment(owner, slug, tid, None)
    txn.record(slug, tid, previous, to_status, out.get('started_at'),
               execution_id=out['current_execution_id'], caller_check=check, actor_hint=hint)
    return _execution_of(txn, slug, tid, out)


def complete_execution(txn, slug, tid, caller=NO_CALLER, *, to_status, meta_updates=None, meta_remove=(), body=None,
                       now=None, dry_run=False):
    """running → completed。`to_status` が `done` (終了コード `DONE`・枠を撤去) か `verified` (`VERIFIED`・枠は残す。
    R-2 が後で消す。state-store.md §7)。task の遷移元は §4.3 の狭めた表 (done は in_progress だけ・verified は検証待ち)。

    `meta_updates` / `body` は plan.sh が決めた他の欄 (`completed_at`・`pr_number`・Result 本文) を**同じ card の書き込み**に
    入れるため。status / worker / started_at / 試行の欄は渡せない。同じ ID の同じ操作の再送は冪等 (何も書かない)。
    `dry_run=True` は照合と遷移の検査だけを行い判定 (`PROCEED` / `IDEMPOTENT` / `TASK_ONLY`) を返す (何も書かない)。
    """
    rules = {_S_DONE: (ex.DONE, _C_DONE, _C_DONE, True),
             _S_VERIFIED: (ex.VERIFIED, _C_VERIFY_RESULT, _OP_VERIFY_PASS, False)}
    if to_status not in rules:
        raise _err(ex.INVALID_ARGUMENT, "complete の to_status は done / verified だけ", slug, tid)
    end, command, operation, retire = rules[to_status]
    meta, card_body = _load(txn, slug, tid)
    return _finish(txn, slug, tid, meta, card_body, caller, new_status=ex.COMPLETED, end_code=end,
                   to_status=to_status, command=command, operation=operation, clear_owner=False,
                   retire_slot=retire, meta_updates=meta_updates, new_body=body, now=now,
                   required_exec_status={ex.RUNNING}, meta_remove=meta_remove, dry_run=dry_run)


def fail_execution(txn, slug, tid, caller=NO_CALLER, failure_code=None, *, to_status, meta_updates=None,
                   meta_remove=(), body=None, now=None, dry_run=False):
    """reserved | running → failed。`failure_code` は終了コード (`_FAIL_RULES` の 6 つ)、`to_status` は task の遷移先で、
    組み合わせは §4.2 の表だけ:

    | failure_code | to_status | worker / 枠 |
    |---|---|---|
    | WORKER_FAILED (fail) | failed | worker 残す・枠撤去 |
    | NEEDS_DIRECTOR (needs-director) | needs_director | 同上 |
    | VERIFICATION_REJECTED (verify-result fail) | pending | worker・started_at を null・枠撤去 |
    | RESET_BY_DIRECTOR (update --reset) | pending | 同上 (試行が無い / DETACHED の扱いは `reset_task`) |
    | RETIRED (retire) | pending / needs_director | pending のときだけ worker・started_at を null。枠撤去 |
    | WORKSPACE_CREATE_FAILED (G1) | needs_director | worker 残す・枠撤去 |
    """
    rule = _FAIL_RULES.get(failure_code)
    if rule is None:
        raise _err(ex.INVALID_ARGUMENT, "failure_code が不明です", slug, tid)
    allowed, command, operation, clear_owner, retire = rule
    if to_status not in allowed:
        raise _err(ex.INVALID_ARGUMENT, f"{failure_code} と to_status の組み合わせが表にありません", slug, tid)
    if clear_owner is None:
        clear_owner = to_status == _S_PENDING
    meta, card_body = _load(txn, slug, tid)
    return _finish(txn, slug, tid, meta, card_body, caller, new_status=ex.FAILED, end_code=failure_code,
                   to_status=to_status, command=command, operation=operation, clear_owner=clear_owner,
                   retire_slot=retire, meta_updates=meta_updates, new_body=body, now=now,
                   required_exec_status=({ex.RESERVED} if failure_code == ex.WORKSPACE_CREATE_FAILED else None),
                   abandon_detached=(failure_code == ex.RESET_BY_DIRECTOR), meta_remove=meta_remove, dry_run=dry_run)


def release_execution(txn, slug, tid, caller=NO_CALLER, reason_code=None, *, to_status=_S_PENDING, now=None,
                      meta_updates=None, body=None):
    """reserved → released (まだ誰も作業を始めていない試行を手放す)。`reason_code` は `RESET_BY_DIRECTOR` / `RETIRED`。
    running の試行は released にできない (`INVALID_TRANSITION`。結果なしで手放すのは `fail_execution`)。
    `to_status` は pending (worker・started_at を null) か、RETIRED の `needs_director` (worker を残す)。枠は撤去する。
    `meta_updates` / `body` は `fail_execution` と同じ (retire の needs-director が理由を同じ card の書き込みに入れる)。"""
    if reason_code not in _RELEASE_RULES:
        raise _err(ex.INVALID_ARGUMENT, "reason_code は RESET_BY_DIRECTOR / RETIRED だけ", slug, tid)
    allowed = _FAIL_RULES[reason_code][0]
    if to_status not in allowed:
        raise _err(ex.INVALID_ARGUMENT, f"{reason_code} と to_status の組み合わせが表にありません", slug, tid)
    command = _RELEASE_RULES[reason_code]
    meta, card_body = _load(txn, slug, tid)
    return _finish(txn, slug, tid, meta, card_body, caller, new_status=ex.RELEASED, end_code=reason_code,
                   to_status=to_status, command=command, operation=command, clear_owner=(to_status == _S_PENDING),
                   retire_slot=True, meta_updates=meta_updates, new_body=body, now=now,
                   required_exec_status={ex.RESERVED}, abandon_detached=(reason_code == ex.RESET_BY_DIRECTOR))


def reset_task(txn, slug, tid, caller=NO_CALLER, *, now=None, meta_updates=None):
    """Director の `update --reset`: task を pending に戻し、試行は次のどれかで閉じる (execution.md §4.2)。

    - ACTIVE: reserved → released / running → failed (どちらも `RESET_BY_DIRECTOR`)
    - DETACHED で active: `failed` / `ABANDONED_OUTSIDE_CONTROLLER` (試行は reset の前に Controller の外で手放されていた)
    - 試行なし (legacy)・TERMINAL: **task の遷移だけ** (terminal の欄は残す)
    worker・started_at を null にし、枠を撤去する。遷移元は問わない (今の `update` と同じ。any → pending)。
    `meta_updates` は同じ card の書き込みに入れる他の欄 (`update --reset` の `completed_at = None`)。
    """
    meta, body = _load(txn, slug, tid)
    view = _view(meta)
    if view == ex.ACTIVE and meta['execution_status'] == ex.RESERVED:
        return _finish(txn, slug, tid, meta, body, caller, new_status=ex.RELEASED, end_code=ex.RESET_BY_DIRECTOR,
                       to_status=_S_PENDING, command=None, operation=None, clear_owner=True, retire_slot=True,
                       meta_updates=meta_updates, new_body=None, now=now, required_exec_status={ex.RESERVED})
    return _finish(txn, slug, tid, meta, body, caller, new_status=ex.FAILED, end_code=ex.RESET_BY_DIRECTOR,
                   to_status=_S_PENDING, command=None, operation=None, clear_owner=True, retire_slot=True,
                   meta_updates=meta_updates, new_body=None, now=now, abandon_detached=True)


#: `mark_task` が扱う (command, to_status)。どれも**試行を変えない** task の遷移。verify-result の `needs_human_review`
#: (verdict が needs_human_review、または fail で rework が上限に達した) は検証待ちの task を人間の判断待ちにするだけで、
#: 試行 (running) はそのまま (§4.2)。
_MARK_TARGETS = {'ready-for-verification': 'ready_for_verification', 'verifying': 'verifying',
                 _C_VERIFY_RESULT: 'needs_human_review'}


def mark_task(txn, slug, tid, caller=NO_CALLER, *, command, to_status, meta_updates=None, meta_remove=(), body=None):
    """**試行を変えない** task の遷移 (ready-for-verification: in_progress → ready_for_verification・
    verifying: ready_for_verification → verifying・verify-result の needs_human_review: 検証待ち → needs_human_review)。
    試行が active なら名乗りを照合する (違えば拒否)。試行の欄・record・枠には触らない。
    戻り値は今の試行 (試行なしなら None)。"""
    if _MARK_TARGETS.get(command) != to_status:
        raise _err(ex.INVALID_ARGUMENT, "mark_task は ready-for-verification / verifying / verify-result(needs_human_review) の遷移だけ", slug, tid)
    _check_caller(caller)
    _check_body(body)
    meta, card_body = _load(txn, slug, tid)
    # operation=None: 再送を成功にする操作ではない (TERMINAL の試行に名乗り付きで来れば `_authorize` が conflict で拒否する)
    decision, check = _authorize(txn, slug, tid, meta, caller, operation=None, require_name=True)
    _check_task_status(txn, command, slug, tid, meta, caller)
    updates, remove = _check_updates(meta_updates, meta_remove)
    out = dict(meta)
    _apply_updates(out, updates, remove)
    out['status'] = to_status
    txn.write_card(slug, tid, out, card_body if body is None else body)
    owner = _owner_slot(meta)
    txn.record(slug, tid, meta.get('status'), to_status, out.get('started_at'),
               execution_id=out.get('current_execution_id'), caller_check=check,
               actor_hint=owner if check == ex.CHECK_VERIFIED else None)
    return _execution_of(txn, slug, tid, out) if decision == PROCEED else None


# ---------------------------------------------------------------------------
# 読み取り
# ---------------------------------------------------------------------------

def get_execution(queue_dir, slug, execution_id):
    """試行の record を読む (ロック不要・履歴の表示用。**判断には使わない**)。

    形が不正な ID・無い record は `EXECUTION_NOT_FOUND`、読めない・identity の食い違う record は `STATE_INVALID`
    (空・「無い」に潰さない)。"""
    if not ex.is_execution_id(execution_id):
        raise _err(ex.EXECUTION_NOT_FOUND, "execution id の形が不正です", slug)
    try:
        got = store.read_execution_record(queue_dir, slug, execution_id)
    except store.InvalidName:
        raise _err(ex.INVALID_ARGUMENT, "mission slug の形が不正です") from None
    if got[0] == 'absent':
        raise _err(ex.EXECUTION_NOT_FOUND, f"{slug}: execution {execution_id} の record が無い", slug, None, execution_id)
    if got[0] == 'unreadable':
        raise _err(ex.STATE_INVALID, f"{slug}: record が読めません ({got[1]})", slug, None, execution_id)
    record = got[1]
    ok = (ex.record_shape_problem(record) is None and record.get('execution_id') == execution_id
          and record.get('mission') == slug)
    if not ok:
        raise _err(ex.STATE_INVALID, f"{slug}: record の形が不正です", slug, None, execution_id)
    return Execution.from_record(record)
