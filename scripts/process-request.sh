#!/usr/bin/env bash
set -euo pipefail

# process-request.sh — taskvia の依頼 1 件を取り込んで crewvia mission に変換する
#
# Usage:
#   bash scripts/process-request.sh <request_id>
#
# 動作:
#   1. GET /api/requests/<id> で依頼を取得
#   2. plan.sh init で mission を作成 (slug: YYYYMMDD-req<short_id>)
#   3. plan.sh add で body を含む仮タスク t001 を追加
#   4. PATCH /api/requests/<id> で status=processing, mission_slug=<slug> を書き戻す
#
# 環境変数:
#   TASKVIA_URL    (必須)
#   TASKVIA_TOKEN  (任意) Bearer トークン

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- 引数チェック ---
if [ $# -lt 1 ]; then
  echo "Usage: $0 <request_id>" >&2
  exit 1
fi

REQUEST_ID="$1"

# --- 環境変数チェック ---
TASKVIA_URL="${TASKVIA_URL:-}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"

if [ -z "$TASKVIA_URL" ]; then
  echo "[process-request] ERROR: TASKVIA_URL が未設定です" >&2
  exit 1
fi

# --- 認証ヘッダ ---
AUTH_HEADER=""
if [ -n "$TASKVIA_TOKEN" ]; then
  AUTH_HEADER="Authorization: Bearer $TASKVIA_TOKEN"
fi

_curl_get() {
  if [ -n "$AUTH_HEADER" ]; then
    curl -sf -H "$AUTH_HEADER" "$@"
  else
    curl -sf "$@"
  fi
}

_curl_post() {
  if [ -n "$AUTH_HEADER" ]; then
    curl -sf -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" "$@"
  else
    curl -sf -X POST -H "Content-Type: application/json" "$@"
  fi
}

_curl_patch() {
  if [ -n "$AUTH_HEADER" ]; then
    curl -sf -X PATCH -H "$AUTH_HEADER" -H "Content-Type: application/json" "$@"
  else
    curl -sf -X PATCH -H "Content-Type: application/json" "$@"
  fi
}

# --- JSON パースヘルパー (jq 優先、なければ python3) ---
_jq() {
  local json="$1"
  local expr="$2"
  if command -v jq &>/dev/null; then
    echo "$json" | jq -r "$expr"
  else
    python3 - <<EOF
import json, sys
data = json.loads('''$json'''.replace("'", "\\'"))
EOF
    # フォールバック: python3 で個別フィールドを取得
    python3 -c "
import json, sys
raw = sys.stdin.read()
" <<< "$json"
  fi
}

# python3 ベースのフィールド取得（jq 代替）
_py_get() {
  local json="$1"
  local key="$2"
  python3 -c "
import json, sys
data = json.loads(sys.argv[1])
keys = '${key}'.split('.')
val = data
for k in keys:
    if isinstance(val, dict):
        val = val.get(k)
    else:
        val = None
        break
if val is None:
    print('')
elif isinstance(val, list):
    print(','.join(str(v) for v in val))
else:
    print(val)
" "$json"
}

# ============================================================
# STEP 1: 依頼を取得
# ============================================================
echo "[process-request] STEP 1: 依頼 ID=${REQUEST_ID} を取得中..."

REQUEST_JSON=$(_curl_get "${TASKVIA_URL}/api/requests/${REQUEST_ID}" 2>&1) || {
  echo "[process-request] ERROR: 依頼の取得に失敗しました (ID=${REQUEST_ID})" >&2
  echo "  URL: ${TASKVIA_URL}/api/requests/${REQUEST_ID}" >&2
  exit 1
}

# フィールド取得
if command -v jq &>/dev/null; then
  REQ_TITLE=$(echo "$REQUEST_JSON" | jq -r '.title // empty')
  REQ_BODY=$(echo "$REQUEST_JSON" | jq -r '.body // empty')
  REQ_PRIORITY=$(echo "$REQUEST_JSON" | jq -r '.priority // "medium"')
  REQ_SKILLS=$(echo "$REQUEST_JSON" | jq -r '.skills | join(",")' 2>/dev/null || echo "code")
  REQ_TARGET_DIR=$(echo "$REQUEST_JSON" | jq -r '.target_dir // empty')
  REQ_STATUS=$(echo "$REQUEST_JSON" | jq -r '.status')
  REQ_DEADLINE=$(echo "$REQUEST_JSON" | jq -r '.deadline_note // empty')
else
  REQ_TITLE=$(_py_get "$REQUEST_JSON" "title")
  REQ_BODY=$(_py_get "$REQUEST_JSON" "body")
  REQ_PRIORITY=$(_py_get "$REQUEST_JSON" "priority")
  REQ_SKILLS=$(_py_get "$REQUEST_JSON" "skills")
  REQ_TARGET_DIR=$(_py_get "$REQUEST_JSON" "target_dir")
  REQ_STATUS=$(_py_get "$REQUEST_JSON" "status")
  REQ_DEADLINE=$(_py_get "$REQUEST_JSON" "deadline_note")
fi

# デフォルト値
[ -z "$REQ_PRIORITY" ] && REQ_PRIORITY="medium"
[ -z "$REQ_SKILLS" ] && REQ_SKILLS="code"

if [ -z "$REQ_TITLE" ]; then
  echo "[process-request] ERROR: 依頼の title が空です" >&2
  exit 1
fi

# ステータス確認
if [ "$REQ_STATUS" = "processing" ] || [ "$REQ_STATUS" = "done" ]; then
  echo "[process-request] WARNING: この依頼は既に ${REQ_STATUS} 状態です" >&2
  echo "  強制処理する場合は plan.sh init を手動で実行してください" >&2
  exit 1
fi

echo "  Title   : $REQ_TITLE"
echo "  Priority: $REQ_PRIORITY"
echo "  Skills  : $REQ_SKILLS"
[ -n "$REQ_TARGET_DIR" ] && echo "  Target  : $REQ_TARGET_DIR"
[ -n "$REQ_DEADLINE" ] && echo "  Deadline: $REQ_DEADLINE"

# ============================================================
# STEP 2: mission スラグを生成して plan.sh init
# ============================================================
echo ""
echo "[process-request] STEP 2: mission を初期化中..."

# スラグ: YYYYMMDD-req<id の先頭8文字>
DATE_PREFIX=$(date +%Y%m%d)
# ID の先頭 8 文字（英数字のみ抽出）
SHORT_ID=$(echo "$REQUEST_ID" | tr -dc 'a-zA-Z0-9' | head -c 8)
MISSION_SLUG="${DATE_PREFIX}-req${SHORT_ID}"

cd "$REPO_ROOT"
"$SCRIPT_DIR/plan.sh" init "$REQ_TITLE" --mission "$MISSION_SLUG"
echo "  Mission slug: $MISSION_SLUG"

# ============================================================
# STEP 3: 仮タスク t001 を追加
# ============================================================
echo ""
echo "[process-request] STEP 3: 仮タスク t001 を追加中..."

# タスクタイトルは依頼タイトルをそのまま使う
TASK_TITLE="[依頼取り込み] ${REQ_TITLE}"

# description: body + target_dir 情報を含める
TASK_DESC="${REQ_BODY}"
if [ -n "$REQ_TARGET_DIR" ]; then
  TASK_DESC="${TASK_DESC}

対象プロジェクト: ${REQ_TARGET_DIR}"
fi
if [ -n "$REQ_DEADLINE" ]; then
  TASK_DESC="${TASK_DESC}
期限メモ: ${REQ_DEADLINE}"
fi
TASK_DESC="${TASK_DESC}

※ このタスクは Orchestrator が分解する前の仮タスクです。
  taskvia request ID: ${REQUEST_ID}"

# plan.sh add の呼び出し
ADD_ARGS=(
  "$TASK_TITLE"
  --mission "$MISSION_SLUG"
  --skills "$REQ_SKILLS"
  --priority "$REQ_PRIORITY"
)
if [ -n "$REQ_TARGET_DIR" ]; then
  ADD_ARGS+=(--target-dir "$REQ_TARGET_DIR")
fi

"$SCRIPT_DIR/plan.sh" add "${ADD_ARGS[@]}"
echo "  仮タスク追加完了"

# ============================================================
# STEP 4: taskvia に status=processing を書き戻す
# ============================================================
echo ""
echo "[process-request] STEP 4: taskvia に処理中を書き戻し中..."

NOW_ISO=$(python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())")

PATCH_BODY=$(python3 -c "
import json
print(json.dumps({
    'status': 'processing',
    'mission_slug': '${MISSION_SLUG}',
    'processed_at': '${NOW_ISO}'
}))
")

PATCH_RESPONSE=$(_curl_patch "${TASKVIA_URL}/api/requests/${REQUEST_ID}" -d "$PATCH_BODY" 2>&1) || {
  echo "[process-request] WARNING: taskvia への書き戻しに失敗しました（ネットワーク不通など）" >&2
  echo "  mission は作成済みです: $MISSION_SLUG" >&2
  echo "  後で手動で PATCH /api/requests/${REQUEST_ID} を実行してください" >&2
  # 書き戻し失敗はエラー扱いにしない（mission は作成済み）
}

if [ -n "${PATCH_RESPONSE:-}" ]; then
  if command -v jq &>/dev/null; then
    OK=$(echo "$PATCH_RESPONSE" | jq -r '.ok // false')
  else
    OK=$(python3 -c "import json,sys; d=json.loads('${PATCH_RESPONSE}'); print(str(d.get('ok',False)).lower())")
  fi
  if [ "$OK" = "true" ]; then
    echo "  書き戻し成功"
  else
    echo "  WARNING: 書き戻しレスポンスに問題があります: $PATCH_RESPONSE" >&2
  fi
fi

# ============================================================
# 完了サマリ
# ============================================================
echo ""
echo "========================================"
echo "  完了: request ${REQUEST_ID} → mission ${MISSION_SLUG}"
echo "========================================"
echo ""
echo "次のステップ:"
echo "  1. plan.sh status --mission $MISSION_SLUG でタスクを確認"
echo "  2. 仮タスク t001 を適切なサブタスクに分解"
echo "  3. Worker を起動してタスクを割り当て"
echo ""
echo "完了時の書き戻し:"
echo "  curl -X PATCH ${TASKVIA_URL}/api/requests/${REQUEST_ID} \\"
echo "    -H 'Authorization: Bearer \$TASKVIA_TOKEN' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"status\": \"done\"}'"
