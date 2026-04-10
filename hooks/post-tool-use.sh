#!/usr/bin/env bash
# hooks/post-tool-use.sh
# Claude Code PostToolUse hook — Taskvia 作業ログ投稿
#
# ~/.claude/settings.json に登録:
#   "hooks": {
#     "PostToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "/path/to/hooks/post-tool-use.sh" }] }]
#   }
#
# 環境変数:
#   TASKVIA_URL    — Taskvia のベースURL (default: https://taskvia.vercel.app)
#   TASKVIA_TOKEN  — Bearer トークン（未設定時はスキップ）
#   AGENT_NAME     — エージェント識別子 (default: hostname)
#   TASK_TITLE     — 現在のタスク名 (任意)
#   TASK_ID        — 現在のタスクID (任意)

set -euo pipefail

TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"
AGENT_NAME="${AGENT_NAME:-$(hostname -s)}"
TASK_TITLE="${TASK_TITLE:-}"
TASK_ID="${TASK_ID:-}"

# スタンドアロンモード: トークン未設定なら投稿スキップ
if [ -z "$TASKVIA_TOKEN" ]; then
  exit 0
fi

# stdin から hook の JSON ペイロードを読む
INPUT="$(cat)"
TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // "unknown"')"
TOOL_INPUT="$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || echo '{}')"

# tool_input の先頭80文字をサマリーとして使用
TOOL_INPUT_SUMMARY="$(echo "$TOOL_INPUT" | head -c 80)"

CONTENT="${TOOL_NAME}: ${TOOL_INPUT_SUMMARY}"

# ログペイロード構築
PAYLOAD="$(jq -nc \
  --arg type    "work" \
  --arg content "$CONTENT" \
  --arg title   "${TASK_TITLE:-}" \
  --arg tid     "${TASK_ID:-}" \
  --arg agent   "$AGENT_NAME" \
  '{type: $type, content: $content, task_title: $title, task_id: ($tid | if . == "" then null else . end), agent: $agent}')"

# curl 失敗でもエージェントを止めないため exit 0 で終了
curl -sf -X POST "${TASKVIA_URL}/api/log" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
  -d "$PAYLOAD" >/dev/null 2>&1 || true

exit 0
