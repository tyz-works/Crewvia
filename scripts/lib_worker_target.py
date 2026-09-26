#!/usr/bin/env python3
"""lib_worker_target.py — Worker が起動された TARGET_DIR の記録 (t009 / backlog #21)。

## なぜ要るのか

自動の `plan.sh pull` は task の `target_dir` で絞るが、dispatcher は **skill の交差だけ** で
割り当てていた。別 repo 用 (TARGET_DIR 付き) の Worker に crewvia 本体の task が回り、差し戻しても
同じ Worker に再割り当てされた (2026-09-25、memory `cross-repo-mission-worker-routing`)。
dispatcher が Worker の TARGET_DIR を知らないことが根なので、Worker の起動時にそれを残す。

## 置き場: `registry/workers/<Name>/target_dir.json`

**`registry/mux/<Name>-worker.json` (spawn 記録) には相乗りしない。** あれは kill の認可の
唯一の証拠で、`drop_pane_record(expect=)` の突合・掃除・`.records.lock` の区間が中身に依存して
いる。相乗りすると、kill の恒久拒否や、pane 消滅と同時に TARGET_DIR も消える経路ができる。

中身: `{"agent": <名前>, "target_dir": <絶対パス | null>, "written_at": <epoch 秒>}`。
`target_dir: null` は「crewvia 本体で起動した Worker」という**事実**で、「記録が無い」とは違う。

## 読み方 (倒す向きは判定ごとに違う)

`load_record()` は検証済みの dict か `Unreadable` を返す (入口は `lib_daemon_state.load_json_store`。
`is_missing()` が ENOENT = まだ書かれていない)。`worker_may_take()` が唯一の判定:

| task の target_dir | Worker の記録            | 結果                                        |
|--------------------|--------------------------|---------------------------------------------|
| null               | 無い / 読めない          | **割り当てる** (従来どおり)                  |
| null               | target_dir: null         | 割り当てる                                  |
| null               | target_dir: <パス>       | 割り当てない (別 repo の Worker)             |
| <パス>             | 一致                     | 割り当てる                                  |
| <パス>             | 不一致                   | 割り当てない                                |
| <パス>             | 無い / 読めない          | **割り当てない** (保留に倒す)                |

保留に倒すのは「target_dir 付きの task」のときだけ。PR3 merge 時点で起動済みの Worker は記録を
持たないので、null の task まで止めると dispatcher の restart の瞬間に全 task の割り当てが止まる。
記録は **start.sh の起動時にだけ** 書く (起動済みの Worker は次の再起動で記録を持つ。それまでは
target_dir: null の Worker として扱われる)。

## 片付け

記録は Worker が生きているあいだだけ意味を持つ。dispatcher は記録を **窓が生きている Worker の分しか
引かない** ので、退役した Worker の記録が残っても誤って使われることはない (同名の後任は start.sh が
上書きする)。掃除 (`sweep_stale_records()`) は、生きていない (窓も新しい heartbeat も無い) 、かつ一定時間 (`SWEEP_MIN_AGE_SECONDS`) より
古い記録だけを消す。後任が起動した直後の記録は新しいので消えない。

## 停止スイッチは無い

規則を dispatcher と plan.sh が共有するもの (`lib_dep_rules` 等) には env スイッチを付けない
(memory `no-env-killswitch-for-shared-rule`)。戻し方は knowledge/assignment-routing.md。

CLI (start.sh が使う):

    lib_worker_target.py record <registry_dir> <agent> [<target_dir>]   # 原子的に書く
    lib_worker_target.py show   <registry_dir> <agent>
"""

import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_daemon_state import load_json_store  # noqa: E402
from lib_task_cards import Unreadable, is_missing, is_unreadable  # noqa: E402

#: 窓の無い Worker の記録を掃除してよい最低の古さ (秒)。同名の後任の start.sh が書いた直後の
#: 記録を、「窓がまだ見えていない」だけで消さないための猶予。
SWEEP_MIN_AGE_SECONDS = 300


def normalize_target(path):
    """task の `target_dir` と Worker の TARGET_DIR を比べるための正規形。

    `plan.sh pull` が `os.path.abspath` で比べているのと同じ規則 (`None` は `None`)。
    """
    if path is None:
        return None
    path = str(path).strip()
    if not path:
        return None
    return os.path.abspath(os.path.expanduser(path))


def agent_name_problem(agent):
    """記録のディレクトリ名として使えない名前の理由 (使えれば `None`)。"""
    if (not isinstance(agent, str) or not agent or '/' in agent or '\0' in agent
            or agent in ('.', '..') or agent.startswith('.')):
        return "'/' や先頭の '.' を含まない、空でない名前が必要です"
    return None


def record_path(registry_dir, agent):
    problem = agent_name_problem(agent)
    if problem:
        raise ValueError(f'invalid agent name {agent!r}: {problem}')
    return Path(registry_dir) / 'workers' / agent / 'target_dir.json'


def record_problem(data):
    """記録の形が使えない理由 (使えれば `None`)。書き手が書くものと同じ形を要求する。"""
    agent = data.get('agent')
    if not isinstance(agent, str) or not agent:
        return f"'agent' is {agent!r}, expected a non-empty string"
    target = data.get('target_dir')
    if target is not None and (not isinstance(target, str) or not os.path.isabs(target)):
        return f"'target_dir' is {target!r}, expected an absolute path or null"
    written = data.get('written_at')
    if (not isinstance(written, (int, float)) or isinstance(written, bool)
            or not math.isfinite(written)):
        return f"'written_at' is {written!r}, expected a finite epoch number"
    return None


def build_record(agent, target_dir, now=None):
    return {
        'agent': agent,
        'target_dir': normalize_target(target_dir),
        'written_at': time.time() if now is None else now,
    }


def write_record(registry_dir, agent, target_dir, now=None):
    """記録を **原子的に** 書く (tmp に書いて `os.replace`)。書いた記録を返す。

    読み手が半端な JSON を見ないこと・同名の後任が先任の記録を丸ごと置き換えることが要件。
    """
    path = record_path(registry_dir, agent)
    record = build_record(agent, target_dir, now)
    problem = record_problem(record)
    if problem:
        raise ValueError(f'refusing to write an unusable record: {problem}')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.target_dir.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return record


def load_record(registry_dir, agent, warn=None):
    """検証済みの記録 dict か `Unreadable` (ENOENT は `is_missing()`)。例外は出さない。"""
    try:
        path = record_path(registry_dir, agent)
    except ValueError as e:
        return Unreadable(agent, str(e))
    return load_json_store(path, check=record_problem, warn=warn)


def worker_may_take(record, task_target_dir):
    """この記録を持つ Worker に、`task_target_dir` の task を割り当ててよいか。

    戻り値: `(可否, 理由)`。理由は人間向けの 1 行 (可のときは空)。**唯一の定義**で、
    冒頭の表がその全部。
    """
    task_target = normalize_target(task_target_dir)
    known = not is_unreadable(record)   # 無い (ENOENT) も読めないも、ここでは「知らない」
    if task_target is None:
        if not known:
            return True, ''
        if record['target_dir'] is None:
            return True, ''
        return False, (f"この Worker は TARGET_DIR={record['target_dir']} で起動されており、"
                       f"crewvia 本体の task (target_dir なし) は担当しない")
    if not known:
        why = 'TARGET_DIR の記録が無い' if is_missing(record) else 'TARGET_DIR の記録が読めない'
        return False, f'{why}ため、target_dir={task_target} の task は割り当てない (保留)'
    if record['target_dir'] is None:
        return False, (f'この Worker は crewvia 本体 (TARGET_DIR なし) で起動されており、'
                       f'target_dir={task_target} の task は担当しない')
    if normalize_target(record['target_dir']) != task_target:
        return False, (f"この Worker は TARGET_DIR={record['target_dir']} で起動されており、"
                       f"target_dir={task_target} の task は担当しない")
    return True, ''


def sweep_stale_records(registry_dir, live_agents, now=None, warn=None):
    """生きていない Worker の古い記録を消す。消した名前の list を返す。例外は出さない。

    `live_agents` は窓が有る、または heartbeat が新しい Worker (dispatcher の `_alive_workers`)。
    空のときは何もしない: mux が一時的に空を返しただけで全記録を消すと、生きている Worker まで
    記録を失う (そのまま target_dir 付き task が止まる)。空のあいだ残るだけなら害はない
    (dispatcher は窓のある Worker の記録しか引かない)。
    消す前にもう一度読み直し、読んだ時点と同じ記録であることを確かめる (後任が置き換えた
    ばかりの記録を消さない)。
    """
    removed = []
    if not live_agents:
        return removed
    now = time.time() if now is None else now
    base = Path(registry_dir) / 'workers'
    try:
        names = sorted(p.name for p in base.iterdir() if p.is_dir())
    except OSError:
        return removed
    live = set(live_agents)
    for name in names:
        if name in live or agent_name_problem(name):
            continue
        rec = load_record(registry_dir, name, warn=warn)
        if is_missing(rec):
            continue
        # 読めない・壊れた記録も、窓の無い Worker のものなら古さで片付ける (誰も引かない)。
        path = record_path(registry_dir, name)
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < SWEEP_MIN_AGE_SECONDS:
            continue
        again = load_record(registry_dir, name)
        if is_unreadable(rec) != is_unreadable(again) or (
                not is_unreadable(rec) and rec != again):
            continue   # 読んだ後に置き換えられた
        try:
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
            removed.append(name)
        except OSError:
            pass
    return removed


def _main(argv):
    if len(argv) >= 4 and argv[1] == 'record':
        registry_dir, agent = argv[2], argv[3]
        target = argv[4] if len(argv) > 4 and argv[4] else None
        try:
            rec = write_record(registry_dir, agent, target)
        except (ValueError, OSError) as e:
            print(f'[lib_worker_target] cannot record TARGET_DIR for {agent}: {e}', file=sys.stderr)
            return 1
        print(json.dumps(rec, ensure_ascii=False, sort_keys=True))
        return 0
    if len(argv) == 4 and argv[1] == 'show':
        rec = load_record(argv[2], argv[3], warn=lambda m: print(m, file=sys.stderr))
        if is_missing(rec):
            print('missing')
            return 2
        if is_unreadable(rec):
            print(f'unreadable: {rec.reason}')
            return 1
        print(json.dumps(rec, ensure_ascii=False, sort_keys=True))
        return 0
    print(__doc__.split('CLI (start.sh が使う):')[-1].strip(), file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(_main(sys.argv))
