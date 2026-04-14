#!/usr/bin/env bash
set -euo pipefail

# fetch-requests.sh — taskvia から未処理リクエストを取得して表示する
#
# Usage:
#   bash scripts/fetch-requests.sh [--status pending|processing|done|rejected] [--json] [--process <id>]
#
# 環境変数:
#   TASKVIA_URL    (必須) taskvia WebUIのURL (例: https://taskvia.vercel.app)
#   TASKVIA_TOKEN  (任意) Bearer トークン（未設定時は無認証）
#
# オプション:
#   --status <s>    絞り込むステータス (デフォルト: pending)
#   --json          JSON 形式で出力 (jq / python3 -m json.tool で加工可能)
#   --process <id>  指定 ID の request 詳細を表示して終了

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- 環境変数チェック ---
TASKVIA_URL="${TASKVIA_URL:-}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"

if [ "${CREWVIA_TASKVIA:-}" = "disabled" ]; then
  echo "[fetch-requests] ERROR: Taskvia 連携が無効化されています（CREWVIA_TASKVIA=disabled）" >&2
  exit 1
fi

if [ -z "$TASKVIA_URL" ]; then
  echo "[fetch-requests] ERROR: TASKVIA_URL が未設定です" >&2
  exit 1
fi

# --- 引数パース ---
STATUS="pending"
JSON_MODE=false
PROCESS_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --status)
      STATUS="$2"
      shift 2
      ;;
    --json)
      JSON_MODE=true
      shift
      ;;
    --process)
      PROCESS_ID="$2"
      shift 2
      ;;
    *)
      echo "[fetch-requests] 不明なオプション: $1" >&2
      echo "Usage: $0 [--status <s>] [--json] [--process <id>]" >&2
      exit 1
      ;;
  esac
done

# --- 認証ヘッダ設定 ---
AUTH_HEADER=""
if [ -n "$TASKVIA_TOKEN" ]; then
  AUTH_HEADER="Authorization: Bearer $TASKVIA_TOKEN"
fi

# --- curl ヘルパー ---
_curl() {
  if [ -n "$AUTH_HEADER" ]; then
    curl -sf -H "$AUTH_HEADER" "$@"
  else
    curl -sf "$@"
  fi
}

# --- JSON パースヘルパー (jq 優先、なければ python3) ---
_json_field() {
  local json="$1"
  local field="$2"
  if command -v jq &>/dev/null; then
    echo "$json" | jq -r "$field"
  else
    echo "$json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
# field は .key または .key.subkey 形式を想定
keys = '${field}'.lstrip('.').split('.')
val = data
for k in keys:
    if isinstance(val, list) and k.isdigit():
        val = val[int(k)]
    elif isinstance(val, dict):
        val = val.get(k, '')
    else:
        val = ''
        break
print(val if val is not None else '')
"
  fi
}

_json_pretty() {
  local json="$1"
  if command -v jq &>/dev/null; then
    echo "$json" | jq .
  else
    echo "$json" | python3 -m json.tool
  fi
}

# ============================================================
# --process <id>: 指定 ID の詳細を表示
# ============================================================
if [ -n "$PROCESS_ID" ]; then
  RESPONSE=$(_curl "${TASKVIA_URL}/api/requests/${PROCESS_ID}" 2>&1) || {
    echo "[fetch-requests] ERROR: ID=${PROCESS_ID} の取得に失敗しました" >&2
    echo "  URL: ${TASKVIA_URL}/api/requests/${PROCESS_ID}" >&2
    exit 1
  }

  if $JSON_MODE; then
    echo "$RESPONSE"
  else
    echo "=== Request: ${PROCESS_ID} ==="
    if command -v jq &>/dev/null; then
      echo "$RESPONSE" | jq -r '"Title    : \(.title)\nBody     : \(.body)\nStatus   : \(.status)\nPriority : \(.priority)\nSkills   : \(.skills | join(", "))\nTargetDir: \(.target_dir // "(crewvia本体)")\nDeadline : \(.deadline_note // "")\nCreatedAt: \(.created_at)"'
    else
      _json_pretty "$RESPONSE"
    fi
  fi
  exit 0
fi

# ============================================================
# 一覧取得
# ============================================================
URL="${TASKVIA_URL}/api/requests?status=${STATUS}"
RESPONSE=$(_curl "$URL" 2>&1) || {
  echo "[fetch-requests] ERROR: リクエスト一覧の取得に失敗しました" >&2
  echo "  URL: $URL" >&2
  exit 1
}

if $JSON_MODE; then
  echo "$RESPONSE"
  exit 0
fi

# --- 人間が読める形式で表示 ---
if command -v jq &>/dev/null; then
  COUNT=$(echo "$RESPONSE" | jq '.requests | length')
  echo "=== Taskvia Requests (status=${STATUS}) — ${COUNT} 件 ==="
  echo ""

  if [ "$COUNT" -eq 0 ]; then
    echo "  (なし)"
    exit 0
  fi

  echo "$RESPONSE" | jq -r '.requests[] | "[\(.id)] \(.title)\n  Status  : \(.status)\n  Priority: \(.priority)\n  Skills  : \(.skills | join(", "))\n  Target  : \(.target_dir // "(crewvia)")\n  Created : \(.created_at)\n"'
else
  echo "=== Taskvia Requests (status=${STATUS}) ==="
  echo ""
  _json_pretty "$RESPONSE"
fi
