#!/usr/bin/env python3
"""scripts/lib_review_engine.py — kai-review.sh のレビュー先 (codex / agy / both) の設定の読み口。

`config/crewvia.yaml` の `review:` ブロック（`engine` / `agy_model` / `agy_timeout_seconds`）を、
`lib_task_cards` の入口で読む。env `CREWVIA_REVIEW_ENGINE` は engine だけを上書きする。

- **キー（`review:` / `engine` 等）が無い・config ファイルが無い = 既定**（engine=codex。今までと同じ挙動）。**キーがあって値が null は不正**。
- **PyYAML が無く config ファイルがあれば止める**（regex で「review: が無い」と決めない）。
- **config が読めない・値が不正 = 既定に倒さず止める**（exit 1・stderr に固定コード）。
  engine を黙って codex に戻すと、agy を選んだつもりのレビューが別物になる。
- CLI: `lib_review_engine.py get --config <path>` が 3 行（engine / agy_model / agy_timeout_seconds）を stdout に出す。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import lib_task_cards as _cards  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover — PyYAML が無いとき review: があれば止める (下記)
    yaml = None

ENGINES = ('codex', 'agy', 'both')
DEFAULT_ENGINE = 'codex'
DEFAULT_AGY_MODEL = 'gemini-4-argon-high'
DEFAULT_AGY_TIMEOUT_SECONDS = 600
ENV_ENGINE = 'CREWVIA_REVIEW_ENGINE'

_MODEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


class ReviewConfigError(Exception):
    """`code` は固定語彙。値は持たない。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _engine(value, code):
    if not isinstance(value, str) or value not in ENGINES:
        raise ReviewConfigError(code)
    return value


def load(config_path, environ=None):
    """`(engine, agy_model, agy_timeout_seconds)`。"""
    environ = os.environ if environ is None else environ
    block = {}
    raw = _cards.read_regular_text_or_unreadable(str(config_path))
    if _cards.is_missing(raw):
        pass
    elif _cards.is_unreadable(raw):
        raise ReviewConfigError('config_unreadable')
    else:
        # 共有の parse_yaml は入れ子の途中のコメント行・空行で読むのをやめ、字下げは 2 空白しか認めない。
        # 正しい `review:` を「無い」と読んで codex に倒さないよう、このブロックは PyYAML で読む。
        # PyYAML が無いときは regex で「review: が無い」と決めない (`"review":` や flow 形式を見落とす)。
        # config ファイルがある限り読めないものとして止める。
        if yaml is None:
            raise ReviewConfigError('yaml_unavailable')
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            raise ReviewConfigError('config_malformed')
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ReviewConfigError('config_malformed')
        # キーが「無い」ときだけ既定。キーがあって値が null (`review:` / `review: null`) は拒否する。
        if 'review' in data:
            if not isinstance(data['review'], dict):
                raise ReviewConfigError('review_block_malformed')
            block = data['review']

    engine = DEFAULT_ENGINE
    if 'engine' in block:
        engine = _engine(block['engine'], 'bad_engine')
    if ENV_ENGINE in environ:
        engine = _engine(environ[ENV_ENGINE], 'bad_engine_env')   # 空も不正 (既定に倒さない)

    model = DEFAULT_AGY_MODEL
    if 'agy_model' in block:
        model = block['agy_model']
        if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
            raise ReviewConfigError('bad_agy_model')

    timeout = DEFAULT_AGY_TIMEOUT_SECONDS
    if 'agy_timeout_seconds' in block:
        t = block['agy_timeout_seconds']
        if isinstance(t, str) and t.isdigit():
            t = int(t)
        if isinstance(t, bool) or not isinstance(t, int) or t <= 0:
            raise ReviewConfigError('bad_agy_timeout')
        timeout = t
    return engine, model, timeout


# --- agy 失敗の診断 (Result に残す。後片付けで一時ファイルが消える前に) ---
_DIAG_READ_BYTES = 65536     # 各ファイルの末尾からこれだけ読む (redaction の対象の上限でもある)
_DIAG_LINE_MAX = 300
_DIAG_TAIL_LINES = 8
_DIAG_ERROR_LINES = 3
_DIAG_DENIED_MAX = 5
_DIAG_TOTAL_MAX = 2000
_WITHHELD = '(diagnostics withheld: redaction failed)'


def _tail_text(path):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - _DIAG_READ_BYTES)
            f.seek(start)
            text = f.read().decode('utf-8', 'replace')
        if start > 0:
            # 窓の先頭は途中から始まる (token を割っている) ので、最初の行は捨てる
            text = text.partition('\n')[2]
        return text
    except OSError:
        return ''


def _cut(text, n=_DIAG_LINE_MAX):
    text = text.replace('\r', ' ').strip()
    return text if len(text) <= n else text[:n] + '…'


def diagnose(out_path, err_path):
    """失敗した agy の診断 (AGY_ERROR 行・stdout JSON の status・denied_actions・stderr の末尾) を短い文字列にする。
    **伏せ字 (lib_checkpoint.redact) を通してから**切る (切ってから伏せると秘密を途中で割って残す)。
    伏せ字が失敗したら中身を出さない。"""
    try:
        import lib_checkpoint
        err = lib_checkpoint.redact(_tail_text(err_path))[0]
        raw_out = _tail_text(out_path)
        status, denied = None, []
        try:
            doc = json.loads(raw_out)
            if isinstance(doc, dict):
                if isinstance(doc.get('status'), str):
                    status = doc['status']
                d = doc.get('denied_actions')
                if isinstance(d, list):
                    denied = d
        except ValueError:
            pass
        lines = []
        err_lines = [ln for ln in err.splitlines() if ln.strip()]
        for ln in [ln for ln in err_lines if ln.lstrip().startswith('AGY_ERROR:')][:_DIAG_ERROR_LINES]:
            lines.append('AGY_ERROR line: ' + _cut(ln))
        if status is not None:
            lines.append('stdout status: ' + _cut(lib_checkpoint.redact(status)[0], 80))
        for item in denied[:_DIAG_DENIED_MAX]:
            if isinstance(item, dict):
                parts = [f'{k}={v}' for k, v in item.items() if isinstance(v, (str, int, float, bool))]
                text = ', '.join(parts)
            else:
                text = str(item)
            lines.append('denied: ' + _cut(lib_checkpoint.redact(text)[0]))
        if len(denied) > _DIAG_DENIED_MAX:
            lines.append(f'denied: ... (+{len(denied) - _DIAG_DENIED_MAX} more)')
        for ln in err_lines[-_DIAG_TAIL_LINES:]:
            lines.append('stderr tail: ' + _cut(ln))
        text = '\n'.join(lines)
        return text if len(text) <= _DIAG_TOTAL_MAX else text[:_DIAG_TOTAL_MAX] + '…'
    except Exception:  # noqa: BLE001 — 伏せ字が通らないものは出さない側に倒す
        return _WITHHELD


def main(argv=None):
    ap = argparse.ArgumentParser(prog='lib_review_engine.py')
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('get')
    g.add_argument('--config', required=True)
    d = sub.add_parser('diagnose')
    d.add_argument('--out', required=True)
    d.add_argument('--err', required=True)
    args = ap.parse_args(argv)
    if args.cmd == 'diagnose':
        print(diagnose(args.out, args.err))
        return 0
    try:
        engine, model, timeout = load(args.config)
    except ReviewConfigError as e:
        print(f'review_config_error={e.code}', file=sys.stderr)
        return 1
    print(engine)
    print(model)
    print(timeout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
