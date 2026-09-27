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
        [--gate publish|release|none] [--reached <file> --go <file>] \
        [--lock-wait <秒>]

`--reached` は「その地点まで来た」印。`--go` が現れるまでそこで待ち、現れたら
本番どおり続けて終了する。止める地点は `--gate` で選ぶ:

  publish …… queue を読み終え、生成物を書く直前 (既定)。
  release …… publish し終え、**読み直しの要求が無いことを確認したあと、本
              ロックを解放する直前**。受け渡しの取りこぼしはこの一瞬でしか
              起きないので、ここを開けられないと再現できない。
  none   …… どこでも止めない。`--lock-wait` を短くして「ロックを待ち切れずに
              引き返す側 (要求者)」を演じるときに使う。

`--lock-wait` は本ロックを待つ上限 (TASK_GRAPH_LOCK_WAIT_SECONDS) の差し替え。
待ち時間そのものはテストしたい仕組みではなく、テストを 10 秒待たせないための
定数なので、ここだけ短くする。判定のコードは本番のまま走る。
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
    missing = [n for n in ('refresh_task_graph', '_atomic_write', 'task_graph_path',
                           'release_task_graph_lock')
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
    ap.add_argument('--gate', choices=('publish', 'release', 'none'), default='publish')
    ap.add_argument('--reached')
    ap.add_argument('--go')
    ap.add_argument('--lock-wait', type=float, default=None)
    args = ap.parse_args()

    if args.gate != 'none' and not (args.reached and args.go):
        raise SystemExit('--gate publish/release には --reached と --go が要る')

    ns = load_plan_namespace(pathlib.Path(args.plan), args.queue, args.repo_root)
    if args.lock_wait is not None:
        ns['TASK_GRAPH_LOCK_WAIT_SECONDS'] = args.lock_wait

    graph_path = os.path.realpath(ns['task_graph_path']())

    def hold():
        """テストの合図が来るまでその場で止まる。"""
        pathlib.Path(args.reached).write_text(str(os.getpid()))
        wait_for(pathlib.Path(args.go), GO_TIMEOUT_SECONDS)

    if args.gate == 'publish':
        original_write = ns['_atomic_write']

        def gated_write(path, text):
            # 生成物への publish だけを止める。plan.sh が他に書くものには触らない。
            if os.path.realpath(path) == graph_path:
                hold()
            return original_write(path, text)

        ns['_atomic_write'] = gated_write
    elif args.gate == 'release':
        original_release = ns['release_task_graph_lock']
        held = []

        def gated_release(lf):
            # 最初の解放だけ止める (2 回目以降は後片付けの経路でありうる)。
            if not held:
                held.append(True)
                hold()
            return original_release(lf)

        ns['release_task_graph_lock'] = gated_release

    ns['refresh_task_graph']()
    return 0


if __name__ == '__main__':
    sys.exit(main())
