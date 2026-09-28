#!/usr/bin/env bash
# t110 — PR #244 (t053) Codex review 2 巡目の P2 ×2 が、修正前は緑・修正後は
# 赤になることの実証。
#
# 対象: tests/test_observation_authority_does_not_fail_open.py
#
#   P2-1  import した観測関数を裸の名前で呼ぶと (`from os import stat` の後の
#         `stat(...)`) 修正前の `_risky_calls()` は "listdir" しか裸の名前を
#         知らず、_all_sites() に一件も現れない (=fail-open な呼び出しが
#         allowlist の目に一切触れない)。
#   P2-2  `OBSERVATION_SITES` はキー (script, function, snippet) の有無しか
#         見ないので、既に SAFE と判定された呼び出しと全く同じ文面の 2 つ目を
#         同じ関数に足しても、修正前は何も落ちない (1 つ目の分類を黙って
#         引き継ぐ)。
#
# このガードは純粋な AST 静的検査なので (t053 の red proof と同様)、注入は
# **使い捨てのコピー**にだけ行い、本番の worktree のファイルには一切触れない。
#
# 使い方:  bash tests/red_proof_t110.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t110.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# --- 修正前の _risky_calls() の凍結コピー (P2-1 で直された箇所を再現) -------
# "listdir" だけを裸の Name として特別扱いしていた旧アルゴリズム。
OLD_RISKY_CALLS_PY=$(cat <<'PYEOF'
import ast

RISKY_ATTRS = frozenset({
    "stat", "exists", "read_bytes", "read_text", "readlink", "access",
    "iterdir", "listdir",
})


def old_risky_calls(path):
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        hit = False
        if isinstance(func, ast.Attribute) and func.attr in RISKY_ATTRS:
            hit = True
        elif isinstance(func, ast.Name) and func.id == "listdir":
            hit = True
        if not hit:
            continue
        seg = ast.get_source_segment(src, node) or "<unparsed>"
        found.append(" ".join(seg.split()))
    return found
PYEOF
)
echo "$OLD_RISKY_CALLS_PY" >"$WORK/old_risky_calls.py"

echo "== P2-1: from os import <risky> の後の裸呼び出し =="

cp "$REPO_ROOT/scripts/worktree_gc.py" "$WORK/worktree_gc.py"
cat >>"$WORK/worktree_gc.py" <<'PYEOF'


from os import stat as _t110_stat


def _t110_injected_import_alias_probe(pid):
    """red_proof_t110.sh (P2-1) が注入する、import 別名経由の裸呼び出し。
    実際の欠陥と同じ形: stat が読めなければ「対象は居ない (uid 0 扱い)」に潰す。"""
    try:
        return _t110_stat(f"/proc/{pid}").st_uid
    except OSError:
        return 0
PYEOF

python3 - "$REPO_ROOT" "$WORK/worktree_gc.py" "$WORK/old_risky_calls.py" <<'PYEOF'
import importlib.util
import pathlib
import sys

repo_root, injected, old_mod_path = sys.argv[1], sys.argv[2], sys.argv[3]

spec = importlib.util.spec_from_file_location("t110_old", old_mod_path)
old_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old_mod)

test_path = pathlib.Path(repo_root) / "tests" / "test_observation_authority_does_not_fail_open.py"
spec2 = importlib.util.spec_from_file_location("t110_guard", str(test_path))
new_mod = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(new_mod)

injected_path = pathlib.Path(injected)

# --- OLD: 旧アルゴリズムはこの注入を一件も見つけられない (=fail-open が
#     allowlist の目に触れない) ---
old_hits = [s for s in old_mod.old_risky_calls(injected_path)
            if "_t110_stat" in s]
assert not old_hits, f"旧アルゴリズムが既に検出している (前提が崩れている): {old_hits}"
print("OLD: 旧アルゴリズムは import 別名経由の呼び出しを一件も検出しない (bug再現)")

# --- NEW: 修正後の _risky_calls() はこの注入を検出し、allowlist に無いので
#     test_every_risky_call_is_classified が RED になる (注入先と同じ basename
#     の実ファイルは差し替え、二重カウントしない) ---
new_mod.AUTHORITY_MODULES = [m for m in new_mod.AUTHORITY_MODULES if m.name != "worktree_gc.py"] + [injected_path]
sites = new_mod._all_sites()
injected_sites = [s for s in sites if s[1] == "_t110_injected_import_alias_probe"]
assert injected_sites, f"新アルゴリズムでも検出できていない (修正が効いていない): {sites}"
assert len(injected_sites) == 1, injected_sites
injected_site = injected_sites[0]
assert "_t110_stat" in injected_site[2], injected_site

try:
    new_mod.test_every_risky_call_is_classified(injected_site)
except AssertionError:
    print("NEW: RED CONFIRMED —", injected_site)
else:
    raise SystemExit("NEW-FAIL: injected import-alias call was not caught")

# 既存の (本物の) 箇所は注入と無関係に緑のまま。
real_sites = [s for s in sites if s != injected_site]
assert real_sites, "worktree_gc.py の実在サイトが 1 件も見つからない"
for s in real_sites:
    new_mod.test_every_risky_call_is_classified(s)
print(f"NEW: 既存の {len(real_sites)} 件は注入と無関係に緑のまま")
PYEOF
if [ $? -eq 0 ]; then
  ok "P2-1: 旧アルゴリズムは見逃す (緑) / 修正後は検出して RED になる"
else
  ng "P2-1: 期待した緑/赤の分岐が再現できなかった"
fi

echo "== P2-2: 同じ関数に同じ文面の呼び出しを 2 つ目として足す =="

# lib_* の隔離コピーは唯一の入口 tests/fixture_tree.sh の copy_scripts_libs を
# 通す (直接 cp すると tests/test_fixture_tree_is_the_only_copier.py が赤にする)。
source "$REPO_ROOT/tests/fixture_tree.sh"
copy_scripts_libs "$REPO_ROOT" "$WORK"

python3 - "$WORK/scripts/lib_mux.py" <<'PYEOF'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
src = path.read_text()
needle = "        entries = sorted(os.listdir(directory))\n"
assert src.count(needle) == 1, "対象行が見つからない (lib_mux.py が変わった?)"
# 同じ関数 (reap_stale_pane_records) の中に、全く同じ文面の呼び出しをもう1つ
# 足す (P2-2 が実際に起きる形: コピペで2つ目を足す)。
injected = needle + "        entries = sorted(os.listdir(directory))  # t110 injected duplicate\n"
path.write_text(src.replace(needle, injected, 1))
PYEOF

python3 - "$REPO_ROOT" "$WORK/scripts/lib_mux.py" <<'PYEOF'
import importlib.util
import pathlib
import sys

repo_root, injected = sys.argv[1], sys.argv[2]
test_path = pathlib.Path(repo_root) / "tests" / "test_observation_authority_does_not_fail_open.py"
spec = importlib.util.spec_from_file_location("t110_guard2", str(test_path))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

injected_path = pathlib.Path(injected)
mod.AUTHORITY_MODULES = [m for m in mod.AUTHORITY_MODULES if m.name != "lib_mux.py"] + [injected_path]
sites = mod._all_sites()

dup_key = ("lib_mux.py", "reap_stale_pane_records", "os.listdir(directory)")
count = sites.count(dup_key)
assert count == 2, f"重複が期待通り2回検出されていない: count={count}, sites={[s for s in sites if s[1]=='reap_stale_pane_records']}"
assert dup_key in mod.OBSERVATION_SITES, "比較対象の実サイトが allowlist から消えている"

# --- OLD 相当: test_every_risky_call_is_classified はキーの有無しか見ないので
#     2 件とも (allowlist にある本物のキーと同じ文面なら) 緑のまま —— これが
#     P2-2 の欠陥そのもの (before: 何も落ちない)。
for s in [s for s in sites if s == dup_key]:
    mod.test_every_risky_call_is_classified(s)  # raise しなければ「気付かない」の再現
print("BEFORE (site 単体の classify): 2 件とも気付かれず緑のまま (P2-2 再現)")

# --- NEW: test_duplicate_call_counts_are_audited は件数を独立に見るので、
#     EXPECTED_OCCURRENCES に書かれていない 2 件目の出現で RED になる。
try:
    mod.test_duplicate_call_counts_are_audited()
except AssertionError as e:
    assert "reap_stale_pane_records" in str(e) or dup_key[1] in str(e)
    print("AFTER: RED CONFIRMED — test_duplicate_call_counts_are_audited が重複を検出")
else:
    raise SystemExit("AFTER-FAIL: duplicate occurrence was not caught")
PYEOF
if [ $? -eq 0 ]; then
  ok "P2-2: classify 単体では見逃す (緑) / 新しい件数テストが RED になる"
else
  ng "P2-2: 期待した緑/赤の分岐が再現できなかった"
fi

echo "== QA-a: 変数束縛 / getattr 経由の呼び出し (QA t054 (a)) =="

# 修正直前 (P2-1/P2-2 は入っている状態) の _risky_calls() の凍結コピー ——
# import 別名は解決するが、変数束縛/getattr はまだ解決しない。
OLD_V2_RISKY_CALLS_PY=$(cat <<'PYEOF'
import ast

RISKY_ATTRS = frozenset({
    "stat", "exists", "read_bytes", "read_text", "readlink", "access",
    "iterdir", "listdir",
})


def _imported_risky_names(tree):
    aliases = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name in RISKY_ATTRS:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def old_v2_risky_calls(path):
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    risky_names = _imported_risky_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        hit = False
        if isinstance(func, ast.Attribute) and func.attr in RISKY_ATTRS:
            hit = True
        elif isinstance(func, ast.Name) and func.id in risky_names:
            hit = True
        if not hit:
            continue
        seg = ast.get_source_segment(src, node) or "<unparsed>"
        found.append(" ".join(seg.split()))
    return found
PYEOF
)
echo "$OLD_V2_RISKY_CALLS_PY" >"$WORK/old_v2_risky_calls.py"

cp "$REPO_ROOT/scripts/watchdog.py" "$WORK/watchdog.py"
cat >>"$WORK/watchdog.py" <<'PYEOF'


def _t110_injected_variable_alias_probe(p):
    """red_proof_t110.sh (QA t054 (a)) が注入する、変数束縛経由の裸呼び出し。"""
    bound = p.stat
    try:
        return bound().st_uid
    except OSError:
        return 0


def _t110_injected_getattr_probe(p):
    """同じく getattr(...)() の即時呼び出し形。"""
    try:
        return getattr(p, "stat")().st_uid
    except OSError:
        return 0


def _t110_injected_getattr_bound_probe(p):
    """getattr(...) を変数へ束縛してから呼ぶ形。"""
    bound = getattr(p, "stat")
    try:
        return bound().st_uid
    except OSError:
        return 0
PYEOF

python3 - "$REPO_ROOT" "$WORK/watchdog.py" "$WORK/old_v2_risky_calls.py" <<'PYEOF'
import importlib.util
import pathlib
import sys

repo_root, injected, old_mod_path = sys.argv[1], sys.argv[2], sys.argv[3]

spec = importlib.util.spec_from_file_location("t110_old_v2", old_mod_path)
old_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old_mod)

test_path = pathlib.Path(repo_root) / "tests" / "test_observation_authority_does_not_fail_open.py"
spec2 = importlib.util.spec_from_file_location("t110_guard3", str(test_path))
new_mod = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(new_mod)

injected_path = pathlib.Path(injected)

old_hits = old_mod.old_v2_risky_calls(injected_path)
old_hits = [s for s in old_hits if "bound" in s or "getattr" in s]
assert not old_hits, f"旧アルゴリズム(P2-1/P2-2後)が既に検出している (前提が崩れている): {old_hits}"
print("OLD (P2-1/P2-2 後、(a) 未修正): 変数束縛/getattr の3形とも一件も検出しない (bug再現)")

new_mod.AUTHORITY_MODULES = [m for m in new_mod.AUTHORITY_MODULES if m.name != "watchdog.py"] + [injected_path]
sites = new_mod._all_sites()

expected_fns = [
    "_t110_injected_variable_alias_probe",
    "_t110_injected_getattr_probe",
    "_t110_injected_getattr_bound_probe",
]
for fn in expected_fns:
    matches = [s for s in sites if s[1] == fn]
    assert matches, f"{fn} が新アルゴリズムでも検出できていない (修正が効いていない)"
    assert len(matches) == 1, matches
    site = matches[0]
    try:
        new_mod.test_every_risky_call_is_classified(site)
    except AssertionError:
        print(f"NEW: RED CONFIRMED — {fn}: {site}")
    else:
        raise SystemExit(f"NEW-FAIL: {fn} was not caught")

real_sites = [s for s in sites if s[1] not in expected_fns]
assert real_sites, "watchdog.py の実在サイトが 1 件も見つからない"
for s in real_sites:
    new_mod.test_every_risky_call_is_classified(s)
print(f"NEW: 既存の {len(real_sites)} 件は注入と無関係に緑のまま")
PYEOF
if [ $? -eq 0 ]; then
  ok "QA-a: 変数束縛/getattr の3形とも旧アルゴリズムは見逃す / 修正後は検出して RED になる"
else
  ng "QA-a: 期待した緑/赤の分岐が再現できなかった"
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
