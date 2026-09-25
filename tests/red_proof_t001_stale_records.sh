#!/usr/bin/env bash
# 欠陥を戻すと赤くなることの実証 (t001, backlog #7)。
#
# 期待値をテストの中に書き直しただけのテストは、欠陥の留め金になる
# (memory: regression-test-must-prove-red)。だから「直したら緑」ではなく
# **「戻したら赤」** を機械的に確かめる。
#
# 使い方:
#     bash tests/red_proof_t001_stale_records.sh
#
# 対象: tests/test_stale_pane_record_sweep.py (pytest) と
#       tests/start-sh-spawn-refusal.bats (bats)。
#
# 隔離: 本番の worktree には触らない。使い捨ての木にコピーして、そこへ注入する
# (memory: qa-isolated-copy-without-rm-rf)。`PYTHONDONTWRITEBYTECODE=1` は必須 —
# 同サイズ・同秒の注入は前の .pyc が再利用されて偽の緑になる
# (memory: defect-injection-needs-pyc-purge)。
#
# 本番の mux にも触れない: pytest / bats は HOME を捨てのディレクトリに向けた空の
# 環境で走らせる (conftest が本番 herdr に空 workspace を残す事故があるため)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t001.XXXXXX")"
export PYTHONDONTWRITEBYTECODE=1

cleanup() { chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null; }
trap cleanup EXIT

echo "== 作業用のコピーを作る: $WORK"
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$WORK" || {
    echo "FATAL: git archive に失敗"; exit 1; }
# 未コミットの変更も含めて、いま手元にある姿を試す。
rsync -a --exclude='.git' --exclude='__pycache__' "$REPO_ROOT/scripts/" "$WORK/scripts/" 2>/dev/null
rsync -a --exclude='__pycache__' "$REPO_ROOT/tests/" "$WORK/tests/" 2>/dev/null
mkdir -p "$WORK/home" "$WORK/.git"   # repo_identity_ok() は .git を見る

PASS=0
FAIL=0

# ペイロード一式: pytest は user site を要するので PYTHONPATH で渡す。
USER_SITE="$(python3 -m site --user-site 2>/dev/null)"
run_pytest() {   # <selector>
    ( cd "$WORK" && env -i HOME="$WORK/home" PATH="$PATH" TERM=xterm \
        GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-$HOME/.gitconfig}" \
        PYTHONPATH="$USER_SITE" PYTHONDONTWRITEBYTECODE=1 \
        timeout 300 python3 -m pytest -p no:cacheprovider -q \
        tests/test_stale_pane_record_sweep.py -k "$1" )
}
run_bats() {
    ( cd "$WORK" && env -i HOME="$WORK/home" PATH="$PATH" TERM=xterm \
        timeout 300 bats tests/start-sh-spawn-refusal.bats )
}

# run_case <名前> <pytest|bats> <selector> <対象ファイル> <注入する python>
run_case() {
    local name="$1" kind="$2" selector="$3" target="$4" inject="$5"
    echo
    echo "================================================================"
    echo "== $name"
    echo "================================================================"

    cp "$WORK/$target" "$WORK/$target.orig"
    find "$WORK" -name '*.pyc' -delete 2>/dev/null

    if ! python3 - "$WORK/$target" <<< "$inject"; then
        echo "  FATAL: 注入に失敗した (本番コードの形が変わっている)"
        FAIL=$((FAIL + 1)); cp "$WORK/$target.orig" "$WORK/$target"; rm -f "$WORK/$target.orig"
        return
    fi
    if diff -q "$WORK/$target" "$WORK/$target.orig" >/dev/null; then
        echo "  FATAL: 注入しても差分が出ていない — 何も戻していない"
        FAIL=$((FAIL + 1)); rm -f "$WORK/$target.orig"
        return
    fi
    echo "  注入した (差分あり)"

    local rc rc2
    if [ "$kind" = pytest ]; then run_pytest "$selector" > "$WORK/red.out" 2>&1
    else run_bats > "$WORK/red.out" 2>&1; fi
    rc=$?
    echo "  [欠陥あり] rc=$rc"
    grep -E "^(FAILED|not ok)|passed|failed" "$WORK/red.out" | head -8 | sed 's/^/    /'

    cp "$WORK/$target.orig" "$WORK/$target"
    find "$WORK" -name '*.pyc' -delete 2>/dev/null
    if [ "$kind" = pytest ]; then run_pytest "$selector" > "$WORK/green.out" 2>&1
    else run_bats > "$WORK/green.out" 2>&1; fi
    rc2=$?
    echo "  [修正あり] rc=$rc2"
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

# --- 欠陥 1: pane get の error を全部「消えた」と読む ---------------------------
# herdr は server 不達も {"error": ...} で返す。これが元の _resolve_* の読み方。
read -r -d '' INJ_ANY_ERROR_IS_GONE <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        err = data.get("error")
        if isinstance(err, dict) and err.get("code") == "pane_not_found":
            return PANE_GONE
        return PANE_UNOBSERVED"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        return PANE_GONE"""))
PY
run_case "欠陥 1: server 不達も「pane が消えた」と読む" pytest \
    "server_not_running or other_error or resolution_keeps or existence_is_read" \
    scripts/lib_mux.py "$INJ_ANY_ERROR_IS_GONE"

# --- 欠陥 2: 猶予を外す --------------------------------------------------------
read -r -d '' INJ_NO_GRACE <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if age is None or age < STALE_RECORD_GRACE_SECONDS:
            continue"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, "        pass"))
PY
run_case "欠陥 2: 若い記録 / 年齢が読めない記録も掃除する" pytest \
    "young_record or age_cannot_be_read" scripts/lib_mux.py "$INJ_NO_GRACE"

# --- 欠陥 3: mux に尋ねずに消す -----------------------------------------------
read -r -d '' INJ_NO_ASK <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if backend.record_existence(name, record) != PANE_GONE:
            continue"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, "        pass"))
PY
run_case "欠陥 3: 生きている pane の記録まで消す" pytest \
    "live_pane_is_kept or not_a_definite_absence or unobserved or stop_switch" \
    scripts/lib_mux.py "$INJ_NO_ASK"

# --- 欠陥 4: server 束縛を見ない -----------------------------------------------
read -r -d '' INJ_NO_SERVER_BINDING <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if not server or ("""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, "        if False and ("))
PY
run_case "欠陥 4: 別の server / 世代に対する記録も、いまの server に尋ねる" pytest \
    "not_provably_about_this_server or cannot_be_identified" \
    scripts/lib_mux.py "$INJ_NO_SERVER_BINDING"

# --- 欠陥 5: 記録を drop_pane_record 以外で消す --------------------------------
read -r -d '' INJ_OWN_UNLINK <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        drop_pane_record(name, repo_root=repo_root)
        dropped.append(name)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        pane_record_path(name, repo_root=repo_root).unlink()
        dropped.append(name)"""))
PY
run_case "欠陥 5: 掃除が独自に unlink する (削除の入口が 2 つになる)" pytest \
    "deleted_by_one_function" scripts/lib_mux.py "$INJ_OWN_UNLINK"

# --- 欠陥 6: list() が掃除を兼ねる ---------------------------------------------
# list() は watchdog も呼ぶ。registry/mux/ の書き手が増える。
read -r -d '' INJ_LIST_SWEEPS <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        panes = data.get("result", {}).get("panes", [])
        prefix = _pane_prefix()
        names = [_caller_name(p["label"]) for p in panes"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        reap_stale_pane_records(self)
        panes = data.get("result", {}).get("panes", [])
        prefix = _pane_prefix()
        names = [_caller_name(p["label"]) for p in panes"""))
PY
run_case "欠陥 6: list() が掃除する (watchdog も registry/mux/ の書き手になる)" pytest \
    "nothing_that_lists_also_sweeps" scripts/lib_mux.py "$INJ_LIST_SWEEPS"

# --- 欠陥 7: spawn の拒否理由が終了コードに乗らない ------------------------------
read -r -d '' INJ_EXIT_CODES <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if refusal == SPAWN_OCCUPIED:
            return SPAWN_EXIT_OCCUPIED
        if refusal == SPAWN_UNREADABLE:
            return SPAWN_EXIT_UNREADABLE
        return 1"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, "        return 1"))
PY
run_case "欠陥 7: 拒否理由が終了コードに乗らない (pytest)" pytest \
    "exit_10 or exit_11" scripts/lib_mux.py "$INJ_EXIT_CODES"

# --- 欠陥 8: start.sh が、失敗を全部 already running と言う ----------------------
read -r -d '' INJ_START_SH <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """mux_spawn "$WINDOW_NAME" "$LAUNCH_CMD" "$WORK_DIR" || SPAWN_RC=$?"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """mux_spawn "$WINDOW_NAME" "$LAUNCH_CMD" "$WORK_DIR" || SPAWN_RC=10"""))
PY
run_case "欠陥 8: start.sh が spawn の失敗を全部 already running と言う (bats)" bats \
    "" scripts/start.sh "$INJ_START_SH"

# --- 欠陥 9: dispatcher が掃除を呼ばない ----------------------------------------
read -r -d '' INJ_NO_DISPATCHER_CALL <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """sweep_spawn_grace_markers()
sweep_stale_pane_records()
"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """sweep_spawn_grace_markers()
"""))
PY
run_case "欠陥 9: dispatcher の cycle が掃除を呼ばない" pytest \
    "dispatcher_cycle_calls_the_sweep" scripts/dispatcher.sh "$INJ_NO_DISPATCHER_CALL"

echo
echo "================================================================"
echo "== 結果: OK=$PASS NG=$FAIL"
echo "================================================================"
[ "$FAIL" -eq 0 ]
