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
            # pytest を名前空間の PID1 に**しない** (`sh -c '… ; ec=$?; exit "$ec"'` で必ず
            # fork させ、sh を PID1 のまま残す)。対象テストのうち `test_ancestors_and_self_are_refused` /
            # `test_broken_predicate_refuses_the_dangerous_targets` は「pytest に祖先が
            # 1 つ以上見える」ことを前提にしており (PID1 には祖先が無い)、素の
            # `"${NS_CMD[@]}" python3 -m pytest …` だと pytest 自身が PID1 になって
            # 両方とも前提が崩れて red proof 自体が赤くなる (pytest の直後に別コマンドが
            # あるので shell の tail-call 最適化で sh が python3 に化けることもない)。
            #
            # ただし `ec=$?; exit "$ec"` で pytest の**実際の**終了コードを sh -c 自身の
            # 終了コードとして持ち帰る (3巡目 codex review finding 2)。旧実装は `; :` で
            # 終えており、`:` は常に 0 を返すので run_py の呼び出し元は「N passed, 1 error」
            # のような、`N failed` の行が出ず passed 件数だけが並ぶ teardown/sessionfinish
            # の非 0 終了コードを検知できなかった (exit-code-through-a-pipe-is-not-the-suite-s
            # と同じ型)。
    ( cd "$TREE" && PYTHONDONTWRITEBYTECODE=1 "${NS_CMD[@]}" \
        sh -c 'python3 -m pytest -q -p no:cacheprovider "$@"; ec=$?; exit "$ec"' sh "$@" 2>&1 )
}

run_py_swallowing_exit_code() {  # case H 専用の比較対象。finding 2 の欠陥そのもの (旧実装) を
                                  # そのまま再現するだけの関数 —— 本番の run_py はこの形に戻さない。
    ( cd "$TREE" && PYTHONDONTWRITEBYTECODE=1 "${NS_CMD[@]}" \
        sh -c 'python3 -m pytest -q -p no:cacheprovider "$@"; :' sh "$@" 2>&1 )
}

# case G 専用のランナー。`del os.pidfd_open` を pytest がテストモジュールを import するより
# **前**に行うことで、「属性として存在しない環境」を Linux 上でも直接模す (実 OS 差し替えは
# 不要 —— os モジュールはプロセス内シングルトンなので、この 1 プロセスの中では以降
# `hasattr(os, "pidfd_open")` も `os.pidfd_open(...)` も無い環境と同じに振る舞う)。
NO_PIDFD_RUNNER="$WORK/run_without_pidfd_open.py"
cat > "$NO_PIDFD_RUNNER" <<'PY'
import os, sys
if hasattr(os, "pidfd_open"):
    del os.pidfd_open
import pytest
sys.exit(pytest.main(sys.argv[1:]))
PY

run_py_without_pidfd_open() {  # case G: os.pidfd_open が属性として無い環境を模して起動する (finding 1)。
                                # `${@:2}` は bash 専用のスライス構文で、ここで起動する `sh`
                                # (dash) には無く `Bad substitution` になる。`shift` で POSIX に。
    ( cd "$TREE" && PYTHONDONTWRITEBYTECODE=1 "${NS_CMD[@]}" \
        sh -c 'runner="$1"; shift; python3 "$runner" -q -p no:cacheprovider "$@"; ec=$?; exit "$ec"' \
        sh "$NO_PIDFD_RUNNER" "$@" 2>&1 )
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
out="$(run_py "${TESTS[@]}")"; rc=$?
# 終了コード 0 を必須にする (finding 2)。`N passed, 1 error` のように passed 件数だけが
# 出て `N failed` の行が無いまま非 0 で終わるケースを、文字列一致だけでは緑と誤認する。
if [ "$rc" -eq 0 ] && echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then
  ok "baseline は緑 (exit=$rc)"
else
  ng "baseline が緑でない (exit=$rc)"; echo "$out" | tail -20
fi

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
        # bytes の path を渡すと os.readlink は bytes のまま返す (str を経由しない)。\`base / \"cwd\"\`
        # 自体は ASCII しか含まない (/proc/<pid>/cwd) が、シンボリックリンクの**指す先**
        # (相手プロセスの cwd) は任意バイト列でありうる。旧実装 (\`os.readlink(str_path).encode()\`)
        # は readlink が str へ decode する際に surrogateescape を使うため文字列としては読めて
        # しまい、その後の \`.encode()\` (既定は strict UTF-8) が孤立サロゲートを再エンコードできず
        # \`UnicodeEncodeError\` (\`OSError\` のサブクラスではない) を投げていた —— 無関係な同 UID の
        # 1 プロセスの cwd が UTF-8 でないだけで session-finish の走査全体が落ちる (5巡目 P2-1)。
        # bytes 経由なら str 化を一切経ないのでこの往復が起きない。
        cwd = os.readlink(os.fsencode(base / \"cwd\"))
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
        cwd = os.readlink(os.fsencode(base / \"cwd\"))
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
        ok, exc = _signal_one(kill, survivor, signal.SIGKILL)" \
"        ok, exc = _signal_one(kill, survivor, signal.SIGKILL)"
expect_red "case E" "test_kill_all_refuses_a_survivor_without_a_verified_pidfd"

echo "== case F: _default_kill が pidfd ではなく pid 番号で送る (finding 3 のフォールバック復活)"
fresh_copy
inject tests/leaked_descendants.py \
"    signal.pidfd_send_signal(survivor.pidfd, sig)" \
"    os.kill(survivor.pid, sig)"
expect_red "case F" "test_default_kill_sends_only_through_the_pidfd_never_by_bare_pid"

echo "== case G (fix): os.pidfd_open が属性として無い環境でも収集は落ちず skip になる (3巡目 finding 1)"
fresh_copy
out="$(run_py_without_pidfd_open "${TESTS[@]}")"; rc=$?
# 「このカーネルは pidfd_open が使えない」の警告文自体が "ERROR" という語を含む (install() が
# 意図して出す想定内の文言) ので、粗い `grep -qi error` はこの警告に誤爆する。pytest の
# サマリ行が実際に出す `N error(s)` の形だけを見る。
if [ "$rc" -eq 0 ] && echo "$out" | grep -qE "[0-9]+ skipped" && ! echo "$out" | grep -qE "[0-9]+ error"; then
  ok "case G (fix) → os.pidfd_open 不在でも skip として緑 (exit=$rc)"
else
  ng "case G (fix) → os.pidfd_open 不在で壊れた (exit=$rc)"; echo "$out" | tail -20
fi

echo "== case G (defect): hasattr 保護を外すと os.pidfd_open 不在で収集失敗になる"
fresh_copy
inject tests/leaked_descendants.py \
"    if not hasattr(os, \"pidfd_open\") or not hasattr(signal, \"pidfd_send_signal\"):
        return False
    try:
        fd = os.pidfd_open(os.getpid())" \
"    try:
        fd = os.pidfd_open(os.getpid())"
out="$(run_py_without_pidfd_open "${TESTS[@]}")"; rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -qi "AttributeError"; then
  ok "case G (defect) → 赤 (AttributeError で収集失敗, exit=$rc)"
else
  ng "case G (defect) → 赤にならなかった (exit=$rc)"; echo "$out" | tail -20
fi

echo "== case H: run_py が pytest の終了コードを捨てると N passed, 1 error を green 扱いしてしまう (3巡目 finding 2)"
fresh_copy
LEAK_TEST_REL="tests/red_proof_case_h_leak.py"
cat > "$TREE/$LEAK_TEST_REL" <<'PY'
"""case H 専用の使い捨てテスト。red_proof_t047.sh 以外からは呼ばれない。

テスト本体は何もせず合格するが、autouse の LeakGuard フィクスチャが後片付けで見つけた
子孫プロセスに対して teardown で `pytest.fail` するため、この 1 本は「FAILED」ではなく
「ERROR」として集計される (`leaked_descendants.LeakGuard._crewvia_no_leaked_descendants` 参照)。
`N passed, 1 error` (`N failed` の行が無い) を確実に再現する。
"""
import subprocess


def test_intentionally_leaks_a_child_for_red_proof_case_h():
    subprocess.Popen(["sleep", "30"])
PY

out_fixed="$(run_py "$LEAK_TEST_REL")"; rc_fixed=$?
if echo "$out_fixed" | grep -q " passed" && echo "$out_fixed" | grep -qE "[0-9]+ error" \
   && ! echo "$out_fixed" | grep -qE "[0-9]+ failed"; then
  ok "case H → N passed, 1 error の場面を再現できた"
else
  ng "case H → N passed, 1 error を再現できなかった (前提が崩れた)"; echo "$out_fixed" | tail -20
fi
if [ "$rc_fixed" -ne 0 ]; then
  ok "case H (現行 run_py) → 非0 exit=$rc_fixed で検知できた"
else
  ng "case H (現行 run_py) → exit=0 のまま検知できなかった"; echo "$out_fixed" | tail -20
fi

out_old="$(run_py_swallowing_exit_code "$LEAK_TEST_REL")"; rc_old=$?
if [ "$rc_old" -eq 0 ]; then
  ok "case H (旧実装 run_py '; :') → 欠陥を再現: 同じ場面でも exit=0 のまま green に見える"
else
  ng "case H (旧実装 run_py '; :') → 欠陥が再現しなかった (前提が崩れた: \$? が本当に捨てられているか要確認)"
  echo "$out_old" | tail -20
fi

echo "== case I1: leaked_descendants._read_stat が UTF-8 でない comm で UnicodeDecodeError を漏らす (4巡目 P2-1)"
fresh_copy
inject tests/leaked_descendants.py \
"    try:
        raw = (_PROC / str(pid) / \"stat\").read_bytes()
    except OSError:
        return None
    rp = raw.rfind(b\")\")
    if rp < 0:
        return None
    rest = raw[rp + 2:].split()
    try:
        state = rest[0].decode(\"ascii\", errors=\"replace\")
        return state, int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None" \
"    try:
        raw = (_PROC / str(pid) / \"stat\").read_text()
    except OSError:
        return None
    rp = raw.rfind(\")\")
    if rp < 0:
        return None
    rest = raw[rp + 2:].split()
    try:
        return rest[0], int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None"
expect_red "case I1" "test_read_stat_survives_a_non_utf8_process_name"

echo "== case I2: kill_budget._ppid_and_start が UTF-8 でない comm で UnicodeDecodeError を漏らす (4巡目 P2-1、同じ族)"
fresh_copy
inject tests/kill_budget.py \
"    try:
        raw = (_PROC / str(pid) / \"stat\").read_bytes()
    except OSError:
        return None
    rp = raw.rfind(b\")\")
    if rp < 0:
        return None
    rest = raw[rp + 2:].split()
    try:
        return int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None" \
"    try:
        raw = (_PROC / str(pid) / \"stat\").read_text()
    except OSError:
        return None
    rp = raw.rfind(\")\")
    if rp < 0:
        return None
    rest = raw[rp + 2:].split()
    try:
        return int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None"
expect_red "case I2" "test_ppid_and_start_survives_a_non_utf8_process_name"

echo "== case I3: proc_group.descendants が UTF-8 でない comm で UnicodeDecodeError を漏らす (4巡目 P2-1、同じ族)"
fresh_copy
inject tests/proc_group.py \
"        try:
            raw = pathlib.Path(f\"/proc/{name}/stat\").read_bytes()
        except OSError:
            continue                 # 走査中に死んだ
        rp = raw.rfind(b\")\")
        if rp < 0:
            continue
        rest = raw[rp + 2:].split()
        try:
            ppid = int(rest[1])
        except (IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(name))" \
"        try:
            raw = pathlib.Path(f\"/proc/{name}/stat\").read_text()
        except OSError:
            continue                 # 走査中に死んだ
        rest = raw[raw.rfind(\")\") + 2:].split()
        children.setdefault(int(rest[1]), []).append(int(name))"
expect_red "case I3" "test_descendants_survives_a_non_utf8_process_name"

echo "== case J: _open_pidfd_verified が signal.pidfd_send_signal 不在をゲートしない (4巡目 P2-2)"
fresh_copy
inject tests/leaked_descendants.py \
"    if not hasattr(os, \"pidfd_open\") or not hasattr(signal, \"pidfd_send_signal\"):
        return None
    try:
        fd = os.pidfd_open(pid)" \
"    if not hasattr(os, \"pidfd_open\"):
        return None
    try:
        fd = os.pidfd_open(pid)"
expect_red "case J" "test_open_pidfd_verified_returns_none_without_raising_when_signal_pidfd_send_signal_is_absent"

echo "== case K1: _belongs の cwd readlink が UTF-8 でない cwd で UnicodeEncodeError を漏らす (5巡目 P2-1)"
fresh_copy
inject tests/leaked_descendants.py \
"    try:
        # bytes の path を渡すと os.readlink は bytes のまま返す (str を経由しない)。\`base / \"cwd\"\`
        # 自体は ASCII しか含まない (/proc/<pid>/cwd) が、シンボリックリンクの**指す先**
        # (相手プロセスの cwd) は任意バイト列でありうる。旧実装 (\`os.readlink(str_path).encode()\`)
        # は readlink が str へ decode する際に surrogateescape を使うため文字列としては読めて
        # しまい、その後の \`.encode()\` (既定は strict UTF-8) が孤立サロゲートを再エンコードできず
        # \`UnicodeEncodeError\` (\`OSError\` のサブクラスではない) を投げていた —— 無関係な同 UID の
        # 1 プロセスの cwd が UTF-8 でないだけで session-finish の走査全体が落ちる (5巡目 P2-1)。
        # bytes 経由なら str 化を一切経ないのでこの往復が起きない。
        cwd = os.readlink(os.fsencode(base / \"cwd\"))
    except OSError:
        observed = False" \
"    try:
        cwd = os.readlink(base / \"cwd\").encode()
    except OSError:
        observed = False"
expect_red "case K1" "test_belongs_survives_a_non_utf8_cwd"

echo "== case L: kill_all が PermissionError 等 (ProcessLookupError 以外) を握り潰さず kill_all ごと止まる (5巡目 P2-2)"
fresh_copy
inject tests/leaked_descendants.py \
"        ok, exc = _signal_one(kill, survivor, signal.SIGKILL)
        if ok:
            attempted.append(pid)
        else:
            # 境界 (5巡目 P2-2 / この task の主眼): シグナル送信で何が起きても、この 1 件の失敗が
            # 残りの survivor の処理を止めない。\`pidfd_send_signal\` は \`PermissionError\` (資格情報
            # の変化・セキュリティ制約) も出しうるが、旧実装は \`ProcessLookupError\` しか捕まえて
            # おらず、他の例外は \`kill_all\` を丸ごと抜けて hook まで伝播し、残りの survivor の
            # kill 試行も \`_close_survivor_pidfds\` による fd の後始末も飛んでいた。
            refused.append(kill_budget.Refusal(
                pid, f\"シグナルを送れなかった ({type(exc).__name__}: {exc}) — 他の survivor の処理は続ける\"))" \
"        attempted.append(pid)
        try:
            kill(survivor, signal.SIGKILL)
        except ProcessLookupError:
            pass"
expect_red "case L" "test_kill_all_continues_past_a_permission_error_and_closes_all_pidfds"

echo "== case M: _scan_one の境界を Exception から OSError に狭めると、未知の例外が走査全体を落とす (5巡目の主眼)"
fresh_copy
inject tests/leaked_descendants.py \
"        return survivor, False
    except Exception:
        return None, True" \
"        return survivor, False
    except OSError:
        return None, True"
expect_red "case M" "test_scan_one_boundary_catches_an_arbitrary_exception_from_belongs"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
