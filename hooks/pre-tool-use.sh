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

# クラッシュガードを最上部に登録する。
# これより後のどの箇所で set -e が発動しても crash guard が確実に発火する。
_DECISION_EMITTED=false
_crash_guard_early() {
  if ! $_DECISION_EMITTED; then
    echo "[pre-tool-use] ⚠️ crash guard: hook exited without decision" >&2
    jq -nc '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: "Hook crashed without emitting decision"}}' 2>/dev/null || true
  fi
}
trap '_crash_guard_early' EXIT

# Director は承認不要 — registry で role: director を確認して即通過
_CREWVIA_REPO_EARLY="${CREWVIA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
_REGISTRY="${_CREWVIA_REPO_EARLY}/registry/workers.yaml"
if [ -f "$_REGISTRY" ] && grep -qA1 "name: ${AGENT_NAME}$" "$_REGISTRY" 2>/dev/null; then
  # grep 'role:' が見つからない（一般 Worker）場合に pipefail で落ちないよう || true を付ける
  _AGENT_ROLE="$(grep -A3 "name: ${AGENT_NAME}$" "$_REGISTRY" | grep 'role:' | awk '{print $2}' | head -1 || true)"
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
# 注意: _DECISION_EMITTED と crash guard trap はスクリプト上部に移動済み。
#       ここでは emit_decision を定義し、_APPROVAL_LOG_DIR を設定する。
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

# trap は上部の _crash_guard_early で既に登録済み。
# emit_decision が定義された後に trap を上書きして同名関数を使う（より詳細なメッセージを出力可）
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
# NotebookEdit は file_path ではなく notebook_path を使う (t014: Edit/Write/MultiEdit
# と同じ書き込み系ツールなのにフィールド名が違うため下記ガードで二重に漏れていた)
NOTEBOOK_PATH="$(echo "$TOOL_INPUT" | jq -r '.notebook_path // empty' 2>/dev/null || true)"
if [ -n "$COMMAND" ]; then
  TOOL_SUMMARY="${TOOL_NAME}($(echo "$COMMAND" | head -c 80))"
elif [ -n "$FILE_PATH" ]; then
  TOOL_SUMMARY="${TOOL_NAME}(${FILE_PATH})"
elif [ -n "$NOTEBOOK_PATH" ]; then
  TOOL_SUMMARY="${TOOL_NAME}(${NOTEBOOK_PATH})"
fi

# --- Worktree scope guard: crewvia 自身の main checkout への直接書き込みを防ぐ ---
# 背景 (t009/t011): worktree モードで作業中の Worker が Edit/Write ツールに main repo
# ($CREWVIA_REPO) の絶対パスを渡してしまい、専用 worktree ではなく main checkout
# (branch=main) を直接編集する事故が発生した (git status で気づき自己復旧、実害なし。
# 気づかなければ main に混入していた)。worker.md の明文化だけでは再発を防げないため、
# 構造的なガードをここに入れる。_global.deny と同様、skill 設定や urgent 例外では
# バイパスできない絶対安全弁として扱う（skill/Taskvia チェックより前に判定する）。
#
# 対象: 「worktree を持つ Worker」の Edit/Write/MultiEdit/NotebookEdit のみ
#   = TASK_ID が解決済み (タスクを pull 済み。下記 t014 の注記参照)
#   AND TARGET_DIR が未設定 (target project モードではない = worktree モードのはず)
#   AND anchor ($CREWVIA_REPO) が main checkout であることを .git の種別で確認できる
#     (下記 t014 の注記参照)
#   AND 編集先が $CREWVIA_REPO 配下 かつ queue/ registry/ .claude/worktrees/ 以外
# ★ 新しい書き込み系ツールが増えたら必ずここに追加すること。現状の対象:
#   - Edit/Write/MultiEdit: config/skill-permissions.yaml は全 skill で三点セットで
#     常に許可しており、ファイル書き込みという意味では全く同格のツール。Edit/Write
#     だけを見るガードは MultiEdit 経由で素通しになる control-bypass だった
#     (commit security review で検出、t011 で修正)。
#   - NotebookEdit: 上と全く同じ理由に加え、パスが file_path ではなく notebook_path
#     なので抽出ロジックも二重に漏れていた (t014 で判明。$FILE_PATH の代わりに
#     ${FILE_PATH:-$NOTEBOOK_PATH} を使うことで対応)。
#   ※ Bash 経由での書き込み (heredoc / sed -i / tee 等) はこのガードの対象外
#   のまま残る既知の残存リスク。ツールベースの最終防波堤であり、worker.md の
#   明文化 (呼び出しは $CREWVIA_REPO、編集は worktree 内) が一次防御である
#   前提は変わらない。
# 対象外 (誤爆防止。いずれかに該当すれば即スキップ):
#   - Director (この関数より前の role チェックで既に exit 済み)
#   - TARGET_DIR モードの Worker (worktree を持たない。上記条件で自動的に除外)
#   - TASK_ID 未解決のセッション (対話デバッグ等。上記条件で自動的に除外)
#   - anchor の .git がディレクトリでない場合 (下記 t014 の注記参照)
#   - queue/ registry/ 配下 (plan.sh 等が書く共有領域。正当な経路)
#   - .claude/worktrees/ 配下 (= 自分の worktree、または他 Worker の worktree。
#     いずれも main checkout そのものではないので対象外)
#   - $CREWVIA_REPO の外 (target project 等、無関係のパス)
# 注意: cwd は見ない。「TARGET_DIR 未設定なら plan.sh pull は必ず worktree を作り
# worktree_path を返す」という不変条件があるため、TASK_ID 解決済み + TARGET_DIR
# 未設定なのに worktree の外を指すパスを編集しようとしている時点で、cwd が実際どこに
# あるかによらず既に異常な状態 — パスだけで判定して構わない（むしろ「cwd も main に
# 迷い込んでいる」というより深刻なケースも同時に拾える）。
#
# t014 (Seo/Opus 5 レビュー指摘) での修正:
#   [P1] トリガ条件が env の CREWVIA_TASK_ID 単独だと本番で dead code だった。
#     CREWVIA_TASK_ID を設定する経路 (plan.sh の .crewvia-env 書き出し、worker.md の
#     source) はいずれも Bash tool の subshell 内であり、hook は Claude Code の
#     子プロセスとして起動されるため subshell の export は届かない。実測でも
#     「実運用と同じ env (未設定) → 発火せず素通り」「テストと同じ env (明示付与)
#     → 発火」の差が確認された。実際には TASK_ID は :127-140 の assignments
#     ファイル ($CREWVIA_REPO/queue/assignments/<agent>) fallback で既にここまで
#     に解決済みなので、TASK_ID (env の TASK_ID か assignments 解決結果) を見る
#     ことで本番でも発火するようにする。env の CREWVIA_TASK_ID も (source 済みの
#     同一呼び出し内など稀に届くケース向けに) OR で残す。
#   [P2] _CREWVIA_REPO の fallback (:77) が worktree を指すと、worktree には
#     .claude/worktrees/ が存在せず除外句がどれもヒットしないため、その Worker は
#     自分の worktree すら一切 Edit できなくなる (誤爆で運用停止)。main checkout の
#     .git はディレクトリ、linked worktree の .git はファイルなので、anchor の
#     .git がディレクトリであることを確認できた場合のみガードを有効にする。
#     誤爆で止まるより、ガードが効かないほうがはるかにマシという判断。
if { [ "$TOOL_NAME" = "Edit" ] || [ "$TOOL_NAME" = "Write" ] || [ "$TOOL_NAME" = "MultiEdit" ] || [ "$TOOL_NAME" = "NotebookEdit" ]; } \
   && { [ -n "${TASK_ID:-}" ] || [ -n "${CREWVIA_TASK_ID:-}" ]; } && [ -z "${TARGET_DIR:-}" ]; then
  _GUARD_PATH="${FILE_PATH:-${NOTEBOOK_PATH:-}}"
  if [ -n "$_GUARD_PATH" ]; then
    case "$_GUARD_PATH" in
      /*) _GUARD_ABS_FILE="$_GUARD_PATH" ;;
      *)  _GUARD_ABS_FILE="$(pwd)/$_GUARD_PATH" ;;
    esac
    # realpath -m: パス正規化のみ (存在チェックなし。Write は新規ファイル作成のため
    # 対象ファイルが存在しないケースがある)
    _GUARD_REPO_REAL="$(realpath -m "$_CREWVIA_REPO" 2>/dev/null || echo "$_CREWVIA_REPO")"
    # [P2] anchor が本当に main checkout かどうかを .git の種別で自己確認する。
    # fallback が worktree に化けていた場合はここで .git がファイルになり、
    # ガードそのものを安全に無効化する。
    if [ -d "${_GUARD_REPO_REAL}/.git" ]; then
      _GUARD_FILE_REAL="$(realpath -m "$_GUARD_ABS_FILE" 2>/dev/null || echo "$_GUARD_ABS_FILE")"
      case "$_GUARD_FILE_REAL" in
        "${_GUARD_REPO_REAL}"/queue/*|"${_GUARD_REPO_REAL}"/registry/*|"${_GUARD_REPO_REAL}"/.claude/worktrees/*)
          : # 正当な経路 — 対象外
          ;;
        "${_GUARD_REPO_REAL}"/*)
          echo "[pre-tool-use] 🚫 main repo direct edit blocked: ${_GUARD_FILE_REAL}" >&2
          emit_decision "deny" "worktree Worker が main checkout (${_CREWVIA_REPO}) を直接編集しようとしました。worktree 内のパスを使ってください (cwd: $(pwd))。"
          exit 0
          ;;
        *)
          : # $CREWVIA_REPO の外 (target project 等) — 対象外
          ;;
      esac
    fi
  fi
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
  _SENSITIVE_PATTERNS=(.env .pem .key _rsa _ed25519 _dsa .secret credentials .ssh/ .aws/)
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

  # compound command 内の危険パターン検出（Python 側 _GLOBAL_DENY_SUBSTRINGS と二重化）
  if [[ "$COMMAND" == *"&&"* || "$COMMAND" == *"||"* || "$COMMAND" == *";"* || "$COMMAND" == *"|"* ]]; then
    _CMD_STRIPPED=$(printf '%s' "$COMMAND" | sed "s/'[^']*'//g; s/\"[^\"]*\"//g")
    if printf '%s' "$_CMD_STRIPPED" | grep -qE '\brm\s+-\S*r\S*f'; then
      _NEEDS_APPROVAL=true
    fi
    if printf '%s' "$_CMD_STRIPPED" | grep -qE '\bsudo\b'; then
      _NEEDS_APPROVAL=true
    fi
    if printf '%s' "$_CMD_STRIPPED" | grep -qE '\|\s*(ba)?sh\b'; then
      _NEEDS_APPROVAL=true
    fi
  fi

  if ! $_NEEDS_APPROVAL; then
    # SKILLS 設定時は per-skill チェックへ進む（skill deny を評価するため）
    # SKILLS 未設定時は非破壊コマンドとして即許可
    if [ -z "${SKILLS:-}" ]; then
      emit_decision "allow" "Non-destructive command"
      exit 0
    fi
  fi
fi

# Per-skill チェック（SKILLS がある場合のみ）
# Note: Bash safety の後に配置。Non-Bash ツール (Edit/Write 等) と、
# Bash safety で _NEEDS_APPROVAL=true になったコマンドがここに到達する。
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
