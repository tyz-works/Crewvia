#!/usr/bin/env python3
"""横断ガード: 観測 (stat / /proc / exists / iterdir) の失敗を「許可 / 不在」に
潰している箇所が増えたら CI を落とす (t053)。

## なぜこの task があるか

mission B の Codex review 1 巡で、**同じ欠陥族が 3 件**出た。

* PR #240: `stat` が読めないとき pid を kill 許可に入れる (fail-open)
* PR #240: unobservable だけのスキャンが「クリーン」を返す
* PR #237: 画面が読めないとき「trust ダイアログは無い」に倒して Enter を送る

いずれも **「観測に失敗した」を「許可してよい」または「対象は存在しない」に
潰している**。3 件とも既に直っている (`tests/leaked_descendants.py` /
`tests/kill_budget.py` / `scripts/start.sh:_abort_on_unobservable_dialog`)。
この task は個別修正ではなく、**同じ型が増えたら CI が落ちる構造**に切り替える
(memory: structural-guard-beats-site-patches)。

## スコープ (実装から選定。usage 行やドキュメントの一覧からではない)

「観測の失敗が、破壊的な結論 (kill・退役・respawn 拒否・隔離・復元の上書き) に
使われうる」モジュールだけを対象にする (`AUTHORITY_MODULES`):

* `lib_pane_process.py` — watchdog の idle/job 判定。誤判定は Worker の
  hard_idle terminate に直結する。
* `lib_retirement.py` — Worker の退役・kill を実行するモジュールそのもの。
* `lib_daemon_watch.py` — dispatcher/watchdog の相互監視・respawn 権限。
* `lib_mux.py` — pane の kill・spawn 記録 (`registry/mux/*.json`) の抹消。
* `worktree_gc.py` — worktree の隔離・復元 (上書き事故になりうる)。
* `watchdog.py` — Worker の hard_idle/max による terminate。
* `tests/leaked_descendants.py` / `tests/kill_budget.py` — pytest 自身の
  kill 権限 (2026-09-27 の自爆の当事者。同じ観測失敗の型を持ちうる)。

queue/registry のファイル読み取り (`lib_task_cards` 経由) は既に
`tests/test_queue_reads_go_through_the_guard.py` が別に見ている。ここが見るのは
**stat / /proc / exists / iterdir / listdir / readlink / access** ——
そちらがカバーしない観測手段。

## 検査の仕方

対象モジュールの `.stat()` / `.exists()` / `.read_bytes()` / `.read_text()` /
`.readlink()` / `os.access()` / `.iterdir()` / `os.listdir()` 呼び出しを AST で
拾う (`_risky_calls()`、`test_queue_reads_go_through_the_guard.py` と同じ
「実際の呼び出し形」で見る流儀)。属性アクセス経由の直接呼び出し・import した
関数の裸呼び出し (別名を含む)・同じ関数内で変数や `getattr` に一旦束縛して
から呼ぶ形は拾う。**ただし「全部」ではない** — `AUTHORITY_MODULES` 8 ファイル
の AST 上に呼び出しそのものが現れない形 (呼び出しを監査対象外のヘルパーへ
委譲する等) は原理的に見えない。見える形・見えない形の一覧は下の表 (QA t054
(2026-09-28) で実測)。1 つでも `OBSERVATION_SITES` に無ければ
`test_every_risky_call_is_classified` が落ちる —— 新しい呼び出しは、安全だろうと
危険だろうと、まず理由を書かせる (allowlist の思想は
memory: approve-judgment-needs-allowlist-and-scope と同じ)。

## SAFE と KNOWN_FAIL_OPEN

`OBSERVATION_SITES` の値は `(status, reason)`。

* `SAFE` — 観測の失敗を破壊的な結論に流し込んでいないことを確認済み。
  ENOENT/ESRCH だけを「無い」として扱い他は re-raise / None / unobservable
  フラグで返す、fail-closed な向き、または実際の安全装置が別の層 (O_EXCL の
  atomic create 等) にある、のいずれか。reason に根拠を書く。
* `KNOWN_FAIL_OPEN` — **この task で見つかった、まだ直っていない同族の欠陥**。
  この PR では直さない (直しに行くとスコープが広がる。memory:
  pr-size-breaks-review-machinery)。reason に危険度を書く。Director backlog へ
  (Result 参照)。

赤の実証: `tests/red_proof_t053.sh` (最初の実装) / `tests/red_proof_t110.sh`
(Codex review 2 巡目、下記 P2-1 / P2-2 の穴)。

    python3 -m pytest tests/test_observation_authority_does_not_fail_open.py -v

## 検出できる形・できない形 (Codex review 2 巡目 対応、t110 / t113)

Codex が 1 巡目で指摘した P2 ×2: (1) `from os import stat` のように import
した観測関数を裸の名前で呼ぶと `_risky_calls()` は "listdir" しか裸の名前を
知らず見逃す、(2) `OBSERVATION_SITES` はキー (script, function, snippet) の
有無しか見ないので、同じ関数に全く同じ文面の呼び出しを 2 つ目として足しても
(扱いが違っても) 1 つ目の分類を黙って引き継いで通ってしまう。

QA t054（Erik、2026-09-28）がさらに 2 形の素通りを実測: (a) `m = p.stat; m()`
/ `getattr(p, "stat")()` のような変数束縛・`getattr` 経由の呼び出しは
`Call.func` が `ast.Attribute` でないため検出されない、(b) 実際の
`os.stat()` 等を `AUTHORITY_MODULES` 外の新しいヘルパー関数に置き、監査対象
ファイル側はそのヘルパーを呼ぶだけにすると、監査対象ファイルの AST 上には
リスクのある属性名が一切現れないため検出されない。(a) は同じ関数内の代入を
たどれば安く塞げるので塞いだ (t110)。(b) はモジュール単位の静的解析の原理的
な限界であり **塞がない** — 「監査対象のファイルが新しい lib を import した
らその lib も監査対象に入れるか理由を書け」という自動化案も検討したが、
import の並び替え・re-export・動的 import まで含めると検出器自体が
`AUTHORITY_MODULES` と同じ穴を抱える別の静的解析になり、この task のスコープ
(2 巡目の P2 ×2 + QA (a)) を大きく超えるため見送り、(b) は下表への明記のみで
Director backlog に送る (Result 参照)。

Codex 2 巡目レビューの残り P2 ×2 (t113、この PR の最後の fix): (1) `m: object
= p.stat` のような型注釈つき代入 (`ast.AnnAssign`) は (a) の修正が
`ast.Assign` しか見ていなかったため引き続き見逃していた — 注釈の無い同じ形
(`m = p.stat`) は拾えるのに、である。ついでに `ast.NamedExpr` (代入式、
walrus) 経由の束縛・即時呼び出しも同じ理由で見ていなかったので、あわせて
塞いだ (P2-1)。(2) `_owner_by_line()` は「関数ごとに部分木全体へ
`setdefault`」する作りで、`ast.walk(tree)` が外側の関数を先に見つけるため
**入れ子関数の行がすべて外側の関数名に帰属していた** —— 入れ子の兄弟関数へ
呼び出しを移しても `(script, function, snippet)` キーが変わらず、上の表の
「呼び出しを別関数へ移動」の保証 (「移動先で未分類として拾われる」) が
入れ子関数の間では成り立っていなかった。現在のスコープを追う再帰的な辿り方
に変えて、各行を最も内側の関数へ帰属させるようにした (P2-2)。

| 形 | 検出 | 根拠 |
|---|---|---|
| 直接 attribute (`os.stat(...)`, `p.exists()`) | する | `.attr` で判定 (モジュール名を問わない) |
| `import os as o` → `o.stat(...)` | する | 上と同じ (attribute のまま、alias の影響を受けない) |
| `from os import stat` → `stat(...)` | する (t110) | `_imported_risky_names()` が `from ... import` の別名を RISKY_ATTRS に解決してから裸の `Name` 呼び出しと突き合わせる |
| `from os import stat as st` → `st(...)` | する (t110) | 同上。`asname` を解決 |
| `os.path.exists(...)` / `Path(...).exists()` | する | `.attr == "exists"` |
| 変数束縛 (`m = p.stat` → `m()`) | する (t110、QA t054 (a)) | `_bound_risky_names_by_line()` が同じ関数内の `Assign` を辿り、右辺が risky attribute アクセスの変数名を集めて後続の裸呼び出しと突き合わせる |
| 型注釈つき代入 (`m: object = p.stat` → `m()`) | する (t113、P2-1) | `_bound_risky_names_by_line()` は `ast.Assign` に加え `ast.AnnAssign` も同じ判定に含める (値の無い注釈だけの形は束縛が起きないので対象外) |
| 代入式 (walrus) で束縛し、後で裸呼び出し (`if (m := p.stat): ... m()`) | する (t113、P2-1) | `_bound_risky_names_by_line()` は `ast.NamedExpr` も同じ判定に含める |
| 代入式 (walrus) の結果をその場で呼ぶ (`(m := p.stat)()`) | する (t113、P2-1) | `Call.func` 自身が `ast.NamedExpr` になるこの形は束縛→裸呼び出しの突き合わせでは捉えられないので、`_is_risky_named_expr()` で個別に判定する |
| `getattr(p, "stat")()` (即時呼び出し) | する (t110、QA t054 (a)) | `_is_getattr_literal_risky()` が `Call.func` 自身が `getattr(obj, "risky-name")` の形かを判定する |
| `getattr` を変数へ束縛して後で呼ぶ (`m = getattr(p, "stat"); m()`) | する (t110、QA t054 (a)) | 上 2 つと同じ仕組み。`Assign` の右辺が `getattr(..., "risky-name")` の Call でも束縛対象に加える |
| 同一関数内で同一文面の呼び出しが 2 回目以降 | する (t110) | `test_duplicate_call_counts_are_audited` が `_all_sites()` の出現回数を数え、`EXPECTED_OCCURRENCES` に無い増加を落とす (allowlist のキー一致だけでは通ってしまうための補強) |
| 呼び出しを別関数 (兄弟でない) へ移動 | する | `_owner_by_line()` が行番号→関数名を都度再計算するので、移動先で「未分類」として拾われる (allowlist のキーに旧関数名が入っているため) |
| 呼び出しを入れ子関数の兄弟間で移動 (`def outer(): def a(): ...call...; def b(): ...`) | する (t113、P2-2 で修正。それまでは見逃していた) | 以前は `_owner_by_line()` が入れ子関数の行をすべて外側の関数名に帰属させていたため、`a()` から `b()` へ移しても `(outer, snippet)` という同じキーのままで「未分類」として拾われなかった。現在のスコープを追う再帰的な辿り方に変え、各行を最も内側の関数 (`a` または `b`) へ帰属させる |
| ネストした関数・ラムダの中の呼び出し (検出そのもの) | する | `ast.walk` は関数境界を無視して木全体の `Call` を辿る (帰属先の正しさは上の行を参照 — 検出と帰属は別の問題) |
| `contextlib.suppress(OSError)` で囲んだ呼び出し | 呼び出し自体はする / 握り潰しの有無はしない | `_risky_calls` は `Call` ノードだけを拾い、周囲が `try/except` か `suppress` かは見ない。呼び出しは必ず allowlist 行を要求されるので目には触れるが、「fail-open に潰していないか」の判定は reason 欄に人間が書く (この task のスコープはあくまで「未分類の呼び出しを見逃さないこと」) |
| `try` の `else` 節での呼び出し | 呼び出し自体はする / 上と同じ限界 | `ast.walk` は `try/else` 内の `Call` も辿るが、else に置くことで何を握り潰していないかの検証は reason 欄に委ねる |
| 呼び出し結果を変数に代入し、離れた場所で fail-open な判定に変換する (間接化) | しない | AST は呼び出し箇所そのものは拾うが、戻り値がどう使われるかまでは追跡しない (`test_every_risky_call_is_classified` の元々の設計限界。reason は人間が読んで書く前提) |
| **監査対象外 (`AUTHORITY_MODULES` に無いファイル) へ委譲したヘルパー呼び出し** | **しない (QA t054 (b)、意図的に塞がない)** | モジュール単位の静的解析の原理的な限界。監査対象ファイルの AST 上に risky attribute 名そのものが現れないので `_risky_calls()` は原理的に検出できない。自動追跡 (import 先も再帰的に監査対象へ入れる) は動的 import・re-export まで含めると検出器自身が同じ穴を抱えるため見送り (Result 参照)。新しいヘルパーへ観測呼び出しを切り出すときは、そのヘルパーを手動で `AUTHORITY_MODULES` に足すこと |
| 同名メソッドを持つ別クラスへ呼び出しを移動 (`A.check(...)` → `B.check(...)`) | しない (3 巡目、t104 backlog) | `_owner_by_line()` は行番号を**関数名だけ**でキーにし、クラス (修飾名) を区別しない。同じメソッド名の別クラスへ呼び出しを移しても `(script, function, snippet)` キーが変わらず「未分類」として拾われない |
| 別名の連鎖 (`a = p.stat; b = a; b()`) | しない (3 巡目、t104 backlog) | `_bound_risky_names_by_line()` は risky attribute への直接代入 (`m = p.stat`) は解決するが、変数から変数への再代入までは辿らない。`b = a` の時点で `a` が risky であることを追跡しないため `b()` は未分類にならない |

**この表に無い見逃し形を Codex が 3 巡目に見つけたら、その時点で打ち切る**
(ガードは完全でなくてよい。見逃す形が文書化されていればよい)。**実際に 3 巡目でこの 2 形が
見つかり、Director 判断で打ち切りの線が引かれた (#244、2026-09-28) ため上の 2 行が最後の追記に
なる** — コードは変えず、この表への記載だけで確定した。
"""

from __future__ import annotations

import ast
import pathlib
from collections import Counter

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"

#: この関数呼び出しの `.attr` が現れたら「観測」とみなす。
#: stat 系 (`stat`/`access`) / 存在確認 (`exists`) / 内容読み取り
#: (`read_bytes`/`read_text`) / symlink 解決 (`readlink`) / 列挙
#: (`iterdir`/`listdir`)。書き込み系 (`write_*`/`unlink`) は対象外 ——
#: この task は「観測の失敗」の扱いを見るのであって、書き込みの安全性は
#: 別の関心事 (`tests/test_queue_reads_go_through_the_guard.py` 等)。
RISKY_ATTRS = frozenset({
    "stat", "exists", "read_bytes", "read_text", "readlink", "access",
    "iterdir", "listdir",
})

AUTHORITY_MODULES = [
    SCRIPTS_DIR / "lib_pane_process.py",
    SCRIPTS_DIR / "lib_retirement.py",
    SCRIPTS_DIR / "lib_daemon_watch.py",
    SCRIPTS_DIR / "lib_mux.py",
    SCRIPTS_DIR / "worktree_gc.py",
    SCRIPTS_DIR / "watchdog.py",
    TESTS_DIR / "leaked_descendants.py",
    TESTS_DIR / "kill_budget.py",
]


def _owner_by_line(tree: ast.AST) -> dict[int, str]:
    """行番号 → その行を含む、**最も内側の**関数名。

    以前は `test_queue_reads_go_through_the_guard.py` の `_owner_by_line()` と
    同じ「`ast.walk(tree)` で見つけた関数ごとに、その部分木全体へ
    `setdefault` する」作りだった。`ast.walk()` は幅優先なので外側の関数が
    先に見つかり、その `ast.walk(outer_func)` が入れ子関数の行も含めて丸ごと
    `setdefault` してしまう —— 後から本当の持ち主 (入れ子関数自身) を処理
    しても `setdefault` は上書きしないため、**入れ子関数の行がすべて外側の
    関数名に帰属していた** (P2-2, PR #244 Codex review 2 巡目)。入れ子の
    兄弟関数へ呼び出しを移しても `_owner_by_line` の答え (外側の関数名) が
    変わらず、`OBSERVATION_SITES` のキーも変わらないため、移動が「未分類」
    として拾われない (モジュール docstring の「呼び出しを別関数へ移動」の
    保証に反する)。

    ここでは木を根から再帰的に辿りながら「現在のスコープ (直近の関数名)」を
    引き継ぎ、各行に到達した時点のスコープをそのまま記録する —— 各行は
    この再帰の中でちょうど 1 回だけ訪れるので `setdefault` は不要 (後から
    見つかった方が常に正しい、最も内側の関数)。
    """
    owner: dict[int, str] = {}

    def visit(node: ast.AST, current: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            scope = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current
            )
            if scope is not None and hasattr(child, "lineno"):
                owner[child.lineno] = scope
            visit(child, scope)

    visit(tree, None)
    return owner


def _imported_risky_names(tree: ast.AST) -> dict[str, str]:
    """`from module import name [as alias]` で束縛されたローカル名 → 元の名前。

    `foo.stat()` のような属性アクセスはどのモジュールから来たかを問わず
    `.attr` だけで判定できる (`import os as o` で `o` に別名を付けても
    `o.stat()` は変わらず Attribute なので影響を受けない)。しかし
    `from os import stat` の後の裸の `stat(...)` はローカル名を元の名前へ
    解決しないと拾えない (P2-1, PR #244 Codex review 2 巡目 — 以前は
    "listdir" だけを裸の名前として特別扱いしており、他の観測関数を
    import されると見逃していた)。
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name in RISKY_ATTRS:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def _is_getattr_literal_risky(node: ast.AST) -> bool:
    """`getattr(obj, "risky-name", ...)` の形か (QA t054 (a))。

    `obj` がどんな式かは問わない — 属性アクセス (`obj.attr`) の判定が
    モジュール名を問わないのと同じで、第2引数の文字列リテラルだけを見る。
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        and node.args[1].value in RISKY_ATTRS
    )


def _is_risky_named_expr(node: ast.AST) -> bool:
    """`(m := p.stat)()` のように、代入式 (walrus) の結果をその場で呼ぶ形か
    (P2-1, PR #244 Codex review 2 巡目)。

    `_bound_risky_names_by_line()` は「先に束縛して後で裸の名前で呼ぶ」形を
    拾うが、この形は `Call.func` 自身が `ast.NamedExpr` になり束縛と呼び出しが
    同じ式の中で起きるので、束縛→裸呼び出しの突き合わせでは捉えられない。
    """
    return (
        isinstance(node, ast.NamedExpr)
        and (
            (isinstance(node.value, ast.Attribute) and node.value.attr in RISKY_ATTRS)
            or _is_getattr_literal_risky(node.value)
        )
    )


def _bound_risky_names_by_line(tree: ast.AST) -> dict[int, set[str]]:
    """行番号 → その行を含む関数内で、risky attribute に束縛されたローカル
    変数名の集合 (QA t054 (a): `m = p.stat` / `m = getattr(p, "stat")`)。

    代入の時点ではまだ呼び出していないので `.attr`/`getattr` の判定だけでは
    捉えられない。後で `m()` と呼ばれた時点で初めて観測が起きる。
    `_owner_by_line()` と同じ「関数ごとに」束縛名を集める作りに揃えている
    (ネストした関数での扱いも一貫させるため)。

    P2-1 (PR #244 Codex review 2 巡目): 以前は `ast.Assign` しか見ておらず、
    `m: object = p.stat` のような型注釈つき代入 (`ast.AnnAssign`) は束縛として
    拾えなかった (注釈の無い同じ形は拾えていたのに、である)。`ast.NamedExpr`
    (`m := p.stat`) 経由の束縛 — 呼び出しとは別の場所で束縛し、後で `m()` と
    裸呼び出しする形 — も同じ理由で見ていなかったので、あわせて対象にする。
    3 つの形はどれも「対象 (`target`) に risky な値 (`value`) を束縛する」
    という同じ構造なので、`(targets, value)` に正規化してから 1 本の判定に
    まとめる (分岐を増やさない)。
    """
    bound: dict[int, set[str]] = {}
    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names: set[str] = set()
        for node in ast.walk(func_node):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            elif isinstance(node, ast.NamedExpr):
                targets, value = [node.target], node.value
            else:
                continue
            if value is None:
                continue  # `x: int` (注釈のみ、値の無い AnnAssign) — 束縛が起きない
            risky = (
                (isinstance(value, ast.Attribute) and value.attr in RISKY_ATTRS)
                or _is_getattr_literal_risky(value)
            )
            if not risky:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        for sub in ast.walk(func_node):
            if hasattr(sub, "lineno"):
                bound.setdefault(sub.lineno, names)
    return bound


def _risky_calls(path: pathlib.Path) -> list[tuple[str, str]]:
    """`(関数名, ソース断片)` を、このファイルの「観測」呼び出しについて返す。

    `p.stat()` / `os.stat()` はどちらも `ast.Attribute` (`.attr` で拾える —
    `os.stat` は `Attribute(attr="stat", value=Name("os"))` なので
    RISKY_ATTRS の `"stat"` に自然に当たる)。`from os import listdir` の
    ような裸の名前 (`ast.Name`) の呼び出しは `_imported_risky_names()` が
    解決した別名の集合と、同じ関数内で変数や `getattr` に束縛された名前は
    `_bound_risky_names_by_line()` と突き合わせる (QA t054 (a))。`(m := p.stat)()`
    のように束縛と呼び出しが同じ式で起きる形は `_is_risky_named_expr()` で
    別に見る (P2-1)。`AUTHORITY_MODULES` 外のヘルパーへ委譲された呼び出しは、
    このファイルの AST 上に risky attribute 名そのものが現れないため原理的に
    見えない (QA t054 (b)。モジュール上の docstring 表を参照)。
    """
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    owner = _owner_by_line(tree)
    risky_names = _imported_risky_names(tree)
    bound_by_line = _bound_risky_names_by_line(tree)
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        hit = False
        if isinstance(func, ast.Attribute) and func.attr in RISKY_ATTRS:
            hit = True
        elif isinstance(func, ast.Name) and func.id in risky_names:
            hit = True
        elif isinstance(func, ast.Name) and func.id in bound_by_line.get(node.lineno, ()):
            hit = True
        elif _is_getattr_literal_risky(func):
            hit = True
        elif _is_risky_named_expr(func):
            hit = True
        if not hit:
            continue
        seg = ast.get_source_segment(src, node) or "<unparsed>"
        found.append((owner.get(node.lineno, "<module>"), " ".join(seg.split())))
    return found


def _all_sites() -> list[tuple[str, str, str]]:
    """`(script, function, snippet)` を全 AUTHORITY_MODULES について返す。"""
    out: list[tuple[str, str, str]] = []
    for path in AUTHORITY_MODULES:
        for fn, seg in _risky_calls(path):
            out.append((path.name, fn, seg))
    return out


SAFE = "SAFE"
KNOWN_FAIL_OPEN = "KNOWN_FAIL_OPEN"

# ---------------------------------------------------------------------------
# 判定した対象 (script, function, snippet) -> (status, reason)
# ---------------------------------------------------------------------------
#
# 各行の reason は、そのコードを実際に読んで確かめた結論。「たぶん安全」では
# なく、fail-closed の根拠 (ENOENT/ESRCH だけを通す・None/unobservable で
# 返す・呼び出し側が hold/assume-alive 側に倒す・O_EXCL のような別層の安全
# 装置がある、等) を書く。

OBSERVATION_SITES: dict[tuple[str, str, str], tuple[str, str]] = {
    # -- lib_pane_process.py: watchdog の idle/job 判定 --------------------
    ("lib_pane_process.py", "_proc_stat",
     'Path(f"/proc/{pid}/stat").read_bytes()'):
        (SAFE, "ENOENT/ESRCH だけ None。他の OSError は re-raise し呼び出し側 "
               "(classify_process_tree) が unknown に倒す (t082/t049 族A)"),
    ("lib_pane_process.py", "_proc_cmdline",
     'Path(f"/proc/{pid}/cmdline").read_bytes()'):
        (SAFE, "同上。cmdline が読めない job wrapper を idle_process に "
               "誤判定しない契約 (t082)"),
    ("lib_pane_process.py", "_proc_environ",
     'Path(f"/proc/{pid}/environ").read_bytes()'):
        (SAFE, "同上。空 ({}) は消滅でも読めないでもない正当な結果として区別 (t091)"),
    ("lib_pane_process.py", "_proc_exe", 'os.readlink(f"/proc/{pid}/exe")'):
        (SAFE, "同上契約。_is_session_body() は exe が読めない場合 False = "
               "infra 側委譲で、危険側 (job 見逃し) には倒れない (t097)"),
    ("lib_pane_process.py", "_proc_state",
     'Path(f"/proc/{pid}/stat").read_bytes()'):
        (SAFE, "§11 (t003): 全 OSError を None (= zombie と確定できない) にするが、"
               "None は『木から外す』の根拠にならない (外すのは state が Z と読めた時だけ)。"
               "読めないノードは通常の分類に進み、そちらの cmdline/environ の OSError が "
               "unknown に倒す。zombie でないものを zombie と読む向きの誤りは作らない"),
    ("lib_pane_process.py", "_classify", 'Path("/proc").iterdir()'):
        (SAFE, "列挙自体が失敗した場合の扱いはこの1呼び出しの外 (呼び出し元は "
               "個々の pid だけを辿るので、iterdir 失敗は root 直下の列挙のみに "
               "影響し pane 内の既知 pid ツリーには波及しない"),

    # -- lib_retirement.py: Worker の退役・kill --------------------------
    ("lib_retirement.py", "process_alive",
     'Path(f"/proc/{pid}/stat").read_text(errors="replace")'):
        (SAFE, "呼び出しは try 節の外で FileNotFoundError/ProcessLookupError を "
               "個別に捕まえ、それ以外は re-raise (呼び出し側で fail-closed)"),
    ("lib_retirement.py", "has_marker",
     'request_path(self.registry_dir, agent).exists()'):
        (SAFE, "pathlib.Path.exists() は EACCES を False に潰さず raise する "
               "(実測)。本当の二重書き込み防止は _write_request() の "
               "write_json_exclusive (O_EXCL) —— has_marker() は早期ログ用の "
               "ソフトな事前チェックに過ぎない"),
    ("lib_retirement.py", "has_marker",
     'progress_path(self.registry_dir, agent).exists()'):
        (SAFE, "同上。O_EXCL が本当の関門"),
    ("lib_retirement.py", "_write_request",
     'progress_path(self.registry_dir, agent).exists()'):
        (SAFE, "同上。この直後の write_json_exclusive(request_path, ...) が "
               "WROTE_EXISTS を返せば書き込みを拒否するので、この exists() が "
               "誤って False になっても二重request の実害は O_EXCL 側で止まる"),
    ("lib_retirement.py", "_check_stall",
     'stall_path(self.registry_dir, agent).exists()'):
        (SAFE, "誤って False (再報告扱い) になっても、結果は Director への "
               "再通知 (ノイズ) であって kill/queue 書き換えではない"),
    ("lib_retirement.py", "list_agents", "d.iterdir()"):
        (SAFE, "OSError は個々の agent ディレクトリではなく registry_dir 直下の "
               "列挙。呼び出し側 (process_all) は空リストなら「今回は何もしない」 "
               "側に倒れ、既存の退役処理を止めない"),
    ("lib_retirement.py", "_guard",
     '(self.queue_dir / "assignments" / agent).exists()'):
        (KNOWN_FAIL_OPEN,
         "HIGH: `except OSError: pass` が exists() の PermissionError も含めて "
         "握り潰し、コードは『assignment 無し』のときと同じ経路へ抜ける。"
         "assignment が実在するのに (EACCES 等で) 観測できなかった場合、"
         "idle retirement の premise 再チェックが素通りし GUARD_DISCARD を "
         "返さない —— タスクを再取得した Worker が誤って kill 候補に残る。"
         "この task では直さない (t053 スコープ外)。Director backlog へ。"),

    # -- lib_daemon_watch.py: dispatcher/watchdog 相互監視 -----------------
    ("lib_daemon_watch.py", "process_generation",
     'Path(proc_root, str(pid), "stat").read_text(encoding="utf-8", errors="replace")'):
        (SAFE, "全 OSError で None。呼び出し元 instance_alive() は "
               "『generation を read できない = alive のまま』に倒す設計 "
               "(docstring: \"the direction we must fail in is 'do not "
               "respawn'\")"),
    ("lib_daemon_watch.py", "scan_daemon_pids", "root.iterdir()"):
        (SAFE, "OSError で None を返し、呼び出し側は『could not look』として "
               "hold する (docstring: \"holding forever ... is the correct "
               "direction to fail in\")"),
    ("lib_daemon_watch.py", "scan_daemon_pids",
     '(entry / "cmdline").read_bytes()'):
        (SAFE, "ENOENT/ESRCH だけ continue (消えた)。他は None を return し "
               "walk 全体を『不完全』として hold させる"),
    ("lib_daemon_watch.py", "scan_daemon_pids",
     '(entry / "stat").read_text(encoding="utf-8")'):
        (SAFE, "cmdline 一致後の zombie 判定専用。読めなければ pass して "
               "found.append(pid) —— 『生きているとみなす』方向で、これは "
               "respawn を許可しない (=二重起動させない) 側の安全な倒し方"),
    ("lib_daemon_watch.py", "_remove_marker", "path.exists()"):
        (SAFE, "unlink() が FileNotFoundError 以外の OSError を投げた後の "
               "確認。exists() が (EACCES 等で) raise すれば例外は関数の外へ "
               "伝播し『消せたか分からない』が明示的に見える形になる —— 黙って "
               "\"消せた\" にはならない"),

    # -- lib_mux.py: pane の kill・spawn 記録 ------------------------------
    ("lib_mux.py", "_proc_cwd", "os.readlink(link)"):
        (SAFE, "OSError を捕まえ _PROC_GONE (ENOENT かつ親も消滅) か "
               "_PROC_UNREADABLE の三値で返す。呼び出し側は UNREADABLE を "
               "absence と区別する契約 (docstring)"),
    ("lib_mux.py", "_proc_cwd", "link.parent.exists()"):
        (SAFE, "ENOENT 系エラーが本当に『/proc/<pid> ごと消えた』かを補強する "
               "追加確認。ここが raise しても外側の except OSError が拾わず "
               "_PROC_UNREADABLE 側へは倒れない実装だが、EACCES で親が読めない "
               "状況自体が readlink 側で既に UNREADABLE を返した後の分岐で "
               "起きるので影響は absence 判定を誤って GONE にしない方向に留まる"),
    ("lib_mux.py", "repo_identity_ok", '(root / ".git").exists()'):
        (SAFE, "docstring: 'Anything else (deleted, recreated as an empty "
               "directory, a permission error) returns False so callers can "
               "fail closed: skip the action (do not kill)'"),
    ("lib_mux.py", "_read_proc",
     'path.read_text(encoding="utf-8", errors="replace")'):
        (SAFE, "OSError を _PROC_GONE (ENOENT/ESRCH) / _PROC_UNREADABLE の "
               "三値に分ける。呼び出し側は UNREADABLE を absence と区別する"),
    ("lib_mux.py", "_live_children", "Path(proc_root).iterdir()"):
        (SAFE, "OSError で None (docstring: \"could not look\" is not "
               "\"nothing there\")。空リストと未観測を区別する呼び出し契約"),
    ("lib_mux.py", "proc_table", "Path(proc_root).iterdir()"):
        (SAFE, "同上 (proc_table も None/空を区別する契約)"),
    ("lib_mux.py", "proc_table",
     '(entry / "cmdline").read_bytes()'):
        (SAFE, "t101: errors=\"replace\" で decode 例外を避けるのみ。読み取り "
               "失敗 (OSError) 時の扱いはこの関数のさらに外側の except で "
               "None に倒す設計 (docstring: incomplete walk = None)"),
    ("lib_mux.py", "proc_table",
     '(entry / "stat").read_text(encoding="utf-8", errors="replace")'):
        (SAFE, "同上"),
    ("lib_mux.py", "reap_stale_pane_records", "os.listdir(directory)"):
        (SAFE, "FileNotFoundError は [] (無いのが普通)。他の OSError は "
               "warning を出して [] —— 記録を 1 件も落とさない (docstring: "
               "\"Every doubt keeps the record\")"),
    ("lib_mux.py", "_resolve_for_ownership", "os.readlink(current)"):
        (SAFE, "呼び出しは try/except で包まれ、読めなければ UNDECIDED を返す "
               "設計 (docstring: \"there is no fact of the matter ... "
               "UNDECIDED rather than a guess\")。UNDECIDED は kill を "
               "authorise する MINE 判定にはならない"),

    # -- worktree_gc.py: worktree の隔離・復元 -----------------------------
    ("worktree_gc.py", "cmd_restore", "os.path.exists(orig)"):
        (KNOWN_FAIL_OPEN,
         "LOW/MEDIUM: os.path.exists() は EACCES を含む全 OSError を False に "
         "潰す (pathlib.Path.exists() と異なり raise しない)。orig が実在する "
         "のに権限で観測できないと、この『既に何かある』ガードを素通りして "
         "git worktree move が orig の中へ移動してしまう (既存ディレクトリへの "
         "move は『中へ移す』動作になる、と同関数の docstring)。操作は人間が "
         "CLI から明示的に叩く復元コマンドなので影響範囲は限定的。この task "
         "では直さない。Director backlog へ。"),
    ("worktree_gc.py", "apply_quarantine", "os.path.exists(dest)"):
        (KNOWN_FAIL_OPEN,
         "LOW: 同じ os.path.exists() の EACCES 吸収。dest はこの関数が "
         "マイクロ秒精度のタイムスタンプで新規生成する隔離先パスなので、"
         "他プロセスが同じ dest を既に権限制限付きで作っている確率は極めて "
         "低いが、理論上は同じ型。git worktree move 失敗時は『削除にフォール "
         "バックせず failed にする』(族A) ため、最悪でも隔離は失敗として "
         "報告されるだけでデータ消失はしない。この task では直さない。"
         "Director backlog へ。"),
    ("worktree_gc.py", "load_target_dirs", "os.listdir(workers)"):
        (SAFE, "ENOENT だけ [] (docstring: registry/workers が無いのは普通)。"
               "他の OSError は理由文字列付きで返し、呼び出し側は全 Worker を "
               "keep にする (docstring: \"呼び出し側は全部 keep にする\")"),
    ("worktree_gc.py", "_cwd_unreadable_but_harmless",
     "(base / 'stat').read_text()"):
        (SAFE, "FileNotFoundError だけ '消えた' を返す。他の (OSError, "
               "ValueError) は None (=harmless と言えない=デフォルトで keep "
               "側)。この関数自体が『keep するかどうか』の除外判定で、既定の "
               "戻り値 None が keep 側 (呼び出し元 scan_process_cwds の "
               "docstring: 読めなければ 'the cwd が worktree の中かもしれない "
               "ので取れなかった扱い')"),
    ("worktree_gc.py", "_cwd_unreadable_but_harmless",
     "(base / 'cmdline').read_bytes()"):
        (SAFE, "同上 (同じ try/except ブロック内)"),
    ("worktree_gc.py", "_cwd_unreadable_but_harmless",
     "(base / 'comm').read_text()"):
        (SAFE, "同上"),
    ("worktree_gc.py", "scan_process_cwds", "os.readlink(proc / pid / 'cwd')"):
        (SAFE, "ENOENT/ESRCH は継続 (消えた)。それ以外は "
               "_cwd_unreadable_but_harmless() で harmless と言えたものだけを "
               "除外し、それ以外は found に『取れなかった』側で残す "
               "(docstring: \"取れなかった扱い\")"),
    ("worktree_gc.py", "scan_process_cwds", "os.listdir(proc)"):
        (SAFE, "OSError はエラー文字列付きで [] を返し呼び出し側 (verdict "
               "判定) は keep 側に倒す (\"読めない・...は keep\"、"
               "scripts/CLAUDE.md worktree_gc.py 節)"),
    ("worktree_gc.py", "scan_process_cwds", "os.stat(proc / pid)"):
        (SAFE, "st_uid 判定用。OSError (ENOENT/ESRCH) は消滅として continue、"
               "それ以外は harmless 判定を経て『取れなかった』側に残る "
               "(_cwd_unreadable_but_harmless と同じ判断)"),
    ("worktree_gc.py", "scan_process_cwds",
     '(proc / pid / \'comm\').read_text(errors="replace")'):
        (SAFE, "cwd が読めず harmless でもないと既に確定した後の、エラー文言 "
               "用の表示専用の読み取り。失敗しても except OSError: comm = '?' "
               "で握り潰し、直後の return [], f'... を読めない ...' は不変 —— "
               "この読み取りの成否は『取れなかった扱い』という結論に影響しない"),

    # -- watchdog.py: Worker の hard_idle/max による terminate --------------
    ("watchdog.py", "load_active_tasks", "state_file.exists()"):
        (SAFE, "docstring: 読めなければ『監視対象なし』=『誰も kill しない』が "
               "この判定の安全な向き (empty-vs-unobservable.md 表 F と同じ)"),
    ("watchdog.py", "_leave_legacy_log_pointer", "legacy.exists()"):
        (SAFE, "旧 logs/ の移行コード。失敗しても watchdog の判定には入らない "
               "(ALLOWED_DIRECT_READS の同エントリと同じ理由)"),
    ("watchdog.py", "_leave_legacy_log_pointer",
     "legacy.read_text(errors=\"replace\")"):
        (SAFE, "同上"),
    ("watchdog.py", "_mtimes_since_floor", "p.stat()"):
        (SAFE, "t017 で修正済み。FileNotFoundError だけ continue、他は "
               "unobservable=True を立てて呼び出し側 (terminate 判定) が "
               "抑制する (tests/test_stat_failure_is_not_silence.py が固定)"),
    ("watchdog.py", "_notification_files", "notif_dir.iterdir()"):
        (SAFE, "t016 で修正済み。FileNotFoundError だけ [] (無いのが普通)、"
               "他は None で『観測できなかった』を呼び出し側へ渡す"),
    ("watchdog.py", "_newest_notification", "f.stat()"):
        (SAFE, "t017 で修正済み。FileNotFoundError だけ continue、他は "
               "(now, \"(unobservable)\") を返し _awaiting_human() の抑制を "
               "外さない"),

    # -- tests/leaked_descendants.py: pytest 自身の kill 権限 ---------------
    ("leaked_descendants.py", "available", '(_PROC / "self" / "stat").exists()'):
        (SAFE, "ガード全体が動くかどうかの可用性チェック。False なら "
               "install() が warning を出して guard 自体を無効化する ("
               "黙って全部通す側ではなく明示的な警告)"),
    ("leaked_descendants.py", "_read_stat",
     '(_PROC / str(pid) / "stat").read_bytes()'):
        (SAFE, "OSError で None。呼び出し元 _scan_one() は stat is None を "
               "『居るのに読めない』として (_PROC/str(pid)).exists() で "
               "再確認し unobservable 側へ倒す (kill 許可にはしない)"),
    ("leaked_descendants.py", "_read_bytes", "path.read_bytes()"):
        (SAFE, "OSError で None。_belongs() は None を observed=False として "
               "扱い、3 signal のうち 1 つでも読めなければ観測失敗に倒す"),
    ("leaked_descendants.py", "pids", "os.listdir(_PROC)"):
        (KNOWN_FAIL_OPEN,
         "MEDIUM (P2-3, PR #244 Codex review 2 巡目): 以前は「OSError で空集合。"
         "呼び出し元 snapshot()/scan() は差分ベースなので、列挙自体が失敗すると "
         "新規プロセスを 1 つも検出できず kill 対象が増えない方向 (fail-closed)」"
         "を根拠に SAFE としていたが、これは『kill を許可しない』ことだけを見て "
         "おり、この監査の目的 (観測失敗を不在に潰さないこと) そのものに反する。"
         "`/proc` の列挙自体が失敗する (EACCES 等) と `scan()` は 1 件も pid を "
         "見つけられないまま `unobservable=0` の『クリーン』な `Scan` を返し、"
         "`settle()` は即座に終わって不確かさの警告 (`_report_unobservable_only`) "
         "が一切出ない —— 観測できなかったことが呼び出し元に一切伝わらない。"
         "この task では直さない (スコープ外)。Director backlog へ。"),
    ("leaked_descendants.py", "_belongs",
     'os.readlink(os.fsencode(base / "cwd"))'):
        (SAFE, "OSError で observed=False。3 signal (environ/cmdline/cwd) の "
               "うち 1 つでも読めなければ観測失敗として survivor 判定を "
               "保留する設計 (docstring)"),
    ("leaked_descendants.py", "_scan_one", '(_PROC / str(pid)).exists()'):
        (SAFE, "stat が None のときの再確認。exists() の結果をそのまま "
               "unobservable フラグとして返すだけで、kill 許可には使わない"),
    ("leaked_descendants.py", "_scan_one", '(_PROC / str(pid)).stat()'):
        (KNOWN_FAIL_OPEN,
         "HIGH (P2-3, PR #244 Codex review 2 巡目): 以前は「uid 判定用。OSError "
         "は『走査中に死んだだけ』として survivor=None, unobservable=False "
         "(=「居ない」であって kill 許可の根拠ではない)」を根拠に SAFE として "
         "いたが、`except OSError: return None, False` は ENOENT/ESRCH と "
         "PermissionError/EIO 等を区別せず全部『居なくなった』に潰している。"
         "`kill を許可しないこと` は SAFE の根拠にならない —— 生きているが "
         "権限や I/O で観測できないだけのプロセスが survivors からも "
         "unobservable からも消え、settle() が『クリーン』を返して不確かさの "
         "警告が出ない (この監査の目的そのものに反する)。この task では直さ "
         "ない。Director backlog へ。"),
    ("leaked_descendants.py", "_uptime", '(_PROC / "uptime").read_text()'):
        (SAFE, "boot 経過時間の補助値。読めなければ 0.0 —— age 計算がやや "
               "不正確になるだけで kill 許可/survivor 判定そのものには使わない"),

    # -- tests/kill_budget.py: 判定と独立した第二の関門 ---------------------
    ("kill_budget.py", "_ppid_and_start",
     '(_PROC / str(pid) / "stat").read_bytes()'):
        (SAFE, "OSError で None。partition() は None を『年齢を検証できない』"
               "として refused に回す (fail-open にしない、と docstring が "
               "明言)"),
}


@pytest.mark.parametrize(
    "site", _all_sites(),
    ids=lambda s: f"{s[0]}:{s[1]}:{s[2][:40]}")
def test_every_risky_call_is_classified(site):
    """観測の呼び出しは、安全だろうと危険だろうと `OBSERVATION_SITES` に載って
    いること。

    RED の作り方 —— `tests/red_proof_t053.sh` を参照。新しい観測呼び出しを
    足すと (安全に見えても) このテストがその 1 件を報告して落ちる。
    """
    assert site in OBSERVATION_SITES, (
        f"{site[0]}:{site[1]}() に、分類されていない観測呼び出しがある:\n"
        f"  {site[2]}\n\n"
        f"  stat/proc/exists/iterdir の失敗を『許可 / 不在』に潰していないか "
        f"確認し、OBSERVATION_SITES に理由付きで 1 行足すこと。\n"
        f"  安全と確認できたなら status=SAFE、まだ直っていない同族の欠陥なら "
        f"status=KNOWN_FAIL_OPEN (危険度を reason に書き、Result で "
        f"Director backlog へ)。")


#: 既定では同じ (script, function, snippet) の呼び出しは 1 回だけ現れることを
#: 期待する。`OBSERVATION_SITES` はキーの有無しか見ないので、同じ関数に
#: 全く同じ文面の呼び出しを 2 つ目としてコピペで足しても (例外の扱いが違って
#: いても) 1 つ目の分類を黙って引き継いで通ってしまう (P2-2, PR #244 Codex
#: review 2 巡目)。意図して同じ呼び出しが複数回現れる (かつそれぞれの扱いを
#: 個別に確認済みの) 場合だけ、ここに件数を書く。書かずに件数が増えると
#: `test_duplicate_call_counts_are_audited` が落ちる。
EXPECTED_OCCURRENCES: dict[tuple[str, str, str], int] = {}


def test_duplicate_call_counts_are_audited():
    """同じ (script, function, snippet) の呼び出し回数が想定と違ったら落ちる。

    `test_every_risky_call_is_classified` はキーの有無しか見ないので、既に
    allowlist にある呼び出しと文面が全く同じ 2 つ目の呼び出しが増えても
    (キーが同じなら) 気付かれない。件数を独立に検査することで、新しい
    (2 つ目以降の) 呼び出しにも必ずレビューを要求する。
    """
    counts = Counter(_all_sites())
    mismatches = [
        (key, EXPECTED_OCCURRENCES.get(key, 1), actual)
        for key, actual in counts.items()
        if actual != EXPECTED_OCCURRENCES.get(key, 1)
    ]
    assert not mismatches, (
        "呼び出しの出現回数が想定と違う (未監査の重複、または件数の更新漏れ):\n"
        + "\n".join(
            f"  {s}:{f}(): {seg!r} expected={e} actual={a}"
            for (s, f, seg), e, a in mismatches
        )
        + "\n  意図した重複なら EXPECTED_OCCURRENCES に件数を書き、"
          "それぞれの呼び出しの扱いが本当に同じか確認すること。")


def test_the_allowlist_has_no_dead_entries():
    """`OBSERVATION_SITES` に、もう存在しない呼び出しの行が残っていないこと。

    死んだ行が残ると、その関数に別の観測呼び出しが戻ってきたときに **黙って
    許可** される —— `test_queue_reads_go_through_the_guard.py` の同名テストと
    同じ理由。
    """
    live = set(_all_sites())
    dead = sorted(k for k in OBSERVATION_SITES if k not in live)
    assert not dead, (
        "OBSERVATION_SITES に、もう存在しない観測呼び出しの行が残っている:\n"
        + "\n".join(f"  {s}:{f}(): {seg}" for s, f, seg in dead)
        + "\n  直したなら、その行は消すこと。")


def test_audited_call_count_has_a_floor():
    """検査した箇所の件数が 0 件や極端な減少で「空虚に PASS」しないこと。

    `AUTHORITY_MODULES` からの呼び出し形が変わって検出漏れが起きた場合、
    件数が黙って 0 に近づいて全テストが空虚に緑になる (memory:
    registry-dir-single-definition-and-vacuous-static-guards /
    coverage-table-cannot-find-what-it-omits)。2026-09-28 時点の実測は 52 件
    (8 ファイル)。10 件を割ったら検出器そのものが壊れている疑いが強い。
    """
    sites = _all_sites()
    assert len(sites) >= 10, (
        f"AUTHORITY_MODULES から検出された観測呼び出しが {len(sites)} 件しか "
        f"ない (期待は 10 件以上)。検出器 (_risky_calls / RISKY_ATTRS) が "
        f"対象モジュールの呼び出し形を見失っていないか確認すること。")


def test_terminal_summary(capsys):
    """検査した件数と内訳を出す (0 件で PASS にしないための可視化)。"""
    sites = _all_sites()
    safe = sum(1 for s in sites if OBSERVATION_SITES.get(s, (None,))[0] == SAFE)
    fail_open = sum(1 for s in sites
                    if OBSERVATION_SITES.get(s, (None,))[0] == KNOWN_FAIL_OPEN)
    print(f"[observation-authority] 検査した観測呼び出し: {len(sites)} 件 "
          f"(SAFE={safe} / KNOWN_FAIL_OPEN={fail_open}) / "
          f"対象モジュール: {len(AUTHORITY_MODULES)} 件")
    assert safe + fail_open == len(sites)
