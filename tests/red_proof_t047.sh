#!/usr/bin/env bash
# t047 (B8 fix / PR#240 Codex findings) — 欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t047.sh
#
#   baseline — いまの木では対象テストが緑
#   A1 leaked_descendants: cmdline の一致が前の境界を見ない (別ディレクトリの接頭辞誤認) → 赤
#   A2 leaked_descendants: cmdline の一致が `..` を正規化しない (隣のディレクトリ誤認) → 赤
#   B1 kill_budget: 対象の stat が読めない候補を allowed に落とす (fail-open) → 赤
#   B2 kill_budget: 自分の開始時刻が読めなくても候補を allowed に落とす (fail-open) → 赤
#   C1 leaked_descendants: settle() が unobservable の間は再試行しない → 赤
#   C2 leaked_descendants: pytest_sessionfinish が unobservable だけの結果を報告しない → 赤
#   D  leaked_descendants: _belongs が cmdline/cwd の読み取り失敗を observed に反映しない (族A の横展開) → 赤
#
# 安全性 (2 巡目 codex review finding 1 で誤りと判明): このスクリプトの注入は
# `partition()` / `_dir_in_cmdline()` / `settle()` / `pytest_sessionfinish()` を直接・
# 少数の候補で呼ぶだけだが、`run_py` が呼ぶのは **フルの** `python3 -m pytest
# tests/test_leaked_descendants_guard.py tests/test_leak_guard_self_preservation.py` である。
# `tests/conftest.py` の `pytest_configure` はこのセッション**全体**に (欠陥入りの複製の)
# `LeakGuard` を登録するので、個々のテストの後 (autouse fixture) と session finish の
# たびに、複製側の (欠陥入りの) `scan()` が `/proc` を全走査し `kill_all()` が本物の
# `os.kill` を撃つ。「注入点を直接少数の候補で呼ぶだけ」は run_py 単体のテスト関数には
# 当てはまっても、run_py が起動する pytest セッション自体には当てはまらない。
#
# よって `run_py` の呼び出しは**すべて** (baseline も含めて) PID 名前空間の中で実行する。
# 名前空間を作れなければホストへフォールバックせず即座に拒否する (fail closed。族A)。
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t047.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# --- PID 名前空間 (finding 1) -----------------------------------------------------------
#
# `--user --map-root-user` は非特権ユーザーでも `--pid --mount-proc` を使えるようにする
# (WSL2 で動作確認済み)。名前空間の中で `--mount-proc` すると /proc がその名前空間専用に
# 差し替わり、外の pid はそもそも見えない (PID 名前空間はカーネルの階層構造そのもので、
# 子の名前空間から親の pid 空間は一切参照できない —— 名前空間の外へシグナルを送る手段が
# 無い。man 7 pid_namespaces)。
NS_CMD=(unshare --user --map-root-user --pid --fork --mount-proc)

require_pid_namespace() {
    if ! "${NS_CMD[@]}" true 2>/dev/null; then
        echo "FATAL: PID 名前空間 (unshare --user --pid --mount-proc) が使えない環境。" >&2
        echo "       欠陥注入した木は名前空間の外では走らせない (fail closed)。中止する。" >&2
        exit 3
    fi
    local pid1 count
    pid1="$("${NS_CMD[@]}" sh -c 'echo $$')" || { echo "FATAL: 名前空間の自己確認 (PID1) に失敗した" >&2; exit 3; }
    if [ "$pid1" != "1" ]; then
        echo "FATAL: 名前空間の中の PID 1 が自分のラッパーでない (got=$pid1) — 隔離を疑って中止する" >&2
        exit 3
    fi
    count="$("${NS_CMD[@]}" sh -c 'ls /proc | grep -cE "^[0-9]+\$"')" || count=999
    if [ "$count" -gt 10 ]; then
        echo "FATAL: 名前空間の中に $count 個のプロセスが見える (ホストの /proc が漏れている疑い) — 中止する" >&2
        exit 3
    fi
    if "${NS_CMD[@]}" sh -c "kill -0 $$ 2>/dev/null"; then
        echo "FATAL: 名前空間の中からホストの pid $$ にシグナルが届いた — 隔離が効いていない。中止する" >&2
        exit 3
    fi
    echo "  (PID 名前空間の隔離を確認: PID1=$pid1 見えるプロセス=$count 件 ホストへの kill=届かない)"
}

require_pid_namespace

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

run_py() {  # run_py <pytest の引数...> — PID 名前空間の中で走らせる (finding 1)。
            # このセッションの env はそのまま使う (名前空間の中の /proc の実プロセスを見る必要が
            # あるため env -i にはしない —— 名前空間の外はどのみち見えない)。
            #
            # pytest を名前空間の PID1 に**しない** (`sh -c '…; :'` で必ず fork させ、sh を
            # PID1 のまま残す)。対象テストのうち `test_ancestors_and_self_are_refused` /
            # `test_broken_predicate_refuses_the_dangerous_targets` は「pytest に祖先が
            # 1 つ以上見える」ことを前提にしており (PID1 には祖先が無い)、素の
            # `"${NS_CMD[@]}" python3 -m pytest …` だと pytest 自身が PID1 になって
            # 両方とも前提が崩れて red proof 自体が赤くなる (「; :」の直後の command が
            # あるので shell の tail-call 最適化で sh が python3 に化けることもない)。
    ( cd "$TREE" && PYTHONDONTWRITEBYTECODE=1 "${NS_CMD[@]}" \
        sh -c 'python3 -m pytest -q -p no:cacheprovider "$@"; :' sh "$@" 2>&1 )
}
TESTS=(tests/test_leaked_descendants_guard.py tests/test_leak_guard_self_preservation.py)

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local name="$1" frag="$2"
    local out; out="$(run_py "${TESTS[@]}")"
    if echo "$out" | grep -q "FAILED .*$frag"; then ok "$name → 赤 ($frag)"
    else ng "$name → 赤にならなかった ($frag)"; echo "$out" | tail -12; fi
}

fresh_copy
echo "== baseline"
out="$(run_py "${TESTS[@]}")"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then ok "baseline は緑"
else ng "baseline が緑でない"; echo "$out" | tail -20; fi

echo "== case A1: cmdline の一致が前の境界を見ない (別ディレクトリの接頭辞誤認に戻す)"
fresh_copy
inject tests/leaked_descendants.py \
"            if normalized == dirpath or normalized.startswith(dirpath + b\"/\"):
                return True" \
"            if dirpath in normalized:
                return True"
expect_red "case A1" "test_cmdline_match_rejects_a_prefix_directory_sharing_only_a_suffix"

echo "== case A2: cmdline の一致が \`..\` を正規化しない (隣のディレクトリ誤認に戻す)"
fresh_copy
inject tests/leaked_descendants.py \
"            normalized = os.path.normpath(candidate)
            if normalized == dirpath or normalized.startswith(dirpath + b\"/\"):" \
"            normalized = candidate
            if normalized == dirpath or normalized.startswith(dirpath + b\"/\"):"
expect_red "case A2" "test_cmdline_match_normalizes_dot_dot_before_comparing"

echo "== case B1: kill_budget が stat の読めない候補を allowed に落とす (fail-open)"
fresh_copy
inject tests/kill_budget.py \
"        if my_start is None:
            refused.append(Refusal(pid, \"自分の開始時刻が読めない — 年齢を検証できないため拒否\"))
            continue
        got = _ppid_and_start(pid)
        if got is None:
            refused.append(Refusal(pid, \"対象の stat が読めない — 年齢を検証できないため拒否\"))
            continue
        if got[1] < my_start:
            refused.append(Refusal(pid, \"このテストセッションより古い — テストの子孫ではありえない\"))
            continue
        allowed.append(pid)" \
"        got = _ppid_and_start(pid)
        if got is not None and my_start is not None and got[1] < my_start:
            refused.append(Refusal(pid, \"このテストセッションより古い — テストの子孫ではありえない\"))
            continue
        allowed.append(pid)"
expect_red "case B1" "test_a_candidate_whose_stat_cannot_be_read_is_refused_not_allowed"

echo "== case B2: kill_budget が自分の開始時刻が読めなくても候補を allowed に落とす (同じ欠陥、別テスト)"
fresh_copy
inject tests/kill_budget.py \
"        if my_start is None:
            refused.append(Refusal(pid, \"自分の開始時刻が読めない — 年齢を検証できないため拒否\"))
            continue
        got = _ppid_and_start(pid)
        if got is None:
            refused.append(Refusal(pid, \"対象の stat が読めない — 年齢を検証できないため拒否\"))
            continue
        if got[1] < my_start:
            refused.append(Refusal(pid, \"このテストセッションより古い — テストの子孫ではありえない\"))
            continue
        allowed.append(pid)" \
"        got = _ppid_and_start(pid)
        if got is not None and my_start is not None and got[1] < my_start:
            refused.append(Refusal(pid, \"このテストセッションより古い — テストの子孫ではありえない\"))
            continue
        allowed.append(pid)"
expect_red "case B2" "test_all_candidates_are_refused_when_the_sessions_own_start_time_is_unreadable"

echo "== case C1: settle() が survivors がある間しか再試行しない (unobservable を無視する)"
fresh_copy
inject tests/leaked_descendants.py \
"    while (result.survivors or result.unobservable) and time.monotonic() < deadline:" \
"    while result.survivors and time.monotonic() < deadline:"
expect_red "case C1" "test_settle_retries_while_unobservable_remains"

echo "== case C2: pytest_sessionfinish が unobservable だけの結果を報告しない"
fresh_copy
inject tests/leaked_descendants.py \
"        elif result.unobservable:
            self.unobservable_only += 1
            print(f\"\\n[leaked-descendants] session finish: 子孫プロセスとは確認できなかったが、\"
                  f\"環境が読めず観測できなかった同 uid のプロセスが {result.unobservable} 個残っている \"
                  f\"(『無い』とは言えない。数には入れていない)\")" \
""
expect_red "case C2" "test_sessionfinish_reports_but_does_not_fail_on_unobservable_only"

echo "== case D: _belongs が cmdline/cwd の読み取り失敗を observed に反映しない (族A の横展開)"
fresh_copy
inject tests/leaked_descendants.py \
"    base = _PROC / str(pid)
    observed = True

    environ = _read_bytes(base / \"environ\")
    if not environ:
        observed = False
    elif marker in environ.split(b\"\\0\"):
        return \"env-marker\", True

    cmdline = _read_bytes(base / \"cmdline\")
    if not cmdline:
        observed = False
    elif basetemp and _dir_in_cmdline(cmdline, basetemp):
        return \"cmdline-in-basetemp\", True

    try:
        cwd = os.readlink(base / \"cwd\").encode()
    except OSError:
        observed = False
    else:
        if basetemp and (cwd == basetemp or cwd.startswith(basetemp + b\"/\")):
            return \"cwd-in-basetemp\", True

    return None, observed" \
"    base = _PROC / str(pid)
    environ = _read_bytes(base / \"environ\")
    if not environ:
        observed = False
    else:
        observed = True
        if marker in environ.split(b\"\\0\"):
            return \"env-marker\", True
    cmdline = _read_bytes(base / \"cmdline\")
    if cmdline is not None and basetemp and _dir_in_cmdline(cmdline, basetemp):
        return \"cmdline-in-basetemp\", True
    try:
        cwd = os.readlink(base / \"cwd\").encode()
    except OSError:
        cwd = None
    if cwd is not None and basetemp and (cwd == basetemp or cwd.startswith(basetemp + b\"/\")):
        return \"cwd-in-basetemp\", True
    return None, observed"
expect_red "case D" "test_a_readable_environ_does_not_mask_a_failed_cmdline_read"

echo "== case E: kill_all が pidfd を束縛できなかった survivor にフォールバックする (finding 3)"
fresh_copy
inject tests/leaked_descendants.py \
"        if survivor.pidfd is None:
            refused.append(kill_budget.Refusal(
                pid, \"観測時に pidfd で同一性を束縛できなかった (pid 再利用の疑い) — kill しない\"))
            continue
        attempted.append(pid)" \
"        attempted.append(pid)"
expect_red "case E" "test_kill_all_refuses_a_survivor_without_a_verified_pidfd"

echo "== case F: _default_kill が pidfd ではなく pid 番号で送る (finding 3 のフォールバック復活)"
fresh_copy
inject tests/leaked_descendants.py \
"    signal.pidfd_send_signal(survivor.pidfd, sig)" \
"    os.kill(survivor.pid, sig)"
expect_red "case F" "test_default_kill_sends_only_through_the_pidfd_never_by_bare_pid"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
