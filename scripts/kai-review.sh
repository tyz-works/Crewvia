#!/usr/bin/env bash
set -euo pipefail

# kai-review.sh — codex exec review ラッパー (Kai reviewer 専用)
#
# Usage:
#   bash scripts/kai-review.sh --pr <PR#> --task <task_id>
#                              [--mission <slug>] [--model <model>] [--agent <name>]
#                              [--skip-pull] [--dry-run]
#
# --skip-pull vs --dry-run (F6, PR#180):
#   --skip-pull : plan.sh pull だけを飛ばす（task は既に in_progress 前提）。
#                 終盤の plan.sh done / needs-director は通常通り実行され、
#                 実タスクの status が書き換わる。「再実行」用。
#   --dry-run   : plan.sh への書き込み (pull / done / needs-director) を
#                 一切行わない。--skip-pull も暗黙で有効になる。判定結果
#                 (done 相当 / needs-director 相当 + サマリ) を stdout に
#                 人が読める形で表示するだけ。実 PR に対する動作確認・
#                 smoke test 用途（実タスクの status を壊さない）。
#
# 処理フロー (Phase 3):
#   1. 引数パース (--pr, --task, --mission, --model, --agent, --skip-pull, --dry-run)
#   2. heartbeat 更新 (dispatcher.publish_agents が Kai-codex を認識するため)
#   3. plan.sh pull --task <task> --agent <agent> --skills codex-review
#      → task.status: pending → in_progress + Taskvia PATCH + assignment file
#      (--skip-pull / --dry-run 時はスキップ)
#   4. gh pr view <PR#> --json headRefName で head branch 取得
#   5. 対象 branch を origin から fetch → 専用 git worktree (mktemp -d) を
#      --detach で作成し、そこで diff を確認する。主 working tree
#      ($CREWVIA_REPO_ROOT) の HEAD は一切動かさない (F1/F1b, PR#180)
#   6. codex exec -C <review-worktree> review --base main -m <model>
#      -o <mktemp output file> を実行
#   7. 出力ファイルを読み込み findings を判定 ([P1]/[P2]/[P3] タグ判定が主経路。
#      JSON 構造化出力にも対応し、旧来の散文キーワード判定は critical 系の
#      defense-in-depth として残す。詳細は「findings 判定」セクション参照)
#   8. findings なし / all-low(P3) → plan.sh done
#      修正必要 (P1/P2 or critical keyword) → plan.sh needs-director
#      (plan.sh done は Taskvia sync + registry.workers.yaml の task_count 自動 bump)
#      --dry-run 時はどちらも実行せず、判定結果を表示するのみ
#   9. レビュー用 worktree・出力/stderr の一時ファイルは trap で必ず後始末する

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- 定数 ---
DEFAULT_MODEL=""  # 空 = codex CLI のデフォルトモデルに任せる (--model 未指定)
DEFAULT_AGENT="Kai-codex"  # registry/workers.yaml に登録済みの Codex 用 worker 名

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
DRY_RUN=0    # F6: plan.sh への書き込み (pull/done/needs-director) を一切行わない smoke-test モード

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)         PR_NUM="$2";       shift 2 ;;
    --task)       TASK_ID="$2";      shift 2 ;;
    --mission)    MISSION_SLUG="$2"; shift 2 ;;
    --model)      MODEL="$2";        shift 2 ;;
    --agent)      AGENT="$2";        shift 2 ;;
    --skip-pull)  SKIP_PULL=1;       shift 1 ;;
    --dry-run)    DRY_RUN=1;         shift 1 ;;
    -h|--help)
      sed -n '3,40p' "$0" | sed 's/^# //'
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
  echo "Usage: bash scripts/kai-review.sh --pr <PR#> --task <task_id> [--mission <slug>] [--model <model>] [--agent <name>] [--skip-pull] [--dry-run]" >&2
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

# --- failure path 共通ヘルパー (F4, PR#180) ---
# 全ての needs-director 呼び出しをここに統一し、--mission 漏れを防ぐ。
# call_needs_director: needs-director を呼ぶだけ（exit しない）。findings 判定後の
#   「review は完走したが修正が必要」パスで使う。--dry-run 時は plan.sh を呼ばず、
#   何を呼ぶはずだったかを stdout に表示するだけにする (F6, PR#180)。
# fail_needs_director: エラーログ + needs-director + exit 1。スクリプト自体が
#   完走できなかった failure path 用。
call_needs_director() {
  if [[ $DRY_RUN -eq 1 ]]; then
    _info "[DRY-RUN] would call: plan.sh needs-director ${TASK_ID} ${MISSION_SLUG:+--mission ${MISSION_SLUG} }-- ${1}"
    return 0
  fi
  "$PLAN_SH" needs-director "$TASK_ID" ${MISSION_SLUG:+--mission "$MISSION_SLUG"} "$1"
}

fail_needs_director() {
  _error "$1"
  call_needs_director "$1"
  exit 1
}

# --- 一時リソースの後始末 (F1b/F3, PR#180) ---
# レビュー用 worktree と出力/stderr の一時ファイルは、成功・失敗どちらの経路でも
# 必ず削除する。REVIEW_WT はここで空文字初期化しておき、set -u 下でも
# cleanup が安全に参照できるようにする。
REVIEW_WT=""
OUTPUT_FILE=""
STDERR_FILE=""

cleanup() {
  if [[ -n "$OUTPUT_FILE" ]]; then
    rm -f "$OUTPUT_FILE"
  fi
  if [[ -n "$STDERR_FILE" ]]; then
    rm -f "$STDERR_FILE"
  fi
  if [[ -n "$REVIEW_WT" && -d "$REVIEW_WT" ]]; then
    git -C "${WORK_DIR:-$REPO_ROOT}" worktree remove --force "$REVIEW_WT" 2>/dev/null \
      || rm -rf "$REVIEW_WT"
  fi
}
trap cleanup EXIT

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
# --dry-run は plan.sh への書き込みを一切行わないため、pull も暗黙でスキップする
# (F6, PR#180) — 実 PR に対する smoke test で実タスクの status を書き換えないため。
if [[ $SKIP_PULL -eq 0 && $DRY_RUN -eq 0 ]]; then
  _info "Pulling task ${TASK_ID} as ${AGENT}..."
  PULL_ARGS=(pull --task "$TASK_ID" --agent "$AGENT" --skills codex-review)
  if [[ -n "$MISSION_SLUG" ]]; then
    PULL_ARGS+=(--mission "$MISSION_SLUG")
  fi
  # plan.sh pull は結果を stdout に JSON で吐くが、ここでは status/assignment 更新が
  # 主目的なので出力は捨てる。ただし失敗時は stderr が見える方が良いので tee はしない。
  # (task がまだ in_progress に遷移できていない = needs-director を打っても plan.sh 側で
  #  弾かれる可能性が高いため、ここは元のまま plain exit とする)
  if ! "$PLAN_SH" "${PULL_ARGS[@]}" >/dev/null; then
    _error "plan.sh pull failed for task ${TASK_ID} — aborting review"
    exit 1
  fi
elif [[ $DRY_RUN -eq 1 ]]; then
  _info "DRY_RUN=1: skipping plan.sh pull (no writes to plan.sh in dry-run mode)"
else
  _info "SKIP_PULL=1: skipping plan.sh pull (assumes task is already in_progress with worker=${AGENT})"
fi

# --- gh コマンド確認 ---
if ! command -v gh &>/dev/null; then
  fail_needs_director "NEEDS FIX: gh command not found on this machine"
fi

# --- codex コマンド確認 ---
if ! command -v codex &>/dev/null; then
  fail_needs_director "NEEDS FIX: codex command not found on this machine"
fi

# --- PR の head branch 取得 ---
_info "Fetching PR#${PR_NUM} head branch..."
HEAD_BRANCH=$(gh pr view "$PR_NUM" --json headRefName --jq '.headRefName' 2>&1) || {
  fail_needs_director "NEEDS FIX: gh pr view #${PR_NUM} failed — ${HEAD_BRANCH}"
}

if [[ -z "$HEAD_BRANCH" ]]; then
  fail_needs_director "NEEDS FIX: headRefName empty for PR#${PR_NUM}"
fi

_info "PR#${PR_NUM} head branch: ${HEAD_BRANCH}"

# --- 主 working tree (worktree 非依存の実体) ---
# git fetch はここで実行するが、checkout はしない。主 WT の HEAD は一切動かさない
# (F1b, PR#180) — review は専用の使い捨て worktree で行う。
WORK_DIR="${CREWVIA_REPO_ROOT:-$REPO_ROOT}"
_info "Repo root (unaffected by review): ${WORK_DIR}"

# --- head branch を fetch ---
# 旧実装は fetch 失敗時に「既存 local branch で続行」していたが、それこそが F1
# (stale branch を review してしまう) の原因だった。fetch が失敗したら安全側に倒して
# needs-director にする。
_info "Fetching origin/${HEAD_BRANCH}..."
if ! git -C "$WORK_DIR" fetch origin "$HEAD_BRANCH" 2>&1; then
  fail_needs_director "NEEDS FIX: git fetch origin ${HEAD_BRANCH} failed for PR#${PR_NUM}"
fi

# --- 専用 review worktree を作成 (F1/F1b, PR#180) ---
# FETCH_HEAD ではなく origin/<branch> を使う: FETCH_HEAD は $WORK_DIR (主リポジトリ)
# 単位で共有される 1 ファイルなので、他プロセスが同時に fetch すると上書きされ得る。
# 直前の fetch で更新された remote-tracking ref を直接指す方が競合を避けられる。
REVIEW_WT="$(mktemp -d "${TMPDIR:-/tmp}/kai-review-wt.XXXXXX")"
_info "Creating isolated review worktree at ${REVIEW_WT} (detached at origin/${HEAD_BRANCH})..."
if ! git -C "$WORK_DIR" worktree add --detach "$REVIEW_WT" "origin/${HEAD_BRANCH}" 2>&1; then
  fail_needs_director "NEEDS FIX: git worktree add failed for PR#${PR_NUM} (${HEAD_BRANCH})"
fi

# --- 出力/stderr ファイル (F3, PR#180) ---
# 固定 /tmp パスは codex-review task が並列実行された場合に相互上書きする事故を招く
# (Kai-codex は dispatcher 側で同時 1 本しか spawn しないが、手動起動との衝突も
#  避けるため mktemp で一意化する)。cleanup trap で削除する。
OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/kai-review-output.XXXXXX")"
STDERR_FILE="$(mktemp "${TMPDIR:-/tmp}/kai-review-stderr.XXXXXX")"

# --- codex exec review 実行 ---
# -C "$REVIEW_WT" で review 対象ディレクトリを指定する (プロセスの cwd はどこでもよい)。
_info "Running codex exec -C ${REVIEW_WT} review --base main ${MODEL:+-m $MODEL} ..."
CODEX_EXIT=0
codex exec -C "$REVIEW_WT" review \
  --base main \
  ${MODEL:+-m "$MODEL"} \
  --ephemeral \
  -o "$OUTPUT_FILE" \
  2>&1 | tee "$STDERR_FILE" || CODEX_EXIT=$?

if [[ $CODEX_EXIT -ne 0 ]]; then
  fail_needs_director "CODEX FAILURE: exit=${CODEX_EXIT} — review not completed reliably"
fi

# --- 出力ファイル確認 ---
if [[ ! -f "$OUTPUT_FILE" ]]; then
  fail_needs_director "NEEDS FIX: codex review produced no output file (exit=${CODEX_EXIT})"
fi

REVIEW_CONTENT="$(cat "$OUTPUT_FILE")"
_info "Review output length: ${#REVIEW_CONTENT} chars"

# --- findings 判定 (F2, PR#180) ---
# 判定方針 (2026-09-08 現物確認: codex-cli 0.144.5 の `codex exec review`):
#   実際の出力は JSON ではなく自然文 + Markdown 箇条書きだった。finding がある場合は
#   各行に `- [P1]` / `- [P2]` / `- [P3]` という優先度タグが付き、finding が無い場合は
#   タグなしの説明文のみで、"LGTM" 等のキーワードは一切出ない (現物例は knowledge/codex-reviewer.md
#   および本 task の Result 参照)。
#   旧ロジックの「明示的 LGTM キーワードが無ければ needs-director」というルールは、
#   clean な review でも LGTM と言わないため常に発火してしまう既知バグ (F2 そのもの) だったため、
#   このルールは廃止し [P#] タグの有無で判定する。critical 系キーワードは
#   defense-in-depth として引き続き検出し、tag 判定の結果を上書きできるようにする。
#   将来 / 別環境の codex CLI が構造化 JSON ({"findings":[...], ...}) を返すケースに備え、
#   JSON 判定を最優先で試す (forward-compat パス。現行 CLI では通らない想定)。
NEEDS_FIX=0
JUDGE_METHOD="tags"

if FINDINGS_COUNT=$(echo "$REVIEW_CONTENT" | jq -e '.findings | length' 2>/dev/null); then
  JUDGE_METHOD="json"
  HIGH_COUNT=0
  if [[ "$FINDINGS_COUNT" -gt 0 ]]; then
    HIGH_COUNT=$(echo "$REVIEW_CONTENT" | jq '[
        .findings[]
        | ((.priority // "") | tostring | ascii_downcase | ltrimstr("p")) as $pr
        | ((.severity // "") | tostring | ascii_downcase) as $sev
        | select($pr == "1" or $pr == "2" or $sev == "high" or $sev == "critical")
      ] | length' 2>/dev/null || echo 0)
    HIGH_COUNT="${HIGH_COUNT:-0}"
    [[ "$HIGH_COUNT" -gt 0 ]] && NEEDS_FIX=1
  fi
  _info "Judged via structured JSON output (findings=${FINDINGS_COUNT}, high_or_critical=${HIGH_COUNT})"
else
  P_TAGS="$(echo "$REVIEW_CONTENT" | grep -oiE '\[P[1-3]\]' | tr '[:upper:]' '[:lower:]' | sort -u || true)"
  if [[ -n "$P_TAGS" ]]; then
    if echo "$P_TAGS" | grep -qE '\[p[12]\]'; then
      NEEDS_FIX=1
    fi
    _info "Judged via [P#] priority tags: $(echo "$P_TAGS" | tr '\n' ' ')"
  else
    _info "No [P#] tags found in review output — treating as clean (no findings reported)"
  fi
fi

# 散文キーワード fallback: critical 系キーワードは常に defense-in-depth として上書きする
if echo "$REVIEW_CONTENT" | grep -qiE 'critical|high severity|security vulnerability|must fix|must be fixed|breaking change|data loss'; then
  if [[ $NEEDS_FIX -eq 0 ]]; then
    _warn "Critical keyword found in review output — overriding to needs-director as a safety net"
  fi
  NEEDS_FIX=1
fi

# レビュー内容が空 or 極端に短い場合は要確認
if [[ ${#REVIEW_CONTENT} -lt 20 ]]; then
  _warn "Review output is very short (${#REVIEW_CONTENT} chars), treating as needs-director"
  NEEDS_FIX=1
fi

_info "Findings judgment: method=${JUDGE_METHOD} needs_fix=${NEEDS_FIX}"

# --- 結果報告 ---
# 先頭200文字をサマリとして使用
SUMMARY="${REVIEW_CONTENT:0:200}"
SUMMARY="${SUMMARY//$'\n'/ }"  # 改行をスペースに置換

DONE_MSG="LGTM: PR#${PR_NUM} ${HEAD_BRANCH} reviewed by Kai (model=${MODEL}) — ${SUMMARY}"
NEEDS_FIX_MSG="NEEDS FIX: PR#${PR_NUM} ${HEAD_BRANCH} — ${SUMMARY}"

if [[ $DRY_RUN -eq 1 ]]; then
  # F6, PR#180: plan.sh には一切書き込まず、判定結果を人が読める形で表示するだけ。
  echo ""
  echo "=================================================="
  echo " [DRY-RUN] kai-review.sh 判定結果 (plan.sh へは書き込みません)"
  echo "=================================================="
  echo " PR       : #${PR_NUM} (${HEAD_BRANCH})"
  echo " Task     : ${TASK_ID}${MISSION_SLUG:+ (mission=${MISSION_SLUG})}"
  echo " Judge    : method=${JUDGE_METHOD} needs_fix=${NEEDS_FIX}"
  if [[ $NEEDS_FIX -eq 1 ]]; then
    echo " Verdict  : NEEDS-DIRECTOR相当 (実行時は plan.sh needs-director を呼ぶ)"
    echo " Message  : ${NEEDS_FIX_MSG}"
  else
    echo " Verdict  : DONE/LGTM相当 (実行時は plan.sh done を呼ぶ)"
    echo " Message  : ${DONE_MSG}"
  fi
  echo "=================================================="
elif [[ $NEEDS_FIX -eq 1 ]]; then
  _info "Review found issues requiring fixes"
  call_needs_director "$NEEDS_FIX_MSG"
else
  _info "Review passed (no P1/P2 findings, no critical keywords)"
  "$PLAN_SH" done "$TASK_ID" ${MISSION_SLUG:+--mission "$MISSION_SLUG"} "$DONE_MSG"
fi

_info "Review complete."
