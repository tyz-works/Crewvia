#!/usr/bin/env bash
# hooks/pre-tool-use.sh
# Claude Code PreToolUse hook — Taskvia 承認ゲート
#
# ~/.claude/settings.json に登録:
#   "hooks": {
#     "PreToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "/path/to/hooks/pre-tool-use.sh" }] }]
#   }
#
# 環境変数:
#   TASKVIA_URL               — Taskvia のベースURL (default: https://taskvia.vercel.app)
#   TASKVIA_TOKEN             — Bearer トークン（未設定時はスタンドアロンモード）
#   AGENT_NAME                — エージェント識別子 (default: hostname)
#   TASK_TITLE                — 現在のタスク名 (任意)
#   TASK_ID                   — 現在のタスクID (任意)
#   APPROVAL_TIMEOUT          — 承認ポーリング上限秒数 (default: 600)
#   CREWVIA_PROJECT           — Taskvia に送るプロジェクト識別子 (default: crewvia)
#   CREWVIA_APPROVAL_CHANNEL  — 承認チャネル: taskvia|ntfy|both (default: taskvia)
#   NTFY_URL / NTFY_TOPIC     — ntfy サーバー設定（mode=ntfy|both 時に必要）
#   NTFY_USER / NTFY_PASS     — ntfy Basic 認証（任意）

set -euo pipefail

# 読み取り・メタ系ツール: Taskvia 承認をスキップして即 exit 0
SAFE_TOOLS=(
  Read
  Grep
  Glob
  LS
  NotebookRead
  TodoWrite
  TaskCreate
  TaskGet
  TaskList
  TaskOutput
  TaskStop
  TaskUpdate
  WebFetch
  WebSearch
  Skill
  ToolSearch
  Agent
)

TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"
AGENT_NAME="${AGENT_NAME:-$(hostname -s)}"

# Director は承認不要 — registry で role: director を確認して即通過
_CREWVIA_REPO_EARLY="${CREWVIA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
_REGISTRY="${_CREWVIA_REPO_EARLY}/registry/workers.yaml"
if [ -f "$_REGISTRY" ] && grep -qA1 "name: ${AGENT_NAME}$" "$_REGISTRY" 2>/dev/null; then
  _AGENT_ROLE="$(grep -A3 "name: ${AGENT_NAME}$" "$_REGISTRY" | grep 'role:' | awk '{print $2}' | head -1)"
  if [ "$_AGENT_ROLE" = "director" ]; then
    _DECISION_EMITTED=true
    exit 0
  fi
fi
TASK_TITLE="${TASK_TITLE:-}"
TASK_ID="${TASK_ID:-}"
# APPROVAL_TIMEOUT env var でポーリング上限を上書きできる（デフォルト: 600秒）
TIMEOUT="${APPROVAL_TIMEOUT:-600}"
_SKILL_EXCEPTION=false

_CREWVIA_REPO="${CREWVIA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# 承認チャネルライブラリを読み込む
# shellcheck source=hooks/lib_approval_channel.sh
if [ -f "${_CREWVIA_REPO}/hooks/lib_approval_channel.sh" ]; then
  . "${_CREWVIA_REPO}/hooks/lib_approval_channel.sh"
  load_ntfy_config
  _APPROVAL_CHANNEL_MODE="$(get_approval_channel_mode)"
else
  _APPROVAL_CHANNEL_MODE="taskvia"
fi

# Claude Code PreToolUse hook の permission 決定を stdout に出力する
_DECISION_EMITTED=false
_APPROVAL_LOG_DIR="${_CREWVIA_REPO}/registry/approvals"

emit_decision() {
  _DECISION_EMITTED=true
  local decision="$1" reason="$2"

  # ローカル承認ログ（safe tool の allow 以外を記録）
  if [ "$decision" != "allow" ] || [[ "$reason" != "Safe tool:"* && "$reason" != "Non-destructive command" ]]; then
    mkdir -p "$_APPROVAL_LOG_DIR"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      "$AGENT_NAME" \
      "${TASK_ID:-}" \
      "$decision" \
      "${TOOL_SUMMARY:-$TOOL_NAME}" \
      "$reason" \
      >> "${_APPROVAL_LOG_DIR}/approvals.tsv"
  fi

  jq -nc \
    --arg d "$decision" \
    --arg r "$reason" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: $d, permissionDecisionReason: $r}}'
}

# set -e で hook がクラッシュした場合、decision 未発行なら deny を発行する安全弁
# これにより Claude Code が native TUI プロンプトにフォールバックするのを防ぐ
_crash_guard() {
  if ! $_DECISION_EMITTED; then
    echo "[pre-tool-use] ⚠️ crash guard: hook exited without decision" >&2
    jq -nc '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: "Hook crashed without emitting decision"}}' 2>/dev/null || true
  fi
}
trap '_crash_guard' EXIT

# env に TASK_ID がなければ assignments ファイルから補完する
_TASK_FILE=""
if [ -z "$TASK_ID" ] && [ -n "$AGENT_NAME" ]; then
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

# stdin から hook の JSON ペイロードを読む
INPUT="$(cat)"
TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // "unknown"')"
TOOL_INPUT="$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || echo '{}')"

# 読み取り・メタ系ツールは即通過（明示的 allow で crash guard を抑制）
for _safe in "${SAFE_TOOLS[@]}"; do
  if [ "$TOOL_NAME" = "$_safe" ]; then
    emit_decision "allow" "Safe tool: ${TOOL_NAME}"
    exit 0
  fi
done

# tool_input から簡易サマリーを作成
TOOL_SUMMARY="${TOOL_NAME}"
COMMAND="$(echo "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null || true)"
FILE_PATH="$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null || true)"
if [ -n "$COMMAND" ]; then
  TOOL_SUMMARY="${TOOL_NAME}($(echo "$COMMAND" | head -c 80))"
elif [ -n "$FILE_PATH" ]; then
  TOOL_SUMMARY="${TOOL_NAME}(${FILE_PATH})"
fi

# --- Skill-based permission check ---
_SKILL_PERMS_YAML="${_CREWVIA_REPO}/config/skill-permissions.yaml"
_SKILL_PERMS_PY="${_CREWVIA_REPO}/hooks/lib_skill_perms.py"

# ツール署名を構築（_global.deny と skill チェック両方で使う）
if [ "$TOOL_NAME" = "Bash" ] && [ -n "$COMMAND" ]; then
  _TOOL_SIG="Bash(${COMMAND})"
else
  _TOOL_SIG="${TOOL_NAME}"
fi

# _global.deny は全チェックに先行する絶対安全弁（何があってもバイパスされない）
# 注意: CREWVIA_TASKVIA=disabled でも _global.deny は発動する（意図的な設計）
if [ -f "$_SKILL_PERMS_YAML" ] && [ -f "$_SKILL_PERMS_PY" ]; then
  _GLOBAL_RESULT="$(python3 "$_SKILL_PERMS_PY" "$_SKILL_PERMS_YAML" "__global_only__" "$_TOOL_SIG" 2>/dev/null || echo '{"decision":"none"}')"
  _GLOBAL_DECISION="$(echo "$_GLOBAL_RESULT" | jq -r '.decision')"
  if [ "$_GLOBAL_DECISION" = "deny" ]; then
    _GLOBAL_SOURCE="$(echo "$_GLOBAL_RESULT" | jq -r '.source // "unknown"')"
    echo "[skill-perms] ❌ global denied: ${_TOOL_SIG} (${_GLOBAL_SOURCE})" >&2
    emit_decision "deny" "Global permission denied: ${_GLOBAL_SOURCE}"
    exit 0
  fi
fi

# Bash コマンドの安全性判定（_global.deny 通過後に評価）
if [ "$TOOL_NAME" = "Bash" ] && [ -n "$COMMAND" ]; then

  # 壊滅的コマンド — 承認不可・即拒否（コマンド先頭のみマッチ）
  _CATASTROPHIC_PREFIXES=(
    "rm -rf /"
    "rm -rf ~"
    "rm -rf ."
    "mkfs "
    "mkfs."
    "dd if="
    ":(){ :|:& };:"
  )
  for _cat in "${_CATASTROPHIC_PREFIXES[@]}"; do
    if [[ "$COMMAND" == ${_cat}* ]]; then
      echo "[pre-tool-use] 🚫 catastrophic command blocked: ${COMMAND}" >&2
      emit_decision "deny" "Catastrophic command blocked: ${_cat}"
      exit 0
    fi
  done

  _NEEDS_APPROVAL=false

  # 機密ファイルパターン — コマンド文字列に含まれていたら承認必須
  _SENSITIVE_PATTERNS=(.env .pem .key _rsa _ed25519 _dsa .secret credentials .ssh/ .aws/ .config/)
  for _pat in "${_SENSITIVE_PATTERNS[@]}"; do
    if [[ "$COMMAND" == *"$_pat"* ]]; then
      _NEEDS_APPROVAL=true
      break
    fi
  done

  # 破壊的 / 外部影響コマンド — prefix マッチで承認必須
  _DANGEROUS_COMMANDS=(
    "rm "
    "curl "
    "wget "
    "ssh "
    "scp "
    "rsync "
    "docker "
    "kubectl "
    "terraform "
    "sudo "
    "chmod "
    "chown "
    "npm publish"
  )
  for _dcmd in "${_DANGEROUS_COMMANDS[@]}"; do
    if [[ "$COMMAND" == ${_dcmd}* ]]; then
      _NEEDS_APPROVAL=true
      break
    fi
  done

  if ! $_NEEDS_APPROVAL; then
    emit_decision "allow" "Non-destructive command"
    exit 0
  fi
fi

# Per-skill チェック（SKILLS がある場合のみ）
if [ -n "${SKILLS:-}" ] && [ -f "$_SKILL_PERMS_YAML" ] && [ -f "$_SKILL_PERMS_PY" ]; then
  # タスクファイルから skills を取得 (フォールバック: SKILLS env)
  _TASK_SKILLS="${SKILLS}"
  if [ -n "${_TASK_FILE:-}" ] && [ -f "${_TASK_FILE:-}" ]; then
    _TS="$(grep '^skills:' "$_TASK_FILE" 2>/dev/null | head -1 | sed 's/^skills:[[:space:]]*//' | tr -d '[]"' | tr ',' ' ' | xargs | tr ' ' ',' || true)"
    [ -n "$_TS" ] && _TASK_SKILLS="$_TS"
  fi

  _PERM_RESULT="$(python3 "$_SKILL_PERMS_PY" "$_SKILL_PERMS_YAML" "$_TASK_SKILLS" "$_TOOL_SIG" 2>/dev/null || echo '{"decision":"none"}')"
  _PERM_DECISION="$(echo "$_PERM_RESULT" | jq -r '.decision')"
  _PERM_SOURCE="$(echo "$_PERM_RESULT" | jq -r '.source // "unknown"')"

  case "$_PERM_DECISION" in
    allow)
      emit_decision "allow" "Skill permission: ${_PERM_SOURCE}"
      exit 0
      ;;
    deny)
      # urgent タスクなら Taskvia に例外リクエストとして転送
      if [ -n "${_TASK_FILE:-}" ] && grep -q '^priority:[[:space:]]*urgent' "$_TASK_FILE" 2>/dev/null; then
        echo "[skill-perms] ⚠️ deny but urgent task, forwarding to Taskvia: ${_TOOL_SIG}" >&2
        _SKILL_EXCEPTION=true
        # fall through to Taskvia
      else
        echo "[skill-perms] ❌ denied: ${_TOOL_SIG} (${_PERM_SOURCE})" >&2
        emit_decision "deny" "Skill permission denied: ${_PERM_SOURCE}"
        exit 0
      fi
      ;;
    # none → fall through to Taskvia
  esac
fi

# Taskvia 無効モード: CREWVIA_TASKVIA=disabled または トークン未設定なら承認なしで通過
# ただし skill-deny の urgent 例外は Taskvia なしでは承認できないため拒否する
if [ "${CREWVIA_TASKVIA:-}" = "disabled" ] || [ -z "$TASKVIA_TOKEN" ]; then
  if [ "$_SKILL_EXCEPTION" = "true" ]; then
    echo "[skill-perms] ❌ urgent exception denied: Taskvia unavailable for approval" >&2
    emit_decision "deny" "Skill deny (urgent): Taskvia unavailable for exception approval"
    exit 0
  fi
  # Taskvia 無効時は native permission にフォールバック（crash guard 抑制）
  _DECISION_EMITTED=true
  exit 0
fi

# 優先度判定: Bash / Write / Edit → high、その他 → medium
PRIORITY="medium"
case "$TOOL_NAME" in Bash|Write|Edit) PRIORITY="high" ;; esac

AUTH_HEADER="Authorization: Bearer ${TASKVIA_TOKEN}"

# ntfy/both モードでは Taskvia に notify:true を渡し Taskvia 側で ntfy publish させる (α方針)
_NOTIFY_FLAG="false"
case "${_APPROVAL_CHANNEL_MODE:-taskvia}" in
  ntfy|both) _NOTIFY_FLAG="true" ;;
esac

# 承認リクエスト投入
PAYLOAD="$(jq -nc \
  --arg tool   "$TOOL_SUMMARY" \
  --arg agent  "$AGENT_NAME" \
  --arg title  "${TASK_TITLE:-Untitled}" \
  --arg tid    "${TASK_ID:-}" \
  --arg prio   "$PRIORITY" \
  --arg proj   "${CREWVIA_PROJECT:-crewvia}" \
  --argjson exc "${_SKILL_EXCEPTION}" \
  --argjson notify "${_NOTIFY_FLAG}" \
  '{tool: $tool, agent: $agent, task_title: $title, task_id: ($tid | if . == "" then null else . end), priority: $prio, project: $proj, exception: $exc, notify: $notify}' 2>/dev/null)" || {
  emit_decision "deny" "Taskvia payload construction failed"
  exit 0
}

RESPONSE="$(curl -sf --connect-timeout 5 --max-time 10 -X POST "${TASKVIA_URL}/api/request" \
  -H "Content-Type: application/json" \
  -H "$AUTH_HEADER" \
  -d "$PAYLOAD" 2>/dev/null)" || RESPONSE=""

CARD_ID="$(echo "$RESPONSE" | jq -r '.id' 2>/dev/null)" || CARD_ID=""

if [ -z "$CARD_ID" ] || [ "$CARD_ID" = "null" ]; then
  echo "[taskvia] リクエスト投入失敗。デフォルト拒否。" >&2
  emit_decision "deny" "Taskvia request submission failed"
  exit 0
fi

echo "[taskvia] 承認待ち: ${TOOL_SUMMARY} (id=${CARD_ID}, channel=${_APPROVAL_CHANNEL_MODE:-taskvia})" >&2

# ポーリング（1秒間隔・TIMEOUT秒）
for i in $(seq 1 "$TIMEOUT"); do
  sleep 1
  STATUS="$(curl -sf --connect-timeout 5 --max-time 10 "${TASKVIA_URL}/api/status/${CARD_ID}" \
    -H "$AUTH_HEADER" \
    | jq -r '.status' 2>/dev/null || echo "error")"

  case "$STATUS" in
    approved)
      echo "[taskvia] ✅ 承認済み: ${TOOL_SUMMARY}" >&2
      emit_decision "allow" "Taskvia approved (id=${CARD_ID})"
      exit 0
      ;;
    denied)
      echo "[taskvia] ❌ 拒否: ${TOOL_SUMMARY}" >&2
      emit_decision "deny" "Taskvia denied (id=${CARD_ID})"
      exit 0
      ;;
    not_found)
      echo "[taskvia] TTL切れ（拒否扱い）: ${TOOL_SUMMARY}" >&2
      emit_decision "deny" "Taskvia card not found / TTL expired (id=${CARD_ID})"
      exit 0
      ;;
  esac
done

echo "[approval] ⏱️ タイムアウト（${TIMEOUT}秒）: ${TOOL_SUMMARY} (channel=${_APPROVAL_CHANNEL_MODE:-taskvia})" >&2
emit_decision "deny" "Approval timed out after ${TIMEOUT}s (channel=${_APPROVAL_CHANNEL_MODE:-taskvia}, id=${CARD_ID})"
exit 0
