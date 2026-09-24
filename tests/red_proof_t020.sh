#!/usr/bin/env bash
# 欠陥を戻すと赤くなることの実証 (Codex 11 巡目 P2-1 / P2-2 / P2-3, t020)。
#
# 期待値をテストの中に書き直しただけのテストは、欠陥の留め金になる
# (memory: regression-test-must-prove-red)。だから「直したら緑」ではなく
# **「戻したら赤」** を機械的に確かめる。
#
# 使い方:
#     bash tests/red_proof_t020.sh
#
# t016 は tests/red_proof_unobservable.sh、t017 は
# tests/red_proof_stat_and_direct_reads.sh、t018 は tests/red_proof_t018.sh、
# t019 は tests/red_proof_t019.sh。混ぜない —— 向こうの分は今も緑でなければ
# ならない。
#
# ここで見ているのは 4 つ。どれも **検出器そのものの穴** である ——
# 検出器に漏れがあると、構造テストは全部緑のまま未ガードの queue 読み取りを
# 通す (t019 の教訓の続き)。
#
#   A. `os.open()` のフラグ判定 (P2-1)。`O_CREAT` / `O_APPEND` / `O_RDWR` が
#      あれば書き込みだ、という t019 の見方に戻すと、
#      `os.open(path, os.O_RDONLY | os.O_CREAT)` —— 既存の FIFO で無期限に
#      止まる形 —— が検出から外れる。
#   B. import の別名解決 (P2-2)。レシーバの型が分からないものを全部 `Path` と
#      仮定する t019 の見方に戻すと、`fs.open("queue/state.yaml")` の
#      **ファイル名がモードとして解釈され**、その中の `a` が書き込み除外を
#      発火させて検出から外れる。
#   C. 間接読み取りの別名追跡 (P2-3)。`from io import open as read_file` を
#      追わない形に戻すと、`read_file(path).read()` が検出から外れる。
#   D. subprocess 経由の読み取り (P2-3)。**本番モジュールに
#      `subprocess.check_output(["cat", ...])` を実際に足し**、t019 までの
#      テストが全部緑のままであることと、t020 で足した表が赤になることを
#      対にして出す。これが「規約に委ねていた」の実体である。
#
# 隔離: 本番の worktree には触らない。`git archive HEAD` で使い捨ての木を作り、
# そこへ注入する (memory: qa-isolated-copy-without-rm-rf)。
# `PYTHONDONTWRITEBYTECODE=1` は必須 —— 同サイズ・同秒の注入は前の .pyc が
# 再利用されて偽の緑になる (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t020.XXXXXX")"
export PYTHONDONTWRITEBYTECODE=1

cleanup() { chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null; }
trap cleanup EXIT

echo "== 作業用のコピーを作る: $WORK"
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$WORK" || {
    echo "FATAL: git archive に失敗 (commit してから実行すること)"; exit 1; }
# 未コミットの変更も含めて、いま手元にある姿を試す。
rsync -a --exclude='.git' --exclude='__pycache__' \
      "$REPO_ROOT/scripts/" "$WORK/scripts/" 2>/dev/null
rsync -a --exclude='__pycache__' "$REPO_ROOT/tests/" "$WORK/tests/" 2>/dev/null

PASS=0
FAIL=0
GUARD_TEST="tests/test_queue_reads_go_through_the_guard.py"

_purge_pyc() { find "$WORK" -name '*.pyc' -delete 2>/dev/null; }

# _inject <相対パス> <python スクリプト>  — 差分が出なければ非 0。
_inject() {
    local target="$1" script="$2"
    cp "$WORK/$target" "$WORK/$target.orig"
    if ! python3 - "$WORK/$target" <<< "$script"; then
        echo "  FATAL: 注入に失敗した (直したコードの形が変わっている)"
        cp "$WORK/$target.orig" "$WORK/$target"; rm -f "$WORK/$target.orig"
        return 1
    fi
    if diff -q "$WORK/$target" "$WORK/$target.orig" >/dev/null; then
        echo "  FATAL: 注入しても差分が出ていない — 何も戻していない"
        rm -f "$WORK/$target.orig"
        return 1
    fi
    return 0
}

_restore() {
    local target="$1"
    [ -f "$WORK/$target.orig" ] && cp "$WORK/$target.orig" "$WORK/$target"
    rm -f "$WORK/$target.orig"
}

_pytest() {  # _pytest <-k 式> <出力ファイル> → rc
    ( cd "$WORK" && timeout 300 python3 -m pytest -p no:cacheprovider -q \
        -k "$1" "$GUARD_TEST" ) > "$WORK/$2" 2>&1
}

# _case <見出し> <注入先> <注入スクリプト> <-k 式>
#   欠陥あり → 赤、欠陥なし → 緑 を対で出す。
_case() {
    local title="$1" target="$2" script="$3" selector="$4"
    local tag="${5:-case}"
    echo
    echo "================================================================"
    echo "== $title"
    echo "================================================================"
    _purge_pyc
    if ! _inject "$target" "$script"; then FAIL=$((FAIL + 1)); return; fi
    echo "  注入した (差分あり): $target"
    _purge_pyc
    _pytest "$selector" "${tag}_red.out"; local RC_RED=$?
    echo "  [欠陥あり] pytest -k '$selector' rc=$RC_RED"
    grep -E "^(FAILED|[0-9]+ (failed|passed))" "$WORK/${tag}_red.out" \
        | head -8 | sed 's/^/    /'

    _restore "$target"
    _purge_pyc
    _pytest "$selector" "${tag}_green.out"; local RC_GREEN=$?
    echo "  [欠陥なし] pytest -k '$selector' rc=$RC_GREEN"
    tail -n 2 "$WORK/${tag}_green.out" | sed 's/^/    /'

    if [ "$RC_RED" -ne 0 ] && [ "$RC_GREEN" -eq 0 ]; then
        echo "  => OK: 欠陥を戻すと赤、直すと緑 (赤=$RC_RED / 緑=$RC_GREEN)"
        PASS=$((PASS + 1))
    else
        echo "  => NG: 対照になっていない (赤=$RC_RED / 緑=$RC_GREEN)"
        FAIL=$((FAIL + 1))
    fi
}


# ---------------------------------------------------------------------------
# 注入 A: os.open のフラグ判定を t019 の形に戻す
# ---------------------------------------------------------------------------
read -r -d '' INJECT_OS_FLAGS <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = '''    flags = _arg(node, 1, "flags")
    if flags is None:
        return False
    names = _flag_names(flags)
    if names is None:
        return False
    if names & _OS_ACCESS_READABLE:
        return False
    return _OS_ACCESS_WRITE_ONLY in names'''
assert old in s, "注入点が見つからない"
new = '''    _t019_write_flags = frozenset({
        "O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_EXCL", "O_TRUNC"})
    flags = {sub.attr for sub in ast.walk(node)
             if isinstance(sub, ast.Attribute)}
    return bool(flags & _t019_write_flags)'''
p.write_text(s.replace(old, new, 1))
PY

# ---------------------------------------------------------------------------
# 注入 B: 別名解決をやめ、型の分からないレシーバを Path と仮定する
# ---------------------------------------------------------------------------
#
# t019 はレシーバの **名前** を直接見ていた (別名 import は追えない) うえ、
# 知らない名前を全部 Path とみなして第 1 引数をモードとして読んでいた。
read -r -d '' INJECT_ALIAS <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()

old_aliases = '''    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for entry in node.names:
                if entry.asname:
                    aliases[entry.asname] = entry.name
                else:
                    root = entry.name.split(".")[0]
                    aliases[root] = root
    return aliases'''
assert old_aliases in s, "注入点 (_module_aliases) が見つからない"
# t019 相当: 別名は追わず、素の名前だけが自分自身に解決する。
s = s.replace(old_aliases, '''    return {n: n for n in
            ("os", "io", "codecs", "builtins", "subprocess")}''', 1)

old_tail = '''    first = _arg(node, 0, "mode")
    if isinstance(first, ast.Constant) and _looks_like_mode(first.value):
        return _mode_is_write(first)
    second = _arg(node, 1, "mode")
    if isinstance(second, ast.Constant) and _looks_like_mode(second.value):
        return _mode_is_write(second)
    return False'''
assert old_tail in s, "注入点 (_is_write_open の ambiguous 側) が見つからない"
# t019 相当: モードとして通る形かを見ずに、第 1 引数をモードとして読む。
s = s.replace(old_tail, '''    modes = list(node.args[:1])
    modes += [kw.value for kw in node.keywords if kw.arg == "mode"]
    for m in modes:
        if isinstance(m, ast.Constant) and isinstance(m.value, str):
            if any(c in m.value for c in "wax"):
                return True
    return False''', 1)
p.write_text(s)
PY

# ---------------------------------------------------------------------------
# 注入 C: 別名 import された「開く関数」を追わない
# ---------------------------------------------------------------------------
read -r -d '' INJECT_FROM_IMPORT <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = '''    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in modules:
            for entry in node.names:
                if entry.name in names:
                    out[entry.asname or entry.name] = node.module
    return out'''
assert old in s, "注入点 (_from_import_aliases) が見つからない"
p.write_text(s.replace(old, "    return {}", 1))
PY

# ---------------------------------------------------------------------------
# 注入 D: 本番モジュールに subprocess 経由の queue 読み取りを足す
# ---------------------------------------------------------------------------
read -r -d '' INJECT_CAT <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = "def load_active_tasks("
assert old in s, "注入点 (watchdog.load_active_tasks) が見つからない"
s = s.replace(old, '''def _read_state_through_cat_for_red_proof(path):
    import subprocess
    return subprocess.check_output(["cat", str(path)], text=True)


''' + old, 1)
p.write_text(s)
PY


# ===========================================================================
# A / B / C —— 検出器の穴を 1 つずつ戻す
# ===========================================================================

_case "A: os.open のフラグ判定を戻すと、読める open が検出から外れる (P2-1)" \
      "$GUARD_TEST" "$INJECT_OS_FLAGS" \
      "detector_sees_this_read or allowlist_has_no_dead_entries" "a"

_case "B: 別名解決をやめると、ファイル名がモードとして読まれる (P2-2)" \
      "$GUARD_TEST" "$INJECT_ALIAS" \
      "detector_sees_this_read" "b"

_case "C: from-import の別名を追わないと、間接読み取りが外れる (P2-3)" \
      "$GUARD_TEST" "$INJECT_FROM_IMPORT" \
      "detector_sees_this_read or detector_sees_this_subprocess" "c"


# ===========================================================================
# D. subprocess 経由の読み取り —— 「規約に委ねていた」の実体
# ===========================================================================
#
# ここだけは **本番モジュールに実際の欠陥を足す**。t019 までのテストが全部
# 緑のまま未ガードの queue 読み取りが入る、というのが Codex 11 巡目 P2-3 の
# 指摘そのものだからである。
echo
echo "================================================================"
echo "== D: 本番に cat 経由の読み取りを足す —— t019 のテストは緑、t020 は赤"
echo "================================================================"

# t019 までに存在していたテストだけを選ぶ。
T019_SELECTOR="no_unguarded_read_remains or the_read_goes_through_a_guard \
or the_allowlist_has_no_dead_entries or the_direct_read_did_not_come_back \
or the_one_deliberate_exception"
T020_SELECTOR="no_unaudited_subprocess_remains"

_purge_pyc
if _inject "scripts/watchdog.py" "$INJECT_CAT"; then
    echo "  注入した (差分あり): scripts/watchdog.py"
    echo "    + subprocess.check_output([\"cat\", str(path)])"
    _purge_pyc
    _pytest "$T019_SELECTOR" "d_t019.out"; RC_T019=$?
    echo "  [t019 までのテスト] rc=$RC_T019  <- 0 なら、規約に委ねていた穴"
    tail -n 2 "$WORK/d_t019.out" | sed 's/^/    /'

    _purge_pyc
    _pytest "$T020_SELECTOR" "d_t020.out"; RC_T020=$?
    echo "  [t020 で足した表]   rc=$RC_T020  <- 非 0 なら、機械で止まる"
    grep -E "^(E +watchdog|FAILED|[0-9]+ (failed|passed))" "$WORK/d_t020.out" \
        | head -5 | sed 's/^/    /'

    _restore "scripts/watchdog.py"
    _purge_pyc
    _pytest "$T020_SELECTOR" "d_green.out"; RC_D_GREEN=$?
    echo "  [欠陥なし]          rc=$RC_D_GREEN"
    tail -n 2 "$WORK/d_green.out" | sed 's/^/    /'

    if [ "$RC_T019" -eq 0 ] && [ "$RC_T020" -ne 0 ] && [ "$RC_D_GREEN" -eq 0 ]; then
        echo "  => OK: t019 は素通り / t020 が捕まえる / 取り除くと緑"
        PASS=$((PASS + 1))
    else
        echo "  => NG: 対照になっていない" \
             "(t019=$RC_T019 / t020=$RC_T020 / 修正後=$RC_D_GREEN)"
        FAIL=$((FAIL + 1))
    fi
else
    FAIL=$((FAIL + 1))
fi


echo
echo "================================================================"
echo "== 合計: PASS=$PASS FAIL=$FAIL"
echo "================================================================"
[ "$FAIL" -eq 0 ]
