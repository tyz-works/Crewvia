#!/usr/bin/env bats
# tests/start-sh-trust-precheck.bats
#
# t021 (mission 20260927-mechanize-guards-b / B6, backlog #28): 信頼されていない cwd で claude を起動すると
# 「Do you trust this folder?」(既定 "No, exit") が出て、start.sh の kickoff の Enter がそれを選んで
# claude が即終了し、残りの文字列がシェルに落ちた。しかも start.sh は `❯` を入力行の目印として見るので
# ダイアログの選択カーソルにも反応し、「Kickoff message sent (verified)」と言っていた。
#
# 2 枚の防御を start.sh 経由で確かめる:
#   1. 事前検査 — claude を起動する前に ~/.claude.json を読み、cwd (か祖先) が信頼されていなければ止める。
#      拒否は端末とログ (logs/start-sh/refusals.log) の両方に出す。**~/.claude.json は書き換えない**。
#      読めない・形が違うときも止める (「読めない」を「信頼済み」に潰さない)
#   2. 最後の網 — 事前検査をすり抜けても、pane の画面にダイアログの文言があれば kickoff を送らず、
#      「verified」と言わず、非 0 で止める
# 判定そのものの表 (祖先・symlink・NFC・形の崩れ) は tests/test_trust_precheck.py。
#
# 実 start.sh + 実 lib_trust.py + fake tmux。実 tmux / herdr / 本物の ~/.claude.json には触れない
# (CLAUDE_CONFIG_DIR を使い捨ての dir に向ける)。
# **start.sh は実 checkout では走らせない** (start-sh-registry-skills.bats と同じ理由): 作業ツリーを
# 使い捨ての複製にして、そこの registry / logs に書く。teardown が消すのはその複製だけ。
#
# Run: bats tests/start-sh-trust-precheck.bats

REAL_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
source "$(dirname "$BATS_TEST_FILENAME")/trust_fixture.sh"

_real_footprint() {
    local f
    for f in .claude/settings.local.json registry/workers.yaml; do
        if [[ -e "${REAL_ROOT}/${f}" ]]; then
            echo "${f} $(stat -c '%s %Y' "${REAL_ROOT}/${f}")"
        else
            echo "${f} absent"
        fi
    done
    if [[ -d "${REAL_ROOT}/registry/mux" ]]; then
        ls -A "${REAL_ROOT}/registry/mux" | sort
    fi
    if [[ -d "${REAL_ROOT}/logs/start-sh" ]]; then
        ls -A "${REAL_ROOT}/logs/start-sh" | sort
    fi
}

setup() {
    REAL_FOOTPRINT_BEFORE="$(_real_footprint)"

    SANDBOX="$(mktemp -d)"
    if git -C "$REAL_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        ( cd "$REAL_ROOT" && git ls-files -z --cached --others --exclude-standard \
            | tar --null --ignore-failed-read -T - -cf - 2>/dev/null ) \
            | tar -xf - -C "$SANDBOX"
    else
        ( cd "$REAL_ROOT" && tar --exclude=./.git --exclude=./.claude/worktrees -cf - . ) \
            | tar -xf - -C "$SANDBOX"
    fi
    REPO_ROOT="$SANDBOX"
    START_SH="${REPO_ROOT}/scripts/start.sh"
    REGISTRY="${REPO_ROOT}/registry/workers.yaml"
    REFUSALS="${REPO_ROOT}/logs/start-sh/refusals.log"
    [[ -f "$START_SH" ]]

    mkdir -p "${REPO_ROOT}/registry"
    printf '%s\n' \
        '# registry/workers.yaml (bats fixture)' \
        '' \
        'workers:' \
        '  - name: Ren' \
        '    skills: [code, python]' \
        '    task_count: 7' \
        '    last_active: 2026-09-01' \
        > "$REGISTRY"

    # 信頼されていない dir (使い捨て)。sandbox の外に置く: sandbox 自身も未信頼にできるように。
    TARGET="$(mktemp -d)"

    FAKE_DIR="$(mktemp -d)"
    FAKE_TMUX_LOG="${FAKE_DIR}/tmux_calls.log"
    FAKE_CAPTURES="${FAKE_DIR}/capture_count"
    FAKE_DIALOG="${FAKE_DIR}/dialog.txt"
    touch "$FAKE_TMUX_LOG"
    echo 0 > "$FAKE_CAPTURES"
    # claude 2.1.283 の trust ダイアログ (選択カーソルが `❯` で出る)。
    printf '%s\n' \
        ' Accessing workspace:' '' ' /home/user/newproj' '' \
        ' Quick safety check: Is this a project you created or one you trust?' '' \
        ' ❯ 1. Yes, I trust this folder' \
        '   2. No, exit' '' \
        ' Enter to confirm · Esc to cancel' > "$FAKE_DIALOG"

    cat > "${FAKE_DIR}/tmux" <<'FAKESCRIPT'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
cmd="${1:-}"
case "$cmd" in
  has-session) exit 0 ;;
  list-sessions) echo "crewvia: 1 windows"; exit 0 ;;
  new-session|new-window)
    for arg in "$@"; do case "$arg" in '#{window_id}'*) echo "@1" ;; esac; done
    exit 0 ;;
  list-windows) exit 0 ;;
  send-keys) exit 0 ;;
  kill-window) exit 0 ;;
  capture-pane)
    # FAKE_DIALOG_AFTER=N: N 回目までの capture は普通の入力行、それ以降は trust ダイアログ
    # (0 なら最初から)。unset なら常に普通の入力行。
    # FAKE_CAPTURE_EMPTY_AFTER=N: N 回目以降は出力なし (空) を返す — capture の失敗／画面が
    # 本当に空、のどちらも区別できない状態を模す (t051 P2)。両方は同時に使わない。
    if [[ -n "${FAKE_DIALOG_AFTER:-}" ]]; then
      n=$(cat "$FAKE_CAPTURES"); echo $((n + 1)) > "$FAKE_CAPTURES"
      if [[ $n -ge $FAKE_DIALOG_AFTER ]]; then cat "$FAKE_DIALOG"; exit 0; fi
    elif [[ -n "${FAKE_CAPTURE_EMPTY_AFTER:-}" ]]; then
      n=$(cat "$FAKE_CAPTURES"); echo $((n + 1)) > "$FAKE_CAPTURES"
      if [[ $n -ge $FAKE_CAPTURE_EMPTY_AFTER ]]; then exit 0; fi
    fi
    echo "❯ "; exit 0 ;;
  display-message)
    fmt="${!#}"
    fmt="${fmt//'#{window_id}'/@1}"
    fmt="${fmt//'#{pane_pid}'/999999}"
    fmt="${fmt//'#{pid}'/900}"
    fmt="${fmt//'#{socket_path}'//tmp/tmux-fake/default}"
    echo "$fmt"
    exit 0 ;;
  *) exit 1 ;;
esac
FAKESCRIPT
    # インライン (exec claude) に転落した場合の記録。実 claude は起動させない。
    cat > "${FAKE_DIR}/claude" <<'FAKESCRIPT'
#!/usr/bin/env bash
echo "FAKE_CLAUDE_INVOKED cwd=$PWD" >> "$FAKE_CLAUDE_LOG"
exit 0
FAKESCRIPT
    chmod +x "${FAKE_DIR}/tmux" "${FAKE_DIR}/claude"
    FAKE_CLAUDE_LOG="${FAKE_DIR}/claude_calls.log"
    touch "$FAKE_CLAUDE_LOG"

    export FAKE_TMUX_LOG FAKE_CAPTURES FAKE_DIALOG FAKE_CLAUDE_LOG
    export PATH="${FAKE_DIR}:${PATH}"
    export CREWVIA_TASKVIA=disabled
    export CREWVIA_MUX=tmux
    unset CREWVIA_MUX_ENABLED CREWVIA_BENCH_MODE CREWVIA_TMUX_SESSION AGENT_NAME TARGET_DIR FAKE_DIALOG_AFTER FAKE_CAPTURE_EMPTY_AFTER
}

teardown() {
    trust_fixture_teardown
    local d
    for d in "${FAKE_DIR:-}" "${TARGET:-}"; do
        if [[ -n "$d" && -d "$d" ]]; then
            find "$d" -depth -delete 2>/dev/null || true
        fi
    done
    if [[ -n "${SANDBOX:-}" && "$SANDBOX" != "$REAL_ROOT" && -d "$SANDBOX" ]]; then
        find "$SANDBOX" -depth -delete 2>/dev/null || true
    fi
    # どのテストも、開発者の checkout (registry / settings.local.json / logs) を 1 バイトも変えていない。
    [ "$(_real_footprint)" = "$REAL_FOOTPRINT_BEFORE" ]
}

_config_sum() { sha256sum "${CLAUDE_CONFIG_DIR}/.claude.json"; }
_launched()   { grep -qE '^(new-window|new-session)' "$FAKE_TMUX_LOG"; }
_kickoffs()   { grep -cF -- "ミッション開始" "$FAKE_TMUX_LOG" || true; }

# ---------------------------------------------------------------------------
# 1. 事前検査
# ---------------------------------------------------------------------------

@test "an untrusted TARGET_DIR is refused before claude is launched" {
    trust_fixture_setup /nowhere/near/this
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" == *"claude に信頼されていない"* ]]
    [[ "$output" == *"$TARGET"* ]]
    # mux には何も触れていない: 窓も作らず、キーも送らない。
    ! _launched
    ! grep -q 'send-keys' "$FAKE_TMUX_LOG"
    [ "$(_kickoffs)" -eq 0 ]
    [[ "$output" != *"Agent launched"* ]]
    [[ "$output" != *"(verified)"* ]]
}

@test "the refusal tells the user what to type, and does not edit ~/.claude.json" {
    trust_fixture_setup /nowhere/near/this
    before="$(_config_sum)"
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" == *"! cd $TARGET && claude"* ]]
    [[ "$output" == *"jq --arg k $TARGET"* ]]
    [[ "$output" == *"書き換えません"* ]]
    [ "$(_config_sum)" = "$before" ]
}

@test "the refusal is written to the log file, not only the terminal (backlog #18)" {
    trust_fixture_setup /nowhere/near/this
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [ -f "$REFUSALS" ]
    [ "$(wc -l < "$REFUSALS")" -eq 1 ]                    # 1 拒否 = 1 行 (複数行の説明は畳む)
    grep -q ' REFUSED kind=trust role=worker agent=Ren ' "$REFUSALS"
    grep -qF "dir=${TARGET} " "$REFUSALS"
    grep -q '信頼されていない' "$REFUSALS"
    grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+[+-][0-9]{4} ' "$REFUSALS"
}

@test "a refused launch leaves nothing behind: no settings in the target, registry untouched" {
    trust_fixture_setup /nowhere/near/this
    reg_before="$(sha256sum "$REGISTRY")"
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code python bash

    [ "$status" -eq 1 ]
    [ -z "$(ls -A "$TARGET")" ]                            # crewvia-worker-Ren.json も settings.local.json も無い
    [ "$(sha256sum "$REGISTRY")" = "$reg_before" ]         # last_active / skills の更新も、起動してから
    [ ! -e "${REPO_ROOT}/registry/workers/Ren/target_dir.json" ]
}

@test "crewvia itself (no TARGET_DIR) is checked too" {
    trust_fixture_setup /nowhere/near/this
    run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" == *"$(realpath "$REPO_ROOT")"* ]] || [[ "$output" == *"$REPO_ROOT"* ]]
    ! _launched
    grep -q ' REFUSED kind=trust role=worker agent=Ren ' "$REFUSALS"
}

@test "a TARGET_DIR recorded as trusted launches" {
    trust_fixture_setup "$TARGET"
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 0 ]
    _launched
    [ "$(_kickoffs)" -eq 1 ]
    [[ "$output" == *"Kickoff message sent to Ren-worker (verified)"* ]]
    [ ! -e "$REFUSALS" ]
}

@test "a TARGET_DIR under a trusted ancestor launches (the ancestor's trust is inherited)" {
    trust_fixture_setup "$(dirname "$TARGET")"
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 0 ]
    _launched
    [ ! -e "$REFUSALS" ]
}

@test "a config that cannot be parsed is refused, never read as trusted" {
    trust_fixture_setup "$TARGET"                          # 信頼済みのはずの dir でも、設定が壊れていれば止める
    printf '{"projects": {broken' > "${CLAUDE_CONFIG_DIR}/.claude.json"
    before="$(_config_sum)"
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" == *"確認できない"* ]]
    [[ "$output" == *"${CLAUDE_CONFIG_DIR}/.claude.json"* ]]
    ! _launched
    grep -q ' REFUSED kind=trust ' "$REFUSALS"
    [ "$(_config_sum)" = "$before" ]
}

@test "a config of the wrong shape is refused, never read as trusted" {
    trust_fixture_setup "$TARGET"
    printf '{"projects": []}' > "${CLAUDE_CONFIG_DIR}/.claude.json"
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    ! _launched
}

@test "a missing config means nothing is trusted: refused, with the claude-based way to trust" {
    trust_fixture_setup /nowhere/near/this
    find "$CLAUDE_CONFIG_DIR" -name .claude.json -delete
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" == *"存在しません"* ]]
    [[ "$output" == *"! cd $TARGET && claude"* ]]
    [[ "$output" != *"jq --arg"* ]]                       # 無いファイルに jq で書かせない
    ! _launched
    [ ! -e "${CLAUDE_CONFIG_DIR}/.claude.json" ]           # 作りもしない
}

@test "without CLAUDE_CONFIG_DIR the config is HOME/.claude.json (a fake HOME, never the real one)" {
    # 偽 HOME に「TARGET を信頼する」設定を置く。CLAUDE_CONFIG_DIR は外す。python の user site は本物の HOME
    # から引き続き見えるように PYTHONUSERBASE を渡す (HOME を変えると ~/.local の yaml を見失う)。
    unset CLAUDE_CONFIG_DIR
    FAKE_HOME="$(mktemp -d)"
    printf '{"projects": {"%s": {"hasTrustDialogAccepted": true}}}' "$TARGET" > "${FAKE_HOME}/.claude.json"
    PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" HOME="$FAKE_HOME" TARGET_DIR="$TARGET" \
        run bash "$START_SH" worker --name Ren code
    [ "$status" -eq 0 ]
    _launched

    # 同じ偽 HOME で、信頼されていない dir は止まる。
    OTHER="$(mktemp -d)"
    PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" HOME="$FAKE_HOME" TARGET_DIR="$OTHER" \
        run bash "$START_SH" worker --name Ren code
    [ "$status" -eq 1 ]
    [[ "$output" == *"${FAKE_HOME}/.claude.json"* ]]
    find "$FAKE_HOME" "$OTHER" -depth -delete 2>/dev/null || true
}

@test "inline mode is not gated: the user answers the dialog in their own terminal" {
    trust_fixture_setup /nowhere/near/this
    CREWVIA_MUX_ENABLED=0 TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 0 ]
    [[ "$output" != *"信頼されていない"* ]]
    grep -q 'FAKE_CLAUDE_INVOKED' "$FAKE_CLAUDE_LOG"
    [ ! -e "$REFUSALS" ]
}

# ---------------------------------------------------------------------------
# 1b. precheck が読んだ設定と、spawn 先が読む設定が一致すること (t051 P1)
# ---------------------------------------------------------------------------

@test "CLAUDE_CONFIG_DIR read by the precheck is forwarded to the spawned claude (t051 P1)" {
    trust_fixture_setup "$TARGET"
    CFG="$CLAUDE_CONFIG_DIR"
    TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 0 ]
    _launched
    grep -qF "CLAUDE_CONFIG_DIR='${CFG}'" "$FAKE_TMUX_LOG"
}

@test "without CLAUDE_CONFIG_DIR, the launch command clears any stale value the pane's server env might hold (t051 P1)" {
    unset CLAUDE_CONFIG_DIR
    FAKE_HOME="$(mktemp -d)"
    printf '{"projects": {"%s": {"hasTrustDialogAccepted": true}}}' "$TARGET" > "${FAKE_HOME}/.claude.json"
    PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" HOME="$FAKE_HOME" TARGET_DIR="$TARGET" \
        run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 0 ]
    _launched
    grep -q 'unset CLAUDE_CONFIG_DIR' "$FAKE_TMUX_LOG"
    ! grep -q "CLAUDE_CONFIG_DIR='" "$FAKE_TMUX_LOG"
    find "$FAKE_HOME" -depth -delete 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 2. 最後の網: 事前検査をすり抜けてダイアログが出てしまった場合
# ---------------------------------------------------------------------------

@test "last net: a trust dialog on the pane stops the launch — no kickoff, not 'verified', non-zero" {
    trust_fixture_setup                                    # 事前検査は通る (git worktree・継承規則の未知など)
    FAKE_DIALOG_AFTER=0 TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" == *"trust ダイアログで止まっています"* ]]
    [[ "$output" != *"(verified)"* ]]
    [[ "$output" != *"Kickoff message sent"* ]]
    # kickoff の本文 (= Enter を伴う入力) はダイアログへ 1 度も送られていない。
    [ "$(_kickoffs)" -eq 0 ]
    # 窓は片付ける (dispatcher の割り当て + Enter がダイアログに届かないように)。
    grep -q '^kill-window' "$FAKE_TMUX_LOG"
    grep -q ' REFUSED kind=trust-dialog role=worker agent=Ren ' "$REFUSALS"
}

@test "last net: the dialog cursor '❯' does not count as the Claude prompt" {
    # 元の症状の再現: 画面は `❯ 1. Yes, I trust this folder` — `❯` grep だけなら「準備完了」に見える。
    trust_fixture_setup
    FAKE_DIALOG_AFTER=0 run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" != *"WARNING: Claude prompt not detected"* ]]   # 待ち続けた (30 秒) のではなく、見つけて止めた
    [ "$(_kickoffs)" -eq 0 ]
}

@test "last net: a dialog that shows up after the prompt was seen is still caught before the send" {
    trust_fixture_setup
    # 1 回目 (ダイアログ検出) と 2 回目 (`❯` の待機) は普通の入力行、3 回目 (送信直前の検出) からダイアログ。
    FAKE_DIALOG_AFTER=2 run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" == *"kickoff 送信前"* ]]
    [ "$(_kickoffs)" -eq 0 ]
}

@test "last net: a dialog still on screen after the send is not reported as verified" {
    trust_fixture_setup
    # 送信前の検出まで (1〜3 回目) は普通の入力行。送った後の capture からダイアログ。
    FAKE_DIALOG_AFTER=3 run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" != *"(verified)"* ]]
    [[ "$output" == *"trust ダイアログで止まっています"* ]]
}

@test "last net: an ordinary screen passes through and the kickoff is verified as before" {
    trust_fixture_setup
    run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 0 ]
    [[ "$output" == *"Kickoff message sent to Ren-worker (verified)"* ]]
    [ "$(_kickoffs)" -eq 1 ]
    ! grep -q '^kill-window' "$FAKE_TMUX_LOG"
    [ ! -e "$REFUSALS" ]
}

@test "last net: a screen that cannot be captured is never treated as 'no dialog' — refused before send (t051 P2)" {
    trust_fixture_setup
    # capture-pane が (delivery 失敗か画面が本当に空かの区別なく) 空を返し続ける状況を模す。
    FAKE_CAPTURE_EMPTY_AFTER=2 TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 1 ]
    [[ "$output" == *"確認できません"* ]]
    [[ "$output" != *"(verified)"* ]]
    [[ "$output" != *"Kickoff message sent"* ]]
    [ "$(_kickoffs)" -eq 0 ]
    grep -q '^kill-window' "$FAKE_TMUX_LOG"
    grep -q ' REFUSED kind=trust-dialog-unobservable role=worker agent=Ren ' "$REFUSALS"
}

@test "bench mode is not gated by the last-net trust dialog check in the prompt-wait loop either (t051 P2x2)" {
    trust_fixture_setup
    # プロンプト待ちループが最初から trust ダイアログの画面を見る状況 (precheck 自体は bench mode
    # なので既に対象外)。この網も bench mode を対象外にしないと、bench controller が処理する前に
    # start.sh がこの pane を kill して exit 1 してしまう。
    FAKE_DIALOG_AFTER=0 CREWVIA_BENCH_MODE=1 TARGET_DIR="$TARGET" run bash "$START_SH" worker --name Ren code

    [ "$status" -eq 0 ]
    _launched
    [[ "$output" == *"BENCH_MODE: skipping auto-kickoff"* ]]
    [[ "$output" != *"trust ダイアログで止まっています"* ]]
    ! grep -q '^kill-window' "$FAKE_TMUX_LOG"
    [ ! -e "$REFUSALS" ]
}
