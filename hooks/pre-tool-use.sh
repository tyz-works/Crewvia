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
)

TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
TASKVIA_TOKEN="${TASKVIA_TOKEN:-}"
AGENT_NAME="${AGENT_NAME:-$(hostname -s)}"
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
emit_decision() {
  local decision="$1" reason="$2"
  jq -nc \
    --arg d "$decision" \
    --arg r "$reason" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: $d, permissionDecisionReason: $r}}'
}

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

# 読み取り・メタ系ツールは即通過
for _safe in "${SAFE_TOOLS[@]}"; do
  if [ "$TOOL_NAME" = "$_safe" ]; then
    exit 0
  fi
done

# tool_input から簡易サマリーを作成
TOOL_SUMMARY="${TOOL_NAME}"
COMMAND="$(echo "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null || true)"
if [ -n "$COMMAND" ]; then
  TOOL_SUMMARY="${TOOL_NAME}($(echo "$COMMAND" | head -c 80))"
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

# _global.deny は SKILLS 有無・Taskvia 有効/無効に関係なく常にチェック（絶対安全弁）
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
  exit 0
fi

# 優先度判定: Bash / Write / Edit → high、その他 → medium
PRIORITY="medium"
case "$TOOL_NAME" in Bash|Write|Edit) PRIORITY="high" ;; esac

AUTH_HEADER="Authorization: Bearer ${TASKVIA_TOKEN}"

# ntfy モードでは Taskvia に ntfy=true を渡してトークン URL を生成させる
_NTFY_FLAG="false"
case "${_APPROVAL_CHANNEL_MODE:-taskvia}" in
  ntfy|both) _NTFY_FLAG="true" ;;
esac

# 承認リクエスト投入
PAYLOAD="$(jq -nc \
  --arg tool   "$TOOL_SUMMARY" \
  --arg agent  "$AGENT_NAME" \
  --arg title  "${TASK_TITLE:-Untitled}" \
  --arg tid    "${TASK_ID:-}" \
  --arg prio   "$PRIORITY" \
  --argjson exc "${_SKILL_EXCEPTION}" \
  --argjson ntfy "${_NTFY_FLAG}" \
  '{tool: $tool, agent: $agent, task_title: $title, task_id: ($tid | if . == "" then null else . end), priority: $prio, exception: $exc, ntfy: $ntfy}')"

RESPONSE="$(curl -sf --connect-timeout 5 --max-time 10 -X POST "${TASKVIA_URL}/api/request" \
  -H "Content-Type: application/json" \
  -H "$AUTH_HEADER" \
  -d "$PAYLOAD")"

CARD_ID="$(echo "$RESPONSE" | jq -r '.id')"

if [ -z "$CARD_ID" ] || [ "$CARD_ID" = "null" ]; then
  echo "[taskvia] リクエスト投入失敗。デフォルト拒否。" >&2
  emit_decision "deny" "Taskvia request submission failed"
  exit 0
fi

# ntfy / both モードでは Crewvia 側から直接 ntfy 通知を送信する
_NTFY_SENT=false
case "${_APPROVAL_CHANNEL_MODE:-taskvia}" in
  ntfy|both)
    _TOKEN_URLS="$(parse_token_urls "$RESPONSE")"
    _APPROVE_URL="${_TOKEN_URLS%% *}"
    _DENY_URL="${_TOKEN_URLS##* }"
    if [ -n "$_APPROVE_URL" ] && [ "$_APPROVE_URL" != "$_DENY_URL" ]; then
      if ntfy_publish "$AGENT_NAME" "$TOOL_SUMMARY" "$_APPROVE_URL" "$_DENY_URL"; then
        _NTFY_SENT=true
      else
        # ntfy 送信失敗時のフォールバック
        if [ "${_APPROVAL_CHANNEL_MODE}" = "ntfy" ]; then
          # mode=ntfy: 通知手段がないため skill-permissions の静的ルールで判定する
          echo "[ntfy] ⚠️ ntfy 送信失敗。skill-permissions フォールバックを適用します。" >&2
          if [ -f "$_SKILL_PERMS_YAML" ] && [ -f "$_SKILL_PERMS_PY" ]; then
            _FB_RESULT="$(python3 "$_SKILL_PERMS_PY" "$_SKILL_PERMS_YAML" "${SKILLS:-}" "$_TOOL_SIG" 2>/dev/null || echo '{"decision":"none"}')"
            _FB_DECISION="$(echo "$_FB_RESULT" | jq -r '.decision')"
            if [ "$_FB_DECISION" = "allow" ]; then
              _FB_SOURCE="$(echo "$_FB_RESULT" | jq -r '.source // "skill-permissions"')"
              echo "[ntfy] ✅ skill-permissions allow: ${_TOOL_SIG} (${_FB_SOURCE})" >&2
              emit_decision "allow" "ntfy failed; skill-permissions fallback: ${_FB_SOURCE}"
              exit 0
            fi
          fi
          # allow ルールなし → 承認者に届かないため拒否
          echo "[ntfy] ❌ ntfy 送信失敗かつ skill-permissions allow なし。ツール実行を拒否します。" >&2
          emit_decision "deny" "ntfy publish failed; no approval channel available"
          exit 0
        else
          # mode=both: Taskvia WebUI で承認可能なため警告のみで継続
          echo "[ntfy] ⚠️ ntfy 送信失敗。Taskvia WebUI で承認を継続します。" >&2
        fi
      fi
    else
      echo "[ntfy] approve_url/deny_url が取得できませんでした。taskvia フォールバックで継続します。" >&2
    fi
    ;;
esac

echo "[taskvia] 承認待ち: ${TOOL_SUMMARY} (id=${CARD_ID})" >&2

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
