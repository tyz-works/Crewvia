#!/usr/bin/env bash
set -euo pipefail

# taskvia-sync.sh — plan.yaml の状態を Taskvia にミラーリングする（オプション）
# Usage: bash scripts/taskvia-sync.sh [--plan <path>]
# TASKVIA_TOKEN が未設定の場合は何もしない（exit 0）

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"

# TASKVIA_TOKEN 未設定ならサイレントにスキップ
if [[ -z "$TASKVIA_TOKEN" ]]; then
  exit 0
fi

PLAN_FILE="${REPO_ROOT}/queue/plan.yaml"

# 引数パース
while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan)
      PLAN_FILE="$2"
      shift 2
      ;;
    *)
      echo "[taskvia-sync] WARNING: Unknown argument: $1" >&2
      shift
      ;;
  esac
done

# plan.yaml が存在しない場合はサイレントに exit 0
if [[ ! -f "$PLAN_FILE" ]]; then
  exit 0
fi

QUEUE_DIR="$(dirname "$PLAN_FILE")"
MAP_FILE="${QUEUE_DIR}/.taskvia-map.json"
AUTH_HEADER="Authorization: Bearer ${TASKVIA_TOKEN}"

python3 - "$PLAN_FILE" "$MAP_FILE" "$TASKVIA_URL" "$AUTH_HEADER" <<'PYEOF'
import sys
import os
import json
import urllib.request

plan_file   = sys.argv[1]
map_file    = sys.argv[2]
taskvia_url = sys.argv[3]
auth_header = sys.argv[4]


# ---------- plan.yaml 読み込み ----------

def load_plan(path):
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        print("[taskvia-sync] WARNING: PyYAML が利用できません。pip install pyyaml を実行してください。", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"[taskvia-sync] WARNING: plan.yaml の読み込み失敗: {e}", file=sys.stderr)
        return {}


# ---------- map I/O ----------

def load_map(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_map(path, data):
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
    except OSError as e:
        print(f"[taskvia-sync] WARNING: .taskvia-map.json 保存失敗: {e}", file=sys.stderr)


# ---------- HTTP helpers ----------

def _headers():
    name, val = auth_header.split(': ', 1)
    return {'Content-Type': 'application/json', name: val}


def http_post(url, payload):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=_headers(),
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[taskvia-sync] WARNING: POST {url} 失敗: {e}", file=sys.stderr)
        return None


# ---------- main ----------

plan = load_plan(plan_file)
tasks = plan.get('tasks') or []

if not tasks:
    print("[taskvia-sync] plan.yaml にタスクが見つかりません。スキップ。", file=sys.stderr)
    sys.exit(0)

task_map = load_map(map_file)
updated = False

for task in tasks:
    if not isinstance(task, dict):
        continue
    task_id = str(task.get('id', '')).strip()
    if not task_id:
        continue

    title    = str(task.get('title',    task_id))
    status   = str(task.get('status',   'pending'))
    priority = str(task.get('priority', 'medium'))
    agent    = str(task.get('worker') or task.get('agent') or 'orchestrator')

    if task_id not in task_map:
        # 未登録 → Taskvia にカード登録
        payload = {
            'tool':       f"task:{task_id}",
            'agent':      agent,
            'task_title': title,
            'task_id':    task_id,
            'priority':   priority,
        }
        resp = http_post(f"{taskvia_url}/api/request", payload)
        if resp and resp.get('id'):
            card_id = resp['id']
            task_map[task_id] = {'card_id': card_id, 'status': status}
            print(f"[taskvia-sync] 登録: {task_id} → card_id={card_id}")
            updated = True
        else:
            print(f"[taskvia-sync] WARNING: {task_id} の登録失敗。スキップ。", file=sys.stderr)
    else:
        # 登録済み → ステータス差分があれば更新
        card_id     = task_map[task_id].get('card_id', '')
        last_status = task_map[task_id].get('status', '')
        if card_id and status != last_status:
            resp = http_post(
                f"{taskvia_url}/api/cards/{card_id}",
                {'status': status},
            )
            if resp is not None:
                task_map[task_id]['status'] = status
                print(f"[taskvia-sync] 更新: {task_id} status {last_status} → {status}")
                updated = True
            else:
                print(f"[taskvia-sync] WARNING: {task_id} のステータス更新失敗。スキップ。", file=sys.stderr)

if updated:
    save_map(map_file, task_map)
    print(f"[taskvia-sync] .taskvia-map.json を保存しました ({len(task_map)} 件)。")
else:
    print("[taskvia-sync] 変更なし。同期完了。")
PYEOF

exit 0
