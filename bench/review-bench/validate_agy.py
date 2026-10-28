#!/usr/bin/env python3
"""agy の出力 (--output-format json の 1 ドキュメント) を本番の collector (kai-review.sh) と同じ基準で検証する。

usage: validate_agy.py --raw <file> --schema <schema.json> --scripts <repo>/scripts
exit 0 = structured_output を stdout に 1 行で出す / 1 = 不合格 (固定コードを stderr に出す)
不合格: 複数・壊れた JSON / status != SUCCESS / denied_actions が空でない / structured_output が object でない / schema 不適合
"""
import argparse
import json
import sys


def validate(text, schema, schema_ok):
    try:
        dec, pos, docs = json.JSONDecoder(), 0, []
        while True:
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text):
                break
            obj, pos = dec.raw_decode(text, pos)
            docs.append(obj)
    except ValueError:
        return None, 'unparseable_output'
    if len(docs) != 1 or not isinstance(docs[0], dict):
        return None, 'unparseable_output'
    doc = docs[0]
    if doc.get('status') != 'SUCCESS':
        return None, 'status'
    denied = doc.get('denied_actions') or []
    if not isinstance(denied, list) or denied:
        return None, 'denied_actions'
    found = doc.get('structured_output')
    if not isinstance(found, dict):
        return None, 'no_structured_output'
    if not schema_ok(found, schema):
        return None, 'schema_invalid'
    return found, None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--raw', required=True)
    ap.add_argument('--schema', required=True)
    ap.add_argument('--scripts', required=True)
    a = ap.parse_args(argv)
    sys.path.insert(0, a.scripts)
    import lib_review_engine  # 本番と同じ schema 検査 (_schema_ok) を使う
    try:
        with open(a.schema, encoding='utf-8') as f:
            schema = json.load(f)
        with open(a.raw, encoding='utf-8', errors='replace') as f:
            text = f.read()
    except (OSError, ValueError):
        print('unparseable_output', file=sys.stderr)
        return 1
    found, err = validate(text, schema, lib_review_engine._schema_ok)
    if err:
        print(err, file=sys.stderr)
        return 1
    print(json.dumps(found, ensure_ascii=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
