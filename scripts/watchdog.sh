#!/usr/bin/env bash
set -euo pipefail

# watchdog.sh — Worker 死活監視
#
# Usage:
#   bash scripts/watchdog.sh [--threshold <秒数>] [--interval <秒数>]
#
# 引数:
#   --threshold <秒数>   ハートビートが途切れてから警告するまでの秒数（デフォルト: 600）
#   --interval  <秒数>   監視ループのポーリング間隔（秒）（デフォルト: 60）
#
# 環境変数:
#   TASKVIA_URL     Taskvia WebUI の URL（デフォルト: https://taskvia.vercel.app）
#   TASKVIA_TOKEN   Taskvia API 認証トークン（設定時のみ警告ログを投稿）
#
# 動作:
#   registry/heartbeats/ 配下のファイルを監視し、<threshold> 秒以上更新されていない
#   Worker を検出して警告を出す。TASKVIA_TOKEN が設定されていれば Taskvia にもログ投稿。
#   Orchestrator の start.sh から自動起動される。直接実行も可。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HEARTBEAT_DIR="${REPO_ROOT}/registry/heartbeats"

THRESHOLD=600  # 10分
INTERVAL=60    # 1分

# 引数パース
while [[ $# -gt 0 ]]; do
  case "$1" in
    --threshold)
      THRESHOLD="$2"
      shift 2
      ;;
    --interval)
      INTERVAL="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# SIGTERM / SIGINT で正常終了
trap 'exit 0' TERM INT

echo "[watchdog] Started — threshold=${THRESHOLD}s, interval=${INTERVAL}s"

while true; do
  if [[ -d "$HEARTBEAT_DIR" ]]; then
    NOW=$(date +%s)
    for hb_file in "${HEARTBEAT_DIR}"/*; do
      [[ -f "$hb_file" ]] || continue
      name="$(basename "$hb_file")"
      last_beat="$(cat "$hb_file" 2>/dev/null || echo 0)"
      age=$(( NOW - last_beat ))

      if [[ "$age" -ge "$THRESHOLD" ]]; then
        echo "[watchdog] STALE: ${name} — last heartbeat ${age}秒前" >&2

        # Taskvia に alert を投稿（トークンがある場合のみ）
        if [[ -n "${TASKVIA_TOKEN:-}" ]]; then
          curl -s -X POST "${TASKVIA_URL:-https://taskvia.vercel.app}/api/log" \
            -H "Authorization: Bearer ${TASKVIA_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "{\"type\":\"alert\",\"agent\":\"watchdog\",\"content\":\"STALE: ${name} — last heartbeat ${age}秒前\"}" \
            >/dev/null 2>&1 || true
        fi
      fi
    done
  fi

  sleep "$INTERVAL"
done
