#!/usr/bin/env bash
# review-plan.sh <slug>
# Launches Plan Reviewer in a separate tmux window and waits for plan_review.md output.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CREWVIA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SLUG="${1:-}"

if [[ -z "$SLUG" ]]; then
    echo "Usage: review-plan.sh <slug>" >&2
    exit 1
fi

MISSION_DIR="$CREWVIA_DIR/queue/missions/$SLUG"
REVIEW_OUTPUT="$MISSION_DIR/plan_review.md"

if [[ ! -d "$MISSION_DIR" ]]; then
    echo "Mission not found: $SLUG" >&2
    exit 1
fi

WINDOW_NAME="plan-reviewer-$$"

# Use lib_mux.sh for mux-backend-agnostic window management.
# shellcheck source=lib_mux.sh
source "${SCRIPT_DIR}/lib_mux.sh"

# --- t002 (mission 20260908-launch-reliability) パターン3対策 ---
# レビュー開始前に既存の plan_review.md を削除する。削除しないと、前 cycle の
# 古いファイルが残っている場合に下の待機ループが即座に「有効な verdict あり」
# と誤認し、古い判定をそのまま新しい判定として採用してしまう (本ミッション
# 自身の cycle 2 で実際に発生。誤った revise が cycle 1 と同一内容のまま返り、
# cycle_count だけを消費した)。
#
# REVIEW_START_EPOCH は、万一 rm が何らかの理由で効かなかった場合の二重の
# 安全策として scripts/wait_for_plan_review.sh に渡す — mtime が
# レビュー開始時刻より新しいファイルだけを「今回の実行の出力」とみなす。
rm -f "$REVIEW_OUTPUT"
REVIEW_START_EPOCH=$(date +%s)

# SKILLS=plan_review: config/skill-permissions.yaml の plan_review セクション
# (Bash 全面禁止 / Edit・MultiEdit 禁止 / Write は plan_review.md 限定) が
# 実際に適用されるようにするための必須設定。
#
# 発見した副次バグ (t002): hooks/pre-tool-use.sh の per-skill チェックは
# `SKILLS` 環境変数が設定されている場合にしか動かない。旧版はここで
# CLAUDE_SKILL=plan_review しか設定しておらず、hook はその変数を一切読まない
# ため、plan-reviewer セッションは skill-permissions.yaml の plan_review
# セクションを完全にバイパスして動いていた (Bash も Edit も無制限)。
# 実際に mission.yaml が書き換えられた事故は、この export 漏れが真因の一つ。
# CLAUDE_SKILL は claude CLI 自体には影響しないが、ログ上の識別用に残す。
#
# 注意: SKILLS=plan_review を有効にすると Bash が完全に deny されるため、
# agents/plan_reviewer.md 側も「Step 1 で `ls` (Bash) を使う」という旧来の
# 手順を Glob ツールに置き換えてある (Bash 前提の手順のままだと reviewer が
# Step 1 から動けなくなる)。
#
# AGENT_NAME='Plan-Reviewer' (t008, PR#188 レビュー指摘 P1・Codex 指摘):
# review-plan.sh は Director セッションの子プロセスとして起動されるため、
# ここで AGENT_NAME を上書きしないと Director の AGENT_NAME (registry で
# role: director) がそのまま継承される。hooks/pre-tool-use.sh の Director
# bypass (role: director を見て即 allow) は SKILLS=plan_review の per-skill
# チェックより前に評価されるため、上書きしないと reviewer セッションは
# 「登録済み Director」として全ツールが即通過し、上の SKILLS=plan_review が
# 実質 dead code になっていた (mission.yaml を書き換えられる事故の未解決分)。
# registry/workers.yaml に登録の無い名前を使うことで role: director 判定に
# 一致させず、SKILLS ベースの per-skill チェックを必ず通過させる
# (scripts/kai-review.sh / scripts/start.sh と同じ「非 Director identity で
# 起動する」パターン)。
#
# unset CLAUDE_CODE_CHILD_SESSION: herdr server 由来の汚染変数が Plan Reviewer に伝播しないよう除去。
# CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1: 二重防御として transcript 保存を公式 env var で保証 (→ t004)。
INLINE_CMD="unset CLAUDE_CODE_CHILD_SESSION; export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1; export AGENT_NAME='Plan-Reviewer'; export SKILLS=plan_review; cd '$CREWVIA_DIR' && CLAUDE_SKILL=plan_review claude --model claude-opus-4-5 \
     -p 'Mission slug: $SLUG. agents/plan_reviewer.md の手順に従い queue/missions/$SLUG/ の全タスクを検査し、queue/missions/$SLUG/plan_review.md を出力せよ。' \
     2>&1 | tee /tmp/plan_reviewer_$$.log"

MUX_LAUNCHED=0
# F4 (PR#188 t012 Seo 指摘): 以前は成功時とタイムアウト時の2箇所に明示的な
# mux_kill を置いていたが、それでは Director の Ctrl-C (SIGINT) / SIGTERM /
# spawn 後〜末尾の間で set -e により中断した場合の経路をカバーできず、
# plan-reviewer の pane が残り続けていた。EXIT/INT/TERM の trap 1箇所に
# 集約することで、スクリプトがどう終わっても (正常終了・タイムアウト・
# 中断) 必ず1回だけ kill されるようにする (成功時・タイムアウト時の個別
# kill は不要になったため削除)。
trap '[[ $MUX_LAUNCHED -eq 1 ]] && mux_kill "$WINDOW_NAME" 2>/dev/null || true' EXIT INT TERM

if mux_available && mux_spawn "$WINDOW_NAME" "$INLINE_CMD" "$CREWVIA_DIR"; then
    MUX_LAUNCHED=1
else
    echo "[review-plan.sh] WARNING: mux unavailable or spawn failed — running Plan Reviewer inline" >&2
    cd "$CREWVIA_DIR"
    # herdr server 汚染の伝播を防ぐため claude 実行直前に除去する。
    unset CLAUDE_CODE_CHILD_SESSION
    # 二重防御: transcript 保存を公式 env var で保証する。
    export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1
    # 上の INLINE_CMD と同じ理由 (Director identity 継承による
    # skill-permissions.yaml バイパスを防ぐため)。
    export AGENT_NAME='Plan-Reviewer'
    # 上の INLINE_CMD と同じ理由 (skill-permissions.yaml の plan_review 制限を
    # 実際に適用するため)。
    export SKILLS=plan_review
    CLAUDE_SKILL=plan_review claude --model claude-opus-4-5 \
        -p "Mission slug: $SLUG. agents/plan_reviewer.md の手順に従い queue/missions/$SLUG/ の全タスクを検査し、queue/missions/$SLUG/plan_review.md を出力せよ。" \
        2>&1 | tee /tmp/plan_reviewer_$$.log
    if [[ $? -ne 0 ]]; then
        echo "[review-plan.sh] ERROR: Plan Reviewer exited with non-zero status" >&2
        exit 1
    fi
fi

# Wait up to 600s for plan_review.md with valid verdict.
# ポーリング判定本体は scripts/wait_for_plan_review.sh に切り出してある —
# claude CLI を spawn しない独立スクリプトにすることで、t002 の3パターン
# (規定形式 / 別表記 / 前 cycle の残骸) を claude を起動せずに回帰テストできる
# (scripts/test_wait_for_plan_review.sh 参照)。
echo "[review-plan.sh] Waiting for plan_review.md with valid verdict (max 600s)..."
set +e
WAIT_OUTPUT="$(bash "${SCRIPT_DIR}/wait_for_plan_review.sh" "$REVIEW_OUTPUT" "$REVIEW_START_EPOCH" 600 5)"
WAIT_RC=$?
set -e
WAIT_STATUS="$(printf '%s\n' "$WAIT_OUTPUT" | head -1)"
# 2行目以降 (人間向けログ) を stderr に転記する。実際のログは
# wait_for_plan_review.sh 自身も stderr に出しているため、ここでは1行目だけ
# 拾えれば十分だが、標準出力に紛れ込んだ場合に備えて残りも表示しておく。
printf '%s\n' "$WAIT_OUTPUT" | tail -n +2 >&2 || true

if [[ "$WAIT_RC" -eq 0 && "$WAIT_STATUS" == "OK" ]]; then
    echo "[review-plan.sh] plan_review.md output complete"
    # kill は上の EXIT trap (F4) が exit 時に自動で行うため、ここでは呼ばない。
    exit 0
fi

# --- t002: タイムアウト時の挙動改善 ---
# plan_review.md 自体は書かれていた場合 (TIMEOUT_FRESH) は「判定が読めなかった
# だけ」であり、内容自体は活かせる可能性が高い。scripts/plan.sh 側 (cmd_review)
# がこのメッセージを拾って Director に「再レビューではなく手動確認」を促す。
if [[ "$WAIT_STATUS" == "TIMEOUT_FRESH" ]]; then
    echo "[review-plan.sh] Timeout: plan_review.md was written during this run but no verdict (standard or recognized alternate wording) could be found. Inspect ${REVIEW_OUTPUT} by hand — the judgement content may still be usable without consuming another review cycle." >&2
else
    echo "[review-plan.sh] Timeout: plan_review.md was not produced within 600s" >&2
fi

# kill は上の EXIT trap (F4) が exit 時に自動で行うため、ここでは呼ばない。
exit 1
