#!/usr/bin/env bash
# t047 (B8 fix / PR#240 Codex findings) — 欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t047.sh
#
#   baseline — いまの木では対象テストが緑
#   A  leaked_descendants: cmdline の basetemp 一致が境界を要求しない (素の部分文字列一致に戻す) → 赤
#   B1 kill_budget: 対象の stat が読めない候補を allowed に落とす (fail-open) → 赤
#   B2 kill_budget: 自分の開始時刻が読めなくても候補を allowed に落とす (fail-open) → 赤
#   C1 leaked_descendants: settle() が unobservable の間は再試行しない → 赤
#   C2 leaked_descendants: pytest_sessionfinish が unobservable だけの結果を報告しない → 赤
#   D  leaked_descendants: _belongs が cmdline/cwd の読み取り失敗を observed に反映しない (族A の横展開) → 赤
#
# 安全性: このスクリプトの注入はいずれも `partition()` / `_dir_in_cmdline()` / `settle()` /
# `pytest_sessionfinish()` を直接・少数の候補 (存在しない合成 pid や自分で spawn した子) で
# 呼ぶだけで、`scan()` で `/proc` を全走査してから本物の `os.kill` を送る経路 (2026-09-27 の
# 事故の型) は一切通らない。よって unshare によるプロセス名前空間の隔離は不要
# (tests/CLAUDE.md 「判定を壊す変異テストは PID 名前空間の中だけで走らせる」は
# `_belongs` を壊して `scan()` の全走査 + 本物の kill を組み合わせる変異が対象で、
# ここでの回帰は範囲がそれとは違う)。
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

run_py() {  # run_py <pytest の引数...> — このセッションの env をそのまま使う (/proc の実プロセスを見る必要があるため env -i にしない)
    ( cd "$TREE" && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider "$@" 2>&1 )
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

echo "== case A: cmdline の basetemp 一致が境界を要求しない (部分文字列一致に戻す)"
fresh_copy
inject tests/leaked_descendants.py \
"        if end == len(haystack) or haystack[end:end + 1] in (b\"\\0\", b\"/\"):
            return True" \
"        if True:
            return True"
expect_red "case A" "test_cmdline_match_requires_a_path_boundary_not_a_bare_prefix"

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

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
