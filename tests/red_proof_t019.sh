#!/usr/bin/env bash
# 欠陥を戻すと赤くなることの実証 (Codex 10 巡目 P2-1 / P2-2, t019)。
#
# 期待値をテストの中に書き直しただけのテストは、欠陥の留め金になる
# (memory: regression-test-must-prove-red)。だから「直したら緑」ではなく
# **「戻したら赤」** を機械的に確かめる。
#
# 使い方:
#     bash tests/red_proof_t019.sh
#
# t016 は tests/red_proof_unobservable.sh、t017 は
# tests/red_proof_stat_and_direct_reads.sh、t018 は tests/red_proof_t018.sh。
# 混ぜない —— 向こうの分は今も緑でなければならない。
#
# ここで見ているのは 3 つ:
#
#   A. 構造テストの検出器が **属性形式の `open()` を見ていなかった** こと。
#      `Path.open()` を使った直接読み取りを足すと、今の検出器は赤になる。
#      同じ注入に t018 の検出器 (裸の `open()` だけ) を当てると **緑のまま**
#      —— 検出器そのものが穴だった、という対照を出す。
#   B. それが実害を隠していたこと。`lib_model.resolve()` を `p.open()` に
#      戻すと、FIFO の config でモデル解決が **無期限にブロックする**
#      (timeout で 124)。直すと即座に返る。
#   C. 隔離フィクスチャに `lib_task_cards.py` が無いと、
#      `test_review_plan_verdict_binding_e2e.sh` が SANITY で落ちること。
#
# 隔離: 本番の worktree には触らない。`git archive HEAD` で使い捨ての木を作り、
# そこへ注入する (memory: qa-isolated-copy-without-rm-rf)。
# `PYTHONDONTWRITEBYTECODE=1` は必須 —— 同サイズ・同秒の注入は前の .pyc が
# 再利用されて偽の緑になる (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t019.XXXXXX")"
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
        echo "  FATAL: 注入に失敗した (本番コードの形が変わっている)"
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
        -k "$1" tests/ ) > "$WORK/$2" 2>&1
}


# ---------------------------------------------------------------------------
# 注入 1: 属性形式 (`Path.open()`) の直接読み取りを足す
# ---------------------------------------------------------------------------
#
# 「allowlist に無い読み取りが 1 つでもあれば落ちる」はずの検出器が、裸の
# `open()` しか見ていなかったので、この形は素通りしていた。`lib_mux.py` の
# `_config_mode()` —— GUARDED_READS の表にも載っている関数 —— に足す。
# 表に載っている関数でさえ、**ガードを通ったうえで直接読み取りも持つ** 形は
# 機械検出でしか止まらない。
read -r -d '' INJECT_ATTR_OPEN <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = "def _config_mode(config_path: Optional[Path] = None) -> Optional[str]:"
assert old in s, "注入点が見つからない"
s = s.replace(old, '''def _config_mode_direct_read_for_red_proof(path):
    with path.open(encoding="utf-8") as f:
        return f.read()


''' + old, 1)
p.write_text(s)
PY

# ---------------------------------------------------------------------------
# 注入 2: 検出器を t018 の形 (裸の `open()` だけ) に戻す
# ---------------------------------------------------------------------------
read -r -d '' INJECT_OLD_DETECTOR <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
start = s.index('        elif isinstance(func, ast.Attribute) and func.attr == "open":')
end = s.index("        elif isinstance(func, ast.Attribute) and func.attr in _READ_ATTRS:")
assert start < end, "注入点の順序が想定と違う"
p.write_text(s[:start] + s[end:])
PY

# ---------------------------------------------------------------------------
# 注入 3: lib_model.resolve() を素の p.open() に戻す
# ---------------------------------------------------------------------------
read -r -d '' INJECT_LIB_MODEL <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
start = s.index("            # 通常 (PyYAML) 経路も固定パスのガードを通す (t019)。")
end = s.index("        else:\n            # PyYAML 未導入時は簡易 fallback パーサーを使う")
assert start < end, "注入点の順序が想定と違う"
p.write_text(s[:start] + """            with p.open(encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
""" + s[end:])
PY

# ---------------------------------------------------------------------------
# 注入 4: e2e フィクスチャから lib_task_cards.py を外す
# ---------------------------------------------------------------------------
read -r -d '' INJECT_FIXTURE <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """  for f in lint_plan.py lib_verdict.py lib_model.py lib_task_cards.py \\
           wait_for_plan_review.sh review-plan.sh; do"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """  for f in lint_plan.py lib_verdict.py lib_model.py \\
           wait_for_plan_review.sh review-plan.sh; do""")
p.write_text(s)
PY


# ===========================================================================
# A. 検出器の漏れ —— 属性形式の open
# ===========================================================================
echo
echo "================================================================"
echo "== A: 属性形式 (Path.open) の直接読み取りを足すと、構造テストが落ちる"
echo "================================================================"

_purge_pyc
if _inject "scripts/lib_mux.py" "$INJECT_ATTR_OPEN"; then
    echo "  注入した (差分あり)"
    _purge_pyc
    _pytest "no_unguarded_read_remains" "a_new.out"; RC_NEW=$?
    echo "  [欠陥あり / t019 の検出器] pytest rc=$RC_NEW"
    tail -n 4 "$WORK/a_new.out" | sed 's/^/    /'

    # 同じ注入のまま、検出器だけ t018 の形へ戻す。
    cp "$WORK/$GUARD_TEST" "$WORK/$GUARD_TEST.t019"
    if _inject "$GUARD_TEST" "$INJECT_OLD_DETECTOR"; then
        _purge_pyc
        _pytest "no_unguarded_read_remains" "a_old.out"; RC_OLD=$?
        echo "  [欠陥あり / t018 の検出器] pytest rc=$RC_OLD  <- ここが緑なら、検出器が穴"
        tail -n 3 "$WORK/a_old.out" | sed 's/^/    /'
        _restore "$GUARD_TEST"
    else
        RC_OLD=-1
    fi
    cp "$WORK/$GUARD_TEST.t019" "$WORK/$GUARD_TEST"; rm -f "$WORK/$GUARD_TEST.t019"

    _restore "scripts/lib_mux.py"
    _purge_pyc
    _pytest "no_unguarded_read_remains" "a_green.out"; RC_FIXED=$?
    echo "  [欠陥なし / t019 の検出器] pytest rc=$RC_FIXED"
    tail -n 2 "$WORK/a_green.out" | sed 's/^/    /'

    if [ "$RC_NEW" -ne 0 ] && [ "$RC_OLD" -eq 0 ] && [ "$RC_FIXED" -eq 0 ]; then
        echo "  => OK: 新しい検出器だけが捕まえる (新=$RC_NEW / 旧=$RC_OLD / 修正後=$RC_FIXED)"
        PASS=$((PASS + 1))
    else
        echo "  => NG: 対照になっていない (新=$RC_NEW / 旧=$RC_OLD / 修正後=$RC_FIXED)"
        FAIL=$((FAIL + 1))
    fi
else
    FAIL=$((FAIL + 1))
fi


# ===========================================================================
# B. 検出器の漏れが隠していた実害 —— FIFO の config でモデル解決が止まる
# ===========================================================================
echo
echo "================================================================"
echo "== B: lib_model.resolve() を p.open() に戻すと、FIFO の config で止まる"
echo "================================================================"

FIFO="$WORK/fifo-config.yaml"
rm -f "$FIFO"; mkfifo "$FIFO"

_purge_pyc
if _inject "scripts/lib_model.py" "$INJECT_LIB_MODEL"; then
    echo "  注入した (差分あり)"
    _purge_pyc
    timeout 5 python3 "$WORK/scripts/lib_model.py" resolve \
        --config "$FIFO" --skills plan_review >/dev/null 2>&1
    RC_BLOCK=$?
    echo "  [欠陥あり] timeout 5 … rc=$RC_BLOCK  (124 = 5 秒たっても返らない)"

    # 同じ注入のまま、構造テストが何と言うか (t018 の検出器なら緑だった)。
    cp "$WORK/$GUARD_TEST" "$WORK/$GUARD_TEST.t019"
    if _inject "$GUARD_TEST" "$INJECT_OLD_DETECTOR"; then
        _purge_pyc
        _pytest "no_unguarded_read_remains" "b_old.out"; RC_B_OLD=$?
        echo "  [欠陥あり / t018 の検出器] pytest rc=$RC_B_OLD" \
             " <- 無期限ブロックするのに構造チェックは通る"
        _restore "$GUARD_TEST"
    else
        RC_B_OLD=-1
    fi
    cp "$WORK/$GUARD_TEST.t019" "$WORK/$GUARD_TEST"; rm -f "$WORK/$GUARD_TEST.t019"

    _restore "scripts/lib_model.py"
    _purge_pyc
    timeout 5 python3 "$WORK/scripts/lib_model.py" resolve \
        --config "$FIFO" --skills plan_review >/dev/null 2>&1
    RC_OK=$?
    echo "  [欠陥なし] timeout 5 … rc=$RC_OK  (0 = 待たずに断った)"

    if [ "$RC_BLOCK" -eq 124 ] && [ "$RC_B_OLD" -eq 0 ] && [ "$RC_OK" -eq 0 ]; then
        echo "  => OK: 欠陥ありは無期限ブロック / 旧検出器は素通り / 直すと即返る"
        PASS=$((PASS + 1))
    else
        echo "  => NG: 対照になっていない (block=$RC_BLOCK / 旧検出器=$RC_B_OLD / 修正後=$RC_OK)"
        FAIL=$((FAIL + 1))
    fi
else
    FAIL=$((FAIL + 1))
fi
rm -f "$FIFO"


# ===========================================================================
# C. 隔離フィクスチャの依存漏れ
# ===========================================================================
echo
echo "================================================================"
echo "== C: e2e フィクスチャから lib_task_cards.py を外すと SANITY で落ちる"
echo "================================================================"

E2E="scripts/test_review_plan_verdict_binding_e2e.sh"

_purge_pyc
if _inject "$E2E" "$INJECT_FIXTURE"; then
    echo "  注入した (差分あり)"
    ( cd "$WORK" && timeout 300 env BINDING_E2E_CASE_FILTER='SANITY' \
        bash "$E2E" ) > "$WORK/c_red.out" 2>&1
    RC_C_RED=$?
    echo "  [欠陥あり] rc=$RC_C_RED"
    grep -E "ModuleNotFoundError|ABORT|FAIL:" "$WORK/c_red.out" | head -3 | sed 's/^/    /'

    _restore "$E2E"
    ( cd "$WORK" && timeout 300 env BINDING_E2E_CASE_FILTER='SANITY' \
        bash "$E2E" ) > "$WORK/c_green.out" 2>&1
    RC_C_GREEN=$?
    echo "  [欠陥なし] rc=$RC_C_GREEN"
    tail -n 2 "$WORK/c_green.out" | sed 's/^/    /'

    if [ "$RC_C_RED" -ne 0 ] && [ "$RC_C_GREEN" -eq 0 ]; then
        echo "  => OK: 欠陥を戻すと赤、直すと緑"
        PASS=$((PASS + 1))
    else
        echo "  => NG: 対照になっていない (欠陥あり rc=$RC_C_RED / 修正あり rc=$RC_C_GREEN)"
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
