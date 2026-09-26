#!/usr/bin/env python3
"""tests/fixture_tree.py — plan.sh の隔離コピーを作る**唯一の**入口 (pytest 用)。

## なぜ 1 つにしたのか

plan.sh は `scripts/lib_*.py` を自分の位置から読む (依存規則・task カードの読み取り・
registry ...)。テストが `plan.sh` と「そのとき要ると思った lib」だけを一時ディレクトリ
へ写す作りだと、**lib を足すたびに、その一覧を持つ fixture のうち書き換え忘れたものが
CI でだけ赤くなる** (手元では主 checkout の scripts/ が見えるので気づけない)。この型は
3 回再発した (memory: shared-module-breaks-single-script-fixtures /
new-lib-import-breaks-single-copy-fixtures-again)。20 箇所以上に散った一覧を直すのを
やめ、**「scripts/ の実行に要るものは全部写す」を 1 箇所に置いた**。

写す対象は名前ではなく glob で決める (`lib_*` と、plan.sh が起動する補助スクリプト)。
新しい lib は何もしなくても次のコピーから付いてくる。

## 隔離の約束

`copy_plan_tree(root)` は `root/scripts/` にだけ書く。`root/queue` と `root/registry` は
呼び出し側が作る (それが「このテストの本番」になる)。fixture は
`CREWVIA_QUEUE=<root>/queue` を向けて plan.sh を走らせ、`registry` は `root/registry` が
使われる (`plan.sh` の `registry_dir()` は queue の隣)。

`tests/test_registry_isolation.py` が「plan.sh を `scripts/plan.sh` からコピーする
テストは、ここを通していなければ赤」を構造で固定している。
"""

from __future__ import annotations

import pathlib
import shutil

#: この checkout のルート (`tests/` の親)。
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: plan.sh が自分の位置から import する補助。`lib_*` は名前でなく glob で拾う。
#:
#: **写さないもの (意図)**: `git-helpers.sh` — plan.sh は「その有無」で pull 時に worktree を
#: 自動作成するかを決めるので、隔離コピーに置くとテストの外側 (本物の repo) に worktree を作る。
#: `review-plan.sh` — `plan.sh review` が claude を起動する。どちらも必要なテストが自分で置く。
_PLAN_SUPPORT = ("lint_plan.py",)


def plan_tree_files(src_root: pathlib.Path | None = None) -> list[pathlib.Path]:
    """`scripts/` のうち plan.sh の隔離コピーに写すファイル (plan.sh 自身を含む)。"""
    scripts = (src_root or REPO_ROOT) / "scripts"
    found = [scripts / "plan.sh"]
    found += sorted(scripts.glob("lib_*"))
    found += [scripts / name for name in _PLAN_SUPPORT if (scripts / name).is_file()]
    return [p for p in found if p.is_file()]


def copy_plan_tree(root: pathlib.Path, src_root: pathlib.Path | None = None) -> pathlib.Path:
    """`root/scripts/` に plan.sh と、それが読む scripts/ の補助を **まとめて** 写す。

    戻り値は `root/scripts/plan.sh`。`root/queue` / `root/registry` は作らない。
    """
    dest = pathlib.Path(root) / "scripts"
    dest.mkdir(parents=True, exist_ok=True)
    for src in plan_tree_files(src_root):
        shutil.copy2(src, dest / src.name)
    return dest / "plan.sh"
