#!/usr/bin/env python3
"""task-graph の「読んでから書くまで」の隙間を、外から開かせるための実行役。

`refresh_task_graph()` の退行は *読み取りと publish のあいだに別の publish が
挟まる* ときにだけ起きる。実時間の競争で再現しようとすると必ずフレークになるので、
この harness は **本番の `refresh_task_graph()` をそのまま呼びつつ**、publish の
直前で止まってテスト側の合図を待つ。

止め方は「本番にテスト用のフックを足す」ことではない。plan.sh の python 本体を
そのまま名前空間に読み込み、その名前空間の中の協力者 (`_atomic_write`) だけを
差し替える — 呼ばれる `refresh_task_graph()` は本番のコードそのものである。

別プロセスにしてあるのは flock のため。同じプロセスの中で 2 つの publish を
走らせると、ロックの奪い合いがプロセス境界をまたがず、直列化されているのか
どうかをテストが言い当てられない。

使い方 (テスト側から subprocess で起動する):

    python3 tests/task_graph_publisher_harness.py \
        --plan <plan.sh> --queue <queue> --repo-root <root> \
        --reached <file> --go <file>

`--reached` は「queue を読み終えて publish の直前まで来た」印。
`--go` が現れるまでそこで待ち、現れたら本番どおり publish して終了する。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import time

#: `--go` を待つ上限。テストが落ちても harness が居座らないための保険。
GO_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 0.01

#: plan.sh に dispatch されていないサブコマンド名。全ての def を定義し終えた
#: あとの「Unknown subcommand」で SystemExit させ、コマンドは 1 つも走らせない。
NOOP_SUBCOMMAND = '__task_graph_harness__'


def plan_python_source(plan_sh: pathlib.Path) -> str:
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", plan_sh.read_text(), re.DOTALL)
    if not m:
        raise SystemExit(f"{plan_sh}: python ヒアドキュメント (PYEOF) が見つからない")
    return m.group(1)


def load_plan_namespace(plan_sh: pathlib.Path, queue: str, repo_root: str) -> dict:
    """plan.sh の python 本体を名前空間に読み込む (コマンドは走らせない)。"""
    ns: dict = {'__name__': '__main__', '__file__': str(plan_sh)}
    argv = sys.argv
    sys.argv = ['-', queue, NOOP_SUBCOMMAND, repo_root]
    try:
        exec(compile(plan_python_source(plan_sh), str(plan_sh), 'exec'), ns)
    except SystemExit:
        # 想定どおり: 未知のサブコマンドとして弾かれた。def は全て揃っている。
        pass
    finally:
        sys.argv = argv
    missing = [n for n in ('refresh_task_graph', '_atomic_write', 'task_graph_path')
               if n not in ns]
    if missing:
        raise SystemExit(f"plan.sh に期待した関数が無い: {missing}")
    return ns


def wait_for(path: pathlib.Path, timeout: float) -> None:
    deadline = time.time() + timeout
    while not path.exists():
        if time.time() > deadline:
            raise SystemExit(f"合図 {path} が {timeout}s 以内に来なかった")
        time.sleep(POLL_SECONDS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan', required=True)
    ap.add_argument('--queue', required=True)
    ap.add_argument('--repo-root', required=True)
    ap.add_argument('--reached', required=True)
    ap.add_argument('--go', required=True)
    args = ap.parse_args()

    ns = load_plan_namespace(pathlib.Path(args.plan), args.queue, args.repo_root)

    graph_path = os.path.realpath(ns['task_graph_path']())
    reached = pathlib.Path(args.reached)
    go = pathlib.Path(args.go)
    original_write = ns['_atomic_write']

    def gated_write(path, text):
        # 生成物への publish だけを止める。plan.sh が他に書くものには触らない。
        if os.path.realpath(path) == graph_path:
            reached.write_text(str(os.getpid()))
            wait_for(go, GO_TIMEOUT_SECONDS)
        return original_write(path, text)

    ns['_atomic_write'] = gated_write
    ns['refresh_task_graph']()
    return 0


if __name__ == '__main__':
    sys.exit(main())
