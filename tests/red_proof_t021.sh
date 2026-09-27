#!/usr/bin/env bash
# 欠陥を戻すと赤くなることの実証 (Codex 12 巡目 指摘 6, t021)。
#
# 期待値をテストの中に書き直しただけのテストは、欠陥の留め金になる
# (memory: regression-test-must-prove-red)。だから「直したら緑」ではなく
# **「戻したら赤」** を機械的に確かめる。
#
# 使い方:
#     bash tests/red_proof_t021.sh
#
# t016 は tests/red_proof_unobservable.sh、t017 は
# tests/red_proof_stat_and_direct_reads.sh、t018 は tests/red_proof_t018.sh、
# t019 は tests/red_proof_t019.sh、t020 は tests/red_proof_t020.sh。
# 混ぜない —— 向こうの分は今も緑でなければならない。
#
# ここで見ているのは **誤検出** である。これまでの巡は「検出漏れ」を潰して
# きたが、今回直したのは逆向きの欠陥 —— **正当な書き込みを読み取りとして
# 報告する**形である。誤検出を放っておくと、人は理由を書かずに allowlist へ
# 足して黙らせる方向に流れ、allowlist が「書き込みの置き場」に変わる。
# そこに本物の読み取りが 1 行紛れても、もう誰も気付けない。
#
# 原因はどちらも同じ一点 —— **import した関数の実体を解決していない**。
# 整数フラグを第 2 引数に取るのは `os.open` だけで、`os.fdopen` はモード
# 文字列を取る。モジュール名だけを覚えていると、この 2 つを区別できない。
#
#   A. `from os import fdopen; fdopen(fd, "w")` を **os.open だと読む**形に
#      戻す。モード文字列 `"w"` が整数フラグとして解析され、読み切れない
#      ので「書き込みだと証明できない」= 読み取り扱いになる。
#   B. `from os import open, O_WRONLY; open(path, O_WRONLY)` を
#      **組み込みの open だと読む**形に戻す (`func.id == "open"` の判定を
#      別名解決より先に置く)。`O_WRONLY` は文字列定数ではないので
#      `_mode_is_write()` が False を返し、これも読み取り扱いになる。
#
# どちらの注入も **片方だけ** を赤にする (もう片方は緑のまま残る)。
# 2 つの誤検出が独立した原因を持つことを、対照で示すためである。
#
# 隔離: 本番の worktree には触らない。`git archive HEAD` で使い捨ての木を作り、
# そこへ注入する (memory: qa-isolated-copy-without-rm-rf)。
# `PYTHONDONTWRITEBYTECODE=1` は必須 —— 同サイズ・同秒の注入は前の .pyc が
# 再利用されて偽の緑になる (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t021.XXXXXX")"
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

# 誤検出の 2 件。ここが「赤になってほしい」対象。
CASE_A="${GUARD_TEST}::test_the_detector_sees_this_read[from-import-os-fdopen-write]"
CASE_B="${GUARD_TEST}::test_the_detector_sees_this_read[from-import-os-open-write-only]"
# 読み取りが読み取りとして残っていること。**どちらの注入でも緑のまま**で
# なければならない —— 誤検出を消す修正が、本物の見落としを作らないための対。
KEEP_A="${GUARD_TEST}::test_the_detector_sees_this_read[from-import-os-fdopen-read]"
KEEP_B="${GUARD_TEST}::test_the_detector_sees_this_read[from-import-os-open-rdonly]"

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

# _pytest <nodeid> <出力ファイル> → rc
_pytest() {
    ( cd "$WORK" && timeout 300 python3 -m pytest -p no:cacheprovider -q \
        "$1" ) > "$WORK/$2" 2>&1
}

# _case <見出し> <注入先> <注入スクリプト> <赤になる nodeid> <緑のまま残る nodeid> <tag>
_case() {
    local title="$1" target="$2" script="$3"
    local red_id="$4" other_id="$5" tag="$6"
    echo
    echo "================================================================"
    echo "== $title"
    echo "================================================================"
    _purge_pyc
    if ! _inject "$target" "$script"; then FAIL=$((FAIL + 1)); return; fi
    echo "  注入した (差分あり): $target"
    _purge_pyc

    _pytest "$red_id" "${tag}_red.out"; local RC_RED=$?
    echo "  [欠陥あり] $(basename "$red_id") rc=$RC_RED"
    grep -E "^(FAILED|E +assert|[0-9]+ (failed|passed))" "$WORK/${tag}_red.out" \
        | head -6 | sed 's/^/    /'

    # もう片方の誤検出は、この注入では **緑のまま** であること。
    _pytest "$other_id" "${tag}_other.out"; local RC_OTHER=$?
    echo "  [欠陥あり] 独立性の確認: $(basename "$other_id") rc=$RC_OTHER"

    # 読み取りが読み取りとして残っていること。
    _pytest "$KEEP_A" "${tag}_keepa.out"; local RC_KA=$?
    _pytest "$KEEP_B" "${tag}_keepb.out"; local RC_KB=$?
    echo "  [欠陥あり] 読み取りは残る: fdopen-read rc=$RC_KA / open-rdonly rc=$RC_KB"

    _restore "$target"
    _purge_pyc
    _pytest "$red_id" "${tag}_green.out"; local RC_GREEN=$?
    echo "  [欠陥なし] $(basename "$red_id") rc=$RC_GREEN"
    tail -n 2 "$WORK/${tag}_green.out" | sed 's/^/    /'

    if [ "$RC_RED" -ne 0 ] && [ "$RC_GREEN" -eq 0 ] \
       && [ "$RC_OTHER" -eq 0 ] && [ "$RC_KA" -eq 0 ] && [ "$RC_KB" -eq 0 ]; then
        echo "  => OK: 欠陥を戻すとこの 1 件だけ赤、直すと緑"
        echo "         (赤=$RC_RED / 緑=$RC_GREEN / 他の誤検出=$RC_OTHER / 読み取り=$RC_KA,$RC_KB)"
        PASS=$((PASS + 1))
    else
        echo "  => NG: 対照になっていない"
        echo "         (赤=$RC_RED / 緑=$RC_GREEN / 他の誤検出=$RC_OTHER / 読み取り=$RC_KA,$RC_KB)"
        FAIL=$((FAIL + 1))
    fi
}


# ---------------------------------------------------------------------------
# 注入 A: import 元の関数名を捨て、os から来たものを全部 os.open と読む
# ---------------------------------------------------------------------------
read -r -d '' INJECT_IGNORE_FUNC_NAME <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = '''            module, orig_name = origin
            if module == "os" and orig_name == "open":
                return "os"
            return "builtin"'''
assert old in s, "注入点が見つからない (A)"
new = '''            module, orig_name = origin
            if module == "os":
                return "os"
            return "builtin"'''
p.write_text(s.replace(old, new, 1))
PY

# ---------------------------------------------------------------------------
# 注入 B: 裸の `open` を、別名解決より先に組み込みだと決める
# ---------------------------------------------------------------------------
read -r -d '' INJECT_BUILTIN_FIRST <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = '''        origin = open_aliases.get(func.id)
        if origin is not None:
            module, orig_name = origin
            if module == "os" and orig_name == "open":
                return "os"
            return "builtin"
        if func.id == "open":
            return "builtin"
        return "ambiguous"'''
assert old in s, "注入点が見つからない (B)"
new = '''        if func.id == "open":
            return "builtin"
        origin = open_aliases.get(func.id)
        if origin is not None:
            module, orig_name = origin
            if module == "os" and orig_name == "open":
                return "os"
            return "builtin"
        return "ambiguous"'''
p.write_text(s.replace(old, new, 1))
PY


_case "A. os から来た関数を全部 os.open と読む → fdopen(fd,\"w\") が読み取り扱い" \
      "$GUARD_TEST" "$INJECT_IGNORE_FUNC_NAME" "$CASE_A" "$CASE_B" "a"

_case "B. 裸の open を別名解決より先に組み込みと決める → from os import open の書き込みが読み取り扱い" \
      "$GUARD_TEST" "$INJECT_BUILTIN_FIRST" "$CASE_B" "$CASE_A" "b"


echo
echo "================================================================"
echo "== 結果: PASS=$PASS FAIL=$FAIL"
echo "================================================================"
if [ "$FAIL" -ne 0 ]; then
    echo "NG: 対照実験になっていないケースがある"
    exit 1
fi
echo "OK: 誤検出の修正は、欠陥を戻すと赤くなることで裏付けられている"
