#!/usr/bin/env bash
# benchmark-ctx.sh: M-CTX-1 Worker context strategy benchmark orchestrator
#
# Runs 5 bench tasks with the specified context strategy and records
# per-task token usage, compaction events, and wall-clock time.
#
# Usage:
#   ./scripts/benchmark-ctx.sh --strategy A|B|C \
#     [--worker <name>] [--max-runtime-minutes <N>] [--dry-run]
#
# Strategies:
#   A — No context management (continuous session across all 5 tasks)
#   B — /clear between tasks (requires dispatcher.sh Strategy B support: t004)
#   C — Worker restart between tasks (requires dispatcher.sh Strategy C support: t005)
#
# Prerequisites:
#   - Strategies B and C require the crewvia dispatcher to be launched with
#     CREWVIA_BENCH_MODE=1 so the gate-file and .restarting guards take effect.
#     Without this env var, the dispatcher may race ahead and assign the next
#     task before the context strategy action has been applied.
#   - start.sh sets CREWVIA_BENCH_MODE=1 for the Worker launched by this script,
#     but the dispatcher must be restarted with the same env if already running.
#
# Output: registry/benchmarks/M-CTX-1/<timestamp>_strategy-<X>.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Constants ─────────────────────────────────────────────────────────────────

FIXTURE_DIR="$REPO_ROOT/bench/fixture"
BENCH_TASKS_DIR="$REPO_ROOT/bench/tasks"
OUTPUT_DIR="$REPO_ROOT/registry/benchmarks/M-CTX-1"
STRATEGY_CONF="/tmp/crewvia-bench-strategy.conf"
TMUX_SESSION="crewvia"
NUM_TASKS=5
WORKER_STARTUP_SLEEP=10   # seconds to wait for Claude to initialise
TASK_POLL_INTERVAL=10     # seconds between task-done polls

# ── Argument parsing ──────────────────────────────────────────────────────────

STRATEGY=""
WORKER_NAME=""
MAX_RUNTIME_MINUTES=60
DRY_RUN=0

usage() {
    cat >&2 <<'EOF'
Usage: benchmark-ctx.sh --strategy A|B|C [OPTIONS]

Options:
  --strategy A|B|C            Context management strategy (required)
  --worker <name>             Worker agent name (default: auto-assign)
  --max-runtime-minutes <N>   Safety timeout in minutes (default: 60)
  --dry-run                   Validate setup without executing Worker

Strategies:
  A  No context management — single session, no /clear or restart
  B  /clear between tasks — context window cleared after each task done
  C  Restart between tasks — Worker killed and re-launched after each task done
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --strategy)            STRATEGY="${2:?'--strategy requires A|B|C'}"; shift 2 ;;
        --worker)              WORKER_NAME="${2:?'--worker requires a name'}"; shift 2 ;;
        --max-runtime-minutes) MAX_RUNTIME_MINUTES="${2:?'--max-runtime-minutes requires N'}"; shift 2 ;;
        --dry-run)             DRY_RUN=1; shift ;;
        -h|--help)             usage ;;
        *)                     echo "Unknown argument: $1" >&2; usage ;;
    esac
done

[[ -z "$STRATEGY" ]] && { echo "ERROR: --strategy is required" >&2; usage; }
[[ ! "$STRATEGY" =~ ^[ABC]$ ]] && { echo "ERROR: --strategy must be A, B, or C (got '$STRATEGY')" >&2; exit 1; }

# ── Helpers ───────────────────────────────────────────────────────────────────

log()  { echo "[bench] $(date -u +%H:%M:%SZ) $*" >&2; }
die()  { echo "[bench] FATAL: $*" >&2; exit 1; }

now_iso() { date -u +"%Y-%m-%dT%H:%M:%S.000Z"; }

get_task_status() {
    local f="$1"
    [[ -f "$f" ]] || { echo "missing"; return; }
    grep '^status:' "$f" | head -1 | awk '{print $2}'
}

# ── Step 1: Resolve worker name ───────────────────────────────────────────────

if [[ -z "$WORKER_NAME" ]]; then
    WORKER_NAME=$(bash "$SCRIPT_DIR/assign-name.sh" "code" "bash" 2>/dev/null \
        || echo "BenchWorker")
fi

GATE_FILE="/tmp/crewvia-bench-gate-${WORKER_NAME}"
WORKER_WINDOW="${WORKER_NAME}-worker"
TMUX_TARGET="${TMUX_SESSION}:${WORKER_WINDOW}"
RESTARTING_FLAG="$REPO_ROOT/queue/assignments/${WORKER_NAME}.restarting"

log "strategy=$STRATEGY worker=$WORKER_NAME max_runtime=${MAX_RUNTIME_MINUTES}m dry_run=$DRY_RUN"

# ── Step 2: Reset fixture ─────────────────────────────────────────────────────

log "Resetting bench fixture..."
if [[ $DRY_RUN -eq 0 ]]; then
    bash "$REPO_ROOT/bench/init-fixture.sh" 2>&1 | sed 's/^/  /' >&2
else
    log "(dry-run) skipping fixture reset"
fi

# ── Step 3: Create bench mission ──────────────────────────────────────────────

BENCH_SLUG="bench-$(date -u +%Y%m%dT%H%M%S)"
log "Creating bench mission: $BENCH_SLUG"

MISSION_TASK_IDS=()   # filled below

if [[ $DRY_RUN -eq 0 ]]; then
    CREWVIA_TASKVIA=disabled \
        "$SCRIPT_DIR/plan.sh" init "M-CTX-1 Bench: Strategy $STRATEGY" \
        --mission "$BENCH_SLUG" >/dev/null 2>&1

    prev_id=""
    for i in $(seq 1 $NUM_TASKS); do
        BENCH_FILE="$BENCH_TASKS_DIR/bench-t00${i}.md"
        [[ -f "$BENCH_FILE" ]] || die "bench task file not found: $BENCH_FILE"

        TASK_TITLE=$(grep '^title:' "$BENCH_FILE" | head -1 \
            | sed 's/^title:[[:space:]]*//' | tr -d '"')
        TASK_BODY=$(cat "$BENCH_FILE")

        BLOCKED_ARG=""
        [[ -n "$prev_id" ]] && BLOCKED_ARG="--blocked-by $prev_id"

        # shellcheck disable=SC2086
        ADD_OUT=$(CREWVIA_TASKVIA=disabled \
            "$SCRIPT_DIR/plan.sh" add "$TASK_TITLE" \
            --mission "$BENCH_SLUG" \
            --skills "code,typescript" \
            --priority high \
            --target-dir "$FIXTURE_DIR" \
            --description "$TASK_BODY" \
            $BLOCKED_ARG 2>/dev/null)

        prev_id=$(echo "$ADD_OUT" | grep -oE '\bt[0-9]+\b' | head -1)
        MISSION_TASK_IDS+=("$prev_id")
        log "  task $i → $prev_id"
    done
else
    log "(dry-run) skipping mission creation"
    BENCH_SLUG="bench-dry-run"
    MISSION_TASK_IDS=(t001 t002 t003 t004 t005)
fi

# ── Step 4: Write strategy conf ───────────────────────────────────────────────

echo "$STRATEGY" > "$STRATEGY_CONF"

# ── Step 5: JSONL session tracking ────────────────────────────────────────────

# Claude Code stores sessions in ~/.claude/projects/<path-with-slashes-as-dashes>/
# Mapping: replace every '/' with '-' (the leading '/' becomes a leading '-').
FIXTURE_PATH_KEY=$(echo "$FIXTURE_DIR" | sed 's|/|-|g')
JSONL_DIR="$HOME/.claude/projects/$FIXTURE_PATH_KEY"
mkdir -p "$JSONL_DIR"

jsonl_snapshot() { ls "$JSONL_DIR"/*.jsonl 2>/dev/null | sort || true; }

new_jsonl_since() {
    # Returns the first JSONL file added since snapshot string $1
    local before="$1"
    local after
    after=$(jsonl_snapshot)
    comm -23 <(echo "$after") <(echo "$before") | head -1 || true
}

# ── Step 6: Worker launch helper ─────────────────────────────────────────────

_launch_worker_window() {
    local agent="$1" target_dir="$2"
    local window="${TMUX_SESSION}:${agent}-worker"

    if ! command -v tmux >/dev/null 2>&1; then
        die "tmux is required for benchmark execution"
    fi

    tmux has-session -t "$TMUX_SESSION" 2>/dev/null || \
        tmux new-session -d -s "$TMUX_SESSION" -n "_placeholder"

    tmux kill-window -t "$window" 2>/dev/null || true
    sleep 1

    # Write a minimal settings.json for the bench Worker.
    # The full Worker system prompt (~38KB) crashes claude when written to settings.json;
    # this compact version covers exactly what bench tasks need.
    mkdir -p "$target_dir/.claude"
    python3 - "$target_dir/.claude/settings.json" "$agent" "$REPO_ROOT" <<'PYEOF'
import sys, json
settings_path, agent_name, crewvia_repo = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    settings = json.load(open(settings_path))
except Exception:
    settings = {}
settings['systemPrompt'] = (
    f"You are {agent_name}, a crewvia bench Worker (skills: code, typescript).\n"
    "Your cwd is the bench/fixture TypeScript project. "
    f"CREWVIA_REPO env var = {crewvia_repo} (use this for plan.sh calls).\n\n"
    "For each bench task:\n"
    "1. Pull: $CREWVIA_REPO/scripts/plan.sh pull --task <id> --mission <mission>\n"
    "2. Read description; fix the TypeScript bug in src/\n"
    "3. Verify: npm test\n"
    "4. Commit: git add -A && git commit -m 'fix: <desc> (task/<id>)'\n"
    "5. Done: $CREWVIA_REPO/scripts/plan.sh done <id> '<summary>' --mission <mission>\n\n"
    "Always use absolute $CREWVIA_REPO prefix for plan.sh since cwd is bench/fixture."
)
# Allow all tools without permission prompts so the bench loop runs unblocked.
settings['permissions'] = {'allow': ['Bash(*)', 'Edit(*)', 'Write(*)', 'Read(*)', 'MultiEdit(*)']}
with open(settings_path, 'w') as f:
    json.dump(settings, f, ensure_ascii=False, indent=2)
PYEOF

    # Launch via start.sh — pass AGENT_NAME so start.sh uses it instead of assign-name.sh.
    # CREWVIA_BENCH_MODE=1 suppresses start.sh auto-kickoff and settings.json overwrite.
    AGENT_NAME="$agent" TARGET_DIR="$target_dir" CREWVIA_TMUX=1 CREWVIA_TASKVIA=disabled \
        CREWVIA_BENCH_MODE=1 bash "$SCRIPT_DIR/start.sh" worker code bash &
    disown $!

    log "Worker launched (window: $window). Waiting ${WORKER_STARTUP_SLEEP}s for Claude..."
    sleep "$WORKER_STARTUP_SLEEP"
}

# ── Step 7: Start initial Worker + identify session JSONL ─────────────────────

JSONL_BEFORE=$(jsonl_snapshot)
SESSION_FILE=""

if [[ $DRY_RUN -eq 0 ]]; then
    _launch_worker_window "$WORKER_NAME" "$FIXTURE_DIR"
    SESSION_FILE=$(new_jsonl_since "$JSONL_BEFORE")
    if [[ -n "$SESSION_FILE" ]]; then
        log "Session JSONL identified: $SESSION_FILE"
    else
        log "WARNING: could not identify session JSONL (Strategy C will track per-task)"
    fi
else
    log "(dry-run) skipping Worker start"
fi

# ── Step 8: Task execution loop ───────────────────────────────────────────────

TASK_MARKERS=()         # ISO timestamps at start-of-task boundaries
TASK_SESSION_FILES=()   # JSONL file used per task (changes on Strategy C restart)
TASKS_COMPLETED=0
CURRENT_SESSION_FILE="$SESSION_FILE"
RUN_START=$(date +%s)

_wait_for_task_done() {
    local mission="$1" tid="$2" timeout_secs="$3"
    local task_file="$REPO_ROOT/queue/missions/$mission/tasks/${tid}.md"
    local waited=0

    while [[ $waited -lt $timeout_secs ]]; do
        local st
        st=$(get_task_status "$task_file")
        [[ "$st" == "done" ]] && return 0
        sleep "$TASK_POLL_INTERVAL"
        waited=$(( waited + TASK_POLL_INTERVAL ))
    done
    return 1
}

_send_task_kickoff() {
    local task_id="$1" mission="$2"
    # Use $CREWVIA_REPO absolute path — Worker CWD is bench/fixture, not crewvia root
    local msg="タスク ${task_id} (mission=${mission}) を実行して。\$CREWVIA_REPO/scripts/plan.sh pull --task ${task_id} --mission ${mission} で取得後、作業→\$CREWVIA_REPO/scripts/plan.sh done で完了。"
    tmux send-keys -t "$TMUX_TARGET" "$msg" Enter
    log "Kickoff sent: $task_id"
}

_apply_strategy() {
    local strategy="$1" task_num="$2"

    case "$strategy" in
        A)
            log "Strategy A: no context action"
            ;;
        B)
            log "Strategy B: sending /clear to Worker..."
            touch "$GATE_FILE"
            tmux send-keys -t "$TMUX_TARGET" "/clear" Enter
            sleep 3
            rm -f "$GATE_FILE"
            log "Strategy B: context cleared, gate released"
            ;;
        C)
            log "Strategy C: restarting Worker..."
            touch "$GATE_FILE"
            touch "$RESTARTING_FLAG"

            local before_restart
            before_restart=$(jsonl_snapshot)

            tmux kill-window -t "$TMUX_TARGET" 2>/dev/null || true
            sleep 2

            _launch_worker_window "$WORKER_NAME" "$FIXTURE_DIR"

            local new_sf
            new_sf=$(new_jsonl_since "$before_restart")
            if [[ -n "$new_sf" ]]; then
                CURRENT_SESSION_FILE="$new_sf"
                log "Strategy C: new session JSONL: $CURRENT_SESSION_FILE"
            fi

            rm -f "$RESTARTING_FLAG" "$GATE_FILE"
            log "Strategy C: Worker restarted, gate released"
            ;;
    esac
}

for idx in "${!MISSION_TASK_IDS[@]}"; do
    TASK_ID="${MISSION_TASK_IDS[$idx]}"
    TASK_NUM=$(( idx + 1 ))

    # Safety: check remaining runtime before each task
    NOW=$(date +%s)
    ELAPSED=$(( NOW - RUN_START ))
    REMAINING=$(( MAX_RUNTIME_MINUTES * 60 - ELAPSED ))
    if [[ $REMAINING -le 0 ]]; then
        log "Max runtime exceeded before task $TASK_NUM — saving intermediate results"
        break
    fi

    # Record task start boundary (used as segment boundary by parse-cc-log.py)
    MARKER_TS=$(now_iso)
    TASK_MARKERS+=("$MARKER_TS")
    TASK_SESSION_FILES+=("${CURRENT_SESSION_FILE:-}")

    log "Task $TASK_NUM/$NUM_TASKS: $TASK_ID | remaining=${REMAINING}s"

    if [[ $DRY_RUN -eq 1 ]]; then
        log "(dry-run) simulating task $TASK_NUM"
        sleep 1
        TASKS_COMPLETED=$(( TASKS_COMPLETED + 1 ))
        continue
    fi

    _send_task_kickoff "$TASK_ID" "$BENCH_SLUG"

    if ! _wait_for_task_done "$BENCH_SLUG" "$TASK_ID" "$REMAINING"; then
        log "Timeout waiting for task $TASK_NUM ($TASK_ID) — saving intermediate results"
        break
    fi

    TASKS_COMPLETED=$(( TASKS_COMPLETED + 1 ))
    log "Task $TASK_NUM done."

    # Apply strategy between tasks (not after the last task)
    if [[ $TASK_NUM -lt $NUM_TASKS ]]; then
        _apply_strategy "$STRATEGY" "$TASK_NUM"
    fi
done

# ── Step 9: Collect metrics via parse-cc-log.py ───────────────────────────────

mkdir -p "$OUTPUT_DIR"
TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
RESULT_FILE="$OUTPUT_DIR/${TIMESTAMP}_strategy-${STRATEGY}.json"

# Build task_markers JSON array
if [[ ${#TASK_MARKERS[@]} -gt 0 ]]; then
    MARKERS_JSON=$(printf '"%s"\n' "${TASK_MARKERS[@]}" | jq -s '.')
else
    MARKERS_JSON="[]"
fi

_collect_metrics() {
    # Strategy C: each task runs in its own session → parse each file separately
    if [[ "$STRATEGY" == "C" ]]; then
        local arr="[]"
        for i in "${!TASK_SESSION_FILES[@]}"; do
            local sf="${TASK_SESSION_FILES[$i]:-}"
            local seg="{}"
            if [[ -n "$sf" && -f "$sf" ]]; then
                seg=$(python3 "$SCRIPT_DIR/parse-cc-log.py" \
                    --session-file "$sf" 2>/dev/null \
                    | jq '.[0] // {}' || echo "{}")
            fi
            arr=$(echo "$arr" | jq --argjson s "$seg" '. + [$s]')
        done
        echo "$arr"
        return
    fi

    # Strategy A/B: single session with task_markers for segmentation
    local sf="${TASK_SESSION_FILES[0]:-}"
    if [[ -n "$sf" && -f "$sf" && ${#TASK_MARKERS[@]} -gt 0 ]]; then
        python3 "$SCRIPT_DIR/parse-cc-log.py" \
            --session-file "$sf" \
            --task-markers "$MARKERS_JSON" 2>/dev/null || echo "[]"
    else
        echo "[]"
    fi
}

if [[ $DRY_RUN -eq 0 ]]; then
    METRICS=$(_collect_metrics)
else
    METRICS="[]"
fi

# ── Step 10: Write result JSON ─────────────────────────────────────────────────

jq -n \
    --arg     strategy         "$STRATEGY" \
    --arg     worker           "$WORKER_NAME" \
    --arg     run_at           "$TIMESTAMP" \
    --argjson max_runtime      "$MAX_RUNTIME_MINUTES" \
    --argjson dry_run          "$DRY_RUN" \
    --argjson tasks_completed  "$TASKS_COMPLETED" \
    --arg     bench_mission    "$BENCH_SLUG" \
    --arg     session_file     "${SESSION_FILE:-}" \
    --argjson task_markers     "$MARKERS_JSON" \
    --argjson metrics          "$METRICS" \
    '{
        strategy:            $strategy,
        worker:              $worker,
        run_at:              $run_at,
        max_runtime_minutes: $max_runtime,
        dry_run:             ($dry_run == 1),
        tasks_completed:     $tasks_completed,
        bench_mission:       $bench_mission,
        session_file:        (if $session_file == "" then null else $session_file end),
        task_markers:        $task_markers,
        metrics:             $metrics
    }' > "$RESULT_FILE"

log "Results → $RESULT_FILE"
echo "$RESULT_FILE"

# ── Step 11: Cleanup ──────────────────────────────────────────────────────────

rm -f "$STRATEGY_CONF" "$GATE_FILE" "$RESTARTING_FLAG" 2>/dev/null || true

if [[ $DRY_RUN -eq 0 ]]; then
    CREWVIA_TASKVIA=disabled \
        "$SCRIPT_DIR/plan.sh" archive "$BENCH_SLUG" >/dev/null 2>&1 || true
fi

log "Done. tasks_completed=$TASKS_COMPLETED/$NUM_TASKS result=$RESULT_FILE"
