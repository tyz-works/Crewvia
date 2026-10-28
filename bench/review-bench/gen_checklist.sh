#!/usr/bin/env bash
# 正本 (knowledge/review-attack-checklist.md) から、採点対象 PR (#312-#314) の事例を除いた checklist を stdout に出す。
# reviewer が正解表の答えを読めないようにするため (型の一般論は残す)。
# usage: gen_checklist.sh [<正本のパス>]   正本が読めない・空なら exit 非0
set -eo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="${1:-$KIT/../../knowledge/review-attack-checklist.md}"
[[ -s $src ]] || { echo "gen_checklist.sh: cannot read $src" >&2; exit 2; }
# PR 番号 #312-#314 を含む行 (事例の箇条書き) を落とす
out="$(grep -vE '#31[234]([^0-9]|$)' "$src")"
[[ -n $out ]] || { echo "gen_checklist.sh: empty output" >&2; exit 3; }
printf '%s\n' "$out"
