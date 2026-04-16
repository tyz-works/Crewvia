#!/usr/bin/env python3
"""hooks/lib_skill_perms.py — Skill-based permission checker for PreToolUse hook.

Usage:
    python3 hooks/lib_skill_perms.py <yaml_path> <skills_csv> <tool_signature>

Output (JSON, single line):
    {"decision": "allow"|"deny"|"none", "source": "skill:code:allow:Edit"}

    - allow: Tool is permitted by skill permissions (skip Taskvia)
    - deny:  Tool is forbidden by skill permissions (block or escalate)
    - none:  No matching rule; fall through to existing Taskvia flow

Performance target: <100ms including Python startup.
"""

import json
import os
import sys
from fnmatch import fnmatch
from hashlib import md5

try:
    import yaml
except ImportError:
    yaml = None


def _parse_yaml_fallback(path: str) -> dict:
    """Minimal YAML parser for flat skill-permissions structure.

    Handles only the specific schema used by skill-permissions.yaml:
    top-level keys with nested allow/deny string lists.
    """
    result: dict = {}
    current_section = None
    current_key = None

    with open(path) as f:
        for line in f:
            stripped = line.rstrip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            content = stripped.strip()

            if indent == 0 and content.endswith(":"):
                current_section = content[:-1]
                if current_section == "skills":
                    continue
                result[current_section] = {}
                current_key = None
            elif indent == 2 and content.endswith(":"):
                key = content[:-1]
                if current_section == "skills":
                    result.setdefault("skills", {})[key] = {}
                    current_key = ("skills", key)
                elif current_section:
                    result[current_section][key] = []
                    current_key = (current_section, key)
            elif indent == 4 and content.endswith(":"):
                sub_key = content[:-1]
                if current_key and current_key[0] == "skills":
                    result["skills"][current_key[1]][sub_key] = []
                    current_key = ("skills", current_key[1], sub_key)
            elif content.startswith("- "):
                value = content[2:].strip().strip('"').strip("'")
                if current_key:
                    if len(current_key) == 3:
                        result["skills"][current_key[1]][current_key[2]].append(value)
                    elif len(current_key) == 2 and current_key[0] != "skills":
                        result[current_key[0]][current_key[1]].append(value)
    return result


def load_config(yaml_path: str) -> dict:
    """Load and cache skill-permissions.yaml (mtime-based)."""
    cache_key = md5(yaml_path.encode()).hexdigest()
    cache_path = f"/tmp/crewvia_skill_perms_{cache_key}.json"

    try:
        file_mtime = os.path.getmtime(yaml_path)
        if os.path.exists(cache_path):
            cache_mtime = os.path.getmtime(cache_path)
            if cache_mtime >= file_mtime:
                with open(cache_path) as f:
                    return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    with open(yaml_path) as f:
        if yaml:
            config = yaml.safe_load(f)
        else:
            config = _parse_yaml_fallback(yaml_path)

    try:
        with open(cache_path, "w") as f:
            json.dump(config, f)
    except OSError:
        pass

    return config


def check_permission(config: dict, skills_csv: str, tool_sig: str) -> dict:
    """Check tool_sig against skill permissions.

    Returns {"decision": "allow"|"deny"|"none", "source": "..."}.
    """
    if not skills_csv:
        return {"decision": "none", "source": "no_skills"}

    skills = [s.strip() for s in skills_csv.split(",") if s.strip()]
    if not skills:
        return {"decision": "none", "source": "no_skills"}

    # 1. _global.deny — always checked, cannot be overridden
    global_deny = config.get("_global", {}).get("deny", [])
    for pattern in global_deny:
        if fnmatch(tool_sig, pattern):
            return {"decision": "deny", "source": f"_global:deny:{pattern}"}

    skills_config = config.get("skills", {})
    known_skills = [s for s in skills if s in skills_config]
    if not known_skills:
        return {"decision": "none", "source": "unknown_skills"}

    # 2. Skill deny — union across all task skills
    for skill in known_skills:
        deny_patterns = skills_config[skill].get("deny", [])
        for pattern in deny_patterns:
            if fnmatch(tool_sig, pattern):
                return {"decision": "deny", "source": f"skill:{skill}:deny:{pattern}"}

    # 3. Skill allow — union across all task skills
    for skill in known_skills:
        allow_patterns = skills_config[skill].get("allow", [])
        for pattern in allow_patterns:
            if fnmatch(tool_sig, pattern):
                return {"decision": "allow", "source": f"skill:{skill}:allow:{pattern}"}

    # 4. No match — fall through
    return {"decision": "none", "source": "no_match"}


def main():
    if len(sys.argv) != 4:
        print(json.dumps({"decision": "none", "source": "usage_error"}))
        sys.exit(0)

    yaml_path, skills_csv, tool_sig = sys.argv[1], sys.argv[2], sys.argv[3]

    if not os.path.isfile(yaml_path):
        print(json.dumps({"decision": "none", "source": "yaml_not_found"}))
        sys.exit(0)

    config = load_config(yaml_path)
    result = check_permission(config, skills_csv, tool_sig)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
