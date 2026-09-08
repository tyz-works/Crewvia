#!/usr/bin/env bats
# tests/start-sh-kickoff.bats
#
# start.sh 側の kickoff 着弾検証 + リトライテスト (t001, mission 20260908-launch-reliability)。
#
# 背景: mux_send の戻り値だけでは着弾を検知できない (lib_mux.send は best-effort
# で rc=0 を返す)。2026-09-08 に Worker が指示未着弾のまま idle → 自主終了で
# pane 消滅する事故が3回発生した。start.sh は送信後に mux_verify_sent で
# 入力行の残留を確認し、残っていればリトライ、それでも駄目なら「送った
# つもり」にせず失敗として報告する。
#
# 実際の tmux は使わず、fake tmux で capture-pane の応答を固定して
# 「一発で着弾」「常に入力行に残り続ける（失敗として報告される）」の
# 2 パターンを検証する。
#
# Run:
#   bats tests/start-sh-kickoff.bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
START_SH="${REPO_ROOT}/scripts/start.sh"

# capture-pane の応答を $FAKE_TMUX_SCREEN の内容に固定した fake tmux を用意する。
setup_fake_tmux_kickoff() {
    local screen_content="$1"

    FAKE_TMUX_DIR="$(mktemp -d)"
    FAKE_TMUX_LOG="${FAKE_TMUX_DIR}/calls.log"
    FAKE_TMUX_SCREEN="${FAKE_TMUX_DIR}/screen"
    touch "$FAKE_TMUX_LOG"
    printf '%s' "$screen_content" > "$FAKE_TMUX_SCREEN"

    cat > "${FAKE_TMUX_DIR}/tmux" <<'FAKESCRIPT'
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
  capture-pane) cat "$FAKE_TMUX_SCREEN"; exit 0 ;;
  kill-window) exit 0 ;;
  display-message) echo "12345"; exit 0 ;;
  *) exit 1 ;;
esac
FAKESCRIPT
    chmod +x "${FAKE_TMUX_DIR}/tmux"

    export FAKE_TMUX_DIR FAKE_TMUX_LOG FAKE_TMUX_SCREEN
    export PATH="${FAKE_TMUX_DIR}:${PATH}"
    export CREWVIA_MUX=tmux
    export CREWVIA_MUX_ENABLED=1
    export CREWVIA_TASKVIA=disabled
    unset CREWVIA_BENCH_MODE
    export AGENT_NAME="KickoffTest"
    unset CREWVIA_TMUX_SESSION
}

teardown() {
    if [[ -n "${FAKE_TMUX_DIR:-}" && -d "$FAKE_TMUX_DIR" ]]; then
        find "$FAKE_TMUX_DIR" -mindepth 1 -delete 2>/dev/null || true
        rmdir "$FAKE_TMUX_DIR" 2>/dev/null || true
    fi
    # start.sh writes the worker system prompt into WORK_DIR/.claude/settings.local.json
    # (gitignored) since CREWVIA_BENCH_MODE is unset for these tests — clean it up.
    rm -f "${REPO_ROOT}/.claude/settings.local.json"
}

@test "kickoff lands on the first attempt: exactly one send, no retry, success logged" {
    # capture-pane always shows a clear input line — the kickoff text never
    # appears there (already submitted / never stuck).
    setup_fake_tmux_kickoff "❯ "

    run bash "$START_SH" worker --name KickoffTest code

    [ "$status" -eq 0 ]
    [[ "$output" == *"Kickoff message sent to KickoffTest-worker (verified)"* ]]
    [[ "$output" != *"WARNING: kickoff message not confirmed"* ]]
    [[ "$output" != *"ERROR: kickoff message did NOT land"* ]]

    # Exactly one kickoff send: one "send-keys ... ミッション開始" line.
    count="$(grep -cF -- "ミッション開始" "$FAKE_TMUX_LOG" || true)"
    [ "$count" -eq 1 ]
}

@test "kickoff stuck in the input line every time: retries 3x then reports failure explicitly" {
    # capture-pane always echoes the kickoff text back on a "❯ " line —
    # simulates pane_run/send-keys never actually submitting it (Enter dropped
    # or spawn not yet accepting input), so verify-sent always says "stuck".
    setup_fake_tmux_kickoff "❯ ミッション開始。plan pull --agent KickoffTest --skills code でタスクを取得し"

    run bash "$START_SH" worker --name KickoffTest code

    [ "$status" -eq 0 ]
    # Never claims success ("送ったつもり" を出さない).
    [[ "$output" != *"(verified)"* ]]
    [[ "$output" == *"ERROR: kickoff message did NOT land in KickoffTest-worker after 3 attempts"* ]]
    # Warned once per attempt (3 total — bounded retry, no 4th attempt).
    warn_count="$(grep -c "WARNING: kickoff message not confirmed" <<< "$output" || true)"
    [ "$warn_count" -eq 3 ]

    # Exactly 3 kickoff send attempts — bounded retry, no infinite loop.
    count="$(grep -cF -- "ミッション開始" "$FAKE_TMUX_LOG" || true)"
    [ "$count" -eq 3 ]
}
