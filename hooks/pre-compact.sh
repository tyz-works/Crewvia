#!/usr/bin/env bash
# PreCompact hook — saves task state before context compaction
#
# card への書き込みは `plan.sh snapshot` だけが行う (S5 / t020)。以前はこの hook が card 全体を
# ロックなし・in-place (`open('w')`) で書き戻していたので、(1) 読んでから書くまでの間に done が
# 進めた status を古い内容で巻き戻し、(2) 書いている最中に強制終了されると card が途中で残り、
# (3) task id だけで探すので別 mission の同じ tNNN に書きえた。
# 書き込み先は `snapshot --self` (AGENT_NAME の assignment) が決める。別 mission の同じ tNNN には書かない。

set -euo pipefail

CREWVIA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUEUE_DIR="${CREWVIA_QUEUE:-${CREWVIA_ROOT}/queue}"

# Read JSON payload from stdin
payload="$(cat)"
trigger="$(echo "$payload" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('trigger','unknown'))" 2>/dev/null || echo "unknown")"
custom_instructions="$(echo "$payload" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('custom_instructions',''))" 2>/dev/null || echo "")"

# task は pull のあとに決まるので env には無い。`plan.sh snapshot --self` が AGENT_NAME の assignment から
# mission / task / execution_id を決め、自分の in_progress の card だけに書く (決められなければ書かず拒否)。
agent="${AGENT_NAME:-unknown}"
timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# Build snapshot content
snapshot="## Pre-Compact Snapshot

- trigger: ${trigger}
- agent: ${agent}
- timestamp: ${timestamp}"

if [[ "$trigger" == "manual" && -n "$custom_instructions" ]]; then
    snapshot="${snapshot}
- custom_instructions: ${custom_instructions}"
fi

snapshot="${snapshot}

> Worker: compaction 後の resume 時はこのセクションを読んで作業を再開すること。
"

written=0
if [[ -n "${AGENT_NAME:-}" ]]; then
    # 本文は stdin (`--section-file -`) で渡す (引数に埋めると custom_instructions 中の記号がシェルに解釈される)
    if printf '%s' "$snapshot" \
        | CREWVIA_QUEUE="$QUEUE_DIR" bash "${CREWVIA_ROOT}/scripts/plan.sh" snapshot --self \
            --section-file - >/dev/null 2>&1; then
        written=1
    fi
fi

if [[ "$written" -ne 1 ]]; then
    # Fallback: log to pre-compact-fallback.log
    fallback_log="${QUEUE_DIR}/pre-compact-fallback.log"
    printf '[%s] trigger=%s agent=%s (task file not found or snapshot refused)\n' \
        "$timestamp" "$trigger" "$agent" >> "$fallback_log" || true
fi

exit 0
