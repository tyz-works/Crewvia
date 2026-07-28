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
#   AGENT_NAME — エージェント識別子 (default: hostname)
#
# task_162 P3是正: 以前は CLAUDE_AGENT_NAME という、crewvia のどこにも設定されない
# 変数名を読んでおり、全ての通知が registry/notifications/unknown/ に落ちていた
# (pre-tool-use.sh:47・post-tool-use.sh:21 は共に AGENT_NAME を読む。start.sh が
# export するのも AGENT_NAME であり、他hookと同じ流儀に揃えた)。
#
# 動作:
#   stdin から Notification hook の JSON ペイロードを受け取り、
#   registry/notifications/<agent_name>/<unix_timestamp>.txt に書き出す。
#   PreToolUse 承認プロンプトが TUI に漏れた場合、このファイルを監視することで
#   即時検知できる（2026-04-14 事案の再発防止）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AGENT_NAME="${AGENT_NAME:-$(hostname -s)}"
NOTIFICATIONS_DIR="${REPO_ROOT}/registry/notifications/${AGENT_NAME}"

# 書き出し先ディレクトリを作成（なければ）
mkdir -p "$NOTIFICATIONS_DIR"

# stdin からペイロードを読む
INPUT="$(cat)"

# タイムスタンプ（秒精度衝突防止: epoch_pid_random）
TS="$(date +%s)_${BASHPID:-$$}_${RANDOM}"

# ペイロードを <timestamp>.txt に書き出す
printf '%s\n' "$INPUT" > "${NOTIFICATIONS_DIR}/${TS}.txt"

exit 0
