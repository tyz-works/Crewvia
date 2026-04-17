#!/usr/bin/env bash
# PreCompact hook — saves task state before context compaction

set -euo pipefail

CREWVIA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Read JSON payload from stdin
payload="$(cat)"
trigger="$(echo "$payload" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('trigger','unknown'))" 2>/dev/null || echo "unknown")"
custom_instructions="$(echo "$payload" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('custom_instructions',''))" 2>/dev/null || echo "")"

# Resolve task ID from environment
task_id="${CREWVIA_TASK_ID:-${CLAUDE_TASK_ID:-}}"
agent="${CREWVIA_AGENT_NAME:-unknown}"
timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# Build snapshot content
snapshot="## Pre-Compact Snapshot

- trigger: ${trigger}
- agent: ${agent}
- task_id: ${task_id:-none}
- timestamp: ${timestamp}"

if [[ "$trigger" == "manual" && -n "$custom_instructions" ]]; then
    snapshot="${snapshot}
- custom_instructions: ${custom_instructions}"
fi

snapshot="${snapshot}

> Worker: compaction 後の resume 時はこのセクションを読んで作業を再開すること。
"

# Find task file if task_id is set
task_file=""
if [[ -n "$task_id" ]]; then
    task_file="$(find "${CREWVIA_ROOT}/queue/missions" -path "*/tasks/${task_id}.md" 2>/dev/null | head -1)"
fi

if [[ -n "$task_file" && -f "$task_file" ]]; then
    # Update or append "## Pre-Compact Snapshot" section using python3
    python3 - "$task_file" "$snapshot" <<'PYEOF' || true
import sys, re

task_file = sys.argv[1]
new_section = sys.argv[2]

with open(task_file, 'r') as f:
    content = f.read()

pattern = r'## Pre-Compact Snapshot\n[\s\S]*?(?=\n## |\Z)'
if re.search(pattern, content):
    updated = re.sub(pattern, new_section.rstrip(), content)
else:
    updated = content.rstrip('\n') + '\n\n' + new_section

with open(task_file, 'w') as f:
    f.write(updated)
PYEOF
else
    # Fallback: log to pre-compact-fallback.log
    fallback_log="${CREWVIA_ROOT}/queue/pre-compact-fallback.log"
    printf '[%s] trigger=%s agent=%s task_id=%s (task file not found)\n' \
        "$timestamp" "$trigger" "$agent" "${task_id:-none}" >> "$fallback_log"
fi

exit 0
