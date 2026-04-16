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

# env に TASK_ID がなければ assignments ファイルから補完する
if [ -z "$TASK_ID" ] && [ -n "$AGENT_NAME" ]; then
  _CREWVIA_REPO="${CREWVIA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  _ASSIGNMENT_FILE="${_CREWVIA_REPO}/queue/assignments/${AGENT_NAME}"
  if [ -f "$_ASSIGNMENT_FILE" ]; then
    _ASSIGNMENT="$(cat "$_ASSIGNMENT_FILE" | tr -d '\n')"
    _MISSION_SLUG="${_ASSIGNMENT%%:*}"
    TASK_ID="${_ASSIGNMENT##*:}"
    _TASK_FILE="${_CREWVIA_REPO}/queue/missions/${_MISSION_SLUG}/tasks/${TASK_ID}.md"
    if [ -f "$_TASK_FILE" ]; then
      TASK_TITLE="$(grep '^title:' "$_TASK_FILE" | head -1 | sed 's/^title:[[:space:]]*//' | sed 's/^"\(.*\)"$/\1/')"
    fi
  fi
fi

# --- Activity logging for Watchdog v2 ---
# Appends a timestamped entry to registry/activity/<AGENT_NAME>/<TASK_ID>.activity
# so that watchdog.py can detect live tool execution activity.
# Runs unconditionally (before Taskvia guard) so it works in standalone mode too.
if [ -n "${AGENT_NAME:-}" ] && [ -n "${TASK_ID:-}" ]; then
  _ACTIVITY_REPO="${CREWVIA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  ACTIVITY_DIR="${_ACTIVITY_REPO}/registry/activity/${AGENT_NAME}"
  mkdir -p "$ACTIVITY_DIR"
  echo "$(date +%s) tool=${CLAUDE_TOOL_NAME:-unknown}" >> "${ACTIVITY_DIR}/${TASK_ID}.activity"
fi

# Taskvia 無効モード: CREWVIA_TASKVIA=disabled または トークン未設定なら投稿スキップ
if [ "${CREWVIA_TASKVIA:-}" = "disabled" ] || [ -z "$TASKVIA_TOKEN" ]; then
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

# SKILLS と AGENT_NAME が設定されている場合のみ knowledge ログを追記
# SKILLS は カンマ区切り（例: "code,typescript"）
if [[ -n "${SKILLS:-}" ]] && [[ -n "${AGENT_NAME:-}" ]]; then
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  IFS=',' read -ra SKILL_ARRAY <<< "$SKILLS"
  for skill in "${SKILL_ARRAY[@]}"; do
    KNOWLEDGE_FILE="${REPO_ROOT}/knowledge/${skill}.md"
    if [[ -f "$KNOWLEDGE_FILE" ]] && [[ -n "${TASK_ID:-}" ]]; then
      printf "\n<!-- log: %s %s %s -->\n" \
        "$(date +%Y-%m-%d)" "${TASK_ID}" "${TOOL_NAME:-unknown}" \
        >> "$KNOWLEDGE_FILE" 2>/dev/null || true
    fi
  done
fi

# --- Heartbeat 自動更新 + Taskvia /api/agents 送信 ---
# AGENT_NAME が設定されている場合、ツール実行のたびに heartbeat を更新する
# Worker は何も意識しなくてよい。hook が自動で処理する。
if [[ -n "${AGENT_NAME:-}" ]]; then
  _HB_REPO="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  HEARTBEAT_DIR="${_HB_REPO}/registry/heartbeats"
  mkdir -p "$HEARTBEAT_DIR"
  date +%s > "${HEARTBEAT_DIR}/${AGENT_NAME}" 2>/dev/null || true

  # workers.yaml からロール・スキルを取得
  _WORKERS_YAML="${_HB_REPO}/registry/workers.yaml"
  _HB_ROLE="worker"
  _HB_SKILLS_STR=""
  if [[ -f "$_WORKERS_YAML" ]]; then
    _HB_AGENT_META="$(python3 - "$AGENT_NAME" "$_WORKERS_YAML" <<'PYEOF' 2>/dev/null || echo "worker|"
import re, sys
from pathlib import Path
agent_name, yaml_path = sys.argv[1], sys.argv[2]
content = Path(yaml_path).read_text()
in_target = False
role = "worker"
skills = []
for line in content.splitlines():
    if re.match(r'\s*- name: ' + re.escape(agent_name) + r'\s*$', line):
        in_target = True
        continue
    if in_target:
        if re.match(r'\s*- name:', line):
            break
        m = re.match(r'\s*role:\s*(.+)', line)
        if m:
            role = m.group(1).strip()
        m = re.match(r'\s*skills:\s*\[(.+)\]', line)
        if m:
            skills = [s.strip() for s in m.group(1).split(",")]
print(f"{role}|{','.join(skills)}")
PYEOF
)"
    _HB_ROLE="${_HB_AGENT_META%%|*}"
    _HB_SKILLS_STR="${_HB_AGENT_META##*|}"
  fi

  # assignments から現在タスク情報を補完（env 未設定時のみ）
  _HB_TASK_ID="${TASK_ID:-}"
  _HB_TASK_TITLE="${TASK_TITLE:-}"
  if [[ -z "$_HB_TASK_ID" ]]; then
    _HB_ASSIGNMENT_FILE="${_HB_REPO}/queue/assignments/${AGENT_NAME}"
    if [[ -f "$_HB_ASSIGNMENT_FILE" ]]; then
      _HB_ASSIGNMENT="$(tr -d '\n' < "$_HB_ASSIGNMENT_FILE")"
      _HB_MISSION="${_HB_ASSIGNMENT%%:*}"
      _HB_TASK_ID="${_HB_ASSIGNMENT##*:}"
      _HB_TASK_FILE="${_HB_REPO}/queue/missions/${_HB_MISSION}/tasks/${_HB_TASK_ID}.md"
      if [[ -f "$_HB_TASK_FILE" ]]; then
        _HB_TASK_TITLE="$(grep '^title:' "$_HB_TASK_FILE" | head -1 | sed 's/^title:[[:space:]]*//' | sed 's/^"\(.*\)"$/\1/')"
      fi
    fi
  fi

  # Taskvia /api/agents にハートビートを送信（TASKVIA_TOKEN が設定済みの場合のみここに到達）
  _AGENTS_PAYLOAD="$(jq -nc \
    --arg name   "$AGENT_NAME" \
    --arg role   "$_HB_ROLE" \
    --arg skills "$_HB_SKILLS_STR" \
    --arg tid    "${_HB_TASK_ID:-}" \
    --arg ttitle "${_HB_TASK_TITLE:-}" \
    '{name: $name, role: $role, skills: ($skills | split(",") | map(select(. != ""))), current_task_id: ($tid | if . == "" then null else . end), current_task_title: ($ttitle | if . == "" then null else . end)}')"

  curl -sf -X POST "${TASKVIA_URL}/api/agents" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
    -d "$_AGENTS_PAYLOAD" >/dev/null 2>&1 || true
fi


exit 0
