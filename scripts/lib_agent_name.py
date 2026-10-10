"""Worker 名が assignment ファイル名として使えるか —— crewvia の中で唯一の定義。

`plan.sh pull --agent` が受け付ける名前と、dispatcher が割り当て文に貼る名前を
同じ規則で決めるために、queue の書き込み lib (State Store) から切り出した (PR #294 Codex P2)。
dispatcher は State Store を import しない取り決め
(`tests/test_state_store_callers.py`。名前が出ただけで呼び出し元と数える) なので、規則だけをここに置く。

このモジュールは **データと純粋関数だけ** を持つ (I/O なし・標準ライブラリ以外を
import しない)。State Store は同じ名前で re-export する (plan.sh の
`_STORE.agent_name_problem` 等はそのまま)。

`lib_worker_target.agent_name_problem` は別の規則 (予約 suffix を見ない。
registry の記録のファイル名) で、ここには寄せていない。
"""

from __future__ import annotations

import shlex

IDENTITY_SUFFIX = '.identity'
#: assignments ディレクトリで別の意味を持つ suffix。Worker 名として使わせない。
RESERVED_AGENT_SUFFIXES = (IDENTITY_SUFFIX, '.restarting', '.tmp')


def agent_name_problem(agent):
    """Worker 名が assignment ファイル名として使えない理由。使えるなら None。"""
    if not isinstance(agent, str) or not agent or '/' in agent or '\0' in agent \
            or agent in ('.', '..') or agent.startswith('.'):
        return "'/' や先頭の '.' を含まない名前にしてください"
    for suffix in RESERVED_AGENT_SUFFIXES:
        if agent.endswith(suffix):
            return f"'{suffix}' で終わる名前は queue/assignments/ で予約済みです"
    return None


def shell_pasteable_agent_name_problem(agent):
    """Worker 名を指示文の `--agent <名前>` にクォートなしで貼れない理由。貼れるなら None。

    `agent_name_problem` に加え、前後の空白・シェルで形が変わる名前 (空白・`;`・`$(...)` など) を断る。
    dispatcher の `pull_agent_flag` と benchmark-ctx.sh が同じ答えになるよう、ここに 1 つだけ置く。
    """
    problem = agent_name_problem(agent)
    if problem:
        return problem
    if not agent.strip() or agent != agent.strip() or shlex.quote(agent) != agent:
        return "空白・引用符・シェルのメタ文字を含まない名前にしてください"
    return None
