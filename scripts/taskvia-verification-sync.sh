#!/usr/bin/env bash
# taskvia-verification-sync.sh <task_id> [queue_dir]
# Pushes the latest verification result to Taskvia POST /api/verification.
# No-op when TASKVIA_TOKEN is unset or CREWVIA_TASKVIA=disabled.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TASK_ID="${1:-}"
QUEUE_DIR="${2:-$REPO_ROOT/queue}"

TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"

# --- No-op guards ---
if [[ "${CREWVIA_TASKVIA:-}" = "disabled" ]]; then
  exit 0
fi
if [[ -z "$TASKVIA_TOKEN" ]]; then
  exit 0
fi
if [[ -z "$TASK_ID" ]]; then
  echo "[taskvia-verification-sync] ERROR: task_id argument required" >&2
  exit 1
fi

# --- Find latest verification JSON ---
VERIFY_DIR="$REPO_ROOT/registry/verification/$TASK_ID"
if [[ ! -d "$VERIFY_DIR" ]]; then
  echo "[taskvia-verification-sync] No verification dir for ${TASK_ID}, skipping." >&2
  exit 0
fi

LATEST_JSON="$(ls -t "$VERIFY_DIR"/*.json 2>/dev/null | head -1 || true)"
if [[ -z "$LATEST_JSON" ]]; then
  echo "[taskvia-verification-sync] No verification JSON for ${TASK_ID}, skipping." >&2
  exit 0
fi

# --- Resolve mission_slug from task file ---
MISSION_SLUG=""
TASK_FILE="$(find "$QUEUE_DIR/missions" -name "${TASK_ID}.md" 2>/dev/null | head -1 || true)"
if [[ -n "$TASK_FILE" ]]; then
  MISSION_SLUG="$(echo "$TASK_FILE" | sed 's|.*/missions/||; s|/tasks/.*||')"
fi

# --- Resolve verification mode from task frontmatter ---
MODE="standard"
if [[ -n "${TASK_FILE:-}" ]] && [[ -f "$TASK_FILE" ]]; then
  _MODE="$(grep '^mode:' "$TASK_FILE" 2>/dev/null | head -1 | awk '{print $2}' | tr -d '"' || true)"
  [[ -n "${_MODE:-}" ]] && MODE="$_MODE"
fi

VERIFIER="${AGENT_NAME:-${HOSTNAME:-crewvia}}"

# --- Build JSON payload (summary only — no stdout/stderr to keep body small) ---
PAYLOAD="$(python3 - "$LATEST_JSON" "$TASK_ID" "$MISSION_SLUG" "$MODE" "$VERIFIER" <<'PYEOF'
import sys, json

json_path    = sys.argv[1]
task_id      = sys.argv[2]
mission_slug = sys.argv[3]
mode         = sys.argv[4]
verifier     = sys.argv[5]

with open(json_path) as f:
    data = json.load(f)

checks_summary = [
    {"name": c.get("name"), "status": c.get("status"), "duration_s": c.get("duration_s")}
    for c in data.get("checks", [])
]

# cycle 1 = first run = 0 reworks; cycle N = N-1 reworks
rework_count = max(0, data.get("cycle", 1) - 1)

payload = {
    "task_id":      task_id,
    "mission_slug": mission_slug or None,
    "mode":         mode,
    "verdict":      data.get("overall", "fail"),
    "checks":       checks_summary,
    "rework_count": rework_count,
    "verified_at":  data.get("executed_at"),
    "verifier":     verifier,
}
print(json.dumps(payload))
PYEOF
)"

# --- POST to Taskvia (fail-open: never block verify-task.sh) ---
HTTP_RESPONSE="$(curl -sf --connect-timeout 10 --max-time 15 \
  -o /tmp/taskvia_verification_resp.txt -w "%{http_code}" \
  -X POST "${TASKVIA_URL}/api/verification" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
  -d "$PAYLOAD" 2>/dev/null)" || HTTP_RESPONSE="000"

if [[ "$HTTP_RESPONSE" = "200" ]]; then
  echo "[taskvia-verification-sync] ✅ Pushed verification for ${TASK_ID} (verdict=$(echo "$PAYLOAD" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["verdict"])'))" >&2
else
  echo "[taskvia-verification-sync] ⚠️ POST failed (HTTP ${HTTP_RESPONSE}) for ${TASK_ID}. Continuing." >&2
fi

exit 0
