#!/usr/bin/env bash
set -euo pipefail

# kai-review.sh — codex exec review ラッパー (Kai reviewer 専用)
#
# Usage:
#   bash scripts/kai-review.sh --pr <PR#> --task <task_id> [--mission <slug>] [--model <model>]
#
# 処理フロー:
#   1. 引数パース (--pr, --task, --mission, --model)
#   2. gh pr view <PR#> --json headRefName で head branch 取得
#   3. 対象 branch を checkout して diff を確認
#   4. codex exec review --base main -m <model> -o /tmp/kai-review-output.txt を実行
#   5. /tmp/kai-review-output.txt を読み込み findings を判定
#   6. findings なし / all low → plan.sh done
#      修正必要 → plan.sh needs-director

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- 定数 ---
DEFAULT_MODEL=""  # 空 = codex CLI のデフォルトモデルに任せる (--model 未指定)
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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)       PR_NUM="$2";      shift 2 ;;
    --task)     TASK_ID="$2";     shift 2 ;;
    --mission)  MISSION_SLUG="$2"; shift 2 ;;
    --model)    MODEL="$2";       shift 2 ;;
    -h|--help)
      sed -n '3,8p' "$0" | sed 's/^# //'
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
  echo "Usage: bash scripts/kai-review.sh --pr <PR#> --task <task_id> [--mission <slug>] [--model <model>]" >&2
  exit 1
fi

_info "Starting review: PR#${PR_NUM} task=${TASK_ID} model=${MODEL}"

# --- plan.sh パス解決 ---
PLAN_SH="${CREWVIA_REPO_ROOT:-$REPO_ROOT}/scripts/plan.sh"
if [[ ! -f "$PLAN_SH" ]]; then
  _error "plan.sh not found: $PLAN_SH"
  exit 1
fi

# --- gh コマンド確認 ---
if ! command -v gh &>/dev/null; then
  _error "gh command not found. Please install GitHub CLI."
  exit 1
fi

# --- codex コマンド確認 ---
if ! command -v codex &>/dev/null; then
  _error "codex command not found. Please install Codex CLI."
  "$PLAN_SH" needs-director "$TASK_ID" "NEEDS FIX: codex command not found on this machine"
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
