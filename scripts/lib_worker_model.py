#!/usr/bin/env python3
"""lib_worker_model.py — Worker が起動されたときのモデルの記録 (モデルの段上げ PR-1。設計 knowledge/model-escalation.md §4.2)。

## なぜ要るのか

試行の記録 (card・record・台帳) には「どのモデルで走ったか」が無い。解決したモデルは `start.sh` の `LAUNCH_CMD` の
`--model` に入るだけで、どこにも残らなかった。段上げ (後続の PR) は「前の試行のモデル」を起点にするので、まず記録する。
この PR は**書いて見せるだけ**で、割り当ての判定は変えない (`worker_may_take` は PR-2)。

## 置き場: `registry/workers/<Name>/model.json`

`lib_worker_target.py` (`target_dir.json`) と同じ dir・同じ作法。**`registry/mux/*.json` (kill の認可の証拠) には相乗りしない**。

中身: `{"agent": <名前>, "model": <文字列 | null>, "source": <env|override|skill|none>, "bound_task": <"<slug>/<tid>" | null>,
"written_at": <epoch 秒>}`。

- `model`: 起動で解決したモデル (`SELECTED_MODEL`)。空 (= `--model` なし = CLI の既定) は `null`。
- `source`: `env` (人が `CREWVIA_WORKER_MODEL` を明示) / `override` (段上げ。PR-2 以降) / `skill` (`model_per_skill` か
  `worker_model`) / `none` (解決が空)。
- `bound_task`: 束ねた Worker のとき `"<slug>/<tid>"`。PR-1 では常に `null`。

## 読み方

`load_record()` は検証済みの dict か `Unreadable` (`is_missing()` = まだ書かれていない = PR-1 より前に起動した Worker)。
「無い」と「壊れている」は別: 取り違えない (memory `load-json-store-missing-is-also-unreadable`。`is_missing` を先に見る)。
pull は記録が無い・読めない Worker では `execution_model` を**書かない** (`unknown` のような値を作らない)。

## 停止スイッチは無い (共有規則ではないが、書くだけの lib に env の分岐を足さない)

CLI (start.sh が使う):

    lib_worker_model.py record <registry_dir> <agent> <source> [<model> [<bound_task>]]   # 原子的に書く
    lib_worker_model.py show   <registry_dir> <agent>
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
import lib_worker_target as _target  # noqa: E402

#: `source` の語彙。
SOURCES = ('env', 'override', 'skill', 'none')

RECORD_FILE = 'model.json'


def record_path(registry_dir, agent):
    problem = _target.agent_name_problem(agent)
    if problem:
        raise ValueError(f'invalid agent name {agent!r}: {problem}')
    return Path(registry_dir) / 'workers' / agent / RECORD_FILE


def _is_text(value):
    """空でも空白だけでもない文字列。Controller の `start_execution` (`model.strip()`) と同じ基準にそろえる
    (ここで通した値を Controller が INVALID_ARGUMENT で断って pull が止まる、という食い違いを作らない)。"""
    return isinstance(value, str) and value.strip() != ''


def record_problem(data):
    """記録の形が使えない理由 (使えれば `None`)。**書き手が書くものと同じ形を要求する**。

    読み手 (`load_record` の呼び手) は全部の欄を `[]` で読むので、欄が 1 つでも欠ければ読めない扱いにする。
    型を先に確かめる (`source: []` のような値が集合との照合で TypeError にならないように)。
    """
    if not isinstance(data, dict):
        return 'record is not a JSON object'
    agent = data.get('agent')
    if not _is_text(agent):
        return f"'agent' is {agent!r}, expected a non-empty string"
    for key in ('model', 'source', 'bound_task', 'written_at'):
        if key not in data:
            return f"'{key}' is missing"
    model = data['model']
    if model is not None and not _is_text(model):
        return f"'model' is {model!r}, expected a non-empty string or null"
    source = data['source']
    if not _is_text(source) or source not in SOURCES:
        return f"'source' is {source!r}, expected one of {list(SOURCES)}"
    bound = data['bound_task']
    if bound is not None and (not _is_text(bound) or '/' not in bound):
        return f"'bound_task' is {bound!r}, expected '<slug>/<tid>' or null"
    written = data['written_at']
    if (not isinstance(written, (int, float)) or isinstance(written, bool)
            or not math.isfinite(written)):
        return f"'written_at' is {written!r}, expected a finite epoch number"
    return None


def build_record(agent, source, model=None, bound_task=None, now=None):
    return {
        'agent': agent,
        'model': model if model and model.strip() else None,
        'source': source,
        'bound_task': bound_task if bound_task else None,
        'written_at': time.time() if now is None else now,
    }


def write_record(registry_dir, agent, source, model=None, bound_task=None, now=None):
    """記録を **原子的に** 書く (tmp に書いて `os.replace`)。書いた記録を返す。

    読み手が半端な JSON を見ないこと・同名の後任が先任の記録を丸ごと置き換えることが要件。
    """
    path = record_path(registry_dir, agent)
    record = build_record(agent, source, model, bound_task, now)
    problem = record_problem(record)
    if problem:
        raise ValueError(f'refusing to write an unusable record: {problem}')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.model.', suffix='.tmp')
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


def recorded_model(registry_dir, agent):
    """pull が card の `execution_model` に写す値。`(model, warning)`。

    model は**書けるときだけ**文字列。None = 記録が無い (PR-1 より前に起動した Worker)・読めない・モデルが空 (CLI の既定)。
    warning は「読めない (missing でない)」ときだけの 1 行 (呼び出し側が stderr に出す)。それ以外は None。
    「無い」と「壊れている」の判定をここで済ませる (呼び出し側が別のモジュールの `Unreadable` で isinstance を
    取り違えない: plan.sh は lib_task_cards を別に読み込んでいるので、呼び出し側の `is_missing` は別のクラスを見る)。
    """
    rec = load_record(registry_dir, agent)
    if is_missing(rec):
        return None, None
    if is_unreadable(rec):
        return None, f"{agent} のモデルの記録を読めないので execution_model を書きません ({rec.reason})"
    return rec['model'], None


def _main(argv):
    if len(argv) >= 5 and argv[1] == 'record':
        registry_dir, agent, source = argv[2], argv[3], argv[4]
        model = argv[5] if len(argv) > 5 and argv[5] else None
        bound = argv[6] if len(argv) > 6 and argv[6] else None
        try:
            rec = write_record(registry_dir, agent, source, model, bound)
        except (ValueError, OSError) as e:
            print(f'[lib_worker_model] cannot record the model for {agent}: {e}', file=sys.stderr)
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
