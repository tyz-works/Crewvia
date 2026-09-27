#!/usr/bin/env bash
# 除外を足してもガードが生きていることの実証 (t022)。
#
# t022 は `scripts/test_registry_lock.sh` の静的ガード (workers.yaml への生
# write_text 迂回の近接ヒューリスティック) に、誤検出された 2 ファイルを
# 除外として足した。除外は「検出器を黙らせる」操作なので、**黙らせすぎて
# いないこと** を機械的に示さないと、本物の迂回がそのまま通る
# (memory: detector-exemption-needs-proof / fail-closed-guard-can-recreate-the-defect)。
#
# 使い方:
#     bash tests/red_proof_t022.sh
#
# 確かめること:
#   baseline — いまの木では静的検査が緑 (= 除外が効いている)
#   case A   — tests/ に置いた合成の迂回は**今も赤**
#              (tests/ を丸ごと外してはいない。除外はファイル単位のまま)
#   case B   — scripts/ に置いた合成の迂回は**今も赤** (検出器そのものが生きている)
#   case C   — 足した 2 行の除外を取り除くと、その 2 ファイルが**赤に戻る**
#              (= 静的検査は今もこの 2 ファイルを走査していて、緑なのは
#               「除外したから」であって「検査が壊れたから」ではない。
#               memory: red-proof-catches-tests-green-for-the-wrong-reason)
#
# 隔離: 本番の worktree には触らない。`git archive HEAD` で使い捨ての木を作る
# (memory: qa-isolated-copy-without-rm-rf)。
#
# ★ $WORK を mktemp -d に置くのは必須 —— このガードは `.claude/worktrees` 配下を
#   丸ごと走査対象から外すので、**worktree の中で走らせると常に偽の緑**になる
#   (memory: static-guard-skipped-in-worktrees)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t022.XXXXXX")"

cleanup() { chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null; }
trap cleanup EXIT

echo "== 作業用のコピーを作る: $WORK"
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$WORK" || {
    echo "FATAL: git archive に失敗 (commit してから実行すること)"; exit 1; }
# 未コミットの変更も含めて、いま手元にある姿を試す。
rsync -a --exclude='.git' --exclude='__pycache__' \
      "$REPO_ROOT/scripts/" "$WORK/scripts/" 2>/dev/null
rsync -a --exclude='__pycache__' "$REPO_ROOT/tests/" "$WORK/tests/" 2>/dev/null

GUARD="$WORK/scripts/test_registry_lock.sh"

# 合成の迂回に使う 2 つのトークン。**変数に分けて持つのは意図的**で、
# このスクリプト自身がガードの近接ヒューリスティックに引っかからないように
# するため —— ガードが探す書き込み呼び出しの綴りを、registry ファイル名に
# 言及している行の近くに literal で置いてはいけない (この注意書き自体も
# 含めて)。取り違えたら下の baseline が赤くなるので、間違いは黙って通らない。
YAML_TOKEN="workers.yaml"
WRITE_TOKEN="write_text"

PASS=0
FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng()   { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# 静的検査のセクションだけを取り出す。suite 全体の exit code は section 2/3 の
# 結果も混ざるので使わない (memory: exit-code-through-a-pipe-is-not-the-suite-s)。
# 取り出しが空なら suite が手前で落ちている = FATAL 扱いにする。
static_section() {
    ( cd "$WORK" && bash scripts/test_registry_lock.sh 2>&1 ) \
        | sed -n '/-- 1\. static check/,/-- 2\./p'
}

# plant_bypass <相対パス> — 合成の迂回を 1 つ置く。
# 「workers.yaml への言及」と「生の write_text 呼び出し」を隣接行に並べる、
# ガードがまさに捕まえるべき形。
plant_bypass() {
    local rel="$1"
    mkdir -p "$(dirname "$WORK/$rel")"
    {
        printf 'import pathlib\n'
        printf '# 合成の迂回 (red proof 用): registry/%s を lib_registry を通さず書く\n' "$YAML_TOKEN"
        printf 'target = pathlib.Path("registry") / "%s"\n' "$YAML_TOKEN"
        printf 'target.%s("workers: []\\n")\n' "$WRITE_TOKEN"
    } > "$WORK/$rel"
}

expect_clean() {
    local label="$1" out
    out="$(static_section)"
    if [[ -z "$out" ]]; then
        ng "$label: 静的検査のセクションが取れなかった (suite が手前で落ちている)"
    elif grep -q "FAIL:" <<< "$out"; then
        ng "$label: 緑であるべきなのに検出された"
        sed 's/^/      | /' <<< "$out"
    elif grep -q "PASS: no raw write_text bypass" <<< "$out"; then
        ok "$label"
    else
        ng "$label: PASS 行が見当たらない (検査の出力の形が変わった?)"
        sed 's/^/      | /' <<< "$out"
    fi
}

# expect_flagged <ラベル> <期待するパスの部分文字列>...
expect_flagged() {
    local label="$1"; shift
    local out missing=0 needle
    out="$(static_section)"
    if [[ -z "$out" ]]; then
        ng "$label: 静的検査のセクションが取れなかった (suite が手前で落ちている)"
        return
    fi
    for needle in "$@"; do
        grep -q "bypass detected: .*$needle" <<< "$out" || {
            echo "      期待した検出が無い: $needle"; missing=1; }
    done
    if (( missing )); then
        ng "$label: 迂回を置いたのに赤くならなかった (ガードが死んでいる)"
        sed 's/^/      | /' <<< "$out"
    else
        ok "$label"
    fi
}

echo
echo "================================================================"
echo "== baseline: いまの木では静的検査が緑"
echo "================================================================"
expect_clean "baseline: 除外が効いて静的検査は緑 (この red proof 自身も検出されない)"

echo
echo "================================================================"
echo "== case A: tests/ に置いた合成の迂回は今も赤"
echo "================================================================"
plant_bypass "tests/_synthetic_bypass_t022.py"
expect_flagged "case A: tests/ 配下の迂回は検出される (tests/ を丸ごと除外してはいない)" \
               "tests/_synthetic_bypass_t022.py"
rm -f "$WORK/tests/_synthetic_bypass_t022.py"

echo
echo "================================================================"
echo "== case B: scripts/ に置いた合成の迂回は今も赤"
echo "================================================================"
plant_bypass "scripts/_synthetic_bypass_t022.py"
expect_flagged "case B: scripts/ 配下の迂回は検出される (検出器そのものが生きている)" \
               "scripts/_synthetic_bypass_t022.py"
rm -f "$WORK/scripts/_synthetic_bypass_t022.py"

echo
echo "================================================================"
echo "== case C: 足した除外を取り除くと、その 2 ファイルが赤に戻る"
echo "================================================================"
cp "$GUARD" "$GUARD.orig"
if python3 - "$GUARD" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
removed = 0
for line in ('    root / "tests" / "test_guarded_reads_on_direct_paths.py",\n',
             '    root / "tests" / "red_proof_t018.sh",\n'):
    assert line in s, f"除外の行が見つからない: {line!r}"
    s = s.replace(line, "", 1)
    removed += 1
assert removed == 2
p.write_text(s)
PY
then
    expect_flagged "case C: 除外を外すと 2 ファイルとも赤に戻る (検査は今も走査している)" \
                   "tests/test_guarded_reads_on_direct_paths.py" \
                   "tests/red_proof_t018.sh"
else
    ng "case C: 除外の行を取り除けなかった (ガードの形が変わっている)"
fi
cp "$GUARD.orig" "$GUARD"
rm -f "$GUARD.orig"

echo
echo "================================================================"
echo "red_proof_t022.sh: PASS=$PASS FAIL=$FAIL"
echo "================================================================"
#
# ★ 限界: この除外はファイル単位なので、**除外した 2 ファイルの中に後から
#   本物の迂回を書いても検出されない**。ファイル単位に留めているのは、
#   tests/ 全体を外すより穴が小さいからであって、穴が無いからではない
#   (元のコメントの「迂回を隠す余地を残さないよう、ファイル単位の明示列挙に
#   留める」と同じ判断)。除外を足すときは、その都度このスクリプトの case C に
#   対象を足して「走査対象ではある」ことを示すこと。

(( FAIL == 0 )) || exit 1
