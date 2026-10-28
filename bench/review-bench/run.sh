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
set -uo pipefail

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
# 相対指定は clone への cd の前に絶対パスにする (cd 後は基準の dir が変わる)
src="$(realpath -m -- "$src")" || die "cannot resolve --src"
out="$(realpath -m -- "$out")" || die "cannot resolve --out"

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

# clone が head sha にあり、変更・未追跡・ignored が無いこと
clone_ok() {
  [[ "$(git -C "$1" rev-parse HEAD 2>/dev/null)" == "$2" ]] || return 1
  local st
  st="$(git -C "$1" status --porcelain --ignored 2>/dev/null)" || return 1   # status 自体の失敗は汚れ扱い
  [[ -z "$st" ]] || return 1
}

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
  git -C "$R" diff --no-color "$base...HEAD" > "$DD/review.diff" || return 94
  git -C "$R" log --no-color --format='%h %s' "$base..HEAD" > "$DD/review.log" || return 94
  [[ -s $DD/review.diff ]] || return 94
  # 採点対象 PR (#312-#314) の事例を除いた版を正本から生成する
  bash "$KIT/gen_checklist.sh" > "$DD/review-attack-checklist.md" || return 96
  # engine の前に、clone が HEAD=head sha かつ clean であること
  clone_ok "$R" "$head" || return 95
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
  rm -f "$out/$label.$engine.json.tmp"   # 前回の途中の残り
  if [[ $engine == claude ]]; then
    local schema; schema="$(cat "$KIT/schema.json")"
    # 汚染検査をこの実行の session 1 ファイルに縛るため、UUID を渡して記録する
    local sid; sid="$(python3 -c 'import uuid; print(uuid.uuid4())')" || return 98
    echo "$sid" > "$out/$label.claude.session"
    # prompt は stdin で渡す (可変長 option が prompt を食う / diff を argv に載せない)
    printf '%s' "$prompt$extra" | ( cd "$R" && env -i PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}" TERM=dumb \
      timeout --kill-after=10s 900s claude -p --session-id "$sid" --output-format json --json-schema "$schema" --permission-mode plan \
      --model claude-opus-5-5 --setting-sources user --strict-mcp-config \
      --add-dir "$DD" --allowedTools "Read($R/**)" "Read($DD/**)" "Grep" "Glob" \
      --disallowedTools Bash Write Edit MultiEdit NotebookEdit WebFetch WebSearch Task Agent ExitPlanMode ) \
      > "$out/$label.claude.raw" 2> "$out/$label.claude.err"
    rc=$?; echo "rc=$rc" > "$out/$label.claude.rc"
    if [[ $rc -eq 0 ]]; then
      PYTHONPATH="$src/scripts" python3 "$src/scripts/lib_review_engine.py" extract-claude \
        --out "$out/$label.claude.raw" --schema "$KIT/schema.json" > "$out/$label.claude.json.tmp" 2>> "$out/$label.claude.err" \
        || { echo extract_failed >> "$out/$label.claude.rc"; rm -f "$out/$label.claude.json.tmp"; }
    fi
  else
    rm -f "$out/$label.agy.json" "$out/$label.agy.json.tmp"
    ( cd "$R" && agy --mode plan --model gemini-4-argon-high --output-format json --json-schema "$KIT/schema.json" \
        --add-dir "$DD" --print-timeout 600s -p "$prompt$extra" ) > "$out/$label.agy.raw" 2> "$out/$label.agy.err"
    rc=$?; echo "rc=$rc" > "$out/$label.agy.rc"
    if [[ $rc -eq 0 ]]; then
      # 検証は本番 lib (extract-agy: status SUCCESS・denied_actions 空・重複キー拒否・schema) に一本化。bench で再実装しない。
      # 通っても一時名のまま。clone の事後検査が通ってから公開する (公開 = 次回から「済み」で飛ばされる)
      if PYTHONPATH="$src/scripts" python3 "$src/scripts/lib_review_engine.py" extract-agy \
          --out "$out/$label.agy.raw" --schema "$KIT/schema.json" \
          > "$out/$label.agy.json.tmp" 2>> "$out/$label.agy.err"; then
        :
      else
        echo agy_invalid >> "$out/$label.agy.rc"; rm -f "$out/$label.agy.json.tmp"
      fi
    fi
  fi
  # 読み取り専用のはずなので、engine の後も HEAD=head sha かつ clean。ずれたら結果を捨てる (採点しない)
  git -C "$R" status --porcelain --ignored | head -3 > "$out/$label.$engine.dirty"
  if ! clone_ok "$R" "$head"; then
    rm -f "$out/$label.$engine.json" "$out/$label.$engine.json.tmp"; echo tainted_clone >> "$out/$label.$engine.rc"
    return 97
  fi
  # 検査が通った結果だけを公開する (resume は公開済みの .json だけを done とみなす)
  if [[ -s "$out/$label.$engine.json.tmp" ]]; then
    mv -f "$out/$label.$engine.json.tmp" "$out/$label.$engine.json" || return 99
  fi
  return 0
}

for label in "${sel[@]}"; do
  if [[ -s "$out/$label.$engine.json" ]]; then echo "skip $label (done)"; continue; fi
  run_round "$label"; st=$?
  echo "$(date -u +%T) $label $engine setup=$st $(cat "$out/$label.$engine.rc" 2>/dev/null)" | tee -a "$out/progress.log"
done
