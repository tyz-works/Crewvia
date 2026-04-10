#!/usr/bin/env bash
set -euo pipefail

# start.sh — Launch a crewvia agent (Orchestrator or Worker)
# Usage:
#   ./scripts/start.sh orchestrator               # Start as Orchestrator
#   ./scripts/start.sh worker [skill1 skill2 ...]  # Start as Worker with given skills

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 orchestrator | worker [skill1 skill2 ...]" >&2
  exit 1
fi

ROLE="$1"
shift || true

case "$ROLE" in
  orchestrator)
    AGENT_FILE="agents/orchestrator.md"
    SKILLS=()
    ;;
  worker)
    AGENT_FILE="agents/worker.md"
    SKILLS=("$@")
    ;;
  *)
    echo "ERROR: Unknown role '$ROLE'. Use 'orchestrator' or 'worker'." >&2
    exit 1
    ;;
esac

# Determine AGENT_NAME via assign-name.sh
if [[ "${ROLE}" == "orchestrator" ]]; then
  AGENT_NAME=$(bash "${SCRIPT_DIR}/assign-name.sh")
else
  AGENT_NAME=$(bash "${SCRIPT_DIR}/assign-name.sh" "${SKILLS[@]+"${SKILLS[@]}"}")
fi

export AGENT_NAME
export TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"

echo "[crewvia] Starting as $AGENT_NAME ($ROLE)"

# Check for TASKVIA_TOKEN
if [[ -z "${TASKVIA_TOKEN:-}" ]]; then
  echo "[crewvia] WARNING: TASKVIA_TOKEN is not set. Running in standalone mode (no Taskvia integration)." >&2
fi

# Resolve agent markdown path
AGENT_MD="${REPO_ROOT}/${AGENT_FILE}"
if [[ ! -f "$AGENT_MD" ]]; then
  echo "[crewvia] WARNING: Agent file not found: $AGENT_MD. Running without system prompt." >&2
  AGENT_MD=""
fi

# Build the --append-system-prompt flag value
SYSTEM_PROMPT_FLAG=()
if [[ -n "$AGENT_MD" ]]; then
  SYSTEM_PROMPT_FLAG=(--append-system-prompt "$(cat "$AGENT_MD")")
fi

# Launch with or without tmux
if [[ "${CREWVIA_TMUX:-0}" == "1" ]]; then
  SESSION="crewvia"
  WINDOW_NAME="${AGENT_NAME}-${ROLE}"

  # Write a bootstrap env file so tmux windows pick up env vars
  ENV_EXPORTS="export AGENT_NAME='$AGENT_NAME' TASKVIA_URL='$TASKVIA_URL' ROLE='$ROLE'"
  [[ -n "${TASKVIA_TOKEN:-}" ]] && ENV_EXPORTS+=" TASKVIA_TOKEN='$TASKVIA_TOKEN'"

  LAUNCH_CMD="$ENV_EXPORTS; cd '$REPO_ROOT'; claude${AGENT_MD:+ --append-system-prompt \"\$(cat '$AGENT_MD')\"}"

  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[crewvia] Creating tmux session: $SESSION"
    tmux new-session -d -s "$SESSION" -n "$WINDOW_NAME"
    TARGET="${SESSION}:${WINDOW_NAME}"
  else
    tmux new-window -t "$SESSION" -n "$WINDOW_NAME"
    TARGET="${SESSION}:${WINDOW_NAME}"
  fi

  tmux send-keys -t "$TARGET" "$LAUNCH_CMD"
  tmux send-keys -t "$TARGET" Enter
  echo "[crewvia] Agent launched in tmux window: ${SESSION}:${WINDOW_NAME}"
else
  # Default: run inline (no tmux)
  exec claude "${SYSTEM_PROMPT_FLAG[@]}"
fi
