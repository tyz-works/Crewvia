#!/usr/bin/env bash
set -euo pipefail

# start.sh — Launch a crewvia agent (Director or Worker)
# Usage:
#   ./scripts/start.sh director               # Start as Director
#   ./scripts/start.sh worker [skill1 skill2 ...]  # Start as Worker with given skills

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- .env ファイルから環境変数を読み込む ---
# 既存の env var は上書きしない（env > .env > config の優先順位を維持）
if [[ -f "${REPO_ROOT}/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    if [[ -z "${!key:-}" ]]; then
      export "$line"
    fi
  done < "${REPO_ROOT}/.env"
fi

# --- crewvia.yaml からシステム設定を読み込む ---
# 環境変数が既に設定されていれば config より優先される。
# 設定を追加する場合はここにロード処理を足す。
CONFIG_FILE="${REPO_ROOT}/config/crewvia.yaml"
if [[ -f "$CONFIG_FILE" ]]; then
  if [[ -z "${CREWVIA_WIP_LIMIT:-}" ]]; then
    WIP_FROM_CONFIG=$(grep -E '^wip_limit:[[:space:]]*[0-9]+' "$CONFIG_FILE" | awk '{print $2}' | tr -d '"' | head -1)
    if [[ -n "$WIP_FROM_CONFIG" ]]; then
      export CREWVIA_WIP_LIMIT="$WIP_FROM_CONFIG"
    fi
  fi
  if [[ -z "${CREWVIA_DIRECTOR_MODEL:-}" ]]; then
    ORCH_MODEL_FROM_CONFIG=$(grep -E '^director_model:[[:space:]]*\S' "$CONFIG_FILE" | awk '{print $2}' | tr -d '"' | head -1)
    if [[ -n "$ORCH_MODEL_FROM_CONFIG" ]]; then
      export CREWVIA_DIRECTOR_MODEL="$ORCH_MODEL_FROM_CONFIG"
    fi
  fi
  if [[ -z "${CREWVIA_WORKER_MODEL:-}" ]]; then
    WORKER_MODEL_FROM_CONFIG=$(grep -E '^worker_model:[[:space:]]*\S' "$CONFIG_FILE" | awk '{print $2}' | tr -d '"' | head -1)
    if [[ -n "$WORKER_MODEL_FROM_CONFIG" ]]; then
      export CREWVIA_WORKER_MODEL="$WORKER_MODEL_FROM_CONFIG"
    fi
  fi
fi
# Default fallback（config ファイル無しでも必ず値が入る）
export CREWVIA_WIP_LIMIT="${CREWVIA_WIP_LIMIT:-8}"

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 director | worker [skill1 skill2 ...]" >&2
  exit 1
fi

ROLE="$1"
shift || true

case "$ROLE" in
  director)
    AGENT_FILE="agents/director.md"
    SKILLS_ARR=()
    ;;
  worker)
    AGENT_FILE="agents/worker.md"
    SKILLS_ARR=("$@")
    ;;
  *)
    echo "ERROR: Unknown role '$ROLE'. Use 'director' or 'worker'." >&2
    exit 1
    ;;
esac

# Determine AGENT_NAME
REGISTRY_YAML="${REPO_ROOT}/registry/workers.yaml"

if [[ "${ROLE}" == "director" ]]; then
  # Check if an director is already registered
  EXISTING_ORCH=$(python3 "${SCRIPT_DIR}/lib_registry.py" get-director "$REGISTRY_YAML" 2>/dev/null || true)

  if [[ -n "$EXISTING_ORCH" ]]; then
    AGENT_NAME="$EXISTING_ORCH"
  else
    # First launch: prompt for name or use random
    echo ""
    echo "[crewvia] 初回起動です。Director の名前を入力してください。"
    echo "          半角英字のみ（例: Alex）。Enter で自動割り当て。"
    printf "> "
    read -r INPUT_NAME </dev/tty || INPUT_NAME=""
    if [[ "$INPUT_NAME" =~ ^[a-zA-Z]+$ ]]; then
      AGENT_NAME="$INPUT_NAME"
    else
      AGENT_NAME=$(bash "${SCRIPT_DIR}/assign-name.sh")
    fi
    # Register director in registry
    mkdir -p "${REPO_ROOT}/registry"
    python3 "${SCRIPT_DIR}/lib_registry.py" register-director "$REGISTRY_YAML" "$AGENT_NAME"
    echo "[crewvia] Director '${AGENT_NAME}' を registry に登録しました。"
  fi
else
  if [[ -z "${AGENT_NAME:-}" ]]; then
    AGENT_NAME=$(bash "${SCRIPT_DIR}/assign-name.sh" "${SKILLS_ARR[@]+"${SKILLS_ARR[@]}"}")
  fi
fi

# --- tmux モード選択（Director 起動時のみ、CREWVIA_TMUX 未設定時のみ） ---
# Director が tmux モードを選ぶと、この env は exec claude に引き継がれ、
# Director が後続で起動する Worker もすべて tmux ウィンドウで動く。
if [[ "${ROLE}" == "director" ]] && [[ -z "${CREWVIA_TMUX:-}" ]]; then
  # config/crewvia.yaml の mode 設定を読む
  MODE_FROM_CONFIG=$(grep -E '^mode:[[:space:]]*\S' "$CONFIG_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"' | head -1)
  if [[ "$MODE_FROM_CONFIG" == "tmux" ]]; then
    export CREWVIA_TMUX=1
    echo "[crewvia] tmux モードで起動します（config 設定）。"
  elif [[ "$MODE_FROM_CONFIG" == "inline" ]]; then
    export CREWVIA_TMUX=0
    echo "[crewvia] インラインモードで起動します（config 設定）。"
  elif ! command -v tmux >/dev/null 2>&1; then
    echo "[crewvia] tmux 未検出 → インラインモードで起動します。" >&2
    echo "          （並列 Worker 起動には 'brew install tmux' を推奨）" >&2
    export CREWVIA_TMUX=0
  elif [[ ! -e /dev/tty ]]; then
    # 非対話環境（CI など）: tmux モードはスキップ
    export CREWVIA_TMUX=0
  else
    echo ""
    echo "[crewvia] tmux を使ってマルチエージェント並列モードで起動しますか？"
    echo "          Y = tmux モード（Director と Worker を crewvia セッションに展開）"
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

# --- 承認チャネル設定を config から読み込む ---
# CREWVIA_APPROVAL_CHANNEL が未設定の場合は config を読む
_read_approval_channel_yaml() {
  local cfg="$1" key_path="$2"
  [[ ! -f "$cfg" ]] && return
  awk -v kp="$key_path" '
    BEGIN { in_ac=0; in_ntfy=0; n=split(kp,a,"."); tk=a[1]; sk=( n>1 ? a[2] : "" ) }
    /^approval_channel:/ { in_ac=1; next }
    in_ac && /^[a-zA-Z_]/ { in_ac=0; in_ntfy=0 }
    in_ac {
      if (/^  ntfy:/) { in_ntfy=1; next }
      if (in_ntfy && /^  [a-zA-Z_]/ && !/^    /) { in_ntfy=0 }
      if (tk=="mode" && sk=="" && /^  mode:/) {
        v=$0; gsub(/^  mode:[[:space:]]*/,"",v); gsub(/^['"'"'"]|['"'"'"]$/,"",v); print v; exit
      }
      if (tk=="ntfy" && in_ntfy) {
        pfx="    " sk ":"
        if (index($0,pfx)==1) {
          v=substr($0,length(pfx)+1); gsub(/^[[:space:]]*/,"",v)
          gsub(/^['"'"'"]|['"'"'"]$/,"",v); print v; exit
        }
      }
    }
  ' "$cfg"
}

if [[ -z "${CREWVIA_APPROVAL_CHANNEL:-}" ]] && [[ -f "$CONFIG_FILE" ]]; then
  _AC_MODE=$(_read_approval_channel_yaml "$CONFIG_FILE" "mode")
  [[ -n "$_AC_MODE" ]] && export CREWVIA_APPROVAL_CHANNEL="$_AC_MODE"
fi
export CREWVIA_APPROVAL_CHANNEL="${CREWVIA_APPROVAL_CHANNEL:-taskvia}"

# ntfy 設定を config から読み込む（env var 優先）
if [[ -f "$CONFIG_FILE" ]]; then
  [[ -z "${NTFY_URL:-}" ]]                   && export NTFY_URL="$(_read_approval_channel_yaml "$CONFIG_FILE" "ntfy.url")"
  [[ -z "${NTFY_TOPIC:-}" ]]                 && export NTFY_TOPIC="$(_read_approval_channel_yaml "$CONFIG_FILE" "ntfy.topic")"
  [[ -z "${NTFY_USER:-}" ]]                  && export NTFY_USER="$(_read_approval_channel_yaml "$CONFIG_FILE" "ntfy.user")"
  [[ -z "${NTFY_PASS:-}" ]]                  && export NTFY_PASS="$(_read_approval_channel_yaml "$CONFIG_FILE" "ntfy.pass")"
  [[ -z "${APPROVAL_TOKEN_TTL_SECONDS:-}" ]] && export APPROVAL_TOKEN_TTL_SECONDS="$(_read_approval_channel_yaml "$CONFIG_FILE" "ntfy.token_ttl_seconds")"
fi
export NTFY_URL="${NTFY_URL:-}"
export NTFY_TOPIC="${NTFY_TOPIC:-}"
export NTFY_USER="${NTFY_USER:-}"
export NTFY_PASS="${NTFY_PASS:-}"
export APPROVAL_TOKEN_TTL_SECONDS="${APPROVAL_TOKEN_TTL_SECONDS:-900}"

# --- Taskvia 連携モード確定（CREWVIA_TASKVIA 未設定の場合は config を読む） ---
# crewvia ランチャー経由なら ask は既に resolved 済み。直接 start.sh を呼んだ場合は
# ここで config を読み、ask なら非対話フォールバック（enabled）を使う。
if [[ -z "${CREWVIA_TASKVIA:-}" ]]; then
  if [[ -f "$CONFIG_FILE" ]]; then
    TASKVIA_MODE_FROM_CONFIG=$(grep -E '^taskvia:[[:space:]]*\S' "$CONFIG_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"' | head -1)
    case "${TASKVIA_MODE_FROM_CONFIG:-ask}" in
      enabled|disabled|ask) export CREWVIA_TASKVIA="${TASKVIA_MODE_FROM_CONFIG:-ask}" ;;
      *) export CREWVIA_TASKVIA="ask" ;;
    esac
  else
    export CREWVIA_TASKVIA="ask"
  fi
  # ask が残っている場合は enabled にフォールバック（start.sh 直呼び = 非対話前提）
  if [[ "${CREWVIA_TASKVIA}" == "ask" ]]; then
    export CREWVIA_TASKVIA="enabled"
  fi
fi

if [[ "${CREWVIA_TASKVIA}" == "disabled" ]]; then
  echo "[crewvia] Taskvia 連携を無効化します（スタンドアロンモード）。" >&2
  export TASKVIA_TOKEN=""
  export TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
else
  export TASKVIA_URL="${TASKVIA_URL:-https://taskvia.vercel.app}"
  if [[ "${TASKVIA_URL}" != https://* ]]; then
    echo "[crewvia] ERROR: TASKVIA_URL must start with https://: ${TASKVIA_URL}" >&2
    exit 1
  fi
fi

# CREWVIA_REPO: crewvia 本体のパス。Worker の cwd が target project に
# 切り替わった後も、plan.sh / registry / knowledge / hooks などを絶対パスで
# 呼び出せるように、常に export する。
export CREWVIA_REPO="$REPO_ROOT"

# Export SKILLS as comma-separated env var
if [[ ${#SKILLS_ARR[@]} -gt 0 ]]; then
  export SKILLS="$(IFS=','; echo "${SKILLS_ARR[*]}")"
else
  export SKILLS=""
fi

# --- Worker の cwd を決定する ---
# Worker は TARGET_DIR env var が指定されていればそのプロジェクトの cwd で
# claude を起動する (claude は cwd の CLAUDE.md / .claude/settings.json /
# git 状態を読むため、target project の文脈で動かすには cwd 切替が必須)。
# Director は常に crewvia 本体の cwd のまま (registry / queue を管理
# する必要があるため)。
WORK_DIR="$REPO_ROOT"
if [[ "${ROLE}" == "worker" ]] && [[ -n "${TARGET_DIR:-}" ]]; then
  if [[ ! -d "$TARGET_DIR" ]]; then
    echo "[crewvia] ERROR: TARGET_DIR does not exist or is not a directory: $TARGET_DIR" >&2
    exit 1
  fi
  WORK_DIR="$(cd "$TARGET_DIR" && pwd)"  # canonicalize
  echo "[crewvia] Worker target project: $WORK_DIR"
  export TARGET_DIR="$WORK_DIR"  # Worker 側にも canonicalized pathを渡す
fi

echo "[crewvia] Starting as $AGENT_NAME ($ROLE)"

# Check for TASKVIA_TOKEN (try token file if env not set; skip entirely when Taskvia disabled)
if [[ "${CREWVIA_TASKVIA}" != "disabled" ]]; then
  if [[ -z "${TASKVIA_TOKEN:-}" ]]; then
    TOKEN_FILE="${REPO_ROOT}/config/.taskvia-token"
    if [[ -f "$TOKEN_FILE" ]]; then
      chmod 600 "$TOKEN_FILE"
      TASKVIA_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
      export TASKVIA_TOKEN
    else
      echo "[crewvia] WARNING: TASKVIA_TOKEN is not set. Running in standalone mode (no Taskvia integration)." >&2
    fi
  fi
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
    python3 "${SCRIPT_DIR}/lib_registry.py" set-last-active "$REGISTRY_YAML" "$AGENT_NAME"
  fi

else
  # Director: identity header + agent.md
  NAME_HEADER="# Director Identity

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

# Resolve model per role (config / env で指定されていれば --model を渡す)
if [[ "${ROLE}" == "director" ]]; then
  SELECTED_MODEL="${CREWVIA_DIRECTOR_MODEL:-}"
else
  SELECTED_MODEL="${CREWVIA_WORKER_MODEL:-}"
fi
MODEL_FLAG=()
if [[ -n "$SELECTED_MODEL" ]]; then
  MODEL_FLAG=(--model "$SELECTED_MODEL")
  echo "[crewvia] Model: $SELECTED_MODEL"
fi

# Launch with or without tmux
if [[ "${CREWVIA_TMUX:-0}" == "1" ]]; then
  SESSION="crewvia"
  WINDOW_NAME="${AGENT_NAME}-${ROLE}"

  ENV_EXPORTS="export AGENT_NAME='$AGENT_NAME' TASKVIA_URL='$TASKVIA_URL' TASKVIA_TOKEN='${TASKVIA_TOKEN:-}' CREWVIA_TASKVIA='${CREWVIA_TASKVIA:-enabled}' ROLE='$ROLE' SKILLS='${SKILLS:-}' CREWVIA_REPO='$CREWVIA_REPO' CREWVIA_APPROVAL_CHANNEL='${CREWVIA_APPROVAL_CHANNEL:-taskvia}' NTFY_URL='${NTFY_URL:-}' NTFY_TOPIC='${NTFY_TOPIC:-}' NTFY_USER='${NTFY_USER:-}' NTFY_PASS='${NTFY_PASS:-}' APPROVAL_TOKEN_TTL_SECONDS='${APPROVAL_TOKEN_TTL_SECONDS:-900}'"
  [[ "${ROLE}" == "worker" ]] && [[ "$WORK_DIR" != "$REPO_ROOT" ]] && ENV_EXPORTS+=" TARGET_DIR='$WORK_DIR'"

  # --model flag (空なら省略)
  MODEL_CLI_ARG=""
  if [[ -n "$SELECTED_MODEL" ]]; then
    MODEL_CLI_ARG=" --model '$SELECTED_MODEL'"
  fi

  if [[ -n "$FULL_PROMPT" ]] && [[ "${CREWVIA_BENCH_MODE:-0}" != "1" ]]; then
    # Write the system prompt to .claude/settings.json in the target dir so claude
    # picks it up without shell-expansion issues (the prompt contains $ / backticks
    # that would be incorrectly expanded when embedded in a send-keys string).
    # Python handles JSON encoding safely including Japanese / special characters.
    # BENCH_MODE skips this to avoid the ~38KB prompt causing claude to crash;
    # benchmark-ctx.sh writes a minimal bench-specific settings.json instead.
    PROMPT_TMPFILE=$(mktemp /tmp/crewvia_prompt.XXXXXX)
    printf '%s' "$FULL_PROMPT" > "$PROMPT_TMPFILE"
    SETTINGS_DIR="$WORK_DIR/.claude"
    mkdir -p "$SETTINGS_DIR"
    python3 - "$PROMPT_TMPFILE" "$SETTINGS_DIR/settings.json" <<'PYEOF'
import sys, json
prompt = open(sys.argv[1]).read()
try:
    existing = json.load(open(sys.argv[2]))
except Exception:
    existing = {}
existing['systemPrompt'] = (existing.get('systemPrompt') or '') + '\n' + prompt
with open(sys.argv[2], 'w') as f:
    json.dump(existing, f, ensure_ascii=False, indent=2)
PYEOF
  fi
  LAUNCH_CMD="$ENV_EXPORTS; cd '$WORK_DIR'; claude${MODEL_CLI_ARG}"

  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[crewvia] Creating tmux session: $SESSION"
    tmux new-session -d -s "$SESSION" -n "$WINDOW_NAME"
    TARGET="${SESSION}:${WINDOW_NAME}"
  elif tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$WINDOW_NAME"; then
    echo "[crewvia] Window ${SESSION}:${WINDOW_NAME} already exists, skipping launch"
    exit 0
  else
    tmux new-window -t "$SESSION" -n "$WINDOW_NAME"
    TARGET="${SESSION}:${WINDOW_NAME}"
  fi

  tmux send-keys -t "$TARGET" "$LAUNCH_CMD"
  tmux send-keys -t "$TARGET" Enter
  echo "[crewvia] Agent launched in tmux window: ${SESSION}:${WINDOW_NAME}"

  # Claude が起動して入力待ちになるまで待ってから kickoff メッセージを送る。
  # インラインモードは exec claude に引数を渡せるが、tmux モードは
  # ユーザーメッセージがないと Claude がハングするため send-keys で補う。
  sleep 5
  if [[ "${CREWVIA_BENCH_MODE:-0}" != "1" ]]; then
    if [[ "${ROLE}" == "worker" ]]; then
      # TARGET_DIR が設定されている場合は --target-dir を渡して target 不一致タスクをスキップ
      PULL_TARGET_DIR_ARG=""
      [[ -n "${TARGET_DIR:-}" ]] && PULL_TARGET_DIR_ARG=" --target-dir ${TARGET_DIR}"
      KICKOFF_MSG="ミッション開始。./scripts/plan.sh pull --agent ${AGENT_NAME} --skills ${SKILLS}${PULL_TARGET_DIR_ARG} でタスクを取得し、指示に従って作業してください。完了したら ./scripts/plan.sh done で報告し、待機してください（Dispatcher が次のタスクを自動割り当てします）。"
    else
      KICKOFF_MSG="ミッション開始。./scripts/plan.sh status で状態を確認し、タスク分解・Worker 割り当て・全体管理を開始してください。"
    fi
    tmux send-keys -t "$TARGET" "$KICKOFF_MSG"
    tmux send-keys -t "$TARGET" Enter
    echo "[crewvia] Kickoff message sent to ${SESSION}:${WINDOW_NAME}"
  else
    echo "[crewvia] BENCH_MODE: skipping auto-kickoff (benchmark-ctx.sh will control task dispatch)"
  fi

  # Director: dispatcher を crewvia:dispatcher 窓で起動（二重起動防止）
  if [[ "${ROLE}" == "director" ]]; then
    if tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null | grep -q '^dispatcher$'; then
      echo "[crewvia] Dispatcher already running (${SESSION}:dispatcher)"
    else
      tmux new-window -t "$SESSION" -n "dispatcher"
      tmux send-keys -t "${SESSION}:dispatcher" "cd '${REPO_ROOT}' && bash '${SCRIPT_DIR}/dispatcher.sh'" Enter
      echo "[crewvia] Dispatcher started in tmux window: ${SESSION}:dispatcher"
    fi

    # Auto-attach to Director window after kickoff + dispatcher launch.
    # $TMUX が set されている（既に tmux 内にいる）場合は switch-client、
    # tmux 外のシェルなら attach-session で Director 窓に移動する。
    if [[ -n "${TMUX:-}" ]]; then
      tmux switch-client -t "$SESSION"
    else
      echo "[crewvia] Attaching to ${SESSION}:${WINDOW_NAME} ..."
      exec tmux attach-session -t "${SESSION}:${WINDOW_NAME}"
    fi
  fi
else
  # Default: run inline (no tmux)
  # Director 起動時に watchdog をバックグラウンドで起動
  if [[ "${ROLE}" == "director" ]]; then
    WATCHDOG="${REPO_ROOT}/scripts/watchdog.sh"
    if [[ -f "$WATCHDOG" ]]; then
      bash "$WATCHDOG" &
      WATCHDOG_PID=$!
      echo "[crewvia] Watchdog started (PID: ${WATCHDOG_PID})"
      trap "kill ${WATCHDOG_PID} 2>/dev/null || true" EXIT
    fi
  fi
  # Worker 向け cwd 切替 (TARGET_DIR 指定時のみ)。Director はそのまま。
  cd "$WORK_DIR" || {
    echo "[crewvia] ERROR: failed to cd into $WORK_DIR" >&2
    exit 1
  }
  exec claude "${MODEL_FLAG[@]+"${MODEL_FLAG[@]}"}" "${PROMPT_FLAG[@]+"${PROMPT_FLAG[@]}"}"
fi
