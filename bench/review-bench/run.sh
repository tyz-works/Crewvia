#!/usr/bin/env bash
# レビュー採点キットの runner。巡ごとに隔離 repo (その head までの履歴だけ・後の ref なし) を作り、
# engine (claude / agy) で 1 回レビューさせる。本番の queue / registry / config には書かない。
#
# usage: run.sh --engine claude|agy [--rounds all|pr314-r2,pr313-r1] [--src <repo>] [--out <dir>] [--dry-run]
#   --src   巡の commit を持つ repo (既定: この kit のある checkout)
#   --out   出力先 (既定: <kit>/out。commit しない)
#   --dry-run  実行せず、走らせる巡と出力先だけ表示する
# 出力: <out>/<label>.<engine>.{raw,json,err,rc,dirty}。既に <label>.<engine>.json があれば飛ばす (再開できる)
# diff は argv に載せない (ファイルで渡す)。ここは claude / agy のログインが要る → CI では回さない
set -u

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
engine="" rounds="all" src="" out="" dry=0
usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; }
die() { echo "run.sh: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --engine) [[ $# -ge 2 ]] || die "--engine needs a value"; engine=$2; shift 2 ;;
    --rounds) [[ $# -ge 2 ]] || die "--rounds needs a value"; rounds=$2; shift 2 ;;
    --src) [[ $# -ge 2 ]] || die "--src needs a value"; src=$2; shift 2 ;;
    --out) [[ $# -ge 2 ]] || die "--out needs a value"; out=$2; shift 2 ;;
    --dry-run) dry=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done
case "$engine" in claude|agy) ;; *) usage; die "--engine must be claude or agy" ;; esac
[[ -n "$src" ]] || src="$(git -C "$KIT" rev-parse --show-toplevel)" || die "cannot find repo"
[[ -n "$out" ]] || out="$KIT/out"

# 巡の一覧 (rounds.tsv: label<TAB>head_sha)
mapfile -t all_lines < <(grep -v '^#' "$KIT/rounds.tsv" | grep -v '^$')
declare -A head_of; labels=()
for l in "${all_lines[@]}"; do
  IFS=$'\t' read -r lab sha <<<"$l"
  head_of[$lab]=$sha; labels+=("$lab")
done
if [[ "$rounds" == all ]]; then
  sel=("${labels[@]}")
else
  IFS=, read -r -a sel <<<"$rounds"
  for r in "${sel[@]}"; do [[ -n "${head_of[$r]:-}" ]] || die "unknown round: $r"; done
fi

if [[ $dry -eq 1 ]]; then
  echo "engine=$engine src=$src out=$out"
  printf '%s\n' "${sel[@]}"
  exit 0
fi

command -v "$engine" >/dev/null || die "$engine not found in PATH"
mkdir -p "$out/rounds" "$out/diffs" || die "cannot create $out"
UP='git -c uploadpack.allowAnySHA1InWant=true upload-pack'

run_round() {
  local label=$1 head=${head_of[$1]}
  local R="$out/rounds/$label" DD="$out/diffs/$label.$engine" base
  if [[ ! -f $R/.git/base_sha ]]; then
    mkdir -p "$R"; [[ -d $R/.git ]] || git -C "$R" init -q
    git -C "$R" fetch -q --no-tags --upload-pack "$UP" "$src" "$head" || return 90
    git -C "$R" checkout -q --detach "$head" || return 91
    base=$(git -C "$src" merge-base "$head" origin/main) || return 93
    git -C "$R" fetch -q --no-tags --upload-pack "$UP" "$src" "$base" || return 92
    echo "$base" > "$R/.git/base_sha"
  fi
  base=$(cat "$R/.git/base_sha")
  mkdir -p "$DD"
  git -C "$R" diff --no-color "$base...HEAD" > "$DD/review.diff"
  git -C "$R" log --no-color --format='%h %s' "$base..HEAD" > "$DD/review.log"
  cp "$KIT/checklist.md" "$DD/review-attack-checklist.md"
  local prompt extra
  prompt="$(sed "s#the <stdin> block below#the file $DD/review.diff#" "$KIT/prompt.txt")"
  extra="

You are an independent, adversarial reviewer — not the author of this change (a different agent wrote it).
Your job is to find what breaks. Try to construct concrete failing inputs, states and command sequences.
The review checklist is in $DD/review-attack-checklist.md — read it first and apply it.

Every finding's \"body\" MUST include either concrete reproduction steps or the shape of a failing test
(inputs, the command or call, the expected vs. the actual result). If you cannot write one, do not report it.

Read the unified diff from the file $DD/review.diff (it is outside this checkout; read it as a file).
The commits of this change (git log from the base to HEAD) are in $DD/review.log.
Read files in this checkout (Read / Grep / Glob) to verify the code. Read ONLY files inside this checkout ($R) and $DD; do not look anywhere else on the filesystem.
Shell commands are not available. Do not create, modify or delete any file. Return only the JSON object required by the schema."
  local rc
  if [[ $engine == claude ]]; then
    local schema; schema="$(cat "$KIT/schema.json")"
    # prompt は stdin で渡す (可変長 option が prompt を食う / diff を argv に載せない)
    printf '%s' "$prompt$extra" | ( cd "$R" && env -i PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}" TERM=dumb \
      timeout --kill-after=10s 900s claude -p --output-format json --json-schema "$schema" --permission-mode plan \
      --model claude-opus-5-5 --setting-sources user --strict-mcp-config \
      --add-dir "$DD" --allowedTools "Read($R/**)" "Read($DD/**)" "Grep" "Glob" \
      --disallowedTools Bash Write Edit MultiEdit NotebookEdit WebFetch WebSearch Task Agent ExitPlanMode ) \
      > "$out/$label.claude.raw" 2> "$out/$label.claude.err"
    rc=$?; echo "rc=$rc" > "$out/$label.claude.rc"
    if [[ $rc -eq 0 ]]; then
      PYTHONPATH="$src/scripts" python3 "$src/scripts/lib_review_engine.py" extract-claude \
        --out "$out/$label.claude.raw" --schema "$KIT/schema.json" > "$out/$label.claude.json" 2>> "$out/$label.claude.err" \
        || echo extract_failed >> "$out/$label.claude.rc"
    fi
  else
    ( cd "$R" && agy --mode plan --model gemini-4-argon-high --output-format json --json-schema "$KIT/schema.json" \
        --add-dir "$DD" --print-timeout 600s -p "$prompt$extra" ) > "$out/$label.agy.raw" 2> "$out/$label.agy.err"
    rc=$?; echo "rc=$rc" > "$out/$label.agy.rc"
    [[ $rc -eq 0 ]] && jq -c '.structured_output' "$out/$label.agy.raw" > "$out/$label.agy.json" 2>> "$out/$label.agy.err"
  fi
  # 読み取り専用のはずなので、clone に変更が無いこと
  git -C "$R" status --porcelain --ignored | head -3 > "$out/$label.$engine.dirty"
  return 0
}

for label in "${sel[@]}"; do
  if [[ -s "$out/$label.$engine.json" ]]; then echo "skip $label (done)"; continue; fi
  run_round "$label"; st=$?
  echo "$(date -u +%T) $label $engine setup=$st $(cat "$out/$label.$engine.rc" 2>/dev/null)" | tee -a "$out/progress.log"
done
