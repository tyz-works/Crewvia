#!/usr/bin/env python3
"""scripts/lib_review_engine.py — kai-review.sh のレビュー先 (codex / agy / cursor / both) の設定の読み口。

`config/crewvia.yaml` の `review:` ブロック（`engine` / `agy_model` / `agy_timeout_seconds` /
`cursor_model` / `cursor_timeout_seconds` / `compare`）を、
`lib_task_cards` の入口で読む。env `CREWVIA_REVIEW_ENGINE` は engine だけを上書きする。

- **キー（`review:` / `engine` 等）が無い・config ファイルが無い = 既定**（engine=codex。今までと同じ挙動）。**キーがあって値が null は不正**。
- **PyYAML が無く config ファイルがあれば止める**（regex で「review: が無い」と決めない）。
- **config が読めない・値が不正 = 既定に倒さず止める**（exit 1・stderr に固定コード）。
  engine を黙って codex に戻すと、agy を選んだつもりのレビューが別物になる。
- `compare` は `both` のときに判定（Codex）の横に併記するエンジンの一覧（agy / cursor の部分集合・空と重複は不正）。既定 `[agy]` = 従来の `both` と同じ。
- cursor_model は `claude-fable-*`（Cursor の一覧で NO ZDR = データ保持の契約外）を拒否する。括弧付きパラメータ `[a=b,c=d]` は許す。
- CLI: `lib_review_engine.py get --config <path>` が 6 行（engine / agy_model / agy_timeout_seconds /
  cursor_model / cursor_timeout_seconds / compare をカンマ区切り）を stdout に出す。
  `extract-cursor` は cursor-agent の JSON 出力から findings を取り出して schema で検証する（fail-closed）。
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

ENGINES = ('codex', 'agy', 'cursor', 'both')
COMPARE_ENGINES = ('agy', 'cursor')
DEFAULT_COMPARE = ('agy',)
DEFAULT_ENGINE = 'codex'
DEFAULT_AGY_MODEL = 'gemini-4-argon-high'
DEFAULT_AGY_TIMEOUT_SECONDS = 600
DEFAULT_CURSOR_MODEL = 'grok-4.7-high'
DEFAULT_CURSOR_TIMEOUT_SECONDS = 900
ENV_ENGINE = 'CREWVIA_REVIEW_ENGINE'

_MODEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
# cursor は括弧付きパラメータ (`name[effort=high,fast=false]`) を取る。先頭は英数字 (option に読まれない)
_CURSOR_MODEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._\-\[\]=,]{0,127}')
_CURSOR_DENIED_PREFIXES = ('claude-fable',)   # Cursor の一覧で NO ZDR


class ReviewConfigError(Exception):
    """`code` は固定語彙。値は持たない。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _engine(value, code):
    if not isinstance(value, str) or value not in ENGINES:
        raise ReviewConfigError(code)
    return value


def _timeout(block, key, default, code):
    if key not in block:
        return default
    t = block[key]
    if isinstance(t, str) and t.isdigit():
        t = int(t)
    if isinstance(t, bool) or not isinstance(t, int) or t <= 0:
        raise ReviewConfigError(code)
    return t


def load_full(config_path, environ=None):
    """dict (engine / agy_model / agy_timeout / cursor_model / cursor_timeout / compare)。読み口はここ 1 か所。"""
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

    cursor_model = DEFAULT_CURSOR_MODEL
    if 'cursor_model' in block:
        cursor_model = block['cursor_model']
        if (not isinstance(cursor_model, str) or not _CURSOR_MODEL_RE.fullmatch(cursor_model)
                or cursor_model.lower().startswith(_CURSOR_DENIED_PREFIXES)):
            raise ReviewConfigError('bad_cursor_model')

    compare = list(DEFAULT_COMPARE)
    if 'compare' in block:
        c = block['compare']
        if (not isinstance(c, list) or not c or len(set(c)) != len(c)
                or any(not isinstance(x, str) or x not in COMPARE_ENGINES for x in c)):
            raise ReviewConfigError('bad_compare')
        compare = c

    return {
        'engine': engine,
        'agy_model': model,
        'agy_timeout': _timeout(block, 'agy_timeout_seconds', DEFAULT_AGY_TIMEOUT_SECONDS, 'bad_agy_timeout'),
        'cursor_model': cursor_model,
        'cursor_timeout': _timeout(block, 'cursor_timeout_seconds', DEFAULT_CURSOR_TIMEOUT_SECONDS,
                                   'bad_cursor_timeout'),
        'compare': compare,
    }


def load(config_path, environ=None):
    """`(engine, agy_model, agy_timeout_seconds)`。後方互換の薄い窓 (全部は `load_full`)。"""
    c = load_full(config_path, environ)
    return c['engine'], c['agy_model'], c['agy_timeout']


# --- cursor-agent の出力から findings を取り出す (fail-closed) ---
# stdout は JSON 1 個 {"type":"result","subtype":"success","is_error":false,"result":"<自由文>",...}。
# `--json-schema` が無いので result は前置きの文 + JSON。末尾の JSON (後ろが空白か ``` だけ) を findings として読み、schema で検証する。

def _schema_ok(value, schema):
    t = schema.get('type')
    types = t if isinstance(t, list) else ([t] if t else [])
    pytypes = {'object': dict, 'array': list, 'string': str, 'null': type(None)}
    if types and not any(isinstance(value, pytypes[x]) for x in types):
        return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    if isinstance(value, dict):
        props = schema.get('properties', {})
        if any(k not in value for k in schema.get('required', [])):
            return False
        if schema.get('additionalProperties') is False and any(k not in props for k in value):
            return False
        return all(_schema_ok(value[k], sub) for k, sub in props.items() if k in value)
    if isinstance(value, list) and 'items' in schema:
        return all(_schema_ok(v, schema['items']) for v in value)
    return True


class CursorOutputError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def extract_cursor_findings(out_text, schema):
    """cursor-agent の stdout 全文 → schema 準拠の findings dict。だめなら `CursorOutputError(固定コード)`。"""
    try:
        doc = json.loads(out_text)
    except ValueError:
        raise CursorOutputError('unparseable_output')
    if not isinstance(doc, dict):
        raise CursorOutputError('unparseable_output')
    if doc.get('is_error') is not False:
        raise CursorOutputError('is_error')
    if doc.get('type') != 'result' or doc.get('subtype') != 'success':
        raise CursorOutputError('subtype')
    result = doc.get('result')
    if not isinstance(result, str):
        raise CursorOutputError('no_findings_json')
    dec = json.JSONDecoder()
    marker = '{"findings"'
    pos = result.find(marker)
    schema_failed = False
    while pos != -1:
        try:
            obj, end = dec.raw_decode(result, pos)
        except ValueError:
            obj = None
        if obj is not None and result[end:].strip().strip('`').strip() == '':
            if _schema_ok(obj, schema):
                return obj
            schema_failed = True
        pos = result.find(marker, pos + 1)
    raise CursorOutputError('schema_invalid' if schema_failed else 'no_findings_json')


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


def diagnose(out_path, err_path, engine='agy'):
    """失敗した agy / cursor の診断 (AGY_ERROR 行・stdout JSON の status・denied_actions・stderr の末尾) を短い文字列にする。
    **伏せ字 (lib_checkpoint.redact) を通してから**切る (切ってから伏せると秘密を途中で割って残す)。
    伏せ字が失敗したら中身を出さない。"""
    try:
        import lib_checkpoint
        err = lib_checkpoint.redact(_tail_text(err_path))[0]
        raw_out = _tail_text(out_path)
        status, denied, cursor_info = None, [], []
        try:
            doc = json.loads(raw_out)
            if isinstance(doc, dict):
                if isinstance(doc.get('status'), str):
                    status = doc['status']
                d = doc.get('denied_actions')
                if isinstance(d, list):
                    denied = d
                # cursor: result (レビュー本文・コード片が入りうる) は出さない。型・subtype・is_error だけ
                for k in ('type', 'subtype'):
                    if isinstance(doc.get(k), str):
                        cursor_info.append(f'stdout {k}: ' + _cut(lib_checkpoint.redact(doc[k])[0], 80))
                if isinstance(doc.get('is_error'), bool):
                    cursor_info.append(f"stdout is_error: {doc['is_error']}")
        except ValueError:
            pass
        lines = []
        if engine == 'cursor':
            lines.extend(cursor_info)
            status, denied = None, []
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
    d.add_argument('--engine', choices=('agy', 'cursor'), default='agy')
    x = sub.add_parser('extract-cursor')
    x.add_argument('--out', required=True)
    x.add_argument('--schema', required=True)
    args = ap.parse_args(argv)
    if args.cmd == 'diagnose':
        print(diagnose(args.out, args.err, args.engine))
        return 0
    if args.cmd == 'extract-cursor':
        try:
            with open(args.schema, encoding='utf-8') as f:
                schema = json.load(f)
            with open(args.out, encoding='utf-8', errors='replace') as f:
                text = f.read()
            found = json.dumps(extract_cursor_findings(text, schema), ensure_ascii=False, separators=(',', ':'))
            # cursor は worktree の外 (秘密を含みうる) も読める。Result に入る前に伏せ字を通す。伏せ字が失敗したら出さない
            import lib_checkpoint
            try:
                found = lib_checkpoint.redact(found)[0]
            except Exception:
                print('redaction_failed', file=sys.stderr)
                return 1
            print(found)
        except CursorOutputError as e:
            print(e.code, file=sys.stderr)
            return 1
        except (OSError, ValueError):
            print('unparseable_output', file=sys.stderr)
            return 1
        return 0
    try:
        c = load_full(args.config)
    except ReviewConfigError as e:
        print(f'review_config_error={e.code}', file=sys.stderr)
        return 1
    for k in ('engine', 'agy_model', 'agy_timeout', 'cursor_model', 'cursor_timeout'):
        print(c[k])
    print(','.join(c['compare']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
