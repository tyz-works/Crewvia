#!/usr/bin/env python3
"""
parse-cc-log.py: Extract per-task metrics from Claude Code CLI session JSONL.

Usage:
    python3 parse-cc-log.py --session-file <path> [--task-markers <json>]

Arguments:
    --session-file  Path to the .jsonl session log file.
    --task-markers  JSON array of ISO timestamp strings defining task boundaries.
                    E.g. '["2026-04-30T05:00:00.000Z", "2026-04-30T06:00:00.000Z"]'
                    Creates N+1 segments from N boundary points.
                    Omit to treat the entire session as a single task.

Output: JSON array, one object per segment:
    [
      {
        "segment": 0,
        "start_time": "2026-04-30T05:43:18.332Z",  // first msg timestamp (null if empty)
        "end_time":   "2026-04-30T06:00:00.000Z",  // last msg timestamp
        "input_tokens": 3,
        "cache_creation_input_tokens": 30133,
        "cache_read_input_tokens": 14230,
        "output_tokens": 155,
        "compaction_count": 0,
        "compaction_duration_ms": 0,
        "wall_clock_seconds": 1001.668,
        "turn_count": 4
      },
      ...
    ]

Token fields are summed from assistant message.usage across all turns in the segment.
Compaction fields are derived from type==system && subtype==compact_boundary entries.
"""

import argparse
import json
import sys
from datetime import datetime, timezone


def _parse_ts(ts_str: str) -> float:
    """Convert ISO 8601 timestamp string to Unix seconds (float)."""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()


def _to_iso(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _segment_index(ts: float, boundaries: list) -> int:
    """Return which segment a timestamp belongs to given sorted boundary list."""
    for i, b in enumerate(boundaries):
        if ts < b:
            return i
    return len(boundaries)


def parse_session(session_file: str, task_markers: list) -> list:
    boundaries = sorted(_parse_ts(m) for m in task_markers)
    n_segments = len(boundaries) + 1

    segs = [
        {
            "segment": i,
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "compaction_count": 0,
            "compaction_duration_ms": 0,
            "turn_count": 0,
            "_first_ts": None,
            "_last_ts": None,
        }
        for i in range(n_segments)
    ]

    def _touch(seg: dict, ts: float) -> None:
        if seg["_first_ts"] is None or ts < seg["_first_ts"]:
            seg["_first_ts"] = ts
        if seg["_last_ts"] is None or ts > seg["_last_ts"]:
            seg["_last_ts"] = ts

    with open(session_file) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_str = obj.get("timestamp")
            if not ts_str:
                continue

            ts = _parse_ts(ts_str)
            seg = segs[_segment_index(ts, boundaries)]
            _touch(seg, ts)

            msg_type = obj.get("type", "")
            msg_subtype = obj.get("subtype", "")

            if msg_type == "assistant":
                seg["turn_count"] += 1
                usage = obj.get("message", {}).get("usage") or {}
                seg["input_tokens"] += usage.get("input_tokens") or 0
                seg["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens") or 0
                seg["cache_read_input_tokens"] += usage.get("cache_read_input_tokens") or 0
                seg["output_tokens"] += usage.get("output_tokens") or 0

            elif msg_type == "system" and msg_subtype == "compact_boundary":
                seg["compaction_count"] += 1
                meta = obj.get("compactMetadata") or {}
                seg["compaction_duration_ms"] += meta.get("durationMs") or 0

    result = []
    for seg in segs:
        first_ts = seg.pop("_first_ts")
        last_ts = seg.pop("_last_ts")

        if first_ts is not None:
            seg["start_time"] = _to_iso(first_ts)
            seg["end_time"] = _to_iso(last_ts)
            seg["wall_clock_seconds"] = round(last_ts - first_ts, 3)
        else:
            seg["start_time"] = None
            seg["end_time"] = None
            seg["wall_clock_seconds"] = 0.0

        result.append(seg)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract per-task token usage and compaction metrics from a Claude Code JSONL session log."
    )
    parser.add_argument("--session-file", required=True, help="Path to the .jsonl session log file.")
    parser.add_argument(
        "--task-markers",
        default=None,
        help=(
            'JSON array of ISO timestamp strings defining task segment boundaries. '
            'Omit to treat the entire session as one segment.'
        ),
    )
    args = parser.parse_args()

    task_markers = []
    if args.task_markers:
        try:
            task_markers = json.loads(args.task_markers)
        except json.JSONDecodeError as exc:
            print(f"Error: --task-markers is not valid JSON: {exc}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(task_markers, list):
            print("Error: --task-markers must be a JSON array.", file=sys.stderr)
            sys.exit(1)

    try:
        result = parse_session(args.session_file, task_markers)
    except FileNotFoundError:
        print(f"Error: session file not found: {args.session_file}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
