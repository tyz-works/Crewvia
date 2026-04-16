#!/usr/bin/env bash
# scripts/log_to_obsidian.sh
# Crewvia mission archive を Obsidian の mission_log に出力する。
#
# 使い方:
#   scripts/log_to_obsidian.sh --mission <slug>
#
# 入力: queue/archive/<slug>/mission.yaml + tasks/tNNN.md
# 出力: $OBSIDIAN_VAULT/research/mission_log/YYYY-MM-DD_<slug>.md
#
# 環境変数:
#   OBSIDIAN_VAULT  Obsidian Vault のルート (default: ~/obsidian)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OBSIDIAN_VAULT="${OBSIDIAN_VAULT:-$HOME/obsidian}"

usage() {
  cat <<EOF
Usage: $0 --mission <slug>

Export an archived mission to Obsidian mission_log.

Options:
  --mission <slug>   Mission slug under queue/archive/
  -h, --help         Show this help

Environment:
  OBSIDIAN_VAULT     Obsidian Vault root (default: \$HOME/obsidian)
EOF
}

MISSION_SLUG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mission)
      MISSION_SLUG="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [ -z "$MISSION_SLUG" ]; then
  echo "Error: --mission <slug> is required" >&2
  usage
  exit 1
fi

ARCHIVE_DIR="${REPO_ROOT}/queue/archive/${MISSION_SLUG}"
if [ ! -d "$ARCHIVE_DIR" ]; then
  echo "Error: archive not found: ${ARCHIVE_DIR}" >&2
  exit 1
fi
if [ ! -f "${ARCHIVE_DIR}/mission.yaml" ]; then
  echo "Error: mission.yaml not found in ${ARCHIVE_DIR}" >&2
  exit 1
fi

OUTPUT_DIR="${OBSIDIAN_VAULT}/research/mission_log"
mkdir -p "$OUTPUT_DIR"

python3 - "$ARCHIVE_DIR" "$MISSION_SLUG" "$OUTPUT_DIR" <<'PYEOF'
import re
import sys
from pathlib import Path
from datetime import date

archive_dir = Path(sys.argv[1])
slug = sys.argv[2]
output_dir = Path(sys.argv[3])

def parse_simple_yaml(text):
    """Minimal YAML scalar parser — enough for mission.yaml / task frontmatter."""
    result = {}
    for line in text.splitlines():
        m = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$', line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        elif raw.startswith('[') and raw.endswith(']'):
            inner = raw[1:-1].strip()
            raw = [s.strip() for s in inner.split(',')] if inner else []
        elif raw.lower() in ('null', '~', ''):
            raw = None
        result[key] = raw
    return result

def parse_task(path):
    """Split frontmatter + body. Return (meta_dict, description, result)."""
    text = path.read_text()
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', text, re.DOTALL)
    if not m:
        return {}, '', ''
    meta = parse_simple_yaml(m.group(1))
    body = m.group(2)
    desc_match = re.search(r'## Description\s*\n(.*?)(?=\n## Result|\Z)', body, re.DOTALL)
    result_match = re.search(r'## Result\s*\n(.*?)\Z', body, re.DOTALL)
    description = desc_match.group(1).strip() if desc_match else ''
    result_text = result_match.group(1).strip() if result_match else ''
    return meta, description, result_text

mission = parse_simple_yaml((archive_dir / 'mission.yaml').read_text())
title = mission.get('title', slug)
status = mission.get('status', 'unknown')
created_at = mission.get('created_at', '')
completed_at = mission.get('completed_at', '')

task_files = sorted((archive_dir / 'tasks').glob('t*.md'))
tasks = [parse_task(p) for p in task_files]

today = date.today().isoformat()
status_icon = '✅ DONE' if status == 'done' else '⏳ ' + str(status).upper()

lines = []
lines.append('---')
lines.append(f'title: "{title}"')
lines.append(f'date: "{today}"')
lines.append(f'tags: [mission-log, crewvia, {slug}]')
lines.append(f'status: {status_icon}')
lines.append('---')
lines.append('')
lines.append(f'# {title}')
lines.append('')
lines.append(f'- **Slug**: `{slug}`')
lines.append(f'- **Status**: {status_icon}')
if created_at:
    lines.append(f'- **Created**: {created_at}')
if completed_at:
    lines.append(f'- **Completed**: {completed_at}')
lines.append(f'- **Tasks**: {len(tasks)}')
lines.append('')
lines.append('## タスク結果')
lines.append('')

for meta, description, result_text in tasks:
    tid = meta.get('id', '?')
    ttitle = meta.get('title', '')
    tstatus = meta.get('status', 'unknown')
    worker = meta.get('worker') or '(未割当)'
    started = meta.get('started_at', '')
    completed = meta.get('completed_at', '')
    skills = meta.get('skills', [])
    priority = meta.get('priority', '')

    task_icon = '✅' if tstatus == 'done' else ('❌' if tstatus == 'failed' else '⏳')
    lines.append(f'### {task_icon} {tid} — {ttitle}')
    lines.append('')
    lines.append(f'- **Worker**: {worker}')
    lines.append(f'- **Status**: {tstatus}')
    if skills:
        skills_str = ', '.join(skills) if isinstance(skills, list) else str(skills)
        lines.append(f'- **Skills**: {skills_str}')
    if priority:
        lines.append(f'- **Priority**: {priority}')
    if started:
        lines.append(f'- **Started**: {started}')
    if completed:
        lines.append(f'- **Completed**: {completed}')
    lines.append('')

    if result_text:
        lines.append('**Result**:')
        lines.append('')
        lines.append(result_text)
        lines.append('')

    if description:
        lines.append('<details><summary>Description</summary>')
        lines.append('')
        lines.append(description)
        lines.append('')
        lines.append('</details>')
        lines.append('')

lines.append('## 完了')
lines.append('')

out_path = output_dir / f'{today}_{slug}.md'
out_path.write_text('\n'.join(lines))
print(f'Wrote: {out_path}')
PYEOF
