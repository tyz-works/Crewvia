#!/usr/bin/env bats
# tests/start-sh-mux-gate.bats
#
# t002 (mission 20260909-dead-config-sweep): 並列モードゲート二重化の回帰テスト。
#
# 症状: 並列モードを実際にゲートしているのは scripts/start.sh の
# `CREWVIA_MUX_ENABLED == 1` 判定 1 箇所のみ。しかし CREWVIA_MUX_ENABLED を
# 設定する処理は「ROLE=director かつ CREWVIA_MUX_ENABLED 未設定」の対話選択
# ブロックの中にしか無かった。そのため ROLE=worker では CREWVIA_MUX=herdr/tmux
# を明示しても CREWVIA_MUX_ENABLED が設定されず、ゲート判定でインラインモード
# (exec claude) に転落していた。CLAUDE.md / config/crewvia.yaml は元々
# 「CREWVIA_MUX が設定済みなら CREWVIA_MUX_ENABLED は不要」と説明しており、
# ドキュメントと実装が食い違っていた。
#
# 修正 (設計 (A)): ゲート判定の直前で実効値を解決する。CREWVIA_MUX_ENABLED が
# 明示されていれば最優先、未設定なら CREWVIA_MUX の有無にフォールバックする。
#
# 実 tmux は使わず fake tmux で「並列モードへ入り spawn が呼ばれたか」を
# 検証する。exec claude (インラインモード) に転落した場合の安全策として、
# fake claude スタブも PATH 先頭に置く — 万一ゲートが壊れて転落しても、
# 実 Claude Code (このテストを実行している claude 自身) が再帰的に
# 起動されることはない。
#
# Run: bats tests/start-sh-mux-gate.bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
START_SH="${REPO_ROOT}/scripts/start.sh"

setup_fake_env() {
    FAKE_DIR="$(mktemp -d)"
    FAKE_TMUX_LOG="${FAKE_DIR}/tmux_calls.log"
    FAKE_CLAUDE_LOG="${FAKE_DIR}/claude_calls.log"
    touch "$FAKE_TMUX_LOG" "$FAKE_CLAUDE_LOG"

    cat > "${FAKE_DIR}/tmux" <<'FAKESCRIPT'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
cmd="${1:-}"
case "$cmd" in
  has-session) exit 0 ;;
  list-sessions) echo "crewvia: 1 windows"; exit 0 ;;
  new-session) exit 0 ;;
  new-window) exit 0 ;;
  list-windows) echo "watchdog"; exit 0 ;;
  send-keys) exit 0 ;;
  capture-pane) echo "❯ "; exit 0 ;;
  kill-window) exit 0 ;;
  display-message) echo "12345"; exit 0 ;;
  *) exit 1 ;;
esac
FAKESCRIPT
    chmod +x "${FAKE_DIR}/tmux"

    # 安全弁: 万一インライン (exec claude) に転落しても実 claude を起動させない。
    # FAKE_DIR は PATH の先頭に置くので、実 claude より必ず先に見つかる。
    cat > "${FAKE_DIR}/claude" <<'FAKESCRIPT'
#!/usr/bin/env bash
echo "FAKE_CLAUDE_INVOKED $*" >> "$FAKE_CLAUDE_LOG"
exit 0
FAKESCRIPT
    chmod +x "${FAKE_DIR}/claude"

    export FAKE_DIR FAKE_TMUX_LOG FAKE_CLAUDE_LOG
    export PATH="${FAKE_DIR}:${PATH}"
    export CREWVIA_TASKVIA=disabled
    unset CREWVIA_BENCH_MODE
    export AGENT_NAME="GateTest"
    unset CREWVIA_TMUX_SESSION
}

teardown() {
    if [[ -n "${FAKE_DIR:-}" && -d "$FAKE_DIR" ]]; then
        find "$FAKE_DIR" -mindepth 1 -delete 2>/dev/null || true
        rmdir "$FAKE_DIR" 2>/dev/null || true
    fi
    # start.sh writes the worker system prompt into WORK_DIR/.claude/settings.local.json
    # (gitignored) when reaching the mux/prompt-write path — clean it up.
    rm -f "${REPO_ROOT}/.claude/settings.local.json"
}

@test "ROLE=worker + CREWVIA_MUX=tmux + CREWVIA_MUX_ENABLED unset uses parallel mode, not inline (t002 fix)" {
    setup_fake_env
    export CREWVIA_MUX=tmux
    unset CREWVIA_MUX_ENABLED

    run bash "$START_SH" worker --name GateTest code

    [ "$status" -eq 0 ]
    [[ "$output" == *"Agent launched in mux window"* ]]

    # fake tmux が実際に呼ばれたこと（並列モードへ入った証拠）
    [ -s "$FAKE_TMUX_LOG" ]
    grep -q "new-window\|new-session" "$FAKE_TMUX_LOG"

    # exec claude (インライン) には転落していないこと
    [ ! -s "$FAKE_CLAUDE_LOG" ]
}

@test "ROLE=worker + CREWVIA_MUX unset + CREWVIA_MUX_ENABLED unset still falls back to inline (default behavior preserved)" {
    setup_fake_env
    unset CREWVIA_MUX
    unset CREWVIA_MUX_ENABLED

    run bash "$START_SH" worker --name GateTest code

    [ "$status" -eq 0 ]
    # fake claude (inline 経路) が呼ばれたこと。fake tmux は一切呼ばれない。
    [ -s "$FAKE_CLAUDE_LOG" ]
    grep -q "FAKE_CLAUDE_INVOKED" "$FAKE_CLAUDE_LOG"
    [ ! -s "$FAKE_TMUX_LOG" ]
}

@test "CREWVIA_MUX_ENABLED=0 explicit still forces inline even when CREWVIA_MUX is set (explicit override honored)" {
    setup_fake_env
    export CREWVIA_MUX=tmux
    export CREWVIA_MUX_ENABLED=0

    run bash "$START_SH" worker --name GateTest code

    [ "$status" -eq 0 ]
    [ -s "$FAKE_CLAUDE_LOG" ]
    [ ! -s "$FAKE_TMUX_LOG" ]
}
