#!/usr/bin/env bash
# 欠陥を戻すと赤くなることの実証 (Codex 8 巡目 P1 / P2, t017)。
#
# 期待値をテストの中に書き直しただけのテストは、欠陥の留め金になる
# (memory: regression-test-must-prove-red)。だから「直したら緑」ではなく
# **「戻したら赤」** を機械的に確かめる。
#
# 使い方:
#     bash tests/red_proof_stat_and_direct_reads.sh
#
# t016 の分は tests/red_proof_unobservable.sh にある (こちらは別ファイル ——
# 向こうの 3 件は今も緑でなければならないので、混ぜない)。
#
# 隔離: 本番の worktree には触らない。`git archive HEAD` で使い捨ての木を作り、
# そこへ注入する (memory: qa-isolated-copy-without-rm-rf)。
# `PYTHONDONTWRITEBYTECODE=1` は必須 —— 同サイズ・同秒の注入は前の .pyc が
# 再利用されて偽の緑になる (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-stat-direct.XXXXXX")"
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


# --- 欠陥 P1-a: stat の失敗を「活動なし」と読む ------------------------------
#
# 戻すのは **区別そのもの** だけ。戻り値の形 (tuple) は残すので、呼び出し側は
# 一切触らない —— 触ると「欠陥を戻す」ではなく別の層を壊すことになる。
read -r -d '' INJECT_P1A <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            try:
                mtime = p.stat().st_mtime
            except FileNotFoundError:
                continue                    # 本当に無い
            except OSError as e:
                unobservable = True         # 読めなかった — 「無い」ではない
                self._note_signal_observable(
                    False, f"{self.agent_name}: cannot stat {p}: {e} — "
                           f"treating this as unobservable, not as silence "
                           f"(terminate is suppressed until it can be read)")
                continue"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue""")
p.write_text(s)
PY

# --- 欠陥 P1-b: 列挙後の通知の stat 失敗を捨てる -----------------------------
read -r -d '' INJECT_P1B <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            except FileNotFoundError:
                # 列挙と stat の間に消えた。通知は読まれたあと消されるので、
                # これは競合ではなく通常の並び —— 本当に無い。
                continue
            except OSError:
                # 列挙はできたのに stat できない (列挙後に権限が変わった等)。
                # 「通知は無い」に潰すと抑制が外れて terminate 側に落ちるので、
                # 一覧そのものを取れなかったときと同じ答えに揃える。
                return time.time(), "(unobservable)\""""
assert old in s, "注入点が見つからない"
s = s.replace(old, """            except OSError:
                continue""")
p.write_text(s)
PY

# --- 欠陥 P1-c: watchdog が state.yaml を種類も見ずに開く --------------------
read -r -d '' INJECT_P1C <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
start = s.index("    # 種類を確かめてから読む (Codex 8 巡目 P2)")
end = s.index("    state = parse_yaml(state_text)")
assert start < end, "注入点の順序が想定と違う"
p.write_text(s[:start] + "    state_text = state_file.read_text()\n" + s[end:])
PY

# --- 欠陥 P2-a: plan.sh がカード / mission.yaml を種類も見ずに開く -----------
#
# `try_read_queue_file()` の中身だけを戻す。呼び出し側 (load_task /
# load_mission / 表示系) はそのままなので、**確かめたいのは種類の判定だけ**に
# なる。
read -r -d '' INJECT_P2A <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    try:
        return _TASK_CARDS.read_regular_text(path, newline=newline), None
    except _TASK_CARDS.NotARegularFile as e:"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """    try:
        with open(path, newline=newline) as f:
            return f.read(), None
    except _TASK_CARDS.NotARegularFile as e:""")
p.write_text(s)
PY

# --- 欠陥 P2-b: dispatcher が割り当て済みカードを直接読む -------------------
read -r -d '' INJECT_P2B <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = "                            meta, _ = read_task_card(task_file, task_id)"
assert old in s, "注入点が見つからない"
s = s.replace(
    old,
    "                            meta, _ = parse_frontmatter(task_file.read_text())")
p.write_text(s)
PY

# --- 欠陥 P2-c: 常駐デーモン 3 者が queue / registry を直接読む --------------
#
# 戻すのは **共有の入口 1 つ** だけ。dispatcher / verifier-dispatcher /
# taskvia-sync はどれもここを通るので、「3 者を守っているのはこの 1 箇所だ」と
# いうことまで含めて赤で示せる (呼び出し側には一切触らない)。
read -r -d '' INJECT_P2C <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    try:
        return read_regular_text(path)
    except NotARegularFile as e:"""
assert old in s, "注入点が見つからない"
s = s.replace(old, """    try:
        with open(path) as f:
            return f.read()
    except NotARegularFile as e:""")
p.write_text(s)
PY

# --- 欠陥 P2-d: 書き戻す前にカードの種類を確かめない -------------------------
read -r -d '' INJECT_P2D <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = "    text = read_regular_text(task_path)"
assert old in s, "注入点が見つからない"
s = s.replace(old, "    text = task_path.read_text()")
p.write_text(s)
PY


run_case "P1-a: stat の失敗を「活動なし」と読む" \
         "$INJECT_P1A" \
         "unsearchable or unstattable_after_listing" \
         "scripts/watchdog.py"

run_case "P1-b: 列挙後の通知の stat 失敗を捨てる" \
         "$INJECT_P1B" \
         "unstattable_notification_does_not_look_like" \
         "scripts/watchdog.py"

run_case "P1-c: watchdog が state.yaml を種類も見ずに開く" \
         "$INJECT_P1C" \
         "watchdog_load_active_tasks" \
         "scripts/watchdog.py"

run_case "P2-a: plan.sh がカード / mission.yaml を種類も見ずに開く" \
         "$INJECT_P2A" \
         "fifo_card_does_not_block_a_change_command or fifo_mission_yaml or directory_named_like_a_card_is_refused" \
         "scripts/plan.sh"

run_case "P2-b: dispatcher が割り当て済みカードを直接読む" \
         "$INJECT_P2B" \
         "publish_agents_does_not_block" \
         "scripts/dispatcher.sh"

run_case "P2-c: 常駐デーモン 3 者が queue / registry を直接読む" \
         "$INJECT_P2C" \
         "dispatcher_load_state or dispatcher_load_workers or mission_done_on_an_unreadable or verifier_load_state" \
         "scripts/lib_task_cards.py"

run_case "P2-d: 書き戻す前にカードの種類を確かめない" \
         "$INJECT_P2D" \
         "verifier_does_not_block_writing_back" \
         "scripts/verifier-dispatcher.sh"

echo
echo "================================================================"
echo "== 結果: OK=$PASS  NG=$FAIL"
echo "================================================================"
[ "$FAIL" -eq 0 ]
