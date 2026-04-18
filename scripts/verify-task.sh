#!/usr/bin/env bash
# verify-task.sh <task_id> [queue_dir]
# Runs verification checks for a Crewvia task defined in its frontmatter.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TASK_ID="${1:-}"
QUEUE_DIR="${2:-$SCRIPT_DIR/../queue}"

if [[ -z "$TASK_ID" ]]; then
    echo "Usage: verify-task.sh <task_id> [queue_dir]" >&2
    exit 1
fi

exec python3 "$SCRIPT_DIR/run_verification_checks.py" "$TASK_ID" "$QUEUE_DIR"
