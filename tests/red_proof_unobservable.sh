#!/usr/bin/env bash
# 欠陥を戻すと赤くなることの実証 (Codex 7 巡目 P1 / P2, t016)。
#
# 期待値をテストの中に書き直しただけのテストは、欠陥の留め金になる
# (memory: regression-test-must-prove-red)。だから「直したら緑」ではなく
# **「戻したら赤」** を機械的に確かめる。
#
# 使い方:
#     bash tests/red_proof_unobservable.sh
#
# 3 つの欠陥を 1 つずつ本番コードへ戻し、そのたびに対象のテストを走らせて
# **赤くなること** を確認し、元へ戻して緑になることまで見る。
#
# 隔離: 本番の worktree には触らない。`git archive HEAD` で使い捨ての木を作り、
# そこへ注入する (memory: qa-isolated-copy-without-rm-rf)。
# `PYTHONDONTWRITEBYTECODE=1` は必須 —— 同サイズ・同秒の注入は前の .pyc が
# 再利用されて偽の緑になる (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-unobservable.XXXXXX")"
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
run_case() {
    local name="$1" inject="$2" selector="$3" target="$4"
    echo
    echo "================================================================"
    echo "== $name"
    echo "================================================================"

    cp "$WORK/$target" "$WORK/$target.orig"
    find "$WORK" -name '__pycache__' -type d -exec chmod -R u+rwX {} + 2>/dev/null
    find "$WORK" -name '*.pyc' -delete 2>/dev/null

    if ! python3 - "$WORK/$target" <<< "$inject"; then
        echo "  FATAL: 注入に失敗した (本番コードの形が変わっている)"
        FAIL=$((FAIL + 1))
        cp "$WORK/$target.orig" "$WORK/$target"
        return
    fi

    if ! diff -q "$WORK/$target" "$WORK/$target.orig" >/dev/null; then
        echo "  注入した (差分あり)"
    else
        echo "  FATAL: 注入しても差分が出ていない — 何も戻していない"
        FAIL=$((FAIL + 1))
        return
    fi

    find "$WORK" -name '*.pyc' -delete 2>/dev/null
    ( cd "$WORK" && timeout 300 python3 -m pytest -p no:cacheprovider -q \
        -k "$selector" tests/ ) > "$WORK/red.out" 2>&1
    local rc=$?
    echo "  [欠陥あり] pytest rc=$rc"
    tail -n 4 "$WORK/red.out" | sed 's/^/    /'

    cp "$WORK/$target.orig" "$WORK/$target"
    find "$WORK" -name '*.pyc' -delete 2>/dev/null
    ( cd "$WORK" && timeout 300 python3 -m pytest -p no:cacheprovider -q \
        -k "$selector" tests/ ) > "$WORK/green.out" 2>&1
    local rc2=$?
    echo "  [修正あり] pytest rc=$rc2"
    tail -n 2 "$WORK/green.out" | sed 's/^/    /'

    if [ "$rc" -ne 0 ] && [ "$rc2" -eq 0 ]; then
        echo "  => OK: 欠陥を戻すと赤、直すと緑"
        PASS=$((PASS + 1))
    else
        echo "  => NG: 対照になっていない (欠陥あり rc=$rc / 修正あり rc=$rc2)"
        FAIL=$((FAIL + 1))
    fi
    rm -f "$WORK/$target.orig"
}

# --- 欠陥 1: 走査の失敗を空リストにする (P1) --------------------------------
read -r -d '' INJECT_P1 <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    try:
        st = os.stat(tasks_dir)
    except FileNotFoundError:"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """    if not os.path.isdir(tasks_dir):
        return []
    try:
        st = os.stat(tasks_dir)
    except FileNotFoundError:""")
old2 = """        return [scan_failure_task(tasks_dir, 'listing error', str(e))]"""
assert old2 in s, "注入点 2 が見つからない"
s = s.replace(old2, """        return []""")
p.write_text(s)
PY

# --- 欠陥 2: カードを種類を確かめずに開く (P2) ------------------------------
read -r -d '' INJECT_P2 <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise _NotARegularFile(_describe_file_type(mode))
        os.set_blocking(fd, True)
        f = os.fdopen(fd)
    except BaseException:
        os.close(fd)
        raise
    with f:
        return f.read()"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """    with open(path) as f:
        return f.read()""")
p.write_text(s)
PY

# --- 欠陥 3: 通知を読めなかったことを沈黙として数える (D) -------------------
#
# 注入するのは **`_notification_files()` そのもの** で、読み手 (`_last_activity_mtime`
# と `_newest_notification`) には手を付けない。読み手を 1 つだけ戻す形で試したら
# 緑のままだった —— もう 1 つの読み手が先に terminate を抑制していたからで、
# 「欠陥を戻す」つもりで別の層を壊しただけになっていた
# (memory: red-proof-catches-tests-green-for-the-wrong-reason)。
# 欠陥は「観測の失敗を空に潰すこと」なので、潰していた場所に戻す。
read -r -d '' INJECT_D <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
# メソッド 1 つを丸ごと、修正前の実装に差し替える。境界は def 行どうしで取る
# ので、中身の書き方が変わっても注入点を見失わない (見失ったら assert で
# 止まり、静かに「注入したつもり」にならない)。
start = s.index("    def _notification_files(self)")
end = s.index("    def _note_notifications_observable(self")
assert start < end, "注入点の順序が想定と違う"
original = (
    "    def _notification_files(self):\n"
    "        notif_dir = self.repo_root / \"registry\" / \"notifications\" / self.agent_name\n"
    "        try:\n"
    "            return [f for f in notif_dir.iterdir() if f.is_file()]\n"
    "        except OSError:\n"
    "            return []\n"
    "\n"
)
p.write_text(s[:start] + original + s[end:])
PY

run_case "P1: 走査の失敗を「空の mission」と読む" \
         "$INJECT_P1" \
         "unobservable or scan or could_not_scan or failing_warn_callback" \
         "scripts/lib_task_cards.py"

run_case "P2: カードの種類を確かめずに open() する" \
         "$INJECT_P2" \
         "fifo or regular_file or directory_named_like_a_card or parked" \
         "scripts/lib_task_cards.py"

run_case "D: 通知を読めなかったことを沈黙として数える" \
         "$INJECT_D" \
         "notification_dir" \
         "scripts/watchdog.py"

echo
echo "================================================================"
echo "== 結果: OK=$PASS  NG=$FAIL"
echo "================================================================"
[ "$FAIL" -eq 0 ]
