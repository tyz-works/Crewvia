#!/usr/bin/env python3
"""lib_review_refusal.py — codex-review が「差分が大きすぎる」で拒否した事実の記録 (t010 / #11)。

## なぜこれが要るのか

`kai-review.sh` は差分が `MAX_DIFF_BYTES` (300KB) を超えると codex を呼ばず
`plan.sh needs-director` に倒す。これは正しい (fail-closed)。しかし拒否した事実が
どこにも残らないので、次のループが回っていた:

    dispatcher が kai-review.sh を spawn → 拒否 → needs_director
      → Director が pending に戻す → dispatcher がまた spawn → また拒否 → ...

同じ PR は何度やっても同じ大きさなので、**再 spawn は必ず同じ結論に戻る**。
拒否したという事実を 1 件の記録に残し、dispatcher はそれがある間は spawn しない。

## 置き場

`registry/daemons/review-refusals/<mission>__<task>.json` (`.gitignore` 対象)。
書き手は `kai-review.sh` だけ、読み手は dispatcher。

## 「無い」と「読めない」

記録が **無い** (ENOENT) は「拒否されていない」。記録が **読めない / 壊れている** は
「拒否されていないと証明できない」で、呼び出し側は spawn を保留する
(`Unreadable` を返す。空の入れ物としては振る舞わない — `lib_task_cards.Unreadable`)。
読めないことを「拒否されていない」に倒すと、壊れた記録 1 枚でループが戻る。

## Director が意図して再試行する経路

- PR を分割した / 差分を縮めた → 別の PR 番号を task に設定する
  (`plan.sh update <id> --pr-number <N>`)。記録の PR 番号と task の PR 番号が
  食い違えば「別の PR」なので拒否は効かない。
- 同じ PR のまま、意図して再試行する → `clear` (下記 CLI)。task を作り直しても
  よい (記録は task id に紐づく)。

CLI:
    python3 scripts/lib_review_refusal.py record --mission M --task T --pr N \
        --diff-bytes B --max-bytes X
    python3 scripts/lib_review_refusal.py show   --mission M --task T
    python3 scripts/lib_review_refusal.py clear  --mission M --task T
`--registry DIR` を省略すると `$CREWVIA_REPO_ROOT/registry`、それも無ければこの
ファイルの隣の repo の `registry/`。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_task_cards import (  # noqa: E402
    Unreadable, is_missing, is_unreadable, read_regular_text_or_unreadable,
)

#: 記録に必須の欄。欠けていたら「壊れている」であって「拒否されていない」ではない。
REQUIRED_FIELDS = ('mission', 'task', 'pr', 'diff_bytes', 'max_bytes')


def refusal_dir(registry_dir):
    return Path(registry_dir) / 'daemons' / 'review-refusals'


def refusal_path(registry_dir, mission, task):
    for what, value in (('mission', mission), ('task', task)):
        # ファイル名に使うので、区切り文字や空を通さない。
        if not value or '/' in str(value) or str(value) in ('.', '..'):
            raise ValueError(f'invalid {what} for a refusal record: {value!r}')
    return refusal_dir(registry_dir) / f'{mission}__{task}.json'


def record(registry_dir, mission, task, pr, diff_bytes, max_bytes):
    """拒否の事実を書く。失敗は `OSError` (呼び出し側が警告に落とす)。"""
    path = refusal_path(registry_dir, mission, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        'mission': str(mission),
        'task': str(task),
        'pr': str(pr),
        'diff_bytes': int(diff_bytes),
        'max_bytes': int(max_bytes),
        'refused_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(body, ensure_ascii=False) + '\n')
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def load(registry_dir, mission, task, warn=None):
    """記録を返す。

    - `dict`: 拒否の記録がある
    - `Unreadable` で `is_missing()`: 記録が無い (= 拒否されていない)
    - それ以外の `Unreadable`: 読めない / 壊れている (= 拒否されていないと証明できない)
    """
    path = refusal_path(registry_dir, mission, task)
    text = read_regular_text_or_unreadable(path, warn=warn)
    if is_unreadable(text):
        return text
    try:
        data = json.loads(text)
    except ValueError as e:
        return Unreadable(path, f'malformed JSON ({e})')
    if not isinstance(data, dict):
        return Unreadable(path, f'expected a JSON object, got {type(data).__name__}')
    missing = [k for k in REQUIRED_FIELDS if k not in data]
    if missing:
        return Unreadable(path, f'missing fields: {", ".join(missing)}')
    return data


def clear(registry_dir, mission, task):
    """記録を消す (Director が意図して再試行するとき)。消したら True。"""
    path = refusal_path(registry_dir, mission, task)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def refused_for_pr(rec, pr_number):
    """`load()` が返した `rec` が、この PR 番号に対する拒否か。

    task の `pr_number` が変わっていれば別の PR (= 分割した / 出し直した) なので
    拒否は効かない。`rec` が `dict` のときだけ呼ぶこと。
    """
    return str(rec['pr']).strip() == str(pr_number).strip()


def describe(rec):
    """人が読む 1 行。"""
    return (f"PR#{rec['pr']} の diff が {rec['diff_bytes']} bytes "
            f"(上限 {rec['max_bytes']} bytes, 超過 {int(rec['diff_bytes']) - int(rec['max_bytes'])} bytes)")


def _default_registry():
    root = os.environ.get('CREWVIA_REPO_ROOT')
    if root:
        return Path(root) / 'registry'
    return Path(__file__).resolve().parent.parent / 'registry'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('record', 'show', 'clear'):
        sp = sub.add_parser(name)
        sp.add_argument('--registry', default=None)
        sp.add_argument('--mission', required=True)
        sp.add_argument('--task', required=True)
        if name == 'record':
            sp.add_argument('--pr', required=True)
            sp.add_argument('--diff-bytes', required=True, type=int)
            sp.add_argument('--max-bytes', required=True, type=int)
    args = p.parse_args(argv)
    registry = Path(args.registry) if args.registry else _default_registry()

    if args.cmd == 'record':
        try:
            path = record(registry, args.mission, args.task, args.pr,
                          args.diff_bytes, args.max_bytes)
        except (OSError, ValueError) as e:
            print(f'lib_review_refusal: cannot record: {e}', file=sys.stderr)
            return 1
        print(path)
        return 0
    if args.cmd == 'show':
        rec = load(registry, args.mission, args.task,
                   warn=lambda m: print(m, file=sys.stderr))
        if is_missing(rec):
            print('no refusal recorded')
            return 0
        if is_unreadable(rec):
            print(f'UNREADABLE: {rec.reason}', file=sys.stderr)
            return 2
        print(json.dumps(rec, ensure_ascii=False))
        return 0
    if args.cmd == 'clear':
        try:
            removed = clear(registry, args.mission, args.task)
        except OSError as e:
            print(f'lib_review_refusal: cannot clear: {e}', file=sys.stderr)
            return 1
        print('cleared' if removed else 'nothing to clear')
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
