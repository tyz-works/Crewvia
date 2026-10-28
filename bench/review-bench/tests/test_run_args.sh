#!/usr/bin/env bash
# run.sh の引数解析 (--dry-run だけ。claude / agy は呼ばない)
set -u
RUN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/run.sh"
fail=0
check() { if [[ "$2" != "$3" ]]; then echo "FAIL $1: expected '$2' got '$3'"; fail=1; fi; }

bash "$RUN" --engine claude --dry-run >/dev/null 2>&1; check all-ok 0 $?
check count-15 15 "$(bash "$RUN" --engine agy --dry-run | tail -n +2 | wc -l)"
check one-round "pr314-r2" "$(bash "$RUN" --engine claude --rounds pr314-r2 --dry-run | tail -n 1)"
bash "$RUN" --dry-run >/dev/null 2>&1; check no-engine 2 $?
bash "$RUN" --engine codex --dry-run >/dev/null 2>&1; check bad-engine 2 $?
bash "$RUN" --engine claude --rounds nope --dry-run >/dev/null 2>&1; check bad-round 2 $?
bash "$RUN" --engine claude --bogus >/dev/null 2>&1; check bad-arg 2 $?
bash "$RUN" --engine claude --rounds >/dev/null 2>&1; check missing-value 2 $?
[[ $fail -eq 0 ]] && echo OK
exit $fail
