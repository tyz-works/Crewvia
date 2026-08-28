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

# クラッシュガード: set -euo pipefail で予期せず exit した場合に exit 0 で収束させる。
# PostToolUse はログ投稿のみで、失敗してもエージェント動作に影響しないため exit 0 が正しい。
# trap の登録を set -euo pipefail の直後に置くことで、以降のどの行でクラッシュしても捕捉できる。
_CURRENT_STEP="init"
_crash_guard() {
  local _EXIT_CODE=$?
  if [ "$_EXIT_CODE" -ne 0 ]; then
    echo "[post-tool-use] ⚠️ crash guard: hook exited unexpectedly (exit=${_EXIT_CODE}, step=${_CURRENT_STEP})" >&2
  fi
  exit 0
}
trap '_crash_guard' EXIT

_CURRENT_STEP="env-setup"
TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"
AGENT_NAME="${AGENT_NAME:-$(hostname -s)}"
TASK_TITLE="${TASK_TITLE:-}"
TASK_ID="${TASK_ID:-}"

# env に TASK_ID がなければ assignments ファイルから補完する
_CURRENT_STEP="task-id-lookup"
if [ -z "$TASK_ID" ] && [ -n "$AGENT_NAME" ]; then
  _CREWVIA_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
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
_CURRENT_STEP="activity-log"
if [ -n "${AGENT_NAME:-}" ] && [ -n "${TASK_ID:-}" ]; then
  _ACTIVITY_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  ACTIVITY_DIR="${_ACTIVITY_REPO}/registry/activity/${AGENT_NAME}"
  mkdir -p "$ACTIVITY_DIR"
  echo "$(date +%s) tool=${CLAUDE_TOOL_NAME:-unknown}" >> "${ACTIVITY_DIR}/${TASK_ID}.activity"
fi

# --- Heartbeat ファイル更新 (task_162 P2是正) ---
# heartbeat は crewvia 内部の信号(watchdog.py が読む)であり Taskvia とは無関係。
# 以前は下の Taskvia ガードの下流にあり standalone モードでは一切書かれなかった
# (順序の事故であり設計判断ではない — task_162 Picard裁定)。activity と同じ扱いにし、
# ガードより前(無条件実行)へ移す。Taskvia への役割・スキル送信(/api/agents)自体は
# 引き続きガードの下流のままで変更していない(下記参照)。
_CURRENT_STEP="heartbeat"
if [[ -n "${AGENT_NAME:-}" ]]; then
  _HB_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  HEARTBEAT_DIR="${_HB_REPO}/registry/heartbeats"
  mkdir -p "$HEARTBEAT_DIR"
  date +%s > "${HEARTBEAT_DIR}/${AGENT_NAME}" 2>/dev/null || true
fi

# Taskvia 無効モード: CREWVIA_TASKVIA=disabled または トークン未設定なら投稿スキップ
if [ "${CREWVIA_TASKVIA:-}" = "disabled" ] || [ -z "$TASKVIA_TOKEN" ]; then
  exit 0
fi

# stdin から hook の JSON ペイロードを読む
_CURRENT_STEP="read-input"
INPUT="$(cat)"
TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // "unknown"')"
TOOL_INPUT="$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || echo '{}')"

# tool_input の先頭80文字をサマリーとして使用
TOOL_INPUT_SUMMARY="$(echo "$TOOL_INPUT" | head -c 80)"

CONTENT="${TOOL_NAME}: ${TOOL_INPUT_SUMMARY}"

# ログペイロード構築
_CURRENT_STEP="build-payload"
PAYLOAD="$(jq -nc \
  --arg type    "work" \
  --arg content "$CONTENT" \
  --arg title   "${TASK_TITLE:-}" \
  --arg tid     "${TASK_ID:-}" \
  --arg agent   "$AGENT_NAME" \
  --arg proj    "${CREWVIA_PROJECT:-crewvia}" \
  '{type: $type, content: $content, task_title: $title, task_id: ($tid | if . == "" then null else . end), agent: $agent, project: $proj}')"

# curl 失敗でもエージェントを止めないため exit 0 で終了
curl -sf -X POST "${TASKVIA_URL}/api/log" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
  -d "$PAYLOAD" >/dev/null 2>&1 || true

# --- Taskvia /api/agents 送信(役割・スキル付きハートビート情報) ---
# heartbeat ファイル自体は上流(ガード前)で既に更新済み。ここは Taskvia への
# メタデータ送信のみ(TASKVIA_TOKEN が設定済みの場合のみここに到達)。
_CURRENT_STEP="agents-heartbeat"
if [[ -n "${AGENT_NAME:-}" ]]; then
  # task_160 F9是正: 汎用名 REPO_ROOT は外部から乗っ取り可能なため CREWVIA_REPO_ROOT を読む
  # (start.sh:248 が既に export 済み)。読み手(watchdog.py)側は変更しないこと — 向きが重要。
  _HB_REPO="${CREWVIA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

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

_CURRENT_STEP="done"
exit 0
