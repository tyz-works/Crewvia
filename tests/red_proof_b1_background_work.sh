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
#   O  分類 (t091 P1 本体): environ フォールバックを丸ごと外す                     → 赤
#   P  分類 (t074 族C監査の向き訂正 / t091): CLAUDECODE すら無いものを job 側に倒す → 赤
#   Q  dispatcher (t074 追補): BACKGROUND_JOB_MAX_SECONDS の安全弁を外す           → 赤
#   R  dispatcher (t074 追補): job_since が grace の since と混同される            → 赤
#
# t049 の時刻ベースの判定 (grace_seconds の枠内で始まった裏 job を assignment mtime
# で拾う仕組み。旧 case K/N) は t065 でプロセスの同定 (comm) に置き換えられ、丸ごと
# 撤去した。t074 (Codex review 3巡目) はその comm ベースの同定も「誰が起動したか」
# (Bash tool / Monitor のラッパーの子孫か) へ丸ごと置き換えた — 本番の
# `npm exec @playwright/mcp` が npm の process.title 書き換えと `sh -c "..."` を
# 挟む経路のせいで、comm ベースの許可リストでは MCP を job と誤読する (偽陰性) ため。
# Q/R は Director が本番で踏んだ実例 (Ren の pgrep 自己一致ループが起動元判定でも
# job のまま黙り続ける) への追補。
#
# t091 (B1 5巡目 P1): Bash tool 内で `exec` を使うと cmdline のマーカーが消える
# (execve が argv を丸ごと差し替える) ので、cmdline だけを見ていた判定 (t074) は
# 本物の job を idle_process に誤分類する。environ (`exec` は envp を継承する) を
# 第二の証拠に足した — これに伴い旧 case O (wrapper marker 定数破損) は
# 「environ フォールバックが救うので、この単独欠陥ではもう赤にならない」という
# **改善の実証**に変わった (`tests/test_background_work_is_not_idle.py` の
# `test_a_changed_wrapper_marker_no_longer_loses_the_job_thanks_to_environ` 参照)。
# ここでの O/P は t091 の環境変数フォールバック自体を狙った赤の実証に更新した。
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
inject scripts/dispatcher.sh "    has_job = st in ('idle', 'done') and assignment_file.exists() and worker_has_background_work(target)" \
                             "    has_job = False"
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
inject scripts/dispatcher.sh "    has_job = st in ('idle', 'done') and assignment_file.exists() and worker_has_background_work(target)" \
                             "    has_job = st in ('idle', 'done', 'blocked') and assignment_file.exists() and worker_has_background_work(target)"
expect_red "case E" "test_blocked_is_still_notified_with_a_background_job" "${B1_TESTS[@]}"

echo "== case F: 裏の job 中に grace を測り直さない"
fresh_copy
# 'working' への読み替えをやめ、裏の job があるあいだは単に return する (state entry を触らない)
inject scripts/dispatcher.sh "        if reliable and (now - job_since <= BACKGROUND_JOB_MAX_SECONDS):
            st = 'working'" \
                             "        if reliable and (now - job_since <= BACKGROUND_JOB_MAX_SECONDS):
            return"
expect_red "case F" "test_a_background_job_restarts_the_grace_and_clears_the_dedup_key" "${B1_TESTS[@]}"

echo "== case G: 分類が裏の job を executing と読まない"
fresh_copy
inject scripts/lib_pane_process.py '        if origin == "job":
            return "executing"  # job はどこで見つかっても即座に確定する' '        if origin == "job":
            pass  # job はどこで見つかっても即座に確定する'
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

echo "== case O (t091 P1 本体): environ フォールバックを丸ごと外す (exec で cmdline のマーカーが消える P1 の再現)"
fresh_copy
inject scripts/lib_pane_process.py '    if BASH_TOOL_WRAPPER_MARKER in cmdline:
        return "job"
    environ = _proc_environ(pid)
    if environ is None:
        return "infra"
    if any(marker in environ for marker in JOB_ENVIRON_MARKERS):
        return "job"
    if INFRA_ENVIRON_MARKER in environ:
        return "infra"
    return "unknown"' \
                                    '    if BASH_TOOL_WRAPPER_MARKER in cmdline:
        return "job"
    return "infra"'
expect_red "case O" "test_exec_erasing_the_cmdline_marker_is_still_a_job" "${B1_TESTS[@]}"

echo "== case P (t074 族C監査の向き訂正 / t091): CLAUDECODE すら無いものを job 側に倒し直してしまう"
fresh_copy
inject scripts/lib_pane_process.py '    if INFRA_ENVIRON_MARKER in environ:
        return "infra"
    return "unknown"' \
                                    '    if INFRA_ENVIRON_MARKER in environ:
        return "infra"
    return "job"'
expect_red "case P" "test_a_process_with_no_claudecode_evidence_at_all_is_unknown_not_idle" "${B1_TESTS[@]}"

echo "== case Q (t074 追補): BACKGROUND_JOB_MAX_SECONDS の安全弁を外す (黙り続ける方に戻る)"
fresh_copy
inject scripts/dispatcher.sh "        if reliable and (now - job_since <= BACKGROUND_JOB_MAX_SECONDS):
            st = 'working'" "        st = 'working'"
expect_red "case Q" "test_a_job_older_than_the_ceiling_no_longer_suppresses_rule5" "${B1_TESTS[@]}"

echo "== case R (t074 追補): job_since が grace の since と混同され、job 中に測り直ってしまう"
fresh_copy
inject scripts/dispatcher.sh "        job_since, reliable = _load_job_since(name)
        if job_since is None:
            job_since = now
            # t082 P2: 保存に失敗したら、今回計った job_since は次のサイクルで
            # 読み直せない (= 保てていない)。reliable を落とし、上限判定を
            # 信用しない側に倒す (握り潰して「保存できた」ふりをしない)。
            reliable = _save_job_since(name, job_since) and reliable" \
                             "        job_since, reliable = now, True
        _save_job_since(name, job_since)"
expect_red "case R" "test_the_ceiling_timer_is_independent_of_the_grace_timer" "${B1_TESTS[@]}"

echo "== case S (t082 P1 / t091): cmdline が読めないノードを『インフラ』に潰す (job が idle_process に化ける)"
fresh_copy
inject scripts/lib_pane_process.py '    cmdline = _proc_cmdline(pid)
    if cmdline is None:
        return "infra"' \
                             '    try:
        cmdline = _proc_cmdline(pid)
    except OSError:
        return "infra"
    if cmdline is None:
        return "infra"'
expect_red "case S" "test_an_unreadable_wrapper_is_unknown_not_infra" "${B1_TESTS[@]}"

echo "== case T (t082 P2): job_since の『無い』と『読めない』を同じに潰す"
fresh_copy
inject scripts/dispatcher.sh "    if is_missing(entry):
        return None, True
    if is_unreadable(entry):
        return None, False
    value = entry.get('job_since')
    if is_finite_number(value):
        return value, True
    return None, False  # スキーマ検証済みのはずだが、念のため信用しない側に倒す" \
                         "    if is_unreadable(entry):
        return None, True
    value = entry.get('job_since')
    return (value, True) if is_finite_number(value) else (None, True)"
expect_red "case T" "test_an_unreadable_job_since_file_does_not_suppress_forever" "${B1_TESTS[@]}"

echo "== case U (t082 P2): job_since の書き込み失敗を握り潰す (保存できたふりをする)"
fresh_copy
inject scripts/dispatcher.sh "    except Exception as e:
        log(f'WARNING: cannot write job_since entry for {name!r}: {e}')
        return False
    return True" \
                             "    except Exception as e:
        log(f'WARNING: cannot write job_since entry for {name!r}: {e}')
    return True"
expect_red "case U" "test_a_job_since_write_failure_does_not_suppress_forever" "${B1_TESTS[@]}"

echo
echo "Results: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
