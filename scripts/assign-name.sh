#!/usr/bin/env bash
set -euo pipefail

# assign-name.sh — Registry-first worker name assignment
# Usage: ./scripts/assign-name.sh [skill1 skill2 ...]
# Same skill set returns the registered name, or assigns a new one from the pool.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NAMES_YAML="${REPO_ROOT}/config/worker-names.yaml"
REGISTRY_DIR="${REPO_ROOT}/registry"
REGISTRY_YAML="${REGISTRY_DIR}/workers.yaml"

if [[ ! -f "$NAMES_YAML" ]]; then
  echo "ERROR: worker-names.yaml not found at $NAMES_YAML" >&2
  exit 1
fi

# Ensure registry directory and file exist
mkdir -p "$REGISTRY_DIR"
if [[ ! -f "$REGISTRY_YAML" ]]; then
  printf 'workers: []\n' > "$REGISTRY_YAML"
fi

# Registry-first lookup and assignment via python3
python3 - "$NAMES_YAML" "$REGISTRY_YAML" "$@" <<'PYEOF'
import sys
import re
from datetime import date

names_yaml_path = sys.argv[1]
registry_yaml_path = sys.argv[2]
input_skills = sorted(sys.argv[3:]) if len(sys.argv) > 3 else []
input_skills_set = set(input_skills)


def parse_worker_names(path):
    """Parse config/worker-names.yaml → (names list, custom_map dict)."""
    names = []
    customizations = []
    section = None
    current_custom = None

    with open(path) as f:
        lines = f.readlines()

    for line in lines:
        content = re.sub(r'\s*#.*$', '', line.rstrip()).rstrip()
        if not content:
            continue
        if content == 'names:':
            section = 'names'
            continue
        elif content == 'customizations:':
            section = 'customizations'
            continue

        if section == 'names':
            m = re.match(r'^\s+-\s+(\S+)', content)
            if m:
                names.append(m.group(1))

        elif section == 'customizations':
            m = re.match(r'^\s+-\s+name:\s+(\S+)', content)
            if m:
                if current_custom is not None:
                    customizations.append(current_custom)
                current_custom = {'name': m.group(1)}
                continue
            if current_custom is not None:
                m = re.match(r'^\s+role:\s+(\S+)', content)
                if m:
                    current_custom['role'] = m.group(1)
                    continue
                m = re.match(r'^\s+disabled:\s+(\S+)', content)
                if m:
                    current_custom['disabled'] = (m.group(1).lower() == 'true')
                    continue
                m = re.match(r'^\s+skills:\s+\[([^\]]*)\]', content)
                if m:
                    current_custom['skills'] = [s.strip() for s in m.group(1).split(',') if s.strip()]
                    continue

    if current_custom is not None:
        customizations.append(current_custom)

    return names, {c['name']: c for c in customizations}


def parse_registry(path):
    """Parse registry/workers.yaml → list of worker dicts."""
    workers = []
    current = None

    with open(path) as f:
        lines = f.readlines()

    for line in lines:
        content = re.sub(r'\s*#.*$', '', line.rstrip()).rstrip()
        if not content or content in ('workers:', 'workers: []'):
            continue
        m = re.match(r'^\s+-\s+name:\s+(\S+)', content)
        if m:
            if current is not None:
                workers.append(current)
            current = {'name': m.group(1), 'skills': [], 'task_count': 0, 'last_active': ''}
            continue
        if current is not None:
            m = re.match(r'^\s+skills:\s+\[([^\]]*)\]', content)
            if m:
                current['skills'] = [s.strip() for s in m.group(1).split(',') if s.strip()]
                continue
            m = re.match(r'^\s+task_count:\s+(\d+)', content)
            if m:
                current['task_count'] = int(m.group(1))
                continue
            m = re.match(r'^\s+last_active:\s+(\S+)', content)
            if m:
                current['last_active'] = m.group(1)
                continue

    if current is not None:
        workers.append(current)
    return workers


def write_registry(path, workers):
    """Write worker list back to registry/workers.yaml."""
    lines = ['workers:\n']
    for w in workers:
        skills_str = ', '.join(w['skills'])
        lines.append(f"  - name: {w['name']}\n")
        lines.append(f"    skills: [{skills_str}]\n")
        lines.append(f"    task_count: {w['task_count']}\n")
        lines.append(f"    last_active: {w['last_active']}\n")
    with open(path, 'w') as f:
        f.writelines(lines)


pool_names, custom_map = parse_worker_names(names_yaml_path)
registry = parse_registry(registry_yaml_path)

# Step 2: Return existing name if same skill set is already registered
for w in registry:
    if set(w['skills']) == input_skills_set:
        print(w['name'])
        sys.exit(0)

# Step 3: Find first eligible name not already in registry
registered_names = {w['name'] for w in registry}


def is_pool_eligible(name):
    c = custom_map.get(name)
    if c is None:
        return True
    if c.get('disabled'):
        return False
    if c.get('role') == 'orchestrator':
        return False
    return True


chosen = None
for name in pool_names:
    if is_pool_eligible(name) and name not in registered_names:
        chosen = name
        break

if chosen is None:
    # Fallback: reuse first eligible name
    for name in pool_names:
        if is_pool_eligible(name):
            chosen = name
            break

if chosen is None:
    chosen = "Unknown"

# Step 4: Append new worker to registry
registry.append({
    'name': chosen,
    'skills': input_skills,
    'task_count': 0,
    'last_active': str(date.today()),
})
write_registry(registry_yaml_path, registry)

# Step 5: Output name
print(chosen)
PYEOF
