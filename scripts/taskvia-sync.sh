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

python3 - "$QUEUE_DIR" "$MAP_FILE" "$TASKVIA_URL" "$AUTH_HEADER" "$SCRIPT_DIR" <<'PYEOF'
import sys
import os
import re
import json
import urllib.request

queue_dir   = sys.argv[1]
map_file    = sys.argv[2]
taskvia_url = sys.argv[3]
auth_header = sys.argv[4]
scripts_dir = sys.argv[5]

# task カードの読み取りは crewvia の中で 1 箇所しかない (Codex 5 巡目 P2)。
# ここに frontmatter を直接読むコードを書き戻さないこと — Taskvia に出る姿と
# `plan.sh status` に出る姿が、静かにズレる。
sys.path.insert(0, scripts_dir)
from lib_task_cards import (  # noqa: E402
    is_unreadable, list_task_cards, read_regular_text_or_unreadable,
)

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


# ---------- map I/O ----------

def load_map(path):
    # queue/.taskvia-map.json。素の open() だと FIFO 1 枚で同期全体が
    # 無期限に止まる (t018)。欠損 = 初回同期なので `{}` でよい —— 倒す先は
    # 「もう一度送る」= 冪等 (knowledge/empty-vs-unobservable.md §2 の K)。
    text = _read_queue_text(path)
    if is_unreadable(text):
        return {}
    try:
        return json.loads(text)
    except ValueError:
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


def http_get(url):
    try:
        req = urllib.request.Request(url, headers=_headers(), method='GET')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"[taskvia-sync] WARNING: GET {url} 失敗: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[taskvia-sync] WARNING: GET {url} 失敗: {e}", file=sys.stderr)
        return None


def http_patch(url, payload):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=_headers(),
            method='PATCH',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[taskvia-sync] WARNING: PATCH {url} 失敗: {e}", file=sys.stderr)
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

def _read_queue_text(path):
    """queue のファイルを種類を確かめてから読む。

    読めたら `str`、読めなければ `Unreadable` —— 空文字でも None でもない
    (t018)。失敗を「空」と同じ形で返さないので、呼び出し側が取り違えられない。
    """
    return read_regular_text_or_unreadable(
        path,
        warn=lambda msg: print(f"[taskvia-sync] WARNING: {msg}", file=sys.stderr))



def scan_missions():
    """Yield (slug, mission_meta, task_meta) for every task in active missions."""
    if not os.path.exists(state_file):
        return
    # 固定パスの読み取りにも種類のガードを当てる (Codex 8 巡目 P2)。上限の無い
    # `open()` は、書き手のいない FIFO 1 枚で同期全体を無期限に止める。
    state_text = _read_queue_text(state_file)
    if is_unreadable(state_text):
        return                      # 観測できなかった = 何も同期しない
    state = parse_yaml(state_text)
    active = state.get('active_missions') or []
    for slug in active:
        mdir = os.path.join(missions_dir, slug)
        myaml = os.path.join(mdir, 'mission.yaml')
        if not os.path.exists(myaml):
            continue
        mission_text = _read_queue_text(myaml)
        if is_unreadable(mission_text):
            continue                # この mission だけ飛ばす (他は同期する)
        mission_meta = parse_yaml(mission_text)
        # 読み取りは scripts/lib_task_cards.py に 1 つだけ (plan.sh / dispatcher.sh
        # と同じもの)。識別子はファイル名から来るので、`id` 行を直し忘れたコピーが
        # Taskvia 側で空 id / 別カードの上書きになることが構造上なくなる。
        # 信用できないカードも `[破損]` として出る —— 黙って消すと、ボードだけを
        # 見ている人には「そんな task は無い」と読めてしまう。
        for meta, _body in list_task_cards(os.path.join(mdir, 'tasks')):
            yield slug, mission_meta, meta


# ---------- main ----------

records = list(scan_missions())
if not records:
    print("[taskvia-sync] active mission にタスクが見つかりません。スキップ。", file=sys.stderr)
    sys.exit(0)

task_map = load_map(map_file)
updated = False

# ---------- ミッション登録 (冪等) ----------
existing_resp = http_get(f"{taskvia_url}/api/missions")
existing_slugs = set()
if existing_resp:
    for m in existing_resp.get('missions', []):
        existing_slugs.add(m.get('slug', ''))

seen_slugs = set()
for slug, mission_meta, _ in records:
    if slug in seen_slugs or slug in existing_slugs:
        seen_slugs.add(slug)
        continue
    seen_slugs.add(slug)
    resp = http_post(f"{taskvia_url}/api/missions", {
        'slug': slug,
        'title': mission_meta.get('title', slug),
    })
    if resp:
        print(f"[taskvia-sync] ミッション登録: {slug}")
        updated = True
    else:
        print(f"[taskvia-sync] WARNING: ミッション {slug} の登録失敗。スキップ。", file=sys.stderr)

# ---------- done タスク ID を収集（blocked 判定用） ----------
done_ids = set()
for slug, mission_meta, task in records:
    if task.get('status') in ('done', 'verified', 'skipped'):
        tid = task.get('id', '')
        if tid:
            done_ids.add(f"{slug}:{tid}")


def taskvia_status(status, blocked_by, slug):
    """crewvia status → Taskvia status に変換。pending + 未解決 blocked_by → blocked"""
    if status == 'pending' and blocked_by:
        if any(f"{slug}:{dep}" not in done_ids for dep in blocked_by):
            return 'blocked'
    return status


# ---------- タスク登録・更新 ----------
for slug, mission_meta, task in records:
    task_id = task.get('id', '')
    if not task_id:
        continue

    map_key = f"{slug}:{task_id}"
    title = task.get('title', task_id)
    raw_status = task.get('status', 'pending')
    priority = task.get('priority', 'medium')
    agent = task.get('worker') or 'director'
    skills = task.get('skills') or []
    blocked_by = task.get('blocked_by') or []
    status = taskvia_status(raw_status, blocked_by, slug)

    if map_key not in task_map:
        payload = {
            'id': task_id,
            'title': title,
            'status': status,
            'assignee': agent,
            'skills': skills,
            'priority': priority,
            'blocked_by': blocked_by,
        }
        resp = http_post(f"{taskvia_url}/api/missions/{slug}/tasks", payload)
        if resp is not None:
            task_map[map_key] = {'registered': True, 'status': status}
            print(f"[taskvia-sync] 登録: {map_key} (blocked_by={blocked_by})")
            updated = True
        else:
            print(f"[taskvia-sync] WARNING: {map_key} の登録失敗。スキップ。", file=sys.stderr)
    else:
        last_status = task_map[map_key].get('status', '')
        if status != last_status:
            resp = http_patch(
                f"{taskvia_url}/api/missions/{slug}/tasks/{task_id}",
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
