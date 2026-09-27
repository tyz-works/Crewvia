#!/usr/bin/env bash
# 裏の shell / monitor を idle と見なさない仕組み (B1 / #27) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_b1_background_work.sh
#
#   baseline — いまの木では tests/test_background_work_is_not_idle.py と test_watchdog_idle.py が緑
#   A  dispatcher: 裏の job があっても idle-with-task を通知する (B1 の分岐を外す)  → 赤
#   B  dispatcher: 裏の job が無い pane (idle_process) も「裏の job あり」と読む    → 赤
#   C  dispatcher: 分類が例外のとき「裏の job あり」に倒す (黙る側)                → 赤
#   D  dispatcher: pane pid が引けないとき「裏の job あり」に倒す (黙る側)          → 赤
#   E  dispatcher: blocked (承認・質問待ち) も裏の job があれば黙らせる             → 赤
#   F  dispatcher: 裏の job 中に grace を測り直さない (終わった瞬間に通知する)      → 赤
#   G  分類: 裏の job を `executing` と読まない (lib 1 か所の欠陥が両側に効く)      → 赤
#   H  watchdog: 裏の job があると max も効かなくなる                              → 赤
#   I  watchdog: 裏の job があっても hard idle を terminate にする                 → 赤
#   J  dispatcher に /proc を読む分類のコピーが生える                              → 赤
#   L  テスト: pane tree の teardown が group ごとでなく root だけになる (t049 P3)   → 赤
#   M  分類: 読めない pid を静かに「子孫なし」へ倒す (族A監査)                    → 赤
#   O  分類 (t065 P2 2巡目): シェルを job と同定しない                            → 赤
#   P  分類 (t065 族C監査): 同定できない永続プロセスを infra 側に倒してしまう      → 赤
#
# t049 の時刻ベースの判定 (grace_seconds の枠内で始まった裏 job を assignment mtime
# で拾う仕組み。旧 case K/N) は t065 でプロセスの同定に置き換えられ、丸ごと撤去した
# (Codex review 2巡目: 時刻を代理指標にする限り、plan.sh pull の後で初めて起動する
# MCP サーバー / ブラウザを本物の job と区別できず、偽陰性 [本当に止まっているのに
# 気付けない] が残るため)。この赤の実証は消えたコードを再現できないので K/N は削除、
# 代わりに O/P で同定ベースの欠陥を実証する。
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# pytest は FakeMux / 隔離した queue・registry と本物の sh の親子木で動き、
# 本物の herdr / tmux / 本番 queue には届かない (Mux は差し替え、pane pid はテストが立てた sh)。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避するだけ)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-b1.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

TREE=""
fresh_copy() {
    N=$((N + 1)); TREE="$WORK/tree$N"; mkdir -p "$TREE"
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
          --exclude='queue' --exclude='logs' "$REPO_ROOT/" "$TREE/"
}

# inject <file> <old> <new> — 複製した <file> の old を new に置換 (ちょうど 1 か所。無ければ FATAL)
inject() {
    FILE="$1" OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    print(f"count={s.count(old)}", file=sys.stderr)
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

# 呼び出し元の AGENT_NAME / CREWVIA_* を引き継がない (テストは自前の env を組む)
run_py() {  # run_py <pytest の引数...>
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider "$@" 2>&1 )
}
B1_TESTS=(tests/test_background_work_is_not_idle.py)
WD_TESTS=(tests/test_watchdog_idle.py)

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片> <pytest の引数...>
    local name="$1" frag="$2"; shift 2
    local out; out="$(run_py "$@")"
    if echo "$out" | grep -q "FAILED .*$frag"; then ok "$name → 赤 ($frag)"
    else ng "$name → 赤にならなかった ($frag)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_py "${B1_TESTS[@]}" "${WD_TESTS[@]}")"
if echo "$out" | grep -qE "[0-9]+ passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then
    ok "baseline は緑 ($(echo "$out" | tail -1))"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== case A: 裏の job があっても idle-with-task を通知する"
fresh_copy
inject scripts/dispatcher.sh "    if st in ('idle', 'done') and assignment_file.exists() and worker_has_background_work(target):" \
                             "    if False:"
expect_red "case A" "test_a_live_background_job_is_not_idle_with_task" "${B1_TESTS[@]}"

echo "== case B: 裏の job が無い pane も「裏の job あり」と読む"
fresh_copy
inject scripts/dispatcher.sh "        return classify_process_tree(pane_pid) == 'executing'" \
                             "        return classify_process_tree(pane_pid) != 'no_process'"
expect_red "case B" "test_the_same_pane_without_a_background_job_is_still_notified" "${B1_TESTS[@]}"

echo "== case C: 分類が例外のとき黙る側に倒す"
fresh_copy
inject scripts/dispatcher.sh "        log(f'WARNING: Rule 5 — cannot classify pane process tree for {target!r}: {e!r}')
        return False" \
                             "        log(f'WARNING: Rule 5 — cannot classify pane process tree for {target!r}: {e!r}')
        return True"
expect_red "case C" "test_a_classifier_error_falls_to_the_notifying_side_and_is_logged" "${B1_TESTS[@]}"

echo "== case D: pane pid が引けないとき黙る側に倒す"
fresh_copy
inject scripts/dispatcher.sh "        if pane_pid is None:
            return False" \
                             "        if pane_pid is None:
            return True"
expect_red "case D" "test_unobservable_pane_pid_falls_to_the_notifying_side" "${B1_TESTS[@]}"

echo "== case E: blocked も裏の job があれば黙らせる"
fresh_copy
inject scripts/dispatcher.sh "    if st in ('idle', 'done') and assignment_file.exists() and worker_has_background_work(target):" \
                             "    if st in ('idle', 'done', 'blocked') and assignment_file.exists() and worker_has_background_work(target):"
expect_red "case E" "test_blocked_is_still_notified_with_a_background_job" "${B1_TESTS[@]}"

echo "== case F: 裏の job 中に grace を測り直さない"
fresh_copy
# 'working' への読み替えをやめ、裏の job があるあいだは単に return する (state entry を触らない)
inject scripts/dispatcher.sh "worker_has_background_work(target):
        st = 'working'" \
                             "worker_has_background_work(target):
        return"
expect_red "case F" "test_a_background_job_restarts_the_grace_and_clears_the_dedup_key" "${B1_TESTS[@]}"

echo "== case G: 分類が裏の job を executing と読まない"
fresh_copy
inject scripts/lib_pane_process.py '        if origin == "job":
            return "executing"' '        if origin == "job":
            pass'
expect_red "case G (dispatcher)" "test_a_live_background_job_is_not_idle_with_task" "${B1_TESTS[@]}"
expect_red "case G (watchdog)"   "test_watchdog_does_not_terminate_a_worker_with_a_live_background_job" "${B1_TESTS[@]}"

echo "== case H: 裏の job があると max も効かなくなる"
fresh_copy
inject scripts/watchdog.py '        if now - self.started_at > self.max_threshold:
            return CheckResult("terminate", "max_exceeded"' \
                           '        if now - self.started_at > self.max_threshold and self._process_signal() != "executing":
            return CheckResult("terminate", "max_exceeded"'
expect_red "case H" "test_the_absolute_max_still_applies_with_a_live_background_job" "${B1_TESTS[@]}"

echo "== case I: 裏の job があっても hard idle を terminate にする"
fresh_copy
inject scripts/watchdog.py '                "executing": "hard_idle_but_executing",
' ''
expect_red "case I" "test_watchdog_does_not_terminate_a_worker_with_a_live_background_job" "${B1_TESTS[@]}"

echo "== case J: dispatcher に /proc を読む分類のコピーが生える"
fresh_copy
inject scripts/dispatcher.sh "_mux = Mux()
" "_mux = Mux()
_PROC_COPY = '/proc/self/stat'
"
expect_red "case J" "test_watchdog_and_dispatcher_share_one_classifier" "${B1_TESTS[@]}"

echo "== case L (t049 P3): pane tree の teardown が group ごとでなく root だけになる"
fresh_copy
inject tests/test_background_work_is_not_idle.py '    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass  # 既に居ない (group ごと消えた) — teardown の目的は既に達成
    proc.wait()' '    proc.kill()
    proc.wait()'
expect_red "case L" "test_kill_pane_tree_leaves_no_orphans_behind" "${B1_TESTS[@]}"

echo "== case M (族A監査): 読めない pid を静かに『子孫なし』へ倒す"
fresh_copy
inject scripts/lib_pane_process.py '        try:
            st = _proc_stat(int(entry.name))
        except OSError:
            # この pid が「消滅」以外の理由で読めない。None に潰して静かに
            # スキップすると、この pid を親に持つ (読めている) 子孫が
            # children から永久に辿り着けなくなり、生きている裏 job のサブ
            # ツリーごと見えなくなる (t049 族A監査: 観測失敗を「子孫なし」に
            # 倒していた)。わからないことは "unknown" として呼び出し側に返す。
            return "unknown"
        if st is not None:
            procs[int(entry.name)] = st' '        st = _proc_stat(int(entry.name))
        if st is not None:
            procs[int(entry.name)] = st'
expect_red "case M" "test_an_unreadable_intermediate_pid_falls_to_unknown_not_idle" "${B1_TESTS[@]}"

echo "== case O (t065 P2 2巡目): 既知インフラの comm 一覧から node が抜け、MCP を job と誤読する"
fresh_copy
inject scripts/lib_pane_process.py '    "claude",                # セッション本体
    "node", "npm", "npx",    # MCP サーバーの起動経路 (npm exec / npx 経由で node が実行体になることが多い)' \
                             '    "claude",                # セッション本体
    "npm", "npx",    # MCP サーバーの起動経路 (npm exec / npx 経由で node が実行体になることが多い)'
expect_red "case O" "test_a_late_starting_mcp_browser_does_not_suppress_rule_5_forever" "${B1_TESTS[@]}"

echo "== case P (t065 族C監査): 同定できない永続プロセスを infra 側に倒してしまう"
fresh_copy
inject scripts/lib_pane_process.py '        else:
            # 同定できない (シェルでも既知インフラでもない、親も infra) —
            # job 側に倒す (族C: モジュール docstring 参照)。
            origin = "job"' '        else:
            origin = "infra"'
expect_red "case P" "test_an_unidentified_persistent_process_is_treated_as_a_job_not_infra" "${B1_TESTS[@]}"

echo
echo "Results: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
