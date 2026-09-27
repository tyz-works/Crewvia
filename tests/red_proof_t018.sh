#!/usr/bin/env bash
# 欠陥を戻すと赤くなることの実証 (Codex 9 巡目 P1 / P2, t018)。
#
# 期待値をテストの中に書き直しただけのテストは、欠陥の留め金になる
# (memory: regression-test-must-prove-red)。だから「直したら緑」ではなく
# **「戻したら赤」** を機械的に確かめる。
#
# 使い方:
#     bash tests/red_proof_t018.sh
#
# t016 の分は tests/red_proof_unobservable.sh、t017 の分は
# tests/red_proof_stat_and_direct_reads.sh にある。混ぜない —— 向こうの分は
# 今も緑でなければならないので、注入すると何を見ているのか読めなくなる。
#
# 隔離: 本番の worktree には触らない。`git archive HEAD` で使い捨ての木を作り、
# そこへ注入する (memory: qa-isolated-copy-without-rm-rf)。
# `PYTHONDONTWRITEBYTECODE=1` は必須 —— 同サイズ・同秒の注入は前の .pyc が
# 再利用されて偽の緑になる (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t018.XXXXXX")"
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

# run_case <名前> <注入する python スクリプト> <pytest の -k 式> <対象ファイル>
#          [緑のままでなければならない -k 式]
#
# 第 5 引数を渡すと、**欠陥がある状態でもそちらは緑のまま**であることまで
# 確かめる。「この欠陥を捕まえているのは新しいテストだけで、既存の表は
# 見逃していた」を示すのに使う。
run_case() {
    local name="$1" inject="$2" selector="$3" target="$4" still_green="${5:-}"
    echo
    echo "================================================================"
    echo "== $name"
    echo "================================================================"

    cp "$WORK/$target" "$WORK/$target.orig"
    find "$WORK" -name '*.pyc' -delete 2>/dev/null

    if ! python3 - "$WORK/$target" <<< "$inject"; then
        echo "  FATAL: 注入に失敗した (本番コードの形が変わっている)"
        FAIL=$((FAIL + 1))
        cp "$WORK/$target.orig" "$WORK/$target"
        rm -f "$WORK/$target.orig"
        return
    fi

    if diff -q "$WORK/$target" "$WORK/$target.orig" >/dev/null; then
        echo "  FATAL: 注入しても差分が出ていない — 何も戻していない"
        FAIL=$((FAIL + 1))
        rm -f "$WORK/$target.orig"
        return
    fi
    echo "  注入した (差分あり)"

    find "$WORK" -name '*.pyc' -delete 2>/dev/null
    ( cd "$WORK" && timeout 300 python3 -m pytest -p no:cacheprovider -q \
        -k "$selector" tests/ ) > "$WORK/red.out" 2>&1
    local rc=$?
    echo "  [欠陥あり] pytest rc=$rc"
    tail -n 4 "$WORK/red.out" | sed 's/^/    /'

    local rc_green=0
    if [ -n "$still_green" ]; then
        ( cd "$WORK" && timeout 300 python3 -m pytest -p no:cacheprovider -q \
            -k "$still_green" tests/ ) > "$WORK/stillgreen.out" 2>&1
        rc_green=$?
        echo "  [欠陥あり / 既存の表だけ] pytest rc=$rc_green"
        tail -n 2 "$WORK/stillgreen.out" | sed 's/^/    /'
    fi

    cp "$WORK/$target.orig" "$WORK/$target"
    find "$WORK" -name '*.pyc' -delete 2>/dev/null
    ( cd "$WORK" && timeout 300 python3 -m pytest -p no:cacheprovider -q \
        -k "$selector" tests/ ) > "$WORK/green.out" 2>&1
    local rc2=$?
    echo "  [修正あり] pytest rc=$rc2"
    tail -n 2 "$WORK/green.out" | sed 's/^/    /'

    if [ "$rc" -ne 0 ] && [ "$rc2" -eq 0 ] && [ "$rc_green" -eq 0 ]; then
        echo "  => OK: 欠陥を戻すと赤、直すと緑"
        PASS=$((PASS + 1))
    else
        echo "  => NG: 対照になっていない (欠陥あり rc=$rc / 修正あり rc=$rc2" \
             "/ 緑のままのはず rc=$rc_green)"
        FAIL=$((FAIL + 1))
    fi
    rm -f "$WORK/$target.orig"
}


# --- 欠陥 P1: 読めない state.yaml を「空」で返す ----------------------------
#
# t017 の形にそのまま戻す。`dispatch()` の側には触らない —— 確かめたいのは
# 「失敗を空と同じ形で返すと、`if not active_missions: shutdown_idle_workers()`
# に落ちる」という 1 点だけである。
read -r -d '' INJECT_P1_STATE <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    text = read_queue_text(STATE_FILE, 'state file')
    if is_missing(text):
        return {}          # 本当に無い = active mission ゼロ
    if is_unreadable(text):
        return text        # 観測できなかった —— 呼び出し側が「空」と読めない形
    return parse_yaml(text)"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """    if not STATE_FILE.exists():
        return {}
    text = read_queue_text(STATE_FILE, 'state file')
    if is_unreadable(text):
        return {}
    return parse_yaml(text)""")
p.write_text(s)
PY

# --- 欠陥 P1': 読めない workers.yaml を「0 人」で返す -----------------------
read -r -d '' INJECT_P1_WORKERS <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    text = read_queue_text(WORKERS_FILE, 'workers file')
    if is_missing(text):
        return {}          # 本当に無い = Worker 0 人
    if is_unreadable(text):
        return text
    data = parse_yaml(text)"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """    if not WORKERS_FILE.exists():
        return {}
    text = read_queue_text(WORKERS_FILE, 'workers file')
    if is_unreadable(text):
        return {}
    data = parse_yaml(text)""")
p.write_text(s)
PY

# --- 欠陥 P2-a: wrapper がデコード失敗を捕まえない --------------------------
#
# `except UnicodeError` と backstop の両方を外す = t017 の形。
read -r -d '' INJECT_P2_DECODE <<'PY'
import sys, pathlib, re
p = pathlib.Path(sys.argv[1]); s = p.read_text()
start = s.index("    except UnicodeError as e:\n        # `UnicodeDecodeError` は `OSError` ではなく")
end = s.index("def _unreadable(warn, path, reason, err_no, hint=''):")
assert start < end, "注入点の順序が想定と違う"
p.write_text(s[:start] + "\n\n" + s[end:])
PY

# --- 欠陥 P2-b: plan.sh の wrapper がデコード失敗を捕まえない ---------------
read -r -d '' INJECT_P2_DECODE_PLAN <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
start = s.index("    except UnicodeError as e:\n        # `UnicodeDecodeError` は `OSError` ではなく")
end = s.index("def read_queue_file(path, what):")
assert start < end, "注入点の順序が想定と違う"
p.write_text(s[:start] + "\n\n" + s[end:])
PY

# --- 欠陥 P2-c: 表に無い関数に、新しい直接読み取りが入る --------------------
#
# ここが t018 で足したテストの正体。t017 の表 (GUARDED_READS) は「載せた関数」
# しか見ないので、**載っていない関数**に直接読み取りが増えても緑のままだった
# —— Codex 9 巡目 P2 で名指しされた 4 件はどれもその形である。
# `shutdown_idle_workers()` はどの表にも載っていない。
read -r -d '' INJECT_P2_UNLISTED <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        assignment_file = ASSIGNMENTS_DIR / agent_name
        is_idle = not assignment_file.exists()
        if is_idle:
            if in_spawn_grace(target):"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """        assignment_file = ASSIGNMENTS_DIR / agent_name
        is_idle = (not assignment_file.exists()
                   or not assignment_file.read_text().strip())
        if is_idle:
            if in_spawn_grace(target):""")
p.write_text(s)
PY

# --- 欠陥 P2-d: 退役がカードを直接読む -------------------------------------
read -r -d '' INJECT_P2_RETIREMENT <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    text = read_regular_text_or_unreadable(path)
    if is_unreadable(text):
        return UNKNOWN_STARTED_AT"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return UNKNOWN_STARTED_AT""")
p.write_text(s)
PY


run_case "P1: 読めない state.yaml を「空」で返すと、idle Worker の退役が走る" \
         "$INJECT_P1_STATE" \
         "unreadable_state_does_not_retire" \
         "scripts/dispatcher.sh"

run_case "P1': 読めない workers.yaml を「0 人」で返すと、全員が Taskvia から消える" \
         "$INJECT_P1_WORKERS" \
         "unreadable_registry or workers_file_concludes_nothing" \
         "scripts/dispatcher.sh"

run_case "P2-a: wrapper がデコード失敗を捕まえない" \
         "$INJECT_P2_DECODE" \
         "test_the_wrapper_never_raises or non_utf8_file_is_isolated" \
         "scripts/lib_task_cards.py"

run_case "P2-b: plan.sh の wrapper がデコード失敗を捕まえない" \
         "$INJECT_P2_DECODE_PLAN" \
         "plan_sh_try_read_queue_file_never_raises" \
         "scripts/plan.sh"

# 第 5 引数 —— t017 の表だけなら緑のままであることを示す。「新しいテストが
# 増やしたのはここだ」を、言葉ではなく対照で出す。
run_case "P2-c: 表に無い関数に、新しい直接読み取りが入る" \
         "$INJECT_P2_UNLISTED" \
         "no_unguarded_read_remains" \
         "scripts/dispatcher.sh" \
         "test_the_read_goes_through_a_guard"

run_case "P2-d: 退役がカードを直接読む" \
         "$INJECT_P2_RETIREMENT" \
         "no_unguarded_read_remains or read_task_started_at" \
         "scripts/lib_retirement.py"


echo
echo "================================================================"
echo "== 合計: PASS=$PASS FAIL=$FAIL"
echo "================================================================"
[ "$FAIL" -eq 0 ]
