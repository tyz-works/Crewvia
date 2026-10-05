#!/usr/bin/env bash
# ask_user.sh — Director がユーザーに Telegram でボタン付きの質問を送る入口 (PR-A)。
#
#   scripts/ask_user.sh ask --question "<本文>" --option "<ラベル>" [--option …] \
#       [--task <slug>/<tid>] [--ttl-minutes N] [--session-link <URL>]
#   scripts/ask_user.sh verify --q <qid>
#   scripts/ask_user.sh cancel --q <qid> --by <screen|other>
#   scripts/ask_user.sh list
#
# 薄いラッパー。ロジックは scripts/lib_telegram.py の同名の動詞に 1 か所だけ (bash に JSON を組み立てない)。
# 引数は厳格 (未知のオプションは終了コード 1。exit 2 は使わない)。本文・token・URL は終了時のエラーに出さない。
#
# `ask` の終了コード: 0 = 送信成功 (標準出力は qid 1 行) / 3 = Telegram 未設定 (AskUserQuestion に戻る) /
#   4 = 送信できない・受信側が無効 / 古い / 不明 / 別の bot (stderr に固定コード。同じく戻る) /
#   5 = 未回答の質問が上限 (8 件) / 1 = 使い方の誤り。
# 設計: knowledge/director-escalation-telegram.md §4。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-}" in
  ask|verify|cancel|list) ;;
  *)
    echo "usage: ask_user.sh ask|verify|cancel|list ... (see header)" >&2
    exit 1
    ;;
esac

exec python3 "${SCRIPT_DIR}/lib_telegram.py" "$@"
