#!/usr/bin/env bash
# t113 — PR #244 (t053) Codex review 2 巡目の残り P2 ×2 が、修正前は緑・修正後は
# 赤になることの実証 (P2-3 は allowlist の分類替えなので赤の実証は無い)。
#
# 対象: tests/test_observation_authority_does_not_fail_open.py
#
#   P2-1  型注釈つき代入 (`m: object = p.stat` → `m()`) / 代入式 (walrus,
#         `ast.NamedExpr`) 経由の束縛・即時呼び出しは、修正前の
#         `_bound_risky_names_by_line()` が `ast.Assign` しか見ておらず
#         `_risky_calls()` が検出できない (注釈の無い同じ形 `m = p.stat` は
#         t110 で既に拾えている)。
#   P2-2  `_owner_by_line()` は「関数ごとに部分木全体へ setdefault」する
#         作りで、外側の関数を先に処理するため入れ子関数の行がすべて外側の
#         関数名に帰属していた。入れ子の兄弟関数へ呼び出しを移しても
#         `(script, function, snippet)` キーが変わらず「未分類」として
#         拾われない。
#
# このガードは純粋な AST 静的検査なので (t053/t110 の red proof と同様)、
# 注入は**使い捨てのコピー**にだけ行い、本番の worktree のファイルには
# 一切触れない。
#
# 使い方:  bash tests/red_proof_t113.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t113.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

echo "== P2-1: AnnAssign / NamedExpr 経由の束縛・即時呼び出し =="

cp "$REPO_ROOT/scripts/worktree_gc.py" "$WORK/worktree_gc.py"
cat >>"$WORK/worktree_gc.py" <<'PYEOF'


def _t113_injected_annassign_probe(p):
    """red_proof_t113.sh (P2-1) が注入する、型注釈つき代入経由の束縛呼び出し。"""
    m: object = p.stat
    try:
        return m().st_uid
    except OSError:
        return 0


def _t113_injected_namedexpr_bound_probe(p):
    """代入式 (walrus) で束縛し、後で裸の名前で呼ぶ形。"""
    if (m := p.stat):
        try:
            return m().st_uid
        except OSError:
            return 0


def _t113_injected_namedexpr_immediate_probe(p):
    """代入式 (walrus) の結果をその場で呼ぶ形 (finding が例示した形そのもの)。"""
    try:
        return (m := p.stat)().st_uid
    except OSError:
        return 0
PYEOF

python3 - "$REPO_ROOT" "$WORK/worktree_gc.py" <<'PYEOF'
import ast
import importlib.util
import pathlib
import sys

repo_root, injected = sys.argv[1], sys.argv[2]
test_path = pathlib.Path(repo_root) / "tests" / "test_observation_authority_does_not_fail_open.py"
spec = importlib.util.spec_from_file_location("t113_guard1", str(test_path))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

injected_path = pathlib.Path(injected)
probe_fns = [
    "_t113_injected_annassign_probe",
    "_t113_injected_namedexpr_bound_probe",
    "_t113_injected_namedexpr_immediate_probe",
]


# --- OLD: t110 時点の _bound_risky_names_by_line() の凍結コピー (Assign だけ
#     を見る。getattr は既に扱うが AnnAssign/NamedExpr はまだ扱わない) ---
def old_bound_risky_names_by_line(tree):
    bound = {}
    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = set()
        for node in ast.walk(func_node):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            risky = (
                (isinstance(value, ast.Attribute) and value.attr in mod.RISKY_ATTRS)
                or mod._is_getattr_literal_risky(value)
            )
            if not risky:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        for sub in ast.walk(func_node):
            if hasattr(sub, "lineno"):
                bound.setdefault(sub.lineno, names)
    return bound


def old_hit(tree, bound_by_line):
    """t110 時点の _risky_calls() の判定 (NamedExpr 直接呼び出しの分岐が無い)。"""
    risky_names = mod._imported_risky_names(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        hit = (
            (isinstance(func, ast.Attribute) and func.attr in mod.RISKY_ATTRS)
            or (isinstance(func, ast.Name) and func.id in risky_names)
            or (isinstance(func, ast.Name) and func.id in bound_by_line.get(node.lineno, ()))
            or mod._is_getattr_literal_risky(func)
        )
        if hit:
            hits.append(node)
    return hits


src = injected_path.read_text()
tree = ast.parse(src, filename=str(injected_path))
old_bound = old_bound_risky_names_by_line(tree)
old_hits = old_hit(tree, old_bound)
owner = mod._owner_by_line(tree)
old_hit_fns = {owner.get(n.lineno, "<module>") for n in old_hits}

for fn in probe_fns:
    assert fn not in old_hit_fns, (
        f"{fn}: 旧アルゴリズムが既に検出している (前提が崩れている): {old_hit_fns}")
print("OLD: 旧アルゴリズム (t110 時点) は AnnAssign/NamedExpr を一件も検出しない (bug再現)")

# --- NEW: 修正後は 3 件とも検出し、allowlist に無いので RED になる ---
mod.AUTHORITY_MODULES = [m for m in mod.AUTHORITY_MODULES if m.name != "worktree_gc.py"] + [injected_path]
sites = mod._all_sites()
for fn in probe_fns:
    matches = [s for s in sites if s[1] == fn]
    assert matches, f"{fn} が新アルゴリズムでも検出できていない (修正が効いていない): {sites}"
    assert len(matches) == 1, matches
    site = matches[0]
    try:
        mod.test_every_risky_call_is_classified(site)
    except AssertionError:
        print(f"NEW: RED CONFIRMED — {fn}: {site}")
    else:
        raise SystemExit(f"NEW-FAIL: {fn} was not caught")

real_sites = [s for s in sites if s[1] not in probe_fns]
assert real_sites, "worktree_gc.py の実在サイトが 1 件も見つからない"
for s in real_sites:
    mod.test_every_risky_call_is_classified(s)
print(f"NEW: 既存の {len(real_sites)} 件は注入と無関係に緑のまま")
PYEOF
if [ $? -eq 0 ]; then
  ok "P2-1: 旧アルゴリズムは AnnAssign/NamedExpr を見逃す (緑) / 修正後は検出して RED になる"
else
  ng "P2-1: 期待した緑/赤の分岐が再現できなかった"
fi

echo "== P2-2: 入れ子関数の兄弟間で呼び出しを移動しても _owner_by_line が気付かない =="

# lib_* の隔離コピーは唯一の入口 tests/fixture_tree.sh の copy_scripts_libs を
# 通す (直接 cp すると tests/test_fixture_tree_is_the_only_copier.py が赤にする)。
source "$REPO_ROOT/tests/fixture_tree.sh"
copy_scripts_libs "$REPO_ROOT" "$WORK"

# 入れ子関数を持つ使い捨てモジュールを 2 版用意する: V1 は risky call が
# inner_a() の中、V2 は全く同じ文面のまま inner_b() へ移した版。
cat >"$WORK/scripts/t113_nested_probe_v1.py" <<'PYEOF'
def outer_wrapper(p):
    def inner_a():
        try:
            return p.stat().st_uid
        except OSError:
            return 0

    def inner_b():
        return None

    return inner_a() or inner_b()
PYEOF

cat >"$WORK/scripts/t113_nested_probe_v2.py" <<'PYEOF'
def outer_wrapper(p):
    def inner_a():
        return None

    def inner_b():
        try:
            return p.stat().st_uid
        except OSError:
            return 0

    return inner_a() or inner_b()
PYEOF

python3 - "$REPO_ROOT" "$WORK/scripts/t113_nested_probe_v1.py" "$WORK/scripts/t113_nested_probe_v2.py" <<'PYEOF'
import ast
import importlib.util
import pathlib
import sys

repo_root, v1_path, v2_path = sys.argv[1], sys.argv[2], sys.argv[3]
test_path = pathlib.Path(repo_root) / "tests" / "test_observation_authority_does_not_fail_open.py"
spec = importlib.util.spec_from_file_location("t113_guard2", str(test_path))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


# --- OLD: t110 時点の _owner_by_line() の凍結コピー (関数ごとに部分木全体へ
#     setdefault — 外側の関数が先に処理されるので入れ子は外側に帰属する) ---
def old_owner_by_line(tree):
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    owner.setdefault(sub.lineno, node.name)
    return owner


def owner_of_risky_call(owner_by_line, path):
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "stat"]
    assert len(calls) == 1, f"想定外の呼び出し件数: {calls}"
    owner = owner_by_line(tree)
    return owner.get(calls[0].lineno, "<module>")


v1_path, v2_path = pathlib.Path(v1_path), pathlib.Path(v2_path)

# OLD: V1 (inner_a に呼び出しがある) も V2 (inner_b へ移した) も、旧アルゴリズム
# では同じ "outer_wrapper" に帰属する —— 兄弟間の移動が見分けられない。
old_v1 = owner_of_risky_call(old_owner_by_line, v1_path)
old_v2 = owner_of_risky_call(old_owner_by_line, v2_path)
assert old_v1 == "outer_wrapper", old_v1
assert old_v2 == "outer_wrapper", old_v2
assert old_v1 == old_v2, (old_v1, old_v2)
print(f"OLD: V1/V2 とも同じキーに帰属する ({old_v1!r}) — 兄弟間の移動が「未分類」"
      f"として拾われない (bug再現)")

# NEW: 修正後は inner_a / inner_b それぞれの名前に正しく帰属し、キーが変わる。
new_v1 = owner_of_risky_call(mod._owner_by_line, v1_path)
new_v2 = owner_of_risky_call(mod._owner_by_line, v2_path)
assert new_v1 == "inner_a", new_v1
assert new_v2 == "inner_b", new_v2
assert new_v1 != new_v2
print(f"NEW: V1 は {new_v1!r} / V2 は {new_v2!r} に正しく帰属する (RED CONFIRMED"
      f" — 修正前と同じ allowlist キーを V2 にそのまま流用すると未分類扱いになる)")

# 実際に allowlist で確認する: V1 の呼び出しを "inner_a" として SAFE 登録した
# 状態で V2 を監査すると、V2 の呼び出しは "inner_b" という別キーになるので
# test_every_risky_call_is_classified が RED になる (allowlist を書き換えず
# 兄弟へ移しただけでは通らない、が意図した挙動)。
v1_sites = mod._risky_calls(v1_path)
assert len(v1_sites) == 1 and v1_sites[0][0] == "inner_a", v1_sites
mod.OBSERVATION_SITES[("t113_nested_probe.py", "inner_a", v1_sites[0][1])] = (
    mod.SAFE, "red proof 用のダミー分類 (V1)")

mod.AUTHORITY_MODULES = [pathlib.Path(str(v2_path).replace(
    "t113_nested_probe_v2.py", "t113_nested_probe.py"))]
# _risky_calls は path.name をキーの script として使うので、V2 の内容を
# "t113_nested_probe.py" という同じ script 名で読ませる (V1 の allowlist
# エントリと script 名を揃えて比較するため)。
import shutil
shutil.copy(v2_path, mod.AUTHORITY_MODULES[0])

sites = mod._all_sites()
assert len(sites) == 1, sites
site = sites[0]
assert site[1] == "inner_b", site
try:
    mod.test_every_risky_call_is_classified(site)
except AssertionError:
    print(f"NEW: RED CONFIRMED — {site} (V1 用の allowlist エントリでは通らない)")
else:
    raise SystemExit("NEW-FAIL: V2 の呼び出しが V1 の分類をそのまま引き継いでしまった")
PYEOF
if [ $? -eq 0 ]; then
  ok "P2-2: 旧アルゴリズムは兄弟間の移動を見分けられない (緑) / 修正後は正しく帰属し RED になる"
else
  ng "P2-2: 期待した緑/赤の分岐が再現できなかった"
fi

echo "== C: 本番の木には何も注入していないので、対象テストは引き続き緑 =="
if python3 -m pytest "$REPO_ROOT/tests/test_observation_authority_does_not_fail_open.py" -q \
    >"$WORK/after.log" 2>&1; then
  ok "C: production tree still green (nothing was injected into it)"
else
  ng "C: production tree unexpectedly red after the scratch-copy injections"
  cat "$WORK/after.log"
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
