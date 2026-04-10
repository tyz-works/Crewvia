#!/usr/bin/env bash
set -euo pipefail

# assign-name.sh — Deterministically assign a worker name based on skill tags
# Usage: ./scripts/assign-name.sh [skill1 skill2 ...]
# Example: ./scripts/assign-name.sh ops bash
# Same skill set always returns the same name.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMES_YAML="${SCRIPT_DIR}/../config/worker-names.yaml"

if [[ ! -f "$NAMES_YAML" ]]; then
  echo "ERROR: worker-names.yaml not found at $NAMES_YAML" >&2
  exit 1
fi

# Sort skill tags alphabetically and join with space
if [[ $# -eq 0 ]]; then
  SORTED_TAGS=""
else
  SORTED_TAGS=$(echo "$@" | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//')
fi

# SHA256 hash of sorted tags
if command -v sha256sum &>/dev/null; then
  HASH=$(echo "$SORTED_TAGS" | sha256sum | awk '{print $1}')
else
  HASH=$(echo "$SORTED_TAGS" | shasum -a 256 | awk '{print $1}')
fi

# First 8 hex chars → decimal
HASH8="${HASH:0:8}"
HASH_DEC=$(python3 -c "print(int('$HASH8', 16))")

# Read names and customizations from YAML using python3
python3 - "$NAMES_YAML" "$SORTED_TAGS" "$HASH_DEC" "$@" <<'PYEOF'
import sys
import re

yaml_path = sys.argv[1]
sorted_tags_str = sys.argv[2]
hash_dec = int(sys.argv[3])
input_skills = set(sys.argv[4:]) if len(sys.argv) > 4 else set()

# Minimal YAML parser for worker-names.yaml structure
def parse_worker_names(path):
    names = []
    customizations = []

    with open(path) as f:
        lines = f.readlines()

    section = None
    current_custom = None

    for line in lines:
        stripped = line.rstrip()
        # Skip comments and blank lines
        content = re.sub(r'\s*#.*$', '', stripped).rstrip()
        if not content:
            continue

        # Detect section headers
        if content == 'names:':
            section = 'names'
            continue
        elif content == 'customizations:':
            section = 'customizations'
            continue

        if section == 'names':
            # Match list items: "  - Name"
            m = re.match(r'^\s+-\s+(\S+)', content)
            if m:
                names.append(m.group(1))

        elif section == 'customizations':
            # Match "  - name: Foo"
            m = re.match(r'^\s+-\s+name:\s+(\S+)', content)
            if m:
                if current_custom is not None:
                    customizations.append(current_custom)
                current_custom = {'name': m.group(1)}
                continue
            if current_custom is not None:
                # role
                m = re.match(r'^\s+role:\s+(\S+)', content)
                if m:
                    current_custom['role'] = m.group(1)
                    continue
                # disabled
                m = re.match(r'^\s+disabled:\s+(\S+)', content)
                if m:
                    current_custom['disabled'] = (m.group(1).lower() == 'true')
                    continue
                # skills: [a, b]
                m = re.match(r'^\s+skills:\s+\[([^\]]*)\]', content)
                if m:
                    skill_list = [s.strip() for s in m.group(1).split(',') if s.strip()]
                    current_custom['skills'] = skill_list
                    continue

    if current_custom is not None:
        customizations.append(current_custom)

    return names, customizations

names, customizations = parse_worker_names(yaml_path)

# Build lookup for customizations
custom_map = {c['name']: c for c in customizations}

# Filter names based on customization rules
def is_eligible(name, input_skills):
    c = custom_map.get(name)
    if c is None:
        # No customization — eligible for any worker assignment
        return True
    # disabled names are always excluded
    if c.get('disabled'):
        return False
    # orchestrator-fixed names excluded for worker assignment
    if c.get('role') == 'orchestrator':
        return False
    # skills-fixed names: only match if input skills exactly equal the fixed skills
    if 'skills' in c:
        fixed = set(c['skills'])
        if input_skills != fixed:
            return False
    return True

# Build eligible list (preserving order, shifting index on disabled/excluded)
eligible = [n for n in names if is_eligible(n, input_skills)]

if not eligible:
    echo_result = names[hash_dec % len(names)] if names else "Unknown"
    print(echo_result)
    sys.exit(0)

index = hash_dec % len(eligible)
print(eligible[index])
PYEOF
