#!/usr/bin/env bash
# pytest 終了時の宛先の後始末 (mission 20260925-pytest-workspace-leak / t001 / backlog #15) が、
# 欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t001_pytest_workspace.sh
#
#   baseline — いまの木では tests/test_pytest_workspace_sweep.py が緑
#   case A   — 終了フックが後始末を呼ばない (元の漏れそのもの)          → 赤
#   case B   — ガードを外す (本番の宛先・接頭辞違いを閉じる)            → 赤
#   case C   — EPERM を「死んでいる」に倒す                            → 赤
#   case D   — 自分の label を前方一致で閉じる                          → 赤
#   case E   — 「live な pane が無い」の AND を外す                     → 赤
#   case F   — 停止スイッチを無視する                                  → 赤
#   case G   — 生きている pid の残骸まで閉じる                          → 赤
#   case H   — pane 一覧が 0 個 / 読めないを「空」に倒す                → 赤
#   case I   — 後始末の例外がテスト結果へ漏れる                         → 赤
#   case J   — 「idle でない pane」を idle と読む                       → 赤
#   case K   — pid 0 の label を残骸として扱う (os.kill(0,0) は常に成功) → 赤
#   case L   — tmux の kill-session が完全一致 (`=`) でない             → 赤
#   case M   — 残り時間で頭打ちにせず subprocess を走らせる (締切が届かない) → 赤
#   case N   — backend ごとに時間予算が戻る (herdr と tmux で 2 倍)      → 赤
#   case O   — 実 subprocess に timeout を渡さない                      → 赤
#   case P   — timeout の統合テストのスタブが PATH に無い sleep を呼ぶ    → 赤
#              (`exec sleep 60` が not found で即死し、CLI エラーと同じ形で緑になっていた。
#               「timeout を通った」assert (待ち時間・スタブの pid) で赤になる)
#
# 隔離 (2 重):
#   1. 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
#   2. **欠陥を注入したコードの終了フック (pytest_unconfigure) が、本物の tmux / herdr に
#      届かない状態で走らせる。** 複製の pytest は終了時に後始末を走らせるので、case E / G の
#      ように「空か」「pid は死んでいるか」の確認を外した版は、届けば本物の tmux server の
#      `crewvia-pytest-*` (別 worktree で同時に走っている pytest のもの) を kill しうる。
#        - PATH の先頭に tmux / herdr のスタブ (呼び出しを記録して失敗を返す) を置く
#        - TMUX_TMPDIR を空の隔離ディレクトリに向け、TMUX を外す (スタブを迂回しても
#          本物の server には届かない)
#        - CREWVIA_HERDR_SOCK を存在しないパスにする (sweep は socket が無ければ herdr を呼ばない)
#      そのうえで**実行の前後で本物の `tmux ls` が同じ**であること、スタブに kill-session が
#      届いていないこと、本物でなくスタブに届いたこと (隔離が効いていた証拠) を assert する。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t001-pytest-ws.XXXXXX")"
trap 'mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

PASS=0; FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# --- 本物の tmux の観測 (隔離を入れる前の環境で。これは読み取りだけ) -------------------
ORIG_PATH="$PATH"; ORIG_TMUX="${TMUX-}"; ORIG_TMUX_TMPDIR="${TMUX_TMPDIR-}"
REAL_TMUX="$(command -v tmux || true)"
real_tmux_snapshot() {
    [ -n "$REAL_TMUX" ] || { echo "(tmux が無い)"; return 0; }
    ( PATH="$ORIG_PATH"
      if [ -n "$ORIG_TMUX" ]; then export TMUX="$ORIG_TMUX"; else unset TMUX; fi
      if [ -n "$ORIG_TMUX_TMPDIR" ]; then export TMUX_TMPDIR="$ORIG_TMUX_TMPDIR"
      else unset TMUX_TMPDIR; fi
      "$REAL_TMUX" list-sessions -F '#{session_name}:#{session_created}' 2>&1 )
}
TMUX_BEFORE="$(real_tmux_snapshot)"

# --- 隔離: 以降に走らせるものはすべてスタブにしか届かない --------------------------------
STUBS="$WORK/stubs"; mkdir -p "$STUBS" "$WORK/tmux-tmp"
export STUB_LOG="$WORK/stub-calls.log"; : > "$STUB_LOG"
for tool in tmux herdr; do
    # 呼び出しを記録して「server が居ない」と答える。何も作らず何も消さない。
    printf '#!/bin/sh\necho "%s $*" >> "$STUB_LOG"\necho "no server running (red-proof stub)" >&2\nexit 1\n' \
        "$tool" > "$STUBS/$tool"
    chmod +x "$STUBS/$tool"
done
export PATH="$STUBS:$PATH"
export TMUX_TMPDIR="$WORK/tmux-tmp"; unset TMUX
export PYTHONDONTWRITEBYTECODE=1
export CREWVIA_HERDR_SOCK="$WORK/no-such-herdr.sock"

echo "== isolation: 注入版の終了フックは本物の tmux / herdr に届かない"
if [ "$(command -v tmux)" = "$STUBS/tmux" ] && [ "$(command -v herdr)" = "$STUBS/herdr" ]; then
    ok "PATH 先頭の tmux / herdr はスタブ"
else
    echo "FATAL: スタブが PATH の先頭に無い。本物に届きうるので中止する"; exit 2
fi

# case ごとに別のディレクトリ (前の注入を消すために rm しない)
N=0
fresh_copy() {
    N=$((N + 1)); TREE="$WORK/tree-$N"; mkdir -p "$TREE"
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
          --exclude='queue' --exclude='logs' "$REPO_ROOT/" "$TREE/"
}

# inject <相対パス> <old> <new> — 複製したファイルの old を new に置換 (1 か所だけ。無ければ FATAL)
inject() {
    OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

run_suite() {
    (cd "$TREE" && timeout 300 python3 -m pytest tests/test_pytest_workspace_sweep.py \
        -q -p no:cacheprovider -x 2>&1 | tail -4)
}

expect_red() {
    local name="$1" out
    out="$(run_suite)"
    if echo "$out" | grep -q "failed"; then
        ok "$name → 赤"
        echo "$out" | grep -E "^FAILED|failed" | head -2 | sed 's/^/        /'
    else
        ng "$name → 緑のまま (欠陥を検出できない)"
        echo "$out" | sed 's/^/        /'
    fi
}

SWEEP=tests/pytest_workspace_sweep.py

echo "== baseline"
fresh_copy
out="$(run_suite)"
if echo "$out" | grep -q "passed" && ! echo "$out" | grep -q "failed"; then
    ok "baseline は緑: $(echo "$out" | tail -1)"
else
    ng "baseline が緑でない"; echo "$out"
fi

echo "== case A: 終了フックが後始末を呼ばない"
fresh_copy
inject tests/conftest.py \
    "    pytest_workspace_sweep.run_cleanup(TEST_DESTINATION, PRODUCTION_DESTINATION)" \
    "    pass"
expect_red "終了フックを空にする"

echo "== case B: ガードを外す"
fresh_copy
inject $SWEEP \
'    if label == production:
        return f"label {label!r} は本番の宛先"
    if not label.startswith(SESSION_PREFIX):
        return f"label {label!r} は {SESSION_PREFIX!r} で始まらない"
    return None' \
'    return None'
expect_red "guard_refusal が常に None"

echo "== case C: EPERM を「死んでいる」に倒す"
fresh_copy
inject $SWEEP \
'        return PID_DEAD if e.errno == errno.ESRCH else PID_ALIVE' \
'        return PID_DEAD'
expect_red "OSError を全部 dead に"

echo "== case D: 前方一致で閉じる"
fresh_copy
inject $SWEEP \
'    own = [n for n in names if n == own_label]' \
'    own = [n for n in names if n.startswith(own_label)]'
expect_red "own を前方一致で拾う"

echo "== case E: 「live な pane が無い」の AND を外す"
fresh_copy
inject $SWEEP \
'                if not backend.is_empty(handle, deadline):' \
'                if False:'
expect_red "is_empty を見ない"

echo "== case F: 停止スイッチを無視する"
fresh_copy
inject $SWEEP \
'        sweep_enabled = env.get(SWEEP_SWITCH) != "0"' \
'        sweep_enabled = True'
expect_red "CREWVIA_PYTEST_WORKSPACE_SWEEP=0 を読まない"

echo "== case G: 生きている pid の残骸まで閉じる"
fresh_copy
inject $SWEEP \
'            if pid_alive(pid) == PID_DEAD:' \
'            if True:'
expect_red "pid の生死を見ない"

echo "== case H: pane が 0 個 / 読めないを「空」に倒す"
fresh_copy
inject $SWEEP \
'        if not isinstance(panes, list) or not panes:
            return False' \
'        if not isinstance(panes, list) or not panes:
            return True'
expect_red "pane 一覧を観測できないのを空扱い"

echo "== case I: 後始末の例外がテスト結果へ漏れる"
fresh_copy
inject $SWEEP \
'        warn(f"後始末を中断した: {type(e).__name__}: {e}")' \
'        raise'
expect_red "cleanup が例外を握りつぶさない"

echo "== case J: idle でない pane を idle と読む"
fresh_copy
inject $SWEEP \
'    return state == "idle"' \
'    return True'
expect_red "_pane_is_idle が常に True"

echo "== case K: pid 0 の label を残骸として扱う"
fresh_copy
inject $SWEEP \
'    return pid if pid > 0 else None' \
'    return pid'
expect_red "pid 0 を通す"

echo "== case L: tmux の kill-session が完全一致でない"
fresh_copy
inject $SWEEP \
'"kill-session", "-t", f"={session}"' \
'"kill-session", "-t", session'
expect_red "= を付けない"

echo "== case M: 残り時間で頭打ちにせず subprocess を走らせる"
fresh_copy
inject $SWEEP \
'        return run(argv, timeout)' \
'        return run(argv, CLI_TIMEOUT_SECONDS)'
expect_red "締切が subprocess の timeout に届かない (pane 10 個 × 4 秒が上限を超える)"

echo "== case N: backend ごとに時間予算が戻る"
fresh_copy
inject $SWEEP \
'            cleanup(backend, own_label, production, sweep_enabled, deadline=deadline)' \
'            cleanup(backend, own_label, production, sweep_enabled,
                    clock=clock, budget=budget)'
expect_red "run_cleanup が共有の締切を渡さない"

echo "== case O: 実 subprocess に timeout を渡さない"
fresh_copy
inject $SWEEP \
'    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)' \
'    r = subprocess.run(argv, capture_output=True, text=True,
                       timeout=CLI_TIMEOUT_SECONDS)'
expect_red "_subprocess_run が残り時間を捨てる"

echo "== case P: timeout のスタブが PATH に無い sleep を呼ぶ (即死して timeout を通らない)"
fresh_copy
inject tests/test_pytest_workspace_sweep.py \
'herdr_stub=HUNG_HERDR.format(python=sys.executable))' \
'herdr_stub="#!/bin/sh\nexec sleep 60\n")'
expect_red "スタブが即死しても exit code だけでは緑 → timeout を通った assert で赤"

echo "== isolation: 実行後 (本物の tmux は変わらず、スタブにだけ届いた)"
TMUX_AFTER="$(real_tmux_snapshot)"
echo "  本物の tmux (前): $(echo "$TMUX_BEFORE" | tr '\n' ' ')"
echo "  本物の tmux (後): $(echo "$TMUX_AFTER" | tr '\n' ' ')"
if [ "$TMUX_BEFORE" = "$TMUX_AFTER" ]; then
    ok "本物の tmux のセッション一覧は前後で同じ"
else
    ng "本物の tmux のセッション一覧が変わった"
fi
if grep -q '^tmux list-sessions' "$STUB_LOG"; then
    ok "各ケースの終了フックはスタブの tmux に届いた ($(grep -c '^tmux ' "$STUB_LOG") 回。隔離が効いていた証拠)"
else
    ng "スタブの tmux に 1 回も届いていない (隔離が効いているか確かめられない)"
fi
if grep -q -e 'kill-session' -e 'kill-server' "$STUB_LOG"; then
    ng "スタブに kill が届いた: $(grep -e kill "$STUB_LOG" | head -3)"
else
    ok "kill-session / kill-server はどこにも届いていない"
fi
if grep -q '^herdr ' "$STUB_LOG"; then
    ng "herdr に話しかけた: $(grep '^herdr ' "$STUB_LOG" | head -3)"
else
    ok "herdr には話しかけていない (socket が無いので CLI を呼ばない)"
fi

echo
echo "== 結果: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
