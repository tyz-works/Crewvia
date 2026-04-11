#!/usr/bin/env bash
set -euo pipefail

# start.sh — Launch a crewvia agent (Orchestrator or Worker)
# Usage:
#   ./scripts/start.sh orchestrator               # Start as Orchestrator
#   ./scripts/start.sh worker [skill1 skill2 ...]  # Start as Worker with given skills

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- crewvia.yaml からシステム設定を読み込む ---
# 環境変数が既に設定されていれば config より優先される。
# wip_limit 以外の設定を追加する場合はここにロード処理を足す。
CONFIG_FILE="${REPO_ROOT}/config/crewvia.yaml"
if [[ -f "$CONFIG_FILE" ]] && [[ -z "${CREWVIA_WIP_LIMIT:-}" ]]; then
  WIP_FROM_CONFIG=$(grep -E '^wip_limit:[[:space:]]*[0-9]+' "$CONFIG_FILE" | awk '{print $2}' | tr -d '"' | head -1)
  if [[ -n "$WIP_FROM_CONFIG" ]]; then
    export CREWVIA_WIP_LIMIT="$WIP_FROM_CONFIG"
  fi
fi
# Default fallback（config ファイル無しでも必ず値が入る）
export CREWVIA_WIP_LIMIT="${CREWVIA_WIP_LIMIT:-8}"

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 orchestrator | worker [skill1 skill2 ...]" >&2
  exit 1
fi

ROLE="$1"
shift || true

case "$ROLE" in
  orchestrator)
    AGENT_FILE="agents/orchestrator.md"
    SKILLS_ARR=()
    ;;
  worker)
    AGENT_FILE="agents/worker.md"
    SKILLS_ARR=("$@")
    ;;
  *)
    echo "ERROR: Unknown role '$ROLE'. Use 'orchestrator' or 'worker'." >&2
    exit 1
    ;;
esac

# Determine AGENT_NAME
REGISTRY_YAML="${REPO_ROOT}/registry/workers.yaml"

if [[ "${ROLE}" == "orchestrator" ]]; then
  # Check if an orchestrator is already registered
  EXISTING_ORCH=$(python3 - "$REGISTRY_YAML" <<'PYEOF'
import sys, re

path = sys.argv[1]
try:
    with open(path) as f:
        lines = f.readlines()
except FileNotFoundError:
    sys.exit(0)

current = None
for line in lines:
    content = re.sub(r'\s*#.*$', '', line.rstrip()).rstrip()
    if not content:
        continue
    m = re.match(r'^\s+-\s+name:\s+(\S+)', content)
    if m:
        current = {'name': m.group(1)}
        continue
    if current is not None:
        if re.match(r'^\s+role:\s+orchestrator', content):
            print(current['name'])
            sys.exit(0)
PYEOF
)

  if [[ -n "$EXISTING_ORCH" ]]; then
    AGENT_NAME="$EXISTING_ORCH"
  else
    # First launch: prompt for name or use random
    echo ""
    echo "[crewvia] 初回起動です。Orchestrator の名前を入力してください。"
    echo "          半角英字のみ（例: Alex）。Enter で自動割り当て。"
    printf "> "
    read -r INPUT_NAME </dev/tty || INPUT_NAME=""
    if [[ "$INPUT_NAME" =~ ^[a-zA-Z]+$ ]]; then
      AGENT_NAME="$INPUT_NAME"
    else
      AGENT_NAME=$(bash "${SCRIPT_DIR}/assign-name.sh")
    fi
    # Register orchestrator in registry
    mkdir -p "${REPO_ROOT}/registry"
    python3 - "$REGISTRY_YAML" "$AGENT_NAME" <<'PYEOF'
import sys, re
from datetime import date

path, name = sys.argv[1], sys.argv[2]
today = str(date.today())

try:
    with open(path) as f:
        content = f.read()
except FileNotFoundError:
    content = 'workers: []\n'

entry = (
    f"  - name: {name}\n"
    f"    role: orchestrator\n"
    f"    task_count: 0\n"
    f"    last_active: {today}\n"
)

if content.strip() == 'workers: []' or content.strip() == 'workers:':
    content = f"workers:\n{entry}"
else:
    content = content.rstrip('\n') + '\n' + entry

with open(path, 'w') as f:
    f.write(content)
PYEOF
    echo "[crewvia] Orchestrator '${AGENT_NAME}' を registry に登録しました。"
  fi
else
  AGENT_NAME=$(bash "${SCRIPT_DIR}/assign-name.sh" "${SKILLS_ARR[@]+"${SKILLS_ARR[@]}"}")
fi

# --- tmux モード選択（Orchestrator 起動時のみ、CREWVIA_TMUX 未設定時のみ） ---
# Orchestrator が tmux モードを選ぶと、この env は exec claude に引き継がれ、
# Orchestrator が後続で起動する Worker もすべて tmux ウィンドウで動く。
if [[ "${ROLE}" == "orchestrator" ]] && [[ -z "${CREWVIA_TMUX:-}" ]]; then
  if ! command -v tmux >/dev/null 2>&1; then
    echo "[crewvia] tmux 未検出 → インラインモードで起動します。" >&2
    echo "          （並列 Worker 起動には 'brew install tmux' を推奨）" >&2
    export CREWVIA_TMUX=0
  elif [[ ! -e /dev/tty ]]; then
    # 非対話環境（CI など）: tmux モードはスキップ
    export CREWVIA_TMUX=0
  else
    echo ""
    echo "[crewvia] tmux を使ってマルチエージェント並列モードで起動しますか？"
    echo "          Y = tmux モード（Orchestrator と Worker を crewvia セッションに展開）"
    echo "          n = インラインモード（Worker 並列起動不可、単独セッション）"
    printf "> [Y/n]: "
    read -r TMUX_CHOICE </dev/tty || TMUX_CHOICE=""
    case "$TMUX_CHOICE" in
      n|N|no|No|NO)
        export CREWVIA_TMUX=0
        echo "[crewvia] インラインモードで起動します。"
        ;;
      *)
        export CREWVIA_TMUX=1
        echo "[crewvia] tmux モードで起動します（セッション名: crewvia）。"
        echo "          別ターミナルから 'tmux attach -t crewvia' で覗けます。"
        ;;
    esac
  fi
fi

export AGENT_NAME
export ROLE
export TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"

# Export SKILLS as comma-separated env var
if [[ ${#SKILLS_ARR[@]} -gt 0 ]]; then
  export SKILLS="$(IFS=','; echo "${SKILLS_ARR[*]}")"
else
  export SKILLS=""
fi

echo "[crewvia] Starting as $AGENT_NAME ($ROLE)"

# Check for TASKVIA_TOKEN
if [[ -z "${TASKVIA_TOKEN:-}" ]]; then
  echo "[crewvia] WARNING: TASKVIA_TOKEN is not set. Running in standalone mode (no Taskvia integration)." >&2
fi

# Resolve agent markdown path
AGENT_MD="${REPO_ROOT}/${AGENT_FILE}"
if [[ ! -f "$AGENT_MD" ]]; then
  echo "[crewvia] WARNING: Agent file not found: $AGENT_MD. Running without system prompt." >&2
  AGENT_MD=""
fi

# --- Worker: load knowledge files and update registry last_active ---
FULL_PROMPT=""

if [[ "${ROLE}" == "worker" ]]; then
  # Build knowledge context from per-skill knowledge files
  KNOWLEDGE_CONTEXT=""
  for skill in "${SKILLS_ARR[@]+"${SKILLS_ARR[@]}"}"; do
    KNOWLEDGE_FILE="${REPO_ROOT}/knowledge/${skill}.md"
    if [[ -f "$KNOWLEDGE_FILE" ]] && [[ -s "$KNOWLEDGE_FILE" ]]; then
      KNOWLEDGE_CONTEXT+=$'\n\n'"## ${skill} ナレッジベース"$'\n'"$(cat "$KNOWLEDGE_FILE")"
    fi
  done

  # Build full prompt: identity header + agent.md + knowledge context
  NAME_HEADER="# Worker Identity

あなたの名前は **${AGENT_NAME}** です。
担当スキル: ${SKILLS:-（未指定）}

registry/workers.yaml でこの名前のエントリを確認し、過去の task_count を把握してください。"

  BASE_PROMPT="${AGENT_MD:+$(cat "$AGENT_MD")}"
  FULL_PROMPT="${NAME_HEADER}"$'\n\n'"${BASE_PROMPT}${KNOWLEDGE_CONTEXT}"

  # Update last_active for this worker in registry
  REGISTRY_YAML="${REPO_ROOT}/registry/workers.yaml"
  if [[ -f "$REGISTRY_YAML" ]]; then
    python3 - "$REGISTRY_YAML" "$AGENT_NAME" <<'PYEOF'
import sys, re
from datetime import date

path, agent_name = sys.argv[1], sys.argv[2]
today = str(date.today())

with open(path) as f:
    lines = f.readlines()

in_target = False
result = []
for line in lines:
    m_name = re.match(r'^\s+-\s+name:\s+(\S+)', line)
    if m_name:
        in_target = (m_name.group(1) == agent_name)
    if in_target and re.match(r'^\s+last_active:', line):
        line = re.sub(r'(last_active:\s+)\S+', rf'\g<1>{today}', line)
    result.append(line)

with open(path, 'w') as f:
    f.writelines(result)
PYEOF
  fi

else
  # Orchestrator: identity header + agent.md
  NAME_HEADER="# Orchestrator Identity

あなたの名前は **${AGENT_NAME}** です。

registry/workers.yaml を読んで現在のチーム構成を把握してください。"

  BASE_PROMPT="${AGENT_MD:+$(cat "$AGENT_MD")}"
  FULL_PROMPT="${NAME_HEADER}"$'\n\n'"${BASE_PROMPT}"
fi

# Build prompt flag
PROMPT_FLAG=()
if [[ -n "$FULL_PROMPT" ]]; then
  PROMPT_FLAG=(--append-system-prompt "$FULL_PROMPT")
fi

# Launch with or without tmux
if [[ "${CREWVIA_TMUX:-0}" == "1" ]]; then
  SESSION="crewvia"
  WINDOW_NAME="${AGENT_NAME}-${ROLE}"

  ENV_EXPORTS="export AGENT_NAME='$AGENT_NAME' TASKVIA_URL='$TASKVIA_URL' ROLE='$ROLE' SKILLS='${SKILLS:-}'"
  [[ -n "${TASKVIA_TOKEN:-}" ]] && ENV_EXPORTS+=" TASKVIA_TOKEN='$TASKVIA_TOKEN'"

  if [[ -n "$FULL_PROMPT" ]]; then
    # Write prompt to temp file to avoid quoting issues with large content in send-keys
    PROMPT_TMPFILE=$(mktemp /tmp/crewvia_prompt.XXXXXX)
    printf '%s' "$FULL_PROMPT" > "$PROMPT_TMPFILE"
    LAUNCH_CMD="$ENV_EXPORTS; cd '$REPO_ROOT'; claude --append-system-prompt \"\$(cat '${PROMPT_TMPFILE}')\""
  else
    LAUNCH_CMD="$ENV_EXPORTS; cd '$REPO_ROOT'; claude"
  fi

  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[crewvia] Creating tmux session: $SESSION"
    tmux new-session -d -s "$SESSION" -n "$WINDOW_NAME"
    TARGET="${SESSION}:${WINDOW_NAME}"
  else
    tmux new-window -t "$SESSION" -n "$WINDOW_NAME"
    TARGET="${SESSION}:${WINDOW_NAME}"
  fi

  tmux send-keys -t "$TARGET" "$LAUNCH_CMD"
  tmux send-keys -t "$TARGET" Enter
  echo "[crewvia] Agent launched in tmux window: ${SESSION}:${WINDOW_NAME}"
else
  # Default: run inline (no tmux)
  # Orchestrator 起動時に watchdog をバックグラウンドで起動
  if [[ "${ROLE}" == "orchestrator" ]]; then
    WATCHDOG="${REPO_ROOT}/scripts/watchdog.sh"
    if [[ -f "$WATCHDOG" ]]; then
      bash "$WATCHDOG" &
      WATCHDOG_PID=$!
      echo "[crewvia] Watchdog started (PID: ${WATCHDOG_PID})"
      trap "kill ${WATCHDOG_PID} 2>/dev/null || true" EXIT
    fi
  fi
  exec claude "${PROMPT_FLAG[@]+"${PROMPT_FLAG[@]}"}"
fi
