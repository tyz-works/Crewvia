"""Worker 名が assignment ファイル名として使えるか —— crewvia の中で唯一の定義。

`plan.sh pull --agent` が受け付ける名前と、dispatcher が割り当て文に貼る名前を
同じ規則で決めるために、queue の書き込み lib (State Store) から切り出した (PR #294 Codex P2)。
dispatcher は State Store を import しない取り決め
(`tests/test_state_store_callers.py`。名前が出ただけで呼び出し元と数える) なので、規則だけをここに置く。

このモジュールは **データと純粋関数だけ** を持つ (I/O なし・標準ライブラリの re 以外を
import しない)。State Store は同じ名前で re-export する (plan.sh の
`_STORE.agent_name_problem` 等はそのまま)。

`lib_worker_target.agent_name_problem` は別の規則 (予約 suffix を見ない。
registry の記録のファイル名) で、ここには寄せていない。
"""

from __future__ import annotations

import re

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


#: 指示文の `--agent <名前>` にクォートなしで貼ってよい名前の**正の形** (許可リスト)。
#: 英数字で始まり、英数字・`_`・`.`・`-` だけ。先頭が `-` だと plan.sh の parse_opts が option と読んで
#: 拒否し、空白・`;`・`$(...)` はシェルで形が変わる。拒否する形を足していくのをやめて、通す形を 1 つ決める。
#: 本番の Worker 名 (config/worker-names.yaml・registry/workers.yaml・`Kai-codex` 等) は全部この形で、
#: tests/test_agent_name_pull_roundtrip.py が確かめている。
PASTEABLE_AGENT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')


def shell_pasteable_agent_name_problem(agent):
    """Worker 名を指示文の `--agent <名前>` にクォートなしで貼れない理由。貼れるなら None。

    `agent_name_problem` (assignment のファイル名として使えるか) に加え、`PASTEABLE_AGENT_NAME` の
    許可リストに合わない名前を断る。dispatcher の `pull_agent_flag` と benchmark-ctx.sh が同じ答えに
    なるよう、ここに 1 つだけ置く。通した名前が `plan.sh pull` の引数解析で同じ名前として
    受理されることは、往復の性質テストが本物の parse_opts で確かめる。
    """
    problem = agent_name_problem(agent)
    if problem:
        return problem
    if not PASTEABLE_AGENT_NAME.fullmatch(agent):
        return "英数字で始まり、英数字・'_'・'.'・'-' だけの名前にしてください (空白・引用符・シェルのメタ文字・先頭の '-' は不可)"
    return None
