#!/usr/bin/env python3
"""run_verification_checks.py — Run verification checks for a Crewvia task.

Reads `verification.commands` from the task frontmatter, executes each
command in parallel using ThreadPoolExecutor, and saves structured results to:

  registry/verification/<task_id>/<cycle>.json

If the task has no `verification` block, this is a no-op (exit 0, no JSON).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# YAML helpers (minimal — mirrors plan.sh's parse_yaml subset)
# ---------------------------------------------------------------------------

def _scalar(s: str):
    if s in ('null', '~', ''):
        return None
    if s in ('true', 'yes'):
        return True
    if s in ('false', 'no'):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    return s.strip('"\'')


def _parse_minimal_yaml(text: str) -> dict:
    lines = text.splitlines()
    result: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            i += 1
            continue
        m = re.match(r'^([\w-]+):\s*(.*)$', line)
        if not m:
            i += 1
            continue
        key = m.group(1)
        val = m.group(2).rstrip()
        if val == '':
            i += 1
            items: list = []
            sub: dict = {}
            while i < len(lines):
                lm = re.match(r'^\s+-\s*(.*)$', lines[i])
                if lm:
                    items.append(_scalar(lm.group(1).strip()))
                    i += 1
                else:
                    mm = re.match(r'^  ([\w-]+):\s*(.*)$', lines[i])
                    if mm:
                        sub[mm.group(1)] = _scalar(mm.group(2).rstrip())
                        i += 1
                    else:
                        break
            result[key] = items if items else (sub if sub else None)
        elif val.startswith('[') and val.endswith(']'):
            inner = val[1:-1].strip()
            result[key] = [_scalar(s.strip()) for s in inner.split(',')] if inner else []
            i += 1
        else:
            result[key] = _scalar(val)
            i += 1
    return result


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise ValueError("missing frontmatter delimiter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end = i
            break
    if end is None:
        raise ValueError("unterminated frontmatter")
    meta_text = '\n'.join(lines[1:end])
    body = '\n'.join(lines[end + 1:])
    return _parse_minimal_yaml(meta_text), body


# ---------------------------------------------------------------------------
# Verification block parser
# ---------------------------------------------------------------------------

def _strip_outer_quotes(s: str) -> str:
    """Remove matching outer quotes only (prevents stripping internal quotes)."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _parse_verification_block(text: str) -> Optional[dict]:
    """Extract the `verification:` block as a dict from frontmatter text.

    The minimal YAML parser handles scalar sub-dicts (key: value) but the
    `commands:` list contains sub-items with `name:` and `command:` sub-keys,
    which requires special handling.
    """
    lines = text.splitlines()
    # Find verification: block boundaries
    in_fm = False
    fm_lines: list[str] = []
    for line in lines:
        if line.strip() == '---':
            if not in_fm:
                in_fm = True
                continue
            else:
                break
        if in_fm:
            fm_lines.append(line)

    # Find verification: block
    verif_start = None
    for i, line in enumerate(fm_lines):
        if re.match(r'^verification:\s*$', line):
            verif_start = i
            break
    if verif_start is None:
        return None

    # Collect lines until next top-level key or EOF
    block_lines: list[str] = []
    for i in range(verif_start + 1, len(fm_lines)):
        line = fm_lines[i]
        if line and not line.startswith(' ') and not line.startswith('\t') and not line.startswith('#'):
            break
        block_lines.append(line)

    if not block_lines:
        return {}

    # Parse mode, timeout_profile as simple key:val
    result: dict = {}
    commands: list[dict] = []
    in_commands = False
    current_cmd: dict = {}

    for line in block_lines:
        # Detect `  commands:` block
        if re.match(r'^\s+commands:\s*$', line):
            in_commands = True
            continue
        if in_commands:
            # `    - name: ...` or `      command: ...`
            m_item = re.match(r'^\s+-\s+(\w+):\s*(.+)$', line)
            m_cont = re.match(r'^\s+(\w+):\s*(.+)$', line)
            if m_item:
                if current_cmd:
                    commands.append(current_cmd)
                current_cmd = {m_item.group(1): _strip_outer_quotes(m_item.group(2).strip())}
            elif m_cont and current_cmd is not None:
                current_cmd[m_cont.group(1)] = _strip_outer_quotes(m_cont.group(2).strip())
            else:
                # Leaving commands block
                if current_cmd:
                    commands.append(current_cmd)
                current_cmd = {}
                in_commands = False
            continue

        # Simple key: val
        m = re.match(r'^\s+([\w-]+):\s*(.+)$', line)
        if m:
            result[m.group(1)] = m.group(2).strip().strip('"\'')

    if current_cmd:
        commands.append(current_cmd)
    if commands:
        result['commands'] = commands
    return result if result else None


# ---------------------------------------------------------------------------
# Timeout profile loader
# ---------------------------------------------------------------------------

DEFAULT_PROFILE = 'verify_standard'
DEFAULT_TIMEOUT = 1800


def _load_timeout_max(timeout_profile: str, config_dir: str) -> int:
    profiles_path = os.path.join(config_dir, 'timeout-profiles.yaml')
    if not os.path.exists(profiles_path):
        return DEFAULT_TIMEOUT
    with open(profiles_path) as f:
        content = f.read()
    in_profiles = False
    current = None
    for line in content.splitlines():
        if re.match(r'^profiles:\s*$', line):
            in_profiles = True
            continue
        if not in_profiles:
            continue
        pm = re.match(r'^  ([a-zA-Z_][a-zA-Z0-9_-]*):\s*$', line)
        if pm:
            current = pm.group(1)
            continue
        if current == timeout_profile:
            fm = re.match(r'^    max:\s*(\d+)', line)
            if fm:
                return int(fm.group(1))
    return DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# Task file finder
# ---------------------------------------------------------------------------

def _find_task_file(task_id: str, queue_dir: str) -> Optional[str]:
    missions_dir = os.path.join(queue_dir, 'missions')
    if not os.path.isdir(missions_dir):
        return None
    for mission in os.listdir(missions_dir):
        candidate = os.path.join(missions_dir, mission, 'tasks', f'{task_id}.md')
        if os.path.exists(candidate):
            return candidate
    # Also check archive
    archive_dir = os.path.join(queue_dir, 'archive')
    if os.path.isdir(archive_dir):
        for mission in os.listdir(archive_dir):
            candidate = os.path.join(archive_dir, mission, 'tasks', f'{task_id}.md')
            if os.path.exists(candidate):
                return candidate
    return None


# ---------------------------------------------------------------------------
# Single check executor
# ---------------------------------------------------------------------------

def _run_check(check: dict, timeout_s: int) -> dict:
    name = check.get('name', 'unnamed')
    command = check.get('command', '')
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        duration = round(time.monotonic() - start, 3)
        return {
            'name': name,
            'command': command,
            'status': 'pass' if proc.returncode == 0 else 'fail',
            'exit_code': proc.returncode,
            'stdout': proc.stdout[-4096:] if proc.stdout else '',
            'stderr': proc.stderr[-4096:] if proc.stderr else '',
            'duration_s': duration,
        }
    except subprocess.TimeoutExpired:
        duration = round(time.monotonic() - start, 3)
        return {
            'name': name,
            'command': command,
            'status': 'timeout',
            'exit_code': -1,
            'stdout': '',
            'stderr': f'Command timed out after {timeout_s}s',
            'duration_s': duration,
        }
    except Exception as e:
        duration = round(time.monotonic() - start, 3)
        return {
            'name': name,
            'command': command,
            'status': 'error',
            'exit_code': -1,
            'stdout': '',
            'stderr': str(e),
            'duration_s': duration,
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_checks(task_id: str, queue_dir: str) -> int:
    """Run verification checks for a task. Returns 0 (always — no-op is also 0)."""
    task_file = _find_task_file(task_id, queue_dir)
    if not task_file:
        print(f"[verify] task '{task_id}' not found in {queue_dir}", file=sys.stderr)
        return 1

    with open(task_file) as f:
        text = f.read()

    verification = _parse_verification_block(text)

    # no-op: verification block absent or has no commands
    if not verification or not verification.get('commands'):
        print(f"[verify] task '{task_id}': no verification.commands defined — no-op", file=sys.stderr)
        return 0

    commands: list[dict] = verification['commands']
    timeout_profile = verification.get('timeout_profile', DEFAULT_PROFILE)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_dir = os.path.join(repo_root, 'config')
    timeout_s = _load_timeout_max(timeout_profile, config_dir)

    print(f"[verify] task '{task_id}': running {len(commands)} check(s) "
          f"(profile={timeout_profile}, timeout={timeout_s}s)", file=sys.stderr)

    # Parallel execution
    results: list[dict] = [{}] * len(commands)
    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        future_to_idx = {
            executor.submit(_run_check, cmd, timeout_s): i
            for i, cmd in enumerate(commands)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()

    overall = 'pass' if all(r.get('status') == 'pass' for r in results) else 'fail'

    # Determine cycle number
    registry_dir = os.path.join(
        os.path.dirname(queue_dir),  # repo root via queue_dir parent
        'registry', 'verification', task_id,
    )
    # Fallback: if queue_dir IS the repo root, put registry alongside queue
    if not os.path.basename(queue_dir) == 'queue':
        registry_dir = os.path.join(queue_dir, '..', 'registry', 'verification', task_id)
    registry_dir = os.path.normpath(registry_dir)
    os.makedirs(registry_dir, exist_ok=True)

    existing = [f for f in os.listdir(registry_dir) if f.endswith('.json')]
    cycle = len(existing) + 1

    output = {
        'task_id': task_id,
        'cycle': cycle,
        'executed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'overall': overall,
        'checks': results,
    }

    out_path = os.path.join(registry_dir, f'{cycle}.json')
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write('\n')

    print(f"[verify] result: {overall} — saved to {out_path}", file=sys.stderr)
    for r in results:
        icon = '✅' if r.get('status') == 'pass' else ('⏱️' if r.get('status') == 'timeout' else '❌')
        print(f"  {icon} {r.get('name')}: {r.get('status')} ({r.get('duration_s')}s)", file=sys.stderr)

    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: run_verification_checks.py <task_id> [queue_dir]", file=sys.stderr)
        sys.exit(1)
    _task_id = sys.argv[1]
    _queue_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser('~/workspace/crewvia/queue')
    sys.exit(run_checks(_task_id, _queue_dir))
