#!/usr/bin/env bash
# hooks/notification.sh
# Claude Code Notification hook — PreToolUse 承認プロンプト漏れの即時検知基盤
#
# ~/.claude/settings.json に登録:
#   "hooks": {
#     "Notification": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "/path/to/hooks/notification.sh" }] }]
#   }
#
# 環境変数:
#   CLAUDE_AGENT_NAME — エージェント識別子（未設定時は "unknown"）
#
# 動作:
#   stdin から Notification hook の JSON ペイロードを受け取り、
#   registry/notifications/<agent_name>/<unix_timestamp>.txt に書き出す。
#   PreToolUse 承認プロンプトが TUI に漏れた場合、このファイルを監視することで
#   即時検知できる（2026-04-14 事案の再発防止）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AGENT_NAME="${CLAUDE_AGENT_NAME:-unknown}"
NOTIFICATIONS_DIR="${REPO_ROOT}/registry/notifications/${AGENT_NAME}"

# 書き出し先ディレクトリを作成（なければ）
mkdir -p "$NOTIFICATIONS_DIR"

# stdin からペイロードを読む
INPUT="$(cat)"

# タイムスタンプ（Unix エポック秒）
TS="$(date +%s)"

# ペイロードを <timestamp>.txt に書き出す
printf '%s\n' "$INPUT" > "${NOTIFICATIONS_DIR}/${TS}.txt"

exit 0
