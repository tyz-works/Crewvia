#!/usr/bin/env bash
# scripts/wait_for_plan_review.sh <review_output_path> <start_epoch> [max_wait_seconds] [poll_interval]
#
# plan-reviewer が書く plan_review.md を待ち、有効な verdict が見つかったら
# 成功として返す。scripts/review-plan.sh から切り出したポーリング判定の本体
# (t002, mission 20260908-launch-reliability)。
#
# 切り出した理由: review-plan.sh 本体は `claude` CLI を spawn するため
# end-to-end ではテストしにくい。ポーリング判定だけを独立スクリプトにする
# ことで、claude を一切起動せずに以下の回帰テストができる:
#   1. 有効な verdict (規定形式 `**Verdict:** approve` 等) → 即成功
#   2. 別表記の verdict (`## 総合判定: **GO**` 等) → 正規化して成功
#   3. start_epoch より古い plan_review.md (前 cycle の残骸) → 無視して待ち
#      続け、新しいファイルが来なければタイムアウトすること (誤って古い
#      判定を新しい判定として採用しない。t002 で実際に発生したパターン3)
#   4. plan_review.md が一度も作られない → タイムアウトすること
#
# 呼び出し前提: review-plan.sh 側で「レビュー開始前に既存の plan_review.md を
# 削除する」処理を既に行っていること。本スクリプトの mtime チェックは
# その削除が何らかの理由 (権限・競合等) で効かなかった場合の二重の安全策
# であり、削除自体の代替ではない。
#
# 標準出力の1行目で結果種別を返す (2行目以降は人間向けログ、stderr にも複製):
#   OK             — 有効な verdict が見つかった (規定形式 or 正規化成功)
#   TIMEOUT_FRESH  — このレビュー実行で書かれたファイルは観測できたが、
#                    最後まで有効な verdict が見つからなかった
#                    (フォーマット不明で判定不能。内容自体は活かせる可能性あり)
#   TIMEOUT_NONE   — このレビュー実行で書かれたファイルが一度も観測できなかった
# exit code: 0 = OK, 1 = TIMEOUT (fresh/none いずれも)
#
# Usage: wait_for_plan_review.sh <review_output_path> <start_epoch> [max_wait_seconds=600] [poll_interval=5]
set -uo pipefail

REVIEW_OUTPUT="${1:?Usage: wait_for_plan_review.sh <review_output_path> <start_epoch> [max_wait_seconds] [poll_interval]}"
START_EPOCH="${2:?start_epoch required}"
MAX_WAIT="${3:-600}"
POLL_INTERVAL="${4:-5}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NORMALIZE_SCRIPT="${SCRIPT_DIR}/normalize_plan_review_verdict.py"

# GNU (Linux/WSL) と BSD/macOS の stat 引数差を吸収する。
_mtime_of() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

_iterations=$(( (MAX_WAIT + POLL_INTERVAL - 1) / POLL_INTERVAL ))
[ "$_iterations" -lt 1 ] && _iterations=1

_fresh_seen=0
_i=0
while [ "$_i" -lt "$_iterations" ]; do
  _i=$((_i + 1))
  if [ -f "$REVIEW_OUTPUT" ]; then
    _mtime="$(_mtime_of "$REVIEW_OUTPUT")"
    if [ "$_mtime" -ge "$START_EPOCH" ]; then
      _fresh_seen=1
      if grep -q '^\*\*Verdict:\*\*' "$REVIEW_OUTPUT" 2>/dev/null; then
        echo "OK"
        echo "[wait_for_plan_review] valid verdict found in $REVIEW_OUTPUT" >&2
        exit 0
      fi
      if [ -f "$NORMALIZE_SCRIPT" ] && python3 "$NORMALIZE_SCRIPT" "$REVIEW_OUTPUT"; then
        echo "OK"
        echo "[wait_for_plan_review] verdict normalized to standard format in $REVIEW_OUTPUT" >&2
        exit 0
      fi
      # 別表記も見つからなかった — このまま次のポーリングへ (reviewer が
      # まだ書き終えていない可能性があるため、即座には諦めない)
    fi
    # _mtime < START_EPOCH: 前 cycle の残骸。無視して待ち続ける (t002 パターン3対策)
  fi
  sleep "$POLL_INTERVAL"
done

if [ "$_fresh_seen" -eq 1 ]; then
  echo "TIMEOUT_FRESH"
  echo "[wait_for_plan_review] $REVIEW_OUTPUT was written during this run but no verdict (standard or recognized alternate wording) could be found — inspect it by hand, the content may still be usable without consuming another review cycle" >&2
else
  echo "TIMEOUT_NONE"
  echo "[wait_for_plan_review] $REVIEW_OUTPUT was not produced within ${MAX_WAIT}s" >&2
fi
exit 1
