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

SESSION="${TMUX_SESSION:-crewvia}"
WINDOW_NAME="plan-reviewer-$$"

tmux new-window -t "$SESSION" -n "$WINDOW_NAME" \
    "cd '$CREWVIA_DIR' && CLAUDE_SKILL=plan_review claude --model claude-opus-4-5 \
     -p 'Mission slug: $SLUG. agents/plan_reviewer.md の手順に従い queue/missions/$SLUG/ の全タスクを検査し、queue/missions/$SLUG/plan_review.md を出力せよ。' \
     2>&1 | tee /tmp/plan_reviewer_$$.log; tmux kill-window" 2>/dev/null || {
    echo "[review-plan.sh] WARNING: tmux session '$SESSION' not found — running Plan Reviewer inline" >&2
    cd "$CREWVIA_DIR"
    CLAUDE_SKILL=plan_review claude --model claude-opus-4-5 \
        -p "Mission slug: $SLUG. agents/plan_reviewer.md の手順に従い queue/missions/$SLUG/ の全タスクを検査し、queue/missions/$SLUG/plan_review.md を出力せよ。" \
        2>&1 | tee /tmp/plan_reviewer_$$.log || true
}

# Wait up to 600s for plan_review.md
echo "[review-plan.sh] Waiting for plan_review.md (max 600s)..."
for i in $(seq 1 120); do
    if [[ -f "$REVIEW_OUTPUT" ]]; then
        echo "[review-plan.sh] plan_review.md output complete"
        exit 0
    fi
    sleep 5
done

echo "[review-plan.sh] Timeout: plan_review.md not produced within 600s" >&2
exit 1
