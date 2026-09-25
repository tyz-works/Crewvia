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
        ${TEST_FILES:-tests/test_stale_pane_record_sweep.py} -k "$1" )
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
    [ -n "${BEFORE_FIXED:-}" ] && eval "$BEFORE_FIXED"
    if [ "$kind" = pytest ]; then run_pytest "$selector" > "$WORK/green.out" 2>&1
    else run_bats > "$WORK/green.out" 2>&1; fi
    rc2=$?
    echo "  [修正あり] rc=$rc2"
    tail -n 2 "$WORK/green.out" | sed 's/^/    /'

    if [ "${EXPECT:-red}" = green ]; then
        # 「塞げない」と明記した範囲: 欠陥を入れても赤にならないことを、そのまま記録する。
        if [ "$rc" -eq 0 ] && [ "$rc2" -eq 0 ]; then
            echo "  => 既知の限界: この型は検出できない (緑のまま)。テストの docstring と表の理由欄に明記済み"
            PASS=$((PASS + 1))
        else
            echo "  => 想定外: 限界のはずが赤になった — 限界の記述を更新すること (rc=$rc / rc=$rc2)"
            FAIL=$((FAIL + 1))
        fi
    elif [ "$rc" -ne 0 ] && [ "$rc2" -eq 0 ]; then
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
        if isinstance(err, dict) and err.get("code") == "no_answer":
            return PANE_UNANSWERED
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
old = """        if seen != PANE_GONE:
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
old = """        if drop_pane_record(name, repo_root=repo_root, expect=record):
            dropped.append(name)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        pane_record_path(name, repo_root=repo_root).unlink()
        dropped.append(name)"""))
PY
run_case "欠陥 5: 掃除が独自に unlink する (削除の入口が 2 つになる)" pytest \
    "deletions_next_to_the_records or unlinked_by_exactly_one" \
    scripts/lib_mux.py "$INJ_OWN_UNLINK"

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

# ============================================================================
# PR #215 追加コミット (t022): Kai P1×2 / QA F1・F2
# ============================================================================

# --- 欠陥 10 (Kai P1-1): 比較と unlink が書き手とロックを共有しない -----------------
# 元の欠陥そのもの: 掃除が「読んで比べる」を書き手のロックの外で行い、そのあと
# 無条件に unlink する。比較の後に spawn が書いた記録を消す。
read -r -d '' INJ_COMPARE_OUTSIDE_LOCK <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if drop_pane_record(name, repo_root=repo_root, expect=record):
            dropped.append(name)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        if read_pane_record(name, repo_root=repo_root) != record:
            continue
        if drop_pane_record(name, repo_root=repo_root):
            dropped.append(name)"""))
PY
run_case "欠陥 10a: 最終比較がロックの外 (比較の後に書かれた spawn の記録を消す)" pytest \
    "rewritten_after_the_final_comparison" scripts/lib_mux.py "$INJ_COMPARE_OUTSIDE_LOCK"

# 書き手 (write_pane_record / drop_pane_record) がロックに並ばない。
read -r -d '' INJ_NO_FLOCK <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """                break"""))
PY
run_case "欠陥 10b: ロックが実際には何も排除しない (flock を外す)" pytest \
    "rewritten_after_the_final_comparison or holds_the_lock or waits_for_the_lock" \
    scripts/lib_mux.py "$INJ_NO_FLOCK"

# --- 欠陥 11 (QA F1): label 不一致を「pane が無い」と読む ---------------------------
read -r -d '' INJ_LABEL_MISMATCH_IS_GONE <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            recorded_tab = str(record.get("tab_id") or "")
            if recorded_tab and str(pane.get("tab_id") or "") == recorded_tab:
                return PANE_RENAMED
            return PANE_UNOBSERVED"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """            return PANE_GONE"""))
PY
run_case "欠陥 11: rename された生存 pane の記録を掃除する / 解決経路が消す" pytest \
    "another_label or renamed_live_pane or existence_is_read" \
    scripts/lib_mux.py "$INJ_LABEL_MISMATCH_IS_GONE"

# --- 欠陥 12 (QA F2 / I5): dispatcher.sh の埋め込み python が記録を独自に消す ----------
read -r -d '' INJ_I5_DISPATCHER_UNLINK <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            marker.unlink(missing_ok=True)
            log(f"[spawn_grace] swept stale marker for vanished window {target!r}")"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """            marker.unlink(missing_ok=True)
            for _rec in STATE_JSON_DIR.glob('*-worker.json'):
                os.unlink(_rec)
            log(f"[spawn_grace] swept stale marker for vanished window {target!r}")"""))
PY
run_case "欠陥 12 (I5): dispatcher.sh の python が registry/mux/*-worker.json を os.unlink" pytest \
    "deletions_next_to_the_records" scripts/dispatcher.sh "$INJ_I5_DISPATCHER_UNLINK"

# --- 欠陥 13 (QA F2 / I4): shutil.move で記録を消す -----------------------------------
read -r -d '' INJ_I4_SHUTIL_MOVE <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if drop_pane_record(name, repo_root=repo_root, expect=record):
            dropped.append(name)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        shutil.move(str(pane_record_path(name, repo_root=repo_root)), os.devnull)
        if drop_pane_record(name, repo_root=repo_root, expect=record):
            dropped.append(name)"""))
PY
run_case "欠陥 13 (I4): lib_mux が shutil.move で記録を動かして消す" pytest \
    "deletions_next_to_the_records or unlinked_by_exactly_one" \
    scripts/lib_mux.py "$INJ_I4_SHUTIL_MOVE"

# --- 欠陥 14 (QA F2 / I3): 別関数が os.unlink で記録を消す -----------------------------
read -r -d '' INJ_I3_OTHER_FUNCTION <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """def read_pane_record(name: str, *, repo_root=None) -> Optional[dict]:"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """def _sneaky_drop(name, repo_root=None):
    os.unlink(pane_record_path(name, repo_root=repo_root))


""" + old))
PY
run_case "欠陥 14 (I3): 別の関数が os.unlink で記録を消す" pytest \
    "deletions_next_to_the_records or unlinked_by_exactly_one" \
    scripts/lib_mux.py "$INJ_I3_OTHER_FUNCTION"

# --- 欠陥 15 (QA F2 / I1): 2 つ目のメソッドが pane_get を直に読む ----------------------
read -r -d '' INJ_I1_SECOND_READER <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    def record_existence(self, name: str, record: dict,
                         timeout: Optional[float] = None) -> str:
        return self._pane_existence("""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """    def _second_reader(self, pane_id):
        return _herdr_run("pane_get", [pane_id], timeout=5)

""" + old))
PY
run_case "欠陥 15 (I1): 2 つ目のメソッドが pane get を直に読む" pytest \
    "pane_get_answers_are_read" scripts/lib_mux.py "$INJ_I1_SECOND_READER"

# --- 既知の限界 (I2): 連結した名前は静的に見えない ------------------------------------
# AST では塞げない。緑のままであることを記録し、限界を隠さない
# (memory: detector-exemption-needs-proof / 閉じない指摘は閉じないと明言する)。
read -r -d '' INJ_I2_CONCATENATED <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    def record_existence(self, name: str, record: dict,
                         timeout: Optional[float] = None) -> str:
        return self._pane_existence("""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """    def _second_reader(self, pane_id):
        op = "pane" + "_get"
        return _herdr_run(op, [pane_id], timeout=5)

""" + old))
PY
EXPECT=green run_case "既知の限界 (I2): op = \"pane\" + \"_get\" の連結は検出できない" pytest \
    "pane_get_answers_are_read" scripts/lib_mux.py "$INJ_I2_CONCATENATED"

# --- 欠陥 16 (Kai P1-2): bats が実 checkout で start.sh を走らせ、設定を消す -----------
# 開発者の `.claude/settings.local.json` が「すでにある」状態を用意し、元の欠陥
# (実 checkout で走らせ、teardown で無条件に rm) を戻す。**ここで消えるのは作業用の
# コピーの中のファイルで、本番の worktree ではない。**
mkdir -p "$WORK/.claude"
echo '{"keep": "me"}' > "$WORK/.claude/settings.local.json"
read -r -d '' INJ_BATS_REAL_CHECKOUT <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old1 = '    REPO_ROOT="$SANDBOX"\n'
old2 = "    # どのテストも、開発者の checkout を 1 バイトも変えていない。\n"
assert old1 in s and old2 in s, "注入点が見つからない"
s = s.replace(old1, '    REPO_ROOT="$REAL_ROOT"\n')
s = s.replace(old2, '    rm -f "${REPO_ROOT}/.claude/settings.local.json"\n' + old2)
p.write_text(s)
PY
BEFORE_FIXED='[ -e "$WORK/.claude/settings.local.json" ] \
    && echo "  (欠陥版の実行で settings.local.json は変わった/消えた — これが Kai P1-2 の実害)" \
    || echo "  (欠陥版の実行で、事前にあった settings.local.json は消えた — これが Kai P1-2 の実害)"; \
    echo "{\"keep\": \"me\"}" > "$WORK/.claude/settings.local.json"' \
run_case "欠陥 16: bats が実 checkout で走り、teardown が settings.local.json を無条件に消す" bats \
    "" tests/start-sh-spawn-refusal.bats "$INJ_BATS_REAL_CHECKOUT"
unset BEFORE_FIXED
if [ "$(cat "$WORK/.claude/settings.local.json" 2>/dev/null)" = '{"keep": "me"}' ]; then
    echo "  (修正版の実行後も、事前にあった settings.local.json は無傷で残っている)"
else
    echo "  NG: 修正版の実行が settings.local.json を変えた"; FAIL=$((FAIL + 1))
fi

# ============================================================================
# PR #215 2 巡目 (t027): Kai-codex の P2 2 件
#   欠陥 17 = 存在確認を記録の世代に束ねない (P2-2) / 欠陥 18 = 記録経由の解決が無い (P2-1)
# 対象テストは tests/test_renamed_pane_and_bound_existence.py (+ 一部は sweep 側)。
# ============================================================================
NEW_TESTS="tests/test_renamed_pane_and_bound_existence.py tests/test_stale_pane_record_sweep.py"

# --- 欠陥 17a (Kai P2-2): 問い合わせを流す接続の世代を、記録の世代と突き合わせない ------
# 検証と問い合わせが別の呼び出し (= 別の server) でありうる、という元の穴。
read -r -d '' INJ_NO_GENERATION_CHECK <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if live_endpoint != endpoint or live_generation != generation:
            print(f"[mux:herdr] WARNING: not asking whether pane {pane_id!r} \""""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        if False:
            print(f"[mux:herdr] WARNING: not asking whether pane {pane_id!r} \""""))
PY
TEST_FILES="$NEW_TESTS" run_case "欠陥 17a: 問い合わせの接続の世代を記録と突き合わせない (再起動後の「無い」で記録が消える)" pytest \
    "replaced_server or restart_during_the_sweep" scripts/lib_mux.py "$INJ_NO_GENERATION_CHECK"

# --- 欠陥 17b (Kai P2-2): 識別子を sweep 全体で 1 回だけ取って使い回す ------------------
read -r -d '' INJ_IDENTITY_CACHED <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old1 = """        server = backend.server_identity()
        if not server or ("""
old2 = """                                 if budget is None else budget)
    for fname in entries:"""
assert old1 in s and old2 in s, "注入点が見つからない"
s = s.replace(old1, """        if server is None:
            server = backend.server_identity() or False
        if not server or (""")
s = s.replace(old2, """                                 if budget is None else budget)
    server = None
    for fname in entries:""")
p.write_text(s)
PY
TEST_FILES="$NEW_TESTS" run_case "欠陥 17b: 識別子を全記録で使い回す (窓を広げる)" pytest \
    "identity_per_record" scripts/lib_mux.py "$INJ_IDENTITY_CACHED"

# --- 欠陥 17c (Kai P2-2): 存在確認が束縛のない CLI (`herdr pane get`) に戻る ---------------
read -r -d '' INJ_UNBOUND_CLI <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        data = _herdr_pane_get_bound(
            pane_id, (recorded.get("endpoint"), recorded.get("generation")),
            timeout=timeout)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        data = _herdr_run("pane_get", [pane_id], timeout=5)"""))
PY
run_case "欠陥 17c: 存在確認が、束縛の無い CLI 接続に戻る (構造テスト)" pytest \
    "pane_get_answers_are_read" scripts/lib_mux.py "$INJ_UNBOUND_CLI"

# --- 欠陥 18a (Kai P2-1): 記録経由の解決が無い (= t022 の状態) --------------------------
# 記録は残るが、label で見つからない rename 済み pane に誰も届かない。
read -r -d '' INJ_NO_RECORD_RESOLUTION <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        return self._label_lookup(name) or renamed"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        return self._label_lookup(name)"""))
PY
TEST_FILES="$NEW_TESTS" run_case "欠陥 18a: rename された pane を記録の id で解決しない (capture/send/pid/kill/--force が届かない)" pytest \
    "reaches or orphaned_by_resolution" scripts/lib_mux.py "$INJ_NO_RECORD_RESOLUTION"

# --- 欠陥 18b (Kai P2-1): rename を「同定できない」に戻す (t022 の PANE_UNOBSERVED) ---------
read -r -d '' INJ_RENAMED_UNOBSERVED <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            if recorded_tab and str(pane.get("tab_id") or "") == recorded_tab:
                return PANE_RENAMED"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """            if False:
                return PANE_RENAMED"""))
PY
TEST_FILES="$NEW_TESTS" run_case "欠陥 18b: rename された pane を同定しない (t022 の UNOBSERVED のまま)" pytest \
    "reaches or renamed or another_label" scripts/lib_mux.py "$INJ_RENAMED_UNOBSERVED"

# --- 欠陥 19 (認可を緩める 1): 記録が label より優先される --------------------------------
# label が別の pane を指しているのに記録の pane を返すと、名前 → pane の意味が変わる。
read -r -d '' INJ_RECORD_BEATS_LABEL <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        return self._label_lookup(name) or renamed"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        return renamed or self._label_lookup(name)"""))
PY
TEST_FILES="$NEW_TESTS" run_case "欠陥 19: 記録が label より優先される (別の pane を指す label を無視する)" pytest \
    "label_that_finds_a_pane or handle_mismatch" scripts/lib_mux.py "$INJ_RECORD_BEATS_LABEL"

# --- 欠陥 20 (認可を緩める 2): tab を確かめずに「同じ pane」とみなす ------------------------
read -r -d '' INJ_NO_TAB_CHECK <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            if recorded_tab and str(pane.get("tab_id") or "") == recorded_tab:"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """            if True:"""))
PY
run_case "欠陥 20: tab を確かめずに label 違いの pane を同じ pane とみなす" pytest \
    "another_label or exactly_one_place" scripts/lib_mux.py "$INJ_NO_TAB_CHECK"

# --- 欠陥 21 / 22 (認可のゲートそのもの): 「再確認テスト」が空振りでないことの実証 ---------
# 記録経由で pane に届くようになっても、kill の認可 (`pane_record_status`) が別 checkout の
# 記録・handle 食い違いを拒否し続けること。ゲートを緩めて、再確認テストが赤になるかを見る。
read -r -d '' INJ_GATE_IGNORES_CHECKOUT <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if str(written_by) != mine:"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        if False:"""))
PY
TEST_FILES="$NEW_TESTS" run_case "欠陥 21: kill の認可が、他の checkout が書いた記録を受け入れる" pytest \
    "another_checkouts_record" scripts/lib_mux.py "$INJ_GATE_IGNORES_CHECKOUT"

read -r -d '' INJ_GATE_IGNORES_HANDLE <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    if str(handle) != recorded:"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """    if False:"""))
PY
TEST_FILES="$NEW_TESTS" run_case "欠陥 22: kill の認可が、記録と別の pane (handle 食い違い) を受け入れる" pytest \
    "handle_mismatch" scripts/lib_mux.py "$INJ_GATE_IGNORES_HANDLE"

# ============================================================================
# PR #215 3 巡目 (t030, 最終): Kai-codex の P2 2 件
#   欠陥 23 = 判定した記録ではなく「いまの記録」を消す (P2-1)
#   欠陥 24 = 掃除に時間の上限が無い (P2-2)
# 対象テストは tests/test_sweep_budget_and_conditional_drop.py。
# ============================================================================
T30_TESTS="tests/test_sweep_budget_and_conditional_drop.py"

# --- 欠陥 23a (Kai P2-1): 解決経路が素の drop で消す (= 3 巡目の指摘そのもの) -------------
read -r -d '' INJ_RESOLVE_UNCONDITIONAL <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """                self._forget_record(name, cached)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """                drop_pane_record(name)"""))
PY
TEST_FILES="$T30_TESTS" run_case "欠陥 23a: 解決経路が、判定した記録ではなく いまの記録 を消す (置き換えを消す)" pytest \
    "replacement_written_while_it_was_asking or every_path_that_drops or use_forget_record" \
    scripts/lib_mux.py "$INJ_RESOLVE_UNCONDITIONAL"

# --- 欠陥 23b: herdr の kill が、閉じたあとに素の drop で消す ---------------------------
read -r -d '' INJ_HERDR_KILL_UNCONDITIONAL <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        data = _herdr_run("tab_close", [tab_id], timeout=10)
        if data is None:
            return False

        self._forget_record(name, judged)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        data = _herdr_run("tab_close", [tab_id], timeout=10)
        if data is None:
            return False

        drop_pane_record(name)"""))
PY
TEST_FILES="$T30_TESTS" run_case "欠陥 23b: herdr の kill が、閉じたあと素の drop で新しい pane の記録を消す" pytest \
    "herdr_kill_does_not_drop or every_path_that_drops or use_forget_record" \
    scripts/lib_mux.py "$INJ_HERDR_KILL_UNCONDITIONAL"

# --- 欠陥 23c: tmux の kill が、殺したあと素の drop で消す -----------------------------
read -r -d '' INJ_TMUX_KILL_UNCONDITIONAL <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            if r.returncode != 0:
                return False
            self._forget_record(name, judged)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """            if r.returncode != 0:
                return False
            drop_pane_record(name)"""))
PY
TEST_FILES="$T30_TESTS" run_case "欠陥 23c: tmux の kill が、殺したあと素の drop で新しい window の記録を消す" pytest \
    "tmux_kill_does_not_drop or every_path_that_drops or use_forget_record" \
    scripts/lib_mux.py "$INJ_TMUX_KILL_UNCONDITIONAL"

# --- 欠陥 23d: 入口 (_forget_record) が判定した記録を無視する -------------------------------
read -r -d '' INJ_FORGET_IGNORES_JUDGED <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if judged is not None:
            drop_pane_record(name, expect=judged)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        if judged is not None:
            drop_pane_record(name)"""))
PY
TEST_FILES="$T30_TESTS" run_case "欠陥 23d: _forget_record が expect= を渡さない (全経路が無条件になる)" pytest \
    "replacement_written or every_path_that_drops" \
    scripts/lib_mux.py "$INJ_FORGET_IGNORES_JUDGED"

# --- 欠陥 24a (Kai P2-2): 時間予算が無い ---------------------------------------------------
read -r -d '' INJ_NO_BUDGET <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """    deadline = _sweep_clock() + (STALE_SWEEP_BUDGET_SECONDS
                                 if budget is None else budget)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """    deadline = float("inf")"""))
PY
TEST_FILES="$T30_TESTS" run_case "欠陥 24a: sweep に全体の時間予算が無い (遅い server で 13 件ぶん待つ)" pytest \
    "slow_server or several_cycles or inside_the_budget or same_bounded_sweep or budget_stops" \
    scripts/lib_mux.py "$INJ_NO_BUDGET"

# --- 欠陥 24b (Kai P2-2): 「答えなかった」で打ち切らない -----------------------------------
read -r -d '' INJ_NO_STOP_ON_SILENCE <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """        if seen == PANE_UNANSWERED:"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """        if False:"""))
PY
TEST_FILES="$T30_TESTS" run_case "欠陥 24b: 応答しない server でも次の記録を問い合わせ続ける" pytest \
    "never_answers or stops_answering" \
    scripts/lib_mux.py "$INJ_NO_STOP_ON_SILENCE"

# --- 欠陥 24c (Kai P2-2): 問い合わせを「残り」に切り詰めない --------------------------------
read -r -d '' INJ_NO_CLAMP <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            timeout=min(STALE_SWEEP_QUERY_TIMEOUT_SECONDS, remaining))"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """            timeout=STALE_SWEEP_QUERY_TIMEOUT_SECONDS)"""))
PY
TEST_FILES="$T30_TESTS" run_case "欠陥 24c: 最後の問い合わせが、残りを越えて待つ" pytest \
    "slow_server" scripts/lib_mux.py "$INJ_NO_CLAMP"

# --- 欠陥 24d (Kai P2-2): dispatcher / start.sh の入口 (Mux) だけ予算を外す ---------------
# 掃除本体は予算を持っていても、入口が無効化すれば dispatcher と start.sh は待たされる。
read -r -d '' INJ_FACADE_NO_BUDGET <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            return reap_stale_pane_records(self._backend)"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """            return reap_stale_pane_records(self._backend, budget=float("inf"))"""))
PY
TEST_FILES="$T30_TESTS" run_case "欠陥 24d: dispatcher / start.sh の入口 (Mux) が予算を無効にする" pytest \
    "dispatcher_cycle_stays_inside or start_sh_waits" \
    scripts/lib_mux.py "$INJ_FACADE_NO_BUDGET"

# --- 欠陥 24e (Kai P2-2): 本物の socket で、応答しない server を timeout なしで待つ ------------
read -r -d '' INJ_NO_SOCKET_TIMEOUT <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = """            sock.settimeout(5 if timeout is None else max(timeout, 0.001))"""
assert old in s, "注入点が見つからない"
p.write_text(s.replace(old, """            sock.settimeout(5)"""))
PY
TEST_FILES="$T30_TESTS" run_case "欠陥 24e: 束縛付き問い合わせが、渡された timeout を無視する (実 socket)" pytest \
    "real_silent_server" scripts/lib_mux.py "$INJ_NO_SOCKET_TIMEOUT"

echo
echo "================================================================"
echo "== 結果: OK=$PASS NG=$FAIL"
echo "================================================================"
[ "$FAIL" -eq 0 ]
