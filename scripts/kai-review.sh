#!/usr/bin/env bash
set -euo pipefail

# kai-review.sh — codex exec ラッパー (Kai reviewer 専用)
# t006 (mission 20260909-safety-gate-hardening) で `codex exec review` サブコマンド
# から `codex exec --output-schema` (diff は自前取得して stdin で渡す) に移行した。
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
# 処理フロー (t006, mission 20260909-safety-gate-hardening で `codex exec review`
# サブコマンドから `codex exec` 本体 + `--output-schema` へ移行。旧フローは
# git blame 参照):
#   1. 引数パース (--pr, --task, --mission, --model, --agent, --skip-pull, --dry-run)
#   2. heartbeat 更新 (dispatcher.publish_agents が Kai-codex を認識するため)
#   3. plan.sh pull --task <task> --agent <agent> --skills codex-review
#      → task.status: pending → in_progress + Taskvia PATCH + assignment file
#      (--skip-pull / --dry-run 時はスキップ)
#   4. gh pr view <PR#> --json headRefName で head branch 取得 (存在確認・ログ用)
#   5. refs/pull/<PR#>/head を origin から一意な local ref に fetch し (F-2, PR#180 —
#      fork PR や削除済み branch でも動く)、そこから専用 git worktree (mktemp -d) を
#      --detach で作成する。主 working tree ($CREWVIA_REPO_ROOT) の
#      HEAD は一切動かさない (F1/F1b, PR#180)
#   6. (t006) `origin/main` を一意な local ref へ都度 fetch し (local main の
#      陳腐化を避ける。実機で 2 commit 差の乖離を観測済み)、`git diff
#      <fetched-main>...HEAD` で diff を自前取得する。`codex exec review
#      --base <BRANCH>` はカスタム [PROMPT] と同時指定できない (実機確認済み、
#      t001 Result 参照) ため、clean review でも必ず構造化シグナルを出させる
#      手段が review サブコマンドには無かった。diff が空 / 異常に巨大
#      (context 切り詰めリスク) な場合は codex を呼ばず無条件で needs-director
#      に倒す (受け入れ基準(i), t006 Description 参照 — 空配列は「clean」と
#      「レビューできていない」を区別しないため)。
#   7. 取得した diff を stdin で `codex exec -C <review-worktree>
#      --output-schema <schema> -o <mktemp output file> "<review prompt>"` に渡す。
#      `--output-schema` で最終応答を `{"findings":[...]}`  形式の JSON に強制する
#      ことで、「clean review 相当」は信頼できる空配列として、finding ありは
#      構造化データとして返る (詳細: config/kai-review-findings.schema.json)。
#   8. 出力ファイルを読み込み findings を判定。JSON 経路 (`.findings` が
#      ちょうど 1 つの JSON ドキュメントとして得られ、かつ配列である場合) が
#      唯一の auto-done 経路 (t011: `[P0]`-`[P3]` タグ判定の fallback は
#      廃止済み — 「引用かどうか」を行単位で判別しようとする設計自体が
#      再発可能な穴だったため。詳細は「findings 判定」セクション参照)。
#      JSON findings 配列が得られない場合 (HAD_SIGNAL=0) は、内容に関わらず
#      無条件で needs-director 側に倒す (fail-closed, R-1,
#      mission 20260909-safety-gate-hardening)。JSON が複数ドキュメント
#      (JSONL 等、スキーマ違反) の場合も同様に fail-closed に倒す
#      (F-B, t006 — 詳細は「findings 判定」セクション参照)
#   9. findings なし / all-low(P3) → plan.sh done
#      修正必要 (P0/P1/P2 or 構造化シグナル無し) → plan.sh needs-director
#      (plan.sh done は Taskvia sync + registry.workers.yaml の task_count 自動 bump)
#      --dry-run 時はどちらも実行せず、判定結果を表示するのみ
#  10. レビュー用 worktree・fetch した一時 ref・出力/stderr の一時ファイルは
#      trap で必ず後始末する

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
      sed -n '3,43p' "$0" | sed 's/^# //'
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
    # P3 fix (PR#180 Seo review): 実呼び出し (下の行) には `--` 区切りが無いため、
    # ここでも付けない。付けたままだと dry-run ログを見て手で再現しようとした際に
    # 実際のコマンドと食い違う。
    _info "[DRY-RUN] would call: plan.sh needs-director ${TASK_ID} ${MISSION_SLUG:+--mission ${MISSION_SLUG} }${1}"
    return 0
  fi
  "$PLAN_SH" needs-director "$TASK_ID" ${MISSION_SLUG:+--mission "$MISSION_SLUG"} "$1"
}

fail_needs_director() {
  _error "$1"
  call_needs_director "$1"
  exit 1
}

# --- 一時リソースの後始末 (F1b/F2/F3, PR#180) ---
# レビュー用 worktree・fetch した一時 local ref・出力/stderr の一時ファイルは、
# 成功・失敗どちらの経路でも必ず削除する。ここで空文字初期化しておき、set -u 下でも
# cleanup が安全に参照できるようにする。
REVIEW_WT=""
OUTPUT_FILE=""
STDERR_FILE=""
FETCH_LOCAL_REF=""
BASE_FETCH_LOCAL_REF=""  # t006: origin/main を都度 fetch する一時 ref (下記参照)

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
  if [[ -n "$FETCH_LOCAL_REF" ]]; then
    git -C "${WORK_DIR:-$REPO_ROOT}" update-ref -d "$FETCH_LOCAL_REF" 2>/dev/null || true
  fi
  if [[ -n "$BASE_FETCH_LOCAL_REF" ]]; then
    git -C "${WORK_DIR:-$REPO_ROOT}" update-ref -d "$BASE_FETCH_LOCAL_REF" 2>/dev/null || true
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

# --- PR の head commit を fetch (F-2, PR#180) ---
# 旧実装は `git fetch origin "$HEAD_BRANCH"` + `origin/<branch>` を参照していたが、
# これは headRefName が origin 上に生きているブランチであることが前提になる。
# fork からの PR や、マージ後に削除済みの branch (実機再現: PR#179) では
# `fatal: couldn't find remote ref ...` で必ず失敗する。
# GitHub は PR がある限り `refs/pull/<PR#>/head` を必ず公開しているため、
# こちらを直接 fetch する方式に変更する (fork PR・削除済み branch 双方で動く)。
# fetch 先は $WORK_DIR (主リポジトリ) 内の一意な local ref に固定する:
#   - FETCH_HEAD は $WORK_DIR 単位で共有される 1 ファイルなので、他プロセスが
#     同時に fetch すると上書きされ得る (F1 で origin/<branch> に変更した際と同じ理由)。
#   - PR番号を含む固定名だと同一 PR の同時 review で衝突し得るため $$ (PID) で一意化する。
FETCH_LOCAL_REF="refs/kai-review-fetch/pr-${PR_NUM}-$$"
_info "Fetching refs/pull/${PR_NUM}/head → ${FETCH_LOCAL_REF} ..."
if ! git -C "$WORK_DIR" fetch --force origin "refs/pull/${PR_NUM}/head:${FETCH_LOCAL_REF}" 2>&1; then
  fail_needs_director "NEEDS FIX: git fetch origin refs/pull/${PR_NUM}/head failed for PR#${PR_NUM}"
fi

# --- 専用 review worktree を作成 (F1/F1b, PR#180) ---
REVIEW_WT="$(mktemp -d "${TMPDIR:-/tmp}/kai-review-wt.XXXXXX")"
_info "Creating isolated review worktree at ${REVIEW_WT} (detached at ${FETCH_LOCAL_REF})..."
if ! git -C "$WORK_DIR" worktree add --detach "$REVIEW_WT" "$FETCH_LOCAL_REF" 2>&1; then
  fail_needs_director "NEEDS FIX: git worktree add failed for PR#${PR_NUM} (${FETCH_LOCAL_REF})"
fi

# --- diff base の取得 (t006) ---
# 旧実装 (`codex exec review --base main`) はローカルの `main` ブランチを直接
# 参照していた。実機確認 (t006, 2026-09-09): 本番の主リポジトリで local main
# (348fa96) が origin/main (06fb70d, 2 commit 差) より古いまま放置されている
# ケースを実際に観測した — Director が review/自動化を頻繁に回す一方で
# 明示的な `git pull` は都度行われないため、local main が容易に陳腐化する。
# diff の base が古いと「レビュー対象外の差分まで含む」または「本来レビュー
# すべき差分の一部を merge-base 計算で除外する」方向に誤る可能性があり、
# 受け入れ基準(i) (診断できない不完全な diff は信用しない) の趣旨に反する。
# そのため origin から都度 `main` を一意な local ref へ fetch し、必ず最新の
# origin/main を diff base とする (旧実装からの意図的な改善。PR head の
# fetch と同じパターンを流用)。
BASE_FETCH_LOCAL_REF="refs/kai-review-fetch/base-main-$$"
if ! git -C "$WORK_DIR" fetch --force origin "main:${BASE_FETCH_LOCAL_REF}" 2>&1; then
  fail_needs_director "NEEDS FIX: git fetch origin main failed while resolving diff base"
fi
DIFF_BASE="$BASE_FETCH_LOCAL_REF"
_info "Computing diff (origin/main...HEAD) in ${REVIEW_WT} ..."
if ! DIFF_CONTENT="$(git -C "$REVIEW_WT" diff "${DIFF_BASE}...HEAD" 2>&1)"; then
  fail_needs_director "NEEDS FIX: git diff ${DIFF_BASE}...HEAD failed in review worktree — ${DIFF_CONTENT}"
fi

# 受け入れ基準(i) (Seo 指摘, t006): diff 取得の失敗 / 空 diff / context 超過による
# 切り詰め のいずれでも codex の空配列は「clean」と区別のつかない信頼できない
# シグナルになる。空 diff はレビュー対象が無い = review が成立していないことを
# 意味するため、codex を一切呼ばず無条件で needs-director に倒す。
if [[ -z "$DIFF_CONTENT" ]]; then
  fail_needs_director "NEEDS FIX: diff ${DIFF_BASE}...HEAD is empty — cannot trust an empty findings array without a genuine diff (fail-closed, t006 acceptance criterion i)"
fi

# 巨大すぎる diff は codex の context window 内で切り詰められ、「見えていない
# 部分に finding が無い」という誤った安全確認につながりうる。切り詰めの発生を
# 直接検知する手段が無いため、実測ベースの安全マージンを上限として設け、
# 超過時は codex を呼ばず needs-director に倒す(完全automated な代替が無いため、
# diff を分割する等の運用は Director 判断に委ねる)。
DIFF_BYTES=${#DIFF_CONTENT}
MAX_DIFF_BYTES=$((300 * 1024))  # 300KB
if [[ $DIFF_BYTES -gt $MAX_DIFF_BYTES ]]; then
  fail_needs_director "NEEDS FIX: diff is ${DIFF_BYTES} bytes (> ${MAX_DIFF_BYTES}) — too large to trust against silent context truncation (fail-closed, t006 acceptance criterion i)"
fi
_info "Diff size: ${DIFF_BYTES} bytes"

# --- 出力/stderr ファイル (F3, PR#180) ---
# 固定 /tmp パスは codex-review task が並列実行された場合に相互上書きする事故を招く
# (Kai-codex は dispatcher 側で同時 1 本しか spawn しないが、手動起動との衝突も
#  避けるため mktemp で一意化する)。cleanup trap で削除する。
OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/kai-review-output.XXXXXX")"
STDERR_FILE="$(mktemp "${TMPDIR:-/tmp}/kai-review-stderr.XXXXXX")"

# --- schema ファイル (t006) ---
SCHEMA_FILE="${CREWVIA_REPO_ROOT:-$REPO_ROOT}/config/kai-review-findings.schema.json"
if [[ ! -f "$SCHEMA_FILE" ]]; then
  fail_needs_director "NEEDS FIX: findings schema file not found: ${SCHEMA_FILE}"
fi

REVIEW_PROMPT="$(cat <<'PROMPT_EOF'
Review the diff provided in the <stdin> block below (a unified git diff, base
against HEAD, in this checkout's working directory). Read any files referenced
by the diff in this working directory as needed to understand the surrounding
context before judging correctness — do not judge from the diff hunks alone.

Focus on:
  - correctness bugs (crashes, wrong output, logic errors, off-by-one, race
    conditions, fail-open defaults where fail-closed is required)
  - security issues (injection, unsafe shell quoting/expansion, path
    traversal, secrets)
  - other issues worth a human's attention before merge

For every issue found, add an entry to the JSON `findings` array with:
  - "priority": "P0" (release-blocking: crash / data loss / security) through
    "P3" (minor nit)
  - "title": a short one-line summary
  - "body": more detail, including why it matters (or null if not needed)
  - "file": the path most relevant to the finding (or null if not applicable)

Only return an empty `findings` array after you have actually read and
reasoned about the diff and found nothing worth flagging. Do not guess; if you
are unsure whether something is a real bug, report it at a lower priority
rather than omitting it.
PROMPT_EOF
)"

# --- codex exec 実行 (t006: review サブコマンドではなく exec 本体 + --output-schema) ---
# -C "$REVIEW_WT" で作業ディレクトリを指定する (codex が周辺コードを読む際の cwd)。
# --sandbox read-only: レビューは書き込みを必要としないため、誤って
# ファイルを変更されるリスクを構造的に無くす。
_info "Running codex exec -C ${REVIEW_WT} --output-schema ... ${MODEL:+-m $MODEL} (diff=${DIFF_BYTES} bytes) ..."
CODEX_EXIT=0
printf '%s' "$DIFF_CONTENT" | codex exec -C "$REVIEW_WT" \
  --sandbox read-only \
  --output-schema "$SCHEMA_FILE" \
  ${MODEL:+-m "$MODEL"} \
  --ephemeral \
  -o "$OUTPUT_FILE" \
  "$REVIEW_PROMPT" \
  2>&1 | tee "$STDERR_FILE" || CODEX_EXIT=$?

if [[ $CODEX_EXIT -ne 0 ]]; then
  fail_needs_director "CODEX FAILURE: exit=${CODEX_EXIT} — review not completed reliably"
fi

# --- 出力ファイル確認 (P2 fix, PR#180 Seo review) ---
# OUTPUT_FILE は F3 で mktemp 導入済みのため、`-f` (存在するか) だけでは
# codex 実行前から既に true になっており、このチェックは原理的に発火しない
# (到達不能な dead code だった)。`-s` (非空かどうか) に変えることで、
# 「codex が exit 0 で終わったのにファイルが空/未書き込みのまま」という
# 実際に起こり得るケースを検出できるようにする。
if [[ ! -s "$OUTPUT_FILE" ]]; then
  fail_needs_director "NEEDS FIX: codex review produced no output (file missing or empty, exit=${CODEX_EXIT})"
fi

REVIEW_CONTENT="$(cat "$OUTPUT_FILE")"
_info "Review output length: ${#REVIEW_CONTENT} chars"

# --- findings 判定 (F2, PR#180 / F-1, F-3, PR#180 QA fix) ---
# 判定方針 (2026-09-08 現物確認: codex-cli 0.144.5 の `codex exec review`):
#   実際の出力は JSON ではなく自然文 + Markdown 箇条書きだった。finding がある場合は
#   各行に `- [P0]` / `- [P1]` / `- [P2]` / `- [P3]` という優先度タグが付き、finding が
#   無い場合はタグなしの説明文のみで、"LGTM" 等のキーワードは一切出ない (現物例は
#   knowledge/codex-reviewer.md および本 task の Result 参照)。
#   旧ロジックの「明示的 LGTM キーワードが無ければ needs-director」というルールは、
#   clean な review でも LGTM と言わないため常に発火してしまう既知バグ (F2 そのもの) だったため、
#   このルールは廃止し [P#] タグの有無で判定する方式に移った (t011 で [P#] タグ経路
#   自体を廃止済み。下記参照。このパラグラフ以下は当時の判断の記録として残す)。
#
#   F-1 (P0 抜け, QA FAIL): タグ抽出が `[P1-3]` のみで P0 を拾わず、[P0] だけの
#   findings が LGTM 誤判定 (自動 done) されていた。レビューゲートが「危険な方向
#   (自動承認)」に倒れる欠陥のため、P0 もタグ抽出・JSON priority 判定の両方に含めた
#   (タグ抽出自体は t011 で廃止済み)。
#
#   F-3 (critical keyword 誤発火, QA FAIL): 旧ロジックは critical 系キーワードを
#   タグ/JSON 判定の結果によらず常に上書き適用していたため、"No critical issues
#   found." のような**否定文脈の健全な報告文**まで拾って clean な review を
#   needs-director に誤発火させ、F2 の目的 (誤 needs-director を減らす) を部分的に
#   打ち消していた。対応 (t008 Description の一案を採用): (a) JSON 判定が成功した場合、
#   または [P#] タグが 1 つでも見つかった場合は、それらの判定に自信があるとみなし
#   keyword fallback を一切適用しない (HAD_SIGNAL=1)。(b) [P#] タグが 1 つも
#   見つからなかった場合のみ、critical キーワードを safety net として見る
#   (HAD_SIGNAL=0 のまま) — この「タグ不在」の状況こそ、コード側の判定材料が
#   無いためキーワードに頼らざるを得ないケースであり、かつ QA の再現例 3 件が
#   まさにこの経路 (タグ無し・散文のみ) で誤爆していたため。同一行に
#   no/not/none/nothing/without/clean/zero 等の否定語が同居する行は「~の問題は
#   無い」という健全な報告と判断し除外する。
#
#   [P2] (Seo 最終レビュー, t018): JSON 経路の「入口ゲート」自体が fail-open
#   だった。旧 `jq -e '.findings | length'` は `.findings` が存在しないスキーマ
#   でも `null` でも `length` が `0` を返し (jq の `length` は null に対して
#   エラーではなく 0 を返す仕様)、`0` は `-e` にとって false/null ではないため
#   exit 0 になる。結果 FINDINGS_COUNT=0 → JSON 経路に入ったまま NEEDS_FIX=0
#   (自動 done)、しかも HAD_SIGNAL=1 が立つため後段の safety net まで無効化
#   されていた。これは **同一構造 (倒れる方向が常に自動承認) の 3 度目**:
#   F-1 ([P0] タグ取りこぼし) → [P1]/t012 (JSON 内側の allowlist) → 今回
#   (JSON 経路への入口ゲート)。`.findings | arrays | length` に変更し、
#   `.findings` が真に配列である場合のみ (空配列 `[]` も含む) JSON 経路に
#   入るようにした。配列でない/存在しない/null の場合は `arrays` がフィルタで
#   除外して jq が何も出力せず exit 非 0 になるため、`if` が false になって
#   (t011 以降は) fail-closed 分岐 (no-signal) へ確実にフォールバックする。
#
#   R-1 (t001, mission 20260909-safety-gate-hardening): PR#180 で F-3 として
#   導入した「critical キーワード + 同一行否定語除外」の散文 safety net は、
#   これ自体が **4 度目の「倒れる方向が自動承認」欠陥**の温床だった。
#   同一行内のどこかに no/not/none/without 等の否定語があれば行ごと除外する
#   実装は、否定語が critical な指摘と無関係な箇所を否定しているだけの場合
#   (再現例: "This introduces a critical race condition ... that does not
#   have a workaround, and callers cannot recover once it triggers." —
#   "not"/"cannot" は "critical race condition" ではなく別の節を否定して
#   いる) に、本物の critical finding ごと安全側 (auto-done) に倒してしまう
#   (実測: Phase 3 QA t010)。「同一行」という判定 unit が粗すぎ、かつ
#   「危険パターンに一致しなければ自動 done」という default-safe な構造
#   自体が、設計原則 (危険な結論=自動 done は allowlist であるべき) に反して
#   いた。個別のキーワード/否定語調整では同種の欠陥を再発させるだけと判断し、
#   safety net を全廃した。
#
#   実機検証 (t001, 2026-09-09, codex-cli 0.144.5): 代替案として「codex に
#   必ず [P#] タグ (finding 無しでも明示マーカー) を出力させるカスタム
#   prompt を渡す」ことを検討したが、`codex exec review --base <BRANCH>` は
#   `--base` と `[PROMPT]` (カスタム指示) を同時指定できないことを実機で
#   確認した (`error: the argument '--base <BRANCH>' cannot be used with
#   '[PROMPT]'`)。`--output-schema <FILE>` (JSON Schema 強制) も試したが、
#   空 diff / 実質的な diff の双方で review サブコマンドの最終出力書式
#   (自然文 + [P#] タグ) は一切変化しないことを確認した。つまり当時の
#   codex-cli は `--base` を使う限り「finding が無い場合の明示マーカー」を
#   強制する手段が無く、clean な review は今後も引き続きタグ無しの自然文
#   のみを返すと考えられていた (t006 で `--output-schema` への移行によりこの
#   制約自体が解消された。下記参照)。
#
#   当時の結論だった「JSON findings 配列も [P#] タグも一切見つからない
#   (HAD_SIGNAL=0) 場合は、内容に関わらず無条件で needs-director に倒す」
#   という fail-closed の原則は、t011 で [P#] タグ経路を廃止した後も
#   変わらず継続している — 判定材料が「JSON findings 配列」1 つだけに
#   絞られた点が変更点である。
#
#   t006 (mission 20260909-safety-gate-hardening) での更新: 上記 R-1 は
#   「`--base` を使う限りタグを強制する手段が無い」ことを根拠に fail-closed
#   方式を採ったが、`review` サブコマンドを使うのをやめ diff を自前取得して
#   `codex exec --output-schema` に渡す方式に移行することで、clean review も
#   含めて **常に構造化 JSON (信頼できる空配列) を返させる**ことができた
#   (Director 実機検証・本 task の実機検証 Result 参照)。fail-closed の設計
#   原則自体 (構造化シグナルが無ければ needs-director) は変更していない —
#   単に「clean review でもタグ相当のシグナルが得られない」という R-1 時点の
#   制約が解消されたため、実運用上 no-signal に落ちる頻度が下がるだけである。
#
#   F-B (Seo 指摘, t006): `--output-schema` 経路では codex の最終応答が常に
#   JSON になる前提だが、複数 JSON ドキュメント (JSONL 等、スキーマ違反) が
#   返った場合、旧実装は `jq -e '.findings | arrays | length'` が複数行を
#   出力し、後続の `[[ "$FINDINGS_COUNT" -gt 0 ]]` が bash の構文エラーに
#   なって false 扱いになり、**`NEEDS_FIX=0` のまま `HAD_SIGNAL=1` が
#   立って auto-done してしまう**欠陥を持っていた (Seo 隔離ハーネス実測:
#   `{"findings":[]}` と `{"findings":[{"severity":"critical"}]}` の 2 行
#   入力で `[[: 0 1: syntax error in expression` → method=json needs_fix=0)。
#   `codex exec review` (JSON を返さない) では休眠していたが、`--output-schema`
#   移行でこの経路が本番化するため、JSON 経路に入る前に「出力がちょうど 1 つの
#   JSON ドキュメントであること」を jq でゲートし、2 つ以上 (または 0、パース
#   不能) の場合は JSON 経路そのものに入らず fail-closed 側 (no-signal) に
#   確実にフォールバックするようにした。
#
#   F-A (Seo 指摘, t007): [P#] タグ抽出 (当時存在した [P#] タグ判定分岐) が
#   出力全体への無アンカー grep だったため、レビュー対象の diff/コードが
#   (`kai-review.sh` / `test_kai_review.sh` 自身のように) 文字列 `[P3]` 等を
#   含んでいて codex がそれを地の文で引用しただけで `HAD_SIGNAL=1` が立ち、
#   同じ出力中に散文で述べられた本物の critical finding が丸ごと無視されて
#   いた (実測: "...mentioning [P3] priority tags... critical data-loss
#   bug..." → method=tags needs_fix=0 で auto-done)。R-1 (t001) で auto-done
#   経路が「JSON 空配列」と「[P#] タグ」の 2 本に絞られた結果、この偽造され
#   うるトークンが auto-done への最安経路になっていた。t007 では「タグ抽出を
#   finding 行 (行頭の箇条書きマーカーに続くタグ) にアンカーする」対処を
#   採ったが、t009 QA の実測 (18 ケース) で **fenced コードブロック /
#   インデント コードブロック内の bullet 形式の引用は依然として行頭アンカー
#   に一致してしまう**ことが判明した (D2/D4)。最悪ケースでは codex が構造化
#   JSON で報告した P0 finding が、地の文 + fenced 引用 + JSON の混在により
#   JSON ドキュメントが 1 つに切り出せず F-B ゲートで tag fallback に落ち、
#   引用された [P3] を拾って丸ごと捨てられた (自動承認) ことも実機で確認
#   された。
#
#   t011 (mission 20260909-safety-gate-hardening): 「引用かどうか」は行単位
#   では判別できないのに、判定 unit を行に置いたまま解決したつもりになって
#   いた、という t009 QA の構造的な指摘を受け、コードブロックを剥がす前処理
#   を追加する案ではなく **[P#] タグ経路そのものを削除する**方針を採った。
#   前処理追加案は markdown パースの完全性に依存するため、同じ形の穴が別の
#   引用形式 (ネストした fence、チルダ fence `~~~`、HTML `<pre>` 等) で再発
#   する — 「表現の列挙で網羅するのは原理的に不可能」という、本 mission が
#   F-1 → [P1]/t012 → [P2] → F-A → t009 で 5 回学んだ教訓そのものである。
#   t006 以降 codex は `--output-schema` により schema 準拠の JSON を必ず
#   返すため、タグ経路は「JSON 解析が失敗したときの fallback」という位置づけ
#   以上の価値を持たなくなっていた。JSON 解析の失敗自体が異常であり、
#   needs-director が正しい結論である。削除後、auto-done に到達する経路は
#   「codex が単一 JSON ドキュメントを返し、`.findings` が配列で、denylist
#   判定で危険な finding が 0 件」の 1 本だけになった (判定 unit を 1 つに
#   絞る、という本 mission の中心原則の直接適用)。
#
#   現在の判定ロジック (入口・内側・fallback):
#   - 入口: [P2] で修正済み。findings が配列でない限り JSON 経路に入らない。
#     F-B (t006) で「JSON ドキュメントがちょうど 1 つであること」も追加ゲート。
#   - 内側 (findings > 0 の denylist, t012): 未知の priority/severity・欠損・
#     jq 自体の失敗はいずれも危険側 (NEEDS_FIX=1) に倒れることを確認済み。
#   - jq 不在環境: jq が無ければ exit 127 で入口の `if` が false になり
#     fail-closed 分岐 (no-signal) に落ちるため安全。
#   - fallback (no-signal, t011 で [P#] タグ判定を削除): HAD_SIGNAL は
#     「配列として JSON 判定できた」場合のみ 1 になり、それ以外
#     (HAD_SIGNAL=0 — JSON 解析失敗・複数ドキュメント・codex 未起動・
#     clean review でも JSON を返さなかった、等すべて) は下記の通り
#     無条件で needs-director に倒れるため、fail-open な抜け道は無い。
NEEDS_FIX=0
JUDGE_METHOD="pending"
HAD_SIGNAL=0

# F-B (t006): 出力に含まれる JSON ドキュメント数を数える。ちょうど 1 つの
# 場合のみ JSON 経路の対象とする (JSONL/破損出力は bash 数値比較のエラーで
# 握りつぶされる余地を残すため、根本から入口を絞る)。
JSON_LINES="$(printf '%s' "$REVIEW_CONTENT" | jq -c '.' 2>/dev/null)" || true
JSON_DOC_COUNT=0
if [[ -n "$JSON_LINES" ]]; then
  JSON_DOC_COUNT=$(printf '%s\n' "$JSON_LINES" | grep -c '.')
fi
if [[ "$JSON_DOC_COUNT" -gt 1 ]]; then
  _warn "codex output contains ${JSON_DOC_COUNT} JSON documents (F-B: expected exactly 1) — schema violation, skipping JSON path"
fi

if [[ "$JSON_DOC_COUNT" -eq 1 ]] && FINDINGS_COUNT=$(printf '%s' "$REVIEW_CONTENT" | jq -e '.findings | arrays | length' 2>/dev/null); then
  JUDGE_METHOD="json"
  HAD_SIGNAL=1
  HIGH_COUNT=0
  if [[ "$FINDINGS_COUNT" -gt 0 ]]; then
    # denylist で判定する (P1 fix, PR#180 Seo review): 旧実装は「危険な値」の
    # allowlist (priority 0/1/2, severity high/critical) だったため、列挙外の
    # 値 (例: severity="major"/"medium") や priority/severity が丸ごと欠損した
    # finding が全て安全側 (自動 done) に落ちる fail-open 構造だった。
    # findings が 1 件でもある以上、「安全と確認できたものだけ」を安全とし、
    # それ以外 (未知の値・分類不能・欠損・jq 自体の失敗) は全て危険側
    # (NEEDS_FIX=1) に倒す。F-1 で採った「倒れる方向が自動承認である以上
    # マージ前に塞ぐ」という判断基準を JSON 経路全体に適用する。
    if HIGH_COUNT=$(echo "$REVIEW_CONTENT" | jq '[
        .findings[]
        | ((.priority // "") | tostring | ascii_downcase | ltrimstr("p")) as $pr
        | ((.severity // "") | tostring | ascii_downcase) as $sev
        | select(
            ($pr == "" and $sev == "")
            or ($pr != "" and $pr != "3")
            or ($sev != "" and ($sev | IN("low", "info", "none") | not))
          )
      ] | length' 2>/dev/null); then
      HIGH_COUNT="${HIGH_COUNT:-1}"
    else
      # jq 自体が失敗した場合も、findings>0 は既に確定しているので
      # 安全側 (0) には倒さず危険側 (1 以上) に倒す。
      HIGH_COUNT=1
    fi
    [[ "$HIGH_COUNT" -gt 0 ]] && NEEDS_FIX=1
  fi
  _info "Judged via structured JSON output (findings=${FINDINGS_COUNT}, unsafe_or_unclassified=${HIGH_COUNT})"
else
  # t011: [P#] タグ判定 (旧 F-A fallback) を削除した。JSON 経路に入れなかった
  # 出力は理由を問わず HAD_SIGNAL=0 のままにし、下の fail-closed 分岐
  # (no-signal → needs-director) に委ねる。[P#] タグは codex の地の文や
  # fenced/インデント コードブロック内の引用にも現れ得るため、「引用かどうか」
  # を行単位のアンカリングで判別しようとする設計自体が再発可能な穴だった
  # (F-A → t009 QA FAIL)。判定 unit を JSON 経路 1 つに絞ることで、この種の
  # 「引用に化けうるトークン」を根本から判定材料の対象外にする。
  _info "No valid JSON findings array in review output (doc_count=${JSON_DOC_COUNT}) — no structured signal"
fi

# fail-closed (R-1, t011 で判定材料を JSON findings 配列 1 つに絞った):
# JSON findings 配列が得られなかった (HAD_SIGNAL=0) 場合、内容に関わらず
# 無条件で needs-director に倒す。
# 旧実装 (F-3) はここで critical キーワード + 同一行否定語除外の散文
# ヒューリスティクスを safety net として使っていたが、これ自体が「無関係な
# 否定語が同一行にあると本物の critical finding を見逃す」欠陥 (R-1) の
# 温床だった。さらに旧実装 (F-A, t007) は [P#] タグを fallback の判定材料と
# していたが、「引用かどうか」を行単位で判別するのは原理的に不可能だったため
# t011 で廃止した。HAD_SIGNAL=0 は「codex が JSON を返さなかった/出力が
# 壊れた/API エラー/JSON が複数ドキュメントだった」のいずれかを意味し、
# いずれの場合も「安全と確認できた」わけではないため、危険な結論
# (auto-done) の allowlist には入れない。
if [[ $HAD_SIGNAL -eq 0 ]]; then
  JUDGE_METHOD="no-signal"
  _warn "No structured JSON findings array found in review output — treating as needs-director (fail-closed, R-1/t011)"
  NEEDS_FIX=1
fi

# レビュー内容が空 or 極端に短い場合は要確認。
# t006 で判明: この閾値は `codex exec review` の自然文出力 (どんな内容でも
# 数十文字は超える) を前提にしていたため、`--output-schema` 移行後の正当な
# clean review `{"findings": []}` (16 文字) まで誤って needs-director に
# 倒してしまっていた (実機確認で検出)。JSON 経路で確定判定できた場合は
# 「ちょうど 1 つの JSON ドキュメントとして構造的に検証済み」であることが
# 既に信頼の根拠であり、文字数は無関係なので、この長さチェックは JSON 以外の
# 経路 (タグ判定 / no-signal) にのみ適用する。
if [[ "$JUDGE_METHOD" != "json" && ${#REVIEW_CONTENT} -lt 20 ]]; then
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
  _info "Review passed (structured signal confirmed safe: no P0-P2 findings)"
  "$PLAN_SH" done "$TASK_ID" ${MISSION_SLUG:+--mission "$MISSION_SLUG"} "$DONE_MSG"
fi

_info "Review complete."
