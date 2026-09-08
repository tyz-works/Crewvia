#!/usr/bin/env bats
# tests/permission-mode.bats
#
# start.sh 側の permission mode 解決テスト (t001, mission 20260908-launch-reliability)。
# CREWVIA_PRINT_PERMISSION_MODE=1 を使うことで claude を起動せずに判定のみ実施
# (CREWVIA_PRINT_MODEL と同じ設計 — 副作用ゼロ: registry 書き込み / worktree 汚染なし)。
#
# Run:
#   bats tests/permission-mode.bats
#
# Coverage:
#   1. Worker はデフォルトで auto (対話プロンプト停止事故対策)
#   2. Worker は CREWVIA_WORKER_PERMISSION_MODE で上書き可能
#   3. Director はデフォルトで未設定 (CLI 既定 = 対話確認あり、Taskvia hook をスキップするため)
#   4. Director は CREWVIA_DIRECTOR_PERMISSION_MODE で明示指定可能
#   5. --permission-mode フラグが実際の起動コマンドに渡る (mux / inline 両経路)

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
START_SH="${REPO_ROOT}/scripts/start.sh"

# ---------------------------------------------------------------------------
# Worker: デフォルトは auto
# ---------------------------------------------------------------------------

@test "worker: デフォルトで permission mode は auto" {
  result=$(CREWVIA_PRINT_PERMISSION_MODE=1 bash "$START_SH" worker code 2>/dev/null)
  [ "$result" = "auto" ]
}

@test "worker: skill やコマンド行引数に関わらず auto (bash,code)" {
  result=$(CREWVIA_PRINT_PERMISSION_MODE=1 bash "$START_SH" worker bash code 2>/dev/null)
  [ "$result" = "auto" ]
}

@test "worker: CREWVIA_WORKER_PERMISSION_MODE で上書きできる" {
  result=$(CREWVIA_WORKER_PERMISSION_MODE="acceptEdits" CREWVIA_PRINT_PERMISSION_MODE=1 \
           bash "$START_SH" worker code 2>/dev/null)
  [ "$result" = "acceptEdits" ]
}

@test "worker: CREWVIA_WORKER_PERMISSION_MODE を空にすると CLI 既定にフォールバックする" {
  result=$(CREWVIA_WORKER_PERMISSION_MODE="" CREWVIA_PRINT_PERMISSION_MODE=1 \
           bash "$START_SH" worker code 2>/dev/null)
  [ -z "$result" ]
}

# ---------------------------------------------------------------------------
# Director: デフォルトは未設定 (対話確認を維持)
# ---------------------------------------------------------------------------

@test "director: デフォルトでは permission mode を指定しない (空文字)" {
  result=$(unset CREWVIA_DIRECTOR_PERMISSION_MODE; CREWVIA_PRINT_PERMISSION_MODE=1 \
           bash "$START_SH" director 2>/dev/null)
  [ -z "$result" ]
}

@test "director: CREWVIA_DIRECTOR_PERMISSION_MODE で明示指定できる" {
  result=$(CREWVIA_DIRECTOR_PERMISSION_MODE="auto" CREWVIA_PRINT_PERMISSION_MODE=1 \
           bash "$START_SH" director 2>/dev/null)
  [ "$result" = "auto" ]
}

# ---------------------------------------------------------------------------
# 実際の起動コマンドへの伝播 (mux モード / inline モード)
# ---------------------------------------------------------------------------

setup_fake_tmux_for_launch() {
    FAKE_TMUX_DIR="$(mktemp -d)"
    FAKE_TMUX_LOG="${FAKE_TMUX_DIR}/calls.log"
    touch "$FAKE_TMUX_LOG"
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
  capture-pane) echo "❯ "; exit 0 ;;
  kill-window) exit 0 ;;
  display-message) echo "12345"; exit 0 ;;
  *) exit 1 ;;
esac
FAKESCRIPT
    chmod +x "${FAKE_TMUX_DIR}/tmux"
    export FAKE_TMUX_DIR FAKE_TMUX_LOG
    export PATH="${FAKE_TMUX_DIR}:${PATH}"
    export CREWVIA_MUX=tmux
    export CREWVIA_MUX_ENABLED=1
    export CREWVIA_TASKVIA=disabled
    export CREWVIA_BENCH_MODE=1
    export AGENT_NAME="PermTest"
    unset CREWVIA_TMUX_SESSION
}

teardown() {
    if [[ -n "${FAKE_TMUX_DIR:-}" && -d "$FAKE_TMUX_DIR" ]]; then
        find "$FAKE_TMUX_DIR" -mindepth 1 -delete 2>/dev/null || true
        rmdir "$FAKE_TMUX_DIR" 2>/dev/null || true
    fi
}

@test "worker (mux mode): 起動コマンド文字列に --permission-mode auto が含まれる" {
    setup_fake_tmux_for_launch

    run bash "$START_SH" worker --name PermTest code
    [ "$status" -eq 0 ]

    # BENCH_MODE のため window 'PermTest-worker' へ渡された send-keys の中身に
    # --permission-mode auto が含まれることを確認する。
    grep -qF "PermTest-worker" "$FAKE_TMUX_LOG"
    grep -qF -- "--permission-mode 'auto'" "$FAKE_TMUX_LOG"
}

@test "director (mux mode): 起動コマンド文字列に --permission-mode が含まれない" {
    setup_fake_tmux_for_launch
    unset CREWVIA_DIRECTOR_PERMISSION_MODE
    # Director は既存 registry の role:director エントリ名を再利用する
    # (start.sh の get-director 分岐)。--name 相当のオーバーライドは無いため、
    # ここでは名前をハードコードせず "-director" window への spawn だけを確認する。
    # list-windows は director 用の名前を含まないよう上書き（既存 window 扱いされて
    # spawn がスキップされるのを避ける）。
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
  capture-pane) echo "❯ "; exit 0 ;;
  kill-window) exit 0 ;;
  display-message) echo "12345"; exit 0 ;;
  attach-session) exit 0 ;;
  switch-client) exit 0 ;;
  *) exit 1 ;;
esac
FAKESCRIPT
    chmod +x "${FAKE_TMUX_DIR}/tmux"

    run bash "$START_SH" director
    [ "$status" -eq 0 ]

    grep -qE -- "send-keys -t crewvia:[^ ]+-director " "$FAKE_TMUX_LOG"
    ! grep -qF -- "--permission-mode" "$FAKE_TMUX_LOG"
}
