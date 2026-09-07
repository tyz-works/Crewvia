#!/usr/bin/env bash
set -euo pipefail

# kai-review.sh — codex exec review ラッパー (Kai reviewer 専用)
#
# Usage:
#   bash scripts/kai-review.sh --pr <PR#> --task <task_id>
#                              [--mission <slug>] [--model <model>] [--agent <name>]
#
# 処理フロー (Phase 2):
#   1. 引数パース (--pr, --task, --mission, --model, --agent)
#   2. heartbeat 更新 (dispatcher.publish_agents が Kai-codex を認識するため)
#   3. plan.sh pull --task <task> --agent <agent> --skills codex-review
#      → task.status: pending → in_progress + Taskvia PATCH + assignment file
#   4. gh pr view <PR#> --json headRefName で head branch 取得
#   5. 対象 branch を checkout して diff を確認
#   6. codex exec review --base main -m <model> -o /tmp/kai-review-output.txt を実行
#   7. /tmp/kai-review-output.txt を読み込み findings を判定
#   8. findings なし / all low → plan.sh done
#      修正必要 → plan.sh needs-director
#      (plan.sh done は Taskvia sync + registry.workers.yaml の task_count 自動 bump)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- 定数 ---
DEFAULT_MODEL=""  # 空 = codex CLI のデフォルトモデルに任せる (--model 未指定)
DEFAULT_AGENT="Kai-codex"  # registry/workers.yaml に登録済みの Codex 用 worker 名
OUTPUT_FILE="/tmp/kai-review-output.txt"

# --- カラー出力 ---
_info()  { echo "[kai-review] $*"; }
_warn()  { echo "[kai-review] WARNING: $*" >&2; }
_error() { echo "[kai-review] ERROR: $*" >&2; }

# --- 引数パース ---
PR_NUM=""
TASK_ID=""
MISSION_SLUG=""
MODEL="$DEFAULT_MODEL"
AGENT="$DEFAULT_AGENT"
SKIP_PULL=0  # デバッグ用: plan.sh pull を skip する (task が既に in_progress の場合の再実行時など)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)         PR_NUM="$2";       shift 2 ;;
    --task)       TASK_ID="$2";      shift 2 ;;
    --mission)    MISSION_SLUG="$2"; shift 2 ;;
    --model)      MODEL="$2";        shift 2 ;;
    --agent)      AGENT="$2";        shift 2 ;;
    --skip-pull)  SKIP_PULL=1;       shift 1 ;;
    -h|--help)
      sed -n '3,20p' "$0" | sed 's/^# //'
      exit 0
      ;;
    *)
      _error "Unknown option: $1"
      exit 1
      ;;
  esac
done

# --- 必須引数チェック ---
if [[ -z "$PR_NUM" || -z "$TASK_ID" ]]; then
  _error "--pr <PR#> and --task <task_id> are required"
  echo "Usage: bash scripts/kai-review.sh --pr <PR#> --task <task_id> [--mission <slug>] [--model <model>] [--agent <name>]" >&2
  exit 1
fi

# AGENT_NAME を export しておくと plan.sh done が assignment file を掃除できる
export AGENT_NAME="$AGENT"

_info "Starting review: PR#${PR_NUM} task=${TASK_ID} agent=${AGENT} model=${MODEL}"

# --- plan.sh パス解決 ---
PLAN_SH="${CREWVIA_REPO_ROOT:-$REPO_ROOT}/scripts/plan.sh"
if [[ ! -f "$PLAN_SH" ]]; then
  _error "plan.sh not found: $PLAN_SH"
  exit 1
fi

# --- heartbeat 更新 (Phase 2) ---
# dispatcher.publish_agents は registry/heartbeats/<agent> の mtime を見て
# Taskvia に agent presence を送る。ここで touch しないと Kai-codex が
# カンバンに表示されない。alive 判定閾値は AGENT_PRESENCE_TTL=600s。
HEARTBEATS_DIR="${CREWVIA_REPO_ROOT:-$REPO_ROOT}/registry/heartbeats"
mkdir -p "$HEARTBEATS_DIR"
touch "$HEARTBEATS_DIR/$AGENT"

# --- plan.sh pull で task を in_progress に遷移させる (Phase 2) ---
# これにより:
#   - task.status: pending → in_progress
#   - task.worker: null → $AGENT (plan.sh done の bump_task_count が発火する条件)
#   - queue/assignments/$AGENT: dispatcher に「in-flight」を伝える
#   - Taskvia PATCH (taskvia_sync_pull) が発火
#
# 既に in_progress の task を dispatcher が誤って再 spawn した場合、
# plan.sh pull は「already in_progress」で exit 1 する。今回の実行は abort する。
# 手動で再実行するときは --skip-pull を指定する。
if [[ $SKIP_PULL -eq 0 ]]; then
  _info "Pulling task ${TASK_ID} as ${AGENT}..."
  PULL_ARGS=(pull --task "$TASK_ID" --agent "$AGENT" --skills codex-review)
  if [[ -n "$MISSION_SLUG" ]]; then
    PULL_ARGS+=(--mission "$MISSION_SLUG")
  fi
  # plan.sh pull は結果を stdout に JSON で吐くが、ここでは status/assignment 更新が
  # 主目的なので出力は捨てる。ただし失敗時は stderr が見える方が良いので tee はしない。
  if ! "$PLAN_SH" "${PULL_ARGS[@]}" >/dev/null; then
    _error "plan.sh pull failed for task ${TASK_ID} — aborting review"
    exit 1
  fi
else
  _info "SKIP_PULL=1: skipping plan.sh pull (assumes task is already in_progress with worker=${AGENT})"
fi

# --- gh コマンド確認 ---
if ! command -v gh &>/dev/null; then
  _error "gh command not found. Please install GitHub CLI."
  "$PLAN_SH" needs-director "$TASK_ID" ${MISSION_SLUG:+--mission "$MISSION_SLUG"} "NEEDS FIX: gh command not found on this machine"
  exit 1
fi

# --- codex コマンド確認 ---
if ! command -v codex &>/dev/null; then
  _error "codex command not found. Please install Codex CLI."
  "$PLAN_SH" needs-director "$TASK_ID" ${MISSION_SLUG:+--mission "$MISSION_SLUG"} "NEEDS FIX: codex command not found on this machine"
  exit 1
fi

# --- PR の head branch 取得 ---
_info "Fetching PR#${PR_NUM} head branch..."
HEAD_BRANCH=$(gh pr view "$PR_NUM" --json headRefName --jq '.headRefName' 2>&1) || {
  _error "Failed to get PR#${PR_NUM} info: $HEAD_BRANCH"
  "$PLAN_SH" needs-director "$TASK_ID" "NEEDS FIX: gh pr view #${PR_NUM} failed — ${HEAD_BRANCH}"
  exit 1
}

if [[ -z "$HEAD_BRANCH" ]]; then
  _error "headRefName is empty for PR#${PR_NUM}"
  "$PLAN_SH" needs-director "$TASK_ID" "NEEDS FIX: headRefName empty for PR#${PR_NUM}"
  exit 1
fi

_info "PR#${PR_NUM} head branch: ${HEAD_BRANCH}"

# --- CREWVIA_REPO_ROOT 直下で実行 (worktree 非依存) ---
WORK_DIR="${CREWVIA_REPO_ROOT:-$REPO_ROOT}"
_info "Working directory: ${WORK_DIR}"

# --- head branch を fetch して checkout ---
_info "Fetching origin/${HEAD_BRANCH}..."
git -C "$WORK_DIR" fetch origin "$HEAD_BRANCH" 2>&1 || {
  _warn "fetch failed, trying to proceed with existing local branch"
}

_info "Checking out ${HEAD_BRANCH}..."
git -C "$WORK_DIR" checkout "$HEAD_BRANCH" 2>&1 || {
  _error "Failed to checkout branch: ${HEAD_BRANCH}"
  "$PLAN_SH" needs-director "$TASK_ID" "NEEDS FIX: checkout ${HEAD_BRANCH} failed for PR#${PR_NUM}"
  exit 1
}

# --- 出力ファイルを初期化 ---
: > "$OUTPUT_FILE"

# --- codex exec review 実行 ---
_info "Running codex exec review --base main ${MODEL:+-m $MODEL} ..."
CODEX_EXIT=0
codex exec review \
  --base main \
  ${MODEL:+-m "$MODEL"} \
  --ephemeral \
  -o "$OUTPUT_FILE" \
  2>&1 | tee /tmp/kai-review-stderr.txt || CODEX_EXIT=$?

if [[ $CODEX_EXIT -ne 0 ]]; then
  _error "codex exec failed with exit code ${CODEX_EXIT} — cannot trust review output"
  "$PLAN_SH" needs-director "$TASK_ID" ${MISSION_SLUG:+--mission "$MISSION_SLUG"} "CODEX FAILURE: exit=${CODEX_EXIT} — review not completed reliably"
  exit 1
fi

# --- 出力ファイル確認 ---
if [[ ! -f "$OUTPUT_FILE" ]]; then
  _error "Output file not created: ${OUTPUT_FILE}"
  "$PLAN_SH" needs-director "$TASK_ID" "NEEDS FIX: codex review produced no output file (exit=${CODEX_EXIT})"
  exit 1
fi

REVIEW_CONTENT="$(cat "$OUTPUT_FILE")"
_info "Review output length: ${#REVIEW_CONTENT} chars"

# --- findings 判定 ---
# "LGTM" / "no issues" / "no findings" などのパターンを検出
# 修正が必要なキーワード (ERROR, CRITICAL, HIGH, MEDIUM, BUG, SECURITY, VULNERABILITY) を検出
NEEDS_FIX=0

# 修正必要キーワード（大文字小文字無視で検索）
if echo "$REVIEW_CONTENT" | grep -qiE 'critical|high severity|security vulnerability|must fix|must be fixed|breaking change|data loss'; then
  NEEDS_FIX=1
fi

# LGTM 判定: 明示的 LGTM パターンが一致し、かつ critical/high 系 keyword がない場合のみ LGTM
# 明示的 LGTM なし → Kai の意図不明として保守的に needs-director とする
LGTM_PATTERN='no issues|lgtm|looks good|no findings|no problems|no concerns|nothing to report'
if [[ $NEEDS_FIX -eq 0 ]]; then
  if ! echo "$REVIEW_CONTENT" | grep -qiE "$LGTM_PATTERN"; then
    _warn "No explicit LGTM pattern found and no critical keywords — treating as needs-director for safety"
    NEEDS_FIX=1
  fi
fi

# レビュー内容が空 or 極端に短い場合は要確認
if [[ ${#REVIEW_CONTENT} -lt 20 ]]; then
  _warn "Review output is very short (${#REVIEW_CONTENT} chars), treating as needs-director"
  NEEDS_FIX=1
fi

# --- 結果報告 ---
# 先頭200文字をサマリとして使用
SUMMARY="${REVIEW_CONTENT:0:200}"
SUMMARY="${SUMMARY//$'\n'/ }"  # 改行をスペースに置換

if [[ $NEEDS_FIX -eq 1 ]]; then
  _info "Review found issues requiring fixes"
  "$PLAN_SH" needs-director "$TASK_ID" ${MISSION_SLUG:+--mission "$MISSION_SLUG"} "NEEDS FIX: PR#${PR_NUM} ${HEAD_BRANCH} — ${SUMMARY}"
else
  _info "Review passed (LGTM or no critical issues)"
  "$PLAN_SH" done "$TASK_ID" ${MISSION_SLUG:+--mission "$MISSION_SLUG"} "LGTM: PR#${PR_NUM} ${HEAD_BRANCH} reviewed by Kai (model=${MODEL}) — ${SUMMARY}"
fi

_info "Review complete."
