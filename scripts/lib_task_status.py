"""task の status の語彙と許可遷移 —— crewvia の中で唯一の置き場 (vNext 01a S1)。

設計と根拠: `knowledge/state-store.md` §1。

status の集合は以前 lint_plan.py / plan.sh (update・TERMINAL・PR_NOT_AWAITED・
ORPHAN_ASSIGNMENT_FINISHED・retire) / dispatcher.sh (TERMINAL・RELEASED) /
taskvia-sync.sh / lib_dep_rules.py にコピーがあり、揃え漏れが実害になっていた
(`needs_director` を plan.sh 自身が書くのに lint が FAIL / `cancelled` は書き手が
無いのに依存判定に現れる / done を受け付ける元の status が command ごとに違う)。

このモジュールは **データだけ** を持つ (I/O なし・他のモジュールを import しない)。
`lib_dep_rules.py` と同じ作法で、import して使う。値を並べ直したコピーは
`tests/test_task_status_single_definition.py` が落とす。

**フォールバックを持たない**。読み込めなければ呼び出し側はそのまま死ぬ。
「読めなかったら自前の集合で続行」に倒すと、集合が 1 つであるという性質そのものが
壊れた環境でだけ崩れる。
"""

from __future__ import annotations

#: 書いてよい status の全集合。`cancelled` は含めない (書き手が無く、本番カードも
#: 0 件。「やらない」は `skipped` が担う。knowledge/state-store.md §1.3)。
#: `corrupted` (読み取りの擬似 status。lib_task_cards.CORRUPT_TASK_STATUS) も
#: 書かないので含めない。
TASK_STATUSES = frozenset({
    'pending',
    'in_progress',
    'needs_director',
    'done',
    'failed',
    'ready_for_verification',
    'verifying',
    'verified',
    'needs_human_review',
    'blocked',
    'skipped',
    'verification_failed',
})

#: 依存を満たす完了。
TERMINAL_STATUSES = frozenset({'done', 'verified', 'skipped'})

#: 「もう完了しないが、誰の判断も経ていない」status。依存としては保留 (HELD) で、
#: 進める出口は Director の `plan.sh release-dep` だけ。依存の意味づけは
#: lib_dep_rules.py が持つが、語彙の分類はここが 1 つだけ持つ (RELEASED の定義に要る)。
HELD_DEP_STATUSES = ('failed',)

#: 「Worker がその card をもう手放している」status。TERMINAL (= 依存が満たされた) とは
#: 問いが違う: `failed` は依存を満たさない (HELD) が、Worker は手放している。
#: Worker の生死・Kai-codex の孤児判定・孤児 assignment の撤去がこれを使う。
RELEASED_WORK_STATUSES = frozenset(TERMINAL_STATUSES | set(HELD_DEP_STATUSES))

#: assignment を持ったままの status (in_progress の他、検証・判断待ちの間も card は
#: まだ Worker のもの)。§2 の projection の定義にも使う。
ASSIGNMENT_HOLDING_STATUSES = frozenset({
    'in_progress',
    'ready_for_verification',
    'verifying',
    'needs_human_review',
})

#: Worker がいま実行している (検証中を含む) status。task id が複数 mission に当たったとき、
#: 環境変数の mission を信じてよいのはこの間だけ (plan.sh `_env_mission_for_task`)。
EXECUTING_STATUSES = frozenset({'in_progress', 'verifying'})

#: Director の判断待ち (`plan.sh needs-director` が書く)。assignment は外れるが card の
#: worker は残る。
WAITS_ON_DIRECTOR_STATUSES = frozenset({'needs_director'})

#: もう PR 番号を待っていない status (`codex_reviews_awaiting_pr` が数えない)。
PR_NOT_AWAITED_STATUSES = frozenset({
    'done', 'verified', 'failed', 'skipped', 'verification_failed',
})

#: コマンドごとの「受け付ける元の status」。**S1 は現状を写す** (狭めるのは 01c)。
#: 「〜以外すべて」だった受け付け方は、語彙 (TASK_STATUSES) からの引き算で書く —— 語彙に
#: 無い status (`cancelled` の手書きカード・status 欄なし) は、以前は「以外すべて」に
#: 含まれて通っていたが、いまは拒否される。
_ALREADY_FINISHED = frozenset({'done', 'verified', 'failed', 'skipped'})

ACCEPTS_FROM = {
    'pull': frozenset({'pending'}),
    'needs-director': frozenset({'in_progress'}),
    'done': TASK_STATUSES - _ALREADY_FINISHED - WAITS_ON_DIRECTOR_STATUSES,
    'fail': TASK_STATUSES - _ALREADY_FINISHED,
    'ready-for-verification': frozenset({'in_progress'}),
    'verify-result': TASK_STATUSES - _ALREADY_FINISHED,
    'retire': frozenset({'in_progress'}),
    'verifying': frozenset({'ready_for_verification'}),
}


def accepts(command, status):
    """`command` が `status` の task を受け付けるか。知らない command は KeyError。"""
    return status in ACCEPTS_FROM[command]


def refusal_reason(command, status):
    """`accepts()` が False のときの 1 行の理由 (どのコマンドも同じ書き方)。"""
    allowed = ', '.join(sorted(ACCEPTS_FROM[command]))
    return (f"{command} は status={status!r} の task には使えません"
            f" (受け付けるのは: {allowed})")
