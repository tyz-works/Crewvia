#!/usr/bin/env python3
"""scripts/lib_config.py — 共通 config/crewvia.yaml にマシン固有の config/crewvia.local.yaml を重ねて読む唯一の入口。

設計: knowledge/machine-local-config.md (§2-E 読み先 / §3 重ね方 / §4 allowlist)。PR-1 では**読み口はまだ変えない**
(既存の読み口の移行は PR-2)。ここは重ねと検査だけを持つ。

- ファイルを開くのは `lib_task_cards.read_regular_text_or_unreadable` だけ (不変条件 1)。
- 共通 = 呼び出し側 (スクリプトの位置) の `config/crewvia.yaml`。local = **主 checkout** の `config/crewvia.local.yaml`
  (`git rev-parse --path-format=absolute --git-common-dir` の親。git 管理外ではスクリプトの位置だけ。
  `CREWVIA_REPO_ROOT` は使わない)。
- local が無い = 共通だけ。local が**ある**のに読めない / yaml でない / dict でない / 通常ファイルでない / 許可外キー /
  型違い = `Unreadable` (共通の値に黙って戻さない)。
- 重ね方: dict は深いマージ、list / scalar は置換、`null` は null で置換 (= 共通の値を消す)、空ファイルは上書きなし。
- エラーは**固定コード + 位置 (行・キー名) だけ**。元の行・値は出さない (local は op:// 参照などを含みうる)。
- `CREWVIA_MUX_TEST_ISOLATION` が立っている間は local を読まない。`local_path=` を明示したときだけ読む。

CLI:
    python3 lib_config.py get <dotted.key> [--default X]
    python3 lib_config.py dump --json
  壊れ = exit 1 + stderr に `lib_config: <固定コード>`。`get` でキーが無く --default も無い = exit 2。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import lib_task_cards as _cards  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover — PyYAML が無ければ local の有無に関わらず読めない扱い
    yaml = None

COMMON_RELPATH = Path('config') / 'crewvia.yaml'
LOCAL_RELPATH = Path('config') / 'crewvia.local.yaml'
ENV_TEST_ISOLATION = 'CREWVIA_MUX_TEST_ISOLATION'

#: local で上書きしてよいトップキー (§4)。typo は「知らないキー」として拒否される。
ALLOWED_KEYS = frozenset({
    'mode', 'rex', 'mux',
    'director_model', 'worker_model', 'model_per_skill',
    'taskvia', 'taskvia_url', 'approval_channel',
    'telegram', 'irc', 'escalation',
    'review',
    'wip_limit', 'daemons',
})
#: 共有規則 (不変条件 5): 機ごとにずれると dispatcher と plan.sh の答えが割れる。local では上書き不可。
EXCLUDED_KEYS = frozenset({'attempts', 'timeout_max_per_skill'})

_SAFE_KEY_RE = re.compile(r'[A-Za-z0-9_.-]{1,64}')


def _safe_key(key):
    s = str(key)
    return s if _SAFE_KEY_RE.fullmatch(s) else '?'


class ConfigError(Exception):
    """`code` は固定語彙、`where` は位置 (行・キー名) だけ。値は持たない。"""

    def __init__(self, code, where=''):
        super().__init__(code)
        self.code = code
        self.where = where

    def __str__(self):
        return f'{self.code} ({self.where})' if self.where else self.code


def _kind(value):
    if isinstance(value, dict):
        return 'dict'
    if isinstance(value, list):
        return 'list'
    return 'scalar'


def merge(base, over, path=''):
    """base に over を重ねた新しい dict。型違いは ConfigError('type_mismatch', キー経路)。"""
    out = dict(base)
    for key, ov in over.items():
        here = f'{path}.{_safe_key(key)}' if path else _safe_key(key)
        if key not in out or out[key] is None or ov is None:
            out[key] = ov
            continue
        bv = out[key]
        if _kind(bv) != _kind(ov):
            raise ConfigError('type_mismatch', here)
        out[key] = merge(bv, ov, here) if isinstance(bv, dict) else ov
    return out


def find_main_checkout(repo_root):
    """local の置き場所 (主 checkout)。git が使えない・想定外の形ならスクリプトの位置 (repo_root) だけ。"""
    root = Path(repo_root)
    try:
        r = subprocess.run(
            ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            cwd=str(root), capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return root
    if r.returncode != 0:
        return root
    common = Path(r.stdout.strip())
    if not common.is_absolute() or common.name != '.git':
        return root
    return common.parent


def _default_repo_root():
    return _SCRIPTS_DIR.parent


def _parse(path, kind):
    """path の yaml を dict で返す。無い → None。壊れ → ConfigError (固定コード)。"""
    raw = _cards.read_regular_text_or_unreadable(str(path))
    if _cards.is_missing(raw):
        return None
    if _cards.is_unreadable(raw):
        raise ConfigError(f'{kind}_unreadable')
    if yaml is None:
        raise ConfigError('yaml_unavailable')
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        mark = getattr(e, 'problem_mark', None)
        where = f'line {mark.line + 1}' if mark is not None else ''
        raise ConfigError(f'{kind}_malformed', where)
    except Exception:  # noqa: BLE001 — 想定外の構文でも原文を出さず固定コードに
        raise ConfigError(f'{kind}_malformed')
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f'{kind}_not_a_mapping')
    for key in data:
        if not isinstance(key, str):
            raise ConfigError(f'{kind}_bad_key')
    return data


def _check_allowlist(local):
    for key in local:
        if key not in ALLOWED_KEYS:
            code = 'local_key_excluded' if key in EXCLUDED_KEYS else 'local_key_not_allowed'
            raise ConfigError(code, _safe_key(key))


def load_effective(repo_root=None, *, common_path=None, local_path=None, environ=None):
    """共通 + local を重ねた dict。読めなければ `Unreadable` (例外は投げない)。

    repo_root: 共通の置き場所 (既定 = このスクリプトの親)。local はその主 checkout から探す。
    common_path / local_path: 明示したときだけ上書き。`local_path` を明示した場合は
    テスト隔離の印があっても local を読む。
    """
    environ = os.environ if environ is None else environ
    root = Path(repo_root) if repo_root is not None else _default_repo_root()
    cpath = Path(common_path) if common_path is not None else root / COMMON_RELPATH
    lpath = None
    try:
        common = _parse(cpath, 'common')
        if common is None:
            common = {}
        if local_path is not None:
            lpath = Path(local_path)
        elif environ.get(ENV_TEST_ISOLATION):
            return common
        else:
            lpath = find_main_checkout(root) / LOCAL_RELPATH
        local = _parse(lpath, 'local')
        if not local:           # 無い / 空 = 上書きなし
            return common
        _check_allowlist(local)
        return merge(common, local)
    except ConfigError as e:
        where = f' ({e.where})' if e.where else ''
        path = cpath if (e.code.startswith('common') or lpath is None) else lpath
        return _cards.Unreadable(path, e.code + where)


def _unreadable_code(u):
    return u.reason


def get_dotted(cfg, dotted):
    """(found, value)。途中が dict でなければ見つからない。null は「無い」として扱う。"""
    cur = cfg
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    if cur is None:
        return False, None
    return True, cur


def _render(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='lib_config.py')
    ap.add_argument('--root', help='共通 config の置き場所 (既定: このスクリプトの親)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('get')
    g.add_argument('key')
    g.add_argument('--default')
    d = sub.add_parser('dump')
    d.add_argument('--json', action='store_true', required=True)
    args = ap.parse_args(argv)

    cfg = load_effective(args.root)
    if _cards.is_unreadable(cfg):
        print(f'lib_config: {_unreadable_code(cfg)}', file=sys.stderr)
        return 1
    if args.cmd == 'dump':
        print(json.dumps(cfg, ensure_ascii=False, sort_keys=True))
        return 0
    found, value = get_dotted(cfg, args.key)
    if found:
        print(_render(value))
        return 0
    if args.default is not None:
        print(args.default)
        return 0
    print('lib_config: key_not_found', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
