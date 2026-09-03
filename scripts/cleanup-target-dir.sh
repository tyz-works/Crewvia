#!/usr/bin/env bash
# cleanup-target-dir.sh — crewvia-worker-*.json を TARGET_DIR から手動削除する安全網スクリプト
#
# 用途:
#   - Worker が SIGKILL で異常終了し plan.sh done のクリーンアップが実行されなかった場合の手動リカバリ
#   - 孤立した crewvia-worker-*.json を一括削除
#
# 使用例:
#   # 特定 Worker の設定ファイルを削除
#   bash scripts/cleanup-target-dir.sh /path/to/target/project Haruto
#
#   # TARGET_DIR 内の全 crewvia-worker-*.json を削除
#   bash scripts/cleanup-target-dir.sh /path/to/target/project
#
# 注意: start.sh の新実装（Option D）では settings.local.json は変更されません。
#       このスクリプトも settings.local.json には一切触れません。

set -euo pipefail

TARGET="${1:-}"
AGENT="${2:-}"

if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 <TARGET_DIR> [AGENT_NAME]" >&2
  exit 1
fi

if [[ ! -d "$TARGET" ]]; then
  echo "ERROR: TARGET_DIR が存在しません: $TARGET" >&2
  exit 1
fi

CLAUDE_DIR="$TARGET/.claude"

if [[ ! -d "$CLAUDE_DIR" ]]; then
  echo "INFO: .claude ディレクトリが存在しません: $CLAUDE_DIR — クリーンアップ不要"
  exit 0
fi

if [[ -n "$AGENT" ]]; then
  # 特定 Agent のファイルのみ削除
  WORKER_SETTINGS="$CLAUDE_DIR/crewvia-worker-${AGENT}.json"
  if [[ -f "$WORKER_SETTINGS" ]]; then
    rm "$WORKER_SETTINGS"
    echo "✅ 削除: $WORKER_SETTINGS"
  else
    echo "INFO: 対象ファイルが存在しません: $WORKER_SETTINGS — クリーンアップ不要"
  fi
else
  # 全 crewvia-worker-*.json を削除
  FOUND=0
  while IFS= read -r -d '' f; do
    rm "$f"
    echo "✅ 削除: $f"
    FOUND=1
  done < <(find "$CLAUDE_DIR" -maxdepth 1 -name 'crewvia-worker-*.json' -print0 2>/dev/null)

  if [[ "$FOUND" -eq 0 ]]; then
    echo "INFO: crewvia-worker-*.json が存在しません — クリーンアップ不要"
  fi
fi
