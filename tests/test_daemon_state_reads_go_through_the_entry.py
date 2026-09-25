#!/usr/bin/env python3
"""デーモン側 JSON 状態ストアの読み取りが、**1 つの入口** を通っていること (t026)。

## なぜ構造で見るのか

PR #214 で **同じ根の欠陥が 3 回** 出た (`scripts/lib_daemon_state.py` の docstring 参照):
読めた JSON を、**中身の形を確かめずに** 使う型である。

  1. t019 1 巡目  `lib_review_refusal.load()`  欄の存在しか見ない → `diff_bytes: null` で
                  `describe()` が TypeError → dispatch サイクル全体が落ちる
  2. t019 3 巡目  `load_told()`  外側しか見ない → 内側のエントリで `prune_told()` が
                  TypeError → 毎サイクル繰り返し・prune 不能・**永久沈黙**
  3. t015 (#217)  `released_deps` が未検証で `true` / `123` → TypeError

1 件ずつ振る舞いで固定しても、4 回目は「まだ書かれていないストア」に出る。だから
「どの関数が JSON を `json.loads` しているか」そのものをテストにする。向きは
`tests/test_queue_reads_go_through_the_guard.py` と同じ: **表 (allowlist) に無い
`json.load` / `json.loads` が 1 つでもあれば落ちる**。新しい読み取りを足したら必ず赤になる。
allowlist は「理由を書けるものだけ」— 載せるとは、形の検証が入口の外にあることを誰かが
引き受けたという意味である。

## 走査の範囲

`AUDITED_MODULES` (queue / registry を読む側の全モジュール) + `lib_*.py` 全部 +
`lib_review_refusal.py` / `lib_daemon_state.py`。**`.sh` は埋め込み python を全ブロック**
(`dispatcher.sh` の `<<'PYEOF'` を含む — QA t002 の F2 で、dispatcher.sh 内の直接読み取りが
検出できないことが実証されている)。`lib_*.py` を glob にしたのは、新しい lib が
黙って対象から漏れないようにするため。

## 検出器が見えるもの / 見えないもの

見える: `json.load` / `json.loads` / `json.JSONDecoder` (import の別名を解決した
`import json as j`、`from json import loads as parse` を含む)、呼ばずに取り置く形
(`parse = json.loads`)、`getattr(json, "loads")`、`__import__("json")` /
`importlib.import_module("json")`。

見えない (**実態より狭く書かない**): `exec()` / `eval()` / 完全に動的な属性名 /
`json` を変数に入れて渡す形 / `pickle` / `yaml.safe_load` など JSON 以外のパーサ /
走査対象外のモジュール (`hooks/` 等)。これは「うっかり足す」ことを止める補助であって、
敵対的なすり抜けを防ぐ境界ではない (`test_queue_reads_go_through_the_guard.py` と同じ立場)。

    python3 -m pytest tests/test_daemon_state_reads_go_through_the_entry.py -v
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# 走査の対象と別名解決の道具は、姉妹テストのものをそのまま使う (別々に持つと食い違う)。
from test_queue_reads_go_through_the_guard import (  # noqa: E402
    AUDITED_MODULES, _from_import_aliases, _module_aliases, _owner_by_line,
)

import lib_daemon_state  # noqa: E402
from lib_task_cards import Unreadable, is_missing, is_unreadable  # noqa: E402


# ---------------------------------------------------------------------------
# 対象のソースを取り出す
# ---------------------------------------------------------------------------

def _scanned_modules() -> list[str]:
    modules = set(AUDITED_MODULES) | {"lib_review_refusal.py", "lib_daemon_state.py"}
    modules |= {p.name for p in SCRIPTS_DIR.glob("lib_*.py")}
    return sorted(modules)


def _python_blocks(script: str) -> list[str]:
    """`.py` はそのまま、`.sh` は `<<'PYEOF'` ブロックを **全部**。"""
    text = (SCRIPTS_DIR / script).read_text()
    if script.endswith(".py"):
        return [text]
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    assert blocks, f"{script}: python ヒアドキュメント (PYEOF) が見つからない"
    return blocks


# ---------------------------------------------------------------------------
# 検出器
# ---------------------------------------------------------------------------

_JSON_PARSERS = frozenset({"load", "loads", "JSONDecoder"})


def _scan_json_parses(source: str, filename: str = "<synthetic>"):
    """`(関数名, ソース断片)` を、JSON を読む式の全部について返す。

    **合成ソースでも呼べる形にしてある** — 検出器そのものの回帰テストが、本番コードを
    触らずに「この形を見落としていないか」を確かめられるように。
    """
    tree = ast.parse(source, filename=filename)
    aliases = _module_aliases(tree)
    json_names = {name for name, mod in aliases.items() if mod == "json"}
    from_names = _from_import_aliases(tree, {"json"}, _JSON_PARSERS)
    owner = _owner_by_line(tree)
    call_of_func = {id(n.func): n for n in ast.walk(tree) if isinstance(n, ast.Call)}

    found = []

    def _record(node):
        # 呼び出しなら呼び出し全体を断片にする (同じ関数の中の別の呼び出しと区別するため)。
        shown = call_of_func.get(id(node), node)
        segment = ast.get_source_segment(source, shown) or "<unparsed>"
        found.append((owner.get(node.lineno, "<module>"), " ".join(segment.split())))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr in _JSON_PARSERS
                and isinstance(node.value, ast.Name) and node.value.id in json_names):
            _record(node)                       # json.loads(...) / parse = json.loads
        elif (isinstance(node, ast.Name) and node.id in from_names
                and isinstance(node.ctx, ast.Load)):
            _record(node)                       # from json import loads as parse; parse(...)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name) and node.args[0].id in json_names
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _JSON_PARSERS):
            _record(node)                       # getattr(json, "loads")
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            first = node.args[0]
            if (name in ("__import__", "import_module")
                    and isinstance(first, ast.Constant) and first.value == "json"):
                _record(node)                   # __import__("json") / importlib.import_module("json")
    return found


def _all_parses():
    out = []
    for script in _scanned_modules():
        for block in _python_blocks(script):
            for owner, fragment in _scan_json_parses(block, script):
                out.append((script, owner, fragment))
    return out


# ---------------------------------------------------------------------------
# allowlist: この JSON 読み取りは、なぜ入口 (load_json_store) を通さなくてよいか
# ---------------------------------------------------------------------------
#
# **理由を書けないものは載せない。** 分類は 2 つ:
#
#   (E) 外から来る応答 (subprocess の stdout / socket / HTTP)、または観測専用のログ。
#       ファイルの状態ストアではない — 壊れていれば呼び出し側が例外・空応答として扱う経路を
#       既に持ち、結果が割り当て・破壊・通知の判定に入らない
#   (Q) **queue 側** (`registry/daemons/` の外) の小さな sidecar。t026 では移していない。
#       plan.sh は単体コピーの隔離テストが多く、新しい lib への依存を足すと fixture が
#       まとめて壊れる (memory: shared-module-breaks-single-script-fixtures)。
#       「まだ移していない」ことを **明示して凍結** するのが目的で、ここに新しい行が
#       増えるなら、それは入口を通さない理由を新しく引き受けるということ。
#       移す先 (backlog) は knowledge/notify-once.md「入口を通していない読み取り (backlog)」に書いた。

_EXTERNAL_RESPONSE = "(E) 外から来る応答。ファイルの状態ストアではない: "
_QUEUE_SIDE = "(Q) queue 側 (registry/daemons の外)。t026 では移していない: "

ALLOWED_JSON_PARSES: dict[tuple[str, str, str], str] = {
    # -- (E) herdr の応答 -----------------------------------------------------
    ("lib_mux.py", "_herdr_ping", "json.loads(data.decode().strip())"):
        _EXTERNAL_RESPONSE + "herdr socket の ping 応答。壊れていれば「応答なし」と読む",
    ("lib_mux.py", "_herdr_run", "json.loads(r.stdout)"):
        _EXTERNAL_RESPONSE + "herdr CLI の stdout。`JSONDecodeError` を捕まえて次の経路へ",
    ("lib_mux.py", "_herdr_run", "json.loads(r.stderr)"):
        _EXTERNAL_RESPONSE + "同上 (エラー本文が stderr の JSON で来る版)",
    ("lib_mux.py", "_herdr_close_tab_bound", "json.loads(data.split(b\"\\n\")[0].decode())"):
        _EXTERNAL_RESPONSE + "herdr socket の応答。壊れていれば「閉じられなかった」と読む",
    # -- (E) Taskvia の HTTP 応答 --------------------------------------------
    ("plan.sh", "_taskvia_request", "json.loads(resp.read().decode())"):
        _EXTERNAL_RESPONSE + "Taskvia の HTTP 応答。同期は best-effort (Taskvia 非依存)",
    ("taskvia-sync.sh", "http_post", "json.loads(resp.read().decode())"):
        _EXTERNAL_RESPONSE + "Taskvia の HTTP 応答。同期は best-effort",
    ("taskvia-sync.sh", "http_get", "json.loads(resp.read().decode())"):
        _EXTERNAL_RESPONSE + "同上",
    ("taskvia-sync.sh", "http_patch", "json.loads(resp.read().decode())"):
        _EXTERNAL_RESPONSE + "同上",
    ("taskvia-sync.sh", "http_delete", "json.loads(resp.read().decode())"):
        _EXTERNAL_RESPONSE + "同上",
    # -- (E) 観測専用 ---------------------------------------------------------
    ("watchdog.py", "_newest_notification", "json.loads(payload_text)"):
        _EXTERNAL_RESPONSE + "Claude Code の notification payload。★観測専用 (ログにだけ出し、"
        "check() の判定に使わない)。`except Exception` で '(unparseable)' に倒す",
    # -- (Q) queue 側の sidecar ----------------------------------------------
    ("plan.sh", "_read_assignment_identity", "json.loads(text)"):
        _QUEUE_SIDE + "queue/assignments の実行アイデンティティ sidecar。外側 (dict か) は強制済みで、"
        "読めない・形が違うは None = 世代不明 (関数の docstring の契約)。"
        "内側の欄の型は呼び出し側任せ — backlog",
    ("plan.sh", "_load_taskvia_map", "json.loads(text)"):
        _QUEUE_SIDE + "queue/.taskvia-map.json (Taskvia id のキャッシュ)。外側は強制済みで、"
        "壊れていれば {} = もう一度送る (冪等)",
    ("taskvia-sync.sh", "load_map", "json.loads(text)"):
        _QUEUE_SIDE + "同じ .taskvia-map.json。**外側の型も未検証** (list だと呼び出し側の "
        ".get が落ちうる既知の穴 — plan.sh 側と揃える 1 行の修正で閉じる。backlog)",
}


# ---------------------------------------------------------------------------
# 1. 表に無い JSON 読み取りが 1 つも残っていない (negative)
# ---------------------------------------------------------------------------

def test_no_json_parse_bypasses_the_entry():
    """`json.load` / `json.loads` が、入口 (`load_json_store`) の外に増えていないこと。

    落ちたら: そのストアを `lib_daemon_state.load_json_store(path, check=...)` で読むこと。
    形の検証 (`check`) を書くのが要点 — 外側だけ確かめると、内側のエントリで
    `TypeError` になり、サイクルが毎回落ちる。
    """
    unexpected = [(s, o, f) for (s, o, f) in _all_parses()
                  if (s, o, f) not in ALLOWED_JSON_PARSES
                  and not (s == "lib_daemon_state.py" and o == "load_json_store")]
    assert not unexpected, (
        "入口を通さない JSON 読み取りが増えた:\n"
        + "\n".join(f"  {s}:{o}()  {f}" for s, o, f in unexpected)
        + "\n  → lib_daemon_state.load_json_store(path, check=<形の検証>) で読むこと。"
          "\n    どうしても通せないなら、理由付きで ALLOWED_JSON_PARSES に載せる "
          "(載せるとは、形の検証が入口の外にあることを引き受けるという意味)")


def test_the_allowlist_has_no_stale_entries():
    """コードが消えたのに allowlist に残っている行は、次の欠陥の隠れ場所になる。"""
    present = set(_all_parses())
    stale = [k for k in ALLOWED_JSON_PARSES if k not in present]
    assert not stale, ("allowlist に載っているが、もう存在しない読み取り "
                       f"(表から消すこと): {stale}")


def test_every_allowlist_entry_carries_a_reason():
    weak = [k for k, why in ALLOWED_JSON_PARSES.items()
            if not isinstance(why, str) or len(why.strip()) < 12]
    assert not weak, f"理由が書かれていない (または短すぎる) allowlist 行: {weak}"


def test_the_entry_itself_is_the_only_parser_inside_lib_daemon_state():
    """入口の中の `json.loads` は 1 つだけ。増えたら「入口が 2 つ」になっている。"""
    here = [(o, f) for (s, o, f) in _all_parses() if s == "lib_daemon_state.py"]
    assert here == [("load_json_store", "json.loads(text)")], here


# ---------------------------------------------------------------------------
# 2. 表 (positive): このストアの読み手は、この入口を通っている
# ---------------------------------------------------------------------------

def _function(script: str, name: str) -> ast.FunctionDef:
    for block in _python_blocks(script):
        for node in ast.walk(ast.parse(block, filename=script)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
    pytest.fail(f"{script}: 関数 {name}() が見つからない — 改名したなら、この表も直すこと")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                names.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                names.add(sub.func.attr)
    return names


#: (スクリプト, 関数, 通っていなければならない入口)
ENTRY_READERS = [
    ("dispatcher.sh", "load_told", {"load_json_store"}),
    ("lib_review_refusal.py", "load", {"load_json_store"}),
    ("lib_daemon_state.py", "load_json_store", {"read_regular_text_or_unreadable"}),
]


@pytest.mark.parametrize("script,function,entries", ENTRY_READERS,
                         ids=[f"{s}:{f}" for s, f, _ in ENTRY_READERS])
def test_the_store_reader_goes_through_the_entry(script, function, entries):
    called = _called_names(_function(script, function))
    assert called & entries, (
        f"{script}:{function}() が {sorted(entries)} のどれも通っていない。実際: {sorted(called)}")


#: (スクリプト, 関数, 渡していなければならない形の検証)。`check=` を渡さない読み方は
#: 「外側 (object か) しか検証しない」ことになり、内側の欄を信じて `TypeError` になる
#: (Kai 3 巡目 P2 の根)。検証器の中身は tests/test_daemon_state_fail_direction.py が見る。
VALIDATED_STORES = [
    ("dispatcher.sh", "load_told", "told_ledger_problem"),
    ("dispatcher.sh", "load_notify_cache", "notify_cache_problem"),
    ("dispatcher.sh", "_load_state_entry", "rule5_state_problem"),
    ("verifier-dispatcher.sh", "load_notify_cache", "notify_cache_problem"),
    ("lib_daemon_watch.py", "_read_state", "watch_state_problem"),
]


@pytest.mark.parametrize("script,function,validator", VALIDATED_STORES,
                         ids=[f"{s}:{f}" for s, f, _ in VALIDATED_STORES])
def test_the_store_reader_passes_a_shape_validator(script, function, validator):
    fn = _function(script, function)
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "load_json_store"]
    assert calls, f"{script}:{function}() が load_json_store() を呼んでいない"
    assert any(kw.arg == "check" and isinstance(kw.value, ast.Name)
               and kw.value.id == validator
               for c in calls for kw in c.keywords), \
        f"{script}:{function}() が {validator} を check= に渡していない (外側しか検証しない読み方)"


def test_pull_and_resolve_mission_share_one_search_order():
    """`plan.sh pull` と `plan.sh resolve-mission` は、`--mission` 省略時の探索順を
    `mission_search_order()` から得ていること (t026 / Kai 3 巡目 P2 の 2 件目)。

    kai-review.sh が拒否記録を書く先と、pull が実際に task を取る先が食い違うと、記録は別
    mission の名前で書かれて dispatcher に見つけられず、#11 の再 spawn ループが戻る。
    別々に解決しない。"""
    for function in ("cmd_pull", "cmd_resolve_mission"):
        assert "mission_search_order" in _called_names(_function("plan.sh", function)), \
            f"plan.sh:{function}() が mission_search_order() を通っていない"


# ---------------------------------------------------------------------------
# 3. 検出器の回帰: **実際の呼び出し形** で見落としていないこと
# ---------------------------------------------------------------------------
#
# 「増えたら赤」の正規表現が実形で緑のまま、という事故を避けるため
# (memory: structural-guard-must-match-real-call-shape)、合成だけでなく
# **本物の dispatcher.sh の `load_told` を欠陥の形に戻したもの** を走査する。

_OLD_LOAD_TOLD = '''\
def load_told():
    text = read_regular_text_or_unreadable(TOLD_FILE)
    if is_missing(text):
        return {}
    if is_unreadable(text):
        return text
    try:
        data = json.loads(text)
    except ValueError as e:
        return Unreadable(TOLD_FILE, f'malformed JSON ({e})')
    if not isinstance(data, dict):
        return Unreadable(TOLD_FILE, 'not a JSON object')
    return data
'''


def _dispatcher_with_the_old_ledger_reader() -> str:
    """本物の dispatcher.sh の全文のうち、`load_told()` だけを外側検証の形に戻したもの。"""
    text = (SCRIPTS_DIR / "dispatcher.sh").read_text()
    new, n = re.subn(r"def load_told\(\):\n.*?\n(?=def save_told)", _OLD_LOAD_TOLD + "\n\n",
                     text, count=1, flags=re.S)
    assert n == 1, "load_told() を差し替えられなかった (このテストの前提が崩れている)"
    return new


def test_the_detector_sees_the_reverted_ledger_reader_in_the_real_dispatcher():
    """欠陥の形 (外側だけ検証する `json.loads`) を **本物の dispatcher.sh の中に** 戻すと、
    検出器がそれを拾う。dispatcher.sh の埋め込み python が走査対象に入っていることの実証。"""
    text = _dispatcher_with_the_old_ledger_reader()
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    hits = [h for b in blocks for h in _scan_json_parses(b, "dispatcher.sh")]
    assert ("load_told", "json.loads(text)") in hits, hits


@pytest.mark.parametrize("source,expected", [
    ("import json\ndef f(t):\n    return json.loads(t)\n", "json.loads(t)"),
    ("import json\ndef f(p):\n    return json.load(open(p))\n", "json.load(open(p))"),
    ("import json as j\ndef f(t):\n    return j.loads(t)\n", "j.loads(t)"),
    ("from json import loads as parse\ndef f(t):\n    return parse(t)\n", "parse"),
    ("import json\nparse = json.loads\n", "json.loads"),
    ("import json\ndef f(t):\n    return json.JSONDecoder().decode(t)\n", "json.JSONDecoder"),
    ("import json\ndef f(t):\n    return getattr(json, 'loads')(t)\n", "getattr(json, 'loads')"),
    ("def f(t):\n    return __import__('json').loads(t)\n", "__import__('json')"),
    ("import importlib\ndef f(t):\n    return importlib.import_module('json').loads(t)\n",
     "importlib.import_module('json')"),
], ids=["loads", "load-open", "module-alias", "from-alias", "unreferenced", "decoder",
        "getattr", "dunder-import", "import_module"])
def test_the_detector_sees_every_way_to_parse_json(source, expected):
    fragments = [f for _, f in _scan_json_parses(source)]
    assert any(expected in f for f in fragments), (expected, fragments)


@pytest.mark.parametrize("source", [
    "import json\ndef f(x):\n    return json.dumps(x)\n",
    "import json\ndef f(x):\n    try:\n        pass\n    except json.JSONDecodeError:\n        pass\n",
    "import orjson\ndef f(t):\n    return orjson.loads(t)\n",
    "def f(loads, t):\n    return loads(t)\n",
], ids=["dumps", "decode-error-type", "other-module", "local-name"])
def test_the_detector_ignores_what_is_not_a_json_parse(source):
    assert _scan_json_parses(source) == []


# ---------------------------------------------------------------------------
# 4. 入口の振る舞い
# ---------------------------------------------------------------------------

def test_missing_file_is_missing_not_empty(tmp_path):
    got = lib_daemon_state.load_json_store(tmp_path / "nope.json")
    assert is_unreadable(got) and is_missing(got)


@pytest.mark.parametrize("body", ["{ not json", "", "﻿{", "[" * 20000],
                         ids=["truncated", "empty", "bom", "recursion"])
def test_unparseable_is_unreadable_not_missing_and_never_raises(tmp_path, body):
    p = tmp_path / "s.json"
    p.write_text(body)
    got = lib_daemon_state.load_json_store(p)
    assert is_unreadable(got) and not is_missing(got)


@pytest.mark.parametrize("body", ["[]", "null", "123", '"str"'],
                         ids=["list", "null", "number", "string"])
def test_wrong_outer_type_is_unreadable(tmp_path, body):
    p = tmp_path / "s.json"
    p.write_text(body)
    assert is_unreadable(lib_daemon_state.load_json_store(p))


def test_a_failing_validator_means_unreadable_not_a_crash(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{}")

    def boom(_):
        raise RuntimeError("validator bug")

    got = lib_daemon_state.load_json_store(p, check=boom)
    assert is_unreadable(got) and "validator failed" in got.reason


def test_a_broken_warn_callback_does_not_change_the_answer(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[]")

    def bad_warn(_):
        raise OSError("stderr is closed")

    assert is_unreadable(lib_daemon_state.load_json_store(p, warn=bad_warn))


def test_a_fifo_in_the_place_of_the_store_does_not_hang(tmp_path):
    p = tmp_path / "s.json"
    os.mkfifo(p)
    got = lib_daemon_state.load_json_store(p)      # 書き手のいない FIFO。待たずに返る
    assert is_unreadable(got) and not is_missing(got)


def test_a_valid_store_comes_back_as_is(tmp_path):
    p = tmp_path / "s.json"
    body = {"k": {"fp": "a", "kind": "needs_director", "slug": "m", "task": "t001"}}
    p.write_text(json.dumps(body))
    assert lib_daemon_state.load_json_store(
        p, check=lib_daemon_state.told_ledger_problem) == body


# -- 台帳の形 (壊れたエントリが 1 つでもあれば全体が使えない) ---------------------

_GOOD = {"fp": "abc", "kind": "needs_director", "slug": "m", "task": "t001"}

BAD_LEDGERS = {
    "outer-list": [],
    "entry-list": {"k": []},
    "entry-null": {"k": None},
    "slug-list": {"k": {**_GOOD, "slug": []}},                    # Kai 3 巡目の実例
    "fingerprint-dict": {"k": {**_GOOD, "fp": {"a": 1}}},
    "fingerprint-number": {"k": {**_GOOD, "fp": 123}},
    "task-number": {"k": {**_GOOD, "task": 1}},
    "kind-bool": {"k": {**_GOOD, "kind": True}},
    "field-missing": {"k": {"fp": "abc", "kind": "x", "slug": "m"}},
    "field-empty": {"k": {**_GOOD, "slug": ""}},
    "one-bad-among-good": {"good": _GOOD, "bad": {"slug": []}},   # 良いエントリがあっても全体が不可
}


@pytest.mark.parametrize("name", sorted(BAD_LEDGERS))
def test_a_malformed_ledger_is_unreadable_as_a_whole(tmp_path, name):
    p = tmp_path / "notified-state.json"
    p.write_text(json.dumps(BAD_LEDGERS[name]))
    got = lib_daemon_state.load_json_store(
        p, check=lib_daemon_state.told_ledger_problem)
    assert isinstance(got, Unreadable) and not is_missing(got), name


def test_the_empty_ledger_is_valid():
    assert lib_daemon_state.told_ledger_problem({}) is None


def test_what_the_writer_writes_the_reader_accepts():
    """書き手が書く形 (`record_told` のエントリ) を、読み手が必ず受け付けること。

    読み手だけが厳しいと、書いたばかりの台帳を自分が「壊れている」と読み、永久に再送側へ
    倒れる (a new guard creates a new state)。"""
    entry = {"fp": "0123456789abcdef", "kind": "review-refused", "slug": "20260925-x",
             "task": "t010"}
    assert lib_daemon_state.told_entry_problem("needs_director_20260925-x_t010", entry) is None
