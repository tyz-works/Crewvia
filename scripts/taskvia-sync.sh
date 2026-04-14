#!/usr/bin/env bash
set -euo pipefail

# taskvia-sync.sh — queue/missions/ 配下の状態を Taskvia にミラーリングする（オプション）
# Usage: bash scripts/taskvia-sync.sh [--queue <path>]
# TASKVIA_TOKEN が未設定の場合は何もしない（exit 0）

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"

if [[ "${CREWVIA_TASKVIA:-}" = "disabled" ]]; then
  echo "[taskvia-sync] Taskvia 無効モード。同期をスキップ。" >&2
  exit 0
fi

if [[ -z "$TASKVIA_TOKEN" ]]; then
  exit 0
fi

if [[ "${TASKVIA_URL}" != https://* ]]; then
  echo "[taskvia-sync] ERROR: TASKVIA_URL must start with https://: ${TASKVIA_URL}" >&2
  exit 1
fi

QUEUE_DIR="${CREWVIA_QUEUE:-${REPO_ROOT}/queue}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue)
      QUEUE_DIR="$2"
      shift 2
      ;;
    *)
      echo "[taskvia-sync] WARNING: Unknown argument: $1" >&2
      shift
      ;;
  esac
done

if [[ ! -d "$QUEUE_DIR" ]]; then
  exit 0
fi

MAP_FILE="${QUEUE_DIR}/.taskvia-map.json"
AUTH_HEADER="Authorization: Bearer ${TASKVIA_TOKEN}"

python3 - "$QUEUE_DIR" "$MAP_FILE" "$TASKVIA_URL" "$AUTH_HEADER" <<'PYEOF'
import sys
import os
import re
import json
import urllib.request

queue_dir   = sys.argv[1]
map_file    = sys.argv[2]
taskvia_url = sys.argv[3]
auth_header = sys.argv[4]

state_file = os.path.join(queue_dir, 'state.yaml')
missions_dir = os.path.join(queue_dir, 'missions')


# ---------- minimal YAML / frontmatter helpers ----------

def _scalar(val):
    if val == 'null' or val == '':
        return None
    if val in ('true', 'True'):
        return True
    if val in ('false', 'False'):
        return False
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return val[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    if re.fullmatch(r'-?\d+', val):
        return int(val)
    return val


def parse_yaml(text):
    lines = text.splitlines()
    result = {}
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
        key, val = m.group(1), m.group(2).rstrip()
        if val == '':
            i += 1
            items = []
            while i < len(lines):
                lst = re.match(r'^\s+-\s*(.*)$', lines[i])
                if lst:
                    items.append(_scalar(lst.group(1).strip()))
                    i += 1
                else:
                    break
            result[key] = items if items else None
        elif val.startswith('[') and val.endswith(']'):
            inner = val[1:-1].strip()
            result[key] = [_scalar(s.strip()) for s in inner.split(',') if s.strip()] if inner else []
            i += 1
        else:
            result[key] = _scalar(val)
            i += 1
    return result


def parse_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end = i
            break
    if end is None:
        return None
    return parse_yaml('\n'.join(lines[1:end]))


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


def http_delete(url):
    try:
        req = urllib.request.Request(url, headers=_headers(), method='DELETE')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # すでに存在しない — OK
        print(f"[taskvia-sync] WARNING: DELETE {url} 失敗: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[taskvia-sync] WARNING: DELETE {url} 失敗: {e}", file=sys.stderr)
        return None


# ---------- mission scanning ----------

def scan_missions():
    """Yield (slug, mission_meta, task_meta) for every task in active missions."""
    if not os.path.exists(state_file):
        return
    with open(state_file) as f:
        state = parse_yaml(f.read())
    active = state.get('active_missions') or []
    for slug in active:
        mdir = os.path.join(missions_dir, slug)
        myaml = os.path.join(mdir, 'mission.yaml')
        if not os.path.exists(myaml):
            continue
        with open(myaml) as f:
            mission_meta = parse_yaml(f.read())
        tdir = os.path.join(mdir, 'tasks')
        if not os.path.isdir(tdir):
            continue
        entries = []
        for fn in os.listdir(tdir):
            m = re.fullmatch(r't(\d+)\.md', fn)
            if m:
                entries.append((int(m.group(1)), fn))
        entries.sort()
        for _, fn in entries:
            with open(os.path.join(tdir, fn)) as f:
                meta = parse_frontmatter(f.read())
            if meta is None:
                continue
            yield slug, mission_meta, meta


# ---------- main ----------

records = list(scan_missions())
if not records:
    print("[taskvia-sync] active mission にタスクが見つかりません。スキップ。", file=sys.stderr)
    sys.exit(0)

task_map = load_map(map_file)
updated = False

for slug, mission_meta, task in records:
    task_id = task.get('id', '')
    if not task_id:
        continue

    map_key = f"{slug}:{task_id}"
    title = task.get('title', task_id)
    status = task.get('status', 'pending')
    priority = task.get('priority', 'medium')
    agent = task.get('worker') or 'director'

    if map_key not in task_map:
        payload = {
            'tool': f"task:{slug}/{task_id}",
            'agent': agent,
            'task_title': title,
            'task_id': f"{slug}/{task_id}",
            'priority': priority,
        }
        resp = http_post(f"{taskvia_url}/api/request", payload)
        if resp and resp.get('id'):
            card_id = resp['id']
            task_map[map_key] = {'card_id': card_id, 'status': status}
            print(f"[taskvia-sync] 登録: {map_key} → card_id={card_id}")
            updated = True
        else:
            print(f"[taskvia-sync] WARNING: {map_key} の登録失敗。スキップ。", file=sys.stderr)
    else:
        card_id = task_map[map_key].get('card_id', '')
        last_status = task_map[map_key].get('status', '')
        if card_id and status != last_status:
            resp = http_post(
                f"{taskvia_url}/api/cards/{card_id}",
                {'status': status},
            )
            if resp is not None:
                task_map[map_key]['status'] = status
                print(f"[taskvia-sync] 更新: {map_key} status {last_status} → {status}")
                updated = True
            else:
                print(f"[taskvia-sync] WARNING: {map_key} のステータス更新失敗。スキップ。", file=sys.stderr)

if updated:
    save_map(map_file, task_map)
    print(f"[taskvia-sync] .taskvia-map.json を保存しました ({len(task_map)} 件)。")
else:
    print("[taskvia-sync] 変更なし。同期完了。")

# ---------- archive 済み mission の掃除 ----------
# queue/archive/ にある slug を taskvia からも削除する（冪等: 404 は無視）
archive_dir = os.path.join(queue_dir, 'archive')
if os.path.isdir(archive_dir):
    for entry in os.listdir(archive_dir):
        entry_path = os.path.join(archive_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        slug = entry
        result = http_delete(f"{taskvia_url}/api/missions/{slug}")
        if result is not None:
            print(f"[taskvia-sync] archive 済み mission 削除: {slug}")
PYEOF

exit 0
