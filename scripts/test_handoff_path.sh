#!/usr/bin/env bash
# test_handoff_path.sh — task_158 回帰テスト
#
# 不具合: agents/worker.md の Handoff 手順 (書き手) が相対パス
# "registry/handoffs/$AGENT_NAME/${TASK_ID}_HANDOFF.md" を使っていたため、
# Worker が既定の専用 worktree 内で作業しているとき、この相対パスは
# <worktree>/registry/handoffs/... に書かれる。一方 scripts/dispatcher.sh
# (読み手) は同じ相対パスを REGISTRY_DIR.parent (= main repo root) 基準で
# 解決するため、両者が別ファイルを指し dispatcher からは存在しないファイルに
# 見える → Director への通知が中身なしで飛ぶ (task_158 で実機再現)。
#
# 修正: 書き手 (agents/worker.md) は scripts/git-helpers.sh の crewvia_handoff_path()
# (== main repo root からの絶対パス、worktree の cwd に依存しない) を単一の
# 真実源として使う。読み手 (scripts/dispatcher.sh の handoff detection) は
# embedded Python (`python3 - <<'PYEOF'`) のためこの bash 関数を直接 source
# できないが、crewvia_handoff_path が返す絶対パスをそのまま受け取り、
# 非絶対パスを受け取った場合は警告を出しつつ main repo root 基準で解決する
# (同じ絶対パス規約に独自実装で追随している)。このテストは、worktree の中から
# crewvia_handoff_path で書いたファイルが、main repo root から見て存在することを
# 実機で確認する (dispatcher.sh の hp.exists() 相当のチェック)。
#
# 実行: bash scripts/test_handoff_path.sh
# 副作用: 一時的な worktree/branch/HANDOFF ファイルを作成し、終了時に必ず削除する
#         (crewvia の実データ = queue/ missions・main の状態には触れない)。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Top level of THIS checkout (whichever branch/worktree this script itself is
# running from) — used for the .gitignore check, since .gitignore is a tracked,
# branch-dependent file and we want to validate the branch actually under test.
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/git-helpers.sh"
# _crewvia_repo_root (not OWN_CHECKOUT_ROOT): this script may itself be invoked
# from inside a worktree (e.g. a Worker running its own test suite), and must
# still validate against the one true main repo root — the same resolution the
# fix (crewvia_handoff_path) uses. This is a physical-location invariant, not a
# branch-content one, so it deliberately differs from OWN_CHECKOUT_ROOT above.
REPO_ROOT="$(_crewvia_repo_root)"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

RUN_ID="selftest-$$"
MISSION_SLUG="t158handoffpath${RUN_ID}"
TASK_ID="t${RUN_ID}"
AGENT_NAME="agent${RUN_ID}"
WT=""
WRITTEN_PATH=""

cleanup() {
  if [[ -n "$WRITTEN_PATH" && -f "$WRITTEN_PATH" ]]; then
    rm -f "$WRITTEN_PATH"
  fi
  if [[ -n "$WT" ]]; then
    git -C "$REPO_ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
  git -C "$REPO_ROOT" branch -D "task/${MISSION_SLUG}/${TASK_ID}-selftest" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== test_handoff_path.sh (task_158 regression test) =="

WT="$(crewvia_create_worktree "$MISSION_SLUG" "$TASK_ID" "selftest" 2>/dev/null)"
if [[ -z "$WT" || ! -d "$WT" ]]; then
  fail "crewvia_create_worktree did not produce a usable worktree"
  echo ""
  echo "test_handoff_path.sh: PASS=$PASS_COUNT FAIL=$FAIL_COUNT"
  exit 1
fi
pass "created a per-task worktree (mimics a real Worker's default cwd): $WT"

# --- 書き手側: worktree の中(=Workerの実際のcwd)から HANDOFF.md を書く ---
(
  cd "$WT" || exit 1
  # shellcheck disable=SC1091
  source "$REPO_ROOT/scripts/git-helpers.sh"
  HANDOFF_PATH="$(crewvia_handoff_path "$AGENT_NAME" "$TASK_ID")"
  mkdir -p "$(dirname "$HANDOFF_PATH")"
  echo "task_158 regression test content" > "$HANDOFF_PATH"
  echo "$HANDOFF_PATH"
) > "/tmp/test_handoff_path.$$.out"
WRITTEN_PATH="$(cat "/tmp/test_handoff_path.$$.out")"
rm -f "/tmp/test_handoff_path.$$.out"

if [[ "$WRITTEN_PATH" == /* ]]; then
  pass "crewvia_handoff_path returned an absolute path from inside a worktree: $WRITTEN_PATH"
else
  fail "crewvia_handoff_path returned a NON-absolute path from inside a worktree: $WRITTEN_PATH"
fi

case "$WRITTEN_PATH" in
  "$REPO_ROOT"/.claude/worktrees/*)
    fail "resolved path is inside the per-task worktree, not the main repo root (same bug as task_158): $WRITTEN_PATH"
    ;;
  "$REPO_ROOT"/registry/handoffs/*)
    pass "resolved path is under the main repo root's registry/handoffs/ (branch/worktree-independent)"
    ;;
  *)
    fail "resolved path is neither under the worktree nor under repo-root registry/handoffs/: $WRITTEN_PATH"
    ;;
esac

# --- 読み手側: dispatcher.sh の hp.exists() 相当を main repo root 基準で評価 ---
# (dispatcher.sh 自身は REPO_ROOT = メインリポジトリで常時起動される — scripts/start.sh 参照)
if [[ -f "$WRITTEN_PATH" ]]; then
  pass "dispatcher-equivalent read (absolute-path exists() check) finds the file the worker wrote"
else
  fail "dispatcher-equivalent read does NOT find the file the worker wrote (this is the task_158 bug reproduced)"
fi

# --- gitignore: registry/handoffs/ が git 追跡対象にならないこと (DW-4 案C 制約1) ---
# ★ここは _crewvia_repo_root (= REPO_ROOT、物理的に唯一の main clone) ではなく
# SCRIPT_DIR (= このスクリプト自身が checkout されているブランチ) の .gitignore を見る。
# .gitignore はブランチ依存の tracked ファイルなので、fix ブランチの内容を検証するには
# 「今チェックアウトされているこの checkout」を見る必要がある(main clone が別ブランチの
# ままだと _crewvia_repo_root 側は古い .gitignore を返し得る — 実際にローカル検証時に発生)。
if git -C "$OWN_CHECKOUT_ROOT" check-ignore -q "registry/handoffs/${AGENT_NAME}/${TASK_ID}_HANDOFF.md"; then
  pass "registry/handoffs/ is gitignored (current-state visibility does not depend on git branch/checkout)"
else
  fail "registry/handoffs/ is NOT gitignored — a HANDOFF.md could get committed and become branch-dependent"
fi

# --- 静的チェック: agents/worker.md が worker.md:136 の絶対パス規約を守っていること ---
# ★★W-1 敵対的検証で判明した欠陥A: 上の実機テストは crewvia_handoff_path() を直接呼ぶだけで
# agents/worker.md を一度も読まない。そのため誰かが worker.md の Handoff 手順 (Step 2) を
# 元の相対パス "registry/handoffs/$AGENT_NAME/${TASK_ID}_HANDOFF.md" へ書き戻しても、
# 上のテストは緑のまま通ってしまう (実測済み: 2026-07-27, Worf)。
# ここでは worker.md 本文の ```bash コードブロックを静的に検査し、この回帰を検出する。
# 対象は worker.md:136 の規約が明示的に適用される2箇所 (:582 相当の handoff、:514 相当の
# verify-task.sh) に限定する — agents/*.md 全体には他にも相対 registry/ 参照が残っているが、
# それらは別ミッション扱い (F1 registry/workers.yaml, F7 Verifier 到達不能) であり
# task_158 の対象外 (W-3 参照)。ここを全面 grep にすると無関係な既知の残存に対して
# 誤検知で fail する。
# OWN_CHECKOUT_ROOT (not REPO_ROOT): agents/worker.md はブランチ依存の tracked ファイル
# なので、gitignore チェックと同じ理由で「今チェックアウトされているこの checkout」を見る
# 必要がある (REPO_ROOT はメインクローンが指す任意のブランチであり、fix ブランチとは限らない)。
WORKER_MD="$OWN_CHECKOUT_ROOT/agents/worker.md"
extract_bash_blocks() {
  awk '/^```bash/{flag=1; next} /^```/{flag=0} flag' "$1"
}
WORKER_BASH_BLOCKS="$(extract_bash_blocks "$WORKER_MD")"

if echo "$WORKER_BASH_BLOCKS" | grep -qE '^HANDOFF_PATH="?registry/handoffs/'; then
  fail "agents/worker.md の Handoff 手順が crewvia_handoff_path() を経由しない生の相対パスに回帰している (task_158 の元バグの再発)"
elif echo "$WORKER_BASH_BLOCKS" | grep -qF 'HANDOFF_PATH="$(crewvia_handoff_path'; then
  pass "agents/worker.md の Handoff 手順は引き続き crewvia_handoff_path() 経由で HANDOFF_PATH を解決している"
else
  fail "agents/worker.md の Handoff 手順から HANDOFF_PATH の代入行が見つからない (想定外の構造変化 — 要調査)"
fi

if echo "$WORKER_BASH_BLOCKS" | grep -qE '(^|[^"$/[:alnum:]_])\.?/?scripts/verify-task\.sh'; then
  fail "agents/worker.md の verify-task.sh 呼び出しが \$CREWVIA_REPO 起点でない相対パスに回帰している (worker.md:136 違反の再発)"
elif echo "$WORKER_BASH_BLOCKS" | grep -qF '"$CREWVIA_REPO/scripts/verify-task.sh"'; then
  pass "agents/worker.md の verify-task.sh 呼び出しは引き続き \$CREWVIA_REPO 起点の絶対パスである"
else
  fail "agents/worker.md から verify-task.sh の呼び出し行が見つからない (想定外の構造変化 — 要調査)"
fi

echo ""
echo "test_handoff_path.sh: PASS=$PASS_COUNT FAIL=$FAIL_COUNT"
[[ "$FAIL_COUNT" -eq 0 ]]
