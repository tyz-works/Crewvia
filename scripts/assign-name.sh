#!/usr/bin/env bash
set -euo pipefail

# assign-name.sh — Registry-first worker name assignment
#
# Usage:
#   ./scripts/assign-name.sh [skill1 skill2 ...]
#
# 引数:
#   [skill ...]   スキルタグ（スペース区切り）。同じスキルセットには同じ名前が返る。
#                 省略時はスキルなしとして扱い、未登録なら新規名前を割り当て。
#
# 動作:
#   1. registry/workers.yaml から同スキルセットの登録名を検索
#   2. 見つかれば同じ名前を返す（継続性の確保）
#   3. なければ config/worker-names.yaml のプールから未使用の名前を割り当て
#
# 出力:
#   stdout に決定した名前を出力（例: "Haruto"）
#
# 依存:
#   python3, config/worker-names.yaml, registry/workers.yaml

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

# Registry-first lookup and assignment via python3.
# lib_registry.py でヘッダ保存・同名 dedup・role 保存を一元化している。
python3 - "$SCRIPT_DIR" "$NAMES_YAML" "$REGISTRY_YAML" "$@" <<'PYEOF'
import sys
import re
from datetime import date

sys.path.insert(0, sys.argv[1])
from lib_registry import parse, write

names_yaml_path = sys.argv[2]
registry_yaml_path = sys.argv[3]
input_skills = sorted(sys.argv[4:]) if len(sys.argv) > 4 else []
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


pool_names, custom_map = parse_worker_names(names_yaml_path)
header, order, by_name = parse(registry_yaml_path)

# Step 2: Return existing name if the same skill set is already registered.
# Skip orchestrator entries — their empty skills list would false-match callers
# that also pass zero skills (e.g. `start.sh worker` with no args).
for name in order:
    w = by_name[name]
    if w.get('role') == 'orchestrator':
        continue
    if set(w['skills']) == input_skills_set:
        print(w['name'])
        sys.exit(0)

# Step 3: Find the first eligible name not already in the registry.
# All registered names are excluded (orchestrator + worker alike) so a new
# Worker never collides with an existing entry.
registered_names = set(by_name.keys())


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
    # Fallback: reuse the first eligible name even if already registered.
    for name in pool_names:
        if is_pool_eligible(name):
            chosen = name
            break

if chosen is None:
    chosen = "Unknown"

# Step 4: Append (or merge into) the chosen name.
today = str(date.today())
if chosen in by_name:
    # Fallback path hit — merge skills / refresh last_active, keep task_count & role.
    by_name[chosen]['skills'] = input_skills
    by_name[chosen]['last_active'] = today
else:
    by_name[chosen] = {
        'name': chosen,
        'role': '',
        'skills': input_skills,
        'task_count': 0,
        'last_active': today,
    }
    order.append(chosen)
write(registry_yaml_path, header, order, by_name)

# Step 5: Output name
print(chosen)
PYEOF
