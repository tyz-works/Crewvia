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
if [ -n "$COMMAND" ]; then
  TOOL_SUMMARY="${TOOL_NAME}($(echo "$COMMAND" | head -c 80))"
elif [ -n "$FILE_PATH" ]; then
  TOOL_SUMMARY="${TOOL_NAME}(${FILE_PATH})"
fi

# --- Task file direct-write guard: Bash 経由の task ファイル書き込みを防ぐ (t015) ---
# 背景: t004 で review skill の Worker (review/research/verify/planning は
# config/skill-permissions.yaml で Edit/Write/MultiEdit を deny されており、
# ファイルを書く手段が Bash しか無い) が Result を
# `cat >> queue/missions/<slug>/tasks/t004.md <<'EOF' ... EOF` で書き込もうとして
# ハングした (CPU は回っていたが 42 分間無応答)。過去にも research skill Worker が
# 同じパターンで 9 分以上ハングした記録がある (再発)。
#
# 根本原因は plan.sh を経由せず task ファイルを直接シェルリダイレクトで書こうと
# したこと自体にある。cmd_done は result を build_task_body 経由で body に
# 書くだけで frontmatter には触れないため、`plan.sh done <task_id> "<全文>"` に
# 複数行を渡すのは完全に安全 (1 行制約が必要なのは `needs-director` の reason
# だけ)。つまり task ファイルへの直接書き込みには正規の用途が存在しない。
#
# 対象: TOOL_NAME=Bash の COMMAND が、以下のいずれかの書き込み系構文で
#   `queue/missions/**/tasks/*.md` を対象にしている場合のみ deny する
#   - `>` / `>>` によるリダイレクト (heredoc と組み合わせた `cat >> file <<EOF` /
#     `cat <<EOF > file` のどちらの語順でも、最終的にリダイレクト演算子の直後に
#     パスが来る点は変わらないため、この 1 パターンで両方を捕捉できる)
#   - `sed -i`
#   - `tee`
# t011/t014 の worktree edit guard と同じ配置・crash guard 作法 (skill チェック
# より前、emit_decision "deny" + exit 0) に従う。Director はこの関数より前の
# role チェックで既に exit 済みのため対象外 (t011/t014 と同じ前提)。
#
# ★ スコープを意図的に絞っている: heredoc 自体はテストスクリプト作成等で
#   正当に使われるため、対象は「queue/missions/**/tasks/*.md への書き込み」
#   のみに限定する。他の heredoc / リダイレクトは一切対象にしない。
# ★ plan.sh 自身の書き込みは対象外: plan.sh は task ファイルを Python の
#   open()/write() で書いており、シェルリダイレクト構文を一切使わないため、
#   `./scripts/plan.sh done ...` のようなコマンド文字列はそもそもこのパターンに
#   マッチしない (誤検知しない)。
# ★ 既知の残存リスク (worker.md に明記する): `cd queue/missions/<slug> && sed -i
#   ... tasks/t001.md` のように相対パスの先頭に `queue/missions/` が現れない
#   形、変数展開 (`F=queue/missions/.../t001.md; cat >> "$F"`)、`dd of=...`、
#   `python3 -c "open('...').write(...)"` はシェルの cwd/変数状態を追跡したり
#   コマンドの意味を解釈したりしないため、このガードでは捕捉できない (t019:
#   Seo 最終レビューで実測)。t011/t014 の Bash 経由書き込みと同様、
#   ツールベースの構造的ガードは「最終防波堤」であり、worker.md の明文化
#   (Result は plan.sh 経由) が一次防御である前提は変わらない。
#
# t019 (Seo/Opus 5 最終レビュー [P2]) での修正: 上記 3 つの grep はコマンド
# 文字列全体を対象にしているため、リダイレクトが実際のリダイレクトなのか
# クォート内の引用テキストなのかを区別できず、`./scripts/plan.sh done t017
# "原因は cat >> queue/missions/.../t004.md の heredoc"` や `gh pr comment
# 183 --body "guard blocks: cat >> queue/missions/.../t004.md"` のように
# 該当パスを**報告のために引用しただけ**のコマンドまで deny していた (本イン
# シデントを Result / PR コメントで報告しようとする行為そのものが deny され
# る自己矛盾)。
#
# 対策: シングル/ダブルクォートで囲まれた区間を除去した「クォート除去後の
# コマンド文字列」に対して判定する。これにより引用テキストの中に現れる
# `>>`/`sed -i`/`tee` は判定対象から外れる。
#
# ただし「先頭トークンが plan.sh / gh なら丸ごとスキップ」という素朴な除外は
# 採用しない — その除外文字列がコマンドのどこかに含まれていれば良い
# (glob `*plan.sh\ *` は先頭以外にもマッチする) ため、
# `X; ./scripts/plan.sh --help; cat >> queue/missions/.../t004.md <<EOF`
# のような compound command で偽装すればガードを丸ごと迂回できてしまう
# (Director 指摘)。代わりに「クォート除去が安全と判断できる場合に限り
# クォート除去後の文字列で判定し、そうでなければ常に元の $COMMAND 全体を
# 判定する (= 何もしなければ従来どおり検出できる、フェイルセーフな設計)」
# という方式にする。安全と判断できないケース (= 常に元の $COMMAND で判定):
#   - `$(...)` / `` `...` `` (コマンド置換) を含む場合: クォート内であって
#     も実際にシェルへ渡されて実行されるため、除去すると置換内部の本物の
#     書き込みコマンドを見逃してしまう (=検出漏れ)。したがって除去自体を
#     行わない。
#   - クォート除去後もなお `;` `&&` `||` `|` が残っている場合: これらは元々
#     クォートの外側にあった実際の制御演算子であり、compound command で
#     偽装した別コマンドが続いている可能性がある。したがってこの場合も
#     除去した文字列は使わず、元の $COMMAND 全体で判定する
#     (`X; ./scripts/plan.sh --help; cat >> .../t004.md <<EOF` は
#     `;` が残るため除去版は使われず、元の $COMMAND に対する grep が
#     `cat >> .../t004.md` を検出して deny する)。
#
# t023 (Seo/Opus 5 再レビュー [P2]) での追加修正: 上の「いずれの場合も除去
# しないだけなので新たな見逃しは生まれない」という当初の想定は誤りだった。
# `cat >> "queue/missions/m1/tasks/t004.md" <<EOF` のように**パス自体を
# クォートで囲む**ごく自然な書き方は、`$(...)` も制御演算子も含まないため
# フェイルセーフ分岐に入らず、クォート除去版がそのまま判定に使われる。
# するとクォートで囲まれた本物のパスがクォートごと丸ごと消え、判定から
# 抜け落ちてしまう (t004 の再発防止という本来の目的に対して最も素直な書き方
# が抜ける、という Seo 実測による指摘。元の 3 つの grep が `['"]?` を持って
# クォート付きパスを明示的に想定していたのに、その想定を一括除去が壊した形)。
#
# 対策: 一括除去の**前に**、「クォート区間の中身が丸ごと task ファイルパスと
# 一致する場合に限り」そのクォートだけを外す (中身は残す) 前処理を挟む。
# 散文 (`"原因は cat >> .../t004.md の heredoc"` 等) はパスの前後に他の語を
# 含むため `^パスだけ$` に一致せずクォートが外れない → 続く一括除去で丸ごと
# 消える (t019 の誤 deny 修正は維持)。一方 `cat >> "queue/missions/.../t004.md"`
# のようにクォートの中身がパスそのものである場合は、このクォートだけが先に
# 外れてパスが裸のテキストとして残るため、続く一括除去の対象にならず生き残る
# (Seo が実測で 10 ケース全通過を確認済みの方式)。
if [ "$TOOL_NAME" = "Bash" ] && [ -n "$COMMAND" ]; then
  _QM_CHECK_CMD="$COMMAND"
  case "$COMMAND" in
    *'$('*|*'`'*)
      : # コマンド置換あり → クォート除去は行わず元の $COMMAND のまま判定
      ;;
    *)
      # クォート区間の中身が丸ごと task ファイルパスに一致する場合のみ、
      # そのクォートだけを剥がす (中身のパスは残す)。sed の区切り文字は
      # パスに `/` を含むため `@` を使う。
      _QM_PATH_RE='[A-Za-z0-9_./-]*queue/missions/[A-Za-z0-9_.-]+/tasks/[A-Za-z0-9_-]+\.md'
      _CMD_UNQUOTE_PATH="$(printf '%s' "$COMMAND" | sed -E \
        "s@'(${_QM_PATH_RE})'@\1@g; s@\"(${_QM_PATH_RE})\"@\1@g")"
      _CMD_NO_QUOTES="$(printf '%s' "$_CMD_UNQUOTE_PATH" | sed "s/'[^']*'//g; s/\"[^\"]*\"//g")"
      case "$_CMD_NO_QUOTES" in
        *';'*|*'&&'*|*'||'*|*'|'*)
          : # クォート除去後も制御演算子が残る (=元からクォート外) →
          #   compound command の可能性があるため元の $COMMAND のまま判定
          ;;
        *)
          _QM_CHECK_CMD="$_CMD_NO_QUOTES"
          ;;
      esac
      ;;
  esac

  _TASK_FILE_WRITE=0
  if echo "$_QM_CHECK_CMD" | grep -qE '>{1,2}[[:space:]]*['"'"'\"]?[A-Za-z0-9_./-]*queue/missions/[A-Za-z0-9_.-]+/tasks/[A-Za-z0-9_-]+\.md'; then
    _TASK_FILE_WRITE=1
  elif echo "$_QM_CHECK_CMD" | grep -qE 'sed[[:space:]]+-i[^|;&]*queue/missions/[A-Za-z0-9_.-]+/tasks/[A-Za-z0-9_-]+\.md'; then
    _TASK_FILE_WRITE=1
  elif echo "$_QM_CHECK_CMD" | grep -qE '\btee\b[^|;&]*queue/missions/[A-Za-z0-9_.-]+/tasks/[A-Za-z0-9_-]+\.md'; then
    _TASK_FILE_WRITE=1
  fi
  if [ "$_TASK_FILE_WRITE" = "1" ]; then
    echo "[pre-tool-use] 🚫 task file direct write blocked: $(echo "$COMMAND" | head -c 200)" >&2
    emit_decision "deny" "task ファイル (queue/missions/**/tasks/*.md) への直接書き込みは禁止されています (過去に heredoc がハングした事故が複数回あります)。'./scripts/plan.sh done <task_id> \"<Result全文>\"' で記録してください (複数行可。1行制約が必要なのは needs-director の reason だけです)。"
    exit 0
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
